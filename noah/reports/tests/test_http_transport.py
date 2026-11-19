"""The documented HTTP reader option uses a bounded request, without sockets."""
import io
import json
from noah.reports.backing.rpc import websocket_transport


def test_http_reader_sends_json_and_preserves_rpc_result(monkeypatch):
    calls = []
    def fake(request, timeout):
        calls.append((request.full_url, json.loads(request.data), timeout))
        return io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x1234"}')
    monkeypatch.setattr('urllib.request.urlopen', fake)
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'chain_getFinalizedHead', 'params': []}
    assert websocket_transport('http://reader.invalid:9944', payload, 2)['result'] == '0x1234'
    assert calls == [('http://reader.invalid:9944', payload, 2)]
