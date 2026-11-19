"""Offline token and EMA admission with exact runtime 491 and 492 reference and parent."""
import pytest

from noah.chain.sources import Unverified
from noah.points.tests.test_ema_context import EmaSDK, snapshot
from noah.points.tests.test_runtime490_context import Runtime490
from noah.points.tests.test_token_context import chain, h, height

CODES = {
    491: '0x4201278656a17aff924ca24123c8e5866d569a84a7150bb0372bc7078fc6173c',
    492: '0x22786f72f280dc968f38a10627c085eaf3ea530dd3a1621a4776882851ac3d8e',
}
UNKNOWN = '0x' + '00' * 32


class ReviewedRuntime(EmaSDK):
    def __init__(self, spec, *, fault=None, upgrade=False):
        super().__init__()
        self.version = spec
        self.fault = fault
        self.upgrade = upgrade

    def code(self, block):
        return self.version - 1 if self.upgrade and block < 50 else self.version

    def rpc_request(self, method, args):
        if method == 'state_getStorageHash':
            spec = self.code(height(args[1]))
            if spec in CODES:
                bad_code = CODES.get(spec - 1, UNKNOWN)
                return {'result': bad_code if self.fault == 'code' else CODES[spec]}
        result = super().rpc_request(method, args)
        if method == 'state_getRuntimeVersion':
            if self.fault == 'spec':
                result['result']['specVersion'] = 493
            elif self.fault == 'transaction':
                result['result']['transactionVersion'] = 2
        return result


def token_snapshot(a, b):
    return chain(a, b).token_reference_context(50, [2, 3])


@pytest.mark.parametrize(('capture', 'payload'), [(token_snapshot, 'assets'), (snapshot, 'prices')])
@pytest.mark.parametrize('spec', [491, 492])
def test_exact_reviewed_runtime_keeps_reference_values(capture, payload, spec):
    current = capture(ReviewedRuntime(spec), ReviewedRuntime(spec))
    old = capture(Runtime490(), Runtime490())
    assert current['block_hash'] == h(50)
    assert current['runtime'] == {'spec_version': spec, 'code_hash': CODES[spec],
                                  'transaction_version': 1}
    assert current['parent_runtime'] == current['runtime']
    assert current[payload] == old[payload]


@pytest.mark.parametrize('capture', [token_snapshot, snapshot])
@pytest.mark.parametrize('fault', ['code', 'spec', 'transaction'])
@pytest.mark.parametrize('spec', [491, 492])
def test_unrecognized_tuple_refuses_before_values(capture, fault, spec):
    bad = ReviewedRuntime(spec, fault=fault)
    with pytest.raises(Unverified):
        capture(bad, ReviewedRuntime(spec))
    assert bad.batch_calls == 0 and bad.price_queries == []


@pytest.mark.parametrize('spec', [491, 492])
def test_upgrade_token_uses_parent_metadata_but_ema_requires_target(spec):
    token = token_snapshot(ReviewedRuntime(spec, upgrade=True), ReviewedRuntime(spec, upgrade=True))
    assert token['runtime']['spec_version'] == spec
    assert token['parent_runtime']['spec_version'] == spec - 1
    with pytest.raises(Unverified, match='exact reference runtime'):
        snapshot(ReviewedRuntime(spec, upgrade=True), ReviewedRuntime(spec, upgrade=True))

    class TargetMetadata(ReviewedRuntime):
        def init_runtime(self, *, block_hash):
            runtime = super().init_runtime(block_hash=block_hash)
            runtime.metadata.data.to_hex = lambda: '0x' + self.md(spec).hex()
            return runtime

    result = snapshot(TargetMetadata(spec, upgrade=True), TargetMetadata(spec, upgrade=True))
    assert result['runtime']['spec_version'] == spec
    assert result['parent_runtime']['spec_version'] == spec - 1
