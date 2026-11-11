import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.chain.foreign_value import ForeignDepositUnpriced
from noah.core.publish_intent import PublicationStatusUnknown
from noah.points.engine import INPUT_SCHEMA, POLICY
from noah.points.outage import authorize, record_failure, record_recovery
from noah.starter.publication import check_before_send, retain_overlay
from noah.starter.runtime import republish
from noah.starter.tests.test_epoch_clock import request_live
from noah.starter.tests.test_runtime import ready, send_overlay


def setup(tmp_path, monkeypatch):
    from noah.core.calculation_record import retain
    from noah.core.tests._prepared_transport_fake import install

    install(monkeypatch)
    raw = request_live()
    raw["fixture"]["schema"] = INPUT_SCHEMA
    manifest = dict(policy=POLICY, sha256="pinned-policy", cutoff_day=0)

    def points_retain(*a, **kw):
        kw["context"] = {**kw["context"], "points_policy": manifest}
        return retain(*a, **kw)

    with (
        patch("noah.starter.tests.test_runtime.request_live", return_value=raw),
        patch("noah.starter.tests.test_runtime.retain", side_effect=points_retain),
    ):
        a, c, raw, source = ready(tmp_path)
    a.points_policy = SimpleNamespace(manifest=manifest)
    a.ark_coldkey = "ark"
    a.designated_validator_hotkey = "validator"
    send_overlay(a, c, raw, source)
    now = [a.day_anchor_ts + 3 * 86400]
    a.runner_chain.block_timestamp = lambda b: now[0]
    c.epoch = c.block = c.finalized_head = 560
    return a, c, raw, source, now


def test_actual_adapter_refresh_then_expiry_and_restart(tmp_path, monkeypatch):
    a, c, raw, source, now = setup(tmp_path, monkeypatch)
    assert record_failure(a, ForeignDepositUnpriced("reference unavailable"))
    assert republish(a, 560, 560) == "points_outage_refreshed"
    assert dict(c.stored)[4] == 0  # no repeated starter allowance
    assert a._publish_intent.load() is None
    assert republish(a, 560, 560) == "already_published_this_epoch"
    now[0] += 86399
    assert authorize(a, source, now[0]) == a.day_anchor_ts + 4 * 86400
    saved = Path(a._submitted_record_dir) / "points-outage.json"
    first = saved.read_bytes()
    record_failure(a, ForeignDepositUnpriced("another retry/restart"))
    assert saved.read_bytes() == first
    now[0] += 1
    with pytest.raises(PublicationStatusUnknown, match="24 hours"):
        authorize(a, source, now[0])
    before = len(c.calls)
    # Independent guard cannot bypass the same fixed expiry.
    ref = retain_overlay(a._submitted_record_dir, source, raw, fallback=True)
    from noah.core.calculation_record import verify

    vector = {
        int(k): v
        for k, v in verify(a._submitted_record_dir, ref)["publishable_weights_u16"].items()
    }
    with pytest.raises(PublicationStatusUnknown, match="24 hours"):
        check_before_send(a, ref, vector)
    assert len(c.calls) == before


def test_no_failure_or_recovered_failure_never_authorizes_fallback(tmp_path, monkeypatch):
    a, c, raw, source, now = setup(tmp_path, monkeypatch)
    before = len(c.calls)
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        republish(a, 560, 560)
    assert record_failure(a, ValueError("programming/policy error")) is None
    record_failure(a, ForeignDepositUnpriced("missing"))
    record_recovery(a)
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        republish(a, 560, 560)
    assert len(c.calls) == before


@pytest.mark.parametrize("change", ["wallet", "hotkey", "owner", "policy", "destination"])
def test_fallback_preserves_identity_and_policy_guards(tmp_path, monkeypatch, change):
    a, c, raw, source, now = setup(tmp_path, monkeypatch)
    record_failure(a, ForeignDepositUnpriced("missing"))
    if change == "wallet":
        a.runner_chain.hotkey_owners = lambda keys, b: {"hotkey-0": "intruder"}
    if change == "hotkey":
        a.runner_chain.seat_hotkeys = lambda b: {3: "intruder", 4: "hotkey-1"}
    if change == "owner":
        a.owner_uids = lambda: {0, 3}
    if change == "policy":
        a.points_policy.manifest = {"policy": POLICY, "sha256": "changed"}
    if change == "destination":
        a.ark_coldkey = "changed"
    before = len(c.calls)
    with pytest.raises(Exception):
        republish(a, 560, 560)
    assert len(c.calls) == before


def test_daily_retry_recovery_preserves_recorded_earnings(tmp_path, monkeypatch):
    from noah.core.chain_adapter import ChainAdapter

    a, c, raw, source, now = setup(tmp_path, monkeypatch)
    with patch.object(
        ChainAdapter, "_read_verified", side_effect=ForeignDepositUnpriced("missing")
    ):
        with pytest.raises(ForeignDepositUnpriced):
            a.read(99)
    assert authorize(a, source, now[0]) is not None
    with patch.object(ChainAdapter, "_read_verified", return_value=raw["fixture"]):
        assert a.read(99) == raw["fixture"]
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        authorize(a, source, now[0])


def test_uncertain_fallback_reconciles_without_duplicate_send(tmp_path, monkeypatch):
    from noah.core.publish import NotOnChain

    a, c, raw, source, now = setup(tmp_path, monkeypatch)
    record_failure(a, ForeignDepositUnpriced("missing"))
    c.lost_reply = True
    with pytest.raises(NotOnChain):
        republish(a, 560, 560)
    assert a._publish_intent.load() is not None
    count = len(c.calls)
    c.lost_reply = False
    assert republish(a, 560, 560) == "already_published_this_epoch"
    assert len(c.calls) == count
    assert a._publish_intent.load() is None


def test_expiry_during_signing_blocks_transport(tmp_path, monkeypatch):
    from noah.core.publish import PublicationRefused

    a, c, raw, source, now = setup(tmp_path, monkeypatch)
    record_failure(a, ForeignDepositUnpriced("missing"))
    # Directly exercise the pre-transport boundary with a previously valid proof.
    ref = retain_overlay(a._submitted_record_dir, source, raw, fallback=True)
    from noah.core.calculation_record import verify

    vector = {
        int(k): v
        for k, v in verify(a._submitted_record_dir, ref)["publishable_weights_u16"].items()
    }
    check_before_send(a, ref, vector)
    now[0] = a.day_anchor_ts + 4 * 86400
    a._calculation_reference = ref
    before = len(c.calls)
    with pytest.raises((PublicationRefused, PublicationStatusUnknown)):
        a.publish_direct(vector, 99, refresh_epoch=560, source_day=1)
    assert len(c.calls) == before


def test_collection_outage_uses_bounded_refresh_in_real_maintenance(tmp_path, monkeypatch):
    from noah.core.epoch_work import maintain
    from noah.core.reader import ChainUnreadable

    a, c, raw, source, now = setup(tmp_path, monkeypatch)

    def unavailable(*args):
        raise ChainUnreadable("historical source offline")

    monkeypatch.setattr("noah.core.epoch_work.collect", unavailable)
    before = len(c.calls)
    maintain(a, tmp_path / "writer.lock")
    assert len(c.calls) == before + 1
    assert dict(c.stored)[4] == 0
    state = json.loads((Path(a._submitted_record_dir) / "epoch-maintenance.json").read_text())
    assert state["publication_status"] == "points_outage_refreshed"
    assert state["published_epoch"] == 560


def test_collection_policy_error_never_triggers_fallback(tmp_path, monkeypatch):
    from noah.core.epoch_work import maintain

    a, c, raw, source, now = setup(tmp_path, monkeypatch)

    def broken(*args):
        raise ValueError("invalid identity/configuration")

    monkeypatch.setattr("noah.core.epoch_work.collect", broken)
    before = len(c.calls)
    maintain(a, tmp_path / "writer.lock")
    assert len(c.calls) == before
    assert (
        json.loads((Path(a._submitted_record_dir) / "points-outage.json").read_text())["reason"]
        == "UnsafeFailure"
    )


@pytest.mark.parametrize("error_name", ["SourceDisagreement", "SourceLied", "Unverified"])
def test_wrapped_identity_failure_never_authorizes_outage(tmp_path, monkeypatch, error_name):
    from noah.chain import sources

    a, c, raw, source, now = setup(tmp_path, monkeypatch)
    error = ForeignDepositUnpriced("unavailable")
    error.__cause__ = getattr(sources, error_name)("identity mismatch")
    assert record_failure(a, error) is False
    assert (
        json.loads((Path(a._submitted_record_dir) / "points-outage.json").read_text())["reason"]
        == "UnsafeFailure"
    )
