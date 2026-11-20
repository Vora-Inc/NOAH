"""Exact public493 V15 evidence, retained old proofs and negative tuple controls."""
import gzip
from hashlib import sha256
from pathlib import Path

import pytest

from noah.chain import burn_context as burn
from noah.chain.sources import Refusal
from noah.chain.tests.test_burn_context import RPC, BLOCK, PIN, META as OLD_META, context

CODE = '0x51adea39b16a2b9d60b58d430246d49312f1111b74607edc1166e47a73de4591'
DIGEST = '89f3e67a94db5f9a155678122b5a5daff5b2ddbb1f179ac669df996bd3332a98'
META = gzip.decompress((Path(__file__).parent / 'fixtures/runtime493-v15.scale.gz').read_bytes())


def frame(raw):
    return '0x' + (b'\x01' + ((len(raw) << 2) | 2).to_bytes(4, 'little') + raw).hex()


def rpc493(raw=None):
    rpc = RPC(raw)
    rpc.overrides.update(state_getRuntimeVersion={'specVersion':493,'transactionVersion':1},
                         state_getStorageHash=CODE, state_call=frame(META))
    return rpc


@pytest.mark.parametrize('raw', [None, '0x00'])
def test_exact493_preserves_proven_default_and_explicit_burn(raw):
    assert len(META) == 440323 and sha256(META).hexdigest() == DIGEST
    assert burn.REVIEWED[493] == (CODE, DIGEST)
    rpc = rpc493(raw)
    evidence = burn.read(rpc, BLOCK, PIN, 82)
    assert evidence['spec_version'] == 493 and evidence['raw'] == raw
    assert evidence['origin'] == ('runtime-default' if raw is None else 'explicit')
    assert rpc.raw_reads == 2
    assert burn.validate(context())['spec_version'] == 492  # historical receipt remains valid


@pytest.mark.parametrize('method,value', [
    ('state_getRuntimeVersion', {'specVersion':494,'transactionVersion':1}),
    ('state_getRuntimeVersion', {'specVersion':493,'transactionVersion':2}),
    ('state_getRuntimeVersion', {'specVersion':493,'transactionVersion':True}),
    ('state_getStorageHash', '0x22786f72f280dc968f38a10627c085eaf3ea530dd3a1621a4776882851ac3d8e'),
    ('state_getStorageHash', None),
    ('state_call', frame(OLD_META)),
    ('state_call', frame(META).replace('6d6574610f','6d6574610e',1)),
])
def test493_refuses_wrong_runtime_code_or_metadata_before_raw_reads(method, value):
    rpc = rpc493(); rpc.overrides[method] = value
    with pytest.raises(Refusal): burn.read(rpc, BLOCK, PIN, 82)
    assert rpc.raw_reads == 0


@pytest.mark.parametrize('raw', ['0x01','0x02','0x0000',False,0])
def test493_never_accepts_recycle_or_malformed_presence(raw):
    with pytest.raises(Refusal): burn.read(rpc493(raw), BLOCK, PIN, 82)
