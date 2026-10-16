"""Persist the possibility that a publication send is in flight.

Write the marker before sending. On restart, reconcile it with chain evidence
before allowing a retry. Lack of immediate confirmation does not establish that
nothing was submitted."""

import json
import os
from pathlib import Path

from ..pathtext import plain_path_text
from .reader import ChainUnreadable

SCHEMA_VERSION = 1
REASON_PREFIX = "PUBLICATION STATUS UNKNOWN"


class PublicationStatusUnknown(ChainUnreadable):
    """A publication send may have happened and its outcome is not established.
    Nothing is decided and nothing is sent until it is reconciled."""


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class PublishIntent:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        """``None`` when no send is open, else the marker dict. A corrupt file is
        refused by name (it is treated as an open marker the person must look at)."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise PublicationStatusUnknown(
                f"{REASON_PREFIX}: the publish-intent file {plain_path_text(self.path)} exists "
                f"but could not be "
                f"read ({type(error).__name__}); nothing is sent."
            ) from None
        try:
            doc = json.loads(text)
            if not isinstance(doc, dict) or type(doc.get("height")) is not int:
                raise ValueError("no integer height")
        except ValueError as error:
            raise PublicationStatusUnknown(
                f"{REASON_PREFIX}: the publish-intent file {plain_path_text(self.path)} is not a "
                f"valid marker "
                f"({type(error).__name__}); nothing is sent until a person reconciles it."
            ) from None
        return doc

    def open(self, *, day, height, hotkey, netuid, exclusive=False, **fields):
        self._write(
            {
                **fields,
                "schema_version": SCHEMA_VERSION,
                "day": int(day),
                "height": int(height),
                "hotkey": hotkey,
                "netuid": int(netuid),
                "at": _utc_now(),
            },
            exclusive=exclusive,
        )

    def annotate(self, **fields):
        """Add what identifies the exact transaction (written just before the send).
        Only fields the caller really has are passed; the marker stays open either way."""
        doc = self.load()
        if doc is None:
            raise PublicationStatusUnknown(
                f"{REASON_PREFIX}: no open marker at {plain_path_text(self.path)} to annotate."
            )
        doc.update(fields)
        self._write(doc)

    def _write(self, doc, *, exclusive=False):
        import uuid

        tmp = self.path.with_name(self.path.name + f".tmp-{uuid.uuid4().hex}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as handle:
            json.dump(doc, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if exclusive:
                # Atomic no-overwrite installation of the complete, fsynced intent.
                os.link(tmp, self.path)
            else:
                os.replace(tmp, self.path)
        except FileExistsError as error:
            raise PublicationStatusUnknown(
                "an unresolved publication intent already exists"
            ) from error
        finally:
            tmp.unlink(missing_ok=True)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
