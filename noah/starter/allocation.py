"""Pure starter allocation. Ordinary scoring arithmetic remains unchanged.

The production reader supplies finalized facts; this module has no side effects.
The shared cap applies to this validator's vector, not arbitrary Yuma peers.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from ..scoring.engine import score_fixture, score_miner
from ..scoring.fixture_io import parse_fixture

ALLOWANCE = 5_000_000_000
DAILY_TARGET = 5_000_000_000
U16 = 65535
SHARED_POOL_BPS = 25
SHARED_POOL_UNITS = U16 * SHARED_POOL_BPS // 10000
POLICY_ID = "noah-starter-5noah-shared-25bps-v1"


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name}: integer >= {minimum} required")
    return value


def fields(obj, names, name):
    if not isinstance(obj, dict) or set(obj) != set(names.split()):
        raise ValueError(f"{name}: exact fields required: {names}")


def text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: nonempty string required")
    return value


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def apportion(weights, units):
    total = sum(weights.values())
    if not total:
        raise ValueError("positive ordinary weight required; cannot give starters the entire pool")
    out = {uid: w * units // total for uid, w in weights.items()}
    order = sorted(weights, key=lambda uid: (-(weights[uid] * units % total), uid))
    for uid in order[: units - sum(out.values())]:
        out[uid] += 1
    return out


def equal_capped(requests, budget):
    """Integer max-min shares: small requests fill first, remaining wallets tie.

    Stable identity order breaks the unavoidable single-u16-unit remainder.
    No request is increased and unused budget is returned to ordinary miners.
    """
    shares = {key: 0 for key in requests}
    remaining = min(budget, sum(requests.values()))
    active = sorted(key for key, request in requests.items() if request > 0)
    while active and remaining:
        quotient, remainder = divmod(remaining, len(active))
        small = [key for key in active if requests[key] <= quotient]
        if small:
            for key in small:
                shares[key] = requests[key]
                remaining -= shares[key]
            active = [key for key in active if key not in small]
        else:
            for index, key in enumerate(active):
                shares[key] = quotient + (index < remainder)
            remaining = 0
    return shares


def preview(raw, previous=None):
    fields(raw, "schema policyId fixture scoredHotkeys snapshot lifetimeEarnings", "preview input")
    live_clock = raw["schema"] == "noah-starter-epoch-preview/4"
    if raw["schema"] not in ("noah-starter-epoch-preview/3", "noah-starter-epoch-preview/4"):
        raise ValueError("unsupported preview schema")
    if raw["policyId"] != POLICY_ID:
        raise ValueError("starter policy mismatch; expected five NOAH and shared 25 bps")
    # Legacy fixture parsing and full ordinary engine are deliberately unchanged.
    fx = parse_fixture(raw["fixture"])
    ordinary = score_fixture(fx)
    snap = raw["snapshot"]
    fields(
        snap,
        "genesisHash netuid epoch finalizedBlock finalizedHash epochSeconds "
        + (
            "miningPoolSample "
            + ("epochStartTimestamp nextEpochBlock " if live_clock else "epochZeroTimestamp ")
            + "finalizedTimestamp "
        )
        + "weightEffectiveEpoch pendingThroughEpoch seats",
        "snapshot",
    )
    for name in ("genesisHash", "finalizedHash"):
        text(snap[name], name)
    for name in (
        "netuid",
        "epoch",
        "finalizedBlock",
        "epochSeconds",
        "finalizedTimestamp",
        "weightEffectiveEpoch",
        "pendingThroughEpoch",
    ):
        integer(snap[name], name, 1 if name in ("epochSeconds",) else 0)
    if live_clock:
        epoch_start = integer(snap["epochStartTimestamp"], "epoch start timestamp")
        integer(snap["nextEpochBlock"], "next epoch block", 1)
        if not snap["epoch"] <= snap["finalizedBlock"] < snap["nextEpochBlock"]:
            raise ValueError("finalized block is outside the observed epoch")
        if snap["finalizedTimestamp"] < epoch_start:
            raise ValueError("finalized time precedes actual epoch start")
    else:
        integer(snap["epochZeroTimestamp"], "epoch zero timestamp")
        epoch_start = snap["epochZeroTimestamp"] + snap["epoch"] * snap["epochSeconds"]
        if not epoch_start <= snap["finalizedTimestamp"] < epoch_start + snap["epochSeconds"]:
            raise ValueError("epoch and finalized timestamp disagree")
    daily_end = fx.day_epoch_ts + (fx.eval_day + 1) * 86400
    if not daily_end <= snap["finalizedTimestamp"] < daily_end + 86400:
        raise ValueError("daily fixture must describe the latest completed UTC day")
    sample = snap["miningPoolSample"]
    fields(sample, "amountRao durationSeconds endTimestamp", "mining pool sample")
    amount = integer(sample["amountRao"], "pool amount", 1)
    duration = integer(sample["durationSeconds"], "pool sample duration", 1)
    end = integer(sample["endTimestamp"], "pool sample end")
    if duration not in (snap["epochSeconds"], 86400):
        raise ValueError("pool sample must explicitly cover one epoch or one day")
    if not 0 <= snap["finalizedTimestamp"] - end < snap["epochSeconds"]:
        raise ValueError("pool sample is stale or from the future")
    pool = amount * snap["epochSeconds"] // duration
    if pool <= 0:
        raise ValueError("normalized epoch mining pool must be positive")
    if live_clock and snap["weightEffectiveEpoch"] != snap["nextEpochBlock"]:
        raise ValueError("effective epoch must be the observed next epoch block")
    if snap["weightEffectiveEpoch"] <= snap["epoch"]:
        raise ValueError("effective epoch must follow the observed epoch")
    if snap["pendingThroughEpoch"] != snap["weightEffectiveEpoch"] - 1:
        raise ValueError("pending rewards must cover every epoch before weight effectiveness")
    identity = [snap["genesisHash"], snap["netuid"]]
    input_hash = digest(raw)
    if previous is not None:
        fields(
            previous,
            "schema policyId identity epoch block hash inputHash "
            + ("epochStartTimestamp nextEpochBlock " if live_clock else "epochZeroTimestamp ")
            + "epochSeconds seats",
            "preview state",
        )
        expected_state = "noah-starter-state/4" if live_clock else "noah-starter-state/3"
        if previous["schema"] != expected_state or previous["identity"] != identity:
            raise ValueError("state chain identity mismatch")
        if previous["policyId"] != POLICY_ID:
            raise ValueError("state starter policy mismatch")
        if not live_clock and any(
            previous[name] != snap[name] for name in ("epochZeroTimestamp", "epochSeconds")
        ):
            raise ValueError("epoch clock changed; audited migration required")
        integer(previous["epoch"], "state epoch")
        integer(previous["block"], "state block")
        text(previous["hash"], "state block hash")
        text(previous["inputHash"], "state input hash")
        if snap["epoch"] < previous["epoch"] or snap["finalizedBlock"] < previous["block"]:
            raise ValueError("observations cannot go backward")
        if snap["finalizedBlock"] == previous["block"] and (
            snap["finalizedHash"] != previous["hash"] or input_hash != previous["inputHash"]
        ):
            raise ValueError("different input at the same finalized block")
        if not isinstance(previous["seats"], dict):
            raise ValueError("state seats must be an object")
    history = dict(previous["seats"]) if previous is not None else {}
    for key, record in history.items():
        fields(record, "earnedRao terminal", "state seat")
        integer(record["earnedRao"], "state earnings")
        if record["terminal"] not in ("", "ordinary", "exhausted"):
            raise ValueError("invalid terminal state")
        pair = json.loads(key)
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("state seat identity must be a coldkey/hotkey pair")
        for address in pair:
            text(address, "state seat address")
    if not isinstance(raw["lifetimeEarnings"], list):
        raise ValueError(
            "complete lifetime earnings ledger required, including departed identities"
        )
    ledger = {}
    lifetime_pending = {}
    for record in raw["lifetimeEarnings"]:
        fields(record, "coldkey hotkey earnedRao pendingExpectedRao", "lifetime earnings record")
        cold = text(record["coldkey"], "lifetime coldkey")
        hot = text(record["hotkey"], "lifetime hotkey")
        key = json.dumps([cold, hot], separators=(",", ":"))
        if key in ledger:
            raise ValueError("duplicate lifetime identity")
        earned = integer(record["earnedRao"], "lifetime earnings")
        if earned < history.get(key, {"earnedRao": 0})["earnedRao"]:
            raise ValueError("lifetime earnings cannot disappear")
        ledger[key] = earned
        lifetime_pending[key] = integer(record["pendingExpectedRao"], "lifetime pending rewards")
    if not set(history) <= set(ledger):
        raise ValueError("lifetime history cannot omit departed identities")
    for key, earned in ledger.items():
        history[key] = dict(earnedRao=earned, terminal=history.get(key, {}).get("terminal", ""))
    cold_earned, hot_earned = defaultdict(int), defaultdict(int)
    for key, earned in ledger.items():
        cold, hot = json.loads(key)
        cold_earned[cold] += earned
        hot_earned[hot] += earned
    live = {m.uid: m for m in fx.miners if m.uid is not None}
    if not isinstance(raw["scoredHotkeys"], dict) or set(raw["scoredHotkeys"]) != {
        str(u) for u in live
    }:
        raise ValueError("scored hotkey mapping must cover every live scoring row")
    if not isinstance(snap["seats"], list):
        raise ValueError("snapshot seats must be an array")
    # Identity failures invalidate the overlay. Numeric faults with a known identity
    # quarantine that wallet (siblings share one allowance), not ordinary miners.
    invalid, cold_pending, hot_pending = {}, defaultdict(int), defaultdict(int)
    for key, pending in lifetime_pending.items():
        cold, hot = json.loads(key)
        cold_pending[cold] += pending
        hot_pending[hot] += pending
    seen, hotkeys = set(), set()
    for seat in snap["seats"]:
        fields(
            seat,
            "uid coldkey hotkey observedBlock spendableEarnedRao pendingExpectedRao ownerExcluded",
            "seat",
        )
        uid = integer(seat["uid"], "uid")
        cold, hot = text(seat["coldkey"], "coldkey"), text(seat["hotkey"], "hotkey")
        if uid not in live or uid in seen or hot in hotkeys:
            raise ValueError("missing or duplicate scoring identity")
        if cold != live[uid].coldkey or hot != raw["scoredHotkeys"][str(uid)]:
            raise ValueError("snapshot identity does not match scored coldkey/hotkey")
        seen.add(uid)
        hotkeys.add(hot)
        try:
            if integer(seat["observedBlock"], "observed block") != snap["finalizedBlock"]:
                raise ValueError("seat observation is stale")
            earned = integer(seat["spendableEarnedRao"], "spendable earnings")
            pending = integer(seat["pendingExpectedRao"], "pending estimate")
            key = json.dumps([cold, hot], separators=(",", ":"))
            if key not in ledger or ledger[key] != earned or lifetime_pending[key] != pending:
                raise ValueError("seat earnings differ from lifetime ledger")
            closed = sum(d.paid_rao + d.pay_unlocked_rao for d in live[uid].days.values())
            if earned < closed:
                raise ValueError("daily earnings cannot disappear")
            if type(seat["ownerExcluded"]) is not bool:
                raise ValueError("owner exclusion must be explicit boolean")
        except ValueError as exc:
            invalid[cold] = str(exc)
    if seen != set(live):
        raise ValueError("snapshot must include every scored live identity")
    reports, proposed, targets = [], {}, {}
    results = {u: score_miner(m, fx.dials, fx.eval_day) for u, m in live.items()}
    for seat in sorted(snap["seats"], key=lambda x: x["uid"]):
        uid, cold, hot = seat["uid"], seat["coldkey"], seat["hotkey"]
        if cold in invalid:
            # No ordinary or starter allocation to an unvalidated seat. Other
            # wallets continue. In particular an invalid owner flag is not a
            # reason to fall back to that seat's unfiltered ordinary score.
            proposed[uid] = 0
            reports.append(
                dict(
                    uid=uid,
                    coldkey=cold,
                    hotkey=hot,
                    status="quarantined",
                    reason=invalid[cold],
                    starterWeightU16=0,
                )
            )
            continue
        earned, pending = seat["spendableEarnedRao"], seat["pendingExpectedRao"]
        key = json.dumps([cold, hot], separators=(",", ":"))
        prior = history.get(key, {"earnedRao": 0, "terminal": ""})
        # Keep the stated five-NOAH transition. Early contributions remain in
        # the ordinary calculation, but do not cut off the starter phase early
        # or stack another allocation on top. Existing qualified miners never
        # become newcomers. After graduation, falling behind cannot reopen it.
        ordinary_credit = results[uid].base_fp > 0
        exhausted = cold_earned[cold] >= ALLOWANCE or hot_earned[hot] >= ALLOWANCE
        terminal = (
            "ordinary"
            if (
                live[uid].entry_deposit_rao > 0
                or prior["terminal"] == "ordinary"
                or (ordinary_credit and (exhausted or prior["terminal"] == "exhausted"))
            )
            else ("exhausted" if exhausted or prior["terminal"] == "exhausted" else "")
        )
        status = terminal or "starter"
        if seat["ownerExcluded"]:
            if results[uid].pay_weight_fp:
                raise ValueError("owner seat has ordinary weight; exclude before preview")
            status = "owner-excluded"
        elif not results[uid].door_open:
            status = "holding-gate"
        remaining = max(
            0,
            min(
                ALLOWANCE - cold_earned[cold] - cold_pending[cold],
                ALLOWANCE - hot_earned[hot] - hot_pending[hot],
            ),
        )
        units = 0
        if status == "starter" and remaining:
            # Exact integer ceiling of min(5/day * epoch duration, remaining)
            # divided by the estimated epoch mining pool.
            target_numerator = min(DAILY_TARGET * snap["epochSeconds"], remaining * 86400)
            denominator = 86400 * pool
            units = (target_numerator * U16 + denominator - 1) // denominator
        if status == "starter":
            proposed[uid] = units
            targets[uid] = (cold, remaining)
        history[key] = {"earnedRao": earned, "terminal": terminal}
        reports.append(
            dict(
                uid=uid,
                coldkey=cold,
                hotkey=hot,
                status=status,
                earnedRao=earned,
                pendingExpectedRao=pending,
                remainingAfterPendingRao=remaining,
                starterWeightU16=units,
            )
        )
        seen.add(uid)
        hotkeys.add(hot)
    if seen != set(live):
        raise ValueError("snapshot must include every scored live identity")
    # Wallet-first allocation. Additional hotkeys cannot increase a wallet's
    # request or its share of the capped pool. First share capacity equally
    # between wallets, then distribute each wallet's share among its hotkeys.
    groups = {
        cold: {uid: proposed[uid] for uid, value in targets.items() if value[0] == cold}
        for cold in sorted({value[0] for value in targets.values()})
    }
    wallet_requests = {}
    for cold, group in groups.items():
        remaining = max(0, ALLOWANCE - cold_earned[cold] - cold_pending[cold])
        target_numerator = min(DAILY_TARGET * snap["epochSeconds"], remaining * 86400)
        denominator = 86400 * pool
        wallet_requests[cold] = min(
            sum(group.values()),
            (target_numerator * U16 + denominator - 1) // denominator,
        )
    wallet_shares = equal_capped(wallet_requests, SHARED_POOL_UNITS)
    requested = dict(proposed)
    for cold, group in groups.items():
        if sum(group.values()):
            proposed.update(apportion(group, wallet_shares[cold]))
    for report in reports:
        if report["uid"] in proposed:
            report["starterWeightU16"] = proposed[report["uid"]]
    normal = {u: results[u].pay_weight_fp for u in live if u not in proposed}
    weights = {int(k): v for k, v in ordinary["weightsU16"].items()}
    no_ordinary = bool(proposed) and not sum(normal.values())
    if no_ordinary:
        # Publishing all-zero weights has chain-specific behavior. Preserve the
        # evidence, but expose a blocked plan rather than paying the whole pool
        # to newcomers or silently assigning the owner the remainder.
        weights = {u: 0 for u in live}
        for report in reports:
            if report["uid"] in proposed:
                report["starterWeightU16"] = 0
                report["status"] = "paused-no-ordinary-weight"
    elif proposed:
        if sum(normal.values()) or sum(proposed.values()):
            weights = apportion(normal, U16 - sum(proposed.values()))
        else:
            weights = {u: 0 for u in normal}
        weights.update(proposed)
    fallback = {u: normal.get(u, 0) if live[u].coldkey not in invalid else 0 for u in live}
    fallback = apportion(fallback, U16) if sum(fallback.values()) else {u: 0 for u in live}
    state = dict(
        schema="noah-starter-state/4" if live_clock else "noah-starter-state/3",
        policyId=POLICY_ID,
        identity=identity,
        epoch=snap["epoch"],
        block=snap["finalizedBlock"],
        hash=snap["finalizedHash"],
        inputHash=input_hash,
        **(
            {"epochStartTimestamp": epoch_start, "nextEpochBlock": snap["nextEpochBlock"]}
            if live_clock
            else {"epochZeroTimestamp": snap["epochZeroTimestamp"]}
        ),
        epochSeconds=snap["epochSeconds"],
        seats=history,
    )
    return dict(
        schema="noah-starter-epoch-result/4" if live_clock else "noah-starter-epoch-result/3",
        policyId=POLICY_ID,
        starterPolicy=dict(
            allowanceRao=ALLOWANCE,
            dailyTargetRao=DAILY_TARGET,
            sharedPoolBps=SHARED_POOL_BPS,
            maxSharedWeightU16=SHARED_POOL_UNITS,
            lifetimeLimitPerColdkey=True,
            lifetimeLimitPerHotkey=True,
            allocationOrder="equal-coldkeys-then-hotkeys",
        ),
        requestedStarterWeightsU16={str(u): w for u, w in sorted(requested.items())},
        allocatedStarterWeightU16=0 if no_ordinary else sum(proposed.values()),
        offlineOnly=True,
        ordinaryOutput=ordinary,
        fixtureHash=digest(raw["fixture"]),
        normalizedEpochPoolRao=pool,
        validUntilTimestamp=epoch_start + snap["epochSeconds"],
        **({"validUntilBlock": snap["nextEpochBlock"]} if live_clock else {}),
        ordinaryFallbackWeightsU16={str(u): w for u, w in sorted(fallback.items())},
        releaseReady=False,
        noOrdinaryWeight=no_ordinary,
        releaseBlockers=[
            "A pure allocation is not deployment approval; "
            "verify retained chain evidence and guard health.",
        ],
        proposedWeightsU16={str(k): v for k, v in sorted(weights.items())},
        starter=reports,
        state=state,
        limitations=[
            "Snapshot and pending estimates are supplied, not chain-verified here.",
            "0.25% bounds the proposed vector, not independently enforced Yuma payouts.",
            "No exact five-token chain payout cap; independent Yuma rows can change payouts.",
            "Lifetime ledger completeness is an input requirement, not verified chain history.",
            "An expired local allocation does not expire weights already on chain.",
            "Ordinary scores are unchanged; this preview alters allocation only.",
        ],
    )


def main():
    p = argparse.ArgumentParser(description="OFFLINE preview only; cannot publish or sign")
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--state", type=Path)
    a = p.parse_args()
    result = preview(
        json.loads(a.input.read_text()), json.loads(a.state.read_text()) if a.state else None
    )
    # Refuse to overwrite a previous review artifact or recovery state.
    with a.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print("Offline preview saved. No signing, submission or chain verification occurred.")


if __name__ == "__main__":
    main()
