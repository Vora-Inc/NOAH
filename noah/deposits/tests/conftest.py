"""Stores the report tests read from, built once and shared."""

import importlib.util

import pytest

if importlib.util.find_spec("noah.deposits.d143") is not None:
    import noah.deposits.d143 as d143
else:
    d143 = None
import noah.deposits.entry_gate as entry_gate
from noah.chain.store import Store

ANCHOR = 1_699_920_000  # 2023-11-14 00:00:00 UTC -- already midnight-aligned
DAY_SECONDS = 86_400
WALLET = "5Cold"
HOTKEY = "5Hot"
BLOCK_HASH = "0x" + "ef" * 32


def _paid(store, day, amount):
    store.record_attributed_pay(
        uid=0,
        block=day * 100 + 10,
        coldkey=WALLET,
        hotkey=HOTKEY,
        block_timestamp=ANCHOR + day * DAY_SECONDS + 3600,
        amount_rao=amount,
        block_hash=BLOCK_HASH,
    )


@pytest.fixture
def a_store(tmp_path):
    """Paid in both days, deposited in neither, so there is ground to make up."""
    store = Store.create(tmp_path / "noah.db", day_anchor_ts=ANCHOR)
    for day in (0, 1):
        store.mark_day_scanned(day, from_block=day * 100 + 1, to_block=day * 100 + 99)
        _paid(store, day, 1_000_000)
    return store


@pytest.fixture
def a_store_missing_a_day(tmp_path):
    """Day 1 was never scanned, so nothing about it is known."""
    store = Store.create(tmp_path / "noah2.db", day_anchor_ts=ANCHOR)
    store.mark_day_scanned(0, from_block=1, to_block=99)
    _paid(store, 0, 1_000_000)
    return store


@pytest.fixture(autouse=True)
def default_profile_figures(monkeypatch):
    """Keep historical test amounts explicit and independent of optional modules."""
    entry = 100 * 1_000_000_000
    holding_floor = 400 * 1_000_000_000
    if d143 is not None:
        monkeypatch.setattr(d143, "entry_deposit_rao", lambda: entry)
        monkeypatch.setattr(d143, "ongoing_hold_rao", lambda: holding_floor)
    monkeypatch.setattr(entry_gate, "entry_deposit_rao", lambda: entry)
