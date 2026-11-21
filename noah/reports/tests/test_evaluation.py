"""Finalized height alone is not report cutoff evidence. Synthetic chain only."""
import pytest
from noah.reports.backing.keys import hex_encode, timestamp_now_key
from noah.reports.backing.rpc import result_sha256
from noah.reports.backing.source import ReplaySource
from noah.reports.common import ReportError
from noah.reports.evaluation import prove_evaluation

GENESIS = '11' * 32
PIN = '22' * 32
HEAD = '33' * 32
BOUNDARY = {'block_number': 100, 'block_hash': PIN, 'timestamp_ms': 1794528180000}


class Chain:
    def __init__(self):
        self.headers = {
            '0x' + HEAD: {'number': '0x65', 'parentHash': '0x' + PIN},
            '0x' + PIN: {'number': '0x64', 'parentHash': '0x' + '44' * 32}}
        self.timestamp = BOUNDARY['timestamp_ms']
        self.genesis = GENESIS
        self.receipts = []

    def call(self, method, params):
        if method == 'chain_getBlockHash':
            result = '0x' + (self.genesis if params == [0] else PIN)
        elif method == 'chain_getFinalizedHead':
            result = '0x' + HEAD
        elif method == 'chain_getHeader':
            result = self.headers[params[0]]
        elif method == 'state_getRuntimeVersion':
            assert params == ['0x' + PIN]
            result = {'specVersion': 490}
        elif method == 'state_getStorage':
            assert params == [hex_encode(timestamp_now_key()), '0x' + PIN]
            result = hex_encode(self.timestamp.to_bytes(8, 'little'))
        else:
            raise AssertionError(method)
        digest, size = result_sha256(result)
        self.receipts.append({'method': method, 'params': params, 'result': result,
                              'sha256': digest, 'byte_length': size})
        return result


def prove(chain, **kwargs):
    return prove_evaluation(chain, BOUNDARY, genesis_hash=GENESIS,
                            max_ancestor_distance=kwargs.get('limit', 2))


def test_exact_ancestor_and_clock_replay_from_raw_responses():
    chain = Chain()
    proof = prove(chain)
    assert proof['ancestry'] == [HEAD, PIN]
    assert proof['evaluation'] == BOUNDARY
    assert prove(ReplaySource(chain.receipts, '0x' + PIN)) == proof


def test_same_height_on_another_fork_is_not_finalized_cutoff():
    chain = Chain()
    other = '0x' + 'aa' * 32
    chain.headers['0x' + HEAD]['parentHash'] = other
    chain.headers[other] = dict(chain.headers['0x' + PIN])
    with pytest.raises(ReportError, match='fork mismatch'):
        prove(chain)


def test_ancestry_cannot_skip_heights_or_cycle():
    chain = Chain()
    chain.headers['0x' + HEAD]['parentHash'] = '0x' + HEAD
    with pytest.raises(ReportError, match='discontinuity'):
        prove(chain)


def test_resource_limit_refuses_before_walking():
    chain = Chain()
    with pytest.raises(ReportError, match='ancestry budget'):
        prove(chain, limit=0)
    assert len(chain.receipts) == 3


def test_wrong_clock_is_not_a_closed_window():
    chain = Chain()
    chain.timestamp -= 1
    with pytest.raises(ReportError, match='timestamp mismatch'):
        prove(chain)


def test_wrong_genesis_stops_before_head_or_storage():
    chain = Chain()
    chain.genesis = '99' * 32
    with pytest.raises(ReportError, match='genesis mismatch'):
        prove(chain)
    assert len(chain.receipts) == 1
