"""Frozen daily-schedule selection is part of R1's offline proof graph."""

import pytest

from noah.chain.ss58 import address_of
from noah.core.calculation_record import encoded, retain, verify
from noah.core.report_publication import ReportPublicationRefused, retain_report, semantic_digest, verify_report
from noah.core.report_runtime import load_policy, select_policy, verify_selection
from noah.core.tests.test_report_publication import scenario
from noah.reports.schedule import _resolve
from noah.reports.tests.test_schedule import ANCHOR, Chain, schedule


def prepared(tmp_path):
    configuration = schedule()
    resolution = _resolve(configuration, [Chain(), Chain(head=9100)], 1)
    request, result, score, submission = scenario()
    policy = resolution["policy"]
    score["evalDay"] = "1"
    request.update(policy=policy, policy_sha256=semantic_digest(policy), mining_netuid=2,
                   eval_day=1, day_anchor_ts=ANCHOR,
                   evaluation={"block_number": 9100, "block_hash": Chain().block_hash(9100)[2:],
                               "timestamp_ms": (ANCHOR + 9100 * 12) * 1000})
    for seat in request["scored_seats"]:
        seat["observed_at"] = dict(block_number=7250, block_hash=resolution["round"]["block_hash"])
    fixture = dict(schema="noah-fixture/6", evalDay="1", miners=[])
    source = retain(tmp_path, fixture=fixture, agreed_text=encoded(score).decode(), vector={5: 65535, 6: 0},
                    context=dict(day=1, height=9000, genesis_hash=request["genesis_hash"], netuid=2,
                                 ark_address=address_of(bytes.fromhex(configuration["ark_coldkey"])),
                                 designated_validator_hotkey=address_of(bytes.fromhex(configuration["ark_validator_hotkey"])),
                                 day_start_utc_unix=ANCHOR + 86400))
    request["calculation"] = dict(reference=source,
                                  fixture_sha256=verify(tmp_path, source)["artifacts"]["fixture"]["sha256"])
    result.update(request_sha256=semantic_digest(request), policy_sha256=request["policy_sha256"])
    round_sha = semantic_digest(resolution["round"])
    result["rounds"][0].update(round_sha256=round_sha, evaluation=request["evaluation"])
    for decision in result["scored_decisions"]:
        decision["rounds"][0]["round_sha256"] = round_sha
    selection = dict(schema="noah-report-selection/1", configuration=configuration,
                     configuration_sha256=semantic_digest(configuration), resolution=resolution)
    package = dict(path=f"reports/{result['manifest_sha256']}/manifest.json", sha256=result["manifest_sha256"])
    return source, request, result, submission, package, selection


def test_frozen_schedule_and_raw_boundary_replay_are_retained_in_r1(tmp_path):
    source, request, result, submission, package, selection = prepared(tmp_path)
    ref, vector = retain_report(tmp_path, source, request, package, submission,
                                replay=lambda *_: result, selection=selection)
    assert vector == {5: 0, 6: 65535}
    assert verify_report(tmp_path, ref, replay=lambda *_: result)["selection"] == selection


@pytest.mark.parametrize("mutation", [
    lambda selection: selection["configuration"].__setitem__("effective_day", 0),
    lambda selection: selection["resolution"]["policy"]["required_rounds"][0].__setitem__("commit_open_ms", 1),
    lambda selection: selection["resolution"]["boundary_proof"]["readers"][0]["receipts"].pop(),
])
def test_changed_frozen_schedule_or_boundary_refused_without_network(tmp_path, mutation):
    source, request, result, submission, package, selection = prepared(tmp_path)
    mutation(selection)
    with pytest.raises((ValueError, ReportPublicationRefused)):
        verify_selection(selection, request)


def test_schedule_configuration_loads_purely_and_selects_via_isolated_bridge(tmp_path, monkeypatch):
    source, request, result, submission, package, selection = prepared(tmp_path)
    path = tmp_path / "configuration.json"
    path.write_bytes(encoded(selection["configuration"]))
    configuration = load_policy(path)
    calls = []
    def resolve(config, endpoints, day):
        calls.append((config, endpoints, day))
        return selection["resolution"]
    monkeypatch.setattr("noah.core.report_runtime.resolve_schedule", resolve)
    policy, saved = select_policy(configuration, ["reader-a", "reader-b"], 1, ANCHOR)
    assert policy == request["policy"] and saved == selection
    assert len(calls) == 1
