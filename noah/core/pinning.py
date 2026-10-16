"""Choose a settled period boundary consistently across heads in the same period.

Use pinned = (head // tempo - settle_periods) * tempo - 1. Convert a block margin
conservatively: the worst head in the window must still satisfy the requested
wait. Refuse when the chain is too young to supply such a boundary."""

from .settings import SettingsError


class NoSettledPeriod(SettingsError):
    """No period has both ended and settled, so there is nothing to score yet."""


def settle_periods_for(*, settle_blocks, tempo):
    """A block margin as a whole number of periods, never rounded down to zero."""
    if tempo <= 0:
        raise NoSettledPeriod(f"a period of {tempo} blocks is not a period.")
    if settle_blocks < 0:
        raise NoSettledPeriod(
            f"a settle margin of {settle_blocks} blocks is not a margin; it would pin a "
            f"height the chain has not reached."
        )
    # The wait at the WORST head in a window is (periods - 1) * tempo + 1, so
    # solve that for periods rather than dividing the margin by the period. The
    # simple division delivers less margin than was asked for and says nothing.
    #
    # No floor at one. This is already at least one for every margin that gets
    # this far, since a negative one was refused above, and a floor that cannot
    # bind is a guard nothing can falsify. A sweep found it unkillable, which is
    # what redundant code looks like from outside.
    return -(-(settle_blocks - 1) // tempo) + 1


def pin_height(*, head, tempo, settle_blocks):
    """The last block of a period that has settled for at least `settle_blocks`.

    Two properties, both checked rather than reasoned about. It is constant for
    every head inside a period, so validators agree without coordinating. And
    the gap between the head and the pinned height is at least `settle_blocks`
    at every head, including the first one in the window.
    """
    periods = settle_periods_for(settle_blocks=settle_blocks, tempo=tempo)
    target = head // tempo - periods
    if target < 0:
        raise NoSettledPeriod(
            f"at height {head}, with periods of {tempo} blocks and a margin of "
            f"{settle_blocks} blocks ({periods} whole period(s)), no period has both ended "
            f"and settled. There is nothing to score yet, which on a new chain is ordinary. "
            f"This does NOT reach further back: a height that fails the rule is one some "
            f"validators would have scored and others not, and that disagreement is exactly "
            f"what pinning exists to prevent."
        )
    return (target + 1) * tempo - 1


def period_of(*, height, tempo):
    """Which period a height belongs to, counted from the chain's start."""
    if tempo <= 0:
        raise NoSettledPeriod(f"a period of {tempo} blocks is not a period.")
    return height // tempo


def period_bounds(*, period, tempo):
    """The first and last block of a period, inclusive."""
    if tempo <= 0:
        raise NoSettledPeriod(f"a period of {tempo} blocks is not a period.")
    return period * tempo, (period + 1) * tempo - 1
