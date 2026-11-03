import copy
import json
from pathlib import Path

import pytest

from noah.chain.store import DAY_SECONDS, Store
from noah.chain.tests._agreeing_helpers import SEAT0_COLD, SEAT0_HOT
from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
from noah.core.tests._adapter_helpers import FLOORED_ANCHOR
from noah.research.entry_migration import LEGACY_ENTRY, FrozenEntries, freeze, save_new
from noah.research.starter_epoch import digest


def _a_store(tmp_path):
    return Store.create(Path(tmp_path) / "entry-deposits.db", day_anchor_ts=FLOORED_ANCHOR)


R = 10**9
REG = {SEAT0_COLD: {SEAT0_HOT: 0}}


@pytest.fixture
def store(tmp_path):
    value = _a_store(str(tmp_path))
    for day, amount in ((0, 4 * R), (1, 9 * R), (2, 20 * R)):
        value.mark_day_scanned(day, day + 100, day + 100)
        value.record_noah_deposit(
            coldkey=SEAT0_COLD,
            hotkey=SEAT0_HOT,
            block_timestamp=FLOORED_ANCHOR + day * DAY_SECONDS + 1,
            amount_rao=amount,
            block=day + 100,
            event_ordinal=0,
            extrinsic_index=0,
            block_hash=f"0x{day + 100:x}",
            carried_by="StakeAdded",
        )
    yield value
    value.close()


def snapshot(store, day=1):
    return freeze(
        store,
        REG,
        genesis_hash="test-genesis",
        netuid=82,
        finalized_block=101,
        finalized_hash="test-finalized",
        as_of_day=day,
    )


def view(store, saved, **kw):
    return FrozenEntries(
        store,
        saved,
        genesis_hash="test-genesis",
        netuid=82,
        registration_days=kw.get("reg", REG),
        as_of_day=kw.get("day", 2),
    )


def test_real_store_preserves_entry_amount_days_and_original_contributions(store):
    saved = snapshot(store)
    frozen = view(store, saved)
    credited, days = _entry_deposits_for(store, REG, LEGACY_ENTRY, as_of_day=1)
    old = _ExcludingEntryDeposit(store, credited, days)
    assert frozen.credited == credited == {SEAT0_COLD: {SEAT0_HOT: 10 * R}}
    assert frozen.entry_days == days == {SEAT0_COLD: {SEAT0_HOT: 1}}
    for d, expected in ((0, 0), (1, 3 * R), (2, 20 * R)):
        assert frozen.noah_deposits_for_hotkey(SEAT0_COLD, SEAT0_HOT, d) == expected
        assert frozen.noah_deposits_for_hotkey(
            SEAT0_COLD, SEAT0_HOT, d
        ) == old.noah_deposits_for_hotkey(SEAT0_COLD, SEAT0_HOT, d)
        assert frozen.noah_deposits(SEAT0_COLD, d) == old.noah_deposits(SEAT0_COLD, d)
    # Historical ten cannot become five entry plus five extra ordinary credit.
    assert sum(saved["payload"]["records"][0]["allocation"].values()) == LEGACY_ENTRY


def test_future_receipts_do_not_retroactively_qualify_partial_entry(store):
    saved = snapshot(store, day=0)
    frozen = view(store, saved)
    assert frozen.credited == {}
    assert frozen.noah_deposits_for_hotkey(SEAT0_COLD, SEAT0_HOT, 0) == 4 * R
    assert frozen.noah_deposits_for_hotkey(SEAT0_COLD, SEAT0_HOT, 1) == 9 * R


def test_re_registration_never_awards_legacy_credit_again(store):
    frozen = view(store, snapshot(store), reg={SEAT0_COLD: {SEAT0_HOT: 2}})
    assert frozen.credited == {}
    assert frozen.noah_deposits_for_hotkey(SEAT0_COLD, SEAT0_HOT, 1) == 3 * R


def test_migration_is_deterministic_survives_roundtrip_and_refuses_overwrite(store, tmp_path):
    saved = snapshot(store)
    assert snapshot(store) == saved
    dst = tmp_path / "migration.json"
    save_new(dst, saved)
    assert view(store, json.loads(dst.read_text())).credited == view(store, saved).credited
    before = dst.read_bytes()
    with pytest.raises(FileExistsError):
        save_new(dst, saved)
    assert dst.read_bytes() == before


def test_changed_hash_chain_missing_receipts_and_future_cutoff_refuse(store):
    saved = snapshot(store)
    changed = copy.deepcopy(saved)
    changed["payload"]["records"][0]["amountRao"] = 5 * R
    with pytest.raises(ValueError, match="digest"):
        view(store, changed)
    with pytest.raises(ValueError, match="cutoff"):
        view(store, saved, day=0)
    changed = copy.deepcopy(saved)
    changed["payload"]["netuid"] = 1
    changed["sha256"] = digest(changed["payload"])
    with pytest.raises(ValueError, match="identity"):
        view(store, changed)
    changed = copy.deepcopy(saved)
    changed["payload"]["records"][0]["allocation"] = {"0": 10 * R}
    changed["sha256"] = digest(changed["payload"])
    with pytest.raises(ValueError, match="supported by receipts"):
        view(store, changed)


def test_missing_scanned_history_blocks_freeze(store):
    # The store's actual history gap check must propagate, not produce a zero-credit snapshot.
    from noah.chain.store import DayNotScanned

    with pytest.raises(DayNotScanned):
        freeze(
            store,
            {SEAT0_COLD: {"new-hotkey": 0}},
            genesis_hash="test",
            netuid=82,
            finalized_block=100,
            finalized_hash="block",
            as_of_day=3,
        )
