"""The scheduling loop against a stand-in chain: boundaries, not signing.

Every test here drives `tick`/`run_forever` directly, in-process, with a fake
`sleep` so nothing here waits on a real clock. The real-process kill-and-
restart proof lives in `test_loop_restart_kill.py`, which actually starts and
SIGKILLs a subprocess -- this file is the pure-logic half: does the loop wake
at the right boundaries, exactly once each, without inventing a second
period math next to `pinning.py`'s own.
"""

import json
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from ...chain.agreeing import AgreeingChain
from ...chain.dayblocks import day_block_range
from ..loop import LoopRefused, TickResult, read_state, run_forever, tick
from ..pinning import period_of, pin_height
from ..vector import SeatOwnerUnknown
from ..writer_lock import acquire, release


class SchedulableChain:
    """`run_period`'s own chain interface, plus `current_block`/`tempo`, the
    two extra facts the loop itself needs to decide WHEN to call it.

    A close cousin of `test_integration.py`'s own `StandInChain`, extended
    with a mutable head so a test can advance the chain between ticks the
    way real blocks arrive between polls.
    """

    def __init__(self, *, head=0, tempo=20, **world):
        self.head = head
        self.tempo_ = tempo
        self.w = world
        # Tracked by HEIGHT, not by a period number re-derived at check
        # time -- matching period.py's own settled contract exactly:
        # `chain.already_published(height)` (period.py step 2) is called
        # with a HEIGHT, never a period number. Found by codex adversarial
        # review, round 10 (2026-08-29): the earlier version of this fixture
        # tracked PERIOD numbers instead (`period_of(height, self.tempo_)`,
        # computed fresh both at publish time and at check time), which is
        # NOT what the real interface asks for, and it let a tempo change
        # between two publishes fool `already_published` -- height 19,
        # already published as "period 1" under tempo 10, would (wrongly)
        # read as "never published" once tempo changed to 20, because
        # `period_of(19, 20)` is 0, a period number nothing had ever
        # recorded. period.py's own choice of a HEIGHT-keyed check, not a
        # period-keyed one, is precisely what makes the real settled
        # interface immune to this: a height either was or was not
        # published, independent of whatever tempo is in effect when the
        # question is asked later. This fixture now matches that.
        self.published_heights = set()
        # Found by an independent Verifier (2026-09-21) against Day 0
        # catch-up: HEIGHT-keyed membership alone is faithful for the
        # round-10 scenario above (an exact literal height repeated after a
        # LATER, unrelated tempo change), but it is NOT faithful to the
        # real `ChainAdapter.already_published` for a DIFFERENT height that
        # lands in the SAME period under the SAME (unchanged) tempo -- the
        # real check (`noah/core/chain_adapter.py`) scans
        # `reader.committed_in_period` for a whole RANGE of periods, not a
        # literal scored height, so any two heights sharing a period read
        # as "already published" together on the real chain once either
        # one has landed. Day 0 catch-up can return a height that is NOT a
        # tempo-period boundary at all (Day 0's own true last block), so it
        # can share a period with the ordinary tempo-pinned height the very
        # next tick -- a case the pre-fix fixture never modeled, matching
        # `test_chain_adapter.py`'s own `FakeReader.committed_in_period`
        # pattern: track published PERIODS too, keyed by the tempo that was
        # actually in effect at publish time (never mixed across a tempo
        # change -- see `already_published`'s own comment below for why
        # that separate keying still protects the round-10 scenario).
        self.published_periods_by_tempo = {}
        self.publish_calls = []

    # --- the loop's own extra requirements ------------------------------
    def current_block(self):
        return self.head

    def tempo(self):
        return self.tempo_

    # --- run_period's chain interface -----------------------------------
    def pinned_height(self):
        return pin_height(
            head=self.head, tempo=self.tempo_, settle_blocks=self.w.get("settle_blocks", 1)
        )

    def already_published(self, height):
        if height in self.published_heights:
            return True
        # Same period, SAME tempo as when it was actually published --
        # deliberately NOT compared against a period recorded under a
        # DIFFERENT tempo (see `__init__`'s own comment): a period number
        # only means the same thing when both sides are computed under the
        # identical tempo, the identical reasoning `..loop`'s own
        # reorg-suspicion check and its new same-period check already rest
        # on.
        period = period_of(height=height, tempo=self.tempo_)
        return period in self.published_periods_by_tempo.get(self.tempo_, set())

    def read(self, height):
        return {"height": height}

    def score(self, observations):
        if isinstance(self.w.get("score"), Exception):
            raise self.w["score"]
        return dict(self.w.get("scores", {0: 100, 1: 0}))

    def agree(self, observations):
        return dict(self.w.get("scores", {0: 100, 1: 0}))

    def live_uids(self, height):
        return self.w.get("live_uids", [0, 1])

    def owner_uids(self):
        return self.w.get("owner_uids", set())

    def fixture_hotkeys(self, height):
        default = {uid: f"hk{uid}" for uid in self.w.get("live_uids", [0, 1])}
        return self.w.get("fixture_hotkeys", default)

    def chain_hotkeys(self):
        default = {uid: f"hk{uid}" for uid in self.w.get("live_uids", [0, 1])}
        return self.w.get("chain_hotkeys", default)

    def previous_period(self):
        return self.w.get("previous_period")

    def publish(self, vector, height):
        period = period_of(height=height, tempo=self.tempo_)
        self.published_heights.add(height)
        self.published_periods_by_tempo.setdefault(self.tempo_, set()).add(period)
        self.publish_calls.append((period, dict(vector), height))
        return {"confirmed_on_chain": True, "reveal_round": 1}


# --- Day 0 catch-up fixtures (ticket #16, the short-first-day gap) --------
#
# A real, confirmed problem diagnosed against tonight's live chain reset:
# `pinning.pin_height` only ever returns a tempo-period boundary, and the
# smallest height it can EVER return is `tempo - 1` -- so if a chain's own
# genesis lands late enough in the UTC day that Day 0 ends before block
# `tempo - 1`, `pin_height` can structurally never name a height inside it,
# no matter how long the unattended loop runs. See `..loop`'s own top
# docstring, "Day 0 catch-up", for the fix.
#
# `_DaySource`/`_day_aware_runner_chain` reproduce, for these tests only,
# the identical fake-source shape `noah.chain.tests.test_dayblocks.Fake`
# already establishes for `noah.chain.dayblocks`'s own tests (linear,
# monotonic block times, a real `AgreeingChain` wrapping two of them, not a
# hand-rolled single-source stand-in) -- proving the fix against the SAME
# real, reviewed day-boundary search `day_block_range` actually uses, not a
# mock of its answer.
DAY_ZERO_TICK = 241  # fake seconds per block
DAY_ZERO_ANCHOR = 1_000_000  # day_anchor_ts for every fixture below

# tempo=360: Day 0 (block 1 through the last block before the boundary)
# spans blocks 1..358 -- block 358's own fake timestamp is
# DAY_ZERO_ANCHOR + 358*241 = ...+86278, still short of one day
# (86400) past the anchor; block 359's is ...+86519, past it. Block 359 is
# therefore Day 1's own first block -- the exact shape tonight's real reset
# produced (block 358 real, block 359 the next day's first), reproduced
# here through fake numbers rather than a real chain.
DAY_ZERO_TEMPO = 360
DAY_ZERO_LAST_BLOCK = 358


class _DaySource:
    """One source for a fake, dual-sourced ``runner_chain``: block
    timestamps only, linear in block number, and a live head read through
    ``head_getter`` so a test that advances a chain's own ``head`` between
    ticks (the way real blocks arrive between polls) moves this source's
    own head right along with it, without rebuilding it.
    """

    def __init__(self, head_getter):
        self._head_getter = head_getter

    def head_block(self):
        return self._head_getter()

    def block_timestamp(self, block):
        return DAY_ZERO_ANCHOR + block * DAY_ZERO_TICK

    def canary(self, block):
        return 256


def _day_aware_runner_chain(head_getter):
    a, b = _DaySource(head_getter), _DaySource(head_getter)
    return AgreeingChain(
        sources={"archive": a, "latent": b},
        endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
    )


class DayZeroAwareChain(SchedulableChain):
    """``SchedulableChain`` plus the two attributes
    ``_day_zero_catch_up_height`` reads (``runner_chain``,
    ``day_anchor_ts``) -- everything else, including the height-keyed
    ``already_published`` that is the actual, chain-authoritative
    protection against a republish, is inherited unchanged.
    """

    def __init__(self, *, head=0, tempo=DAY_ZERO_TEMPO, **world):
        super().__init__(head=head, tempo=tempo, **world)
        self.day_anchor_ts = DAY_ZERO_ANCHOR
        self.runner_chain = _day_aware_runner_chain(lambda: self.head)


# --- Day catch-up fixtures (issue #244, any day's own last chance) --------
#
# The real, confirmed gap: `pin_height` only ever names the LATEST settled
# tempo period, with no memory of what it named a moment ago. An ordinary
# day's own last-chance period boundary can be attempted while that day
# genuinely has not ended yet -- refused `DayNotComplete`, correctly -- and
# the very next period `pin_height` ever names can already belong to the
# NEXT day, so the day that just closed never gets a second look. See
# `..loop`'s own top docstring, "Day catch-up: any day's own last chance,
# not only Day 0's", for the fix this proves.
#
# Reuses the identical `DAY_ZERO_ANCHOR`/`DAY_ZERO_TICK`/`DAY_ZERO_TEMPO`
# timing fixture Day 0's own tests already establish above (so this is
# proven against the SAME real, reviewed `day_block_range` machinery, not a
# second stand-in), but examines Day 1 -- an ORDINARY day, long enough to
# span many tempo periods (359..717, nothing like Day 0's own short-day
# edge case, so `_day_zero_catch_up_height`'s own length check never
# engages here: `to_block=717` is nowhere near `< tempo - 1 = 359`),
# confirmed directly below (`test_day_one_is_an_ordinary_day_not_a_day_zero_edge_case`).
class DayAwareChain(DayZeroAwareChain):
    """``DayZeroAwareChain`` plus a REAL day-boundary check in
    ``already_published`` -- the exact shape
    ``noah.core.chain_adapter.ChainAdapter.already_published``'s own
    direct-mode path uses (``day, _from_block, to_block =
    self._day_window(height)``, called UNWRAPPED, before any existence
    check -- see that method's own docstring), so a period whose own day
    has not ended yet is refused a REAL ``DayNotComplete``, exactly the
    shape the live rehearsal chain log shows (issue #244), not a stand-in
    exception. Publication is tracked by DAY, not by height or period --
    matching ``ChainAdapter.read``'s own real behaviour (`day_window`
    reads a whole day's range regardless of which height inside it
    triggered the read): any height within an already-published day reads
    back as published too, exactly like the real system.
    """

    def __init__(self, *, head=0, tempo=DAY_ZERO_TEMPO, **world):
        super().__init__(head=head, tempo=tempo, **world)
        self.published_days = set()

    def _day_window(self, height):
        return day_block_range(self.runner_chain, self.day_anchor_ts, height)

    def already_published(self, height):
        day, _from_block, _to_block = self._day_window(height)
        return day in self.published_days

    def read(self, height):
        day, from_block, to_block = self._day_window(height)
        return {"height": height, "day": day, "from_block": from_block, "to_block": to_block}

    def publish(self, vector, height):
        day, _from_block, _to_block = self._day_window(height)
        self.published_days.add(day)
        period = period_of(height=height, tempo=self.tempo_)
        self.published_heights.add(height)
        self.published_periods_by_tempo.setdefault(self.tempo_, set()).add(period)
        self.publish_calls.append((period, dict(vector), height))
        return {"confirmed_on_chain": True, "reveal_round": 1}


# Day 1's own last-chance period boundary (period 0, `pin_height`'s
# smallest possible output for this tempo): confirmed by direct computation
# against the same fixed timing above -- day 1 spans blocks 359..717, and
# the very NEXT period boundary (719) already falls in day 2 (718..1075),
# with no period boundary landing anywhere in between. This is the exact
# shape issue #244 reports: "pin height jumped from inside Day 2 straight
# to inside Day 3", reproduced here one day earlier in the numbering only
# because Day 0 needs its own dedicated fixture above.
DAY_ONE_LAST_CHANCE_HEIGHT = 359


def _paths(tmp):
    return str(Path(tmp) / "state.json"), str(Path(tmp) / "lock")


class MultiplePeriodsInSequence(unittest.TestCase):
    """The property #535 names first: several real boundaries, unattended,
    no missed period and no double-fire."""

    def test_advancing_head_by_a_full_period_each_tick_publishes_each_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)

            steps = []

            def fake_sleep(_seconds):
                chain.head += chain.tempo_
                steps.append("slept")

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=5,
            )

            periods = [p for p, _v, _h in chain.publish_calls]
            self.assertEqual(periods, sorted(set(periods)), "no period published out of order")
            self.assertEqual(len(periods), len(set(periods)), "no period published twice")
            self.assertEqual(len(periods), 5, "every one of the 5 ticks found a new period ready")
            # Consecutive: nothing skipped between the first and the last.
            self.assertEqual(periods, list(range(periods[0], periods[0] + 5)))

    def test_polling_more_often_than_periods_change_does_not_double_fire(self):
        # Advance the head by a QUARTER period each tick, so most ticks land
        # inside the same period as the one before them. Only every 4th tick
        # should actually publish.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)

            def fake_sleep(_seconds):
                chain.head += 5  # a quarter of tempo=20

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=16,
            )

            periods = [p for p, _v, _h in chain.publish_calls]
            self.assertEqual(len(periods), len(set(periods)), "no period published twice")
            self.assertGreaterEqual(len(periods), 3, "at least the boundaries actually crossed")


class AGapIsReportedNotHidden(unittest.TestCase):
    """Found by codex adversarial review (2026-08-29): if this loop fell
    behind by more than one period, `pin_height` only ever names the LATEST
    one -- by its own settled design (pinning.py, "does NOT reach further
    back") -- so the intervening periods can never be individually
    attempted. Not a bug this loop can fix, but it must say so rather than
    silently moving on as if nothing were skipped."""

    def test_a_multi_period_gap_is_named_in_the_result_and_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # height 39 is period 1 (head=45 pins it elsewhere in this file);
            # head=105, tempo=20, settle_blocks=1 pins height 99, period 4 --
            # so periods 2 and 3 were never individually attempted.
            chain = SchedulableChain(head=105, tempo=20, settle_blocks=1)
            result, last_attempted_height = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=39,
            )
            self.assertTrue(result.acted)
            self.assertEqual(result.period, 4)
            self.assertEqual(last_attempted_height, 99)
            self.assertIn("GAP: 2 period(s)", result.detail)
            saved = read_state(state_path)
            self.assertEqual(saved["gap_before"], 2)

    def test_no_gap_note_when_the_very_next_period_is_the_one_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=45, tempo=20, settle_blocks=1)  # period 1
            result, _ = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=19,  # height 19 is period 0
            )
            self.assertNotIn("GAP:", result.detail)
            self.assertEqual(read_state(state_path)["gap_before"], 0)

    def test_a_first_ever_run_with_no_last_attempted_reports_no_gap(self):
        # last_attempted_height=None means "never run before", not "fell
        # behind" -- there is nothing to have skipped yet.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=105, tempo=20, settle_blocks=1)
            result, _ = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=None,
            )
            self.assertNotIn("GAP:", result.detail)


class DriftBetweenThisTicksPlanAndWhatRunPeriodActuallyDid(unittest.TestCase):
    """Found by codex adversarial review (2026-08-29): `run_period` re-derives
    its own pinned height independently rather than trusting the value this
    tick already computed. If the chain crosses a period boundary in the gap
    between them, trusting this tick's own stale precomputed height for the
    state file and `last_attempted_height` would mislabel what actually
    happened -- and, combined with pin_height's own "latest only" design,
    make the period this tick PLANNED for permanently unrecoverable while a
    DIFFERENT period silently got published in its place.
    """

    def test_the_recorded_period_is_what_run_period_actually_scored_not_what_this_tick_planned(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class DriftingChain(SchedulableChain):
                """The chain advances by a full period on every call to
                `pinned_height()` itself, AFTER computing its own answer --
                the narrowest residual window left once `tick`'s own
                pre-run_period re-check (`_freshest_period`) already closes
                the wider one this test used to exercise (a bump inside
                `current_block()`, called once before the lock). This is the
                gap between `_freshest_period`'s own call to
                `chain.pinned_height()` and `run_period`'s own, one
                statement later -- proving that gap is narrowed, not
                eliminated, without touching `run_period`'s settled
                interface."""

                def pinned_height(self):
                    value = pin_height(head=self.head, tempo=self.tempo_, settle_blocks=1)
                    self.head += self.tempo_
                    return value

            chain = DriftingChain(head=25, tempo=20, settle_blocks=1)
            planned_period = period_of(
                height=pin_height(head=25, tempo=20, settle_blocks=1), tempo=20
            )
            actually_scored_height = pin_height(head=45, tempo=20, settle_blocks=1)
            actually_scored_period = period_of(height=actually_scored_height, tempo=20)
            self.assertNotEqual(planned_period, actually_scored_period, "the setup must drift")

            result, last_attempted_height = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertTrue(result.acted)
            self.assertEqual(result.period, actually_scored_period)
            self.assertEqual(last_attempted_height, actually_scored_height)
            self.assertIn("DRIFT:", result.detail)
            self.assertEqual(len(chain.publish_calls), 1)
            self.assertEqual(chain.publish_calls[0][0], actually_scored_period)

            saved = read_state(state_path)
            self.assertEqual(saved["period"], actually_scored_period)
            self.assertEqual(saved["height"], actually_scored_height)

    def test_no_drift_note_when_nothing_moved_between_the_two_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            result, _ = tick(chain, settle_blocks=1, state_path=state_path, lock_path=lock_path)
            self.assertNotIn("DRIFT:", result.detail)


class DriftCanLoseAWholePeriodAndTheGapMustStillBeCounted(unittest.TestCase):
    """Found by codex adversarial review, round 2 (2026-08-29): recording
    `actual_period` (round 1's own fix) stopped MISLABELING what happened,
    but the gap count was still measured against the stale planned `period`
    -- so a period lost entirely to drift (the chain moving one MORE period
    ahead than planned, between this tick's own read and run_period's own
    fresh one) produced no GAP note at all, even though a whole period had
    just been silently skipped. The gap must be measured against the
    AUTHORITATIVE `actual_period`, not the plan.
    """

    def test_a_period_lost_purely_to_drift_is_still_reported_as_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class DriftingChain(SchedulableChain):
                def current_block(self):
                    value = self.head
                    self.head += self.tempo_
                    return value

            # head=45 plans period 1; the chain then drifts one more period
            # ahead before run_period's own fresh pin, actually scoring
            # period 2. last_attempted_height=19 (period 0) means the PLAN
            # (period 1) would show zero gap on its own -- period 1 itself
            # is the one this test proves must not vanish silently.
            chain = DriftingChain(head=45, tempo=20, settle_blocks=1)
            planned_period = period_of(
                height=pin_height(head=45, tempo=20, settle_blocks=1), tempo=20
            )
            actual_height = pin_height(head=65, tempo=20, settle_blocks=1)
            actual_period = period_of(height=actual_height, tempo=20)
            self.assertEqual(planned_period, 1)
            self.assertEqual(actual_period, 2)
            # Confirms the bug's own precondition: the OLD gap math (planned
            # vs before_attempt) would have reported zero.
            self.assertEqual(planned_period - 0 - 1, 0)

            result, last_attempted_height = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=19,
            )
            self.assertEqual(result.period, actual_period)
            self.assertEqual(last_attempted_height, actual_height)
            self.assertIn("GAP: 1 period(s)", result.detail)
            self.assertEqual(read_state(state_path)["gap_before"], 1)


class ATransientPinFailureDoesNotPermanentlyLoseThePeriod(unittest.TestCase):
    """Found by codex adversarial review, round 2 (2026-08-29):
    `run_period`'s own step 1 wraps ANY exception from
    `chain.pinned_height()` -- including a genuinely transient one -- into
    `HeightNotPinned`. Treating that the same as a real per-period refusal
    (the ORIGINAL code did, via PERIOD_REFUSALS) would advance
    `last_attempted` past a period nothing was actually attempted for, and
    `run_forever`'s own "do not retry a refused period" discipline would
    then never come back to it even after the connection recovered.
    """

    def test_a_transient_pin_failure_is_waiting_not_a_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class FlakyPinChain(SchedulableChain):
                def pinned_height(self):
                    raise RuntimeError("transient RPC failure")

            chain = FlakyPinChain(head=25, tempo=20, settle_blocks=1)
            result, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "waiting")
            self.assertFalse(result.acted)
            self.assertIsNone(last_attempted, "not advanced: nothing was actually attempted")
            self.assertEqual(read_state(state_path)["phase"], "read_failed")
            self.assertEqual(chain.publish_calls, [])

    def test_the_period_is_still_published_once_the_pin_read_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class RecoveringPinChain(SchedulableChain):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    self.pin_attempts = 0

                def pinned_height(self):
                    self.pin_attempts += 1
                    if self.pin_attempts <= 2:
                        raise RuntimeError("transient RPC failure")
                    return pin_height(head=self.head, tempo=self.tempo_, settle_blocks=1)

            chain = RecoveringPinChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=3,
            )
            self.assertEqual(
                len(chain.publish_calls), 1, "the SAME period, not skipped, once recovered"
            )


class LockBusyIsNotSeededAsARealRefusalOnRestart(unittest.TestCase):
    """Found by codex adversarial review (2026-08-29): the state file used to
    record a lock-contention tick under the same "refused" phase as a real
    scoring refusal. `run_forever`'s own restart-seeding treats "refused" as
    terminal (do not retry until the next boundary) -- so a restart right
    after a merely-busy lock would skip a period that was never actually
    attempted at all."""

    def test_a_lock_busy_tick_is_persisted_under_its_own_distinct_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            other = acquire(lock_path)
            try:
                tick(chain, settle_blocks=1, state_path=state_path, lock_path=lock_path)
            finally:
                release(other)
            self.assertEqual(read_state(state_path)["phase"], "lock_busy")

    def test_run_forever_retries_the_same_period_immediately_after_a_restart_that_saw_lock_busy(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)

            # Simulate the crash point: a prior run's LAST tick found the
            # lock busy and persisted that, then the process restarted.
            other = acquire(lock_path)
            try:
                tick(chain, settle_blocks=1, state_path=state_path, lock_path=lock_path)
            finally:
                release(other)
            self.assertEqual(read_state(state_path)["phase"], "lock_busy")

            # A fresh run_forever call re-reads state from disk (the restart)
            # and, because "lock_busy" does not seed last_attempted, must
            # still publish this period rather than skipping it.
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(len(chain.publish_calls), 1)


class ATransientReadFailureDoesNotCrashTheWholeLoop(unittest.TestCase):
    """`chain.current_block()`/`chain.tempo()` are the loop's own first read
    each tick, before `pin_height` even runs. A dropped connection there must
    not take down an otherwise-healthy unattended process -- it is the same
    "nothing to act on this tick" shape `pin_height` itself already treats
    `chain.pinned_height()` failing as (see `period.py` step 1)."""

    def test_a_read_failure_is_reported_as_waiting_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class FlakyChain(SchedulableChain):
                def current_block(self):
                    raise RuntimeError("dropped connection")

            chain = FlakyChain(head=25, tempo=20, settle_blocks=1)
            result, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "waiting")
            self.assertFalse(result.acted)
            self.assertIsNone(last_attempted)

    def test_the_loop_recovers_and_publishes_once_reads_start_working_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class RecoveringChain(SchedulableChain):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    self.attempts = 0

                def current_block(self):
                    self.attempts += 1
                    if self.attempts <= 2:
                        raise RuntimeError("dropped connection")
                    return self.head

            chain = RecoveringChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=3,
            )
            self.assertEqual(len(chain.publish_calls), 1, "recovered and published once")


class ARefusedPeriodIsNotRetriedWithinTheRun(unittest.TestCase):
    def test_a_refusal_is_recorded_and_the_same_period_is_not_retried_until_the_next_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(
                head=25, tempo=20, settle_blocks=1, score=RuntimeError("engine down")
            )

            calls = []

            def fake_sleep(_seconds):
                calls.append("tick")

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=4,
                on_tick=lambda r: calls.append(r),
            )

            # Every tick after the first must be "waiting", not a fresh
            # attempt: run_period is refusing for a REASON, and re-running it
            # every tick would spend nothing new while looking like progress.
            results = [c for c in calls if isinstance(c, TickResult)]
            self.assertEqual(results[0].outcome, "refused")
            for later in results[1:]:
                self.assertEqual(later.outcome, "waiting")
            self.assertEqual(chain.publish_calls, [])

    def test_the_next_real_boundary_is_still_caught_after_a_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(
                head=25, tempo=20, settle_blocks=1, score=RuntimeError("engine down")
            )

            ticks = {"n": 0}

            def fake_sleep(_seconds):
                ticks["n"] += 1
                if ticks["n"] == 2:
                    # The engine recovers, and the chain moves into the NEXT
                    # period -- both at once, to prove the loop does not stay
                    # stuck on the period that already refused.
                    chain.w.pop("score", None)
                chain.head += chain.tempo_

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=3,
            )

            self.assertEqual(len(chain.publish_calls), 1)


class AChainUnreadableFromAnUnwrappedCallIsWaitingNotFatal(unittest.TestCase):
    """Found by codex adversarial review, round 4 (2026-08-29): run_period
    calls chain.already_published(), chain.live_uids(), chain.owner_uids(),
    chain.fixture_hotkeys(), and chain.chain_hotkeys() with no try/except of
    its own around any of them. This codebase's own established reader
    (reader.py) raises ChainUnreadable for exactly "a dropped connection, a
    timeout" -- explicitly not a scoring fault -- and every other caller of
    it in this codebase (neurons/validator.py's gate()/survey()) treats it
    as a clean, retryable condition, never a crash. Before this fix, it
    would have propagated straight out of run_forever and killed the whole
    unattended process on a normal transport hiccup.
    """

    def test_chain_unreadable_from_live_uids_is_waiting_not_a_crash(self):
        from ..reader import ChainUnreadable

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class FlakyLiveUidsChain(SchedulableChain):
                def live_uids(self, height):
                    raise ChainUnreadable("dropped mid-read")

            chain = FlakyLiveUidsChain(head=25, tempo=20, settle_blocks=1)
            result, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "waiting")
            self.assertFalse(result.acted)
            self.assertIsNone(last_attempted, "not advanced: nothing was actually attempted")
            self.assertEqual(read_state(state_path)["phase"], "read_failed")
            self.assertEqual(chain.publish_calls, [])

    def test_chain_unreadable_from_owner_uids_is_waiting_not_a_crash(self):
        from ..reader import ChainUnreadable

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class FlakyOwnerUidsChain(SchedulableChain):
                def owner_uids(self):
                    raise ChainUnreadable("dropped mid-read")

            chain = FlakyOwnerUidsChain(head=25, tempo=20, settle_blocks=1)
            result, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "waiting")
            self.assertIsNone(last_attempted)
            self.assertEqual(chain.publish_calls, [])

    def test_run_forever_recovers_and_publishes_once_the_read_recovers(self):
        from ..reader import ChainUnreadable

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class RecoveringLiveUidsChain(SchedulableChain):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    self.attempts = 0

                def live_uids(self, height):
                    self.attempts += 1
                    if self.attempts <= 2:
                        raise ChainUnreadable("dropped mid-read")
                    return self.w.get("live_uids", [0, 1])

            chain = RecoveringLiveUidsChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=3,
            )
            self.assertEqual(
                len(chain.publish_calls), 1, "the SAME period, not skipped, once recovered"
            )


class AChainUnreadableWrappedInsidePeriodRefusedIsAlsoWaiting(unittest.TestCase):
    """Found by codex adversarial CHALLENGE (2026-08-29, after review round
    8): `run_period`'s own chain.read/chain.score/chain.agree/chain.publish
    calls each catch `except Exception as error: raise PeriodRefused(...)
    from error` (period.py, its own settled steps). A `ChainUnreadable` from
    ANY of those four calls therefore arrives here already re-wrapped as
    `PeriodRefused` -- it can never reach the dedicated
    `except (HeightNotPinned, ChainUnreadable)` clause, which only ever sees
    one raised directly, unwrapped. Before this fix, a plain dropped
    connection during chain.read() -- a substantial chain query, no less
    exposed to a timeout than the calls already handled -- was treated as a
    genuine, permanent refusal: last_terminal_height advanced, and the
    period was lost forever the moment the connection recovered.
    """

    def test_chain_unreadable_from_read_wrapped_in_periodrefused_is_waiting_not_terminal(self):
        from ..reader import ChainUnreadable

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class FlakyReadChain(SchedulableChain):
                def read(self, height):
                    raise ChainUnreadable("dropped mid-read")

            chain = FlakyReadChain(head=25, tempo=20, settle_blocks=1)
            result, last_attempted_height = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "waiting")
            self.assertFalse(result.acted)
            self.assertIsNone(last_attempted_height, "not advanced: nothing was actually attempted")
            self.assertEqual(read_state(state_path)["phase"], "read_failed")
            self.assertEqual(chain.publish_calls, [])

    def test_a_genuine_periodrefused_with_no_chainunreadable_cause_still_refuses_terminally(self):
        # Control: the fix must not swallow REAL refusals. A PeriodRefused
        # NOT caused by a ChainUnreadable (here: chain.score() raising a
        # plain bug-shaped error) must still be a genuine, terminal refusal.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(
                head=25, tempo=20, settle_blocks=1, score=RuntimeError("engine down")
            )
            result, last_attempted_height = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "refused")
            self.assertTrue(result.acted)
            self.assertIsNotNone(last_attempted_height, "a genuine refusal is still terminal")
            self.assertEqual(read_state(state_path)["phase"], "refused")

    def test_run_forever_recovers_and_publishes_once_the_wrapped_read_recovers(self):
        from ..reader import ChainUnreadable

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class RecoveringReadChain(SchedulableChain):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    self.attempts = 0

                def read(self, height):
                    self.attempts += 1
                    if self.attempts <= 2:
                        raise ChainUnreadable("dropped mid-read")
                    return {"height": height}

            chain = RecoveringReadChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=3,
            )
            self.assertEqual(
                len(chain.publish_calls), 1, "the SAME period, not skipped, once recovered"
            )

    def test_chain_unreadable_from_publish_wrapped_in_periodrefused_is_terminal_not_waiting(self):
        # Found by codex adversarial review, round 10 (2026-08-29): the fix
        # above (read-side ChainUnreadable is transient) must NOT extend to
        # a ChainUnreadable from chain.publish() itself -- a failure there
        # can mean the broadcast succeeded and only the confirmation read
        # failed, so retrying risks a second submission for a period that
        # may already be on chain. period.py's own step-6 docstring says
        # plainly that a publish failure must not auto-retry; this proves
        # the loop actually honours that for THIS specific failure shape,
        # not just for a plain, unwrapped publish exception.
        from ..reader import ChainUnreadable

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class FlakyPublishChain(SchedulableChain):
                def publish(self, vector, height):
                    raise ChainUnreadable("dropped mid-confirm, after the broadcast")

            chain = FlakyPublishChain(head=25, tempo=20, settle_blocks=1)
            result, last_attempted_height = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "refused")
            self.assertTrue(result.acted)
            self.assertIsNotNone(
                last_attempted_height, "a publish-stage failure is terminal, not retried"
            )
            self.assertEqual(read_state(state_path)["phase"], "refused")

    def test_run_forever_does_not_retry_a_publish_stage_chain_unreadable(self):
        from ..reader import ChainUnreadable

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class FlakyPublishChain(SchedulableChain):
                def publish(self, vector, height):
                    raise ChainUnreadable("dropped mid-confirm, after the broadcast")

            chain = FlakyPublishChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=3,
            )
            # Never actually landed (this fake always raises), and never
            # retried within the run either: a second attempt would show up
            # as more calls reaching publish, which there is no direct
            # counter for here, so the state file's own terminal phase
            # after 3 ticks (never reverting to "attempting" a second time
            # for the SAME period) is the observable proxy -- it stays
            # "refused" at the same height across every remaining tick.
            self.assertEqual(read_state(state_path)["phase"], "refused")


class ARefusalIsAttributedToTheFreshlyVerifiedPeriodNotTheStalePlan(unittest.TestCase):
    """Found by codex adversarial review, round 3 (2026-08-29): a refusal
    used to be recorded against this tick's own STALE pre-lock plan, which
    can name a DIFFERENT period than the one run_period's own internal pin
    actually failed on. That silently permits an accidental retry of the
    period that actually failed (last_attempted would be set to the wrong
    number, so the NEXT tick sees the real failing period as still "not yet
    attempted" and tries it again) -- exactly the auto-retry run_period's
    own docstring says is the operator's decision, not the loop's.
    """

    def test_a_refusal_after_drift_is_attributed_to_the_refreshed_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class DriftingFailingChain(SchedulableChain):
                def current_block(self):
                    value = self.head
                    self.head += self.tempo_
                    return value

                def score(self, observations):
                    raise RuntimeError("engine down")

            chain = DriftingFailingChain(head=25, tempo=20, settle_blocks=1)
            planned_period = period_of(
                height=pin_height(head=25, tempo=20, settle_blocks=1), tempo=20
            )
            refreshed_height = pin_height(head=45, tempo=20, settle_blocks=1)
            refreshed_period = period_of(height=refreshed_height, tempo=20)
            self.assertNotEqual(planned_period, refreshed_period, "the setup must drift")

            result, last_attempted_height = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "refused")
            self.assertEqual(result.period, refreshed_period)
            self.assertEqual(last_attempted_height, refreshed_height)
            self.assertEqual(read_state(state_path)["period"], refreshed_period)
            self.assertEqual(read_state(state_path)["height"], refreshed_height)


class AnyChainRefusalIsHandledNotJustTheTwoOriginallyNamed(unittest.TestCase):
    """Found by codex adversarial review, round 3 (2026-08-29): round 1's
    fix caught `OwnerWouldBePaid`/`SeatOwnerUnknown` (vector.py) by name,
    but those are two of OVER FORTY `Refusal` subclasses spread across
    `noah/chain/` (`runner.py`'s own `RunRefused`, `sources.py`'s own
    `SourceDisagreement`, and many more) -- any of which a real chain
    adapter's own `score`/`agree`/`read` could raise, none of which
    `run_period`'s own `except Exception` around those calls would catch,
    since `Refusal` is a `BaseException` subclass. Catching the shared base
    class itself, rather than enumerating subclasses one review round at a
    time, is what actually closes this."""

    def test_a_refusal_type_never_individually_named_is_still_handled(self):
        from noah.chain.sources import SourceDisagreement

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class DisagreeingChain(SchedulableChain):
                def score(self, observations):
                    raise SourceDisagreement("two independent sources disagree")

            chain = DisagreeingChain(head=25, tempo=20, settle_blocks=1)
            result, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "refused")
            self.assertIn("SourceDisagreement", result.detail)
            self.assertIsNotNone(last_attempted)
            self.assertEqual(chain.publish_calls, [])

    def test_run_forever_keeps_running_after_an_unnamed_refusal_type(self):
        from noah.chain.sources import SourceDisagreement

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class DisagreeingChain(SchedulableChain):
                def score(self, observations):
                    raise SourceDisagreement("two independent sources disagree")

            chain = DisagreeingChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=2,
            )
            self.assertEqual(chain.publish_calls, [])


class AVectorRefusalIsHandledNotFatal(unittest.TestCase):
    """Found by codex adversarial review (2026-08-29): `publishable_vector`
    (vector.py) raises `SeatOwnerUnknown`/`OwnerWouldBePaid`, both
    `BaseException` subclasses `run_period` calls with no try/except of its
    own around them. Before this was in `PERIOD_REFUSALS`, either one would
    propagate straight out of `run_forever`'s own while loop and kill the
    entire unattended process -- on a condition (a live seat's current owner
    not read) that is realistic in production, not hypothetical.
    """

    def test_a_seat_whose_current_owner_was_not_read_refuses_the_period_not_the_whole_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # live_uids names seat 2; chain_hotkeys omits it -- exactly the
            # condition publishable_vector's SeatOwnerUnknown exists for.
            chain = SchedulableChain(
                head=25,
                tempo=20,
                settle_blocks=1,
                live_uids=[0, 1, 2],
                chain_hotkeys={0: "hk0", 1: "hk1"},
            )
            result, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "refused")
            self.assertIn("SeatOwnerUnknown", result.detail)
            self.assertIsNotNone(last_attempted)
            self.assertEqual(chain.publish_calls, [])
            self.assertEqual(read_state(state_path)["phase"], "refused")

    def test_run_forever_keeps_running_after_this_refusal_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(
                head=25,
                tempo=20,
                settle_blocks=1,
                live_uids=[0, 1, 2],
                chain_hotkeys={0: "hk0", 1: "hk1"},
            )
            # Must not raise: the whole point of this fix is that
            # run_forever survives this, exactly like every other named
            # refusal in PERIOD_REFUSALS.
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=2,
            )
            self.assertEqual(chain.publish_calls, [])


class AMissingLocalCanonicalScoreRefusesThePeriodNotTheWholeLoop(unittest.TestCase):
    """Found by an independent Verifier (issue #212's phase-two fix,
    2026-09-2x): `already_published`'s new `CanonicalScoreNotWired`/
    `CanonicalScoreUnavailable` were `ChainAdapterRefused`/`SettingsError`
    only, not `Refusal` -- `run_period` calls `already_published` (step 2)
    with no `try`/`except` of its own around it, and neither exception
    matched any of `PERIOD_REFUSALS`'s named members, so either would have
    propagated straight out of `run_forever`'s own while loop and killed
    the whole unattended process (that loop's own docstring: it
    deliberately does not catch-all).

    `CanonicalScoreUnavailable` -- day D's own canonical score genuinely
    missing locally -- is the practically reachable one: a fresh or
    rebuilt instance, a deleted local score file, or a day scored by a
    different validator instance are all real, ordinary ways to reach it
    with a fully-wired, working reader, not a hypothetical. Worse than an
    ordinary crash: since `run_forever`'s own restart-seeding reads the
    identical missing file again on every restart, this would very
    plausibly crash-loop on the same period until a human manually
    repaired it. The fix makes both exceptions also `Refusal`
    (`noah.chain.sources`), so `PERIOD_REFUSALS`'s own catch-all already
    handles them -- proven here the identical way
    `AnyChainRefusalIsHandledNotJustTheTwoOriginallyNamed`/
    `AVectorRefusalIsHandledNotFatal` above already prove it for two
    other real `Refusal` subclasses, against the real integration path
    (`tick`/`run_forever`), not just an isolated `already_published` unit
    test.
    """

    @staticmethod
    def _chain_missing_canonical_score():
        from ..chain_adapter import CanonicalScoreUnavailable

        class MissingCanonicalScoreChain(SchedulableChain):
            def already_published(self, height):
                raise CanonicalScoreUnavailable(
                    f"height {height}: day 1 has no local canonical score on this "
                    f"deployment. Simulated for this test."
                )

        return MissingCanonicalScoreChain(head=25, tempo=20, settle_blocks=1)

    def test_a_missing_canonical_score_file_refuses_the_period_not_the_whole_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self._chain_missing_canonical_score()
            result, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result.outcome, "refused")
            self.assertIn("CanonicalScoreUnavailable", result.detail)
            self.assertIsNotNone(last_attempted)
            self.assertEqual(chain.publish_calls, [])
            self.assertEqual(read_state(state_path)["phase"], "refused")

    def test_run_forever_keeps_running_after_a_missing_canonical_score_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self._chain_missing_canonical_score()
            # Must not raise: the whole point of this fix is that
            # run_forever survives this, exactly like every other named
            # refusal in PERIOD_REFUSALS, across more than one tick (the
            # "crash-loop on restart" risk this fix closes -- confirmed
            # here by staying up across repeated attempts at the SAME
            # period, not just surviving once).
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=3,
            )
            self.assertEqual(chain.publish_calls, [])


class TheLockBlocksTransiently(unittest.TestCase):
    def test_a_lock_held_by_someone_else_refuses_without_touching_the_chain_and_retries_next_tick(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)

            other = acquire(lock_path)  # simulate activity_keeper (or a second instance)
            try:
                result, last_attempted = tick(
                    chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
                )
                self.assertEqual(result.outcome, "held_by_other_writer")
                self.assertFalse(result.acted)
                self.assertIsNone(
                    last_attempted, "not counted as attempted: never entered run_period"
                )
                self.assertEqual(chain.publish_calls, [], "nothing was published while locked out")
            finally:
                release(other)

            # Lock now free: the very next tick (not the next period) succeeds.
            result2, last_attempted2 = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertTrue(result2.acted)
            self.assertEqual(len(chain.publish_calls), 1)


class RestartEvidenceIsWrittenBeforeTheRiskyWork(unittest.TestCase):
    """Proves the state file is not merely claimed to be written early --
    it is on disk BEFORE an exception that `tick` never catches propagates.

    `chain.live_uids` is called by `run_period` with no try/except around it
    (see `period.py`), so raising an unnamed exception there stands in for a
    genuine, uncaught bug -- the same shape of failure a real crash is. This
    does not use a subprocess: proving the WRITE ORDER needs no real kill,
    only that the file already has the right content the instant a crash
    happens. `test_loop_restart_kill.py` proves the process-level restart
    behaviour that follows from this ordering.
    """

    def test_the_state_file_says_attempting_the_moment_an_unnamed_exception_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)

            class CrashingChain(SchedulableChain):
                def live_uids(self, height):
                    raise RuntimeError("a genuine bug, not a named refusal")

            chain = CrashingChain(head=25, tempo=20, settle_blocks=1)

            with self.assertRaises(RuntimeError):
                tick(chain, settle_blocks=1, state_path=state_path, lock_path=lock_path)

            saved = read_state(state_path)
            self.assertIsNotNone(saved, "the state file must exist even though tick() crashed")
            self.assertEqual(saved["phase"], "attempting")
            self.assertIn("period", saved)
            self.assertIn("height", saved)


class RunForeverRefusesBadConfiguration(unittest.TestCase):
    def test_a_non_positive_poll_interval_is_refused_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            with self.assertRaises(LoopRefused):
                run_forever(
                    chain,
                    settle_blocks=1,
                    state_path=state_path,
                    lock_path=lock_path,
                    poll_seconds=0,
                    iterations=1,
                )
            self.assertEqual(chain.publish_calls, [])


class SeedingFromDiskOnRestart(unittest.TestCase):
    """`run_forever`'s own startup: a fresh call, given a state file left by
    a previous one, does not immediately re-attempt an already-refused
    period -- purely the throttle described in the module docstring, not the
    thing that prevents double-publishing (that is `already_published`,
    exercised separately in `test_loop_restart_kill.py`)."""

    def test_a_persisted_refusal_seeds_last_attempted_so_the_same_period_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            # head=25, tempo=20, settle_blocks=1 pins height 19, period 0 --
            # matched here deliberately, not guessed, so the seed genuinely
            # names the period the chain would otherwise be asked to attempt.
            self.assertEqual(pin_height(head=25, tempo=20, settle_blocks=1), 19)
            self.assertEqual(period_of(height=19, tempo=20), 0)
            Path(state_path).write_text(
                json.dumps(
                    {
                        "phase": "refused",
                        "period": 0,
                        "height": 19,
                        "last_terminal_height": 19,
                        "reason": "x",
                    }
                )
            )

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(chain.publish_calls, [], "period 0 was already recorded as refused")


class GapBaselineSurvivesACrashLandingInANonterminalPhase(unittest.TestCase):
    """Found by codex adversarial review, round 5 (2026-08-29): the OLD
    seeding logic set `last_attempted = None` whenever the persisted phase
    was nonterminal (attempting, read_failed, lock_busy) -- correct for the
    retry throttle (nothing was actually finished, so nothing should be
    skipped), but it ALSO meant the gap-reporting baseline was lost: a
    restart landing mid-attempt, after the chain had advanced multiple
    periods while the process was down, reported the next successful tick
    as gap-free even though real periods had gone by unrecorded.
    `last_terminal_height` is written into every state (terminal or not,
    always the value from BEFORE the current attempt started) precisely so
    this baseline survives regardless of which phase a crash lands on.
    """

    def test_a_gap_across_a_crash_mid_attempt_is_still_reported_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # A prior run finished period 0 (terminal, height 19), then
            # crashed while ATTEMPTING period 1 (nonterminal, height 39) --
            # the "attempting" write correctly carries
            # last_terminal_height=19 forward, per the fix.
            Path(state_path).write_text(
                json.dumps(
                    {
                        "phase": "attempting",
                        "period": 1,
                        "height": 39,
                        "last_terminal_height": 19,
                        "at": "irrelevant",
                    }
                )
            )
            # While the process was down, the chain advanced past period 1
            # entirely -- head=105 pins period 4.
            chain = SchedulableChain(head=105, tempo=20, settle_blocks=1)

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(len(chain.publish_calls), 1)
            self.assertEqual(chain.publish_calls[0][0], 4)
            saved = read_state(state_path)
            self.assertEqual(saved["gap_before"], 3, "periods 1, 2, and 3 were never attempted")

    def test_without_the_fix_the_same_crash_would_have_reported_no_gap(self):
        # Documents the OLD behaviour precisely, so the fix's own value is
        # not just asserted but shown against the alternative: seeding from
        # the tick's own bare "period" field (pre-fix schema, no
        # last_terminal_height at all) loses the baseline entirely.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            Path(state_path).write_text(
                json.dumps({"phase": "attempting", "period": 1, "height": 39, "at": "irrelevant"})
            )
            chain = SchedulableChain(head=105, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            # last_terminal_height is absent from this OLD-schema file, so
            # read_state's own validation (see AMalformedStateFileDoesNotCrashTheLoop)
            # still accepts it (the field is optional/None-tolerant), and
            # the seed correctly falls back to None -- no gap can be
            # measured from an unknown baseline, which is the honest answer
            # here, not a silent zero.
            saved = read_state(state_path)
            self.assertEqual(saved["gap_before"], 0)


class AMalformedStateFileDoesNotCrashTheLoop(unittest.TestCase):
    """Found by codex adversarial review, round 5 (2026-08-29):
    `read_state` used to return parseable-but-malformed JSON (an array
    instead of an object, or a non-integer period) unchanged, crashing
    `run_forever`'s own startup on `seed.get(...)` or a period comparison --
    turning restart evidence this module treats as non-authoritative into
    something that could stop the unattended loop from starting at all.
    """

    def test_a_json_array_instead_of_an_object_is_treated_as_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            Path(state_path).write_text(json.dumps([]))
            self.assertIsNone(read_state(state_path))

            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )  # must not raise
            self.assertEqual(len(chain.publish_calls), 1)

    def test_a_string_valued_period_is_treated_as_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            Path(state_path).write_text(
                json.dumps({"phase": "done", "last_terminal_height": "not-a-number"})
            )
            self.assertIsNone(read_state(state_path))

            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )  # must not raise
            self.assertEqual(len(chain.publish_calls), 1)

    def test_a_missing_phase_is_treated_as_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            Path(state_path).write_text(json.dumps({"last_terminal_height": 0}))
            self.assertIsNone(read_state(state_path))

    def test_a_valid_state_file_still_round_trips(self):
        # Control: the validation must not reject genuinely well-formed state.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, _lock_path = _paths(tmp)
            Path(state_path).write_text(
                json.dumps({"phase": "done", "period": 3, "last_terminal_height": 59})
            )
            seed = read_state(state_path)
            self.assertIsNotNone(seed)
            self.assertEqual(seed["last_terminal_height"], 59)


class ATempoChangeDoesNotMakeTheLoopSkipValidPeriods(unittest.TestCase):
    """Found by codex adversarial review, round 6 (2026-08-29), with its own
    worked example: height 119 at tempo 20 is period 5; height 199, LATER,
    at tempo 100 (the subnet's own governance changed it), is period 1. A
    period-number comparison would treat period 1 as "already attempted"
    (since 1 <= 5) and skip it -- and every period after it, until the
    period-number arithmetic under the new tempo eventually climbs back
    past 5, which for a large tempo increase could be a long stretch of
    real, unattended time with nothing published. Height is what this loop
    actually compares now (`last_attempted_height`), and height never goes
    backward when tempo changes, only the period number derived from it.
    """

    def test_codexs_own_worked_example_still_advances(self):
        # Reproduced directly: last attempted height 119 (period 5 at the
        # OLD tempo, 20); the chain then settles at height 199 under a NEW
        # tempo, 100, where 199 is period 1 -- a smaller period NUMBER, but
        # a strictly LATER height, and the loop must still act on it.
        self.assertEqual(period_of(height=119, tempo=20), 5)
        self.assertEqual(period_of(height=199, tempo=100), 1)
        # head=299, tempo=100, settle_blocks=1 pins height 199 exactly --
        # verified rather than guessed, so the tick below genuinely lands
        # on codex's own worked height.
        self.assertEqual(pin_height(head=299, tempo=100, settle_blocks=1), 199)

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=299, tempo=100, settle_blocks=1)
            result, last_attempted_height = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=119,
            )
            self.assertTrue(result.acted, "a genuinely later height must not be skipped")
            self.assertEqual(result.outcome, "published")
            self.assertGreater(last_attempted_height, 119)

    def test_a_height_that_genuinely_has_not_advanced_is_still_skipped(self):
        # Control: the fix must not turn the skip-gate into a no-op. The
        # SAME height, asked again, is still "already attempted".
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=199, tempo=100, settle_blocks=1)
            pinned = pin_height(head=199, tempo=100, settle_blocks=1)
            result, last_attempted_height = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=pinned,
            )
            self.assertFalse(result.acted)
            self.assertEqual(result.outcome, "waiting")
            self.assertEqual(last_attempted_height, pinned)


class APinnedHeightGoingBackwardAcrossATempoChangeIsStillAttempted(unittest.TestCase):
    """Found by codex adversarial CHALLENGE (2026-08-29, the round after
    review round 8): round 6's own `<=` comparison of PINNED heights
    (`tick`'s own `pinned`, not the raw `chain.current_block()`) inherited
    the identical class of bug it was written to fix, one level deeper.
    `pin_height`'s own output depends on tempo, not purely on monotonic raw
    height, so it CAN go backward across a tempo change even with the
    IDENTICAL head, not just a later one: verified directly,
    `pin_height(head=31, tempo=10, settle_blocks=1)` is 29, and
    `pin_height(head=31, tempo=20, settle_blocks=1)` is 19. Under the OLD
    `<=` gate, 19 <= 29 would have been treated as "already attempted" --
    silently skipping a genuinely new, never-before-seen period. The fix:
    compare pinned heights for EXACT EQUALITY, which needs no monotonicity
    assumption at all.
    """

    def test_the_exact_counterexample_is_still_attempted_not_skipped(self):
        self.assertEqual(pin_height(head=31, tempo=10, settle_blocks=1), 29)
        self.assertEqual(pin_height(head=31, tempo=20, settle_blocks=1), 19)

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # The SAME head, 31, but tempo has changed to 20 by the time
            # this tick runs -- pin_height now returns 19, LESS than the
            # previously-attempted 29.
            chain = SchedulableChain(head=31, tempo=20, settle_blocks=1)
            result, last_attempted_height = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=29,
            )
            self.assertTrue(
                result.acted, "a backward-drifted but genuinely new height must not be skipped"
            )
            self.assertEqual(result.outcome, "published")
            self.assertEqual(len(chain.publish_calls), 1)
            self.assertEqual(chain.publish_calls[0][2], 19)


class ABackwardHeadUnderConstantTempoWaitsRatherThanScoresAStaleHeight(unittest.TestCase):
    """Found by codex adversarial review, round 13 (2026-08-29): under a
    CONSTANT tempo, `pin_height`'s own output can STILL jump backward
    across a tempo-period boundary from a plain chain-head rollback --
    codex's own verified numbers: `pin_height(head=40, tempo=20,
    settle_blocks=1)` is 39, and `pin_height(head=39, tempo=20,
    settle_blocks=1)` -- one block EARLIER, SAME tempo -- is 19. This is a
    DIFFERENT root cause than the tempo-change tests above (no tempo change
    here at all), so neither `refused_heights` nor the exact-equality
    skip-gate protects it: height 19 was never attempted before. Only
    `last_attempted_tempo` (see `tick`'s own docstring) can tell "the tempo
    genuinely changed, this is a legitimate new period" apart from "the
    tempo did NOT change, so this backward jump can only be a reorg."
    """

    def test_a_one_block_head_rollback_with_no_tempo_change_is_waiting_not_published(self):
        from ..pinning import pin_height as _ph

        self.assertEqual(_ph(head=40, tempo=20, settle_blocks=1), 39)
        self.assertEqual(_ph(head=39, tempo=20, settle_blocks=1), 19)

        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=40, tempo=20, settle_blocks=1)

            result1, last_attempted_height = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result1.outcome, "published")
            self.assertEqual(chain.publish_calls[-1][2], 39)
            last_attempted_tempo = result1.last_attempted_tempo
            self.assertEqual(last_attempted_tempo, 20)

            # The chain head rolls back by one block; tempo is UNCHANGED.
            chain.head = 39
            result2, last_attempted_height = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=last_attempted_height,
                last_attempted_tempo=last_attempted_tempo,
            )
            self.assertEqual(
                result2.outcome,
                "waiting",
                "a backward pin with NO tempo change can only be a reorg -- must not score it",
            )
            self.assertFalse(result2.acted)
            self.assertIn("EARLIER", result2.detail)
            self.assertEqual(
                len(chain.publish_calls), 1, "no second publish -- height 19 was not scored"
            )
            self.assertEqual(last_attempted_height, 39, "the last REAL attempt is unchanged")

    def test_without_a_prior_tempo_the_check_is_disabled_first_attempt_is_never_suspicious(self):
        # last_attempted_tempo defaults to None -- a validator's own FIRST
        # observation of the chain is not a reorg, there is nothing to
        # compare against yet.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=39, tempo=20, settle_blocks=1)
            result, _ = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=999,  # unrelated -- e.g. a stale, higher seed
            )
            self.assertEqual(result.outcome, "published")

    def test_run_forever_persists_and_reseeds_last_attempted_tempo_across_a_restart(self):
        # The full production path: run_forever writes last_attempted_tempo
        # to disk, a fresh run_forever call (standing in for a restart)
        # reads it back, and the reorg-suspicion protection is active
        # immediately -- not only when a test hand-threads it through tick.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=40, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(chain.publish_calls[-1][2], 39)
            self.assertEqual(read_state(state_path)["last_attempted_tempo"], 20)

            # "Restart": a fresh run_forever call, same state file, chain
            # head has rolled back one block, tempo unchanged.
            chain.head = 39
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(
                len(chain.publish_calls),
                1,
                "restart must not score the reorg-resurfaced height 19 either",
            )


class ATempoChangeCannotRepublishAnAlreadyPublishedHeight(unittest.TestCase):
    """Found by codex adversarial review, round 10 (2026-08-29), on
    `AChainUnreadableWrappedInsidePeriodRefusedIsAlsoWaiting`'s sibling
    tempo-reversal test above: that test proves a backward-drifted height is
    ATTEMPTED, not skipped -- but it never actually published anything at
    the higher height first, so it never exercised `already_published`
    against a REAL prior publish. This test does: publish at height 19
    under tempo 10 (period 1), publish again at height 29 under tempo 10
    (period 2), THEN change tempo to 20 so the SAME head pins height 19
    again -- and proves `already_published` correctly refuses a second
    attempt at 19, rather than being fooled into treating it as a
    never-before-seen period because `period_of(19, tempo=20)` is 0, a
    period number nothing had ever recorded under the OLD tempo. Depends on
    `SchedulableChain.already_published` being height-keyed (see that
    fixture's own comment) -- this is the test that would have caught it
    being period-keyed instead.
    """

    def test_republishing_the_same_height_after_a_tempo_change_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=10, settle_blocks=1)  # pins 19, period 1

            result1, last_attempted = tick(
                chain, settle_blocks=1, state_path=state_path, lock_path=lock_path
            )
            self.assertEqual(result1.outcome, "published")
            self.assertEqual(chain.publish_calls[-1][2], 19)

            chain.head = 35  # pins 29, period 2
            result2, last_attempted = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=last_attempted,
            )
            self.assertEqual(result2.outcome, "published")
            self.assertEqual(chain.publish_calls[-1][2], 29)
            self.assertEqual(len(chain.publish_calls), 2)

            # Tempo changes; the SAME head (35) now pins height 19 again --
            # period_of(19, tempo=20) is 0, a period number the chain above
            # never recorded (it recorded periods 1 and 2, under tempo 10).
            self.assertEqual(pin_height(head=35, tempo=10, settle_blocks=1), 29)
            self.assertEqual(pin_height(head=35, tempo=20, settle_blocks=1), 19)
            chain.tempo_ = 20
            result3, _ = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=last_attempted,
            )
            self.assertEqual(
                result3.outcome,
                "already_published",
                "height 19 was already published; a tempo change must not let it "
                "republish under a period number that happens to look unused",
            )
            self.assertEqual(
                len(chain.publish_calls), 2, "no THIRD publish call -- 19 was not republished"
            )


class TheInLockRefreshRereadsTempoNotJustHeight(unittest.TestCase):
    """Found by codex adversarial review, round 15 (2026-08-29): the
    in-lock refresh (`_freshest_period`) used to re-read the HEIGHT
    (`chain.pinned_height()`) but reused the tick's own STALE, top-of-tick
    `tempo` to derive the period number from that fresh height, and every
    downstream `last_attempted_tempo` write inherited that same stale
    value. If the subnet's tempo genuinely changed in the gap between the
    top-of-tick read and this refresh, the WRONG tempo gets memorialized
    alongside the height `run_period` actually acted on -- and a later
    tick's reorg-suspicion check (`tempo == last_attempted_tempo`) then
    silently fails to fire when it should, because the stored tempo no
    longer matches reality. A false NEGATIVE: worse than the false
    positive this file otherwise risks, since it disables protection
    instead of merely delaying one publish.
    """

    class TempoChangesDuringTheLockChain(SchedulableChain):
        """`tempo()` returns the STALE value (10) on its first call (the
        tick's own top-of-tick read) and the FRESH value (20) on every
        call after that (the in-lock refresh, and -- since it also mutates
        `self.tempo_`, the attribute `pinned_height()` itself reads --
        `run_period`'s own internal re-pin too, consistently).
        """

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.tempo_calls = 0

        def tempo(self):
            self.tempo_calls += 1
            if self.tempo_calls == 1:
                return 10
            self.tempo_ = 20
            return 20

    def test_freshest_period_returns_the_freshly_read_tempo_not_the_stale_one(self):
        from ..loop import _freshest_period

        chain = self.TempoChangesDuringTheLockChain(head=45, tempo=10, settle_blocks=1)
        stale_tempo = chain.tempo()  # stands in for tick()'s own top-of-tick read
        self.assertEqual(stale_tempo, 10)

        fresh_tempo, fresh_period, fresh_pinned = _freshest_period(
            chain, fallback_tempo=stale_tempo, fallback_period=999, fallback_pinned=999
        )
        self.assertEqual(fresh_tempo, 20, "must be the FRESH tempo, not the stale one (10)")
        self.assertEqual(fresh_pinned, pin_height(head=45, tempo=20, settle_blocks=1))

    def test_a_terminal_outcome_records_the_tempo_it_was_actually_pinned_under(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self.TempoChangesDuringTheLockChain(head=45, tempo=10, settle_blocks=1)
            result, _ = tick(chain, settle_blocks=1, state_path=state_path, lock_path=lock_path)
            self.assertEqual(result.outcome, "published")
            self.assertEqual(
                result.last_attempted_tempo,
                20,
                "must record the FRESH tempo (20) run_period actually pinned under, "
                "not the tick's own stale top-of-tick read (10)",
            )
            self.assertEqual(read_state(state_path)["last_attempted_tempo"], 20)


class TheGatesAreReCheckedAfterTheInLockRefresh(unittest.TestCase):
    """Found by codex adversarial review, round 14 (2026-08-29): the
    exact-repeat/reorg-suspicion/refused-heights checks used to run ONLY
    against this tick's own PRE-LOCK plan, never again against
    `_freshest_period`'s fresher read taken immediately before `run_period`
    is actually called. If the chain moves in that narrow window -- here,
    simulated directly rather than through a real race -- a height these
    checks would have caught could still reach `run_period`. Proven here
    for `refused_heights` specifically (the sharpest case: it can NEVER be
    caught by `already_published`, since a refused height was never
    published at all).
    """

    class MovesDuringTheLockChain(SchedulableChain):
        """`pinned_height()` -- the call `_freshest_period` makes -- mutates
        `self.head` on its FIRST invocation, standing in for the chain
        genuinely advancing between this tick's own top-of-tick read
        (`current_block()`/`tempo()`, used to compute the PRE-lock plan)
        and this second, in-lock read. Every later call (including
        `run_period`'s own internal one, step 1, if this fix did NOT stop
        it first) sees the already-moved head consistently.
        """

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._pinned_height_calls = 0

        def pinned_height(self):
            self._pinned_height_calls += 1
            if self._pinned_height_calls == 1:
                self.head = 30  # was 51 -- pins 29 now, was 49
            return super().pinned_height()

    def test_a_height_that_becomes_refused_only_after_the_in_lock_refresh_is_still_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            self.assertEqual(pin_height(head=51, tempo=10, settle_blocks=1), 49)
            self.assertEqual(pin_height(head=30, tempo=10, settle_blocks=1), 29)

            chain = self.MovesDuringTheLockChain(head=51, tempo=10, settle_blocks=1)
            # Height 29 was already refused earlier THIS run -- the
            # pre-lock plan (height 49) knows nothing wrong with it; only
            # the in-lock refresh (height 29, after the simulated chain
            # move) can catch this.
            refused_heights = {29}

            result, last_attempted_height = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                refused_heights=refused_heights,
            )
            self.assertEqual(
                result.outcome,
                "waiting",
                "the in-lock refresh landed on a refused height; run_period must not run",
            )
            self.assertFalse(result.acted)
            self.assertEqual(chain.publish_calls, [], "run_period was never reached")
            self.assertEqual(
                chain._pinned_height_calls,
                1,
                "run_period's own internal pinned_height() call never happened either -- "
                "the gate stopped this BEFORE run_period, not after",
            )


class ATempoChangeDoesNotSilentlyRetryARefusedHeight(unittest.TestCase):
    """Found by codex adversarial review, round 10 (2026-08-29): the
    skip-gate's exact-equality comparison only ever remembers the SINGLE
    MOST RECENT terminal height. If height 19 is refused, then height 29 is
    later ALSO refused, `last_attempted_height` becomes 29 and 19 is no
    longer remembered anywhere -- so a subsequent tempo change that makes
    `pin_height` return 19 again would fall through and re-attempt it,
    silently retrying a refusal `run_period`'s own docstring says
    explicitly must be an OPERATOR's decision, not this loop's own.
    `refused_heights` (see `tick`'s own docstring) closes this: proven here
    in the sharpest case codex named -- the underlying failure has
    genuinely CLEARED by the time the tempo change resurfaces the height,
    so a naive retry would actually succeed and publish, which is exactly
    why it must not happen automatically.
    """

    class FlakyForSomeHeightsChain(SchedulableChain):
        def score(self, observations):
            if observations["height"] in self.w.get("fail_at_heights", set()):
                raise RuntimeError(f"a real scoring bug at height {observations['height']}")
            return dict(self.w.get("scores", {0: 100, 1: 0}))

    def test_a_refused_height_is_not_retried_even_once_the_underlying_failure_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self.FlakyForSomeHeightsChain(
                head=25, tempo=10, settle_blocks=1, fail_at_heights={19, 29}
            )
            refused_heights = set()

            result1, last_attempted = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                refused_heights=refused_heights,
            )
            self.assertEqual(result1.outcome, "refused")
            self.assertEqual(refused_heights, {19})

            chain.head = 35  # pins 29 under tempo 10
            result2, last_attempted = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=last_attempted,
                refused_heights=refused_heights,
            )
            self.assertEqual(result2.outcome, "refused")
            self.assertEqual(refused_heights, {19, 29})

            # The underlying failure clears -- height 19 would now score and
            # publish successfully if it were actually attempted again.
            chain.w["fail_at_heights"] = set()
            self.assertEqual(pin_height(head=35, tempo=10, settle_blocks=1), 29)
            self.assertEqual(pin_height(head=35, tempo=20, settle_blocks=1), 19)
            chain.tempo_ = 20  # the SAME head (35) now pins 19 again

            result3, _ = tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                last_attempted_height=last_attempted,
                refused_heights=refused_heights,
            )
            self.assertEqual(
                result3.outcome,
                "waiting",
                "height 19 was already refused; a tempo change and a cleared failure "
                "must not silently retry it -- that is the operator's decision",
            )
            self.assertFalse(result3.acted)
            self.assertIn("already refused earlier this run", result3.detail)
            self.assertEqual(chain.publish_calls, [], "no publish -- the refusal was honoured")

    def test_run_forever_threads_the_same_protection_across_a_continuous_run(self):
        # The same scenario, but driven through run_forever itself (as
        # production actually calls this), proving the set really is
        # threaded through every tick, not just when a test wires it up by
        # hand above.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self.FlakyForSomeHeightsChain(
                head=25, tempo=10, settle_blocks=1, fail_at_heights={19, 29}
            )

            heads = iter([35, 35])  # tick 2 -> pins 29; tick 3 (tempo now 20) -> pins 19 again

            def fake_sleep(_seconds):
                if chain.tempo_ == 10 and chain.head == 25:
                    chain.head = next(heads)
                else:
                    chain.tempo_ = 20
                    chain.w["fail_at_heights"] = set()

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=3,
            )
            self.assertEqual(
                chain.publish_calls, [], "the refused height must not publish, even via run_forever"
            )
            self.assertEqual(read_state(state_path)["refused_heights"], [19, 29])


class ALegacyRefusedStateFileIsMigratedIntoRefusedHeights(unittest.TestCase):
    """Found by codex adversarial review, round 12 (2026-08-29): a state
    file written by an EARLIER version of this module -- before
    `refused_heights` existed, or upgraded mid-deployment from one that
    predates it -- has `phase == "refused"` and `last_terminal_height` but
    no `"refused_heights"` key at all. Seeding straight from a missing key
    (`seed.get("refused_heights", [])`) would silently start with an EMPTY
    set even though the file's own `phase`/`last_terminal_height` already
    say plainly that height was refused -- reopening exactly the hole
    `refused_heights` exists to close, for no reason other than an upgrade.
    `run_forever`'s startup migration folds that legacy refusal in.
    """

    def test_a_pre_round_10_refused_state_file_still_blocks_a_tempo_revisit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # Hand-written, exactly as an older version of this module would
            # have left it: no "refused_heights" key at all.
            Path(state_path).write_text(
                json.dumps(
                    {
                        "phase": "refused",
                        "period": 1,
                        "height": 19,
                        "last_terminal_height": 19,
                        "reason": "PeriodRefused: a real scoring bug at height 19",
                        "at": "2026-08-29T00:00:00Z",
                    }
                )
            )
            chain = ATempoChangeDoesNotSilentlyRetryARefusedHeight.FlakyForSomeHeightsChain(
                head=35, tempo=10, settle_blocks=1, fail_at_heights={29}
            )

            def fake_sleep(_seconds):
                chain.tempo_ = 20  # SAME head (35): pins 29 under tempo 10, 19 under tempo 20
                chain.w["fail_at_heights"] = set()  # the failure has cleared

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=2,
            )
            self.assertEqual(
                chain.publish_calls,
                [],
                "height 19 was refused by a PRE-round-10 file with no refused_heights key; "
                "the migration must still block a tempo-driven revisit of it",
            )
            self.assertEqual(
                read_state(state_path)["refused_heights"],
                [19, 29],
                "the legacy refusal (19) and the new one from this run (29) are both present",
            )

    def test_a_legacy_NONTERMINAL_snapshot_still_carries_an_earlier_refusal_forward(self):
        # Sharpened by codex adversarial review, round 14 (2026-08-29): the
        # LAST write before an upgrade is not always the refusal itself --
        # `last_terminal_height` is carried forward through nonterminal
        # writes too (round 5's own fix, which predates refused_heights),
        # so a legacy file can be caught mid-"attempting" for a LATER
        # period while still naming an EARLIER refusal. The migration must
        # not require phase == "refused" to catch this.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # Height 19 was refused; the loop then started (but never
            # finished, before an upgrade+restart) attempting height 29.
            # No "refused_heights" key -- exactly a pre-round-10 file.
            Path(state_path).write_text(
                json.dumps(
                    {
                        "phase": "attempting",
                        "period": 2,
                        "height": 29,
                        "last_terminal_height": 19,
                        "at": "2026-08-29T00:00:00Z",
                    }
                )
            )
            chain = ATempoChangeDoesNotSilentlyRetryARefusedHeight.FlakyForSomeHeightsChain(
                head=35, tempo=10, settle_blocks=1, fail_at_heights={29}
            )

            def fake_sleep(_seconds):
                chain.tempo_ = 20  # SAME head (35): pins 29 under tempo 10, 19 under tempo 20
                chain.w["fail_at_heights"] = set()  # the failure has cleared

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=2,
            )
            self.assertEqual(
                chain.publish_calls,
                [],
                "height 19 was refused earlier, even though the legacy file's LAST "
                "recorded phase was a nonterminal 'attempting' for a different height",
            )


class AMismatchedIdentityDiscardsTheStaleSeed(unittest.TestCase):
    """Found by codex adversarial review, round 7 (2026-08-29): reusing
    `state_path` across a local-chain rebuild ("gets a new identity every
    rebuild", per this project's own established knowledge -- see
    settings.py's local_genesis), a network switch, or a changed signing
    hotkey leaves `last_terminal_height` looking like an ordinary integer
    while meaning something else entirely. Seeding from it blindly could
    make this loop believe every genuinely new period on the new chain is
    "already attempted" and stall. `identity` lets a caller that DOES have
    one say so, and a mismatch is discarded rather than trusted.
    """

    def test_a_different_identity_discards_the_seed_and_the_period_is_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # A stale seed from a PREVIOUS chain/hotkey, at a height that
            # would (wrongly) block every period on the new one if trusted.
            Path(state_path).write_text(
                json.dumps(
                    {
                        "phase": "done",
                        "period": 999,
                        "last_terminal_height": 999_999,
                        "identity": "chain-A:hotkey-1",
                    }
                )
            )
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                identity="chain-B:hotkey-1",
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(
                len(chain.publish_calls), 1, "the new identity's period was not skipped"
            )

    def test_a_matching_identity_still_seeds_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            Path(state_path).write_text(
                json.dumps(
                    {
                        "phase": "done",
                        "period": 0,
                        "last_terminal_height": 19,
                        "identity": "chain-A:hotkey-1",
                    }
                )
            )
            # Same identity, next period ready (head=45 -> period 1): must
            # publish period 1, not re-publish period 0.
            chain = SchedulableChain(head=45, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                identity="chain-A:hotkey-1",
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(len(chain.publish_calls), 1)
            self.assertEqual(chain.publish_calls[0][0], 1)

    def test_no_identity_supplied_skips_the_check_entirely(self):
        # Backward compatible: a caller with nothing meaningful to supply
        # (every existing test, and any stand-in chain) sees no behaviour
        # change at all.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # last_terminal_height=19 is EXACTLY what head=25/tempo=20/
            # settle_blocks=1 pins (matched deliberately, not guessed, per
            # the skip-gate's own equality comparison -- see loop.py's
            # own comment on why it compares pinned heights for EXACT
            # equality, not ordering, after codex adversarial challenge
            # 2026-08-29). A coincidentally-matching stale height from an
            # unchecked, un-identified seed still throttles, which is
            # exactly the case identity-checking exists to catch.
            Path(state_path).write_text(
                json.dumps(
                    {
                        "phase": "done",
                        "period": 0,
                        "last_terminal_height": 19,
                        "identity": "chain-A:hotkey-1",
                    }
                )
            )
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(
                chain.publish_calls, [], "the stale, un-identity-checked seed still throttles"
            )

    def test_an_unlabeled_seed_is_also_discarded_once_this_run_has_a_real_identity(self):
        # Found by codex adversarial review, round 8 (2026-08-29): the round
        # 7 version only discarded a seed that was LABELED with a different
        # identity -- an UNLABELED seed (written before identity-tracking
        # existed, or by a caller that has not started supplying one) was
        # trusted unconditionally, even though it is exactly as unverifiable
        # as a labeled-but-different one once THIS run actually has an
        # identity to check against.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            Path(state_path).write_text(
                json.dumps({"phase": "done", "period": 999, "last_terminal_height": 999_999})
            )
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                identity="chain-B:hotkey-1",
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(
                len(chain.publish_calls), 1, "the unlabeled stale seed was not trusted"
            )

    def test_identity_is_persisted_on_every_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
            tick(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                identity="chain-A:hotkey-1",
            )
            self.assertEqual(read_state(state_path)["identity"], "chain-A:hotkey-1")


class StateWritesAreFsyncedAndThreadQualified(unittest.TestCase):
    """Found by codex adversarial CHALLENGE (2026-08-29, after review round
    8): `_write_state`'s tmp filename used only `os.getpid()`, and the write
    itself was a plain `write_text` with no fsync -- so (a) two threads in
    the same process racing on `_write_state` would collide on the same tmp
    path, and (b) a crash right after an apparently-successful state write
    could still lose the content to the OS page cache, defeating the whole
    point of this being the restart-readable evidence the loop relies on.
    """

    def test_the_tmp_path_is_qualified_by_both_pid_and_thread_id(self):
        # `_write_state` is a private module function; imported directly
        # here because this test is specifically about ITS OWN mechanism
        # (the tmp filename it chooses), not about anything `tick`/
        # `run_forever` expose through their own public behavior.
        from .. import loop as loop_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            seen_tmp_names = []
            real_replace = loop_module.os.replace

            def _spying_replace(src, dst):
                seen_tmp_names.append(str(src))
                return real_replace(src, dst)

            with unittest.mock.patch.object(loop_module.os, "replace", _spying_replace):
                loop_module._write_state(path, {"phase": "waiting"})

            self.assertEqual(len(seen_tmp_names), 1)
            tmp_name = seen_tmp_names[0]
            self.assertIn(f".tmp{loop_module.os.getpid()}", tmp_name)
            self.assertIn(f".{loop_module.threading.get_ident()}", tmp_name)

    def test_two_threads_writing_the_same_state_path_do_not_collide_on_the_tmp_name(self):
        # The real, reachable proof for the fix above: two threads calling
        # `_write_state` on the SAME path at (as close to) the same time no
        # longer race on an identical tmp filename. Before the fix, this
        # reliably produced a `FileNotFoundError` inside `os.replace` (one
        # thread's `os.replace` removing/renaming the tmp file the other
        # thread's `os.replace` was about to use) or a corrupted read. After
        # the fix, each thread has never collided across 50 real repetitions.
        from .. import loop as loop_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            errors = []

            def _writer(n):
                try:
                    for _ in range(50):
                        loop_module._write_state(path, {"phase": "waiting", "n": n})
                except Exception as error:  # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(errors, [], f"a race produced: {errors!r}")
            # The file must still be present and parse as valid JSON -- proof
            # that no writer ever observed a half-written or missing file.
            self.assertIsNotNone(read_state(path))

    def test_the_write_is_fsynced_before_and_after_the_replace(self):
        from .. import loop as loop_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            fsync_calls = []
            real_fsync = loop_module.os.fsync

            def _spying_fsync(fd):
                fsync_calls.append(fd)
                return real_fsync(fd)

            with unittest.mock.patch.object(loop_module.os, "fsync", _spying_fsync):
                loop_module._write_state(path, {"phase": "waiting"})

            # One fsync for the tmp file's own content, one for the
            # containing directory's entry -- both, not just the file's.
            self.assertEqual(
                len(fsync_calls),
                2,
                "expected exactly one file-content fsync and one directory fsync",
            )


class DayZeroCatchUpTests(unittest.TestCase):
    """The real, confirmed gap (ticket #16): `pinning.pin_height` can never
    select a height inside a Day 0 shorter than one tempo period. Proven
    directly against `run_forever`'s own unmodified entry point -- the
    identical call shape a real unattended validator service already uses,
    no manual height, no helper script -- and that a restart afterward is a
    safe no-op.
    """

    def test_no_head_ever_makes_plain_pin_height_reach_day_zero(self):
        # The diagnosis itself, against the real, UNTOUCHED `pinning.
        # pin_height`: sweep many heads from the moment Day 0 ends (block
        # 359) far past it, and confirm none of them ever produces a
        # pinned height inside Day 0 (<= 358). `pin_height` itself is not
        # changed by this fix -- this is the regression guard for the
        # PROBLEM the fix addresses, so a future change to `pinning.py`
        # that silently stopped this being true would be caught here.
        from ..pinning import NoSettledPeriod

        swept_at_least_one = False
        for head in range(DAY_ZERO_LAST_BLOCK + 1, DAY_ZERO_LAST_BLOCK + 1 + 5000, 37):
            try:
                pinned = pin_height(head=head, tempo=DAY_ZERO_TEMPO, settle_blocks=1)
            except NoSettledPeriod:
                # Not settled yet at this head -- not the case being swept
                # here (nothing has ever been produced at all yet).
                continue
            swept_at_least_one = True
            self.assertGreater(
                pinned, DAY_ZERO_LAST_BLOCK, f"head={head} produced a height inside Day 0"
            )
        self.assertTrue(swept_at_least_one, "the sweep never reached a settled height at all")

    def test_same_period_different_height_is_not_republished_within_one_continuous_run(self):
        # The exact defect an independent Verifier found (2026-09-21)
        # against these same fixtures: Day 0 catch-up publishes period 0 at
        # height 358; with the head held fixed, the VERY NEXT tick of the
        # SAME continuous `run_forever` call -- no restart -- would have
        # `pin_height` alone name height 359 instead. period_of(358, 360)
        # == period_of(359, 360) == 0: the SAME period, reached through a
        # different literal height. Proven here by inspecting each tick's
        # own outcome (not just the final publish count), so this actually
        # demonstrates `_terminal_gate`'s own new same-period check catches
        # it at the cheap, in-process gate -- before `run_period`, and
        # therefore before any real signing attempt -- rather than relying
        # entirely on `already_published`'s chain read to refuse it one
        # step later.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = DayZeroAwareChain(head=400, settle_blocks=1)
            outcomes = []

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _seconds: None,
                iterations=4,
                on_tick=lambda result: outcomes.append(result.outcome),
            )

            self.assertEqual(outcomes[0], "published", "the first tick should be Day 0 itself")
            self.assertEqual(
                outcomes[1:],
                ["waiting", "waiting", "waiting"],
                "period 0's own ordinary tempo-pinned height (359) must be recognized as the "
                "same period already attempted at height 358, not scored again",
            )
            self.assertEqual(
                len(chain.publish_calls),
                1,
                "period 0 was published more than once -- once at height 358, and again once "
                "the ordinary tempo-pinned height (359, the same period) became reachable",
            )
            self.assertEqual(chain.publish_calls[0][2], DAY_ZERO_LAST_BLOCK)

    def test_run_forever_discovers_scores_and_publishes_day_zero_on_its_own(self):
        # The chain head is already well past Day 0 when this starts --
        # "tonight's reset happened late, Day 0 already closed before this
        # service ever started" -- and nothing here names Day 0 or its
        # height anywhere: this calls `run_forever` exactly the way the
        # real unattended validator service does, with no human involved.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = DayZeroAwareChain(head=400, settle_blocks=1)

            last = run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _seconds: None,
                iterations=1,
            )

            self.assertEqual(
                last, DAY_ZERO_LAST_BLOCK, "the loop's own first attempt should be Day 0 itself"
            )
            self.assertEqual(len(chain.publish_calls), 1)
            self.assertEqual(
                chain.publish_calls[0][2],
                DAY_ZERO_LAST_BLOCK,
                "the actual publish call was not for Day 0's own true last block",
            )
            # `already_published` -- unchanged, chain-authoritative -- now
            # confirms it too, the same fact a real validator restarting
            # would check.
            self.assertTrue(chain.already_published(DAY_ZERO_LAST_BLOCK))

            state = read_state(state_path)
            self.assertEqual(state["phase"], "done")
            self.assertEqual(state["height"], DAY_ZERO_LAST_BLOCK)
            self.assertEqual(state["period"], 0)

    def test_restart_after_day_zero_publishes_is_a_safe_no_op(self):
        # Two SEPARATE `run_forever` calls against the same state file --
        # the real restart shape: a fresh process, a fresh in-memory
        # `day_zero_cache` (never persisted -- see `..loop`'s own top
        # docstring), seeded ONLY from what `read_state` finds on disk,
        # exactly what a restarted systemd unit does. What actually
        # prevents the republish is asserted directly: `already_published`
        # (chain-authoritative, unmodified) is what both this loop's own
        # pre-check and `run_period`'s own settled step 2 rely on, not
        # anything either run remembers locally.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = DayZeroAwareChain(head=400, settle_blocks=1)

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _seconds: None,
                iterations=1,
            )
            self.assertEqual(chain.publish_calls[0][2], DAY_ZERO_LAST_BLOCK)
            self.assertEqual(len(chain.publish_calls), 1)

            # Simulate a restart: a little real time passed (the head moved
            # a few blocks), and a brand-new `run_forever` call runs
            # against the SAME chain (so `already_published` is the real,
            # chain-authoritative record a restarted process would also
            # see) and the SAME state file, but nothing carried over
            # in-process.
            chain.head += 5
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _seconds: None,
                iterations=3,
            )

            # UNFILTERED -- an earlier version of this assertion filtered
            # for height == DAY_ZERO_LAST_BLOCK specifically, which would
            # have silently missed a second, unwanted publish call landing
            # at a DIFFERENT height in the SAME period (359, the ordinary
            # tempo-pinned height once Day 0 closes -- see
            # `test_same_period_different_height_is_not_republished_within_one_continuous_run`
            # above) after this restart's own seeded state names 358.
            # Found by an independent Verifier (2026-09-21) against this
            # exact test.
            self.assertEqual(
                len(chain.publish_calls),
                1,
                "period 0 was published more than once across the restart -- either Day 0 "
                "itself was republished, or the same period was republished at a different "
                "height (359) once the ordinary tempo pin reached it",
            )
            self.assertEqual(chain.publish_calls[0][2], DAY_ZERO_LAST_BLOCK)


class TheSamePeriodCheckDoesNotShadowTheReorgSuspicionCheck(unittest.TestCase):
    """An independent Verifier's own second pass (2026-09-21): the new
    same-period-different-height check above sits BEFORE the existing
    reorg-suspicion check inside `_terminal_gate`, so it must not intercept
    a genuine same-tempo, IN-PERIOD backward reorg and hide the specific
    "EARLIER" warning behind its own generic message. Both outcomes are
    `acted=False`/"waiting" either way -- no safety issue -- but
    `TickResult.detail` exposes the result to callers, so the specific diagnostic
    matters. Drives `_terminal_gate` directly with hand-picked heights
    (rather than trying to reproduce a real reorg through the day-zero
    fixtures) -- the smallest, most direct proof of the gate's own
    ordering, independent of whether a real chain could physically produce
    this exact sequence.
    """

    def test_an_in_period_backward_move_gets_the_specific_reorg_message(self):
        from ..loop import _terminal_gate

        tempo = 360
        last_attempted_height = 358  # e.g. Day 0 catch-up's own target
        pinned = 350  # a SMALLER height, but the SAME period (period 0)
        self.assertEqual(period_of(height=pinned, tempo=tempo), 0)
        self.assertEqual(period_of(height=last_attempted_height, tempo=tempo), 0)

        result = _terminal_gate(
            pinned,
            0,
            last_attempted_height=last_attempted_height,
            last_attempted_tempo=tempo,
            tempo=tempo,
            refused_heights=set(),
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.acted)
        self.assertEqual(result.outcome, "waiting")
        self.assertIn(
            "EARLIER",
            result.detail,
            "an in-period backward move must still get the specific reorg-suspicion "
            "message, not the new check's generic same-period one",
        )

    def test_a_forward_same_period_move_still_gets_the_day_zero_message(self):
        # The case the new check was actually written for -- Day 0
        # catch-up (358), then later the ordinary tempo-pinned height in
        # the same period (359) -- still caught, and still gets ITS OWN
        # message, now that the reorg check has a forward-direction guard
        # sitting right next to it.
        from ..loop import _terminal_gate

        tempo = 360
        result = _terminal_gate(
            359,
            0,
            last_attempted_height=358,
            last_attempted_tempo=tempo,
            tempo=tempo,
            refused_heights=set(),
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.acted)
        self.assertEqual(result.outcome, "waiting")
        self.assertIn("same period", result.detail)
        self.assertNotIn("EARLIER", result.detail)


class DayZeroDoesNotAffectAnOrdinaryDayTests(unittest.TestCase):
    """Hard constraint: a NORMAL (full-length) Day 0 -- long enough that
    ordinary `pin_height` tempo pinning already reaches inside it -- must
    pin at exactly the same height as before this fix, whether or not the
    chain exposes day-boundary awareness at all.
    """

    def test_identical_outcome_with_and_without_day_boundary_awareness(self):
        # Day 0 spans a full 86,400 fake-second day at 1 fake second per
        # block (blocks 0..86399; block 86400 is Day 1's own first block) --
        # far longer than one tempo period (360 blocks), the ordinary case
        # every existing chain in this codebase is in. `head` is placed
        # deep into Day 1, well past Day 0's own end, the exact place the
        # short-day fix's own catch-up check is asked at all.
        tempo = 360
        settle_blocks = 1
        head = 90_000

        plain_chain = SchedulableChain(head=head, tempo=tempo, settle_blocks=settle_blocks)

        class _FullDaySource:
            def __init__(self, head_getter):
                self._head_getter = head_getter

            def head_block(self):
                return self._head_getter()

            def block_timestamp(self, block):
                return block  # 1 fake second per block, anchor 0

            def canary(self, block):
                return 256

        day_aware_chain = SchedulableChain(head=head, tempo=tempo, settle_blocks=settle_blocks)
        day_aware_chain.day_anchor_ts = 0
        a, b = _FullDaySource(lambda: day_aware_chain.head), _FullDaySource(
            lambda: day_aware_chain.head
        )
        day_aware_chain.runner_chain = AgreeingChain(
            sources={"archive": a, "latent": b},
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )

        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            plain_state, plain_lock = _paths(tmp1)
            aware_state, aware_lock = _paths(tmp2)

            plain_result, plain_last = tick(
                plain_chain,
                settle_blocks=settle_blocks,
                state_path=plain_state,
                lock_path=plain_lock,
            )
            aware_result, aware_last = tick(
                day_aware_chain,
                settle_blocks=settle_blocks,
                state_path=aware_state,
                lock_path=aware_lock,
            )

        self.assertEqual(plain_last, aware_last)
        self.assertEqual(plain_result.outcome, aware_result.outcome)
        self.assertEqual(plain_result.period, aware_result.period)
        self.assertEqual(
            plain_chain.publish_calls,
            [(p, v, h) for p, v, h in day_aware_chain.publish_calls],
        )
        # And neither one is Day 0's own true last block (86399) taken as a
        # forced early catch-up target -- both pinned the same ordinary
        # tempo-period boundary `pin_height` alone would have named.
        expected = pin_height(head=head, tempo=tempo, settle_blocks=settle_blocks)
        self.assertEqual(plain_last, expected)
        self.assertEqual(aware_last, expected)


class DayZeroCatchUpRefusalRetriesTests(unittest.TestCase):
    """The real, live gap found the first night Day 0 catch-up shipped:
    `SeatOwnerUnknown` -- a real, honest, TRANSIENT refusal (a 200-miner
    registration wave still settling seat ownership concurrently) -- refused
    Day 0's own catch-up attempt at height 358, and a restart reproduced
    the IDENTICAL, permanently-stuck outcome (`refused_heights: [358]`
    surviving the restart), because the general `refused_heights`/
    `last_terminal_height` machinery remembers a refusal the same way for
    every height -- correct for an ORDINARY period (a later one always
    supersedes it), wrong for Day 0's own fixed, singular target (nothing
    ever supersedes it). See `..loop`'s own top docstring, "Day 0 catch-up's
    own refusal is deliberately NOT remembered this way", for the fix this
    proves.
    """

    class FlakyDayZeroChain(DayZeroAwareChain):
        """Refuses scoring with a real `SeatOwnerUnknown` -- tonight's own
        real refusal, not a stand-in `RuntimeError` -- either always
        (``w["fail_always"]``, standing in for an ORDINARY period's genuine,
        non-transient refusal) or only at Day 0's own catch-up height while
        ``w["seat_owner_unresolved"]`` is true (standing in for tonight's
        own real, transient case: the registration wave has not settled
        yet, but will).
        """

        def score(self, observations):
            height = observations["height"]
            if self.w.get("fail_always"):
                raise SeatOwnerUnknown(
                    f"seat 42 is registered on the subnet and its current owner was not "
                    f"read (height {height})"
                )
            if height == DAY_ZERO_LAST_BLOCK and self.w.get("seat_owner_unresolved", False):
                raise SeatOwnerUnknown(
                    "seat 160 is registered on the subnet and its current owner was not read"
                )
            return dict(self.w.get("scores", {0: 100, 1: 0}))

    def test_a_transient_day_zero_refusal_retries_next_tick_and_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self.FlakyDayZeroChain(head=400, settle_blocks=1, seat_owner_unresolved=True)

            def fake_sleep(_seconds):
                # The registration wave settles between the first and
                # second tick -- the underlying condition genuinely
                # clearing, the identical live shape tonight's own
                # `SeatOwnerUnknown` was.
                chain.w["seat_owner_unresolved"] = False

            outcomes = []
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=2,
                on_tick=lambda r: outcomes.append(r),
            )

            self.assertEqual(outcomes[0].outcome, "day_zero_catch_up_refused")
            self.assertIn("SeatOwnerUnknown", outcomes[0].detail)
            self.assertEqual(
                outcomes[1].outcome,
                "published",
                "Day 0 catch-up must retry on the very next tick and succeed once the "
                "underlying (transient) refusal reason has cleared",
            )
            self.assertEqual(len(chain.publish_calls), 1)
            self.assertEqual(chain.publish_calls[0][2], DAY_ZERO_LAST_BLOCK)

            state = read_state(state_path)
            self.assertEqual(state["phase"], "done")
            self.assertNotIn(DAY_ZERO_LAST_BLOCK, state.get("refused_heights", []))

    def test_a_transient_day_zero_refusal_also_retries_across_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self.FlakyDayZeroChain(head=400, settle_blocks=1, seat_owner_unresolved=True)

            # First process: refuses once (the live SeatOwnerUnknown), then
            # the process itself ends -- nothing carried over in-process,
            # exactly like tonight's own live restart.
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            state_after_first_process = read_state(state_path)
            self.assertEqual(state_after_first_process["phase"], "day_zero_catch_up_refused")
            self.assertNotIn(
                DAY_ZERO_LAST_BLOCK, state_after_first_process.get("refused_heights", [])
            )
            self.assertIsNone(state_after_first_process.get("last_terminal_height"))
            self.assertEqual(chain.publish_calls, [])

            # The underlying condition clears while the process is down --
            # the real live shape: the registration wave finishes settling
            # before anything restarts this service.
            chain.w["seat_owner_unresolved"] = False

            # Second, brand-new process: a fresh `run_forever` call against
            # the SAME state file and the SAME chain, nothing carried over
            # in-process -- the real restart shape `test_loop_restart_kill.py`
            # itself proves for the general case.
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )

            self.assertEqual(
                len(chain.publish_calls),
                1,
                "the restarted, unattended process must discover and publish Day 0 on its "
                "own, with no human step, once the transient refusal reason has cleared",
            )
            self.assertEqual(chain.publish_calls[0][2], DAY_ZERO_LAST_BLOCK)
            self.assertEqual(read_state(state_path)["phase"], "done")

    def test_an_ordinary_periods_refusal_still_behaves_exactly_as_before(self):
        # Hard constraint: this fix must not weaken `refused_heights`'s own
        # protection for an ORDINARY period. Reuses the identical
        # day-boundary-aware chain and the identical real `SeatOwnerUnknown`
        # reason as the Day 0 tests above, but for an ORDINARY period
        # reached once Day 0 has already resolved -- proving the new
        # day-zero-only branch (`pinned == day_zero_target`) never fires
        # for a period that merely happens to also be refused, and that
        # `refused_heights`/exact-repeat/restart-seeding all still behave
        # exactly as round 10 (`ATempoChangeDoesNotSilentlyRetryARefusedHeight`,
        # unmodified elsewhere in this file) already proves for the general
        # case.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = self.FlakyDayZeroChain(head=400, settle_blocks=1)

            # Day 0 publishes cleanly first (no refusal at all here).
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            self.assertEqual(chain.publish_calls[0][2], DAY_ZERO_LAST_BLOCK)

            # Now move well past Day 0 (already resolved, so
            # `_day_zero_catch_up_height` is permanently `None` from here
            # on) into an ORDINARY period, and make THAT period refuse for
            # a reason that never clears within this run.
            chain.head = 2000
            chain.w["fail_always"] = True

            outcomes = []
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=3,
                on_tick=lambda r: outcomes.append(r),
            )

            self.assertEqual(
                outcomes[0].outcome, "refused", "an ordinary refusal keeps its own, unchanged phase"
            )
            for later in outcomes[1:]:
                self.assertEqual(
                    later.outcome,
                    "waiting",
                    "an ordinary period's refusal must still be honoured -- not retried -- "
                    "exactly as before this fix",
                )
            self.assertEqual(
                len(chain.publish_calls), 1, "the refused ordinary period must never publish"
            )
            state = read_state(state_path)
            self.assertEqual(state["phase"], "refused")
            self.assertIn(state["height"], state["refused_heights"])

            # And the SAME protection survives a restart, exactly as round
            # 10 already established for the general case.
            fresh_outcome_holder = []
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
                on_tick=lambda r: fresh_outcome_holder.append(r),
            )
            self.assertEqual(
                fresh_outcome_holder[0].outcome,
                "waiting",
                "a restart must not silently retry an ordinary period's already-refused height",
            )
            self.assertEqual(len(chain.publish_calls), 1)


class _AlwaysFailsAtHeight(DayAwareChain):
    """``DayAwareChain`` plus a real, non-transient scoring refusal at one
    specific height -- standing in for a day catch-up retry that genuinely
    cannot succeed (as opposed to `DayNotComplete`, which resolves on its
    own once the day closes).
    """

    def __init__(self, *, fail_height, **world):
        super().__init__(**world)
        self._fail_height = fail_height

    def score(self, observations):
        if observations["height"] == self._fail_height:
            raise SeatOwnerUnknown(
                f"seat 42 is registered on the subnet and its current owner was not read "
                f"(height {observations['height']})"
            )
        return dict(self.w.get("scores", {0: 100, 1: 0}))


class DayCatchUpTests(unittest.TestCase):
    """Issue #244: `pin_height` only ever names the LATEST settled tempo
    period, with no memory of what it named a moment ago. A day's own
    last-chance period boundary can be attempted while that day genuinely
    has not ended yet -- refused `DayNotComplete`, correctly -- and the
    very next period `pin_height` ever names can already belong to the
    NEXT day, so the day that just closed never gets a second look, with
    no gap in the PERIOD sequence for `_gap_note`'s own GAP warning to
    report. See `..loop`'s own top docstring, "Day catch-up: any day's own
    last chance, not only Day 0's", for the fix these tests prove -- proven
    directly against `run_forever`'s own unmodified entry point, the
    identical call shape a real unattended validator service uses.
    """

    def test_day_one_is_an_ordinary_day_not_a_day_zero_edge_case(self):
        # Guards this whole test class's own premise: Day 1 here must be
        # nothing like Day 0's short-day edge case, or these tests would
        # actually be re-proving Day 0 catch-up's own fix, not issue #244's
        # genuinely different one. `_day_zero_catch_up_height`'s own length
        # check (`to_block < tempo - 1`) exists precisely to keep the two
        # separate -- confirmed here against the real `day_block_range`.
        day, from_block, to_block = day_block_range(
            _day_aware_runner_chain(lambda: 2000), DAY_ZERO_ANCHOR, height=400
        )
        self.assertEqual((day, from_block), (1, 359))
        self.assertGreaterEqual(
            to_block,
            DAY_ZERO_TEMPO - 1,
            "Day 1 must be at least one full tempo period long, unlike Day 0's own edge case",
        )
        self.assertEqual(
            to_block,
            717,
            "the module docstring's own claim -- the NEXT period boundary after Day 1's own "
            "last chance (719) already falls in Day 2, with nothing in between -- depends on "
            "Day 1's own true last block being 717; this pins it down directly rather than "
            "leaving it implicit",
        )

    def test_a_day_whose_own_last_chance_completes_between_ticks_is_still_published(self):
        # The exact live shape issue #244 reports: the pinned height jumps
        # from a height still inside the day (refused, correctly, at that
        # moment) straight to a height already inside the NEXT day, one
        # tempo period later.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = DayAwareChain(head=400, settle_blocks=1)
            chain.published_days.add(0)  # Day 0 already handled -- out of this test's scope

            def fake_sleep(_seconds):
                chain.head = 720  # Day 1 closes (Day 2's own first block is 718)

            outcomes = []
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=3,
                on_tick=lambda r: outcomes.append(r),
            )

            self.assertEqual(outcomes[0].outcome, "refused")
            self.assertIn("DayNotComplete", outcomes[0].detail)
            self.assertIn("day 1", outcomes[0].detail)

            self.assertEqual(
                outcomes[1].outcome,
                "published",
                "day 1's own last-chance height must be retried, and publish, the moment day "
                "1 actually closes -- even though pin_height's own fresh output for this tick "
                "has already moved on to day 2's own period",
            )
            self.assertIn("day 1's own catch-up", outcomes[1].detail)

            self.assertEqual(
                outcomes[2].outcome,
                "refused",
                "ordinary progression resumes normally once day 1 is resolved -- day 2's own "
                "period, refused DayNotComplete exactly like any ordinary mid-day period",
            )
            self.assertIn("day 2", outcomes[2].detail)

            self.assertEqual(len(chain.publish_calls), 1, "day 1 must publish exactly once")
            period, _vector, height = chain.publish_calls[0]
            self.assertEqual(height, DAY_ONE_LAST_CHANCE_HEIGHT)
            self.assertEqual(period, 0)
            self.assertTrue(chain.already_published(DAY_ONE_LAST_CHANCE_HEIGHT))

            state = read_state(state_path)
            self.assertNotIn(
                DAY_ONE_LAST_CHANCE_HEIGHT,
                state.get("refused_heights", []),
                "day 1's own last-chance height must never be permanently blacklisted -- it "
                "was a transient DayNotComplete, not a genuine refusal",
            )

    def test_a_day_that_completes_before_its_own_period_is_first_reached_publishes_in_one_shot(
        self,
    ):
        # No regression: the ordinary case (Day 3's own real, successful,
        # fully-automatic publish tonight) must be completely unaffected --
        # a period reached only once its own day has ALREADY closed
        # publishes directly, with no DayNotComplete refusal and no day
        # catch-up involved at all.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            # A generous settle margin (materially more than one day long,
            # at this fixture's own block spacing) so the very FIRST
            # pinned height's own day has unambiguously already ended by
            # the time this loop ever looks at it.
            chain = DayAwareChain(head=3000, tempo=DAY_ZERO_TEMPO, settle_blocks=800)
            for day in range(5):
                chain.published_days.add(day)

            outcomes = []
            run_forever(
                chain,
                settle_blocks=800,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
                on_tick=lambda r: outcomes.append(r),
            )

            self.assertEqual(outcomes[0].outcome, "published")
            self.assertEqual(len(chain.publish_calls), 1)
            period, _vector, height = chain.publish_calls[0]
            self.assertEqual((period, height), (4, 1799))
            state = read_state(state_path)
            self.assertIsNone(
                state.get("day_catchup_pending"),
                "day catch-up must never engage when the ordinary path already succeeds on "
                "its own -- this is exactly the unaffected, common case",
            )

    def test_a_day_whose_catch_up_genuinely_fails_is_abandoned_loudly_not_retried_forever(self):
        # Not every day can be recovered automatically -- a real, different
        # refusal (not DayNotComplete) at the pending height must be named
        # loudly (matching `_gap_note`'s own GAP-warning precedent for
        # "something this loop could not do on its own"), not silently
        # dropped, and must not be retried forever either.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = _AlwaysFailsAtHeight(
                head=400, settle_blocks=1, fail_height=DAY_ONE_LAST_CHANCE_HEIGHT
            )
            chain.published_days.add(0)

            def fake_sleep(_seconds):
                chain.head = 720

            outcomes = []
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=3,
                on_tick=lambda r: outcomes.append(r),
            )

            self.assertEqual(outcomes[0].outcome, "refused")
            self.assertEqual(outcomes[1].outcome, "day_catchup_abandoned")
            self.assertIn("DAY SKIPPED", outcomes[1].detail)
            self.assertIn("day 1", outcomes[1].detail)
            self.assertEqual(len(chain.publish_calls), 0, "an abandoned day must never publish")

            state = read_state(state_path)
            self.assertIn(DAY_ONE_LAST_CHANCE_HEIGHT, state.get("refused_heights", []))

            # Not retried forever: the next tick is an ordinary, UNRELATED
            # day 2 period, not another attempt at the abandoned day -- the
            # state file's own pending entry, by the end of this run, names
            # day 2 (the NEW, legitimate pending day), never day 1 again.
            self.assertEqual(outcomes[2].outcome, "refused")
            self.assertIn("day 2", outcomes[2].detail)
            self.assertEqual(state.get("day_catchup_pending"), {"day": 2, "height": 719})

    def test_a_pending_day_catch_up_survives_a_restart(self):
        # Two SEPARATE `run_forever` calls against the same state file --
        # the real restart shape: a fresh process, a fresh in-memory
        # `day_catchup` dict, seeded ONLY from what `read_state` finds on
        # disk, exactly what a restarted systemd unit does. Unlike
        # `day_zero_cache`, this MUST be seeded from disk (see
        # `_write_state`'s own docstring for `day_catchup_pending`) -- there
        # is no equivalent fixed anchor this loop could cheaply re-derive
        # the pending day from otherwise.
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = DayAwareChain(head=400, settle_blocks=1)
            chain.published_days.add(0)

            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
            )
            state_after_first_process = read_state(state_path)
            self.assertEqual(
                state_after_first_process.get("day_catchup_pending"),
                {"day": 1, "height": DAY_ONE_LAST_CHANCE_HEIGHT},
            )
            self.assertEqual(chain.publish_calls, [])

            # Day 1 closes while the process is down.
            chain.head = 720

            outcomes = []
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=lambda _s: None,
                iterations=1,
                on_tick=lambda r: outcomes.append(r),
            )

            self.assertEqual(
                outcomes[0].outcome,
                "published",
                "a pending day catch-up must be recovered across a restart, not silently lost",
            )
            self.assertEqual(len(chain.publish_calls), 1)
            self.assertEqual(chain.publish_calls[0][2], DAY_ONE_LAST_CHANCE_HEIGHT)

    def test_a_multi_period_day_is_retried_once_per_period_not_every_poll(self):
        # Found by an independent Verifier, and reproduced live against
        # these exact fixtures: `pin_height` is a STEP function (it jumps
        # once per period, then HOLDS that value for the rest of the
        # period -- `pinning.py`'s own docstring), so a throttle compared
        # directly against the pending day's own FIXED target height
        # stayed true on every poll for the rest of the day once
        # `pin_height` first moved past it, not once per period as this
        # module's own top docstring originally (and wrongly) claimed --
        # 53 of 60 polls in that reproduction actually re-entered
        # `_attempt_day_catchup` (a real lock, a real chain read, a real
        # state write) at the identical, still-stale height. All 5 tests
        # above use tempo=DAY_ZERO_TEMPO=360 against a Day 1 only ~1
        # period long, so day catch-up always resolved on the very next
        # tick and never actually exercised more than one period of
        # holding -- exactly what this test covers instead, with tempo=60
        # against the SAME Day 1 (359..717), now ~6 periods long (359,
        # 419, 479, 539, 599, 659 all fall inside it), the same shape as
        # rehearsal chain's real Day 2 (roughly a dozen periods at
        # tempo=360).
        with tempfile.TemporaryDirectory() as tmp:
            state_path, lock_path = _paths(tmp)
            chain = DayAwareChain(head=400, tempo=60, settle_blocks=1)
            chain.published_days.add(0)

            already_published_calls = []
            real_already_published = chain.already_published

            def counting_already_published(height):
                already_published_calls.append(height)
                return real_already_published(height)

            chain.already_published = counting_already_published

            # Head advances slowly -- 10 blocks per poll, never closing
            # Day 1 (which needs head >= 718): 30 polls at +10 ends at
            # head=690, still well inside Day 1's own range.
            def fake_sleep(_seconds):
                chain.head += 10

            outcomes = []
            run_forever(
                chain,
                settle_blocks=1,
                state_path=state_path,
                lock_path=lock_path,
                poll_seconds=1,
                sleep=fake_sleep,
                iterations=30,
                on_tick=lambda r: outcomes.append(r),
            )

            self.assertLess(
                chain.head, 718, "test setup error: Day 1 must still be genuinely open throughout"
            )
            self.assertEqual(chain.publish_calls, [], "Day 1 never actually closes in this test")

            # A REAL attempt is `acted=True` (a real lock/chain-read/state
            # write happened); the cheap, no-op throttle in between is
            # `acted=False` but reports the identical outcome string, so
            # `acted` is what actually distinguishes them here.
            real_attempts = sum(
                1 for r in outcomes if r.outcome == "day_catchup_pending" and r.acted
            )
            self.assertEqual(
                real_attempts,
                5,
                f"day catch-up must retry once per NEW pin_height value (5 period "
                f"transitions after the initial ordinary refusal, across 30 polls spanning "
                f"~6 periods), not once per poll -- got {real_attempts} real attempts",
            )
            # The chain itself was not hammered: one ordinary `already_published`
            # call for the initial refusal, plus exactly one more per real
            # catch-up attempt -- never one per poll.
            self.assertEqual(len(already_published_calls), 1 + real_attempts)

            cheap_waits = sum(
                1 for r in outcomes if r.outcome == "day_catchup_pending" and not r.acted
            )
            self.assertGreater(
                cheap_waits,
                0,
                "most polls between period transitions must be cheap no-ops, not real attempts",
            )


if __name__ == "__main__":
    unittest.main()
