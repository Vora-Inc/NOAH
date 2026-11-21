"""Option 1 preimage. 207 bytes. Commitment is SHA-256 of those bytes.

The illustrative 41-byte layout does not bind miner, network, round date, or
block hash. This layout does.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

DOMAIN = b"bfpoc-s2-v2"
PREIMAGE_LEN = 207
assert len(DOMAIN) == 11
REPORT_DOMAIN = b"noah-rpt-v1"
REPORT_FORMAT = struct.Struct("<11s32s32s32sHHII32sIQQ16s16s")
REPORT_POLICY = "registered-at-b-accurate-report-v1"


@dataclass(frozen=True)
class ReportSubmission:
    """Report-only v1. Both subnet IDs are committed; no deposit claim exists."""
    genesis_hash: bytes
    hotkey: bytes
    coldkey: bytes
    netuid: int
    mining_netuid: int
    round_yyyymmdd: int
    block_number: int
    block_hash: bytes
    out1_ppm: int
    out2_rao: int
    out3_rao: int
    release_prefix: bytes
    salt: bytes

    def encode(self):
        values = []
        for name, size in (("genesis_hash", 32), ("hotkey", 32), ("coldkey", 32),
                           ("block_hash", 32), ("release_prefix", 16), ("salt", 16)):
            _check_len(getattr(self, name), size, name)
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name not in {"genesis_hash", "hotkey", "coldkey", "block_hash", "release_prefix", "salt"}:
                if type(value) is not int or value < 0:
                    raise ValueError("report integers must be nonnegative integers")
            values.append(value)
        try:
            return REPORT_FORMAT.pack(REPORT_DOMAIN, *values)
        except struct.error as exc:
            raise ValueError("report integer out of bounds") from exc


@dataclass(frozen=True)
class Submission:
    genesis_hash: bytes
    hotkey: bytes
    coldkey: bytes
    netuid: int
    round_yyyymmdd: int
    block_number: int
    block_hash: bytes
    deposit_block: int
    deposit_event: int
    out1_ppm: int
    out2_rao: int
    out3_rao: int
    release_prefix: bytes
    salt: bytes

    def encode(self) -> bytes:
        _check_len(self.genesis_hash, 32, "genesis")
        _check_len(self.hotkey, 32, "hotkey")
        _check_len(self.coldkey, 32, "coldkey")
        _check_len(self.block_hash, 32, "block hash")
        _check_len(self.release_prefix, 16, "release prefix")
        _check_len(self.salt, 16, "salt")
        body = b"".join(
            (
                DOMAIN,
                self.genesis_hash,
                self.hotkey,
                self.coldkey,
                self.netuid.to_bytes(2, "little"),
                self.round_yyyymmdd.to_bytes(4, "little"),
                self.block_number.to_bytes(4, "little"),
                self.block_hash,
                self.deposit_block.to_bytes(4, "little"),
                self.deposit_event.to_bytes(2, "little"),
                self.out1_ppm.to_bytes(4, "little"),
                self.out2_rao.to_bytes(8, "little"),
                self.out3_rao.to_bytes(8, "little"),
                self.release_prefix,
                self.salt,
            )
        )
        if len(body) != PREIMAGE_LEN:
            raise ValueError(f"preimage length {len(body)}")
        return body


def commitment_of(preimage: bytes) -> bytes:
    if len(preimage) not in (PREIMAGE_LEN, REPORT_FORMAT.size):
        raise ValueError("malformed preimage")
    return hashlib.sha256(preimage).digest()


def decode(preimage: bytes) -> Submission:
    if len(preimage) == REPORT_FORMAT.size and preimage.startswith(REPORT_DOMAIN):
        return ReportSubmission(*REPORT_FORMAT.unpack(preimage)[1:])
    if len(preimage) != PREIMAGE_LEN or not preimage.startswith(DOMAIN):
        raise ValueError("malformed preimage")
    offset = 11
    def take(n: int) -> bytes:
        nonlocal offset
        chunk = preimage[offset : offset + n]
        offset += n
        return chunk
    genesis = take(32)
    hotkey = take(32)
    coldkey = take(32)
    netuid = int.from_bytes(take(2), "little")
    ymd = int.from_bytes(take(4), "little")
    number = int.from_bytes(take(4), "little")
    block_hash = take(32)
    deposit_block = int.from_bytes(take(4), "little")
    deposit_event = int.from_bytes(take(2), "little")
    out1 = int.from_bytes(take(4), "little")
    out2 = int.from_bytes(take(8), "little")
    out3 = int.from_bytes(take(8), "little")
    prefix = take(16)
    salt = take(16)
    if offset != PREIMAGE_LEN:
        raise ValueError("malformed preimage")
    return Submission(
        genesis, hotkey, coldkey, netuid, ymd, number, block_hash,
        deposit_block, deposit_event, out1, out2, out3, prefix, salt,
    )


def _check_len(value: bytes, size: int, name: str) -> None:
    if len(value) != size:
        raise ValueError(f"{name} must be {size} bytes")
