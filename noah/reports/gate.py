"""Apply the backing-report gate to an already scored day.

``score_miner`` is not called here. ``score_fp``, streak, and the raw
``payWeightFp`` stay as the engines wrote them. The gate replaces only the
weights that allocation uses, when explicitly enabled by the caller. Live publication does not call this module.

An empty ``weightsU16`` means this validator declines to publish a new vector.
It does not erase weights that are already on chain. There is no equal-weight
fallback.
"""

from __future__ import annotations

import hashlib
import json

from ..chain.store import DAY_SECONDS
from ..scoring.pay import weights_u16

POLICY_ID = "noah.report-payout-gate/1"

# Checker status and reason -> gate class. A pair that is not listed does not
# pass and does not zero the miner; it stays an unresolved exception.
PASS_REASONS = {
    ("VERIFIED", "exact_match"): "verified",
}

# Confirmed miner results. A malformed submission is in this table, not in the
# infrastructure list.
FAILURE_REASONS = {
    ("MISSING", "commit"): "confirmed_missing",
    ("MISSING", "reveal"): "confirmed_missing",
    ("LATE", "commit"): "confirmed_late",
    ("LATE", "reveal"): "confirmed_late",
    ("EARLY", "commit"): "confirmed_incorrect",
    ("EARLY", "reveal"): "confirmed_incorrect",
    ("MISMATCH", "outputs_differ"): "confirmed_incorrect",
    ("MALFORMED", "commit_target_or_signer"): "confirmed_malformed_submission",
    ("MALFORMED", "reveal_target_or_signer"): "confirmed_malformed_submission",
    ("MALFORMED", "payload_encoding"): "confirmed_malformed_submission",
    ("MALFORMED", "commitment_hash"): "confirmed_malformed_submission",
    ("MALFORMED", "payload_context"): "confirmed_malformed_submission",
    ("INELIGIBLE", "not_registered_at_b"): "confirmed_ineligible",
    ("INELIGIBLE", "no_qualifying_cited_deposit"): "confirmed_ineligible",
    ("INELIGIBLE", "deposit_does_not_qualify"): "confirmed_ineligible",
}

# Validator-side gaps. The pay weight is left as the engine computed it.
UNRESOLVED_REASONS = {
    ("UNVERIFIABLE", "snapshot_context"): "infrastructure",
    ("UNVERIFIABLE", "submission_collection_incomplete"): "infrastructure",
    ("UNVERIFIABLE", "commit_chain_evidence"): "infrastructure",
    ("UNVERIFIABLE", "reveal_chain_evidence"): "infrastructure",
    ("UNVERIFIABLE", "eligibility_history_incomplete"): "infrastructure",
    ("UNVERIFIABLE", "malformed_history_receipt"): "infrastructure",
    ("UNVERIFIABLE", "ambiguous_deposit_reference"): "infrastructure",
    ("UNVERIFIABLE", "calculation_context"): "infrastructure",
    ("UNVERIFIABLE", "calculation_output"): "infrastructure",
    ("UNVERIFIABLE", "threshold_resolution"): "infrastructure",
    ("UNVERIFIABLE", "finality_not_proved"): "infrastructure",
    ("UNVERIFIABLE", "malformed_collector_evidence"): "infrastructure",
    ("UNVERIFIABLE", "evidence_not_canonical_json"): "infrastructure",
    ("UNVERIFIABLE", "conflicting_completed_evidence"): "evidence_conflict",
    ("UNVERIFIABLE", "conflicting_incomplete_evidence"): "evidence_conflict",
}

EMPTY_VECTOR_NOTE = (
    "An empty weightsU16 declines to publish a new vector. "
    "It does not erase weights already on chain."
)


def eval_day_for_close(reveal_close_ms: int, day_anchor_ts: int) -> int:
    """The scoring ``eval_day`` that contains ``reveal_close``.

    ``day_anchor_ts`` is genesis floored to UTC midnight. Day ``n`` is
    ``[anchor + n * 86400, anchor + (n + 1) * 86400)`` in unix seconds.
    """
    if type(reveal_close_ms) is not int or type(day_anchor_ts) is not int:
        raise ValueError("reveal_close_ms and day_anchor_ts must be integers")
    if reveal_close_ms < 0 or day_anchor_ts < 0:
        raise ValueError("reveal_close_ms and day_anchor_ts must be non-negative")
    close_unix = reveal_close_ms // 1000
    if close_unix < day_anchor_ts:
        raise ValueError("reveal_close is before day_anchor_ts")
    return (close_unix - day_anchor_ts) // DAY_SECONDS


def apply_backing_gate(output: dict, bundle, *, enabled: bool) -> dict:
    """Return ``output`` unchanged when the gate is off.

    When it is on, ``payWeightFp`` stays the raw engine value and
    ``effectivePayWeightFp`` is what ``weightsU16`` uses.
    """
    if not enabled:
        return output
    if not isinstance(output, dict) or not isinstance(output.get("miners"), list):
        raise ValueError("score output has no miner list")
    gated = json.loads(json.dumps(output))
    eval_day = int(gated["evalDay"])
    effective_by_uid = {}
    for miner in gated["miners"]:
        raw = int(miner["payWeightFp"])
        decision = _miner_decision(miner, eval_day, bundle)
        effective = 0 if decision["effect"] == "zero" else raw
        miner["payWeightFp"] = str(raw)
        miner["effectivePayWeightFp"] = str(effective)
        miner["backingGate"] = decision
        if miner.get("uid") is not None:
            effective_by_uid[int(miner["uid"])] = effective
    weights = weights_u16(effective_by_uid)
    gated["weightsU16"] = {str(uid): weight for uid, weight in weights.items()}
    gated["backingGate"] = {
        "allZero": bool(effective_by_uid) and all(value == 0 for value in effective_by_uid.values()),
        "declinedNewVector": weights == {},
        "enabled": True,
        "inputs": bundle.get("inputs") if isinstance(bundle, dict) else None,
        "note": EMPTY_VECTOR_NOTE,
    }
    return gated


def _miner_decision(miner, eval_day: int, bundle) -> dict:
    rounds = _rounds_for_day(eval_day, bundle)
    if isinstance(rounds, dict):
        return rounds
    rows = [_round_decision(miner, eval_day, bundle, one) for one in rounds]
    required = [row for row in rows if row["outcome"] != "not_required"]
    if not required:
        return {"effect": "preserve", "outcome": "not_required", "rounds": rows}
    if any(row["effect"] == "zero" for row in required):
        effect, outcome = "zero", "confirmed_failure"
    elif any(row["effect"] == "preserve" and row["outcome"] != "verified" for row in required):
        effect, outcome = "preserve", "unresolved"
    else:
        effect, outcome = "preserve", "verified"
    return {"effect": effect, "outcome": outcome, "rounds": required}


def _rounds_for_day(eval_day: int, bundle):
    if not isinstance(bundle, dict) or not isinstance(bundle.get("rounds"), list) or not bundle["rounds"]:
        return _exception("required_rounds_absent")
    if type(bundle.get("day_anchor_ts")) is not int or type(bundle.get("evaluated_at_ms")) is not int:
        return _exception("evaluation_clock_absent")
    if bundle.get("genesis_hash") is None or type(bundle.get("netuid")) is not int:
        return _exception("network_absent")
    selected = []
    for contract in bundle["rounds"]:
        if not isinstance(contract, dict) or type(contract.get("reveal_close_ms")) is not int:
            return _exception("required_round_malformed")
        try:
            day = eval_day_for_close(contract["reveal_close_ms"], bundle["day_anchor_ts"])
        except ValueError:
            return _exception("required_round_malformed")
        if day == eval_day:
            selected.append(contract)
    if not selected:
        return _exception("no_required_round_this_day")
    return selected


def _round_decision(miner, eval_day: int, bundle, contract) -> dict:
    digest = _digest(contract)
    registration = _registration_day(bundle, miner.get("uid"))
    registered_at = _registration_block(bundle, miner.get("uid"), contract.get("block_number"))
    snapshot_block = contract.get("block_number")
    if registered_at is not None and type(snapshot_block) is int and registered_at > snapshot_block:
        return _row(digest, "preserve", "not_required", "registered_after_snapshot", None, None)
    if registration is not None and eval_day < registration:
        return _row(digest, "preserve", "not_required", "not_required", None, None)
    if bundle["evaluated_at_ms"] < contract["reveal_close_ms"]:
        return _row(digest, "preserve", "unresolved", "windows_still_open", None, None)
    if contract.get("genesis_hash") != bundle["genesis_hash"] or contract.get("mining_netuid") != bundle["netuid"]:
        return _row(digest, "preserve", "unresolved", "round_network_mismatch", None, None)
    uid = miner.get("uid")
    seats = bundle.get("seats") if isinstance(bundle.get("seats"), dict) else {}
    hotkey = seats.get(uid, seats.get(str(uid)))
    if uid is None or not isinstance(hotkey, str):
        return _row(digest, "preserve", "unresolved", "identity_unbound", None, None)
    verdicts = bundle.get("verdicts") if isinstance(bundle.get("verdicts"), dict) else {}
    per_round = verdicts.get(digest)
    record = per_round.get(hotkey) if isinstance(per_round, dict) else None
    if not isinstance(record, dict):
        return _row(digest, "preserve", "unresolved", "verdict_absent", None, None)
    if not _bound(record, digest, uid, hotkey, bundle, eval_day):
        return _row(digest, "preserve", "unresolved", "evidence_mismatch", record.get("status"), record.get("reason"))
    status, reason = record.get("status"), record.get("reason")
    key = (status, reason)
    if key in PASS_REASONS:
        return _row(digest, "preserve", "verified", PASS_REASONS[key], status, reason)
    if key in FAILURE_REASONS:
        return _row(digest, "zero", "confirmed_failure", FAILURE_REASONS[key], status, reason)
    if key in UNRESOLVED_REASONS:
        return _row(digest, "preserve", "unresolved", UNRESOLVED_REASONS[key], status, reason)
    return _row(digest, "preserve", "unresolved", "unknown_checker_result", status, reason)


def _bound(record, digest, uid, hotkey, bundle, eval_day) -> bool:
    return (
        record.get("round_sha256") == digest
        and record.get("uid") == uid
        and record.get("hotkey") == hotkey
        and record.get("genesis_hash") == bundle["genesis_hash"]
        and record.get("netuid") == bundle["netuid"]
        and record.get("eval_day") == eval_day
    )


def _registration_block(bundle, uid, snapshot_block):
    """Trusted ``BlockAtRegistration`` for this round's snapshot block."""
    table = bundle.get("registration_blocks") if isinstance(bundle, dict) else None
    if not isinstance(table, dict) or uid is None:
        return None
    bucket = table.get(snapshot_block, table.get(str(snapshot_block)))
    if not isinstance(bucket, dict):
        return None
    value = bucket.get(uid, bucket.get(str(uid)))
    return value if type(value) is int else None


def _registration_day(bundle, uid):
    participation = bundle.get("participation") if isinstance(bundle, dict) else None
    if not isinstance(participation, dict) or uid is None:
        return None
    value = participation.get(uid, participation.get(str(uid)))
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if type(value) is int else None


def _exception(reason: str) -> dict:
    return {"effect": "preserve", "outcome": "unresolved", "rounds": [_row(None, "preserve", "unresolved", reason, None, None)]}


def _row(digest, effect, outcome, classification, status, reason) -> dict:
    return {
        "classification": classification,
        "effect": effect,
        "outcome": outcome,
        "reason": reason,
        "round_sha256": digest,
        "status": status,
    }


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
