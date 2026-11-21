"""Minimal SCALE codec for the types this slice reads."""

from __future__ import annotations

from dataclasses import dataclass


class ScaleError(ValueError):
    """Bytes were not the expected SCALE value. This is a decode failure, not a sale."""


@dataclass(frozen=True)
class SimSwap:
    tao_amount: int
    alpha_amount: int
    tao_fee: int
    alpha_fee: int
    tao_slippage: int
    alpha_slippage: int

    def as_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.tao_amount,
            self.alpha_amount,
            self.tao_fee,
            self.alpha_fee,
            self.tao_slippage,
            self.alpha_slippage,
        )


@dataclass(frozen=True)
class Availability:
    total: int
    locked: int
    available: int


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def take(self, size: int) -> bytes:
        if self.remaining() < size:
            raise ScaleError("truncated")
        chunk = self.data[self.offset : self.offset + size]
        self.offset += size
        return chunk

    def finish(self) -> None:
        if self.remaining() != 0:
            raise ScaleError("trailing bytes")

    def compact(self) -> int:
        if self.remaining() < 1:
            raise ScaleError("truncated compact")
        first = self.data[self.offset]
        mode = first & 0b11
        if mode == 0:
            self.offset += 1
            return first >> 2
        if mode == 1:
            raw = int.from_bytes(self.take(2), "little")
            return raw >> 2
        if mode == 2:
            raw = int.from_bytes(self.take(4), "little")
            return raw >> 2
        nbytes = (first >> 2) + 4
        self.offset += 1
        return int.from_bytes(self.take(nbytes), "little")

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def u128(self) -> int:
        return int.from_bytes(self.take(16), "little")

    def bool(self) -> bool:
        value = self.take(1)[0]
        if value not in (0, 1):
            raise ScaleError("invalid bool")
        return value == 1

    def account(self) -> bytes:
        return self.take(32)

    def option_byte(self) -> bool:
        flag = self.take(1)[0]
        if flag == 0:
            return False
        if flag == 1:
            return True
        raise ScaleError("invalid option")


def _finish(reader: Reader) -> None:
    reader.finish()


def encode_compact(value: int) -> bytes:
    if value < 0:
        raise ScaleError("negative compact")
    if value < 1 << 6:
        return bytes([value << 2])
    if value < 1 << 14:
        return (value << 2 | 0b01).to_bytes(2, "little")
    if value < 1 << 30:
        return (value << 2 | 0b10).to_bytes(4, "little")
    payload = value.to_bytes((value.bit_length() + 7) // 8, "little")
    if len(payload) > 67:
        raise ScaleError("compact too wide")
    return bytes([((len(payload) - 4) << 2) | 0b11]) + payload


def encode_u16(value: int) -> bytes:
    return value.to_bytes(2, "little")


def encode_u64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def encode_vec_accounts(accounts: list[bytes]) -> bytes:
    out = bytearray(encode_compact(len(accounts)))
    for account in accounts:
        if len(account) != 32:
            raise ScaleError("account must be 32 bytes")
        out += account
    return bytes(out)


def encode_option_vec_u16(values: list[int] | None) -> bytes:
    if values is None:
        return b"\x00"
    out = bytearray(b"\x01")
    out += encode_compact(len(values))
    for value in values:
        out += encode_u16(value)
    return bytes(out)


def encode_sim_args(netuid: int, amount: int) -> bytes:
    return encode_u16(netuid) + encode_u64(amount)


def encode_lock_args(coldkey: bytes, netuid: int) -> bytes:
    return coldkey + encode_u16(netuid)


def encode_availability_args(coldkeys: list[bytes], netuid: int) -> bytes:
    return encode_vec_accounts(coldkeys) + encode_option_vec_u16([netuid])


def decode_u64(data: bytes) -> int:
    reader = Reader(data)
    value = reader.u64()
    _finish(reader)
    return value


def decode_u16(data: bytes) -> int:
    reader = Reader(data)
    value = reader.u16()
    _finish(reader)
    return value


def decode_bool(data: bytes) -> bool:
    reader = Reader(data)
    value = reader.bool()
    _finish(reader)
    return value


def decode_u8_enum(data: bytes) -> int:
    reader = Reader(data)
    value = reader.take(1)[0]
    _finish(reader)
    return value


def decode_sim(data: bytes) -> SimSwap:
    reader = Reader(data)
    sim = SimSwap(
        tao_amount=reader.u64(),
        alpha_amount=reader.u64(),
        tao_fee=reader.u64(),
        alpha_fee=reader.u64(),
        tao_slippage=reader.u64(),
        alpha_slippage=reader.u64(),
    )
    _finish(reader)
    return sim


def decode_quote_parts(data: bytes) -> int:
    # Balancer { quote: Perquintill } encodes as one u64.
    return decode_u64(data)


def decode_availability_map(data: bytes) -> dict[bytes, dict[int, Availability]]:
    reader = Reader(data)
    outer_len = reader.compact()
    result: dict[bytes, dict[int, Availability]] = {}
    for _ in range(outer_len):
        coldkey = reader.account()
        inner_len = reader.compact()
        inner: dict[int, Availability] = {}
        for _inner in range(inner_len):
            netuid = reader.u16()
            row = Availability(total=reader.compact(), locked=reader.compact(), available=reader.compact())
            inner[netuid] = row
        result[coldkey] = inner
    _finish(reader)
    return result


def decode_lock_mass(data: bytes) -> int | None:
    """Option<LockState> → locked_mass, or None when the option is empty."""
    reader = Reader(data)
    if not reader.option_byte():
        _finish(reader)
        return None
    locked = reader.u64()
    reader.u128()  # conviction, unused
    reader.u64()  # last_update, unused
    _finish(reader)
    return locked
