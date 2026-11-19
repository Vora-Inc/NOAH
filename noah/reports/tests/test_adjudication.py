"""Paired resolution and package binding on explicitly synthetic inputs."""
import copy
import hashlib
from pathlib import Path

import pytest

from noah.reports.adjudication import (
    resolve_round, resolve_seats, verify_adjudication, _write, _manifest, _policy,
)
from noah.reports.artifacts import canonical_bytes
from noah.reports.common import digest, ReportError
from noah.reports.backing.protocol.preimage import REPORT_POLICY
from noah.reports.tests.test_adjudication_contract import request as old_request


def paired():
    request = old_request()
    request['policy']['eligibility_policy_version'] = REPORT_POLICY
    contract = request['policy']['required_rounds'][0]
    contract['eligibility_policy_version'] = REPORT_POLICY
    request['policy_sha256'] = digest(request['policy'])
    members = [{k: request['scored_seats'][0][k] for k in ('uid', 'hotkey', 'coldkey', 'registration_block')}]
    members.append({**members[0], 'uid': 9, 'hotkey': 'ab' * 32})
    roster = {'schema': 'noah.report-snapshot-roster/1', 'members': members,
              'block_hash': contract['block_hash'], 'block_number': contract['block_number']}
    engine = {'status': 'OK', 'block_hash': '0x' + contract['block_hash'],
        'block_number': contract['block_number'], 'netuid': contract['reported_netuid'],
        'ark_coldkey': contract['ark_coldkey'], 'coverage': {'coverage_gap_exceeds_holder': False},
        'output1': {'status': 'OK', 'drop_ppm': 1},
        'output2': {'status': 'OK', 'alpha_required_rao': 1000000},
        'output3': {'status': 'OK', 'alpha_required_rao': 2000000}}
    report = {'engine': engine,
        'verdicts': {m['hotkey']: {'status': 'VERIFIED', 'reason': 'exact_match'} for m in members},
        'evidence': {m['hotkey']: {'synthetic': True, 'hotkey': m['hotkey']} for m in members}}
    readers = {label: {'roster': copy.deepcopy(roster), 'report': copy.deepcopy(report),
                     'manifest_sha256': letter * 64} for label, letter in [('reader_a', 'a'), ('reader_b', 'b')]}
    return request, contract, readers


def test_complete_round_keeps_deregistered_member_in_required_count():
    request, contract, readers = paired()
    summary, artifact = resolve_round(request, contract, readers)
    assert summary['resolved']
    assert len(artifact['required_roster']['required_members']) == 2
    assert len(request['scored_seats']) == 1
    assert resolve_seats(request, [artifact])[0]['outcome'] == 'verified'
    assert summary['verdicts_sha256'] == hashlib.sha256(canonical_bytes(artifact['verdicts'])).hexdigest()


def test_one_unknown_miner_does_not_erase_another_confirmed_failure():
    request, contract, readers = paired()
    hotkey = request['scored_seats'][0]['hotkey']
    for reader in readers.values():
        reader['report']['verdicts'][hotkey] = {'status': 'MISSING', 'reason': 'commit'}
        reader['report']['verdicts']['ab' * 32] = {'status': 'NEW_STATUS', 'reason': 'unknown'}
    summary, artifact = resolve_round(request, contract, readers)
    assert summary['resolved'] is False
    decision = resolve_seats(request, [artifact])[0]
    assert decision['effect'] == 'zero' and decision['outcome'] == 'confirmed_failure'


def test_reader_disagreement_never_confirms_miner_failure():
    request, contract, readers = paired()
    key = request['scored_seats'][0]['hotkey']
    readers['reader_a']['report']['verdicts'][key] = {'status': 'MISSING', 'reason': 'commit'}
    summary, artifact = resolve_round(request, contract, readers)
    assert summary['resolved'] is False
    assert resolve_seats(request, [artifact])[0]['outcome'] == 'unresolved'


def test_engine_disagreement_invalidates_otherwise_equal_verdicts():
    request, contract, readers = paired()
    for reader in readers.values():
        for key in reader['report']['verdicts']:
            reader['report']['verdicts'][key] = {'status': 'MISSING', 'reason': 'commit'}
    readers['reader_b']['report']['engine']['output1']['drop_ppm'] += 1
    summary, artifact = resolve_round(request, contract, readers)
    assert summary['engine_sha256'] is None and summary['calculation_sha256'] is None
    assert all(row['effect'] == 'preserve' for row in artifact['verdicts']['rows'])


@pytest.mark.parametrize('field,value', [('coldkey', 'ef' * 32), ('registration_block', 51), ('hotkey', 'cd' * 32)])
def test_uid_reuse_or_owner_change_cannot_inherit_old_verdict(field, value):
    request, contract, readers = paired()
    _, artifact = resolve_round(request, contract, readers)
    request['scored_seats'][0][field] = value
    with pytest.raises(ReportError, match='incarnation'):
        resolve_seats(request, [artifact])


def test_after_snapshot_registration_is_not_retroactively_required():
    request, contract, readers = paired()
    _, artifact = resolve_round(request, contract, readers)
    request['scored_seats'][0]['registration_block'] = contract['block_number'] + 1
    assert resolve_seats(request, [artifact])[0]['outcome'] == 'not_required'


def test_incomplete_or_conflicting_snapshot_roster_is_fatal():
    request, contract, readers = paired()
    readers['reader_b']['roster']['members'].pop()
    with pytest.raises(ReportError, match='roster disagreement'):
        resolve_round(request, contract, readers)


def test_provider_cannot_silently_select_old_deposit_policy():
    with pytest.raises(ReportError, match='report-only'):
        _policy(old_request())


def test_saved_manifest_status_and_decisions_are_not_replay_inputs(tmp_path, monkeypatch):
    request, contract, readers = paired()
    summary, artifact = resolve_round(request, contract, readers)
    decisions = resolve_seats(request, [artifact])
    folder = tmp_path / 'rounds' / digest(contract)
    _write(folder / 'required-roster.json', artifact['required_roster'])
    _write(folder / 'final-verdicts.json', artifact['verdicts'])
    monkeypatch.setattr('noah.reports.adjudication._semantic_reconstruction',
        lambda *_: ([summary], decisions, [artifact]))
    monkeypatch.setattr('noah.reports.adjudication._expected_members',
        lambda *_: {'manifest.json', *[p.relative_to(tmp_path).as_posix()
                                      for p in tmp_path.rglob('*') if p.is_file()]})
    manifest = _manifest(request, tmp_path, [summary], decisions)
    _write(tmp_path / 'manifest.json', manifest)
    pin = hashlib.sha256((tmp_path / 'manifest.json').read_bytes()).hexdigest()
    assert verify_adjudication(tmp_path, request, pin)['status'] == 'COMPLETE'
    # Even a caller supplying a newly pinned edited manifest cannot override
    # independently reconstructed decisions.
    manifest = copy.deepcopy(manifest)
    manifest['scored_decisions'][0]['effect'] = 'zero'
    (tmp_path / 'manifest.json').write_bytes(canonical_bytes(manifest))
    pin = hashlib.sha256((tmp_path / 'manifest.json').read_bytes()).hexdigest()
    with pytest.raises(ReportError, match='raw reconstruction'):
        verify_adjudication(tmp_path, request, pin)


def test_observation_identity_authenticates_owner_and_registration():
    from noah.reports.tests.test_roster import case
    from noah.reports.roster import read_snapshot_roster, read_scored_seat
    source, contract, _ = case()
    member = read_snapshot_roster(source, contract, max_members=2)['members'][0]
    seat = {**member, 'registration_day': 0,
            'observed_at': {'block_number': contract['block_number'], 'block_hash': contract['block_hash']}}
    assert read_scored_seat(source, seat, genesis_hash=contract['genesis_hash'], mining_netuid=82) == seat
    for field, changed in [('coldkey', 'aa' * 32), ('registration_block', 49), ('hotkey', 'bb' * 32)]:
        bad = {**seat, field: changed}
        with pytest.raises(ReportError):
            read_scored_seat(source, bad, genesis_hash=contract['genesis_hash'], mining_netuid=82)


def test_seat_group_reuses_frozen_runtime_and_head_without_losing_any_identity_reads():
    from noah.reports.tests.test_roster import case
    from noah.reports.roster import read_snapshot_roster
    from noah.reports.adjudication import _seat_group_operation
    source, contract, _ = case()
    members = read_snapshot_roster(source, contract, max_members=2)['members']
    seats = [{**member, 'registration_day': 0, 'observed_at': {
        'block_number': contract['block_number'], 'block_hash': contract['block_hash']}} for member in members]
    source.calls.clear()
    request = {'genesis_hash': contract['genesis_hash'], 'mining_netuid': 82}
    assert _seat_group_operation(request, seats)(source) == seats
    assert sum(method == 'state_getRuntimeVersion' for method, _ in source.calls) == 1
    assert sum(method == 'chain_getFinalizedHead' for method, _ in source.calls) == 1
    bad = copy.deepcopy(seats)
    bad[1]['registration_block'] -= 1
    with pytest.raises(ReportError, match='incarnation'):
        _seat_group_operation(request, bad)(source)


def test_sdk_snapshot_cannot_disagree_with_raw_roster_owner():
    from noah.reports.adjudication import _bind_collection_roster
    request, _, readers = paired()
    reader = readers['reader_a']
    for member in reader['roster']['members']:
        reader['report']['evidence'][member['hotkey']]['snapshot'] = {
            'registered': True, 'hotkey': member['hotkey'], 'coldkey': member['coldkey']}
    _bind_collection_roster(reader['roster'], reader['report'])
    key = request['scored_seats'][0]['hotkey']
    reader['report']['evidence'][key]['snapshot']['coldkey'] = 'ff' * 32
    with pytest.raises(ReportError, match='authenticated roster'):
        _bind_collection_roster(reader['roster'], reader['report'])


def test_conflicting_pinned_responses_across_sessions_refuse(tmp_path):
    import gzip
    import json
    from noah.reports.adjudication import _check_stream_consistency
    paths = [tmp_path / 'a.gz', tmp_path / 'b.gz']
    row = {'method': 'state_getStorage', 'params': ['0x00', '0x' + 'aa' * 32], 'result': '0x01'}
    for index, path in enumerate(paths):
        with gzip.open(path, 'wt') as handle:
            handle.write(json.dumps({**row, 'result': f'0x0{index + 1}'}) + '\n')
    with pytest.raises(ReportError, match='cross-session'):
        _check_stream_consistency(paths)


def test_changing_unpinned_heads_are_not_false_conflicts(tmp_path):
    import gzip
    import json
    from noah.reports.adjudication import _check_stream_consistency
    path = tmp_path / 'heads.gz'
    with gzip.open(path, 'wt') as handle:
        for value in ('11', '22'):
            handle.write(json.dumps({'method': 'chain_getFinalizedHead', 'params': [], 'result': '0x' + value * 32}) + '\n')
    _check_stream_consistency([path])


def test_storage_alias_cannot_contradict_equivalent_pinned_storage_read(tmp_path):
    import gzip
    import json
    from noah.reports.adjudication import _check_stream_consistency
    params = ['0x'+'11'*32, '0x'+'22'*32]
    paths = [tmp_path/'first.gz', tmp_path/'second.gz']
    for path, method in zip(paths, ('state_getStorage', 'state_getStorageAt')):
        with gzip.open(path, 'wt') as handle:
            handle.write(json.dumps({'method': method, 'params': params, 'result': '0x00'})+'\n')
    _check_stream_consistency(paths)
    with gzip.open(paths[1], 'wt') as handle:
        handle.write(json.dumps({'method': 'state_getStorageAt', 'params': params, 'result': '0x01'})+'\n')
    with pytest.raises(ReportError, match='contradicts'):
        _check_stream_consistency(paths)
