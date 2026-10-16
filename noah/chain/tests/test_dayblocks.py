"""noah.chain.dayblocks: a completed calendar day's own exact block range,
and the chain's own genesis moment -- both read fresh from a real,
dual-sourced runner_chain, proven against a real AgreeingChain (not a
hand-rolled single-source stand-in), the same way every other chain-facing
module in this package is tested.
"""

import unittest

from noah.chain.agreeing import AgreeingChain
from noah.chain.genesis_times import MAINNET_BLOCK_ONE_TIME, MAINNET_GENESIS_HASH
from noah.chain.sources import NotEnoughSources, SourceDisagreement
from noah.chain.dayblocks import (
    BlockTimeNotRead,
    DayBoundaryUnstable,
    DayNotComplete,
    day_anchor,
    day_block_range,
)
from noah.chain.store import DAY_SECONDS

TICK = 3600  # seconds per block -- one simulated hour, so a day is exactly
# DAY_SECONDS // TICK = 24 blocks: small enough to binary-search quickly in
# a test, big enough that "one block" and "one day" are never confusable.
BASE = 1_000  # block 0's own timestamp; kept > 0 so _validated_block_time's
# own "must be positive" guard is satisfied by every block, including 0.


class Fake:
    """One source. Answers the three questions dayblocks.py asks of a
    runner_chain: head_block, block_timestamp, canary. Time advances a
    fixed TICK per block from BASE, so day boundaries land on round block
    numbers a test can state directly (day N starts at block N * 24)."""

    def __init__(self, head=200, times=None, canary=256, fail=None, genesis=None):
        self._head = head
        self._genesis = genesis
        self._times = dict(times or {})
        self._canary = canary
        self._fail = fail
        self.asked = []

    def head_block(self):
        if self._fail == "head":
            raise RuntimeError("the node hung up")
        return self._head

    def block_timestamp(self, block):
        self.asked.append(block)
        if self._fail == "time":
            raise RuntimeError("the node hung up")
        return self._times.get(block, BASE + block * TICK)

    def canary(self, block):
        return self._canary

    def block_hash(self, block):
        # Only block 0's hash is ever asked for by day_anchor. A pruned node
        # still answers it, so this needs no state behind it.
        if block == 0 and self._genesis is not None:
            return self._genesis
        return f"0x{block:064x}"


def chain(**kwargs):
    a, b = Fake(**kwargs), Fake(**kwargs)
    return AgreeingChain(
        sources={"archive": a, "latent": b},
        endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
    )


class DayAnchorTests(unittest.TestCase):
    def test_reads_block_ones_own_timestamp(self):
        reader = chain(head=200)
        self.assertEqual(day_anchor(reader), BASE + TICK)

    def test_refuses_a_negative_genesis_timestamp(self):
        # Exactly zero is already caught one layer down, by AgreeingChain's
        # own zero_is_impossible check (SourceLied) -- this proves the
        # BROADER guard _validated_block_time adds on top of that (also
        # catching a negative or non-int value, which zero_is_impossible
        # alone would not).
        reader = chain(head=200, times={1: -500})
        with self.assertRaises(BlockTimeNotRead):
            day_anchor(reader)


class FixedGenesisTimeTests(unittest.TestCase):
    """Issue #237, item B: a mainnet node keeps about 90 days of state, so block
    1's timestamp is gone from it. The anchor for a chain named in
    genesis_times must come from the fixed table, never from a read of block 1."""

    def test_a_listed_chain_gets_its_fixed_time_without_reading_block_one(self):
        # The fake still holds block 1, and answers a DIFFERENT time for it, so
        # only the table can have supplied the answer.
        a = Fake(genesis=MAINNET_GENESIS_HASH, times={1: BASE + TICK})
        b = Fake(genesis=MAINNET_GENESIS_HASH, times={1: BASE + TICK})
        reader = AgreeingChain(
            sources={"archive": a, "latent": b},
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )
        self.assertEqual(day_anchor(reader), MAINNET_BLOCK_ONE_TIME)
        self.assertNotEqual(MAINNET_BLOCK_ONE_TIME, BASE + TICK)
        self.assertEqual(a.asked, [])
        self.assertEqual(b.asked, [])

    def test_a_listed_chain_needs_no_state_at_block_zero(self):
        # Block 0 has no timestamp, so the canary there reads 0 on a real chain.
        # The genesis hash must not be gated on it.
        reader = chain(genesis=MAINNET_GENESIS_HASH, canary=0)
        self.assertEqual(day_anchor(reader), MAINNET_BLOCK_ONE_TIME)

    def test_the_hash_is_matched_whatever_its_spelling(self):
        reader = chain(genesis=MAINNET_GENESIS_HASH.upper().replace("0X", "0x"))
        self.assertEqual(day_anchor(reader), MAINNET_BLOCK_ONE_TIME)

    def test_an_unlisted_chain_still_reads_block_one(self):
        a = Fake()
        b = Fake()
        reader = AgreeingChain(
            sources={"archive": a, "latent": b},
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )
        self.assertEqual(day_anchor(reader), BASE + TICK)
        self.assertIn(1, a.asked)

    def test_two_sources_naming_different_genesis_hashes_are_refused(self):
        reader = AgreeingChain(
            sources={
                "archive": Fake(genesis=MAINNET_GENESIS_HASH),
                "latent": Fake(genesis="0x" + "ab" * 32),
            },
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )
        with self.assertRaises(SourceDisagreement):
            day_anchor(reader)

    def test_one_endpoint_under_two_names_is_not_two_sources(self):
        reader = AgreeingChain(
            sources={
                "archive": Fake(genesis=MAINNET_GENESIS_HASH),
                "latent": Fake(genesis=MAINNET_GENESIS_HASH),
            },
            endpoints={"archive": "wss://same.example", "latent": "wss://same.example"},
        )
        with self.assertRaises(NotEnoughSources):
            day_anchor(reader)


class DayBlockRangeTests(unittest.TestCase):
    def test_day_zero_starts_at_block_one_by_definition(self):
        # height=10 is still day 0 (block 0..23); head=200 is day 8 (well
        # past day 1's own start), so day 0 has genuinely ended.
        reader = chain(head=200)
        day, from_block, to_block = day_block_range(reader, BASE, height=10)
        self.assertEqual(day, 0)
        self.assertEqual(from_block, 1)
        self.assertEqual(to_block, 23)  # block 24 is day 1's own first block

    def test_a_later_day_finds_its_own_true_start_and_end(self):
        # day 3 spans blocks 72..95 (3*24 .. 4*24-1). height=80 falls in it.
        reader = chain(head=200)
        day, from_block, to_block = day_block_range(reader, BASE, height=80)
        self.assertEqual(day, 3)
        self.assertEqual(from_block, 72)
        self.assertEqual(to_block, 95)

    def test_refuses_when_the_day_has_not_ended_yet(self):
        # height=80 (day 3) with head still inside day 3 (block 90, day 3)
        # -- there is no true to_block for an ongoing day.
        reader = chain(head=90)
        with self.assertRaises(DayNotComplete):
            day_block_range(reader, BASE, height=80)

    def test_refuses_when_the_day_ends_exactly_at_the_head(self):
        """head itself still inside the day being scored (the boundary
        case: head is day 3's own LAST block, 95) must refuse exactly like
        any other still-ongoing day -- there is no proof block 96 (day 4's
        own start) has even been produced yet."""
        reader = chain(head=95)
        with self.assertRaises(DayNotComplete):
            day_block_range(reader, BASE, height=80)

    def test_succeeds_the_instant_the_next_day_has_genuinely_started(self):
        reader = chain(head=96)  # block 96 is day 4's own first block
        day, from_block, to_block = day_block_range(reader, BASE, height=80)
        self.assertEqual((day, from_block, to_block), (3, 72, 95))

    def test_refuses_on_a_block_time_read_that_never_happened(self):
        reader = chain(head=200, times={80: -1})
        with self.assertRaises(BlockTimeNotRead):
            day_block_range(reader, BASE, height=80)

    def test_refuses_on_a_reorg_that_moves_the_boundary_mid_search(self):
        """The identical mid-search-reorg protection neurons/miner.py's own
        _day_start_block (#478) was adversarially found to need, re-proven
        here for this module's own, independently-derived search
        (noah.chain.dayblocks._find_day_start). Mirrors the exact fixture
        shape codex's own reproduction of the #478 finding used (a shared
        call counter, both sources answering identically, the true
        boundary moving from block 40 to block 60 after a few reads) --
        two sources that never once disagree with EACH OTHER at any single
        read, yet the converged answer is still stale by the time the
        search finishes. Calls _find_day_start directly (rather than through
        day_block_range) so the shared read counter drives exactly the
        search under test, not also the height/head reads day_block_range
        makes first.

        Empirically confirmed to trigger the exact mismatch this asserts
        (not merely reasoned about): with this fixture, the search
        converges on blocks 49 and 50 under the stale boundary (40), and
        the freshness re-check -- reading both again under the now-real
        boundary (60) -- finds them BOTH in day 0, not the day 0 / day 1
        pair the search itself found moments earlier.
        """
        from noah.chain.dayblocks import _find_day_start

        state = {"reads": 0}

        def shared_block_timestamp(block):
            state["reads"] += 1
            boundary = 40 if state["reads"] <= 4 else 60
            return BASE if block < boundary else BASE + DAY_SECONDS

        class Shifting:
            def head_block(self):
                return 200

            def block_timestamp(self, block):
                return shared_block_timestamp(block)

            def canary(self, block):
                return 256

        reader = AgreeingChain(
            sources={"archive": Shifting(), "latent": Shifting()},
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )
        with self.assertRaises(DayBoundaryUnstable):
            _find_day_start(reader, day=1, day_anchor_ts=BASE, lower_block=1, upper_block=100)

    def test_refuses_when_a_reorg_moves_from_block_earlier_after_the_second_search(self):
        """[codex adversarial review of this port, 2026-09-01, P1] The exact
        bug class neurons/miner.py's own _populate_current_day already found
        and fixed once, in a different two-boundary composition: composing
        from_block (found by the FIRST search) with to_block (found by a
        SECOND, later search) without re-checking from_block after the
        second search completes can silently return a range whose own start
        is stale, if a reorg moved day 3's true start earlier in between.

        Empirically confirmed (not merely reasoned about): with this
        fixture, day 3's boundary is at block 72 until enough reads have
        happened for both searches to converge, then permanently moves to
        block 67 (a deep reorg, discovered only once the second search
        starts probing that old territory). The first search finds 72 under
        the still-old boundary; by the time the second search finishes and
        the fresh from_block re-check below runs, the boundary has already
        moved, and the re-check must catch it."""
        SHIFT_AFTER = 20  # total block_timestamp calls before the boundary moves

        state = {"n": 0}

        def block_timestamp(block):
            state["n"] += 1
            boundary = 72 if state["n"] <= SHIFT_AFTER else 67
            if block < 24:
                day = 0
            elif block < 48:
                day = 1
            elif block < boundary:
                day = 2
            elif block < 96:
                day = 3
            else:
                day = 4
            return BASE + day * DAY_SECONDS + 100

        class Shifting:
            def head_block(self):
                return 200

            def block_timestamp(self, block):
                return block_timestamp(block)

            def canary(self, block):
                return 256

        reader = AgreeingChain(
            sources={"archive": Shifting(), "latent": Shifting()},
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )
        with self.assertRaises(DayBoundaryUnstable) as refused:
            day_block_range(reader, BASE, height=80)
        # Specifically the NEW from_block re-check's own message, not the
        # pre-existing per-search freshness check inside _find_day_start --
        # proving this test exercises the fix this commit adds, not a check
        # that already existed.
        self.assertIn("no longer proves out as", str(refused.exception))


# ---- a pruned, mainnet-scale chain (issue #237, item C) ----------------------
#
# Mainnet keeps only about 90 days of state on Noah's node. A read of a block
# older than that does not answer, so a boundary search that probes the middle
# of the chain's whole history (its old lower bound was block 1) fails there.
# This fake has mainnet's shape: 12 second blocks, a genesis in 2023, a head
# around block 9.14 million, and NO answer for a block older than the window.

MAINNET_ANCHOR = 1679270400  # 2023-03-20T00:00:00Z, the floor of block 1's time
MAINNET_BLOCK_ONE = 1679338080  # 2023-03-20T18:48:00Z
MAINNET_HEAD = 9_140_000
RETENTION = 648_000


class StateDiscarded(RuntimeError):
    pass


class PrunedMainnet:
    """One source with mainnet-shaped time and a retention window. Every read
    of a block older than the window raises, as a pruned node's does."""

    def __init__(self, head=MAINNET_HEAD, retention=RETENTION):
        self._head = head
        self._retention = retention
        self.asked = []

    def head_block(self):
        return self._head

    def block_timestamp(self, block):
        self.asked.append(block)
        if block < self._head - self._retention:
            raise StateDiscarded(f"State already discarded for block {block}")
        return MAINNET_BLOCK_ONE + (block - 1) * 12

    def canary(self, block):
        return 256


def pruned_chain(**kwargs):
    a, b = PrunedMainnet(**kwargs), PrunedMainnet(**kwargs)
    return AgreeingChain(
        sources={"archive": a, "latent": b},
        endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
    ), (a, b)


def true_first_block_of(day):
    """The first block whose time is at or after the start of ``day``, by
    plain arithmetic on this fake's own fixed 12 second spacing."""
    start = MAINNET_ANCHOR + day * DAY_SECONDS
    return max(1, -(-(start - MAINNET_BLOCK_ONE) // 12) + 1)


class PrunedNodeDayRangeTests(unittest.TestCase):
    def test_a_recent_day_is_found_without_reading_any_block_the_node_discarded(self):
        reader, sources = pruned_chain()
        height = MAINNET_HEAD - 20_000  # inside yesterday, which is complete
        day, from_block, to_block = day_block_range(reader, MAINNET_ANCHOR, height)
        self.assertEqual(from_block, true_first_block_of(day))
        self.assertEqual(to_block, true_first_block_of(day + 1) - 1)
        oldest_read = min(min(source.asked) for source in sources)
        self.assertGreaterEqual(oldest_read, MAINNET_HEAD - RETENTION)

    def test_the_search_stays_close_to_the_day_it_is_looking_for(self):
        """Not just inside the window: within a few days of the height, so the
        reads a caught-up validator makes never creep toward the window's edge."""
        reader, sources = pruned_chain()
        height = MAINNET_HEAD - 20_000
        day_block_range(reader, MAINNET_ANCHOR, height)
        oldest_read = min(min(source.asked) for source in sources)
        self.assertGreaterEqual(oldest_read, height - 4 * 7200)

    def test_a_height_late_in_its_day_still_finds_a_block_before_the_day(self):
        # Take the last block of a day, so the distance back to its start is
        # as long as it gets, and confirm the step-back finds the true start.
        reader, _ = pruned_chain()
        day = (MAINNET_HEAD - 30_000 - true_first_block_of(0)) * 12 // DAY_SECONDS
        last_of_day = true_first_block_of(day + 1) - 1
        got_day, from_block, to_block = day_block_range(reader, MAINNET_ANCHOR, last_of_day)
        self.assertEqual(got_day, day)
        self.assertEqual(from_block, true_first_block_of(day))
        self.assertEqual(to_block, last_of_day)

    def test_a_young_chain_still_searches_all_the_way_back_to_block_one(self):
        # The step-back must reach block 1 when that is the nearest block
        # provably before the day: the existing small-chain behaviour.
        reader = chain(head=200)
        self.assertEqual(day_block_range(reader, BASE, height=30)[1:], (24, 47))


if __name__ == "__main__":
    unittest.main()
