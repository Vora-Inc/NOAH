"""Weekly subnet assignments. One signed file, three reports a day.

The weekly setting is off unless a caller turns it on. This module does not
publish weights or open a host.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
from contextlib import contextmanager
import fcntl
import os
import re
import stat
from urllib.error import URLError
from urllib.request import Request, urlopen

from .common import ReportError, digest, immutable_json


TEMPLATE = 'noah.weekly-assignment-template/2'
PUBLISHED = 'noah.weekly-assignments/2'
TEMPLATE_KEYS = (
    'day_anchor_ts effective_day snapshot_offset_seconds prepare_lead_seconds '
    'commit_seconds reveal_seconds eligibility_policy_version calculation_version '
    'scoring_release ark_coldkey ark_validator_hotkey genesis_hash mining_netuid assignment_signer_public_key commitment_format'
).split()
PUBLISHED_KEYS = set(TEMPLATE_KEYS) | {
    'schema', 'week_start', 'roster_block_number', 'roster_block_hash', 'targets',
    'uncovered', 'counts', 'assignments', 'signature',
}
SIGNATURE_DOMAIN = b'noah-weekly-assignments-v2\x00'
MAX_DOCUMENT_BYTES = 2_000_000
MAX_MEMBERS = 4096
IDENTITY_FIELDS = {'genesis_hash', 'mining_netuid', 'ark_coldkey', 'ark_validator_hotkey'}
MAX_ASSIGNED = 3
# Package member budget fits this many production-window rounds. See
# adjudication.LIMITS and test_assignment_package_capacity.
MAX_ROUNDS = 128


def _require(condition, message):
    if not condition:
        raise ReportError('weekly assignments: ' + message)


def canonical_bytes(document):
    body = {key: value for key, value in document.items() if key != 'signature'}
    return json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def week_start_date(unix_seconds):
    moment = datetime.fromtimestamp(int(unix_seconds), timezone.utc).date()
    return moment - timedelta(days=(moment.weekday() + 1) % 7)


def _hex32(value, label):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), label)


def _sunday(value):
    _require(type(value) is str, 'week_start must be a Sunday')
    try:
        start = datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError as exc:
        raise ReportError('weekly assignments: invalid week_start') from exc
    _require(start.isoformat() == value and start.weekday() == 6, 'week_start must be a Sunday')
    return start


def template_from_document(document):
    from .schedule import SCHEMA, validate_schedule
    _require(type(document) is dict and document.get('schema') in (TEMPLATE, PUBLISHED),
             'unsupported assignment document')
    _require(set(TEMPLATE_KEYS) <= set(document), 'template fields')
    _hex32(document['assignment_signer_public_key'], 'assignment signer public key')
    _require(document['assignment_signer_public_key'] not in
             (document['ark_validator_hotkey'], document['ark_coldkey']),
             'assignment signing key must be separate from Ark custody')
    _require(document['commitment_format'] == 'context-v1', 'weekly assignments require context-v1 commitments')
    schedule = {key: document[key] for key in TEMPLATE_KEYS if key not in ('assignment_signer_public_key', 'commitment_format')}
    schedule.update(schema=SCHEMA, reported_netuid=1)
    validate_schedule(schedule)
    return {key: document[key] for key in TEMPLATE_KEYS}


def validate_template(document):
    _require(type(document) is dict and set(document) == set(TEMPLATE_KEYS) | {'schema'}, 'template fields')
    _require(document['schema'] == TEMPLATE, 'template schema')
    return digest(template_from_document(document))


def schedule_for_target(template, netuid):
    from .schedule import SCHEMA, validate_schedule
    schedule = {key: value for key, value in template.items() if key != 'assignment_signer_public_key'}
    schedule.update(schema=SCHEMA, reported_netuid=netuid)
    validate_schedule(schedule)
    return schedule


def _member(row):
    _require(type(row) is dict and set(row) in (
        {'uid', 'hotkey', 'coldkey', 'registration_block'},
        {'uid', 'hotkey', 'coldkey', 'registration_block', 'targets'}), 'roster fields')
    _require(type(row['uid']) is int and 0 <= row['uid'] < 65536, 'roster UID')
    _hex32(row['hotkey'], 'roster hotkey')
    _hex32(row['coldkey'], 'roster coldkey')
    _require(type(row['registration_block']) is int and 0 <= row['registration_block'] < 2**32,
             'roster registration')
    if 'targets' in row:
        chosen = row['targets']
        _require(type(chosen) is list and len(chosen) <= MAX_ASSIGNED
                 and all(type(n) is int and 1 <= n <= 65535 for n in chosen)
                 and len(set(chosen)) == len(chosen), 'assignment targets')


def _rank(seed, kind, label):
    return hmac.new(bytes.fromhex(seed), f'{kind}\n{label}'.encode(), hashlib.sha256).digest()


def deal(roster, targets, seed):
    """Give every target one reporter before any target receives a second.

    Each miner receives min(3, len(targets)) distinct targets. Extra reports
    are spread so counts differ by at most one. Targets left at zero are
    uncovered. The seed is the week-open block hash, so a retry deals the
    same rows.
    """
    _hex32(seed, 'week seed')
    _require(type(targets) is list and targets == sorted(set(targets)), 'targets')
    _require(all(type(item) is int and 1 <= item <= 65535 for item in targets), 'target subnet')
    _require(type(roster) is list and len(roster) <= MAX_MEMBERS, 'roster size')
    for row in roster:
        _member(row)
    _require(len({row['uid'] for row in roster}) == len(roster), 'duplicate roster UID')
    members = sorted(roster, key=lambda row: row['uid'])
    seen = set()
    for row in members:
        _require(row['hotkey'] not in seen, 'duplicate roster hotkey')
        seen.add(row['hotkey'])
    order = sorted(members, key=lambda row: _rank(seed, 'miner', row['hotkey']))
    tie = sorted(targets, key=lambda netuid: _rank(seed, 'target', str(netuid)))
    slots = min(MAX_ASSIGNED, len(tie))
    counts = {netuid: 0 for netuid in tie}
    assignments = []
    for row in order:
        chosen = []
        while len(chosen) < slots:
            options = [netuid for netuid in tie if netuid not in chosen]
            if not options:
                break
            netuid = min(options, key=lambda item: (counts[item], tie.index(item)))
            chosen.append(netuid)
            counts[netuid] += 1
        assignments.append({
            'uid': row['uid'], 'hotkey': row['hotkey'], 'coldkey': row['coldkey'],
            'registration_block': row['registration_block'], 'targets': chosen,
        })
    assignments.sort(key=lambda row: row['uid'])
    uncovered = sorted(netuid for netuid, count in counts.items() if count == 0)
    listed = [{'netuid': netuid, 'reporters': counts[netuid]} for netuid in sorted(counts)]
    return assignments, listed, uncovered


def build_week(template, roster, targets, *, week_start, roster_block_number, roster_block_hash):
    validate_template(template)
    fields = template_from_document(template)
    _sunday(week_start)
    _require(type(roster_block_number) is int and 1 <= roster_block_number < 2**32, 'roster block')
    _hex32(roster_block_hash, 'roster block hash')
    _require(type(targets) is list and len(targets) <= MAX_ROUNDS, 'supported subnet count exceeds adjudication capacity')
    assignments, counts, uncovered = deal(roster, targets, roster_block_hash)
    document = {
        'schema': PUBLISHED, **fields, 'week_start': week_start,
        'roster_block_number': roster_block_number, 'roster_block_hash': roster_block_hash,
        'targets': targets, 'uncovered': uncovered, 'counts': counts, 'assignments': assignments,
    }
    validate_unsigned_week(document, trusted_public_key=fields['assignment_signer_public_key'])
    return document


def validate_unsigned_week(document, *, trusted_public_key):
    _hex32(trusted_public_key, 'independently configured assignment signer required')
    _require(type(document) is dict and set(document) == PUBLISHED_KEYS - {'signature'}, 'published fields')
    _require(document['schema'] == PUBLISHED, 'published schema')
    template_from_document(document)
    _require(document['assignment_signer_public_key'] == trusted_public_key, 'assignment signer differs from trusted key')
    _sunday(document['week_start'])
    height = document['roster_block_number']
    _require(type(height) is int and 1 <= height < 2**32, 'roster block')
    _hex32(document['roster_block_hash'], 'roster block hash')
    _require(type(document['targets']) is list and len(document['targets']) <= MAX_ROUNDS,
             'supported subnet count exceeds adjudication capacity')
    expected, counts, uncovered = deal(document['assignments'], document['targets'], document['roster_block_hash'])
    _require(all(row['registration_block'] <= height for row in document['assignments']), 'registration after week snapshot')
    _require(document['assignments'] == expected, 'assignments do not match the week-open deal')
    _require(type(document['counts']) is list and all(type(row) is dict
                 and set(row) == {'netuid', 'reporters'} and type(row['netuid']) is int
                 and type(row['reporters']) is int for row in document['counts'])
             and type(document['uncovered']) is list
             and all(type(n) is int for n in document['uncovered']), 'coverage fields')
    _require(document['counts'] == counts and document['uncovered'] == uncovered, 'coverage does not match the deal')
    _require(len(canonical_bytes(document)) <= MAX_DOCUMENT_BYTES - 160, 'assignment file is too large')
    return document


def signature_payload(document):
    return SIGNATURE_DOMAIN + canonical_bytes(document)


def load_assignment_key(path, *, trusted_public_key):
    """Read one private assignment-only seed; never consult a validator wallet."""
    _hex32(trusted_public_key, 'independently configured assignment signer required')
    path = Path(path)
    _require(path.is_absolute() and path.resolve() == path, 'assignment key path alias')
    parent = path.parent.stat()
    _require(parent.st_uid in (0, os.geteuid()) and not parent.st_mode & 0o022,
             'assignment key parent permissions')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        mode = stat.S_IMODE(before.st_mode)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == 32,
                 'assignment key must be one regular 32-byte seed')
        _require(before.st_uid in (0, os.geteuid()) and mode in (0o600, 0o640)
                 and (mode == 0o600 or os.geteuid() == 0 or before.st_gid in {os.getegid(), *os.getgroups()}),
                 'assignment key owner or permissions')
        seed = handle.read(33)
        after = os.fstat(handle.fileno())
    signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                            st.st_uid, st.st_gid, st.st_mode, st.st_nlink)
    _require(len(seed) == 32 and signature(before) == signature(after)
             and signature(after) == signature(path.stat(follow_symlinks=False)), 'assignment key changed')
    import sr25519
    pair = sr25519.pair_from_seed(seed)
    _require(pair[0].hex() == trusted_public_key, 'assignment signing key differs')
    return pair


def sign_week(document, keypair, *, trusted_public_key):
    """Sign with an explicitly supplied assignment-only key; never load a wallet."""
    validate_unsigned_week(document, trusted_public_key=trusted_public_key)
    import sr25519
    if isinstance(keypair, tuple) and len(keypair) == 2:
        _require(keypair[0] == bytes.fromhex(trusted_public_key), 'assignment signing key differs')
        signature = sr25519.sign(keypair, signature_payload(document))
    else:
        _require(getattr(keypair, 'crypto_type', None) == 1, 'assignment signature requires SR25519')
        _require(getattr(keypair, 'public_key', None) == bytes.fromhex(trusted_public_key), 'assignment signing key differs')
        signature = keypair.sign(signature_payload(document))
    signed = {**document, 'signature': signature.hex()}
    return verify_week(signed, trusted_public_key=trusted_public_key)


def verify_week(document, *, trusted_public_key):
    _require(type(document) is dict and set(document) == PUBLISHED_KEYS, 'published fields')
    validate_unsigned_week({k: v for k, v in document.items() if k != 'signature'}, trusted_public_key=trusted_public_key)
    _require(type(document['signature']) is str and re.fullmatch('[0-9a-f]{128}', document['signature']), 'assignment signature')
    import sr25519
    try:
        accepted = sr25519.verify(bytes.fromhex(document['signature']), signature_payload(document), bytes.fromhex(trusted_public_key))
    except (ValueError, TypeError):
        accepted = False
    _require(accepted is True, 'assignment signature')
    return document


def assignment_for(document, *, hotkey, coldkey, registration_block, trusted_public_key):
    verify_week(document, trusted_public_key=trusted_public_key)
    matches = [row for row in document['assignments']
               if row['hotkey'] == hotkey and row['coldkey'] == coldkey
               and row['registration_block'] == registration_block]
    _require(len(matches) <= 1, 'assignment identity is ambiguous')
    return matches[0] if matches else None


def earns_reporting_bonus(decision, request):
    """One existing bonus, and only when all three assigned reports verify.

    A request without weekly assignments keeps the single-round rule: the
    seat outcome ``verified`` earns the bonus. Fewer than three assigned
    targets never earns it, even when every available report verifies.
    """
    if 'assignments' not in request:
        return decision.get('outcome') == 'verified'
    identity = (decision.get('hotkey'), decision.get('coldkey'), decision.get('registration_block'))
    row = next((item for item in request['assignments']
                if (item['hotkey'], item['coldkey'], item['registration_block']) == identity), None)
    targets = [] if row is None else list(row['targets'])
    if len(targets) != MAX_ASSIGNED or len(set(targets)) != MAX_ASSIGNED:
        return False
    by_netuid = {}
    for contract in request['policy']['required_rounds']:
        by_netuid.setdefault(contract['reported_netuid'], digest(contract))
    outcomes = {item.get('round_sha256'): item for item in decision.get('rounds') or []}
    for netuid in targets:
        item = outcomes.get(by_netuid.get(netuid))
        if item is None or item.get('outcome') != 'verified' or item.get('reason') == 'not_assigned':
            return False
    return True


def assignment_url(url, now_seconds):
    """A directory URL stays configured. The chain clock names each Sunday."""
    if type(url) is str and url.endswith('/'):
        return url + week_start_date(now_seconds).isoformat() + '.json'
    return url


class AssignmentUnavailable(ReportError):
    """An expected transport outage; only this may fall back to a verified cache."""


def _document(raw):
    _require(len(raw) <= MAX_DOCUMENT_BYTES, 'assignment file is too large')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, 'duplicate assignment JSON field')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode(), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReportError('weekly assignments: assignment file is not JSON') from exc


def _read_document(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_DOCUMENT_BYTES, 'assignment file shape')
        return _document(handle.read(MAX_DOCUMENT_BYTES + 1))


def fetch_document(url, *, trusted_public_key, limit=MAX_DOCUMENT_BYTES):
    _require(type(url) is str and url.startswith(('https://', 'http://')), 'assignment URL')
    try:
        with urlopen(Request(url, headers={'accept': 'application/json'}), timeout=30) as response:
            raw = response.read(min(limit, MAX_DOCUMENT_BYTES) + 1)
    except (OSError, URLError, TimeoutError) as exc:
        raise AssignmentUnavailable('weekly assignments: assignment file is unavailable') from exc
    _require(len(raw) <= min(limit, MAX_DOCUMENT_BYTES), 'assignment file is too large')
    return verify_week(_document(raw), trusted_public_key=trusted_public_key)


@contextmanager
def _locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _require(directory.absolute() == directory.resolve(), 'assignment directory alias')
    fd = os.open(directory / '.operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == 0, 'assignment lock shape')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def retain_week(directory, document, *, trusted_public_key):
    verify_week(document, trusted_public_key=trusted_public_key)
    path = Path(directory) / (document['week_start'] + '.json')
    with _locked(directory):
        if os.path.lexists(path):
            current = verify_week(_read_document(path), trusted_public_key=trusted_public_key)
            _require(canonical_bytes(current) == canonical_bytes(document), 'retained week file differs')
            # Complete durability after an earlier crash between atomic link and directory fsync.
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return current
        immutable_json(path, document)
        return document


def cache_document(directory, document, *, trusted_public_key):
    retain_week(Path(directory) / 'weekly-assignments', document, trusted_public_key=trusted_public_key)
    return Path(directory) / 'weekly-assignments' / (document['week_start'] + '.json')


def _validate_expected_identity(expected_template_identity):
    _require(type(expected_template_identity) is dict and IDENTITY_FIELDS <= set(expected_template_identity)
             and set(expected_template_identity) <= set(TEMPLATE_KEYS), 'independently configured assignment identity required')
    for key in ('genesis_hash', 'ark_coldkey', 'ark_validator_hotkey'):
        _hex32(expected_template_identity[key], 'configured assignment identity')
    netuid = expected_template_identity['mining_netuid']
    _require(type(netuid) is int and 1 <= netuid <= 65535, 'configured mining subnet')


def _expected(document, week, expected_template_identity):
    _validate_expected_identity(expected_template_identity)
    _require(document['week_start'] == week, 'assignment file is for a different week')
    _require(all(document[k] == v for k, v in expected_template_identity.items()), 'assignment template identity differs')
    return document


def load_cached(directory, week_start, *, trusted_public_key, expected_template_identity):
    _sunday(week_start)
    _validate_expected_identity(expected_template_identity)
    path = Path(directory) / 'weekly-assignments' / (week_start + '.json')
    if not os.path.lexists(path):
        return None
    return _expected(verify_week(_read_document(path), trusted_public_key=trusted_public_key), week_start, expected_template_identity)


def supported_targets(reader):
    """Supported initialized pools; daily holder/output checks remain required."""
    from .backing.engine import _read_pool
    _require(reader.qualified_runtime(reader.spec_version()), 'assignment runtime unsupported')
    reader.require_staked_coverage()
    from .backing.keys import networks_added_key, subnet_state_key
    # Both reviewed layouts use Identity(NetUid=u16), not a dense ID range.
    key = networks_added_key if reader.runtime_spec == 483 else subnet_state_key
    prefix = '0x' + key(0)[:-2].hex()
    cursor = prefix
    ids = []
    for _ in range(257):  # Complete u16 keyspace plus its terminal empty page.
        page = reader.source.call('state_getKeysPaged', [prefix, 256, cursor, reader.block_hash])
        _require(type(page) is list and len(page) <= 256, 'subnet key page')
        if not page:
            break
        for item in page:
            _require(type(item) is str and item.startswith(prefix) and len(item) == len(prefix) + 4
                     and item == item.lower() and item > cursor, 'subnet key ordering or layout')
            try:
                encoded = bytes.fromhex(item[2:])
            except ValueError as exc:
                raise ReportError('subnet key encoding') from exc
            _require('0x' + encoded.hex() == item, 'subnet key encoding')
            ids.append(int.from_bytes(encoded[-2:], 'little'))
            cursor = item
    else:
        raise ReportError('subnet key enumeration incomplete')
    found = []
    for netuid in sorted(ids):
        if netuid == 0:
            continue
        pool = _read_pool(reader, netuid)
        if isinstance(pool, dict):
            found.append(netuid)
    _require(len(found) <= MAX_ROUNDS, 'supported subnet count exceeds adjudication capacity')
    return found


def publish_week(template, endpoint, keypair, *, week_start, out_dir, trusted_public_key):
    """Supervised reports-environment compatibility entry; no chain transaction."""
    from .assignment_worker import build
    result = build({'schema': 'noah.weekly-assignment-build/1', 'template': template,
                    'endpoint': endpoint, 'week_start': week_start})
    document = sign_week(result['week'], keypair, trusted_public_key=trusted_public_key)
    retained = retain_week(out_dir, document, trusted_public_key=trusted_public_key)
    return {'action': 'published', 'week_start': retained['week_start'],
            'path': str(Path(out_dir) / (retained['week_start'] + '.json')),
            'targets': retained['targets'], 'uncovered': retained['uncovered'],
            'miners': len(retained['assignments'])}


def current_document(directory, url, now_seconds, *, trusted_public_key, expected_template_identity):
    """Bind a current-week file to local trust before caching or returning it."""
    _hex32(trusted_public_key, 'independently configured assignment signer required')
    _validate_expected_identity(expected_template_identity)
    week = week_start_date(now_seconds).isoformat()
    if url:
        try:
            document = fetch_document(assignment_url(url, now_seconds), trusted_public_key=trusted_public_key)
        except AssignmentUnavailable:
            pass
        else:
            _expected(document, week, expected_template_identity)
            cache_document(directory, document, trusted_public_key=trusted_public_key)
            return document
    cached = load_cached(directory, week, trusted_public_key=trusted_public_key,
                         expected_template_identity=expected_template_identity)
    if cached is None and url:
        raise AssignmentUnavailable('weekly assignments: assignment file is unavailable')
    return cached
