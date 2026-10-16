"""The single-writer lock (#526): acquire, refuse, and break a real stale one.

The stale-lock case is proven against an actually-dead process, not a faked
pid number: a real subprocess is started, allowed to exit, and its pid --
genuinely no longer running, confirmed by `wait()` reaping it -- is written
into a lock file by hand. A made-up large integer would not prove the same
thing: it could coincidentally never have been a pid at all, which is a
different (and much easier) case than "this exact pid used to be a process
and now is not".
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from ..writer_lock import LockHeld, LockInfo, acquire, held, release


class AcquireAndRelease(unittest.TestCase):
    def test_a_fresh_lock_can_be_acquired_and_names_this_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            info = acquire(path)
            try:
                self.assertTrue(path.exists())
                import os

                self.assertEqual(info.pid, os.getpid())
            finally:
                release(info)
            self.assertFalse(path.exists(), "release must remove the lock file")

    def test_release_on_an_already_missing_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            info = acquire(path)
            path.unlink()
            release(info)  # must not raise

    def test_held_context_manager_releases_on_a_raised_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            with self.assertRaises(RuntimeError):
                with held(path):
                    self.assertTrue(path.exists())
                    raise RuntimeError("boom")
            self.assertFalse(path.exists())


class ALiveHolderRefusesASecondAcquisition(unittest.TestCase):
    def test_a_second_acquisition_while_the_first_is_still_held_by_a_live_pid_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            first = acquire(path)  # held by THIS test process -- definitely alive
            try:
                with self.assertRaises(LockHeld) as caught:
                    acquire(path)
                self.assertIn("#526", str(caught.exception))
            finally:
                release(first)

    def test_no_data_was_touched_by_the_refused_acquisition(self):
        # The refusal must not disturb the lock the live holder still owns.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            first = acquire(path)
            try:
                with self.assertRaises(LockHeld):
                    acquire(path)
                self.assertEqual(json.loads(path.read_text())["pid"], first.pid)
            finally:
                release(first)


class AStaleLockFromAGenuinelyDeadProcessIsBroken(unittest.TestCase):
    def _dead_pid(self):
        """A pid that used to be a real process and, confirmed by `wait()`
        reaping it, is not one any more."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = proc.pid
        proc.wait(timeout=5)
        return pid

    def test_acquire_breaks_a_lock_left_by_a_process_that_is_actually_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            path.write_text(json.dumps({"pid": self._dead_pid(), "started_at": "long ago"}))

            info = acquire(path)  # must succeed: the recorded pid is verifiably gone
            try:
                self.assertTrue(path.exists())
                import os

                self.assertEqual(json.loads(path.read_text())["pid"], os.getpid())
            finally:
                release(info)


class TwoSpellingsOfTheSamePathAreTheSameLock(unittest.TestCase):
    """Found by codex adversarial review, round 6 (2026-08-29):
    `_believed_held_by_this_thread` used to be keyed by the raw string form
    of the path. Acquiring "the same" lock through a relative path and then
    its absolute equivalent would make self-reclaim think this thread does
    not currently hold what it, in fact, still actively holds under the
    other spelling -- breaking its own live lock out from under it and
    letting a second acquisition through, exactly the two-writer outcome
    this whole module exists to prevent.
    """

    def test_a_relative_and_absolute_spelling_of_the_same_path_are_one_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            absolute = (Path(tmp) / "lock").resolve()
            import os as _os

            cwd = _os.getcwd()
            try:
                _os.chdir(tmp)
                relative = Path("lock")

                first = acquire(relative)
                try:
                    # The SAME thread, same process, addressing the SAME
                    # lock through its absolute spelling: must refuse, not
                    # silently steal its own still-active lock.
                    with self.assertRaises(LockHeld):
                        acquire(absolute)
                finally:
                    release(first)
            finally:
                _os.chdir(cwd)

    def test_release_through_either_spelling_clears_the_same_belief(self):
        with tempfile.TemporaryDirectory() as tmp:
            absolute = (Path(tmp) / "lock").resolve()
            import os as _os

            cwd = _os.getcwd()
            try:
                _os.chdir(tmp)
                relative = Path("lock")
                info = acquire(relative)
                release(info)  # releases via the resolved path LockInfo carries

                # Now genuinely free: acquiring via the absolute spelling
                # must succeed normally, not be refused by a stale belief.
                second = acquire(absolute)
                release(second)
            finally:
                _os.chdir(cwd)


class AProcessCanReclaimItsOwnFailedRelease(unittest.TestCase):
    """Found by codex adversarial review, round 5 (2026-08-29): `release()`
    deliberately warns rather than raises on a real `unlink()` I/O failure
    so a failing cleanup step never masks an already-completed result. Without this
    fix, that rare failure leaves the lock file on disk still naming THIS
    process's own, still-alive pid -- and the very next `acquire()` from
    the same process would see a live pid and refuse forever, wedging every
    future period for the rest of the process's life over one transient
    unlink failure.
    """

    def test_a_lock_left_behind_by_a_failed_release_is_reclaimed_by_the_same_process(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            info = acquire(path)

            # Actually call release() -- it must be genuinely ATTEMPTED, not
            # skipped, for this to be the scenario the fix is about (as
            # opposed to a live re-entrant hold, which must still refuse --
            # see the sibling class below). Its own unlink is made to fail
            # exactly once, matching release()'s own documented "warn, don't
            # raise" contract for a real I/O failure.
            with patch.object(Path, "unlink", side_effect=OSError("simulated I/O failure")):
                release(info)  # must not raise
            self.assertTrue(path.exists(), "the simulated failure must have left the file behind")
            self.assertEqual(json.loads(path.read_text())["pid"], info.pid)

            # The SAME process asks again, for the next period -- must not
            # be refused by its own leftover lock.
            second = acquire(path)
            try:
                self.assertEqual(json.loads(path.read_text())["pid"], second.pid)
            finally:
                release(second)

    def test_a_genuine_reentrant_acquisition_without_releasing_still_refuses(self):
        # The property self-reclaim must NOT weaken: calling acquire() a
        # second time on a lock this SAME thread still actively holds --
        # never having called release() at all -- is a bug, and must still
        # raise, exactly like it did before self-reclaim existed. Caught by
        # this module's own test suite the moment the first, pid-only
        # version of self-reclaim was written.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            first = acquire(path)
            try:
                with self.assertRaises(LockHeld):
                    acquire(path)
            finally:
                release(first)

    def test_the_reclaim_does_not_open_a_hole_for_a_genuinely_different_live_process(self):
        # Control: a lock held by a DIFFERENT, definitely-alive pid must
        # still refuse normally -- self-reclaim is not a general bypass.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
            try:
                path.write_text(json.dumps({"pid": proc.pid, "started_at": "just now"}))
                with self.assertRaises(LockHeld):
                    acquire(path)
            finally:
                proc.kill()
                proc.wait(timeout=5)


class AReusedPidFromAnEarlierIncarnationIsReclaimed(unittest.TestCase):
    """Found by codex adversarial review, round 9 (2026-08-29): a killed
    validator's replacement can be handed the SAME pid deterministically
    (not rarely) -- a container's own main process is always pid 1, so
    every restart reuses it. The replacement's `threading.get_ident()`
    differs from the dead process's own (so the existing same-pid,
    same-thread self-reclaim case does not fire), and it genuinely IS
    alive as that pid right now (so plain pid-liveness checking correctly,
    and unhelpfully, says so too) -- wedging the lock forever, every single
    restart, without the incarnation token this fix adds.

    Real pid reuse cannot be forced deterministically in a portable test
    (the OS assigns pids, not us), so this proves the DECISION LOGIC
    directly: a lock file hand-crafted to claim this exact process's own
    pid, under a thread_id and incarnation token that are NOT this
    process's own, is exactly what such a lock would look like on disk --
    and it must still be reclaimed.
    """

    def test_a_lock_naming_this_pid_under_a_foreign_incarnation_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),  # genuinely THIS process's pid
                        "thread_id": -1,  # not this thread's real ident
                        "incarnation": "a-different-processs-token",
                        "started_at": "long ago, a different incarnation",
                    }
                )
            )
            info = acquire(path)  # must succeed: pid reuse, proven, not guessed
            try:
                self.assertEqual(json.loads(path.read_text())["pid"], os.getpid())
            finally:
                release(info)

    def test_a_lock_naming_a_foreign_pid_under_this_incarnation_is_not_treated_as_reused(self):
        # Control: the incarnation token alone must not become a blanket
        # bypass. A DIFFERENT pid, even if (implausibly) it carried this
        # process's own incarnation token, is not "us" -- pid identity is
        # still the primary fact; incarnation only disambiguates a MATCHING
        # pid.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
            try:
                path.write_text(
                    json.dumps(
                        {
                            "pid": proc.pid,
                            "thread_id": 1,
                            "incarnation": "irrelevant-here",
                            "started_at": "just now",
                        }
                    )
                )
                with self.assertRaises(LockHeld):
                    acquire(path)
            finally:
                proc.kill()
                proc.wait(timeout=5)


class ReleaseWarnsOnAnUnreadableButPresentLock(unittest.TestCase):
    """Found by codex adversarial review, round 16 (2026-08-29):
    `_holder_identity` returns the identical all-`None` triple whether the
    lock path is genuinely ABSENT or PRESENT-but-corrupt (see that
    function's own docstring). The pre-round-16 `release()` treated both
    the same way -- a silent return -- which is correct for "already gone"
    but actively misleading for "exists but corrupt": the file is left
    behind, unremoved, and no operator-visible signal says so or that
    manual cleanup may be needed.
    """

    def test_a_present_but_corrupt_lock_is_left_alone_and_warned_about_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            path.write_text("not json at all")
            fake_info = LockInfo(
                path=path,
                pid=os.getpid(),
                started_at="irrelevant",
                thread_id=threading.get_ident(),
                incarnation="irrelevant",
            )
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                release(fake_info)
            self.assertTrue(path.exists(), "a corrupt lock must be left alone, not silently gone")
            self.assertEqual(path.read_text(), "not json at all")
            self.assertIn(
                "skipped",
                captured.getvalue(),
                "release() must say something when it declines to touch a corrupt lock",
            )

    def test_a_genuinely_absent_lock_prints_nothing(self):
        # Control: the fix above must not make the ORDINARY "already gone,
        # nothing to release" case noisy -- only the corrupt-but-present one.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"  # never created at all
            fake_info = LockInfo(
                path=path,
                pid=os.getpid(),
                started_at="irrelevant",
                thread_id=threading.get_ident(),
                incarnation="irrelevant",
            )
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                release(fake_info)
            self.assertEqual(captured.getvalue(), "", "a genuinely absent lock is silent")


class ReleaseNeverRemovesSomeoneElsesLock(unittest.TestCase):
    """Found by codex adversarial CHALLENGE (2026-08-29, after review round
    8): `release()` used to unlink by pathname unconditionally, with no
    check that the file CURRENTLY there still matched the acquisition being
    released. If a real writer's lock ever ends up at the same path as an
    old, already-invalidated `LockInfo` (e.g. a delayed or duplicate
    `release()` call reached after a different writer legitimately took the
    same path), the blind unlink would remove the NEW writer's own live
    lock, letting a third writer in.
    """

    def test_release_of_a_stale_info_does_not_remove_a_different_writers_real_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            first = acquire(path)
            # Simulate the path now belonging to a genuinely different
            # writer (a different pid/thread/incarnation) -- as if `first`
            # had been released and re-acquired by someone else before this
            # (stale, no-longer-valid) `release(first)` call runs.
            path.unlink()
            path.write_text(
                json.dumps(
                    {
                        "pid": first.pid + 1,
                        "thread_id": first.thread_id + 1,
                        "incarnation": "a-different-writers-incarnation",
                        "started_at": "someone else, now",
                    }
                )
            )
            release(first)  # must NOT remove the file above
            self.assertTrue(path.exists(), "a different writer's real lock must survive")
            self.assertEqual(json.loads(path.read_text())["pid"], first.pid + 1)


class TwoWritersCannotBothBreakTheSameStaleLock(unittest.TestCase):
    """Found by codex adversarial review (2026-08-29): the naive
    check-then-unlink-then-relink has a real window where two processes
    racing to break the same stale lock can BOTH end up believing they hold
    it -- exactly the two-writer outcome #526 exists to prevent. This drives
    real concurrent threads at the actual race window (`_break_and_relink`'s
    own flock-guarded critical section) rather than reasoning about it.

    Threads share this test process's own pid, so `_pid_alive` cannot tell
    them apart the way it would two real processes -- which makes this a
    HARDER case than two real processes racing, not an easier one: the
    second racer, once it gets the mutex, sees the winner's own fresh lock
    (written under the SAME pid this test runs as) and must recognise that
    as live from its CONTENT being current, not from a pid mismatch.
    """

    def _dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = proc.pid
        proc.wait(timeout=5)
        return pid

    def test_exactly_one_of_two_racing_threads_ever_holds_the_lock(self):
        dead_pid = self._dead_pid()
        for _ in range(30):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "lock"
                path.write_text(json.dumps({"pid": dead_pid, "started_at": "long ago"}))

                barrier = threading.Barrier(2)
                results = []
                lock = threading.Lock()

                def racer():
                    barrier.wait()
                    try:
                        info = acquire(path)
                        with lock:
                            results.append(("ok", info))
                    except LockHeld as error:
                        with lock:
                            results.append(("refused", error))

                threads = [threading.Thread(target=racer) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5)

                successes = [r for r in results if r[0] == "ok"]
                self.assertEqual(
                    len(successes),
                    1,
                    f"expected exactly one winner, got {len(results)} results: {results}",
                )
                # Clean up whichever one actually won, matching normal use.
                release(successes[0][1])


class AnUnreadableLockFailsClosed(unittest.TestCase):
    def test_a_lock_file_with_no_parseable_pid_is_treated_as_still_held(self):
        # Fail-closed, per the module's own stated reasoning: unable to name
        # a pid to check means unable to PROVE the lock is stale, and the
        # cost of wrongly refusing (a delayed retry) is far smaller than the
        # cost of wrongly breaking a lock something else may genuinely hold.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            path.write_text("not json at all")
            with self.assertRaises(LockHeld):
                acquire(path)
            self.assertEqual(
                path.read_text(), "not json at all", "the unreadable lock is untouched"
            )


if __name__ == "__main__":
    unittest.main()
