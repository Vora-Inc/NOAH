"""Derive the same daily report from an immutable, pre-distributed schedule.

This module announces nothing and signs nothing. Windows use the configured UTC
slot, never process start time. A delayed chain cannot shorten a miner's window.
RPC observations identify the boundary; the report provider separately verifies
the complete round and its finalized evidence before enforcing any result.
"""
from datetime import datetime, timezone
import re
import json

from .common import ReportError, digest

SCHEMA = 'noah.report-schedule/1'
FIELDS = set(('schema genesis_hash mining_netuid reported_netuid day_anchor_ts '
              'effective_day snapshot_offset_seconds prepare_lead_seconds '
              'commit_seconds reveal_seconds eligibility_policy_version '
              'calculation_version scoring_release ark_coldkey ark_validator_hotkey').split())


def _require(condition, message):
    if not condition:
        raise ReportError('report schedule: ' + message)


def validate_schedule(schedule):
    from .backing.protocol.preimage import REPORT_POLICY
    from .verification import CALCULATION
    _require(type(schedule) is dict and FIELDS <= set(schedule) <= FIELDS | {'commitment_format'}, 'fields differ')
    if 'commitment_format' in schedule:
        _require(schedule['commitment_format'] == 'context-v1', 'unsupported commitment format')
    _require(schedule['schema'] == SCHEMA, 'unsupported schema')
    for key in ('genesis_hash', 'ark_coldkey', 'ark_validator_hotkey'):
        value = schedule[key]
        _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'invalid ' + key)
    for key in ('mining_netuid', 'reported_netuid'):
        _require(type(schedule[key]) is int and 1 <= schedule[key] <= 65535, 'invalid ' + key)
    for key in ('day_anchor_ts', 'effective_day', 'snapshot_offset_seconds',
                'prepare_lead_seconds', 'commit_seconds', 'reveal_seconds'):
        _require(type(schedule[key]) is int and 0 <= schedule[key] < 2**32, 'invalid ' + key)
    _require(schedule['day_anchor_ts'] % 86400 == 0, 'anchor must be UTC midnight')
    _require(schedule['prepare_lead_seconds'] > 0, 'positive preparation lead required')
    _require(schedule['commit_seconds'] == schedule['reveal_seconds'] == 7200,
             'production windows must each be two hours')
    _require(schedule['snapshot_offset_seconds'] + schedule['prepare_lead_seconds'] + 14400 < 86400,
             'round must close within its scoring day')
    _require(schedule['eligibility_policy_version'] == REPORT_POLICY, 'report-only policy required')
    _require(schedule['calculation_version'] == CALCULATION, 'unsupported calculation')
    _require(type(schedule['scoring_release']) is str and 0 < len(schedule['scoring_release']) <= 256,
             'release identity required')
    return digest(schedule)


def day_for_time(schedule, unix_ms):
    validate_schedule(schedule)
    _require(type(unix_ms) is int and unix_ms >= schedule['day_anchor_ts'] * 1000,
             'time precedes anchor')
    return (unix_ms // 1000 - schedule['day_anchor_ts']) // 86400


def _target(schedule, eval_day):
    _require(type(eval_day) is int and schedule['effective_day'] <= eval_day < 2**32,
             'day precedes activation or is invalid')
    target = (schedule['day_anchor_ts'] + eval_day * 86400 + schedule['snapshot_offset_seconds']) * 1000
    _require(target + 86400000 < 2**53, 'date outside supported range')
    return target


class _Reader:
    def __init__(self, connection):
        self.connection, self.cache, self.calls = connection, {}, 0
        self.receipts, self.receipt_bytes = [], 0

    def rpc(self, method, params):
        self.calls += 1
        _require(self.calls <= 160, 'boundary read budget exceeded')
        response = self.connection.rpc_request(method, params)
        _require(isinstance(response, dict) and 'result' in response and 'error' not in response,
                 'boundary RPC failed')
        receipt = {'method': method, 'params': params, 'result': response['result']}
        size = len(json.dumps(receipt, separators=(',', ':'), allow_nan=False).encode())
        self.receipt_bytes += size
        _require(size <= 1024**2 and self.receipt_bytes <= 4 * 1024**2, 'boundary response budget exceeded')
        self.receipts.append(receipt)
        return response['result']

    def block(self, number):
        if number in self.cache:
            return self.cache[number]
        pin = self.rpc('chain_getBlockHash', [number])
        _require(type(pin) is str and re.fullmatch('0x[0-9a-f]{64}', pin), 'missing boundary hash')
        from .backing.keys import timestamp_now_key
        raw = self.rpc('state_getStorage', ['0x' + timestamp_now_key().hex(), pin])
        if number == 0 and raw is None:
            timestamp = 0
        else:
            _require(type(raw) is str and re.fullmatch('0x[0-9a-f]{16}', raw), 'invalid boundary timestamp')
            timestamp = int.from_bytes(bytes.fromhex(raw[2:]), 'little')
        row = {'block_number': number, 'block_hash': pin[2:], 'timestamp_ms': timestamp}
        self.cache[number] = row
        return row

    def boundary(self, schedule, target):
        _require(self.rpc('chain_getBlockHash', [0]) == '0x' + schedule['genesis_hash'], 'wrong genesis')
        head = self.rpc('chain_getFinalizedHead', [])
        header = self.rpc('chain_getHeader', [head])
        raw_number = header['number']
        number = int(raw_number, 16) if isinstance(raw_number, str) else raw_number
        _require(type(number) is int and 1 <= number < 2**32, 'invalid finalized height')
        final = self.block(number)
        _require(final['block_hash'] == head.removeprefix('0x'), 'finalized hash differs')
        _require(final['timestamp_ms'] >= target, 'snapshot slot not finalized yet')
        low, high = 1, number
        while low < high:
            middle = (low + high) // 2
            if self.block(middle)['timestamp_ms'] < target:
                low = middle + 1
            else:
                high = middle
        chosen, predecessor = self.block(low), self.block(low - 1)
        _require(predecessor['timestamp_ms'] < target <= chosen['timestamp_ms'],
                 'snapshot boundary is not the first block in the slot')
        chosen_header = self.rpc('chain_getHeader', ['0x' + chosen['block_hash']])
        _require(chosen_header['parentHash'] == '0x' + predecessor['block_hash'], 'boundary parent differs')
        return {'snapshot': chosen, 'predecessor': predecessor, 'finalized': final,
                'receipts': self.receipts}


def _resolve(schedule, clients, eval_day):
    identity = validate_schedule(schedule)
    target = _target(schedule, eval_day)
    proof = [_Reader(client).boundary(schedule, target) for client in clients]
    boundary = proof[0]['snapshot']
    _require(all(p['snapshot'] == boundary and p['predecessor'] == proof[0]['predecessor'] for p in proof),
             'readers disagree about snapshot boundary')
    opened = target + schedule['prepare_lead_seconds'] * 1000
    _require(boundary['timestamp_ms'] < opened, 'chain missed the preparation slot; windows are not retimed')
    contract = {
        'schema': 'noah.backing.round.v1',
        **{key: schedule[key] for key in ('genesis_hash', 'mining_netuid', 'reported_netuid',
            'eligibility_policy_version', 'calculation_version', 'scoring_release',
            'ark_coldkey', 'ark_validator_hotkey')},
        'round_date': datetime.fromtimestamp(target // 1000, timezone.utc).date().isoformat(),
        'block_number': boundary['block_number'], 'block_hash': boundary['block_hash'],
        'block_timestamp_ms': boundary['timestamp_ms'], 'commit_open_ms': opened,
        'commit_close_ms': opened + 7200000, 'reveal_open_ms': opened + 7200000,
        'reveal_close_ms': opened + 14400000, 'release_prefix': identity[:32],
    }
    if 'commitment_format' in schedule:
        contract['commitment_format'] = schedule['commitment_format']
    from .verification import validate_round
    validate_round(contract)
    policy = {'schema': 'noah.report-policy/1',
              **{k: schedule[k] for k in ('effective_day', 'genesis_hash', 'mining_netuid', 'eligibility_policy_version')},
              'required_rounds': [contract]}
    return {'round': contract, 'policy': policy, 'schedule_sha256': identity,
            'boundary_proof': {'schema': 'noah.report-schedule-boundary/1', 'target_ms': target,
                               'readers': proof}}


def resolve_day(schedule, endpoints, eval_day, *, require_pair=False):
    """Resolve a daily contract; no files, wallet access or publication.

    Public endpoint URLs are transport only and are never returned in evidence.
    The caller freezes the returned round using the existing immutable journal.
    """
    validate_schedule(schedule)
    _target(schedule, eval_day)
    _require(type(endpoints) is list and len(endpoints) == (2 if require_pair else 1),
             'expected two readers for validator or one reader for miner')
    _require(all(type(url) is str and url for url in endpoints) and len(set(endpoints)) == len(endpoints),
             'invalid or duplicate endpoints')
    from .budget import ProviderBudget
    budget = ProviderBudget(seconds=180, calls=320, response_bytes=1024**2,
                            total_bytes=8 * 1024**2)

    class Live:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def rpc_request(self, method, params):
            return budget.transport(self.endpoint,
                {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, 10)

    try:
        return _resolve(schedule, [Live(endpoint) for endpoint in endpoints], eval_day)
    finally:
        budget.close()


def verify_resolution(schedule, eval_day, resolution, *, require_pair=True):
    """Rebuild the boundary and round from bounded raw receipts without network.

    Receipt integrity does not authenticate an untrusted node. The deployment
    supplies the trusted independent readers and pins this complete resolution
    in R1; provider replay separately verifies the round at its finalized cutoff.
    """
    _require(type(resolution) is dict and set(resolution) ==
             {'round', 'policy', 'schedule_sha256', 'boundary_proof'}, 'resolution fields differ')
    proof = resolution['boundary_proof']
    _require(type(proof) is dict and set(proof) == {'schema', 'target_ms', 'readers'} and
             proof['schema'] == 'noah.report-schedule-boundary/1', 'boundary proof fields differ')
    readers = proof['readers']
    _require(type(readers) is list and len(readers) == (2 if require_pair else 1), 'boundary reader count')

    class Replay:
        def __init__(self, row):
            _require(type(row) is dict and set(row) == {'snapshot', 'predecessor', 'finalized', 'receipts'},
                     'reader proof fields differ')
            self.rows, self.index = row['receipts'], 0
            _require(type(self.rows) is list and 0 < len(self.rows) <= 160, 'raw receipt count')
            _require(len(json.dumps(self.rows, allow_nan=False).encode()) <= 5 * 1024**2, 'raw receipt byte limit')

        def rpc_request(self, method, params):
            _require(self.index < len(self.rows), 'raw boundary response missing')
            row = self.rows[self.index]
            self.index += 1
            _require(type(row) is dict and set(row) == {'method', 'params', 'result'} and
                     row['method'] == method and row['params'] == params, 'raw boundary request differs')
            return {'result': row['result']}

    clients = [Replay(row) for row in readers]
    rebuilt = _resolve(schedule, clients, eval_day)
    _require(all(client.index == len(client.rows) for client in clients), 'unconsumed raw boundary receipts')
    _require(rebuilt == resolution, 'saved schedule resolution differs from raw replay')
    return rebuilt
