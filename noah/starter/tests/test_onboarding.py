"""Real-store ingestion for registered wallets that have never paid or earned."""

import pytest

from noah.chain.fixture_build import build_fixture
from noah.chain.holdings import Share
from noah.chain.runner import HoldingSampleReorged, StakingHotkeysIncomplete, run_day
from noah.chain.store import ColdkeyNotSampled
from noah.chain.tests.test_epoch_collection import FIRST, LAST, collect
from noah.chain.tests.test_runner import (
    ANCHOR,
    ARK,
    GIVER,
    HOTKEY,
    NETUID,
    HoldingsChain,
    a_deposit,
    a_store,
)
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture
from noah.starter.migration import FrozenEntries, freeze

OTHER = "registered-never-funded"
OTHER_HOTKEY = "registered-hotkey-2"


class RegisteredChain(HoldingsChain):
    def genesis_hash(self):
        return "0xgenesis"

    def seat_hotkeys(self, block):
        self.asked.append(("seats", block))
        return {1: HOTKEY}

    def hotkey_owners(self, hotkeys, block):
        self.asked.append(("owners", block))
        return {hot: GIVER for hot in hotkeys}


def full(store, chain, **kwargs):
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
        **kwargs,
    )


def test_never_active_registered_wallet_is_sampled_without_fabricating_activity(tmp_path):
    store = a_store(tmp_path)
    chain = RegisteredChain()
    full(store, chain, sample_registered=True)
    assert store.wallets_seen() == []
    assert store.first_activity_day(GIVER) is None
    assert store.holding_by_hotkey(GIVER, 0) == {HOTKEY: 1_000_000_000}
    for block in range(FIRST, LAST + 1, 2):
        assert ("seats", block) in chain.asked and ("owners", block) in chain.asked


def test_completed_legacy_day_repairs_missing_registered_cohort_without_event_replay(tmp_path):
    store = a_store(tmp_path)
    chain = RegisteredChain()
    full(store, chain)
    with pytest.raises(ColdkeyNotSampled):
        store.holding_by_hotkey(GIVER, 0)
    event_reads = [q for q in chain.asked if q[0] == "events"]
    full(store, chain, sample_registered=True)
    assert [q for q in chain.asked if q[0] == "events"] == event_reads
    assert store.holding_by_hotkey(GIVER, 0)[HOTKEY] == 1_000_000_000


def test_partial_legacy_cursor_does_not_skip_new_registered_wallet(tmp_path):
    store = a_store(tmp_path)
    chain = RegisteredChain()
    collect(store, chain, FIRST + 3)
    assert chain.share_calls == []
    collect(store, chain, finalize=True, sample_registered=True)
    assert len([q for q in chain.asked if q[0] == "events"]) == LAST - FIRST + 1
    assert store.holding_by_hotkey(GIVER, 0)[HOTKEY] == 1_000_000_000


def test_late_new_wallet_waits_for_the_next_days_first_real_sample(tmp_path):
    class LateRegistration(RegisteredChain):
        def seat_hotkeys(self, block):
            return {1: HOTKEY} if block >= LAST else {}

    store = a_store(tmp_path)
    chain = LateRegistration()
    full(store, chain, sample_registered=True)
    assert store._conn.execute("SELECT count(*) FROM holding_sample").fetchone()[0] == 0
    with pytest.raises(ColdkeyNotSampled):
        store.holding_by_hotkey(GIVER, 0)
    chain.block_timestamp = lambda block: ANCHOR + 86400 + (block - FIRST - 10) * 12
    run_day(
        store,
        chain,
        day=1,
        from_block=FIRST + 10,
        to_block=LAST + 10,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"a": "a", "b": "b"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
        sample_registered=True,
    )
    assert store._conn.execute("SELECT min(block) FROM holding_sample").fetchone()[0] == FIRST + 10
    assert store.holding_by_hotkey(GIVER, 1)[HOTKEY] == 1_000_000_000


def test_starter_sampling_preserves_established_wallet_grid_and_holding_minimum(tmp_path):
    class EndBlockDrop(RegisteredChain):
        def hotkey_alpha(self, hotkey, block):
            return 1 if block == LAST else 1_000_000_000

    expected = [(GIVER, block, 1_000_000_000) for block in range(FIRST, LAST + 1, 2)]
    for enabled in (False, True):
        store = a_store(tmp_path, f"holdings-{enabled}.db")
        full(store, EndBlockDrop(deposits={FIRST + 1: a_deposit()}), sample_registered=enabled)
        assert (
            store._conn.execute(
                "SELECT coldkey,block,total_rao FROM holding_sample ORDER BY block"
            ).fetchall()
            == expected
        )
        assert store.holding_by_hotkey(GIVER, 0)[HOTKEY] == 1_000_000_000


def test_registered_hotkey_invalidates_old_wallet_cache_and_proves_enumeration(tmp_path):
    class OmittedHotkey(RegisteredChain):
        def seat_hotkeys(self, block):
            return {1: HOTKEY, 2: OTHER_HOTKEY}

        def alpha_share(self, hotkey, coldkey, block):
            if hotkey == OTHER_HOTKEY:
                return Share(encoding="u64f64", raw=1, exponent=0, source="v1")
            return super().alpha_share(hotkey, coldkey, block)

    store = a_store(tmp_path)
    chain = OmittedHotkey(deposits={FIRST + 1: a_deposit()})
    collect(store, chain, FIRST + 3)
    with pytest.raises(StakingHotkeysIncomplete, match="does not list"):
        collect(store, chain, FIRST + 3, sample_registered=True)
    assert not store.day_was_scanned(0)


def test_roster_ownership_is_read_at_each_historical_sample_not_current_head(tmp_path):
    class ChangingOwner(RegisteredChain):
        def hotkey_owners(self, hotkeys, block):
            return {hot: GIVER if block < FIRST + 4 else OTHER for hot in hotkeys}

        def staking_hotkeys(self, coldkey, block):
            return [HOTKEY]

    store = a_store(tmp_path)
    full(store, ChangingOwner(), sample_registered=True)
    rows = store._conn.execute("SELECT coldkey,block FROM holding_sample ORDER BY block").fetchall()
    assert rows == [
        (GIVER, FIRST),
        (GIVER, FIRST + 2),
        (OTHER, FIRST + 4),
        (OTHER, FIRST + 6),
        (OTHER, FIRST + 8),
    ]


def test_registered_owner_read_reorg_does_not_commit_a_sample(tmp_path):
    class Reorg(RegisteredChain):
        moved = False

        def hotkey_owners(self, hotkeys, block):
            self.moved = True
            return super().hotkey_owners(hotkeys, block)

        def block_hash(self, block):
            return "changed" if self.moved else super().block_hash(block)

    store = a_store(tmp_path)
    with pytest.raises(HoldingSampleReorged):
        full(store, Reorg(), sample_registered=True)
    assert store._conn.execute("SELECT count(*) FROM holding_sample").fetchone()[0] == 0
    assert not store.day_was_scanned(0)


def test_registration_and_real_holdings_admit_zero_entry_shared_wallet_to_daily_fixture(tmp_path):
    class TwoSeats(RegisteredChain):
        def seat_hotkeys(self, block):
            return {1: HOTKEY, 2: OTHER_HOTKEY, 0: "owner-hotkey"}

        def hotkey_owners(self, hotkeys, block):
            return {hot: ARK if hot == "owner-hotkey" else GIVER for hot in hotkeys}

        def staking_hotkeys(self, coldkey, block):
            return [HOTKEY, OTHER_HOTKEY] if coldkey == GIVER else []

    store = a_store(tmp_path)
    chain = TwoSeats()
    full(store, chain)
    registrations = {GIVER: {HOTKEY: 0, OTHER_HOTKEY: 0}, ARK: {"owner-hotkey": 0}}
    snapshot = freeze(
        store,
        registrations,
        genesis_hash="0xgenesis",
        netuid=NETUID,
        finalized_block=LAST,
        finalized_hash=chain.block_hash(LAST),
        as_of_day=0,
    )
    # The pre-policy complete day omitted its never-active registered cohort.
    # Migration must not need invented or backdated historical holding samples.
    assert store._conn.execute("SELECT count(*) FROM holding_sample").fetchone()[0] == 0
    chain.block_timestamp = lambda block: ANCHOR + 86400 + (block - FIRST - 10) * 12
    run_day(
        store,
        chain,
        day=1,
        from_block=FIRST + 10,
        to_block=LAST + 10,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"a": "a", "b": "b"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
        sample_registered=True,
    )
    frozen = FrozenEntries(
        store,
        snapshot,
        genesis_hash="0xgenesis",
        netuid=NETUID,
        registration_days=registrations,
        as_of_day=1,
    )
    dials = {
        "targetFp": "500000000",
        "harshness": 1,
        "capFp": "1000000000",
        "halfLifeDays": "182",
        "streakCeilingFp": "2000000000",
        "streakRampDays": "365",
        "ongoingHoldRao": "0",
    }
    fixture = build_fixture(
        frozen,
        "registered",
        1,
        ANCHOR,
        dials,
        {GIVER: {HOTKEY: 1, OTHER_HOTKEY: 2}, ARK: {"owner-hotkey": 0}},
        held_out={ARK},
        entry_deposits=frozen.credited,
        registration_days=registrations,
    )
    rows = fixture["miners"]
    assert {row["uid"] for row in rows} == {1, 2}
    assert all(row["entryDepositRao"] == "0" and row["holdRao"] == "1000000000" for row in rows)
    assert all(row["firstActivityDay"] == "1" and set(row["days"]) == {"1"} for row in rows)
    assert store.wallets_seen() == [] and store.first_activity_day(GIVER) is None
    result = score_fixture(parse_fixture(fixture))
    assert all(row["doorOpen"] for row in result["miners"])


def test_registration_fallback_does_not_move_established_wallet_first_activity(tmp_path):
    store = a_store(tmp_path)
    full(store, RegisteredChain(), sample_registered=True)
    store.record_noah_deposit(
        coldkey=GIVER,
        hotkey=HOTKEY,
        block_timestamp=ANCHOR + 86400 + 12,
        amount_rao=1_000_000_000,
        block=LAST + 1,
        event_ordinal=0,
        extrinsic_index=1,
        block_hash="day1-deposit",
        carried_by="StakeTransferred",
    )
    store.mark_day_scanned(1, from_block=LAST + 1, to_block=LAST + 10)
    registrations = {GIVER: {HOTKEY: 0}}
    snapshot = freeze(
        store,
        registrations,
        genesis_hash="g",
        netuid=NETUID,
        finalized_block=LAST + 10,
        finalized_hash="h",
        as_of_day=1,
    )
    frozen = FrozenEntries(
        store,
        snapshot,
        genesis_hash="g",
        netuid=NETUID,
        registration_days=registrations,
        as_of_day=1,
    )
    assert frozen.first_activity_day(GIVER) == store.first_activity_day(GIVER) == 1
