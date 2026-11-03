"""Persist the possibility that a publication send is in flight.

Write the marker before sending. On restart, reconcile it with chain evidence
before allowing a retry. Lack of immediate confirmation does not establish that
nothing was submitted."""

import json
import os
import stat
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

    def persist_prepared(self, evidence):
        """Fsync exact signed bytes privately before exposing the send boundary.

        Submitted-score exports receive only hash/context, never usable signed
        bytes. Sidecars are retained after completion for operator recovery.
        """
        from .prepared_transaction import transaction_hash

        marker = self.load()
        if marker is None or marker.get("prepared_transaction") is not None:
            raise PublicationStatusUnknown("no fresh intent for prepared transaction")
        identity = marker.get("intent_id")
        if (
            not isinstance(identity, str)
            or not identity
            or any(c not in "0123456789abcdef-" for c in identity)
        ):
            raise PublicationStatusUnknown("invalid prepared transaction identity")
        data = bytes.fromhex(evidence["signed_hex"].removeprefix("0x"))
        if transaction_hash(data) != evidence["extrinsic_hash"]:
            raise PublicationStatusUnknown("prepared transaction hash mismatch")
        directory = self.path.parent / "signed-transactions"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        sidecar_name = identity + ".json"
        try:
            metadata = os.fstat(directory_fd)
            if (
                stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
            ):
                raise PublicationStatusUnknown(
                    "signed transaction directory is not private/owned"
                )
            fd = os.open(
                sidecar_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "w") as handle:
                json.dump(evidence, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        public = {key: value for key, value in evidence.items() if key != "signed_hex"}
        public["sidecar"] = sidecar_name
        self.annotate(
            prepared_transaction=public,
            account_nonce_at_head=evidence["nonce"],
            head_at_send=evidence["head_at_send"],
            era_period=evidence["era"]["period"],
        )

    def _write(self, doc, *, exclusive=False):
        import uuid

        tmp = self.path.with_name(self.path.name + f".tmp-{uuid.uuid4().hex}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(
            os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as handle:
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
