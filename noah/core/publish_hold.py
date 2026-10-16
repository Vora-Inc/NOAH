"""A persistent operator-set hold that prevents publication.

Hold state is separate from automatic progress and send-intent state. Inspect and
resolve the underlying uncertainty before clearing an operator hold."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..pathtext import plain_path_text
from .publish_intent import PublicationStatusUnknown

SCHEMA_VERSION = 1
REASON_PREFIX = "PUBLICATION ON HOLD"


class PublicationOnHold(PublicationStatusUnknown):
    """A person has put publication on hold; nothing is decided or sent until released."""


def _utc_now():
    return datetime.now(timezone.utc)


class PublishHold:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        """``None`` when there is no hold, else the hold dict. An unreadable or corrupt
        file counts as a hold (fail closed): it is returned with a reason saying so."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            return {"reason": f"the hold file could not be read ({type(error).__name__})"}
        try:
            doc = json.loads(text)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except ValueError:
            return {"reason": "the hold file is not valid JSON"}
        return doc

    def raise_if_held(self):
        doc = self.load()
        if doc is not None:
            raise PublicationOnHold(
                f"{REASON_PREFIX}: publication is held ({plain_path_text(self.path)}), set "
                f"{doc.get('set_at', 'at an unknown time')}. Nothing is sent. Reconcile against "
                f"the chain and release it with scripts/publication_hold.py "
                f"after confirming its status."
            )

    def set(self, reason):
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("a hold needs a reason")
        doc = {"schema_version": SCHEMA_VERSION, "reason": reason, "set_at": _utc_now().isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as handle:
            json.dump(doc, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        return doc

    def release(self, evidence_note):
        """Archive the hold with the evidence note beside it; return the archive path."""
        note = (evidence_note or "").strip()
        if not note:
            raise ValueError("releasing a hold needs a non-empty evidence note")
        doc = self.load()
        if doc is None:
            raise FileNotFoundError(f"no hold at {plain_path_text(self.path)}")
        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        archive = self.path.with_name(f"{self.path.stem}.released-{stamp}{self.path.suffix}")
        doc = {**doc, "released_at": _utc_now().isoformat(), "evidence_note": note}
        archive.write_text(json.dumps(doc, sort_keys=True))
        self.path.unlink()
        return archive
