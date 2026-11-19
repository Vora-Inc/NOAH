"""Fresh-signing guards and recovery. Sign callbacks return synthetic zeros.

No private key, cryptographic signing, broadcast or network is used. The final
test uses the real installed SDK and raw SCALE metadata with a divergent head.
"""
import json
from types import SimpleNamespace as NS

import pytest

from noah.reports import miner
from noah.reports.backing.chain_session import SubstrateChain
from noah.reports.backing.keys import ss58_encode
from noah.reports.backing.submit import SigningRuntimeRefused, SubmitError
from noah.reports.common import state_directory
from noah.reports.tests.test_miner_commands import case, prepare, sign, no_sign


PIN = '0x' + 'ab' * 32
PARENT = '0x' + 'cd' * 32


def metadata(value=b'synthetic-metadata'):
    return NS(data=NS(data=bytearray(value)))


class SDK:
    def __init__(self):
        self.block_hash, self.runtime_version, self.transaction_version = PIN, 490, 1
        self.metadata = metadata()
        self.before_sign = lambda: None
        self.compositions = 0

    def compose_call(self, *_args, **_kwargs):
        self.compositions += 1
        self.call = NS(metadata=self.metadata)
        return self.call

    def create_signed_extrinsic(self, *, call, keypair):
        self.before_sign()
        keypair.sign(b'synthetic-signing-payload')
        return NS(extrinsic_hash=b'\xaa' * 32)


def adapter():
    chain = SubstrateChain.__new__(SubstrateChain)
    chain.substrate = SDK()
    chain.version = {'specVersion': 490, 'transactionVersion': 1}
    def rpc(method, params):
        if method == 'chain_getFinalizedHead':
            return PIN
        if method == 'chain_getHeader':
            return {'parentHash': PARENT}
        if method == 'state_getRuntimeVersion':
            return dict(chain.version)
        raise AssertionError(method)
    chain._rpc = rpc
    # These tests isolate signing-runtime races; quota arithmetic/RPC binding is
    # independently exercised through the real helper in test_commitment_quota.
    chain.commitment_quota = lambda *_: {'allowed': True,
        'finalized_timestamp_ms': chain.finalized_time_ms(), 'genesis_hash': chain.genesis()}
    return chain


def key(callback):
    return NS(crypto_type=1, public_key=b'\x33' * 32,
              ss58_address=ss58_encode(b'\x33' * 32), sign=callback)


def test_supported_unchanged_runtime_invokes_signer_exactly_once():
    chain, calls = adapter(), []
    identity, _ = chain.sign(key(lambda payload: calls.append(payload) or bytes(64)), 82, 'Raw32', bytes(32))
    assert identity == '0x' + 'aa' * 32
    assert calls == [b'synthetic-signing-payload']


@pytest.mark.parametrize('change', ['unsupported', 'sdk_spec', 'transaction', 'sdk_metadata', 'call_metadata', 'rpc_failure'])
def test_actual_signer_boundary_refuses_runtime_or_metadata_change(change):
    chain, calls = adapter(), []
    def mutate():
        if change == 'unsupported':
            chain.version['specVersion'] = chain.substrate.runtime_version = 999
        elif change == 'sdk_spec':
            chain.substrate.runtime_version = 491
        elif change == 'transaction':
            chain.version['transactionVersion'] = chain.substrate.transaction_version = 2
        elif change == 'sdk_metadata':
            chain.substrate.metadata.data.data[0] ^= 1
        elif change == 'call_metadata':
            chain.substrate.call.metadata = metadata(b'different')
        else:
            chain._rpc = lambda *_: (_ for _ in ()).throw(ConnectionError('synthetic read failure'))
    chain.substrate.before_sign = mutate
    with pytest.raises(SigningRuntimeRefused):
        chain.sign(key(lambda payload: calls.append(payload) or bytes(64)), 82, 'Raw32', bytes(32))
    assert calls == []


@pytest.mark.parametrize('error', [ValueError('synthetic signer failure'), SigningRuntimeRefused('wallet-raised lookalike')])
def test_any_error_from_actual_signer_is_not_classified_as_proven_unsent(error):
    chain, calls = adapter(), []
    def signing(_):
        calls.append(1)
        raise error
    with pytest.raises(Exception) as caught:
        chain.sign(key(signing), 82, 'Raw32', bytes(32))
    assert not isinstance(caught.value, SigningRuntimeRefused)
    assert calls == [1]


def test_metadata_mutation_after_signing_begins_remains_uncertain():
    chain = adapter()
    def signing(_):
        chain.substrate.metadata = metadata(b'changed-after-sign')
        return bytes(64)
    with pytest.raises(SubmitError, match='after signing began') as caught:
        chain.sign(key(signing), 82, 'Raw32', bytes(32))
    assert not isinstance(caught.value, SigningRuntimeRefused)


def test_sdk_error_after_callback_is_never_a_proven_unsent_refusal():
    chain, calls = adapter(), []
    def fail_after_callback(*, call, keypair):
        keypair.sign(b'synthetic-signing-payload')
        raise SigningRuntimeRefused('SDK lookalike after signature')
    chain.substrate.create_signed_extrinsic = fail_after_callback
    with pytest.raises(SubmitError, match='SDK failed after signing began') as caught:
        chain.sign(key(lambda payload: calls.append(payload) or bytes(64)), 82, 'Raw32', bytes(32))
    assert not isinstance(caught.value, SigningRuntimeRefused)
    assert len(calls) == 1


def test_second_guard_upgrade_retains_terminal_unsent_journal_and_does_not_block_next_round(case):
    config, contract, fake, engine = case
    prepare(case)
    chain = adapter()
    for name in ('genesis', 'finalized_time_ms', 'is_registered'):
        setattr(chain, name, getattr(fake, name))
    runtime_reads = []
    def rpc(method, _params):
        if method == 'chain_getFinalizedHead':
            return PIN
        runtime_reads.append(method)
        return {'specVersion': 490 if len(runtime_reads) == 1 else 999}
    chain._rpc = rpc
    with pytest.raises(SigningRuntimeRefused):
        miner.submit_tick(config, contract, chain=chain, authorized=True, signer_factory=sign)
    path = state_directory(config, contract) / 'commit.journal'
    before = path.read_bytes()
    saved = json.loads(before)
    assert saved['state'] == 'failed' and saved['submission_attempted'] is False
    assert saved['extrinsic_hash'] is None and chain.substrate.compositions == 0
    with pytest.raises(SubmitError, match='no submission attempted'):
        miner.submit_tick(config, contract, chain=chain, authorized=True, signer_factory=no_sign)
    assert path.read_bytes() == before
    later = {**contract, 'release_prefix': 'aa' * 16}
    miner.prepare_report(config, later, 90, 3, compute_fn=lambda *_: engine)
    assert miner.submit_tick(config, later, chain=fake, authorized=True, signer_factory=sign)['sent']
    assert len(fake.sent) == 1


def test_actual_signer_exception_keeps_existing_unknown_journal_unreplayable(case):
    config, contract, fake, _ = case
    prepare(case)
    chain = adapter()
    for name in ('genesis', 'finalized_time_ms', 'is_registered'):
        setattr(chain, name, getattr(fake, name))
    def fail(_):
        raise SigningRuntimeRefused('wallet-internal failure after invocation')
    with pytest.raises(SubmitError, match='after signing began'):
        miner.submit_tick(config, contract, chain=chain, authorized=True, signer_factory=lambda _: key(fail))
    path = state_directory(config, contract) / 'commit.journal'
    saved = json.loads(path.read_bytes())
    assert saved['state'] == 'claimed' and 'submission_attempted' not in saved
    with pytest.raises(SubmitError, match='outcome unknown'):
        miner.submit_tick(config, contract, chain=chain, authorized=True, signer_factory=no_sign)
    saved = json.loads(path.read_bytes())
    assert saved['state'] == 'unresolved' and 'submission_attempted' not in saved


@pytest.mark.parametrize('best_spec', [490, 999])
def test_real_sdk_best_head_runtime_is_checked_at_signing(best_spec):
    from substrateinterface import SubstrateInterface
    from noah.reports.tests.raw_export_fixture import RawChain, RawSocket, block_hash, HOT
    raw = RawChain()
    original = raw.call
    for n in (7, 8):
        raw.headers[block_hash(n)] = {**raw.headers[block_hash(6)],
            'number': hex(n), 'parentHash': block_hash(n - 1)}
    def rpc(method, params):
        if method == 'system_accountNextIndex':
            return 0
        if method == 'chain_getHead' or (method == 'chain_getBlockHash' and not params):
            return block_hash(8)
        if method == 'state_getRuntimeVersion' and params == [block_hash(7)]:
            return {**original(method, params), 'specVersion': best_spec}
        return original(method, params)
    raw.call = rpc
    sdk = SubstrateInterface(websocket=RawSocket(raw), auto_discover=False)
    sdk.init_runtime()
    assert sdk.runtime_version == best_spec
    chain = SubstrateChain.__new__(SubstrateChain)
    chain.substrate = sdk
    chain.ensure_supported_signing_runtime()  # Finalized block6 still reports490.
    calls = []
    public_key = NS(public_key=HOT, ss58_address=ss58_encode(HOT), crypto_type=1,
                    sign=lambda _: calls.append(1) or bytes(64))
    if best_spec == 999:
        with pytest.raises(SigningRuntimeRefused, match='SDK signing runtime is not supported'):
            chain.sign(public_key, 82, 'Raw32', bytes(32))
        assert calls == []
    else:
        identity, extrinsic = chain.sign(public_key, 82, 'Raw32', bytes(32))
        assert calls == [1]
        assert identity == '0x' + extrinsic.extrinsic_hash.hex()
