"""Whether a history.db holds every day it should, with no holes (issue #239)."""

import pytest

from noah.chain.history_check import (
    HistoryGap,
    confirm_history_contiguous,
    history_problems,
    missing_days,
)
from noah.chain.store import Store

ANCHOR = 1679270400


def a_store(tmp_path, days):
    """A store whose scanned days are ``days``: {day: (from_block, to_block)}."""
    store = Store.create(tmp_path / "history.db", day_anchor_ts=ANCHOR)
    for day, (low, high) in days.items():
        store.mark_day_scanned(day, low, high)
    return store


def whole(first_day, last_day, *, first_block=1, size=100):
    """A gap-free run of days, each `size` blocks, day `first_day` starting at `first_block`."""
    days, low = {}, first_block
    for day in range(first_day, last_day + 1):
        days[day] = (low, low + size - 1)
        low += size
    return days


class TestAWholeRecord:
    def test_a_run_from_day_zero_is_whole(self, tmp_path):
        store = a_store(tmp_path, whole(0, 5))
        assert history_problems(store, through_day=5, from_day=0) == []
        assert confirm_history_contiguous(store, through_day=5, from_day=0) == (0, 5)
        store.close()

    def test_a_run_that_starts_later_is_whole_from_its_own_first_day(self, tmp_path):
        store = a_store(tmp_path, whole(40, 45, first_block=288_001))
        assert confirm_history_contiguous(store, through_day=45) == (40, 45)
        assert confirm_history_contiguous(store, through_day=45, from_day=40) == (40, 45)
        store.close()

    def test_days_after_through_day_are_ignored(self, tmp_path):
        days = whole(0, 5)
        days[9] = (5_000, 5_099)  # a stray later row must not fail an earlier claim
        store = a_store(tmp_path, days)
        assert history_problems(store, through_day=5, from_day=0) == []
        store.close()


class TestAHoleInTheMiddle:
    def test_a_missing_day_is_named(self, tmp_path):
        days = whole(0, 5)
        del days[3]
        store = a_store(tmp_path, days)
        problems = history_problems(store, through_day=5, from_day=0)
        assert any("day 3 has no recorded scan" in p for p in problems)
        store.close()

    def test_a_run_of_missing_days_is_named_as_a_range(self, tmp_path):
        days = whole(0, 8)
        for day in (3, 4, 5):
            del days[day]
        store = a_store(tmp_path, days)
        problems = history_problems(store, through_day=8, from_day=0)
        assert any("days 3 through 5 have no recorded scan" in p for p in problems)
        store.close()

    def test_a_break_in_the_blocks_between_two_recorded_days_is_named(self, tmp_path):
        days = whole(0, 3)
        days[2] = (days[2][0] + 10, days[2][1] + 10)  # 10 blocks belong to no day
        store = a_store(tmp_path, days)
        problems = history_problems(store, through_day=3, from_day=0)
        assert any("Blocks 201 through 210 are in no recorded day" in p for p in problems)
        store.close()

    def test_two_days_claiming_the_same_blocks_is_named(self, tmp_path):
        days = whole(0, 3)
        days[2] = (days[2][0] - 5, days[2][1])
        store = a_store(tmp_path, days)
        problems = history_problems(store, through_day=3, from_day=0)
        assert any("record some of the same blocks" in p for p in problems)
        store.close()

    def test_every_problem_is_reported_not_just_the_first(self, tmp_path):
        days = whole(0, 9)
        del days[2]
        del days[6]
        store = a_store(tmp_path, days)
        with pytest.raises(HistoryGap) as caught:
            confirm_history_contiguous(store, through_day=9, from_day=0)
        text = str(caught.value)
        assert "day 2 has no recorded scan" in text
        assert "day 6 has no recorded scan" in text
        assert len(caught.value.problems) >= 2
        store.close()


class TestAStoreThatLostItsEnds:
    def test_a_backup_that_stops_early_is_missing_its_recent_days(self, tmp_path):
        store = a_store(tmp_path, whole(0, 3))
        problems = history_problems(store, through_day=6, from_day=0)
        assert problems == [
            "days 4 through 6 have no recorded scan. Only a node that still holds that "
            "state can supply it, and none is invented here."
        ]
        store.close()

    def test_a_store_restored_from_a_later_backup_is_missing_its_start(self, tmp_path):
        # Looks whole from its own first row, so it passes without from_day and
        # fails once the caller says where its history should begin.
        store = a_store(tmp_path, whole(30, 35, first_block=216_001))
        assert history_problems(store, through_day=35) == []
        problems = history_problems(store, through_day=35, from_day=0)
        assert any("days 0 through 29 have no recorded scan" in p for p in problems)
        store.close()

    def test_an_empty_store_holds_none_of_it(self, tmp_path):
        store = a_store(tmp_path, {})
        problems = history_problems(store, through_day=4, from_day=0)
        assert len(problems) == 1
        assert "records no scanned day" in problems[0]
        store.close()

    def test_a_store_whose_earliest_day_is_after_through_day_holds_none_of_it(self, tmp_path):
        store = a_store(tmp_path, whole(10, 12, first_block=900))
        problems = history_problems(store, through_day=5)
        assert "records no scanned day" in problems[0]
        store.close()


class TestDayZero:
    def test_day_zero_must_begin_at_the_chains_first_block(self, tmp_path):
        store = a_store(tmp_path, whole(0, 2, first_block=50))
        problems = history_problems(store, through_day=2, from_day=0)
        assert any("day 0 begins at block 50, but the chain's first block is 1" in p for p in problems)
        store.close()


class TestNothingIsInvented:
    def test_checking_writes_nothing(self, tmp_path):
        days = whole(0, 5)
        del days[3]
        store = a_store(tmp_path, days)
        before = store.scanned_days()
        history_problems(store, through_day=5, from_day=0)
        with pytest.raises(HistoryGap):
            confirm_history_contiguous(store, through_day=5, from_day=0)
        assert store.scanned_days() == before
        store.close()


class TestMissingDays:
    """Issue #309: the structured, act-on-able twin of history_problems's own
    missing-day sentences, for a recovery tool to group into runs."""

    def test_a_whole_record_has_no_missing_days(self, tmp_path):
        store = a_store(tmp_path, whole(0, 5))
        assert missing_days(store, through_day=5, from_day=0) == []
        store.close()

    def test_a_late_first_start_reports_every_day_before_it(self, tmp_path):
        store = a_store(tmp_path, whole(5, 8, first_block=1))
        assert missing_days(store, through_day=8, from_day=0) == [0, 1, 2, 3, 4]
        store.close()

    def test_a_hole_in_the_middle_reports_exactly_that_run(self, tmp_path):
        days = whole(0, 8)
        for day in (3, 4, 5):
            del days[day]
        store = a_store(tmp_path, days)
        assert missing_days(store, through_day=8, from_day=0) == [3, 4, 5]
        store.close()

    def test_two_separate_runs_are_both_reported(self, tmp_path):
        days = whole(0, 10)
        del days[2]
        del days[7]
        store = a_store(tmp_path, days)
        assert missing_days(store, through_day=10, from_day=0) == [2, 7]
        store.close()

    def test_an_empty_store_reports_every_requested_day(self, tmp_path):
        store = a_store(tmp_path, {})
        assert missing_days(store, through_day=3, from_day=0) == [0, 1, 2, 3]
        store.close()

    def test_a_day_recorded_with_a_backward_or_overlapping_range_still_counts_as_held(
        self, tmp_path
    ):
        # missing_days only asks whether a day has a row at all, the same
        # scope check_blocks=False gives history_problems for scoring's own
        # use -- a day's block-range shape is a separate question.
        days = whole(0, 3)
        days[2] = (days[2][0] - 5, days[2][1])  # overlaps day 1
        store = a_store(tmp_path, days)
        assert missing_days(store, through_day=3, from_day=0) == []
        store.close()

    def test_from_day_is_required(self, tmp_path):
        store = a_store(tmp_path, whole(0, 3))
        with pytest.raises(TypeError):
            missing_days(store, through_day=3)
        store.close()

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "3", None])
    def test_through_day_must_be_a_whole_day_number(self, tmp_path, bad):
        store = a_store(tmp_path, whole(0, 2))
        with pytest.raises(ValueError):
            missing_days(store, through_day=bad, from_day=0)
        store.close()

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "3", None])
    def test_from_day_must_be_a_whole_day_number(self, tmp_path, bad):
        store = a_store(tmp_path, whole(0, 2))
        with pytest.raises(ValueError):
            missing_days(store, through_day=2, from_day=bad)
        store.close()

    def test_from_day_after_through_day_is_refused(self, tmp_path):
        store = a_store(tmp_path, whole(0, 2))
        with pytest.raises(ValueError):
            missing_days(store, through_day=2, from_day=5)
        store.close()


class TestBadArguments:
    @pytest.mark.parametrize("bad", [-1, True, 1.5, "3", None])
    def test_through_day_must_be_a_whole_day_number(self, tmp_path, bad):
        store = a_store(tmp_path, whole(0, 2))
        with pytest.raises(ValueError):
            history_problems(store, through_day=bad)
        store.close()

    def test_from_day_after_through_day_is_refused(self, tmp_path):
        store = a_store(tmp_path, whole(0, 2))
        with pytest.raises(ValueError):
            history_problems(store, through_day=2, from_day=5)
        store.close()
