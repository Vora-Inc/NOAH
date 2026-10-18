"""Running the reader over a range of days, and being able to prove what it did.

Four things are one thing here, which is why they are built together rather
than bolted on: resuming without redoing finished work, producing the same
bytes on a second run, recording what was read and from where, and knowing
which reads went through the agreement gate.

Provenance added to a working runner afterwards records what the code thinks it
did. Recorded as it runs, it records what it did. This project has shipped the
first kind twice.

**There is no flag that lets a run continue past a refusal**, and a test asserts
its absence. Such a flag is how a system like this starts publishing wrong
numbers, and it always arrives as a convenience during testing.
"""

import pytest

from noah.chain.deposits import NotADepositEvent
from noah.chain.holdings import Share, ShareNotRead
from noah.chain.live import ChainUnavailable
from noah.chain.reads import HeadWentBackwards
from noah.chain.runner import (
    BlockTimeNotRead,
    DepositBlockReorged,
    HoldingSampleReorged,
    QuietBlockReorged,
    RunRefused,
    run_day,
)
from noah.chain.sources import Refusal
from noah.chain.store import NOT_READ, DayNotSampled, DayNotScanned, Store

ANCHOR = 1767571200
DAY = 86_400
ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
GIVER = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
NETUID = 35


def _held(store, coldkey, day=0, amount=1, hotkey=HOTKEY):
    """Record one hotkey's holding directly (#193: `build_fixture` now reads
    `holdRao` from the store), on a block range disjoint from anything `run`
    itself writes, so the two never collide. share=1 over denominator=1 times
    a pool of `amount` reads back as exactly `amount`.
    """
    store.record_holding_sample(
        coldkey=coldkey,
        block_timestamp=ANCHOR + day * DAY + 300,
        block=8_700_000 + day,
        block_hash=f"0xheld{day:x}",
        total_rao=amount,
        locked_rao=NOT_READ,
        available_rao=NOT_READ,
        positions=[
            {
                "hotkey": hotkey,
                "shares": Share(encoding="u64f64", raw=1, exponent=0, source="v1"),
                "denominator": Share(encoding="u64f64", raw=1, exponent=0, source="v1"),
                "total_hotkey_alpha_rao": amount,
            }
        ],
    )
    store.mark_day_sampled(day, stride=1_000_000, from_block=8_700_000, to_block=8_799_999)


class Chain:
    """A stubbed chain. Records what was asked of it, so a test can prove the
    runner read what its provenance claims."""

    def __init__(
        self,
        head=8_900_000,
        deposits=(),
        fail_at=None,
        extrinsics=(),
        owners_at_hash=None,
        owner_fail_at=None,
    ):
        self.head = head
        self._deposits = dict(deposits)
        self._fail_at = fail_at
        self._extrinsics = dict(extrinsics)
        self._owners_at_hash = owners_at_hash or {}
        self._owner_fail_at = owner_fail_at
        self.asked = []
        self.locks = {}
        self.arity = {
            "ColdkeyMinerCollateral": 2,
            "MinerCollateral": 3,
            "ColdkeyCollateralHotkeys": 2,
        }

    def head_block(self):
        return self.head

    def block_hash(self, block):
        self.asked.append(("hash", block))
        return f"0x{block:x}"

    def block_timestamp(self, block):
        self.asked.append(("time", block))
        return ANCHOR + (block - 8_800_000) * 12

    def events(self, block):
        self.asked.append(("events", block))
        if self._fail_at is not None and block == self._fail_at:
            raise RuntimeError("the node hung up")
        return self._deposits.get(block, [])

    def staking_hotkeys(self, coldkey, block):
        self.asked.append(("staking_hotkeys", block))
        return []

    def alpha_share(self, hotkey, coldkey, block):
        return Share(encoding="safefloat", raw=0, exponent=0, source="absent")

    def hotkey_shares(self, hotkey, block):
        return None

    def hotkey_alpha(self, hotkey, block):
        return 0

    def coldkey_collateral(self, coldkey, block):
        """What this wallet had locked at this block.

        Zero because these fixtures describe a subnet where nobody holds
        collateral, which is the ordinary case and the one Noah launches into.
        A test that wants locked pay puts real figures in `locks` and gets a
        reading either side of the epoch block, the way the chain answers it.
        """
        self.asked.append(("coldkey_collateral", block))
        return self.locks.get((coldkey, block), 0)

    def collateral_hotkeys(self, coldkey, block):
        return ()

    def miner_collateral(self, hotkey, coldkey, block):
        return None

    def collateral_arity(self, block):
        """What the chain's metadata declares, matching the real runtime.

        ColdkeyMinerCollateral takes (netuid, coldkey); MinerCollateral takes
        (netuid, hotkey, coldkey); ColdkeyCollateralHotkeys takes
        (netuid, coldkey). Stated here rather than defaulted so a test can make
        one of them disagree and prove the check fires.
        """
        return dict(self.arity)

    def extrinsics(self, block):
        """Issue #112-15: `run_day`'s own `attribute_deposits=True` wiring
        asks this of every block. Defaults to an empty list -- honest for
        every existing fixture in this file, none of which describes a
        batch_all."""
        self.asked.append(("extrinsics", block))
        return list(self._extrinsics.get(block, []))

    def owner_at(self, *, hotkey, block_hash, block=None):
        """Model an owner lookup at the requested block. Configured transport failures
        raise ChainUnavailable, preserving the real source refusal type."""
        self.asked.append(("owner_at", hotkey, block_hash))
        if self._owner_fail_at is not None and block == self._owner_fail_at:
            raise ChainUnavailable(
                f"asking for the owner of {hotkey} at block hash {block_hash} failed: the node "
                f"hung up."
            )
        return self._owners_at_hash[(hotkey, block_hash)]


def a_store(tmp_path, name="noah.db"):
    store = Store.create(tmp_path / name, day_anchor_ts=ANCHOR)
    store.record_emission_start(block=8_000_000, block_timestamp=ANCHOR, block_hash="0xs")
    return store


def a_deposit(xt=7, ordinal=145):
    """Events carry the position they held in the whole block, as real ones do.
    Counting them here would reproduce the defect an independent reading of
    three real blocks found: the identity of a contribution becoming a property
    of which events the caller passed in."""
    return [
        {
            "ordinal_in_block": ordinal,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": xt,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [ARK, HOTKEY, 8_000_000_000, 469_790_773_712, NETUID, 0],
        },
        {
            "ordinal_in_block": ordinal + 1,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": xt,
            "module_id": "SubtensorModule",
            "event_id": "StakeTransferred",
            "attributes": [GIVER, ARK, HOTKEY, NETUID, NETUID, 8_000_000_000],
        },
    ]


def run(
    store,
    chain,
    day=0,
    first=8_800_000,
    last=8_800_002,
    sources=None,
    may_resume=True,
    holding_stride=2,
    attribute_deposits=False,
    designated_validator_hotkey=HOTKEY,
):
    return run_day(
        store,
        chain,
        day=day,
        from_block=first,
        to_block=last,
        ark_coldkey=ARK,
        designated_validator_hotkey=designated_validator_hotkey,
        sources=sources or {"archive": "wss://a:443", "entrypoint": "wss://b:443"},
        netuid=NETUID,
        holding_stride=holding_stride,
        read_collateral=True,
        may_resume=may_resume,
        attribute_deposits=attribute_deposits,
    )


# ---- it reads, and the store shows it --------------------------------------


def test_a_run_records_what_it_found(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})
    run(store, chain)
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712


def test_a_run_carries_the_deposits_hotkey_into_the_store(tmp_path):
    """D-138 reopened (issue #112, 2026-09-19): the real StakeAdded event a
    deposit is read from already names the hotkey the alpha arrived on
    (`noah.chain.deposits.deposits_in_block` has decoded it all along, for
    `noah.deposits.verify.py`'s own use), but `_record` used to leave it off
    the call into `record_noah_deposit`, discarding it before it ever
    reached the store. This is the real event-parsing path end to end:
    `run_day` -> `_record` -> `store.record_noah_deposit`."""
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})
    run(store, chain)
    assert store.noah_deposit_hotkeys(GIVER, 0) == [HOTKEY]


def test_a_run_marks_the_day_it_covered(tmp_path):
    store = a_store(tmp_path)
    run(store, Chain())
    assert store.noah_deposits(GIVER, 0) == 0  # scanned, and genuinely empty


def test_a_day_with_nothing_in_it_is_still_a_day_that_was_read(tmp_path):
    """The difference between nothing happened and nobody looked is the whole
    reason the store refuses an unscanned day."""
    store = a_store(tmp_path)
    provenance = run(store, Chain())
    assert provenance.as_dict()["blocks_read"] == [8_800_000, 8_800_001, 8_800_002]


# ---- resuming ---------------------------------------------------------------


def test_a_finished_day_is_not_read_again(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})
    run(store, chain)
    first_pass = len(chain.asked)
    chain.asked.clear()
    run(store, chain)
    assert first_pass > 0
    assert chain.asked == []


def test_a_deposits_qualification_is_baked_in_at_scan_time_never_re_derived(tmp_path):
    """Issue #282 item 4/4a, #302: "do not retroactively change credited
    history merely because ... a configured validator changes." A day
    already scanned under one designated_validator_hotkey config is never
    rescanned merely because a LATER call configures a different one --
    `run_day`'s own `may_resume=True` default skips a day the store
    already recorded as scanned, using the store's own recorded rows, not
    re-deriving anything from a fresh chain read -- so a deposit's
    qualification, once recorded, cannot silently change out from under
    it."""
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})

    # First scan: HOTKEY (a_deposit()'s own real position) IS the
    # designated validator -- the deposit qualifies.
    run_day(
        store,
        chain,
        day=0,
        from_block=8_800_000,
        to_block=8_800_002,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://a:443", "entrypoint": "wss://b:443"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
    )
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712
    row = store._conn.execute(
        "SELECT unattributed_reason FROM noah_deposit WHERE block = ? AND event_ordinal = ?",
        (8_800_001, 145),
    ).fetchone()
    assert row == (None,)

    # Second call, SAME store, SAME already-scanned day, a DIFFERENT
    # configured designated_validator_hotkey this time (one that would
    # mark the SAME deposit wrong-validator if it were scanned fresh under
    # this config). may_resume=True (the default) must skip re-scanning
    # day 0 entirely -- the chain is asked nothing -- so the already-
    # recorded row's qualification does not change.
    chain.asked.clear()
    run_day(
        store,
        chain,
        day=0,
        from_block=8_800_000,
        to_block=8_800_002,
        ark_coldkey=ARK,
        designated_validator_hotkey="5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj",
        sources={"archive": "wss://a:443", "entrypoint": "wss://b:443"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
    )
    assert chain.asked == []  # never re-scanned
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712  # unchanged
    row_after = store._conn.execute(
        "SELECT unattributed_reason FROM noah_deposit WHERE block = ? AND event_ordinal = ?",
        (8_800_001, 145),
    ).fetchone()
    assert row_after == (None,)  # still qualifies -- not retroactively revoked


def test_resuming_leaves_the_store_holding_the_same_thing(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})
    run(store, chain)
    before = store.deposit_observations(GIVER, 0)
    run(store, chain)
    assert store.deposit_observations(GIVER, 0) == before


# ---- may_resume=False: a day nobody can yet prove is finished --------------
#
# neurons/miner.py's own bug, reproduced here at the layer that actually
# caused it: to_block=head only ever covers blocks produced so far, and
# marking that range as the whole day means every later call trusts a
# marker that was already stale the moment it was written.


def test_a_day_marked_scanned_with_may_resume_false_is_read_again(tmp_path):
    """THE TEST THAT WOULD HAVE CAUGHT IT.

    With the default (may_resume=True, exercised above by
    test_a_finished_day_is_not_read_again), a second call over the same
    range asks the chain nothing. A caller that cannot prove its range is the
    whole day must not get that shortcut, because the marker it would be
    trusting is its OWN, written by a call that already knew it might be
    partial.
    """
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})
    run(store, chain, may_resume=False)
    first_pass = len(chain.asked)
    chain.asked.clear()

    run(store, chain, may_resume=False)

    assert first_pass > 0
    assert chain.asked != [], "a may_resume=False caller must never take the resume shortcut"


def test_a_wider_range_with_may_resume_false_sees_blocks_the_first_call_missed(tmp_path):
    """The actual bug: an in-progress day's head grows between two calls.

    This is neurons/miner.py's own shape end to end, minus the process
    boundary: call it once with 'to_block=head' while the day is still
    open, deposit something in a block that did not exist yet, call it again
    with the new head. Before this fix, the second call saw the day already
    marked scanned (from the first, necessarily-partial call) and asked the
    chain nothing, so the second deposit was silently never recorded -- not
    refused, not retried, just permanently missing. With may_resume=False the
    second call re-reads the whole range and finds it.
    """
    store = a_store(tmp_path)
    chain = Chain(head=8_800_001, deposits={8_800_001: a_deposit()})
    run_day(
        store,
        chain,
        day=0,
        from_block=8_800_000,
        to_block=chain.head_block(),
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://a:443", "entrypoint": "wss://b:443"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
        may_resume=False,
    )
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712

    # More blocks landed, one of them a second, later deposit -- exactly what
    # "the chain produced more blocks since the last call" looks like.
    chain.head = 8_800_003
    chain._deposits[8_800_003] = a_deposit(xt=9, ordinal=200)

    run_day(
        store,
        chain,
        day=0,
        from_block=8_800_000,
        to_block=chain.head_block(),
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://a:443", "entrypoint": "wss://b:443"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
        may_resume=False,
    )

    assert store.noah_deposits(GIVER, 0) == 2 * 469_790_773_712, (
        "the second deposit, in a block that did not exist during the first call, "
        "must be recorded rather than silently missed"
    )


def test_may_resume_false_still_marks_the_day_scanned(tmp_path):
    """Read-again does not mean unread: assemble_miner's own gate is
    store.day_was_scanned, and a caller like RealServices.show() that reads
    the store right after populating it must see the day as scanned, or
    every command would refuse with HistoryIncomplete forever."""
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})

    run(store, chain, may_resume=False)

    assert store.day_was_scanned(0)


def test_a_run_that_died_partway_leaves_the_day_unfinished(tmp_path):
    """A day marked finished when it was not is worse than one left undone:
    the first is read as complete forever and the second is simply redone."""
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_000: a_deposit()}, fail_at=8_800_002)
    with pytest.raises(RunRefused):
        run(store, chain)
    from noah.chain.store import DayNotScanned

    with pytest.raises(DayNotScanned):
        store.noah_deposits(GIVER, 0)


def test_the_generic_refusal_does_not_falsely_promise_a_later_run_will_reread_it(tmp_path):
    """The same message-accuracy fix already applied to ``_raise_reorged``
    and ``QuietBlockReorged`` (real codex's own rounds 4 and 6), applied at
    the third and last place this file made the identical promise: the
    outer ``except Exception`` handler's own ``RunRefused``, which used to
    say "Nothing was marked as read, so the day will be done again" --
    false whenever an earlier, narrower, successful scan had already
    marked the day, since this handler never clears an existing marker
    either, and a caller using the default ``may_resume=True`` would trust
    that stale marker and skip rereading. Real codex's own round-6 review
    found this exact spot.

    Reproduced the same way as the other two: a narrow, successful scan
    first (marks day 0 scanned), then a wider forced rescan
    (``may_resume=False``) whose new block hits an ordinary node failure.
    """
    store = a_store(tmp_path)
    run(store, Chain(), first=8_800_000, last=8_800_000, may_resume=False)
    assert store.day_was_scanned(0)

    chain = Chain(deposits={8_800_000: a_deposit()}, fail_at=8_800_001)
    with pytest.raises(RunRefused) as refused:
        run(store, chain, first=8_800_000, last=8_800_002, may_resume=False)

    assert store.day_was_scanned(0), (
        "an earlier marker is not cleared by a later refusal -- the exact scenario the "
        "old message got wrong"
    )
    message = str(refused.value)
    assert "will be done again" not in message
    assert "did not mark the day" in message


def test_the_run_after_a_failure_starts_the_day_over(tmp_path):
    store = a_store(tmp_path)
    broken = Chain(deposits={8_800_000: a_deposit()}, fail_at=8_800_002)
    with pytest.raises(RunRefused):
        run(store, broken)
    working = Chain(deposits={8_800_000: a_deposit()})
    run(store, working)
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712


# ---- the same bytes twice ---------------------------------------------------


def test_two_runs_of_the_same_range_produce_the_same_store(tmp_path):
    from noah.chain.fixture_build import build_fixture
    from noah.scoring.fixture_io import render_output

    dials = {
        "targetFp": "500000000",
        "harshness": 1,
        "capFp": "1000000000",
        "halfLifeDays": "182",
        "streakCeilingFp": "2000000000",
        "streakRampDays": "365",
        "ongoingHoldRao": "400000000000",
    }

    def once(name):
        store = a_store(tmp_path, name)
        run(store, Chain(deposits={8_800_001: a_deposit()}))
        _held(store, GIVER)
        return render_output(
            build_fixture(
                store,
                name="rerun",
                eval_day=0,
                day_epoch_ts=ANCHOR,
                dials=dials,
                seats={GIVER: {HOTKEY: 1}},
            )
        )

    assert once("first.db") == once("second.db")


# ---- the record of what happened -------------------------------------------


def test_the_record_names_the_sources_and_their_endpoints(tmp_path):
    store = a_store(tmp_path)
    got = run(store, Chain()).as_dict()
    assert [s["name"] for s in got["sources"]] == ["archive", "entrypoint"]
    assert got["single_source"] is False


def test_the_record_says_which_reads_were_at_the_head(tmp_path):
    """Only a read at the head is exempt from needing sources to agree, so
    which was which has to survive the run."""
    store = a_store(tmp_path)
    got = run(store, Chain(head=8_800_002)).as_dict()
    kinds = {r["block"]: r["kind"] for r in got["reads"]}
    assert kinds[8_800_002] == "head"
    assert kinds[8_800_000] == "catch-up"


def test_every_read_of_a_past_block_is_recorded_as_catching_up(tmp_path):
    store = a_store(tmp_path)
    got = run(store, Chain(head=8_900_000)).as_dict()
    assert {r["kind"] for r in got["reads"]} == {"catch-up"}


def test_the_record_separates_what_was_read_from_what_was_configured(tmp_path):
    store = a_store(tmp_path)
    got = run(store, Chain(deposits={8_800_001: a_deposit()})).as_dict()
    assert "the Ark's address" in got["configured_fields"]
    assert "noahDepositRao" in got["read_fields"]


def test_the_record_is_the_same_bytes_for_the_same_run(tmp_path):
    store_one = a_store(tmp_path, "one.db")
    store_two = a_store(tmp_path, "two.db")
    first = run(store_one, Chain(deposits={8_800_001: a_deposit()})).as_json()
    second = run(store_two, Chain(deposits={8_800_001: a_deposit()})).as_json()
    assert first == second


# ---- nothing continues past a refusal ---------------------------------------


def test_there_is_no_way_to_ask_a_run_to_continue_past_a_refusal():
    """Asserted on the signature, because such a flag arrives as a convenience
    during testing and is how a system like this starts publishing numbers
    nobody stands behind. If one is ever wanted it is a conversation, not a
    parameter.
    """
    import inspect

    from noah.chain import runner

    names = " ".join(inspect.signature(runner.run_day).parameters)
    for banned in ("skip", "ignore", "force", "continue", "lenient", "best_effort", "strict"):
        assert banned not in names


def test_a_refusal_inside_a_block_stops_the_whole_day(tmp_path):
    store = a_store(tmp_path)
    broken = [
        {
            "ordinal_in_block": 145,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": 7,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [ARK, HOTKEY, 1, 469_790_773_712, NETUID, 0],
        }
    ]
    chain = Chain(deposits={8_800_001: broken})
    with pytest.raises(NotADepositEvent, match="nothing says who"):
        run(store, chain)


# ---- a reorg during the deposit read race (issue #518) ----------------------
#
# block_hash and events are two separate chain reads, both taken at the top
# of run_day's loop. deposits_in_block tags every deposit it extracts from
# events with that block_hash, and _record persists it. If the two reads
# straddle a reorg -- on both sources at once, which is exactly what two
# honest, independent nodes do the moment a reorg finalizes and they
# converge on the new canonical chain -- the deposits just extracted may
# describe a fork the hash no longer names. Same bug class EpochBlockReorged
# already closed for epoch blocks (test_runner_pay.py); this closes it for
# ordinary deposit blocks too.


def _row_count(store, block):
    """The number of physical deposit rows this block holds, read below the
    store's own ``day_was_scanned`` gate.

    Every public read on ``Store`` refuses for an unscanned day, so a query
    that refuses before looking is indistinguishable from one that looked
    and found nothing. This settles it directly, the way real codex's own
    round-2 adversarial challenge found the earlier ``DayNotScanned``-only
    assertions here could not.
    """
    return store._conn.execute(
        "SELECT COUNT(*) FROM noah_deposit WHERE block = ?", (block,)
    ).fetchone()[0]


def test_a_reorg_between_the_top_of_loop_hash_and_deposit_recording_refuses(tmp_path):
    """The direct reproduction: two reads of the same block returning
    different hashes, with a deposit present, refuses cleanly and records
    nothing."""
    store = a_store(tmp_path)
    deposit_block = 8_800_001

    class Reorging(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._deposit_hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != deposit_block:
                return f"0x{block:x}"
            # The first call is run_day's own, at the top of the loop,
            # before `events` is read. The second is the bookend check just
            # before the deposit is recorded.
            self._deposit_hash_calls += 1
            return "0xold" if self._deposit_hash_calls == 1 else "0xnew"

    chain = Reorging(deposits={deposit_block: a_deposit()})
    with pytest.raises(DepositBlockReorged) as refused:
        run(store, chain)
    assert str(deposit_block) in str(refused.value)
    assert refused.value.__context__ is None, (
        "nothing in this chain's data raises during the read burst (no epoch, and "
        "block_timestamp always succeeds), so this must be the late check's own direct "
        "raise, not a reclassification of some other caught refusal"
    )
    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"
    with pytest.raises(DayNotScanned):
        store.noah_deposits(GIVER, 0)
    assert _row_count(store, deposit_block) == 0, (
        "not merely unscanned -- physically zero rows for this block"
    )


def test_a_reorg_that_already_happened_is_diagnosed_as_a_reorg_not_as_a_bad_timestamp(tmp_path):
    """The same diagnosis-quality bug class ``test_runner_pay.py``'s
    ``test_a_reorg_that_already_happened_is_diagnosed_as_a_reorg_not_as_a_slot_turnover``
    documents, found at a second read site by round 3 of real codex's own
    adversarial challenge: ``_block_time`` runs before either reorg check a
    round-2 version of this fix had, and in the real ``LiveRunnerChain`` it
    resolves its own hash internally (``block_timestamp``,
    ``noah/chain/live.py:74``), so a reorg landing before it can make the
    timestamp it reads come back zero -- ``BlockTimeNotRead``'s own default-
    value guard -- instead of surfacing as the reorg that actually caused it.

    ``run_day`` now wraps its whole per-block read burst, ``_block_time``
    included, in one ``try`` and reclassifies ANY ``Refusal`` raised inside
    it by re-checking the hash. Modeled directly: the block has already
    reorged by the time ``events`` is read, and the reorg'd fork's
    timestamp read comes back as the zero default a source that does not
    yet have state for a block it only just adopted can produce.
    """
    store = a_store(tmp_path)
    deposit_block = 8_800_001

    class ReorgAlreadyHappened(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._reorged = False

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != deposit_block:
                return f"0x{block:x}"
            return "0xnew" if self._reorged else "0xold"

        def events(self, block):
            result = super().events(block)
            if block == deposit_block:
                # The reorg has already happened by the time run_day would
                # read this block's timestamp.
                self._reorged = True
            return result

        def block_timestamp(self, block):
            self.asked.append(("time", block))
            if block == deposit_block and self._reorged:
                return 0
            return ANCHOR + (block - 8_800_000) * 12

    chain = ReorgAlreadyHappened(deposits={deposit_block: a_deposit()})
    with pytest.raises(DepositBlockReorged) as refused:
        run(store, chain)
    assert str(deposit_block) in str(refused.value)
    assert ("time", deposit_block) in chain.asked, (
        "block_timestamp must actually have run -- otherwise this proves nothing about "
        "the catch-and-reclassify path, only that SOME check caught the reorg"
    )
    # ("time", deposit_block) in chain.asked proves the read happened, not that
    # BlockTimeNotRead specifically was what fired and got reclassified. __context__
    # settles that directly, via Python's own implicit exception chaining.
    assert isinstance(refused.value.__context__, BlockTimeNotRead), (
        "must be a reclassified BlockTimeNotRead specifically, not some other refusal or "
        "a direct raise with nothing chained"
    )
    assert not store.day_was_scanned(0)
    assert _row_count(store, deposit_block) == 0


def test_the_reorg_refusal_does_not_falsely_promise_a_later_run_will_reread_it(tmp_path):
    """Real codex's own round-4 review found: the refusal message used to say
    "the day is not marked scanned, so a later run reads this block again"
    -- but ``mark_day_scanned`` is only ever reached on success, never
    cleared on a refusal, so a day already marked scanned by an EARLIER,
    narrower, successful call keeps that marker straight through a LATER
    refusal. A caller using the default ``may_resume=True`` would trust
    that stale marker and skip rereading, contrary to what the old message
    promised.

    Reproduced directly: scan a narrow range successfully (marking day 0
    scanned), then rescan a WIDER range, with ``may_resume=False`` (the
    only way a real caller forces a reread regardless of the marker), whose
    new block reorgs. The marker survives untouched -- proving the old
    claim would have been false here -- and the message now makes no such
    promise, describing only what THIS pass itself did.
    """
    store = a_store(tmp_path)
    # A narrow, successful scan first: marks day 0 scanned.
    run(store, Chain(), first=8_800_000, last=8_800_000, may_resume=False)
    assert store.day_was_scanned(0)

    # A wider rescan, forced with may_resume=False, whose new block reorgs.
    deposit_block = 8_800_001

    class Reorging(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._deposit_hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != deposit_block:
                return f"0x{block:x}"
            self._deposit_hash_calls += 1
            return "0xold" if self._deposit_hash_calls == 1 else "0xnew"

    chain = Reorging(deposits={deposit_block: a_deposit()})
    with pytest.raises(DepositBlockReorged) as refused:
        run(store, chain, first=8_800_000, last=8_800_002, may_resume=False)

    # The earlier marker survives this refusal untouched -- exactly the
    # scenario that made the old message's claim false.
    assert store.day_was_scanned(0), (
        "an earlier marker is not cleared by a later refusal -- this is exactly the "
        "scenario the old message got wrong"
    )
    message = str(refused.value)
    assert "is not marked scanned" not in message, (
        "false whenever an earlier, narrower scan already marked the day -- just reproduced above"
    )
    assert "did not mark the day scanned" in message, (
        "the accurate claim is about THIS PASS's own behavior, not the store's overall state"
    )


def test_every_block_costs_exactly_one_extra_hash_read_for_its_own_bookend(tmp_path):
    """The happy path: no reorg, so the deposit is recorded exactly as before.

    AS OF ISSUE #518, ROUND 5 (real codex's own review found a quiet block's
    emptiness needs bookending too -- ``QuietBlockReorged``'s own docstring
    has the full finding): every block in the range, not just the one
    carrying the deposit, now costs one extra ``block_hash`` re-read,
    because a reorg can make a block that WOULD have had a deposit read as
    quiet instead, and nothing scoped to "blocks with a finding" could ever
    catch that. This doubles the hash-read cost of an ordinary day's scan,
    deliberately -- see ``QuietBlockReorged``'s own docstring for why that
    trade was made rather than trying to scope the check more narrowly.

    ``holding_stride=None``: with the default stride of 2 (this file's own
    ``run()`` helper), ``_sample_holdings`` samples 8_800_000 and 8_800_002
    too and re-reads their hash for its own, separate reason -- a wallet
    position snapshot, nothing to do with reorg bookending -- which would
    push their count to 3 and break this test's claim about what the
    block-loop's OWN bookend costs. Disabled here so this counts only the
    reads ``run_day``'s per-block loop itself makes.
    """
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})
    run(store, chain, holding_stride=None)

    hash_calls = [asked[1] for asked in chain.asked if asked[0] == "hash"]
    assert hash_calls.count(8_800_000) == 2, "quiet blocks are bookended too, now"
    assert hash_calls.count(8_800_001) == 2, "top-of-loop read, plus the bookend re-read"
    assert hash_calls.count(8_800_002) == 2, "quiet blocks are bookended too, now"
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712, "the happy path still records"


def test_a_quiet_block_that_reorged_before_its_own_recheck_refuses(tmp_path):
    """``QuietBlockReorged``'s own direct reproduction: real codex's own
    round-5 review found that every guard in this file, up through round 4,
    only ran at all when ``deposits or is_epoch`` was true -- so a block
    that read as carrying NOTHING got none of them, including the recheck
    that would have caught a reorg. A block whose ``events`` happened to be
    read from a fork about to be superseded can look completely quiet while
    the canonical fork's actual block, moments from becoming visible,
    carries a real deposit. Before this fix, the loop would move on, never
    look back, and mark the day scanned as if nothing was ever there.

    Modeled directly: a block with no deposit configured at all (so
    ``events`` is genuinely empty for it, exactly what a stale read of an
    otherwise deposit-carrying block would look like) whose hash has
    already moved by the time this fix's own unconditional recheck runs.
    """
    store = a_store(tmp_path)
    quiet_block = 8_800_001

    class ReorgsWhileQuiet(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != quiet_block:
                return f"0x{block:x}"
            self._hash_calls += 1
            return "0xold" if self._hash_calls == 1 else "0xnew"

    chain = ReorgsWhileQuiet()  # no deposits configured anywhere
    with pytest.raises(QuietBlockReorged) as refused:
        run(store, chain)
    assert str(quiet_block) in str(refused.value)
    assert refused.value.__context__ is None, (
        "nothing raises for a genuinely quiet block -- this is the unconditional "
        "recheck's own direct raise"
    )
    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"


def test_the_quiet_block_refusal_does_not_falsely_promise_a_later_run_will_reread_it(
    tmp_path,
):
    """The same message-accuracy fix
    ``test_the_reorg_refusal_does_not_falsely_promise_a_later_run_will_reread_it``
    proves for ``_raise_reorged``, applied to ``QuietBlockReorged``: real
    codex's own round-6 review found the first version of this message had
    the identical flaw -- promising "a later run reads it again" when a
    forced rescan of an ALREADY-marked day hits this refusal, and the
    earlier marker survives untouched, so a caller trusting that marker
    (the default ``may_resume=True``) would never retry at all.

    Reproduced the same way: a narrow, successful scan first (marks day 0
    scanned), then a wider forced rescan (``may_resume=False``) whose new
    block reads as quiet but has already reorged.
    """
    store = a_store(tmp_path)
    run(store, Chain(), first=8_800_000, last=8_800_000, may_resume=False)
    assert store.day_was_scanned(0)

    quiet_block = 8_800_001

    class ReorgsWhileQuiet(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != quiet_block:
                return f"0x{block:x}"
            self._hash_calls += 1
            return "0xold" if self._hash_calls == 1 else "0xnew"

    chain = ReorgsWhileQuiet()
    with pytest.raises(QuietBlockReorged) as refused:
        run(store, chain, first=8_800_000, last=8_800_002, may_resume=False)

    assert store.day_was_scanned(0), (
        "an earlier marker is not cleared by a later refusal -- the exact scenario the "
        "old message got wrong"
    )
    message = str(refused.value)
    assert "is not marked scanned" not in message
    assert "did not mark the day scanned" in message


def test_a_genuinely_quiet_day_with_no_reorg_still_scans_cleanly(tmp_path):
    """The happy path this fix must not break: a day where every block is
    honestly, permanently empty (no reorg at all) still scans cleanly and
    gets marked scanned, at the cost of exactly one extra hash read per
    block -- not a refusal.
    """
    store = a_store(tmp_path)
    run(store, Chain())
    assert store.day_was_scanned(0)
    assert store.noah_deposits(GIVER, 0) == 0


def test_an_ordinary_failure_from_the_node_becomes_a_refusal(tmp_path):
    """A node hanging up raises an ordinary exception, which a retry loop
    swallows. It has to become a refusal before it reaches one."""
    store = a_store(tmp_path)
    with pytest.raises(RunRefused, match="hung up"):
        run(store, Chain(fail_at=8_800_001))


@pytest.mark.parametrize(
    "kwargs,why",
    [
        ({"first": 8_800_002, "last": 8_800_000}, "ends"),
        ({"first": 0}, "greater than zero"),
        ({"day": -1}, "day"),
    ],
    ids=["backwards", "zero-block", "negative-day"],
)
def test_a_range_that_makes_no_sense_is_refused(tmp_path, kwargs, why):
    store = a_store(tmp_path)
    with pytest.raises(RunRefused, match=why):
        run(store, Chain(), **kwargs)


def test_a_block_beyond_the_head_is_refused(tmp_path):
    store = a_store(tmp_path)
    with pytest.raises(HeadWentBackwards):
        run(store, Chain(head=8_800_001), first=8_800_000, last=8_800_005)


def test_a_refusal_keeps_its_own_diagnosis_rather_than_being_flattened(tmp_path):
    """A run stopping is not itself the interesting fact. Which guard stopped
    it is, because that is what tells an operator whether a source lied, a
    decode failed, or a contribution could not be attributed. So refusals pass
    through as themselves and only an ordinary failure becomes a run refusal.
    """
    store = a_store(tmp_path)
    orphan = [
        {
            "ordinal_in_block": 145,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": 7,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [ARK, HOTKEY, 1, 469_790_773_712, NETUID, 0],
        }
    ]
    with pytest.raises(NotADepositEvent):
        run(store, Chain(deposits={8_800_001: orphan}))
    with pytest.raises(RunRefused, match="hung up"):
        run(store, Chain(fail_at=8_800_001))


def test_a_run_with_one_source_says_so_rather_than_hiding_it(tmp_path):
    store = a_store(tmp_path)
    got = run(store, Chain(), sources={"archive": "wss://a:443"}).as_dict()
    assert got["single_source"] is True


def test_two_readings_of_one_block_under_different_filtering_are_one_contribution(tmp_path):
    """The test that actually catches it, and it took two attempts to write.

    My first attempt compared the fixtures two separate stores produced and it
    passed with the fault reintroduced, because a fixture never carries an
    event's position: it carries wallets, days and totals. The position only
    decides the key a contribution is stored under, so two stores could hold it
    under different keys and still produce identical fixtures.

    The damage is in one store. Read the same block twice with different
    slicing and the contribution lands under two keys, so it is counted twice.
    Measured with the fault reintroduced: one contribution of five hundred rao
    recorded as a thousand.
    """
    from noah.chain.deposits import deposits_in_block

    store = a_store(tmp_path)
    pair = a_deposit()
    noise = {
        "ordinal_in_block": 12,
        "phase": "ApplyExtrinsic",
        "extrinsic_idx": 2,
        "module_id": "SubtensorModule",
        "event_id": "StakeRemoved",
        "attributes": [GIVER, HOTKEY, 1, 1, 80, 0],
    }

    for slicing in (pair, [noise] + pair):
        for deposit in deposits_in_block(
            slicing,
            ark_coldkey=ARK,
            block=8_800_001,
            block_hash="0xa",
            designated_validator_hotkey=HOTKEY,
        ):
            store.record_noah_deposit(
                coldkey=deposit.coldkey,
                block_timestamp=ANCHOR + 100,
                amount_rao=deposit.amount_rao,
                block=deposit.block,
                event_ordinal=deposit.event_ordinal,
                extrinsic_index=deposit.extrinsic_index,
                block_hash=deposit.block_hash,
                carried_by="StakeTransferred",
            )
    store.mark_day_scanned(0, from_block=1, to_block=2)
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712


def test_two_runs_into_separate_stores_produce_the_same_fixture(tmp_path):
    """Kept, but demoted to what it actually proves. It does not see an
    event's position at all, which is why it passed with the fault in place.

    An earlier version derived a contribution's identity by counting the events
    it was handed, so the identity was a property of how the caller had sliced
    the block. Two runs filtering differently gave one contribution two
    identities, and the byte-identical rerun held only because the same caller
    filtered the same way both times. It was never a property of the reader.

    Here the two runs are handed genuinely different slices of the same block:
    one gets every stake-family event, the other only the two that matter. The
    store must not be able to tell.
    """
    from noah.chain.fixture_build import build_fixture
    from noah.scoring.fixture_io import render_output

    dials = {
        "targetFp": "500000000",
        "harshness": 1,
        "capFp": "1000000000",
        "halfLifeDays": "182",
        "streakCeilingFp": "2000000000",
        "streakRampDays": "365",
        "ongoingHoldRao": "400000000000",
    }
    noise = [
        {
            "ordinal_in_block": 12,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": 2,
            "module_id": "SubtensorModule",
            "event_id": "StakeRemoved",
            "attributes": [GIVER, HOTKEY, 1, 1, 80, 0],
        },
        {
            "ordinal_in_block": 90,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": 5,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [GIVER, HOTKEY, 1, 1, 80, 0],
        },
    ]

    class Filtered(Chain):
        """Hands over only the two events that make a contribution."""

        def events(self, block):
            self.asked.append(("events", block))
            return self._deposits.get(block, [])

    class Unfiltered(Chain):
        """Hands over everything of the same family, in block order."""

        def events(self, block):
            self.asked.append(("events", block))
            rows = list(noise) + list(self._deposits.get(block, []))
            return sorted(rows, key=lambda r: r["ordinal_in_block"])

    def once(name, chain_class):
        store = a_store(tmp_path, name)
        run(store, chain_class(deposits={8_800_001: a_deposit()}))
        _held(store, GIVER)
        return render_output(
            build_fixture(
                store,
                name="rerun",
                eval_day=0,
                day_epoch_ts=ANCHOR,
                dials=dials,
                seats={GIVER: {HOTKEY: 1}},
            )
        )

    assert once("thin.db", Filtered) == once("fat.db", Unfiltered)


# ---- holdings reorg protection (issue #566) ---------------------------------
#
# HoldingSampleReorged's own direct reproduction, and proof that fixing it
# also removed the second, smaller defect found alongside it: the write used
# to re-read alpha_share/hotkey_shares a second time per hotkey, after the
# same fact had already been read once to compute the total.


class HoldingsChain(Chain):
    """A Chain double that also answers _sample_holdings's own four reads
    (issue #134), with one real position -- GIVER on HOTKEY -- so a sample
    has something real to read and write, not just an empty pass."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.share_calls = []

    def staking_hotkeys(self, coldkey, block):
        self.asked.append(("staking_hotkeys", block))
        return [HOTKEY] if coldkey == GIVER else []

    def alpha_share(self, hotkey, coldkey, block):
        self.share_calls.append(("alpha_share", hotkey, coldkey, block))
        if hotkey not in self.staking_hotkeys(coldkey, block):
            return Share(encoding="safefloat", raw=0, exponent=0, source="absent")
        return Share(encoding="u64f64", raw=1, exponent=0, source="v1")

    def hotkey_shares(self, hotkey, block):
        self.share_calls.append(("hotkey_shares", hotkey, block))
        return Share(encoding="u64f64", raw=1, exponent=0, source="v1")

    def hotkey_alpha(self, hotkey, block):
        return 1_000_000_000


def test_a_holding_sample_is_still_recorded_correctly_and_from_one_read_each(tmp_path):
    """The happy path, and the second defect's own regression proof: GIVER's
    one position (share 1 over denominator 1, pool 1_000_000_000) reads back
    as exactly 1_000_000_000, the fix's read/write split changed nothing
    about the arithmetic -- and alpha_share/hotkey_shares are each asked
    exactly ONCE per hotkey per sample block, not twice, because the write
    below now reuses the Share objects the read burst already collected
    instead of asking the chain again."""
    store = a_store(tmp_path)
    chain = HoldingsChain(deposits={8_800_001: a_deposit()})
    run(store, chain, holding_stride=2)  # samples 8_800_000 and 8_800_002

    assert store.holding_by_hotkey(GIVER, 0) == {HOTKEY: 1_000_000_000}
    for block in (8_800_000, 8_800_002):
        assert chain.share_calls.count(("alpha_share", HOTKEY, GIVER, block)) == 1, (
            f"alpha_share asked more than once for block {block}"
        )
        assert chain.share_calls.count(("hotkey_shares", HOTKEY, block)) == 1, (
            f"hotkey_shares asked more than once for block {block}"
        )


def test_a_holding_sample_reorged_after_a_clean_read_refuses_and_writes_nothing(tmp_path):
    """HoldingSampleReorged's own direct reproduction (issue #566): a reorg
    landing between the top of a sample block's own read burst and the
    moment its wallets would be written must not commit a mixed-fork
    holding. Modeled the same way test_a_quiet_block_that_reorged_before_
    its_own_recheck_refuses models QuietBlockReorged: block_hash returns one
    value the first time it is asked for the sample block and a different
    one on every later ask, so the unconditional late re-check this fix adds
    is what catches it -- nothing in the read burst itself raises anything,
    proving this is the "burst finished clean but the hash still moved"
    branch, not the except-Refusal one below."""
    store = a_store(tmp_path)
    sample_block = 8_800_000

    class ReorgsAfterCleanRead(HoldingsChain):
        """block_hash's first THREE calls for sample_block agree: the main
        per-block loop's own top-of-loop read and its QuietBlockReorged
        bookend (sample_block carries no deposit or epoch, so it takes that
        quiet-block path first, before _sample_holdings ever runs), then
        _sample_holdings's own top-of-block read. The FOURTH call --
        _sample_holdings's own late, unconditional recheck -- is where this
        reorg actually lands, proving it is caught there and not mistaken
        for the (already-passed) QuietBlockReorged bookend above it."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != sample_block:
                return f"0x{block:x}"
            self._hash_calls += 1
            return "0xold" if self._hash_calls <= 3 else "0xnew"

    chain = ReorgsAfterCleanRead(deposits={8_800_001: a_deposit()})
    with pytest.raises(HoldingSampleReorged) as refused:
        run(store, chain, holding_stride=2)
    assert str(sample_block) in str(refused.value)
    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"
    with pytest.raises(DayNotSampled):
        store.holding_by_hotkey(GIVER, 0)


def test_a_genuine_holding_read_failure_is_not_misdiagnosed_as_a_reorg(tmp_path):
    """The companion case test_a_reorg_that_already_happened_is_diagnosed_as_
    a_reorg_not_as_a_bad_timestamp already proves for the main loop: a real
    ShareNotRead, on a hash that never moved, is a genuine data problem and
    must propagate unchanged rather than being reported as a reorg it was
    not. A zero denominator (a hotkey with a total of zero shares) is
    ShareNotRead's own direct trigger (noah.chain.holdings.positions_for_
    coldkey)."""
    store = a_store(tmp_path)

    class ZeroDenominator(HoldingsChain):
        def hotkey_shares(self, hotkey, block):
            self.share_calls.append(("hotkey_shares", hotkey, block))
            return Share(encoding="u64f64", raw=0, exponent=0, source="v1")

    chain = ZeroDenominator(deposits={8_800_001: a_deposit()})
    with pytest.raises(ShareNotRead) as refused:
        run(store, chain, holding_stride=2)
    assert refused.value.__context__ is None, (
        "a genuine failure on an unmoved hash propagates as itself, not caught and "
        "re-raised through the reorg path"
    )
    assert not store.day_was_scanned(0)


def test_a_holding_read_failure_that_really_was_a_reorg_is_diagnosed_as_one(tmp_path):
    """The mirror of the previous test: the identical ShareNotRead trigger,
    but this time the sample block's own hash really has moved by the time
    the except-Refusal handler re-checks it -- so the reorg, not the
    downstream ShareNotRead it happened to produce, is what gets reported.
    Same shape as test_a_reorg_that_already_happened_is_diagnosed_as_a_
    reorg_not_as_a_slot_turnover for the main per-block loop."""
    store = a_store(tmp_path)
    sample_block = 8_800_000

    class ReorgedBeforeTheRead(HoldingsChain):
        """block_hash's first THREE calls for sample_block agree (the main
        loop's own quiet-block pass, then _sample_holdings's own top-of-
        block read) -- the reorg is diagnosed from hotkey_shares raising
        ShareNotRead plus the FOURTH call (the except-Refusal handler's own
        re-check) disagreeing with the first three, not from block_hash
        alone."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != sample_block:
                return f"0x{block:x}"
            self._hash_calls += 1
            return "0xold" if self._hash_calls <= 3 else "0xnew"

        def hotkey_shares(self, hotkey, block):
            self.share_calls.append(("hotkey_shares", hotkey, block))
            if block == sample_block:
                # The reorg already happened by the time this read runs: a
                # zero-shares answer is exactly the kind of internally
                # inconsistent state a stale fork can produce.
                return Share(encoding="u64f64", raw=0, exponent=0, source="v1")
            return Share(encoding="u64f64", raw=1, exponent=0, source="v1")

    chain = ReorgedBeforeTheRead(deposits={8_800_001: a_deposit()})
    with pytest.raises(HoldingSampleReorged) as refused:
        run(store, chain, holding_stride=2)
    assert str(sample_block) in str(refused.value)
    assert not store.day_was_scanned(0)


def test_a_reorg_between_the_hash_capture_and_the_timestamp_read_is_caught(tmp_path):
    """Real codex adversarial review (2026-09-01) of an EARLIER version of
    this fix found the timestamp read sitting entirely OUTSIDE the reorg
    bracket: `_sample_holdings` used to read `block_timestamp` before
    capturing `block_hash` at all, so a reorg landing between the two could
    commit new-fork holdings tagged with an old-fork timestamp, or wrongly
    skip a genuine sample -- and neither of this function's own two checks
    had anything to compare the timestamp read against, because both
    compared against a hash captured AFTER it.

    Modeled here as a stale/wrong-fork timestamp (one that names a
    different day than the canonical one actually would) landing at the
    exact moment a reorg is also underway, proven by the hash mismatch
    between the burst's own first read and the late, unconditional
    recheck. Under the CURRENT code (hash captured first, timestamp read
    inside the same bracket), this is caught regardless of which day the
    stale timestamp happened to name -- proving the unconditional recheck
    covers the `in_target_day is False` branch too, the identical
    discipline QuietBlockReorged's own docstring already argues for the
    main per-block loop."""
    store = a_store(tmp_path)
    # The LAST stride point, deliberately: _block_time's own monotonic
    # check chains across sample blocks, so injecting a stale answer at the
    # FIRST one would (harmlessly, but confusingly) trip that unrelated
    # guard on the block sampled after it instead of exercising the
    # scenario this test exists for. Using the last stride point means
    # nothing samples after it in this range, isolating the failure to
    # exactly this block's own read burst.
    sample_block = 8_800_002

    class ReorgBetweenHashAndTimestamp(HoldingsChain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != sample_block:
                return f"0x{block:x}"
            self._hash_calls += 1
            # Calls 1-3 (the main loop's own two, then _sample_holdings's
            # own top-of-burst capture) agree; call 4 (the late,
            # unconditional recheck) is where the reorg actually shows up.
            return "0xold" if self._hash_calls <= 3 else "0xnew"

        def block_timestamp(self, block):
            self.asked.append(("time", block))
            if block != sample_block:
                return ANCHOR + (block - 8_800_000) * 12
            # A stale/wrong-fork answer: names a day OTHER than the one
            # being sampled (the NEXT day, still >= the prior sampled
            # block's own chained time, so this does not ALSO trip
            # _block_time's unrelated monotonic guard -- this test is about
            # the day-membership/reorg check specifically), exactly what a
            # reorg landing at this read could produce. The block would be
            # silently excluded (in_target_day=False) if nothing rechecked
            # the hash afterward regardless of that outcome.
            return ANCHOR + DAY

    chain = ReorgBetweenHashAndTimestamp(deposits={8_800_001: a_deposit()})
    with pytest.raises(HoldingSampleReorged) as refused:
        run(store, chain, holding_stride=2)
    assert str(sample_block) in str(refused.value)
    assert not store.day_was_scanned(0)


# ---- issue #112-15: real batch_all+remark attribution wired into run_day ---
#
# `run_day`'s own `attribute_deposits=True` parameter is what actually closes
# the gap Verifier found: neither real production caller (`noah.core.
# chain_adapter.ChainAdapter.read`, `noah.deposits.live_sequence.
# populate_store_through_day`) used to pass `extrinsics`/`parent_block_hash`/
# `owner_source` into `deposits_in_block` at all, so a wrong- or
# ambiguous-owner batch_all deposit would have been silently recorded and
# credited through the old, attribution-free path. These tests prove the
# wiring itself, through `run_day`, not `deposits_in_block` directly (already
# proven in `test_deposits_batch_all.py`).

import hashlib

from noah.chain.ss58 import public_key_of

_MINER_HOTKEY = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
_MINER_HOTKEY_RAW = public_key_of(_MINER_HOTKEY)
_MINER_HOTKEY_HASH = "0x" + hashlib.blake2b(_MINER_HOTKEY_RAW, digest_size=32).hexdigest()


def _a_batch_all_deposit(xt=7, ordinal=145):
    events = a_deposit(xt=xt, ordinal=ordinal)
    events.append(
        {
            "ordinal_in_block": ordinal + 2,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": xt,
            "module_id": "System",
            "event_id": "Remarked",
            "attributes": {"sender": GIVER, "hash": _MINER_HOTKEY_HASH},
        }
    )
    return events


def _batch_all_extrinsics(xt=7):
    padding = [
        {"address": None, "call": {"call_module": "Timestamp", "call_function": "set", "call_args": []}}
        for _ in range(xt)
    ]
    batch = {
        "address": GIVER,
        "call": {
            "call_module": "Utility",
            "call_function": "batch_all",
            "call_args": [
                {
                    "name": "calls",
                    "value": [
                        {
                            "call_module": "SubtensorModule",
                            "call_function": "transfer_stake",
                            "call_args": [
                                {"name": "destination_coldkey", "value": ARK},
                                {"name": "hotkey", "value": HOTKEY},
                                {"name": "origin_netuid", "value": 80},
                                {"name": "destination_netuid", "value": 80},
                                {"name": "alpha_amount", "value": 469_790_773_712},
                            ],
                        },
                        {
                            "call_module": "System",
                            "call_function": "remark_with_event",
                            "call_args": [
                                {"name": "remark", "value": "0x" + _MINER_HOTKEY_RAW.hex()}
                            ],
                        },
                    ],
                }
            ],
        },
    }
    return padding + [batch]


def test_attribute_deposits_false_reproduces_the_old_unattributed_default(tmp_path):
    """The default: a caller that never opts in gets exactly today's
    behaviour, even for a block that WOULD have decoded a real batch_all --
    `origin_hotkey` stays None because `deposits_in_block` was never even
    given `extrinsics` to look at."""
    store = a_store(tmp_path)
    chain = Chain(
        deposits={8_800_001: _a_batch_all_deposit()},
        extrinsics={8_800_001: _batch_all_extrinsics()},
    )
    run(store, chain)  # attribute_deposits defaults to False
    assert store.noah_deposit_hotkeys(GIVER, 0) == [HOTKEY]
    row = store._conn.execute(
        "SELECT origin_hotkey, unattributed_reason FROM noah_deposit WHERE block = ? "
        "AND event_ordinal = ?",
        (8_800_001, 145),
    ).fetchone()
    assert row == (None, None)


def test_attribute_deposits_true_wires_a_real_owner_source_through_run_day(tmp_path):
    """The fix: `attribute_deposits=True` supplies real `extrinsics`,
    `parent_block_hash`, and an owner_source built off `chain` ITSELF
    (`_ParentBlockOwnerSource`) -- proving the wiring reaches all the way
    from `run_day`'s own per-block loop into a real attribution."""
    store = a_store(tmp_path)
    parent_hash = "0x" + format(8_800_000, "x")
    chain = Chain(
        deposits={8_800_001: _a_batch_all_deposit()},
        extrinsics={8_800_001: _batch_all_extrinsics()},
        owners_at_hash={(_MINER_HOTKEY, parent_hash): GIVER},
    )
    run(store, chain, attribute_deposits=True)
    assert ("owner_at", _MINER_HOTKEY, parent_hash) in chain.asked
    row = store._conn.execute(
        "SELECT origin_hotkey, owner_at_deposit, unattributed_reason FROM noah_deposit "
        "WHERE block = ? AND event_ordinal = ?",
        (8_800_001, 145),
    ).fetchone()
    assert row == (_MINER_HOTKEY, GIVER, None)


def test_attribute_deposits_true_records_owner_not_entitled_but_does_not_lose_the_deposit(
    tmp_path,
):
    """The exact failure this whole ticket exists to close: a coldkey that
    did NOT own the named hotkey at the deposit's own block. The deposit is
    still recorded -- "received but unattributed" -- not silently credited
    to the wrong miner and not dropped."""
    store = a_store(tmp_path)
    parent_hash = "0x" + format(8_800_000, "x")
    chain = Chain(
        deposits={8_800_001: _a_batch_all_deposit()},
        extrinsics={8_800_001: _batch_all_extrinsics()},
        owners_at_hash={(_MINER_HOTKEY, parent_hash): "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"},
    )
    run(store, chain, attribute_deposits=True)
    row = store._conn.execute(
        "SELECT origin_hotkey, owner_at_deposit, unattributed_reason FROM noah_deposit "
        "WHERE block = ? AND event_ordinal = ?",
        (8_800_001, 145),
    ).fetchone()
    assert row[0] is None and row[1] is None
    assert "not entitled" in row[2]
    assert store.day_was_scanned(0)  # recorded, not a scan failure


def test_an_owner_source_connection_failure_stops_the_run_and_does_not_mark_the_day_scanned(
    tmp_path,
):
    """A failed owner read propagates ChainUnavailable and leaves the day unscanned.
    Transport failure must not become an attribution refusal or successful scan."""
    store = a_store(tmp_path)
    chain = Chain(
        deposits={8_800_001: _a_batch_all_deposit()},
        extrinsics={8_800_001: _batch_all_extrinsics()},
        owner_fail_at=8_800_000,  # block 8_800_001's own parent
    )
    with pytest.raises(ChainUnavailable):
        run(store, chain, attribute_deposits=True)
    assert not store.day_was_scanned(0)


def test_an_owner_source_connection_failure_is_a_refusal_not_an_ordinary_exception(tmp_path):
    """Distinguishing it from `OwnerNotEntitled`/`AmbiguousDepositBatch`/etc:
    those are recorded and the scan continues (proven above); this one is a
    `Refusal` that is NOT one of `deposits_in_block`'s own four caught
    attribution refusals, so it is never swallowed into a normal record."""
    store = a_store(tmp_path)
    chain = Chain(
        deposits={8_800_001: _a_batch_all_deposit()},
        extrinsics={8_800_001: _batch_all_extrinsics()},
        owner_fail_at=8_800_000,
    )
    with pytest.raises(Refusal):
        run(store, chain, attribute_deposits=True)


# ---- issue #282 item 4/4a, #302: no retroactive change to credited history --


def test_a_scanned_days_qualification_is_not_retroactively_changed_by_a_later_reconfigured_validator(
    tmp_path,
):
    """The check is baked in at SCAN time, inside `deposits_in_block`
    (called from `run_day`), never re-derived at read time against
    whatever `designated_validator_hotkey` happens to be configured now.

    Scans one real deposit (`a_deposit()`'s own position is `HOTKEY`)
    under a designated validator hotkey that matches it -- qualifies,
    `unattributed_reason` stays `None`. A second `run_day` call for the
    SAME day, configured with a DIFFERENT designated validator hotkey
    (one the deposit's own position does NOT match), must not re-derive
    or change that already-recorded row at all: `may_resume=True`
    (unchanged, the default) skips the day entirely once it is already
    marked scanned, so the row's qualification stays exactly what it was
    recorded as, regardless of what a later call configures."""
    store = a_store(tmp_path)
    chain = Chain(deposits={8_800_001: a_deposit()})

    run(store, chain, designated_validator_hotkey=HOTKEY)
    assert store.day_was_scanned(0)
    first_reason = store._conn.execute(
        "SELECT unattributed_reason FROM noah_deposit WHERE block = ? AND event_ordinal = ?",
        (8_800_001, 145),
    ).fetchone()[0]
    assert first_reason is None
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712

    other_hotkey = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
    assert other_hotkey != HOTKEY
    run(store, chain, designated_validator_hotkey=other_hotkey)  # may_resume=True, the default

    second_reason = store._conn.execute(
        "SELECT unattributed_reason FROM noah_deposit WHERE block = ? AND event_ordinal = ?",
        (8_800_001, 145),
    ).fetchone()[0]
    assert second_reason is None, (
        "a day already scanned under one designated_validator_hotkey must not have its "
        "already-recorded row's qualification retroactively changed by a later call "
        "configured with a different one"
    )
    assert store.noah_deposits(GIVER, 0) == 469_790_773_712


def test_a_wrong_validator_deposit_is_recorded_but_excluded_from_every_qualifying_sum(tmp_path):
    """The Store-level half of issue #282 item 4/4a, #302: a deposit
    `deposits_in_block` marks unattributed for landing on the wrong
    validator is a real, visible `noah_deposit` row (`Store.
    deposit_observations`/the raw table still carry it -- never dropped),
    but every QUALIFYING read excludes it: `Store.noah_deposits` (the
    wallet-wide day total), `Store.noah_deposits_for_hotkey` (already
    correctly excluded unattributed rows before this ticket), and `Store.
    cumulative_noah_deposits`."""
    store = a_store(tmp_path)
    other_validator = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    assert other_validator != HOTKEY
    chain = Chain(deposits={8_800_001: a_deposit()})

    run(store, chain, designated_validator_hotkey=other_validator)

    assert store.day_was_scanned(0)
    # The real row is still there.
    observations = store.deposit_observations(GIVER, 0)
    assert len(observations) == 1
    assert observations[0]["amount_rao"] == 469_790_773_712
    # But it qualifies nobody and nothing.
    assert store.noah_deposits(GIVER, 0) == 0
    assert store.noah_deposits_for_hotkey(GIVER, HOTKEY, 0) == 0
    assert store.cumulative_noah_deposits(GIVER, 0) == 0
