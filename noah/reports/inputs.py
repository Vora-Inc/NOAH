"""Load this validator's own backing rounds and retained checker evidence.

The genesis, day anchor, netuid, and seat hotkeys come from the caller.
They are the validator's state. A verdict is not accepted because its own
fields agree with each other.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..reports.verification import verify_collected
from .gate import _digest, eval_day_for_close


class BackingGateNotConfigured(RuntimeError):
    """The gate is on and a required path or identity source is missing."""


class BackingContextRejected(RuntimeError):
    """A required round does not belong to this validator's network."""


def load_backing_snapshot(
    rounds_path,
    evidence_root,
    *,
    genesis_hash: str,
    netuid: int,
    day_anchor_ts: int,
    evaluated_at_ms: int,
    seats: dict,
    participation: dict,
    registration_blocks: dict | None = None,
) -> dict:
    """Read the configured rounds and packages once. Raise if the path is absent."""
    if not rounds_path or not evidence_root:
        raise BackingGateNotConfigured(
            "Report gating is enabled but the rounds path or "
            "evidence path is not set. Scoring is refused."
        )
    rounds_file = Path(rounds_path)
    evidence_dir = Path(evidence_root)
    if not rounds_file.is_file() or not evidence_dir.is_dir():
        raise BackingGateNotConfigured(
            "the backing rounds file or evidence directory does not exist. "
            "Scoring is refused."
        )
    if not seats:
        raise BackingGateNotConfigured(
            "this validator has no trusted seat identities, so backing verdicts "
            "cannot be bound to miners. Scoring is refused."
        )
    rounds_bytes = rounds_file.read_bytes()
    rounds = json.loads(rounds_bytes)
    if isinstance(rounds, dict):
        rounds = rounds.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise BackingGateNotConfigured("the required-round file is empty. Scoring is refused.")
    for contract in rounds:
        if contract.get("genesis_hash") != genesis_hash or contract.get("mining_netuid") != netuid:
            raise BackingContextRejected(
                "a required round does not match this validator's genesis or netuid. "
                "It is not accepted."
            )
    required = {_digest(contract): contract for contract in rounds}
    seat_by_hotkey = {hotkey: int(uid) for uid, hotkey in _seat_pairs(seats)}
    grouped = {}
    evidence_ids = []
    for evidence_path in evidence_dir.rglob("evidence.json"):
        package = evidence_path.parent
        round_path = package / "round.json"
        if not round_path.is_file():
            continue
        contract = json.loads(round_path.read_text())
        digest = _digest(contract)
        if digest not in required:
            continue
        body = json.loads(evidence_path.read_text())
        per_miner = body.get("evidence") if isinstance(body, dict) else None
        if not isinstance(per_miner, dict):
            continue
        evidence_ids.append(_digest(body))
        for claimed_hotkey, evidence in per_miner.items():
            uid = seat_by_hotkey.get(claimed_hotkey)
            snapshot_hotkey = (evidence.get("snapshot") or {}).get("hotkey") if isinstance(evidence, dict) else None
            if uid is None or snapshot_hotkey != claimed_hotkey or not isinstance(evidence, dict):
                continue
            verdict = verify_collected(contract, evidence)
            grouped.setdefault((digest, claimed_hotkey), []).append({
                "complete": _is_complete(evidence, contract),
                "evidence_digest": _digest(evidence),
                "verdict": {
                    "eval_day": eval_day_for_close(contract["reveal_close_ms"], day_anchor_ts),
                    "genesis_hash": genesis_hash,
                    "hotkey": claimed_hotkey,
                    "netuid": netuid,
                    "reason": verdict["reason"],
                    "round_sha256": digest,
                    "status": verdict["status"],
                    "uid": uid,
                },
            })
    verdicts = {digest: {} for digest in required}
    for (digest, hotkey), items in grouped.items():
        verdicts[digest][hotkey] = _choose(items)
    snapshot = {
        "day_anchor_ts": day_anchor_ts,
        "evaluated_at_ms": evaluated_at_ms,
        "evidence_sha256": _digest(sorted(set(evidence_ids))),
        "genesis_hash": genesis_hash,
        "netuid": netuid,
        "participation": {str(uid): day for uid, day in participation.items()},
        "registration_blocks": registration_blocks or {},
        "rounds": rounds,
        "rounds_sha256": _digest(json.loads(rounds_bytes.decode())),
        "seats": {str(uid): hotkey for uid, hotkey in _seat_pairs(seats)},
        "seats_sha256": _digest({str(uid): hotkey for uid, hotkey in _seat_pairs(seats)}),
        "verdicts": verdicts,
    }
    snapshot["inputs"] = input_identity(snapshot)
    return snapshot


def _is_complete(evidence: dict, contract: dict) -> bool:
    """The collector completeness check from ``noah.reports.verification._complete``."""
    for key in ("collection", "deposit_history"):
        record = evidence.get(key)
        if not (
            isinstance(record, dict)
            and record.get("state") == "COMPLETE"
            and record.get("gaps") == []
            and record.get("errors") == []
            and record.get("genesis_hash") == contract.get("genesis_hash")
            and record.get("round_block_hash") == contract.get("block_hash")
        ):
            return False
    return True


def _choose(items: list) -> dict:
    """Collapse identical evidence. A completed package beats an incomplete one.

    Two different completed packages are a conflict. Neither verdict is used.
    """
    by_digest = {}
    for item in items:
        by_digest.setdefault(item["evidence_digest"], item)
    unique = list(by_digest.values())
    complete = [item for item in unique if item["complete"]]
    pool = complete or unique
    if len(pool) == 1:
        return pool[0]["verdict"]
    sample = pool[0]["verdict"]
    reason = "conflicting_completed_evidence" if complete else "conflicting_incomplete_evidence"
    return {**sample, "reason": reason, "status": "UNVERIFIABLE"}


def input_identity(snapshot: dict) -> dict:
    """Digests retained with the calculation. Paths are not copied."""
    return {
        "day_anchor_ts": snapshot["day_anchor_ts"],
        "evidence_sha256": snapshot["evidence_sha256"],
        "genesis_hash": snapshot["genesis_hash"],
        "netuid": snapshot["netuid"],
        "rounds_sha256": snapshot["rounds_sha256"],
        "seats_sha256": snapshot["seats_sha256"],
    }


def participation_from_observations(observations: dict, seats: dict) -> dict:
    """Registration days from the validator's own scoring fixture, by trusted uid."""
    known = {int(uid) for uid, _hotkey in _seat_pairs(seats)}
    found = {}
    for miner in observations.get("miners") or []:
        uid = miner.get("uid")
        if type(uid) is not int or uid not in known:
            continue
        registration = miner.get("registrationDay")
        if isinstance(registration, str) and registration.isdigit():
            registration = int(registration)
        if type(registration) is int:
            found[uid] = registration
    return found


def _seat_pairs(seats: dict):
    pairs = []
    for uid, hotkey in seats.items():
        if isinstance(uid, str) and uid.isdigit():
            uid = int(uid)
        if type(uid) is int and isinstance(hotkey, str):
            pairs.append((uid, hotkey))
    return sorted(pairs)
