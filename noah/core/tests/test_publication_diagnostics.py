"""Bounded publication diagnostics preserve exceptions and redact payloads."""

import json
from unittest.mock import Mock

import pytest

from noah.core.publication_diagnostics import failure_details, guard_context
from noah.core.publish import CommitRejected, NotOnChain, publish_weights_direct
from noah.core.startup import CannotPublishYet


def test_arbitrary_exception_types_text_and_malformed_metadata_are_not_serialized():
    secret = 'PRIVATE_TEST_SECRET'
    error = type(secret, (Exception,), {})(secret)
    error._noah_publication_phase = [secret]
    error._noah_guard_stage = {secret: secret}
    error._noah_guard_head = secret
    error.__cause__ = error
    d = failure_details(error)
    assert d == dict(error='UnclassifiedError', cause='UnclassifiedError',
                     reason='unclassified', stage='unknown')
    assert secret not in json.dumps(d)


@pytest.mark.parametrize(('result', 'evidence', 'kind', 'phase'), [
    ({'indeterminate': True, 'error': 'PRIVATE_TEST_PAYLOAD'}, None, NotOnChain, 'submit'),
    ({'error': 'PRIVATE_TEST_PAYLOAD'}, None, CommitRejected, 'submit'),
    ({}, {'confirmed': False}, NotOnChain, 'confirm'),
])
def test_result_refusals_identify_phase_without_retry(result, evidence, kind, phase):
    chain = Mock()
    chain.current_block.return_value = 1000
    chain.last_update.return_value = 800
    chain.rate_limit.return_value = 100
    chain.submit_direct.return_value = result
    chain.confirm_direct.return_value = evidence
    with pytest.raises(kind) as caught:
        publish_weights_direct(chain, vector={1: 65535}, uid=0, netuid=82, hotkey='test')
    details = failure_details(caught.value)
    assert details['publication_stage'] == phase
    assert 'PRIVATE_TEST_PAYLOAD' not in json.dumps(details)
    assert chain.submit_direct.call_count == 1
    assert chain.confirm_direct.call_count == (1 if phase == 'confirm' else 0)


def test_non_numeric_cooldown_fields_never_leak():
    error = CannotPublishYet('secret', current_block='secret', last_update=True,
                             rate_limit=100, remaining_blocks=10)
    guard_context(error, 'publish_fallback', 'secret')
    d = failure_details(error)
    assert d['reason'] == 'rate_limited'
    assert 'cooldown' not in d and 'head' not in d
    assert 'secret' not in json.dumps(d)


def test_confirm_failure_keeps_original_exception_cause():
    secret = 'PRIVATE_TEST_SIGNED_BYTES'
    c = Mock()
    c.current_block.return_value = 1000
    c.last_update.return_value = 800
    c.rate_limit.return_value = 100
    c.submit_direct.return_value = {}
    original = TimeoutError(secret)
    c.confirm_direct.side_effect = original
    with pytest.raises(NotOnChain) as caught:
        publish_weights_direct(c, vector={1: 65535}, uid=0, netuid=82, hotkey='test')
    assert caught.value.__cause__ is original
    d = failure_details(caught.value)
    assert (d['reason'], d['publication_stage'], d['cause']) == (
        'publication_unconfirmed', 'confirm', 'TimeoutError')
    assert secret not in json.dumps(d)
