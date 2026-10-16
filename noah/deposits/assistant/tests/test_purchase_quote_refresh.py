"""Refresh only proven pre-PREPARE attempts; synthetic chain, no live keys."""
import copy

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant import purchase_workflow as W
from .test_purchase_proxy import rig, run


def retry(rig, *, ask=None, label='human-label', **kwargs):
    r, settings, _, journal, adapter, _, _ = rig
    return W.run(r.config, settings, adapter, journal, label=label,
                 ask=ask or (lambda _: f'SEND PURCHASE {settings.max_tao_rao} TAO RAO'),
                 say=lambda _: None, **kwargs)


def expire_first_attempt(rig):
    r, settings, signer_journal, journal, adapter, _, loads = rig
    original = adapter.prepare
    def expire(action, amount, quote):
        adapter.prepare = original
        for _ in range(settings.quote_max_age_blocks + 1):
            r.chain.advance()
        return original(action, amount, quote)
    adapter.prepare = expire
    result = run(*rig)
    step = result['steps']['purchase']
    assert step['status'] == 'not_submitted'
    assert step['phase'] == 'preparing' and step['error_code'] == 'quote_expired'
    assert step['submission_started'] is False
    assert not signer_journal.rows() and not loads and not r.chain.submitted
    return copy.deepcopy(journal.workflows()[0])


def test_fresh_approved_retry_preserves_plan_and_signs_exactly_once(rig):
    r, settings, signer_journal, journal, adapter, _, loads = rig
    original = expire_first_attempt(rig)
    ledger_rows = list(adapter.ledger._db.execute('SELECT * FROM mixed_reservations'))
    result = retry(rig, label='changed-human-label')
    assert result['delivery_complete']
    row = journal.workflows()[0]
    for field in ('id', 'label', 'binding', 'scope', 'plan'):
        assert row[field] == original[field]
    assert row['quote']['quote_block'] > original['quote']['quote_block']
    assert row['quote_history'] == [original['quote']]
    assert row['attempt_history']['purchase'] == [original['steps']['purchase']]
    assert result['steps']['purchase']['amount_rao'] == settings.max_tao_rao
    assert result['steps']['deposit-noah']['amount_rao'] == r.config.amount_rao
    assert result['steps']['deposit-foreign']['amount_rao'] == 73
    assert len(r.chain.submitted) == len(loads) == len(signer_journal.rows()) == 3
    assert r.chain.nonces == [0, 1, 2]
    assert len(list(adapter.ledger._db.execute('SELECT * FROM mixed_reservations'))) == len(ledger_rows) == 1
    assert retry(rig)['delivery_complete'] and len(r.chain.submitted) == 3


@pytest.mark.parametrize('operation', ['preview', 'decline', 'status'])
def test_nonapproval_never_persists_refreshed_quote_or_attempts(rig, operation, monkeypatch):
    r, _, signer_journal, journal, adapter, _, loads = rig
    original = expire_first_attempt(rig)
    quote = adapter.quote
    calls = []
    def observed_quote():
        calls.append('quote')
        return quote()
    monkeypatch.setattr(adapter, 'quote', observed_quote)
    if operation == 'status':
        retry(rig, status=True)
        assert not calls
    elif operation == 'preview':
        result = retry(rig, preview=True, ask=lambda _: pytest.fail('preview prompted'))
        assert result['preview'] and result['quote_refreshed']
        assert result['quote']['quote_block'] > original['quote']['quote_block']
    else:
        assert retry(rig, ask=lambda _: 'no')['status'] == 'declined'
    assert journal.workflows() == [original]
    assert not signer_journal.rows() and not loads and not r.chain.submitted


@pytest.mark.parametrize('marker', ['extrinsic_hash', 'reservation_token', 'journal_id', 'proxy_request'])
@pytest.mark.parametrize('value', [None, False, ''])
def test_even_falsey_evidence_marker_prevents_refresh(rig, marker, value, monkeypatch):
    _, _, _, journal, adapter, _, loads = rig
    row = expire_first_attempt(rig)
    row['steps']['purchase'][marker] = value
    journal.save(row)
    monkeypatch.setattr(adapter, 'quote', lambda: pytest.fail('marker-bearing attempt was repriced'))
    result = retry(rig, preview=True)
    assert not result['quote_refreshed']
    assert result['quote'] == row['quote']
    assert journal.workflows() == [row] and not loads


@pytest.mark.parametrize('change', [
    lambda s: s.pop('submission_protocol'),
    lambda s: s.update(submission_protocol='unknown'),
    lambda s: s.update(phase='reserving'),
    lambda s: s.update(phase='signing'),
    lambda s: s.update(submission_started=True),
    lambda s: s.update(status='uncertain'),
    lambda s: s.update(status='failed'),
    lambda s: s.update(status='finalized'),
    lambda s: s.update(signing_context={}),
    lambda s: s.update(params={}),
    lambda s: s.update(amount_rao=s['amount_rao'] + 1),
])
def test_history_must_prove_every_attempt_was_pre_prepare(rig, change):
    row = expire_first_attempt(rig)
    archived = copy.deepcopy(row['steps']['purchase'])
    change(archived)
    row['attempt_history'] = {'purchase': [archived]}
    assert not W.refreshable_quote(row)


def test_lost_prepare_response_cannot_be_hidden_by_later_local_expiry(rig, monkeypatch):
    r, settings, signer_journal, journal, adapter, _, loads = rig
    remote = adapter.proxy_request
    def lose_prepare_response(step, operation):
        result = remote(step, operation)
        if operation == 'prepare':
            raise ConnectionError('lost response after durable reservation')
        return result
    adapter.proxy_request = lose_prepare_response
    first = run(*rig)
    assert first['steps']['purchase']['phase'] == 'reserving'
    assert first['steps']['purchase']['status'] == 'not_submitted'
    assert len(signer_journal.rows()) == 1
    assert signer_journal.rows()[0]['status'] == 'not_submitted'
    adapter.proxy_request = remote
    for _ in range(settings.quote_max_age_blocks + 1):
        r.chain.advance()
    second = retry(rig)
    assert second['steps']['purchase']['phase'] == 'preparing'
    assert second['steps']['purchase']['error_code'] == 'quote_expired'
    original = journal.workflows()[0]
    assert original['attempt_history']['purchase'][0]['phase'] == 'reserving'
    monkeypatch.setattr(adapter, 'quote', lambda: pytest.fail('lost reservation was repriced'))
    preview = retry(rig, preview=True)
    assert not preview['quote_refreshed']
    assert journal.workflows() == [original]
    assert len(signer_journal.rows()) == 1 and not loads and not r.chain.submitted


@pytest.mark.parametrize('boundary', ['approval', 'quote_saved', 'preparing_saved', 'token_saved'])
def test_refresh_crashes_preserve_attempts_and_never_duplicate(rig, boundary, monkeypatch):
    r, _, signer_journal, journal, adapter, _, loads = rig
    original = expire_first_attempt(rig)
    save = journal.save
    def crash_save(row):
        save(row)
        step = row['steps'].get('purchase', {})
        if not row.get('quote_history'):
            return
        if ((boundary == 'quote_saved' and step.get('status') == 'not_submitted') or
                (boundary == 'preparing_saved' and step.get('status') == 'prepared' and step.get('phase') == 'preparing') or
                (boundary == 'token_saved' and step.get('reservation_token') and not step.get('proxy_request'))):
            raise SystemExit('synthetic crash')
    monkeypatch.setattr(journal, 'save', crash_save)
    def approval(_):
        raise SystemExit('before refreshed approval')
    with pytest.raises(SystemExit):
        retry(rig, ask=approval if boundary == 'approval' else None)
    monkeypatch.setattr(journal, 'save', save)
    if boundary == 'approval':
        assert journal.workflows() == [original]
    elif boundary == 'token_saved':
        monkeypatch.setattr(adapter, 'quote', lambda: pytest.fail('reserved request was repriced'))
    result = retry(rig)
    assert result['delivery_complete']
    assert len(r.chain.submitted) == len(loads) == len(signer_journal.rows()) == 3
    assert r.chain.nonces == [0, 1, 2]
    row = journal.workflows()[0]
    assert row['quote_history'][0] == original['quote']
    assert original['steps']['purchase'] in row['attempt_history']['purchase']


@pytest.mark.parametrize('failure', ['funds', 'delegation', 'cap'])
def test_fresh_quote_failure_leaves_original_attempt_and_never_opens_key(rig, failure):
    r, _, signer_journal, journal, _, signer, loads = rig
    original = expire_first_attempt(rig)
    if failure == 'funds':r.chain.free = 1
    elif failure == 'delegation':r.chain.delegations = []
    else:signer.policy['max_amount_rao'] = r.config.amount_rao - 1
    with pytest.raises(Refusal):retry(rig)
    assert journal.workflows() == [original]
    assert not signer_journal.rows() and not loads and not r.chain.submitted


def test_quote_expiring_during_human_approval_is_not_silently_refreshed_again(rig):
    r, settings, signer_journal, journal, _, _, loads = rig
    original = expire_first_attempt(rig)
    def slow_approval(_):
        for _ in range(settings.quote_max_age_blocks + 1):r.chain.advance()
        return f'SEND PURCHASE {settings.max_tao_rao} TAO RAO'
    result = retry(rig, ask=slow_approval)
    assert result['steps']['purchase']['error_code'] == 'quote_expired'
    assert not result['delivery_complete']
    assert journal.workflows()[0]['quote_history'] == [original['quote']]
    assert not signer_journal.rows() and not loads and not r.chain.submitted
    assert retry(rig)['delivery_complete'] and len(r.chain.submitted) == 3


def test_fresh_quote_cannot_change_plan_inputs(rig, monkeypatch):
    _, _, signer_journal, journal, adapter, _, loads = rig
    original = expire_first_attempt(rig)
    quote = adapter.quote
    def changed():
        result = quote()
        result['plan']['max_tao_rao'] += 1
        return result
    monkeypatch.setattr(adapter, 'quote', changed)
    with pytest.raises(Refusal, match='immutable fixed purchase plan'):
        retry(rig)
    assert journal.workflows() == [original]
    assert not signer_journal.rows() and not loads


@pytest.mark.parametrize('state', ['prepared', 'uncertain'])
def test_preview_does_not_reconcile_or_persist_unresolved_attempt(rig, monkeypatch, state):
    _, _, _, journal, adapter, _, _ = rig
    row = expire_first_attempt(rig)
    row['steps']['purchase']['status'] = state
    journal.save(row)
    monkeypatch.setattr(journal, 'save', lambda _: pytest.fail('preview wrote an attempt'))
    monkeypatch.setattr(adapter, 'reconcile', lambda _: pytest.fail('preview reconciled an attempt'))
    monkeypatch.setattr(adapter, 'quote', lambda: pytest.fail('unresolved attempt was repriced'))
    result = retry(rig, preview=True)
    assert result['steps']['purchase']['status'] == state
    assert journal.workflows() == [row]


def test_completed_preview_does_not_write_canonical_ledger(rig, monkeypatch):
    _, _, _, journal, adapter, _, _ = rig
    assert run(*rig)['delivery_complete']
    monkeypatch.setattr(journal, 'save', lambda _: pytest.fail('preview wrote an attempt'))
    monkeypatch.setattr(adapter.ledger, 'complete_mixed', lambda _: pytest.fail('preview completed ledger'))
    assert retry(rig, preview=True)['delivery_complete']
