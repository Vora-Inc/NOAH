"""Runtime490 admission keeps exact code, metadata and paired raw checks."""
import pytest

from noah.chain.ema_context import EMA_CODES
from noah.chain.sources import SourceDisagreement, SourceLied, Unverified
from noah.points.tests.test_ema_context import EmaSDK, snapshot
from noah.points.tests.test_token_context import chain, h

CODE = '0x32a16fe6163e49ae26ade2413e80b14d641c56ab4d3246d4d39013b688369d54'


class Runtime490(EmaSDK):
    def __init__(self):
        super().__init__()
        self.version = 490


def test_exact_reviewed490_pair_preserves_prices_and_identity():
    assert EMA_CODES[490] == CODE
    result = snapshot(Runtime490(), Runtime490())
    assert result['runtime'] == {
        'spec_version': 490, 'code_hash': CODE, 'transaction_version': 1,
    }
    assert result['parent_runtime'] == result['runtime']
    assert result['prices'] == snapshot()['prices']


@pytest.mark.parametrize('fault', ['code', 'transaction', 'spec'])
def test_number_alone_never_authorizes_runtime(fault):
    node = Runtime490()
    original = node.rpc_request

    def rpc(method, args):
        result = original(method, args)
        if method == 'state_getStorageHash' and fault == 'code':
            return {'result': '0x' + '00' * 32}
        if method == 'state_getRuntimeVersion':
            if fault == 'transaction':
                result['result']['transactionVersion'] = 2
            if fault == 'spec':
                result['result']['specVersion'] = 489
        return result

    node.rpc_request = rpc
    with pytest.raises(Unverified):
        snapshot(node, Runtime490())


@pytest.mark.parametrize('fault', ['ema_bad_key', 'ema_bad_decode', 'raw_bad', 'disappear'])
def test_reviewed490_does_not_weaken_raw_evidence(fault):
    node = Runtime490()
    setattr(node, fault, True)
    with pytest.raises(SourceLied):
        snapshot(node, Runtime490())


def test_reviewed490_still_requires_both_readers():
    node = Runtime490()
    node.ema[3] += 1
    with pytest.raises(SourceDisagreement):
        snapshot(Runtime490(), node)


def test_upgrade_parent_metadata_is_not_accepted_for_ema_target():
    class Upgrade(Runtime490):
        def code(self, block):
            return 490 if block >= 50 else 488

    result = chain(Upgrade(), Upgrade()).token_reference_context(50, [2, 3])
    assert result['runtime']['spec_version'] == 490
    assert result['parent_runtime']['spec_version'] == 488
    with pytest.raises(Unverified, match='exact reference runtime'):
        snapshot(Upgrade(), Upgrade())

    class TargetMetadata(Upgrade):
        def init_runtime(self, *, block_hash):
            runtime = super().init_runtime(block_hash=block_hash)
            runtime.metadata.data.to_hex = lambda: '0x' + self.md(490).hex()
            return runtime

    result = snapshot(TargetMetadata(), TargetMetadata())
    assert result['block_hash'] == h(50)
    assert result['runtime']['spec_version'] == 490
    assert result['parent_runtime']['spec_version'] == 488
