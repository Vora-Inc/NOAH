"""Strict public-identity shapes for credential-scanner label projection.

This recognizes data types, not chain authenticity, safe publication or scoring
approval. It never removes values, arbitrary fields, or intrinsic secret scans.
Unknown fields/types refuse the whole enclosing shape. No network/SDK imports.
"""
import re
from pathlib import PurePosixPath

from .adjudication_contract import fields, hex32, natural, require, validate_request, _block
from .common import digest
from .gate import PASS_REASONS, FAILURE_REASONS
from .verification import CALCULATION

IDENTITY = 'uid hotkey coldkey registration_block'


def validate_weekly_identities(value):
    """Recognize public labels only; this does not establish signer trust.

    Operational consumers must separately authenticate against their configured
    key. The scanner validates the full bounded shape without importing crypto
    or treating a document's own public key as a trust anchor.
    """
    from .assignments import (TEMPLATE, PUBLISHED, PUBLISHED_KEYS,
                              validate_template, validate_unsigned_week)
    if value.get('schema') == TEMPLATE:
        validate_template(value)
        members = []
    else:
        require(value.get('schema') == PUBLISHED and set(value) == PUBLISHED_KEYS,
                'weekly identity fields')
        require(type(value['signature']) is str
                and re.fullmatch('[0-9a-f]{128}', value['signature']),
                'weekly identity signature shape')
        validate_unsigned_week({key: item for key, item in value.items() if key != 'signature'},
                               trusted_public_key=value['assignment_signer_public_key'])
        members = [(row, {'hotkey', 'coldkey'}) for row in value['assignments']]
    return [(value, {'ark_coldkey', 'ark_validator_hotkey', 'assignment_signer_public_key'})] + members


def _text(value, maximum=4096):
    require(type(value) is str and len(value) <= maximum, 'identity text')


def _bool(value):
    require(type(value) is bool, 'identity boolean')


def _hash(value, *, prefix=False):
    if prefix:
        require(type(value) is str and value.startswith('0x'), 'identity hash prefix')
        value = value[2:]
    hex32(value, 'identity hash')


def _numbers(value, names):
    fields(value, names, 'numeric object')
    for item in value.values():
        natural(item, 'numeric field', 2**256 - 1)


def _shape(value, specification):
    fields(value, ' '.join(specification), 'identity object')
    for name, check in specification.items():
        check(value[name])


def _array(value, check, maximum=22000):
    require(type(value) is list and len(value) <= maximum, 'identity list')
    for item in value:
        check(item)


def _nullable(check):
    return lambda value: None if value is None else check(value)


def _identity(row, extras=''):
    fields(row, IDENTITY + (' ' + extras if extras else ''), 'identity member')
    natural(row['uid'], 'identity UID', 65535)
    natural(row['registration_block'], 'identity registration', 2**32 - 1)
    _hash(row['hotkey']); _hash(row['coldkey'])


def _ordered_members(rows, extras=''):
    require(type(rows) is list and len(rows) <= 4096, 'identity member list')
    previous, seen = -1, set()
    for row in rows:
        _identity(row, extras)
        require(row['uid'] > previous and row['hotkey'] not in seen, 'identity order/uniqueness')
        previous = row['uid']; seen.add(row['hotkey'])


def _decision(row, *, optional=False):
    require(row['effect'] in ('preserve', 'zero'), 'decision effect')
    require(row['outcome'] in ('verified', 'confirmed_failure', 'unresolved', 'not_required'), 'decision outcome')
    status, reason = row['status'], row['reason']
    require(type(reason) is str and re.fullmatch('[a-z][a-z0-9_]{0,95}', reason), 'decision reason')
    if status is None:
        require(optional and reason in ('registered_after_snapshot', 'not_assigned') and row['outcome'] == 'not_required'
                and row['effect'] == 'preserve', 'not-required decision')
    else:
        require(type(status) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,63}', status), 'decision status')
        pair = (status, reason)
        outcome = 'verified' if pair in PASS_REASONS else 'confirmed_failure' if pair in FAILURE_REASONS else 'unresolved'
        require(row['outcome'] == outcome and row['effect'] == ('zero' if pair in FAILURE_REASONS else 'preserve'),
                'decision classification')


def validate_roster(value):
    fields(value, 'schema genesis_hash mining_netuid block_number block_hash timestamp_ms spec_version members', 'snapshot roster')
    require(value['schema'] == 'noah.report-snapshot-roster/1', 'snapshot roster schema')
    _hash(value['genesis_hash']); _hash(value['block_hash'])
    natural(value['mining_netuid'], 'roster subnet', 65535)
    require(value['mining_netuid'] > 0, 'roster root subnet')
    natural(value['block_number'], 'roster block', 2**32 - 1)
    natural(value['timestamp_ms'], 'roster timestamp'); natural(value['spec_version'], 'roster spec', 2**32 - 1)
    _ordered_members(value['members'])
    require(all(row['registration_block'] <= value['block_number'] for row in value['members']), 'roster registration ordering')
    return [(row, {'hotkey', 'coldkey'}) for row in value['members']]


def validate_required_roster(value):
    fields(value, 'schema request_sha256 round_sha256 evaluation snapshot required_members', 'required roster')
    require(value['schema'] == 'noah.report-required-roster/1', 'required roster schema')
    _hash(value['request_sha256']); _hash(value['round_sha256'])
    _block(value['evaluation'], 'roster evaluation', timestamp=True)
    nodes = validate_roster(value['snapshot'])
    _ordered_members(value['required_members'])
    require(value['required_members'] == value['snapshot']['members'], 'required roster membership differs')
    return nodes + [(row, {'hotkey', 'coldkey'}) for row in value['required_members']]


def validate_verdicts(value):
    fields(value, 'schema request_sha256 round_sha256 evaluation required_roster_sha256 engine_sha256 rows', 'final verdicts')
    require(value['schema'] == 'noah.report-final-verdicts/1', 'final verdict schema')
    for name in ('request_sha256', 'round_sha256', 'required_roster_sha256'): _hash(value[name])
    _nullable(_hash)(value['engine_sha256'])
    _block(value['evaluation'], 'verdict evaluation', timestamp=True)
    _ordered_members(value['rows'], 'effect outcome status reason sources')
    for row in value['rows']:
        _decision(row)
        fields(row['sources'], 'reader_a reader_b', 'verdict sources')
        for source in row['sources'].values():
            _shape(source, {'verdict_sha256': _hash, 'evidence_sha256': _hash})
    return [(row, {'hotkey', 'coldkey'}) for row in value['rows']]


def _summary(row):
    fields(row, 'round_sha256 required_roster_sha256 evaluation readers engine_sha256 calculation_sha256 verdicts_sha256 resolved', 'round summary')
    for name in ('round_sha256', 'required_roster_sha256', 'verdicts_sha256'): _hash(row[name])
    for name in ('engine_sha256', 'calculation_sha256'): _nullable(_hash)(row[name])
    require(row['calculation_sha256'] is None or row['engine_sha256'] is not None, 'round calculation binding')
    _block(row['evaluation'], 'summary evaluation', timestamp=True)
    _shape(row['readers'], {'reader_a': _hash, 'reader_b': _hash})
    _bool(row['resolved'])


def _inventory(entry):
    fields(entry, 'path kind encoding sha256 bytes expanded_sha256 expanded_bytes', 'inventory entry')
    path = entry['path']
    require(type(path) is str and len(path) <= 1024 and re.fullmatch('[A-Za-z0-9_./-]+', path), 'inventory path')
    parsed = PurePosixPath(path)
    require(not parsed.is_absolute() and str(parsed) == path and not any(part in ('.', '..') for part in parsed.parts)
            and len(parsed.parts) <= 12, 'inventory normalized path')
    kinds = {'json': 'identity', 'manifest': 'identity', 'metadata': 'identity',
             'rpc_responses': 'gzip', 'history_segment': 'gzip', 'receipts': 'gzip'}
    require(entry['kind'] in kinds and entry['encoding'] == kinds[entry['kind']], 'inventory kind/encoding')
    _hash(entry['sha256']); _hash(entry['expanded_sha256'])
    natural(entry['bytes'], 'inventory bytes'); natural(entry['expanded_bytes'], 'inventory expanded bytes')
    if entry['encoding'] == 'identity':
        require(entry['sha256'] == entry['expanded_sha256'] and entry['bytes'] == entry['expanded_bytes'], 'identity inventory binding')


def validate_shared_window(value):
    """Recognize the complete public shared-window shape, never its authority.

    The two target-specific fields are absent by format. Restore harmless typed
    placeholders solely to reuse the full round validator; offline replay must
    still authenticate the actual request, bytes, readers and each target.
    """
    from .verification import validate_round
    fields(value, 'schema reader binding files', 'shared window')
    require(value['schema'] == 'noah.report-shared-window/1', 'shared window schema')
    require(value['reader'] in ('reader_a', 'reader_b'), 'shared window reader')
    binding = value['binding']
    fields(binding, 'schema round evaluation max_ancestor_distance', 'shared binding')
    require(binding['schema'] == 'noah.report-shared-window-binding/1', 'shared binding schema')
    contract = binding['round']
    require(type(contract) is dict and 'reported_netuid' not in contract
            and 'release_prefix' not in contract, 'shared round target fields')
    validate_round({**contract, 'reported_netuid': 1, 'release_prefix': '00' * 16})
    require(contract.get('commitment_format') == 'context-v1', 'shared context commitment')
    evaluation = binding['evaluation']
    _block(evaluation, 'shared evaluation', timestamp=True)
    require(contract['reveal_close_ms'] <= evaluation['timestamp_ms']
            and contract['block_number'] <= evaluation['block_number'], 'shared round not closed')
    require(type(binding['max_ancestor_distance']) is int
            and binding['max_ancestor_distance'] == 10000, 'shared ancestor bound')
    require(evaluation['block_number'] - contract['block_number'] <= 10000,
            'shared window distance')
    _array(value['files'], _inventory)
    paths = [row['path'] for row in value['files']]
    require(paths == sorted(set(paths)), 'shared inventory unique sorted paths')
    return [(contract, {'ark_coldkey', 'ark_validator_hotkey'})]


def validate_adjudication(value):
    manifest = value.get('schema') in ('noah.report-adjudication-manifest/1',
                                       'noah.report-adjudication-manifest/2')
    base = 'schema request_sha256 policy_sha256 status rounds scored_decisions'
    fields(value, base + (' request files' if manifest else ' manifest_sha256'), 'adjudication artifact')
    require(manifest or value['schema'] == 'noah.report-adjudication-result/1', 'adjudication result schema')
    _hash(value['request_sha256']); _hash(value['policy_sha256'])
    if manifest:
        require(validate_request(value['request']) == value['request_sha256']
                and value['request']['policy_sha256'] == value['policy_sha256'], 'manifest request binding')
        if value['schema'] == 'noah.report-adjudication-manifest/2':
            rounds = value['request']['policy']['required_rounds']
            require('assignments' in value['request'] and 2 <= len(rounds) <= 128,
                    'shared manifest assigned rounds')
            common = [{key: item for key, item in row.items()
                       if key not in ('reported_netuid', 'release_prefix')} for row in rounds]
            require(all(row.get('commitment_format') == 'context-v1' for row in rounds)
                    and all(row == common[0] for row in common), 'shared manifest common window')
        _array(value['files'], _inventory)
        paths = [row['path'] for row in value['files']]
        require(paths == sorted(set(paths)), 'inventory unique sorted paths')
    else:
        _hash(value['manifest_sha256'])
    _array(value['rounds'], _summary, 128)
    require(bool(value['rounds']), 'adjudication rounds empty')
    round_ids = [row['round_sha256'] for row in value['rounds']]
    require(len(set(round_ids)) == len(round_ids), 'duplicate round summary')
    require(value['status'] == ('COMPLETE' if all(row['resolved'] for row in value['rounds']) else 'UNRESOLVED'), 'adjudication status')
    _ordered_members(value['scored_decisions'], 'effect outcome rounds')
    for row in value['scored_decisions']:
        _array(row['rounds'], lambda item: fields(item, 'round_sha256 effect outcome status reason', 'seat round'), 128)
        require([item['round_sha256'] for item in row['rounds']] == round_ids, 'seat round binding')
        for item in row['rounds']: _decision(item, optional=True)
        outcomes = [item['outcome'] for item in row['rounds']]
        outcome = next((kind for kind in ('confirmed_failure', 'unresolved', 'verified') if kind in outcomes), 'not_required')
        require(row['outcome'] == outcome and row['effect'] == ('zero' if outcome == 'confirmed_failure' else 'preserve'), 'seat aggregate classification')
    if manifest:
        request = value['request']
        require(round_ids == [digest(row) for row in request['policy']['required_rounds']], 'manifest policy round binding')
        require(all(row['evaluation'] == request['evaluation'] for row in value['rounds']), 'manifest evaluation binding')
        require([{key: row[key] for key in IDENTITY.split()} for row in value['scored_decisions']] ==
                [{key: row[key] for key in IDENTITY.split()} for row in request['scored_seats']], 'manifest scored identity binding')
    return [(row, {'hotkey', 'coldkey'}) for row in value['scored_decisions']]


def _nat(value):
    natural(value, 'report integer', 2**256 - 1)


def _enum(*choices):
    return lambda value: require(type(value) is str and value in choices, 'report enum')


def _decimal(places):
    return lambda value: require(type(value) is str and re.fullmatch(r'[0-9]+\.[0-9]{%d}' % places, value), 'report decimal')


def _boundary(value):
    if value is None:
        return
    require(type(value) is dict and type(value.get('valid')) is bool, 'threshold boundary')
    _shape(value, {'k': _nat, 'valid': _bool, **({'drop_ppm': _nat, 'price_after_bits': _nat,
            'qualifies': _bool} if value['valid'] else {'reason': _text})})


def _output(value, target=None):
    require(type(value) is dict and value.get('status') in ('OK', 'UNAVAILABLE'), 'engine output status')
    spec = {'status': _enum('OK', 'UNAVAILABLE'), 'reason': _text}
    if target is not None:
        spec.update(percent_target=lambda item: require(type(item) is int and item == target, 'threshold target'), sim_calls=_nat)
    if value['status'] == 'OK':
        spec['reason'] = lambda item: require(item is None, 'successful reason')
        spec.update(alpha_fee_rao=_nat, drop_ppm=_nat, price_after_bits=_nat, tao_amount_rao=_nat)
        if target is None:
            spec.update(alpha_amount_rao=_nat, alpha_sold_rao=_nat, percent=_decimal(4))
        else:
            spec.update(alpha_decimal=_decimal(3), alpha_required_rao=_nat, k=_nat,
                        k_minus_1=_boundary, k_minus_2=_boundary, k_plus_1=_boundary)
    _shape(value, spec)


def validate_engine(value):
    from .backing.result import definitions
    from .backing.constants import ARK_LABEL, ARK_NOTE
    if value.get('status') in ('UNAVAILABLE', 'UNSUPPORTED'):
        _shape(value, {'schema': _enum('bfpoc-calc-v1'), 'status': _enum('UNAVAILABLE', 'UNSUPPORTED'),
                'block_hash': lambda item: _hash(item, prefix=True), 'block_number': _nullable(_nat),
                'spec_version': _nullable(_nat), 'netuid': _nat, 'reason': _text,
                'definitions': lambda item: require(item == definitions(), 'backing definitions')})
        # Gated results have no account identity labels to project.
        return []
    fields(value, 'schema ark_coldkey ark_context block_hash block_number block_timestamp_ms coverage definitions flags holder netuid output1 output2 output3 pool reason spec_version status', 'backing engine')
    require(value['schema'] == 'bfpoc-calc-v1' and value['status'] == 'OK' and value['reason'] is None, 'backing engine schema/status')
    _hash(value['ark_coldkey'], prefix=True); _hash(value['block_hash'], prefix=True)
    for name in ('block_number', 'block_timestamp_ms', 'netuid', 'spec_version'): _nat(value[name])
    require(value['definitions'] == definitions(), 'backing definitions')
    _shape(value['ark_context'], {'available_rao': _nat, 'locked_rao': _nat, 'total_rao': _nat,
            'present': _bool, 'label': lambda item: require(item == ARK_LABEL, 'Ark label'),
            'note': lambda item: require(item == ARK_NOTE, 'Ark note')})
    _shape(value['coverage'], {**{name: _nat for name in 'coldkey_count coverage_ppm gap_rao subnet_alpha_out_rao sum_available_rao sum_locked_rao sum_total_rao'.split()}, 'coverage_gap_exceeds_holder': _bool})
    _shape(value['flags'], {**{name: _nat for name in ('sum_available_all_rao', 'sum_available_ex_ark_rao')},
            **{name: _bool for name in 'exceeds_available_all_10 exceeds_available_all_20 exceeds_available_ex_ark_10 exceeds_available_ex_ark_20'.split()}})
    _shape(value['holder'], {**{name: _nat for name in 'available_rao collateral_derived_rao locked_rao tie_count total_rao'.split()},
            'pubkey': lambda item: _hash(item, prefix=True), 'ss58': lambda item: require(type(item) is str and re.fullmatch('[1-9A-HJ-NP-Za-km-z]{46,50}', item), 'holder SS58')})
    _numbers(value['pool'], 'alpha_in_rao alpha_out_rao fee_rate price_1e9 price_bits quote_parts tao_rao')
    _output(value['output1']); _output(value['output2'], 10); _output(value['output3'], 20)
    return [(value, {'ark_coldkey'})]


def _proof(value):
    _shape(value, {'finalized_head': lambda item: _hash(item, prefix=True), 'finalized_head_number': _nat,
            'inclusion_number': _nat, 'inclusion_hash': lambda item: _hash(item, prefix=True),
            'canonical_hash': _nullable(lambda item: _hash(item, prefix=True)), 'matches': _bool})


def _call(value):
    _shape(value, {'block_hash': _hash, 'block_number': _nat, 'call': _enum('Commitments.set_commitment'),
            'data_hex': lambda item: require(type(item) is str and len(item) <= 6202 and re.fullmatch('(0x)?(?:[0-9a-f]{2})*', item), 'commitment data'),
            'extrinsic_index': _nat, 'finality': _proof, 'finalized': _bool, 'genesis_hash': _hash,
            'signer': _hash, 'structurally_valid': _bool, 'successful': _bool, 'target_netuid': _nat, 'timestamp_ms': _nat})


def _calculation(value):
    def output(item, name):
        _shape(item, {'status': _enum('OK', 'UNAVAILABLE'), name: _nat})
    _shape(value, {'ark_coldkey': _hash, 'block_hash': _hash, 'block_number': _nat,
            'calculation_version': lambda item: require(item == CALCULATION, 'calculation version'),
            'coverage': lambda item: _shape(item, {'coverage_gap_exceeds_holder': _bool}),
            'genesis_hash': _hash, 'netuid': _nat, 'output1': lambda item: output(item, 'drop_ppm'),
            'output2': lambda item: output(item, 'alpha_required_rao'), 'output3': lambda item: output(item, 'alpha_required_rao'),
            'status': _enum('OK', 'UNAVAILABLE')})
    require(all(value[name]['status'] == value['status'] for name in ('output1', 'output2', 'output3')),
            'calculation output statuses')
    if value['status'] == 'UNAVAILABLE':
        # _one emits this precise placeholder when no integer projection exists.
        # A null field or invented number is not an available engine result.
        require(value['coverage']['coverage_gap_exceeds_holder'] is True
                and value['output1']['drop_ppm'] == 0
                and value['output2']['alpha_required_rao'] == 0
                and value['output3']['alpha_required_rao'] == 0, 'unavailable calculation fallback')


def _evidence(value, hotkey):
    fields(value, 'calculation candidates collection collector commit deposit_history finality reveal snapshot', 'collector evidence')
    require(value['collector'] == 'noah.reports.backing.collector', 'collector identifier')
    require(value['deposit_history'] == {'state': 'NOT_APPLICABLE'}, 'report-only deposit history')
    _calculation(value['calculation'])
    snapshot = value['snapshot']
    _shape(snapshot, {'block_hash': _hash, 'block_number': _nat, 'coldkey': _hash, 'finalized': _bool,
            'genesis_hash': _hash, 'hotkey': _hash, 'registered': _bool, 'timestamp_ms': _nat})
    require(snapshot['hotkey'] == hotkey, 'evidence identity binding')
    require(all(snapshot[key] == value['calculation'][key] for key in ('genesis_hash', 'block_hash', 'block_number')), 'calculation snapshot binding')
    def gap(item):
        require(type(item) is list and len(item) == 2, 'collection gap')
        _nat(item[0]); _nat(item[1]); require(item[0] <= item[1], 'collection gap ordering')
    _shape(value['collection'], {'errors': lambda item: _array(item, _text), 'from_ms': _nat,
            'gaps': lambda item: _array(item, gap), 'genesis_hash': _hash, 'round_block_hash': _hash,
            'state': _enum('COMPLETE', 'INCOMPLETE'), 'through_ms': _nat})
    require(value['collection']['genesis_hash'] == snapshot['genesis_hash'] and value['collection']['round_block_hash'] == snapshot['block_hash'], 'collection snapshot binding')
    calls = lambda item: _array(item, _call)
    _shape(value['candidates'], {'commits': calls, 'reveals': calls, 'diagnostics': lambda item:
        _shape(item, {'later_commitments': calls, 'other_miners': _nat, 'reveals': calls})})
    _nullable(_call)(value['commit']); _nullable(_call)(value['reveal'])
    _shape(value['finality'], {'commit': _nullable(_proof), 'reveal': _nullable(_proof)})
    return [(snapshot, {'hotkey', 'coldkey'}), (value['calculation'], {'ark_coldkey'})]


def validate_collector(value):
    fields(value, 'evidence observation', 'collector envelope')
    hashers = lambda item: _array(item, _enum('Blake2_128', 'Blake2_256', 'Blake2_128Concat', 'Twox128', 'Twox256', 'Twox64Concat', 'Identity'), 8)
    _shape(value['observation'], {'endpoint': _enum('reader_a', 'reader_b'), 'genesis': lambda item: _hash(item, prefix=True),
            'metadata_sha256': _hash, 'owner_hashers': hashers, 'spec_name': _text, 'spec_version': _nat,
            'uids_hashers': hashers, 'uids_key_matches_identity_blake2': _bool})
    require(type(value['evidence']) is dict and len(value['evidence']) <= 4096, 'collector evidence map')
    nodes = []
    for hotkey, evidence in value['evidence'].items():
        _hash(hotkey)
        nodes.extend(_evidence(evidence, hotkey))
        require('0x' + evidence['snapshot']['genesis_hash'] == value['observation']['genesis'], 'observation genesis binding')
    return nodes
