"""483 has different lifecycle storage; the official code pin is mandatory."""
import pytest

from noah.reports.backing.constants import RUNTIME_483_CODE_HASH
from noah.reports.backing.engine import compute
from noah.reports.backing.keys import hex_encode, networks_added_key, subnet_state_key
from noah.reports.backing.rpc import RpcClient, RunFailure
from noah.reports.tests.test_engine_arithmetic import _chain, _add, BLOCK, NETUID


def legacy_chain(*, code_hash=RUNTIME_483_CODE_HASH, exists='0x01'):
    chain = _chain()
    chain.spec = 483
    _add(chain, bytes([0x22]) + bytes(31), 1000, 0, 1000)
    chain.storage.pop(hex_encode(subnet_state_key(NETUID)))
    chain.storage[hex_encode(networks_added_key(NETUID))] = exists
    original = chain.call
    def call(method, params):
        if method == 'state_getStorageHash':
            chain.calls.append((method, params))
            assert params == ['0x3a636f6465', BLOCK]
            return code_hash
        return original(method, params)
    chain.call = call
    return chain


def test_official_483_uses_networks_added_and_retains_calculation():
    legacy = legacy_chain()
    modern = _chain()
    _add(modern, bytes([0x22]) + bytes(31), 1000, 0, 1000)
    old, _ = compute(legacy, NETUID, BLOCK)
    new, _ = compute(modern, NETUID, BLOCK)
    assert old['status'] == new['status'] == 'OK'
    for field in ('output1', 'output2', 'output3', 'holder'):
        assert old[field] == new[field]
    assert not any(method == 'state_getStorage' and params[0] == hex_encode(subnet_state_key(NETUID))
                   for method, params in legacy.calls)


@pytest.mark.parametrize('code_hash', [None, '0x' + 'ab' * 32, '0x00'])
def test_same_spec_with_unknown_code_refuses_before_pool_or_key_scan(code_hash):
    chain = legacy_chain(code_hash=code_hash)
    result, _ = compute(chain, NETUID, BLOCK)
    assert result['status'] == 'UNSUPPORTED'
    assert not any(method == 'state_getKeysPaged' for method, _ in chain.calls)


@pytest.mark.parametrize('exists', [None, '0x00'])
def test_absent_or_dissolved_483_subnet_cannot_use_remaining_reserves(exists):
    chain = legacy_chain(exists=exists)
    result, _ = compute(chain, NETUID, BLOCK)
    assert (result['status'], result['reason']) == ('UNAVAILABLE', 'subnet_not_found')
    assert not any(method == 'state_getKeysPaged' for method, _ in chain.calls)


def test_invalid_483_existence_encoding_fails_closed():
    result, _ = compute(legacy_chain(exists='0x02'), NETUID, BLOCK)
    assert (result['status'], result['reason']) == ('UNAVAILABLE', 'decode_error')


def test_runtime_code_read_must_be_pinned():
    called = []
    def transport(*args):
        called.append(args)
        return {'result': RUNTIME_483_CODE_HASH}
    client = RpcClient(['ws://fixture.invalid'], BLOCK, transport)
    with pytest.raises(RunFailure) as exc:
        client.call('state_getStorageHash', ['0x3a636f6465'])
    assert exc.value.reason == 'block_not_canonical' and not called
    assert client.call('state_getStorageHash', ['0x3a636f6465', BLOCK]) == RUNTIME_483_CODE_HASH
