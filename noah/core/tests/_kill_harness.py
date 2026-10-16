"""A runnable process for the real kill-and-restart tests in
``test_loop_restart_kill.py``.

Not a test module (no ``test_`` functions; the leading underscore also keeps
pytest's own default collection from picking it up). This is the thing that
gets started with ``subprocess.Popen`` and killed with a real ``SIGKILL`` --
"whatever your test harness's real equivalent of a hard kill is", per the
requirement this exists to satisfy: the kill has to happen to an actual OS
process, not a mocked-away interruption, and the state that survives it has
to be read back from disk exactly as a real restarted validator would read
it, not asserted on in-memory objects the real process would never have had
access to after dying.

Usage::

    python -m noah.core.tests._kill_harness <world_path> <state_path> <lock_path>

``world_path`` is a JSON file standing in for the chain: durable across this
process's own death, exactly as a real chain's state is independent of
whether this validator is currently running. It is created with a fresh,
not-yet-settled world the first time this is invoked for a given path, and
loaded as-is on every later invocation -- which is what makes "kill this,
then run the same command again" a faithful restart rather than a fresh run
that happens to share a name.

Every tick prints a marker line, flushed immediately, so the driving test can
watch stdout and send SIGKILL at the moment it wants rather than guessing
with a fixed sleep:

    MARKER:about_to_score      -- inside chain.score(), before any decision
                                   about what to publish has been made.
    MARKER:published           -- immediately after the simulated chain has
                                   durably recorded a commit (before
                                   run_period returns, so before this loop's
                                   own local state file is updated to match).
    MARKER:idle                -- a tick found nothing new ready; equivalent
                                   to "asleep between periods, nothing in
                                   flight".
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from noah.core.loop import run_forever  # noqa: E402
from noah.core.pinning import period_of, pin_height  # noqa: E402

DEFAULT_TEMPO = 20
DEFAULT_SETTLE_BLOCKS = 1

# A deliberate pause right after each marker print, before the risky code
# continues. Without this, "read the marker line, then send SIGKILL" is a
# race against however many more instructions Python executes before the
# signal is actually delivered -- on a fast local run that gap can be small
# enough that the process finishes the whole critical section (including
# releasing the lock and writing this loop's own "done" state) before the
# kill ever lands, which would make the test pass for the wrong reason: not
# because restart-after-a-real-mid-work-kill was proven safe, but because
# there was, unintentionally, nothing left to interrupt. This widens the
# window the driving test's kill has to land in, deliberately, so the kill
# point this file's own docstring promises is the one that actually happens.
KILL_WINDOW_SECONDS = 0.4


class FileBackedChain:
    """`run_period`'s chain interface, plus `current_block`/`tempo` for the
    loop, backed by a JSON file rather than memory.

    Every method re-reads the file before answering and re-writes it after
    mutating, so state genuinely lives on disk between calls -- and survives
    this process being killed -- rather than only appearing to because a
    single Python object happened to stay alive for the length of one test.
    """

    def __init__(self, world_path):
        self.path = Path(world_path)
        if not self.path.exists():
            self._save(
                {
                    "head": 0,
                    "tempo": DEFAULT_TEMPO,
                    "settle_blocks": DEFAULT_SETTLE_BLOCKS,
                    "scores": {"0": 100, "1": 0},
                    "live_uids": [0, 1],
                    # HEIGHT-keyed, not period-keyed -- matches period.py's
                    # own settled `chain.already_published(height)` contract
                    # exactly (it is called with a height, never a period
                    # number). Found by codex adversarial review, round 12
                    # (2026-08-29): this fixture used to record and check
                    # PERIOD numbers, re-derived from `world["tempo"]` at
                    # whatever moment each call happened to run, which let a
                    # tempo change fool `already_published` into treating an
                    # already-published height as new (the same class of bug
                    # already fixed in `test_loop.py`'s own `SchedulableChain`
                    # -- this fixture is a separate, real-subprocess double
                    # that was not covered by that earlier fix).
                    "published_heights": [],
                    "publish_calls": [],
                }
            )

    def _load(self):
        return json.loads(self.path.read_text())

    def _save(self, world):
        tmp = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(world, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    # --- the loop's own extra requirements ------------------------------
    def current_block(self):
        return self._load()["head"]

    def tempo(self):
        return self._load()["tempo"]

    # --- run_period's chain interface -----------------------------------
    def pinned_height(self):
        world = self._load()
        return pin_height(
            head=world["head"], tempo=world["tempo"], settle_blocks=world["settle_blocks"]
        )

    def already_published(self, height):
        world = self._load()
        return height in world["published_heights"]

    def read(self, height):
        return {"height": height}

    def score(self, observations):
        world = self._load()
        print("MARKER:about_to_score", flush=True)
        time.sleep(KILL_WINDOW_SECONDS)  # see KILL_WINDOW_SECONDS's own comment
        return {int(k): v for k, v in world["scores"].items()}

    def agree(self, observations):
        world = self._load()
        return {int(k): v for k, v in world["scores"].items()}

    def live_uids(self, height):
        return self._load()["live_uids"]

    def owner_uids(self):
        return set()

    def fixture_hotkeys(self, height):
        return {uid: f"hk{uid}" for uid in self._load()["live_uids"]}

    def chain_hotkeys(self):
        return {uid: f"hk{uid}" for uid in self._load()["live_uids"]}

    def previous_period(self):
        return None

    def publish(self, vector, height):
        world = self._load()
        period = period_of(height=height, tempo=world["tempo"])
        # The simulated chain records the commit FIRST, durably, exactly as
        # a real chain accepting a submission does not depend on this
        # process staying alive one instruction longer. Everything after
        # this point (returning from publish(), run_period returning, the
        # loop writing its own "done" state) is exactly what a kill in this
        # window is testing whether the restart handles correctly.
        world["published_heights"].append(height)
        world["publish_calls"].append({"period": period, "height": height, "vector": vector})
        self._save(world)
        print("MARKER:published", flush=True)
        time.sleep(KILL_WINDOW_SECONDS)  # see KILL_WINDOW_SECONDS's own comment
        return {"confirmed_on_chain": True, "reveal_round": 1}


def main():
    world_path, state_path, lock_path = sys.argv[1], sys.argv[2], sys.argv[3]
    chain = FileBackedChain(world_path)

    def on_tick(result):
        if not result.acted and result.outcome == "waiting":
            print("MARKER:idle", flush=True)

    run_forever(
        chain,
        settle_blocks=DEFAULT_SETTLE_BLOCKS,
        state_path=state_path,
        lock_path=lock_path,
        poll_seconds=0.05,
        sleep=time.sleep,
        on_tick=on_tick,
    )


if __name__ == "__main__":
    main()
