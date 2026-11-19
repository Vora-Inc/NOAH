"""Actual shared-window artifacts: typed public labels never waive secret scans."""
import copy
import json

import pytest

from noah.reports.common import digest
from noah.reports.identity_shapes import validate_adjudication, validate_shared_window
from noah.reports.tests.test_shared_window import paired  # noqa: F401
from noah.reports.tests.test_report_package_identity_labels import encode, CANARY_MARKER
from noah.secret_scan import _report_identity_labels, scan_text


@pytest.fixture(scope='module')
def shared(paired):
    root, _, _ = paired
    return json.loads((root/'v2/shared/reader_a/manifest.json').read_bytes())


@pytest.mark.parametrize('style', ['compact', 'pretty', 'spaced'])
def test_real_shared_manifests_preserve_identity_values_and_offsets(paired, shared, style):
    root, _, _ = paired
    manifest = json.loads((root/'v2/manifest.json').read_bytes())
    for value, validator in ((manifest, validate_adjudication), (shared, validate_shared_window)):
        nodes = validator(value)
        text = encode(value, style)
        projected = _report_identity_labels(text)
        assert projected != text and len(projected) == len(text)
        assert all(a == b or b == 'p' for a, b in zip(text, projected))
        assert [n for n, ch in enumerate(text) if ch == '\n'] == [n for n, ch in enumerate(projected) if ch == '\n']
        for owner, names in nodes:
            assert all(owner[name] in projected for name in names)
        assert not scan_text(text)


@pytest.mark.parametrize('damage', [
    'extra', 'schema', 'reader', 'binding_schema', 'round_extra', 'round_hash',
    'target', 'prefix', 'format', 'distance_bool', 'distance_value', 'distance_exceeded',
    'evaluation_extra', 'evaluation_before', 'evaluation_hash', 'evaluation_bool',
    'inventory_duplicate', 'inventory_encoding',
])
def test_malformed_shared_binding_never_projects_partial_round(shared, damage):
    value = copy.deepcopy(shared)
    binding = value['binding']; contract = binding['round']
    if damage == 'extra': value['unexpected'] = True
    elif damage == 'schema': value['schema'] = 'noah.report-shared-window/2'
    elif damage == 'reader': value['reader'] = 'unknown'
    elif damage == 'binding_schema': binding['schema'] = 'unknown'
    elif damage == 'round_extra': contract['unexpected'] = True
    elif damage == 'round_hash': contract['ark_coldkey'] = 'zz' * 32
    elif damage == 'target': contract['reported_netuid'] = 1
    elif damage == 'prefix': contract['release_prefix'] = '00' * 16
    elif damage == 'format': contract['commitment_format'] = 'legacy'
    elif damage == 'distance_bool': binding['max_ancestor_distance'] = True
    elif damage == 'distance_value': binding['max_ancestor_distance'] = 10001
    elif damage == 'distance_exceeded': binding['evaluation']['block_number'] = contract['block_number'] + 10001
    elif damage == 'evaluation_extra': binding['evaluation']['unexpected'] = True
    elif damage == 'evaluation_before': binding['evaluation']['timestamp_ms'] = contract['reveal_close_ms'] - 1
    elif damage == 'evaluation_hash': binding['evaluation']['block_hash'] = 'zz' * 32
    elif damage == 'evaluation_bool': binding['evaluation']['block_number'] = True
    elif damage == 'inventory_duplicate': value['files'].append(value['files'][0])
    else: value['files'][0]['encoding'] = 'unknown'
    with pytest.raises((ValueError, TypeError, KeyError)):
        validate_shared_window(value)
    projected = _report_identity_labels(encode(value))
    assert '"ark_coldkey":' in projected and '"ark_validator_hotkey":' in projected
    assert scan_text(encode(value))


@pytest.mark.parametrize('secret', [{'secret_seed': 'ab' * 32},
    {'unknown': {'hotkey': 'cd' * 32}}, {'private': CANARY_MARKER}])
def test_shared_shape_does_not_hide_sibling_credentials(shared, secret):
    assert scan_text(encode({'artifact': shared, **secret}))


def test_valid_shared_round_free_text_remains_intrinsically_scanned(shared):
    value = copy.deepcopy(shared)
    value['binding']['round']['scoring_release'] = CANARY_MARKER
    assert validate_shared_window(value)
    assert any(f.kind == 'private-key-header' for f in scan_text(encode(value)))


@pytest.mark.parametrize('damage', ['duplicate', 'whitespace', 'nan'])
def test_shared_ambiguous_json_never_projects(shared, damage):
    text = encode(shared)
    if damage == 'duplicate': text = text.replace('{', '{"reader":"reader_a",', 1)
    elif damage == 'whitespace': text = ' ' + text
    else: text = text.replace('{', '{"unexpected":NaN,', 1)
    assert _report_identity_labels(text) == text


def test_rehashed_root_v2_requires_common_context_window(paired):
    root, _, _ = paired
    value = json.loads((root/'v2/manifest.json').read_bytes())
    request = value['request']
    request['policy']['required_rounds'][1]['scoring_release'] += '-different'
    request['policy_sha256'] = digest(request['policy'])
    value['policy_sha256'] = request['policy_sha256']
    value['request_sha256'] = digest(request)
    with pytest.raises(ValueError, match='common window'):
        validate_adjudication(value)


@pytest.mark.parametrize('damage', ['extra', 'hash', 'targets'])
def test_invalid_assignment_keeps_its_key_labels(paired, damage):
    root, _, _ = paired
    request = json.loads((root/'v2/manifest.json').read_bytes())['request']
    assignment = request['assignments'][0]
    if damage == 'extra': assignment['secret_seed'] = 'ab' * 32
    elif damage == 'hash': assignment['hotkey'] = 'zz' * 32
    else: assignment['targets'] = [True]
    projected = _report_identity_labels(encode(request))
    assert '"hotkey":' in projected and '"coldkey":' in projected
    assert scan_text(encode(request))
