"""Frozen report proof and allocation, separate from financial accounting.

The report worker reconstructs raw packages in its isolated decoder environment.
This module never trusts a saved verdict. R0 stays the original twin calculation;
R1 retains a report-derived vector and R2 may add the Legacy starter allocation.
"""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from ..chain.ss58 import public_key_of
from ..scoring.pay import weights_u16
from .calculation_record import artifact, digest, encoded, retain, verify
from .keeper import decide
from .publish_intent import PublicationStatusUnknown
from .vector import publishable_vector

SCHEMA = "noah-report-publication/1"
RESULT_SCHEMA = "noah.report-adjudication-result/1"
MAX_WORKER_OUTPUT = 2 * 1024 * 1024
MAX_ADJUDICATION_OUTPUT = 64 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 900
SHARED_WORKER_TIMEOUT_SECONDS = 3900
SHA = re.compile(r"[0-9a-f]{64}\Z")


class ReportPublicationRefused(PublicationStatusUnknown):
    """Missing or invalid report proof cannot authorize a new publication."""


def require(value, reason):
    if not value:
        raise ReportPublicationRefused(reason)


def semantic_digest(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode())


def worker_python():
    path = os.environ.get("NOAH_REPORT_PYTHON")
    if not path:
        path = str(Path(__file__).resolve().parents[2] / ".venv-reports/bin/python")
    require(Path(path).is_absolute() and Path(path).is_file(),
            "isolated report worker is missing")
    return path


def check_worker_ready():
    """Read-only startup probe of the explicitly selected isolated interpreter."""
    selected = os.environ.get("NOAH_REPORT_PYTHON", "")
    require(bool(selected), "report enforcement requires explicit NOAH_REPORT_PYTHON")
    worker = worker_python()
    require(os.access(worker, os.X_OK), "isolated report worker is not executable")
    expected = str(Path(__file__).resolve().parents[2] / "noah/reports/cli.py")
    code = (
        "import importlib.util as u, sys; from pathlib import Path; "
        "assert u.find_spec('bittensor') is None and u.find_spec('async_substrate_interface') is None; "
        "import substrateinterface, scalecodec, bittensor_wallet; "
        "import noah.reports.cli as c; "
        "from noah.reports.adjudication import adjudicate, verify_adjudication; "
        "from noah.reports.schedule import resolve_day, verify_resolution; "
        "assert Path(c.__file__).resolve() == Path(sys.argv[1]); "
        "assert all(callable(f) for f in (adjudicate, verify_adjudication, resolve_day, verify_resolution)); "
        "print('noah-report-worker-ready/1')"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run([worker, "-I", "-B", "-c", code, expected],
                                    env=env, stdout=output, stderr=errors, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReportPublicationRefused("isolated report worker startup probe failed") from exc
        output.seek(0)
        require(result.returncode == 0 and output.read(128) == b"noah-report-worker-ready/1\n",
                "report worker isolation, dependencies or selected source differ")


def _worker_document(path, limit):
    """Read only a bounded stable regular input when choosing a resource profile."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and 0 < before.st_size <= limit, "worker timeout input shape refused")
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        require(len(raw) == before.st_size and signature(before) == signature(os.fstat(descriptor))
                == signature(Path(path).lstat()), "worker timeout input changed")
    finally:
        os.close(descriptor)
    return raw


def worker_timeout(action, arguments):
    """Only validated package3 shape selects the longer fixed weekly bound.

    This selects time, not proof authority. The isolated worker still performs
    all normal request, package, raw replay and economic verification.
    """
    if action == "assignment-week":
        return 120
    if action not in ("adjudicate", "adjudicate-verify"):
        return WORKER_TIMEOUT_SECONDS
    values = list(map(str, arguments))
    def one(flag):
        require(values.count(flag) == 1 and values.index(flag) + 1 < len(values),
                "worker timeout arguments differ")
        return values[values.index(flag) + 1]
    try:
        from ..reports.adjudication_contract import (strict_json, strict_manifest_json,
            validate_invocation, validate_request, MAX_REQUEST_BYTES, MAX_MANIFEST_BYTES)
        from ..reports.shared_window import eligible, ENGINE_SCHEMA
        if action == "adjudicate":
            invocation = strict_json(_worker_document(one('--invocation'), MAX_REQUEST_BYTES))
            validate_invocation(invocation)
            return SHARED_WORKER_TIMEOUT_SECONDS if eligible(invocation['request']) else WORKER_TIMEOUT_SECONDS
        request = strict_json(_worker_document(one('--request'), MAX_REQUEST_BYTES))
        validate_request(request)
        raw = _worker_document(Path(one('--package')) / 'manifest.json', MAX_MANIFEST_BYTES)
        pin = one('--manifest-sha256')
        require(SHA.fullmatch(pin) and digest(raw) == pin, "worker timeout manifest pin differs")
        manifest = strict_manifest_json(raw)
        require(manifest.get('request') == request, "worker timeout manifest request differs")
        return (SHARED_WORKER_TIMEOUT_SECONDS if manifest['schema'] == ENGINE_SCHEMA
                and eligible(request) else WORKER_TIMEOUT_SECONDS)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ReportPublicationRefused("worker timeout input validation refused") from exc


def run_worker(action, *arguments):
    """Fixed CLI, isolated imports, bounded response; never echo provider secrets."""
    require(action in ("adjudicate", "adjudicate-verify", "resolve-day", "assignment-week"), "unsupported report worker action")
    limit = MAX_ADJUDICATION_OUTPUT if action in ("adjudicate", "adjudicate-verify") else MAX_WORKER_OUTPUT
    timeout = worker_timeout(action, arguments)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    # Retain output on temporary private files so capture_output cannot consume
    # unbounded memory. Provider collection itself enforces its package budgets.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                [worker_python(), "-I", "-m", "noah.reports.cli", "validator", action,
                 *map(str, arguments)], env=env, stdout=out, stderr=errors,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReportPublicationRefused("report worker could not complete") from exc
        require(result.returncode == 0, "report worker refused authenticated evidence")
        out.seek(0)
        data = out.read(limit + 1)
    require(len(data) <= limit, "report worker response exceeds limit")
    try:
        from ..reports.adjudication_contract import _strict_json
        return _strict_json(data, limit, "worker response")
    except (ValueError, TypeError) as exc:
        raise ReportPublicationRefused("report worker response is invalid") from exc


def _package(root, reference):
    require(type(reference) is dict and set(reference) == {"path", "sha256"},
            "invalid report package reference")
    sha = reference["sha256"]
    require(type(sha) is str and SHA.fullmatch(sha) and
            reference["path"] == f"reports/{sha}/manifest.json",
            "report package must be content addressed")
    directory = Path(root) / "reports" / sha
    require(not (Path(root) / "reports").is_symlink() and not directory.is_symlink(),
            "report package link refused")
    return directory


def replay_package(directory, package, request):
    root = _package(directory, package)
    # The request is semantic JSON. Endpoint credentials are never part of it.
    with tempfile.TemporaryDirectory(prefix="noah-report-replay-") as temporary:
        path = Path(temporary) / "request.json"
        path.write_bytes(encoded(request))
        return run_worker("adjudicate-verify", "--package", root,
                          "--manifest-sha256", package["sha256"], "--request", path)


def validate_result(result, request, package_sha):
    from ..reports.adjudication_contract import validate_request
    request_sha = validate_request(request)
    require(type(package_sha) is str and SHA.fullmatch(package_sha), "invalid report package digest")
    require(type(result) is dict and set(result) == {
        "schema", "manifest_sha256", "request_sha256", "policy_sha256", "status",
        "scored_decisions", "rounds"}, "report provider result fields differ")
    require(result["schema"] == RESULT_SCHEMA and result["manifest_sha256"] == package_sha
            and result["request_sha256"] == request_sha
            and result["policy_sha256"] == request["policy_sha256"]
            and result["status"] in ("COMPLETE", "UNRESOLVED"), "report provider result binding differs")
    expected_rounds = {semantic_digest(row) for row in request["policy"]["required_rounds"]}
    round_rows = result["rounds"]
    require(type(round_rows) is list and len(round_rows) == len(expected_rounds),
            "report provider round count differs")
    seen = set()
    for row in round_rows:
        require(type(row) is dict and set(row) == {
            "round_sha256", "required_roster_sha256", "evaluation", "readers",
            "engine_sha256", "calculation_sha256", "verdicts_sha256", "resolved"},
                "report provider round fields differ")
        require(row["round_sha256"] in expected_rounds and row["round_sha256"] not in seen
                and row["evaluation"] == request["evaluation"] and type(row["resolved"]) is bool,
                "report provider round identity differs")
        seen.add(row["round_sha256"])
        for name in ("required_roster_sha256", "verdicts_sha256"):
            require(type(row[name]) is str and SHA.fullmatch(row[name]), "invalid report round digest")
        for name in ("engine_sha256", "calculation_sha256"):
            require(row[name] is None or (type(row[name]) is str and SHA.fullmatch(row[name])),
                    "invalid report calculation digest")
        require(type(row["readers"]) is dict and set(row["readers"]) == {"reader_a", "reader_b"}
                and all(type(v) is str and SHA.fullmatch(v) for v in row["readers"].values()),
                "report independent reader manifests missing")
    rows = result["scored_decisions"]
    require(type(rows) is list and len(rows) == len(request["scored_seats"]),
            "report decisions do not cover the scored roster")
    excluded = set()
    for decision, seat in zip(rows, request["scored_seats"]):
        require(type(decision) is dict and set(decision) == {
            "uid", "hotkey", "coldkey", "registration_block", "effect", "outcome", "rounds"},
                "report decision fields differ")
        require(type(decision["uid"]) is int and type(decision["registration_block"]) is int,
                "report decision identity must use integers")
        require(all(decision[k] == seat[k] for k in
                    ("uid", "hotkey", "coldkey", "registration_block")),
                "report decision belongs to another miner incarnation")
        require((decision["effect"], decision["outcome"]) in {
            ("preserve", "verified"), ("zero", "confirmed_failure"),
            ("preserve", "unresolved"), ("preserve", "not_required")},
                "report decision effect is unsupported")
        require(type(decision["rounds"]) is list and decision["rounds"],
                "report decision round evidence missing")
        require(len(decision["rounds"]) == len(expected_rounds)
                and {row.get("round_sha256") for row in decision["rounds"]
                     if type(row) is dict} == expected_rounds,
                "report decision required rounds differ")
        if decision["effect"] == "zero":
            excluded.add(decision["uid"])
    return excluded


def allocation_inputs(adapter, height):
    """Capture the same keeper/turnover facts as ordinary publication."""
    return {
        "live_uids": sorted(set(adapter.live_uids(height))),
        "owner_uids": sorted(set(adapter.owner_uids())),
        "fixture_hotkeys": {str(k): v for k, v in adapter.fixture_hotkeys(height).items()},
        "chain_hotkeys": {str(k): v for k, v in adapter.chain_hotkeys().items()},
    }


def derive_vector(score, request, result, package_sha, submission, *, reward="gate"):
    """Filter raw pay amounts, then run the unchanged rescale/keeper/turnover."""
    excluded = validate_result(result, request, package_sha)
    require(reward in ("gate", "bonus-20"), "unsupported report reward")
    from ..reports.assignments import earns_reporting_bonus
    awarded = {r["uid"] for r in result["scored_decisions"] if earns_reporting_bonus(r, request)}
    require(type(submission) is dict and set(submission) == {
        "live_uids", "owner_uids", "fixture_hotkeys", "chain_hotkeys"},
            "report submission context differs")
    for name in ("live_uids", "owner_uids"):
        values = submission[name]
        require(type(values) is list and all(type(v) is int and 0 <= v <= 65535 for v in values)
                and values == sorted(set(values)), "invalid report submission UID set")
    for name in ("fixture_hotkeys", "chain_hotkeys"):
        values = submission[name]
        require(type(values) is dict, "invalid report submission hotkey map")
        for uid, hotkey in values.items():
            require(type(uid) is str and re.fullmatch(r"0|[1-9][0-9]{0,4}", uid)
                    and int(uid) <= 65535 and type(hotkey) is str,
                    "invalid report submission hotkey identity")
            public_key_of(hotkey)
    seats = {row["uid"]: row for row in request["scored_seats"]}
    amounts = {}
    for row in score["miners"]:
        uid = row.get("uid")
        if uid is None:
            continue
        require(type(uid) is int and uid in seats and uid not in amounts,
                "report roster differs from raw calculation")
        require(public_key_of(row["coldkey"]).hex() == seats[uid]["coldkey"],
                "report roster owner differs from raw calculation")
        amount = row["payWeightFp"]
        require(type(amount) is str and re.fullmatch(r"0|[1-9][0-9]*", amount),
                "raw pay amount is invalid")
        if reward == "bonus-20":
            from ..scoring.reporting_bonus import bonus_pay
            amounts[uid] = bonus_pay(row, uid in awarded and uid not in submission["owner_uids"])
        else:
            amounts[uid] = 0 if uid in excluded else int(amount)
    require(set(amounts) == set(seats), "report roster does not cover raw calculation")
    for uid, seat in seats.items():
        hotkey = submission["fixture_hotkeys"].get(str(uid))
        require(hotkey is not None and public_key_of(hotkey).hex() == seat["hotkey"],
                "report fixture hotkey differs")
    decision = decide(scored=weights_u16(amounts), live_uids=submission["live_uids"],
                      owner_uids=submission["owner_uids"])
    vector = publishable_vector(
        scored=decision.vector, live_uids=submission["live_uids"],
        owner_uids=submission["owner_uids"],
        fixture_hotkeys={int(k): v for k, v in submission["fixture_hotkeys"].items()},
        chain_hotkeys={int(k): v for k, v in submission["chain_hotkeys"].items()},
    )
    # Reports must not transform an all-filtered result into an all-zero keepalive
    # or an equal-weight fallback. Existing chain weights remain unchanged.
    return vector if any(vector.values()) else {}


def validate_source_request(source, context, request):
    require(request["eval_day"] == source["day"]
            and request["genesis_hash"] == source["genesis_hash"].removeprefix("0x")
            and request["mining_netuid"] == source["netuid"]
            and context["day_start_utc_unix"] == request["day_anchor_ts"] + source["day"] * 86400,
            "report request source network or calendar differs")
    for contract in request["policy"]["required_rounds"]:
        require(contract["ark_coldkey"] == public_key_of(context["ark_address"]).hex()
                and contract["ark_validator_hotkey"] == public_key_of(context["designated_validator_hotkey"]).hex(),
                "report request Ark identity differs from financial source")


def retain_report(directory, source_reference, request, package, submission, *, replay=None, selection=None, reward="gate", reward_transition=None, source_handoff=None):
    source = verify(directory, source_reference)
    context = artifact(directory, source, "context")
    require("report_gate" not in context and "report_no_bonus" not in context and "starter_overlay" not in context,
            "report source must be the original raw calculation")
    fixture = artifact(directory, source, "fixture")
    score = artifact(directory, source, "agreed_score")
    require(request["calculation"]["reference"] == source_reference and
            request["calculation"]["fixture_sha256"] == source["artifacts"]["fixture"]["sha256"],
            "report request does not reference the raw calculation")
    validate_source_request(source, context, request)
    result = (replay or replay_package)(directory, package, request)
    vector = derive_vector(score, request, result, package["sha256"], submission, reward=reward)
    proof = {"schema": SCHEMA, "source": source_reference, "request": request,
             "package": package, "result_sha256": semantic_digest(result),
             "submission": submission}
    if source_handoff is not None:
        proof["source_handoff"] = source_handoff
        from .report_source_handoff import verify_proof
        verify_proof(directory, proof)
    if reward != "gate":
        proof["reward"] = reward
    if reward_transition is not None:
        proof["reward_transition"] = reward_transition
        from .report_reward_transition import verify_proof_reference
        verify_proof_reference(directory, proof)
    if selection is not None:
        from .report_runtime import verify_selection
        verify_selection(selection, request)
        if selection["configuration"]["schema"] == "noah.weekly-assignment-template/2":
            from .weekly_runtime import authority
            require(authority(directory) == selection["configuration"], "weekly proof differs from retained authority")
        proof["selection"] = selection
    reference = retain(directory, fixture=fixture, agreed_text=encoded(score).decode(), vector=vector,
                       context={**context, "report_gate": proof}, canonical_day=False)
    return reference, vector


def verify_report(directory, reference, *, replay=None):
    """Offline R1 replay, including an R2 wrapper; never load current policy."""
    manifest = verify(directory, reference)
    context = artifact(directory, manifest, "context")
    overlay = context.get("starter_overlay")
    if overlay is not None:
        require(type(overlay) is dict and "source" in overlay, "invalid report overlay ancestry")
        # R2's allocator verifier checks the entire overlay separately. Here we
        # verify its R1 source, and refuse a forged copied report field.
        source_manifest = verify(directory, overlay["source"])
        source_context = artifact(directory, source_manifest, "context")
        require(context.get("report_gate") == source_context.get("report_gate"),
                "report overlay source differs")
        require("starter_overlay" not in source_context, "nested starter overlay refused")
        return verify_report(directory, overlay["source"], replay=replay)
    proof = context.get("report_gate")
    if proof is None:
        return None
    require(type(proof) is dict and set(proof) == ({
        "schema", "source", "request", "package", "result_sha256", "submission"}
            | ({"selection"} if "selection" in proof else set())
            | ({"reward"} if "reward" in proof else set())
            | ({"reward_transition"} if "reward_transition" in proof else set())
            | ({"source_handoff"} if "source_handoff" in proof else set()))
            and proof["schema"] == SCHEMA, "invalid report publication proof")
    source = verify(directory, proof["source"])
    source_context = artifact(directory, source, "context")
    require("report_gate" not in source_context and "report_no_bonus" not in source_context and "starter_overlay" not in source_context,
            "report source must be raw calculation")
    require({k: v for k, v in context.items() if k != "report_gate"} == source_context,
            "report calculation context differs from raw source")
    request = proof["request"]
    from .report_source_handoff import verify_proof
    verify_proof(directory, proof)
    from .report_reward_transition import verify_proof_reference
    verify_proof_reference(directory, proof)
    validate_source_request(source, source_context, request)
    if "selection" in proof:
        from .report_runtime import verify_selection
        verify_selection(proof["selection"], request)
        if proof["selection"]["configuration"]["schema"] == "noah.weekly-assignment-template/2":
            from .weekly_runtime import authority
            require(authority(directory) == proof["selection"]["configuration"], "weekly proof differs from retained authority")
    require(request["calculation"]["reference"] == proof["source"] and
            request["calculation"]["fixture_sha256"] == source["artifacts"]["fixture"]["sha256"]
            and request["eval_day"] == source["day"]
            and request["genesis_hash"] == source["genesis_hash"].removeprefix("0x")
            and request["mining_netuid"] == source["netuid"], "report raw source binding differs")
    result = (replay or replay_package)(directory, proof["package"], request)
    vector = derive_vector(artifact(directory, source, "agreed_score"), request, result,
                           proof["package"]["sha256"], proof["submission"], reward=proof.get("reward", "gate"))
    expected = {
        **source,
        "artifacts": manifest["artifacts"],
        "publishable_weights_u16": {str(k): v for k, v in vector.items()},
        "changed_uids": sorted(
            uid for uid in set(map(int, source["engine_weights_u16"])) | set(vector)
            if int(source["engine_weights_u16"].get(str(uid), 0)) != vector.get(uid, 0)
        ),
    }
    require(proof["result_sha256"] == semantic_digest(result)
            and manifest == expected
            and all(manifest["artifacts"][k] == source["artifacts"][k]
                    for k in ("fixture", "agreed_score")), "report allocation does not reproduce")
    return proof


def verify_starter_input(directory, source_reference, raw, *, replay=None):
    """Require the allocator's exclusion list to equal independently replayed R1."""
    proof = verify_report(directory, source_reference, replay=replay)
    if proof is None:
        require(raw.get("schema") != "noah-starter-epoch-preview/5" and "reportGate" not in raw,
                "starter report exclusions lack report proof")
        return
    require(raw.get("schema") == "noah-starter-epoch-preview/5", "starter would bypass report exclusions")
    result = (replay or replay_package)(directory, proof["package"], proof["request"])
    excluded = validate_result(result, proof["request"], proof["package"]["sha256"])
    bonus = proof.get("reward") == "bonus-20"
    if bonus:
        excluded = set()
        from ..reports.assignments import earns_reporting_bonus
    seats = {row["uid"]: row for row in proof["request"]["scored_seats"]}
    rows = []
    for seat in sorted(raw["snapshot"]["seats"], key=lambda row: row["uid"]):
        bound = seats.get(seat["uid"])
        require(bound is not None and public_key_of(seat["hotkey"]).hex() == bound["hotkey"]
                and public_key_of(seat["coldkey"]).hex() == bound["coldkey"],
                "starter exclusion identity differs from report proof")
        rows.append(dict(uid=seat["uid"], coldkey=seat["coldkey"], hotkey=seat["hotkey"],
                         registrationBlock=bound["registration_block"]))
    expected = dict(schema="noah-starter-report-exclusions/1", proofSha256=source_reference["sha256"],
                    evalDay=proof["request"]["eval_day"], registrations=rows,
                    exclusions=[row for row in rows if row["uid"] in excluded],
                    **({"bonusUids": sorted(r["uid"] for r in result["scored_decisions"]
                                            if earns_reporting_bonus(r, proof["request"]))} if bonus else {}))
    require(raw.get("reportGate") == expected, "starter report exclusions differ from replayed proof")
