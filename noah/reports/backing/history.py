"""Resume a deposit scan that already starts at block zero.

This is not a ledger. Attribution stays in ``deposits_in_block``. The
checkpoint only records that a contiguous prefix was decoded, and the raw
response log that decoding used. A later round reads the suffix.
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

SCHEMA = "backing-history-checkpoint.v1"


def checkpoint_problem(
    document,
    *,
    genesis_hash: str,
    responses_sha256: str | None,
    ark_coldkey: str,
    ark_validator_hotkey: str,
    eligibility_policy_version: str,
) -> str | None:
    """Why this saved prefix cannot be used. None means the shape is acceptable."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return "incompatible_checkpoint"
    if document.get("from_block") != 0:
        return "history_not_from_genesis"
    through = document.get("through_block")
    if type(through) is not int or through < 0:
        return "incompatible_checkpoint"
    if document.get("genesis_hash") != genesis_hash:
        return "history_genesis_mismatch"
    if document.get("ark_coldkey") != ark_coldkey or document.get("ark_validator_hotkey") != ark_validator_hotkey:
        return "history_context"
    if document.get("eligibility_policy_version") != eligibility_policy_version:
        return "history_context"
    head = document.get("head_hash")
    if not isinstance(head, str) or not head.startswith("0x") or len(head) != 66:
        return "incompatible_checkpoint"
    if not isinstance(document.get("observed_from_ms"), int):
        return "incompatible_checkpoint"
    receipts = document.get("receipts")
    if not isinstance(receipts, list):
        return "incompatible_checkpoint"
    if any(not isinstance(row, dict) or type(row.get("block")) is not int or row["block"] > through for row in receipts):
        return "incompatible_checkpoint"
    if document.get("responses_sha256") != responses_sha256:
        return "history_responses"
    return None


def segment_chain_digest(segments: list) -> str:
    """Running hash of each segment digest. Older files are not read again."""
    running = ""
    for segment in segments:
        running = hashlib.sha256((running + "\n" + segment["sha256"]).encode()).hexdigest()
    return running


def segments_problem(document, directory: Path | None) -> str | None:
    """Why a segmented checkpoint cannot be trusted. None means the files match.

    A checkpoint without ``segments`` is the older single log. This returns
    None for that shape so the single-log digest check stays in charge.
    """
    if not isinstance(document, dict) or "segments" not in document:
        return None
    segments = document.get("segments")
    if not isinstance(segments, list) or not segments:
        return "incompatible_checkpoint"
    if directory is None:
        return "history_segment_missing"
    root = Path(directory)
    previous = -1
    for segment in segments:
        if not isinstance(segment, dict):
            return "incompatible_checkpoint"
        start = segment.get("from_block")
        end = segment.get("through_block")
        digest = segment.get("sha256")
        name = segment.get("file")
        if type(start) is not int or type(end) is not int or start != previous + 1 or end < start:
            return "history_gap"
        if not isinstance(digest, str) or len(digest) != 64 or not isinstance(name, str):
            return "incompatible_checkpoint"
        if name != f"segments/{start:012d}-{end:012d}.jsonl.gz" or ".." in name:
            return "incompatible_checkpoint"
        path = root / name
        if not path.is_file():
            return "history_segment_missing"
        if _gzip_sha256(path) != digest:
            return "history_responses"
        previous = end
    if previous != document.get("through_block"):
        return "history_gap"
    if document.get("responses_sha256") != segment_chain_digest(segments):
        return "history_responses"
    return None


def _gzip_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError:
        return ""
    return hasher.hexdigest()


def prefix_landed(blocks, document) -> bool:
    head = document["head_hash"].lower()
    return any(row["number"] == document["through_block"] and str(row["hash"]).lower() == head for row in blocks)


def make_checkpoint(round_contract, blocks, receipts, observed_from_ms: int, responses_sha256: str) -> dict | None:
    """A complete bootstrap or extension, or None when the scan does not support one."""
    through = round_contract["block_number"] - 1
    anchor = next((row for row in blocks if row["number"] == through), None)
    if anchor is None or not isinstance(anchor.get("hash"), str):
        return None
    return {
        "ark_coldkey": round_contract["ark_coldkey"],
        "ark_validator_hotkey": round_contract["ark_validator_hotkey"],
        "eligibility_policy_version": round_contract["eligibility_policy_version"],
        "from_block": 0,
        "genesis_hash": round_contract["genesis_hash"],
        "head_hash": anchor["hash"] if anchor["hash"].startswith("0x") else "0x" + anchor["hash"],
        "observed_from_ms": observed_from_ms,
        "receipts": receipts,
        "responses_sha256": responses_sha256,
        "schema": SCHEMA,
        "through_block": through,
    }
