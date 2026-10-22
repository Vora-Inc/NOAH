"""Offline real-reader/store parity and interruption recovery."""

from unittest.mock import patch

import pytest

from noah.chain.collection import CollectionRefused
from noah.chain.runner import RunRefused, run_day
from noah.chain.store import DayNotScanned

from .test_runner import ARK, GIVER, HOTKEY, NETUID, Chain, a_deposit, a_store

FIRST = 8_800_000
LAST = FIRST + 9


class FinalizedChain(Chain):
    def genesis_hash(self):
        return "0xgenesis"


def collect(store, chain, last=LAST, finalize=False, **kwargs):
    with patch("noah.chain.dayblocks.day_block_range", return_value=(0, FIRST, LAST)):
        return run_day(
            store,
            chain,
            day=0,
            from_block=FIRST,
            to_block=last,
            ark_coldkey=ARK,
            designated_validator_hotkey=HOTKEY,
            sources={"a": "a", "b": "b"},
            netuid=NETUID,
            holding_stride=2,
            read_collateral=True,
            progressive=True,
            finalize=finalize,
            **kwargs,
        )


def full(store, chain):
    return run_day(
        store,
        chain,
        day=0,
        from_block=FIRST,
        to_block=LAST,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"a": "a", "b": "b"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
    )


def facts(store):
    tables = [
        r[0]
        for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not r[0].startswith("epoch_")
    ]
    return {
        t: sorted(store._conn.execute(f'SELECT * FROM "{t}"').fetchall(), key=repr) for t in tables
    }


def test_chunks_equal_full_day_with_deposits_holdings_without_second_event_scan(tmp_path):
    a, b = a_store(tmp_path, "a.db"), a_store(tmp_path, "b.db")
    deposits = {FIRST + 1: a_deposit(), FIRST + 7: a_deposit(xt=8)}
    c = FinalizedChain(deposits=deposits)
    collect(a, c, FIRST + 3)
    assert not a.day_was_scanned(0)
    with pytest.raises(DayNotScanned):
        a.noah_deposits(GIVER, 0)
    collect(a, c, FIRST + 6)
    collect(a, c, finalize=True)
    assert len([x for x in c.asked if x[0] == "events"]) == 10
    full(b, FinalizedChain(deposits=deposits))
    assert facts(a) == facts(b)


def test_restart_resumes_contiguous_blocks(tmp_path):
    s = a_store(tmp_path)
    with pytest.raises(RunRefused):
        collect(s, FinalizedChain(fail_at=FIRST + 4))
    assert not s.day_was_scanned(0)
    c = FinalizedChain()
    collect(s, c, finalize=True)
    assert [x[1] for x in c.asked if x[0] == "events"] == list(range(FIRST + 4, LAST + 1))


def test_cursor_write_failure_replays_receipt_without_double_credit(tmp_path):
    s = a_store(tmp_path)
    c = FinalizedChain(deposits={FIRST: a_deposit()})
    with patch(
        "noah.chain.collection.CollectionProgress.advance", side_effect=OSError("disk full")
    ):
        with pytest.raises(RunRefused):
            collect(s, c)
    collect(s, c, finalize=True)
    assert s.noah_deposits(GIVER, 0) == 469_790_773_712


def test_changed_finalized_hash_refuses(tmp_path):
    s = a_store(tmp_path)
    collect(s, FinalizedChain(), FIRST + 3)
    c = FinalizedChain()
    old = c.block_hash
    c.block_hash = lambda b: "changed" if b == FIRST + 3 else old(b)
    with pytest.raises(CollectionRefused):
        collect(s, c, finalize=True)
    assert not s.day_was_scanned(0)


def test_changed_settings_refuse(tmp_path):
    s = a_store(tmp_path)
    collect(s, FinalizedChain(), FIRST + 3)
    with pytest.raises(CollectionRefused):
        collect(s, FinalizedChain(), attribute_deposits=True)


def test_partial_range_cannot_be_finalized(tmp_path):
    s = a_store(tmp_path)
    with pytest.raises(RunRefused, match="entire closed day"):
        collect(s, FinalizedChain(), FIRST + 3, True)
    assert not s.day_was_scanned(0)


def test_unfinalized_target_refused(tmp_path):
    with pytest.raises(CollectionRefused, match="not finalized"):
        collect(a_store(tmp_path), FinalizedChain(head=FIRST + 2))


def test_legacy_reader_replays_partial_collection(tmp_path):
    s = a_store(tmp_path)
    c = FinalizedChain(deposits={FIRST + 1: a_deposit()})
    collect(s, c, FIRST + 3)
    full(s, c)
    assert s.day_was_scanned(0) and s.noah_deposits(GIVER, 0) == 469_790_773_712


def test_fixed_stride_holdings_are_read_once_and_equal_full_scan(tmp_path):
    from .test_runner import HoldingsChain

    class FinalizedHoldings(HoldingsChain):
        def genesis_hash(self):
            return "0xgenesis"

    a, b = a_store(tmp_path, "a.db"), a_store(tmp_path, "b.db")
    c = FinalizedHoldings(deposits={FIRST + 1: a_deposit()})
    collect(a, c, FIRST + 3)
    collect(a, c, FIRST + 6)
    collect(a, c, finalize=True)
    full(b, FinalizedHoldings(deposits={FIRST + 1: a_deposit()}))
    assert facts(a) == facts(b)
    assert a.holding_by_hotkey(GIVER, 0) == {HOTKEY: 1_000_000_000}
    for block in range(FIRST, LAST + 1, 2):
        assert c.share_calls.count(("alpha_share", HOTKEY, GIVER, block)) == 1


def test_late_wallet_discovery_backfills_prior_samples(tmp_path):
    from .test_runner import HoldingsChain

    class FinalizedHoldings(HoldingsChain):
        def genesis_hash(self):
            return "0xgenesis"

    a, b = a_store(tmp_path, "a.db"), a_store(tmp_path, "b.db")
    c = FinalizedHoldings(deposits={FIRST + 7: a_deposit()})
    collect(a, c, FIRST + 3)
    assert c.share_calls == []
    collect(a, c, finalize=True)
    full(b, FinalizedHoldings(deposits={FIRST + 7: a_deposit()}))
    assert facts(a) == facts(b)
    assert a.holding_by_hotkey(GIVER, 0) == {HOTKEY: 1_000_000_000}


def test_real_midnight_boundary_closes_only_complete_day(tmp_path):
    from .test_runner import ANCHOR

    s = a_store(tmp_path)
    c = FinalizedChain(head=LAST + 2)
    c.block_timestamp = lambda b: ANCHOR + max(1, 86400 + (b - FIRST) * 8640)
    run_day(
        s,
        c,
        day=1,
        from_block=FIRST,
        to_block=LAST,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"a": "a", "b": "b"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
        progressive=True,
        finalize=True,
    )
    assert s.day_was_scanned(1)


def test_incomplete_cache_schema_refuses_reopen(tmp_path):
    from noah.chain.store import SchemaTooOld

    s = a_store(tmp_path)
    collect(s, FinalizedChain(), FIRST + 3)
    with s._conn:
        s._conn.execute("DROP TABLE epoch_holding_cache")
    s.close()
    with pytest.raises(SchemaTooOld):
        a_store(tmp_path)
