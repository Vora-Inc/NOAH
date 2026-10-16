"""History-readiness helpers on the store (issues #237 and #239).

A mainnet node keeps only about 90 days of state, so ``history.db`` is the only
place old days, the first-emission time and the registration days live. These
tests cover the store's own records; the callers that rely on them are tested
where they live.
"""

import pytest

from noah.chain.store import Store

ANCHOR = 1679270400


def a_store(tmp_path, name="history.db"):
    return Store.create(tmp_path / name, day_anchor_ts=ANCHOR)


class TestScannedDayContaining:
    def test_finds_the_day_whose_recorded_range_holds_the_block(self, tmp_path):
        store = a_store(tmp_path)
        store.mark_day_scanned(0, 1, 5_640)
        store.mark_day_scanned(1, 5_641, 12_840)
        assert store.scanned_day_containing(1) == (0, 1, 5_640)
        assert store.scanned_day_containing(5_640) == (0, 1, 5_640)
        assert store.scanned_day_containing(5_641) == (1, 5_641, 12_840)
        assert store.scanned_day_containing(9_000) == (1, 5_641, 12_840)
        store.close()

    def test_a_block_in_no_recorded_range_is_none(self, tmp_path):
        store = a_store(tmp_path)
        store.mark_day_scanned(3, 100, 199)
        assert store.scanned_day_containing(99) is None
        assert store.scanned_day_containing(200) is None
        store.close()


class TestRegistrationBlockDay:
    def test_a_block_never_recorded_has_no_day(self, tmp_path):
        store = a_store(tmp_path)
        assert store.registration_block_day(100) is None
        store.close()

    def test_records_once_and_reads_back(self, tmp_path):
        store = a_store(tmp_path)
        assert store.record_registration_block_day(100, ANCHOR + 3 * 86_400 + 5) == 3
        assert store.registration_block_day(100) == 3
        # The same fact again is a no-op.
        assert store.record_registration_block_day(100, ANCHOR + 3 * 86_400 + 9) == 3
        store.close()

    def test_the_record_survives_reopening_the_store(self, tmp_path):
        store = a_store(tmp_path)
        store.record_registration_block_day(100, ANCHOR + 3 * 86_400)
        store.close()
        reopened = Store.open(tmp_path / "history.db", day_anchor_ts=ANCHOR)
        assert reopened.registration_block_day(100) == 3
        reopened.close()

    def test_a_different_day_for_a_recorded_block_is_refused_and_changes_nothing(self, tmp_path):
        from noah.chain.store import RegistrationDayChanged

        store = a_store(tmp_path)
        store.record_registration_block_day(100, ANCHOR + 3 * 86_400)
        with pytest.raises(RegistrationDayChanged):
            store.record_registration_block_day(100, ANCHOR + 4 * 86_400)
        assert store.registration_block_day(100) == 3
        store.close()

    def test_an_unsupported_schema_is_not_silently_upgraded_by_create(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        store = Store.create(path, day_anchor_ts=ANCHOR)
        store.close()
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE registration_day")
        conn.execute("PRAGMA user_version = 16")
        conn.commit()
        conn.close()
        from noah.chain.store import SchemaTooOld

        with pytest.raises(SchemaTooOld):
            Store.open(path, day_anchor_ts=ANCHOR)
        before = path.read_bytes()
        with pytest.raises(SchemaTooOld):
            Store.create(path, day_anchor_ts=ANCHOR)
        assert path.read_bytes() == before


class TestRegistrationBlockOnDay:
    """Issue #309: a real block a gap-recovery tool can resume from, read
    back from what ``record_registration_block_day`` already recorded."""

    def test_a_day_with_no_registration_has_no_block(self, tmp_path):
        store = a_store(tmp_path)
        assert store.registration_block_on_day(5) is None
        store.close()

    def test_reads_back_the_block_recorded_for_that_day(self, tmp_path):
        store = a_store(tmp_path)
        store.record_registration_block_day(100, ANCHOR + 3 * 86_400 + 5)
        assert store.registration_block_on_day(3) == 100
        assert store.registration_block_on_day(2) is None
        assert store.registration_block_on_day(4) is None
        store.close()

    def test_the_lowest_of_several_seats_registered_the_same_day_is_returned(self, tmp_path):
        store = a_store(tmp_path)
        store.record_registration_block_day(500, ANCHOR + 3 * 86_400 + 50)
        store.record_registration_block_day(150, ANCHOR + 3 * 86_400 + 5)
        store.record_registration_block_day(900, ANCHOR + 3 * 86_400 + 90)
        assert store.registration_block_on_day(3) == 150
        store.close()

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "3", None])
    def test_day_must_be_a_whole_day_number(self, tmp_path, bad):
        from noah.chain.store import NotAnAmount

        store = a_store(tmp_path)
        with pytest.raises(NotAnAmount):
            store.registration_block_on_day(bad)
        store.close()


class TestScannedDays:
    def test_lists_every_recorded_scan_oldest_day_first(self, tmp_path):
        store = a_store(tmp_path)
        assert store.scanned_days() == []
        store.mark_day_scanned(2, 201, 300)
        store.mark_day_scanned(0, 1, 100)
        store.mark_day_scanned(1, 101, 200)
        assert store.scanned_days() == [(0, 1, 100), (1, 101, 200), (2, 201, 300)]
        store.close()
