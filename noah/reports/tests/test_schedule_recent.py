"""Recent-state search and immutable pre-upgrade proof compatibility."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from noah.reports.common import ReportError
from noah.reports.schedule import BOUNDARY, LEGACY_BOUNDARY, _Reader, _resolve, verify_resolution
from noah.reports.tests.test_schedule import Chain, schedule


class History:
    genesis = '11' * 32

    def __init__(self, head, *, earliest=0, timestamp=None, unavailable=(), fork_parent=False):
        self.head, self.earliest = head, earliest
        self.timestamp = timestamp or (lambda n: n * 12000)
        self.unavailable = set(unavailable)
        self.fork_parent = fork_parent
        self.calls, self.read_heights = [], []

    def block_hash(self, number):
        return '0x' + (self.genesis if number == 0 else format(number, '064x'))

    def number(self, pin):
        return 0 if pin == self.block_hash(0) else int(pin, 16)

    def rpc_request(self, method, params):
        self.calls.append((method, params))
        if method == 'chain_getBlockHash':
            return {'result': self.block_hash(params[0])}
        if method == 'chain_getFinalizedHead':
            return {'result': self.block_hash(self.head)}
        if method == 'chain_getHeader':
            number = self.number(params[0])
            return {'result': {'number': hex(number), 'parentHash': self.block_hash(
                number - 1 + int(self.fork_parent))}}
        assert method == 'state_getStorage'
        number = self.number(params[1])
        self.read_heights.append(number)
        if number in self.unavailable or number < self.earliest:
            return {'error': {'code': 4003}}
        return {'result': None if number == 0 else '0x' + self.timestamp(number).to_bytes(8, 'little').hex()}

    def boundary(self, target, **kwargs):
        return _Reader(self).boundary({'genesis_hash': self.genesis}, target, **kwargs)


def test_recent_target_does_not_require_ancient_half_chain_state():
    head = 9_540_000
    target = (head - 4000) * 12000
    recent = History(head, earliest=head - 16_384)
    result = recent.boundary(target)
    assert result['snapshot']['block_number'] == head - 4000
    assert result['predecessor']['block_number'] == head - 4001
    assert min(recent.read_heights) >= head - 4096
    assert len(recent.calls) <= 138
    old = History(head, earliest=head - 16_384)
    with pytest.raises(ReportError, match='boundary RPC failed'):
        old.boundary(target, schema=LEGACY_BOUNDARY)
    assert old.read_heights == [head, (head + 1) // 2]


@pytest.mark.parametrize('head,target_block', [(1, 1), (2, 1), (2, 2),
    (2**32 - 1, 1), (2**32 - 1, 2), (2**32 - 1, 2**31),
    (2**32 - 1, 2**32 - 1025), (2**32 - 1, 2**32 - 1)])
def test_first_and_maximum_height_boundaries_fit_unchanged_budget(head, target_block):
    history = History(head)
    result = history.boundary(target_block * 12000)
    assert result['snapshot']['block_number'] == target_block
    assert result['predecessor']['block_number'] == target_block - 1
    assert len(history.calls) <= 138
    assert len(result['receipts']) == len(history.calls)


@pytest.mark.parametrize('timestamp,target,expected', [
    (lambda n: (n // 5) * 12000, 12000 * 10, 50),
    (lambda n: n * 12000 + (600000 if n >= 50 else 0), 50 * 12000, 50),
    (lambda n: n * 12000, 50 * 12000 + 1, 51),
])
def test_duplicate_timestamps_and_gaps_still_choose_exact_first_block(timestamp, target, expected):
    result = History(100, timestamp=timestamp).boundary(target)
    assert result['snapshot']['block_number'] == expected
    assert result['predecessor']['timestamp_ms'] < target <= result['snapshot']['timestamp_ms']


@pytest.mark.parametrize('unavailable', [(99,), (68,), (50,), (49,)])
def test_missing_bracket_or_required_state_never_counts_as_an_old_timestamp(unavailable):
    with pytest.raises(ReportError, match='boundary RPC failed'):
        History(100, unavailable=unavailable).boundary(50 * 12000)


def test_parent_and_finalization_checks_remain_required():
    with pytest.raises(ReportError, match='parent differs'):
        History(100, fork_parent=True).boundary(50 * 12000)
    with pytest.raises(ReportError, match='not finalized'):
        History(100).boundary(101 * 12000)


def legacy_fixture():
    raw = Path(__file__).with_name('fixtures').joinpath('schedule-boundary-v1.json').read_bytes()
    # Frozen from the unmodified bcc9 schedule resolver before this implementation.
    assert hashlib.sha256(raw).hexdigest() == '11d22d308f0dc3e4d262d40ca1a3dcf5cc1abff7e5903b7eae38601990c52d96'
    return json.loads(raw)


def test_frozen_v1_transcript_replays_exactly_without_relabeling():
    fixture = legacy_fixture()
    original = copy.deepcopy(fixture['resolution'])
    assert verify_resolution(fixture['schedule'], fixture['day'], original) == original
    assert original['boundary_proof']['schema'] == LEGACY_BOUNDARY
    fresh = _resolve(fixture['schedule'], [Chain(), Chain(head=9100)], fixture['day'])
    assert fresh['boundary_proof']['schema'] == BOUNDARY
    assert fresh['boundary_proof'] != original['boundary_proof']
    for field in ('round', 'policy', 'schedule_sha256'):
        assert fresh[field] == original[field]
    assert verify_resolution(fixture['schedule'], fixture['day'], fresh) == fresh
    assert fixture['resolution'] == original


@pytest.mark.parametrize('schema', [LEGACY_BOUNDARY, BOUNDARY])
@pytest.mark.parametrize('change', ['relabel', 'unknown', 'extra', 'missing', 'target', 'params'])
def test_both_versions_reject_relabeling_and_transcript_tampering(schema, change):
    value = legacy_fixture()['resolution'] if schema == LEGACY_BOUNDARY else _resolve(schedule(), [Chain(), Chain(head=9100)], 1)
    proof = value['boundary_proof']
    receipts = proof['readers'][0]['receipts']
    if change == 'relabel':
        proof['schema'] = BOUNDARY if schema == LEGACY_BOUNDARY else LEGACY_BOUNDARY
    elif change == 'unknown':
        proof['schema'] = 'noah.report-schedule-boundary/3'
    elif change == 'extra':
        receipts.append(copy.deepcopy(receipts[-1]))
    elif change == 'missing':
        receipts.pop()
    elif change == 'target':
        proof['target_ms'] += 1
    else:
        receipts[0]['params'] = [1]
    with pytest.raises(ReportError):
        verify_resolution(schedule(), 1, value)
