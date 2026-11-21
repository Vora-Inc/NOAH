"""Bounded raw-input reuse for one reader's immutable shared-window queries.

This never shares inputs across providers or caches a verdict. Every admitted
response is first retained in that reader's package transcript. Cache hits return
fresh objects because SDK decoders mutate response objects in place.
"""
from collections import OrderedDict
import json
import re

PIN = re.compile(r"0x[0-9a-fA-F]{64}")
FIRST_PIN = frozenset({"chain_getHeader", "chain_getBlock", "state_getRuntimeVersion", "state_getMetadata"})
LAST_PIN_ARITY = {"state_getStorage": 2, "state_getStorageAt": 2, "state_getStorageHash": 2,
                  "state_queryStorageAt": 2, "state_call": 3, "state_getKeysPaged": 4}


def immutable_key(method, params, result_handler=None):
    if result_handler is not None or type(params) is not list:
        return None
    if method in FIRST_PIN and len(params) == 1:
        pin = params[0]
    elif method in LAST_PIN_ARITY and len(params) == LAST_PIN_ARITY[method]:
        pin = params[-1]
    else:
        return None
    if type(pin) is not str or PIN.fullmatch(pin) is None:
        return None
    return json.dumps([method, params], sort_keys=True, separators=(",", ":"), allow_nan=False)


class ImmutableRpcCache:
    def __init__(self, *, max_bytes, max_entries):
        if type(max_bytes) is not int or max_bytes <= 0 or type(max_entries) is not int or max_entries <= 0:
            raise ValueError("invalid immutable RPC cache bound")
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.rows = OrderedDict()
        self.bytes = 0

    def get(self, key):
        if key is None or key not in self.rows:
            return None
        raw = self.rows[key]
        self.rows.move_to_end(key)
        return json.loads(raw)

    def put(self, key, response):
        if key is None or not isinstance(response, dict) or "error" in response or "result" not in response:
            return
        raw = json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        size = len(key.encode()) + len(raw)
        if size > self.max_bytes:
            return
        if key in self.rows:
            previous = self.rows.pop(key)
            self.bytes -= len(key.encode()) + len(previous)
        while self.rows and (self.bytes + size > self.max_bytes or len(self.rows) >= self.max_entries):
            old_key, old_raw = self.rows.popitem(last=False)
            self.bytes -= len(old_key.encode()) + len(old_raw)
        self.rows[key] = raw
        self.bytes += size

    def clear(self):
        self.rows.clear()
        self.bytes = 0
