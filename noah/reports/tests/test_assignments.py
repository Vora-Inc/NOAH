"""Weekly assignment dealing, selection, and bonus predicate. Synthetic only."""

import hashlib
from datetime import datetime, timezone

import pytest
from substrateinterface import Keypair

from noah.reports.adjudication import LIMITS, _expected_members
from noah.reports.assignments import (
    MAX_ROUNDS, TEMPLATE, assignment_for, build_week, deal, earns_reporting_bonus,
    sign_week, verify_week, week_start_date,
)
from noah.reports.backing.protocol.preimage import REPORT_POLICY, ReportSubmission, commitment_of
from noah.reports.backing.select import select_for_miner
from noah.reports.common import ReportError, digest
from noah.reports.verification import CALCULATION


def template():
    return dict(schema=TEMPLATE, genesis_hash='11' * 32, mining_netuid=82,
                day_anchor_ts=1794787200, effective_day=0, snapshot_offset_seconds=600,
                prepare_lead_seconds=600, commit_seconds=7200, reveal_seconds=7200,
                eligibility_policy_version=REPORT_POLICY, calculation_version=CALCULATION,
                assignment_signer_public_key='34' * 32, commitment_format='context-v1', scoring_release='weekly-test', ark_coldkey='22' * 32, ark_validator_hotkey='33' * 32)


def roster():
    return [{'uid': i, 'hotkey': format(i + 1, '064x'), 'coldkey': format(i + 9, '064x'),
             'registration_block': 10} for i in range(3)]


def signer():
    pair = Keypair.create_from_mnemonic(Keypair.generate_mnemonic(), crypto_type=1, ss58_format=42)
    document = template()
    document['assignment_signer_public_key'] = pair.public_key.hex()
    return document, pair


def test_previous_week_is_not_reused_and_the_command_stays_off(tmp_path):
    from noah.deposits.assistant.config import load_config
    from noah.reports.assignments import cache_document, current_document
    from noah.reports.cli import execute, parser
    body, pair = signer()
    week = sign_week(build_week(body, roster(), [4, 8, 9], week_start='2026-11-15',
                                roster_block_number=20, roster_block_hash='ef' * 32), pair, trusted_public_key=body['assignment_signer_public_key'])
    cache_document(tmp_path, week, trusted_public_key=body['assignment_signer_public_key'])
    assert current_document(tmp_path, None, datetime(2026, 11, 22, tzinfo=timezone.utc).timestamp(), trusted_public_key=body['assignment_signer_public_key'], expected_template_identity={k: body[k] for k in ('genesis_hash', 'mining_netuid', 'ark_coldkey', 'ark_validator_hotkey')}) is None
    ini = tmp_path / 'miner.ini'
    ini.write_text("""
[miner]
network = local
endpoint = ws://127.0.0.1:9
netuid = 82
miner_hotkey = 5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty
coldkey = 5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY
wallet_name = miner
hotkey_name = default
genesis_hash = 0x%s
stake_position = miner
[ark]
ark_coldkey = 5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty
designated_validator_hotkey = 5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy
[deposit]
mode = fixed
amount_noah = 1
[evidence]
file = %s
[state]
dir = %s
""" % ('12' * 32, tmp_path / 'evidence.json', tmp_path / 'state'))
    assert load_config(ini).assignments_url is None
    assert execute(parser().parse_args(['miner', '--config', str(ini), 'assignments'])) == 0


def test_directory_url_uses_the_sunday_of_the_supplied_clock():
    from noah.reports.assignments import assignment_url
    moment = datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp()
    assert assignment_url('http://127.0.0.1:9/', moment) == 'http://127.0.0.1:9/2026-11-15.json'
    assert assignment_url('http://127.0.0.1:9/fixed.json', moment) == 'http://127.0.0.1:9/fixed.json'


def test_sunday_week_and_even_coverage():
    assert week_start_date(datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp()).isoformat() == '2026-11-15'
    seed = 'ab' * 32
    assignments, counts, uncovered = deal(roster(), [1, 7, 18, 64], seed)
    assert uncovered == []
    assert sorted(row['reporters'] for row in counts) == [2, 2, 2, 3]
    assert all(len(row['targets']) == 3 and len(set(row['targets'])) == 3 for row in assignments)
    again, _, _ = deal(roster(), [1, 7, 18, 64], seed)
    assert again == assignments


def test_short_roster_records_uncovered_targets():
    _, counts, uncovered = deal(roster()[:1], [1, 2, 3, 4, 5], 'cd' * 32)
    assert len(uncovered) == 2
    assert sum(row['reporters'] for row in counts) == 3


def test_signed_file_is_stable_and_rejects_a_replaced_uid():
    body, pair = signer()
    week = sign_week(build_week(body, roster(), [4, 8, 9], week_start='2026-11-15',
                                roster_block_number=20, roster_block_hash='ef' * 32), pair, trusted_public_key=body['assignment_signer_public_key'])
    assert verify_week(week, trusted_public_key=body['assignment_signer_public_key'])['week_start'] == '2026-11-15'
    again = sign_week(build_week(body, roster(), [4, 8, 9], week_start='2026-11-15',
                                 roster_block_number=20, roster_block_hash='ef' * 32), pair, trusted_public_key=body['assignment_signer_public_key'])
    assert {key: value for key, value in again.items() if key != 'signature'} == {
        key: value for key, value in week.items() if key != 'signature'}
    owner = week['assignments'][0]
    assert assignment_for(week, trusted_public_key=body['assignment_signer_public_key'], hotkey=owner['hotkey'], coldkey=owner['coldkey'],
                          registration_block=owner['registration_block'])['targets'] == owner['targets']
    assert assignment_for(week, trusted_public_key=body['assignment_signer_public_key'], hotkey=owner['hotkey'], coldkey=owner['coldkey'],
                          registration_block=owner['registration_block'] + 1) is None
    forged = dict(week)
    forged['signature'] = '00' * 64
    with pytest.raises(ReportError, match='signature'):
        verify_week(forged, trusted_public_key=body['assignment_signer_public_key'])


def test_more_targets_than_adjudication_capacity_are_refused():
    body, pair = signer()
    with pytest.raises(ReportError, match='capacity'):
        build_week(body, roster()[:1], list(range(1, MAX_ROUNDS + 2)), week_start='2026-11-15',
                   roster_block_number=1, roster_block_hash='11' * 32)


def _bonus_request(targets, outcomes):
    contracts = [{'reported_netuid': netuid} for netuid in targets]
    decision = {'hotkey': 'aa' * 32, 'coldkey': 'bb' * 32, 'registration_block': 3,
                'outcome': 'verified',
                'rounds': [{'round_sha256': digest(contract), 'outcome': outcome, 'reason': 'exact_match'}
                           for contract, outcome in zip(contracts, outcomes)]}
    request = {'assignments': [{'hotkey': 'aa' * 32, 'coldkey': 'bb' * 32, 'registration_block': 3,
                                'targets': list(targets)}],
               'policy': {'required_rounds': contracts}}
    return decision, request


def test_bonus_requires_three_verified_assignments():
    decision, request = _bonus_request([1, 2, 3], ['verified', 'verified', 'verified'])
    assert earns_reporting_bonus(decision, request) is True
    decision['rounds'][2]['outcome'] = 'confirmed_failure'
    assert earns_reporting_bonus(decision, request) is False
    short, fewer = _bonus_request([1, 2], ['verified', 'verified'])
    short['outcome'] = 'verified'
    assert earns_reporting_bonus(short, fewer) is False
    assert earns_reporting_bonus({'outcome': 'verified'}, {}) is True


def _call(hotkey, number, index, payload, timestamp, **extra):
    body = {'block_number': number, 'call': 'Commitments.set_commitment', 'data_hex': payload.hex(),
            'extrinsic_index': index, 'signer': hotkey, 'structurally_valid': True, 'successful': True,
            'target_netuid': 82, 'timestamp_ms': timestamp, 'genesis_hash': '11' * 32}
    body.update(extra)
    return body


def test_three_reports_in_one_window_select_three_subnets():
    from noah.reports.tests.test_context_commitment import context_case
    cases = [context_case(netuid) for netuid in (7, 18, 64)]
    calls = []
    for index, (_, evidence, _) in enumerate(cases):
        for leg in ('commit', 'reveal'):
            call = dict(evidence[leg], structurally_valid=True)
            call['block_number'] += index
            call['timestamp_ms'] += index
            calls.append(call)
    for contract, evidence, _ in cases:
        chosen = select_for_miner(calls, hotkey_hex=evidence['snapshot']['hotkey'],
            mining_netuid=contract['mining_netuid'], expected_round=contract,
            **{key: contract[key] for key in ('commit_open_ms', 'commit_close_ms', 'reveal_open_ms', 'reveal_close_ms')})
        from noah.reports.backing.protocol.preimage import decode
        from noah.reports.backing.protocol.context_commitment import matches_reveal
        assert matches_reveal(bytes.fromhex(chosen['commit']['data_hex']), bytes.fromhex(chosen['reveal']['data_hex']))
        assert decode(bytes.fromhex(chosen['reveal']['data_hex'])).netuid == contract['reported_netuid']


def test_production_window_package_can_name_one_hundred_twenty_eight_rounds():
    span = 1500
    contracts = [{'block_number': 100, 'marker': index} for index in range(MAX_ROUNDS)]
    request = {'evaluation': {'block_number': 100 + span - 1, 'block_hash': 'ab' * 32},
               'policy': {'required_rounds': contracts},
               'scored_seats': [{'observed_at': {'block_hash': 'cd' * 32, 'block_number': 100}}]}
    names = _expected_members(request)
    assert len(names) <= LIMITS.members + 1
