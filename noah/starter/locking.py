"""Short publication lock, separate from the long daily calculation lock."""

import fcntl
import functools
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from ..core.writer_lock import LockHeld, held

_local = threading.local()


@contextmanager
def deployment_lease(directory):
    """Cooperate with the release engine's exclusive lease, including CLI sends."""
    lock = Path(directory) / "starter-deployment.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockHeld("starter publication is paused by a release transaction") from error
        yield


@contextmanager
def publication_lock(adapter):
    if getattr(adapter, "starter_entry_policy", None) is None:
        yield
        return
    path = str((Path(adapter._submitted_record_dir) / "starter-publication.lock").resolve())
    paths = getattr(_local, "paths", set())
    if path in paths:
        yield
        return
    with deployment_lease(adapter._submitted_record_dir), held(path):
        _local.paths = paths | {path}
        try:
            yield
        finally:
            _local.paths = paths


def serialized(function):
    @functools.wraps(function)
    def wrapped(adapter, *args, **kwargs):
        with publication_lock(adapter):
            return function(adapter, *args, **kwargs)

    return wrapped
