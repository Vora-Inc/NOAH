"""Synthetic pinned-runtime guards, not evidence of a funded root transaction."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from noah.chain.sources import Refusal
from noah.deposits.assistant.purchase import PurchaseSettings, purchase_result
from noah.deposits.assistant.purchase_root import root_gate, root_preflight, root_quote, root_result
from .fakes import ARK, COLD, MINER, VAL, config

GENESIS = '0x' + 'b' * 64
CODE = '0x' + 'a' * 64
CAP = 5_000_000


def settings(**changes):
    return replace(PurchaseSettings(0, VAL, CAP, 5, 1_000_000, CODE, 478), **changes)


def cfg(**changes):
    return config(genesis_hash=GENESIS, mode='fixed', amount_rao=100, reserve_rao=0, **changes)


def simulation(**changes):
    return dict(dict(tao_amount=CAP, alpha_amount=CAP, tao_fee=0, alpha_fee=0,
                     tao_slippage=0, alpha_slippage=0), **changes)


class Chain:
    """Only reads are exposed; an attempt to sign or submit has no API."""
    def __init__(self):
        self.heads = ('final', 'best')
        self.overrides = {}
        self.calls = []
        self.missing = set()
        self.locks = {}
        self.minimums = {'InitialMinStake': 1000, 'InitialMinTransfer': 1000}
        self.owner = {MINER: COLD, VAL: ARK}
        self.price = 1_000_000_000
        self.sim = simulation()
        self.version = 478
        self.code = CODE
        self.genesis = GENESIS
        self.fail = None

    def get_block_hash(self, number):
        assert number == 0
        return self.genesis

    def get_chain_finalised_head(self):
        return self.heads[0]

    def get_chain_head(self):
        return self.heads[1]

    def rpc_request(self, method, params):
        self.calls.append((method, params[-1]))
        if method == 'state_getRuntimeVersion':
            return {'result': {'specVersion': self.version}}
        assert method == 'state_getStorageHash'
        return {'result': self.code}

    def query(self, module, name, params, block_hash=None):
        assert module == 'SubtensorModule'
        self.calls.append((name, block_hash))
        if name == self.fail:
            raise RuntimeError('sensitive SDK endpoint details')
        if (block_hash, name) in self.overrides:
            return self.overrides[(block_hash, name)]
        if name == 'Owner':
            return self.owner[params[0]]
        return {'RootStakeUnlockInterval': 0, 'SubnetMechanism': 0,
                'NetworksAdded': True, 'SubtokenEnabled': True, 'TransferToggle': True}[name]

    def create_storage_key(self, module, name, params, block_hash=None):
        assert (module, name) == ('SubtensorModule', 'Owner')
        return SimpleNamespace(to_hex=lambda: params[0])

    def get_storage_by_key(self, block_hash, key):
        return None if key in self.missing else '0x1234'

    def query_map(self, module, name, params, block_hash=None):
        assert (module, name, params[1]) == ('SubtensorModule', 'Lock', 0)
        self.calls.append(('Lock', block_hash))
        return iter(self.locks.get(params[0], []))

    def get_constant(self, module, name, block_hash=None):
        assert module == 'SubtensorModule'
        self.calls.append((name, block_hash))
        return self.minimums[name]

    def runtime_call(self, api, name, params, block_hash=None):
        assert api == 'SwapRuntimeApi' and params[0] == 0
        self.calls.append((name, block_hash))
        if name == 'current_alpha_price':
            return self.price
        assert name == 'sim_swap_tao_for_alpha' and params[1] == CAP
        return self.sim


@pytest.mark.parametrize('action', ['purchase', 'deposit-foreign'])
def test_secret_free_checks_both_heads_and_exact_price(action):
    chain = Chain()
    out = root_preflight(chain, cfg(), settings(), action=action)
    assert out['amount_rao'] == CAP
    assert not out['complete_dispatch_simulation']
    assert [h['block_hash'] for h in out['heads']] == ['final', 'best']
    assert all(h['limit_price'] == 1_000_000_000 and h['estimated_output_rao'] == CAP
               for h in out['heads'])
    assert ('RootStakeUnlockInterval', 'best') in chain.calls
    assert ('RootStakeUnlockInterval', 'final') in chain.calls
    # No root UID registration query is required: existing hotkey accounts suffice.
    assert not any(name in ('Uids', 'Keys') for name, _ in chain.calls)


@pytest.mark.parametrize('head', ['final', 'best'])
@pytest.mark.parametrize('name,value', [
    ('RootStakeUnlockInterval', 1), ('RootStakeUnlockInterval', None),
    ('RootStakeUnlockInterval', False), ('RootStakeUnlockInterval', '0'),
    ('SubnetMechanism', 1), ('SubnetMechanism', None), ('SubnetMechanism', False),
    ('NetworksAdded', False), ('SubtokenEnabled', None), ('TransferToggle', False),
    ('TransferToggle', 1),
])
def test_chain_changes_and_unknown_facts_refuse(head, name, value):
    chain = Chain()
    chain.overrides[(head, name)] = value
    with pytest.raises(Refusal):
        root_preflight(chain, cfg(), settings())


def test_unknown_metadata_refuses_without_sdk_detail():
    chain = Chain()
    chain.fail = 'RootStakeUnlockInterval'
    with pytest.raises(Refusal) as exc:
        root_preflight(chain, cfg(), settings())
    assert 'sensitive' not in str(exc.value)


def test_nonzero_hold_explains_missing_durable_wait():
    chain = Chain()
    chain.overrides[('best', 'RootStakeUnlockInterval')] = 12
    with pytest.raises(Refusal, match='durable waiting is unsupported'):
        root_preflight(chain, cfg(), settings())


@pytest.mark.parametrize('field,value', [('version', 479), ('version', '478'),
    ('code', '0x' + 'c' * 64), ('code', None), ('genesis', '0x' + 'd' * 64)])
def test_runtime_and_chain_pins(field, value):
    chain = Chain()
    setattr(chain, field, value)
    with pytest.raises(Refusal):
        root_preflight(chain, cfg(), settings())


@pytest.mark.parametrize('hotkey', [MINER, VAL])
def test_default_decoded_owner_does_not_prove_hotkey_exists(hotkey):
    chain = Chain()
    chain.missing.add(hotkey)
    with pytest.raises(Refusal):
        root_preflight(chain, cfg(), settings())


def test_wrong_owner_refuses():
    chain = Chain()
    chain.owner[MINER] = ARK
    with pytest.raises(Refusal):
        root_preflight(chain, cfg(), settings())


@pytest.mark.parametrize('owner', [COLD, ARK])
def test_even_sibling_hotkey_lock_rows_refuse(owner):
    chain = Chain()
    chain.locks[owner] = [('some-other-hotkey', {'amount': 1})]
    with pytest.raises(Refusal):
        root_preflight(chain, cfg(), settings())


@pytest.mark.parametrize('constant', ['InitialMinStake', 'InitialMinTransfer'])
def test_minimum_inclusive_boundary_and_below(constant):
    chain = Chain()
    chain.minimums[constant] = CAP
    root_preflight(chain, cfg(), settings())
    chain.minimums[constant] = CAP + 1
    with pytest.raises(Refusal):
        root_preflight(chain, cfg(), settings())


def test_delivery_uses_transfer_minimum_not_a_new_acquisition_minimum():
    chain = Chain()
    chain.minimums['InitialMinStake'] = CAP + 1
    root_preflight(chain, cfg(), settings(), action='deposit-foreign')


@pytest.mark.parametrize('price', [0, 999999999, 1000000001, None, True, '1000000000'])
def test_bad_root_price(price):
    with pytest.raises(Refusal):
        root_quote(price, simulation(), CAP)


@pytest.mark.parametrize('field', ['tao_amount', 'alpha_amount', 'tao_fee', 'alpha_fee',
                                    'tao_slippage', 'alpha_slippage'])
@pytest.mark.parametrize('value', [None, True, '0', -1, 2**64, 1])
def test_bad_simulation(field, value):
    with pytest.raises(Refusal):
        root_quote(1_000_000_000, simulation(**{field: value}), CAP)


def test_simulation_unknown_shape_refuses():
    for sim in ({}, None, [], dict(simulation(), unknown=0)):
        with pytest.raises(Refusal):
            root_quote(1_000_000_000, sim, CAP)


def test_exact_root_receipt_after_real_event_parser():
    events = [
        {'extrinsic_idx': 7, 'event': {'module_id': 'System', 'event_id': 'ExtrinsicSuccess'}},
        {'extrinsic_idx': 7, 'event': {'module_id': 'SubtensorModule', 'event_id': 'StakeAdded',
                                     'attributes': [COLD, MINER, CAP, CAP, 0, 0]}},
    ]
    parsed = purchase_result(events, coldkey=COLD, hotkey=MINER, netuid=0,
                             requested_rao=CAP, extrinsic_index=7)
    assert root_result(parsed, CAP) == parsed
    assert parsed['event_ordinal'] == 1
    parsed['foreign_received_rao'] -= 1
    with pytest.raises(Refusal):
        root_result(parsed, CAP)


@pytest.mark.parametrize('field,value', [('gross_tao_moved_rao', CAP + 1),
    ('foreign_received_rao', CAP + 1), ('swap_fee_tao_rao', 1),
    ('swap_fee_tao_rao', False)])
def test_non_exact_root_receipt_refuses(field, value):
    result = dict(gross_tao_moved_rao=CAP, foreign_received_rao=CAP, swap_fee_tao_rao=0)
    result[field] = value
    with pytest.raises(Refusal):
        root_result(result, CAP)


@pytest.mark.parametrize('changes', [{'foreign_netuid': 3}, {'foreign_netuid': False},
    {'spec_version': 479}, {'max_slippage_bp': 101}, {'max_tao_rao': 0}, {'test_only': False},
    {'foreign_ark_hotkey': ''}, {'runtime_code_hash': None}, {'runtime_code_hash': 'unknown'}])
def test_root_config_gate(changes):
    with pytest.raises(Refusal):
        root_gate(cfg(), settings(**changes))


def test_mainnet_and_same_owner_recipient_refuse():
    from noah.core.chain import MAINNET
    for c in (replace(cfg(), genesis_hash=MAINNET), replace(cfg(), network='mainnet'),
              replace(cfg(), ark_coldkey=COLD)):
        with pytest.raises(Refusal):
            root_gate(c, settings())


def test_no_arbitrary_action_or_excess_delivery():
    for kw in ({'action': 'convert'}, {'amount_rao': CAP + 1}, {'amount_rao': CAP - 1}):
        with pytest.raises(Refusal):
            root_preflight(Chain(), cfg(), settings(), **kw)
