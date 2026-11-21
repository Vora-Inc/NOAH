"""Whole-invocation and pre-decoder caps; no network connections in these tests."""
import io
import json
from types import SimpleNamespace

import pytest

from noah.reports.budget import ProviderBudget
from noah.reports.common import ReportError
from noah.reports.backing.collector import _attach_recorder


def payload(number=1):
    return {'jsonrpc': '2.0', 'id': number, 'method': 'chain_getHeader', 'params': []}


def http(monkeypatch, raw=b'{"jsonrpc":"2.0","id":1,"result":{}}'):
    reads = []
    class Response(io.BytesIO):
        def read(self, count=-1):
            reads.append(count)
            return super().read(count)
    monkeypatch.setattr('urllib.request.urlopen', lambda *_args, **_kwargs: Response(raw))
    return reads


def test_shared_request_limit_cannot_reset_for_new_reader_or_seat(monkeypatch):
    http(monkeypatch)
    budget = ProviderBudget(calls=1)
    assert budget.transport('http://reader-a.invalid', payload(), 1)['result'] == {}
    with pytest.raises(ReportError, match='call budget'):
        budget.transport('http://reader-b.invalid', payload(), 1)
    assert budget.calls == 1


def test_oversized_response_refuses_before_json_decoder(monkeypatch):
    reads = http(monkeypatch, b'not-json' * 100)
    budget = ProviderBudget(response_bytes=16)
    with pytest.raises(ReportError, match='byte budget'):
        budget.transport('http://reader.invalid', payload(), 1)
    assert reads == [17]


def test_total_bytes_span_all_connections(monkeypatch):
    raw = b'{"id":1,"result":null}'
    http(monkeypatch, raw)
    budget = ProviderBudget(total_bytes=len(raw))
    budget.transport('http://a.invalid', payload(), 1)
    with pytest.raises(ReportError, match='byte budget'):
        budget.transport('http://b.invalid', payload(), 1)
    assert budget.bytes == len(raw)


def test_deadline_does_not_reset_when_creating_another_session():
    now = [0]
    budget = ProviderBudget(seconds=5, clock=lambda: now[0])
    budget.request(payload())
    now[0] = 6
    with pytest.raises(ReportError, match='time budget'):
        budget.socket('http://reader.invalid').send(json.dumps(payload()))


def test_signing_and_subscription_methods_are_refused_before_transport():
    budget = ProviderBudget()
    for method in ('author_submitExtrinsic', 'author_submitAndWatchExtrinsic', 'state_subscribeStorage'):
        with pytest.raises(ReportError, match='request'):
            budget.request({**payload(), 'method': method})


def test_recorder_checks_shared_retention_limit_before_append():
    substrate = SimpleNamespace(rpc_request=lambda *_: {'result': 'abcd'})
    budget = ProviderBudget(retained_bytes=8)
    log = _attach_recorder(substrate, budget)
    substrate.rpc_request('chain_getHeader', [])
    with pytest.raises(ReportError, match='retained'):
        substrate.rpc_request('chain_getHeader', [])
    assert len(log) == 1


def test_unsolicited_response_cannot_fill_sdk_queue(monkeypatch):
    http(monkeypatch, b'{"id":999,"result":null}')
    budget = ProviderBudget()
    with pytest.raises(ReportError, match='ID differs'):
        budget.transport('http://reader.invalid', payload(), 1)


@pytest.mark.parametrize('failing_close', ['socket', 'sdk'])
def test_collector_closes_owned_budget_after_other_cleanup_failure(monkeypatch, tmp_path, failing_close):
    from noah.reports.backing import collector
    from noah.reports.backing.protocol.preimage import REPORT_POLICY
    import noah.reports.budget as budgets
    closed = []
    def close(name):
        closed.append(name)
        if name == failing_close:
            raise RuntimeError('synthetic close failure')
    monkeypatch.setattr(collector, 'require_round', lambda _: None)
    monkeypatch.setattr(budgets, 'ProviderBudget', lambda: SimpleNamespace(close=lambda: close('budget')))
    monkeypatch.setattr(collector, 'close_sockets', lambda: close('socket'))
    monkeypatch.setattr(collector, '_connect', lambda *_args, **_kwargs: (SimpleNamespace(close=lambda: close('sdk')), 'reader_a'))
    def refuse(*_args, **_kwargs):
        raise ReportError('original collection failure')
    monkeypatch.setattr(collector, '_collect', refuse)
    with pytest.raises(ReportError, match='original collection failure'):
        collector.collect({'eligibility_policy_version': REPORT_POLICY}, ['ws://synthetic.invalid'], tmp_path, [])
    assert closed == ['socket', 'sdk', 'budget']


def test_installed_sdk_storage_alias_uses_same_bounded_transport(monkeypatch):
    from substrateinterface import SubstrateInterface
    from noah.reports.backing.offline import _ReplaySocket, _index
    from noah.reports.backing.rpc import result_sha256
    budget = ProviderBudget(calls=1)
    reads = http(monkeypatch, b'{"id":1,"result":"0x0000"}')
    sdk = SubstrateInterface.__new__(SubstrateInterface)
    sdk.config = {'rpc_methods': ['state_getStorageAt']}
    observed = []
    def rpc(method, params):
        observed.append((method, params))
        return budget.transport('http://synthetic.invalid', {'id': 1, 'method': method, 'params': params}, 1)
    sdk.rpc_request = rpc
    pin, storage = '0x'+'ab'*32, '0x'+'cd'*32
    assert sdk.get_storage_by_key(pin, storage) == '0x0000'
    assert observed == [('state_getStorageAt', [storage, pin])]
    assert budget.calls == 1 and reads == [budget.response_limit + 1]
    with pytest.raises(ReportError, match='call budget'):
        sdk.get_storage_by_key(pin, storage)
    replay = _ReplaySocket(_index([{'method': 'state_getStorageAt', 'params': [storage, pin],
                                    'result': '0x0000', 'sha256': result_sha256('0x0000')}]))
    def retained(method, params):
        replay.send(json.dumps({'id': 2, 'method': method, 'params': params}))
        return json.loads(replay.recv())
    sdk.rpc_request = retained
    assert sdk.get_storage_by_key(pin, storage) == '0x0000'


@pytest.mark.parametrize('params', [[], ['0x00'], ['0x00', None], ['0x00', 'latest'],
    ['odd', '0x'+'ab'*32], ['0x0', '0x'+'ab'*32], ['0x00', '0x'+'ab'*32, 4]])
def test_storage_alias_cannot_bypass_pin_or_shape_requirement(params):
    budget = ProviderBudget()
    with pytest.raises(ReportError, match='immutable block pin'):
        budget.request({'method': 'state_getStorageAt', 'params': params})
    assert budget.calls == 0


def test_full_day_call_capacity_remains_shared_and_finite():
    budget = ProviderBudget()
    for _ in range(200000):
        budget.request(payload())
    assert budget.calls == 200000
    with pytest.raises(ReportError, match='call budget'):
        budget.request(payload())
    assert budget.calls == 200000


def test_response_budget_failure_survives_sdk_retries_and_later_deadline(monkeypatch):
    raw = b'{"id":1,"result":null}'
    reads = http(monkeypatch, raw)
    now = [0]
    budget = ProviderBudget(total_bytes=2*len(raw)-1, clock=lambda: now[0])
    endpoint = 'http://reader.invalid'
    budget.transport(endpoint, payload(), 1)
    with pytest.raises(ReportError, match='response byte budget') as original:
        budget.transport(endpoint, payload(), 1)
    counters = (budget.calls, budget.bytes, budget.retained)
    socket = budget.transports[endpoint]
    now[0] = 1000  # Even a later deadline must not replace the original refusal.
    for retry in (socket.check_failure, lambda: socket.send(json.dumps(payload(2))), socket.recv,
                  lambda: budget.transport(endpoint, payload(2), 1)):
        with pytest.raises(ReportError) as repeated:
            retry()
        assert repeated.value is original.value
        assert (budget.calls, budget.bytes, budget.retained) == counters
    assert reads == [2*len(raw), len(raw)]


@pytest.mark.parametrize('raw,error_type', [
    (b'not-json', json.JSONDecodeError),
    (b'{"id":999,"result":null}', ReportError),
])
def test_invalid_response_is_terminal_without_another_http_request(monkeypatch, raw, error_type):
    reads = http(monkeypatch, raw)
    budget = ProviderBudget()
    socket = budget.socket('http://reader.invalid')
    socket.send(json.dumps(payload()))
    with pytest.raises(error_type) as original:
        socket.recv()
    socket.close()
    for retry in (lambda: socket.send(json.dumps(payload(2))), socket.recv):
        with pytest.raises(error_type) as repeated:
            retry()
        assert repeated.value is original.value
    assert budget.calls == 1 and budget.bytes == len(raw)
    assert reads == [budget.response_limit + 1]


@pytest.mark.parametrize('phase', ['send', 'recv'])
def test_failed_websocket_transport_preserves_first_error_after_close(phase):
    budget = ProviderBudget()
    socket = budget.socket('ws://reader.invalid')
    events = []
    failure = TimeoutError('synthetic transport failure')
    def io_step(name):
        events.append(name)
        if phase == name:
            raise failure
    socket.connection = SimpleNamespace(
        send=lambda _: io_step('send'), recv=lambda **_: io_step('recv'),
        close=lambda: events.append('close'))
    with pytest.raises(TimeoutError) as original:
        socket.send(json.dumps(payload()))
        socket.recv()
    assert original.value is failure
    socket.close()
    previous = list(events)
    for retry in (lambda: socket.send(json.dumps(payload(2))), socket.recv):
        with pytest.raises(TimeoutError) as repeated:
            retry()
        assert repeated.value is failure
    assert events == previous and budget.calls == 1 and budget.bytes == 0


def test_successful_socket_reuse_keeps_shared_limits(monkeypatch):
    raw = b'{"id":1,"result":null}'
    reads = http(monkeypatch, raw)
    budget = ProviderBudget(calls=2)
    assert budget.socket('http://health-check.invalid').check_failure() is None
    assert budget.calls == budget.bytes == 0
    for _ in range(2):
        assert budget.transport('http://reader.invalid', payload(), 1)['result'] is None
    with pytest.raises(ReportError, match='call budget'):
        budget.transport('http://reader.invalid', payload(), 1)
    assert budget.calls == 2 and budget.bytes == 2*len(raw)
    assert reads == [budget.response_limit + 1]*2
