"""Signed weekly identity scanner cases use the isolated native report fixture.

The pure scanner suite intentionally runs without the report signing dependency.
All original signed-document, malformed-shape and secret-preservation assertions
remain here; production scanner recognition does not authenticate signatures.
"""
import json
import pytest
from noah.secret_scan import scan_text, _report_identity_labels
from noah.reports.tests.test_assignment_v2 import case as weekly_case


def encoded(value):
    return json.dumps(value, sort_keys=True, indent=2) + '\n'


@pytest.mark.parametrize('kind', ['template', 'signed'])
def test_weekly_public_identity_projection_preserves_values(weekly_case, kind):
    value = getattr(weekly_case, kind)
    text = encoded(value)
    projected = _report_identity_labels(text)
    assert len(projected) == len(text) and projected != text
    assert weekly_case.key in projected
    assert value['ark_coldkey'] in projected
    assert not scan_text(text)


@pytest.mark.parametrize('damage', ['unknown', 'member_extra', 'bad_key', 'bad_signature',
                                   'old_schema', 'duplicate', 'noncanonical'])
def test_malformed_weekly_documents_keep_secret_detection(weekly_case, damage):
    value = weekly_case.signed
    if damage == 'unknown':
        value['seed'] = 'ab' * 32
    elif damage == 'member_extra':
        value['assignments'][0]['private_key'] = 'ab' * 32
    elif damage == 'bad_key':
        value['assignment_signer_public_key'] = 'ab'
    elif damage == 'bad_signature':
        value['signature'] = 'ab'
    elif damage == 'old_schema':
        value['schema'] = 'noah.weekly-assignments/1'
    text = encoded(value)
    if damage == 'duplicate':
        text = text.replace('"assignment_signer_public_key":',
                            '"assignment_signer_public_key": "' + 'ab' * 32 + '", "assignment_signer_public_key":')
    elif damage == 'noncanonical':
        text = ' ' + text
    assert _report_identity_labels(text) == text
    assert scan_text(text)


def test_weekly_projection_does_not_authenticate_or_hide_sibling_seed(weekly_case):
    from noah.reports.assignments import verify_week
    from noah.reports.common import ReportError
    value = weekly_case.signed
    value['signature'] = '00' * 64
    # Shape recognition grants no signer or signature authority.
    assert not scan_text(encoded(value))
    with pytest.raises(ReportError, match='signature'):
        verify_week(value, trusted_public_key=weekly_case.key)
    with pytest.raises(ReportError, match='trusted key'):
        verify_week(value, trusted_public_key='ab' * 32)
    assert scan_text(encoded({'week': value, 'private_seed': 'ab' * 32}))
    assert scan_text(encoded({'assignment_signer_private_key': 'ab' * 32}))

