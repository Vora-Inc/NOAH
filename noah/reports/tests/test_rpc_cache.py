import copy
from types import SimpleNamespace

import pytest

from noah.reports.backing.collector import _attach_recorder, _body_has_commitment, _decode_block
from noah.reports.backing.rpc_cache import ImmutableRpcCache, immutable_key
from noah.reports.budget import ProviderBudget
from noah.reports.common import ReportError

PIN = '0x' + 'a' * 64


def test_cache_only_exact_explicit_pins_and_non_subscriptions():
    assert immutable_key('chain_getBlock', [PIN])
    assert immutable_key('state_getStorage', ['0x01', PIN])
    for method, params in [('chain_getBlock', []), ('chain_getBlockHash', [42]),
                           ('state_getStorage', ['0x01']), ('state_getKeysPaged', ['0x01', 10, PIN]),
                           ('state_call', ['api', PIN]), ('author_submitExtrinsic', [PIN])]:
        assert immutable_key(method, params) is None
    assert immutable_key('chain_getBlock', [PIN], lambda x: x) is None


def test_fresh_results_bounds_and_lru_eviction():
    c = ImmutableRpcCache(max_bytes=100, max_entries=2)
    c.put('a', {'result': [1]});c.put('b', {'result': [2]})
    found = c.get('a');found['result'].append(99)
    assert c.get('a') == {'result': [1]}
    c.put('c', {'result': [3]})
    assert c.get('b') is None and c.bytes <= 100
    c.put('huge', {'result': 'x' * 101})
    assert c.get('huge') is None and c.bytes <= 100
    c.put('bad', {'result': 1, 'error': 'refused'})
    assert c.get('bad') is None
    c.clear();assert c.bytes == 0 and not c.rows


def recorder(value=1, *, enabled=True):
    calls = []
    def rpc(method, params, handler):
        calls.append((method, params))
        return {'result': {'value': value}, 'id': len(calls)}
    substrate = SimpleNamespace(rpc_request=rpc, websocket=SimpleNamespace(check_failure=lambda:None))
    budget = ProviderBudget()
    rows = _attach_recorder(substrate, budget, immutable_cache=enabled)
    return substrate, budget, rows, calls


def test_per_reader_cache_retains_original_input_once_and_survives_log_rotation():
    a, budget, rows, calls = recorder()
    response = a.rpc_request('chain_getBlock', [PIN]);response['result']['value'] = 99
    assert len(rows) == 1 and rows[0]['result'] == {'value': 1}
    retained = budget.retained
    saved = copy.deepcopy(rows);rows.clear()  # Already durably written shared transcript.
    assert a.rpc_request('chain_getBlock', [PIN])['result'] == saved[0]['result']
    assert not rows and len(calls) == 1 and budget.retained == retained
    b, _, other, _ = recorder(value=2)
    assert b.rpc_request('chain_getBlock', [PIN])['result'] != saved[0]['result']
    assert other[0]['result'] == {'value': 2}


def test_unpinned_and_legacy_reads_still_call_provider_and_cache_hits_check_deadline():
    a, budget, rows, calls = recorder()
    a.rpc_request('chain_getBlock', [PIN]);a.rpc_request('chain_getHead', []);a.rpc_request('chain_getHead', [])
    assert len(calls) == 3
    budget.deadline = budget.clock() - 1
    with pytest.raises(ReportError, match='time budget'):a.rpc_request('chain_getBlock', [PIN])
    b, _, rows, calls = recorder(enabled=False)
    b.rpc_request('chain_getBlock', [PIN]);b.rpc_request('chain_getBlock', [PIN])
    assert len(calls) == len(rows) == 2


def extrinsic(call):
    return SimpleNamespace(value={'call': call})


def test_lazy_events_requires_full_decoded_body_and_detects_nested_commitments():
    plain = {'extrinsics': [extrinsic({'call_module': 'Timestamp', 'call_function': 'set', 'call_args': []})]}
    relevant = {'call_module': 'Commitments', 'call_function': 'set_commitment', 'call_args': []}
    nested = {'extrinsics': [extrinsic({'call_module': 'Utility', 'call_function': 'batch',
                                       'call_args': [{'name': 'calls', 'value': [relevant]}]})]}
    assert not _body_has_commitment(plain)
    assert _body_has_commitment(nested)
    loop = {};loop['cycle'] = loop
    assert _body_has_commitment({'extrinsics': [extrinsic(loop)]})
    class Fake:
        def __init__(self, block):self.block=block;self.event_calls=0
        def rpc_request(self, *args):return {'result': {'block': 'retained raw'}}
        def get_block(self, **kwargs):return self.block
        def query(self, *args, **kwargs):self.event_calls += 1;return SimpleNamespace(value=[])
    absent = Fake(plain);assert _decode_block(absent, {'hash': PIN})[0] == []
    assert absent.event_calls == 0
    wrapped = Fake(nested);assert _decode_block(wrapped, {'hash': PIN})[0] == []
    assert wrapped.event_calls == 1  # Existing direct-only interpretation is preserved.


def test_cached_hit_does_not_hide_terminal_transport_failure():
    a, budget, _, calls = recorder()
    a.rpc_request('chain_getBlock', [PIN])
    original = ReportError('first bounded response failure')
    def check():raise original
    a.websocket.check_failure = check
    budget.deadline = budget.clock() - 1
    with pytest.raises(ReportError) as failure:a.rpc_request('chain_getBlock', [PIN])
    assert failure.value is original and len(calls) == 1


def test_unknown_transport_contract_never_uses_cache():
    a, _, rows, calls = recorder()
    a.websocket = object()
    a.rpc_request('chain_getBlock', [PIN]);a.rpc_request('chain_getBlock', [PIN])
    assert len(calls) == len(rows) == 2


@pytest.mark.parametrize('cached', [False, True])
def test_day_sized_window_and_127_target_queries_fit_only_with_input_reuse(cached):
    """Real budget/recorder over7200 pinned bodies; scaled2KiB transport fixture.

    Full SDK/engine semantics are covered separately by shared-window tests;
    actual Mainnet127-target qualification must still measure real payloads.
    """
    import json
    budget = ProviderBudget(total_bytes=20*1024**2)
    source_calls = []
    def rpc(method, params, result_handler):
        budget.request({'method': method, 'params': params})
        source_calls.append((method, tuple(params)))
        result = {'block': {'extrinsics': ['0x'+'ab'*1024], 'pin': params[-1]}}
        response = {'jsonrpc': '2.0', 'id': len(source_calls), 'result': result}
        budget.response(json.dumps(response))
        return response
    substrate = SimpleNamespace(rpc_request=rpc, websocket=SimpleNamespace(check_failure=lambda:None))
    rows = _attach_recorder(substrate, budget, immutable_cache=cached)
    pins = ['0x'+f'{i:064x}' for i in range(1,7201)]
    def collect():
        for pin in pins:substrate.rpc_request('chain_getBlock', [pin])
        for pin in pins:
            substrate.rpc_request('chain_getBlock', [pin])
            substrate.rpc_request('chain_getBlock', [pin])
        for target in range(127):
            substrate.rpc_request('state_getStorage', [f'0x{target:04x}', pins[0]])
    if not cached:
        with pytest.raises(ReportError, match='response byte budget'):collect()
        return
    collect()
    assert len(source_calls) == len(rows) == 7200+127
    assert budget.bytes < 20*1024**2 and budget.retained < budget.retained_limit
    assert {row['params'][0] for row in rows if row['method']=='chain_getBlock'} == set(pins)
