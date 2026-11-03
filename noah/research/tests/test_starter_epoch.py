import copy
import json
import subprocess
import sys

import pytest

from noah.research.starter_epoch import ALLOWANCE, POLICY_ID, SHARED_POOL_UNITS, U16, preview
from noah.scoring.tests.test_engine import day, make_fixture, miner

R = 10**9
POOL = 147600823680


def request(n=1):
    regular = miner(
        "established",
        0,
        "0",
        {"0": day(str(10 * R), str(5 * R)), "1": day(str(10 * R), str(5 * R))},
        entry=str(10 * R),
        reg="0",
    )
    newcomers = [
        miner(f"wallet-{i}", i, "0", {"1": day("0", "0", no_fault=True)}) for i in range(1, n + 1)
    ]
    fx = make_fixture([regular] + newcomers, "1")
    fx["dials"].update(ongoingHoldRao="0", targetFp="500000000", capFp="2000000000")
    raw = dict(
        schema="noah-starter-epoch-preview/3",
        policyId=POLICY_ID,
        fixture=fx,
        scoredHotkeys={str(i): f"hotkey-{i}" for i in range(n + 1)},
        lifetimeEarnings=[],
        snapshot=dict(
            genesisHash="isolated-test-genesis",
            netuid=82,
            epoch=100,
            finalizedBlock=1000,
            finalizedHash="isolated-test-block",
            epochSeconds=4320,
            epochZeroTimestamp=int(fx["dayEpochTs"]) + 2 * 86400 - 100 * 4320,
            finalizedTimestamp=int(fx["dayEpochTs"]) + 2 * 86400,
            miningPoolSample=dict(
                amountRao=POOL, durationSeconds=4320, endTimestamp=int(fx["dayEpochTs"]) + 2 * 86400
            ),
            weightEffectiveEpoch=101,
            pendingThroughEpoch=100,
            seats=[
                dict(
                    uid=i,
                    coldkey=m["coldkey"],
                    hotkey=f"hotkey-{i}",
                    observedBlock=1000,
                    spendableEarnedRao=20 * R if i == 0 else 0,
                    pendingExpectedRao=0,
                    ownerExcluded=False,
                )
                for i, m in enumerate(fx["miners"])
            ],
        ),
    )

    sync(raw)
    return raw


def sync(raw):
    ledger = {(r["coldkey"], r["hotkey"]): r for r in raw["lifetimeEarnings"]}
    for seat in raw["snapshot"]["seats"]:
        ledger[seat["coldkey"], seat["hotkey"]] = dict(
            coldkey=seat["coldkey"],
            hotkey=seat["hotkey"],
            earnedRao=seat["spendableEarnedRao"],
            pendingExpectedRao=max(0, seat["pendingExpectedRao"]),
        )
    raw["lifetimeEarnings"] = list(ledger.values())


def checked(raw, state=None):
    sync(raw)
    return preview(raw, state)


def advance(raw, epoch):
    snap = raw["snapshot"]
    snap.update(
        finalizedTimestamp=snap["epochZeroTimestamp"] + epoch * snap["epochSeconds"],
        epoch=epoch,
        finalizedBlock=epoch * 10,
        finalizedHash=f"isolated-block-{epoch}",
        weightEffectiveEpoch=epoch + 1,
        pendingThroughEpoch=epoch,
    )
    for seat in snap["seats"]:
        seat["observedBlock"] = epoch * 10
    snap["miningPoolSample"]["endTimestamp"] = snap["finalizedTimestamp"]
    # Test helper supplies a current closed-day fixture, retaining complete rows.
    fx = raw["fixture"]
    new_day = (snap["finalizedTimestamp"] - int(fx["dayEpochTs"])) // 86400 - 1
    for miner_row in fx["miners"]:
        for d in range(int(fx["evalDay"]) + 1, new_day + 1):
            miner_row["days"][str(d)] = day("0", "0", no_fault=True)
    fx["evalDay"] = str(new_day)


def test_shared_pool_cap_and_no_allowance_expiry():
    for n in (1, 10, 32, 82):
        raw = request(n)
        advance(raw, 240)
        out = checked(raw)
        allocated = sum(r["starterWeightU16"] for r in out["starter"][1:])
        assert allocated == min(112 * n, SHARED_POOL_UNITS)
        assert allocated * 10000 <= U16 * 25
        assert sum(out["proposedWeightsU16"].values()) == U16
        assert out["proposedWeightsU16"]["0"] == U16 - allocated
        assert all(r["status"] == "starter" for r in out["starter"][1:])


def test_twenty_epochs_then_stops_without_deposit():
    raw = request()
    state = None
    earned = 0
    for epoch in range(100, 120):
        advance(raw, epoch)
        raw["snapshot"]["seats"][1]["spendableEarnedRao"] = earned
        out = checked(raw, state)
        state = out["state"]
        earned += POOL * out["proposedWeightsU16"]["1"] // U16
    assert ALLOWANCE <= earned < ALLOWANCE + POOL // U16 + 1
    advance(raw, 120)
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = earned
    out = checked(raw, state)
    assert out["starter"][1]["status"] == "exhausted"
    assert out["proposedWeightsU16"]["1"] == 0
    assert out["ordinaryOutput"]["weightsU16"]["1"] == 0


def test_pending_rewards_reduce_allocation_and_can_pause_it():
    raw = request()
    raw["snapshot"]["seats"][1].update(spendableEarnedRao=4900000000, pendingExpectedRao=100000000)
    out = checked(raw)
    assert out["starter"][1]["starterWeightU16"] == 0
    raw["snapshot"]["seats"][1]["pendingExpectedRao"] = 0
    assert checked(raw)["starter"][1]["starterWeightU16"] == 45


def test_restart_replay_and_same_block_mutation():
    raw = request()
    out = checked(raw)
    assert preview(json.loads(json.dumps(raw)), json.loads(json.dumps(out["state"]))) == out
    changed = copy.deepcopy(raw)
    changed["snapshot"]["miningPoolSample"]["amountRao"] += 1
    with pytest.raises(ValueError, match="same finalized block"):
        preview(changed, out["state"])


def test_uid_change_does_not_reset_terminal_identity():
    raw = request()
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 5 * R
    out = checked(raw)
    raw["fixture"]["miners"][1]["uid"] = 9
    raw["scoredHotkeys"]["9"] = raw["scoredHotkeys"].pop("1")
    raw["snapshot"]["seats"][1]["uid"] = 9
    advance(raw, 101)
    nextout = checked(raw, out["state"])
    assert nextout["proposedWeightsU16"]["9"] == 0
    assert nextout["starter"][1]["status"] == "exhausted"


def test_new_hotkey_cannot_reset_wallet_allowance():
    raw = request()
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 5 * R
    out = checked(raw)
    advance(raw, 101)
    raw["snapshot"]["seats"][1].update(hotkey="new-identity", spendableEarnedRao=0)
    raw["scoredHotkeys"]["1"] = "new-identity"
    assert checked(raw, out["state"])["starter"][1]["status"] == "exhausted"


def test_contributes_then_normal_only_and_no_starter_restart():
    raw = request()
    m = raw["fixture"]["miners"][1]
    m["firstActivityDay"] = "0"
    m["days"] = {"0": day(str(5 * R), "0"), "1": day("0", str(2500000000))}
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 5 * R
    out = checked(raw)
    assert out["starter"][1]["status"] == "ordinary"
    assert out["starter"][1]["starterWeightU16"] == 0
    assert out["proposedWeightsU16"] == out["ordinaryOutput"]["weightsU16"]
    assert out["proposedWeightsU16"]["1"] > 0
    # Even if the next supplied daily score is zero, persisted graduation
    # cannot reopen the allowance. It is not zeroed merely for reaching five.
    advance(raw, 101)
    m["days"]["1"]["noahDepositRao"] = "0"
    later = checked(raw, out["state"])
    assert later["starter"][1]["status"] == "ordinary"
    assert later["proposedWeightsU16"]["1"] == 0


def test_early_contribution_does_not_stack_starter_and_normal():
    raw = request()
    m = raw["fixture"]["miners"][1]
    m["firstActivityDay"] = "0"
    m["days"] = {"0": day(str(2 * R), "0"), "1": day("0", str(R))}
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 2 * R
    out = checked(raw)
    assert out["starter"][1]["status"] == "starter"
    assert out["ordinaryOutput"]["weightsU16"]["1"] > 0
    assert out["proposedWeightsU16"]["1"] == 112
    raw["snapshot"]["seats"][1]["pendingExpectedRao"] = 3 * R
    assert checked(raw)["proposedWeightsU16"]["1"] == 0
    raw["snapshot"]["seats"][1].update(spendableEarnedRao=5 * R, pendingExpectedRao=0)
    out = checked(raw)
    assert out["starter"][1]["status"] == "ordinary"
    assert out["proposedWeightsU16"] == out["ordinaryOutput"]["weightsU16"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["snapshot"]["seats"].pop(),
        lambda r: r["snapshot"]["seats"].append(r["snapshot"]["seats"][1]),
        lambda r: r["snapshot"]["seats"][1].update(coldkey="wrong"),
        lambda r: r["snapshot"]["seats"][1].update(hotkey="wrong"),
        lambda r: r["snapshot"]["seats"][1].update(spendableEarnedRao=-1),
        lambda r: r["snapshot"].update(epochSeconds=True),
        lambda r: r["snapshot"]["miningPoolSample"].update(amountRao=0),
        lambda r: r["snapshot"].update(pendingThroughEpoch=99),
        lambda r: r["snapshot"].update(weightEffectiveEpoch=100),
        lambda r: r["snapshot"].update(expiryDays=7),
        lambda r: r["snapshot"].update(poolBps=50),
    ],
)
def test_bad_inputs_refuse(mutate):
    raw = request()
    mutate(raw)
    with pytest.raises(ValueError):
        checked(raw)


def test_prior_state_and_chain_mismatch_refuse():
    raw = request()
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = R
    out = checked(raw)
    advance(raw, 101)
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 0
    with pytest.raises(ValueError, match="earnings cannot disappear"):
        checked(raw, out["state"])
    raw["snapshot"]["genesisHash"] = "other"
    with pytest.raises(ValueError, match="identity mismatch"):
        checked(raw, out["state"])


def test_owner_holding_gate_and_no_normal_budget():
    raw = request()
    raw["snapshot"]["seats"][1]["ownerExcluded"] = True
    assert checked(raw)["proposedWeightsU16"]["1"] == 0
    raw = request()
    raw["fixture"]["dials"]["ongoingHoldRao"] = "1"
    raw["fixture"]["miners"][0]["holdRao"] = "100"
    assert checked(raw)["starter"][1]["status"] == "holding-gate"
    raw = request()
    raw["fixture"]["miners"][0]["entryDepositRao"] = "0"
    for d in raw["fixture"]["miners"][0]["days"].values():
        d["noahDepositRao"] = "0"
    blocked = checked(raw)
    assert blocked["noOrdinaryWeight"]
    assert not any(blocked["proposedWeightsU16"].values())
    raw = request(32)
    raw["snapshot"]["miningPoolSample"]["amountRao"] = R
    assert checked(raw)["allocatedStarterWeightU16"] == SHARED_POOL_UNITS


def test_cli_is_offline_and_refuses_output_overwrite(tmp_path):
    src = tmp_path / "input.json"
    dst = tmp_path / "output.json"
    src.write_text(json.dumps(request()))
    cmd = [sys.executable, "-m", "noah.research.starter_epoch", str(src), str(dst)]
    result = subprocess.run(cmd, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "No signing, submission or chain verification" in result.stdout
    assert json.loads(dst.read_text())["offlineOnly"] is True
    before = dst.read_bytes()
    assert subprocess.run(cmd, capture_output=True).returncode != 0
    assert dst.read_bytes() == before


def test_bad_seat_shape_is_value_error_not_key_error():
    raw = request()
    del raw["snapshot"]["seats"][1]["uid"]
    with pytest.raises(ValueError, match="exact fields"):
        preview(raw)


@pytest.mark.parametrize(
    "field,value", [("pendingExpectedRao", -1), ("observedBlock", 999), ("ownerExcluded", 1)]
)
def test_bad_seat_quarantined_other_wallet_and_ordinary_continue(field, value):
    raw = request(2)
    raw["snapshot"]["seats"][1][field] = value
    out = preview(raw)
    assert out["starter"][1]["status"] == "quarantined"
    assert out["proposedWeightsU16"]["1"] == 0
    assert out["proposedWeightsU16"]["2"] == 112
    assert out["proposedWeightsU16"]["0"] == U16 - 112


def test_bad_seat_quarantines_shared_wallet():
    raw = request(2)
    raw["fixture"]["miners"][2]["coldkey"] = "wallet-1"
    raw["snapshot"]["seats"][2]["coldkey"] = "wallet-1"
    sync(raw)
    raw["snapshot"]["seats"][1]["pendingExpectedRao"] = -1
    out = preview(raw)
    assert out["proposedWeightsU16"] == {"0": U16, "1": 0, "2": 0}


def test_same_hotkey_new_wallet_cannot_reset_allowance():
    raw = request()
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = ALLOWANCE
    out = checked(raw)
    advance(raw, 101)
    raw["fixture"]["miners"][1]["coldkey"] = "new-wallet"
    raw["snapshot"]["seats"][1].update(coldkey="new-wallet", spendableEarnedRao=0)
    assert checked(raw, out["state"])["starter"][1]["status"] == "exhausted"


def test_shared_wallet_combined_budget_and_rounding():
    raw = request(32)
    for row, seat in zip(raw["fixture"]["miners"][1:], raw["snapshot"]["seats"][1:]):
        row["coldkey"] = seat["coldkey"] = "siblings"
    # The wallet has already earned 4.9; every sibling must share the last 0.1.
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 4900000000
    out = checked(raw)
    units = sum(r["starterWeightU16"] for r in out["starter"][1:])
    assert units == 45
    assert POOL * units // U16 < 100000000 + POOL // U16 + 1


def test_removed_seat_pending_rewards_reduce_sibling_allowance():
    raw = request()
    raw["lifetimeEarnings"].append(
        dict(coldkey="wallet-1", hotkey="departed", earnedRao=4 * R, pendingExpectedRao=R)
    )
    assert preview(raw)["proposedWeightsU16"]["1"] == 0


def test_cannot_omit_departed_history_on_restart():
    raw = request()
    first = preview(raw)
    advance(raw, 101)
    raw["snapshot"]["seats"][1]["hotkey"] = raw["scoredHotkeys"]["1"] = "replacement"
    raw["lifetimeEarnings"] = []
    sync(raw)
    with pytest.raises(ValueError, match="omit departed"):
        preview(raw, first["state"])


def test_daily_and_epoch_pool_units_produce_identical_weights():
    raw = request()
    epoch = preview(raw)
    raw["snapshot"]["miningPoolSample"].update(amountRao=POOL * 20, durationSeconds=86400)
    daily = preview(raw)
    assert daily["normalizedEpochPoolRao"] == epoch["normalizedEpochPoolRao"]
    assert daily["proposedWeightsU16"] == epoch["proposedWeightsU16"]
    raw["snapshot"]["miningPoolRao"] = POOL * 20
    with pytest.raises(ValueError, match="exact fields"):
        preview(raw)


def test_cannot_reuse_old_daily_fixture_by_advancing_epoch_or_time():
    raw = request()
    raw["snapshot"]["epoch"] = 999999
    with pytest.raises(ValueError, match="timestamp disagree"):
        preview(raw)
    raw["snapshot"]["finalizedTimestamp"] = raw["snapshot"]["epochZeroTimestamp"] + 999999 * 4320
    with pytest.raises(ValueError, match="latest completed"):
        preview(raw)


def test_stale_or_future_pool_observation_rejected():
    for shift in (-4320, 1):
        raw = request()
        raw["snapshot"]["miningPoolSample"]["endTimestamp"] += shift
        with pytest.raises(ValueError, match="stale or from the future"):
            preview(raw)


def test_ordinary_results_unchanged_and_approved_cap_recorded():
    from noah.scoring.engine import score_fixture
    from noah.scoring.fixture_io import parse_fixture

    raw = request(32)
    out = preview(raw)
    assert out["ordinaryOutput"] == score_fixture(parse_fixture(raw["fixture"]))
    assert out["releaseReady"] is False
    assert "Shared cap undecided" not in out["releaseBlockers"]
    assert out["starterPolicy"]["sharedPoolBps"] == 25
    assert out["starterPolicy"]["allowanceRao"] == 5 * R
    assert out["starterPolicy"]["maxSharedWeightU16"] == 163


def test_watchdog_requests_fallback_and_requires_chain_confirmation():
    from noah.research.starter_guard import decision

    raw = request()
    out = preview(raw)
    expiry = out["validUntilTimestamp"]
    before = decision(
        raw, None, now_timestamp=expiry - 1, observed_weights=out["proposedWeightsU16"]
    )
    assert before["action"] == "no-change"
    after = decision(raw, None, now_timestamp=expiry, observed_weights=out["proposedWeightsU16"])
    assert after["action"] == "replace-with-ordinary"
    assert after["desiredWeightsU16"] == out["ordinaryOutput"]["weightsU16"]
    assert after["requiresFinalizedReplacementProof"] and not after["liveProtection"]
    confirmed = decision(
        raw, None, now_timestamp=expiry, observed_weights=after["desiredWeightsU16"]
    )
    assert confirmed["action"] == "no-change"
    stale = decision(
        raw, None, now_timestamp=expiry + 86400, observed_weights=out["proposedWeightsU16"]
    )
    assert stale["action"] == "blocked-fresh-ordinary-score-required"
    assert stale["desiredWeightsU16"] is None


def test_full_shared_wallet_cycle_stops_all_siblings_at_five_total():
    raw = request(32)
    for row, seat in zip(raw["fixture"]["miners"][1:], raw["snapshot"]["seats"][1:]):
        row["coldkey"] = seat["coldkey"] = "siblings"
    state = None
    for epoch in range(100, 130):
        advance(raw, epoch)
        out = checked(raw, state)
        state = out["state"]
        assert out["allocatedStarterWeightU16"] <= SHARED_POOL_UNITS
        for seat in raw["snapshot"]["seats"][1:]:
            seat["spendableEarnedRao"] += POOL * out["proposedWeightsU16"][str(seat["uid"])] // U16
        if all(r["status"] == "exhausted" for r in out["starter"][1:]):
            break
    else:
        pytest.fail("shared wallet allowance did not complete")
    total = sum(s["spendableEarnedRao"] for s in raw["snapshot"]["seats"][1:])
    assert ALLOWANCE <= total < ALLOWANCE + POOL // U16 + 1
    assert sum(out["proposedWeightsU16"][str(i)] for i in range(1, 33)) == 0


def test_watchdog_never_requests_owner_or_uniform_fallback_when_all_scores_zero():
    from noah.research.starter_guard import decision

    raw = request()
    raw["fixture"]["miners"][0]["entryDepositRao"] = "0"
    for row in raw["fixture"]["miners"][0]["days"].values():
        row["noahDepositRao"] = "0"
    out = preview(raw)
    decision_row = decision(
        raw, None, now_timestamp=out["validUntilTimestamp"], observed_weights={"0": 65423, "1": 112}
    )
    assert decision_row["action"] == "blocked-no-ordinary-weight"
    assert not any(decision_row["desiredWeightsU16"].values())
    assert not decision_row["requiresFinalizedReplacementProof"]


def test_quarantined_ordinary_wallet_is_not_reintroduced_by_fallback():
    from noah.research.starter_guard import decision

    raw = request()
    raw["snapshot"]["seats"][0]["ownerExcluded"] = "unknown"
    out = preview(raw)
    assert out["ordinaryFallbackWeightsU16"]["0"] == 0
    guard = decision(raw, None, now_timestamp=out["validUntilTimestamp"], observed_weights={})
    assert guard["action"] == "blocked-no-ordinary-weight"


def test_local_guard_outage_simulation_stops_only_after_mock_confirmation():
    from noah.research.starter_guard import decision

    raw = request()
    out = preview(raw)
    installed = out["proposedWeightsU16"]
    earned = POOL * installed["1"] // U16
    command = decision(
        raw, None, now_timestamp=out["validUntilTimestamp"], observed_weights=installed
    )
    assert command["action"] == "replace-with-ordinary"
    # Failed submission: unchanged actual row, so an explicit retry remains necessary.
    assert (
        decision(
            raw, None, now_timestamp=out["validUntilTimestamp"] + 1, observed_weights=installed
        )["action"]
        == "replace-with-ordinary"
    )
    earned += POOL * installed["1"] // U16  # one further epoch before replacement
    installed = command["desiredWeightsU16"]  # MOCK finalized replacement; not Yuma proof
    assert installed["1"] == 0 and earned < R
    assert (
        decision(
            raw, None, now_timestamp=out["validUntilTimestamp"] + 2, observed_weights=installed
        )["action"]
        == "no-change"
    )


def test_two_newcomers_share_one_pool_not_a_cap_each():
    out = preview(request(2))
    assert out["proposedWeightsU16"] == {"0": 65372, "1": 82, "2": 81}
    assert out["allocatedStarterWeightU16"] == 163
    assert out["requestedStarterWeightsU16"] == {"1": 112, "2": 112}


def test_unused_pool_returns_to_ordinary_and_no_starters_is_identical():
    raw = request()
    one = preview(raw)
    assert one["allocatedStarterWeightU16"] == 112
    assert one["proposedWeightsU16"]["0"] == 65423
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 5 * R
    done = checked(raw)
    assert done["allocatedStarterWeightU16"] == 0
    assert done["proposedWeightsU16"] == done["ordinaryOutput"]["weightsU16"]
    empty = preview(request(0))
    assert empty["proposedWeightsU16"] == empty["ordinaryOutput"]["weightsU16"]


def test_near_finished_request_is_not_increased_to_fill_pool():
    raw = request(2)
    raw["snapshot"]["seats"][1]["spendableEarnedRao"] = 4999999999
    out = checked(raw)
    assert out["proposedWeightsU16"]["1"] == 1
    assert out["proposedWeightsU16"]["2"] == 112
    assert out["allocatedStarterWeightU16"] == 113
    assert out["proposedWeightsU16"]["0"] == U16 - 113


def test_cap_invariants_across_pool_sizes_and_all_live_counts():
    for count in range(1, 83):
        for pool in (R, POOL, 10000 * R):
            raw = request(count)
            raw["snapshot"]["miningPoolSample"]["amountRao"] = pool
            out = preview(raw)
            actual = sum(out["proposedWeightsU16"][str(i)] for i in range(1, count + 1))
            assert actual == out["allocatedStarterWeightU16"]
            assert 10000 * actual <= 25 * 65535
            assert sum(out["proposedWeightsU16"].values()) == 65535
            for uid, requested in out["requestedStarterWeightsU16"].items():
                assert 0 <= out["proposedWeightsU16"][uid] <= requested


def test_input_order_does_not_change_shares_or_wallet_limits():
    raw = request(32)
    expected = preview(raw)["proposedWeightsU16"]
    raw["snapshot"]["seats"].reverse()
    raw["fixture"]["miners"].reverse()
    raw["lifetimeEarnings"].reverse()
    assert preview(raw)["proposedWeightsU16"] == expected


def test_wrong_policy_or_old_uncapped_schema_cannot_be_silently_replayed():
    raw = request()
    wrong = copy.deepcopy(raw)
    wrong["policyId"] = "noah-starter-5noah-shared-50bps-v1"
    with pytest.raises(ValueError, match="policy mismatch"):
        preview(wrong)
    wrong = copy.deepcopy(raw)
    wrong["schema"] = "noah-starter-epoch-preview/2"
    with pytest.raises(ValueError, match="schema"):
        preview(wrong)
    state = preview(raw)["state"]
    state["policyId"] = "uncapped"
    with pytest.raises(ValueError, match="policy mismatch"):
        preview(raw, state)


def test_expired_capped_plan_requests_ordinary_fallback():
    from noah.research.starter_guard import decision

    raw = request(32)
    out = preview(raw)
    assert out["allocatedStarterWeightU16"] == 163
    guard = decision(
        raw,
        out["state"],
        now_timestamp=out["validUntilTimestamp"],
        observed_weights=out["proposedWeightsU16"],
    )
    assert guard["action"] == "replace-with-ordinary"
    assert guard["desiredWeightsU16"]["0"] == 65535
    assert sum(guard["desiredWeightsU16"][str(i)] for i in range(1, 33)) == 0


@pytest.mark.parametrize("siblings", [1, 2, 14, 32, 81])
def test_many_hotkeys_cannot_buy_a_larger_wallet_share(siblings):
    raw = request(siblings + 1)
    for row, seat in zip(raw["fixture"]["miners"][1:-1], raw["snapshot"]["seats"][1:-1]):
        row["coldkey"] = seat["coldkey"] = "wallet-a"
    raw["fixture"]["miners"][-1]["coldkey"] = "wallet-b"
    raw["snapshot"]["seats"][-1]["coldkey"] = "wallet-b"
    out = checked(raw)
    many = sum(r["starterWeightU16"] for r in out["starter"] if r["coldkey"] == "wallet-a")
    single = out["starter"][-1]["starterWeightU16"]
    assert (many, single) == (82, 81)
    assert sum(out["proposedWeightsU16"].values()) == U16


def test_wallet_finishing_redistributes_only_to_other_wallets_up_to_demand():
    from noah.research.starter_epoch import equal_capped
    assert equal_capped({"a": 1, "b": 112, "c": 112}, 163) == {"a": 1, "b": 81, "c": 81}
    assert equal_capped({"a": 0, "b": 5}, 163) == {"a": 0, "b": 5}
    assert equal_capped({"a": 0}, 163) == {"a": 0}
    assert equal_capped({"a": 112, "b": 112, "c": 1}, 163) == {"a": 81, "b": 81, "c": 1}
