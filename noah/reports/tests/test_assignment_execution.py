"""Miner weekly execution regressions; synthetic clocks and chains, no live sends."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from substrateinterface import Keypair

from noah.reports import assignments, schedule_runner as runner
from noah.reports.backing.keys import ss58_encode
from noah.reports.common import ReportError, digest, immutable_json
from noah.reports.schedule import _resolve, validate_schedule
from noah.reports.tests.test_schedule import ANCHOR, Chain, schedule


def setup(tmp_path, *, now=None):
    signer = Keypair.create_from_seed('0x' + '91' * 32)
    template = schedule()
    template.pop('reported_netuid')
    template.update(schema=assignments.TEMPLATE, commitment_format='context-v1',
                    assignment_signer_public_key=signer.public_key.hex())
    config = SimpleNamespace(
        genesis_hash=template['genesis_hash'], netuid=2,
        ark_coldkey=ss58_encode(bytes.fromhex(template['ark_coldkey'])),
        validator_hotkey=ss58_encode(bytes.fromhex(template['ark_validator_hotkey'])),
        miner_hotkey=ss58_encode(b'\x44' * 32), coldkey=ss58_encode(b'\x55' * 32),
        assignment_signer_public_key=signer.public_key.hex(), assignments_url='https://synthetic.invalid/',
        endpoint='ws://synthetic.invalid', state_dir=str(tmp_path))
    roster = [dict(uid=1, hotkey='44' * 32, coldkey='55' * 32, registration_block=10)]
    unsigned = assignments.build_week(template, roster, [3, 4, 5], week_start='2026-11-15',
                                     roster_block_number=20, roster_block_hash='ef' * 32)
    document = assignments.sign_week(unsigned, signer, trusted_public_key=signer.public_key.hex())
    chain = SimpleNamespace(genesis=lambda: '0x' + template['genesis_hash'],
                            finalized_time_ms=lambda: now if now is not None else (ANCHOR + 87600) * 1000,
                            substrate=SimpleNamespace(close=Mock()))
    return config, document, chain


def forbidden(*args, **kwargs):
    pytest.fail('unexpected resolver, signer, or network access')


def tick(config, document, chain, **kwargs):
    return runner.assignments_tick(config, document, chain=chain, registration_block=10, **kwargs)


def test_waits_for_snapshot_without_resolving_or_signing(tmp_path):
    config, document, chain = setup(tmp_path, now=(ANCHOR + 100) * 1000)
    result = tick(config, document, chain, resolver=forbidden, prepare_fn=forbidden, submit_fn=forbidden)
    assert result['action'] == 'waiting'
    assert result['next_at_ms'] == (ANCHOR + 86400 + 600) * 1000


def test_stale_week_refuses_before_new_report_work(tmp_path):
    config, document, chain = setup(tmp_path, now=(ANCHOR + 7 * 86400) * 1000)
    with pytest.raises(ReportError, match='different week'):
        tick(config, document, chain, authorized=True, resolver=forbidden, submit_fn=forbidden)


def test_independent_key_and_identity_are_required(tmp_path):
    config, document, chain = setup(tmp_path)
    config.assignment_signer_public_key = '99' * 32
    with pytest.raises(ReportError):
        tick(config, document, chain, authorized=True, resolver=forbidden, submit_fn=forbidden)
    config.assignment_signer_public_key = document['assignment_signer_public_key']
    config.netuid = 3
    with pytest.raises(ReportError, match='identity differ'):
        tick(config, document, chain, authorized=True, resolver=forbidden, submit_fn=forbidden)


def test_reconcile_does_not_need_current_week_file_or_authorize_a_send(tmp_path):
    config, document, chain = setup(tmp_path)
    contract = _resolve(schedule(), [Chain()], 1)['round']
    root = tmp_path / 'reports' / config.genesis_hash / '2' / ('44' * 32) / digest(contract)
    immutable_json(root / 'round.json', contract)
    immutable_json(root / 'commit.journal', {'state': 'unknown'})
    sender = Mock(return_value={'action': 'unknown', 'sent': False})
    result = runner.assignments_tick(config, None, chain=chain, authorized=True, submit_fn=sender)
    assert result['action'] == 'reconciled' and result['sent'] is False
    assert sender.call_args.kwargs['authorized'] is False


def test_failed_target_does_not_starve_next_commit_or_reveal():
    config, chain = object(), object()
    contracts = [dict(reported_netuid=n, commit_open_ms=10, commit_close_ms=20,
                      reveal_open_ms=20, reveal_close_ms=30) for n in (3, 4, 5)]
    states = [(contracts[0], dict(commit='failed', reveal='absent', prepared=True)),
              (contracts[1], dict(commit='absent', reveal='absent', prepared=True)),
              (contracts[2], dict(commit='absent', reveal='absent', prepared=True))]
    sender = Mock(return_value={'action': 'commit', 'sent': True})
    result = runner._advance(config, states, 'commit', 15, True, False, chain, forbidden, sender)
    assert result['reported_netuid'] == 4
    states[1][1].update(commit='finalized', reveal='failed')
    states[2][1].update(commit='finalized')
    result = runner._advance(config, states, 'reveal', 25, True, False, chain, forbidden, sender)
    assert result['reported_netuid'] == 5


@pytest.mark.parametrize('failed,expected', [(False, 'complete'), (True, 'partial')])
def test_retained_results_remain_terminal_after_window(tmp_path, monkeypatch, failed, expected):
    config, document, chain = setup(tmp_path, now=(ANCHOR + 110000) * 1000)
    template = assignments.template_from_document(document)
    for target in document['assignments'][0]['targets']:
        value = assignments.schedule_for_target(template, target)
        identity = validate_schedule(value)
        immutable_json(tmp_path / 'weekly-assignments' / identity / 'day-1.json',
                       _resolve(value, [Chain()], 1))
    def state(config, contract):
        return dict(prepared=True, commit='finalized', reveal='failed' if failed and contract['reported_netuid'] == 3 else 'finalized')
    monkeypatch.setattr('noah.reports.miner.status', state)
    result = tick(config, document, chain, authorized=True, resolver=forbidden, submit_fn=forbidden)
    assert result['action'] == expected
    assert len(result['reports']) == 3


def test_absent_or_reregistered_miner_waits_for_next_roster():
    responses = iter([None])
    substrate = SimpleNamespace(query=lambda *a, **kw: SimpleNamespace(value=next(responses)))
    document = dict(roster_block_hash='11' * 32, mining_netuid=2)
    assert runner._registration_block(SimpleNamespace(substrate=substrate), document, 'hotkey') is None
    responses = iter([1, 10, 1, 20])
    substrate.rpc_request = lambda *a: {'result': '0x' + '22' * 32}
    assert runner._registration_block(SimpleNamespace(substrate=substrate), document, 'hotkey') is None


def test_follow_does_not_start_tick_at_deadline(tmp_path, monkeypatch):
    from noah.reports import cli
    config, document, chain = setup(tmp_path)
    args = SimpleNamespace(file=None, timeout_seconds=1, yes=True, follow=True, allow_mainnet_report=False)
    clock = [0.0]
    monkeypatch.setattr(cli.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda duration: clock.__setitem__(0, clock[0] + duration))
    monkeypatch.setattr('noah.reports.backing.chain_session.SubstrateChain', lambda _: chain)
    monkeypatch.setattr(assignments, 'current_document', lambda *a, **kw: document)
    sender = Mock(return_value={'action': 'waiting', 'sent': False})
    monkeypatch.setattr(runner, 'assignments_tick', sender)
    assert cli._assignments(config, args) == 1
    assert sender.call_count == 1
    chain.substrate.close.assert_called_once()


def test_follow_refreshes_at_sunday_rollover(tmp_path, monkeypatch):
    from noah.reports import cli
    config, document, chain = setup(tmp_path)
    args = SimpleNamespace(file=None, timeout_seconds=30, yes=True, follow=True, allow_mainnet_report=False)
    clock = [0.0]
    monkeypatch.setattr(cli.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda duration: clock.__setitem__(0, clock[0] + duration))
    sunday = int(datetime(2026, 11, 22, tzinfo=timezone.utc).timestamp())
    chain.finalized_time_ms = lambda: (sunday + (1 if clock[0] else -1)) * 1000
    monkeypatch.setattr('noah.reports.backing.chain_session.SubstrateChain', lambda _: chain)
    fetched = []
    def fetch(*args, **kwargs):
        week = assignments.week_start_date(args[2]).isoformat()
        fetched.append(week)
        return {**document, 'week_start': week}
    monkeypatch.setattr(assignments, 'current_document', fetch)
    monkeypatch.setattr(runner, 'assignments_tick', lambda *a, **kw: {'action': 'waiting', 'sent': False})
    assert cli._assignments(config, args) == 1
    assert fetched == ['2026-11-15', '2026-11-22']


def test_cached_mixed_boundary_versions_preserve_assignment_journals(tmp_path, monkeypatch):
    from noah.reports.schedule import BOUNDARY, LEGACY_BOUNDARY
    config, document, chain = setup(tmp_path, now=(ANCHOR + 110000) * 1000)
    template = assignments.template_from_document(document)
    saved = {}
    for index, target in enumerate(document['assignments'][0]['targets']):
        value = assignments.schedule_for_target(template, target)
        path = tmp_path / 'weekly-assignments' / validate_schedule(value) / 'day-1.json'
        immutable_json(path, _resolve(value, [Chain()], 1,
                       boundary_schema=LEGACY_BOUNDARY if index == 0 else BOUNDARY))
        saved[path] = path.read_bytes()
    monkeypatch.setattr('noah.reports.miner.status', lambda *args:
                        dict(prepared=True, commit='finalized', reveal='finalized'))
    result = tick(config, document, chain, authorized=False, resolver=forbidden,
                  prepare_fn=forbidden, submit_fn=forbidden)
    assert result['action'] == 'complete' and len(result['reports']) == 3
    assert all(path.read_bytes() == raw for path, raw in saved.items())
