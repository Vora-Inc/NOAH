"""Real process kills, at three distinct points, and what survives them.

Not a simulation: `_kill_harness.py` is started as a genuine OS subprocess
and killed with a genuine `SIGKILL` -- the same interruption a crashed host,
an OOM kill, or an operator's `kill -9` would deliver, which no in-process
mock or clean-shutdown path stands in for. What is asserted afterward is read
back from the same two files the harness itself reads and writes (the
JSON-backed simulated chain, and the loop's own state file) -- never from any
in-memory object this test happened to keep a reference to, because the real
restarted process would not have had access to that either.

Three points, matching the three failure windows a restart must survive
without double-publishing and without losing track of a period:

1. **Mid-period, before anything is decided** -- killed inside
   `chain.score()`, before `keeper.decide()` or `chain.publish()` have run.
2. **Mid-publish** -- killed the instant after the simulated chain has
   durably recorded a commit, before `run_period` returns and before this
   loop's own local state file is updated to match. The gap this is built to
   prove closed: the chain already has the commit: does the restart publish
   a second one?
3. **Between periods, nothing in flight** -- killed while idle, with no
   attempt ever started, and the chain moves on regardless while the
   validator is down.
"""

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = [sys.executable, "-u", "-m", "noah.core.tests._kill_harness"]


def _start(world, state, lock):
    return subprocess.Popen(
        HARNESS + [str(world), str(state), str(lock)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _wait_for_marker(proc, marker, timeout=10):
    """Read lines until `marker` appears, honouring `timeout` for real.

    Found by codex adversarial review (2026-08-29): a plain blocking
    `proc.stdout.readline()` does not return control to re-check the
    deadline until a line actually arrives -- if the harness is alive but
    stops producing output before the expected marker, this would block
    forever instead of failing after `timeout` seconds, hanging the whole
    test run (and CI with it) rather than reporting a clear failure.
    `select.select` with a bounded wait is what actually enforces the
    deadline: it returns control on every iteration whether or not data
    arrived, so the `time.time() < deadline` check is re-evaluated for real.
    """
    deadline = time.time() + timeout
    lines = []
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise AssertionError(
                f"timed out waiting for {marker!r}; output so far:\n" + "".join(lines)
            )
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not ready:
            if proc.poll() is not None:
                raise AssertionError(
                    f"process exited (code {proc.returncode}) before printing {marker!r}; "
                    f"output:\n" + "".join(lines)
                )
            continue
        line = proc.stdout.readline()
        if line == "":
            if proc.poll() is not None:
                raise AssertionError(
                    f"process exited (code {proc.returncode}) before printing {marker!r}; "
                    f"output:\n" + "".join(lines)
                )
            continue
        lines.append(line)
        if marker in line:
            return lines


def _hard_kill(proc):
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise AssertionError("process did not die within 5s of a real SIGKILL")
    try:
        proc.stdout.close()
    except Exception:  # noqa: BLE001
        pass


def _world(path):
    return json.loads(Path(path).read_text())


def _state(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


def _seed_ready_world(path):
    """head=25, tempo=20, settle_blocks=1 -- a period is ready immediately."""
    path.write_text(
        json.dumps(
            {
                "head": 25,
                "tempo": 20,
                "settle_blocks": 1,
                "scores": {"0": 100, "1": 0},
                "live_uids": [0, 1],
                "published_heights": [],
                "publish_calls": [],
            }
        )
    )


class KilledMidPeriodBeforeAnyDecision(unittest.TestCase):
    def test_restart_after_a_kill_during_scoring_still_publishes_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, state, lock = (
                Path(tmp) / "world.json",
                Path(tmp) / "state.json",
                Path(tmp) / "lock",
            )
            _seed_ready_world(world)

            proc = _start(world, state, lock)
            try:
                _wait_for_marker(proc, "MARKER:about_to_score")
                _hard_kill(proc)
            finally:
                if proc.poll() is None:
                    _hard_kill(proc)

            self.assertEqual(
                _world(world)["published_heights"], [], "nothing landed before the kill"
            )
            saved = _state(state)
            self.assertIsNotNone(saved, "the attempt must have been recorded before the crash")
            self.assertEqual(saved["phase"], "attempting")

            proc2 = _start(world, state, lock)
            try:
                _wait_for_marker(proc2, "MARKER:published", timeout=10)
                _wait_for_marker(proc2, "MARKER:idle", timeout=10)
            finally:
                _hard_kill(proc2)

            w = _world(world)
            self.assertEqual(len(w["published_heights"]), 1, "the period was not lost")
            self.assertEqual(len(w["publish_calls"]), 1, "and was not published twice")
            self.assertEqual(_state(state)["phase"], "done")


class KilledMidPublish(unittest.TestCase):
    def test_a_kill_right_after_the_chain_records_the_commit_does_not_double_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, state, lock = (
                Path(tmp) / "world.json",
                Path(tmp) / "state.json",
                Path(tmp) / "lock",
            )
            _seed_ready_world(world)

            proc = _start(world, state, lock)
            try:
                # The simulated chain has ALREADY durably recorded the
                # commit by the time this marker prints (see
                # _kill_harness.FileBackedChain.publish). Killing here is
                # killing after the one irreversible thing has happened, but
                # before this process could tell its own state file so.
                _wait_for_marker(proc, "MARKER:published")
                _hard_kill(proc)
            finally:
                if proc.poll() is None:
                    _hard_kill(proc)

            w = _world(world)
            self.assertEqual(len(w["publish_calls"]), 1, "the one real commit")
            saved = _state(state)
            self.assertEqual(
                saved["phase"], "attempting", "never updated past this before the kill"
            )

            proc2 = _start(world, state, lock)
            try:
                # A restart must recognise, via run_period's own
                # already_published check reading the durable chain (not
                # anything this process remembers locally), that this
                # period is done -- and must NOT call publish() again.
                _wait_for_marker(proc2, "MARKER:idle", timeout=10)
            finally:
                _hard_kill(proc2)

            w2 = _world(world)
            self.assertEqual(len(w2["publish_calls"]), 1, "restart must not double-publish")
            self.assertEqual(len(w2["published_heights"]), 1)
            self.assertEqual(_state(state)["phase"], "already_published")


class KilledBetweenPeriods(unittest.TestCase):
    def test_a_kill_while_idle_then_the_chain_moving_on_is_still_caught_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, state, lock = (
                Path(tmp) / "world.json",
                Path(tmp) / "state.json",
                Path(tmp) / "lock",
            )
            world.write_text(
                json.dumps(
                    {
                        "head": 0,  # nothing settled yet -- genuinely nothing in flight
                        "tempo": 20,
                        "settle_blocks": 1,
                        "scores": {"0": 100, "1": 0},
                        "live_uids": [0, 1],
                        "published_heights": [],
                        "publish_calls": [],
                    }
                )
            )

            proc = _start(world, state, lock)
            try:
                _wait_for_marker(proc, "MARKER:idle")
                self.assertIsNone(
                    _state(state), "nothing was ever attempted, so no state file exists"
                )
                _hard_kill(proc)
            finally:
                if proc.poll() is None:
                    _hard_kill(proc)

            # The chain kept moving while the validator was down, exactly as
            # a real chain would, independent of this process's own life.
            w = _world(world)
            w["head"] = 25
            world.write_text(json.dumps(w))

            proc2 = _start(world, state, lock)
            try:
                _wait_for_marker(proc2, "MARKER:published", timeout=10)
                _wait_for_marker(proc2, "MARKER:idle", timeout=10)
            finally:
                _hard_kill(proc2)

            w2 = _world(world)
            self.assertEqual(
                len(w2["publish_calls"]), 1, "the boundary that arrived while down was caught"
            )
            self.assertEqual(len(w2["published_heights"]), 1)


if __name__ == "__main__":
    unittest.main()
