"""Live and replay chain sources.

Replay answers the original calls, including every ``state_getKeysPaged`` page,
from retained bodies. A digest or a derived coldkey list is not enough. A missing,
truncated, or hash-mismatched page fails closed.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from .constants import RPC_ALLOWLIST, STATE_METHODS
from .rpc import Receipt, RpcClient, canonical_result, result_sha256


class ReplayError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def load_receipts(path: str) -> list[dict[str, Any]]:
    opener = gzip.open if path.endswith(".gz") else open
    rows: list[dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def save_receipts(path: str, receipts: list[Receipt]) -> None:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as handle:
        for receipt in receipts:
            handle.write(json.dumps(receipt.to_json(), sort_keys=True, separators=(",", ":")) + "\n")


class LiveSource:
    def __init__(self, client: RpcClient):
        self.client = client

    def call(self, method: str, params: list[Any]) -> Any:
        return self.client.call(method, params)


class ReplaySource:
    def __init__(self, receipts: list[dict[str, Any]], block_hash: str):
        self.block_hash = block_hash.lower()
        self.index: dict[str, dict[str, Any]] = {}
        for row in receipts:
            self._validate_row(row)
            key = self._key(row["method"], row["params"])
            prior = self.index.get(key)
            if prior is not None and prior["sha256"] != row["sha256"]:
                raise ReplayError("conflicting_response")
            self.index[key] = row

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in RPC_ALLOWLIST:
            raise ReplayError("method_not_allowed")
        if method in STATE_METHODS and (not params or params[-1] != self.block_hash):
            raise ReplayError("block_not_canonical")
        row = self.index.get(self._key(method, params))
        if row is None:
            raise ReplayError("missing_page" if method == "state_getKeysPaged" else "missing_request")
        self._validate_row(row)
        return row["result"]

    @staticmethod
    def _key(method: str, params: list[Any]) -> str:
        return method + "\n" + canonical_result(params)

    def _validate_row(self, row: dict[str, Any]) -> None:
        """Every replay receipt must carry sha256 and byte_length, and they must match the body."""
        if row.get("method") == "state_getKeysPaged" and not isinstance(row.get("result"), list):
            raise ReplayError("missing_page")
        digest = row.get("sha256")
        size = row.get("byte_length")
        if "result" not in row or not isinstance(digest, str) or not isinstance(size, int) or isinstance(size, bool):
            raise ReplayError("missing_integrity")
        actual, actual_size = result_sha256(row["result"])
        if actual != digest:
            raise ReplayError("hash_mismatch")
        if actual_size != size:
            raise ReplayError("truncated_page")
