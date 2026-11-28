"""Bounded unsigned weekly roster read, invoked in the isolated reports worker.

No keys, signing, files or transactions. The primary process independently binds
this response to its own chain clock and configured template before signing.
``daily_snapshot_block`` only names one day's snapshot block. ``build`` does
not call it, and the weekly worker result is unchanged.
"""
from datetime import datetime, timezone
import json

from .assignments import (MAX_DOCUMENT_BYTES, MAX_MEMBERS, _hex32, _require,
                          _sunday, build_week, supported_targets,
                          validate_template, week_start_date)
from .budget import ProviderBudget

SCHEMA = 'noah.weekly-assignment-build/1'
RESULT = 'noah.weekly-assignment-build-result/1'


def _hash(value):
    _require(type(value) is str and value.startswith('0x'), 'block hash')
    _hex32(value[2:], 'block hash')
    return value


def _height(value):
    if type(value) is str:
        _require(value.startswith('0x'), 'block height')
        value = int(value, 16)
    _require(type(value) is int and 1 <= value < 2**32, 'block height')
    return value


def _timestamp(source, pin):
    from .backing.keys import timestamp_now_key
    raw = source.call('state_getStorage', ['0x' + timestamp_now_key().hex(), pin])
    _require(type(raw) is str and raw.startswith('0x') and len(raw) == 18, 'block timestamp')
    return int.from_bytes(bytes.fromhex(raw[2:]), 'little')


def _finalized(source, template):
    _require(source.call('chain_getBlockHash', [0]) == '0x' + template['genesis_hash'], 'assignment genesis differs')
    pin = _hash(source.call('chain_getFinalizedHead', []))
    number = _height(source.call('chain_getHeader', [pin])['number'])
    _require(_hash(source.call('chain_getBlockHash', [number])) == pin, 'finalized block differs')
    return dict(block_number=number, block_hash=pin[2:], timestamp_ms=_timestamp(source, pin))


def _week_open_block(source, week_start, finalized):
    from .schedule import _recent_range
    target = int(datetime.combine(_sunday(week_start), datetime.min.time(), timezone.utc).timestamp()) * 1000
    _require(finalized['timestamp_ms'] >= target, 'week open is not finalized')
    cache = {finalized['block_number']: finalized}
    def block(height):
        if height not in cache:
            pin = _hash(source.call('chain_getBlockHash', [height]))
            cache[height] = dict(block_number=height, block_hash=pin[2:], timestamp_ms=_timestamp(source, pin))
        return cache[height]
    low, high = _recent_range(block, finalized['block_number'], target)
    while low < high:
        middle = (low + high) // 2
        if block(middle)['timestamp_ms'] < target:
            low = middle + 1
        else:
            high = middle
    result = block(low)
    _require(result['timestamp_ms'] >= target, 'week boundary timestamp')
    if low > 1:
        _require(block(low - 1)['timestamp_ms'] < target, 'week boundary predecessor')
    return result


def _roster_round(template, block):
    from .backing.protocol.preimage import REPORT_POLICY
    from .verification import CALCULATION
    stamp = block['timestamp_ms']
    return dict(schema='noah.backing.round.v1', genesis_hash=template['genesis_hash'],
                mining_netuid=template['mining_netuid'], reported_netuid=template['mining_netuid'],
                round_date=datetime.fromtimestamp(stamp / 1000, timezone.utc).date().isoformat(),
                block_number=block['block_number'], block_hash=block['block_hash'],
                block_timestamp_ms=stamp, commit_open_ms=stamp, commit_close_ms=stamp + 1000,
                reveal_open_ms=stamp + 1000, reveal_close_ms=stamp + 2000,
                eligibility_policy_version=REPORT_POLICY, scoring_release=template['scoring_release'],
                release_prefix='11' * 16, calculation_version=CALCULATION,
                ark_coldkey=template['ark_coldkey'], ark_validator_hotkey=template['ark_validator_hotkey'])


def _build(template, source, week_start=None):
    from .backing.reads import Reader
    from .roster import read_snapshot_roster
    finalized = _finalized(source, template)
    current = week_start_date(finalized['timestamp_ms'] // 1000).isoformat()
    if week_start is None:
        week_start = current
    _require(_sunday(week_start) <= _sunday(current), 'requested week is not finalized')
    block = _week_open_block(source, week_start, finalized)
    roster = read_snapshot_roster(source, _roster_round(template, block), max_members=MAX_MEMBERS)
    reader = Reader(source, '0x' + block['block_hash'])
    reader.ensure_block(block['block_number'])
    targets = supported_targets(reader)  # Qualifies and initializes this Reader's runtime.
    week = build_week(template, roster['members'], targets, week_start=week_start,
                      roster_block_number=block['block_number'], roster_block_hash=block['block_hash'])
    result = dict(schema=RESULT, week=week, finalized=finalized)
    _require(len(json.dumps(result, separators=(',', ':'), allow_nan=False).encode()) <= MAX_DOCUMENT_BYTES,
             'assignment worker response too large')
    return result


def build(invocation):
    _require(type(invocation) is dict and set(invocation) in (
        {'schema', 'template', 'endpoint'}, {'schema', 'template', 'endpoint', 'week_start'}), 'assignment invocation fields')
    _require(invocation['schema'] == SCHEMA, 'assignment invocation schema')
    validate_template(invocation['template'])
    endpoint = invocation['endpoint']
    _require(type(endpoint) is str and endpoint.startswith(('ws://', 'wss://', 'http://', 'https://')), 'assignment endpoint')
    if 'week_start' in invocation:
        _sunday(invocation['week_start'])
    budget = ProviderBudget(seconds=120, calls=20000, response_bytes=8 * 1024**2,
                            total_bytes=128 * 1024**2)
    class Source:
        counter = 0
        def call(self, method, params):
            self.counter += 1
            result = budget.transport(endpoint, dict(jsonrpc='2.0', id=self.counter, method=method, params=params), 30)
            _require(type(result) is dict and 'result' in result and 'error' not in result, 'assignment RPC failed')
            return result['result']
    try:
        return _build(invocation['template'], Source(), invocation.get('week_start'))
    finally:
        budget.close()


def daily_snapshot_block(source, template, day, finalized):
    """First block at or after the template's daily snapshot instant.

    The caller supplies the same finalized head ``_finalized`` already checked
    for the weekly builder. This search does not read a roster, sign, or write.
    """
    from .assignments import snapshot_target_ms
    from .schedule import _recent_range
    target = snapshot_target_ms(template, day)
    _require(type(finalized) is dict and {'block_number', 'block_hash', 'timestamp_ms'} <= set(finalized),
             'finalized head')
    _require(finalized['timestamp_ms'] >= target, 'daily snapshot is not finalized')
    cache = {finalized['block_number']: finalized}

    def block(height):
        if height not in cache:
            pin = _hash(source.call('chain_getBlockHash', [height]))
            cache[height] = dict(block_number=height, block_hash=pin[2:], timestamp_ms=_timestamp(source, pin))
        return cache[height]

    low, high = _recent_range(block, finalized['block_number'], target)
    while low < high:
        middle = (low + high) // 2
        if block(middle)['timestamp_ms'] < target:
            low = middle + 1
        else:
            high = middle
    result = block(low)
    _require(result['timestamp_ms'] >= target, 'daily snapshot timestamp')
    if low > 1:
        _require(block(low - 1)['timestamp_ms'] < target, 'daily snapshot predecessor')
    return result


SUPPLEMENT_SCHEMA = 'noah.daily-assignment-build/1'
SUPPLEMENT_RESULT = 'noah.daily-assignment-build-result/1'


def _build_supplement(template, source, day):
    """Roster at one day's snapshot. No key, no file, no transaction."""
    from .roster import read_snapshot_roster
    finalized = _finalized(source, template)
    block = daily_snapshot_block(source, template, day, finalized)
    roster = read_snapshot_roster(source, _roster_round(template, block), max_members=MAX_MEMBERS)
    result = dict(schema=SUPPLEMENT_RESULT, snapshot=block, members=roster['members'], finalized=finalized)
    _require(len(json.dumps(result, separators=(',', ':'), allow_nan=False).encode()) <= MAX_DOCUMENT_BYTES,
             'assignment worker response too large')
    return result


def build_supplement_invocation(invocation):
    _require(type(invocation) is dict and set(invocation) == {
        'schema', 'template', 'endpoint', 'day'}, 'supplement invocation fields')
    _require(invocation['schema'] == SUPPLEMENT_SCHEMA, 'supplement invocation schema')
    validate_template(invocation['template'])
    endpoint = invocation['endpoint']
    _require(type(endpoint) is str and endpoint.startswith(('ws://', 'wss://', 'http://', 'https://')),
             'assignment endpoint')
    budget = ProviderBudget(seconds=240, calls=20000, response_bytes=8 * 1024**2, total_bytes=128 * 1024**2)
    class Source:
        counter = 0
        def call(self, method, params):
            self.counter += 1
            result = budget.transport(endpoint, dict(jsonrpc='2.0', id=self.counter, method=method, params=params), 30)
            _require(type(result) is dict and 'result' in result and 'error' not in result, 'assignment RPC failed')
            return result['result']
    try:
        return _build_supplement(invocation['template'], Source(), invocation['day'])
    finally:
        budget.close()
