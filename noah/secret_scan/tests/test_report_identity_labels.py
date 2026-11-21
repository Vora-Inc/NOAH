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


# The actual handoff exposes source filenames such as keys.py next to public
# digest values. These synthetic inventories reproduce that ambiguity without
# retaining a deployment's authorization or financial evidence.
def source_handoff():
    import hashlib
    def sha(value):
        return hashlib.sha256(encoded(value).encode()).hexdigest()
    def release(char):
        files = {'noah/ark/keys.py': char * 64,
                 'noah/miner/hotkey.py': char * 64,
                 'noah/reports/env.py': char * 64,
                 'verification/noah-ts/src/seed.ts': char * 64}
        candidate = {'declared_commit': None, 'files': files, 'source_sha256': sha(files)}
        return {'release_id': char * 12 + '-' + char * 16, 'commit': char * 40,
                'archive_sha256': char * 64, 'manifest_sha256': char * 64,
                'candidate': candidate}
    old, new = release('a'), release('b')
    def reference(release, char):
        return {'path': 'evidence/' + char * 64 + '.json', 'sha256': char * 64,
                'day': 1341, 'genesis_hash': '0x' + 'c' * 64,
                'candidate_sha256': release['candidate']['source_sha256']}
    return {'schema': 'noah-report-source-handoff/1', 'day': 1341, 'netuid': 82,
            'genesis_hash': '0x' + 'c' * 64, 'request_sha256': 'd' * 64,
            'request_semantic_sha256': 'd' * 64, 'financial_sha256': 'e' * 64,
            'checkpoint_sha256': 'f' * 64, 'approval': 'synthetic operator review',
            'source': reference(old, 'a'), 'successor': reference(new, 'b'),
            'from_release': old, 'to_release': new,
            'replay': {name: 'e' * 64 for name in
                       ('old_python', 'old_typescript', 'new_python', 'new_typescript')}}


def test_source_handoff_only_neutralizes_verified_inventory_labels():
    from noah.secret_scan import _source_handoff_labels
    value = source_handoff(); text = encoded(value)
    projected = _source_handoff_labels(text)
    assert projected != text and len(projected) == len(text)
    assert projected.count('\n') == text.count('\n')
    assert not scan_text(text)
    for side in ('from_release', 'to_release'):
        for digest in value[side]['candidate']['files'].values():
            assert projected.count(digest) == text.count(digest)
    assert value['approval'] in projected
    assert encoded(value['source']).strip() in encoded(json.loads(projected)['source'])


@pytest.mark.parametrize('damage', ['digest', 'unknown', 'candidate_field', 'reference_field',
    'reference_day', 'reference_source', 'reference_path', 'path', 'wrong_value',
    'duplicate', 'noncanonical', 'replay'])
def test_malformed_handoff_does_not_gain_label_projection(damage):
    from noah.secret_scan import _source_handoff_labels
    value = source_handoff()
    if damage == 'digest': value['from_release']['candidate']['source_sha256'] = '0' * 64
    elif damage == 'unknown': value['unrecognized'] = True
    elif damage == 'candidate_field': value['from_release']['candidate']['unrecognized'] = True
    elif damage == 'reference_field': value['source']['unrecognized'] = True
    elif damage == 'reference_day': value['source']['day'] += 1
    elif damage == 'reference_source': value['source']['candidate_sha256'] = '0' * 64
    elif damage == 'reference_path': value['source']['path'] = '/private/key'
    elif damage == 'path':
        import hashlib
        candidate = value['from_release']['candidate']
        candidate['files']['../secret.py'] = 'a' * 64
        candidate['source_sha256'] = hashlib.sha256(encoded(candidate['files']).encode()).hexdigest()
        value['source']['candidate_sha256'] = candidate['source_sha256']
    elif damage == 'wrong_value': value['from_release']['candidate']['files']['noah/ark/keys.py'] = 'not a digest'
    elif damage == 'replay': value['replay']['new_python'] = '0' * 64
    text = encoded(value)
    if damage == 'duplicate': text = text.replace('"day": 1341,', '"day": 1341, "day": 1342,', 1)
    elif damage == 'noncanonical': text = ' ' + text
    assert _source_handoff_labels(text) == text
    assert scan_text(text)


@pytest.mark.parametrize('location', ['approval', 'root', 'reference', 'inventory_value', 'arbitrary_label'])
def test_handoff_does_not_hide_real_credential_fields(location):
    value = source_handoff()
    if location == 'approval': value['approval'] = 'secret_seed=' + 'ab' * 32
    elif location == 'root': value['secret_seed'] = 'ab' * 32
    elif location == 'reference': value['source']['secret_seed'] = 'ab' * 32
    elif location == 'inventory_value': value['from_release']['candidate']['files']['secret_seed'] = 'ab' * 32
    else: value = {'noah/ark/keys.py': 'ab' * 32}
    assert any(f.kind in ('key-hex', 'env-assignment') for f in scan_text(encoded(value)))


@pytest.mark.parametrize('token,kind', [('ghp_' + 'A1' * 20, 'token-shape'),
                                       ('secret_seed_' + 'ab' * 32, 'key-hex')])
def test_handoff_filename_intrinsic_token_remains_visible(token,kind):
    import hashlib
    from noah.secret_scan import _source_handoff_labels
    value = source_handoff(); candidate = value['from_release']['candidate']
    path = 'noah/' + token + '.py'
    candidate['files'][path] = 'a' * 64
    candidate['source_sha256'] = hashlib.sha256(encoded(candidate['files']).encode()).hexdigest()
    value['source']['candidate_sha256'] = candidate['source_sha256']
    text = encoded(value)
    assert _source_handoff_labels(text) != text
    expected_line = next(i for i,line in enumerate(text.splitlines(),1) if path in line)
    assert any(f.kind == kind and f.line == expected_line for f in scan_text(text))


def test_handoff_projection_imports_no_chain_sdk(monkeypatch):
    import builtins
    original = builtins.__import__
    def restricted(name,*args,**kwargs):
        assert name.split('.')[0] not in {'substrateinterface','bittensor','bittensor_wallet','bittensor_core'}
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',restricted)
    assert not scan_text(encoded(source_handoff()))
