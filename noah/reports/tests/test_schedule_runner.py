"""Daily miner ticks preserve the first resolution and never re-sign unknown sends."""
from types import SimpleNamespace
from pathlib import Path

import pytest

from noah.reports.common import ReportError, immutable_json, digest
from noah.reports.schedule_runner import scheduled_tick
from noah.reports.schedule import _resolve
from noah.reports.backing.keys import ss58_encode
from noah.reports.tests.test_schedule import schedule, Chain, ANCHOR


def setup(tmp_path, now=None):
    value = schedule()
    config = SimpleNamespace(genesis_hash=value['genesis_hash'], netuid=2,
        ark_coldkey=ss58_encode(bytes.fromhex(value['ark_coldkey'])),
        validator_hotkey=ss58_encode(bytes.fromhex(value['ark_validator_hotkey'])),
        miner_hotkey=ss58_encode(b'\x44'*32), state_dir=str(tmp_path), endpoint='ws://synthetic.invalid')
    chain = SimpleNamespace(genesis=lambda: '0x'+value['genesis_hash'],
        finalized_time_ms=lambda: now if now is not None else (ANCHOR+87600)*1000)
    return config, value, chain


def test_repeat_tick_replays_original_boundary_despite_later_finalized_head(tmp_path):
    config, value, chain = setup(tmp_path)
    resolutions, prepared, submissions = [], [], []
    def resolve(*args, **kwargs):
        resolutions.append(1)
        return _resolve(value, [Chain(head=9000)], 1)
    def submit(*args, **kwargs):
        submissions.append(kwargs)
        return {'action': 'waiting', 'sent': False}
    for _ in range(2):
        result = scheduled_tick(config, value, chain=chain, resolver=resolve,
            prepare_fn=lambda *_: prepared.append(1), submit_fn=submit)
        assert result['schedule_sha256'] == digest(value)
    assert len(resolutions) == 1
    assert len(prepared) == len(submissions) == 2
    assert all(row['authorized'] is False for row in submissions)


@pytest.mark.parametrize('now', [(ANCHOR+100)*1000, (ANCHOR+110000)*1000])
def test_before_activation_or_after_close_waits_without_preparing(tmp_path, now):
    config, value, chain = setup(tmp_path, now)
    def forbidden(*args, **kwargs):
        raise AssertionError('no phase or calculation should start')
    result = scheduled_tick(config, value, chain=chain, resolver=forbidden,
                            prepare_fn=forbidden, submit_fn=forbidden)
    assert result['action'] == 'waiting' and not result['sent']


def test_unknown_prior_send_reconciles_without_authorizing_new_phase(tmp_path):
    config, value, chain = setup(tmp_path)
    prior = _resolve(value, [Chain(head=9000)], 1)['round']
    folder = Path(config.state_dir) / 'reports' / value['genesis_hash'] / '2' / (b'\x44'*32).hex() / digest(prior)
    immutable_json(folder / 'round.json', prior)
    immutable_json(folder / 'commit.journal', {'state': 'unknown'})
    calls = []
    def submit(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs['authorized'] is False
        return {'action': 'commit', 'sent': False}
    def forbidden(*args, **kwargs):
        raise AssertionError('must reconcile before new preparation')
    result = scheduled_tick(config, value, chain=chain, authorized=True,
                            resolver=forbidden, prepare_fn=forbidden, submit_fn=submit)
    assert result['action'] == 'reconciled' and not result['sent'] and len(calls) == 1


def test_changed_schedule_destination_refuses_before_chain_access(tmp_path):
    config, value, chain = setup(tmp_path)
    value['ark_coldkey'] = 'ff' * 32
    chain.genesis = lambda: pytest.fail('mismatch must refuse before RPC')
    with pytest.raises(ReportError, match='schedule differ'):
        scheduled_tick(config, value, chain=chain)
