"""Only exact validated public identities lose raw-key label ambiguity."""
import json
import pytest
from noah.secret_scan import scan_text, _report_identity_labels
from noah.reports.tests.test_adjudication_contract import request


def encoded(value):
    return json.dumps(value, sort_keys=True, indent=2) + '\n'


@pytest.mark.parametrize('wrapper', ['request', 'policy', 'context'])
def test_known_report_identities_are_not_private_keys(wrapper):
    value = request()
    if wrapper == 'policy':
        value = value['policy']
    elif wrapper == 'context':
        value = {'report_gate': {'schema': 'noah-report-publication/1', 'request': value}}
    text = encoded(value)
    projected = _report_identity_labels(text)
    assert len(projected) == len(text) and projected != text
    assert not scan_text(text)
    assert '22' * 32 in projected if wrapper != 'policy' else '55' * 32 in projected


@pytest.mark.parametrize('damage', ['unknown', 'duplicate', 'bad_digest', 'noncanonical'])
def test_malformed_request_does_not_hide_hotkey_bytes(damage):
    value = request()
    if damage == 'unknown':
        value['scored_seats'][0]['unrecognized'] = True
    elif damage == 'bad_digest':
        value['policy_sha256'] = '00' * 32
    text = encoded(value)
    if damage == 'duplicate':
        text = text.replace('"hotkey":', '"hotkey": "' + 'cd' * 32 + '", "hotkey":', 1)
    elif damage == 'noncanonical':
        text = ' ' + text
    assert any(f.kind == 'raw-key-hex' for f in scan_text(text))


def test_unknown_sibling_secrets_still_scanned_next_to_valid_request():
    value = {'request': request(), 'secret_seed': 'ab' * 32,
             'unknown': {'hotkey': 'cd' * 32}}
    findings = scan_text(encoded(value))
    assert any(f.kind == 'raw-key-hex' for f in findings)
    assert any(f.kind in ('env-assignment', 'key-hex') for f in findings)


def test_arbitrary_raw_public_key_file_keeps_original_policy():
    assert any(f.kind == 'raw-key-hex' for f in scan_text(encoded({'hotkey': 'ab' * 32})))


def test_schedule_projection_requires_full_validated_schema():
    from noah.reports.tests.test_schedule import schedule
    value = schedule()
    text = encoded(value)
    assert _report_identity_labels(text) != text
    assert not scan_text(text)
    value['seed'] = 'ab' * 32
    assert scan_text(encoded(value))



def test_shared_window_and_assignments_need_no_chain_sdk(monkeypatch):
    import builtins
    from noah.reports.backing.protocol.preimage import REPORT_POLICY
    from noah.reports.common import digest
    value = request()
    contract = value['policy']['required_rounds'][0]
    contract['commitment_format'] = 'context-v1'
    contract['eligibility_policy_version'] = REPORT_POLICY
    value['policy']['eligibility_policy_version'] = REPORT_POLICY
    value['policy_sha256'] = digest(value['policy'])
    seat = value['scored_seats'][0]
    value['assignments'] = [{key: seat[key] for key in ('uid', 'hotkey', 'coldkey', 'registration_block')}]
    value['assignments'][0]['targets'] = [contract['reported_netuid']]
    shared = {'schema': 'noah.report-shared-window/1', 'reader': 'reader_a', 'files': [],
              'binding': {'schema': 'noah.report-shared-window-binding/1',
                          'round': {key: item for key, item in contract.items()
                                    if key not in ('reported_netuid', 'release_prefix')},
                          'evaluation': value['evaluation'], 'max_ancestor_distance': 10000}}
    original = builtins.__import__
    def restricted(name, *args, **kwargs):
        assert name.split('.')[0] not in {'substrateinterface', 'bittensor', 'bittensor_wallet', 'bittensor_core'}
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', restricted)
    for document in (value, shared):
        text = encoded(document)
        assert _report_identity_labels(text) != text
        assert not scan_text(text)
        assert scan_text(encoded({'artifact': document, 'secret_seed': 'ab' * 32}))
