"""Adversarial regressions for live daily preparation and expiry recovery."""

import fcntl
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from noah.chain.sources import SourceDisagreement
from noah.core.publish import PublicationRefused
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.reader import ChainUnreadable
from noah.core.writer_lock import LockHeld
from noah.starter.runtime import guard_tick, identity, prepare_daily, require_guard, run_guard
from noah.starter.tests.test_reader import prepared
from noah.starter.tests.test_runtime import ready, send_overlay


def test_daily_collects_selected_head_before_real_input_reader(tmp_path):
    a, source, store, cursor = prepared(tmp_path)
    a.runner_chain.head_block = lambda: 202
    a._calculation_reference = source
    a.starter_entry_policy = SimpleNamespace(snapshot={"sha256": "review-policy"})
    a.wallet_hotkey = SimpleNamespace(ss58_address="review-validator")
    (tmp_path / "starter-guard.json").write_text(
        json.dumps(
            {
                "identity": identity(a),
                "observed_at": time.time(),
                "status": "idle",
                "head": 202,
            }
        )
    )

    def collect(adapter, head):
        assert adapter is a and head == 202
        cursor.update(last=head, last_hash=a.runner_chain.block_hash(head))
        store._conn.execute("UPDATE epoch_collection SET body=?", (json.dumps(cursor),))
        # The chain can advance during collection. Preparation must keep its pin.
        a.runner_chain.head_block = lambda: 203
        return {"collected_through": head}

    with patch("noah.core.epoch_work.collect", side_effect=collect) as collection:
        vector = prepare_daily(a)
    collection.assert_called_once_with(a, 202)
    assert sum(vector.values()) == 65535 and vector[1] > 0


def test_bounded_daily_collection_wait_is_retryable_without_intent(tmp_path):
    a, c, raw, source = ready(tmp_path)
    with patch("noah.core.epoch_work.collect", return_value={"collected_through": 200}):
        with pytest.raises(ChainUnreadable, match="catch-up"):
            prepare_daily(a)
    assert a._publish_intent.load() is None and len(c.calls) == 1


def test_definite_pre_send_refusal_does_not_disable_expiry_guard(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    assert len(c.calls) == 2 and dict(c.stored)[4] > 0
    c.block = c.finalized_head
    path = tmp_path / "scores/starter-guard.json"
    health = json.loads(path.read_text())
    health["observed_at"] -= 121
    path.write_text(json.dumps(health))
    with pytest.raises(PublicationRefused, match="pre-send"):
        send_overlay(a, c, raw, source)
    assert len(c.calls) == 2 and a._publish_intent.load() is None
    c.epoch = c.block = c.finalized_head = 560
    assert guard_tick(a)["status"] == "replaced"
    assert dict(c.stored)[4] == 0


def test_release_exclusive_lease_blocks_guard_and_updater_without_intent(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.epoch = c.block = c.finalized_head = 560
    with (tmp_path / "scores/starter-deployment.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockHeld, match="release transaction"):
            guard_tick(a)
        with pytest.raises(LockHeld, match="release transaction"):
            send_overlay(a, c, raw, source)
        assert len(c.calls) == 2 and a._publish_intent.load() is None
    assert guard_tick(a)["status"] == "replaced"


def test_dual_reader_refusal_immediately_blocks_heartbeat_and_sends_nothing(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    before = len(c.calls)
    with patch.object(
        a.runner_chain, "head_block", side_effect=SourceDisagreement("split readers")
    ):
        with pytest.raises(SourceDisagreement):
            guard_tick(a)
        with pytest.raises(PublicationStatusUnknown, match="unhealthy"):
            require_guard(a)
        # The guard reports the refusal and retries; it never converts it into
        # successful evidence or signs using one reader's unagreed number.
        run_guard(a, iterations=1)
    assert json.loads((tmp_path / "scores/starter-guard.json").read_text())["status"] == "blocked"
    assert len(c.calls) == before and a._publish_intent.load() is None
    assert guard_tick(a)["status"] == "watching"
