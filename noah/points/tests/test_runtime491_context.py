"""Offline token and EMA admission with exact runtime491 reference and parent."""
import pytest

from noah.chain.sources import Unverified
from noah.points.tests.test_ema_context import EmaSDK, snapshot
from noah.points.tests.test_runtime490_context import Runtime490
from noah.points.tests.test_token_context import chain, h, height

CODE = '0x4201278656a17aff924ca24123c8e5866d569a84a7150bb0372bc7078fc6173c'
UNKNOWN = '0x' + '00' * 32


class Runtime491(EmaSDK):
    def __init__(self, *, fault=None, upgrade=False):
        super().__init__()
        self.version = 491
        self.fault = fault
        self.upgrade = upgrade

    def code(self, block):
        return 490 if self.upgrade and block < 50 else self.version

    def rpc_request(self, method, args):
        if method == 'state_getStorageHash' and self.code(height(args[1])) == 491:
            return {'result': UNKNOWN if self.fault == 'code' else CODE}
        result = super().rpc_request(method, args)
        if method == 'state_getRuntimeVersion':
            if self.fault == 'spec':
                result['result']['specVersion'] = 492
            elif self.fault == 'transaction':
                result['result']['transactionVersion'] = 2
        return result


def token_snapshot(a, b):
    return chain(a, b).token_reference_context(50, [2, 3])


@pytest.mark.parametrize(('capture', 'payload'), [(token_snapshot, 'assets'), (snapshot, 'prices')])
def test_exact_491_keeps_reference_values(capture, payload):
    current = capture(Runtime491(), Runtime491())
    old = capture(Runtime490(), Runtime490())
    assert current['block_hash'] == h(50)
    assert current['runtime'] == {'spec_version': 491, 'code_hash': CODE,
                                  'transaction_version': 1}
    assert current['parent_runtime'] == current['runtime']
    assert current[payload] == old[payload]


@pytest.mark.parametrize('capture', [token_snapshot, snapshot])
@pytest.mark.parametrize('fault', ['code', 'spec', 'transaction'])
def test_unrecognized_491_tuple_refuses_before_values(capture, fault):
    bad = Runtime491(fault=fault)
    with pytest.raises(Unverified):
        capture(bad, Runtime491())
    assert bad.batch_calls == 0 and bad.price_queries == []


def test_491_upgrade_token_uses_parent_metadata_but_ema_requires_target():
    token = token_snapshot(Runtime491(upgrade=True), Runtime491(upgrade=True))
    assert token['runtime']['spec_version'] == 491 and token['parent_runtime']['spec_version'] == 490
    with pytest.raises(Unverified, match='exact reference runtime'):
        snapshot(Runtime491(upgrade=True), Runtime491(upgrade=True))

    class TargetMetadata(Runtime491):
        def init_runtime(self, *, block_hash):
            runtime = super().init_runtime(block_hash=block_hash)
            runtime.metadata.data.to_hex = lambda: '0x' + self.md(491).hex()
            return runtime

    result = snapshot(TargetMetadata(upgrade=True), TargetMetadata(upgrade=True))
    assert result['runtime']['spec_version'] == 491 and result['parent_runtime']['spec_version'] == 490
