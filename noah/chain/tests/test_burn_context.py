"""Real reviewed metadata plus adversarial offline RPC replay; no chain writes."""
import gzip
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from noah.chain import burn_context as burn
from noah.chain.agreeing import AgreeingChain
from noah.chain.sources import Refusal, SourceDisagreement

PIN = '0x' + 'ab' * 32
GENESIS = '0x' + 'cd' * 32
BLOCK = 100
META = gzip.decompress((Path(__file__).parent / 'fixtures/runtime492-v15.scale.gz').read_bytes())
FRAME = '0x' + (b'\x01' + ((len(META) << 2) | 2).to_bytes(4, 'little') + META).hex()


class RPC:
    def __init__(self, raw=None):
        self.raw = raw
        self.calls = []
        self.overrides = {}
        self.raw_reads = 0

    def __call__(self, method, args):
        self.calls.append((method, args))
        if method in self.overrides:
            value = self.overrides[method]
            return value(args) if callable(value) else value
        if method == 'chain_getFinalizedHead':
            assert args == []
            return PIN
        if method == 'chain_getHeader':
            assert args == [PIN]
            return {'number': hex(BLOCK)}
        if method == 'chain_getBlockHash':
            assert args in ([BLOCK], [0])
            return GENESIS if args == [0] else PIN
        assert args[-1] == PIN
        if method == 'state_getRuntimeVersion':
            return {'specVersion': 492, 'transactionVersion': 1}
        if method == 'state_getStorageHash':
            assert args[0] == '0x3a636f6465'
            return burn.REVIEWED[492][0]
        if method == 'state_call':
            assert args[:2] == ['Metadata_metadata_at_version', '0x0f000000']
            return FRAME
        assert method == 'state_queryStorageAt'
        assert args[0] == [burn.storage_key(82)]
        self.raw_reads += 1
        return [{'block': PIN, 'changes': [[burn.storage_key(82), self.raw]]}]


def context(raw=None):
    return burn.read(RPC(raw), BLOCK, PIN, 82)


@pytest.mark.parametrize('raw,origin', [(None, 'runtime-default'), ('0x00', 'explicit')])
def test_reviewed_metadata_and_actual_raw_presence_remain_distinct(raw, origin):
    rpc = RPC(raw)
    evidence = burn.read(rpc, BLOCK, PIN, 82)
    assert evidence['raw'] == raw and evidence['origin'] == origin
    assert evidence['effective'] == 'Burn' and rpc.raw_reads == 2
    assert sha256(META).hexdigest() == burn.REVIEWED[492][1]
    assert evidence['key'] == '0x658faa385070e074c85bf6b568cf055530823bc1353bfe6cb262413a5bd7a0235200'


@pytest.mark.parametrize('raw', ['0x01', '0x02', '0x0000', '', False, 0, {}, []])
def test_recycle_and_malformed_raw_refuse(raw):
    with pytest.raises(Refusal):
        context(raw)


@pytest.mark.parametrize('method,value', [
    ('state_getRuntimeVersion', {'specVersion': 491, 'transactionVersion': 1}),
    ('state_getRuntimeVersion', {'specVersion': 492, 'transactionVersion': True}),
    ('state_getRuntimeVersion', {'specVersion': '492', 'transactionVersion': 1}),
    ('state_getStorageHash', '0x' + 'ff' * 32),
    ('state_getStorageHash', None),
    ('state_call', None),
    ('state_call', '0x00'),
    ('state_call', FRAME[:-2] + 'ff'),
    ('state_call', '0x01'),
    ('state_queryStorageAt', []),
    ('state_queryStorageAt', [{'block': GENESIS, 'changes': [[burn.storage_key(82), None]]}]),
    ('state_queryStorageAt', [{'block': PIN, 'changes': []}]),
    ('state_queryStorageAt', [{'block': PIN, 'changes': [[burn.storage_key(83), None]]}]),
    ('state_queryStorageAt', [{'block': PIN, 'changes': [[burn.storage_key(82), None]] * 2}]),
    ('chain_getFinalizedHead', None),
    ('chain_getHeader', {'number': '0x63'}),
    ('chain_getBlockHash', GENESIS),
])
def test_unreviewed_or_missing_evidence_refuses(method, value):
    rpc = RPC()
    rpc.overrides[method] = value
    with pytest.raises(Refusal):
        burn.read(rpc, BLOCK, PIN, 82)


def test_missing_storage_declaration_cannot_match_reviewed_metadata():
    # Full metadata digest binds the declaration and default, not merely enum bytes.
    replacement = META.replace(b'RecycleOrBurn', b'MissingOrBurn')
    assert replacement != META and len(replacement) == len(META)
    rpc = RPC()
    rpc.overrides['state_call'] = '0x' + (b'\x01' + ((len(replacement) << 2) | 2).to_bytes(4, 'little') + replacement).hex()
    with pytest.raises(Refusal, match='declaration/default'):
        burn.read(rpc, BLOCK, PIN, 82)


def test_failed_rpc_is_not_absence():
    rpc = RPC()
    def fail(_):
        raise OSError('offline failure')
    rpc.overrides['state_queryStorageAt'] = fail
    with pytest.raises(OSError):
        burn.read(rpc, BLOCK, PIN, 82)


def test_same_pin_conflicting_raw_and_later_runtime_change_never_reuse_proof():
    rpc = RPC()
    def raw(_):
        rpc.raw_reads += 1
        return [{'block': PIN, 'changes': [[burn.storage_key(82), None if rpc.raw_reads == 1 else '0x00']]}]
    rpc.overrides['state_queryStorageAt'] = raw
    with pytest.raises(Refusal, match='changed'):
        burn.read(rpc, BLOCK, PIN, 82)
    rpc = RPC()
    burn.read(rpc, BLOCK, PIN, 82)
    rpc.overrides['state_getStorageHash'] = '0x' + 'ef' * 32
    with pytest.raises(Refusal, match='code'):
        burn.read(rpc, BLOCK, PIN, 82)


def test_real_agreeing_reader_compares_whole_proof_not_effective_value_only():
    def source(raw):
        return SimpleNamespace(block_hash=lambda block: PIN, canary=lambda block: 256,
            burn_context=lambda block, netuid, expected_block_hash: burn.read(RPC(raw), block, expected_block_hash, netuid))
    pair = AgreeingChain({'a': source(None), 'b': source(None)}, {'a': 'wss://a.example', 'b': 'wss://b.example'})
    assert pair.burn_context(BLOCK, 82, expected_block_hash=PIN) == context()
    pair._sources['b'] = source('0x00')
    with pytest.raises(SourceDisagreement):
        pair.burn_context(BLOCK, 82, expected_block_hash=PIN)


def test_independently_reviewed_testnet483_tuple_accepts_absence():
    meta = gzip.decompress((Path(__file__).parent / 'fixtures/runtime483-v15.scale.gz').read_bytes())
    assert sha256(meta).hexdigest() == burn.REVIEWED[483][1]
    rpc = RPC()
    rpc.overrides.update({
        'state_getRuntimeVersion': {'specVersion': 483, 'transactionVersion': 1},
        'state_getStorageHash': burn.REVIEWED[483][0],
        'state_call': '0x' + (b'\x01' + ((len(meta) << 2) | 2).to_bytes(4, 'little') + meta).hex(),
    })
    evidence = burn.read(rpc, BLOCK, PIN, 82)
    assert evidence['spec_version'] == 483 and evidence['raw'] is None
    rpc.overrides['state_call'] = FRAME  # Same storage layout is not the same proof.
    with pytest.raises(Refusal):
        burn.read(rpc, BLOCK, PIN, 82)


@pytest.mark.parametrize('netuid', [False, True, -1, 65536, '82', None])
def test_bad_netuid_never_reaches_rpc(netuid):
    rpc = RPC()
    with pytest.raises(Refusal):
        burn.read(rpc, BLOCK, PIN, netuid)
    assert rpc.calls == []


def test_reviewed_identity_u16_key_boundaries():
    assert burn.storage_key(0).endswith('0000')
    assert burn.storage_key(65535).endswith('ffff')
    assert len(burn.storage_key(0)) == 70


@pytest.mark.parametrize('field,value', [
    ('schema', 'unknown'), ('block', True), ('block', 0), ('block_hash', '0xab'),
    ('genesis', '0x' + '00' * 32), ('netuid', True), ('spec_version', 491),
    ('transaction_version', True), ('code_hash', 'other'), ('metadata_sha256', 'other'),
    ('key', burn.storage_key(83)), ('raw', '0x01'), ('effective', 'Recycle'), ('origin', 'explicit'),
])
def test_retained_evidence_semantics_not_only_content_hash(field, value):
    evidence = context()
    evidence[field] = value
    with pytest.raises(Refusal):
        burn.validate(evidence)


@pytest.mark.parametrize('number', [None, True, 100, '', '-1', '0x', '0xZZ', '100'])
def test_malformed_finalized_header_refuses(number):
    rpc = RPC()
    rpc.overrides['chain_getHeader'] = {'number': number}
    with pytest.raises(Refusal):
        burn.read(rpc, BLOCK, PIN, 82)


@pytest.mark.parametrize('frame', [FRAME + '00', FRAME[:6], '0x01076d6574610f', '0x01046d'])
def test_metadata_framing_is_canonical(frame):
    rpc = RPC()
    rpc.overrides['state_call'] = frame
    with pytest.raises(Refusal):
        burn.read(rpc, BLOCK, PIN, 82)


def test_live_reader_adapts_rpc_envelope_and_preserves_requested_pin():
    from noah.chain.live import LiveRunnerChain
    rpc = RPC()
    live = object.__new__(LiveRunnerChain)
    live._connection = SimpleNamespace(rpc_request=lambda method, args: {'result': rpc(method, args)})
    live.block_hash = lambda block: PIN
    assert live.burn_context(BLOCK, 82, expected_block_hash=PIN) == context()
    with pytest.raises(Refusal):
        live.burn_context(BLOCK, 82, expected_block_hash=GENESIS)
