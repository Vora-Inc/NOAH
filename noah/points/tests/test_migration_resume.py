"""Cutover preserves legacy liabilities without inventing receipt credit."""

import copy

import pytest

from noah.core.calculation_record import encoded, retain
from noah.points.engine import INPUT_SCHEMA, evaluate
from noah.points.prepare import build
from noah.points.run import agreed, typescript_text
from noah.points.runtime import RuntimePolicy
from noah.points.tests.test_engine import R, fixture
from noah.points.tests.test_integration import captured, next_day
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture


def seed(raw, balance, cutoff=1):
    return {
        **raw,
        "schema": INPUT_SCHEMA,
        "pointsMigration": {
            "policySha256": "a" * 64,
            "cutoffDay": cutoff,
            "balances": [
                {"uid": 0, "coldkey": raw["miners"][0]["coldkey"], "balanceRao": str(balance)}
            ],
        },
    }


def test_twin_cutoff_balance_is_not_receipt_or_contribution():
    raw = fixture([[(100 * R, 0, 0), (0, 0, R), (0, 0, 0), (0, 45 * R, 0), (0, 5 * R, 0)]])
    migrated = seed(raw, -50 * R)
    for day, balance in [(1, -50 * R), (2, -50 * R), (3, -5 * R), (4, 0)]:
        sample = copy.deepcopy(migrated)
        sample["evalDay"] = str(day)
        sample["miners"][0]["days"] = {
            k: v for k, v in sample["miners"][0]["days"].items() if int(k) <= day
        }
        state = agreed(sample)["miners"][0]["state"]
        assert int(state["balance"]) == balance
        assert int(state["contributed"]) == (R if day < 3 else 46 * R if day == 3 else 51 * R)
        assert int(state["qualified"]) == (1 if day == 4 else 0)
    # Prepaid is equally exact, and never inflates contribution or maturity.
    raw = fixture([[(100 * R, 0, 0), (0, 100 * R, 0), (100 * R, 0, 0), (0, 0, 0)]])
    normal, migrated = agreed(raw), agreed(seed(raw, 75 * R))
    assert int(migrated["miners"][0]["state"]["balance"]) == 25 * R
    for key in ("earned", "contributed", "qualified", "maturity"):
        assert migrated["miners"][0]["state"][key] == normal["miners"][0]["state"][key]


@pytest.mark.parametrize(
    "damage",
    [
        "hash",
        "cutoff",
        "identity",
        "duplicate",
        "float",
        "unknown",
        "legacy",
        "null",
        "decimal_cutoff",
    ],
)
def test_both_engines_refuse_malformed_migration(damage):
    raw = seed(fixture([[(100 * R, 0, 0), (0, 0, R)]]), -50 * R)
    m = raw["pointsMigration"]
    if damage == "hash":
        m["policySha256"] = "wrong"
    if damage == "cutoff":
        m["cutoffDay"] = 2
    if damage == "identity":
        m["balances"][0]["coldkey"] = "different"
    if damage == "duplicate":
        m["balances"].append(copy.deepcopy(m["balances"][0]))
    if damage == "float":
        m["balances"][0]["balanceRao"] = 1.5
    if damage == "unknown":
        m["extra"] = True
    if damage == "legacy":
        raw["schema"] = "noah-fixture/6"
    if damage == "null":
        raw["pointsMigration"] = None
    if damage == "decimal_cutoff":
        m["cutoffDay"] = 1.0
    with pytest.raises(ValueError):
        evaluate(raw)
    with pytest.raises(ValueError):
        typescript_text(raw)


def test_prepare_runtime_preserve_legacy_foreign_cap_debt(tmp_path):
    adapter, document = captured(tmp_path / "runtime")
    identity = document["migration"]["miners"][0]
    raw = fixture([[(100 * R, 0, 0), (0, 0, R), (0, 0, 0)]])
    raw["miners"][0]["coldkey"] = identity["coldkey"]
    cutoff = document["migration"]["cutoff_block"]
    adapter.day_anchor_ts = int(raw["dayEpochTs"])
    adapter.runner_chain.block_timestamp = lambda b: (
        adapter.day_anchor_ts + 3 * 86400 - (1 if b <= cutoff else 0)
    )
    adapter.runner_chain.seat_hotkeys = lambda b: {0: identity["hotkey"]}
    adapter.runner_chain.hotkey_owners = lambda keys, b: {identity["hotkey"]: identity["coldkey"]}
    legacy = score_fixture(parse_fixture(raw))
    context = dict(
        day=2,
        height=cutoff,
        genesis_hash=document["migration"]["genesis"],
        netuid=82,
        ark_address="ark",
        designated_validator_hotkey="val",
        source_agreement="required_by_ingestion",
        scored_hotkeys={"0": identity["hotkey"]},
        to_block=cutoff,
        to_block_hash=document["migration"]["cutoff_hash"],
        foreign_valuations=[
            dict(
                block=cutoff,
                block_hash=document["migration"]["cutoff_hash"],
                token_netuid=3,
                amount_rao=str(R),
                noah_equivalent_rao=str(R),
            )
        ],
    )
    from ._lineage_capture import for_context
    context["points_lineage_capture"] = for_context(raw, context)
    ref = retain(
        tmp_path / "evidence",
        fixture=raw,
        agreed_text=encoded(legacy).decode(),
        vector={int(k): v for k, v in legacy["weightsU16"].items()},
        context=context,
    )
    result = build(tmp_path / "evidence", ref, document["pricing"], ["receipt-foreign-1"])
    row = result["migration"]["miners"][0]
    assert row["state"]["balance"] == -50 * R
    assert evaluate(raw)["miners"][0]["state"]["balance"] == str(-49 * R)
    assert result["migration"]["consumed_receipt_ids"] == ["receipt-foreign-1"]
    policy = RuntimePolicy(result, adapter)
    future = next_day(raw)
    hotkeys = {0: identity["hotkey"]}
    assert policy.check_snapshot(adapter, future, hotkeys)
    future.update(schema=INPUT_SCHEMA, pointsMigration=policy.fixture_migration(future, hotkeys))
    assert agreed(future)["miners"][0]["state"]["balance"] == str(-50 * R)
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.points.mode import MODE, check_publication_mode

    adapter.points_policy, adapter.scoring_mode = policy, MODE
    adapter._submitted_record_dir = tmp_path / "runtime"
    changed = copy.deepcopy(future)
    changed["pointsMigration"]["balances"][0]["balanceRao"] = str(-49 * R)
    changed_score = agreed(changed)
    new_context = {**context, "day": 3, "scoring_mode": MODE, "points_policy": policy.manifest}
    altered_ref = retain(
        adapter._submitted_record_dir,
        fixture=changed,
        agreed_text=encoded(changed_score).decode(),
        vector={int(k): v for k, v in changed_score["weightsU16"].items()},
        context=new_context,
    )
    with pytest.raises(PublicationStatusUnknown, match="migration"):
        check_publication_mode(adapter, altered_ref)
    # Neither an altered liability nor injected score component can pass startup.
    for field in ("balance", "qualified"):
        broken = copy.deepcopy(result)
        broken["migration"]["miners"][0]["state"][field] += 1
        with pytest.raises(ValueError):
            RuntimePolicy(broken, adapter)
