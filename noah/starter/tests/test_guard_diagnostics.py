"""Failure evidence must be useful without changing publication/recovery decisions."""

import json
from unittest.mock import Mock

import pytest

from noah.core.publication_diagnostics import failure_details
from noah.core.publish import NotOnChain, PublicationRefused
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.writer_lock import held
from noah.starter.runtime import guard_tick, require_guard, run_guard
from noah.starter.tests.test_runtime import ready, send_overlay


def health(tmp_path):
    return json.loads((tmp_path / "scores/starter-guard.json").read_text())


def expired(tmp_path):
    a, c, raw, source = ready(tmp_path)
    send_overlay(a, c, raw, source)
    c.epoch = c.block = c.finalized_head = 560
    return a, c


def test_cooldown_keeps_blocked_guard_and_does_not_send(tmp_path, capsys):
    a, c = expired(tmp_path)
    a.current_block = Mock(return_value=560)
    a.last_update = Mock(return_value=202)
    a.rate_limit = Mock(return_value=400)
    run_guard(a, iterations=1)
    d = health(tmp_path)
    assert d['status'] == 'blocked'
    assert d['error'] == 'PublicationRefused'
    assert d['cause'] == 'CannotPublishYet'
    assert d['stage'] == 'publish_fallback'
    assert d['publication_stage'] == 'rate_limit'
    assert d['reason'] == 'rate_limited'
    assert d['cooldown'] == dict(current_block=560, last_update=202,
                                 rate_limit=400, remaining_blocks=42)
    assert a.current_block.call_count == 1
    assert a.rate_limit.call_count == 1
    assert a._publish_intent.load() is None
    assert len(c.calls) == 2
    with pytest.raises(PublicationStatusUnknown):
        require_guard(a)
    assert '"remaining_blocks": 42' in capsys.readouterr().out


def test_unknown_send_keeps_intent_and_reconciles_without_resending(tmp_path):
    a, c = expired(tmp_path)
    c.lost_reply = True
    with pytest.raises(NotOnChain):
        guard_tick(a)
    d = health(tmp_path)
    assert (d['status'], d['stage'], d['reason']) == (
        'blocked', 'publish_fallback', 'publication_unconfirmed')
    assert a._publish_intent.load() is not None
    c.lost_reply = False
    assert guard_tick(a)['status'] == 'ordinary'
    assert a._publish_intent.load() is None
    assert len(c.calls) == 3


def test_failed_head_read_is_identified_and_secrets_never_logged(tmp_path, capsys):
    a, c, _, _ = ready(tmp_path)
    secret = 'https://rpc.invalid/?token=PRIVATE_TEST_TOKEN'
    a.runner_chain.head_block = Mock(side_effect=TimeoutError(secret))
    run_guard(a, iterations=1)
    d = health(tmp_path)
    assert (d['stage'], d['reason']) == ('read_head', 'read_timeout')
    assert 'head' not in d
    assert secret not in json.dumps(d) + capsys.readouterr().out
    assert len(c.calls) == 1


def test_changed_identity_stays_blocked_without_new_send(tmp_path):
    a, c = expired(tmp_path)
    path = tmp_path / 'scores/starter-active.json'
    record = json.loads(path.read_text())
    record['marker']['uid'] = 99
    path.write_text(json.dumps(record))
    with pytest.raises(PublicationStatusUnknown):
        guard_tick(a)
    assert health(tmp_path)['stage'] == 'check_identity'
    assert len(c.calls) == 2


def test_active_lock_preserves_heartbeat_and_journal_reports_contention(tmp_path, capsys):
    a, _, _, _ = ready(tmp_path)
    path = tmp_path / 'scores/starter-guard.json'
    before = path.read_bytes()
    with held(tmp_path / 'scores/starter-publication.lock'):
        run_guard(a, iterations=1)
    assert path.read_bytes() == before
    out = capsys.readouterr().out
    assert 'publication_lock_held' in out and 'publication_lock' in out


def test_pre_send_stale_guard_identifies_guard_check(tmp_path):
    a, c, raw, source = ready(tmp_path)
    path = tmp_path / 'scores/starter-guard.json'
    record = json.loads(path.read_text()); record['observed_at'] -= 121
    path.write_text(json.dumps(record))
    with pytest.raises(PublicationRefused) as caught:
        send_overlay(a, c, raw, source)
    d = failure_details(caught.value)
    assert d['publication_stage'] == 'pre_send_guard'
    assert a._publish_intent.load() is None
    assert len(c.calls) == 1

