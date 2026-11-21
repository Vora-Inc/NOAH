"""Resumable deposit history from block 0.

Each finished range is one new segment. The checkpoint stores the chain of
segment digests and is replaced atomically. An unfinished range is not
recorded, so a later process continues at the last complete block.

This does not open a round and does not calculate a score. Attribution is
``receipts_for_block``, the same reader the collector uses.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import resource
import tempfile
import time
from pathlib import Path

from noah.chain.deposits import NotADepositEvent
from noah.chain.sources import Refusal

from .collector import _timestamp_ms, receipts_for_block
from . import history as history_mod
from .history import SCHEMA, checkpoint_problem, segment_chain_digest, segments_problem
from .keys import ss58_decode, ss58_encode

ELIGIBILITY_POLICY = "attributed-deposit-before-b-v1"
ARK_VALIDATOR_HOTKEY = "5DLQGc9DoabXWGp4s4vktHyGEA3itg8twResPfDZ4qw8RZJV"
CHECKPOINT_NAME = "checkpoint.json"
# Prefix digest already checked in this process. A new process starts empty.
_opened: dict[str, str] = {}


class BootstrapError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def account_hex(value: str) -> str:
    text = value.strip()
    if text.startswith(("0x", "0X")):
        text = text[2:]
    if len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text):
        return text.lower()
    return ss58_decode(text).hex()


def extend_history(
    substrate,
    directory: Path,
    start: int,
    end: int,
    *,
    ark_coldkey: str,
    ark_validator_hotkey: str,
    eligibility_policy_version: str = ELIGIBILITY_POLICY,
    deadline: float | None = None,
) -> dict:
    """Decode ``start`` through ``end``, inclusive, and commit that range."""
    directory = Path(directory)
    coldkey = account_hex(ark_coldkey)
    hotkey = account_hex(ark_validator_hotkey)
    existing = _load(directory)
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        return _stopped(existing, "incompatible_range")
    if existing is None:
        if start != 0:
            return _stopped(None, "history_not_from_genesis")
        try:
            genesis = _genesis(substrate)
        except BootstrapError as exc:
            return _stopped(None, exc.reason)
        observed_from = None
        previous_hash = None
        receipts = []
        segments = []
    else:
        problem = _resume_problem(directory, existing, coldkey, hotkey, eligibility_policy_version)
        if problem is not None:
            return _stopped(existing, problem)
        try:
            genesis = _genesis(substrate)
        except BootstrapError as exc:
            return _stopped(existing, exc.reason)
        if genesis != existing["genesis_hash"]:
            return _stopped(existing, "history_genesis_mismatch")
        if start != existing["through_block"] + 1:
            return _stopped(existing, "history_gap")
        observed_from = existing["observed_from_ms"]
        previous_hash = existing["head_hash"]
        receipts = list(existing["receipts"])
        segments = list(existing["segments"])
    started = time.monotonic()
    temp = None
    try:
        digest, temp, final, observed_from, head_hash, added = _write_range(
            substrate,
            directory,
            start,
            end,
            coldkey,
            hotkey,
            genesis,
            previous_hash,
            observed_from,
            deadline,
        )
        segment = {
            "file": f"segments/{final.name}",
            "from_block": start,
            "sha256": digest,
            "through_block": end,
        }
        segments.append(segment)
        if history_mod._gzip_sha256(final) != digest:
            final.unlink(missing_ok=True)
            raise BootstrapError("history_responses")
        document = _document(
            coldkey,
            hotkey,
            eligibility_policy_version,
            genesis,
            head_hash,
            observed_from,
            receipts + added,
            segments,
            end,
        )
        _atomic_json(directory / CHECKPOINT_NAME, document)
        _opened[_session_key(directory)] = document["responses_sha256"]
    except BootstrapError as exc:
        if temp is not None:
            temp.unlink(missing_ok=True)
        return _stopped(existing, exc.reason, elapsed=time.monotonic() - started)
    stored = _stored_bytes(directory)
    return {
        "advanced": True,
        "blocks": end - start + 1,
        "elapsed_s": round(time.monotonic() - started, 3),
        "error": None,
        "max_rss_kb": _max_rss_kb(),
        "stored_bytes": stored,
        "through_block": end,
    }


def sample_history(
    substrate,
    directory: Path,
    start: int,
    end: int,
    *,
    ark_coldkey: str,
    ark_validator_hotkey: str,
    eligibility_policy_version: str = ELIGIBILITY_POLICY,
    deadline: float | None = None,
) -> dict:
    """Decode a bounded range and store it as a sample, not as coverage.

    A sample is how a recent window is measured without pretending the gap
    back to genesis was read. It never updates ``checkpoint.json``.
    """
    directory = Path(directory)
    before_bytes = (directory / CHECKPOINT_NAME).read_bytes() if (directory / CHECKPOINT_NAME).is_file() else None
    coldkey = account_hex(ark_coldkey)
    hotkey = account_hex(ark_validator_hotkey)
    started = time.monotonic()
    temp = None
    try:
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            raise BootstrapError("incompatible_range")
        genesis = _genesis(substrate)
        digest, temp, final, _observed, _head, added = _write_range(
            substrate,
            directory,
            start,
            end,
            coldkey,
            hotkey,
            genesis if start == 0 else None,
            None,
            None,
            deadline,
            folder_name="samples",
        )
        # A sample may start away from genesis. Parent links are checked
        # inside the range. The file name stays under samples/, not segments/.
        os.replace(final, directory / "samples" / final.name)
        summary = {
            "blocks": end - start + 1,
            "complete": True,
            "coverage": "sample",
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": None,
            "from_block": start,
            "max_rss_kb": _max_rss_kb(),
            "receipts": len(added),
            "eligibility_policy_version": eligibility_policy_version,
            "sha256": digest,
            "stored_bytes": (directory / "samples" / final.name).stat().st_size,
            "through_block": end,
        }
        _atomic_json(directory / "samples" / f"{start:012d}-{end:012d}.json", summary)
    except BootstrapError as exc:
        if temp is not None:
            temp.unlink(missing_ok=True)
        summary = {
            "blocks": 0,
            "complete": False,
            "coverage": "sample",
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": exc.reason,
            "from_block": start,
            "max_rss_kb": _max_rss_kb(),
            "through_block": None,
        }
    after_bytes = (directory / CHECKPOINT_NAME).read_bytes() if (directory / CHECKPOINT_NAME).is_file() else None
    if after_bytes != before_bytes:
        raise BootstrapError("sample_moved_checkpoint")
    return summary


def verify_history(directory: Path) -> dict:
    """Re-attribute the saved segments. This reads no endpoint."""
    directory = Path(directory)
    document = _load(directory)
    if document is None:
        return {"error": "checkpoint_missing", "ok": False, "receipts": []}
    problem = segments_problem(document, directory)
    if problem is not None:
        return {"error": problem, "ok": False, "receipts": []}
    try:
        coldkey = ss58_encode(bytes.fromhex(document["ark_coldkey"]))
        hotkey = ss58_encode(bytes.fromhex(document["ark_validator_hotkey"]))
        receipts = []
        previous = None
        first_timestamp = None
        expected = 0
        for segment in document["segments"]:
            with gzip.open(directory / segment["file"], "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    number = record["block"]
                    if number != expected:
                        raise BootstrapError("history_gap")
                    if number == 0:
                        if record["hash"][2:] != document["genesis_hash"]:
                            raise BootstrapError("history_genesis_mismatch")
                    elif record["parent"].lower() != previous:
                        raise BootstrapError("history_fork")
                    if number != 0 and not isinstance(record.get("timestamp_ms"), int):
                        raise BootstrapError(f"block {number}: timestamp extrinsic missing")
                    if first_timestamp is None and isinstance(record.get("timestamp_ms"), int):
                        first_timestamp = record["timestamp_ms"]
                    receipts.extend(receipts_for_block(
                        {"hash": record["hash"], "number": number, "parent": record["parent"]},
                        record["events"],
                        record["extrinsics"],
                        coldkey,
                        hotkey,
                    ))
                    previous = record["hash"].lower()
                    expected += 1
        if expected != document["through_block"] + 1:
            raise BootstrapError("history_gap")
        if previous != document["head_hash"].lower():
            raise BootstrapError("history_fork")
        if first_timestamp != document["observed_from_ms"]:
            raise BootstrapError("incompatible_checkpoint")
        if receipts != document["receipts"]:
            raise BootstrapError("history_responses")
    except (BootstrapError, KeyError, TypeError, json.JSONDecodeError) as exc:
        reason = exc.reason if isinstance(exc, BootstrapError) else "incompatible_checkpoint"
        return {"error": reason, "ok": False, "receipts": []}
    return {"error": None, "ok": True, "receipts": receipts, "through_block": document["through_block"]}


def status_history(directory: Path) -> dict:
    directory = Path(directory)
    document = _load(directory)
    if document is None:
        return {"error": "checkpoint_missing", "through_block": None}
    if document.get("schema") != SCHEMA or not isinstance(document.get("segments"), list):
        problem = "incompatible_checkpoint"
    else:
        problem = segments_problem(document, directory)
    return {
        "ark_coldkey": document.get("ark_coldkey"),
        "ark_validator_hotkey": document.get("ark_validator_hotkey"),
        "eligibility_policy_version": document.get("eligibility_policy_version"),
        "error": problem,
        "from_block": document.get("from_block"),
        "genesis_hash": document.get("genesis_hash"),
        "next_block": None if problem or not isinstance(document.get("through_block"), int) else document["through_block"] + 1,
        "segments": len(document["segments"]) if isinstance(document.get("segments"), list) else 0,
        "stored_bytes": _stored_bytes(directory),
        "through_block": document.get("through_block"),
    }


def _write_range(substrate, directory, start, end, coldkey, hotkey, genesis, previous_hash, observed_from, deadline, folder_name="segments"):
    folder = directory / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{start:012d}-{end:012d}.jsonl.gz"
    temp = folder / f".{final.name}.tmp"
    temp.unlink(missing_ok=True)
    ark = ss58_encode(bytes.fromhex(coldkey))
    validator = ss58_encode(bytes.fromhex(hotkey))
    hasher = hashlib.sha256()
    added = []
    head_hash = previous_hash
    number = start
    handle = gzip.GzipFile(filename=str(temp), mode="wb", mtime=0)
    try:
        for number in range(start, end + 1):
            if deadline is not None and time.monotonic() > deadline:
                raise BootstrapError("time_budget")
            block_hash = _block_hash(substrate, number)
            if number == 0:
                if genesis is None or block_hash[2:] != genesis:
                    raise BootstrapError("history_genesis_mismatch")
            record = _read_block(substrate, number, block_hash)
            if previous_hash is not None and record["parent"] != str(previous_hash).lower():
                raise BootstrapError(f"block {number}: parent mismatch")
            found = receipts_for_block(
                {"hash": record["hash"], "number": number, "parent": record["parent"]},
                record["events"],
                record["extrinsics"],
                ark,
                validator,
            )
            if observed_from is None and isinstance(record.get("timestamp_ms"), int):
                observed_from = record["timestamp_ms"]
            line = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            hasher.update(line)
            handle.write(line)
            added.extend(found)
            previous_hash = record["hash"]
            head_hash = record["hash"]
        if observed_from is None:
            raise BootstrapError("timestamp extrinsic missing")
    except (Exception, Refusal, NotADepositEvent) as exc:
        handle.close()
        temp.unlink(missing_ok=True)
        if isinstance(exc, BootstrapError) and str(exc).startswith("block "):
            raise
        raise BootstrapError(f"block {number}: {type(exc).__name__}") from exc
    handle.close()
    _fsync(temp)
    os.replace(temp, final)
    return hasher.hexdigest(), None, final, observed_from, head_hash, added


def _read_block(substrate, number: int, block_hash: str) -> dict:
    responses = []
    original = substrate.rpc_request

    def recorded(method, params=None, result_handler=None):
        response = original(method, params, result_handler)
        if result_handler is None and isinstance(response, dict) and "result" in response:
            safe = _jsonable(response["result"])
            digest, size = _result_sha(safe)
            stored = list(params) if isinstance(params, (list, tuple)) else []
            responses.append({
                "byte_length": size,
                "method": method,
                "params": [str(item) for item in stored],
                "result": safe,
                "sha256": digest,
            })
        return response

    substrate.rpc_request = recorded
    try:
        decoded = substrate.get_block(block_hash=block_hash)
        header = decoded.get("header") if isinstance(decoded, dict) else None
        if not isinstance(header, dict) or "parentHash" not in header or "number" not in header:
            header = substrate.rpc_request("chain_getHeader", [block_hash])["result"]
        if int(header["number"], 16) != number:
            raise BootstrapError(f"block {number}: header number mismatch")
        parent = str(header["parentHash"]).lower()
        events = substrate.query("System", "Events", block_hash=block_hash).value or []
    finally:
        substrate.rpc_request = original
    extrinsics = []
    for item in decoded.get("extrinsics") or []:
        value = item.value if hasattr(item, "value") else item
        extrinsics.append(_jsonable(value if isinstance(value, dict) else {}))
    events = _jsonable(events)
    if not isinstance(events, list):
        raise BootstrapError(f"block {number}: events were not a list")
    timestamp = _timestamp_ms(decoded)
    if timestamp is None and number != 0:
        raise BootstrapError(f"block {number}: timestamp extrinsic missing")
    return {
        "block": number,
        "events": events,
        "extrinsics": extrinsics,
        "hash": block_hash.lower(),
        "parent": parent,
        "responses": responses,
        "timestamp_ms": timestamp,
    }


def _document(coldkey, hotkey, policy, genesis, head_hash, observed_from, receipts, segments, through):
    document = {
        "ark_coldkey": coldkey,
        "ark_validator_hotkey": hotkey,
        "eligibility_policy_version": policy,
        "from_block": 0,
        "genesis_hash": genesis,
        "head_hash": head_hash,
        "observed_from_ms": observed_from,
        "receipts": receipts,
        "responses_sha256": segment_chain_digest(segments),
        "schema": SCHEMA,
        "segments": segments,
        "through_block": through,
    }
    problem = checkpoint_problem(
        document,
        genesis_hash=genesis,
        responses_sha256=document["responses_sha256"],
        ark_coldkey=coldkey,
        ark_validator_hotkey=hotkey,
        eligibility_policy_version=policy,
    )
    if problem is not None:
        raise BootstrapError(problem)
    return document


def _session_key(directory: Path) -> str:
    return str(Path(directory).resolve())


def _resume_problem(directory, document, coldkey, hotkey, policy) -> str | None:
    """Full prefix check when a session opens. Later ranges in that process do not reread it."""
    key = _session_key(directory)
    if _opened.get(key) == document.get("responses_sha256"):
        return checkpoint_problem(
            document,
            genesis_hash=document.get("genesis_hash"),
            responses_sha256=document.get("responses_sha256"),
            ark_coldkey=coldkey,
            ark_validator_hotkey=hotkey,
            eligibility_policy_version=policy,
        )
    problem = _checkpoint_problem(document, directory, coldkey, hotkey, policy)
    if problem is None:
        _opened[key] = document["responses_sha256"]
    return problem


def _checkpoint_problem(document, directory, coldkey, hotkey, policy) -> str | None:
    problem = segments_problem(document, directory)
    if problem is not None:
        return problem
    return checkpoint_problem(
        document,
        genesis_hash=document.get("genesis_hash"),
        responses_sha256=document.get("responses_sha256"),
        ark_coldkey=coldkey,
        ark_validator_hotkey=hotkey,
        eligibility_policy_version=policy,
    )


def _load(directory: Path):
    path = Path(directory) / CHECKPOINT_NAME
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"schema": "unreadable"}
    return document


def _stopped(existing, reason: str, elapsed: float = 0) -> dict:
    through = None if not isinstance(existing, dict) else existing.get("through_block")
    return {
        "advanced": False,
        "elapsed_s": round(elapsed, 3),
        "error": reason,
        "max_rss_kb": _max_rss_kb(),
        "through_block": through,
    }


def _genesis(substrate) -> str:
    value = substrate.get_block_hash(0)
    text = str(value).lower()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != 64:
        raise BootstrapError("genesis_unavailable")
    return text


def _block_hash(substrate, number: int) -> str:
    try:
        value = substrate.get_block_hash(number)
    except Exception as exc:
        raise BootstrapError(f"block {number}: {type(exc).__name__}") from exc
    text = str(value).lower()
    if not text.startswith("0x"):
        text = "0x" + text
    if len(text) != 66 or value is None:
        raise BootstrapError(f"block {number}: hash unavailable")
    return text


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise BootstrapError("undecodable value float")
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return _jsonable(value.value)
    raise BootstrapError(f"undecodable value {type(value).__name__}")


def _result_sha(result) -> tuple[str, int]:
    body = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(body).hexdigest(), len(body)


def _atomic_json(path: Path, document) -> None:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    # A fresh exclusive private staging file cannot follow a pre-existing .tmp
    # symlink. History checkpoints are local; export is a separate operation.
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stored_bytes(directory: Path) -> int:
    total = 0
    if not directory.exists():
        return 0
    for path in directory.rglob("*"):
        if path.is_file() and not path.name.endswith(".tmp"):
            total += path.stat().st_size
    return total


def _max_rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
