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
    # Open without waiting for a FIFO writer; reject nonregular files via fstat.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


def check_send(adapter, proof, *, refresh=None, reference=None, vector=None):
    value = verify_proof(adapter._submitted_record_dir, proof)
    if value is not None:
        selected, _ = configured(adapter)
        require(selected == value, 'handoff sending producer or authorization changed')
        if source_identity() != value['to_release']['candidate']:
            _confirmed_refresh(adapter, value, proof, refresh, reference, vector)


def refresh_attempt(adapter):
    """Return context only for this call's exact, not-yet-signed new intent."""
    owned = getattr(adapter, '_report_refresh_attempt', None)
    if owned is None:
        return None
    marker = adapter._publish_intent.load()
    require(type(marker) is dict and encoded(marker) == owned
            and marker.get('successor_kind') is None
            and 'prepared_transaction' not in marker and 'result' not in marker
            and type(marker.get('refresh_epoch')) is int,
            'handoff refresh attempt changed or may already be signed')
    return {'epoch': marker['refresh_epoch'], 'day': marker['day'], 'height': marker['height']}


def _confirmed_refresh(adapter, value, proof, refresh, reference, vector):
    """Reconfirm one unchanged DAILY publication; a refresh request is not authority.

    Never authorize a first send, uncertain intent, replacement calculation or
    new burn-zero transition. Original handoff bytes and historical proof stay
    unchanged. All ordinary/report policy and live recipient guards still run.
    """
    from . import direct
    from .burn_publication import confirmed_transition, report_subject, transition_dir
    from .submitted_record import SCHEMA as SUBMITTED_SCHEMA, submitted_record_path
    from ..reports.adjudication_contract import strict_json

    require(type(refresh) is dict and set(refresh) == {'epoch', 'day', 'height'}
            and all(type(refresh[k]) is int for k in refresh)
            and refresh['epoch'] > 0 and refresh['day'] == value['day']
            and refresh['height'] >= 0, 'handoff sending producer or authorization changed')
    require(type(reference) is dict
            and set(reference) == {'path', 'sha256', 'day', 'genesis_hash', 'candidate_sha256'}
            and type(reference['day']) is int and reference['day'] == value['day']
            and reference['genesis_hash'] == value['genesis_hash']
            and all(type(reference[k]) is str and SHA.fullmatch(reference[k])
                    for k in ('sha256', 'candidate_sha256'))
            and reference['path'] == f"evidence/{reference['sha256']}.json"
            and type(vector) is dict and bool(vector)
            and all(type(k) is int and 0 <= k < 65536 and type(v) is int and 0 <= v <= 65535
                    for k, v in vector.items()),
            'handoff refresh lacks exact calculation and vector')
    inputs = encoded({'refresh': refresh, 'reference': reference, 'vector': vector})
    root = adapter._submitted_record_dir
    day = refresh['day']
    daily_path = submitted_record_path(root, day)
    daily_raw = read_bytes(daily_path)
    daily = strict_json(daily_raw)
    replacement = confirmed_transition(root, day)
    path, raw, record = daily_path, daily_raw, daily
    if replacement is not None:
        replacement_ref = replacement['direct_intent']['calculation_evidence']
        path = transition_dir(root, day) / replacement_ref['sha256'] / f'day-{day}.submitted.json'
        raw = read_bytes(path)
        record = strict_json(raw)
        require(record == replacement, 'handoff refresh replacement changed')
    require(type(record) is dict, 'handoff refresh receipt is not an object')
    marker = record.get('direct_intent')
    requested = {str(k): v for k, v in vector.items()}
    require(type(record) is dict and record.get('schema') == SUBMITTED_SCHEMA and type(marker) is dict
            and all(type(doc.get(k)) is int for doc in (record, marker) for k in ('day', 'height', 'netuid'))
            and marker.get('refresh_epoch') is None
            and marker.get('day') == record.get('day') == day
            and marker.get('height') == record.get('height') == refresh['height']
            and marker.get('calculation_evidence') == record.get('calculation_evidence') == reference
            and marker.get('vector') == record.get('submitted_weights_u16') == requested
            and record.get('publish_mode') == 'direct'
            and record.get('netuid') == marker.get('netuid') == adapter.netuid,
            'handoff refresh differs from canonical daily receipt')
    result = marker.get('result')
    require(type(result) is dict and type(result.get('block')) is int
            and type(result.get('extrinsic_index')) is int
            and type(record.get('block')) is int and type(record.get('extrinsic_index')) is int
            and result['block'] == record.get('block')
            and result['extrinsic_index'] == record.get('extrinsic_index')
            and result.get('block_hash') == record.get('block_hash')
            and bool(result.get('block_hash')), 'handoff refresh lacks finalized daily inclusion')
    manifest = verify(root, reference)
    context = artifact(root, manifest, 'context')
    require(manifest['day'] == day and manifest['netuid'] == adapter.netuid
            and manifest['genesis_hash'] == value['genesis_hash']
            and manifest['publishable_weights_u16'] == requested,
            'handoff refresh calculation identity differs')
    subject, _ = report_subject(root, reference, vector) if 'burn_allocation' in context else (reference, vector)
    inner = artifact(root, verify(root, subject), 'context')
    require(inner.get('report_gate') == proof, 'handoff refresh report proof differs')

    def current():
        pending = adapter._publish_intent.load() if adapter._publish_intent is not None else None
        own_attempt = (pending is not None and refresh_attempt(adapter) == refresh
                       and pending.get('calculation_evidence') == reference
                       and pending.get('vector') == requested)
        require(getattr(adapter, 'epoch_republication', False) is True
                and adapter.publish_mode == 'direct'
                and adapter._publish_intent is not None and (pending is None or own_attempt),
                'handoff refresh is not an idle direct epoch publication')
        for guard in (adapter._publish_hold, adapter._failure_backoff):
            if guard is not None:
                if guard is adapter._publish_hold: guard.raise_if_held()
                else: guard.raise_if_blocked()
        chain = adapter.runner_chain
        head = chain.head_block()
        epoch = adapter.reader._query('LastEpochBlock', [adapter.netuid], block=head)
        require(type(epoch) is int and 0 < epoch <= head and epoch == refresh['epoch'],
                'handoff refresh epoch is stale')
        require(adapter.last_update(adapter.uid, adapter.netuid) < epoch
                and head - adapter.last_update(adapter.uid, adapter.netuid) >= adapter.rate_limit(adapter.netuid),
                'handoff refresh is already published or rate limited')
        now = chain.block_timestamp(head)
        require(type(now) is int and 0 <= (now - adapter.day_anchor_ts) // 86400 - 1 - day <= 1,
                'handoff refresh day is stale')
        require(chain.genesis_hash() == manifest['genesis_hash']
                and context.get('ark_address') == adapter.ark_coldkey
                and context.get('designated_validator_hotkey') == adapter.designated_validator_hotkey
                and context.get('scoring_dials') == adapter.dials
                and context.get('day_start_utc_unix') == adapter.day_anchor_ts + day * 86400
                and chain.block_hash(context['to_block']) == context.get('to_block_hash'),
                'handoff refresh configuration or finalized calculation changed')
        adapter._check_direct_identity(marker)
        require(configured(adapter)[0] == value, 'handoff refresh authorization changed')

    current()
    # Exact transaction, scoped success, paired finalized hash and complete
    # stored vector are checked by the existing verifier, not a saved boolean.
    direct.reconcile(adapter.reader, marker, finalized_chain=adapter.runner_chain)
    current()
    require(verify_proof(root, proof) == value and verify(root, reference) == manifest
            and read_bytes(daily_path) == daily_raw and read_bytes(path) == raw
            and confirmed_transition(root, day) == replacement
            and encoded({'refresh': refresh, 'reference': reference, 'vector': vector}) == inputs,
            'handoff refresh inputs changed during confirmation')
