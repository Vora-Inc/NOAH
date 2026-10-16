"""Allow at most one local publication writer for a validator identity.

Lock acquisition and stale-lock replacement use filesystem identity checks and
serialization so racing processes cannot both claim ownership. A live owner is
refused. The lock coordinates processes sharing this filesystem; it is not a
distributed lock across independent machines."""

import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..pathtext import plain_path_text
from ..diagnostics import write as diagnostic


class LockHeld(Exception):
    """Another writer already holds this lock."""


@dataclass(frozen=True)
class LockInfo:
    path: Path
    pid: int
    started_at: str
    thread_id: int
    incarnation: str


_INCARNATION = uuid.uuid4().hex


# Which lock paths THIS thread currently believes it holds -- thread-local
# because pid+thread_id together are what identify a single acquirer, and
# threading.local() naturally isolates one thread's belief from another's.
#
# This is what makes self-reclaim (`_reclaimable`) safe against a genuine
# re-entrant call, not just a friendlier label on the same risk: "the lock
# file names my own (pid, thread)" is, on its own, indistinguishable
# between "I already tried to release this and the unlink failed" (safe to
# reclaim) and "I am still inside the SAME acquisition, calling acquire()
# again by mistake" (a bug -- must still refuse, matching ordinary mutex
# semantics). Found needed by this module's own test suite immediately
# after the pid-only version of self-reclaim was written: a live holder
# calling acquire() a second time, without releasing, must still raise
# LockHeld, and pid+thread_id alone cannot tell that case apart from the one
# self-reclaim exists for.
_held_locally = threading.local()


# What a lock path may be called in a message, decided from the path AS TYPED: ``Path.resolve()``
# turns a relative typed value (even a bare key) into an absolute-looking one that would pass the
# filter.
_SHOWN = {}


def _shown(path):
    return _SHOWN.get(str(path)) or plain_path_text(path)


def _mark_held(path):
    if not hasattr(_held_locally, "paths"):
        _held_locally.paths = set()
    _held_locally.paths.add(str(path))


def _mark_released(path):
    if hasattr(_held_locally, "paths"):
        _held_locally.paths.discard(str(path))


def _believed_held_by_this_thread(path):
    return hasattr(_held_locally, "paths") and str(path) in _held_locally.paths


def _read_holder(path):
    try:
        return path.read_text().strip()
    except Exception as error:  # noqa: BLE001 -- best-effort diagnostic only
        return f"(could not read {_shown(path)}: {type(error).__name__})"


def _holder_identity(path):
    """Read the lock's (pid, thread_id, incarnation), or all-None when unreadable.

    Callers must distinguish unreadable-but-present from an absent lock before
    reclaiming or releasing it."""
    try:
        info = json.loads(path.read_text())
        return info.get("pid"), info.get("thread_id"), info.get("incarnation")
    except Exception:  # noqa: BLE001 -- unreadable/corrupt content is not a live identity
        return None, None, None


def _pid_alive(pid):
    """Best-effort, and deliberately fails CLOSED (treated as alive).

    `os.kill(pid, 0)` sends no signal; it only asks whether the pid could be
    signalled. `ProcessLookupError` is the one answer that PROVES the pid is
    gone. Every other outcome -- alive, a permission error (which itself
    proves some process owns that pid), or anything this has not seen before
    -- is treated as still alive, because wrongly refusing to break a live
    lock costs a delayed retry and wrongly breaking one risks two writers
    signing at once.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001
        return True
    return True


def _reclaimable(path, holder_pid, holder_thread_id, holder_incarnation):
    """Decide whether a recorded lock owner can safely be treated as gone.

    Account for process identity and incarnation rather than PID alone. Unreadable or
    ambiguous ownership is not permission to break a lock; PID reuse must not let a
    new process release or reclaim another writer's live lock."""
    if holder_pid == os.getpid():
        if holder_incarnation != _INCARNATION:
            return True  # case 2: same pid, provably a different, earlier incarnation
        if holder_thread_id == threading.get_ident():
            return not _believed_held_by_this_thread(path)  # case 3
        # Same pid, same incarnation, a DIFFERENT thread of this exact
        # process: a sibling thread's own genuinely active lock, never
        # reclaimable from here.
        return False
    return not _pid_alive(holder_pid)


def _break_and_relink(path, tmp):
    """Serialize stale-lock replacement and verify ownership before relinking."""
    control = path.with_suffix(path.suffix + ".break-mutex")
    with open(control, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            holder_pid, holder_thread_id, holder_incarnation = _holder_identity(path)
            if holder_pid is None or not _reclaimable(
                path, holder_pid, holder_thread_id, holder_incarnation
            ):
                # No longer stale (or self-reclaimable) by the time this
                # process got the mutex -- either it was never stale, or
                # another process already won this exact race and relinked
                # its own live lock here.
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            try:
                os.link(tmp, path)
            except FileExistsError:
                # Lost a race with a THIRD process's own fresh acquisition
                # that landed between our unlink and our relink -- reported
                # to the caller as "still not acquired", not raised here.
                return False
            return True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def acquire(path):
    """Take the lock, or refuse naming who already holds it.

    Atomic: writes the lock's content to a temp file first, then creates the
    real lock path with `os.link`. `os.link` fails with `FileExistsError`
    when the target already exists and never overwrites it, so two processes
    racing to acquire this at the same instant cannot both succeed -- one
    `os.link` call wins and the other raises.

    A lock that cannot be acquired is checked once for staleness (see the
    module docstring, "Why this DOES break a stale lock"): if the pid it
    names is verifiably gone, the lock is broken and re-acquisition is tried
    exactly once more, serialized against every other process attempting the
    same break via `_break_and_relink`'s own flock. Losing that retry
    (someone else broke or re-took it first) is a normal refusal, not a bug.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    typed = plain_path_text(path)
    path = path.resolve()
    _SHOWN[str(path)] = typed
    # pid alone identifies the real, intended acquirer (one process, one
    # lock attempt); threading.get_ident() is appended too so two THREADS in
    # one process -- which share a pid, unlike the two separate processes
    # this lock is actually built for -- cannot collide on the same temp
    # filename either, which matters for this module's own test suite
    # exercising concurrent acquisition via threads rather than real
    # processes.
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}-{threading.get_ident()}")
    # thread_id travels in the lock's own content too, not just the temp
    # filename: `_reclaimable` needs it to tell "my own prior attempt" from
    # "a sibling thread's still-active one" -- see that function's own
    # docstring. incarnation travels too, so a LATER process that reused
    # this same pid can prove that to itself -- see `_INCARNATION`'s own
    # docstring.
    info = {
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        "incarnation": _INCARNATION,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        tmp.write_text(json.dumps(info))
        try:
            os.link(tmp, path)
        except FileExistsError:
            if not _break_and_relink(path, tmp):
                raise LockHeld(
                    f"{_shown(path)} is already held ({_read_holder(path)}). Either that "
                    f"pid is "
                    f"still running -- another writer on this hotkey appears to be active "
                    f"right now, so this refuses rather than risk two things committing "
                    f"weights on the same identity at once (#526) -- or it was stale and "
                    f"another process already broke and re-took it first. Refusing either "
                    f"way; the next poll tick tries again."
                ) from None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 -- the lock itself is a separate, already-linked
            pass  # inode; a leftover temp file here is harmless either way.
    _mark_held(path)
    return LockInfo(
        path=path,
        pid=info["pid"],
        started_at=info["started_at"],
        thread_id=info["thread_id"],
        incarnation=info["incarnation"],
    )


def release(info):
    """Release only the lock still owned by this exact process/thread/incarnation.

    Leave changed ownership untouched. Distinguish an absent file from a present but
    unreadable one and report cleanup uncertainty rather than silently deleting it."""
    _mark_released(info.path)
    path = Path(info.path)
    if not path.exists():
        return  # already gone; nothing to release
    current_pid, current_thread_id, current_incarnation = _holder_identity(path)
    if current_pid is None:
        diagnostic(
            f"  (releasing {_shown(path)} skipped: it exists but its content could not "
            f"be read as "
            f"this acquisition's own lock info ({_read_holder(path)}) -- left alone rather "
            f"than blindly removed; it may need manual removal if it is actually stale.)",
            severity="warning",
        )
        return
    if (current_pid, current_thread_id, current_incarnation) != (
        info.pid,
        info.thread_id,
        info.incarnation,
    ):
        diagnostic(
            f"  (releasing {_shown(path)} skipped: the file no longer names this "
            f"acquisition "
            f"(pid={info.pid}, thread={info.thread_id}) -- it now names pid={current_pid}, "
            f"thread={current_thread_id}, a different writer's lock, so it was left alone "
            f"rather than removed.)",
            severity="warning",
        )
        return
    try:
        path.unlink(missing_ok=True)
    except Exception as error:  # noqa: BLE001
        diagnostic(
            f"  (releasing {_shown(path)} failed: {type(error).__name__} -- the "
            f"result above is unaffected; {_shown(path)} may need manual removal.)",
            severity="warning",
        )


@contextmanager
def held(path):
    """Acquire for the duration of a `with` block, released on every exit."""
    info = acquire(path)
    try:
        yield info
    finally:
        release(info)
