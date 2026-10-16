"""Root adapter boundary tests with synthetic SDK-shaped chain evidence only."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant import purchase as P, purchase_runtime as R, purchase_workflow as W
from noah.deposits.assistant.conversion_config import validate
from noah.deposits.assistant.signer import Journal, Signer
from .fakes import ARK, COLD, MINER, VAL, FakeKeypair
from .test_purchase import Chain, settings
from .test_purchase_proxy import ProxyChain, SIGNING, DELEGATE, request
from .test_session import Rig


class RootReads:
    hold = 0
    root_enabled = True
    corrupt_acquisition = False

    def query(self, module, name, params=None, block_hash=None):
        if module == 'SubtensorModule':
            if name == 'Owner' and params == [VAL]:
                return ARK
            if name == 'RootStakeUnlockInterval':
                return self.hold
            if name == 'SubnetMechanism' and params == [0]:
                return 0
            if name in ('SubtokenEnabled', 'TransferToggle') and params == [0]:
                return self.root_enabled
        return super().query(module, name, params, block_hash)

    def create_storage_key(self, module, name, params, block_hash=None):
        assert (module, name) == ('SubtensorModule', 'Owner')
        return SimpleNamespace(to_hex=lambda: params[0])

    def get_storage_by_key(self, block_hash, key):
        assert key in (MINER, VAL)
        return '0x01'

    def query_map(self, module, name, params, block_hash=None):
        assert (module, name, params[1]) == ('SubtensorModule', 'Lock', 0)
        return []

    def get_constant(self, module, name, block_hash=None):
        return super().get_constant(module, name)

    def runtime_call(self, api, name, params, block_hash=None):
        if name == 'sim_swap_tao_for_alpha' and params[0] == 0:
            return dict(tao_amount=params[1], alpha_amount=params[1], tao_fee=0,
                        alpha_fee=0, tao_slippage=0, alpha_slippage=0)
        return super().runtime_call(api, name, params, block_hash)

    def submit_extrinsic(self, signed, **kw):
        super().submit_extrinsic(signed, **kw)
        # Parent fixtures use subnet3 output73; supply the root runtime's exact
        # six-field acquisition event without changing any transfer effects.
        for event in self.blocks[-1]['events']:
            if event.get('event_id') == 'StakeAdded' and event['attributes'][:2] == [COLD, MINER]:
                amount = event['attributes'][2]
                event['attributes'] = [COLD, MINER, amount,
                                       amount - int(self.corrupt_acquisition), 0, 0]


class RootChain(RootReads, Chain):
    pass


class ProxyRootChain(RootReads, ProxyChain):
    pass


@pytest.fixture(params=[False, True], ids=['direct', 'proxy'])
def root_rig(tmp_path, monkeypatch, request):
    import sys
    monkeypatch.setitem(sys.modules, 'bittensor.sp_core', SimpleNamespace(
        Keypair=lambda ss58_address: SimpleNamespace(ss58_address=ss58_address)))
    proxy = request.param
    r = Rig(tmp_path, chain=ProxyRootChain() if proxy else RootChain(),
            genesis_hash='0x' + 'b' * 64, mode='fixed', amount_rao=100,
            reserve_rao=0, signing=SIGNING if proxy else {'mode': 'direct'})
    s = settings(foreign_netuid=0, plan_id='root-day-01')
    client = W.PurchaseJournal(tmp_path / 'client')
    delegate = Journal(tmp_path / 'delegate')
    loads = []
    services = r.services()
    a = R.Adapter(r.config, s, services)
    signer = None
    if proxy:
        def loader(_):
            loads.append('opened')
            return FakeKeypair(DELEGATE)
        policy = dict(version=1, policy_id='root-test', runtime_code_hash=s.runtime_code_hash,
                      spec_version=478, max_amount_rao=1000, max_total_rao=2000,
                      max_fee_rao=s.max_fee_rao, conversion=validate(), purchase=asdict(s),
                      max_total_tao_rao=10000)
        signer = Signer(r.config, services, policy, delegate, loader)
        from .test_purchase_proxy import request as remote_request
        a.proxy_request = lambda step, operation: signer.execute(remote_request(r, s, step, operation))
    yield SimpleNamespace(r=r, s=s, a=a, client=client, delegate=delegate,
                          signer=signer, loads=loads, proxy=proxy)
    client.close()
    delegate.close()
    r.ledger.close()


def run(f, **kw):
    return W.run(f.r.config, f.s, f.a, f.client, label='root-proof',
                 ask=lambda _: f'SEND PURCHASE {f.s.max_tao_rao} TAO RAO',
                 say=lambda _: None, **kw)


def test_root_quote_is_exact_one_to_one_and_does_not_open_key(root_rig):
    f = root_rig
    quote = f.a.quote()
    assert quote['params']['netuid'] == 0
    assert quote['params']['limit_price'] == 1_000_000_000
    assert quote['params']['amount_staked'] == f.s.max_tao_rao
    assert quote['params']['allow_partial'] is False
    assert quote['estimated_output_rao'] == f.s.max_tao_rao
    assert f.r.signer_calls == 0 and not f.loads and not f.r.chain.submitted


@pytest.mark.parametrize('failure', ['hold', 'disabled'])
def test_initial_guard_blocks_workflow_before_key_or_broadcast(root_rig, failure):
    f = root_rig
    if failure == 'hold':
        f.r.chain.hold = 1
    else:
        f.r.chain.root_enabled = False
    with pytest.raises(Refusal):
        run(f)
    assert f.r.signer_calls == 0 and not f.loads and not f.r.chain.submitted
    assert f.client.workflows() == [] and f.delegate.rows() == []


@pytest.mark.parametrize('action', ['purchase', 'deposit-noah', 'deposit-foreign'])
@pytest.mark.parametrize('boundary', ['prepare', 'fresh'])
def test_changed_hold_blocks_all_legs_at_both_boundaries(root_rig, action, boundary):
    f = root_rig
    quote = f.a.quote()
    amount = f.r.config.amount_rao if action == 'deposit-noah' else f.s.max_tao_rao
    if boundary == 'fresh':
        step = f.a.prepare(action, amount, quote)
    f.r.chain.hold = 1
    with pytest.raises(Refusal):
        if boundary == 'prepare':
            f.a.prepare(action, amount, quote)
        else:
            f.a.fresh(step)
    assert f.r.signer_calls == 0 and not f.loads and not f.r.chain.submitted


def test_direct_sign_and_broadcast_recheck_root_before_side_effect(root_rig):
    f = root_rig
    if f.proxy:
        pytest.skip('direct SDK signing boundary; proxy signer tested separately')
    step = f.a.prepare('purchase', f.s.max_tao_rao, f.a.quote())
    f.r.chain.hold = 1
    with pytest.raises(Refusal):
        f.a.sign(step)
    assert f.r.signer_calls == 0 and not f.r.chain.submitted
    f.r.chain.hold = 0
    signed, _ = f.a.sign(step)
    f.r.chain.hold = 1
    persisted = []
    with pytest.raises(Refusal):
        f.a.broadcast(signed, before_submit=lambda: persisted.append(True))
    assert not persisted and not f.r.chain.submitted


def test_common_receipt_parser_applies_root_invariant():
    def events(amount, fee=0):
        return [dict(extrinsic_idx=0, module_id='SubtensorModule', event_id='StakeAdded',
                     attributes=[COLD, MINER, 5000, amount, 0, fee]),
                dict(extrinsic_idx=0, module_id='System', event_id='ExtrinsicSuccess')]
    assert P.purchase_result(events(5000), coldkey=COLD, hotkey=MINER, netuid=0,
                             requested_rao=5000, extrinsic_index=0)['foreign_received_rao'] == 5000
    for output, fee in ((4999, 0), (5001, 0), (5000, 1)):
        with pytest.raises(Refusal):
            P.purchase_result(events(output, fee), coldkey=COLD, hotkey=MINER,
                              netuid=0, requested_rao=5000, extrinsic_index=0)


def test_full_root_flow_and_history_reconciliation_ignore_new_hold(root_rig):
    f = root_rig
    out = run(f)
    assert out['delivery_complete'], out
    assert not out['credit_verified']
    assert len(f.r.chain.submitted) == 3
    assert out['steps']['purchase']['purchase_result']['foreign_received_rao'] == f.s.max_tao_rao
    receipt = out['steps']['deposit-foreign']['receipt']
    assert receipt['token_netuid'] == 0 and receipt['amount_rao'] == f.s.max_tao_rao
    assert receipt['origin_hotkey'] == MINER and receipt['coldkey'] == COLD
    # Force a fresh historical reconciliation rather than relying on cached status.
    row = f.client.workflows()[0]
    for step in row['steps'].values():
        step['status'] = 'uncertain'
    f.client.save(row)
    for entry in f.delegate.rows():
        entry['status'] = 'uncertain'
        f.delegate.put(entry)
    f.r.chain.hold = 100
    def forbidden(_):
        pytest.fail('historical receipt reconciliation applied current root execution preflight')
    f.a.root_check = forbidden
    result = run(f, status=True)
    assert result['delivery_complete'], result
    assert len(f.r.chain.submitted) == 3


def test_root_acquisition_mismatch_blocks_both_delivery_legs(root_rig):
    f = root_rig
    f.r.chain.corrupt_acquisition = True
    out = run(f)
    assert not out['delivery_complete']
    assert out['steps']['purchase']['status'] == 'uncertain'
    assert set(out['steps']) == {'purchase'}
    assert len(f.r.chain.submitted) == 1


def test_new_hold_after_purchase_stops_before_noah_delivery(root_rig):
    f = root_rig
    submit = f.r.chain.submit_extrinsic
    def changed(*args, **kwargs):
        result = submit(*args, **kwargs)
        f.r.chain.hold = 1
        return result
    f.r.chain.submit_extrinsic = changed
    out = run(f)
    assert out['steps']['purchase']['status'] == 'finalized'
    assert out['steps']['deposit-noah']['status'] == 'not_submitted'
    assert 'deposit-foreign' not in out['steps']
    assert not out['delivery_complete'] and len(f.r.chain.submitted) == 1


def test_new_hold_after_key_loading_prevents_signature_and_broadcast(root_rig):
    f = root_rig
    key = FakeKeypair(DELEGATE if f.proxy else COLD)
    def changed(*_):
        f.r.chain.hold = 1
        return key
    if f.proxy:
        f.signer.key_loader = changed
    else:
        f.a.services.signer_factory = changed
    out = run(f)
    assert not out['delivery_complete']
    assert key.signed == 0 and not f.r.chain.submitted


def test_proxy_signer_independently_enforces_root_guard(root_rig):
    f = root_rig
    if not f.proxy:
        pytest.skip('proxy signer boundary')
    quote = f.a.quote()
    step = f.a.prepare('purchase', f.s.max_tao_rao, quote)
    # A malicious client bypassing its own guard cannot bypass signer-owned checks.
    f.a.root_check = lambda _: None
    f.r.chain.hold = 1
    with pytest.raises(Refusal):
        f.signer.execute(request(f.r, f.s, step, 'check'))
    assert not f.loads and not f.r.chain.submitted and not f.delegate.rows()


def test_root_cannot_expand_quote_limit_during_fresh_check(root_rig):
    f = root_rig
    quote = f.a.quote()
    step = f.a.prepare('purchase', f.s.max_tao_rao, quote)
    step['params'] = dict(step['params'], limit_price=1_010_000_000)
    step['quote_params'] = dict(step['params'])
    with pytest.raises(Refusal, match='exact one-to-one'):
        f.a.fresh(step)
    assert f.r.signer_calls == 0 and not f.loads and not f.r.chain.submitted
