"""Provider request binding; none of these fixtures authenticate a live chain."""

import copy
import json

import pytest

from noah.reports.adjudication_contract import (
    INVOCATION_SCHEMA, POLICY_SCHEMA, REQUEST_SCHEMA, strict_json,
    validate_invocation, validate_request,
)
from noah.reports.common import ReportError, digest
from noah.reports.tests.sample import fixture


def request():
    contract = fixture()['round']
    anchor = (contract['block_timestamp_ms'] // 1000 // 86400) * 86400
    policy = {'schema': POLICY_SCHEMA, 'effective_day': 0,
              'genesis_hash': contract['genesis_hash'], 'mining_netuid': 82,
              'eligibility_policy_version': contract['eligibility_policy_version'],
              'required_rounds': [contract]}
    return {'schema': REQUEST_SCHEMA, 'policy': policy, 'policy_sha256': digest(policy),
            'genesis_hash': contract['genesis_hash'], 'mining_netuid': 82,
            'eval_day': 0, 'day_anchor_ts': anchor,
            'evaluation': {'block_number': 110, 'block_hash': '77' * 32,
                           'timestamp_ms': contract['reveal_close_ms']},
            'calculation': {'fixture_sha256': '88' * 32,
                            'reference': {'path': 'evidence/' + '99' * 32 + '.json',
                                          'sha256': '99' * 32, 'day': 0,
                                          'genesis_hash': '0x' + contract['genesis_hash'],
                                          'candidate_sha256': None}},
            'scored_seats': [{'uid': 5, 'hotkey': '22' * 32, 'coldkey': '33' * 32,
                              'registration_block': 50, 'registration_day': 0,
                              'observed_at': {'block_number': 100, 'block_hash': contract['block_hash']}}]}


def invocation():
    return {'schema': INVOCATION_SCHEMA, 'request': request(), 'sources': [
        {'id': 'reader_a', 'endpoint': 'http://127.0.0.1:9001', 'history': None},
        {'id': 'reader_b', 'endpoint': 'http://127.0.0.1:9002', 'history': '/private/history'}]}


def test_transport_paths_do_not_change_semantic_identity():
    value = invocation()
    identity = validate_invocation(value)
    value['sources'][0]['endpoint'] = 'https://example.invalid/private?region=example'
    value['sources'][1]['history'] = '/another/history'
    assert validate_invocation(value) == identity
    serialized = json.dumps(value['request'])
    assert 'example.invalid' not in serialized and '/history' not in serialized


@pytest.mark.parametrize('field,value', [('hotkey', 'ab' * 32), ('coldkey', 'cd' * 32),
                                        ('registration_block', 51), ('registration_day', 1)])
def test_seat_incarnation_and_participation_are_hash_bound(field, value):
    current = request()
    before = validate_request(current)
    current['scored_seats'][0][field] = value
    assert validate_request(current) != before


@pytest.mark.parametrize('bad', [b'{"a":1,"a":2}', b'{"a":1.0}', b'{"a":1e0}',
                               b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-0}'])
def test_ambiguous_json_is_refused(bad):
    with pytest.raises(ReportError):
        strict_json(bad)


@pytest.mark.parametrize('location', ['request', 'policy', 'evaluation', 'seat', 'reference'])
def test_unknown_fields_are_not_ignored(location):
    value = request()
    target = {'request': value, 'policy': value['policy'], 'evaluation': value['evaluation'],
              'seat': value['scored_seats'][0], 'reference': value['calculation']['reference']}[location]
    target['ignored'] = 1
    with pytest.raises(ReportError):
        validate_request(value)


def test_policy_edit_requires_new_identity_and_cannot_start_in_future():
    value = request()
    value['policy']['effective_day'] = 1
    value['policy_sha256'] = digest(value['policy'])
    with pytest.raises(ReportError, match='not effective'):
        validate_request(value)


def test_unclosed_round_refused_before_collection():
    value = request()
    value['evaluation']['timestamp_ms'] -= 1
    with pytest.raises(ReportError, match='not closed'):
        validate_request(value)


def test_r0_cannot_be_an_external_or_different_day_reference():
    value = request()
    value['calculation']['reference']['path'] = '../../arbitrary.json'
    with pytest.raises(ReportError, match='content address'):
        validate_request(value)
    value = request()
    value['calculation']['reference']['day'] = 1
    with pytest.raises(ReportError, match='day/network'):
        validate_request(value)


def test_uid_reuse_and_duplicate_hotkeys_are_not_silently_collapsed():
    value = request()
    value['scored_seats'].append(copy.deepcopy(value['scored_seats'][0]))
    with pytest.raises(ReportError, match='unique'):
        validate_request(value)
    value['scored_seats'][1]['uid'] = 6
    with pytest.raises(ReportError, match='duplicate seat hotkey'):
        validate_request(value)


def test_same_reader_is_not_independent_agreement():
    value = invocation()
    value['sources'][1]['endpoint'] = value['sources'][0]['endpoint']
    with pytest.raises(ReportError, match='duplicate reader'):
        validate_invocation(value)


def test_existing_r0_reference_keeps_its_exact_prefixed_genesis():
    value = request()
    reference = copy.deepcopy(value['calculation']['reference'])
    assert reference['genesis_hash'].startswith('0x')
    validate_request(value)
    assert value['calculation']['reference'] == reference


@pytest.mark.parametrize('genesis', ['0x0x' + '11' * 32, '0X' + '11' * 32, '0x' + '11' * 31])
def test_malformed_r0_genesis_refused(genesis):
    value = request()
    value['calculation']['reference']['genesis_hash'] = genesis
    with pytest.raises(ReportError, match='genesis format'):
        validate_request(value)


def test_direct_api_and_json_share_integer_bound():
    value = request()
    value['evaluation']['timestamp_ms'] = 2**64
    with pytest.raises(ReportError):
        validate_request(value)
    with pytest.raises(ReportError, match='u64'):
        strict_json(json.dumps(value).encode())


def test_retained_manifest_capacity_does_not_raise_request_or_unknown_schema_limits():
    from noah.reports.adjudication_contract import strict_manifest_json, MAX_MANIFEST_BYTES, MAX_REQUEST_BYTES
    raw = json.dumps({'schema': 'noah.report-adjudication-manifest/1',
                      'files': ['x' * MAX_REQUEST_BYTES]}).encode()
    assert len(raw) > MAX_REQUEST_BYTES
    assert strict_manifest_json(raw)['schema'] == 'noah.report-adjudication-manifest/1'
    with pytest.raises(ReportError, match='request byte limit'):
        strict_json(raw)
    with pytest.raises(ReportError, match='manifest byte limit'):
        strict_manifest_json(b' ' * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ReportError, match='manifest schema'):
        strict_manifest_json(b'{"schema":"noah.report-adjudication-request/1"}')
    with pytest.raises(ReportError, match='duplicate JSON key'):
        strict_manifest_json(b'{"schema":"noah.report-reader-package/1","a":1,"a":2}')
