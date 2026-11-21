"""Offline shadow verifier for trusted fixture evidence, never a reward publisher.

The future collector must authenticate headers, call targets, coverage and attribution.
This module cannot establish chain authenticity from arbitrary JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from datetime import date, datetime, timezone
from .backing.protocol.preimage import REPORT_POLICY, REPORT_DOMAIN, REPORT_FORMAT
from pathlib import Path

PAYLOAD = struct.Struct("<11s32s32s32sHII32sIHIQQ16s16s")
CALCULATION = "76f629a3933967170229a732c4b02740be22e5eb"
ELIGIBILITY = "attributed-deposit-before-b-v1"


def _integer(value, low=0, high=2**64 - 1):
    return type(value) is int and low <= value <= high


def _hex(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_round(r):
    """Reject unsupported/operator-invalid configuration; no production defaults."""
    required = {
        "schema",
        "genesis_hash",
        "mining_netuid",
        "reported_netuid",
        "round_date",
        "block_number",
        "block_hash",
        "block_timestamp_ms",
        "commit_open_ms",
        "commit_close_ms",
        "reveal_open_ms",
        "reveal_close_ms",
        "eligibility_policy_version",
        "scoring_release",
        "release_prefix",
        "calculation_version",
        "ark_coldkey",
        "ark_validator_hotkey",
    }
    if not isinstance(r, dict) or set(r) not in (required, required | {"commitment_format"}):
        raise ValueError("round fields differ from noah.backing.round.v1")
    if r["schema"] != "noah.backing.round.v1" or type(r["mining_netuid"]) is not int:
        raise ValueError("unsupported round schema or mining subnet")
    if not all(_integer(r[field], 1, 65535) for field in ("mining_netuid", "reported_netuid")):
        raise ValueError("mining and reported subnets must be non-root u16 identifiers")
    if not _integer(r["block_number"], 1, 2**32 - 1):
        raise ValueError("invalid finalized block number")
    for key in ("genesis_hash", "block_hash", "ark_coldkey", "ark_validator_hotkey"):
        if not _hex(r[key]):
            raise ValueError("invalid round hash or account ID")
    if (
        not _hex(r["release_prefix"], 32)
        or not isinstance(r["scoring_release"], str)
        or not r["scoring_release"]
    ):
        raise ValueError("release identity required")
    if r["calculation_version"] != CALCULATION or r["eligibility_policy_version"] not in (ELIGIBILITY, REPORT_POLICY):
        raise ValueError("unqualified calculation or eligibility version")
    if "commitment_format" in r and (r["commitment_format"] != "context-v1"
            or r["eligibility_policy_version"] != REPORT_POLICY):
        raise ValueError("unsupported commitment format or policy")
    try:
        day = date.fromisoformat(r["round_date"])
        if day.isoformat() != r["round_date"]:
            raise ValueError("noncanonical date")
    except (ValueError, TypeError):
        raise ValueError("round date must be YYYY-MM-DD") from None
    times = [
        r[k]
        for k in (
            "block_timestamp_ms",
            "commit_open_ms",
            "commit_close_ms",
            "reveal_open_ms",
            "reveal_close_ms",
        )
    ]
    if not all(_integer(t, 1, 2**53 - 1) for t in times):
        raise ValueError("invalid UTC millisecond timestamp")
    b, co, cc, ro, rc = times
    if not b <= co < cc <= ro < rc:
        raise ValueError("round windows overlap or precede B")
    if datetime.fromtimestamp(b / 1000, timezone.utc).date() != day:
        raise ValueError("B must belong to round UTC date")


def _complete(c, r):
    return (
        isinstance(c, dict)
        and c.get("state") == "COMPLETE"
        and c.get("genesis_hash") == r["genesis_hash"]
        and c.get("round_block_hash") == r["block_hash"]
        and c.get("gaps") == []
        and c.get("errors") == []
    )


def _decode(raw):
    if len(raw) == REPORT_FORMAT.size and raw.startswith(REPORT_DOMAIN):
        from dataclasses import asdict
        from .backing.protocol.preimage import decode
        values = asdict(decode(raw))
        values['reported_netuid'] = values.pop('netuid')
        values['domain'] = REPORT_DOMAIN
        return {k: v.hex() if isinstance(v, bytes) else v for k, v in values.items()}
    if len(raw) != 207:
        raise ValueError("payload length")
    values = PAYLOAD.unpack(raw)
    if values[0] != b"bfpoc-s2-v2":
        raise ValueError("payload domain")
    names = (
        "domain",
        "genesis_hash",
        "hotkey",
        "coldkey",
        "reported_netuid",
        "round_yyyymmdd",
        "block_number",
        "block_hash",
        "deposit_block",
        "deposit_event",
        "out1_ppm",
        "out2_rao",
        "out3_rao",
        "release_prefix",
        "salt",
    )
    return {k: v.hex() if isinstance(v, bytes) else v for k, v in zip(names, values)}


def _decide(r, e):
    snapshot = e.get("snapshot", {})
    if (
        snapshot.get("finalized") is not True
        or any(snapshot.get(k) != r[k] for k in ("genesis_hash", "block_number", "block_hash"))
        or snapshot.get("timestamp_ms") != r["block_timestamp_ms"]
        or not _hex(snapshot.get("hotkey"))
        or not _hex(snapshot.get("coldkey"))
        or type(snapshot.get("registered")) is not bool
    ):
        return "UNVERIFIABLE", "snapshot_context"
    c = e.get("collection", {})
    if (
        not _complete(c, r)
        or not _integer(c.get("from_ms"))
        or c["from_ms"] > r["commit_open_ms"]
        or not _integer(c.get("through_ms"))
        or c["through_ms"] < r["reveal_close_ms"]
    ):
        return "UNVERIFIABLE", "submission_collection_incomplete"

    # A missing or unusable reference calculation is a validator-side gap.
    # Do not penalize missing/invalid submissions when miners cannot prepare
    # a complete report for this round.
    calc = e.get("calculation", {})
    if (
        calc.get("status") != "OK"
        or any(
            calc.get(k) != r[k]
            for k in (
                "genesis_hash",
                "block_number",
                "block_hash",
                "calculation_version",
                "ark_coldkey",
            )
        )
        or calc.get("netuid") != r["reported_netuid"]
        or calc.get("coverage", {}).get("coverage_gap_exceeds_holder") is not False
    ):
        return "UNVERIFIABLE", "calculation_context"
    outputs = []
    for key, field, maximum in (
        ("output1", "drop_ppm", 1_000_000),
        ("output2", "alpha_required_rao", 2**64 - 1),
        ("output3", "alpha_required_rao", 2**64 - 1),
    ):
        row = calc.get(key, {})
        if row.get("status") != "OK" or not _integer(row.get(field), 0, maximum):
            return "UNVERIFIABLE", "calculation_output"
        outputs.append(row[field])
    if any(v <= 0 or v % 1_000_000 for v in outputs[1:]) or outputs[2] < outputs[1]:
        return "UNVERIFIABLE", "threshold_resolution"

    # The pair is already selected. The fixture supplies it. The collector selects
    # one commitment and one matching reveal per miner before calling this.
    for leg in ("commit", "reveal"):
        tx = e.get(leg)
        if tx is None:
            return "MISSING", leg
        if (
            not isinstance(tx.get("data_hex"), str)
            or tx.get("finalized") is not True
            or tx.get("successful") is not True
            or tx.get("genesis_hash") != r["genesis_hash"]
            or not _hex(tx.get("block_hash"))
            or not _integer(tx.get("block_number"), r["block_number"], 2**32 - 1)
            or not _integer(tx.get("extrinsic_index"))
            or not _integer(tx.get("timestamp_ms"), r["block_timestamp_ms"])
            or not _integer(tx.get("target_netuid"), 0, 65535)
            or not isinstance(tx.get("call"), str)
            or not tx["call"]
            or not _hex(tx.get("signer"))
        ):
            return "UNVERIFIABLE", leg + "_chain_evidence"
        if (
            tx["target_netuid"] != r["mining_netuid"]
            or tx["call"] != "Commitments.set_commitment"
            or tx["signer"] != snapshot["hotkey"]
        ):
            return "MALFORMED", leg + "_target_or_signer"
        if tx["timestamp_ms"] < r[leg + "_open_ms"]:
            return "EARLY", leg
        if tx["timestamp_ms"] >= r[leg + "_close_ms"]:
            return "LATE", leg

    try:
        raw = bytes.fromhex(e["reveal"]["data_hex"])
        p = _decode(raw)
        commitment = bytes.fromhex(e["commit"]["data_hex"])
    except (ValueError, TypeError, KeyError):
        return "MALFORMED", "payload_encoding"
    if r.get("commitment_format") == "context-v1":
        from .backing.protocol.context_commitment import matches_context, matches_reveal
        try:
            if not matches_context(commitment, r):
                return "MALFORMED", "commitment_context"
            if not matches_reveal(commitment, raw):
                return "MALFORMED", "commitment_hash"
        except ValueError:
            return "MALFORMED", "commitment_encoding"
    elif hashlib.sha256(raw).digest() != commitment:
        return "MALFORMED", "commitment_hash"
    report_only = r["eligibility_policy_version"] == REPORT_POLICY
    if (p["domain"] == REPORT_DOMAIN.hex()) != report_only:
        return "MALFORMED", "payload_context"
    if report_only and p["mining_netuid"] != r["mining_netuid"]:
        return "MALFORMED", "payload_context"
    if not snapshot["registered"]:
        return "INELIGIBLE", "not_registered_at_b"
    if (
        any(
            p[k] != r[k]
            for k in (
                "genesis_hash",
                "reported_netuid",
                "block_number",
                "block_hash",
                "release_prefix",
            )
        )
        or p["round_yyyymmdd"] != int(r["round_date"].replace("-", ""))
        or p["hotkey"] != snapshot["hotkey"]
        or p["coldkey"] != snapshot["coldkey"]
    ):
        return "MALFORMED", "payload_context"

    if report_only:
        if outputs != [p["out1_ppm"], p["out2_rao"], p["out3_rao"]]:
            return "MISMATCH", "outputs_differ"
        return "VERIFIED", "exact_match"

    history = e.get("deposit_history", {})
    if (
        not _complete(history, r)
        or type(history.get("from_block")) is not int
        or history["from_block"] != 0
        or not _integer(history.get("through_block"), r["block_number"] - 1)
        or history.get("eligibility_policy_version") != r["eligibility_policy_version"]
        or history.get("ledger_kind") != ("native" if r["reported_netuid"] == r["mining_netuid"] else "foreign")
        or not isinstance(history.get("receipts"), list)
    ):
        return "UNVERIFIABLE", "eligibility_history_incomplete"
    receipts = history["receipts"]
    if any(
        not isinstance(row, dict)
        or not _integer(row.get("block"), 0, 2**32 - 1)
        or not _integer(row.get("event_index"), 0, 65535)
        or not _integer(row.get("amount_rao"))
        or not _integer(row.get("token_netuid"), 0, 65535)
        or "unattributed_reason" not in row
        or (
            row["unattributed_reason"] is not None
            and not isinstance(row["unattributed_reason"], str)
        )
        or any(
            not _hex(row.get(k))
            for k in (
                "origin_hotkey",
                "owner_at_deposit",
                "coldkey",
                "destination_coldkey",
                "destination_hotkey",
            )
        )
        for row in receipts
    ):
        return "UNVERIFIABLE", "malformed_history_receipt"
    matches = [
        d
        for d in receipts
        if (d["block"], d["event_index"]) == (p["deposit_block"], p["deposit_event"])
    ]
    if len(matches) > 1:
        return "UNVERIFIABLE", "ambiguous_deposit_reference"
    if not matches:
        return "INELIGIBLE", "no_qualifying_cited_deposit"
    d = matches[0]
    if (
        d["block"] >= r["block_number"]
        or not _integer(d.get("amount_rao"), 1)
        or d.get("token_netuid") != r["reported_netuid"]
        or d.get("unattributed_reason", "missing") is not None
        or d.get("origin_hotkey") != p["hotkey"]
        or d.get("coldkey") != p["coldkey"]
        or d.get("owner_at_deposit") != p["coldkey"]
        or d.get("destination_coldkey") != r["ark_coldkey"]
        or d.get("destination_hotkey") != r["ark_validator_hotkey"]
    ):
        return "INELIGIBLE", "deposit_does_not_qualify"

    if outputs != [p["out1_ppm"], p["out2_rao"], p["out3_rao"]]:
        return "MISMATCH", "outputs_differ"
    return "VERIFIED", "exact_match"


def verify_collected(round_contract, evidence):
    """Verify evidence produced by the collector. Not the fixture path.

    ``fixture_only`` evidence is rejected here. ``verify_shadow`` remains the
    synthetic fixture entry. Miner sidecar fields are not read. The collector
    selects one commitment and one reveal per miner. Other attempts in
    ``candidates`` are diagnostics and do not by themselves make the result
    ``UNVERIFIABLE``.
    """
    validate_round(round_contract)
    if not isinstance(evidence, dict):
        raise ValueError("collected evidence is not an object")
    if evidence.get("fixture_only") is True:
        raise ValueError("collected evidence must not be labeled fixture_only")
    if evidence.get("collector") != "noah.reports.backing.collector":
        raise ValueError("evidence was not produced by the collector")
    if not isinstance(evidence.get("collection"), dict):
        raise ValueError("missing collector collection record")
    try:
        evidence_sha256 = _digest(evidence)
    except (TypeError, ValueError, RecursionError):
        evidence_sha256 = None
        status, reason = "UNVERIFIABLE", "evidence_not_canonical_json"
    else:
        status, reason = _collected_decision(round_contract, evidence)
    return {
        "schema": "noah.backing.shadow-verdict.v1",
        "mode": "SHADOW",
        "status": status,
        "reason": reason,
        "round_sha256": _digest(round_contract),
        "evidence_sha256": evidence_sha256,
    }


def _collected_decision(round_contract, evidence):
    finality = evidence.get("finality")
    if isinstance(finality, dict):
        for leg in ("commit", "reveal"):
            proof = finality.get(leg)
            if isinstance(proof, dict) and proof.get("matches") is not True:
                return "UNVERIFIABLE", "finality_not_proved"
    candidates = evidence.get("candidates")
    if not isinstance(candidates, dict):
        return "UNVERIFIABLE", "malformed_collector_evidence"
    commits = candidates.get("commits")
    reveals = candidates.get("reveals")
    if not isinstance(commits, list) or not isinstance(reveals, list):
        return "UNVERIFIABLE", "malformed_collector_evidence"
    if commits is None or reveals is None:
        return "UNVERIFIABLE", "malformed_collector_evidence"
    try:
        return _decide(round_contract, evidence)
    except (KeyError, TypeError, ValueError, AttributeError):
        return "UNVERIFIABLE", "malformed_collector_evidence"


def verify_shadow(round_contract, evidence):
    """Consume collector-owned synthetic facts; never trust a miner sidecar as facts.

    No RPC, signing, storage mutation, points, reward arithmetic or publication.
    Invalid operator configuration raises; malformed reference evidence is unavailable.
    """
    validate_round(round_contract)
    if not isinstance(evidence, dict) or evidence.get("fixture_only") is not True:
        raise ValueError("this slice only accepts explicitly synthetic fixture evidence")
    try:
        evidence_sha256 = _digest(evidence)
    except (TypeError, ValueError, RecursionError):
        # Do not hash a lossy replacement or let an unhashable record verify.
        evidence_sha256 = None
        status, reason = "UNVERIFIABLE", "evidence_not_canonical_json"
    else:
        try:
            status, reason = _decide(round_contract, evidence)
        except (KeyError, TypeError, ValueError, AttributeError):
            status, reason = "UNVERIFIABLE", "malformed_collector_evidence"
    return {
        "schema": "noah.backing.shadow-verdict.v1",
        "mode": "SHADOW",
        "status": status,
        "reason": reason,
        "round_sha256": _digest(round_contract),
        "evidence_sha256": evidence_sha256,
    }


def main():
    parser = argparse.ArgumentParser(description="Replay synthetic backing-report evidence only.")
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    print(json.dumps(verify_shadow(fixture["round"], fixture["evidence"]), sort_keys=True))


if __name__ == "__main__":
    main()
