"""Prepare a local commitment. No signature and no transaction.

The current report-only protocol commits both subnet IDs in 203 bytes. Historical
207-byte deposit-policy preimages commit only the reported subnet; their mining
subnet remains an explicitly unauthenticated sidecar checked at submission.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .keys import ss58_decode
from .protocol.preimage import Submission, ReportSubmission, REPORT_POLICY, commitment_of, decode
from .rpc import endpoint_label

SCHEMA = "bfpoc-prepare-v1"
DEPOSIT_LABEL = "miner-supplied-not-verified"
MINING_NOTE = (
    "mining_netuid is not in the 207-byte preimage. The commitment hash does not "
    "authenticate it. A later submit must enforce the configured mining subnet independently."
)


class PrepareError(Exception):
    """The command refused to write a ready submission."""


@dataclass(frozen=True)
class Prepared:
    mining_netuid: int
    submission: Submission | ReportSubmission
    commitment: bytes
    path: Path
    context_round: dict | None = None
    commitment_envelope: bytes | None = None

    @property
    def wire_commitment(self):
        return self.commitment if self.commitment_envelope is None else self.commitment_envelope


def prepare(
    *,
    announcement: dict,
    reported_netuid: int,
    mining_netuid: int,
    deposit_block: int | None = None,
    deposit_event: int | None = None,
    hotkey: str,
    coldkey: str,
    endpoint: str,
    out: Path,
    compute_fn,
    release_prefix_bytes: bytes,
    salt: bytes | None = None,
    eligibility_policy: str = "attributed-deposit-before-b-v1",
    context_round: dict | None = None,
) -> Prepared:
    """Calculate and write one sidecar.

    ``compute_fn(netuid, block_hash, block_number)`` returns the engine result.
    ``block_hash`` is the canonical ``0x`` lowercase hex string. The payload still stores the raw bytes.
    """
    fields = _announcement(announcement, reported_netuid)
    _u16("reported_netuid", reported_netuid)
    _u16("mining_netuid", mining_netuid)
    if reported_netuid == 0 or mining_netuid == 0:
        raise PrepareError("root is not a subnet")
    report_only = eligibility_policy == REPORT_POLICY
    if report_only:
        if deposit_block is not None or deposit_event is not None:
            raise PrepareError("report-only preparation does not accept deposit references")
    elif eligibility_policy == "attributed-deposit-before-b-v1":
        _u32("deposit_block", deposit_block)
        _u16("deposit_event", deposit_event)
        if deposit_block >= fields["block_number"]:
            raise PrepareError("deposit block is not strictly before B")
    else:
        raise PrepareError("unsupported report policy")
    hotkey_bytes = _ss58("hotkey", hotkey)
    coldkey_bytes = _ss58("coldkey", coldkey)
    if out.exists():
        raise PrepareError("output already exists")
    engine = compute_fn(reported_netuid, _block_hash_hex(fields["block_hash"]), fields["block_number"])
    ppm, out2, out3 = _ready_outputs(engine, fields, reported_netuid)
    if salt is None:
        salt = secrets.token_bytes(16)
    if not isinstance(salt, bytes) or len(salt) != 16:
        raise PrepareError("salt must be 16 bytes")
    prefix = release_prefix_bytes
    if not isinstance(prefix, bytes) or len(prefix) != 16:
        raise PrepareError("release prefix must be 16 bytes")
    submission_class = ReportSubmission if report_only else Submission
    context = {"mining_netuid": mining_netuid} if report_only else {
        "deposit_block": deposit_block, "deposit_event": deposit_event}
    submission = submission_class(
        genesis_hash=fields["genesis"],
        hotkey=hotkey_bytes,
        coldkey=coldkey_bytes,
        netuid=reported_netuid,
        round_yyyymmdd=fields["round"],
        block_number=fields["block_number"],
        block_hash=fields["block_hash"],
        **context,
        out1_ppm=ppm,
        out2_rao=out2,
        out3_rao=out3,
        release_prefix=prefix,
        salt=salt,
    )
    preimage = submission.encode()
    digest = commitment_of(preimage)
    envelope = None
    if context_round is not None:
        from .protocol.context_commitment import build_commitment, decode_commitment
        envelope = build_commitment(context_round, preimage)
        digest = decode_commitment(envelope).report_hash
    body = {
        "schema": ("noah-report-prepare/2" if envelope is not None else
                   "noah-report-prepare/1" if report_only else SCHEMA),
        **({"context_round": context_round, "commitment_envelope_hex": envelope.hex()} if envelope is not None else {}),
        "ready": True,
        **({} if report_only else {"deposit_reference_status": DEPOSIT_LABEL}),
        "mining_netuid": mining_netuid,
        "mining_netuid_authenticated_by_commitment": report_only,
        "mining_netuid_note": "Both subnet IDs are committed." if report_only else MINING_NOTE,
        "reveal_preimage_hex": preimage.hex(),
        "commitment_sha256": digest.hex(),
        "endpoint_label": endpoint_label(endpoint),
    }
    _publish_exclusive(out, (json.dumps(body, indent=2, sort_keys=True) + "\n").encode())
    return Prepared(mining_netuid, submission, digest, out, context_round, envelope)


def load_prepared(path: Path) -> Prepared:
    """Reload a sidecar. Refuses a hash mismatch or a changed preimage."""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PrepareError("prepared file is not readable") from exc
    contextual = document.get("schema") == "noah-report-prepare/2"
    report_only = contextual or document.get("schema") == "noah-report-prepare/1"
    if document.get("schema") not in (SCHEMA, "noah-report-prepare/1", "noah-report-prepare/2") or document.get("ready") is not True:
        raise PrepareError("file is not a ready submission")
    if not report_only and document.get("deposit_reference_status") != DEPOSIT_LABEL:
        raise PrepareError("deposit reference is not labeled miner-supplied")
    if document.get("mining_netuid_authenticated_by_commitment") is not report_only:
        raise PrepareError("mining subnet sidecar must stay unauthenticated")
    try:
        preimage = bytes.fromhex(document["reveal_preimage_hex"])
        stored = bytes.fromhex(document["commitment_sha256"])
    except (KeyError, ValueError) as exc:
        raise PrepareError("prepared file is not readable") from exc
    context_round = None
    envelope = None
    if contextual:
        from .protocol.context_commitment import build_commitment, decode_commitment
        try:
            context_round = document["context_round"]
            envelope = bytes.fromhex(document["commitment_envelope_hex"])
            if build_commitment(context_round, preimage) != envelope or decode_commitment(envelope).report_hash != stored:
                raise ValueError("commitment envelope differs")
        except (KeyError, TypeError, ValueError) as exc:
            raise PrepareError("context commitment does not match the round and reveal") from exc
    elif commitment_of(preimage) != stored:
        raise PrepareError("commitment hash does not match the reveal")
    submission = decode(preimage)
    if isinstance(submission, ReportSubmission) != report_only:
        raise PrepareError("prepared schema and payload version differ")
    mining = document.get("mining_netuid")
    _u16("mining_netuid", mining)
    if report_only and mining != submission.mining_netuid:
        raise PrepareError("prepared mining subnet differs from committed subnet")
    return Prepared(mining, submission, stored, Path(path), context_round, envelope)


def _publish_exclusive(path: Path, payload: bytes) -> None:
    """Link a fully written temp onto ``path``. The link fails if ``path`` exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PrepareError("could not write the prepared file")
            view = view[written:]
        os.fsync(fd)
    except Exception:
        os.close(fd)
        os.unlink(temporary)
        raise
    os.close(fd)
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        os.unlink(temporary)
        raise PrepareError("output already exists") from exc
    except Exception:
        os.unlink(temporary)
        raise
    os.unlink(temporary)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _announcement(announcement: dict, reported_netuid: int) -> dict:
    if not isinstance(announcement, dict):
        raise PrepareError("announcement is not an object")
    for name in ("genesis", "round", "block_number", "block_hash", "block_timestamp_ms"):
        if name not in announcement:
            raise PrepareError(f"announcement missing {name}")
    genesis = _hash32("genesis", announcement["genesis"])
    block_hash = _hash32("block_hash", announcement["block_hash"])
    block_number = announcement["block_number"]
    _u32("block_number", block_number)
    timestamp = announcement["block_timestamp_ms"]
    _u64("block_timestamp_ms", timestamp)
    round_date = announcement["round"]
    _u32("round", round_date)
    _calendar_date(round_date)
    if "netuid" in announcement:
        _u16("announcement.netuid", announcement["netuid"])
        if announcement["netuid"] != reported_netuid:
            raise PrepareError("announcement netuid does not match the reported subnet")
    return {
        "genesis": genesis,
        "block_hash": block_hash,
        "block_number": block_number,
        "round": round_date,
    }


def _ready_outputs(engine: dict, fields: dict, reported_netuid: int) -> tuple[int, int, int]:
    if not isinstance(engine, dict) or engine.get("status") != "OK":
        raise PrepareError("calculation is not ready")
    if engine.get("netuid") != reported_netuid:
        raise PrepareError("calculation subnet does not match the reported subnet")
    if engine.get("block_number") != fields["block_number"]:
        raise PrepareError("calculation block does not match B")
    block_hash = engine.get("block_hash")
    if not isinstance(block_hash, str) or block_hash.lower() != _block_hash_hex(fields["block_hash"]):
        raise PrepareError("calculation block hash does not match B")
    coverage = engine.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("coverage_gap_exceeds_holder") is not False:
        raise PrepareError("coverage gap is not explicitly false")
    ppm = _output_int(engine, "output1", "drop_ppm", 32)
    out2 = _output_int(engine, "output2", "alpha_required_rao", 64)
    out3 = _output_int(engine, "output3", "alpha_required_rao", 64)
    return ppm, out2, out3


def _output_int(engine: dict, name: str, field: str, bits: int) -> int:
    output = engine.get(name)
    if not isinstance(output, dict) or output.get("status") != "OK":
        raise PrepareError(f"{name} is not ready")
    return _uint(f"{name}.{field}", output.get(field), bits)


def _uint(name: str, value, bits: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrepareError(f"{name} is not an integer")
    if value < 0 or value >= 1 << bits:
        raise PrepareError(f"{name} is outside u{bits}")
    return value


def _u16(name: str, value) -> int:
    return _uint(name, value, 16)


def _u32(name: str, value) -> int:
    return _uint(name, value, 32)


def _u64(name: str, value) -> int:
    return _uint(name, value, 64)


def _block_hash_hex(raw: bytes) -> str:
    return "0x" + raw.hex()


def _hash32(name: str, value) -> bytes:
    if not isinstance(value, str) or not value.startswith(("0x", "0X")):
        raise PrepareError(f"{name} is not a 32-byte hash")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as exc:
        raise PrepareError(f"{name} is not a 32-byte hash") from exc
    if len(raw) != 32:
        raise PrepareError(f"{name} is not a 32-byte hash")
    return raw


def _ss58(name: str, value: str) -> bytes:
    try:
        return ss58_decode(value)
    except (TypeError, ValueError) as exc:
        raise PrepareError(f"{name} is not a public key") from exc


def _calendar_date(value: int) -> None:
    text = f"{value:08d}"
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise PrepareError("round is not a calendar date") from exc
