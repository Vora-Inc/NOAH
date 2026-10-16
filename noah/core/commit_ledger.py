"""Persist the calendar days of this validator's sealed commits.

Records include the subnet and commit identity, but do not store a genesis hash. The ledger helps distinguish
a previous day's commit from today's after restart; missing or unreadable records
must not be presented as successful publication evidence."""

import json
import os
import threading
from pathlib import Path

from ..pathtext import plain_path_text
from .reader import ChainUnreadable

SCHEMA_VERSION = 1


class CommitLedgerRefused(ChainUnreadable):
    """The ledger file exists but cannot be trusted or understood (corrupt,
    wrong shape, or written by a newer version). Nothing is decided from it and
    nothing is published past it."""


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class CommitLedger:
    def __init__(self, path):
        self.path = Path(path)

    def exists(self):
        return self.path.exists()

    def load(self):
        """``None`` when there is no ledger file; otherwise ``{day(int): entry}``.
        Raises `CommitLedgerRefused` on anything but a well-formed file of a
        known version."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise CommitLedgerRefused(
                f"the commit ledger {plain_path_text(self.path)} exists but could not be read "
                f"({type(error).__name__}). Not ignored, because ignoring it would "
                f"silently turn per-day attribution off; nothing is decided from it."
            ) from None
        try:
            doc = json.loads(text)
        except ValueError as error:
            raise CommitLedgerRefused(
                f"the commit ledger {plain_path_text(self.path)} is not valid JSON "
                f"({type(error).__name__}). Not ignored: "
                f"repair it, or move it aside deliberately to fall back to the window scan "
                f"alone (this loses per-day attribution)."
            ) from None
        if not isinstance(doc, dict) or not isinstance(doc.get("schema_version"), int):
            raise CommitLedgerRefused(
                f"the commit ledger {plain_path_text(self.path)} has no integer schema_version; "
                f"refusing to "
                f"guess its meaning."
            )
        if doc["schema_version"] > SCHEMA_VERSION:
            raise CommitLedgerRefused(
                f"the commit ledger {plain_path_text(self.path)} is schema version "
                f"{doc['schema_version']}, "
                f"newer than this code understands ({SCHEMA_VERSION}). Refusing rather than "
                f"reading a format this build may misread; upgrade the validator."
            )
        if doc["schema_version"] < 1 or not isinstance(doc.get("days"), dict):
            raise CommitLedgerRefused(
                f"the commit ledger {plain_path_text(self.path)} is malformed (schema_version "
                f"{doc['schema_version']}, 'days' is not an object)."
            )
        days = {}
        for key, entry in doc["days"].items():
            try:
                day = int(key)
            except (TypeError, ValueError):
                day = None
            if (
                day is None
                or not isinstance(entry, dict)
                or type(entry.get("block")) is not int
                or entry["block"] < 0
            ):
                raise CommitLedgerRefused(
                    f"the commit ledger {plain_path_text(self.path)} has a malformed entry for "
                    f"day {key!r}: "
                    f"{entry!r}. Refusing the whole file rather than half-trusting it."
                )
            days[day] = entry
        return days

    def record(self, *, day, block, extrinsic_index, reveal_round, hotkey, netuid):
        """Add (or replace) day -> commit. Raises `CommitLedgerRefused` if the
        existing file is untrustworthy (so a corrupt file is never overwritten
        silently), and OSError if the write itself fails."""
        days = self.load() or {}
        days[int(day)] = {
            "block": int(block),
            "extrinsic_index": extrinsic_index,
            "reveal_round": reveal_round,
            "hotkey": hotkey,
            "netuid": int(netuid),
            "at": _utc_now(),
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "days": {str(d): days[d] for d in sorted(days)},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(
            self.path.suffix + f".tmp{os.getpid()}.{threading.get_ident()}"
        )
        try:
            with open(tmp, "w") as f:
                f.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass  # best-effort, same stance as loop._write_state
