"""Unsigned report collection, reconstruction and reward previews.

These functions are the shared command/scheduler API. None publishes weights.
Stored verdict JSON is display only; score previews reconstruct raw evidence.
"""

import json
from pathlib import Path
import tempfile

from .common import ReportError, digest, immutable_json, locked, read_json


def collect_report(contract, endpoints, hotkeys, directory, *, history=None, allow_genesis_scan=False):
    from noah.reports.backing.collector import collect
    from noah.reports.backing.history import segments_problem

    if not isinstance(hotkeys, list) or not hotkeys or any(not isinstance(x, str) for x in hotkeys):
        raise ReportError("hotkeys must be a nonempty list of miner public addresses")
    checkpoint = None
    from .backing.protocol.preimage import REPORT_POLICY
    report_only = contract["eligibility_policy_version"] == REPORT_POLICY
    if report_only and (history is not None or allow_genesis_scan):
        raise ReportError("report-only validation is separate from deposit history")
    if history:
        checkpoint = read_json(Path(history) / "checkpoint.json")
        problem = segments_problem(checkpoint, Path(history))
        if problem:
            raise ReportError(problem)
        if not isinstance(checkpoint.get("segments"), list):
            raise ReportError("use a segmented history checkpoint from reports history")
        if checkpoint["through_block"] != contract["block_number"] - 1:
            raise ReportError("bootstrap must end at B-1 for a self-contained round replay")
    elif not report_only and not allow_genesis_scan:
        raise ReportError("provide --history, or explicitly allow a genesis scan for a small localnet")
    with locked(directory):
        result = collect(contract, endpoints, Path(directory), hotkeys,
                         history_checkpoint=checkpoint, history_dir=history)
    return {"package": result["directory"], "round_sha256": digest(contract),
            "publishable": False, "source_trust": "RPC endpoint; not independent-validator agreement"}


def verify_report(contract, package, *, include_evidence=False):
    from noah.reports.backing.offline import verify_package
    package = Path(package)
    if read_json(package / "round.json") != contract:
        raise ReportError("retained package differs from the operator's trusted round")
    result = verify_package(package, include_evidence=include_evidence)
    return {**result, "round_sha256": digest(contract), "publishable": False,
            "source_trust": "reconstruction of retained RPC responses; not independent agreement"}


def collection_tick(contract, endpoints, hotkeys, directory, *, history=None, allow_genesis_scan=False):
    """One scheduler operation; creates evidence then reconstructs it offline."""
    result = collect_report(contract, endpoints, hotkeys, directory, history=history,
                            allow_genesis_scan=allow_genesis_scan)
    verification = verify_report(contract, result["package"])
    immutable_json(Path(result["package"]) / "verification.json", verification)
    return {**result, "verification": verification}


def status(directory):
    """Display retained results; never accepted as scoring evidence."""
    root = Path(directory)
    if not root.is_dir():
        raise ReportError("evidence directory does not exist")
    rows = []
    for path in sorted(root.rglob("round.json")):
        package = path.parent
        if not (package / "evidence.json").is_file():
            continue
        saved = package / "verification.json"
        rows.append({"package": str(package), "round_sha256": digest(read_json(path)),
                     "verification": read_json(saved) if saved.exists() else None})
    return {"packages": rows, "publishable": False, "notice": "saved display only; verify reconstructs inputs"}


def history_tick(contract, endpoint, directory, end, *, client=None):
    from noah.reports.backing.bootstrap import extend_history, status_history
    from noah.reports.backing.history import checkpoint_problem
    from .backing.sdk_client import live_sdk
    if type(end) is not int or end < 0 or end >= contract["block_number"]:
        raise ReportError("history end must be between zero and B-1")
    with locked(directory):
        saved = Path(directory) / "checkpoint.json"
        start = 0
        if saved.exists():
            prior = status_history(Path(directory))
            if prior.get("error"):
                raise ReportError("saved history checkpoint failed validation")
            checkpoint = read_json(saved)
            problem = checkpoint_problem(checkpoint,
                genesis_hash=contract["genesis_hash"], responses_sha256=checkpoint["responses_sha256"],
                ark_coldkey=contract["ark_coldkey"], ark_validator_hotkey=contract["ark_validator_hotkey"],
                eligibility_policy_version=contract["eligibility_policy_version"])
            if problem:
                raise ReportError(problem)
            start = prior["through_block"] + 1
        if start > end:
            return {"advanced": False, "through_block": start - 1, "status": "already_covered"}
        owned = client is None
        client = client or live_sdk(endpoint)
        try:
            if str(client.get_block_hash(0)).removeprefix("0x") != contract["genesis_hash"]:
                raise ReportError("history endpoint has the wrong genesis")
            return extend_history(client, Path(directory), start, end,
                                  ark_coldkey=contract["ark_coldkey"],
                                  ark_validator_hotkey=contract["ark_validator_hotkey"],
                                  eligibility_policy_version=contract["eligibility_policy_version"])
        finally:
            if owned:
                client.close()


def score_preview(fixture, context, rounds, evidence_root, *, enabled=False):
    """Twin-check the existing scorer, then preview the optional report gate.

    Caller-supplied fixture/context are explicitly diagnostic. Production must
    supply identities from its trusted chain reader, not reuse this JSON output.
    """
    from noah.scoring.fixture_io import parse_fixture, render_output
    from noah.scoring.engine import score_fixture
    from noah.reports.gate import apply_backing_gate
    from noah.reports.inputs import load_backing_snapshot
    from noah.core.twin import require_agreement
    from noah.core.ts_engine import render_with_ts_engine
    from noah.reports.verification import validate_round

    if fixture.get("schema", "").startswith("noah-points-fixture/"):
        from noah.points.run import agreed
        original = agreed(fixture)
    else:
        text = require_agreement(fixture,
            first=lambda raw: render_output(score_fixture(parse_fixture(raw))),
            second=render_with_ts_engine)
        original = json.loads(text)
    if not enabled:
        return {"publishable": False, "gate_enabled": False, "original": original, "effective": original}
    if not isinstance(rounds, list) or not rounds:
        raise ReportError("a nonempty trusted round list is required")
    expected = {}
    for contract in rounds:
        validate_round(contract)
        expected[digest(contract)] = contract
    # Reconstruct packages into a temporary input set. Never gate on a saved
    # evidence.json/verdict without replaying the underlying responses.
    with tempfile.TemporaryDirectory(prefix="noah-report-preview-") as tmp:
        root = Path(tmp)
        immutable_json(root / "rounds.json", rounds)
        evidence = root / "evidence"
        evidence.mkdir()
        for index, path in enumerate(sorted(Path(evidence_root).rglob("round.json"))):
            contract = read_json(path)
            if digest(contract) not in expected:
                continue
            report = verify_report(contract, path.parent, include_evidence=True)
            if report["status"] != "RECONSTRUCTED":
                # No miner failure may be inferred from corrupt or missing
                # validator inputs. The absent verdict is an explicit exception.
                continue
            folder = evidence / str(index)
            immutable_json(folder / "round.json", contract)
            immutable_json(folder / "evidence.json", {"evidence": report["evidence"]})
        if int(fixture["dayEpochTs"]) != context["day_anchor_ts"]:
            raise ReportError("fixture and report context have different day anchors")
        snapshot = load_backing_snapshot(root / "rounds.json", evidence,
            genesis_hash=context["genesis_hash"], netuid=context["netuid"],
            day_anchor_ts=context["day_anchor_ts"], evaluated_at_ms=context["evaluated_at_ms"],
            seats=context["seats"], participation={str(m["uid"]): int(m["registrationDay"]) for m in fixture["miners"]},
            registration_blocks=context["registration_blocks"])
        effective = apply_backing_gate(original, snapshot, enabled=True)
    return {"publishable": False, "gate_enabled": True, "original": original, "effective": effective,
            "notice": "diagnostic inputs; report gate is not connected to live publication"}
