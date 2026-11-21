"""One protected operator authorization for one frozen report request.

Financial evidence and the original producer remain immutable. This document is
not a general source allowlist and cannot change a policy, request or output.
"""
import os
from pathlib import Path
import re
import stat

from .calculation_record import artifact, digest, durable_write, encoded, source_identity, verify
from .report_publication import require, semantic_digest

SCHEMA = 'noah-report-source-handoff/1'
MAX_BYTES = 2 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}')
FIELDS = {'schema', 'day', 'genesis_hash', 'netuid', 'request_sha256',
          'request_semantic_sha256', 'source', 'successor', 'from_release',
          'to_release', 'financial_sha256', 'replay', 'approval', 'checkpoint_sha256'}


def read_bytes(path, *, protected=False):
    path = Path(path)
    require(path.is_absolute() if protected else True, 'handoff path must be absolute')
    if protected:
        for parent in (path.parent, *path.parent.parents):
            info = parent.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                    and not info.st_mode & 0o022, 'handoff ancestor is not protected')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and 0 < before.st_size <= MAX_BYTES, 'handoff file shape refused')
        if protected:
            require(before.st_uid == 0 and not before.st_mode & 0o022,
                    'handoff authorization is not operator controlled')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        signature = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        require(len(raw) == before.st_size and signature(before) == signature(os.fstat(fd))
                and signature(before) == signature(path.lstat()), 'handoff changed during read')
    finally:
        os.close(fd)
    return raw


def document(raw):
    from ..reports.adjudication_contract import strict_json
    value = strict_json(raw)
    require(encoded(value) == raw, 'handoff must be canonical JSON')
    return validate(value)


def identity(value):
    require(type(value) is dict and set(value) == {'declared_commit', 'source_sha256', 'files'}
            and (value['declared_commit'] is None or (type(value['declared_commit']) is str
                 and re.fullmatch(r'[0-9a-f]{40}', value['declared_commit']) is not None))
            and type(value['files']) is dict and value['files'], 'handoff source identity differs')
    for name, sha in value['files'].items():
        require(type(name) is str and not name.startswith('/') and '..' not in name.split('/')
                and type(sha) is str and SHA.fullmatch(sha), 'handoff source member refused')
    require(value['source_sha256'] == digest(encoded(value['files'])), 'handoff source digest differs')
    return value


def validate(value):
    require(type(value) is dict and set(value) == FIELDS and value['schema'] == SCHEMA,
            'invalid report source handoff')
    require(type(value['day']) is int and value['day'] >= 0 and type(value['netuid']) is int
            and 0 <= value['netuid'] < 65536 and type(value['genesis_hash']) is str
            and re.fullmatch(r'0x[0-9a-f]{64}', value['genesis_hash']), 'handoff chain or day differs')
    for key in ('request_sha256', 'request_semantic_sha256', 'financial_sha256', 'checkpoint_sha256'):
        require(type(value[key]) is str and SHA.fullmatch(value[key]), 'handoff digest differs')
    require(type(value['approval']) is str and 1 <= len(value['approval']) <= 256
            and all(32 <= ord(c) < 127 for c in value['approval']), 'handoff approval reference refused')
    for key in ('from_release', 'to_release'):
        release = value[key]
        require(type(release) is dict and set(release) == {'release_id', 'commit', 'archive_sha256', 'manifest_sha256', 'candidate'},
                'handoff release fields differ')
        candidate = identity(release['candidate'])
        require(type(release['release_id']) is str and re.fullmatch(r'[0-9a-f]{12}-[0-9a-f]{16}', release['release_id'])
                and type(release['commit']) is str and re.fullmatch(r'[0-9a-f]{40}', release['commit'])
                and release['release_id'].startswith(release['commit'][:12] + '-')
                and candidate['declared_commit'] in (None, release['commit'])
                and all(type(release[k]) is str and SHA.fullmatch(release[k]) for k in ('archive_sha256', 'manifest_sha256')),
                'handoff release identity differs')
    require(value['from_release']['candidate'] != value['to_release']['candidate'], 'handoff requires different sources')
    replay = value['replay']
    require(type(replay) is dict and set(replay) == {'old_python', 'old_typescript', 'new_python', 'new_typescript'}
            and len(set(replay.values())) == 1 and all(type(x) is str and SHA.fullmatch(x) for x in replay.values()),
            'handoff four engines do not agree')
    require(len(encoded(value)) <= MAX_BYTES, 'handoff exceeds byte bound')
    return value


def raw_pair(root, current, frozen):
    """Authenticate references and permit only attempt height and producer drift."""
    records = []
    for ref in (current, frozen):
        manifest = verify(root, ref)
        require(manifest.get('schema') == 'noah-calculation-evidence/1'
                and manifest.get('agreement') == 'python_typescript_identical'
                and manifest.get('state') == 'calculated'
                and manifest.get('publication') == 'not_established'
                and manifest.get('settlement') == 'not_observed'
                and type(manifest.get('day')) is int and type(manifest.get('netuid')) is int,
                'handoff calculation schema or state differs')
        context = artifact(root, manifest, 'context')
        require(ref == {'path': ref['path'], 'sha256': ref['sha256'], 'day': manifest['day'],
                        'genesis_hash': manifest['genesis_hash'],
                        'candidate_sha256': context.get('candidate', {}).get('source_sha256')}
                and context['height'] == manifest['height']
                and not any(k in context for k in ('report_gate', 'report_no_bonus', 'starter_overlay', 'burn_allocation')),
                'handoff requires authentic raw references')
        records.append((manifest, context))
    (latest, new), (original, old) = records
    require(type(original['height']) is int and type(latest['height']) is int
            and 0 <= original['height'] <= latest['height'], 'handoff attempt moves backwards')
    financial = lambda c: {k: v for k, v in c.items() if k not in ('height', 'candidate')}
    require(financial(new) == financial(old), 'handoff financial context differs')
    expected = {**latest, 'height': original['height'],
                'artifacts': {**latest['artifacts'], 'context': original['artifacts']['context']}}
    require(original == expected, 'handoff fixture, score or vector differs')
    return original, old, new, digest(encoded(financial(old)))


def verify_graph(root, value, request_raw):
    validate(value)
    from ..reports.adjudication_contract import strict_json, validate_request
    request = strict_json(request_raw)
    validate_request(request)
    require(digest(request_raw) == value['request_sha256']
            and semantic_digest(request) == value['request_semantic_sha256']
            and request['calculation']['reference'] == value['source'], 'handoff frozen request differs')
    original, old, new, financial = raw_pair(root, value['successor'], value['source'])
    require((original['day'], original['genesis_hash'], original['netuid']) ==
            (value['day'], value['genesis_hash'], value['netuid'])
            and old['candidate'] == value['from_release']['candidate']
            and new['candidate'] == value['to_release']['candidate']
            and financial == value['financial_sha256']
            and request['calculation']['fixture_sha256'] == original['artifacts']['fixture']['sha256']
            and set(value['replay'].values()) == {original['artifacts']['agreed_score']['sha256']},
            'handoff producer or financial binding differs')
    return value


def configured(adapter):
    path = getattr(adapter, 'report_source_handoff', None)
    sha = getattr(adapter, 'report_source_handoff_sha256', None)
    require(bool(path) and type(sha) is str and SHA.fullmatch(sha), 'source change lacks operator handoff')
    raw = read_bytes(path, protected=True)
    require(digest(raw) == sha, 'configured handoff digest differs')
    return document(raw), raw


def authorize(adapter, current, frozen, request_raw):
    root = adapter._submitted_record_dir
    value, raw = configured(adapter)
    for kind in ('submitted', 'confirmed', 'accounted'):
        path = Path(root) / f"day-{value['day']}.{kind}.json"
        require(not path.exists() and not path.is_symlink(), 'handoff day already has a disposition')
    ledger = getattr(adapter, '_commit_ledger', None)
    require(ledger is None or value['day'] not in (ledger.load() or {}), 'handoff day has a commit ledger entry')
    verify_graph(root, value, request_raw)
    require(value['source'] == frozen, 'handoff original producer differs')
    _, _, new, financial = raw_pair(root, current, frozen)
    require(new['candidate'] == value['to_release']['candidate'] == source_identity()
            and financial == value['financial_sha256'], 'handoff running producer differs')
    ref = {'path': f"report-state/source-handoffs/{digest(raw)}.json", 'sha256': digest(raw)}
    durable_write(Path(root) / ref['path'], raw)
    return ref


def verify_proof(root, proof):
    ref = proof.get('source_handoff')
    if ref is None:
        return None
    require(type(ref) is dict and set(ref) == {'path', 'sha256'}
            and type(ref['sha256']) is str and SHA.fullmatch(ref['sha256'])
            and ref['path'] == f"report-state/source-handoffs/{ref['sha256']}.json", 'handoff proof path differs')
    raw = read_bytes(Path(root) / ref['path'])
    require(digest(raw) == ref['sha256'], 'handoff proof digest differs')
    value = document(raw)
    # Original request bytes are retained separately; semantic equality also binds R1.
    request_raw = read_bytes(Path(root) / 'report-state' / f"day-{value['day']}.request.json")
    verify_graph(root, value, request_raw)
    require(proof['source'] == value['source']
            and semantic_digest(proof['request']) == value['request_semantic_sha256'], 'handoff proof request differs')
    return value


def check_send(adapter, proof):
    value = verify_proof(adapter._submitted_record_dir, proof)
    if value is not None:
        selected, _ = configured(adapter)
        require(selected == value and source_identity() == value['to_release']['candidate'],
                'handoff sending producer or authorization changed')
