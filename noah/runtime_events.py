"""Opt-in, best-effort application events; never authoritative chain evidence.

Set NOAH_EVENT_LOG to an absolute local JSONL file. A bounded background worker
appends and fsyncs records; application threads never perform event-file I/O or
wait for that worker. A full queue, lock contention or I/O failure drops events.
The 16 MiB file cap rotates three retained archives. Sequence gaps and source_dropped expose
losses. A separate health snapshot is refreshed even without a later event;
stale health is unavailable evidence, never proof that logging is healthy.
Abrupt process death can still lose queued events. These files are diagnostics.

Only enumerated labels, bounded integers and validated hashes are accepted.
Never pass exception text, endpoints, wallet objects or signing data.
Completion describes a function return, not chain inclusion, finality or credit.
"""

import atexit
import json
import os
import queue
import re
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from .diagnostics import severity_for, debug

SCHEMA = "noah-runtime-event/1"
MAX_BYTES = 16 * 1024 * 1024
BACKUPS = 3
HEALTH_INTERVAL = 5.0
QUEUE_SIZE = 4096
EVENTS = frozenset(
    (
        "stage.entered",
        "stage.completed",
        "stage.failed",
        "registration.observed",
        "contribution.observed",
        "calculation.recorded",
        "publication.state",
        "eligibility.state",
        "history.state",
        "deposit.attribution",
        "entry.qualification",
        "publication.prepared",
        "epoch.observed",
        "accounting.observed",
    )
)
STAGES = frozenset(
    (
        "validator.run",
        "validator.startup",
        "validator.tick",
        "validator.ingestion",
        "validator.eligibility",
        "validator.history",
        "validator.attribution",
        "validator.preparation",
        "validator.settlement",
        "validator.accounting",
        "validator.scoring",
        "validator.agreement",
        "validator.evidence",
        "validator.publication",
        "miner.preview",
        "miner.reconciliation",
        "miner.submission",
        "miner.ledger",
    )
)
OUTCOMES = frozenset(
    (
        "ready",
        "scanning",
        "observed",
        "already_scanned",
        "attributed",
        "qualified",
        "withheld",
        "unavailable",
        "partial",
        "nothing_due",
        "below_minimum",
        "refused",
        "blocked",
        "declined",
        "superseded",
        "awaiting_approval",
        "approved",
        "submitting",
        "submitted",
        "finalized",
        "credited",
        "calculated",
        "reconciled",
        "rejected",
        "uncertain",
        "unqualified",
        "failed",
        "abandoned",
        "registered",
        "received",
        "returned",
        "raised",
        "waiting",
        "published",
        "day_catchup_lock_busy",
        "day_catchup_already_published",
        "day_catchup_abandoned",
        "day_catchup_pending",
        "held_by_other_writer",
        "already_published",
        "day_zero_catch_up_refused",
    )
)
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_context = ContextVar("noah_runtime_event_context", default=None)
_sink = None
_sink_lock = threading.Lock()


def _fields(values):
    """Project plain primitive values only; never stringify arbitrary objects."""
    result = {}
    if values.get("severity") in ("debug", "info", "warning", "error", "critical"):
        result["severity"] = values["severity"]
    for key in (
        "netuid",
        "day",
        "height",
        "uid",
        "period",
        "token_netuid",
        "amount_rao",
        "locked_arrival_rao",
        "unlocked_rao",
        "event_ordinal",
        "extrinsic_index",
        "status_code",
        "head_block",
        "from_block",
        "to_block",
        "settle_blocks",
        "tempo",
        "day_start_ts",
        "day_end_ts",
        "source_count",
        "elapsed_ms",
        "changed_uid_count",
        "seat_count",
        "entry_day",
    ):
        value = values.get(key)
        if type(value) is int and 0 <= value <= (255 if key == "status_code" else 2**53 - 1):
            result[key] = value
    for key in ("genesis_hash", "tx_hash", "block_hash"):
        value = values.get(key)
        if type(value) is str and len(value) == 66 and _HASH.fullmatch(value):
            result[key] = value.lower()
    for key in ("artifact_sha256", "candidate_sha256"):
        value = values.get(key)
        if type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value):
            result[key] = value
    # Exact monetary amounts outside JavaScript's safe integer range are text.
    for key in ("amount_rao", "locked_arrival_rao", "unlocked_rao"):
        value = values.get(key)
        if type(value) is int and 2**53 <= value < 2**128:
            result[key + "_decimal"] = str(value)
    for key in ("miner_hotkey", "validator_hotkey", "seat_hotkey", "origin_coldkey", "ark_address"):
        value = values.get(key)
        if type(value) is str and 32 <= len(value) <= 64:
            from .chain.ss58 import NotAnAddress, public_key_of

            try:
                public_key_of(value)
            except NotAnAddress:
                pass
            else:
                result[key] = value
    for key in ("intent_id", "attempt_id"):
        value = values.get(key)
        if type(value) is str and len(value) in (32, 36):
            try:
                result[key] = str(uuid.UUID(value))
            except ValueError:
                pass
    for key, allowed in (
        (
            "reason_code",
            {"head_unreadable", "not_settled", "entry_not_confirmed", "attribution_unavailable"},
        ),
        ("publish_mode", {"direct", "sealed"}),
        ("route", {"direct", "proxy"}),
    ):
        value = values.get(key)
        if type(value) is str and value in allowed:
            result[key] = value
    value = values.get("outcome")
    if type(value) is str and len(value) <= 32 and value in OUTCOMES:
        result["outcome"] = value
    return result


class EventSink:
    """One process/run, bounded queue, append-only shared log with nonblocking lock."""

    def __init__(self, path):
        self.path = Path(path)
        self.run_id = str(uuid.uuid4())
        self.pid = os.getpid()
        self.sequence = 0
        self.dropped = 0
        self.written = 0
        self.rotations = 0
        self.active_bytes = None
        self.free_bytes = None
        self.last_success = None
        self.last_error = None
        self.health_write_failed = False
        self._reported_health = None
        self._last_health = 0.0
        self.pending = queue.Queue(maxsize=QUEUE_SIZE)
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self._work, name="noah-event-writer", daemon=True)
        self.worker.start()

    def enqueue(self, source, event, stage, fields):
        # A nonblocking lock bounds contention between application threads too.
        if not self.lock.acquire(blocking=False):
            self.dropped += 1
            return
        try:
            self.sequence += 1
            record = {
                "schema": SCHEMA,
                "event_id": str(uuid.uuid4()),
                "source": source,
                "event": event,
                "stage": stage,
                "run_id": self.run_id,
                "source_sequence": self.sequence,
                "source_dropped": self.dropped,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "evidence": "application_event",
                "public_finality_verified": False,
                **fields,
            }
            try:
                self.pending.put_nowait(record)
            except queue.Full:
                self.dropped += 1
        finally:
            self.lock.release()

    def _append(self, record):
        from .runtime_event_files import append

        data = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        if len(data) > 4096:
            raise ValueError("event too large")
        facts = append(self.path, data, max_bytes=MAX_BYTES, backups=BACKUPS)
        self.active_bytes = facts["active_bytes"]
        self.free_bytes = facts["free_bytes"]
        self.rotations += int(facts["rotated"])

    def health(self):
        """In-memory snapshot; safe for status callers even when disk writes fail."""
        return {
            "schema": "noah-event-sink-health/1",
            "run_id": self.run_id,
            "pid": self.pid,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": "degraded" if self.last_error or self.health_write_failed else "ready",
            "losses_detected": bool(self.dropped),
            "source_dropped": self.dropped,
            "source_written": self.written,
            "source_sequence": self.sequence,
            "queue_depth": self.pending.qsize(),
            "rotations": self.rotations,
            "active_bytes": self.active_bytes,
            "free_bytes_decimal": self.free_bytes,
            "last_success_at": self.last_success,
            "last_error": self.last_error,
            "health_write_failed": self.health_write_failed,
            "retained_files": BACKUPS + 1,
            "max_bytes_per_file": MAX_BYTES,
            "evidence": "diagnostic_health",
            "scope": "latest_writer_process",
        }

    def _health(self, *, force=False):
        if not force and time.monotonic() - self._last_health < HEALTH_INTERVAL:
            return
        from .runtime_event_files import write_health

        self._last_health = time.monotonic()
        try:
            # The new snapshot is itself evidence that this health write succeeded.
            snapshot = {
                **self.health(),
                "health_write_failed": False,
                "status": "degraded" if self.last_error else "ready",
            }
            self.free_bytes = write_health(self.path, snapshot)
            self.health_write_failed = False
        except Exception:
            self.health_write_failed = True
        state = (bool(self.last_error), self.health_write_failed)
        if state != self._reported_health:
            if any(state) or self._reported_health not in (None, (False, False)):
                try:
                    import sys

                    print(
                        "Noah diagnostic sink "
                        + ("degraded" if any(state) else "recovered")
                        + f"; dropped={self.dropped}; health_write_failed="
                        + str(self.health_write_failed),
                        file=sys.stderr,
                    )
                except Exception:
                    pass
            self._reported_health = state

    def _work(self):
        while True:
            try:
                record = self.pending.get(timeout=1.0)
            except queue.Empty:
                self._health()
                continue
            previous_error = self.last_error
            try:
                self._append(record)
                self.written += 1
                self.last_success = datetime.now(timezone.utc).isoformat()
                self.last_error = None
            except Exception as error:
                self.dropped += 1
                self.last_error = (
                    "writer_busy"
                    if isinstance(error, BlockingIOError)
                    else "invalid_destination"
                    if isinstance(error, ValueError)
                    else "write_failed"
                )
            finally:
                self._health(
                    force=self.last_error is not None
                    or self.written == 1
                    or previous_error != self.last_error
                )
                self.pending.task_done()

    def flush(self, timeout=0.2):
        """Bounded best-effort drain, intended for process exit and tests only."""
        deadline = time.monotonic() + max(0, min(timeout, 2))
        while self.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return not self.pending.unfinished_tasks


def _get_sink():
    global _sink
    if _sink is not None and _sink.pid == os.getpid():
        return _sink
    path = os.environ.get("NOAH_EVENT_LOG")
    if not path or not os.path.isabs(path) or not _sink_lock.acquire(blocking=False):
        return None
    try:
        if _sink is None or _sink.pid != os.getpid():
            _sink = EventSink(path)
        return _sink
    finally:
        _sink_lock.release()


def sink_health():
    """Read diagnostics without starting a worker or touching the filesystem."""
    if _sink is None:
        return {"status": "uninitialized" if os.environ.get("NOAH_EVENT_LOG") else "disabled"}
    return _sink.health()


def emit(source, event, stage, **values):
    """Never raise or inspect arbitrary data; an unavailable sink changes no decision."""
    try:
        if source not in ("miner", "validator") or event not in EVENTS or stage not in STAGES:
            return
        sink = _get_sink()
        if sink is None:
            return
        context = _context.get()
        fields = dict(context[1]) if context and context[0] == source else {}
        fields.update(_fields(values))
        fields["severity"] = severity_for(
            event=event, outcome=fields.get("outcome"), severity=fields.get("severity")
        )
        if event in ("epoch.observed", "accounting.observed", "deposit.attribution"):
            # Historical observations can predate the calculation being built.
            # Never imply that this attempt's publication produced that payment.
            if "day" in fields:
                fields["scan_day"] = fields.pop("day")
            fields.pop("artifact_sha256", None)
            fields.pop("intent_id", None)
        sink.enqueue(source, event, stage, fields)
    except Exception:
        pass


def bind(source, **values):
    """Attach facts already observed by this invocation; performs no chain reads."""
    try:
        current = _context.get()
        fields = dict(current[1]) if current and current[0] == source else {}
        fields.update(_fields(values))
        _context.set((source, fields))
    except Exception:
        pass


def call(source, stage, operation, *args, event_fields=None, result_fields=None, **kwargs):
    """Observe one existing call, returning its exact result or reraising its exception."""
    try:
        values = _fields(event_fields) if type(event_fields) is dict else {}
    except Exception:
        values = {}
    emit(source, "stage.entered", stage, **values)
    # Bounded metadata only: no operation args, exception messages or payloads.
    trace = {key: values[key] for key in ("day", "height", "period", "netuid") if key in values}
    debug("stage entered source=%s stage=%s context=%s", source, stage, trace)
    started = time.monotonic_ns()
    try:
        result = operation(*args, **kwargs)
    except BaseException:
        debug("stage failed source=%s stage=%s elapsed_ms=%d", source, stage,
                     (time.monotonic_ns() - started) // 1_000_000)
        emit(
            source,
            "stage.failed",
            stage,
            **{
                **values,
                "outcome": "raised",
                "elapsed_ms": (time.monotonic_ns() - started) // 1_000_000,
            },
        )
        raise
    projected = {**values, "elapsed_ms": (time.monotonic_ns() - started) // 1_000_000}
    if result_fields is not None:
        try:
            projected.update(_fields(result_fields(result)))
        except Exception:
            pass
    if type(result) is dict:
        projected.update(_fields({"outcome": result.get("status")}))
    elif type(result) is str:
        projected.update(_fields({"outcome": result}))
    elif type(result) is int:
        projected.update(_fields({"status_code": result}))
    projected.setdefault("outcome", "returned")
    debug("stage completed source=%s stage=%s outcome=%s elapsed_ms=%d", source, stage,
                 projected["outcome"], projected["elapsed_ms"])
    emit(source, "stage.completed", stage, **projected)
    return result


def traced(source, stage, *, fresh=False, result_fields=None):
    def decorate(operation):
        @wraps(operation)
        def wrapped(*args, **kwargs):
            token = _context.set((source, {}) if fresh else (_context.get() or (source, {})))
            try:
                return call(source, stage, operation, *args, result_fields=result_fields, **kwargs)
            finally:
                _context.reset(token)

        return wrapped

    return decorate


def _at_exit():
    try:
        if _sink is not None and _sink.pid == os.getpid():
            _sink.flush()
    except Exception:
        pass


atexit.register(_at_exit)


def observe_seats(seats, *, day, height):
    """Project a validated seat snapshot without serializing identity addresses."""
    try:
        if _get_sink() is None or type(seats) is not dict:
            return
        for owned in seats.values():
            if type(owned) is dict:
                for hotkey, uid in owned.items():
                    emit(
                        "validator",
                        "registration.observed",
                        "validator.ingestion",
                        day=day,
                        height=height,
                        uid=uid,
                        seat_hotkey=hotkey,
                        outcome="registered",
                    )
    except Exception:
        pass


def observe_receipt(deposit):
    """Report an application's recorded receipt, without claiming qualifying credit."""
    try:
        context = _context.get()
        if context is None or _get_sink() is None:
            return
        source = context[0]
        stage = "validator.ingestion" if source == "validator" else "miner.reconciliation"
        emit(
            source,
            "contribution.observed",
            stage,
            height=deposit.block,
            token_netuid=deposit.token_netuid,
            amount_rao=deposit.amount_rao,
            miner_hotkey=deposit.origin_hotkey,
            block_hash=deposit.block_hash,
            event_ordinal=deposit.event_ordinal,
            extrinsic_index=deposit.extrinsic_index,
            outcome="received",
        )
        if source == "validator":
            attributed = deposit.origin_hotkey is not None and deposit.unattributed_reason is None
            emit(
                source,
                "deposit.attribution",
                "validator.attribution",
                height=deposit.block,
                token_netuid=deposit.token_netuid,
                amount_rao=deposit.amount_rao,
                miner_hotkey=deposit.origin_hotkey,
                origin_coldkey=deposit.owner_at_deposit or deposit.coldkey,
                block_hash=deposit.block_hash,
                event_ordinal=deposit.event_ordinal,
                extrinsic_index=deposit.extrinsic_index,
                outcome="attributed" if attributed else "unqualified",
                reason_code=None if attributed else "attribution_unavailable",
            )
    except Exception:
        pass


def begin_validator_attempt(identity):
    """Fresh diagnostic correlation from the already-established loop identity."""
    try:
        bind("validator", attempt_id=uuid.uuid4().hex)
        if type(identity) is str:
            parts = identity.split("/")
            if len(parts) == 3 and parts[1].isascii() and parts[1].isdigit():
                bind(
                    "validator",
                    genesis_hash=parts[0],
                    netuid=int(parts[1]),
                    validator_hotkey=parts[2],
                )
    except Exception:
        pass


def observe_window(*, day, height, from_block, to_block, anchor, sources, **values):
    try:
        debug("pinned day=%s height=%s from_block=%s to_block=%s source_count=%s",
              day, height, from_block, to_block, sources)
        bind(
            "validator", day=day, height=height, from_block=from_block, to_block=to_block, **values
        )
        emit(
            "validator",
            "eligibility.state",
            "validator.eligibility",
            outcome="ready",
            day_start_ts=anchor + day * 86400,
            day_end_ts=anchor + (day + 1) * 86400,
            source_count=len(sources),
        )
    except Exception:
        pass


def history_state(**values):
    """Project scanner progress only inside an existing validator invocation."""
    try:
        context = _context.get()
        if context and context[0] == "validator":
            emit("validator", "history.state", "validator.history", **values)
    except Exception:
        pass


def observe_entry_qualification(seats, dropped, entry_deposits, entry_days):
    """Report existing seat/entry decisions; never read policy or recompute credit."""
    try:
        if _get_sink() is None:
            return
        for coldkey, hotkeys in seats.items():
            for hotkey, uid in hotkeys.items():
                emit(
                    "validator",
                    "entry.qualification",
                    "validator.attribution",
                    outcome="qualified",
                    origin_coldkey=coldkey,
                    miner_hotkey=hotkey,
                    uid=uid,
                    amount_rao=entry_deposits.get(coldkey, {}).get(hotkey),
                    entry_day=entry_days.get(coldkey, {}).get(hotkey),
                )
        for seat in dropped:
            emit(
                "validator",
                "entry.qualification",
                "validator.attribution",
                outcome="unqualified",
                reason_code="entry_not_confirmed",
                origin_coldkey=seat.get("coldkey"),
                miner_hotkey=seat.get("hotkey"),
                uid=seat.get("uid"),
            )
    except Exception:
        pass


def observe_prepared(agreed, vector, mode, publisher):
    """Summarize the already-retained vector; no new decision or chain read."""
    try:
        emit(
            "validator",
            "publication.prepared",
            "validator.preparation",
            outcome="ready",
            publish_mode=mode,
            seat_count=len(vector),
            route="proxy" if getattr(publisher, "commit_identity", None) is not None else "direct",
            changed_uid_count=sum(
                agreed.get(uid, 0) != vector.get(uid, 0) for uid in set(agreed) | set(vector)
            ),
        )
    except Exception:
        pass


def observe_epoch(attribution, block_hash):
    """Project recorded epoch rows, without inferring their applicable publication."""
    try:
        context = _context.get()
        if context is None or context[0] != "validator" or _get_sink() is None:
            return
        for outcome, rows in (("observed", attribution.paid), ("withheld", attribution.withheld)):
            for uid, hotkey, coldkey, amount in rows:
                emit(
                    "validator",
                    "epoch.observed",
                    "validator.settlement",
                    outcome=outcome,
                    height=attribution.epoch_block,
                    block_hash=block_hash,
                    uid=uid,
                    miner_hotkey=hotkey,
                    origin_coldkey=coldkey,
                    amount_rao=amount,
                )
    except Exception:
        pass


def observe_accounting(coldkey, block, block_hash, effect=None):
    """Project the existing locked-pay record; absence is unavailable, never zero."""
    try:
        context = _context.get()
        if context is None or context[0] != "validator" or _get_sink() is None:
            return
        emit(
            "validator",
            "accounting.observed",
            "validator.accounting",
            outcome="unavailable" if effect is None else "observed",
            height=block,
            block_hash=block_hash,
            origin_coldkey=coldkey,
            locked_arrival_rao=None if effect is None else effect.paid_locked_arrival_rao,
            unlocked_rao=None if effect is None else effect.pay_unlocked_rao,
        )
    except Exception:
        pass
