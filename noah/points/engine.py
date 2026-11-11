"""Versioned points evaluator shared by previews and explicit runtime opt-in."""

from dataclasses import asdict, replace

from ..scoring.fixture_io import parse_fixture
from ..scoring.pay import weights_u16
from . import ema_policy, onboarding
from .amendment import INPUT_SCHEMA as AMENDED_INPUT_SCHEMA
from .amendment import POLICY as AMENDED_POLICY
from .ledger import State, settle

POLICY = "noah-points-402040-30d/2"
INPUT_SCHEMA = "noah-points-fixture/1"


def legacy_shape(raw):
    # For points, verified earnings are authoritative. Preserve the original
    # evidence; only the internal legacy parser view neutralizes its old flag.
    data = (
        {**raw, "schema": "noah-fixture/6"}
        if raw.get("schema") in (INPUT_SCHEMA, AMENDED_INPUT_SCHEMA, ema_policy.INPUT_SCHEMA)
        else dict(raw)
    )
    if isinstance(raw.get("miners"), list) and all(
        isinstance(m, dict) and isinstance(m.get("days"), dict) for m in raw["miners"]
    ):
        data["miners"] = [
            {
                **m,
                "days": {
                    day: {**v, "noFault": False}
                    if isinstance(v, dict) and v.get("noFault") is True
                    else v
                    for day, v in m["days"].items()
                },
            }
            for m in raw["miners"]
        ]
    data.pop("pointsExcludedUids", None)
    data.pop("pointsRules", None)
    data.pop("pointsMigration", None)
    data.pop("pointsReceipts", None)
    data.pop("pointsHotkeys", None)
    data.pop("pointsOwners", None)
    data.pop("pointsPricing", None)
    data.pop("pointsPricingBoundary", None)
    return data


def evaluate(raw):
    rules = onboarding.validate(raw["pointsRules"]) if "pointsRules" in raw else None
    excluded = onboarding.excluded(raw) if rules else set()
    if not rules and "pointsExcludedUids" in raw:
        raise ValueError("owner exclusions require ordinary onboarding")
    from .receipts import validate as validate_receipts

    ema = raw.get("schema") == ema_policy.INPUT_SCHEMA
    amended = ema or raw.get("schema") == AMENDED_INPUT_SCHEMA
    if not ema and any(k in raw for k in ("pointsPricing", "pointsPricingBoundary")):
        raise ValueError("old policy cannot carry EMA pricing")
    if not amended and any(
        key in raw for key in ("pointsReceipts", "pointsHotkeys", "pointsOwners")
    ):
        raise ValueError("old policy cannot carry amended dispositions")
    dispositions = validate_receipts(raw.get("pointsReceipts"), ema=ema) if amended else None
    fixture = parse_fixture(legacy_shape(raw))
    if amended:
        from .receipts import reconcile_fixture

        reconcile_fixture(raw, dispositions)
    if ema:
        from .ema_reference import validate_fixture

        validate_fixture(raw)
    if fixture.dials.target_fp != 500000000 or fixture.dials.ongoing_hold_rao != 0:
        raise ValueError("points candidate requires 50% and no holding gate")
    if fixture.eval_day > 2**53 - 1:
        raise ValueError("unsafe day")
    cutoff, balances = migration_balances(raw, fixture)
    miners = []
    scores = {}
    for miner in fixture.miners:
        if miner.uid is None:
            continue
        state = replay_state(
            miner, fixture.eval_day, cutoff=cutoff, balance=balances.get(miner.uid), rules=rules
        )
        if miner.uid in excluded:
            state = replace(state, points=0, last_compliant=0)
        scores[miner.uid] = state.points
        miners.append(
            dict(
                uid=miner.uid,
                coldkey=miner.coldkey,
                state={k: str(v) for k, v in asdict(state).items()},
                scoreFp=str(state.points * 1000),
                baseFp=str(state.points * 1000),
                payWeightFp=str(state.points * 1000),
                pciFp=str(state.contributed * 10**9 // state.earned if state.earned else 0),
                doorOpen=True,
            )
        )
    return dict(
        schema=ema_policy.OUTPUT_SCHEMA
        if ema
        else "noah-points-output/2"
        if amended
        else "noah-points-output/1",
        **({"receiptDispositions": dispositions} if amended else {}),
        **(
            {
                "pricingReferences": raw["pointsPricing"],
                "pricingBoundary": raw["pointsPricingBoundary"],
            }
            if ema
            else {}
        ),
        policy=onboarding.POLICY
        if rules
        else ema_policy.POLICY
        if ema
        else AMENDED_POLICY
        if amended
        else POLICY,
        fixture=fixture.name,
        evalDay=fixture.eval_day,
        miners=sorted(miners, key=lambda r: r["uid"]),
        weightsU16={str(k): v for k, v in weights_u16(scores).items()},
    )


def replay_state(miner, eval_day, *, cutoff=-2, balance=None, rules=None):
    """Pure per-lineage replay, including identities without an active UID."""
    state = onboarding.initial_state() if rules else State()
    for day in range(miner.first_activity_day, eval_day + 1):
        if day == cutoff + 1 and balance is not None:
            state = replace(state, balance=balance)
        prior, today = miner.days.get(day - 1), miner.days[day]
        earned = prior.paid_rao + prior.pay_unlocked_rao if prior else 0
        state = settle(
            state,
            earned,
            today.noah_deposit_rao + today.foreign_deposit_noah_equiv_rao,
            rules=rules,
        )
    if eval_day == cutoff and balance is not None:
        state = replace(state, balance=balance)
    return state


def migration_balances(raw, fixture):
    """Authenticate structure here; runtime authenticates these pins to its policy.

    Replayed contribution/maturity never count the balance bridge as a deposit.
    Only the signed liability/prepaid balance is replaced at the finalized cutoff.
    """
    import re

    value = raw.get("pointsMigration")
    if "pointsMigration" not in raw:
        return -2, {}
    if (
        raw.get("schema") not in (INPUT_SCHEMA, AMENDED_INPUT_SCHEMA, ema_policy.INPUT_SCHEMA)
        or not isinstance(value, dict)
        or set(value) != {"policySha256", "cutoffDay", "balances"}
    ):
        raise ValueError("invalid points migration fields")
    cutoff = value["cutoffDay"]
    if type(cutoff) is not int or not 0 <= cutoff <= fixture.eval_day:
        raise ValueError("invalid points migration cutoff")
    if not isinstance(value["policySha256"], str) or not re.fullmatch(
        "[0-9a-f]{64}", value["policySha256"]
    ):
        raise ValueError("invalid points migration policy hash")
    if not isinstance(value["balances"], list):
        raise ValueError("invalid points migration balances")
    miners = {m.uid: m for m in fixture.miners if m.uid is not None}
    balances = {}
    for row in value["balances"]:
        if not isinstance(row, dict) or set(row) != {"uid", "coldkey", "balanceRao"}:
            raise ValueError("invalid points migration row")
        uid = row["uid"]
        if type(uid) is not int or uid in balances or uid not in miners:
            raise ValueError("invalid points migration uid")
        miner = miners[uid]
        if row["coldkey"] != miner.coldkey or miner.first_activity_day > cutoff:
            raise ValueError("points migration identity differs")
        balance = row["balanceRao"]
        if not isinstance(balance, str) or not re.fullmatch(r"0|-?[1-9][0-9]*", balance):
            raise ValueError("invalid points migration balance")
        balances[uid] = int(balance)
    return cutoff, balances
