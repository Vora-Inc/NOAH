"""Compare submitted publication vectors with their expected stored form.

The chain storage transform is modeled by rescale.py. Each supplied period must
be checked or explicitly reported as unchecked, and an empty run provides no
evidence. Callers supply the historical observations; these pure comparisons do
not establish that a live adapter actually collected them."""

from dataclasses import dataclass

from .rescale import predict_stored

FULL_SCALE = 65535


class NothingToCheck(ValueError):
    """A run with no periods in it, which cannot produce a meaningful verdict."""


@dataclass(frozen=True)
class PeriodResult:
    """One period's verdict, and why."""

    block: int
    ok: bool
    reason: str


@dataclass(frozen=True)
class Report:
    """What was checked, what failed, and what could not be checked at all."""

    checked: int
    unchecked: int
    blocks: tuple
    failures: tuple
    results: tuple

    @property
    def clean(self):
        """True only when something was actually checked and none of it failed."""
        return self.checked > 0 and not self.failures and self.unchecked == 0

    def summary(self):
        span = f"blocks {min(self.blocks)} to {max(self.blocks)}" if self.blocks else "no blocks"
        if not self.checked:
            return f"NOTHING CHECKED across {span}. This is not a pass."
        line = f"{self.checked} period(s) checked, {span}"
        if self.unchecked:
            line += f", {self.unchecked} could NOT be checked"
        if self.failures:
            return f"{line}. {len(self.failures)} FAILED: {self.failures[0].reason}"
        if self.unchecked:
            return f"{line}. No failures among those checked, which is not the same as none."
        return f"{line}. No exceptions."


def confirm_one(*, block, emitted, stored):
    """Check a single period's emitted vector and what the chain stored for it."""
    if emitted is None or stored is None:
        missing = "our own vector" if emitted is None else "the chain's stored vector"
        return PeriodResult(
            block=block,
            ok=False,
            reason=f"block {block}: {missing} could not be read, so this period is UNCHECKED. "
            f"It is counted as unchecked rather than passed, because a period nobody could "
            f"read is exactly the period a fault would hide in.",
        )

    total = sum(emitted)
    if total != FULL_SCALE:
        return PeriodResult(
            block=block,
            ok=False,
            reason=f"block {block}: our vector totals {total}, not {FULL_SCALE}. Below the "
            f"scale silently underpays the whole subnet for this period; above it the "
            f"chain refuses and the period is lost. Neither is recoverable afterwards.",
        )

    expected = list(predict_stored(emitted).stored)
    actual = list(stored)
    if actual != expected:
        first = next(
            (i for i in range(min(len(expected), len(actual))) if expected[i] != actual[i]),
            min(len(expected), len(actual)),
        )
        if len(expected) != len(actual):
            detail = f"the chain holds {len(actual)} weights and we sent {len(expected)}"
        else:
            detail = f"seat {first} holds {actual[first]} and should hold {expected[first]}"
        return PeriodResult(
            block=block,
            ok=False,
            reason=f"block {block}: what the chain stored is not the rescale of what we sent. "
            f"{detail}. This is compared against the rescale, not against our own numbers, "
            f"because the chain never stores our numbers unchanged.",
        )

    return PeriodResult(block=block, ok=True, reason=f"block {block}: as expected")


def confirm_run(periods):
    """Check every period in a run, and refuse to call an empty run clean.

    ``periods`` is a sequence of ``(block, emitted, stored)``. Either vector may
    be None to mean it could not be read, which is reported as unchecked rather
    than quietly dropped.
    """
    entries = list(periods)
    if not entries:
        raise NothingToCheck(
            "no periods were handed in, so there is nothing to report. An empty run cannot "
            "be clean: 'no exceptions' is true of a run that checked nothing, and a script "
            "that returns success on zero periods is the failure this is built to prevent. "
            "If the run really produced no periods, that is itself the finding."
        )

    results, failures, unchecked = [], [], 0
    for block, emitted, stored in entries:
        result = confirm_one(block=block, emitted=emitted, stored=stored)
        results.append(result)
        if result.ok:
            continue
        if emitted is None or stored is None:
            unchecked += 1
        else:
            failures.append(result)

    return Report(
        checked=sum(1 for r in results if r.ok) + len(failures),
        unchecked=unchecked,
        blocks=tuple(r.block for r in results),
        failures=tuple(failures),
        results=tuple(results),
    )


class PreviousRevealMismatch(Exception):
    """The last period's stored vector was not the rescale of what we sealed."""


@dataclass(frozen=True)
class PriorCheck:
    """Whether the previous period can be trusted, and whether to stop."""

    verified: bool
    halt: bool
    reason: str


def check_previous_reveal(chain):
    """Compare the previous publication with the chain's stored result when available.

    A proven mismatch requests a halt. An unavailable check is returned as unverified
    without a halt; callers must not report that as a pass. This function does not
    guarantee that a particular adapter implements the required historical read."""
    try:
        prior = chain.previous_period()
    except Exception as error:
        return PriorCheck(
            verified=False,
            halt=False,
            reason=f"the previous period could not be read ({type(error).__name__}: {error}), "
            f"so this publication is going out UNVERIFIED. That is not the same as verified: "
            f"nothing here says the last reveal was correct, only that nobody could look. "
            f"Publishing continues because a failed read is not evidence of a fault, and "
            f"stopping on one would take the whole subnet's pay to zero over a chain hiccup.",
        )

    if prior is None:
        return PriorCheck(
            verified=False,
            halt=False,
            reason="there is no previous period to check, which is expected on a first run "
            "and is reported rather than counted as a clean check.",
        )

    block, emitted, stored = prior
    result = confirm_one(block=block, emitted=emitted, stored=stored)
    if result.ok:
        return PriorCheck(verified=True, halt=False, reason=result.reason)

    if emitted is None or stored is None:
        return PriorCheck(
            verified=False,
            halt=False,
            reason=f"{result.reason} Publishing continues UNVERIFIED rather than halting, "
            f"because a period nobody could read is not a period that went wrong.",
        )

    return PriorCheck(
        verified=False,
        halt=True,
        reason=f"{result.reason} PUBLISHING STOPS HERE. The chain is not holding the rescale "
        f"of what we sealed, so what the next publication would do is unknown. Be aware of "
        f"what stopping costs: this validator stays audible only while its commits land, so "
        f"a halt that is left alone runs it out of its activity window and takes the "
        f"subnet's pay to zero. Fix the mismatch or rule that publishing resumes; do not "
        f"leave this sitting.",
    )
