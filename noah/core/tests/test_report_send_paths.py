"""Real adapter transport/fallback boundaries, with no report proof fabricated."""

import copy
import json
from types import SimpleNamespace

import pytest

from noah.core.chain_adapter import ChainAdapter
from noah.core.publish import NotOnChain, PublicationRefused
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.report_publication import ReportPublicationRefused
from noah.core.report_runtime import ELIGIBILITY
from noah.core.tests._prepared_transport_fake import install
from noah.points.outage import authorize, bounds, record_failure, republish_plain
from noah.points.tests.test_no_pool import ordinary_published
from noah.reports.tests.test_adjudication_contract import request
from noah.starter.runtime import guard_tick
from noah.starter.tests.test_runtime import ready, send_overlay

GENESIS = "0x" + "11" * 32


def enable_reports(adapter, tmp_path, day):
    policy = copy.deepcopy(request()["policy"])
    policy["effective_day"] = day
    policy["eligibility_policy_version"] = ELIGIBILITY
    for contract in policy["required_rounds"]:
        contract["eligibility_policy_version"] = ELIGIBILITY
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    adapter.report_gate_policy_path = str(path)
    adapter.report_gate_enabled = True


@pytest.fixture(autouse=True)
def prepared_transport(monkeypatch):
    install(monkeypatch)


def test_legacy_guard_cannot_fallback_to_unfiltered_r0_after_cutoff(tmp_path):
    a, client, raw, source = ready(tmp_path, genesis=GENESIS)
    send_overlay(a, client, raw, source)
    before = len(client.calls)
    enable_reports(a, tmp_path, 1)
    client.epoch = client.block = client.finalized_head = 560
    with pytest.raises(ReportPublicationRefused, match="selected report enforcement"):
        guard_tick(a)
    assert len(client.calls) == before
    assert a._publish_intent.load() is None


def test_points_outage_cannot_refresh_unfiltered_r0_and_deadline_is_not_extended(tmp_path):
    from noah.chain.foreign_value import ForeignDepositUnpriced

    a, client, _, _, day = ordinary_published(tmp_path, genesis=GENESIS)
    source = a._calculation_reference
    due, expiry = bounds(a, day)
    client.block = client.finalized_head = 201
    a.runner_chain.block_timestamp = lambda h: expiry - 1
    assert record_failure(a, ForeignDepositUnpriced("reference unavailable"))
    assert authorize(a, source, expiry - 1) == expiry
    enable_reports(a, tmp_path, day)
    before = len(client.calls)
    with pytest.raises(ReportPublicationRefused, match="selected report enforcement"):
        republish_plain(a, 200, 201, before_send=lambda: None)
    assert len(client.calls) == before
    assert a._publish_intent.load() is None
    assert bounds(a, day) == (due, expiry)
    with pytest.raises(PublicationStatusUnknown, match="24 hours"):
        authorize(a, source, expiry)
    assert record_failure(a, ReportPublicationRefused("missing authenticated package")) is False
    state = json.loads((tmp_path / "scores/points-outage.json").read_text())
    assert state["reason"] == "UnsafeFailure"
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        authorize(a, source, expiry - 1)


def test_final_direct_transport_rechecks_report_source(tmp_path):
    a, client, _, vector, day = ordinary_published(tmp_path, genesis=GENESIS)
    enable_reports(a, tmp_path, day)
    before = len(client.calls)
    with pytest.raises(PublicationRefused, match="nothing submitted") as error:
        a.submit_direct(list(vector), list(vector.values()), a.netuid)
    assert isinstance(error.value.__cause__, ReportPublicationRefused)
    assert len(client.calls) == before
    assert a._publish_intent.load() is None


def test_missing_reference_refuses_before_nonce_or_new_intent(tmp_path):
    a, client, _, vector, day = ordinary_published(tmp_path, genesis=GENESIS)
    enable_reports(a, tmp_path, day)
    a._calculation_reference = None
    before = len(client.calls)
    with pytest.raises(PublicationRefused, match="retained calculation"):
        a.publish_direct(vector, 99, refresh_epoch=200, source_day=day)
    assert len(client.calls) == before
    assert a._publish_intent.load() is None


def test_old_pending_payload_reconciles_before_new_report_proof_requirement(tmp_path):
    a, client, _, vector, day = ordinary_published(tmp_path, genesis=GENESIS)
    client.block = client.finalized_head = 201
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        a.publish_direct(vector, 99, refresh_epoch=200, source_day=day)
    original = a._publish_intent.load()
    assert original is not None
    before = len(client.calls)
    client.lost_reply = False
    enable_reports(a, tmp_path, day)
    a._calculation_reference = None
    assert a._already_published_direct(99) is True
    assert a._publish_intent.load() is None
    assert len(client.calls) == before
    path = tmp_path / f"scores/epoch-publications/200/day-{day}.submitted.json"
    retained = json.loads(path.read_text())["direct_intent"]
    assert retained["intent_id"] == original["intent_id"]
    assert retained["vector"] == original["vector"]
    assert retained["calculation_evidence"] == original["calculation_evidence"]


@pytest.mark.parametrize("operation", ["publish", "submit"])
def test_report_on_refuses_sealed_entry_points_even_if_startup_was_bypassed(operation):
    adapter = SimpleNamespace(report_gate_enabled=True)
    with pytest.raises(PublicationRefused, match="direct publication|sealed transport"):
        if operation == "publish":
            ChainAdapter.publish(adapter, {3: 65535}, 99)
        else:
            ChainAdapter.submit(adapter, b"sealed", 123, 82)
