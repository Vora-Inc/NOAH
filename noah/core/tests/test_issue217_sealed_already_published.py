"""Issue #217: sealed-mode ChainAdapter.already_published must answer for ONE
calendar day, not for the tempo period containing its height.

Every test drives the REAL ChainAdapter and the REAL ChainReader.committed_in_period
(only the substrate client is faked: System.Events per block, Tempo, head), with
publish_mode="sealed". Heights are obtained from the REAL pinned_height().
Each test asserts the CORRECT behaviour. Originally the investigator's
reproduction (3 failures against main); now the regression suite for the fix,
plus the boundary, geometry, bounded-cost and failure-semantics tests.
"""

import unittest

from ...chain.dayblocks import DayNotComplete
from ..chain_adapter import DayWindowNotWired
from ..reader import ChainReader, ChainUnreadable
from ._adapter_helpers import NETUID, TrivialHotkey, an_adapter
from .test_reader import node_down

OURS = "5" + "O" * 47
THEIRS = "5" + "T" * 47


class EventClient:
    """Fake substrate client: every block carries one filler event, plus any
    commit / WeightsSet events placed at that block."""

    def __init__(self, head, tempo, commits=None, weights_set=None):
        self.block = head
        self.tempo = tempo
        self.commits = commits or {}  # {block: [hotkey, ...]}
        self.weights_set = weights_set or {}  # {block: [uid, ...]}
        self.asked_blocks = []

    def query(self, item, params=None, block=None):
        key = item[1]
        if key == "Tempo":
            return self.tempo
        if key != "Events":
            raise RuntimeError(key)
        assert block <= self.block, f"scan read block {block} beyond head {self.block}"
        self.asked_blocks.append(block)
        events = [{"event": {"event_id": "SomethingElse", "attributes": [1, 2]}}]
        for hk in self.commits.get(block, []):
            events.append(
                {
                    "event": {
                        "event_id": "TimelockedWeightsCommitted",
                        "attributes": [hk, NETUID, "0xHASH", 7],
                    }
                }
            )
        for uid in self.weights_set.get(block, []):
            events.append({"event": {"event_id": "WeightsSet", "attributes": [NETUID, uid]}})
        return events

    def query_map(self, item, params=None):
        return []


def adapter_at(head, tempo, day_window, commits=None, weights_set=None, settle=360):
    client = EventClient(head, tempo, commits, weights_set)
    adapter, _, _ = an_adapter(
        reader=ChainReader(client),
        wallet_hotkey=TrivialHotkey(ss58_address=OURS),
        publish_mode="sealed",
        day_window=day_window,
        settle_blocks=settle,
    )
    return adapter, client


# ---- mainnet geometry: tempo 360 (SN82 readback, #293), ~7200-block days -----
TEMPO = 360
DAY = 7200


def to_of(d):
    return (d + 1) * DAY - 1


def mainnet_day_window(height):
    d = height // DAY
    return d, d * DAY, to_of(d)


def head_for_last_chance(d):
    """A head at which the REAL pin_height names day d's last period boundary
    (== to_of(d), which is a multiple of 360 minus 1)."""
    return to_of(d) + 361


class ShortDayShape_Original358_359_Collision(unittest.TestCase):
    """Non-mainnet geometry (day shorter than one tempo): the exact shape direct
    mode had before its fix."""

    def test_sealed_day1_is_not_reported_published_just_because_day0_committed(self):
        def real_day_window(height):
            if height == 358:
                return 0, 0, 358
            if height == 359:
                return 1, 359, 419
            if height == 420:  # day 2, asked about only to find day 1's upper bound
                return 2, 420, 480
            raise AssertionError(height)

        # Day 0's real sealed commit lands at 400 (after Day 0 closed at 358,
        # inside day 1's own range and the same tempo period the old scan
        # covered). Day 1 never committed. (The investigator's original fixture
        # put the commit at 901, more than a full short day after Day 0 -- by
        # design outside the bounded window now; see test_a_commit_more_than_a_
        # full_day_late_is_the_accepted_duplicate_tradeoff below.)
        adapter, _ = adapter_at(8107, 360, real_day_window, commits={400: [OURS]})
        d0 = adapter.already_published(358)
        d1 = adapter.already_published(359)
        self.assertTrue(d0, "Day 0 (height 358) has a real commit at 400")
        self.assertFalse(d1, f"Day 1 (height 359) never committed but already_published={d1}")


class MainnetGeometry_WhatWorks(unittest.TestCase):
    def test_day10_commit_found_at_its_own_last_chance_height_and_not_attributed_to_day11(self):
        x = to_of(10) + 400  # day 10's commit, on time
        adapter, _ = adapter_at(to_of(10) + 700, TEMPO, mainnet_day_window, commits={x: [OURS]})
        self.assertEqual(adapter.pinned_height(), to_of(10))
        self.assertTrue(adapter.already_published(to_of(10)))
        # at day 11's own last-chance tick, with only day 10's on-time commit on chain
        adapter11, _ = adapter_at(
            head_for_last_chance(11), TEMPO, mainnet_day_window, commits={x: [OURS]}
        )
        self.assertEqual(adapter11.pinned_height(), to_of(11))
        self.assertFalse(adapter11.already_published(to_of(11)))

    def test_mid_day_tick_just_after_previous_day_commit_reads_published_harmless(self):
        """Characterisation only: a mid-day-(D+1) height whose scan reaches back
        over day D's commit answers True. run_period then raises AlreadyPublished
        instead of DayNotComplete for a height that could never have been scored
        anyway (day D+1 is still open). Not a day-D+1 false positive."""
        x = to_of(10) + 400
        head = to_of(10) + 1200
        adapter, _ = adapter_at(head, TEMPO, mainnet_day_window, commits={x: [OURS]})
        h = adapter.pinned_height()
        self.assertEqual(h // DAY, 10 + 0 if h <= to_of(10) else 11)
        got = adapter.already_published(h)
        print(f"\n[info] mid-day tick h={h} (day {h // DAY}) already_published={got}")


class MainnetGeometry_FalsePositive(unittest.TestCase):
    def test_a_later_days_commit_does_not_make_an_earlier_never_published_day_look_published(self):
        """Day 10 never published (outage). Day 11 published normally. Something
        later asks about day 10's last-chance height (a retry / re-check)."""
        x11 = to_of(11) + 400
        adapter, _ = adapter_at(to_of(11) + 3000, TEMPO, mainnet_day_window, commits={x11: [OURS]})
        got = adapter.already_published(to_of(10))
        self.assertFalse(got, f"day 10 never committed; already_published(to_of(10))={got}")

    def test_a_day_ten_commit_landing_one_day_late_does_not_hide_day_eleven(self):
        """Day 10's commit lands ~23h55m late (manual recovery or delayed retry),
        inside the scan window of day 11's own last-chance tick. Day 11 has not
        committed. Correct answer for day 11 is False."""
        late_x10 = to_of(11) - 100
        adapter, _ = adapter_at(
            head_for_last_chance(11), TEMPO, mainnet_day_window, commits={late_x10: [OURS]}
        )
        self.assertEqual(adapter.pinned_height(), to_of(11))
        got = adapter.already_published(to_of(11))
        self.assertFalse(got, f"day 11 never committed; already_published(to_of(11))={got}")

    def test_a_day_ten_commit_landing_a_few_hours_late_does_not_hide_day_eleven(self):
        late_x10 = to_of(10) + 1500  # ~5 hours late: outside day 11's last-chance scan window
        adapter, _ = adapter_at(
            head_for_last_chance(11), TEMPO, mainnet_day_window, commits={late_x10: [OURS]}
        )
        self.assertFalse(adapter.already_published(to_of(11)))


class MainnetGeometry_FalseNegative(unittest.TestCase):
    def test_reveal_only_no_commit_event_in_range_reads_not_published(self):
        """Only the REVEAL (WeightsSet) is in the scanned range. Sealed mode looks
        only for commit events, so this answers False (validator would commit again)."""
        x = to_of(10) + 400
        adapter, _ = adapter_at(to_of(10) + 1500, TEMPO, mainnet_day_window, weights_set={x: [0]})
        got = adapter.already_published(to_of(10))
        print(f"\n[info] reveal-only already_published={got}")
        self.assertFalse(got)  # characterisation: documents the semantics

    def test_commit_before_the_scanned_period_is_not_counted(self):
        """Characterisation: a commit one block before period(H) starts is out of range."""
        h = to_of(11)
        start = (h // TEMPO) * TEMPO
        adapter, _ = adapter_at(
            head_for_last_chance(11), TEMPO, mainnet_day_window, commits={start - 1: [OURS]}
        )
        self.assertFalse(adapter.already_published(h))

    def test_another_validators_commit_never_counts(self):
        x = to_of(10) + 400
        adapter, _ = adapter_at(to_of(10) + 1500, TEMPO, mainnet_day_window, commits={x: [THEIRS]})
        self.assertFalse(adapter.already_published(to_of(10)))

    def test_our_on_time_commit_is_always_inside_the_scan_range(self):
        """Every commit position from the first block after day end through head
        is found (no false negative for an on-time or late commit)."""
        for off in (1, 361, 719, 721, 1500, 7000):
            x = to_of(10) + off
            adapter, _ = adapter_at(x + 5, TEMPO, mainnet_day_window, commits={x: [OURS]})
            self.assertTrue(adapter.already_published(to_of(10)), f"missed commit at +{off}")


class RestartAndCatchUp(unittest.TestCase):
    def test_commit_landed_then_crash_then_restart_days_later_reads_published_no_double_commit(
        self,
    ):
        x = to_of(10) + 400
        head = to_of(10) + 3 * DAY  # restart three days later
        adapter, _ = adapter_at(head, TEMPO, mainnet_day_window, commits={x: [OURS]})
        self.assertTrue(adapter.already_published(to_of(10)))

    def test_outage_nothing_committed_retry_of_the_pending_height_reads_not_published(self):
        head = to_of(10) + 2 * DAY
        adapter, client = adapter_at(head, TEMPO, mainnet_day_window)
        self.assertFalse(adapter.already_published(to_of(10)))
        print(f"\n[info] blocks scanned for retry after 2-day outage: {len(client.asked_blocks)}")

    def test_issue244_catchup_retry_shape_pending_height_with_later_period_ready(self):
        """Pending day-10 last-chance height was refused DayNotComplete; the next
        period is ready (head in day 11). Retry: nothing committed -> False (proceed);
        after the commit lands -> True."""
        head = to_of(10) + 721
        a1, _ = adapter_at(head, TEMPO, mainnet_day_window)
        self.assertFalse(a1.already_published(to_of(10)))
        a2, _ = adapter_at(head, TEMPO, mainnet_day_window, commits={to_of(10) + 500: [OURS]})
        self.assertTrue(a2.already_published(to_of(10)))


# ---- fix-specific tests -------------------------------------------------------


def strict_window(day_len, head_ref, first_len=None):
    """A day_window that behaves like the real day_block_range: refuses
    DayNotComplete for any height whose day has not ended at the live head.
    `first_len` makes Day 0 a different length (mainnet: ~1560 blocks)."""
    first_len = day_len if first_len is None else first_len

    def window(height):
        if height < first_len:
            day, lo, hi = 0, 0, first_len - 1
        else:
            day = 1 + (height - first_len) // day_len
            lo = first_len + (day - 1) * day_len
            hi = lo + day_len - 1
        if hi >= head_ref[0]:
            raise DayNotComplete(
                f"day {day} not complete at head {head_ref[0]}", day=day, height=height
            )
        return day, lo, hi

    return window


def strict_adapter(head, tempo, day_len, commits=None, first_len=None, bad_blocks=()):
    head_ref = [head]
    client = EventClient(head, tempo, commits)
    if bad_blocks:
        inner = client.query

        def query(item, params=None, block=None):
            if item[1] == "Events" and block in bad_blocks:
                raise node_down()
            return inner(item, params, block)

        client.query = query
    adapter, _, _ = an_adapter(
        reader=ChainReader(client),
        wallet_hotkey=TrivialHotkey(ss58_address=OURS),
        publish_mode="sealed",
        day_window=strict_window(day_len, head_ref, first_len),
        settle_blocks=360,
    )
    return adapter, client


class Boundaries(unittest.TestCase):
    """Day 10 under mainnet geometry: to_block(10) = 11 * 7200 - 1 = 79199,
    to_block(11) = 86399. A commit counts for day 10 only in 79200..86399
    (or to head, while day 11 is still open)."""

    D10_END = to_of(10)
    D11_END = to_of(11)

    def asked(self, commit_block, head):
        adapter, _ = strict_adapter(head, TEMPO, DAY, commits={commit_block: [OURS]})
        return adapter.already_published(self.D10_END)

    def test_commit_at_to_block_d_is_not_day_ds(self):
        self.assertFalse(self.asked(self.D10_END, self.D10_END + 1000))

    def test_commit_at_to_block_d_plus_1_is_day_ds(self):
        self.assertTrue(self.asked(self.D10_END + 1, self.D10_END + 1000))

    def test_commit_at_to_block_d_plus_1_is_day_ds_with_d_plus_1_complete(self):
        self.assertTrue(self.asked(self.D10_END + 1, self.D11_END + 500))

    def test_commit_at_last_block_of_the_next_day_is_still_day_ds(self):
        self.assertTrue(self.asked(self.D11_END, self.D11_END + 500))

    def test_commit_one_block_past_to_block_d_plus_1_is_not_day_ds(self):
        """The accepted tradeoff (Task Lead decision, #217): a commit more than
        a full day late reads as not-published, so the day may be committed
        again (benign duplicate) rather than skipped silently."""
        self.assertFalse(self.asked(self.D11_END + 1, self.D11_END + 500))

    def test_d_plus_1_incomplete_scans_to_head(self):
        """Day 11 not complete: DayNotComplete is caught and the scan runs to
        head, so an on-time commit at the very head block is found."""
        head = self.D10_END + 5000
        adapter, client = strict_adapter(head, TEMPO, DAY, commits={head: [OURS]})
        self.assertTrue(adapter.already_published(self.D10_END))
        self.assertEqual(max(client.asked_blocks), head)

    def test_d_plus_1_complete_stops_at_its_last_block(self):
        head = self.D11_END + 3000
        adapter, client = strict_adapter(head, TEMPO, DAY)
        self.assertFalse(adapter.already_published(self.D10_END))
        self.assertEqual(min(client.asked_blocks), self.D10_END + 1)
        self.assertEqual(max(client.asked_blocks), self.D11_END)

    def test_head_before_the_scan_window_reads_not_published_without_reads(self):
        adapter, client = strict_adapter(self.D10_END + 1, TEMPO, DAY)
        self.assertFalse(adapter.already_published(self.D10_END))
        self.assertEqual(client.asked_blocks, [self.D10_END + 1])


class StrictWindowRegressions(unittest.TestCase):
    """The investigator's failing shapes against a day_window that refuses
    DayNotComplete exactly as the real one does."""

    def test_a2_previous_days_late_commit_does_not_hide_day_d(self):
        late_x10 = to_of(11) - 100
        adapter, _ = strict_adapter(
            head_for_last_chance(11), TEMPO, DAY, commits={late_x10: [OURS]}
        )
        self.assertFalse(adapter.already_published(to_of(11)))

    def test_a1_later_days_commit_does_not_make_earlier_day_published(self):
        adapter, _ = strict_adapter(to_of(11) + 3000, TEMPO, DAY, commits={to_of(11) + 400: [OURS]})
        self.assertFalse(adapter.already_published(to_of(10)))

    def test_on_time_commit_found_for_its_own_day_and_not_the_next(self):
        x = to_of(10) + 400
        a10, _ = strict_adapter(to_of(10) + 700, TEMPO, DAY, commits={x: [OURS]})
        self.assertTrue(a10.already_published(to_of(10)))
        a11, _ = strict_adapter(head_for_last_chance(11), TEMPO, DAY, commits={x: [OURS]})
        self.assertFalse(a11.already_published(to_of(11)))


class MainnetDayZeroGeometry(unittest.TestCase):
    """Day 0 ~1560 blocks (noah/chain/genesis_times.py), later days 7200,
    tempo 360: Day 0 ends at 1559, Day 1 is 1560..8759."""

    def test_day0_commit_is_day0s_and_not_day1s(self):
        head = 8759 + 800
        commits = {1559 + 400: [OURS]}  # Day 0's commit, early in Day 1
        a0, _ = strict_adapter(head, TEMPO, DAY, commits=commits, first_len=1560)
        self.assertTrue(a0.already_published(1559))
        a1, _ = strict_adapter(head, TEMPO, DAY, commits=commits, first_len=1560)
        self.assertFalse(a1.already_published(8759))

    def test_day1_commit_is_not_attributed_to_day0(self):
        head = 8759 + 800
        a0, _ = strict_adapter(head, TEMPO, DAY, commits={8759 + 400: [OURS]}, first_len=1560)
        self.assertFalse(a0.already_published(1559))  # outside 1560..8759
        a1, _ = strict_adapter(head, TEMPO, DAY, commits={8759 + 400: [OURS]}, first_len=1560)
        self.assertTrue(a1.already_published(8759))


class Original358_359_Geometry(unittest.TestCase):
    def test_short_days_do_not_collide_under_the_strict_window(self):
        head = 8107
        client_commits = {400: [OURS]}  # Day 0's, inside day 1's range

        def window(height):
            d = {358: (0, 0, 358), 359: (1, 359, 419), 420: (2, 420, 480)}
            if height not in d:
                raise AssertionError(height)
            return d[height]

        a0, _ = adapter_at(head, 360, window, commits=client_commits)
        a1, _ = adapter_at(head, 360, window, commits=client_commits)
        self.assertTrue(a0.already_published(358))
        self.assertFalse(a1.already_published(359))


class FailureAndCost(unittest.TestCase):
    def test_unwired_day_window_refuses_by_name(self):
        adapter, _, _ = an_adapter(
            reader=ChainReader(EventClient(to_of(10) + 1000, TEMPO)),
            wallet_hotkey=TrivialHotkey(ss58_address=OURS),
            publish_mode="sealed",
        )
        with self.assertRaises(DayWindowNotWired):
            adapter.already_published(to_of(10))

    def test_an_unreadable_block_in_the_range_raises_not_false(self):
        head = to_of(10) + 1000
        adapter, _ = strict_adapter(head, TEMPO, DAY, bad_blocks={to_of(10) + 500})
        with self.assertRaises(ChainUnreadable):
            adapter.already_published(to_of(10))

    def test_an_unreadable_block_outside_the_range_is_not_read_at_all(self):
        head = to_of(10) + 1000
        adapter, _ = strict_adapter(head, TEMPO, DAY, bad_blocks={to_of(10)})
        self.assertFalse(adapter.already_published(to_of(10)))

    def test_block_reads_are_bounded_by_the_window_not_by_outage_length(self):
        counts = []
        for outage_days in (0, 1, 2, 5):
            head = to_of(10) + outage_days * DAY + 1000
            adapter, client = strict_adapter(head, TEMPO, DAY)
            self.assertFalse(adapter.already_published(to_of(10)))
            counts.append(len(client.asked_blocks))
        # at most one full day of blocks, whatever the outage length
        self.assertTrue(all(c <= DAY for c in counts), counts)
        self.assertEqual(counts[1:], [DAY] * 3, counts)
        self.assertEqual(counts[0], 1000)


class ReaderBlockRange(unittest.TestCase):
    def reader(self, head=100, commits=None):
        return ChainReader(EventClient(head, 360, commits))

    def test_inclusive_bounds_and_hotkey_filter(self):
        r = self.reader(commits={10: [OURS], 20: [THEIRS], 21: [OURS]})
        self.assertEqual(r.committed_in_block_range(NETUID, 10, 20), {OURS, THEIRS})
        self.assertEqual(r.committed_in_block_range(NETUID, 11, 20, hotkey=OURS), set())
        self.assertEqual(r.committed_in_block_range(NETUID, 10, 20, hotkey=OURS), {OURS})

    def test_range_past_head_scans_nothing(self):
        client = EventClient(100, 360)
        self.assertEqual(
            ChainReader(client).committed_in_block_range(NETUID, 101, 200, head=100), set()
        )
        self.assertEqual(client.asked_blocks, [])

    def test_other_netuid_is_ignored(self):
        client = EventClient(100, 360, {5: [OURS]})
        self.assertEqual(ChainReader(client).committed_in_block_range(NETUID + 1, 0, 10), set())

    def test_unreadable_block_names_the_range(self):
        client = EventClient(100, 360)
        inner = client.query

        def query(item, params=None, block=None):
            if item[1] == "Events" and block == 7:
                raise node_down()
            return inner(item, params, block)

        client.query = query
        with self.assertRaisesRegex(ChainUnreadable, "block range 5 to 9"):
            ChainReader(client).committed_in_block_range(NETUID, 5, 9)


if __name__ == "__main__":
    unittest.main()
