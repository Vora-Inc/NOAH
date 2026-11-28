"""Command/service boundary tests. All identities and chain responses are synthetic."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from noah.reports import miner
from noah.reports.common import ReportError, locked, state_directory
from noah.reports.backing.keys import ss58_encode
from noah.reports.backing.submit import SubmitError
from noah.reports.tests.test_submit import FakeChain, Key


@pytest.fixture
def case(tmp_path):
    from noah.reports.tests.sample import fixture
    r = fixture()['round']
    # Explicit gap exercises the product windows rather than the old inferred ones.
    r['reveal_open_ms'] += 15000
    c = NS(genesis_hash=r['genesis_hash'], netuid=82,
           miner_hotkey=ss58_encode(b'\x33' * 32), coldkey=ss58_encode(b'\x44' * 32),
           ark_coldkey=ss58_encode(bytes.fromhex(r['ark_coldkey'])),
           validator_hotkey=ss58_encode(bytes.fromhex(r['ark_validator_hotkey'])),
           state_dir=str(tmp_path), endpoint='ws://fixture.invalid:9944')
    chain = FakeChain()
    chain.registered = {(82, c.miner_hotkey)}
    chain.now = r['commit_open_ms']
    engine = dict(status='OK', netuid=r['reported_netuid'], block_number=r['block_number'],
                  block_hash='0x'+r['block_hash'], block_timestamp_ms=r['block_timestamp_ms'],
                  ark_coldkey=r['ark_coldkey'], coverage={'coverage_gap_exceeds_holder': False},
                  output1={'status':'OK', 'drop_ppm':10},
                  output2={'status':'OK', 'alpha_required_rao':20000000},
                  output3={'status':'OK', 'alpha_required_rao':30000000})
    return c, r, chain, engine


def prepare(case):
    c, r, _, e = case
    return miner.prepare_report(c, r, 90, 3, compute_fn=lambda *_: e)


def sign(_):
    return Key(b'\x33' * 32)


def no_sign(_):
    raise AssertionError('signer must not load')


def test_unready_calculation_names_the_reason_code(case):
    c, r, _, engine = case
    for reason in ('rpc_rate_limit', 'rpc_transport', 'run_budget_exceeded',
                   'block_state_unavailable', 'runtime_spec_not_pinned'):
        failed = {**engine, 'status': 'UNAVAILABLE', 'reason': reason}
        with pytest.raises(ReportError, match=reason):
            miner.prepare_report(c, r, 90, 3, compute_fn=lambda *_: failed)
    output_failed = {**engine, 'output2': {'status': 'UNAVAILABLE', 'reason': 'all_zero_sim'}}
    with pytest.raises(ReportError, match='all_zero_sim'):
        miner.prepare_report(c, r, 90, 3, compute_fn=lambda *_: output_failed)
    fake_auth = 'user' + ':' + 'secret'
    leaked = {**engine, 'status': 'UNAVAILABLE', 'reason': f'wss://{fake_auth}@node.invalid'}
    with pytest.raises(ReportError, match='calculation_not_ready') as raised:
        miner.prepare_report(c, r, 90, 3, compute_fn=lambda *_: leaked)
    assert 'secret' not in str(raised.value)


def test_hotkey_is_unlocked_once_per_process(case, monkeypatch):
    c, r, chain, _ = case
    c.wallet_name, c.hotkey_name, c.wallet_path = 'wallet', 'hotkey', None
    opened = []
    monkeypatch.setattr(miner, '_unlock_wallet', lambda config: opened.append(config.hotkey_name) or sign(config))
    miner._OPEN_HOTKEYS.clear()
    prepare(case)
    assert miner.submit_tick(c, r, chain=chain, authorized=True)['sent']
    chain.now = r['reveal_open_ms']
    assert miner.submit_tick(c, r, chain=chain, authorized=True)['sent']
    assert opened == ['hotkey']
    # An existing journal must not unlock again, even in a fresh process cache.
    miner._OPEN_HOTKEYS.clear()
    opened.clear()
    assert miner.submit_tick(c, r, chain=chain)['action'] == 'complete'
    assert opened == []


def test_prepare_is_immutable_and_retry_never_recalculates(case):
    c, r, _, _ = case
    assert prepare(case)['outputs']['drop_ppm'] == 10
    p = state_directory(c,r)/'prepared.json'
    first = p.read_bytes()
    miner.prepare_report(c,r,90,3,compute_fn=no_sign)
    assert p.read_bytes() == first
    assert p.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ReportError, match='differs'):
        miner.prepare_report(c,r,89,3,compute_fn=no_sign)
    assert p.read_bytes() == first


def test_submit_and_restart_keep_exact_bytes_and_do_not_resend(case):
    c,r,chain,_ = case
    prepare(case)
    p=state_directory(c,r)/'prepared.json'; first=p.read_bytes()
    with pytest.raises(ReportError, match='confirmation'):
        miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)
    assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['sent']
    chain.now=r['commit_close_ms']+1
    assert miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)['action']=='waiting'
    chain.now=r['reveal_open_ms']
    assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['sent']
    assert miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)['action']=='complete'
    assert [h['variant'] for h in chain.sent]==['Raw32','BigRaw']
    assert all(h['netuid']==82 for h in chain.sent)
    assert p.read_bytes()==first


def test_unknown_send_reconciles_without_signer_or_resend(case):
    c,r,chain,_=case; prepare(case)
    broadcast=chain.broadcast
    def uncertain(handle):
        broadcast(handle)
        raise ConnectionError('secret endpoint error')
    chain.broadcast=uncertain
    with pytest.raises(SubmitError, match='unknown'):
        miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)
    chain.visible=False
    with pytest.raises(SubmitError, match='unknown'):
        miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)
    chain.visible=True
    result=miner.submit_tick(c,r,chain=chain,signer_factory=no_sign)
    assert result['commit']=='finalized' and not result['sent']
    assert len(chain.sent)==1


def test_unresolved_commit_blocks_the_next_round(case):
    c,r,chain,e=case; prepare(case)
    def lost(handle):
        chain.sent.append(handle)
        raise ConnectionError('lost reply')
    chain.broadcast = lost
    with pytest.raises(SubmitError, match='unknown'):
        miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)
    next_round={**r,'release_prefix':'aa'*16}
    miner.prepare_report(c,next_round,90,3,compute_fn=lambda *_:e)
    with pytest.raises(ReportError, match='unfinished commitment'):
        miner.submit_tick(c,next_round,chain=chain,authorized=True,signer_factory=no_sign)
    assert len(chain.sent)==1


def test_finalized_commit_allows_the_next_assignment_commit(case):
    c,r,chain,e=case; prepare(case)
    assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=sign)['commit']=='finalized'
    next_round={**r,'release_prefix':'aa'*16}
    miner.prepare_report(c,next_round,90,3,compute_fn=lambda *_:e)
    result=miner.submit_tick(c,next_round,chain=chain,authorized=True,signer_factory=sign)
    assert result['sent'] and result['commit']=='finalized'
    assert len(chain.sent)==2


def test_overlapping_process_stops_before_signing(case):
    c,r,chain,_=case;prepare(case)
    with locked(state_directory(c,r).parent):
        with pytest.raises(ReportError, match='another report operation'):
            miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)


@pytest.mark.parametrize('field,value', [('genesis_hash','ab'*32),('netuid',7),('ark_coldkey',ss58_encode(b'a'*32))])
def test_configuration_mismatch_refuses_without_compute(case,field,value):
    c,r,_,_=case;setattr(c,field,value)
    with pytest.raises(ReportError):
        miner.prepare_report(c,r,90,3,compute_fn=no_sign)


def test_deadline_and_mainnet_refuse_before_signer(case):
    c,r,chain,_=case; prepare(case)
    chain.now=r['commit_close_ms']
    assert miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)['action']=='expired'
    from noah.reports.backing.protocol.policy import MAINNET_GENESIS
    chain.genesis_hash=MAINNET_GENESIS
    with pytest.raises(SubmitError):
        miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=no_sign)


def test_wrong_ark_or_timestamp_calculation_never_prepares(case):
    c,r,_,e=case;e['ark_coldkey']='ff'*32
    with pytest.raises(ValueError,match='Ark'):
        prepare(case)
    assert not (state_directory(c,r)/'prepared.json').exists()
    e['ark_coldkey']=r['ark_coldkey'];e['block_timestamp_ms']+=1
    with pytest.raises(ReportError,match='timestamp'):
        prepare(case)


def test_snapshot_requires_finality_registration_and_owner(case):
    c,r,_,_=case
    class RPC:
        finalized=r['block_number']; owner=c.coldkey; uid=0
        def get_block_hash(self,n): return '0x'+(r['genesis_hash'] if n==0 else r['block_hash'])
        def rpc_request(self,*_):return {'result':'head'}
        def get_block_header(self,*_):return {'header':{'number':hex(self.finalized)}}
        def query(self,module,name,params=None,**kw):
            assert kw['block_hash']=='0x'+r['block_hash']
            return NS(value=r['block_timestamp_ms'] if name=='Now' else self.owner if name=='Owner' else self.uid)
    rpc=RPC(); miner.check_snapshot(c,r,rpc)
    rpc.finalized-=1
    with pytest.raises(ReportError,match='finalized'): miner.check_snapshot(c,r,rpc)
    rpc.finalized+=1;rpc.owner=ss58_encode(b'z'*32)
    with pytest.raises(ReportError,match='owner'): miner.check_snapshot(c,r,rpc)
    rpc.owner=c.coldkey;rpc.uid=None
    with pytest.raises(ReportError,match='registration'): miner.check_snapshot(c,r,rpc)


@pytest.mark.parametrize('crypto_type',[0,2,None,True])
def test_non_sr25519_refused_before_journal_or_signing(case,crypto_type):
    c,r,chain,_=case;prepare(case)
    key=sign(c);key.crypto_type=crypto_type
    with pytest.raises(SubmitError,match='SR25519'):
        miner.submit_tick(c,r,chain=chain,authorized=True,signer_factory=lambda _:key)
    assert not chain.sent
    assert not (state_directory(c,r)/'commit.journal').exists()


def test_miner_commit_and_reveal_use_configured_testnet_subnet(case):
    config, contract, chain, _ = case
    config.netuid = contract["mining_netuid"] = 2
    chain.registered = {(2, config.miner_hotkey)}
    prepare(case)
    first = miner.submit_tick(config, contract, chain=chain, authorized=True, signer_factory=sign)
    assert first["commit"] == "finalized"
    chain.now = contract["reveal_open_ms"]
    second = miner.submit_tick(config, contract, chain=chain, authorized=True, signer_factory=sign)
    assert second["reveal"] == "finalized"
    assert [call["netuid"] for call in chain.sent] == [2, 2]


def test_malformed_finalized_context_refuses_before_wallet_or_new_journal(case):
    from noah.reports.backing.chain_session import SubstrateChain
    c, r, fake, _ = case
    prepare(case)
    adapter = SubstrateChain.__new__(SubstrateChain)
    adapter.genesis = fake.genesis
    adapter.finalized_time_ms = fake.finalized_time_ms
    fake.now = r['commit_open_ms']
    adapter._rpc = lambda method, _params: '0x'+'aa'*32 if method == 'chain_getFinalizedHead' else {'specVersion': 999}
    with pytest.raises(SubmitError, match='signing context could not be read'):
        miner.submit_tick(c, r, chain=adapter, authorized=True, signer_factory=no_sign)
    assert not (state_directory(c, r)/'commit.journal').exists()


@pytest.mark.parametrize('spec', [490, 483, 999])
def test_signing_preflight_accepts_well_formed_finalized_context(spec):
    from noah.reports.backing.chain_session import SubstrateChain
    adapter = SubstrateChain.__new__(SubstrateChain)
    pin = '0x'+'ab'*32
    def rpc(method, params):
        if method == 'chain_getFinalizedHead':
            return pin
        assert method == 'state_getRuntimeVersion' and params == [pin]
        return {'specVersion': spec, 'transactionVersion': 2}
    adapter._rpc = rpc
    adapter.check_finalized_signing_context()


def test_direct_adapter_signing_checks_finalized_context_before_composition():
    from noah.reports.backing.chain_session import SubstrateChain
    adapter = SubstrateChain.__new__(SubstrateChain)
    adapter._rpc = lambda method, _params: '0x'+'aa'*32 if method == 'chain_getFinalizedHead' else {'specVersion': 999}
    adapter.substrate = NS(compose_call=lambda *_a, **_k: pytest.fail('malformed context must refuse before composition'))
    with pytest.raises(SubmitError, match='signing context could not be read'):
        adapter.sign(Key(b'\x33'*32), 2, 'Raw32', bytes(32))
