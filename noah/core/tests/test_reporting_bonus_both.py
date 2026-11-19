"""Daily optional bonus: raw accounting stays immutable and recovery replays it."""

import copy

import pytest

from noah.core.calculation_record import artifact, verify
from noah.core.report_publication import (
    ReportPublicationRefused,
    derive_vector,
    retain_report,
    verify_report,
)
from noah.core.tests.test_report_publication import scenario, stored
from noah.scoring.pay import pay_weight, weights_u16


def scored(mode="legacy", maturity=1000000, missed=0):
    request, result, score, submission = scenario()
    for row in score["miners"]:
        row.update(scoreFp=str(100 * 10**9), multiplierFp=str(10**9), doorOpen=True)
        if mode == "legacy":
            row.pop("state", None)
            row["payWeightFp"] = str(pay_weight(100 * 10**9, 10**9))
        else:
            row["state"] = dict(maturity=str(maturity), missed=str(missed))
            row["payWeightFp"] = str(100 * 10**9 if missed < 2 else 0)
    return request, result, score, submission


@pytest.mark.parametrize(
    "mode,maturity,missed,extra",
    [
        ("legacy", 1000000, 0, None),
        ("points", 1000000, 0, 20 * 10**9),
        ("points", 10000, 0, 200000000),
        ("points", 1000000, 1, 10 * 10**9),
        ("points", 1000000, 2, 0),
    ],
)
def test_bonus_does_not_zero_missing_report_and_respects_v2_protections(
    mode, maturity, missed, extra
):
    request, result, score, submission = scored(mode, maturity, missed)
    before = copy.deepcopy(score)
    expected = {r["uid"]: int(r["payWeightFp"]) for r in score["miners"]}
    expected[6] = pay_weight(120 * 10**9, 10**9) if mode == "legacy" else expected[6] + extra
    assert derive_vector(
        score, request, result, result["manifest_sha256"], submission, reward="bonus-20"
    ) == weights_u16(expected)
    assert score == before


@pytest.mark.parametrize("outcome", ["unresolved", "not_required", "confirmed_failure"])
def test_only_verified_work_earns_bonus(outcome):
    request, result, score, submission = scored()
    result["scored_decisions"][1].update(
        outcome=outcome, effect="zero" if outcome == "confirmed_failure" else "preserve"
    )
    assert derive_vector(
        score, request, result, result["manifest_sha256"], submission, reward="bonus-20"
    ) == weights_u16(
        {5: int(score["miners"][0]["payWeightFp"]), 6: int(score["miners"][1]["payWeightFp"])}
    )


def test_retained_bonus_replays_without_mutating_twin_score(tmp_path):
    request, result, score, submission, source, package = stored(tmp_path)
    # Real retention API must carry the bonus rule independently of raw score.
    from noah.core.calculation_record import encoded, retain

    _, _, score, _ = scored()
    old = verify(tmp_path, source)
    source = retain(
        tmp_path,
        fixture=artifact(tmp_path, old, "fixture"),
        agreed_text=encoded(score).decode(),
        vector={5: 32768, 6: 32767},
        context=artifact(tmp_path, old, "context"),
        canonical_day=False,
    )
    request["calculation"].update(
        reference=source, fixture_sha256=verify(tmp_path, source)["artifacts"]["fixture"]["sha256"]
    )
    from noah.core.report_publication import semantic_digest

    result["request_sha256"] = semantic_digest(request)
    ref, vector = retain_report(
        tmp_path, source, request, package, submission, replay=lambda *_: result, reward="bonus-20"
    )
    assert vector[5] > 0 and vector[6] > vector[5]
    assert verify_report(tmp_path, ref, replay=lambda *_: result)["reward"] == "bonus-20"
    assert artifact(tmp_path, verify(tmp_path, ref), "agreed_score") == score
    changed = copy.deepcopy(result)
    changed["scored_decisions"][1].update(outcome="confirmed_failure", effect="zero")
    with pytest.raises(ReportPublicationRefused):
        verify_report(tmp_path, ref, replay=lambda *_: changed)


def test_bonus_setting_is_explicit_and_cannot_be_misspelled():
    from noah.core.settings import Settings, SettingsError
    from noah.core.tests.test_settings import COMPLETE

    s = Settings.from_mapping(
        dict(
            COMPLETE,
            NOAH_REPORT_REWARD="bonus-20",
            NOAH_REPORT_GATE_ENABLED="true",
            NOAH_REPORT_GATE_POLICY="policy.json",
        )
    )
    assert s.report_reward == "bonus-20"
    with pytest.raises(SettingsError):
        Settings.from_mapping(dict(COMPLETE, NOAH_REPORT_REWARD="bonus-200"))


def test_historical_round_not_passed_to_collector():
    from noah.core.report_runtime import select_policy

    request, _, _, _ = scored()
    current = request["policy"]["required_rounds"][0]
    old = copy.deepcopy(current)
    # The policy remains valid, but its old block must never trigger an archive read.
    for key in old:
        if key.endswith("_ms"):
            old[key] -= 86400000
    from datetime import datetime, timezone

    old["round_date"] = (
        datetime.fromtimestamp(old["block_timestamp_ms"] / 1000, timezone.utc).date().isoformat()
    )
    policy = copy.deepcopy(request["policy"])
    policy["required_rounds"].insert(0, old)
    selected, _ = select_policy(
        policy, [], request["eval_day"] + 1, request["day_anchor_ts"] - 86400
    )
    assert selected["required_rounds"] == [current]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_invalid_evidence_reaches_verifier_without_eager_hash_and_keeps_base(value):
    from noah.reports.adjudication import resolve_round, resolve_seats
    from noah.reports.tests.test_adjudication import paired
    from noah.reports.verification import verify_collected

    request, contract, readers = paired()
    bad = dict(collector="noah.reports.backing.collector", collection={}, extra=value)
    verdict = verify_collected(contract, bad)
    assert verdict["status"] == "UNVERIFIABLE"
    assert verdict["evidence_sha256"] is None
    hot = request["scored_seats"][0]["hotkey"]
    for reader in readers.values():
        reader["report"]["verdicts"][hot] = verdict
    _, round_artifact = resolve_round(request, contract, readers)
    assert resolve_seats(request, [round_artifact])[0]["outcome"] == "unresolved"
    # An unavailable/invalid report earns no bonus and never zeros baseline.
    request, result, score, submission = scored()
    for row in result["scored_decisions"]:
        row.update(effect="preserve", outcome="unresolved")
    assert derive_vector(
        score, request, result, result["manifest_sha256"], submission, reward="bonus-20"
    ) == weights_u16({r["uid"]: int(r["payWeightFp"]) for r in score["miners"]})


def test_owner_and_legacy_zero_multiplier_cannot_gain_from_report():
    request, result, score, submission = scored()
    score["miners"][1].update(multiplierFp="0", payWeightFp="0")
    assert (
        derive_vector(
            score, request, result, result["manifest_sha256"], submission, reward="bonus-20"
        )[6]
        == 0
    )
    score["miners"][1].update(
        multiplierFp=str(10**9), payWeightFp=str(pay_weight(100 * 10**9, 10**9))
    )
    submission["owner_uids"] = [6]
    score["miners"][1]["payWeightFp"] = "0"
    assert (
        derive_vector(
            score, request, result, result["manifest_sha256"], submission, reward="bonus-20"
        )[6]
        == 0
    )


def test_catching_up_returns_current_bonus_without_banking_missed_bonus():
    from noah.points.ledger import settle
    from noah.points.onboarding import RULES, initial_state
    from noah.scoring.reporting_bonus import bonus_pay

    state = initial_state()
    state = settle(state, 100 * 10**9, 75 * 10**9, rules=RULES)
    for earned, deposit, missed in [(100 * 10**9, 0, 1), (100 * 10**9, 0, 2), (0, 75 * 10**9, 0)]:
        state = settle(state, earned, deposit, rules=RULES)
        assert state.missed == missed
        row = dict(
            payWeightFp=str(state.points * 1000),
            doorOpen=True,
            state=dict(maturity=str(state.maturity), missed=str(state.missed)),
        )
        increment = (
            20 * state.maturity if not missed else 10 * state.maturity if missed == 1 else 0
        ) * 1000
        assert bonus_pay(row, True) == state.points * 1000 + increment
        assert bonus_pay(row, False) == state.points * 1000
