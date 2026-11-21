"""Canonical private report artifacts; these are not public website payloads."""

import hashlib
import json
import os
import stat


def canonical_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def bytes_sha256(value):
    return hashlib.sha256(value).hexdigest()


def engine_binding(contract, engine):
    from .common import digest
    from .backing.round_config import calculation_for_shadow
    projection = calculation_for_shadow(contract, engine, allow_unavailable_outputs=True) if engine.get("status") == "OK" else None
    return {
        "schema": "noah.report-engine-binding/1",
        "round_sha256": digest(contract),
        "engine_sha256": bytes_sha256(canonical_bytes(engine)),
        "calculation_sha256": None if projection is None else digest(projection),
        "calculation_version": contract["calculation_version"],
        "genesis_hash": contract["genesis_hash"],
        "block_hash": contract["block_hash"],
    }


def check_retained_engine(directory, contract, engine):
    """Compare exact retained bytes to a fresh raw-response reconstruction.

    Legacy evidence without either artifact can still be replayed, but it is
    explicitly unsuitable for the new producer/adjudication artifact contract.
    """
    path = directory / "engine.json"
    binding = directory / "engine-binding.json"
    if path.is_symlink() or binding.is_symlink():
        return False, "retained_engine_symlink"
    if not path.exists() and not binding.exists():
        return False, None
    try:
        if path.read_bytes() != canonical_bytes(engine):
            return False, "retained_engine_mismatch"
        if binding.read_bytes() != canonical_bytes(engine_binding(contract, engine)):
            return False, "retained_engine_binding_mismatch"
    except (OSError, ValueError, TypeError):
        return False, "retained_engine_unavailable"
    return True, None


def retained_coverage_method(directory, contract):
    """Select historical semantics from bound artifacts, never a saved verdict.

    The containing adjudication manifest is separately authenticated by its
    caller. Standalone replay proves consistency, not external provenance.
    """
    from .backing.constants import COVERAGE_LEGACY, COVERAGE_STAKED_V2
    from .adjudication_contract import require
    from .identity_shapes import validate_engine
    path, binding = directory / "engine.json", directory / "engine-binding.json"
    require(not path.is_symlink() and not binding.is_symlink(), "retained engine link")
    if not path.exists() and not binding.exists():
        return COVERAGE_LEGACY  # Oldest standalone evidence; not eligible for adjudication.
    def read(member):
        fd = os.open(member, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                    and 0 < before.st_size <= 2 * 1024**2, "retained engine bound")
            raw = stream.read(2 * 1024**2 + 1)
            after = os.fstat(stream.fileno())
        current = member.lstat()
        signature = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                                  info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        require(len(raw) == before.st_size and signature(before) == signature(after) == signature(current),
                "retained engine changed while reading")
        return raw
    raw = read(path)
    engine = json.loads(raw)  # Canonical byte equality below rejects duplicate/noncanonical input.
    validate_engine(engine)
    require(raw == canonical_bytes(engine), "noncanonical retained engine")
    require(read(binding) == canonical_bytes(engine_binding(contract, engine)), "retained engine binding differs")
    return COVERAGE_STAKED_V2 if "coverage_method" in engine else COVERAGE_LEGACY
