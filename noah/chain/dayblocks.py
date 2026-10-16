"""Resolve a calendar day's exact block range from chain timestamps.

Searches use the finalized head and require the day to have ended. A configured
history floor bounds available reads; missing/pruned state is not treated as an
empty day. Day numbering uses the same midnight anchor as the history store."""

from .genesis_times import fixed_block_one_time
from .sources import Refusal
from .store import DAY_SECONDS


class BlockTimeNotRead(Refusal):
    """A block's timestamp came back as something other than a decoded,
    positive moment in time."""


class DayNotComplete(Refusal):
    """The finalized chain has not yet reached the end of the requested day."""

    def __init__(self, message, *, day=None, height=None):
        super().__init__(message)
        self.day = day
        self.height = height


class DayBoundaryUnstable(Refusal):
    """The chain moved during the boundary search -- a reorg both sources
    have already converged on, not a source disagreement -- so the
    converged answer is refused rather than trusted stale."""


def _validated_block_time(runner_chain, block, purpose):
    """``block``'s real, decoded, positive timestamp, or refuse.

    Mirrors ``neurons/miner.py``'s own ``_validated_block_time`` guard
    (identical reasoning: ``Timestamp.Now`` is a ``ValueQuery``, so a read
    that did not happen answers zero rather than failing) -- re-derived
    here rather than imported, for the same layering reason this module's
    own docstring gives for not importing ``_day_start_block`` itself.
    """
    timestamp = runner_chain.block_timestamp(block)
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise BlockTimeNotRead(
            f"block {block}'s time came back as {timestamp!r}, which is not a decoded, "
            f"positive moment in time. This is {purpose}, so a read that silently returned "
            f"zero would corrupt it rather than fail loudly."
        )
    return timestamp


def day_anchor(runner_chain):
    """Return the chain's day anchor using its genesis identity and timestamp evidence."""
    fixed = fixed_block_one_time(runner_chain.genesis_hash())
    if fixed is not None:
        return fixed
    return _validated_block_time(
        runner_chain,
        1,
        "the store's day anchor and a permanent identity, so a read that silently returned "
        "zero would shift every day boundary rather than fail loudly, and reopening the "
        "same store under a corrected anchor later is refused rather than allowed",
    )


def _day_of(timestamp, day_anchor_ts):
    """Which day ``timestamp`` falls in, counted from ``day_anchor_ts`` --
    the identical formula ``noah.chain.store.Store.day_of`` uses
    (``store.py``: ``(block_timestamp - self.day_anchor_ts) // DAY_SECONDS``),
    re-derived here because this module has no ``Store`` instance to ask
    (see this module's own docstring: ``day_window(height)`` runs before
    ``store_factory()`` is ever called). One line of pure arithmetic, not
    the reviewed binary-search machinery this module's own docstring is
    careful never to reimplement -- there is nothing here for a second
    implementation to drift from.
    """
    if timestamp < day_anchor_ts:
        raise BlockTimeNotRead(
            f"a block timestamp of {timestamp} is before the day anchor "
            f"{day_anchor_ts}. There is no day for it."
        )
    return (timestamp - day_anchor_ts) // DAY_SECONDS


#: A starting stride, in blocks, for :func:`_bracket_below_day`'s search
#: backward from a block known to be in or after the day being looked for.
#: About one day of blocks at 12 seconds each. It is only a first guess at
#: how far back a day's start might be: the search doubles it until it
#: finds a block provably before the day, so a wrong guess costs a few
#: reads, never a wrong answer.
_BRACKET_START_STRIDE = 7200


def _bracket_below_day(runner_chain, day, day_anchor_ts, upper_block, stride=None):
    """Bracket the first block of a day with a doubling backward search.

    Respect the readable history floor rather than assuming block one is available."""
    if day <= 0:
        raise DayBoundaryUnstable(
            f"there is no block before day {day} to bracket its start with. Day 0 starts at "
            f"block 1 by the chain's own definition and is never searched for."
        )
    stride = _BRACKET_START_STRIDE if stride is None else stride
    while True:
        candidate = upper_block - stride
        if candidate <= 1:
            return 1, upper_block
        candidate_ts = _validated_block_time(
            runner_chain,
            candidate,
            f"a candidate block stepped back from block {upper_block} to find one before "
            f"day {day}'s own first block",
        )
        if _day_of(candidate_ts, day_anchor_ts) < day:
            return candidate, upper_block
        upper_block = candidate
        stride *= 2


def _find_day_start(runner_chain, day, day_anchor_ts, lower_block, upper_block):
    """Find the exact first block of a day by timestamp-bounded binary search."""
    if lower_block is None:
        lower_block, upper_block = _bracket_below_day(
            runner_chain, day, day_anchor_ts, upper_block
        )
    if upper_block <= lower_block:
        raise DayBoundaryUnstable(
            f"searching for day {day}'s own first block needs a lower bound strictly "
            f"before an upper bound; got lower_block={lower_block} >= "
            f"upper_block={upper_block}. Nothing was scanned."
        )

    lo, hi = lower_block, upper_block
    while hi - lo > 1:
        mid = (lo + hi) // 2
        mid_ts = _validated_block_time(
            runner_chain,
            mid,
            f"a candidate block in the binary search for day {day}'s own first block",
        )
        if _day_of(mid_ts, day_anchor_ts) >= day:
            hi = mid
        else:
            lo = mid

    lo_ts = _validated_block_time(
        runner_chain, lo, f"a final freshness re-check of day {day}'s own boundary search"
    )
    hi_ts = _validated_block_time(
        runner_chain, hi, f"a final freshness re-check of day {day}'s own boundary search"
    )
    lo_day, hi_day = _day_of(lo_ts, day_anchor_ts), _day_of(hi_ts, day_anchor_ts)
    if lo_day != day - 1 or hi_day != day:
        raise DayBoundaryUnstable(
            f"the binary search for day {day}'s own first block converged on blocks {lo} "
            f"and {hi}, and a final fresh re-check now finds them in day {lo_day} and day "
            f"{hi_day} respectively, not day {day - 1} and day {day} as the search found "
            f"moments earlier. The chain moved during the search -- a reorg both sources "
            f"have already converged on, not a source disagreement -- and this refuses "
            f"rather than returning a boundary already known to be stale. Nothing was "
            f"scanned; re-run once the chain has settled."
        )
    return hi


def day_block_range(runner_chain, day_anchor_ts, height):
    """Resolve (day, from_block, to_block) for the completed day containing height.

    Refuse an ongoing day. After finding both boundaries, recheck the first boundary
    and its predecessor so a reorg between searches cannot silently omit blocks.
    The result uses observed timestamps rather than an estimated block duration."""
    height_ts = _validated_block_time(
        runner_chain,
        height,
        "the scored height's own timestamp, used to decide which calendar day it falls in",
    )
    day = _day_of(height_ts, day_anchor_ts)

    head = runner_chain.head_block()
    head_ts = _validated_block_time(
        runner_chain,
        head,
        "the chain's own current head, checked to prove day {day} has actually ended "
        "before searching for its own true last block".format(day=day),
    )
    head_day = _day_of(head_ts, day_anchor_ts)
    if head_day < day + 1:
        raise DayNotComplete(
            f"day {day} (height {height} falls in it) has not ended yet: the chain's own "
            f"current head (block {head}) is still in day {head_day}. There is no true "
            f"to_block for a day that has not ended, and this never estimates one. Nothing "
            f"was scanned.",
            day=day,
            height=height,
        )

    if day == 0:
        # Block 1 is day 0's own first block by the chain's own definition
        # (its genesis) -- the identical fact `neurons/miner.py`'s own
        # `day == 0` branch relies on, needing no search.
        from_block = 1
    else:
        from_block = _find_day_start(
            runner_chain, day, day_anchor_ts, lower_block=None, upper_block=height
        )

    next_day_start = _find_day_start(
        runner_chain, day + 1, day_anchor_ts, lower_block=height, upper_block=head
    )
    to_block = next_day_start - 1

    # Re-prove from_block fresh, now that the second search has run --
    # see this function's own docstring, "from_block is re-proven fresh".
    # Day 0 is exempt: block 1 cannot reorg into a different fact.
    if day > 0:
        from_ts = _validated_block_time(
            runner_chain,
            from_block,
            f"re-proving day {day}'s own first block (found before the day {day + 1} "
            f"search ran) still falls in day {day}, now that search has completed",
        )
        before_from_ts = _validated_block_time(
            runner_chain,
            from_block - 1,
            f"re-proving the block immediately before day {day}'s own first block "
            f"still falls in day {day - 1}, so that first block is still provably "
            f"day {day}'s own TRUE start and not merely some block inside it",
        )
        from_day = _day_of(from_ts, day_anchor_ts)
        before_from_day = _day_of(before_from_ts, day_anchor_ts)
        if from_day != day or before_from_day != day - 1:
            raise DayBoundaryUnstable(
                f"day {day}'s own first block ({from_block}), found before searching for "
                f"day {day + 1}'s own start, no longer proves out as day {day}'s own true "
                f"start on a fresh re-check taken after that second search completed: it "
                f"and the block immediately before it now read as day {from_day} and day "
                f"{before_from_day} respectively, not day {day} and day {day - 1} as "
                f"found moments earlier. The chain moved during the second search, deeply "
                f"enough to reach a block already found. Nothing was scanned; re-run once "
                f"the chain has settled."
            )

    return day, from_block, to_block
