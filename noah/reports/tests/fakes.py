"""In-memory chain for engine tests. Not a substitute for pinned runtime receipts."""

from __future__ import annotations

from noah.reports.backing.constants import RPC_ALLOWLIST, STATE_METHODS
from noah.reports.backing.keys import (
    alpha_v2_prefix,
    blake2_128_concat,
    fee_rate_key,
    hex_encode,
    netuid_le,
    pal_swap_initialized_key,
    subnet_alpha_in_key,
    subnet_alpha_out_key,
    subnet_mechanism_key,
    subnet_state_key,
    subnet_tao_key,
    subtoken_enabled_key,
    swap_balancer_key,
    timestamp_now_key,
)
from noah.reports.backing.rpc import RunFailure
from noah.reports.backing.scale import (
    Availability,
    SimSwap,
    encode_compact,
    encode_u16,
    encode_u64,
)


def alpha_storage_key(hot: bytes, cold: bytes, netuid: int) -> bytes:
    return alpha_v2_prefix() + blake2_128_concat(hot) + blake2_128_concat(cold) + netuid_le(netuid)


class FakeChain:
    def __init__(self, block_hash: str):
        self.block_hash = block_hash
        self.finalized = block_hash
        self.number = 100
        self.finalized_number = 100
        self.canonical = block_hash
        self.netuid = 82
        self.timestamp = 1_700_000_000_000
        self.spec = 490
        self.storage: dict[str, str | None] = {}
        self.alpha_keys: list[str] = []
        self.availability: dict[bytes, Availability] = {}
        self.locks: dict[bytes, int | None] = {}
        self.collateral: dict[bytes, int] = {}
        self.calls: list[tuple[str, list]] = []
        self.sim = None
        self.fail: str | None = None

    def set_pool(self, netuid: int, *, alpha_in: int, tao: int, alpha_out: int, quote: int, fee: int = 33, mechanism: int = 1, state: int = 1, subtoken: bool = True, initialized: bool = True):
        self.storage[hex_encode(subnet_mechanism_key(netuid))] = hex_encode(encode_u16(mechanism))
        self.storage[hex_encode(subnet_state_key(netuid))] = hex_encode(bytes([state]))
        self.storage[hex_encode(subtoken_enabled_key(netuid))] = hex_encode(b"\x01" if subtoken else b"\x00")
        self.storage[hex_encode(pal_swap_initialized_key(netuid))] = hex_encode(b"\x01" if initialized else b"\x00")
        self.storage[hex_encode(subnet_alpha_in_key(netuid))] = hex_encode(encode_u64(alpha_in))
        self.storage[hex_encode(subnet_tao_key(netuid))] = hex_encode(encode_u64(tao))
        self.storage[hex_encode(subnet_alpha_out_key(netuid))] = hex_encode(encode_u64(alpha_out))
        self.storage[hex_encode(swap_balancer_key(netuid))] = hex_encode(encode_u64(quote))
        self.storage[hex_encode(fee_rate_key(netuid))] = hex_encode(encode_u16(fee))
        self.storage[hex_encode(timestamp_now_key())] = hex_encode(encode_u64(self.timestamp))

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        if method not in RPC_ALLOWLIST:
            raise RunFailure("method_not_allowed")
        if method in STATE_METHODS and (not params or params[-1] != self.block_hash):
            raise RunFailure("block_not_canonical")
        if self.fail and method == self.fail:
            raise RunFailure("rpc_transport")
        if method == "chain_getFinalizedHead":
            return self.finalized
        if method == "chain_getHeader":
            if params[0] == self.finalized and params[0] != self.block_hash:
                number = self.finalized_number
            else:
                number = self.number
            return {"number": hex(number)}
        if method == "chain_getBlockHash":
            return self.canonical
        if method == "state_getRuntimeVersion":
            return {"specVersion": self.spec}
        if method == "state_getStorage":
            return self.storage.get(params[0])
        if method == "state_getKeysPaged":
            prefix, count, start, _block = params
            keys = [key for key in self.alpha_keys if key.startswith(prefix)]
            if start is not None:
                keys = [key for key in keys if key > start]
            return keys[:count]
        if method == "state_call":
            return self._state_call(params[0], params[1])
        raise RunFailure("decode_error")

    def _state_call(self, name: str, data_hex: str) -> str:
        data = bytes.fromhex(data_hex[2:])
        if name == "SwapRuntimeApi_current_alpha_price":
            return hex_encode(encode_u64(32_393_900))
        if name == "SwapRuntimeApi_sim_swap_alpha_for_tao":
            amount = int.from_bytes(data[2:10], "little")
            if self.sim is not None:
                sim = self.sim(amount)
            else:
                fee = amount * 33 // 65535
                sim = SimSwap(amount // 50, amount - fee, 0, fee, 0, 0)
            return hex_encode(b"".join(part.to_bytes(8, "little") for part in sim.as_tuple()))
        if name == "StakeInfoRuntimeApi_get_stake_availability_for_coldkeys":
            return hex_encode(self._availability_bytes(data))
        if name == "StakeInfoRuntimeApi_get_coldkey_lock":
            cold = data[:32]
            locked = self.locks.get(cold)
            if locked is None:
                return "0x00"
            body = locked.to_bytes(8, "little") + (0).to_bytes(16, "little") + (0).to_bytes(8, "little")
            return hex_encode(b"\x01" + body)
        raise RunFailure("decode_error")

    def _availability_bytes(self, data: bytes) -> bytes:
        # Tests put one netuid; decode the coldkey vector length and accounts.
        from noah.reports.backing.scale import Reader

        reader = Reader(data)
        count = reader.compact()
        coldkeys = [reader.account() for _ in range(count)]
        out = bytearray(encode_compact(len(coldkeys)))
        for cold in coldkeys:
            row = self.availability.get(cold, Availability(0, 0, 0))
            out += cold
            out += encode_compact(1)
            out += encode_u16(self.netuid)
            out += encode_compact(row.total) + encode_compact(row.locked) + encode_compact(row.available)
        return bytes(out)
