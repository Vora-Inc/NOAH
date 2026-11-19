"""Offline, allowlisted website projection of an authenticated completed round.

The public entry point replays adjudication; saved manifests/verdicts are never
accepted as proof. An externally pinned activation receipt is an operator
attestation, not automatic evidence of a running validator configuration.
No default receipt, network access, signing, deployment or website publication.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


GENESIS = "2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
POLICY = "registered-at-b-accurate-report-v1"
PUBLIC_SCHEMA = "noah.backing.public.v1"
ACTIVATION_SCHEMA = "noah.report-publication-activation/1"
COUNT_KEYS = ("VERIFIED", "MISMATCH", "MISSING", "LATE", "EARLY", "MALFORMED",
              "INELIGIBLE", "UNVERIFIABLE", "UNCLASSIFIED")
CALC_REASONS = frozenset(("subnet_not_found", "subnet_dissolving", "subtoken_disabled",
    "pool_uninitialized", "reserves_too_low", "zero_price", "no_holder"))
OUTPUT_REASONS = frozenset(("above_input_cap", "all_zero_sim", "partial_fill",
    "tao_exceeds_reserve", "input_cap_too_small", "threshold_not_reached", "no_valid_sale",
    "non_monotone_boundary", "non_monotone_validity", "search_budget_exceeded"))
HEX64 = re.compile(r"[a-f0-9]{64}\Z")
MAX_PUBLIC_BYTES = 32768
MAX_SAFE_INTEGER = 2**53 - 1


class PublicExportError(ValueError):
    """Publication input, binding or immutable-history refusal."""


def _require(condition, message):
    if not condition:
        raise PublicExportError("report public export: " + message)


def _keys(value, keys, name):
    _require(type(value) is dict and set(value) == set(keys), name + " fields")
    return value


def _integer(value, minimum, maximum, name):
    _require(type(value) is int and minimum <= value <= maximum, name + " integer")
    return value


def _pattern(value, pattern, name):
    _require(type(value) is str and re.fullmatch(pattern, value) is not None, name + " format")
    return value


def _hash(value, name):
    return _pattern(value, HEX64, name)


def canonical_public_bytes(value):
    """Public report and semantic digests: integer-preserving, NO newline."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def canonical_artifact_bytes(value):
    """Exact producer artifact representation, including its final newline."""
    return canonical_public_bytes(value) + b"\n"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _sha(canonical_public_bytes(value))


def _strict_json(raw, limit):
    _require(type(raw) is bytes and len(raw) <= limit, "JSON byte limit")
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def bad_number(_):
        raise PublicExportError("report public export: noninteger JSON token")
    def integer(token):
        _require(re.fullmatch(r"0|[1-9][0-9]*", token) is not None, "integer token")
        return int(token)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_int=integer, parse_float=bad_number, parse_constant=bad_number)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PublicExportError("report public export: invalid JSON") from exc
    return value


def _read_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            import stat
            info = os.fstat(handle.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular single-link file")
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise PublicExportError("report public export: artifact cannot be read safely") from exc
    _require(len(raw) <= limit, "artifact byte limit")
    return raw


def _utc_ms(value):
    _pattern(value, r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", "UTC time")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise PublicExportError("report public export: invalid calendar time") from exc
    _require(parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value, "canonical UTC time")
    return int(parsed.timestamp()) * 1000


def _decimal(value, minimum, maximum, name):
    _pattern(value, r"0|[1-9][0-9]{0,19}", name)
    return _integer(int(value), minimum, maximum, name)


def public_status(calculation_status, required, counts):
    if calculation_status != "OK":
        return "calculation_unavailable"
    if required == 0:
        return "no_submissions"
    if counts["VERIFIED"] == required:
        return "verified"
    if counts["VERIFIED"]:
        return "partial"
    if counts["UNVERIFIABLE"] + counts["UNCLASSIFIED"] == 0:
        return "not_verified"
    return "unresolved"


def report_filename(report):
    return (f"{report['round']['round_date']}-sn{report['round']['reported_netuid']}-"
            f"{report['report_id'][:12]}.json")


def validate_public_report(report, *, now_ms=None):
    """Producer-side contract checks; the website independently checks again."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    _keys(report, ("schema", "report_id", "supersedes", "published_at", "chain", "round",
                   "calculation", "verification"), "public report")
    _require(report["schema"] == PUBLIC_SCHEMA, "public schema")
    _hash(report["report_id"], "report ID")
    if report["supersedes"] is not None:
        _hash(report["supersedes"], "supersedes")
    published = _utc_ms(report["published_at"])
    _require(published <= now_ms + 600000, "publication in future")
    chain = _keys(report["chain"], ("network", "genesis_hash", "spec_version"), "chain")
    _require(chain["network"] == "finney" and chain["genesis_hash"] == "0x" + GENESIS,
             "Mainnet chain identity")
    _integer(chain["spec_version"], 1, MAX_SAFE_INTEGER, "runtime spec")
    round_ = _keys(report["round"], ("round_sha256", "round_date", "reported_netuid",
        "mining_netuid", "block_number", "block_hash", "block_timestamp_ms", "reveal_close_ms",
        "scoring_release", "calculation_version"), "public round")
    _hash(round_["round_sha256"], "round digest")
    _pattern(round_["block_hash"], r"0x[a-f0-9]{64}", "block hash")
    _pattern(round_["calculation_version"], r"[a-f0-9]{40}", "calculation version")
    _integer(round_["reported_netuid"], 1, 65535, "reported subnet")
    _require(type(round_["mining_netuid"]) is int and round_["mining_netuid"] == 82, "Mainnet subnet82")
    _pattern(round_["round_date"], r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "round date")
    try:
        _require(date.fromisoformat(round_["round_date"]).isoformat() == round_["round_date"], "round date")
    except ValueError as exc:
        raise PublicExportError("report public export: invalid round date") from exc
    release = _pattern(round_["scoring_release"], r"[a-z0-9][a-z0-9.-]{0,63}", "release label")
    _require(not re.search(r"fixture|synthetic|test|localnet|rehearsal|\.\.|^http", release), "nonproduction release")
    _require(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{47,49}", release) is None, "address-like release")
    _decimal(round_["block_number"], 0, 2**32 - 1, "snapshot height")
    snapshot = _decimal(round_["block_timestamp_ms"], 0, 8640000000000000, "snapshot time")
    close = _decimal(round_["reveal_close_ms"], 0, 8640000000000000, "reveal close")
    _require(snapshot <= close <= published, "snapshot/close/publication ordering")
    calc = _keys(report["calculation"], ("engine_sha256", "status", "reason", "output1", "output2", "output3"), "calculation")
    _hash(calc["engine_sha256"], "engine digest")
    values = []
    if calc["status"] == "OK":
        _require(calc["reason"] is None, "OK calculation reason")
        for index, name in enumerate(("output1", "output2", "output3")):
            field = "drop_ppm" if index == 0 else "alpha_required_rao"
            output = _keys(calc[name], ("status", "reason", field), name)
            if output["status"] == "OK":
                _require(output["reason"] is None, "OK output reason")
                value = _decimal(output[field], 0 if index == 0 else 1,
                                 1000000 if index == 0 else 2**64 - 1, field)
                _require(index == 0 or value % 1000000 == 0, "alpha grid")
                values.append(value)
            else:
                _require(output["status"] == "UNAVAILABLE" and output["reason"] in OUTPUT_REASONS
                         and output[field] is None, "unavailable output")
                values.append(None)
        _require(values[1] is None or values[2] is None or values[1] <= values[2], "threshold order")
    else:
        _require((calc["status"] == "UNSUPPORTED" and calc["reason"] == "stable_mechanism")
            or (calc["status"] == "UNAVAILABLE" and calc["reason"] in CALC_REASONS), "publishable calculation")
        _require(all(calc[name] is None for name in ("output1", "output2", "output3")), "gated outputs")
    ver = _keys(report["verification"], ("verdicts_sha256", "required_submissions", "counts", "status"), "verification")
    _hash(ver["verdicts_sha256"], "verdict digest")
    required = _integer(ver["required_submissions"], 0, MAX_SAFE_INTEGER, "required submissions")
    counts = _keys(ver["counts"], COUNT_KEYS, "counts")
    _require(sum(_integer(counts[k], 0, MAX_SAFE_INTEGER, k) for k in COUNT_KEYS) == required, "count total")
    _require((len(values) == 3 and None not in values) or counts["VERIFIED"] == 0, "match without complete calculation")
    _require(ver["status"] == public_status(calc["status"], required, counts), "derived public status")
    _require(report["report_id"] == _digest({k: v for k, v in report.items() if k != "report_id"}), "public report digest")
    _require(len(canonical_public_bytes(report)) <= MAX_PUBLIC_BYTES, "public size")
    return report


def _member_identity(member):
    _require(type(member) is dict, "roster member")
    return (_integer(member.get("uid"), 0, 65535, "UID"), _hash(member.get("hotkey"), "hotkey"),
            _hash(member.get("coldkey"), "coldkey"),
            _integer(member.get("registration_block"), 0, 2**32 - 1, "registration block"))


def project_verified_round(artifact, summary, *, request, request_sha256, published_at, supersedes=None,
                           now_ms=None):
    """Pure projection of REBUILT artifacts, not a standalone verification API.

    Caller must obtain these objects from verify_adjudication. No current scored
    subset, arbitrary hotkey list or saved verdict file can establish this input.
    Assignment coverage comes only from the exact manifest-bound request replayed
    by that verifier. Full snapshot/verdict proofs and their hashes are retained;
    only public counts use the assigned snapshot incarnations. The conservative
    resolved-round gate still refuses unresolved evidence for any snapshot member.
    """
    _require(type(artifact) is dict and type(summary) is dict, "rebuilt round")
    contract = artifact["contract"]
    round_sha = _digest(contract)
    _require(type(request) is dict and _digest(request) == request_sha256,
             "projection request digest binding")
    _require(contract in request.get("policy", {}).get("required_rounds", []),
             "projection request round binding")
    _require(summary.get("round_sha256") == round_sha and summary.get("resolved") is True, "round must be resolved")
    _require(contract.get("genesis_hash") == GENESIS and contract.get("mining_netuid") == 82,
             "Mainnet round only")
    _require(contract.get("eligibility_policy_version") == POLICY, "accurate-report-only policy")
    evaluation = artifact["evaluation"]
    _require(evaluation == summary.get("evaluation"), "evaluation binding")
    _integer(evaluation.get("timestamp_ms"), contract["reveal_close_ms"], 8640000000000000, "closed evaluation")
    _integer(evaluation.get("block_number"), contract["block_number"], 2**32 - 1, "evaluation height")
    _hash(evaluation.get("block_hash"), "evaluation hash")
    _require(_utc_ms(published_at) >= evaluation["timestamp_ms"], "publication predates evaluated evidence")
    required = artifact["required_roster"]
    verdicts = artifact["verdicts"]
    engine = artifact["engine"]
    _require(type(engine) is dict, "agreeing retained engine required")
    engine_sha = _sha(canonical_artifact_bytes(engine))
    roster_sha = _sha(canonical_artifact_bytes(required))
    verdicts_sha = _sha(canonical_artifact_bytes(verdicts))
    _require(summary.get("engine_sha256") == engine_sha and summary.get("required_roster_sha256") == roster_sha
             and summary.get("verdicts_sha256") == verdicts_sha, "exact artifact digest binding")
    for envelope, schema in ((required, "noah.report-required-roster/1"), (verdicts, "noah.report-final-verdicts/1")):
        _require(envelope.get("schema") == schema and envelope.get("request_sha256") == request_sha256
                 and envelope.get("round_sha256") == round_sha and envelope.get("evaluation") == evaluation, "artifact context binding")
    _require(verdicts.get("engine_sha256") == engine_sha and verdicts.get("required_roster_sha256") == roster_sha, "verdict context")
    _require(type(required.get("required_members")) is list and type(verdicts.get("rows")) is list, "complete roster/verdict lists")
    members = [_member_identity(row) for row in required["required_members"]]
    rows = verdicts["rows"]
    identities = [_member_identity(row) for row in rows]
    _require(len(set(members)) == len(members) and len({m[1] for m in members}) == len(members)
             and len({m[0] for m in members}) == len(members), "duplicate required identity")
    _require(len(identities) == len(members) and set(identities) == set(members), "exact required verdict coverage")
    _require(all(m[3] <= contract["block_number"] for m in members), "registration after snapshot")
    # Always retain/check the complete authenticated snapshot and final verdicts.
    # Weekly assignments affect public coverage below, never the underlying proof.
    snapshot_members = required.get("snapshot", {}).get("members")
    _require(type(snapshot_members) is list and sorted(_member_identity(m) for m in snapshot_members) == sorted(members), "complete snapshot roster coverage")
    snapshot = required["snapshot"]
    _require(snapshot.get("schema") == "noah.report-snapshot-roster/1"
        and snapshot.get("genesis_hash") == GENESIS and snapshot.get("mining_netuid") == 82
        and snapshot.get("block_number") == contract["block_number"]
        and snapshot.get("block_hash") == contract["block_hash"]
        and snapshot.get("timestamp_ms") == contract["block_timestamp_ms"]
        and snapshot.get("spec_version") == engine.get("spec_version"), "snapshot roster context")
    counted_members = set(members)
    if "assignments" in request:
        from .adjudication_contract import validate_request
        try:
            validate_request(request)
        except (ValueError, TypeError, KeyError) as exc:
            raise PublicExportError("report public export: invalid assignment request") from exc
        assigned = {(row["hotkey"], row["coldkey"], row["registration_block"])
                    for row in request["assignments"]
                    if contract["reported_netuid"] in row["targets"]}
        # A UID can move; hotkey, owner and registration block bind the incarnation.
        counted_members = {member for member in members if member[1:] in assigned}
    counts = dict.fromkeys(COUNT_KEYS, 0)
    for row in rows:
        status, reason = row.get("status"), row.get("reason")
        sources = _keys(row.get("sources"), ("reader_a", "reader_b"), "paired sources")
        for source in sources.values():
            _keys(source, ("verdict_sha256", "evidence_sha256"), "source binding")
            _hash(source["verdict_sha256"], "source verdict digest")
            _hash(source["evidence_sha256"], "source evidence digest")
        _require(sources["reader_a"]["verdict_sha256"] == sources["reader_b"]["verdict_sha256"],
                 "resolved source verdicts disagree")
        if row.get("outcome") == "verified":
            _require(status == "VERIFIED" and reason == "exact_match" and row.get("effect") == "preserve", "accurate verdict consistency")
        else:
            _require(row.get("outcome") == "confirmed_failure" and row.get("effect") == "zero"
                     and status in ("MISMATCH", "MISSING", "LATE", "EARLY", "MALFORMED", "INELIGIBLE"), "unresolved verdict cannot publish")
            _require(type(reason) is str and bool(reason), "confirmed verdict reason")
        if _member_identity(row) in counted_members:
            counts[status] += 1
    _require(engine.get("block_number") == contract["block_number"]
             and engine.get("block_hash") in (contract["block_hash"], "0x" + contract["block_hash"])
             and engine.get("netuid") == contract["reported_netuid"], "engine snapshot binding")
    if engine.get("status") == "OK":
        _require(engine.get("ark_coldkey") in (contract["ark_coldkey"], "0x" + contract["ark_coldkey"]), "engine Ark binding")
    calculation = {"engine_sha256": engine_sha, "status": engine.get("status"), "reason": engine.get("reason")}
    for index, name in enumerate(("output1", "output2", "output3")):
        if calculation["status"] != "OK":
            calculation[name] = None
            continue
        value = engine.get(name)
        _require(type(value) is dict, "engine output")
        field = "drop_ppm" if index == 0 else "alpha_required_rao"
        number = value.get(field)
        if value.get("status") == "OK":
            _require(type(number) is int, "exact integer engine output")
        calculation[name] = {"status": value.get("status"), "reason": value.get("reason"),
                             field: str(number) if value.get("status") == "OK" else None}
    body = {
        "schema": PUBLIC_SCHEMA, "supersedes": supersedes, "published_at": published_at,
        "chain": {"network": "finney", "genesis_hash": "0x" + GENESIS, "spec_version": engine.get("spec_version")},
        "round": {"round_sha256": round_sha, "round_date": contract["round_date"],
            "reported_netuid": contract["reported_netuid"], "mining_netuid": 82,
            "block_number": str(contract["block_number"]), "block_hash": "0x" + contract["block_hash"],
            "block_timestamp_ms": str(contract["block_timestamp_ms"]), "reveal_close_ms": str(contract["reveal_close_ms"]),
            "scoring_release": contract["scoring_release"], "calculation_version": contract["calculation_version"]},
        "calculation": calculation,
        "verification": {"verdicts_sha256": verdicts_sha, "required_submissions": len(counted_members), "counts": counts,
            "status": public_status(calculation["status"], len(counted_members), counts)},
    }
    return validate_public_report({**body, "report_id": _digest(body)}, now_ms=now_ms)


def _activation(path, expected_sha256, request, contract, *, published_at):
    _require(path is not None and expected_sha256 is not None, "explicit pinned activation receipt required")
    _hash(expected_sha256, "activation receipt digest")
    raw = _read_regular(path, 16384)
    _require(_sha(raw) == expected_sha256, "activation receipt digest mismatch")
    receipt = _keys(_strict_json(raw, 16384), ("schema", "genesis_hash", "mining_netuid",
        "policy_sha256", "eligibility_policy_version", "effective_day", "approved_at",
        "report_enforcement", "publication_approved", "approval_reference"), "activation receipt")
    _require(receipt["schema"] == ACTIVATION_SCHEMA, "activation schema")
    _require(receipt["genesis_hash"] == GENESIS and type(receipt["mining_netuid"]) is int
             and receipt["mining_netuid"] == 82, "activation Mainnet identity")
    _require(receipt["policy_sha256"] == request["policy_sha256"]
             and receipt["eligibility_policy_version"] == POLICY, "activation policy binding")
    effective = _integer(receipt["effective_day"], 0, 2**64 - 1, "activation day")
    _require(effective == request["policy"]["effective_day"] and effective <= request["eval_day"], "activation effective day")
    approved = _utc_ms(receipt["approved_at"])
    _require(approved <= contract["commit_open_ms"] and approved <= _utc_ms(published_at),
             "approval must precede required round")
    _require(receipt["report_enforcement"] is True and receipt["publication_approved"] is True,
             "report enforcement and publication must be explicitly approved")
    reference = receipt["approval_reference"]
    _require(type(reference) is str and 1 <= len(reference) <= 512
             and all(ord(c) >= 32 for c in reference), "approval reference")
    return receipt


def _check_history(reports):
    by_id = {report["report_id"]: report for report in reports}
    _require(len(by_id) == len(reports), "duplicate retained report ID")
    corrected = set()
    for report in reports:
        target_id = report["supersedes"]
        if target_id is None:
            continue
        _require(target_id in by_id and target_id not in corrected, "missing or multiply corrected target")
        target = by_id[target_id]
        _require(report["round"] == target["round"], "correction changes round/snapshot identity")
        _require(report["published_at"] >= target["published_at"], "correction predates original")
        corrected.add(target_id)
    for report in reports:
        seen = set()
        current = report
        while current is not None:
            _require(current["report_id"] not in seen, "correction cycle")
            seen.add(current["report_id"])
            current = by_id.get(current["supersedes"])
    return by_id, corrected


@contextmanager
def _publication_lock(directory):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory / ".public-export.lock", flags, 0o600)
    try:
        import stat
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "publication lock file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_export(directory, package, report, raw, *, now_ms):
    directory = Path(directory)
    _require(not directory.is_symlink(), "publication directory symlink")
    package_root = Path(package).resolve()
    output_root = directory.resolve()
    _require(output_root != package_root and package_root not in output_root.parents,
             "public output must be outside private package")
    directory.mkdir(parents=True, exist_ok=True)
    _require(directory.is_dir() and not directory.is_symlink(), "publication directory")
    destination = directory / report_filename(report)
    with _publication_lock(directory):
        history = []
        for path in sorted(directory.glob("*.json")):
            old = validate_public_report(_strict_json(_read_regular(path, MAX_PUBLIC_BYTES), MAX_PUBLIC_BYTES), now_ms=now_ms)
            _require(path.name == report_filename(old), "retained filename mismatch")
            history.append(old)
        by_id, corrected = _check_history(history)
        if report["report_id"] in by_id:
            _require(_read_regular(destination, MAX_PUBLIC_BYTES) == raw, "existing report bytes differ")
            return destination, False
        same_round = [old for old in history if old["round"]["round_sha256"] == report["round"]["round_sha256"]
                      and old["report_id"] not in corrected]
        if same_round:
            _require(len(same_round) == 1 and report["supersedes"] == same_round[0]["report_id"],
                     "changed round export requires superseding its current report")
        _check_history([*history, report])
        fd, temporary = tempfile.mkstemp(prefix=".public-export-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise PublicExportError("report public export: output filename already exists") from exc
            # Sync the containing directory before claiming durable creation.
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return destination, True


def export_public_round(package, request, manifest_sha256, *, round_sha256,
                        activation_receipt=None, activation_receipt_sha256=None,
                        published_at=None, supersedes=None, out_dir=None, verifier=None,
                        now_ms=None):
    """Replay a pinned private package and prepare an immutable public export.

    ``verifier`` is a trusted dependency-injection boundary for tests/integration,
    never a caller-selected plugin or result file. Default is the actual offline
    adjudicator. Its include_round_artifacts output is reconstructed from raw
    evidence, not a saved manifest. No receipt means refusal, even for dry runs.

    Receipt schema noah.report-publication-activation/1 strictly requires genesis,
    mining_netuid, policy_sha256, eligibility_policy_version, effective_day,
    approved_at, report_enforcement=true, publication_approved=true and a private
    approval_reference. Pin its exact-byte SHA outside the package. This records
    operator approval; it does not prove live runtime settings automatically.

    Returns {report, filename, json_bytes, path, created}. Bytes/digests and
    private paths in this local result are not fields of the public report.
    Caller owns retaining a private export log and obtaining publication review.
    """
    _require(activation_receipt is not None and activation_receipt_sha256 is not None,
             "explicit pinned activation receipt required")
    _hash(manifest_sha256, "manifest digest")
    _hash(round_sha256, "selected round digest")
    _require(type(request) is dict and request.get("genesis_hash") == GENESIS
             and type(request.get("mining_netuid")) is int and request["mining_netuid"] == 82,
             "Mainnet request only")
    _require(request.get("policy", {}).get("eligibility_policy_version") == POLICY,
             "accurate-report-only policy")
    _require(_digest(request["policy"]) == request.get("policy_sha256"), "policy semantic digest")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if published_at is None:
        published_at = datetime.fromtimestamp(now_ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if verifier is None:
        from .adjudication import verify_adjudication
        verifier = verify_adjudication
    result = verifier(package, request, manifest_sha256, include_round_artifacts=True)
    _require(type(result) is dict and result.get("schema") == "noah.report-adjudication-result/1"
             and result.get("manifest_sha256") == manifest_sha256
             and result.get("request_sha256") == _digest(request)
             and result.get("policy_sha256") == request["policy_sha256"]
             and result.get("status") in ("COMPLETE", "UNRESOLVED"), "rebuilt adjudication result binding")
    _require(type(result.get("round_artifacts")) is list and type(result.get("rounds")) is list,
             "rebuilt round artifacts required")
    artifacts = [row for row in result["round_artifacts"] if _digest(row["contract"]) == round_sha256]
    summaries = [row for row in result["rounds"] if row.get("round_sha256") == round_sha256]
    _require(len(artifacts) == 1 and len(summaries) == 1, "selected round absent or ambiguous")
    artifact, summary = artifacts[0], summaries[0]
    _require(artifact["contract"] in request["policy"]["required_rounds"]
             and artifact["evaluation"] == request["evaluation"], "selected request round binding")
    _activation(activation_receipt, activation_receipt_sha256, request, artifact["contract"],
                published_at=published_at)
    report = project_verified_round(artifact, summary, request=request, request_sha256=result["request_sha256"],
        published_at=published_at, supersedes=supersedes, now_ms=now_ms)
    raw = (json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    _require(len(raw) <= MAX_PUBLIC_BYTES, "public file size")
    path, created = (None, False) if out_dir is None else _write_export(out_dir, package, report, raw, now_ms=now_ms)
    return {"report": report, "filename": report_filename(report), "json_bytes": raw,
            "path": None if path is None else str(path), "created": created}
