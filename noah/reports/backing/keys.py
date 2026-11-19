"""Storage keys and SS58. xxHash64 is pure Python; nothing is imported from services/."""

from __future__ import annotations

import hashlib

from .constants import (
    PALLET_SUBTENSOR,
    PALLET_SWAP,
    PALLET_TIMESTAMP,
    SS58_PREFIX,
)

_MASK64 = (1 << 64) - 1
_P1 = 0x9E3779B185EBCA87
_P2 = 0xC2B2AE3D27D4EB4F
_P3 = 0x165667B19E3779F9
_P4 = 0x85EBCA77C2B2AE63
_P5 = 0x27D4EB2F165667C5

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _rotl(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & _MASK64


def _round(acc: int, lane: int) -> int:
    acc = (acc + (lane * _P2)) & _MASK64
    acc = _rotl(acc, 31)
    return (acc * _P1) & _MASK64


def _merge(acc: int, accn: int) -> int:
    acc ^= _round(0, accn)
    return (acc * _P1 + _P4) & _MASK64


def xxh64(data: bytes, seed: int = 0) -> int:
    """xxHash64, the hash substrate's twox_* wrappers use."""
    length = len(data)
    offset = 0
    if length >= 32:
        acc1 = (seed + _P1 + _P2) & _MASK64
        acc2 = (seed + _P2) & _MASK64
        acc3 = seed & _MASK64
        acc4 = (seed - _P1) & _MASK64
        while offset + 32 <= length:
            acc1 = _round(acc1, int.from_bytes(data[offset : offset + 8], "little"))
            acc2 = _round(acc2, int.from_bytes(data[offset + 8 : offset + 16], "little"))
            acc3 = _round(acc3, int.from_bytes(data[offset + 16 : offset + 24], "little"))
            acc4 = _round(acc4, int.from_bytes(data[offset + 24 : offset + 32], "little"))
            offset += 32
        acc = (
            _rotl(acc1, 1) + _rotl(acc2, 7) + _rotl(acc3, 12) + _rotl(acc4, 18)
        ) & _MASK64
        acc = _merge(acc, acc1)
        acc = _merge(acc, acc2)
        acc = _merge(acc, acc3)
        acc = _merge(acc, acc4)
    else:
        acc = (seed + _P5) & _MASK64
    acc = (acc + length) & _MASK64
    while offset + 8 <= length:
        lane = int.from_bytes(data[offset : offset + 8], "little")
        acc ^= _round(0, lane)
        acc = (_rotl(acc, 27) * _P1 + _P4) & _MASK64
        offset += 8
    if offset + 4 <= length:
        lane = int.from_bytes(data[offset : offset + 4], "little")
        acc ^= (lane * _P1) & _MASK64
        acc = (_rotl(acc, 23) * _P2 + _P3) & _MASK64
        offset += 4
    while offset < length:
        acc ^= (data[offset] * _P5) & _MASK64
        acc = (_rotl(acc, 11) * _P1) & _MASK64
        offset += 1
    acc ^= acc >> 33
    acc = (acc * _P2) & _MASK64
    acc ^= acc >> 29
    acc = (acc * _P3) & _MASK64
    acc ^= acc >> 32
    return acc


def twox64(data: bytes) -> bytes:
    return xxh64(data, 0).to_bytes(8, "little")


def twox128(data: bytes) -> bytes:
    return xxh64(data, 0).to_bytes(8, "little") + xxh64(data, 1).to_bytes(8, "little")


def twox64_concat(data: bytes) -> bytes:
    return twox64(data) + data


def blake2_128_concat(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest() + data


def storage_prefix(pallet: str, item: str) -> bytes:
    return twox128(pallet.encode()) + twox128(item.encode())


def identity_key(prefix: bytes, encoded_key: bytes) -> bytes:
    return prefix + encoded_key


def netuid_le(netuid: int) -> bytes:
    if not 0 <= netuid <= 0xFFFF:
        raise ValueError("netuid out of range")
    return netuid.to_bytes(2, "little")


def u16_le(value: int) -> bytes:
    return netuid_le(value)


def u64_le(value: int) -> bytes:
    return value.to_bytes(8, "little")


def map_identity(pallet: str, item: str, netuid: int) -> bytes:
    return storage_prefix(pallet, item) + netuid_le(netuid)


def map_twox64(pallet: str, item: str, netuid: int) -> bytes:
    encoded = netuid_le(netuid)
    return storage_prefix(pallet, item) + twox64_concat(encoded)


def subnet_alpha_in_key(netuid: int) -> bytes:
    return map_identity(PALLET_SUBTENSOR, "SubnetAlphaIn", netuid)


def subnet_tao_key(netuid: int) -> bytes:
    return map_identity(PALLET_SUBTENSOR, "SubnetTAO", netuid)


def subnet_alpha_out_key(netuid: int) -> bytes:
    return map_identity(PALLET_SUBTENSOR, "SubnetAlphaOut", netuid)


def subnet_mechanism_key(netuid: int) -> bytes:
    return map_identity(PALLET_SUBTENSOR, "SubnetMechanism", netuid)


def subnet_state_key(netuid: int) -> bytes:
    return map_identity(PALLET_SUBTENSOR, "SubnetState", netuid)


def networks_added_key(netuid: int) -> bytes:
    return map_identity(PALLET_SUBTENSOR, "NetworksAdded", netuid)


def subtoken_enabled_key(netuid: int) -> bytes:
    return map_identity(PALLET_SUBTENSOR, "SubtokenEnabled", netuid)


def total_networks_key() -> bytes:
    return storage_prefix(PALLET_SUBTENSOR, "TotalNetworks")


def alpha_v2_prefix() -> bytes:
    return storage_prefix(PALLET_SUBTENSOR, "AlphaV2")


def swap_balancer_key(netuid: int) -> bytes:
    return map_twox64(PALLET_SWAP, "SwapBalancer", netuid)


def fee_rate_key(netuid: int) -> bytes:
    return map_twox64(PALLET_SWAP, "FeeRate", netuid)


def pal_swap_initialized_key(netuid: int) -> bytes:
    return map_twox64(PALLET_SWAP, "PalSwapInitialized", netuid)


def timestamp_now_key() -> bytes:
    return storage_prefix(PALLET_TIMESTAMP, "Now")


def coldkey_miner_collateral_key(netuid: int, coldkey: bytes) -> bytes:
    # Identity netuid, then Blake2_128Concat coldkey.
    return (
        storage_prefix(PALLET_SUBTENSOR, "ColdkeyMinerCollateral")
        + netuid_le(netuid)
        + blake2_128_concat(coldkey)
    )


def parse_alpha_v2_key(key: bytes) -> tuple[bytes, bytes, int] | None:
    """Return (hotkey, coldkey, netuid) or None if the key is not AlphaV2-shaped."""
    prefix = alpha_v2_prefix()
    if not key.startswith(prefix):
        return None
    body = key[len(prefix) :]
    # blake2_128(16) + hot(32) + blake2_128(16) + cold(32) + netuid(2) = 98
    if len(body) != 98:
        return None
    hot = body[16:48]
    cold = body[64:96]
    netuid = int.from_bytes(body[96:98], "little")
    return hot, cold, netuid


def _b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    chars: list[str] = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(_B58[rem])
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    if not chars:
        chars.append("1")
    return ("1" * pad) + "".join(reversed(chars))


def _b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        number = number * 58 + _B58.index(char)
    pad = 0
    for char in text:
        if char == "1":
            pad += 1
        else:
            break
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return (b"\x00" * pad) + raw


def ss58_encode(pubkey: bytes, prefix: int = SS58_PREFIX) -> str:
    if len(pubkey) != 32:
        raise ValueError("pubkey must be 32 bytes")
    if not 0 <= prefix < 64:
        raise ValueError("only a single-byte SS58 prefix is implemented")
    payload = bytes([prefix]) + pubkey
    checksum = hashlib.blake2b(b"SS58PRE" + payload, digest_size=64).digest()[:2]
    return _b58encode(payload + checksum)


def ss58_decode(address: str, prefix: int = SS58_PREFIX) -> bytes:
    raw = _b58decode(address)
    if len(raw) != 35:
        raise ValueError("unexpected SS58 length")
    payload, checksum = raw[:-2], raw[-2:]
    expected = hashlib.blake2b(b"SS58PRE" + payload, digest_size=64).digest()[:2]
    if checksum != expected or payload[0] != prefix:
        raise ValueError("SS58 checksum or prefix mismatch")
    return payload[1:]


def hex_encode(data: bytes) -> str:
    return "0x" + data.hex()


def hex_decode(value: str) -> bytes:
    text = value[2:] if value.startswith(("0x", "0X")) else value
    if len(text) % 2:
        text = "0" + text
    return bytes.fromhex(text)
