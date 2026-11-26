"""Commit, then reveal, a prepared file. No new salt and no recalculation.

The trusted mining subnet is an argument. The sidecar's mining field is not read.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .keys import ss58_encode
from .prepare import load_prepared
from .protocol.policy import MAINNET_GENESIS
from .protocol.timing import RoundWindows, classify_commit, classify_reveal


class SubmitError(Exception):
    """Submission refused or unresolved; inspect the journal before any next action."""


class SigningRuntimeRefused(SubmitError):
    """Runtime guard proved refusal before any actual keypair.sign invocation.

    Never raise this for an error from the signer or broadcast. The journal
    may classify only this explicit pre-sign boundary as safely unsent.
    """


@dataclass(frozen=True)
class Inclusion:
    extrinsic_hash: str
    block_hash: str
    block_number: int
    extrinsic_index: int
    block_timestamp_ms: int
    success: bool
    dispatch_error: str | None = None


@dataclass(frozen=True)
class Outcome:
    status: str
    sent: bool
    locator: dict | None


def bind_payload(submission, announcement: dict) -> None:
    """The saved payload must name this announcement's chain, round, B, and reported subnet."""
    if submission.genesis_hash != _hash32(announcement.get("genesis"), "genesis"):
        raise SubmitError("announcement genesis does not match the payload")
    if submission.round_yyyymmdd != _int(announcement.get("round"), "round"):
        raise SubmitError("announcement round does not match the payload")
    if submission.block_number != _int(announcement.get("block_number"), "block number"):
        raise SubmitError("announcement block number does not match the payload")
    if submission.block_hash != _hash32(announcement.get("block_hash"), "block hash"):
        raise SubmitError("announcement block hash does not match the payload")
    if submission.netuid != _int(announcement.get("netuid"), "reported subnet"):
        raise SubmitError("announcement reported subnet does not match the payload")


def require_sr25519(keypair) -> None:
    # substrate-interface also imports legacy ECDSA helpers. Report signing
    # must never dispatch to them (GHSA-wj6h-64fc-37mp); use SR25519 only.
    if type(getattr(keypair, "crypto_type", None)) is not int or keypair.crypto_type != 1:
        raise SubmitError("report signing requires an SR25519 miner hotkey")


def run_phase(
    *,
    phase: str,
    prepared_path: Path,
    announcement: dict,
    mining_netuid: int,
    windows_doc: dict,
    keypair,
    evidence: Path,
    chain,
    now_ms: int | None = None,
    round_windows: dict | None = None,
    allow_mainnet: bool = False,
) -> Outcome:
    if phase not in {"commit", "reveal"}:
        raise SubmitError("unknown phase")
    require_sr25519(keypair)
    prepared_path = Path(prepared_path)
    original = prepared_path.read_bytes()
    prepared = load_prepared(prepared_path)
    bind_payload(prepared.submission, announcement)
    _refuse_wrong_chain(chain.genesis(), announcement, allow_mainnet=allow_mainnet)
    if keypair.public_key != prepared.submission.hotkey:
        raise SubmitError("signer is not the payload hotkey")
    from .protocol.preimage import ReportSubmission, REPORT_POLICY
    if allow_mainnet and (not isinstance(prepared.submission, ReportSubmission) or mining_netuid != 82
                          or round_windows is None or round_windows['eligibility_policy_version'] != REPORT_POLICY):
        raise SubmitError("Mainnet opt-in requires report-only policy on mining subnet82")
    if isinstance(prepared.submission, ReportSubmission):
        if prepared.submission.mining_netuid != mining_netuid:
            raise SubmitError("mining subnet differs from committed subnet")
    if round_windows is not None and ((round_windows["eligibility_policy_version"] == REPORT_POLICY)
                                      != isinstance(prepared.submission, ReportSubmission)):
        raise SubmitError("round policy differs from payload protocol")
    evidence.mkdir(parents=True, exist_ok=True)
    if prepared.context_round is not None:
        if round_windows != prepared.context_round:
            raise SubmitError("context commitment round differs from trusted submission round")
    elif round_windows is not None and "commitment_format" in round_windows:
        raise SubmitError("context round requires a context commitment sidecar")
    existing = _read_quiet(evidence / f"{phase}.journal")
    if existing and existing.get("state") == "finalized" and existing.get("payload_sha256") == prepared.commitment.hex():
        if prepared_path.read_bytes() != original:
            raise SubmitError("prepared file changed")
        return Outcome("finalized", False, existing.get("locator"))
    if not chain.is_registered(mining_netuid, keypair.ss58_address):
        raise SubmitError("hotkey has no uid on the mining subnet")
    if round_windows is None:
        windows = _windows(announcement, windows_doc)
    else:
        from types import SimpleNamespace
        from .round_config import require_round
        require_round(round_windows)
        # Bind the explicit window contract to the exact prepared payload.
        if (round_windows["genesis_hash"] != prepared.submission.genesis_hash.hex()
                or round_windows["block_hash"] != prepared.submission.block_hash.hex()
                or round_windows["block_number"] != prepared.submission.block_number
                or int(round_windows["round_date"].replace("-", "")) != prepared.submission.round_yyyymmdd
                or round_windows["release_prefix"] != prepared.submission.release_prefix.hex()
                or round_windows["reported_netuid"] != prepared.submission.netuid
                or round_windows["mining_netuid"] != mining_netuid):
            raise SubmitError("explicit round differs from prepared report")
        windows = SimpleNamespace(**round_windows)
    moment = chain.finalized_time_ms() if now_ms is None else now_ms
    if existing is None:
        if phase == "commit" and classify_commit(moment, windows) != "COMMIT_OK":
            raise SubmitError("commit is outside the announced window")
        if phase == "reveal":
            commit_journal = _read_quiet(evidence / "commit.journal")
            if commit_journal is None or commit_journal.get("state") != "finalized":
                raise SubmitError("reveal requires a finalized commit")
            if classify_reveal(moment, windows, committed_ok=True) != "REVEAL_OK":
                raise SubmitError("reveal is outside the announced window")
    fd, body, owner = _open_journal(evidence / f"{phase}.journal", phase, prepared.commitment.hex())
    try:
        if not owner:
            outcome = _resume(fd, body, chain, phase, prepared, evidence, mining_netuid)
        else:
            outcome = _send(fd, chain, phase, prepared, keypair, mining_netuid, evidence)
        if prepared_path.read_bytes() != original:
            raise SubmitError("prepared file changed")
        return outcome
    finally:
        os.close(fd)


def _send(fd, chain, phase: str, prepared, keypair, mining_netuid: int, evidence: Path) -> Outcome:
    variant = "Raw32" if phase == "commit" and prepared.commitment_envelope is None else "BigRaw"
    payload = prepared.wire_commitment if phase == "commit" else prepared.submission.encode()
    try:
        extrinsic_hash, handle = chain.sign(keypair, mining_netuid, variant, payload)
    except SigningRuntimeRefused:
        _store(fd, _body(fd) | {"state": "failed", "submission_attempted": False,
                               "reason": "signing_runtime_refused"})
        raise
    _store(fd, _body(fd) | {"state": "submitted", "extrinsic_hash": extrinsic_hash, "target_netuid": mining_netuid})
    try:
        inclusion = chain.broadcast(handle)
    except Exception as exc:
        _store(fd, _body(fd) | {"state": "unresolved", "reason": "broadcast did not return"})
        raise SubmitError("outcome unknown") from exc
    return _accept(fd, chain, phase, prepared, evidence, mining_netuid, inclusion, sent=True)


def _resume(fd, body: dict, chain, phase: str, prepared, evidence: Path, mining_netuid: int) -> Outcome:
    state = body.get("state")
    if state == "finalized":
        return Outcome("finalized", False, body.get("locator"))
    if state == "failed":
        if body.get("submission_attempted") is False:
            raise SubmitError("previous signing was refused; no submission attempted")
        error = body.get("dispatch_error")
        raise SubmitError(f"previous extrinsic failed: {error}" if error else "previous extrinsic failed")
    if state == "claimed" or not body.get("extrinsic_hash"):
        _store(fd, body | {"state": "unresolved", "reason": "no recorded extrinsic"})
        raise SubmitError("outcome unknown")
    found = chain.find(body["extrinsic_hash"])
    if found is None:
        _store(fd, body | {"state": "unresolved", "reason": "extrinsic not on the finalized chain"})
        raise SubmitError("outcome unknown")
    return _accept(fd, chain, phase, prepared, evidence, mining_netuid, found, sent=False)


def _accept(fd, chain, phase, prepared, evidence, mining_netuid, inclusion: Inclusion, *, sent: bool) -> Outcome:
    current = _body(fd)
    if not inclusion.success:
        error = inclusion.dispatch_error
        failure = {"state": "failed", "extrinsic_hash": inclusion.extrinsic_hash}
        if error:
            failure["dispatch_error"] = error
        _store(fd, current | failure)
        raise SubmitError(f"extrinsic failed: {error}" if error else "extrinsic failed")
    proof = chain.finality(inclusion.block_hash, inclusion.block_number)
    if not proof.get("matches"):
        _store(fd, current | {"state": "unresolved", "extrinsic_hash": inclusion.extrinsic_hash, "reason": "inclusion is not on the finalized chain"})
        raise SubmitError("outcome unknown")
    if phase == "commit":
        stored = chain.commitment(mining_netuid, ss58_encode(prepared.submission.hotkey))
        if stored != prepared.wire_commitment:
            _store(fd, current | {"state": "unresolved", "extrinsic_hash": inclusion.extrinsic_hash, "reason": "CommitmentOf does not hold the expected commitment"})
            raise SubmitError("outcome unknown")
        _write_copy(evidence, prepared.wire_commitment)
    locator = {
        "extrinsic_hash": inclusion.extrinsic_hash,
        "block_hash": inclusion.block_hash,
        "block_number": inclusion.block_number,
        "extrinsic_index": inclusion.extrinsic_index,
        "block_timestamp_ms": inclusion.block_timestamp_ms,
        "target_netuid": mining_netuid,
        "success": True,
        "finality": proof,
    }
    _store(fd, current | {"state": "finalized", "extrinsic_hash": inclusion.extrinsic_hash, "target_netuid": mining_netuid, "locator": locator})
    return Outcome("finalized", sent, locator)


def _write_copy(evidence: Path, digest: bytes) -> None:
    path = evidence / "commitment-of-before-reveal.json"
    field = "raw32" if len(digest) == 32 else "context_commitment"
    body = {"data_origin": "synthetic", field: "0x" + digest.hex()}
    if path.exists():
        current = json.loads(path.read_text())
        if current.get(field) != body[field]:
            raise SubmitError("copied commitment does not match this payload")
        return
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def _windows(announcement: dict, windows_doc: dict) -> RoundWindows:
    commit_ms = _int(windows_doc.get("commit_window_ms"), "commit window")
    reveal_ms = _int(windows_doc.get("reveal_window_ms"), "reveal window")
    product = commit_ms == 2 * 3_600_000 and reveal_ms == 2 * 3_600_000
    if product and windows_doc.get("accelerated") is True:
        raise SubmitError("two-hour windows must not be labeled accelerated")
    if not product and windows_doc.get("accelerated") is not True:
        raise SubmitError("windows shorter than two hours must be labeled accelerated")
    return RoundWindows(
        block_timestamp_ms=_int(announcement.get("block_timestamp_ms"), "block timestamp"),
        announced_at_ms=_int(announcement.get("announced_at_ms"), "announced at"),
        commit_window_ms=_int(windows_doc.get("commit_window_ms"), "commit window"),
        reveal_window_ms=_int(windows_doc.get("reveal_window_ms"), "reveal window"),
    )


def _refuse_wrong_chain(genesis: str, announcement: dict, *, allow_mainnet=False) -> None:
    if type(allow_mainnet) is not bool:
        raise SubmitError("Mainnet submission opt-in must be boolean")
    connected = genesis.lower()
    announced = str(announcement.get("genesis", "")).lower()
    if not announced.startswith("0x"):
        announced = "0x" + announced
    if connected == MAINNET_GENESIS.lower() and not allow_mainnet:
        raise SubmitError("Mainnet report submission requires explicit --allow-mainnet-report")
    if connected != announced:
        raise SubmitError("connected chain does not match the announcement")


def _open_journal(path: Path, phase: str, payload_sha256: str):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fresh = True
    except FileExistsError:
        fd = os.open(path, os.O_RDWR)
        fresh = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise SubmitError("another command holds this phase") from exc
    if fresh:
        body = {"extrinsic_hash": None, "payload_sha256": payload_sha256, "phase": phase, "state": "claimed"}
        _store(fd, body)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return fd, body, True
    body = _body(fd)
    if body.get("payload_sha256") != payload_sha256:
        os.close(fd)
        raise SubmitError("journal belongs to a different payload")
    return fd, body, False


def _body(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 1_000_000)
    return json.loads(raw.decode()) if raw.strip() else {}


def _store(fd: int, body: dict) -> None:
    data = (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise SubmitError("could not persist submission journal")
        view = view[written:]
    os.fsync(fd)


def _read_quiet(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _hash32(value, name: str) -> bytes:
    if not isinstance(value, str) or not value.startswith(("0x", "0X")):
        raise SubmitError(f"announcement {name} is not a hash")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as exc:
        raise SubmitError(f"announcement {name} is not a hash") from exc
    if len(raw) != 32:
        raise SubmitError(f"announcement {name} is not a hash")
    return raw


def _int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SubmitError(f"announcement {name} is not an integer")
    return value
