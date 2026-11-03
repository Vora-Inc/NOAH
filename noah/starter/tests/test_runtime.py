"""Real adapter, durable intents and receipt recovery with a simulated RPC transport."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.core.calculation_record import encoded, retain
from noah.core.publish import NotOnChain, PublicationRefused
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.tests.test_direct_runtime475 import adapter
from noah.core.tests.test_epoch_work import EpochClient
from noah.core.writer_lock import LockHeld, held
from noah.starter.allocation import preview
from noah.starter.publication import retain_overlay
from noah.starter.runtime import guard_tick, republish, require_guard
from noah.starter.tests.test_epoch_clock import request_live


def ready(tmp_path, genesis="genesis"):
    raw = request_live()
    raw["snapshot"]["genesisHash"] = genesis
    for i, row in enumerate(raw["fixture"]["miners"]):
        row["uid"] = i + 3
    raw["scoredHotkeys"] = {str(i + 3): f"hotkey-{i}" for i in range(2)}
    for i, row in enumerate(raw["snapshot"]["seats"]):
        row["uid"] = i + 3
    snap = raw["snapshot"]
    snap.update(
        epoch=200,
        nextEpochBlock=560,
        finalizedBlock=201,
        finalizedHash=f"0x{201:064x}",
        weightEffectiveEpoch=560,
        pendingThroughEpoch=559,
    )
    for row in snap["seats"]:
        row["observedBlock"] = 201
    a, c = adapter(tmp_path, EpochClient())
    a._day_window = lambda h: (1, 1, 99)
    a.day_anchor_ts = int(raw["fixture"]["dayEpochTs"])
    a.runner_chain.genesis_hash = lambda: genesis
    a.runner_chain.block_timestamp = lambda b: snap["finalizedTimestamp"]
    a.runner_chain.seat_hotkeys = lambda b: {3: "hotkey-0", 4: "hotkey-1"}
    a.runner_chain.hotkey_owners = lambda keys, b: {
        "hotkey-0": "established",
        "hotkey-1": "wallet-1",
    }
    a.runner_chain.starter_epoch_info = lambda b: dict(
        LastEpochBlock=c.epoch, Tempo=360, PendingEpochAt=0
    )
    a.owner_uids = lambda: {0}
    a.epoch_republication = True
    ordinary = preview(raw)["ordinaryOutput"]
    source = retain(
        a._submitted_record_dir,
        fixture=raw["fixture"],
        agreed_text=encoded(ordinary).decode(),
        vector={int(k): v for k, v in ordinary["weightsU16"].items()},
        context=dict(
            day=1,
            height=99,
            genesis_hash=genesis,
            netuid=82,
            ark_address="ark",
            designated_validator_hotkey="validator",
        ),
    )
    a._calculation_reference = source
    a.publish_direct({int(k): v for k, v in ordinary["weightsU16"].items()}, 99)
    a.starter_entry_policy = SimpleNamespace(snapshot={"sha256": "policy"})
    c.block = c.finalized_head = 201
    assert guard_tick(a)["status"] == "idle"
    return a, c, raw, source


def send_overlay(a, c, raw, source):
    out = preview(raw)
    a._calculation_reference = retain_overlay(a._submitted_record_dir, source, raw)
    return a.publish_direct(
        {int(k): v for k, v in out["proposedWeightsU16"].items()},
        99,
        refresh_epoch=200,
        source_day=1,
    )


def test_real_publication_then_independent_guard_removes_expired_starter(tmp_path):
    a, c, raw, source = ready(tmp_path)
    daily = (tmp_path / "scores/day-1.submitted.json").read_bytes()
    send_overlay(a, c, raw, source)
    assert dict(c.stored)[4] > 0
    assert guard_tick(a)["status"] == "watching"
    c.epoch = c.block = c.finalized_head = 560
    # The normal daily calculation's lock does not block the separate guard.
    with held(tmp_path / "daily-writer.lock"):
        assert guard_tick(a)["status"] == "replaced"
    assert dict(c.stored)[4] == 0
    assert guard_tick(a)["status"] == "ordinary"
    assert len(c.calls) == 3
    assert (tmp_path / "scores/day-1.submitted.json").read_bytes() == daily
    assert a._publish_intent.load() is None


def test_lost_overlay_reply_is_recovered_without_resubmission(tmp_path):
    a, c, raw, source = ready(tmp_path)
    c.lost_reply = True
    with pytest.raises(NotOnChain):
        send_overlay(a, c, raw, source)
    assert a._publish_intent.load() is not None
    c.lost_reply = False
    assert guard_tick(a)["status"] == "watching"
    assert a._publish_intent.load() is None
    assert len(c.calls) == 2


def test_expired_guard_heartbeat_blocks_new_starter_send(tmp_path):
    a, c, raw, source = ready(tmp_path)
    path = tmp_path / "scores/starter-guard.json"
    health = json.loads(path.read_text())
    health["observed_at"] -= 121
    path.write_text(json.dumps(health))
    with pytest.raises(PublicationRefused, match="pre-send"):
        send_overlay(a, c, raw, source)
    assert len(c.calls) == 1
    # The transport was never called; this is not an ambiguous send.
    assert a._publish_intent.load() is None


def test_failed_guard_send_preserves_intent_and_blocks_heartbeat(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.epoch = c.block = c.finalized_head = 560
    c.lost_reply = True
    with pytest.raises(NotOnChain):
        guard_tick(a)
    assert a._publish_intent.load() is not None
    with pytest.raises(PublicationStatusUnknown):
        require_guard(a)
    c.lost_reply = False
    assert guard_tick(a)["status"] == "ordinary"
    assert len(c.calls) == 3


def test_updater_uses_current_input_and_same_receipt_path(tmp_path):
    a, c, raw, source = ready(tmp_path)
    with patch("noah.starter.runtime.current_input", return_value=raw):
        assert republish(a, 200, 201) == "starter_overlay_published"
        assert republish(a, 200, 203) == "already_published_this_epoch"
    assert len(c.calls) == 2
    assert dict(c.stored)[4] > 0


def test_daily_send_uses_same_starter_overlay_and_guard(tmp_path):
    from noah.starter.runtime import prepare_daily

    a, c, raw, source = ready(tmp_path)
    # Use an unpublished subsequent day identity in this isolated transport.
    # prepare_daily itself needs no receipt, but must retain the exact source.
    a._calculation_reference = source
    with (
        patch("noah.starter.runtime.current_input", return_value=raw),
        patch("noah.core.epoch_work.collect", return_value={"collected_through": c.finalized_head}),
    ):
        vector = prepare_daily(a)
    assert vector[4] > 0 and vector[4] < 164
    from noah.starter.publication import verify_overlay

    assert verify_overlay(a._submitted_record_dir, a._calculation_reference)["source"] == source


def test_manual_epoch_schedule_change_refuses_before_send(tmp_path):
    a, c, raw, source = ready(tmp_path)
    a.runner_chain.starter_epoch_info = lambda b: dict(
        LastEpochBlock=200, Tempo=360, PendingEpochAt=300
    )
    with pytest.raises(PublicationStatusUnknown, match="schedule changed"):
        send_overlay(a, c, raw, source)
    assert len(c.calls) == 1


def test_unknown_newer_publication_is_not_overwritten(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.epoch = c.block = c.finalized_head = 560
    c.last = 550
    with pytest.raises(PublicationStatusUnknown, match="unrecognized newer"):
        guard_tick(a)
    assert len(c.calls) == 2


def test_guard_can_remove_weight_after_starter_seat_replaced(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.epoch = c.block = c.finalized_head = 560
    a.runner_chain.seat_hotkeys = lambda b: {3: "hotkey-0", 4: "replacement"}
    assert guard_tick(a)["status"] == "replaced"
    assert dict(c.stored)[4] == 0


def test_explicit_removal_confirms_fallback_before_epoch_end(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.block = c.finalized_head = 210
    assert guard_tick(a, remove_now=True)["status"] == "replaced"
    assert dict(c.stored)[4] == 0
    assert a._publish_intent.load() is None


def test_health_monitor_requires_recent_success(tmp_path):
    from noah.starter.health import inspect

    a, c, raw, source = ready(tmp_path)
    assert inspect(tmp_path / "scores")["ok"]
    health = json.loads((tmp_path / "scores/starter-guard.json").read_text())
    assert inspect(tmp_path / "scores", health["observed_at"] + 60)["ok"]
    assert not inspect(tmp_path / "scores", health["observed_at"] + 121)["ok"]
    assert not inspect(tmp_path / "missing")["ok"]


def test_updater_replaces_known_guard_fallback_after_rate_limit(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.epoch = c.block = c.finalized_head = 560
    assert guard_tick(a)["status"] == "replaced"
    last = a.last_update(a.uid, a.netuid)
    a.rate_limit = lambda n: 5
    with patch("noah.starter.runtime.current_input") as read:
        assert republish(a, 560, last + 1) == "awaiting_rate_limit"
        read.assert_not_called()
    c.block = c.finalized_head = last + 6
    raw["snapshot"].update(
        epoch=560,
        nextEpochBlock=920,
        finalizedBlock=c.finalized_head,
        finalizedHash=f"0x{c.finalized_head:064x}",
        weightEffectiveEpoch=920,
        pendingThroughEpoch=919,
    )
    for row in raw["snapshot"]["seats"]:
        row["observedBlock"] = c.finalized_head
    with patch("noah.starter.runtime.current_input", return_value=raw):
        assert republish(a, 560, c.finalized_head) == "starter_overlay_published"
    assert dict(c.stored)[4] > 0


def test_guard_lock_contention_keeps_last_good_heartbeat(tmp_path):
    from noah.core.writer_lock import LockHeld

    a, c, raw, source = ready(tmp_path)
    path = tmp_path / "scores/starter-guard.json"
    before = path.read_bytes()
    with held(tmp_path / "scores/starter-publication.lock"):
        with pytest.raises(LockHeld):
            guard_tick(a)
    assert path.read_bytes() == before


def test_finality_wait_preserves_bounded_guard_health(tmp_path):
    """A real finality wait outlives 30s; contention must not fake a new heartbeat."""
    from noah.starter.health import inspect

    a, c, raw, source = ready(tmp_path)
    path = tmp_path / "scores/starter-guard.json"
    original = path.read_bytes()
    observed = json.loads(original)["observed_at"]
    with held(tmp_path / "scores/starter-publication.lock"):
        with patch("noah.starter.runtime.time.time", return_value=observed + 60):
            with pytest.raises(LockHeld):
                guard_tick(a)
            require_guard(a)
            assert inspect(tmp_path / "scores")["ok"]
        with patch("noah.starter.runtime.time.time", return_value=observed + 121):
            with pytest.raises(PublicationStatusUnknown):
                require_guard(a)
            assert not inspect(tmp_path / "scores")["ok"]
    assert path.read_bytes() == original


def test_removing_all_ordinary_seats_blocks_instead_of_paying_replacements(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.epoch = c.block = c.finalized_head = 560
    a.runner_chain.seat_hotkeys = lambda b: {3: "replacement", 4: "hotkey-1"}
    with pytest.raises(PublicationStatusUnknown, match="empty"):
        guard_tick(a)
    assert len(c.calls) == 2
    with pytest.raises(PublicationStatusUnknown):
        require_guard(a)
