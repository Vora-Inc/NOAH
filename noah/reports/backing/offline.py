"""Reconstruct one round from retained responses. No second copy of the rules.

The retained package is what this validator collected. Replaying it shows
whether those bytes still produce the same verdict. It does not show that
the endpoint told the truth. A hash stored next to a response does not
survive someone replacing both.
"""

from __future__ import annotations

import gzip
import json
import socket
from pathlib import Path

from substrateinterface import SubstrateInterface
from substrateinterface.exceptions import SubstrateRequestException

from .collector import _collect
from .history import segments_problem
from .round_config import require_round
from .rpc import RunFailure, canonical_result, result_sha256
from .source import ReplayError, ReplaySource, load_receipts

REQUIRED = ("responses.jsonl.gz", "round.json", "hotkeys.json", "receipts.jsonl.gz", "metadata.hex")


class EvidenceError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def disable_network() -> None:
    """Make a later connect call fail. Used by the offline command only."""

    def blocked(*_args, **_kwargs):
        raise OSError("offline verification blocked a network connection")

    socket.socket.connect = blocked
    socket.create_connection = blocked


def verify_package(path: Path, saved: dict | None = None, *, include_evidence: bool = False) -> dict:
    """Rebuild verdicts. ``saved`` is compared afterwards and is not an input."""
    directory = Path(path)
    missing = [name for name in REQUIRED if not (directory / name).is_file()]
    if missing:
        report = _unavailable(missing)
        report["comparison"] = _compare(report["verdicts"], saved)
        return report
    history_checkpoint, history_dir, problem = _history_inputs(directory)
    if problem is not None:
        report = _evidence_failure(problem)
        report["comparison"] = _compare(report["verdicts"], saved)
        return report
    try:
        rows = _load_responses(directory / "responses.jsonl.gz")
        receipts = load_receipts(str(directory / "receipts.jsonl.gz"))
        for row in receipts:
            _check_integrity(row)
        round_contract = json.loads((directory / "round.json").read_text())
        require_round(round_contract)
        hotkeys = json.loads((directory / "hotkeys.json").read_text())
        if not isinstance(hotkeys, list) or not hotkeys:
            raise EvidenceError("hotkeys_unavailable")
        _check_binding(round_contract, rows)
        index = _index(rows)
        evaluation_settings = _evaluation_inputs(directory)
        substrate = SubstrateInterface(websocket=_ReplaySocket(index), auto_discover=False)
        substrate.init_runtime()
        pin = "0x" + round_contract["block_hash"]
        from noah.reports.artifacts import retained_coverage_method
        coverage_method = retained_coverage_method(directory, round_contract)
        result = _collect(
            substrate,
            "retained-package",
            [],
            round_contract,
            None,
            hotkeys,
            lambda: 0,
            chain_source=ReplaySource(receipts, pin),
            replay_coverage_method=coverage_method,
            history_checkpoint=history_checkpoint,
            history_dir=history_dir,
            evaluation=evaluation_settings["evaluation"] if evaluation_settings else None,
            evaluation_ancestor_limit=evaluation_settings["max_ancestor_distance"] if evaluation_settings else None,
        )
    except (EvidenceError, ReplayError, RunFailure, SubstrateRequestException, ValueError, OSError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        report = _evidence_failure(str(reason))
        report["comparison"] = _compare(report["verdicts"], saved)
        return report
    return report_from_result(directory, round_contract, result, evaluation_settings,
                              saved=saved, include_evidence=include_evidence)


def report_from_result(directory, round_contract, result, evaluation_settings, *, saved=None, include_evidence=False):
    """Compare retained artifacts only after independently replaying raw inputs."""
    from noah.reports.artifacts import check_retained_engine, engine_binding
    retained, engine_problem = check_retained_engine(directory, round_contract, result["engine"])
    if engine_problem:
        return _evidence_failure(engine_problem)
    if evaluation_settings is not None:
        from noah.reports.artifacts import canonical_bytes
        try:
            for name, expected in (("evaluation.json", evaluation_settings),
                                   ("evaluation-proof.json", result["evaluation_proof"])):
                encoded = canonical_bytes(expected)
                with (directory / name).open("rb") as handle:
                    if handle.read(len(encoded) + 1) != encoded:
                        return _evidence_failure("retained_evaluation_mismatch")
        except OSError:
            return _evidence_failure("retained_evaluation_unavailable")
    verdicts = {}
    for key, evidence in result["evidence"].items():
        if not isinstance(evidence, dict) or evidence.get("fixture_only") is True:
            return _evidence_failure("fixture_only")
        from noah.reports.verification import verify_collected

        verdict = verify_collected(round_contract, evidence)
        verdicts[key] = {"reason": verdict["reason"], "status": verdict["status"]}
    return {
        "comparison": _compare(verdicts, saved),
        **({"evidence": result["evidence"]} if include_evidence else {}),
        **({"engine": result["engine"]} if include_evidence else {}),
        "engine_retained": retained,
        "engine_binding": engine_binding(round_contract, result["engine"]),
        "evaluation_proof": result["evaluation_proof"],
        "reason": "reconstructed",
        "status": "RECONSTRUCTED",
        "unavailable": [],
        "verdicts": verdicts,
    }


def _evaluation_inputs(directory):
    from noah.reports.adjudication_contract import fields, strict_json
    settings, proof = directory / "evaluation.json", directory / "evaluation-proof.json"
    if settings.is_symlink() or proof.is_symlink():
        raise EvidenceError("retained_evaluation_symlink")
    if not settings.exists() and not proof.exists():
        return None
    if not settings.is_file() or not proof.is_file():
        raise EvidenceError("retained_evaluation_unavailable")
    with settings.open("rb") as handle:
        document = strict_json(handle.read(4097))
    if settings.stat().st_size > 4096:
        raise EvidenceError("retained_evaluation_size")
    fields(document, "evaluation max_ancestor_distance", "evaluation settings")
    return document


def _history_inputs(directory: Path):
    """A segmented checkpoint, when the package has one.

    Packages written before segmented history have no checkpoint file, or a
    checkpoint with no segment list. Those keep the single response log.
    """
    path = directory / "history-checkpoint.json"
    if not path.is_file():
        return None, None, None
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None, "incompatible_checkpoint"
    if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
        return None, None, None
    root = directory
    recorded = document.get("segment_root")
    if isinstance(recorded, str) and recorded:
        return None, None, "external_segment_root_refused"
    problem = segments_problem(document, root)
    if problem is not None:
        return None, None, problem
    return document, root, None


def _unavailable(missing: list[str]) -> dict:
    return {
        "comparison": None,
        "reason": "inputs_unavailable",
        "status": "UNVERIFIABLE",
        "unavailable": missing,
        "verdicts": {},
    }


def _evidence_failure(reason: str) -> dict:
    return {
        "comparison": None,
        "reason": reason,
        "status": "UNVERIFIABLE",
        "unavailable": [],
        "verdicts": {},
    }


def _compare(verdicts: dict, saved: dict | None) -> dict | None:
    if saved is None:
        return None
    rows = {}
    for key in sorted(set(verdicts) | set(saved)):
        reconstructed = verdicts.get(key)
        prior = saved.get(key)
        rows[key] = {
            "agree": prior == reconstructed,
            "reconstructed": reconstructed,
            "saved": prior,
        }
    return rows


def _load_responses(path: Path) -> list[dict]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        raise EvidenceError("responses_empty")
    for row in rows:
        _check_integrity(row)
    return rows


def _check_integrity(row: dict) -> None:
    if "result" not in row or not isinstance(row.get("sha256"), str) or type(row.get("byte_length")) is not int:
        raise EvidenceError("missing_integrity")
    digest, size = result_sha256(row["result"])
    if digest != row["sha256"]:
        raise EvidenceError("hash_mismatch")
    if size != row["byte_length"]:
        raise EvidenceError("truncated_response")


def _check_binding(round_contract: dict, rows: list[dict]) -> None:
    for row in rows:
        if row.get("method") != "chain_getBlockHash":
            continue
        params = row.get("params") or []
        if not params or not _is_zero(params[0]):
            continue
        result = row.get("result")
        if not isinstance(result, str):
            raise EvidenceError("round_binding")
        if result.lower().removeprefix("0x") != round_contract["genesis_hash"]:
            raise EvidenceError("round_binding")
        return
    # No recorded genesis read. Do not invent one; reconstruction still has to decode.


def _is_zero(value) -> bool:
    if value == 0:
        return True
    if isinstance(value, str) and value.lower() in {"0x0", "0x00"}:
        return True
    return False


def _index(rows: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for row in rows:
        key = row.get("method", "") + "\n" + canonical_result(_params(row.get("params")))
        queue = index.setdefault(key, [])
        if queue and queue[-1].get("sha256") != row.get("sha256") and row.get("method") != "chain_getHead":
            raise EvidenceError("inconsistent_response")
        queue.append(row)
    return index


def _params(params) -> list:
    return list(params) if isinstance(params, (list, tuple)) else []


class _ReplaySocket:
    def __init__(self, index: dict[str, list[dict]], *, immutable_reuse=False):
        self.immutable_reuse = immutable_reuse
        self.index = {key: list(rows) for key, rows in index.items()}
        self.pending: list[str] = []

    def send(self, payload: str) -> None:
        request = json.loads(payload)
        key = request["method"] + "\n" + canonical_result(_params(request.get("params")))
        queue = self.index.get(key) or []
        if not queue:
            raise EvidenceError("missing_response")
        # Metadata and the runtime version do not change across this package.
        # Replaying them once per recorded call is not enough: initialization
        # and the collector both read them.
        from .rpc_cache import immutable_key
        reusable = self.immutable_reuse and immutable_key(request["method"], request.get("params")) is not None
        if reusable or request["method"] in {"state_getMetadata", "state_getRuntimeVersion", "rpc_methods"}:
            row = queue[0]
        else:
            row = queue.pop(0)
        body = {"id": request["id"], "jsonrpc": "2.0", "result": row["result"]}
        self.pending.append(json.dumps(body))

    def recv(self) -> str:
        return self.pending.pop(0)

    def close(self) -> None:
        return None
