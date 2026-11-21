"""Pinned reads. Every state call carries the supplied block hash."""

from __future__ import annotations

import os
import sys
from typing import Any

from .constants import AVAILABILITY_BATCH, KEY_PAGE_SIZE
from .keys import (
    alpha_v2_prefix,
    coldkey_miner_collateral_key,
    fee_rate_key,
    hex_decode,
    hex_encode,
    pal_swap_initialized_key,
    parse_alpha_v2_key,
    subnet_alpha_in_key,
    subnet_alpha_out_key,
    subnet_mechanism_key,
    subnet_state_key,
    subnet_tao_key,
    subtoken_enabled_key,
    swap_balancer_key,
    timestamp_now_key,
    total_networks_key,
)
from .rpc import RunFailure
from .scale import (
    Availability,
    ScaleError,
    SimSwap,
    decode_availability_map,
    decode_bool,
    decode_lock_mass,
    decode_quote_parts,
    decode_sim,
    decode_u16,
    decode_u64,
    decode_u8_enum,
    encode_availability_args,
    encode_lock_args,
    encode_sim_args,
    encode_u16,
)


class Reader:
    def __init__(self, source, block_hash: str):
        self.source = source
        self.block_hash = block_hash.lower()
        self.page_count = 0
        self.position_count = 0
        self.runtime_spec = None

    def ensure_block(self, supplied_number: int | None) -> tuple[int, int]:
        finalized = self._hash(self.source.call("chain_getFinalizedHead", []))
        finalized_header = self.source.call("chain_getHeader", [finalized])
        header = self.source.call("chain_getHeader", [self.block_hash])
        number = _header_number(header)
        finalized_number = _header_number(finalized_header)
        if number > finalized_number:
            raise RunFailure("block_above_finalized")
        canonical = self._hash(self.source.call("chain_getBlockHash", [hex(number)]))
        if canonical != self.block_hash:
            raise RunFailure("block_not_canonical")
        if supplied_number is not None and supplied_number != number:
            raise RunFailure("block_number_mismatch")
        return number, self.timestamp_ms()

    def spec_version(self) -> int:
        version = self.source.call("state_getRuntimeVersion", [self.block_hash])
        if not isinstance(version, dict) or not isinstance(version.get("specVersion"), int):
            raise RunFailure("decode_error")
        self.runtime_spec = version["specVersion"]
        return self.runtime_spec

    def qualified_runtime(self, spec: int) -> bool:
        from .constants import SUPPORTED_SPECS, RUNTIME_CODE_PINS
        if spec in RUNTIME_CODE_PINS:
            return self.source.call("state_getStorageHash", ["0x3a636f6465", self.block_hash]) == RUNTIME_CODE_PINS[spec]
        return spec in SUPPORTED_SPECS

    def timestamp_ms(self) -> int:
        return self._u64(timestamp_now_key(), default=None)

    def mechanism(self, netuid: int) -> int:
        return self._u16(subnet_mechanism_key(netuid), default=0)

    def subnet_state(self, netuid: int) -> int | None:
        if self.runtime_spec == 483:
            from .keys import networks_added_key
            # The reviewed 483 dissolution path removes NetworksAdded before
            # asynchronous cleanup. Remaining reserves are not proof of existence.
            return 1 if self._bool(networks_added_key(netuid), default=False) else None
        raw = self._raw(subnet_state_key(netuid))
        if raw is None:
            return None
        try:
            return decode_u8_enum(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    def subtoken_enabled(self, netuid: int) -> bool:
        return self._bool(subtoken_enabled_key(netuid), default=False)

    def pool_initialized(self, netuid: int) -> bool:
        return self._bool(pal_swap_initialized_key(netuid), default=False)

    def alpha_in(self, netuid: int) -> int:
        return self._u64(subnet_alpha_in_key(netuid), default=0)

    def tao(self, netuid: int) -> int:
        return self._u64(subnet_tao_key(netuid), default=0)

    def alpha_out(self, netuid: int) -> int:
        return self._u64(subnet_alpha_out_key(netuid), default=0)

    def quote_parts(self, netuid: int) -> int:
        raw = self._raw(swap_balancer_key(netuid))
        if raw is None:
            # ValueQuery default balancer is a half-weight pool. Absent storage is that default.
            return 500_000_000_000_000_000
        try:
            return decode_quote_parts(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    def fee_rate(self, netuid: int) -> int:
        return self._u16(fee_rate_key(netuid), default=33)

    def total_networks(self) -> int:
        return self._u16(total_networks_key(), default=0)

    def current_alpha_price(self, netuid: int) -> int:
        raw = self._call("SwapRuntimeApi_current_alpha_price", encode_u16(netuid))
        try:
            return decode_u64(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    def discover_coldkeys(self, netuid: int) -> list[bytes]:
        inventory = getattr(self.source, 'alpha_inventory', None)
        if inventory is not None:
            coldkeys, pages, positions = inventory(netuid)
            self.page_count += pages
            self.position_count += positions
            return coldkeys
        prefix = hex_encode(alpha_v2_prefix())
        start = None
        found: set[bytes] = set()
        while True:
            page = self.source.call(
                "state_getKeysPaged",
                [prefix, KEY_PAGE_SIZE, start, self.block_hash],
            )
            if not isinstance(page, list):
                raise RunFailure("decode_error")
            self.page_count += 1
            if os.environ.get("BACKING_FORM_PROGRESS"):
                print(f"alpha-v2 page {self.page_count} keys={len(page) if isinstance(page, list) else 'bad'}", file=sys.stderr)
            if not page:
                break
            for key_hex in page:
                if not isinstance(key_hex, str):
                    raise RunFailure("decode_error")
                parsed = parse_alpha_v2_key(hex_decode(key_hex))
                if parsed is None:
                    continue
                _hot, cold, key_netuid = parsed
                if key_netuid == netuid:
                    found.add(cold)
                    self.position_count += 1
            if start is not None and page[-1] == start:
                raise RunFailure("decode_error")
            start = page[-1]
            if len(page) < KEY_PAGE_SIZE:
                break
        return sorted(found)

    def availability(self, coldkeys: list[bytes], netuid: int) -> dict[bytes, Availability]:
        rows: dict[bytes, Availability] = {}
        for offset in range(0, len(coldkeys), AVAILABILITY_BATCH):
            batch = coldkeys[offset : offset + AVAILABILITY_BATCH]
            raw = self._call(
                "StakeInfoRuntimeApi_get_stake_availability_for_coldkeys",
                encode_availability_args(batch, netuid),
            )
            try:
                decoded = decode_availability_map(raw)
            except ScaleError as exc:
                raise RunFailure("decode_error") from exc
            for coldkey, by_netuid in decoded.items():
                row = by_netuid.get(netuid)
                if row is not None:
                    rows[coldkey] = row
        return rows

    def lock_mass(self, coldkey: bytes, netuid: int) -> int | None:
        raw = self._call("StakeInfoRuntimeApi_get_coldkey_lock", encode_lock_args(coldkey, netuid))
        try:
            return decode_lock_mass(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    def collateral(self, netuid: int, coldkey: bytes) -> int:
        return self._u64(coldkey_miner_collateral_key(netuid, coldkey), default=0)

    def sim(self, netuid: int, amount: int) -> SimSwap:
        raw = self._call("SwapRuntimeApi_sim_swap_alpha_for_tao", encode_sim_args(netuid, amount))
        try:
            return decode_sim(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    def _call(self, method: str, data: bytes) -> bytes:
        result = self.source.call("state_call", [method, hex_encode(data), self.block_hash])
        if not isinstance(result, str):
            raise RunFailure("decode_error")
        return hex_decode(result)

    def _raw(self, key: bytes) -> bytes | None:
        result = self.source.call("state_getStorage", [hex_encode(key), self.block_hash])
        if result is None:
            return None
        if not isinstance(result, str):
            raise RunFailure("decode_error")
        return hex_decode(result)

    def _u64(self, key: bytes, default: int | None) -> int:
        raw = self._raw(key)
        if raw is None:
            if default is None:
                raise RunFailure("decode_error")
            return default
        try:
            return decode_u64(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    def _u16(self, key: bytes, default: int) -> int:
        raw = self._raw(key)
        if raw is None:
            return default
        try:
            return decode_u16(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    def _bool(self, key: bytes, default: bool) -> bool:
        raw = self._raw(key)
        if raw is None:
            return default
        try:
            return decode_bool(raw)
        except ScaleError as exc:
            raise RunFailure("decode_error") from exc

    @staticmethod
    def _hash(value: Any) -> str:
        if not isinstance(value, str):
            raise RunFailure("decode_error")
        return value.lower()


def _header_number(header: Any) -> int:
    if not isinstance(header, dict) or "number" not in header:
        raise RunFailure("decode_error")
    number = header["number"]
    try:
        if isinstance(number, str):
            return int(number, 16) if number.startswith(("0x", "0X")) else int(number)
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    except ValueError as exc:
        raise RunFailure("decode_error") from exc
    raise RunFailure("decode_error")


def alpha_inventory(source, block_hash):
    prefix = hex_encode(alpha_v2_prefix())
    start = None
    found, positions, pages = {}, {}, 0
    while True:
        page = source.call('state_getKeysPaged', [prefix, KEY_PAGE_SIZE, start, block_hash])
        if (not isinstance(page, list) or len(page) > KEY_PAGE_SIZE
                or any(type(key) is not str or not key.startswith(prefix) for key in page)
                or page != sorted(set(page))
                or (start is not None and page and page[0] <= start)):
            raise RunFailure('decode_error')
        pages += 1
        if pages > 4096:
            raise RunFailure('decode_error')
        if not page:
            break
        for key_hex in page:
            if not isinstance(key_hex, str):
                raise RunFailure('decode_error')
            parsed = parse_alpha_v2_key(hex_decode(key_hex))
            if parsed is None:
                continue
            _hot, cold, netuid = parsed
            found.setdefault(netuid, set()).add(cold)
            positions[netuid] = positions.get(netuid, 0) + 1
        if start is not None and page[-1] == start:
            raise RunFailure('decode_error')
        start = page[-1]
        if len(page) < KEY_PAGE_SIZE:
            break
    return {netuid: sorted(coldkeys) for netuid, coldkeys in found.items()}, positions, pages
