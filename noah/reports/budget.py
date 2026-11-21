"""One read-only resource budget shared by an entire provider invocation."""
import json
import re
import time
from urllib.parse import urlsplit

from .common import ReportError
from .backing.constants import RPC_ALLOWLIST

READ_METHODS = RPC_ALLOWLIST | {'chain_getHead', 'state_getMetadata', 'rpc_methods',
    'system_chain', 'system_name', 'system_version', 'system_properties', 'state_getStorageAt'}


class ProviderBudget:
    def __init__(self, *, seconds=900, calls=200000, response_bytes=8*1024**2,
                 total_bytes=256*1024**2, retained_bytes=512*1024**2, clock=time.monotonic):
        self.clock, self.deadline = clock, clock() + seconds
        self.max_calls, self.response_limit = calls, response_bytes
        self.total_limit, self.retained_limit = total_bytes, retained_bytes
        self.calls = self.bytes = self.retained = 0
        self.connections = []
        self.transports = {}

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise ReportError('report invocation time budget exceeded')
        return remaining

    def request(self, payload):
        self.remaining()
        if (not isinstance(payload, dict) or payload.get('method') not in READ_METHODS
                or self.calls >= self.max_calls):
            raise ReportError('report read request or call budget refused')
        if payload.get('method') == 'state_getStorageAt':
            params = payload.get('params')
            if (type(params) is not list or len(params) != 2
                    or type(params[0]) is not str or re.fullmatch(r'0x(?:[0-9a-fA-F]{2})+', params[0]) is None
                    or type(params[1]) is not str or re.fullmatch(r'0x[0-9a-fA-F]{64}', params[1]) is None):
                raise ReportError('report storage alias requires an explicit immutable block pin')
        self.calls += 1

    def response(self, raw):
        self.remaining()
        size = len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
        if size > self.response_limit or self.bytes + size > self.total_limit:
            raise ReportError('report invocation response byte budget exceeded')
        self.bytes += size
        return raw

    def retain(self, size):
        self.remaining()
        if size > self.response_limit or self.retained + size > self.retained_limit:
            raise ReportError('report invocation retained byte budget exceeded')
        self.retained += size

    def socket(self, endpoint):
        value = BoundedSocket(endpoint, self)
        self.connections.append(value)
        return value

    def transport(self, endpoint, payload, timeout):
        socket = self.transports.get(endpoint)
        if socket is None:
            socket = self.transports[endpoint] = self.socket(endpoint)
        # send checks terminal failures before the deadline; each actual socket
        # operation still clamps this timeout to the remaining invocation time.
        socket.timeout = timeout
        socket.send(json.dumps(payload))
        raw = socket.recv()  # Checked before JSON decoding or receipt creation.
        return json.loads(raw)

    def close(self):
        for connection in self.connections:
            connection.close()


class BoundedSocket:
    """SDK-compatible send/recv shim; bounded before the SDK JSON decoder."""
    def __init__(self, endpoint, budget):
        self.endpoint, self.budget = endpoint, budget
        self.connection = None
        self.payload = None
        self.timeout = 30
        self._failure = None

    def check_failure(self):
        """Refuse cached or retried work after this transport's first failure."""
        if self._failure is not None:
            error, traceback = self._failure
            # Reuse the original traceback rather than accumulating retry frames.
            raise error.with_traceback(traceback)

    def _remember_failure(self, error):
        # An incomplete response cannot be consumed or retried on this transport.
        # SDK retries must retain its cause, not mask it as a pending request.
        if self._failure is None:
            self._failure = (error, error.__traceback__)
        self.payload = None

    def send(self, raw):
        self.check_failure()
        payload = json.loads(raw)
        self.budget.request(payload)
        if self.payload is not None:
            raise ReportError('report RPC overlapping request refused')
        self.payload = raw
        try:
            if urlsplit(self.endpoint).scheme in ('http', 'https'):
                return
            if urlsplit(self.endpoint).scheme not in ('ws', 'wss'):
                raise ReportError('report RPC scheme refused')
            if self.connection is None:
                from websockets.sync.client import connect
                self.connection = connect(self.endpoint,
                    open_timeout=min(self.timeout, self.budget.remaining()), close_timeout=1,
                    max_size=self.budget.response_limit, max_queue=1)
            self.connection.send(raw)
        except Exception as error:
            self._remember_failure(error)
            raise

    def recv(self):
        self.check_failure()
        if self.payload is None:
            raise ReportError('report RPC response without request')
        try:
            request = json.loads(self.payload)
            if urlsplit(self.endpoint).scheme in ('http', 'https'):
                from urllib.request import Request, urlopen
                req = Request(self.endpoint, data=self.payload.encode('utf-8'),
                              headers={'Content-Type': 'application/json'})
                with urlopen(req, timeout=min(self.timeout, self.budget.remaining())) as response:
                    raw = response.read(min(self.budget.response_limit,
                        self.budget.total_limit - self.budget.bytes) + 1)
            else:
                raw = self.connection.recv(timeout=min(self.timeout, self.budget.remaining()))
            raw = self.budget.response(raw)
            # Refuse unsolicited/subscription replies before the SDK queues them.
            decoded = json.loads(raw)
            if not isinstance(decoded, dict) or decoded.get('id') != request.get('id'):
                raise ReportError('report RPC response ID differs')
            self.payload = None
            return raw
        except Exception as error:
            self._remember_failure(error)
            raise

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
