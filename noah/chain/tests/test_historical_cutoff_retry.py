"""A delayed daily read keeps its original inputs after open-day collection.

Only the network boundary is synthetic. The adapter, epoch collector, SQLite
ledger, entry policy, holdings sampler, fixture, score and lineage capture are
real. No publisher or signing object is constructed.
"""

import copy
from types import SimpleNamespace

import pytest

from noah.chain.entry_migration import FrozenEntries, freeze
from noah.chain.store import NotAnAmount, Store
from noah.chain.tests.test_runner import ANCHOR, ARK, GIVER, HOTKEY, NETUID
from noah.core.chain_adapter import ChainAdapter
from noah.core.epoch_work import collect
from noah.starter.entry import EntryPolicy
from noah.starter.tests.test_onboarding import RegisteredChain

DIALS = {
    "targetFp": "500000000",
    "harshness": 1,
    "capFp": "1000000000",
    "halfLifeDays": "182",
    "streakCeilingFp": "2000000000",
    "streakRampDays": "365",
    "ongoingHoldRao": "0",
}


def pay_event(amounts, netuid):
    return dict(
        ordinal_in_block=3,
        extrinsic_idx=1,
        module_id="SubtensorModule",
        event_id="IncentiveAlphaEmittedToMiners",
        attributes={"netuid": netuid, "emissions": amounts},
    )


class RetryChain(RegisteredChain):
    endpoints = {"primary": "synthetic-primary", "secondary": "synthetic-secondary"}

    def __init__(self):
        super().__init__(head=20)

    def block_timestamp(self, block):
        if block == 0:
            return ANCHOR - 1
        day, offset = divmod(block - 1, 10)
        return ANCHOR + day * 86400 + (86399 if offset == 9 else offset * 100)

    def seat_hotkeys(self, block):
        return {0: HOTKEY} if block >= 15 else {}

    def registration_blocks(self, block):
        return {0: 15} if block >= 15 else {}

    def hotkey_alpha(self, hotkey, block):
        return 1 if block < 15 else 1_000_000_000

    def first_emission_block(self, block):
        return 1

    def published_ark_pin(self, block):
        return ARK

    def subnet_owner(self, block):
        return ARK, ARK

    def mechanism_count(self, block):
        return 1

    def subnetwork_n(self, block):
        return len(self.seat_hotkeys(block))

    def events(self, block):
        self.asked.append(("events", block))
        return [pay_event([100], netuid=NETUID)] if block == 23 else []


def rig(tmp_path, monkeypatch):
    # Source agreement itself has independent tests; this test exercises the
    # normal production accounting path using deterministic network responses.
    monkeypatch.setattr("noah.core.chain_adapter.require_dual_sourced", lambda chain: chain)
    path = tmp_path / "history.db"
    store = Store.create(path, day_anchor_ts=ANCHOR)
    store.record_emission_start(block=1, block_timestamp=ANCHOR, block_hash="0x1")
    store.mark_day_scanned(0, 1, 10)
    store.mark_day_sampled(0, stride=2, from_block=1, to_block=10)
    snapshot = freeze(
        store,
        {},
        genesis_hash="0xgenesis",
        netuid=NETUID,
        finalized_block=10,
        finalized_hash="0xa",
        as_of_day=0,
    )
    store.close()
    opened = []

    def factory():
        s = Store.open(path, day_anchor_ts=ANCHOR)
        opened.append(s)
        return s

    chain = RetryChain()
    adapter = ChainAdapter(
        reader=None,
        publisher=None,
        runner_chain=chain,
        netuid=NETUID,
        uid=0,
        wallet_hotkey=None,
        settle_blocks=0,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        dials=DIALS,
        day_anchor_ts=ANCHOR,
        seats_source=lambda day, s: {GIVER: {HOTKEY: 0}},
        holding_stride=2,
        read_collateral=True,
        day_window=lambda height: (1, 11, 20),
        store_factory=factory,
        entry_deposit_rao=10_000_000_000,
        publish_mode="direct",
    )
    adapter.starter_entry_policy = EntryPolicy(snapshot)
    adapter.capture_points_lineages = True
    return adapter, chain, opened, path


@pytest.mark.parametrize("refused_close", [False, True])
def test_normal_daily_retry_after_real_open_day_collection_preserves_full_inputs(
    tmp_path, monkeypatch, refused_close
):
    adapter, chain, opened, path = rig(tmp_path, monkeypatch)
    try:
        original = adapter.read(20)
        original_score = adapter.score(original)
        original_lineages = copy.deepcopy(adapter._points_lineage_capture)
        assert len(original["miners"]) == 1
        assert original["miners"][0]["holdRao"] == "1000000000"
        assert original["miners"][0]["firstActivityDay"] == "1"
        before = (
            opened[-1]
            ._conn.execute("SELECT block,total_rao FROM holding_sample WHERE day=1 ORDER BY block")
            .fetchall()
        )
        if refused_close:
            # Failure after ingestion but before fixture agreement/publication.
            # The normal loop's subsequent maintenance may collect the open day.
            with monkeypatch.context() as patch:

                def failed(*args, **kwargs):
                    raise OSError("simulated interrupted daily calculation")

                patch.setattr("noah.chain.fixture_build.build_fixture", failed)
                with pytest.raises(OSError):
                    adapter.read(20)
        chain.head = 25
        outcome = collect(adapter, 25)
        assert outcome["collected_through"] == 25
        future = Store.open(path, day_anchor_ts=ANCHOR)
        assert future.first_activity_day(GIVER) == 2
        future_rows = future._conn.execute("SELECT * FROM attributed_pay WHERE day=2").fetchall()
        assert len(future_rows) == 1
        prefix = future._conn.execute("SELECT body FROM epoch_collection WHERE day=2").fetchone()
        assert prefix is not None and not future.day_was_scanned(2)
        future.close()
        retried = adapter.read(20)
        assert retried == original
        assert adapter.score(retried) == original_score
        assert adapter._points_lineage_capture == original_lineages
        assert (
            opened[-1]
            ._conn.execute("SELECT block,total_rao FROM holding_sample WHERE day=1 ORDER BY block")
            .fetchall()
            == before
        )
        assert (
            opened[-1]._conn.execute("SELECT * FROM attributed_pay WHERE day=2").fetchall()
            == future_rows
        )
        assert (
            opened[-1]._conn.execute("SELECT body FROM epoch_collection WHERE day=2").fetchone()
            == prefix
        )
        assert len(original_lineages["records"]) == 1
    finally:
        for store in opened:
            store.close()


def record_pay(store, day, amount=1):
    store.record_attributed_pay(
        GIVER, HOTKEY, 0, ANCHOR + day * 86400 + 100, amount, 1000 + day, f"pay-{day}"
    )


def record_deposit(store, day, hotkey=HOTKEY, *, unattributed=False):
    store.record_noah_deposit(
        coldkey=GIVER,
        hotkey=hotkey,
        block_timestamp=ANCHOR + day * 86400 + 100,
        amount_rao=10_000_000_000,
        block=2000 + day,
        event_ordinal=0,
        extrinsic_index=0,
        block_hash=f"deposit-{day}",
        carried_by="StakeTransferred",
        unattributed_reason="not attributed" if unattributed else None,
    )


def empty_snapshot(store):
    store.mark_day_scanned(0, 1, 10)
    return freeze(
        store,
        {},
        genesis_hash="g",
        netuid=NETUID,
        finalized_block=10,
        finalized_hash="h",
        as_of_day=0,
    )


def view(store, snapshot, day):
    return FrozenEntries(
        store,
        snapshot,
        genesis_hash="g",
        netuid=NETUID,
        registration_days={GIVER: {HOTKEY: 1}},
        as_of_day=day,
    )


def test_registration_fallback_only_without_activity_through_cutoff(tmp_path):
    store = Store.create(tmp_path / "history.db", day_anchor_ts=ANCHOR)
    snapshot = empty_snapshot(store)
    record_pay(store, 2)
    # Future knowledge neither suppresses the historical registration fallback
    # nor expands the view when a caller supplies a later through_day.
    assert view(store, snapshot, 1).first_activity_day(GIVER) == 1
    assert view(store, snapshot, 1).first_activity_day(GIVER, through_day=99) == 1
    # Once activity is in scope, it stays authoritative; do not replace it by
    # min(activity, registration), which would rewrite established history.
    assert view(store, snapshot, 3).first_activity_day(GIVER) == 2
    record_pay(store, 0)
    assert view(store, snapshot, 3).first_activity_day(GIVER) == 0
    store.close()


@pytest.mark.parametrize("bad", [True, -1, 1.5, "1"])
def test_invalid_cutoff_never_falls_back_to_unbounded_history(tmp_path, bad):
    store = Store.create(tmp_path / "history.db", day_anchor_ts=ANCHOR)
    frozen = view(store, empty_snapshot(store), 1)
    for query in (
        lambda: store.first_activity_day(GIVER, through_day=bad),
        lambda: store.wallets_seen(through_day=bad),
        lambda: store.known_hotkeys_of(GIVER, through_day=bad),
        lambda: frozen.first_activity_day(GIVER, through_day=bad),
    ):
        with pytest.raises((ValueError, NotAnAmount)):
            query()
    store.close()


def test_cutoff_wallet_membership_retains_zero_pay_and_unattributed_semantics(tmp_path):
    store = Store.create(tmp_path / "history.db", day_anchor_ts=ANCHOR)
    record_pay(store, 2, amount=0)
    assert store.wallets_seen() == [GIVER]
    assert store.wallets_seen(through_day=1) == []
    assert store.first_activity_day(GIVER) is None
    record_deposit(store, 1, unattributed=True)
    assert store.wallets_seen(through_day=1) == [GIVER]
    assert store.first_activity_day(GIVER, through_day=1) is None
    store.close()


def test_future_deposit_cannot_fund_original_frozen_entry(tmp_path):
    store = Store.create(tmp_path / "history.db", day_anchor_ts=ANCHOR)
    store.mark_day_scanned(0, 1, 10)
    registrations = {GIVER: {HOTKEY: 0}}
    kwargs = dict(
        genesis_hash="g", netuid=NETUID, finalized_block=10, finalized_hash="h", as_of_day=0
    )
    before = freeze(store, registrations, **kwargs)
    record_deposit(store, 2)
    assert freeze(store, registrations, **kwargs) == before
    frozen = FrozenEntries(
        store, before, genesis_hash="g", netuid=NETUID, registration_days=registrations, as_of_day=2
    )
    assert frozen.credited == {}
    assert before["payload"]["records"][0]["amountRao"] == 0
    store.close()


def test_historical_known_position_still_refuses_omission_and_cache_ignores_future(tmp_path):
    from noah.chain.collection import CollectionProgress
    from noah.chain.holdings import Share
    from noah.chain.runner import StakingHotkeysIncomplete, _prove_staking_hotkeys_complete

    store = Store.create(tmp_path / "history.db", day_anchor_ts=ANCHOR)
    progress = CollectionProgress(store, RetryChain(), 1, 11, 20, {"test": "cutoff"})
    before = progress.sample_proof(GIVER, "hash")
    record_deposit(store, 2)
    assert store.known_hotkeys_of(GIVER) == [HOTKEY]
    assert store.known_hotkeys_of(GIVER, through_day=1) == []
    assert progress.sample_proof(GIVER, "hash") == before
    chain = SimpleNamespace(
        alpha_share=lambda *args: Share(encoding="u64f64", raw=1, exponent=0, source="v1")
    )
    # Future-only knowledge is excluded; historically known and registered
    # positions retain the exact fail-closed completeness check.
    _prove_staking_hotkeys_complete(store, chain, GIVER, 18, [], None, through_day=1)
    with pytest.raises(StakingHotkeysIncomplete):
        _prove_staking_hotkeys_complete(store, chain, GIVER, 18, [], None, [HOTKEY], through_day=1)
    record_deposit(store, 1)
    assert progress.sample_proof(GIVER, "hash") != before
    with pytest.raises(StakingHotkeysIncomplete):
        _prove_staking_hotkeys_complete(store, chain, GIVER, 18, [], None, through_day=1)
    store.close()


def test_retry_does_not_silently_remove_preexisting_historical_samples(tmp_path, monkeypatch):
    from noah.chain.holdings import Share

    adapter, chain, opened, _ = rig(tmp_path, monkeypatch)
    try:
        original = adapter.read(20)
        store = opened[-1]
        share = Share(encoding="u64f64", raw=1, exponent=0, source="v1")
        # A contaminated/previously broader sample set is evidence, not a cache
        # this fix may erase to manufacture parity with a retained calculation.
        store.record_holding_sample(
            coldkey=GIVER,
            block=11,
            block_timestamp=chain.block_timestamp(11),
            block_hash=chain.block_hash(11),
            total_rao=1,
            locked_rao=0,
            available_rao=1,
            positions=[
                dict(hotkey=HOTKEY, shares=share, denominator=share, total_hotkey_alpha_rao=1)
            ],
        )
        retry = adapter.read(20)
        assert retry != original
        assert retry["miners"][0]["holdRao"] == "1"
        assert store._conn.execute(
            "SELECT total_rao FROM holding_sample WHERE day=1 AND block=11"
        ).fetchone() == (1,)
    finally:
        for store in opened:
            store.close()
