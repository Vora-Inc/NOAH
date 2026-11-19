"""Instance-local time bounds for live report SDK clients; no retry or signer thread.

Each SDK RPC, including a subscription, has one monotonic socket-I/O deadline.
Header/body/frame fragments and unrelated messages share it. This is not a total
CLI/tick budget. DNS, underlying connect setup and CPU parsing are not forcibly
interrupted; expiry is checked when control returns to the guard.
Replay and provider-budget clients retain their separate transports.
"""
from contextlib import contextmanager
import math
import socket
import time

from ..common import ReportError
from .constants import RPC_TIMEOUT_S


class ReportRpcTimeout(ReportError):
    """A request deadline expired; a submission outcome may be unknown."""


class _Deadline:
    def __init__(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('report SDK timeout must be positive finite seconds')
        self.seconds, self.end = seconds, None

    def remaining(self):
        remaining = self.seconds if self.end is None else self.end - time.monotonic()
        if remaining <= 0:
            raise ReportRpcTimeout('report SDK request deadline exceeded')
        return remaining

    @contextmanager
    def scope(self):
        previous = self.end
        self.end = min(previous, time.monotonic()+self.seconds) if previous is not None else time.monotonic()+self.seconds
        try:
            try:
                yield
            except Exception:
                self.remaining()  # Convert wrapped transport errors after expiry.
                raise
            self.remaining()
        finally:
            self.end = previous


class _DeadlineSocket:
    """Keep a deadline across low-level partial reads, including HTTP makefile IO."""
    def __init__(self, raw, deadline):
        self._raw, self._deadline = raw, deadline
        self._timeout = raw.gettimeout()
        self._io_refs, self._closed = 0, False

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def _prepare(self):
        remaining = self._deadline.remaining()
        self._raw.settimeout(remaining if self._timeout is None else min(remaining, self._timeout))

    def settimeout(self, value):
        self._timeout = value
        self._raw.settimeout(value)

    def gettimeout(self):
        remaining = self._deadline.remaining()
        return remaining if self._timeout is None else min(remaining, self._timeout)

    def recv(self, *args, **kwargs):
        self._prepare()
        return self._raw.recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._prepare()
        return self._raw.recv_into(*args, **kwargs)

    def send(self, *args, **kwargs):
        self._prepare()
        return self._raw.send(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._prepare()
        return self._raw.sendall(*args, **kwargs)

    def makefile(self, *args, **kwargs):
        # SocketIO must call this proxy's recv_into, not bypass it via raw.makefile.
        return socket.socket.makefile(self, *args, **kwargs)

    def close(self):
        self._closed = True
        if not self._io_refs:
            self._raw.close()

    def _decref_socketios(self):
        self._io_refs -= 1
        if self._closed and not self._io_refs:
            self._raw.close()


def _http_session(deadline):
    import requests
    from requests.adapters import HTTPAdapter

    def pools(manager):
        # Copy instance dictionaries; never alter urllib3's global pool classes.
        result = {}
        for scheme, pool in manager.pool_classes_by_scheme.items():
            parent = pool.ConnectionCls
            class Connection(parent):
                def connect(self):
                    super().connect()
                    self.sock = _DeadlineSocket(self.sock, deadline)
            result[scheme] = type('ReportDeadlinePool', (pool,), {'ConnectionCls': Connection})
        manager.pool_classes_by_scheme = result

    class Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            pools(self.poolmanager)

        def proxy_manager_for(self, proxy, **kwargs):
            manager = super().proxy_manager_for(proxy, **kwargs)
            if not getattr(manager, '_report_deadline_installed', False):
                pools(manager)
                manager._report_deadline_installed = True
            return manager

    class Session(requests.Session):
        def request(self, *args, **kwargs):
            kwargs['timeout'] = deadline.remaining()
            kwargs['allow_redirects'] = False  # Never replay an RPC POST via 307/308.
            return super().request(*args, **kwargs)

    session = Session()
    for adapter in session.adapters.values():
        adapter.close()
    session.mount('http://', Adapter(max_retries=0))
    session.mount('https://', Adapter(max_retries=0))
    return session


def live_sdk(url, *, auto_discover=True, timeout_seconds=RPC_TIMEOUT_S):
    """Construct a bounded live SDK; close all owned resources on init failure."""
    import requests
    from substrateinterface import SubstrateInterface
    from websocket import WebSocket, WebSocketTimeoutException

    deadline = _Deadline(timeout_seconds)
    sockets = []

    class DeadlineWebSocket(WebSocket):
        def __init__(self, *args, **kwargs):
            self._report_socket = None
            super().__init__(*args, **kwargs)
            sockets.append(self)

        def recv(self, *args, **kwargs):
            deadline.remaining()
            value = super().recv(*args, **kwargs)
            deadline.remaining()
            return value

        def send(self, *args, **kwargs):
            deadline.remaining()
            return super().send(*args, **kwargs)

        @property
        def sock(self):
            return self._report_socket

        @sock.setter
        def sock(self, value):
            self._report_socket = _DeadlineSocket(value, deadline) if value is not None else None

    class SDK(SubstrateInterface):
        _report_closed = False

        def rpc_request(self, *args, **kwargs):
            if self._report_closed:
                raise ReportError('report SDK connection is closed; reconcile before retrying')
            # SDK creates its requests session before constructor auto-discovery.
            if not self.websocket and not getattr(self, '_report_http_installed', False):
                self.session.close()
                self.session = _http_session(deadline)
                self._report_http_installed = True
            try:
                with deadline.scope():
                    return super().rpc_request(*args, **kwargs)
            except (ReportRpcTimeout, WebSocketTimeoutException, requests.Timeout):
                self.close()
                raise ReportRpcTimeout('report SDK request deadline exceeded') from None

        def close(self):
            self._report_closed = True
            for ws in sockets:
                try:
                    ws.shutdown()  # No close-handshake wait after a deadline.
                except Exception:
                    pass
            session = getattr(self, 'session', None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            extensions = getattr(self, 'extensions', None)
            if extensions is not None:
                try:
                    extensions.unregister_all()
                except Exception:
                    pass

    client = SDK.__new__(SDK)
    try:
        with deadline.scope():
            client.__init__(url=url, auto_discover=auto_discover, auto_reconnect=False,
                            ws_options={'timeout': timeout_seconds, 'class_': DeadlineWebSocket})
        return client
    except BaseException as error:
        client.close()
        if isinstance(error, (TimeoutError, WebSocketTimeoutException, requests.Timeout)):
            raise ReportRpcTimeout('report SDK connection deadline exceeded') from None
        raise
