"""Offline exact runtime491 report compatibility and admission checks."""
import pytest

from noah.reports.backing.commitment_quota import read_commitment_quota
from noah.reports.backing.keys import timestamp_now_key
from noah.reports.backing.scale import SimSwap
from noah.reports.backing.submit import SigningRuntimeRefused
from noah.reports.common import ReportError
from noah.reports.tests.test_commitment_quota import HOT, PIN as QUOTA_PIN, SDK
from noah.reports.tests.test_engine_arithmetic import BLOCK, _add, _chain, _result
from noah.reports.tests.test_signing_runtime_guard import PARENT, PIN, adapter, key

RUNTIME_CODE_HASH = '0x4201278656a17aff924ca24123c8e5866d569a84a7150bb0372bc7078fc6173c'
SOURCE = 'be3a11b1a5300bb58d6fbad3c629e4a926bb2f44'
UNKNOWN = '0x' + '00' * 32
BEST = '0x' + 'ef' * 32
RUNTIME_490_CODE_HASH = '0x32a16fe6163e49ae26ade2413e80b14d641c56ab4d3246d4d39013b688369d54'


def engine_chain(spec, code=RUNTIME_CODE_HASH):
    chain = _chain()
    sold = 2_000_000_000_000
    _add(chain, bytes([0x22]) + bytes(31), sold, 0, sold)
    chain.sim = lambda amount: SimSwap(amount // 10, amount, 0, 0, 0, 0)
    original = chain.call
    calls = []

    def call(method, args):
        calls.append((method, args))
        if method == 'state_getRuntimeVersion':
            assert args == [BLOCK]
            return {'specVersion': spec, 'transactionVersion': 1}
        if method == 'state_getStorageHash':
            assert args == ['0x3a636f6465', BLOCK]
            return code
        return original(method, args)

    chain.call = call
    return chain, calls


def test_reviewed_491_report_calculation_matches_490():
    legacy, _ = engine_chain(490, RUNTIME_490_CODE_HASH)
    current, calls = engine_chain(491)
    old, new = _result(legacy), _result(current)
    assert old['status'] == new['status'] == 'OK'
    assert old.pop('spec_version') == 490
    assert new.pop('spec_version') == 491
    assert new == old
    assert ('state_getStorageHash', ['0x3a636f6465', BLOCK]) in calls


@pytest.mark.parametrize('code', [UNKNOWN, None])
def test_unrecognized_491_refuses_before_pool_or_scan(code):
    chain, calls = engine_chain(491, code)
    assert _result(chain)['status'] == 'UNSUPPORTED'
    assert ('state_getStorageHash', ['0x3a636f6465', BLOCK]) in calls
    assert {method for method, _ in calls} <= {
        'chain_getFinalizedHead', 'chain_getHeader', 'chain_getBlockHash',
        'state_getRuntimeVersion', 'state_getStorageHash', 'state_getStorage',
    }
    assert [args for method, args in calls if method == 'state_getStorage'] == [
        ['0x' + timestamp_now_key().hex(), BLOCK],
    ]


def signing_chain(finalized_spec, finalized_code, parent_code):
    chain = adapter()
    chain.substrate.block_hash = BEST
    chain.substrate.runtime_version = 491
    observed = []

    def rpc(method, params):
        observed.append((method, params))
        if method == 'chain_getFinalizedHead':
            assert params == []
            return PIN
        if method == 'chain_getHeader':
            assert params == [BEST]
            return {'parentHash': PARENT}
        if method == 'state_getRuntimeVersion':
            return {'specVersion': {PIN: finalized_spec, PARENT: 491}[params[0]],
                    'transactionVersion': 1}
        if method == 'state_getStorageHash':
            assert params[0] == '0x3a636f6465'
            return {PIN: finalized_code, PARENT: parent_code}[params[1]]
        pytest.fail(f'unexpected signing RPC {method}')

    chain._rpc = rpc
    return chain, observed


@pytest.mark.parametrize('finalized_spec', [490, 491])
def test_signer_gets_only_reviewed_sdk_parent_491(finalized_spec):
    chain, observed = signing_chain(finalized_spec, RUNTIME_CODE_HASH if finalized_spec == 491 else RUNTIME_490_CODE_HASH, RUNTIME_CODE_HASH)
    payloads = []
    identity, _ = chain.sign(key(lambda payload: payloads.append(payload) or bytes(64)),
                             82, 'Raw32', bytes(32))
    assert identity == '0x' + 'aa' * 32
    assert payloads == [b'synthetic-signing-payload']
    assert observed.count(('state_getStorageHash', ['0x3a636f6465', PARENT])) == 2
    if finalized_spec == 491:
        assert ('state_getStorageHash', ['0x3a636f6465', PIN]) in observed


@pytest.mark.parametrize('mismatch', ['finalized', 'sdk-parent'])
def test_unknown_491_never_reaches_signer(mismatch):
    chain, observed = signing_chain(491, UNKNOWN if mismatch == 'finalized' else RUNTIME_CODE_HASH,
                                   UNKNOWN if mismatch == 'sdk-parent' else RUNTIME_CODE_HASH)
    with pytest.raises(SigningRuntimeRefused):
        chain.sign(key(lambda _: pytest.fail('unsupported runtime reached signer')),
                   82, 'Raw32', bytes(32))
    pin = PIN if mismatch == 'finalized' else PARENT
    assert ('state_getStorageHash', ['0x3a636f6465', pin]) in observed
    assert chain.substrate.compositions == (0 if mismatch == 'finalized' else 1)


def test_quota_491_binds_source_and_code_before_pinned_storage():
    sdk = SDK()
    sdk.spec, sdk.code = 491, RUNTIME_CODE_HASH
    result = read_commitment_quota(sdk, mining_netuid=82, hotkey=HOT, required_space=100)
    assert result['runtime'] == {'spec_version': 491, 'transaction_version': 1,
                                 'code_hash': RUNTIME_CODE_HASH, 'reviewed_source_commit': SOURCE,
                                 'qualification': 'spec-and-code'}
    assert result['allowed'] is True and result['finalized_block_hash'] == QUOTA_PIN
    index = sdk.calls.index(('state_getStorageHash', ['0x3a636f6465', QUOTA_PIN]))
    assert len(sdk.calls[index+1:]) == 4
    assert all(len(call) == 3 and call[2] == QUOTA_PIN for call in sdk.calls[index+1:])


def test_unknown_491_code_does_not_read_quota_storage():
    sdk = SDK()
    sdk.spec, sdk.code = 491, UNKNOWN
    with pytest.raises(ReportError, match='unsupported finalized runtime'):
        read_commitment_quota(sdk, mining_netuid=82, hotkey=HOT, required_space=100)
    assert ('state_getStorageHash', ['0x3a636f6465', QUOTA_PIN]) in sdk.calls
    assert all(len(call) == 2 for call in sdk.calls)
