"""JSON-RPC over a websocket, with the slice-1 allowlist and retry rules.

Transport errors, timeouts, and rate/work-limit errors are retried on the same
block hash. Unknown-block and state-discarded errors are not retried and are
not sale-validity signals. Decode errors are not retried either.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from .constants import RPC_ALLOWLIST, RPC_RETRY_BACKOFF_S, RPC_TIMEOUT_S, RUN_BUDGET_S, STATE_METHODS


class RunFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Receipt:
    method: str
    params: list[Any]
    result: Any
    attempt_count: int
    ms: int
    sha256: str
    byte_length: int
    endpoint: str

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_count": self.attempt_count,
            "byte_length": self.byte_length,
            "endpoint": self.endpoint,
            "method": self.method,
            "ms": self.ms,
            "params": self.params,
            "result": self.result,
            "sha256": self.sha256,
        }


def endpoint_label(url: str) -> str:
    """Scheme, host, and port only. Userinfo, path, query, and fragment are never recorded."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if not parsed.scheme or not host:
        return "redacted-endpoint"
    return f"{parsed.scheme}://{host}"


def canonical_result(result: Any) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def result_sha256(result: Any) -> tuple[str, int]:
    import hashlib

    body = canonical_result(result).encode()
    return hashlib.sha256(body).hexdigest(), len(body)


def _lower_hex(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("0x", "0X")) and all(
        char in "0123456789abcdefABCDEF" for char in value[2:]
    ):
        return "0x" + value[2:].lower()
    if isinstance(value, list):
        return [_lower_hex(item) for item in value]
    if isinstance(value, dict):
        return {key: _lower_hex(item) for key, item in value.items()}
    return value


class RpcClient:
    def __init__(
        self,
        urls: list[str],
        block_hash: str,
        transport: Callable[[str, dict, float], Any],
        *,
        timeout_s: float = RPC_TIMEOUT_S,
        run_budget_s: float = RUN_BUDGET_S,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if not urls:
            raise RunFailure("rpc_transport")
        self.urls = urls
        self.block_hash = block_hash.lower()
        self.transport = transport
        self.timeout_s = timeout_s
        self.sleep = sleep
        self.clock = clock
        self.deadline = clock() + run_budget_s
        self.receipts: list[Receipt] = []
        self.retries = {"transport": 0, "rate_limit": 0}
        self._next_id = 1
        self.connect_ms = 0

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in RPC_ALLOWLIST:
            raise RunFailure("method_not_allowed")
        params = _lower_hex(params)
        self._require_pinned_block(method, params)
        self._check_budget()
        last_reason = "rpc_transport"
        for url in self.urls:
            outcome = self._call_one(url, method, params)
            if isinstance(outcome, tuple):
                return outcome[0]
            last_reason = outcome
            if outcome in {"block_state_unavailable", "decode_error", "run_budget_exceeded"}:
                raise RunFailure(outcome)
        raise RunFailure(last_reason)

    def _require_pinned_block(self, method: str, params: list[Any]) -> None:
        if method not in STATE_METHODS:
            return
        if not params or params[-1] != self.block_hash:
            raise RunFailure("block_not_canonical")

    def _check_budget(self) -> None:
        if self.clock() > self.deadline:
            raise RunFailure("run_budget_exceeded")

    def _call_one(self, url: str, method: str, params: list[Any]) -> Any:
        attempts = 0
        for attempt in range(1 + len(RPC_RETRY_BACKOFF_S)):
            self._check_budget()
            attempts = attempt + 1
            started = self.clock()
            payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
            self._next_id += 1
            try:
                message = self.transport(url, payload, self.timeout_s)
            except RunFailure:
                raise
            except Exception as exc:  # transport, timeout, socket reset
                kind = _classify_exception(exc)
                if kind == "block":
                    return "block_state_unavailable"
                self.retries["transport"] += 1
                if attempt == len(RPC_RETRY_BACKOFF_S):
                    return "rpc_transport"
                self.sleep(RPC_RETRY_BACKOFF_S[attempt])
                continue
            elapsed_ms = int((self.clock() - started) * 1000)
            if self.connect_ms == 0:
                self.connect_ms = elapsed_ms
            problem = _classify_message(message)
            if problem == "rate_limit":
                self.retries["rate_limit"] += 1
                if attempt == len(RPC_RETRY_BACKOFF_S):
                    return "rpc_rate_limit"
                self.sleep(RPC_RETRY_BACKOFF_S[attempt])
                continue
            if problem == "block":
                return "block_state_unavailable"
            if problem == "decode":
                return "decode_error"
            if problem is not None:
                self.retries["transport"] += 1
                if attempt == len(RPC_RETRY_BACKOFF_S):
                    return "rpc_transport"
                self.sleep(RPC_RETRY_BACKOFF_S[attempt])
                continue
            result = _lower_hex(message.get("result"))
            digest, size = result_sha256(result)
            self.receipts.append(
                Receipt(
                    method=method,
                    params=params,
                    result=result,
                    attempt_count=attempts,
                    ms=elapsed_ms,
                    sha256=digest,
                    byte_length=size,
                    endpoint=endpoint_label(url),
                )
            )
            return result, attempts
        return "rpc_transport"


def _error_blob(message: Any) -> tuple[int | None, str]:
    if not isinstance(message, dict):
        return None, ""
    error = message.get("error")
    if not isinstance(error, dict):
        return None, ""
    code = error.get("code")
    text = str(error.get("message", ""))
    return code if isinstance(code, int) else None, text


def _classify_message(message: Any) -> str | None:
    if not isinstance(message, dict) or "result" not in message and "error" not in message:
        return "decode"
    code, text = _error_blob(message)
    if code is None and "error" not in message:
        return None
    lowered = text.lower()
    if code == -32004 or "rate" in lowered or "work limit" in lowered or "exhausted" in lowered:
        return "rate_limit"
    if "unknown block" in lowered or "state discarded" in lowered or "state already discarded" in lowered:
        return "block"
    if "error" in message:
        return "transport"
    return None


def _classify_exception(exc: Exception) -> str:
    text = str(exc).lower()
    if "unknown block" in text or "state discarded" in text or "state already discarded" in text:
        return "block"
    return "transport"


def websocket_transport(url: str, payload: dict, timeout: float) -> Any:
    """Bounded HTTP request or one reusable websocket connection per URL."""
    if urlparse(url).scheme in ('http', 'https'):
        from urllib.request import Request, urlopen
        request = Request(url, data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            raise RunFailure('response_size_limit')
        return json.loads(raw)
    socket = _sockets.get(url)
    if socket is None:
        from websockets.sync.client import connect

        socket = connect(url, open_timeout=timeout, close_timeout=5, max_size=64 * 1024 * 1024)
        _sockets[url] = socket
    try:
        socket.send(json.dumps(payload))
        raw = socket.recv(timeout=timeout)
    except Exception:
        _sockets.pop(url, None)
        try:
            socket.close()
        except Exception:
            pass
        raise
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


_sockets: dict[str, Any] = {}


def close_sockets() -> None:
    for socket in list(_sockets.values()):
        try:
            socket.close()
        except Exception:
            pass
    _sockets.clear()
