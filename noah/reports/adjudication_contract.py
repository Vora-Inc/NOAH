"""Strict private provider inputs. Validation is not chain authentication.

These helpers do not enable enforcement, select an eligibility policy, or trust
saved verdicts. The live provider must additionally authenticate both readers.
"""

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .common import ReportError, digest
from .gate import eval_day_for_close
from .verification import validate_round

REQUEST_SCHEMA = 'noah.report-adjudication-request/1'
INVOCATION_SCHEMA = 'noah.report-adjudication-invocation/1'
POLICY_SCHEMA = 'noah.report-policy/1'
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HEX32 = re.compile(r'[0-9a-f]{64}\Z')
POLICY_NAME = re.compile(r'[a-z][a-z0-9-]{0,95}\Z')


def require(ok, reason):
    if not ok:
        raise ReportError('report adjudication: ' + reason)


def fields(value, names, where):
    require(type(value) is dict and set(value) == set(names.split()), where + ' fields')


def natural(value, where, maximum=2**64 - 1):
    require(type(value) is int and 0 <= value <= maximum, where)


def hex32(value, where):
    require(type(value) is str and HEX32.fullmatch(value) is not None, where)


def strict_json(raw):
    """Request JSON retains its independent two-MiB ceiling."""
    return _strict_json(raw, MAX_REQUEST_BYTES, 'request')


def strict_manifest_json(raw):
    """Only retained report manifests receive the larger full-day inventory cap."""
    value = _strict_json(raw, MAX_MANIFEST_BYTES, 'manifest')
    require(type(value) is dict and value.get('schema') in
            ('noah.report-adjudication-manifest/1', 'noah.report-adjudication-manifest/2',
             'noah.report-reader-package/1', 'noah.report-reader-package/2', 'noah.report-shared-window/1'),
            'retained manifest schema required')
    return value


def _strict_json(raw, limit, kind):
    """Bounded JSON with no duplicate keys, floats, NaN or infinity."""
    require(isinstance(raw, bytes) and len(raw) <= limit, kind + ' byte limit')
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    def refused(_):
        raise ReportError('report adjudication: noninteger JSON number')
    def integer(token):
        require(re.fullmatch(r'0|[1-9][0-9]{0,19}', token), 'noncanonical JSON integer')
        value = int(token)
        natural(value, 'JSON integer exceeds u64')
        return value
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_int=integer, parse_float=refused, parse_constant=refused)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReportError('report adjudication: malformed JSON') from exc


def _block(value, where, *, timestamp=False):
    fields(value, 'block_number block_hash' + (' timestamp_ms' if timestamp else ''), where)
    natural(value['block_number'], where + ' number', 2**32 - 1)
    hex32(value['block_hash'], where + ' hash')
    if timestamp:
        natural(value['timestamp_ms'], where + ' timestamp')


def _validate_assignments(request):
    rows = request['assignments']
    require(type(rows) is list and len(rows) <= 65536, 'assignment count')
    seen = set()
    targets = {contract['reported_netuid'] for contract in request['policy']['required_rounds']}
    for row in rows:
        fields(row, 'uid hotkey coldkey registration_block targets', 'assignment')
        natural(row['uid'], 'assignment UID', 65535)
        hex32(row['hotkey'], 'assignment hotkey')
        hex32(row['coldkey'], 'assignment coldkey')
        natural(row['registration_block'], 'assignment registration', 2**32 - 1)
        require(row['hotkey'] not in seen, 'duplicate assignment hotkey')
        seen.add(row['hotkey'])
        chosen = row['targets']
        require(type(chosen) is list and 1 <= len(chosen) <= 3 and len(set(chosen)) == len(chosen),
                'assignment targets')
        require(all(type(item) is int and item in targets for item in chosen), 'assignment target is not required')


def validate_request(request):
    """Validate a semantic request, returning its newline-free canonical digest.

    Policy recognition is deliberately not policy approval. Callers must check
    the separately approved policy before collection or enforcement.
    """
    base = set('schema policy policy_sha256 genesis_hash mining_netuid eval_day '
               'day_anchor_ts evaluation calculation scored_seats'.split())
    keys = set(request) if type(request) is dict else set()
    require(keys == base or keys == base | {'assignments'}, 'request fields')
    require(request['schema'] == REQUEST_SCHEMA, 'request schema')
    hex32(request['genesis_hash'], 'genesis')
    natural(request['mining_netuid'], 'mining subnet', 65535)
    require(request['mining_netuid'] > 0, 'root subnet refused')
    natural(request['eval_day'], 'evaluation day')
    natural(request['day_anchor_ts'], 'day anchor')
    require(request['day_anchor_ts'] % 86400 == 0, 'anchor must be UTC midnight')
    _block(request['evaluation'], 'evaluation', timestamp=True)
    require(request['evaluation']['timestamp_ms'] >= request['day_anchor_ts'] * 1000,
            'evaluation before anchor')
    policy = request['policy']
    fields(policy, 'schema effective_day genesis_hash mining_netuid eligibility_policy_version required_rounds', 'policy')
    require(policy['schema'] == POLICY_SCHEMA, 'policy schema')
    natural(policy['effective_day'], 'policy effective day')
    natural(policy['mining_netuid'], 'policy mining subnet', 65535)
    require(policy['effective_day'] <= request['eval_day'], 'policy not effective')
    require(policy['genesis_hash'] == request['genesis_hash'] and
            policy['mining_netuid'] == request['mining_netuid'], 'policy network')
    require(type(policy['eligibility_policy_version']) is str and
            POLICY_NAME.fullmatch(policy['eligibility_policy_version']), 'explicit eligibility policy required')
    hex32(request['policy_sha256'], 'policy digest')
    require(digest(policy) == request['policy_sha256'], 'policy digest differs')
    rounds = policy['required_rounds']
    require(type(rounds) is list and 0 < len(rounds) <= 256, 'required round count')
    identities = set()
    for contract in rounds:
        try:
            validate_round(contract)
        except (ValueError, TypeError, KeyError) as exc:
            raise ReportError('report adjudication: invalid required round') from exc
        identity = digest(contract)
        require(identity not in identities, 'duplicate required round')
        identities.add(identity)
        require(contract['genesis_hash'] == request['genesis_hash'] and
                contract['mining_netuid'] == request['mining_netuid'], 'round network')
        require(contract['eligibility_policy_version'] == policy['eligibility_policy_version'], 'round eligibility policy')
        require(eval_day_for_close(contract['reveal_close_ms'], request['day_anchor_ts']) == request['eval_day'], 'round day')
        require(contract['reveal_close_ms'] <= request['evaluation']['timestamp_ms'] and
                contract['block_number'] <= request['evaluation']['block_number'], 'round not closed')
    calculation = request['calculation']
    fields(calculation, 'reference fixture_sha256', 'calculation')
    hex32(calculation['fixture_sha256'], 'fixture byte digest')
    reference = calculation['reference']
    fields(reference, 'path sha256 day genesis_hash candidate_sha256', 'R0 reference')
    hex32(reference['sha256'], 'R0 digest')
    require(reference['path'] == f"evidence/{reference['sha256']}.json", 'R0 content address')
    reference_genesis = reference['genesis_hash']
    require(type(reference_genesis) is str and
            re.fullmatch(r'(?:0x)?[0-9a-f]{64}', reference_genesis), 'R0 genesis format')
    require(type(reference['day']) is int and reference['day'] == request['eval_day'] and
            reference_genesis.removeprefix('0x') == request['genesis_hash'], 'R0 day/network')
    if reference['candidate_sha256'] is not None:
        hex32(reference['candidate_sha256'], 'R0 candidate digest')
    if 'assignments' in request:
        _validate_assignments(request)
    seats = request['scored_seats']
    require(type(seats) is list and 0 < len(seats) <= 65536, 'scored seat count')
    previous = -1
    hotkeys = set()
    for seat in seats:
        fields(seat, 'uid hotkey coldkey registration_block registration_day observed_at', 'seat')
        natural(seat['uid'], 'UID', 65535)
        require(seat['uid'] > previous, 'seats must be UID-sorted and unique')
        previous = seat['uid']
        hex32(seat['hotkey'], 'seat hotkey')
        hex32(seat['coldkey'], 'seat coldkey')
        require(seat['hotkey'] not in hotkeys, 'duplicate seat hotkey')
        hotkeys.add(seat['hotkey'])
        natural(seat['registration_block'], 'registration block', 2**32 - 1)
        natural(seat['registration_day'], 'registration day')
        _block(seat['observed_at'], 'seat observation')
        require(seat['registration_block'] <= seat['observed_at']['block_number'] <=
                request['evaluation']['block_number'], 'registration/observation ordering')
    return digest(request)


def validate_policy(policy):
    """Validate standalone frozen policy configuration, without selecting it."""
    fields(policy, 'schema effective_day genesis_hash mining_netuid eligibility_policy_version required_rounds', 'policy')
    require(policy['schema'] == POLICY_SCHEMA, 'policy schema')
    natural(policy['effective_day'], 'policy effective day')
    hex32(policy['genesis_hash'], 'policy genesis')
    natural(policy['mining_netuid'], 'policy mining subnet', 65535)
    require(policy['mining_netuid'] > 0, 'policy root subnet refused')
    rounds = policy['required_rounds']
    require(type(rounds) is list and 0 < len(rounds) <= 256, 'required round count')
    identities = set()
    for contract in rounds:
        validate_round(contract)
        require(contract['genesis_hash'] == policy['genesis_hash']
                and contract['mining_netuid'] == policy['mining_netuid']
                and contract['eligibility_policy_version'] == policy['eligibility_policy_version'], 'policy round differs')
        require(digest(contract) not in identities, 'duplicate required round')
        identities.add(digest(contract))
    return digest(policy)


def validate_invocation(invocation):
    fields(invocation, 'schema request sources', 'invocation')
    require(invocation['schema'] == INVOCATION_SCHEMA, 'invocation schema')
    identity = validate_request(invocation['request'])
    sources = invocation['sources']
    require(type(sources) is list and len(sources) == 2, 'two configured readers required')
    endpoints = set()
    for expected, source in zip(('reader_a', 'reader_b'), sources):
        fields(source, 'id endpoint history', 'reader')
        require(source['id'] == expected, 'reader IDs/order')
        require(type(source['endpoint']) is str, 'reader endpoint')
        endpoint = urlsplit(source['endpoint'])
        require(endpoint.scheme in ('http', 'https', 'ws', 'wss') and endpoint.hostname,
                'reader URL')
        require(source['endpoint'] not in endpoints, 'duplicate reader endpoint')
        endpoints.add(source['endpoint'])
        require(source['history'] is None or (type(source['history']) is str and source['history']),
                'history path')
    return identity


def load_invocation(path):
    with Path(path).open('rb') as handle:
        value = strict_json(handle.read(MAX_REQUEST_BYTES + 1))
    validate_invocation(value)
    return value
