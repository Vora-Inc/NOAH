"""Report allocation/recovery boundaries using explicit synthetic provider proofs."""

import copy
from types import SimpleNamespace

import pytest

from noah.chain.ss58 import address_of
from noah.core.calculation_record import artifact, encoded, retain, verify
from noah.core.report_publication import (
    ReportPublicationRefused, derive_vector, retain_report, semantic_digest,
    validate_result, verify_report, verify_starter_input,
)
from noah.core.report_runtime import (
    bind_activation, check_before_send, configure, prepare_daily, starter_input,
)
from noah.reports.tests.test_adjudication_contract import request as base_request


def scenario():
    request = base_request()
    second = copy.deepcopy(request["scored_seats"][0])
    second.update(uid=6, hotkey="aa" * 32, coldkey="bb" * 32)
    request["scored_seats"].append(second)
    round_sha = semantic_digest(request["policy"]["required_rounds"][0])
    decisions = []
    for seat in request["scored_seats"]:
        decision = {k: seat[k] for k in ("uid", "hotkey", "coldkey", "registration_block")}
        failure = seat["uid"] == 5
        decision.update(effect="zero" if failure else "preserve",
                        outcome="confirmed_failure" if failure else "verified",
                        rounds=[dict(round_sha256=round_sha,
                                     effect="zero" if failure else "preserve",
                                     outcome="confirmed_failure" if failure else "verified",
                                     status="MISMATCH" if failure else "VERIFIED",
                                     reason="outputs_differ" if failure else "exact_match")])
        decisions.append(decision)
    result = dict(schema="noah.report-adjudication-result/1", manifest_sha256="cc" * 32,
                  request_sha256=semantic_digest(request), policy_sha256=request["policy_sha256"],
                  status="COMPLETE", scored_decisions=decisions,
                  rounds=[dict(round_sha256=round_sha, required_roster_sha256="dd" * 32,
                               evaluation=request["evaluation"],
                               readers={"reader_a": "ee" * 32, "reader_b": "ff" * 32},
                               engine_sha256="01" * 32, calculation_sha256="02" * 32,
                               verdicts_sha256="03" * 32, resolved=True)])
    score = dict(schema="noah-score-output/6", evalDay="0",
                 miners=[dict(uid=row["uid"], coldkey=address_of(bytes.fromhex(row["coldkey"])),
                              payWeightFp=str(10**20 if row["uid"] == 5 else 1),
                              scoreFp="123", state={"debt": "99"})
                         for row in request["scored_seats"]],
                 weightsU16={"5": 65535, "6": 0})
    hotkeys = {str(row["uid"]): address_of(bytes.fromhex(row["hotkey"]))
               for row in request["scored_seats"]}
    submission = dict(live_uids=[5, 6], owner_uids=[], fixture_hotkeys=hotkeys,
                      chain_hotkeys=dict(hotkeys))
    return request, result, score, submission


def stored(tmp_path):
    request, result, score, submission = scenario()
    fixture = dict(schema="noah-fixture/6", evalDay="0", miners=[])
    source = retain(tmp_path, fixture=fixture, agreed_text=encoded(score).decode(),
                    vector={5: 65535, 6: 0},
                    context=dict(day=0, height=100, genesis_hash=request["genesis_hash"],
                                 netuid=82, ark_address=address_of(bytes.fromhex("55" * 32)),
                                 designated_validator_hotkey=address_of(bytes.fromhex("66" * 32)),
                                 day_start_utc_unix=request["day_anchor_ts"]))
    request["calculation"] = dict(reference=source,
                                  fixture_sha256=verify(tmp_path, source)["artifacts"]["fixture"]["sha256"])
    result["request_sha256"] = semantic_digest(request)
    package = dict(path=f"reports/{result['manifest_sha256']}/manifest.json",
                   sha256=result["manifest_sha256"])
    return request, result, score, submission, source, package


def test_filter_before_rounding_preserves_tiny_surviving_pay_and_raw_accounting():
    request, result, score, submission = scenario()
    original = copy.deepcopy(score)
    assert derive_vector(score, request, result, result["manifest_sha256"], submission) == {5: 0, 6: 65535}
    assert score == original


def test_all_filtered_declines_instead_of_zero_keepalive():
    request, result, score, submission = scenario()
    result["scored_decisions"][1].update(effect="zero", outcome="confirmed_failure")
    assert derive_vector(score, request, result, result["manifest_sha256"], submission) == {}


def test_unresolved_survivor_does_not_restore_a_known_failed_miner():
    request, result, score, submission = scenario()
    result["status"] = "UNRESOLVED"
    result["scored_decisions"][1]["outcome"] = "unresolved"
    assert derive_vector(score, request, result, result["manifest_sha256"], submission) == {5: 0, 6: 65535}


@pytest.mark.parametrize("field,value", [("uid", 7), ("hotkey", "ab" * 32),
                                         ("coldkey", "ab" * 32), ("registration_block", 51)])
def test_crossed_incarnation_rejected(field, value):
    request, result, score, submission = scenario()
    result["scored_decisions"][0][field] = value
    with pytest.raises(ReportPublicationRefused, match="incarnation"):
        derive_vector(score, request, result, result["manifest_sha256"], submission)


def test_turnover_is_checked_after_filtering():
    request, result, score, submission = scenario()
    submission["chain_hotkeys"]["6"] = address_of(bytes.fromhex("00" * 32))
    assert derive_vector(score, request, result, result["manifest_sha256"], submission) == {}


@pytest.mark.parametrize("field", ["manifest_sha256", "request_sha256", "policy_sha256"])
def test_crossed_provider_identity_refused(field):
    request, result, score, submission = scenario()
    result[field] = "00" * 32
    with pytest.raises(ReportPublicationRefused, match="binding"):
        validate_result(result, request, "cc" * 32)


def test_r1_replay_preserves_canonical_r0_and_rejects_changed_provider(tmp_path):
    request, result, score, submission, source, package = stored(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("day-*")}
    ref, vector = retain_report(tmp_path, source, request, package, submission,
                                replay=lambda *_: result)
    assert vector == {5: 0, 6: 65535}
    assert verify_report(tmp_path, ref, replay=lambda *_: result)["source"] == source
    assert {p.name: p.read_bytes() for p in tmp_path.glob("day-*")} == before
    altered = copy.deepcopy(result)
    altered["scored_decisions"][0].update(effect="preserve", outcome="verified")
    with pytest.raises(ReportPublicationRefused, match="reproduce"):
        verify_report(tmp_path, ref, replay=lambda *_: altered)
    assert artifact(tmp_path, verify(tmp_path, ref), "agreed_score") == score


def test_missing_package_cannot_become_unresolved_preserve(tmp_path):
    request, result, score, submission, source, package = stored(tmp_path)
    def missing(*_):
        raise ReportPublicationRefused("package missing")
    with pytest.raises(ReportPublicationRefused, match="missing"):
        retain_report(tmp_path, source, request, package, submission, replay=missing)


def test_starter_input_cannot_invent_remove_or_bypass_exclusions(tmp_path):
    request, result, score, submission, source, package = stored(tmp_path)
    replay = lambda *_: result
    ref, _ = retain_report(tmp_path, source, request, package, submission, replay=replay)
    rows = [dict(uid=seat["uid"], coldkey=address_of(bytes.fromhex(seat["coldkey"])),
                 hotkey=address_of(bytes.fromhex(seat["hotkey"])), registrationBlock=50)
            for seat in request["scored_seats"]]
    raw = dict(schema="noah-starter-epoch-preview/5", snapshot={"seats": rows},
               reportGate=dict(schema="noah-starter-report-exclusions/1", proofSha256=ref["sha256"],
                               evalDay=0, registrations=rows, exclusions=[rows[0]]))
    verify_starter_input(tmp_path, ref, raw, replay=replay)
    raw["reportGate"]["exclusions"] = []
    with pytest.raises(ReportPublicationRefused, match="exclusions differ"):
        verify_starter_input(tmp_path, ref, raw, replay=replay)
    raw["schema"] = "noah-starter-epoch-preview/4"
    with pytest.raises(ReportPublicationRefused, match="bypass"):
        verify_starter_input(tmp_path, ref, raw, replay=replay)


def test_off_preparation_reads_nothing_and_preserves_object_identity():
    vector = {5: 100}
    assert prepare_daily(SimpleNamespace(), vector) is vector


def test_r0_starter_behavior_still_accepts_no_report_proof(tmp_path):
    request, result, score, submission, source, package = stored(tmp_path)
    verify_starter_input(tmp_path, source, {"schema": "noah-starter-epoch-preview/4"})
    with pytest.raises(ReportPublicationRefused, match="lack report proof"):
        verify_starter_input(tmp_path, source, {"schema": "noah-starter-epoch-preview/5"})


def test_off_configuration_does_not_read_an_unused_policy(monkeypatch):
    def forbidden(*_):
        raise AssertionError("OFF loaded a report policy")
    monkeypatch.setattr("noah.core.report_runtime.load_policy", forbidden)
    adapter = SimpleNamespace()
    configure(adapter, SimpleNamespace(report_gate_enabled=False, report_gate_policy="missing"))
    assert adapter.report_gate_enabled is False


def test_sealed_report_enforcement_refused_before_policy_or_chain_reads(monkeypatch):
    monkeypatch.setattr("noah.core.report_runtime.load_policy", lambda *_: pytest.fail("policy opened"))
    with pytest.raises(ReportPublicationRefused, match="direct publication"):
        configure(SimpleNamespace(), SimpleNamespace(report_gate_enabled=True, publish_mode="sealed"))


def _sending(tmp_path, request, submission):
    owners = {submission["chain_hotkeys"][str(row["uid"])]: address_of(bytes.fromhex(row["coldkey"]))
              for row in request["scored_seats"]}
    return SimpleNamespace(
        report_gate_enabled=True, report_gate_policy_path="never-read-in-test", netuid=82,
        day_anchor_ts=request["day_anchor_ts"], _submitted_record_dir=tmp_path,
        runner_chain=SimpleNamespace(
            head_block=lambda: request["evaluation"]["block_number"],
            block_hash=lambda _: "0x" + request["evaluation"]["block_hash"],
            genesis_hash=lambda: "0x" + request["genesis_hash"],
            seat_hotkeys=lambda _: {int(k): v for k, v in submission["chain_hotkeys"].items()},
            hotkey_owners=lambda *_: owners,
            registration_blocks=lambda _: {5: 50, 6: 50}))


def test_send_requires_r1_even_if_raw_vector_was_otherwise_valid(tmp_path, monkeypatch):
    request, result, score, submission, source, package = stored(tmp_path)
    adapter = _sending(tmp_path, request, submission)
    monkeypatch.setattr("noah.core.report_runtime.load_policy", lambda _: request["policy"])
    with pytest.raises(ReportPublicationRefused, match="lacks selected"):
        check_before_send(adapter, source, {5: 65535, 6: 0})


def test_new_send_checks_policy_and_live_registration_but_offline_recovery_does_not(tmp_path, monkeypatch):
    request, result, score, submission, source, package = stored(tmp_path)
    ref, vector = retain_report(tmp_path, source, request, package, submission, replay=lambda *_: result)
    monkeypatch.setattr("noah.core.report_publication.replay_package", lambda *_: result)
    monkeypatch.setattr("noah.core.report_runtime.load_policy", lambda _: request["policy"])
    adapter = _sending(tmp_path, request, submission)
    check_before_send(adapter, ref, vector)
    adapter.runner_chain.registration_blocks = lambda _: {5: 50, 6: 51}
    with pytest.raises(ReportPublicationRefused, match="incarnation changed"):
        check_before_send(adapter, ref, vector)
    changed = copy.deepcopy(request["policy"])
    changed["required_rounds"][0]["scoring_release"] = "another-candidate"
    monkeypatch.setattr("noah.core.report_runtime.load_policy", lambda _: changed)
    with pytest.raises(ReportPublicationRefused, match="policy differs"):
        check_before_send(adapter, ref, vector)
    # Historical recovery replays the original request; it ignores live policy,
    # the current registration and the current report ON/OFF setting.
    assert verify_report(tmp_path, ref)["source"] == source


def test_future_activation_preserves_prior_daily_and_starter_vectors(tmp_path, monkeypatch):
    request, result, score, submission, source, package = stored(tmp_path)
    future = {**request["policy"], "effective_day": 1}
    monkeypatch.setattr("noah.core.report_runtime.load_policy", lambda _: future)
    adapter = _sending(tmp_path, request, submission)
    adapter._calculation_reference = source
    raw_vector = {5: 65535, 6: 0}
    assert prepare_daily(adapter, raw_vector) is raw_vector
    check_before_send(adapter, source, raw_vector)
    raw = {"schema": "noah-starter-epoch-preview/4"}
    assert starter_input(adapter, source, raw) is raw
    assert (tmp_path / "report-state/activation.json").is_file()
    assert not list((tmp_path / "reports").glob("*"))
    assert not list((tmp_path / "report-state").glob("day-*.request.json"))


def test_activation_cutoff_cannot_move_on_restart(tmp_path):
    request, result, score, submission, source, package = stored(tmp_path)
    future = {**request["policy"], "effective_day": 1}
    adapter = _sending(tmp_path, request, submission)
    bind_activation(adapter, future, persist=False)
    assert not (tmp_path / "report-state").exists()  # read-only configure
    bind_activation(adapter, future, persist=True)
    before = (tmp_path / "report-state/activation.json").read_bytes()
    for cutoff in (0, 2):
        restarted = _sending(tmp_path, request, submission)
        with pytest.raises(ReportPublicationRefused, match="retained.*cutoff differs"):
            bind_activation(restarted, {**future, "effective_day": cutoff}, persist=False)
        assert (tmp_path / "report-state/activation.json").read_bytes() == before


def test_existing_report_proof_cannot_be_relabeled_preactivation(tmp_path, monkeypatch):
    request, result, score, submission, source, package = stored(tmp_path)
    ref, vector = retain_report(tmp_path, source, request, package, submission, replay=lambda *_: result)
    monkeypatch.setattr("noah.core.report_publication.replay_package", lambda *_: result)
    monkeypatch.setattr("noah.core.report_runtime.load_policy",
                        lambda _: {**request["policy"], "effective_day": 1})
    with pytest.raises(ReportPublicationRefused, match="bypass an existing"):
        check_before_send(_sending(tmp_path, request, submission), ref, vector)
    assert not (tmp_path / "report-state/activation.json").exists()


def test_new_scored_incarnation_after_snapshot_is_not_required_for_that_round():
    request, result, score, submission = scenario()
    seat = request["scored_seats"][1]
    seat["registration_block"] = 101
    seat["observed_at"] = {"block_number": 110, "block_hash": request["evaluation"]["block_hash"]}
    result["request_sha256"] = semantic_digest(request)
    row = result["scored_decisions"][1]
    row.update(registration_block=101, outcome="not_required")
    row["rounds"][0].update(outcome="not_required", status=None, reason="registered_after_snapshot")
    assert derive_vector(score, request, result, result["manifest_sha256"], submission) == {5: 0, 6: 65535}


def test_new_unscored_seat_after_r0_does_not_block_or_receive_old_weight():
    request, result, score, submission = scenario()
    submission["live_uids"].append(7)
    submission["chain_hotkeys"]["7"] = address_of(bytes.fromhex("de" * 32))
    assert derive_vector(score, request, result, result["manifest_sha256"], submission) == {5: 0, 6: 65535, 7: 0}


def test_report_policy_cannot_calculate_another_ark_than_the_financial_source(tmp_path):
    request, result, score, submission, source, package = stored(tmp_path)
    request["policy"]["required_rounds"][0]["ark_coldkey"] = "ab" * 32
    request["policy_sha256"] = semantic_digest(request["policy"])
    result["request_sha256"] = semantic_digest(request)
    with pytest.raises(ReportPublicationRefused, match="Ark identity differs"):
        retain_report(tmp_path, source, request, package, submission, replay=lambda *_: result)


def test_noncanonical_submission_uid_alias_cannot_replace_a_frozen_hotkey():
    request, result, score, submission = scenario()
    submission["fixture_hotkeys"]["06"] = address_of(bytes.fromhex("cd" * 32))
    with pytest.raises(ReportPublicationRefused, match="hotkey identity"):
        derive_vector(score, request, result, result["manifest_sha256"], submission)


def test_validator_loop_calls_weekly_publication_and_stays_off_by_default(tmp_path, monkeypatch):
    from noah.core.loop import TickResult, run_forever
    import noah.core.epoch_work as epoch_work
    import noah.core.report_runtime as report_runtime
    seen = []
    real = report_runtime.publish_weekly
    monkeypatch.setattr(epoch_work, 'maintain', lambda *args, **kwargs: None)
    monkeypatch.setattr(report_runtime, 'publish_weekly', lambda chain: seen.append(chain) or {'action': 'retained'})
    monkeypatch.setattr(
        'noah.core.loop.tick',
        lambda *args, **kwargs: (TickResult(False, None, 'waiting', 'idle', last_attempted_tempo=360), None),
    )
    chain = object()
    run_forever(chain, settle_blocks=10, state_path=str(tmp_path / 'loop.json'),
                lock_path=str(tmp_path / 'loop.lock'), iterations=1, sleep=lambda _seconds: None)
    assert seen == [chain]
    assert real(type('Adapter', (), {})()) is None


def test_boolean_registration_is_not_an_integer_identity():
    request, result, score, submission = scenario()
    request["scored_seats"][0]["registration_block"] = 1
    result["request_sha256"] = semantic_digest(request)
    result["scored_decisions"][0]["registration_block"] = True
    with pytest.raises(ReportPublicationRefused, match="must use integers"):
        validate_result(result, request, result["manifest_sha256"])
