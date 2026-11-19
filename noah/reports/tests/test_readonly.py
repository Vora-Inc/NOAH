import json

from noah.reports.backing.constants import RPC_ALLOWLIST
from noah.reports.backing.engine import _finish
from noah.reports.backing.rpc import RpcClient, RunFailure
from noah.reports.backing.source import LiveSource


def test_allowlist_rejects_writes_and_state_calls_must_carry_the_block():
    block = "0x" + "ab" * 32
    seen = []

    def transport(url, payload, timeout):
        seen.append(payload)
        if payload["method"] not in RPC_ALLOWLIST:
            raise AssertionError(payload["method"])
        return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x01"}

    client = RpcClient(["wss://archive.chain.opentensor.ai:443"], block, transport, sleep=lambda _s: None)
    try:
        client.call("author_submitExtrinsic", ["0xdead"])
    except RunFailure as exc:
        assert exc.reason == "method_not_allowed"
    else:
        raise AssertionError("write method was allowed")
    try:
        client.call("state_getStorage", ["0xaa"])
    except RunFailure as exc:
        assert exc.reason == "block_not_canonical"
    else:
        raise AssertionError("unpinned state read was allowed")
    client.call("state_getStorage", ["0xAA", block])
    assert seen[-1]["params"][-1] == block
    assert seen[-1]["method"] == "state_getStorage"


def test_rate_limit_retries_then_fails_without_changing_the_block():
    block = "0x" + "cd" * 32
    sleeps = []

    def transport(url, payload, timeout):
        return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32004, "message": "Work limit exhausted"}}

    client = RpcClient([ "wss://archive.chain.opentensor.ai:443"], block, transport, sleep=sleeps.append)
    try:
        client.call("state_call", ["SwapRuntimeApi_current_alpha_price", "0x5200", block])
    except RunFailure as exc:
        assert exc.reason == "rpc_rate_limit"
    else:
        raise AssertionError("rate limit became a result")
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert client.retries["rate_limit"] == 6


def test_unknown_block_is_not_retried():
    block = "0x" + "ef" * 32
    calls = []

    def transport(url, payload, timeout):
        calls.append(payload)
        return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32000, "message": "State already discarded"}}

    client = RpcClient(["wss://archive.chain.opentensor.ai:443"], block, transport, sleep=lambda _s: (_ for _ in ()).throw(AssertionError("retried")))
    try:
        client.call("state_getRuntimeVersion", [block])
    except RunFailure as exc:
        assert exc.reason == "block_state_unavailable"
    else:
        raise AssertionError("discarded state was retried into a result")
    assert len(calls) == 1


def test_endpoint_credentials_are_not_recorded():
    secret_url = 'wss://' + 'dummy-user' + ':' + 'dummy-secret' + '@archive.example:443/private-path?' + 'token' + '=dummy-token#frag'
    block = "0x" + "ab" * 32
    seen = []

    def transport(url, payload, timeout):
        seen.append(url)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": "0x01"}

    client = RpcClient([secret_url], block, transport, sleep=lambda _seconds: None)
    client.call("state_getStorage", ["0xaa", block])
    assert seen == [secret_url]
    receipt = client.receipts[0].to_json()
    meta = {"endpoints": [receipt["endpoint"]], "endpoint": receipt["endpoint"]}
    blob = json.dumps(receipt) + json.dumps(meta)
    for needle in ("dummy-user", "dummy-secret", "dummy-token", "private-path"):
        assert needle not in blob
    assert receipt["endpoint"] == "wss://archive.example:443"
    # The same safe label is what compute writes into run metadata.
    source = LiveSource(client)
    finished = _finish({}, type("Timer", (), {"phases": {}, "total_ms": lambda self: 0})(), type("Reader", (), {"page_count": 0, "position_count": 0})(), source)
    saved = json.dumps(finished)
    for needle in ("dummy-user", "dummy-secret", "dummy-token", "private-path"):
        assert needle not in saved
    assert finished["endpoints"] == ["wss://archive.example:443"]


def test_receipt_keeps_the_full_page_body():
    block = "0x" + "11" * 32
    page = ["0xaa", "0xbb"]

    def transport(url, payload, timeout):
        return {"jsonrpc": "2.0", "id": payload["id"], "result": page}

    client = RpcClient(["wss://archive.chain.opentensor.ai:443"], block, transport, sleep=lambda _s: None)
    result = client.call("state_getKeysPaged", ["0x01", 1000, None, block])
    assert result == page
    receipt = client.receipts[0]
    assert receipt.result == page
    assert receipt.byte_length == len(json.dumps(page, separators=(",", ":")))
    assert receipt.sha256
