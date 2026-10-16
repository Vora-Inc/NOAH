"""Run the validator's periodic publication loop.

Ticks check settled chain progress, acquire the local single-writer lock,
select an eligible evaluation height and invoke the period pipeline. Durable
state records attempts and results for restart handling; it is operational
evidence, not proof that a transaction finalized. Publication-unknown states
require explicit reconciliation before another attempt.

Day-zero catch-up and subsequent period scheduling use chain-derived windows.
The normal loop resumes history scans and handles named refusals without
inventing missing chain state. Store state outside the source checkout, retain
it across upgrades, and inspect both publication receipts and chain readbacks.
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..chain.dayblocks import DayNotComplete, day_block_range
from ..chain.foreign_value import ForeignDepositUnpriced
from ..chain.sources import Refusal
from ..diagnostics import severity_for
from ..diagnostics import write as diagnostic
from ..pathtext import plain_path_text
from ..runtime_events import begin_validator_attempt, emit, traced
from .keeper import InvalidScore, KeeperWouldPayOwner
from .landed import PreviousRevealMismatch
from .period import AlreadyPublished, HeightNotPinned, PeriodRefused, run_period
from .pinning import NoSettledPeriod, period_of, pin_height
from .publish_intent import PublicationStatusUnknown
from .reader import ChainUnreadable
from .settings import SettingsError
from .writer_lock import LockHeld, held

# Every one of these already refuses loudly, by name, with its own reason
# (period.py, keeper.py, landed.py, and every noah.chain.* reader via
# Refusal). run_period's own docstring is explicit that it does not retry --
# "retrying is the operator's decision" -- and this loop honours that across
# periods, not just within a single run_period call: once one of these is
# raised for a period, this loop does not attempt that SAME period again
# until a later boundary makes a new one ready.
#
# Refusal (noah.chain.sources) is a BaseException subclass, deliberately
# ("never catch this broadly" -- its own docstring), and run_period's own
# chain.read/chain.score/chain.agree calls catch only `except Exception`
# around it, which does not catch a BaseException. `vector.py`'s
# OwnerWouldBePaid/SeatOwnerUnknown are two of OVER FORTY Refusal subclasses
# spread across noah/chain/ (fixture_build.py, deposits.py, emissions.py,
# pay.py, sources.py, agreeing.py, reads.py, runner.py's own RunRefused,
# store.py, and more) -- codex adversarial review, round 3 (2026-08-29),
# demonstrated the general shape by naming runner.py's RunRefused and
# sources.py's SourceDisagreement specifically, after round 1 already found
# two individual examples the same way. Naming the family's own shared base
# class here, rather than enumerating every subclass (which round 1's fix
# started doing, one type at a time, exactly the pattern that made round 3's
# finding possible), is what the "never catch broadly" docstring actually
# asks for: catching Refusal explicitly, by name, for the deliberate purpose
# of treating it as a named period refusal is not the blanket
# `except Exception`/`except BaseException` that docstring forbids -- it is
# the same explicit, by-name discipline every other entry in this tuple
# already follows, applied once, at the level the class hierarchy actually
# offers, instead of chasing individual subclasses one review round at a
# time. Without this, ANY real production chain-reader adapter's own
# refusal (a source disagreement, an unreadable run, any of the forty-plus
# others) would propagate straight out of run_forever's own while loop and
# kill the whole unattended process.
#
# HeightNotPinned and ChainUnreadable are deliberately NOT in this tuple --
# they have their own shared except clause in `tick`, treated as transient
# (waiting), not a genuine per-period refusal. See that clause's own comment
# for why.
PERIOD_REFUSALS = (
    PeriodRefused,
    PreviousRevealMismatch,
    InvalidScore,
    KeeperWouldPayOwner,
    Refusal,
)


class LoopRefused(SettingsError):
    """Something about how the loop itself is set up stops it starting.

    A ``SettingsError``, like every other startup refusal in this codebase
    (``settings.py``, ``startup.py``): a bad ``poll_seconds`` is the same
    KIND of problem as a bad setting, and a caller that already catches
    ``SettingsError`` around startup catches this too, for free.
    """


@dataclass(frozen=True)
class TickResult:
    """What one tick of the loop did, for a caller or a test to inspect."""

    acted: bool
    period: Optional[int]
    outcome: str
    detail: str
    # The tempo to carry forward alongside the returned `last_attempted_height`
    # (this tick's own `tempo` on a branch that changed that height, the
    # UNCHANGED input value on a branch that did not) -- added round 13
    # (2026-08-29), see `tick`'s own docstring for `last_attempted_tempo`.
    # Not returned as a third tuple element: doing that would change
    # `tick`'s existing 2-tuple return arity, breaking every one of this
    # module's own tests (and any real caller) that already destructures it
    # as `result, last_attempted_height = tick(...)`. Attaching it to
    # `TickResult` instead is purely additive -- unread by anything that
    # does not know about it yet.
    last_attempted_tempo: Optional[int] = None
    severity: Optional[str] = None

    @property
    def log_severity(self):
        return severity_for(outcome=self.outcome, severity=self.severity)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pending_day_ready(chain, pending, day_catchup):
    """Check finalized day readiness at most once per five minutes.

    Only the cadence uses local monotonic time. The adapter proves readiness
    from chain timestamps; a positive hint never replaces run_period's guards.
    This process-local cache is deliberately not a durable success marker.
    A restart may repeat a read, but still must reconcile any prior publication.
    """
    check = getattr(chain, "closed_day_ready", None)
    if not callable(check):
        return False
    key = (pending["day"], pending["height"])
    now = time.monotonic()
    previous = day_catchup.get("readiness_check")
    if previous is not None and previous[0] == key and now - previous[1] < 300:
        return False
    # Failed probes are paced too. Do not cache a positive result: every retry
    # needs fresh chain evidence, even after a lock conflict or lost response.
    day_catchup["readiness_check"] = (key, now)
    return check(pending["day"]) is True


def _day_zero_catch_up_height(chain, *, tempo, day_zero_cache):
    """Day 0's own true last block, if Day 0 is genuinely too short for
    `pinning.pin_height` to ever reach on its own AND has not already been
    published -- ``None`` otherwise. See this module's own top docstring,
    "Day 0 catch-up", for why this exists and how it is composed.

    ``None`` on every one of the following, each of them an ordinary,
    expected case rather than a failure:

    - ``day_zero_cache.get("resolved")`` is already true (a prior call this
      process made already settled the question one way or the other; see
      that cache's own docstring in the module-level section above).
    - ``chain`` has no ``runner_chain``/``day_anchor_ts`` (most of this
      module's own test fixtures, and any real caller with nothing
      day-boundary-aware configured) -- the identical safe
      ``getattr``-with-a-default already used elsewhere in this file for a
      ticket-#16-specific accessor not every chain implements. Cached
      permanently: a chain shaped this way never gains those attributes
      mid-run.
    - `noah.chain.dayblocks.day_block_range` refuses (``Refusal`` --
      ``DayNotComplete``: Day 0, today, has not ended yet, the ordinary
      case for almost this function's entire life on a real chain;
      ``DayBoundaryUnstable``/``BlockTimeNotRead``: a real reorg-adjacent
      read problem). NOT cached: Day 0 will complete eventually, and a
      transient read problem deserves the identical next-tick retry this
      module already gives every other transient chain-read failure.
    - Day 0 is at least one full tempo period long (``to_block >= tempo -
      1``): period 0 (blocks ``0..tempo-1``) is then fully CONTAINED in Day
      0, so `pin_height`'s own ordinary settle-margin timing reaches it
      exactly the way it reaches every later day -- no catch-up needed,
      ever, for this chain. Cached: this is a fact about the chain's own
      genesis moment, which cannot change mid-run.
    - `chain.already_published` (real, chain-authoritative, unchanged) is
      already true for Day 0's own true last block. Cached: once true, it
      can never become false again for the same, immutable, already-closed
      day.
    - `chain.already_published` itself raises: a failed read is not
      evidence of anything (this module's own established treatment for
      every other read failure), and forcing an attempt without being able
      to check first would risk exactly the double-submit this check
      exists to prevent. NOT cached: retried next tick.
    """
    if day_zero_cache.get("resolved"):
        return None

    runner_chain = getattr(chain, "runner_chain", None)
    day_anchor_ts = getattr(chain, "day_anchor_ts", None)
    if runner_chain is None or day_anchor_ts is None:
        day_zero_cache["resolved"] = True
        return None

    try:
        _day, _from_block, to_block = day_block_range(runner_chain, day_anchor_ts, height=1)
    except Refusal:
        return None

    if to_block >= tempo - 1:
        day_zero_cache["resolved"] = True
        return None

    try:
        if chain.already_published(to_block):
            day_zero_cache["resolved"] = True
            return None
    except Exception:  # noqa: BLE001 -- a failed read is not evidence; retried next tick
        return None

    return to_block


class _DayZeroAwareChain:
    """Wraps ``chain`` so ``chain.pinned_height()`` -- the one call
    `run_period`'s own settled step 1 (`period.py`) ever makes to learn
    what to score -- answers with Day 0's own true last block while
    `_day_zero_catch_up_height` says that catch-up is genuinely pending,
    and otherwise delegates every call, including that same one, straight
    through to the real ``chain`` unchanged. See this module's own top
    docstring, "Day 0 catch-up", for why this is the composition point
    rather than a change to `pinning.py` or `period.py`.

    Every OTHER attribute (`already_published`, `read`, `score`, `agree`,
    `publish`, `dropped_duplicate_coldkeys`, and anything else a real
    `ChainAdapter` or a test fixture exposes) passes through via
    ``__getattr__`` untouched -- this wraps exactly one method, not the
    whole interface.
    """

    def __init__(self, chain, day_zero_cache):
        self._chain = chain
        self._day_zero_cache = day_zero_cache

    def pinned_height(self):
        # Read fresh here, not carried in from the caller's own top-of-tick
        # read -- the identical reasoning `_freshest_period` itself already
        # gives for re-reading `tempo` immediately before this decision is
        # used, rather than trusting a value that may already be stale.
        tempo = self._chain.tempo()
        day_zero_target = _day_zero_catch_up_height(
            self._chain, tempo=tempo, day_zero_cache=self._day_zero_cache
        )
        if day_zero_target is not None:
            return day_zero_target
        return self._chain.pinned_height()

    def __getattr__(self, name):
        return getattr(self._chain, name)


class _FixedHeightChain:
    """Wraps ``chain`` so ``chain.pinned_height()`` always answers with one
    fixed height, chosen by the caller, delegating every other attribute to
    ``chain`` unchanged (the identical single-method composition
    `_DayZeroAwareChain` above already establishes for Day 0, generalized to
    an arbitrary height instead of Day 0's own specific one). Used by day
    catch-up (see `_attempt_day_catchup` and this module's own top
    docstring, "Day catch-up: any day's own last chance, not only Day 0's")
    to retry `run_period` against a specific earlier day's own remembered
    height without going through `pin_height`/`_terminal_gate` at all --
    that pending height is, by construction, EARLIER than
    `last_attempted_height` by the time it is retried, which
    `_terminal_gate`'s own reorg-suspicion check would otherwise misread as
    the chain having moved backward (see that check's own docstring).
    """

    def __init__(self, chain, height):
        self._chain = chain
        self._height = height

    def pinned_height(self):
        return self._height

    def __getattr__(self, name):
        return getattr(self._chain, name)


def _freshest_period(chain, *, fallback_tempo, fallback_period, fallback_pinned):
    """The most current tempo/period/height this tick can attribute an
    outcome to, re-read immediately before the risky work begins rather
    than trusted from before this tick acquired the lock.

    Tries `chain.pinned_height()` again -- the EXACT same call `run_period`'s
    own step 1 is about to make anyway, asked one instruction earlier rather
    than a different question. That is why this does not reopen #533's own
    concern about a slow pre-submit check going stale: #533 is about a
    check that sits between an ownership/identity read and a signing
    action, verifying something that can go stale while the check itself
    runs. This asks the SAME question run_period is about to ask regardless,
    narrowing the gap between this tick's own plan and what run_period will
    actually act on, not introducing a new fact that could go stale on its
    own account.

    ALSO tries `chain.tempo()` again -- found by codex adversarial review,
    round 15 (2026-08-29): the original version of this function accepted
    `tempo` as a plain parameter (the tick's own STALE, top-of-tick read)
    and used it to derive `period_of(height=height, tempo=tempo)` from the
    FRESH height -- mixing a fresh height with a stale tempo. If the
    subnet's tempo genuinely changed in the gap between this tick's
    top-of-tick read and this refresh, the returned period number is
    computed under the WRONG tempo, and -- more seriously -- every
    DOWNSTREAM use of that stale `tempo` (this file's own
    `last_attempted_tempo` bookkeeping, specifically) then records the
    WRONG tempo alongside the height `run_period` actually acted on. That
    silently DISABLES the reorg-suspicion check on a LATER tick: it only
    fires when `tempo == last_attempted_tempo`, so a stale recorded tempo
    that happens to differ from the CURRENT one makes a genuine reorg look,
    incorrectly, like an explained tempo change -- a false negative, worse
    than the false positive this file otherwise risks, because it disables
    protection rather than merely delaying a legitimate publish by one
    tick. Reading both together, from the same two back-to-back local
    calls, keeps them as internally consistent as this function's own
    height re-read already is -- narrowed, not fully closed, for the
    identical reason the module docstring's own drift-gap section already
    gives (a genuine race between TWO separate calls, `chain.tempo()` and
    `chain.pinned_height()`, cannot be made atomic from outside `run_period`
    without touching its own settled interface).

    Falls back to `fallback_tempo`/`fallback_period`/`fallback_pinned` --
    the tick's own prior plan -- if either re-read itself fails or returns
    nothing: a failed re-query is not evidence the plan was wrong, and
    guessing would be worse than the tick's own best prior estimate. Falls
    back consistently as a TRIPLE -- if the height re-read fails, the tempo
    re-read (even if it itself succeeded) is discarded too, so a caller
    never receives a freshly-read tempo paired with a stale fallback height
    or vice versa.
    """
    try:
        tempo = chain.tempo()
    except Exception:  # noqa: BLE001 -- see docstring: a failed re-check falls back, it does not raise
        return fallback_tempo, fallback_period, fallback_pinned
    try:
        height = chain.pinned_height()
    except Exception:  # noqa: BLE001 -- see docstring: a failed re-check falls back, it does not raise
        return fallback_tempo, fallback_period, fallback_pinned
    if height is None:
        return fallback_tempo, fallback_period, fallback_pinned
    return tempo, period_of(height=height, tempo=tempo), height


def _terminal_gate(
    pinned, period, *, last_attempted_height, last_attempted_tempo, tempo, refused_heights
):
    """The four "do not attempt this" checks `tick` needs BEFORE calling
    `run_period` -- exact-repeat, same-period-different-height,
    reorg-suspicion, and already-refused -- factored out so they can be run
    TWICE: once against this tick's own pre-lock plan, and again,
    identically, against `_freshest_period`'s fresher read taken
    immediately before `run_period` is actually called.

    Found by codex adversarial review, round 14 (2026-08-29): before this
    refactor, these three checks ran ONLY against the pre-lock `pinned`,
    never again after `_freshest_period` refreshed it inside the lock --
    so a chain that moved (a genuine tempo change, OR the SAME reorg this
    module's own reorg-suspicion check exists to catch) in the narrow
    window between the pre-lock plan and the in-lock refresh could still
    reach `run_period` with a height these checks would have caught had
    they been asked again. Concretely: pre-lock plan names height 49
    (never refused, never a reorg concern); by the time `_freshest_period`
    refreshes inside the lock, the chain has moved such that the FRESH
    pinned height is 29 -- a height THIS run already refused. The pre-lock
    check, asked about 49, correctly saw nothing wrong; nothing asked the
    identical question about the ACTUAL height about to be attempted.

    Returns `None` when it is safe to proceed, or a `TickResult` (never a
    2-tuple -- see `tick`'s own call sites: `last_attempted_height` is
    always unchanged on every branch this function can return, so the
    caller pairs whichever `TickResult` this returns with its OWN current
    `last_attempted_height` rather than this function inventing one).
    """
    if last_attempted_height is not None and pinned == last_attempted_height:
        return TickResult(
            acted=False,
            period=period,
            outcome="waiting",
            severity="error" if pinned in refused_heights else "info",
            detail=(
                f"period {period} was already attempted; waiting for a later period to "
                f"become ready rather than retrying it"
            ),
            last_attempted_tempo=last_attempted_tempo,
        )
    # SAME PERIOD, DIFFERENT, LATER HEIGHT, unchanged tempo -- found by an
    # independent Verifier (2026-09-21) against Day 0 catch-up specifically:
    # under ordinary `pin_height` alone this never happens (a fixed tempo
    # means two DIFFERENT returned heights always land in two DIFFERENT
    # periods -- `pin_height`'s own output is always `(target+1)*tempo - 1`,
    # so `period_of` of it is always exactly `target`, and a different
    # returned height means a different target). Day 0 catch-up is the one
    # thing that breaks that invariant on purpose: it can return a height
    # (Day 0's own true last block) that is NOT a tempo-period boundary at
    # all, and the very next tick -- once `already_published` marks Day 0
    # resolved and this falls back to ordinary `pin_height` -- can legally
    # return the period's own ordinary tempo-pinned height, which is a
    # DIFFERENT, LATER literal height in the SAME period (period 0 in both
    # cases; Day 0 catch-up's own target is always Day 0's true last block,
    # which is always EARLIER than that period's own tempo-aligned end, the
    # only other height `pin_height` could ever hand this period). The
    # exact-equality check above does not catch this (the heights genuinely
    # differ), and without this check the tick would fall through to
    # `run_period` a second time for a period this run already handled -- a
    # redundant scoring/signing attempt even though the real,
    # chain-authoritative `already_published` (period.py step 2) still
    # correctly refuses it before anything is submitted. Gated on
    # `tempo == last_attempted_tempo`, the identical safe precondition the
    # reorg-suspicion check below already establishes for comparing period
    # numbers at all: comparing period numbers computed under DIFFERENT
    # tempos is exactly the anti-pattern round 6 (2026-08-29, see
    # `_gap_note`'s own docstring) already found unsafe, so this never fires
    # across a genuine tempo change.
    #
    # `pinned > last_attempted_height`, not just "different" -- found by an
    # independent Verifier's own second pass (2026-09-21): without this
    # guard, this check runs BEFORE the reorg-suspicion check right below
    # it and would intercept a genuine same-tempo, IN-PERIOD backward reorg
    # too (a period can span more than one settled height only through Day
    # 0 catch-up's own forward-only relationship to it -- see above -- so a
    # backward move inside the same period is never Day 0 catch-up's own
    # doing). Both branches return `acted=False`, so nothing UNSAFE
    # happened either way, but the reorg-suspicion check's own message is
    # the specific, actionable one operators actually need
    # (`TickResult.detail` exposes it to callers), and a reorg deserves that
    # specific warning, not this check's generic one. Restricting this
    # check to the forward direction is exactly the direction Day 0
    # catch-up's own transition (358, then later 359) actually moves in,
    # so nothing this check exists to catch is lost.
    if (
        last_attempted_height is not None
        and last_attempted_tempo is not None
        and tempo == last_attempted_tempo
        and pinned > last_attempted_height
        and period_of(height=pinned, tempo=tempo)
        == period_of(height=last_attempted_height, tempo=tempo)
    ):
        return TickResult(
            acted=False,
            period=period,
            outcome="waiting",
            detail=(
                f"period {period} was already attempted this run at height "
                f"{last_attempted_height} (this tick names height {pinned} instead, the "
                f"same period under the same tempo -- Day 0 catch-up and pinning.pin_height's "
                f"own ordinary tempo pinning can each name a different height inside the same "
                f"tempo period once Day 0 has closed); waiting rather than re-attempting a "
                f"period this run already handled under a different height"
            ),
            last_attempted_tempo=last_attempted_tempo,
        )
    if (
        last_attempted_height is not None
        and last_attempted_tempo is not None
        and tempo == last_attempted_tempo
        and pinned < last_attempted_height
    ):
        return TickResult(
            acted=False,
            period=period,
            outcome="waiting",
            detail=(
                f"pinned height {pinned} is EARLIER than the last one this run attempted "
                f"({last_attempted_height}), with no tempo change to explain it -- the "
                f"observed chain head appears to have moved backward (a reorg, or two "
                f"reads briefly disagreeing); waiting rather than treating this as a new "
                f"period"
            ),
            severity="warning",
            last_attempted_tempo=last_attempted_tempo,
        )
    if pinned in refused_heights:
        return TickResult(
            acted=False,
            period=period,
            outcome="waiting",
            severity="error",
            detail=(
                f"period {period} (height {pinned}) was already refused earlier this "
                f"run; made reachable again, but a refusal is not silently retried -- "
                f"retrying is the operator's decision"
            ),
            last_attempted_tempo=last_attempted_tempo,
        )
    return None


def _gap_note(before_height, observed_height, tempo):
    """How many periods, if any, between `before_height` and
    `observed_height` were never individually attempted, and the message
    naming it -- both converted to period numbers using the SAME, CURRENT
    `tempo` before comparing.

    `before_height` is the last height this loop actually reached (`None`
    on a first-ever run, which reports no gap: there is nothing to have
    skipped yet). `observed_height` is whichever height this tick's own
    outcome is AUTHORITATIVE for -- the planned one for every outcome
    except a successful publish, and `run_period`'s own actually-achieved
    one for that one outcome (see `tick`'s own comment on `actual_height`
    for why those can differ).

    HEIGHTS, not period numbers, travel between calls -- found by codex
    adversarial review, round 6 (2026-08-29): a period NUMBER is
    `height // tempo`, and if the subnet's own tempo changes between two
    calls, a LATER height can produce a SMALLER period number than an
    EARLIER one did under the old tempo (codex's own worked example: height
    119 at tempo 20 is period 5; height 199, later, at tempo 100, is period
    1). Comparing bare period numbers recorded at different tempos is
    comparing two different units that happen to look like the same one.
    Period numbers are derived from a height and the CURRENT tempo only at
    the moment they are needed for a human-facing report, never stored or
    compared directly.

    Precisely: `chain.current_block()`'s own raw output (a plain block
    count) is what is genuinely tempo-independent and monotonic. The
    PINNED/settled height this module actually compares (`pin_height`'s own
    output, what `tick` calls `pinned`) is a DIFFERENT thing and is NOT
    purely a function of monotonic raw height -- codex adversarial
    CHALLENGE (2026-08-29, after review round 8) found a verified
    counterexample: `pin_height(head=31, tempo=10, settle_blocks=1)` is 29,
    and `pin_height(head=31, tempo=20, settle_blocks=1)` -- the identical,
    non-earlier head, only a larger tempo -- is 19. This is exactly why
    `tick`'s own skip-gate compares pinned heights for EXACT EQUALITY, never
    with `<` or `<=` (see that comparison's own comment): equality needs no
    monotonicity assumption at all, only "is this the identical value as
    last time", which stays well-defined regardless of tempo history. This
    function's own gap arithmetic below (subtraction, clamped at zero) is
    NOT rebuilt to make the same guarantee -- see this module's own
    "What round 6's own fix does NOT close" section for the resulting,
    already-accepted reporting limitation.

    Not a bug either caller can fix, independent of the tempo concern
    above: `pin_height` only ever names the LATEST settled period (settled,
    untouched design -- see `pinning.py`, "does NOT reach further back"),
    so once the chain has moved past a period, nothing can make
    `run_period` score it again. Named loudly rather than silently passed
    over, per #535's own "refuses loudly and specifically rather than ...
    silently skipping a period".

    ## What round 6's own fix does NOT close, named rather than hidden

    Converting BOTH heights with the CURRENT tempo (rather than each with
    whatever tempo was actually in effect when it was recorded) keeps this
    function simple and correct for the case that matters most -- the
    SKIP-GATE itself (`tick`'s own `pinned <= last_attempted_height`
    comparison) never uses this function at all, compares raw heights, and
    is unaffected by anything below. But the GAP COUNT this function
    reports can still be WRONG, specifically UNDER-reported, when the
    subnet's tempo changed between `before_height` and `observed_height`.
    Codex adversarial review, round 7 (2026-08-29), gave the exact case:
    terminal height 119 under tempo 20 (period 5 at the time), restarting
    later at height 299 under a NEW tempo 100 -- a real boundary was
    crossed at height 199 under the OLD tempo that this function, using
    ONLY the current tempo (100) for both ends, computes as periods 1 and
    2 and reports as a gap of ZERO. Not merely imprecise: actively
    misleading, since a human reading `gap_before: 0` reasonably concludes
    nothing was missed.

    Closing this fully would mean persisting the tempo that was ACTUALLY in
    effect alongside every `last_terminal_height` (a `last_terminal_tempo`
    field) and detecting a tempo change AT SEED TIME to report "gap
    unknown" rather than attempting a period count across two different
    units -- a real, buildable fix, but one that (like `identity` above)
    needs threading a new value through every state write and, unlike
    `identity`, through this function's own signature and every one of its
    call sites. Deferred here, deliberately, as its own next step rather
    than folded into an already-large round of fixes: this is a
    REPORTING/OBSERVABILITY gap, not a safety one -- no period is ever
    silently skipped or double-published because of it, only mis-COUNTED
    in the audit trail after the fact, in the specific compound case of
    this loop being down or behind AT THE SAME TIME the subnet's tempo
    changes. Named here so it is not mistaken for solved.
    """
    if before_height is None:
        return 0, ""
    before_period = period_of(height=before_height, tempo=tempo)
    observed_period = period_of(height=observed_height, tempo=tempo)
    gap = observed_period - before_period - 1
    if gap <= 0:
        return 0, ""
    return gap, (
        f" GAP: {gap} period(s) between height {before_height} and {observed_height} "
        f"(periods {before_period} and {observed_period} at the current tempo) were "
        f"never individually attempted (pin_height only ever names the latest settled "
        f"period; a period that goes stale before this loop reaches it cannot be "
        f"recovered)."
    )


def _is_pre_publish_read_failure(error):
    """True only for a `PeriodRefused` this module can POSITIVELY identify
    as coming from `run_period`'s own steps 3-5 (`chain.read`/`chain.score`/
    `chain.agree` -- period.py, its own settled ordering) -- the steps that
    run BEFORE anything is submitted, where a transport failure has
    genuinely touched nothing to protect. False for everything else,
    including step 6 (`chain.publish`) and any message this function does
    not recognize -- the safe default, since an unrecognized failure is
    treated exactly as if the caller (`tick`'s own `except PERIOD_REFUSALS`
    handler) had never special-cased a wrapped `ChainUnreadable` at all:
    terminal, not retried within this run.

    Found necessary by codex adversarial review, round 10 (2026-08-29): the
    wrapped-`ChainUnreadable`-is-transient fix (see that handler's own
    comment) originally treated ANY step's wrapped failure as safe to
    retry, but period.py's OWN docstring for step 6 says plainly why a
    publish failure must not auto-retry -- and a `ChainUnreadable` there
    specifically can mean the broadcast itself succeeded and only the
    confirmation read afterward failed, so retrying risks a second
    submission for a period that may already be on chain.

    Matched by MESSAGE TEXT, not a structured marker -- the only signal
    available without changing period.py's settled interface to carry one,
    which is out of this issue's scope. This is deliberately fragile in one
    specific, safe direction: if period.py's own wording for steps 3-5 ever
    changes, this stops recognizing them, and their wrapped ChainUnreadable
    silently reverts to the OLD (pre-round-10) safe-but-pessimistic
    behavior -- terminal rather than retried -- not to anything unsafe. The
    existing `AChainUnreadableWrappedInsidePeriodRefusedIsAlsoWaiting` tests
    exercise this against the REAL `run_period`/period.py code, not a mock
    of it, so a wording change that broke this match would fail those tests
    immediately and loudly, rather than silently changing behavior.
    """
    message = str(error)
    return (
        message.startswith("the chain could not be read at height")
        or message.startswith("scoring failed at height")
        or message.startswith("the two scoring engines did not agree at height")
    )


def _day_not_complete_cause(error):
    """The `DayNotComplete` genuinely behind `error`, or `None` -- issue
    #244, see this module's own top docstring, "Day catch-up: any day's
    own last chance, not only Day 0's".

    Checked TWO ways because `DayNotComplete` can reach `tick`'s own
    `except PERIOD_REFUSALS` handler by two different routes, not one:
    `noah.chain.dayblocks.day_block_range` is a `Refusal` (`PERIOD_REFUSALS`
    itself, directly -- see that tuple's own definition above), and
    `ChainAdapter.already_published`'s direct-mode path (`run_period`'s own
    step 2, `period.py`) calls it with no `try`/`except` of its own around
    it, so a `DayNotComplete` from THERE arrives here completely UNWRAPPED,
    `error` itself. `ChainAdapter.read` (step 3, wrapped in `PeriodRefused`
    by period.py's own `except Exception`) reaches it the other way, the
    identical `error.__cause__` shape `_is_pre_publish_read_failure`'s own
    caller already checks for a `ChainUnreadable` cause, via Python's
    normal exception chaining (`raise ... from error`). Structured
    attributes (`.day`, `.height` -- see `DayNotComplete`'s own docstring),
    not message-text parsing: this is exactly the higher-stakes case that
    docstring already distinguishes from `_is_pre_publish_read_failure`'s
    own, lower-stakes, text-matched precedent.
    """
    if isinstance(error, DayNotComplete):
        return error
    cause = getattr(error, "__cause__", None)
    if isinstance(cause, DayNotComplete):
        return cause
    return None


def _foreign_unpriced_cause(error):
    """Recognize only the structured, pre-publication price refusal."""
    for candidate in (error, getattr(error, "__cause__", None)):
        if isinstance(candidate, ForeignDepositUnpriced):
            return candidate
    return None


def _write_state(
    path, payload, *, identity=None, refused_heights=None, day_catchup_pending=None
):
    """Durable, atomic: a crash mid-write must never leave a half-written,
    unparseable state file behind. Temp file plus `os.replace`, which is
    atomic on the same filesystem.

    ``identity``, when given, is merged into the payload as ``"identity"``
    -- one place doing this rather than every call site repeating it, so
    every state write this module ever performs carries it uniformly. See
    `tick`'s own docstring for what identity is for.

    ``refused_heights``, when given, is merged into the payload as
    ``"refused_heights"`` (a sorted list -- JSON has no set type), using
    the identical one-place-not-every-call-site pattern `identity` already
    established. Every `_write_state` call inside `tick` passes the SAME
    set object through, regardless of which outcome it is recording, so a
    later write (for a different period entirely) never accidentally drops
    a set an earlier write recorded -- `_write_state` always overwrites the
    whole file, never merges with what was there before, so a value that
    is not threaded through EVERY call site would otherwise be silently
    erased by the next unrelated write. See `tick`'s own docstring for what
    this set is for.

    ``day_catchup_pending``, when given, is merged into the payload as
    ``"day_catchup_pending"`` -- issue #244, see this module's own top
    docstring, "Day catch-up: any day's own last chance, not only Day 0's".
    Unlike `refused_heights`, this one does NOT need threading through
    every call site to stay correct: day catch-up's own design blocks
    ordinary progression entirely while a day is pending (see that section
    of the top docstring), so the only writes that ever happen while a
    pending entry is live are day catch-up's own -- every one of which
    passes the CURRENT value explicitly (a dict while retrying, `None` the
    moment it resolves or is abandoned). A write from anywhere else in this
    module always happens with nothing pending, so leaving this argument at
    its default (`None`, merged as absent-from-payload, which `read_state`
    decodes identically to an explicit `null`) is exactly correct there,
    not a gap.

    The temp filename includes both pid AND thread id -- found by codex
    adversarial CHALLENGE (2026-08-29): `run_forever` itself only ever calls
    this from a single thread, but the old `.tmp<pid>` name alone would let
    two threads inside the same process race on the identical tmp path if
    this function were ever called from more than one (a possibility this
    module does not rule out for a future caller). Matching
    `writer_lock.py`'s own established `(pid, thread_id)` naming pattern
    costs nothing and removes the silent "only ever one thread" assumption.

    The write is flushed and fsync'd before `os.replace`, and the containing
    directory is fsync'd afterward (best-effort) -- also found by codex
    adversarial CHALLENGE (2026-08-29): a plain `write_text` can leave new
    content sitting in the OS page cache, never reaching disk, so a crash
    right after an apparently-successful state write could still lose the
    very restart evidence this function exists to make durable.
    The directory fsync is best-effort, not fail-closed: on a
    filesystem that does not support it (notably Windows), the state file's
    own content is already safely on disk either way, and refusing to
    proceed over a directory-entry guarantee this function cannot get would
    turn "restart evidence is slightly less durable" into "the loop cannot
    take its next tick," which is a strictly worse outcome for a value this
    module already treats as non-authoritative (see `read_state`).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if identity is not None:
        payload = {**payload, "identity": identity}
    if refused_heights is not None:
        payload = {**payload, "refused_heights": sorted(refused_heights)}
    if day_catchup_pending is not None:
        payload = {**payload, "day_catchup_pending": day_catchup_pending}
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}.{threading.get_ident()}")
    with open(tmp, "w") as f:
        f.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        diagnostic(
            f"  (could not fsync {plain_path_text(path.parent)}: {type(error).__name__} -- the "
            f"state file's own content was still written and fsync'd; only the "
            f"directory-entry durability guarantee is weaker on this filesystem)",
            severity="warning",
        )


def read_state(path):
    """The persisted state, or None if it does not exist, cannot be parsed,
    or is not shaped the way this module itself ever writes it.

    A corrupted or absent file is not treated as evidence of anything: it
    only ever seeds `last_attempted_height`'s in-memory throttle (see the module
    docstring, "Restart safety"), never a correctness decision. Losing it
    costs at most one needless re-attempt of a period `run_period`'s own
    `already_published` check would refuse harmlessly anyway.

    Shape, not just syntax, is validated -- found by codex adversarial
    review, round 5 (2026-08-29): `[]`, or a `phase`/`last_terminal_height`
    of the wrong type, parses as perfectly valid JSON and used to be
    returned unchanged, only to crash `run_forever`'s own startup
    (`seed.get(...)` on a list, or a height comparison against a string) --
    turning "restart evidence this module treats as non-authoritative" into
    something that could stop the unattended loop from starting at all. A
    state file this module never wrote in this shape is exactly as useless
    as one that failed to parse, and gets the identical, harmless fallback.
    `refused_heights` (added round 10, see `tick`'s own docstring) gets the
    same treatment: present but not a list of plain ints discards the
    whole seed, the same safe fallback as every other field here.
    `last_attempted_tempo` (added round 13) gets the identical treatment to
    `last_terminal_height` itself: present but not a plain int discards the
    whole seed. `day_catchup_pending` (issue #244, see `_write_state`'s own
    docstring and this module's top docstring, "Day catch-up") gets the
    identical treatment: present but not either `None` or a `{"day": int,
    "height": int}` dict discards the whole seed, the same safe fallback
    every other field here already gets -- losing it costs at most one
    needless extra ordinary refusal before this loop rediscovers the same
    pending day on its own, never a correctness decision.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        seed = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 -- see docstring: not authoritative
        return None
    if not isinstance(seed, dict):
        return None
    if not isinstance(seed.get("phase"), str):
        return None
    terminal_height = seed.get("last_terminal_height")
    if terminal_height is not None and (
        not isinstance(terminal_height, int) or isinstance(terminal_height, bool)
    ):
        return None
    if seed.get("identity") is not None and not isinstance(seed.get("identity"), str):
        return None
    refused_heights = seed.get("refused_heights")
    if refused_heights is not None and (
        not isinstance(refused_heights, list)
        or not all(isinstance(h, int) and not isinstance(h, bool) for h in refused_heights)
    ):
        return None
    last_attempted_tempo = seed.get("last_attempted_tempo")
    if last_attempted_tempo is not None and (
        not isinstance(last_attempted_tempo, int) or isinstance(last_attempted_tempo, bool)
    ):
        return None
    day_catchup_pending = seed.get("day_catchup_pending")
    if day_catchup_pending is not None and (
        not isinstance(day_catchup_pending, dict)
        or set(day_catchup_pending.keys()) != {"day", "height"}
        or not isinstance(day_catchup_pending.get("day"), int)
        or isinstance(day_catchup_pending.get("day"), bool)
        or not isinstance(day_catchup_pending.get("height"), int)
        or isinstance(day_catchup_pending.get("height"), bool)
    ):
        return None
    return seed


def _attempt_day_catchup(
    chain,
    pending,
    *,
    state_path,
    lock_path,
    identity,
    refused_heights,
    day_catchup,
    last_attempted_height,
    last_attempted_tempo,
    tempo,
):
    """Retry `run_period` against `pending`'s own remembered
    ``{"day", "height"}`` -- issue #244, see this module's own top
    docstring, "Day catch-up: any day's own last chance, not only Day 0's".
    Returns the same ``(TickResult, last_attempted_height)`` shape `tick`
    itself returns; `tick` returns this call's own result directly rather
    than falling through to its own ordinary `pinned`/`_terminal_gate`/lock
    flow.

    Deliberately bypasses `_terminal_gate` entirely (unlike Day 0
    catch-up's own target, which still flows through it -- see that
    function's own docstring): `pending["height"]` is, by construction,
    EARLIER than `last_attempted_height` by the time this runs (ordinary
    `pin_height` has already moved on to a later period, which is exactly
    what makes this call happen at all), and `_terminal_gate`'s own
    reorg-suspicion check would misread a genuine, deliberate retry of an
    earlier height as the chain having moved backward. `_FixedHeightChain`
    is what makes `run_period` act on `pending["height"]` at all, without
    touching `run_period`'s own settled interface -- the identical
    single-method composition `_DayZeroAwareChain` already establishes for
    Day 0's own target.

    `last_attempted_height` is left completely UNCHANGED on every outcome
    except a genuine publish/already-published (mirroring Day 0 catch-up's
    own refusal treatment exactly, for the identical reason: this height has
    no later period that supersedes it on its own, so advancing past it
    here would be the ONE bookkeeping change able to lose it for good). The
    throttle that keeps this from retrying every poll interval instead of
    once per period lives in `tick`'s own caller-side check (`pinned !=
    pending["height"]`), not here.
    """
    height = pending["height"]
    day = pending["day"]
    period = period_of(height=height, tempo=tempo)
    fixed_chain = _FixedHeightChain(chain, height)
    _write_state(
        state_path,
        {
            "phase": "day_catchup_attempting",
            "period": period,
            "height": height,
            "day": day,
            "last_terminal_height": last_attempted_height,
            "last_attempted_tempo": last_attempted_tempo,
            "at": _now(),
        },
        identity=identity,
        refused_heights=refused_heights,
        day_catchup_pending=pending,
    )
    try:
        with held(lock_path):
            result = run_period(fixed_chain)
    except LockHeld as error:
        # Transient -- another writer holds the lock this one tick; retried
        # next time this is due, exactly like an ordinary `lock_busy` tick.
        detail = f"day {day}'s own catch-up at height {height}: {error}"
        _write_state(
            state_path,
            {
                "phase": "day_catchup_lock_busy",
                "period": period,
                "height": height,
                "day": day,
                "last_terminal_height": last_attempted_height,
                "last_attempted_tempo": last_attempted_tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
            day_catchup_pending=pending,
        )
        return (
            TickResult(
                acted=False,
                period=period,
                outcome="day_catchup_lock_busy",
                detail=detail,
                last_attempted_tempo=last_attempted_tempo,
            ),
            last_attempted_height,
        )
    except (HeightNotPinned, ChainUnreadable) as error:
        # A transient read problem, not evidence about the day itself --
        # the identical "waiting" treatment `tick`'s own ordinary flow
        # already gives this pair. Pending is left exactly as it was.
        detail = f"day {day}'s own catch-up at height {height}: {type(error).__name__}: {error}"
        _write_state(
            state_path,
            {
                "phase": "day_catchup_read_failed",
                "period": period,
                "height": height,
                "day": day,
                "last_terminal_height": last_attempted_height,
                "last_attempted_tempo": last_attempted_tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
            day_catchup_pending=pending,
        )
        return (
            TickResult(
                acted=False,
                period=period,
                outcome="waiting",
                severity="error" if isinstance(error, PublicationStatusUnknown) else "warning",
                detail=detail,
                last_attempted_tempo=last_attempted_tempo,
            ),
            last_attempted_height,
        )
    except AlreadyPublished as error:
        # Day D's own recovery: published some other way (a manual
        # recovery, #212's own delayed-publish detection, or a different
        # run of this same loop) before this retry got to it. Resolved --
        # cleared, loud, and terminal in exactly the same shape a genuine
        # publish below is.
        day_catchup["pending"] = None
        day_catchup["checked_pinned"] = None
        detail = f"day {day}'s own catch-up at height {height}: {error}"
        _write_state(
            state_path,
            {
                "phase": "day_catchup_already_published",
                "period": period,
                "height": height,
                "day": day,
                "last_terminal_height": height,
                "last_attempted_tempo": tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
        )
        return (
            TickResult(
                acted=True,
                period=period,
                outcome="day_catchup_already_published",
                detail=detail,
                last_attempted_tempo=tempo,
            ),
            height,
        )
    except PERIOD_REFUSALS as error:
        day_not_complete = _day_not_complete_cause(error)
        unpriced = _foreign_unpriced_cause(error)
        if day_not_complete is not None or unpriced is not None:
            # Day D genuinely still has not ended -- a day can span many
            # tempo periods, so more than one can legitimately come and go
            # before it closes. Left pending, UNCHANGED (the identical
            # height, not the one `pin_height` just produced): retried
            # again the next time `pin_height`'s own output moves again.
            detail = (
                f"day {day}'s own catch-up attempt at height {height} was refused again "
                f"({type(error).__name__}: {error}); "
                + ("its exact-block foreign price is still unavailable. " if unpriced
                   else f"day {day} still has not ended. ")
                + "The original day remains pending and will be retried automatically; "
                "no later price or later credit day is substituted."
            )
            _write_state(
                state_path,
                {
                    "phase": "day_catchup_pending",
                    "period": period,
                    "height": height,
                    "day": day,
                    "last_terminal_height": last_attempted_height,
                    "last_attempted_tempo": last_attempted_tempo,
                    "reason": detail,
                    "at": _now(),
                },
                identity=identity,
                refused_heights=refused_heights,
                day_catchup_pending=pending,
            )
            return (
                TickResult(
                    acted=True,
                    period=period,
                    outcome="day_catchup_pending",
                    detail=detail,
                    last_attempted_tempo=last_attempted_tempo,
                ),
                last_attempted_height,
            )
        # A real, different reason -- this day cannot be recovered
        # automatically. Loud, not silent: named explicitly (matching
        # `_gap_note`'s own "GAP: N periods never individually attempted"
        # precedent for the identical class of concern -- something this
        # loop could not do on its own, said plainly rather than dropped),
        # and added to `refused_heights` so it is not retried forever.
        refused_heights.add(height)
        day_catchup["pending"] = None
        day_catchup["checked_pinned"] = None
        detail = (
            f"DAY SKIPPED: day {day}'s own catch-up attempt at height {height} was refused "
            f"for a reason that will not resolve itself ({type(error).__name__}: {error}); "
            f"day {day} will NOT be attempted again automatically. Manual recovery required "
            f"-- the same shape issue #212's delayed-recovery detection already exists for."
        )
        _write_state(
            state_path,
            {
                "phase": "day_catchup_abandoned",
                "period": period,
                "height": height,
                "day": day,
                "last_terminal_height": last_attempted_height,
                "last_attempted_tempo": last_attempted_tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
        )
        return (
            TickResult(
                acted=True,
                period=period,
                outcome="day_catchup_abandoned",
                detail=detail,
                last_attempted_tempo=last_attempted_tempo,
            ),
            last_attempted_height,
        )

    # Published. `result["height"]` is what `run_period` actually acted on
    # (its own fresh internal re-pin against `_FixedHeightChain`, which
    # always answers `height` -- see `_freshest_period`'s own reasoning for
    # why the AUTHORITATIVE result, not the plan, is what gets recorded).
    actual_height = result["height"]
    actual_period = period_of(height=actual_height, tempo=tempo)
    day_catchup["pending"] = None
    day_catchup["checked_pinned"] = None
    detail = f"day {day}'s own catch-up: period {actual_period} published at height {actual_height}"
    _write_state(
        state_path,
        {
            "phase": "done",
            "period": actual_period,
            "height": actual_height,
            "day": day,
            "last_terminal_height": actual_height,
            "last_attempted_tempo": tempo,
            "result": result,
            "at": _now(),
        },
        identity=identity,
        refused_heights=refused_heights,
    )
    return (
        TickResult(
            acted=True,
            period=actual_period,
            outcome="published",
            detail=detail,
            last_attempted_tempo=tempo,
        ),
        actual_height,
    )


def _tick_event_fields(result):
    # The tick result's fixed outcome and period are observations, never its detail text.
    tick_result, _height = result
    return {"outcome": tick_result.outcome, "period": tick_result.period,
            "severity": tick_result.log_severity}


@traced("validator", "validator.tick", fresh=True, result_fields=_tick_event_fields)
def tick(
    chain,
    *,
    settle_blocks,
    state_path,
    lock_path,
    last_attempted_height=None,
    last_attempted_tempo=None,
    refused_heights=None,
    identity=None,
    day_zero_cache=None,
    day_catchup=None,
):
    """One unit of work: check the boundary, act at most once if it is ready.

    ``day_catchup`` is threaded through exactly like ``day_zero_cache`` and
    ``refused_heights`` -- a plain mutable dict this call may write
    ``{"pending": {"day": ..., "height": ...}}`` into (or clear back to
    ``{"pending": None}``), not returned. See this module's own top
    docstring, "Day catch-up: any day's own last chance, not only Day 0's",
    for what it is and why it exists (issue #244). Left ``None`` (the
    default), a fresh, throwaway dict is used for this one call only, the
    identical unaffected-by-default pattern ``day_zero_cache`` and
    ``refused_heights`` already establish. ``run_forever`` always supplies a
    real dict, seeded from `read_state` at startup (unlike ``day_zero_cache``,
    which never is -- see that seeding's own comment for why day catch-up
    cannot rely on the identical cheap re-derivation Day 0 catch-up can) and
    threaded into every `tick` call across the whole run.

    ``day_zero_cache`` is threaded through exactly like ``refused_heights``
    (a plain mutable dict this call may write ``{"resolved": True}`` into,
    not returned) -- see `_day_zero_catch_up_height`'s own docstring and
    this module's own top docstring, "Day 0 catch-up", for what it is and
    why it exists. Left ``None`` (the default), a fresh, throwaway dict is
    used for this one call only, the identical unaffected-by-default
    pattern ``refused_heights`` already establishes. ``run_forever`` always
    supplies a real dict, created once and threaded into every `tick` call
    across the whole run.

    Returns ``(TickResult, new_last_attempted_height)``. The caller
    (`run_forever`) carries `new_last_attempted_height` into the next call;
    a fresh process seeds it from `read_state` instead of `None` (see that
    function's docstring).

    ``last_attempted_tempo`` is the tempo that was in effect the LAST time
    `last_attempted_height` was set -- the companion value needed to answer
    "is a backward pin explained by a real tempo change, or not" (see the
    reorg-suspicion check below). Carried forward the same way
    `last_attempted_height` itself is: unchanged on a branch that does not
    change that height, updated to this tick's own `tempo` on a branch that
    does. Exposed via `TickResult.last_attempted_tempo` rather than a third
    return-tuple element, for the identical reason `refused_heights` is a
    mutated-in-place input rather than an additional return value -- see
    that dataclass field's own comment.

    Found by codex adversarial review, round 13 (2026-08-29): under a
    CONSTANT tempo and a head that only ever grows, `pin_height`'s own
    output is monotonic (verified directly: a 5000-height sweep at a fixed
    tempo and settle_blocks never once produced a backward pin) -- but a
    plain chain-head ROLLBACK (a shallow reorg, or simply two reads of
    "current head" from sources that briefly disagree), with NO tempo
    change at all, CAN still make `pin_height` jump backward across a
    tempo-period boundary. codex's own worked example: `pin_height(head=40,
    tempo=20, settle_blocks=1)` is 39, and `pin_height(head=39, tempo=20,
    settle_blocks=1)` -- one block earlier, same tempo -- is 19, a jump back
    of a full tempo. `refused_heights` and the exact-equality skip-gate
    cannot help here: the resurfaced height (19) was never attempted before
    at all, so neither remembers it, and `already_published` legitimately
    says False, because it genuinely was never published. A `pinned` value
    LOWER than `last_attempted_height`, under an UNCHANGED tempo, has no
    other honest explanation than the head itself having moved backward --
    unlike the ordinary case (tempo actually changed), this is not a
    legitimate new period to attempt; it is treated as "waiting" (see the
    check below), the same non-terminal, try-again-next-tick response this
    module already gives to every other read-ambiguity case
    (`HeightNotPinned`, a wrapped `ChainUnreadable`). `last_attempted_tempo`
    being `None` (first-ever attempt, or a caller that does not track it)
    disables the check entirely -- a validator's OWN first observation of
    the chain naming whatever is currently the latest settled period is not
    suspicious; there is no prior tempo to compare against.

    Unlike `refused_heights` (round 10, migrated forward from a legacy
    state file's `phase`/`last_terminal_height` -- see `run_forever`'s own
    migration comment), a state file written before THIS field existed has
    no reliable value to recover `last_attempted_tempo` FROM: there is
    nothing else in an old file that says what tempo was actually in effect
    when `last_terminal_height` was recorded, and guessing the CURRENT
    tempo would be actively wrong if the subnet's tempo had already changed
    since that old write. So there is no migration here, deliberately: the
    very first tick after upgrading (or after any restart against a state
    file predating this field) seeds `None` and runs with the
    reorg-suspicion check disabled for exactly that one tick, self-healing
    from the SECOND tick on once this run has recorded its own real value.
    Every OTHER protection (`already_published`, the exact-equality
    skip-gate, `refused_heights`) is fully active the entire time; only
    this one, narrowest check has a one-tick bootstrap gap.

    ``refused_heights``, when given, is a MUTABLE ``set`` this call may add
    to in place -- not returned, because Python's own pass-by-reference for
    a mutable object already carries the update back to the caller without
    growing `tick`'s return arity, which every one of this module's own
    tests (and any real caller) already destructures as a 2-tuple. Left
    `None` (the default), a fresh, throwaway set is used for this one call
    only -- unaffected behaviour for a caller that does not care about the
    refusal-memory protection below (most direct calls in this module's own
    test suite, which test one tick in isolation). `run_forever` always
    supplies a real set, seeded from `read_state` at startup and threaded
    into every `tick` call across the whole run, so the protection actually
    applies in production and across any single continuous run.

    Found by codex adversarial review, round 10 (2026-08-29), on the
    skip-gate's own exact-equality comparison (see below): equality only
    ever remembers the SINGLE MOST RECENT terminal height, not every height
    this run has already given a terminal outcome to. If height 19 is
    REFUSED, then height 29 is later ALSO refused (`last_attempted_height`
    now 29, 19 no longer remembered anywhere), a subsequent tempo change
    that makes `pin_height` return 19 again passes the equality check
    (19 != 29) and falls through to `run_period` -- and `already_published`
    cannot catch this case the way it catches a republish of something that
    DID land, because 19 was never published, only refused. Without this
    set, a tempo change could silently retry a refusal `run_period`'s own
    docstring says explicitly must be an OPERATOR's decision, not this
    loop's own. `refused_heights` closes it: the skip-gate also checks
    membership in this set (see below), and every terminal REFUSAL adds its
    height to it before returning, so a later revisit of that SAME height
    -- by a tempo change or anything else -- is recognized and skipped
    exactly like the immediate-repeat case already was, rather than treated
    as a fresh, never-before-seen period.

    ``last_attempted_height`` is a PINNED (settled) height, not a period
    number -- found by codex adversarial review, round 6 (2026-08-29): a
    period number is `height // tempo`, and if the subnet's own tempo
    changes between two ticks, a LATER height can produce a SMALLER period
    number than an EARLIER one did under the old tempo. Comparing period
    numbers computed under different tempos is comparing two different
    units that happen to look like the same one, and the ORIGINAL version
    of this parameter did exactly that -- a tempo increase could make this
    loop believe periods it had never seen were "already attempted",
    silently stalling publication until the period-number arithmetic caught
    back up, which for a large enough tempo change could be a long time.

    Comparing PINNED heights directly is not fully safe either, though --
    codex adversarial CHALLENGE (2026-08-29, after review round 8) proved
    it with a real counterexample: `pin_height(head=31, tempo=10,
    settle_blocks=1)` is 29, and the SAME head under tempo 20 is 19 -- a
    tempo increase alone, no head movement at all, moved the pinned height
    BACKWARD. `pin_height`'s own output is a function of tempo, not purely
    of monotonic raw height, so an ordering comparison (`<`, `<=`) against
    it inherits the identical class of bug this parameter was renamed to
    fix, one level deeper. The skip-gate below therefore compares for EXACT
    EQUALITY, not ordering: "is this the identical pinned height as last
    time" needs no monotonicity assumption at all, and anything that
    DIFFERS -- forward, backward, or sideways from a tempo change --
    correctly falls through to `run_period`'s own `already_published`
    check, which answers from the chain, not from a remembered comparison.
    Period NUMBERS are still reported everywhere a human reads this loop's
    output (`TickResult.period`, the `"period"` state field) -- they are
    just derived from a height and the CURRENT tempo at the moment they are
    displayed, never stored or compared as the throttle itself.

    ``identity`` is an opaque string the CALLER supplies to say "this is
    which chain, and/or which signing identity, this state file is FOR" --
    written into every state write, alongside everything else, so
    `run_forever`'s own startup can tell a state file left by a genuinely
    different chain or hotkey from one genuinely written by this same
    setup. `tick` itself never reads or compares it; it only carries it
    through to `_write_state`, because the SEEDING decision (see
    `run_forever`'s own docstring) is the one place a mismatch actually
    matters. Left `None` (the default), no identity is ever recorded or
    checked -- unaffected behaviour for a stand-in chain, or a caller that
    does not have a meaningful identity to supply yet.
    """
    begin_validator_attempt(identity)
    if refused_heights is None:
        refused_heights = set()
    if day_zero_cache is None:
        day_zero_cache = {}
    if day_catchup is None:
        day_catchup = {}
    # A transient failure reading where the chain even IS right now is the
    # same shape of "nothing to act on this tick" as `pin_height` itself
    # already treats `chain.pinned_height()` failing (see `period.py` step 1,
    # `except Exception` there for the identical reason): this is the
    # loop's own FIRST read of the tick, nothing else has run yet, and an
    # unattended process that dies on one dropped connection instead of
    # trying again next poll would not be unattended for long. A genuine bug
    # in `chain` itself looks the same as a real transport hiccup from here
    # -- neither is distinguishable at this one opaque boundary -- so this
    # is deliberately as broad as `pin_height`'s own equivalent catch, not
    # narrower.
    try:
        head = chain.current_block()
        tempo = chain.tempo()
    except Exception as error:  # noqa: BLE001 -- see comment above
        emit("validator", "eligibility.state", "validator.eligibility", outcome="waiting",
             reason_code="head_unreadable", settle_blocks=settle_blocks, severity="warning")
        return (
            TickResult(
                acted=False,
                period=None,
                outcome="waiting",
                severity="warning",
                detail=(
                    f"could not read the chain's current height or tempo "
                    f"({type(error).__name__}: {error}); trying again next tick"
                ),
                last_attempted_tempo=last_attempted_tempo,
            ),
            last_attempted_height,
        )

    # Day 0 catch-up is asked FIRST, before `pin_height` -- see this
    # module's own top docstring, "Day 0 catch-up": it is the one case
    # `pin_height` can structurally never reach on its own (no settling
    # ever helps), so it is not a fallback for when `pin_height` refuses,
    # it is a genuinely separate readiness question, asked and answered the
    # same way every tick regardless of what `pin_height` itself would say.
    day_zero_target = _day_zero_catch_up_height(chain, tempo=tempo, day_zero_cache=day_zero_cache)
    if day_zero_target is not None:
        pinned = day_zero_target
    else:
        try:
            pinned = pin_height(head=head, tempo=tempo, settle_blocks=settle_blocks)
        except NoSettledPeriod as error:
            emit("validator", "eligibility.state", "validator.eligibility", outcome="waiting",
                 reason_code="not_settled", head_block=head, tempo=tempo,
                 settle_blocks=settle_blocks)
            return (
                TickResult(
                    acted=False,
                    period=None,
                    outcome="waiting",
                    detail=str(error),
                    last_attempted_tempo=last_attempted_tempo,
                ),
                last_attempted_height,
            )

        # Day catch-up (issue #244) is asked next, once `pin_height`'s own
        # fresh output for THIS tick is known -- see this module's own top
        # docstring, "Day catch-up: any day's own last chance, not only Day
        # 0's". Only engaged when a day is genuinely pending.
        pending = day_catchup.get("pending")
        if pending is not None:
            # The ordinary repeat/reorg-suspicion checks still run against
            # THIS tick's own fresh, natural candidate, even though a day
            # catch-up is pending -- found by an independent Verifier
            # (issue #244 follow-up): skipping them here meant a genuine
            # reorg during a pending window produced no explicit reorg
            # diagnostic at all, just an ordinary-looking stream of
            # `day_catchup_pending` messages. `_terminal_gate`'s own
            # reorg-suspicion check is exactly the thing that would have
            # caught it; running it here, against the natural
            # `pinned`/`period` (never against the pending day's own
            # height, which is deliberately allowed to be earlier -- see
            # `_attempt_day_catchup`'s own docstring for why THAT bypasses
            # this same gate), costs nothing extra in the ordinary case
            # (see below) and restores the diagnostic when something is
            # genuinely wrong.
            natural_period = period_of(height=pinned, tempo=tempo)
            gate_result = _terminal_gate(
                pinned,
                natural_period,
                last_attempted_height=last_attempted_height,
                last_attempted_tempo=last_attempted_tempo,
                tempo=tempo,
                refused_heights=refused_heights,
            )
            # An open day's initial refusal records this exact height as the
            # last attempt. Only that benign equality can be revisited early;
            # rollback, real refusals and other terminal gates still stop us.
            pending_equality = (
                pinned == last_attempted_height == pending["height"]
                and pinned not in refused_heights
            )
            if gate_result is not None and not pending_equality:
                return (gate_result, last_attempted_height)

            # Ordinary attempts still run once per new pinned period (#244).
            # Between those transitions, a five-minute finalized readiness
            # probe can release a day that has now closed. It keeps the same
            # pending height and enters the same locked publication pipeline.
            # This avoids both the old every-poll full retry and an unnecessary
            # wait for another tempo period after UTC midnight.
            period_due = gate_result is None and pinned != day_catchup.get("checked_pinned")
            try:
                day_ready = not period_due and _pending_day_ready(chain, pending, day_catchup)
            except (Exception, Refusal) as error:
                return (
                    TickResult(
                        acted=False,
                        period=natural_period,
                        outcome="waiting",
                        severity="warning",
                        detail=(f"day {pending['day']} finalized readiness could not be read "
                                f"({type(error).__name__}: {error}); retrying after the "
                                "five-minute readiness interval"),
                        last_attempted_tempo=last_attempted_tempo,
                    ),
                    last_attempted_height,
                )
            if period_due or day_ready:
                day_catchup["checked_pinned"] = pinned
                return _attempt_day_catchup(
                    chain,
                    pending,
                    state_path=state_path,
                    lock_path=lock_path,
                    identity=identity,
                    refused_heights=refused_heights,
                    day_catchup=day_catchup,
                    last_attempted_height=last_attempted_height,
                    last_attempted_tempo=last_attempted_tempo,
                    tempo=tempo,
                )

            # Neither the ordinary period cadence nor the finalized-day
            # readiness check calls for a full attempt. No lock/scan/send.
            return (
                TickResult(
                    acted=False,
                    period=natural_period,
                    outcome="day_catchup_pending",
                    detail=(
                        f"day {pending['day']}'s own catch-up at height {pending['height']} "
                        f"is still pending; waiting for finalized day readiness "
                        f"(checked every five minutes) or a new eligible period"
                    ),
                    last_attempted_tempo=last_attempted_tempo,
                ),
                last_attempted_height,
            )

    # `active_chain` is `chain` itself for every ordinary tick (Day 0
    # catch-up not pending) -- `_DayZeroAwareChain.pinned_height()` falls
    # through to the real, unmodified `chain.pinned_height()` the instant
    # `_day_zero_catch_up_height` stops finding anything pending, so this
    # wrap costs nothing behaviourally once Day 0 (or a chain with no
    # day-boundary support at all) has resolved the question. See this
    # module's own top docstring, "Day 0 catch-up", for why the wrap
    # itself, not a change to `pinning.py` or `period.py`, is the
    # composition point.
    emit("validator", "eligibility.state", "validator.eligibility", outcome="observed",
         head_block=head, height=pinned, tempo=tempo, settle_blocks=settle_blocks)
    active_chain = _DayZeroAwareChain(chain, day_zero_cache)

    period = period_of(height=pinned, tempo=tempo)
    # EQUALITY, not `<=` -- found by codex adversarial CHALLENGE (2026-08-29,
    # the round after review round 8), with a verified counterexample:
    # pin_height(head=31, tempo=10, settle_blocks=1) is 29, and
    # pin_height(head=31, tempo=20, settle_blocks=1) -- the SAME, non-earlier
    # head, only a LARGER tempo -- is 19. `pin_height`'s own OUTPUT is not
    # purely a function of monotonic raw height; it also depends on tempo,
    # and a tempo INCREASE can make it go backward even though `head` itself
    # never does. Round 6's own `<=` comparison inherited exactly the bug it
    # was written to fix, one level deeper: it correctly stopped comparing
    # PERIOD NUMBERS across a tempo change, but still compared PINNED
    # HEIGHTS with an ordering operator that assumes they are monotonic,
    # which they are not once tempo changes. `==` needs no such assumption:
    # "is this the EXACT SAME settled height as last time" is well-defined
    # regardless of how either value was computed, and skips ONLY the
    # narrow, safe case (nothing has changed since the last attempt) rather
    # than the broad, unsafe one (anything numerically not-later). Any
    # height that differs from the remembered one -- forward, backward, or
    # a genuine tempo-driven jump -- now falls through to `run_period`,
    # whose own `already_published` check is the actually-authoritative,
    # tempo-independent answer to "was this specific height already
    # handled", not a local comparison against a remembered value. That
    # claim rests on `already_published(height)` (period.py step 2) being
    # HEIGHT-keyed, per its own settled contract -- codex adversarial
    # review, round 10 (2026-08-29), found this module's own TEST DOUBLE
    # (`SchedulableChain` in test_loop.py) was not actually faithful to
    # that: it tracked PERIOD numbers, re-derived at check time, which let
    # a tempo change fool it into treating an already-published height as
    # unpublished (a period number that happened to look unused). Fixed in
    # the test fixture, not here -- the settled interface was never wrong,
    # the test double modeling it was -- and
    # `ATempoChangeCannotRepublishAnAlreadyPublishedHeight` now proves the
    # fixed, height-keyed fixture actually refuses the republish this
    # comment already claimed was impossible.
    # Exact-repeat, reorg-suspicion, and already-refused -- see
    # `_terminal_gate`'s own docstring for all three. Asked here, against
    # this tick's own PRE-LOCK plan, and asked AGAIN below, against
    # `_freshest_period`'s fresher read, immediately before `run_period` is
    # actually called -- found necessary by codex adversarial review, round
    # 14 (2026-08-29): asking only once, here, left a real (if narrow)
    # window where a chain that moved between this point and the in-lock
    # refresh could reach `run_period` with a height these checks would
    # have caught had they been asked again.
    gate_result = _terminal_gate(
        pinned,
        period,
        last_attempted_height=last_attempted_height,
        last_attempted_tempo=last_attempted_tempo,
        tempo=tempo,
        refused_heights=refused_heights,
    )
    if gate_result is not None:
        return (gate_result, last_attempted_height)

    # The height this tick STARTED planning for, and the loop's own state
    # from before this tick began -- kept under its own name (not
    # overwritten by anything below) because the branch that actually
    # published can end up authoritative for a DIFFERENT height than this
    # one planned (see "actual_height" below), and the gap this tick reports
    # has to be measured from whichever height turns out to be real, not the
    # plan.
    before_attempt_height = last_attempted_height

    # A new period is ready. Persisted BEFORE anything irreversible happens,
    # so a crash between here and the outcome below leaves a restart-readable
    # trace of exactly what was in flight (see module docstring). Uses the
    # planned period/height -- the best information available before any
    # attempt has actually run.
    planned_gap, planned_gap_note = _gap_note(before_attempt_height, pinned, tempo)
    _write_state(
        state_path,
        {
            "phase": "attempting",
            "period": period,
            "height": pinned,
            "gap_before": planned_gap,
            "last_terminal_height": before_attempt_height,
            "last_attempted_tempo": last_attempted_tempo,
            "at": _now(),
        },
        identity=identity,
        refused_heights=refused_heights,
    )

    try:
        with held(lock_path):
            # Re-check the height/period one more time, now that the lock is
            # held, immediately before run_period's own first chain read --
            # narrows (does not eliminate; see "actual_period" below for the
            # residual gap even this cannot close) the window between this
            # tick's own plan and what run_period will actually act on.
            # Found necessary by codex adversarial review, round 3
            # (2026-08-29): without this, a REFUSAL or an already-published
            # event gets attributed to the stale pre-lock plan, which can
            # name a DIFFERENT period than the one run_period's own internal
            # pin actually failed on -- silently permitting an accidental
            # retry of a period that just failed (violating run_period's own
            # "does not retry" contract), rather than the period this tick's
            # bookkeeping believes it already gave up on. See
            # `_freshest_period`'s own docstring for why this does not
            # reopen #533's concern about a slow pre-submit check going
            # stale: it asks the identical question run_period's own first
            # step is about to ask anyway, one instruction earlier, not a
            # signing-adjacent trust check sitting between an ownership read
            # and a submit.
            # `tempo` itself is REASSIGNED here, not just `period`/`pinned`
            # -- round 15 (2026-08-29), see `_freshest_period`'s own
            # docstring for why a fresh height paired with a stale tempo is
            # worse than either alone. Every use of `tempo` below this
            # point (every remaining `_terminal_gate` call, every
            # `last_attempted_tempo` write, the final `actual_period`
            # computation) now sees the freshly re-read value, not the
            # tick's own top-of-tick one.
            tempo, period, pinned = _freshest_period(
                active_chain, fallback_tempo=tempo, fallback_period=period, fallback_pinned=pinned
            )
            # Asked again, against the FRESH read, not just the pre-lock
            # plan above -- found necessary by codex adversarial review,
            # round 14 (2026-08-29): see `_terminal_gate`'s own docstring
            # for the concrete scenario (a chain that moves between the
            # pre-lock plan and this refresh could otherwise still reach
            # `run_period` with a height these checks would have caught).
            # No state write here on a hit: the pre-lock "attempting" write
            # already left restart-evidence for the PLANNED height, and
            # nothing about to be attempted has changed if this fires.
            gate_result = _terminal_gate(
                pinned,
                period,
                last_attempted_height=last_attempted_height,
                last_attempted_tempo=last_attempted_tempo,
                tempo=tempo,
                refused_heights=refused_heights,
            )
            if gate_result is not None:
                return (gate_result, last_attempted_height)
            planned_gap, planned_gap_note = _gap_note(before_attempt_height, pinned, tempo)
            _write_state(
                state_path,
                {
                    "phase": "attempting",
                    "period": period,
                    "height": pinned,
                    "gap_before": planned_gap,
                    "last_terminal_height": before_attempt_height,
                    "last_attempted_tempo": last_attempted_tempo,
                    "at": _now(),
                },
                identity=identity,
                refused_heights=refused_heights,
            )
            result = run_period(active_chain)
    except LockHeld as error:
        detail = str(error) + planned_gap_note
        # Phase "lock_busy", deliberately NOT "refused": found by codex
        # adversarial review (2026-08-29) that reusing "refused" here made
        # this period look, to `run_forever`'s own restart-seeding (see that
        # function's docstring), exactly like a genuine scoring refusal --
        # seeding `last_attempted_height` to it on restart and skipping a period
        # that was never actually attempted, only found the lock busy for
        # one tick. "lock_busy" is deliberately absent from the seedable
        # phases below, so a restart right after this always re-tries.
        _write_state(
            state_path,
            {
                "phase": "lock_busy",
                "period": period,
                "height": pinned,
                "gap_before": planned_gap,
                "last_terminal_height": before_attempt_height,
                "last_attempted_tempo": last_attempted_tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
        )
        # Not attempted: another writer holds the lock, so this process
        # never entered run_period at all. last_attempted_height is left
        # unchanged, deliberately, so the very next tick tries again rather
        # than waiting for the next period boundary -- a busy lock is
        # transient, unlike a genuine scoring refusal.
        return (
            TickResult(
                acted=False,
                period=period,
                outcome="held_by_other_writer",
                detail=detail,
                last_attempted_tempo=last_attempted_tempo,
            ),
            last_attempted_height,
        )
    except (HeightNotPinned, ChainUnreadable) as error:
        # Deliberately treated as WAITING, not a genuine per-period refusal.
        #
        # HeightNotPinned -- found by codex adversarial review, round 2
        # (2026-08-29). `run_period`'s own step 1 wraps ANY exception from
        # `chain.pinned_height()` into HeightNotPinned (period.py, "except
        # Exception" there, on purpose, for the identical reason this loop's
        # own top-of-tick read is caught broadly too), so this is raised for
        # BOTH "genuinely nothing has settled yet" AND "the read itself
        # failed transiently" -- the same ambiguity this tick's own
        # `current_block`/`tempo` read already has to allow for.
        #
        # ChainUnreadable (noah.core.reader) -- found by codex adversarial
        # review, round 4 (2026-08-29). run_period calls
        # chain.already_published(), chain.live_uids(), chain.owner_uids(),
        # chain.fixture_hotkeys(), and chain.chain_hotkeys() with NO
        # try/except of its own around any of them (unlike chain.read/
        # chain.score/chain.agree/chain.publish, which it does wrap). This
        # codebase's own established reader (reader.py, used throughout
        # neurons/validator.py's gate() and survey()) raises exactly this
        # type for "a dropped connection, a timeout" -- explicitly NOT the
        # same fact as a bug in scoring itself, per that module's own
        # docstring -- and every other caller of it in this codebase treats
        # it as a clean, retryable refusal, never a crash.
        #
        # Treating either as a genuine per-period refusal (the ORIGINAL
        # version of this code did, via PERIOD_REFUSALS) would advance
        # `last_attempted_height` to `pinned` on a transient network hiccup that
        # scored and published NOTHING -- permanently losing the period the
        # moment the connection recovered, because `run_forever`'s own "do
        # not retry a refused period" discipline would then skip it forever.
        # No scoring or publication work had begun in either case, so there
        # is nothing here to protect by refusing to retry.
        detail = f"{type(error).__name__}: {error}"
        _write_state(
            state_path,
            {
                "phase": "read_failed",
                "period": period,
                "height": pinned,
                "last_terminal_height": before_attempt_height,
                "last_attempted_tempo": last_attempted_tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
        )
        return (
            TickResult(
                acted=False,
                period=period,
                outcome="waiting",
                severity="error" if isinstance(error, PublicationStatusUnknown) else "warning",
                detail=detail,
                last_attempted_tempo=last_attempted_tempo,
            ),
            last_attempted_height,
        )
    except AlreadyPublished as error:
        detail = str(error) + planned_gap_note
        _write_state(
            state_path,
            {
                "phase": "already_published",
                "period": period,
                "height": pinned,
                "gap_before": planned_gap,
                "last_terminal_height": pinned,
                "last_attempted_tempo": tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
        )
        return (
            TickResult(
                acted=True,
                period=period,
                outcome="already_published",
                detail=detail,
                last_attempted_tempo=tempo,
            ),
            pinned,
        )
    except PERIOD_REFUSALS as error:
        # A PeriodRefused WRAPPING a ChainUnreadable is transient, not a
        # genuine per-period refusal -- found by codex adversarial CHALLENGE
        # (2026-08-29, after review round 8). `run_period`'s own
        # chain.read/chain.score/chain.agree/chain.publish calls each catch
        # `except Exception as error: raise PeriodRefused(...) from error`
        # (period.py, its own settled step 3/4/5/6), so a `ChainUnreadable`
        # from any of THOSE four calls arrives here already re-wrapped as
        # `PeriodRefused` -- it can NEVER reach the dedicated
        # `except (HeightNotPinned, ChainUnreadable)` clause above, which
        # only ever sees a `ChainUnreadable` raised directly, unwrapped, by
        # one of run_period's UNWRAPPED calls. Without this check, a plain
        # dropped connection during `chain.read()` (a real, substantial
        # chain query, more exposed to a transient timeout than most of the
        # other calls) was treated as a genuine, permanent refusal --
        # advancing `last_terminal_height` and losing the period forever the
        # moment the connection recovered, exactly the failure this whole
        # module's "waiting, not a crash" treatment exists to prevent.
        # `from error` (Python's own exception chaining, used by every one
        # of period.py's own four wrapping sites) is what makes this
        # detectable at all: `error.__cause__` is the ORIGINAL, unwrapped
        # exception, not a guess reconstructed from the message text.
        #
        # NOT every wrapped ChainUnreadable is safe to retry, though -- found
        # by codex adversarial review, round 10 (2026-08-29), on the fix
        # above itself. period.py's own step 6 (`chain.publish`) wraps a
        # failure identically to steps 3-5 (`chain.read`/`chain.score`/
        # `chain.agree`), but its docstring says PLAINLY why a publish
        # failure must NOT auto-retry: "a period that failed failed for a
        # reason, and retrying by default turns one diagnosable failure into
        # a rate-limit exhaustion with no better explanation" -- and, more
        # dangerously for THIS transport-failure case specifically, a
        # `ChainUnreadable` from `chain.publish` can mean the broadcast
        # itself SUCCEEDED and only the confirmation read after it failed;
        # blindly retrying would risk a second submission for a period the
        # first attempt may have already landed. Steps 3-5 carry no such
        # risk -- nothing is submitted until step 6 is reached at all, so a
        # transport failure there has genuinely touched nothing to protect.
        # `_is_pre_publish_read_failure` tells the two apart by matching
        # against period.py's own settled step 3/4/5 message wording (the
        # only signal available without changing that settled interface to
        # carry a structured "which step" marker, which is out of this
        # issue's scope): an unrecognized message -- including step 6's own
        # wording, or any future wording change to any step -- falls to the
        # SAFE default, terminal refusal, exactly as if this whole check did
        # not exist. This fix can only ever make MORE cases safely terminal
        # than a naive "any ChainUnreadable is transient" rule would, never
        # fewer.
        if isinstance(
            getattr(error, "__cause__", None), ChainUnreadable
        ) and _is_pre_publish_read_failure(error):
            detail = f"{type(error).__name__}: {error}"
            _write_state(
                state_path,
                {
                    "phase": "read_failed",
                    "period": period,
                    "height": pinned,
                    "last_terminal_height": before_attempt_height,
                    "last_attempted_tempo": last_attempted_tempo,
                    "reason": detail,
                    "at": _now(),
                },
                identity=identity,
                refused_heights=refused_heights,
            )
            return (
                TickResult(
                    acted=False,
                    period=period,
                    outcome="waiting",
                    severity="error" if isinstance(error, PublicationStatusUnknown) else "warning",
                    detail=detail,
                    last_attempted_tempo=last_attempted_tempo,
                ),
                last_attempted_height,
            )
        detail = f"{type(error).__name__}: {error}" + planned_gap_note
        # Day 0 catch-up's OWN refusal does not go through the rest of this
        # handler -- see this module's own top docstring, "Day 0 catch-up's
        # own refusal is deliberately NOT remembered this way", for the
        # full reasoning (a real, live gap found the first night this
        # shipped: `SeatOwnerUnknown` while the registration wave was still
        # settling, then a restart that reproduced the identical
        # permanently-stuck outcome because `refused_heights` and
        # `last_terminal_height` both remembered it the same way an
        # ordinary period's refusal is remembered).
        #
        # `pinned == day_zero_target` is the same safe, unambiguous
        # comparison used pre-lock to decide whether to even attempt this:
        # `day_zero_target` (captured once, before the lock) is fixed for
        # as long as Day 0 remains genuinely unpublished, and
        # `_day_zero_catch_up_height`'s own length check guarantees it can
        # never equal a real ordinary `pin_height` output (always >= `tempo
        # - 1`, strictly greater than any height this function returns) --
        # so this can only match Day 0's own catch-up attempt, never an
        # ordinary period that happens to share a number with it.
        if day_zero_target is not None and pinned == day_zero_target:
            day_zero_detail = (
                f"Day 0 catch-up attempt at height {pinned} was refused "
                f"({type(error).__name__}: {error}); unlike an ordinary period, Day 0 "
                f"has no later period that supersedes it, so this is not remembered as "
                f"permanent -- it retries next tick (and across a restart) rather than "
                f"waiting on a human"
            )
            _write_state(
                state_path,
                {
                    "phase": "day_zero_catch_up_refused",
                    "period": period,
                    "height": pinned,
                    "gap_before": planned_gap,
                    "last_terminal_height": before_attempt_height,
                    "last_attempted_tempo": last_attempted_tempo,
                    "reason": day_zero_detail,
                    "at": _now(),
                },
                identity=identity,
                refused_heights=refused_heights,
            )
            return (
                TickResult(
                    acted=True,
                    period=period,
                    outcome="day_zero_catch_up_refused",
                    detail=day_zero_detail,
                    last_attempted_tempo=last_attempted_tempo,
                ),
                last_attempted_height,
            )
        # Day catch-up (issue #244): a `DayNotComplete` refusal is
        # transient, not terminal -- see this module's own top docstring,
        # "Day catch-up: any day's own last chance, not only Day 0's". This
        # SPECIFIC height might turn out, once `pin_height`'s own next
        # output moves past it, to have been the one chance its own day
        # ever gets -- so it is remembered for day catch-up to retry
        # instead of being permanently blacklisted below.
        # `last_attempted_height` still advances to `pinned`, exactly like
        # every ordinary refusal: the day's own remaining, still-open
        # periods (if any) are each still attempted exactly once, at the
        # same cadence as before this fix -- only the permanent blacklist
        # is skipped. Falls through to the ordinary, permanent handling
        # below on the defensive case `day_not_complete.day` is somehow
        # `None` (a `DayNotComplete` constructed without it, which nothing
        # in this codebase does today -- see that exception's own
        # docstring): with no day number to remember, there is nothing safe
        # to retry later, so the existing, always-safe blacklist is the
        # right fallback rather than silently dropping the refusal.
        day_not_complete = _day_not_complete_cause(error)
        retry_cause = day_not_complete or _foreign_unpriced_cause(error)
        retry_day = getattr(retry_cause, "day", None)
        if type(retry_day) is int:
            day_catchup["pending"] = {"day": retry_day, "height": pinned}
            # Seeded to THIS tick's own `pinned` -- the value that just
            # triggered this refusal -- so the very next tick (almost
            # always still the same period) does not immediately re-check
            # it; see the `checked_pinned` throttle's own comment above
            # for the full reasoning.
            day_catchup["checked_pinned"] = pinned
            _write_state(
                state_path,
                {
                    "phase": "refused",
                    "period": period,
                    "height": pinned,
                    "gap_before": planned_gap,
                    "last_terminal_height": pinned,
                    "last_attempted_tempo": tempo,
                    "reason": detail,
                    "at": _now(),
                },
                identity=identity,
                refused_heights=refused_heights,
                day_catchup_pending=day_catchup["pending"],
            )
            return (
                TickResult(
                    acted=True,
                    period=period,
                    outcome="refused",
                    severity="info" if day_not_complete is not None else "warning",
                    detail=detail,
                    last_attempted_tempo=tempo,
                ),
                pinned,
            )
        # Remembered BEFORE the write below, so the persisted state and the
        # in-memory set agree from this instant on -- the whole point of
        # `refused_heights` (see `tick`'s own docstring) is that a LATER
        # tick, possibly after a tempo change resurfaces this exact height,
        # can see it was already given up on here rather than treating it
        # as new. Round 10 (2026-08-29).
        refused_heights.add(pinned)
        _write_state(
            state_path,
            {
                "phase": "refused",
                "period": period,
                "height": pinned,
                "gap_before": planned_gap,
                "last_terminal_height": pinned,
                "last_attempted_tempo": tempo,
                "reason": detail,
                "at": _now(),
            },
            identity=identity,
            refused_heights=refused_heights,
        )
        return (
            TickResult(
                acted=True,
                period=period,
                outcome="refused",
                detail=detail,
                last_attempted_tempo=tempo,
            ),
            pinned,
        )

    # `run_period` re-derives ITS OWN pinned height independently
    # (`chain.pinned_height()`, its own settled step 1) rather than trusting
    # the value this tick precomputed above. Almost always these agree --
    # the only work in between is a fast local lock acquisition -- but if
    # the chain crosses a period boundary in that gap (plausible with a
    # slow chain read on a real adapter), `result["height"]` names what
    # ACTUALLY got scored and published, which can differ from `period`/
    # `pinned` above. Found by codex adversarial review (2026-08-29):
    # trusting this tick's own stale precomputed value here would mislabel
    # the state file and `last_attempted_height` with a period that was never
    # really published, while the period that WAS published goes unrecorded
    # under its own real number. The AUTHORITATIVE result is used for every
    # record from here on, not the plan this tick started with -- INCLUDING
    # the gap count: round 2 of the same review found that recording
    # `actual_period` alone still under-counted a gap that drift itself
    # caused (the ORIGINAL planned period silently lost when the real
    # outcome lands further ahead than planned), because the gap was still
    # being measured against the STALE `period`. Measuring it against
    # `actual_period` instead makes the one published-outcome number
    # correct even when drift occurred.
    actual_height = result["height"]
    actual_period = period_of(height=actual_height, tempo=tempo)
    actual_gap, actual_gap_note = _gap_note(before_attempt_height, actual_height, tempo)
    drift_note = (
        f" DRIFT: this tick planned for period {period} at height {pinned}; run_period's "
        f"own fresh pin actually scored period {actual_period} at height {actual_height} -- "
        f"the chain crossed a boundary between this tick's read and run_period's. Recorded "
        f"as {actual_period}, the real one."
        if actual_period != period
        else ""
    )
    _write_state(
        state_path,
        {
            "phase": "done",
            "period": actual_period,
            "height": actual_height,
            "gap_before": actual_gap,
            "last_terminal_height": actual_height,
            "last_attempted_tempo": tempo,
            "result": result,
            # Retain the compatibility field. The live adapter permits multiple
            # seats per coldkey and returns an empty dropped-duplicates list.
            "dropped_duplicate_coldkeys": getattr(
                chain, "dropped_duplicate_coldkeys", lambda: []
            )(),
            # Ticket #16 (B06), Codex finding F1: the same observable-record
            # treatment as dropped_duplicate_coldkeys immediately above, for
            # whichever hotkeys this period's own read() excluded for lack
            # of a confirmed 100-NOAH entry deposit (Rule 1, "entry before
            # score"). Same getattr-with-a-safe-default reasoning.
            "dropped_unconfirmed_entry_deposits": getattr(
                chain, "dropped_unconfirmed_entry_deposits", lambda: []
            )(),
            "at": _now(),
        },
        identity=identity,
        refused_heights=refused_heights,
    )
    return (
        TickResult(
            acted=True,
            period=actual_period,
            outcome="published",
            detail=(
                f"period {actual_period} published at height {actual_height}"
                + actual_gap_note
                + drift_note
            ),
            last_attempted_tempo=tempo,
        ),
        actual_height,
    )


def run_forever(
    chain,
    *,
    settle_blocks,
    state_path,
    lock_path,
    identity=None,
    poll_seconds=30,
    sleep=time.sleep,
    iterations=None,
    on_tick=None,
):
    """Wake on a fixed interval, act when a boundary is ready, forever.

    ``iterations`` bounds the run to that many ticks and returns instead of
    looping forever -- used only by tests; real callers leave it as ``None``
    and this runs until the process is stopped.

    Startup reads the persisted state file to seed ``last_attempted_height``
    (see ``read_state``'s own docstring for why that is a throttle, not a
    correctness guarantee): a fresh process does not immediately re-attempt
    a period whose refusal this same loop already recorded moments before a
    restart. It has no effect on whether double-publishing is possible;
    that is `run_period`'s own `already_published` check, unaffected by
    anything this file remembers.

    Seeded from ``last_terminal_height``, not a period number -- found by
    codex adversarial review, round 5 (crash mid-attempt losing the
    baseline) and round 6 (a bare period number is unsafe to compare across
    a tempo change), both 2026-08-29. Every state write (`tick`'s own
    `_write_state` calls), terminal or not, carries the most recent HEIGHT
    this loop KNOWS it terminally handled, precisely so a crash mid-attempt
    (``attempting``, ``read_failed``, ``lock_busy`` -- none of them
    terminal) does not lose that baseline, and so the seed stays comparable
    even if the subnet's own tempo changed since it was written (see
    `tick`'s own docstring for why height, not a period number, is what
    this parameter and the persisted field both actually carry).

    ``identity`` guards against a DIFFERENT hazard found by codex
    adversarial review, round 7 (2026-08-29): `state_path` reused across a
    local-chain rebuild ("gets a new identity every rebuild", per this
    project's own established knowledge -- see `settings.py`'s
    `local_genesis`), a network switch, or a changed signing hotkey. None
    of those change what `last_terminal_height` LOOKS like -- it is still
    a plain integer -- but the height now means something different, or
    nothing this loop has ever actually seen before. Seeding from it
    blindly could, in the worst case (the new chain's real heights are
    still below the stale seed), make this loop believe every genuinely
    new period is "already attempted" and stall indefinitely. If `identity`
    is supplied AND the persisted state names a DIFFERENT, non-null
    identity, the seed is discarded (treated as no prior state, the same
    safe fallback `read_state` already uses for corrupt or malformed
    files) and the mismatch is reported loudly, by name, rather than acted
    on silently. Left `None` (the default), this check does not run at
    all -- unaffected behaviour for any caller with no meaningful identity
    to supply yet.
    """
    if poll_seconds <= 0:
        raise LoopRefused(
            f"poll_seconds must be positive, and it is {poll_seconds!r}. A non-positive "
            f"interval would spin the loop against the chain with no pacing at all."
        )

    seed = read_state(state_path)
    seed_identity = seed.get("identity") if seed else None
    # `seed_identity != identity` alone, not also requiring `seed_identity
    # is not None` -- found by codex adversarial review, round 8
    # (2026-08-29): the round 7 version trusted an UNLABELED old state file
    # (written before this run ever supplied an identity, or by an older
    # version of this code) even when the CURRENT run has a real identity
    # to check against, because `None is not None` is False and the whole
    # mismatch branch was skipped. An unlabeled seed is exactly as
    # unverifiable as a labeled-but-different one once this run actually
    # HAS an identity to check against -- both get the identical, safe
    # "discard rather than trust blindly" treatment. The check still only
    # runs at all when THIS run supplies an identity (`identity is not
    # None`); a caller with nothing meaningful to supply is unaffected,
    # exactly as before.
    if identity is not None and seed_identity != identity:
        diagnostic(
            f"[loop] IDENTITY MISMATCH: {plain_path_text(state_path)} was last written for "
            f"{seed_identity!r}, "
            f"this run is {identity!r}. Discarding its last_terminal_height rather than risk "
            f"stalling on a height that means something else now (a local chain rebuild, a "
            f"network switch, or a changed signing hotkey are the usual causes).",
            severity="warning",
        )
        last_attempted_height = None
        last_attempted_tempo = None
        refused_heights = set()
        # Same mismatch/discard treatment as `refused_heights` right above,
        # for the identical reason -- issue #244, see `_write_state`'s own
        # docstring for `day_catchup_pending` and this module's top
        # docstring, "Day catch-up".
        day_catchup = {"pending": None}
    else:
        last_attempted_height = seed.get("last_terminal_height") if seed else None
        # Same mismatch/discard treatment as `last_attempted_height` right
        # above, for the identical reason: a tempo recorded by a genuinely
        # different chain means nothing here either. Round 13 (2026-08-29).
        last_attempted_tempo = seed.get("last_attempted_tempo") if seed else None
        # Same mismatch/discard treatment as `last_attempted_height` right
        # above, for the same reason: a `refused_heights` list left by a
        # genuinely different chain/hotkey means nothing here, and trusting
        # it could make this loop believe a height it has never actually
        # seen was already refused. Round 10 (2026-08-29), added alongside
        # the set itself -- see `tick`'s own docstring for what it is for.
        refused_heights = set(seed.get("refused_heights", [])) if seed else set()
        # LEGACY MIGRATION -- found by codex adversarial review, round 12
        # (2026-08-29): a state file written by an EARLIER version of this
        # module (before `refused_heights` existed at all, or upgraded
        # mid-run from one that predates it) has no `"refused_heights"` key,
        # so the line above seeds an empty set even when the file's OWN
        # `last_terminal_height` already says, plainly, that SOME earlier
        # attempt reached a terminal outcome at that height.
        #
        # NOT gated on `phase == "refused"` -- sharpened by codex
        # adversarial review, round 14 (2026-08-29): `last_terminal_height`
        # is carried forward, UNCHANGED, through every NONTERMINAL write too
        # (`attempting`, `lock_busy`, `read_failed` -- this is round 5's own
        # fix, so it predates `refused_heights` and applies to legacy files
        # just as much as new ones). A legacy file's CURRENT phase can
        # therefore be nonterminal (a crash mid-attempt on a LATER period)
        # while `last_terminal_height` still names an EARLIER refusal the
        # phase-only check above would miss entirely. Retaining the height
        # unconditionally whenever `refused_heights` itself is absent costs
        # nothing when the guess is wrong (the height was actually
        # PUBLISHED, not refused): `refused_heights` is checked BEFORE
        # `already_published` in this module's own skip-gate ordering, so a
        # false-positive "treat a published height as refused" still
        # produces the identical safe outcome -- no rescoring, no republish
        # attempt -- merely a less precise diagnostic message where
        # `already_published` would have been slightly more informative.
        # The asymmetry only runs one way: missing a REAL refusal risks a
        # silent retry `refused_heights` exists to prevent; over-including
        # a published height risks nothing. A state file this run cannot
        # see as refused any other way is still readable right here, so it
        # is folded in rather than discarded.
        #
        # WHAT THIS MIGRATION CANNOT RECOVER, named rather than silently
        # accepted -- raised by codex adversarial review, round 17
        # (2026-08-29): a pre-round-10 file only ever remembered the SINGLE
        # MOST RECENT terminal height (that is all the code existing at the
        # time ever wrote down). If that legacy run refused height 19, THEN
        # LATER also refused a different height 29, the file on disk shows
        # only `last_terminal_height: 29` -- height 19's refusal was never
        # persisted anywhere, by any version of this code, and there is
        # nothing left to read it back FROM. If, after upgrading, a tempo
        # change ever resurfaces height 19 specifically, this run retries
        # it automatically -- a real, bounded violation of the
        # operator-only-retry contract, not a hypothetical one.
        #
        # The seemingly obvious fix -- fall back to an ORDERING comparison
        # ("treat anything <= the legacy high-water mark as off-limits")
        # for seeds from before this migration existed -- was considered
        # and rejected: it reintroduces the EXACT unsafe pattern round 9's
        # own challenge round eliminated for a documented, verified reason
        # (`pin_height`'s own output is not safely comparable by ordering
        # across a tempo change -- see the skip-gate's own `==`, not `<=`,
        # comment above). An ordering fallback would not just fail to help;
        # it could STALL a genuinely new, never-before-seen period whose
        # height merely LOOKS smaller than the legacy mark after a tempo
        # change, trading a narrow, rare, POLICY violation (an unwanted
        # auto-retry) for the exact WORSE, already-eliminated failure mode
        # (the validator silently stops publishing at all).
        #
        # So this is accepted, not built: bounded to files written before
        # `refused_heights` existed AND carrying more than one refusal in
        # that same legacy run AND a tempo change after upgrade that
        # resurfaces the SPECIFIC earlier one -- three conditions that must
        # all hold together, exactly once, only during the transition off
        # a pre-round-10 deployment. The consequence is `run_period`
        # re-attempting a period that already failed once, which its own
        # already_published/scoring path handles the same as any other
        # attempt (a genuine improvement republishes correctly; an
        # unchanged failure condition refuses again) -- not a double
        # publish, not data loss, not this loop losing track of anything it
        # ever actually recorded. A future session could close this
        # properly by giving `PeriodRefused` (and its siblings) a
        # structured height, the same underlying limitation named in "Why
        # the drift gap is narrowed, not closed" above -- out of this
        # issue's scope by the identical reasoning already given there.
        if seed and "refused_heights" not in seed and seed.get("last_terminal_height") is not None:
            refused_heights.add(seed["last_terminal_height"])

        # Seeded from the state file, unlike `day_zero_cache` below --
        # issue #244, see `tick`'s own docstring for `day_catchup` and
        # `_write_state`'s own docstring for why this one cannot rely on
        # the identical cheap re-derivation Day 0 catch-up can. A seed with
        # no `day_catchup_pending` key (a state file from before this fix,
        # or one whose last write genuinely had nothing pending) seeds
        # `None`, the ordinary, unaffected starting point.
        day_catchup = {"pending": seed.get("day_catchup_pending") if seed else None}

    # Purely an in-process cost cache (never persisted -- see
    # `_day_zero_catch_up_height`'s own docstring and this module's own top
    # docstring, "Day 0 catch-up"), created once and threaded into every
    # `tick` call across this run so the day-boundary search it may need
    # runs at most a handful of times per process, not every poll forever.
    day_zero_cache = {}

    count = 0
    while iterations is None or count < iterations:
        result, last_attempted_height = tick(
            chain,
            settle_blocks=settle_blocks,
            state_path=state_path,
            lock_path=lock_path,
            last_attempted_height=last_attempted_height,
            last_attempted_tempo=last_attempted_tempo,
            identity=identity,
            refused_heights=refused_heights,
            day_zero_cache=day_zero_cache,
            day_catchup=day_catchup,
        )
        # Carried forward exactly the way `last_attempted_height` itself
        # already is (see `TickResult.last_attempted_tempo`'s own comment
        # for why this travels on the result object instead of a third
        # return-tuple element). Round 13 (2026-08-29).
        last_attempted_tempo = result.last_attempted_tempo
        diagnostic(f"[loop] {result.outcome}: {result.detail}", severity=result.log_severity)
        if on_tick is not None:
            on_tick(result)
        count += 1
        if iterations is None or count < iterations:
            sleep(poll_seconds)
    return last_attempted_height
