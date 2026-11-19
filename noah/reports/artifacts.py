"""Canonical private report artifacts; these are not public website payloads."""

import hashlib
import json


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
