"""Driving pay through the runner, at the block the chain settled it.

Everything that turns "seat 140 was paid" into "this wallet was paid" is read at
the epoch block itself. Reading any of it at the end of the day would hand a
whole day of somebody else's earnings to whoever holds the seat on Sunday
night, and a seat changes hands often enough that this is a normal event rather
than an edge case.

The reads are also not free, so a block that is not an epoch block must cost
nothing beyond the events the runner already had. A test counts the questions.
"""

import pytest

from noah.chain.collateral import LockDesynced
from noah.chain.emissions import MechanismsMissing, SeatsNotContiguous
from noah.chain.holdings import Share
from noah.chain.pay import SlotTurnedOver
from noah.chain.runner import DepositBlockReorged, EpochBlockReorged, run_day
from noah.chain.sources import Refusal
from noah.chain.store import NOT_READ, Store

ANCHOR = 1767571200
NETUID = 35
ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
#: The deposit hotkey every StakeAdded fixture in this file already uses
#: (see e.g. _mixed_deposit_and_epoch_events's own local ``hotkey``) --
#: named here so run()'s own run_day call can pass it as
#: designated_validator_hotkey (issue #282 item 4/4a, #302).
HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

ALICE_HOT = "5AliceHotkeyAliceHotkeyAliceHotkeyAliceHotkeyAli"
ALICE_COLD = "5AliceWalletAliceWalletAliceWalletAliceWalletAlic"
BOB_HOT = "5BobHotkeyBobHotkeyBobHotkeyBobHotkeyBobHotkeyBob"
BOB_COLD = "5BobWalletBobWalletBobWalletBobWalletBobWalletBob"
OWNER_COLD = "5OwnerWalletOwnerWalletOwnerWalletOwnerWalletOwn"
OWNER_HOT = "5OwnerHotkeyOwnerHotkeyOwnerHotkeyOwnerHotkeyOwn"

FIRST = 8_800_000
LAST = 8_800_004
EPOCH = 8_800_002


def _held(store, coldkey, hotkey, day=0, amount=1):
    """Record one hotkey's holding directly (#193: `build_fixture` now reads
    `holdRao` from the store), on a block disjoint from anything `run_day`
    itself writes. share=1 over denominator=1 times a pool of `amount` reads
    back as exactly `amount`.
    """
    store.record_holding_sample(
        coldkey=coldkey,
        block_timestamp=ANCHOR + 300,
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


def pay_event(emissions, *, netuid=NETUID, mechanism=0):
    return {
        "ordinal_in_block": 3,
        "module_id": "SubtensorModule",
        "event_id": "IncentiveAlphaEmittedToMiners",
        "attributes": {"netuid": netuid + mechanism * 4096, "emissions": emissions},
    }


class Chain:
    """A stubbed chain that records every question, so cost can be asserted.

    ``seats`` and ``registrations`` may be given per block, which is how the
    mid-day handover test makes the seat map at the epoch block differ from the
    one at the end of the range.
    """

    def __init__(
        self,
        events=(),
        seats=None,
        registrations=None,
        owners=None,
        n=2,
        mechanisms=1,
        head=8_900_000,
    ):
        self.head = head
        self._events = dict(events)
        self._seats = seats if seats is not None else {0: ALICE_HOT, 1: BOB_HOT}
        self._registrations = registrations if registrations is not None else {0: 100, 1: 100}
        self._owners = owners or {
            ALICE_HOT: ALICE_COLD,
            BOB_HOT: BOB_COLD,
            OWNER_HOT: OWNER_COLD,
        }
        self._n = n
        self._mechanisms = mechanisms
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
        return ANCHOR + (block - FIRST) * 12

    def events(self, block):
        self.asked.append(("events", block))
        return self._events.get(block, [])

    def seat_hotkeys(self, block):
        self.asked.append(("seats", block))
        return dict(_at(self._seats, block))

    def registration_blocks(self, block):
        self.asked.append(("registrations", block))
        return dict(_at(self._registrations, block))

    def subnetwork_n(self, block):
        self.asked.append(("n", block))
        return self._n

    def mechanism_count(self, block):
        self.asked.append(("mechanisms", block))
        return self._mechanisms

    def hotkey_owners(self, hotkeys, block):
        self.asked.append(("owners", block, tuple(sorted(hotkeys))))
        return {hotkey: self._owners[hotkey] for hotkey in hotkeys if hotkey in self._owners}

    def subnet_owner(self, block):
        self.asked.append(("subnet_owner", block))
        return OWNER_COLD, OWNER_HOT

    # The four a holding takes. Present on the BASE fake rather than only on the
    # holdings one, because a fake that answers fewer questions than the runner
    # asks is how an interface gap stays invisible from inside a suite.
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
        """The hotkeys carrying a collateral row for this wallet.

        Derived from `locks` rather than stated separately, so the fake cannot
        report an aggregate that its own positions contradict. A fake that can
        be internally inconsistent makes the cross-check fail for a reason the
        chain would never produce.
        """
        return (ALICE_HOT,) if self.locks.get((coldkey, block), 0) else ()

    def miner_collateral(self, hotkey, coldkey, block):
        self.asked.append(("miner_collateral", block))
        held = self.locks.get((coldkey, block), 0)
        if not held:
            return None
        return {"locked": held, "drain_ratio": 1 << 63, "min_locked": 0, "earned": 0}

    def collateral_arity(self, block):
        """What the chain's metadata declares, matching the real runtime.

        ColdkeyMinerCollateral takes (netuid, coldkey); MinerCollateral takes
        (netuid, hotkey, coldkey); ColdkeyCollateralHotkeys takes
        (netuid, coldkey). Stated here rather than defaulted so a test can make
        one of them disagree and prove the check fires.
        """
        self.asked.append(("collateral_arity", block))
        return dict(self.arity)


def _at(table, block):
    """A table given per block, or one standing value used at every block."""
    if table and all(isinstance(key, int) and key > 1000 for key in table):
        return table[block]
    return table


def a_store(tmp_path, name="pay.db"):
    store = Store.create(tmp_path / name, day_anchor_ts=ANCHOR)
    store.record_emission_start(block=8_000_000, block_timestamp=ANCHOR, block_hash="0xs")
    return store


def run(store, chain, day=0, first=FIRST, last=LAST, stride=2):
    return run_day(
        store,
        chain,
        day=day,
        from_block=first,
        to_block=last,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://archive.example"},
        netuid=NETUID,
        holding_stride=stride,
        read_collateral=True,
    )


# ---------------------------------------------------------------------------


def test_one_epoch_block_records_what_each_wallet_earned(tmp_path):
    store = a_store(tmp_path)
    run(store, Chain(events={EPOCH: [pay_event([700, 300])]}))

    assert store.attributed_pay(ALICE_COLD, 0) == 700
    assert store.attributed_pay(BOB_COLD, 0) == 300


def test_a_block_that_is_not_an_epoch_block_costs_no_extra_reads(tmp_path):
    """The expensive reads happen at a handful of blocks in seven thousand."""
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, chain)

    extra = [asked for asked in chain.asked if asked[0] in ("seats", "n", "mechanisms", "owners")]
    assert {asked[1] for asked in extra} == {EPOCH}


def test_a_seat_paid_nothing_is_not_recorded_and_its_owner_is_never_looked_up(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 0])]})
    run(store, chain)

    assert store.attributed_pay(ALICE_COLD, 0) == 700
    assert store.attributed_pay(BOB_COLD, 0) == 0
    looked_up = [asked for asked in chain.asked if asked[0] == "owners"]
    assert looked_up == [("owners", EPOCH, (ALICE_HOT,))]


def test_the_seat_map_is_read_at_the_epoch_block_not_at_the_end_of_the_day(tmp_path):
    """The money test for reading at the epoch.

    Alice holds seat 0 when the epoch pays. Two blocks later the seat is handed
    to Bob. Reading the map at the end of the range would pay Bob everything
    Alice earned, and the total for the day would still be right.
    """
    store = a_store(tmp_path)
    chain = Chain(
        events={EPOCH: [pay_event([700, 300])]},
        seats={
            EPOCH: {0: ALICE_HOT, 1: BOB_HOT},
            LAST: {0: BOB_HOT, 1: BOB_HOT},
        },
        registrations={EPOCH: {0: 100, 1: 100}, LAST: {0: LAST, 1: 100}},
    )
    run(store, chain)

    assert store.attributed_pay(ALICE_COLD, 0) == 700, "Alice earned it and Alice keeps it"
    assert store.attributed_pay(BOB_COLD, 0) == 300


def test_pay_from_both_mechanisms_lands_on_one_wallet(tmp_path):
    store = a_store(tmp_path)
    run(
        store,
        Chain(
            events={EPOCH: [pay_event([700, 0]), pay_event([50, 0], mechanism=1)]},
            mechanisms=2,
        ),
    )
    assert store.attributed_pay(ALICE_COLD, 0) == 750


def test_a_reorg_during_the_epoch_read_burst_refuses_rather_than_mixing_forks(tmp_path):
    """The bookend check ``EpochBlockReorged`` exists for.

    ``events`` is read once, at the top of ``run_day``'s loop; the six
    epoch-state reads (seat map, seat count, mechanism count, registrations,
    and two ownership reads) all happen afterward, keyed by the block NUMBER
    alone -- nothing ties any of them back to the specific block CONTENT
    ``events`` was already read from. If the block's hash changes between the
    two, the seat map and ownership reads may describe a different fork than
    the pay events already read did, and attribution would silently pay
    whoever holds a seat on the NEW fork for what was earned on the OLD one.

    Found by real codex's adversarial challenge of issue #514's AgreeingChain
    port, and reproduced live (a two-source AgreeingChain where both sources
    move to a new fork's seat map between reading events and reading seats)
    before this fix existed.
    """
    store = a_store(tmp_path)

    class Reorging(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._epoch_hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != EPOCH:
                return f"0x{block:x}"
            # The first call is run_day's own, at the top of the loop,
            # before `events` is read. Every call after that returns the
            # reorg'd hash. This chain's data is otherwise perfectly
            # ordinary (a normal seat map, normal registrations), so
            # nothing inside `_epoch_attribution`'s read burst raises
            # anything -- it returns a valid-looking attribution, just one
            # describing the wrong fork. The late check, after that burst,
            # is what catches this: see the read-count assertion below.
            self._epoch_hash_calls += 1
            return "0xold" if self._epoch_hash_calls == 1 else "0xnew"

    chain = Reorging(events={EPOCH: [pay_event([700, 300])]})
    with pytest.raises(EpochBlockReorged) as refused:
        run(store, chain)
    assert str(EPOCH) in str(refused.value)
    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"
    with pytest.raises(DayNotScanned):
        store.attributed_pay(ALICE_COLD, 0)

    epoch_read_types = {asked[0] for asked in chain.asked if asked[1] == EPOCH}
    assert {"seats", "n", "mechanisms", "owners", "registrations", "subnet_owner"} <= (
        epoch_read_types
    ), (
        "all six epoch-state reads must run to completion, not merely one of them -- "
        "nothing inside them detects this reorg, since the data is internally consistent; "
        "only the late check, after all six finish, catches it"
    )
    assert refused.value.__context__ is None, (
        "this must be the LATE check's own direct raise, not a reclassification of some "
        "other refusal caught inside the try -- nothing in this chain's data raises "
        "anything, so there is no exception for one to chain from"
    )


def test_a_reorg_that_already_happened_is_diagnosed_as_a_reorg_not_as_a_slot_turnover(tmp_path):
    """The diagnosis-quality regression real codex found across three
    rounds, at three different read sites, each time the fix's own
    previous round left one exposed: round 2's review and challenge found
    ``_epoch_attribution``'s six reads could raise their own misleading
    refusal (``SlotTurnedOver``, here) if a reorg had already happened
    before that burst started; round 3's challenge then found the SAME
    class one site earlier, at ``_block_time``, which an early check
    planted only before ``_epoch_attribution`` never covered.

    ``run_day`` now wraps the WHOLE per-block read burst -- ``_block_time``
    and ``_epoch_attribution`` together -- in one ``try``, and reclassifies
    ANY ``Refusal`` raised inside it by re-checking the hash: real, if the
    hash has not moved; a reorg, if it has. This test drives exactly the
    scenario round 2 found: the block has already reorged by the time
    ``events`` is read, and the reorg'd fork's registration data would
    trip ``SlotTurnedOver``'s own "this seat changed hands" check.

    The six epoch-state reads DO run -- they are what produces
    ``SlotTurnedOver`` in the first place, from ``attribute_epoch``'s own
    per-seat validation, not from a read itself failing. What must not
    happen is that ``SlotTurnedOver`` reaching the operator: the operator
    sees "this block reorged", true and actionable (retry), not "seat 0
    turned over", which is neither -- there was no genuine turnover, the
    registration data itself came from an already-stale read.
    """
    store = a_store(tmp_path)

    class ReorgAlreadyHappened(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._reorged = False

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != EPOCH:
                return f"0x{block:x}"
            return "0xnew" if self._reorged else "0xold"

        def events(self, block):
            result = super().events(block)
            if block == EPOCH:
                # The reorg has already happened by the time run_day would
                # start is_epoch's own read burst -- before any epoch-state
                # read, not during one.
                self._reorged = True
            return result

        def registration_blocks(self, block):
            self.asked.append(("registrations", block))
            # The reorg'd fork's registration data: seat 0 looks like it
            # changed hands AT the epoch block -- exactly what a genuine
            # seat turnover produces, and exactly what SlotTurnedOver
            # exists to catch when it is genuine.
            return {0: EPOCH, 1: 100}

    chain = ReorgAlreadyHappened(events={EPOCH: [pay_event([700, 300])]})
    with pytest.raises(EpochBlockReorged) as refused:
        run(store, chain)
    assert str(EPOCH) in str(refused.value)

    epoch_read_types = {asked[0] for asked in chain.asked if asked[1] == EPOCH}
    assert {"seats", "n", "mechanisms", "owners", "registrations", "subnet_owner"} <= (
        epoch_read_types
    ), (
        "all six epoch-state reads must run to completion -- SlotTurnedOver comes from "
        "attribute_epoch's own validation, not from a read failing, so there is no way to "
        "avoid running every one of them; what must happen is that SlotTurnedOver never "
        "reaches the operator unreclassified"
    )
    # The read-type check above proves the reads happened, not that
    # SlotTurnedOver specifically was what fired and got reclassified --
    # EpochBlockReorged could in principle have been raised some other way
    # (a bug that skipped attribution entirely and refused for an unrelated
    # reason would still pass the assertions above). __context__ settles it
    # directly: Python's own implicit exception chaining means the reorg
    # exception `_raise_reorged` raises from inside the `except Refusal:`
    # handler carries the ORIGINAL caught exception as its __context__.
    assert isinstance(refused.value.__context__, SlotTurnedOver), (
        "must be a reclassified SlotTurnedOver specifically, not some other refusal or "
        "a direct raise with nothing chained"
    )
    assert not store.day_was_scanned(0)


def _mixed_deposit_and_epoch_events():
    """One block carrying both a real deposit and a real pay event.

    Shared by the two tests below, which both drive it through a chain that
    reorgs specifically DURING the epoch-state reads (see ``_ReorgsOnRead``)
    -- the exact timing window issue #518's own follow-up fix (Finding 1,
    codex's adversarial challenge of the first version of this fix,
    2026-08-29) closed: a reorg landing strictly between an early, separate
    deposit check and a later, separate epoch bookend.
    """
    giver = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    deposit_events = [
        {
            "ordinal_in_block": 12,
            "extrinsic_idx": 4,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [ARK, hotkey, 8_000_000_000, 469_790_773_712, NETUID, 0],
        },
        {
            "ordinal_in_block": 13,
            "extrinsic_idx": 4,
            "module_id": "SubtensorModule",
            "event_id": "StakeTransferred",
            "attributes": [giver, ARK, hotkey, NETUID, NETUID, 8_000_000_000],
        },
    ]
    return giver, {EPOCH: deposit_events + [pay_event([700, 300])]}


class _ReorgsOnRead(Chain):
    """The block's hash flips the moment ANY epoch-state read for it starts.

    Not a call-counted flip: a reorg happens at a wall-clock MOMENT, not at
    "the Nth chain call", and tying it to a call count coupled this test to
    exactly how many hash reads one particular version of the fix happened
    to make. Tying it instead to "has an epoch-state read for this block
    started" models the real timing question -- did the reorg land before or
    after the point a version of the fix chose to check -- regardless of how
    many reads either version needs to get there. ``seat_hotkeys`` is always
    the first epoch-state read (``_epoch_attribution``'s own docstring: the
    seat map has to come first), so flipping there is flipping at the
    earliest possible moment of that read burst.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._reorged = False

    def block_hash(self, block):
        self.asked.append(("hash", block))
        if block != EPOCH:
            return f"0x{block:x}"
        return "0xnew" if self._reorged else "0xold"

    def seat_hotkeys(self, block):
        self._reorged = True
        return super().seat_hotkeys(block)


def _row_counts(store, block):
    """The number of physical rows this block holds, read below the store's
    own ``day_was_scanned`` gate.

    Every public read on ``Store`` refuses for an unscanned day, which is
    real protection for a caller but makes it impossible to use the public
    API to prove a refused run left NO rows behind -- a query that refuses
    before looking is indistinguishable from a query that looked and found
    nothing. This reads the underlying tables directly, the only way to
    settle the question codex's own round-2 adversarial challenge raised:
    are the rows actually absent, or does the assertion only ever get
    refused before it can check?
    """
    deposits = store._conn.execute(
        "SELECT COUNT(*) FROM noah_deposit WHERE block = ?", (block,)
    ).fetchone()[0]
    pay = store._conn.execute(
        "SELECT COUNT(*) FROM attributed_pay WHERE block = ?", (block,)
    ).fetchone()[0]
    return deposits, pay


def _locked_pay_row_counts(store, block):
    """The same proof as ``_row_counts``, for the two locked-pay tables.

    A separate function rather than a third element on ``_row_counts``:
    every existing caller of that one asserts a 2-tuple, and issue #548 is
    the first reorg test that needs locked pay in the count at all.
    """
    settled = store._conn.execute(
        "SELECT COUNT(*) FROM locked_pay WHERE block = ?", (block,)
    ).fetchone()[0]
    unreadable = store._conn.execute(
        "SELECT COUNT(*) FROM locked_pay_unreadable WHERE block = ?", (block,)
    ).fetchone()[0]
    return settled, unreadable


def test_a_mixed_deposit_and_epoch_block_reorging_before_the_shared_bookend_commits_nothing(
    tmp_path,
):
    """The ordering question issue #518 asks to be answered deliberately.

    A block can carry a deposit AND be the epoch block in the same range. A
    first version of this fix checked the deposit early -- before the
    epoch-state reads even started -- and committed it right away, leaving
    ``_record_epoch``'s own, later, independent bookend as the only thing
    protecting the epoch's pay. Real codex's own adversarial challenge
    (2026-08-29,
    ``verification/codex/2026-08-29_issue-518-deposit-block-reorg-challenge.txt``)
    found the gap live: a reorg landing between the two independent checks
    let the deposit commit and then the epoch bookend refuse, leaving that
    committed deposit permanently behind under a refusal that was supposed
    to mean nothing for the block was kept.

    Now there is one bookend, shared, deferred until AFTER every read for
    this block -- deposit-relevant and epoch-state alike -- has finished.
    The epoch-state reads (seat map, registrations, owners, subnet owner)
    DO run before the check fires; they are reads, not writes, so running
    them costs nothing risky. What must never happen is either a deposit
    ``_record`` or a ``store.record_epoch`` before the check passes. Proven
    non-vacuous against the first version of this fix: with
    ``_ReorgsOnRead``, that version raised ``EpochBlockReorged`` only AFTER
    committing the deposit; this version raises ``DepositBlockReorged``
    before committing anything.

    Round 2 of codex's own adversarial challenge found the ``DayNotScanned``
    assertions below prove only that the STORE'S OWN GATE refuses to serve
    this day, not that no row physically exists underneath it -- a query
    that refuses before looking is indistinguishable from one that looked
    and found nothing. ``_row_counts`` reads the tables directly, below
    that gate, to settle it for real.
    """
    giver, events = _mixed_deposit_and_epoch_events()
    store = a_store(tmp_path)
    chain = _ReorgsOnRead(events=events)

    with pytest.raises(DepositBlockReorged) as refused:
        run(store, chain)
    assert str(EPOCH) in str(refused.value)
    assert "epoch pay" in str(refused.value), (
        "the message should say the block's epoch pay was also at risk, not just its deposits"
    )

    epoch_read_types = {asked[0] for asked in chain.asked if asked[1] == EPOCH}
    assert {"seats", "n", "mechanisms", "owners", "registrations", "subnet_owner"} <= (
        epoch_read_types
    ), (
        "all six epoch-state reads are harmless reads and must still run before the shared "
        "bookend -- only the two COMMITS (deposit and epoch) are gated on it"
    )
    assert refused.value.__context__ is None, (
        "this chain's data is internally consistent -- seat_hotkeys flips the reorg flag "
        "but the seat map and registrations it returns are otherwise ordinary, so nothing "
        "inside _epoch_attribution raises; this must be the late check's own direct raise"
    )

    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"
    with pytest.raises(DayNotScanned):
        store.attributed_pay(ALICE_COLD, 0)
    with pytest.raises(DayNotScanned):
        store.noah_deposits(giver, 0)
    assert _row_counts(store, EPOCH) == (0, 0), (
        "not merely unscanned -- physically zero rows for this block in either table"
    )


def test_a_refused_mixed_block_leaves_nothing_for_a_clean_retry_to_trip_over(tmp_path):
    """The concrete damage real codex traced from the bug the test above now
    guards against: a stale row surviving a refused attempt, invisible to
    every later run's own idempotency check because that check only ever
    fires for a (block, ordinal) key a later run asks about again -- and if
    the canonical chain no longer has anything at that key, no later run
    ever asks.

    An ordinary, clean retry over the exact same range, with the exact same
    events, records the deposit and the epoch pay exactly once each -- not
    zero (a stale row silently surviving untouched), not twice (a stale row
    plus a fresh one double-counting), and not an ``ObservationChanged``
    refusal (a stale row colliding with a fresh one at the same key). Uses
    the identical ``_ReorgsOnRead`` timing the test above proved commits
    nothing on the first pass; this one carries that through a second,
    clean pass.

    This models a retry that finds the SAME deposit again, which round 2 of
    codex's own adversarial challenge correctly noted an idempotent accept
    at the store layer cannot fully distinguish from a stale row that
    happened to match. See
    ``test_a_refused_deposit_that_turns_out_to_be_on_an_orphaned_fork_is_never_recorded``
    below for the sharper case that same challenge named: a retry whose
    canonical read no longer has the deposit AT ALL.
    """
    giver, events = _mixed_deposit_and_epoch_events()
    store = a_store(tmp_path)

    with pytest.raises(DepositBlockReorged):
        run(store, _ReorgsOnRead(events=events))
    assert not store.day_was_scanned(0)
    assert _row_counts(store, EPOCH) == (0, 0)

    # A clean retry: same range, same events, but the chain no longer reorgs
    # mid-read (an ordinary settled chain, the shape every retry after a
    # transient reorg eventually sees).
    run(store, Chain(events=events))

    assert store.day_was_scanned(0)
    assert store.noah_deposits(giver, 0) == 469_790_773_712, (
        "exactly the one real deposit -- zero would mean a stale row silently absorbed it, "
        "double would mean a stale row survived alongside the fresh one"
    )
    assert store.attributed_pay(ALICE_COLD, 0) == 700
    assert store.attributed_pay(BOB_COLD, 0) == 300


def test_a_refused_deposit_that_turns_out_to_be_on_an_orphaned_fork_is_never_recorded(tmp_path):
    """Round 1 of real codex's own adversarial challenge's own sharpest
    scenario, modeled directly: the deposit a first attempt read turns out
    to have been on a fork that never became canonical. A retry's clean,
    settled read of the same block shows only the epoch's pay -- no
    deposit at all, because the extrinsic that sent it was never actually
    part of the chain everyone else agreed on.

    Before this round's fix, a version that committed the deposit early
    (before the epoch's own later bookend could catch the reorg) would
    leave that deposit permanently in the store: the canonical retry never
    asks about a (block, ordinal) key it no longer sees anything at, so the
    store's own idempotency check in ``record_noah_deposit`` never gets a
    chance to notice or correct it. The proof that this fix actually closes
    that is not "the total comes out right" (round 1's `noah_deposits`
    check on this exact deposit could, in principle, be satisfied by a
    stale row that happens to match); it is that the deposit is recorded
    ZERO times when the canonical chain never had it, which a stale row
    surviving from the failed attempt would contradict.
    """
    giver, mixed_events = _mixed_deposit_and_epoch_events()
    canonical_events = {EPOCH: [pay_event([700, 300])]}  # the deposit never really happened

    store = a_store(tmp_path)

    with pytest.raises(DepositBlockReorged):
        run(store, _ReorgsOnRead(events=mixed_events))
    assert not store.day_was_scanned(0)
    assert _row_counts(store, EPOCH) == (0, 0)

    # The canonical retry: the epoch's pay is real, but the deposit this
    # block briefly appeared to carry was on a fork that lost, and this
    # settled read never sees it.
    run(store, Chain(events=canonical_events))

    assert store.day_was_scanned(0)
    assert store.noah_deposits(giver, 0) == 0, (
        "the deposit never happened on the canonical chain -- a stale row from the "
        "refused attempt would show up here as a nonzero total nobody ever actually sent"
    )
    assert store.attributed_pay(ALICE_COLD, 0) == 700
    assert store.attributed_pay(BOB_COLD, 0) == 300
    assert _row_counts(store, EPOCH) == (0, 2), (
        "zero deposit rows, two attributed-pay rows (Alice and Bob) -- exactly what the "
        "canonical chain actually contains for this block"
    )


# ---- a reorg during locked-pay's own collateral reads (issue #548) ---------
#
# Every reorg test above models a fork change landing during the SIX
# epoch-state reads _epoch_attribution makes. Issue #548 moved
# _locked_pay_reads's four collateral reads inside the same guarded burst,
# running strictly AFTER _epoch_attribution has already succeeded -- so this
# models the fork changing in that later window instead, and proves the same
# bookend catches it there too.


def test_a_reorg_during_the_locked_pay_read_burst_refuses_and_knows_epoch_pay_was_at_risk(
    tmp_path,
):
    """The bookend covers locked-pay's own reads too, not just epoch state.

    ``_epoch_attribution``'s six reads run first and see the pre-reorg,
    internally consistent fork, so ``attribution`` is a real, complete
    result -- Alice and Bob really are paid 700 and 300 -- by the time
    ``_locked_pay_reads`` starts. The reorg lands exactly then:
    ``coldkey_collateral``'s aggregate read already reflects the new fork,
    but ``miner_collateral``'s positions read a moment later still answers
    for the old one, so the witness check (``lock_total_rao``) finds the two
    disagreeing -- ``LockDesynced``, a message that says nothing about a
    reorg at all. Before issue #548's restructure this read wasn't inside
    any guarded burst, so ``LockDesynced`` would have reached the operator
    unreclassified; now it is caught by the identical ``except Refusal``
    handler ``_epoch_attribution``'s own reads already relied on.

    This block also carries a real deposit, which is what lets this test
    also prove the ``epoch_pay_at_risk`` refinement that came with moving
    these reads here: by the time ``_locked_pay_reads`` raises,
    ``attribution`` already holds Alice's and Bob's real, nonzero pay, so
    the ``DepositBlockReorged`` message can -- and must -- say the epoch's
    pay was at risk too, not just the deposit.
    """
    giver, events = _mixed_deposit_and_epoch_events()
    store = a_store(tmp_path)

    class ReorgsDuringCollateralRead(Chain):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._reorged = False

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != EPOCH:
                return f"0x{block:x}"
            return "0xnew" if self._reorged else "0xold"

        def coldkey_collateral(self, coldkey, block):
            # The earliest read _locked_pay_reads makes (ALICE_COLD sorts
            # before BOB_COLD, and this is its own first call, at
            # block - 1). Flipping the flag here, rather than in
            # block_hash, models the reorg landing at the start of this
            # read burst specifically, after _epoch_attribution's burst has
            # already finished.
            self._reorged = True
            return super().coldkey_collateral(coldkey, block)

        def miner_collateral(self, hotkey, coldkey, block):
            if self._reorged and block == EPOCH:
                # The reorg'd fork's position read no longer agrees with
                # the aggregate read moments earlier -- exactly what a
                # genuine cross-fork read race produces, and exactly the
                # shape LockDesynced exists to catch.
                self.asked.append(("miner_collateral", block))
                return {"locked": 400, "drain_ratio": 1 << 63, "min_locked": 0, "earned": 0}
            return super().miner_collateral(hotkey, coldkey, block)

    chain = ReorgsDuringCollateralRead(events=events)
    chain.locks = {(ALICE_COLD, EPOCH - 1): 0, (ALICE_COLD, EPOCH): 1_000}

    with pytest.raises(DepositBlockReorged) as refused:
        run(store, chain)
    assert str(EPOCH) in str(refused.value)
    assert "epoch pay" in str(refused.value), (
        "attribution had already succeeded, with Alice's and Bob's real pay in hand, by "
        "the time _locked_pay_reads raised -- epoch_pay_at_risk must reflect that, not "
        "the unconditional False the except handler used before this fix"
    )
    assert isinstance(refused.value.__context__, LockDesynced), (
        "must be a reclassified LockDesynced specifically -- the collateral reads' own "
        "internal-consistency check, not a hash mismatch caught some other way"
    )
    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"
    assert _row_counts(store, EPOCH) == (0, 0), (
        "not merely unscanned -- physically zero deposit and attributed-pay rows for this "
        "block; the collateral read failing must not leave the epoch's pay or the deposit "
        "committed behind it"
    )
    assert _locked_pay_row_counts(store, EPOCH) == (0, 0), (
        "and zero locked-pay rows of either kind -- a raise from inside _locked_pay_reads "
        "must leave _commit_locked_pay never called at all, not called and then rolled back"
    )


def test_a_reorg_during_the_locked_pay_read_burst_that_stays_internally_consistent_is_caught_by_the_late_check(  # noqa: E501
    tmp_path,
):
    """The other half of the story: a reorg the burst itself cannot see.

    ``test_a_reorg_during_the_locked_pay_read_burst_refuses_and_knows_epoch_pay_was_at_risk``
    above proves a collateral read that trips its OWN internal-consistency
    check (``LockDesynced``) gets reclassified. This proves the case that
    check cannot catch on its own: the reorg'd fork's collateral state is
    perfectly self-consistent (aggregate and positions agree with each
    other, just not with what the pre-reorg fork would have answered), so
    nothing inside ``_locked_pay_reads`` raises anything at all -- exactly
    the same shape ``test_a_reorg_during_the_epoch_read_burst_refuses_rather_than_mixing_forks``
    proves for the six epoch-state reads. Only the late, unconditional
    ``_check_not_reorged`` -- which now gates ``_commit_locked_pay`` the
    same way it already gated ``store.record_epoch`` -- catches this one.
    """
    store = a_store(tmp_path)

    class ReorgsSilentlyDuringCollateralRead(Chain):
        # No overrides beyond block_hash: chain.locks drives
        # coldkey_collateral/collateral_hotkeys/miner_collateral identically
        # on every call, so whatever this fixture answers always agrees with
        # itself -- exactly what a reorg'd fork's own honestly-read state
        # would also do, and exactly why nothing inside _locked_pay_reads
        # has any reason to raise.
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._hash_calls = 0

        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block != EPOCH:
                return f"0x{block:x}"
            # The first call is run_day's own, at the top of the loop,
            # before `events` is read. The second is the late,
            # unconditional _check_not_reorged, after every read (epoch
            # state AND collateral) has finished without raising.
            self._hash_calls += 1
            return "0xold" if self._hash_calls == 1 else "0xnew"

    chain = ReorgsSilentlyDuringCollateralRead(events={EPOCH: [pay_event([700, 300])]})
    chain.locks = {(ALICE_COLD, EPOCH - 1): 0, (ALICE_COLD, EPOCH): 1_000}

    with pytest.raises(EpochBlockReorged) as refused:
        run(store, chain)
    assert str(EPOCH) in str(refused.value)
    assert refused.value.__context__ is None, (
        "nothing inside the try raises anything here -- this chain's collateral data is "
        "internally consistent throughout, so this must be the late check's own direct "
        "raise, not a reclassification of some other caught refusal"
    )
    assert not store.day_was_scanned(0)
    assert _row_counts(store, EPOCH) == (0, 0)
    assert _locked_pay_row_counts(store, EPOCH) == (0, 0), (
        "the collateral read itself never raised, so only the late, unconditional check -- "
        "the same one that already gated store.record_epoch -- stands between a silently "
        "wrong lock reading and a committed row"
    )


def test_a_mechanism_that_paid_and_was_not_read_stops_the_run(tmp_path):
    store = a_store(tmp_path)
    with pytest.raises(MechanismsMissing):
        run(store, Chain(events={EPOCH: [pay_event([700, 300])]}, mechanisms=2))
    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"


def test_a_gap_in_the_seat_map_stops_the_run(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]}, seats={0: ALICE_HOT, 5: BOB_HOT})
    with pytest.raises(SeatsNotContiguous):
        run(store, chain)
    assert not store.day_was_scanned(0)


def test_the_subnet_owners_own_seat_is_withheld_rather_than_paid(tmp_path):
    """The runtime burns this pay after announcing it (run_coinbase.rs:659-690)."""
    store = a_store(tmp_path)
    chain = Chain(
        events={EPOCH: [pay_event([700, 300])]},
        seats={0: ALICE_HOT, 1: OWNER_HOT},
    )
    run(store, chain)

    assert store.attributed_pay(ALICE_COLD, 0) == 700
    assert store.attributed_pay(OWNER_COLD, 0) == 0


def test_running_the_same_day_twice_records_it_once(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, chain)
    run(store, chain)
    assert store.attributed_pay(ALICE_COLD, 0) == 700


def test_the_provenance_names_the_pay_read(tmp_path):
    store = a_store(tmp_path)
    provenance = run(store, Chain(events={EPOCH: [pay_event([700, 300])]}))
    fields = {read["field"] for read in provenance.as_dict()["reads"]}
    assert "incentiveAlphaEmitted" in fields
    assert {
        "keys",
        "subnetworkN",
        "mechanismCountCurrent",
        "blockAtRegistration",
        "owner",
        "subnetOwner",
    } <= fields, "every read that decided the pay is named"


def test_a_block_with_nothing_in_it_costs_no_timestamp_read(tmp_path):
    """Seven thousand blocks a day, and almost none of them need a timestamp.

    The timestamp is used only to decide which day a deposit or an epoch falls
    in. Fetching one for every block is one wasted round trip per block, which
    over a day is more chain traffic than everything else the runner does.
    """
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    # Not sampled, said deliberately, so the only timestamp that can be read is
    # the one that decides which day the epoch belongs to.
    run(store, chain, stride=None)

    stamped = {asked[1] for asked in chain.asked if asked[0] == "time"}
    assert stamped == {EPOCH}, f"a timestamp was read for {sorted(stamped)}"


def test_a_block_carrying_a_deposit_still_gets_its_timestamp(tmp_path):
    """The saving must not cost the reads that decide which day a deposit is in.

    Real addresses, because the deposit reader checks that an address could be
    one. The made-up names used for the pay tests above contain letters base58
    does not have, and it refused them, correctly.
    """
    giver = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

    store = a_store(tmp_path)
    deposit_block = FIRST + 1
    events = {
        deposit_block: [
            {
                "ordinal_in_block": 12,
                "extrinsic_idx": 4,
                "module_id": "SubtensorModule",
                "event_id": "StakeAdded",
                "attributes": [ARK, hotkey, 8_000_000_000, 469_790_773_712, NETUID, 0],
            },
            {
                "ordinal_in_block": 13,
                "extrinsic_idx": 4,
                "module_id": "SubtensorModule",
                "event_id": "StakeTransferred",
                "attributes": [giver, ARK, hotkey, NETUID, NETUID, 8_000_000_000],
            },
        ]
    }
    chain = Chain(events=events)
    run(store, chain, stride=None)

    stamped = {asked[1] for asked in chain.asked if asked[0] == "time"}
    assert deposit_block in stamped, "the read that decides which day a deposit is in"
    assert store.noah_deposits(giver, 0) == 469_790_773_712


# ---------------------------------------------------------------------------
# A block's time is a read like any other, and it can come back a default.
# ---------------------------------------------------------------------------

from noah.chain.runner import BlockTimeNotRead  # noqa: E402


def test_a_timestamp_of_zero_is_refused_rather_than_becoming_a_day(tmp_path):
    """Read live on 2026-08-20: 1,545 blocks out of 5,278 answered zero.

    Timestamp.Now is a ValueQuery, so a read that did not happen answers with
    the default rather than failing. Nothing errored during that run. A zero
    dates the block to 1970, which either lands every deposit in it in the first
    day of the store or falls before the anchor, depending on the anchor.
    """
    store = a_store(tmp_path)

    class Silent(Chain):
        def block_timestamp(self, block):
            self.asked.append(("time", block))
            return 0

    with pytest.raises(BlockTimeNotRead) as refused:
        run(store, Silent(events={EPOCH: [pay_event([700, 300])]}))
    assert "0" in str(refused.value)
    assert not store.day_was_scanned(0)


def test_a_block_time_that_does_not_move_forward_is_refused(tmp_path):
    """Stronger than refusing zero, and it costs nothing.

    Block times rise. A later block answering with an earlier block's time is a
    source answering about the wrong block, or a client pairing a response with
    the wrong request, and both produce a real-looking number.
    """
    store = a_store(tmp_path)

    class Frozen(Chain):
        def block_timestamp(self, block):
            self.asked.append(("time", block))
            return ANCHOR + 100

    events = {EPOCH: [pay_event([700, 300])], EPOCH + 1: [pay_event([700, 300])]}
    with pytest.raises(BlockTimeNotRead):
        run(store, Frozen(events=events))


def test_block_times_that_do_move_forward_are_fine(tmp_path):
    store = a_store(tmp_path)
    run(store, Chain(events={EPOCH: [pay_event([700, 300])], EPOCH + 1: [pay_event([0, 0])]}))
    assert store.attributed_pay(ALICE_COLD, 0) == 700


def test_a_block_time_that_is_not_a_number_says_so_rather_than_becoming_a_type_error(tmp_path):
    """Which guard stopped a run is the useful part of a refusal.

    Comparing text against zero raises an ordinary TypeError, which the runner's
    outer handler turns into a generic RunRefused. The run still stops, so the
    money is safe either way, and the operator loses the one sentence that would
    have told them a source sent an undecoded value.
    """
    store = a_store(tmp_path)

    class Undecoded(Chain):
        def block_timestamp(self, block):
            self.asked.append(("time", block))
            return "0x5f2a"

    with pytest.raises(BlockTimeNotRead) as refused:
        run(store, Undecoded(events={EPOCH: [pay_event([700, 300])]}))
    assert "0x5f2a" in str(refused.value)


# ---------------------------------------------------------------------------
# The day a run is told it is reading, against the day its blocks are in.
# ---------------------------------------------------------------------------

from noah.chain.runner import RunRefused, WrongDay  # noqa: E402
from noah.chain.store import DayNotScanned  # noqa: E402

DAY_SECONDS = 86_400


def test_a_range_that_holds_none_of_the_day_it_claims_is_refused(tmp_path):
    """The caller picks the block range. If it picks the wrong one, the day is
    marked as read and holds another day's rows, which reads as complete
    forever. Every row still lands in the right day, because the store files by
    the row's own timestamp; it is the finished-marker that goes wrong.
    """
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})

    with pytest.raises(WrongDay) as refused:
        run(store, chain, day=5)
    assert "5" in str(refused.value)
    assert not store.day_was_scanned(5)


def test_a_range_holding_the_day_it_claims_is_fine_even_if_it_spills_over(tmp_path):
    """Ranges do not land on day boundaries, so spilling into the next day is
    ordinary and must not stop a run."""
    store = a_store(tmp_path)

    class Spilling(Chain):
        def block_timestamp(self, block):
            self.asked.append(("time", block))
            # The first epoch sits in day 0, the second just over into day 1.
            return ANCHOR + (0 if block == EPOCH else DAY_SECONDS) + (block - FIRST)

    # Not sampled. This test deliberately reads ONE block range as two days to
    # see the spilled row, and a sample block cannot belong to two days: the
    # store refuses the second, correctly. Holdings are not what this is about.
    chain = Spilling(events={EPOCH: [pay_event([700, 300])], EPOCH + 1: [pay_event([500, 0])]})
    run(store, chain, day=0, stride=None)

    assert store.attributed_pay(ALICE_COLD, 0) == 700
    assert store.day_was_scanned(0)
    assert not store.day_was_scanned(1), "the spill does not let day 1 claim to be read"

    # The spilled row was recorded all the same, filed by its own timestamp. The
    # store will not report a day it was never told was read, which is why this
    # has to run day 1 to see it rather than just asking.
    with pytest.raises(DayNotScanned):
        store.attributed_pay(ALICE_COLD, 1)

    run(store, chain, day=1, stride=None)
    assert store.attributed_pay(ALICE_COLD, 1) == 500, "the spill landed in the day it belongs to"


def test_a_day_where_nothing_happened_is_still_a_day_that_was_read(tmp_path):
    """Before a subnet emits there are no epochs and there may be no deposits.
    A run over such a day has nothing to check itself against, and it must not
    refuse: an unread day gets done again forever.

    day=0 here because it is the day `first`/`last` (the run() default)
    actually fall in under this fake's clock -- a quiet range is still
    checked against a reference read of its own first block (see the test
    below), so the label given has to genuinely match the range for this
    one to stay a "nothing happened" case rather than a "caller's arithmetic
    is wrong" one.
    """
    store = a_store(tmp_path)
    run(store, Chain(), day=0)
    assert store.day_was_scanned(0)


def test_a_quiet_range_under_the_wrong_day_is_refused_even_with_no_wallets_yet(tmp_path):
    """A mislabeled quiet range must leave no marker of any kind behind.

    A brand-new store has no wallets yet, and with the default stride
    _sample_holdings's own coarse day-check (reordered ahead of its
    no-wallets exit, so that exit is covered too -- codex's #454 confirming
    pass on the run_day-level fix below: `_sample_holdings` used to write a
    real, committed "day N sampled" row from that exact branch, for exactly
    this fresh-store-plus-wrong-day shape, before this check -- or
    run_day's own fallback further down -- ever got a chance to refuse the
    run) is what actually catches this. Checking BOTH that nothing got
    marked scanned AND that nothing got marked sampled is the point: either
    marker alone being wrong would read as this day being done when nothing
    about it was ever verified.
    """
    store = a_store(tmp_path)
    # FIRST..LAST is genuinely day 0 under this fake's clock (see run()'s own
    # defaults and the test above). Telling the runner it is day 1, with a
    # range that carries no epoch or deposit event to catch the mistake any
    # other way, is exactly the caller-arithmetic-is-wrong case this exists
    # to refuse.
    with pytest.raises(WrongDay) as refused:
        run(store, Chain(), day=1)
    assert "1" in str(refused.value)
    assert not store.day_was_scanned(1)
    assert store.sampling_of_day(1) is None


def test_the_run_day_level_fallback_still_catches_it_when_sampling_is_off(tmp_path):
    """The same wrong-day-while-quiet case, with _sample_holdings out of it.

    stride=None means _sample_holdings returns in its very first line,
    before it asks anything -- see its own "deliberate do not sample"
    early exit. So this is the one case where run_day's OWN fallback check
    (not the sampler's, now that the sampler's runs first when it runs at
    all) is the only thing standing between a mislabeled quiet range and a
    day marked scanned forever.
    """
    store = a_store(tmp_path)
    with pytest.raises(WrongDay) as refused:
        run(store, Chain(), day=1, stride=None)
    assert "1" in str(refused.value)
    assert not store.day_was_scanned(1)


def test_a_quiet_range_padded_early_across_a_boundary_is_not_a_false_rejection(tmp_path):
    """Checking from_block alone would refuse a range that is actually fine.

    Padding from_block a little early across a day boundary is exactly as
    legitimate as letting to_block spill late past one -- the spill test
    above already treats the late case as ordinary. A quiet range whose
    from_block sits a moment into the PREVIOUS day but whose to_block is
    genuinely in the claimed day must be accepted, not refused: refusing it
    would be a false rejection of a range that never actually got anything
    wrong, found by codex's #454 confirming pass against the first version
    of this fallback (which read only from_block).
    """
    store = a_store(tmp_path)

    class PaddedEarly(Chain):
        def block_timestamp(self, block):
            self.asked.append(("time", block))
            if block == FIRST:
                return ANCHOR + DAY_SECONDS - 12  # the last moment of day 0
            return ANCHOR + DAY_SECONDS + (block - FIRST)  # day 1 from here on

    # Quiet: no events anywhere in the range, so the main pass never touches
    # days_seen and this is the fallback's own case to get right.
    run(store, PaddedEarly(), day=1)
    assert store.day_was_scanned(1)


# ---------------------------------------------------------------------------
# Holdings sampled across the day, which is what criterion 14 asks for.
# ---------------------------------------------------------------------------

from noah.chain.store import DayNotSampled, SplitNotRead  # noqa: E402


class Holding(Chain):
    """A chain that can also answer the four questions a holding takes.

    Deliberately a separate fake from the pay one. The runner asks fourteen
    questions now, and a fake that answers ten while the runner asks fourteen is
    exactly the gap NOAH-VALIDATOR found in the agreement gate.
    """

    def __init__(self, stakes=None, **kwargs):
        super().__init__(**kwargs)
        # coldkey -> hotkey -> (share_raw, denom_raw, pool_alpha)
        self._stakes = stakes if stakes is not None else {}

    def staking_hotkeys(self, coldkey, block):
        self.asked.append(("staking_hotkeys", block))
        return sorted(self._stakes.get(coldkey, {}))

    def alpha_share(self, hotkey, coldkey, block):
        if hotkey not in self._stakes.get(coldkey, {}):
            return Share(encoding="safefloat", raw=0, exponent=0, source="absent")
        share, _denom, _pool = self._stakes[coldkey][hotkey]
        return Share(encoding="u64f64", raw=share, exponent=0, source="v1")

    def hotkey_shares(self, hotkey, block):
        for held in self._stakes.values():
            if hotkey in held:
                return Share(encoding="u64f64", raw=held[hotkey][1], exponent=0, source="v1")
        return None

    def hotkey_alpha(self, hotkey, block):
        for held in self._stakes.values():
            if hotkey in held:
                return held[hotkey][2]
        return 0


def run_sampling(store, chain, day=0, first=FIRST, last=LAST, stride=2):
    return run_day(
        store,
        chain,
        day=day,
        from_block=first,
        to_block=last,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://archive.example"},
        netuid=NETUID,
        holding_stride=stride,
        read_collateral=True,
    )


def test_a_wallet_that_was_paid_has_its_holding_sampled(tmp_path):
    store = a_store(tmp_path)
    chain = Holding(
        events={EPOCH: [pay_event([700, 0])]},
        stakes={ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}},
    )
    run_sampling(store, chain)

    samples = store.holding_samples(ALICE_COLD, 0)
    assert samples, "a wallet that earned this day is sampled"
    assert samples[0]["total_rao"] == 250


def test_every_sample_is_kept_and_not_only_the_last(tmp_path):
    """Criterion 14's whole point: a wallet holding for one block and a wallet
    holding throughout must leave different traces, even when the figure at the
    evaluation height is the same."""
    store = a_store(tmp_path)
    chain = Holding(
        events={EPOCH: [pay_event([700, 0])]},
        stakes={ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}},
    )
    run_sampling(store, chain, stride=2)

    samples = store.holding_samples(ALICE_COLD, 0)
    assert len(samples) > 1, "the day is sampled on a stride, not once at the end"
    assert [s["block"] for s in samples] == sorted(s["block"] for s in samples)


def test_the_split_is_recorded_as_not_read_rather_than_zero(tmp_path):
    store = a_store(tmp_path)
    chain = Holding(
        events={EPOCH: [pay_event([700, 0])]},
        stakes={ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}},
    )
    run_sampling(store, chain)

    assert store.holding_samples(ALICE_COLD, 0)[0]["split_was_read"] is False
    with pytest.raises(SplitNotRead):
        store.locked_rao(ALICE_COLD, 0)


def test_a_run_with_no_stride_is_refused_rather_than_defaulted(tmp_path):
    """A stride decides how finely a day is watched, and a default would be
    this session choosing it quietly for every run that forgot."""
    store = a_store(tmp_path)
    chain = Holding(events={EPOCH: [pay_event([700, 0])]})
    with pytest.raises(TypeError):
        run_day(
            store,
            chain,
            day=0,
            from_block=FIRST,
            to_block=LAST,
            ark_coldkey=ARK,
            designated_validator_hotkey=HOTKEY,
            sources={"archive": "wss://a"},
            netuid=NETUID,
        )


def test_a_wallet_staking_nowhere_is_still_sampled_at_zero(tmp_path):
    """Recorded rather than skipped: a wallet with no position is a fact about
    the day, and skipping it is indistinguishable from never looking."""
    store = a_store(tmp_path)
    chain = Holding(events={EPOCH: [pay_event([700, 0])]}, stakes={})
    run_sampling(store, chain)

    samples = store.holding_samples(ALICE_COLD, 0)
    assert samples and samples[0]["total_rao"] == 0


def test_a_stride_of_zero_or_less_is_refused(tmp_path):
    """A stride is how finely a day is watched. Zero is not a resolution."""
    store = a_store(tmp_path)
    chain = Holding(
        events={EPOCH: [pay_event([700, 0])]}, stakes={ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}}
    )
    for bad in (0, -1):
        with pytest.raises(RunRefused):
            run_sampling(store, chain, stride=bad)


def test_a_quiet_day_still_samples_a_wallet_seen_in_an_earlier_one(tmp_path):
    """Criterion 14's real shape, and the case the timestamp fallback exists for.

    A wallet that earned in day 0 and did nothing in day 1 still has a holding
    in day 1, and the door reads it. Nothing in day 1 carries an event, so the
    block pass stamps no block and the sampler has no timestamp in hand: it
    takes one, once, at the start of the range.

    Skipping her because the day was quiet would be indistinguishable from
    never having looked, and it is the day a shortfall is most likely to go
    unnoticed.
    """
    store = a_store(tmp_path)
    stakes = {ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}}

    run_sampling(store, Holding(events={EPOCH: [pay_event([700, 0])]}, stakes=stakes))
    assert store.attributed_pay(ALICE_COLD, 0) == 700

    # A range whose blocks really do fall in day 1 under the fake's clock,
    # which ticks twelve seconds a block from the anchor.
    quiet_first = FIRST + (DAY_SECONDS // 12) + 1
    run_day(
        store,
        Holding(events={}, stakes=stakes),
        day=1,
        from_block=quiet_first,
        to_block=quiet_first + 4,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://a"},
        netuid=NETUID,
        holding_stride=2,
        read_collateral=True,
    )

    samples = store.holding_samples(ALICE_COLD, 1)
    assert samples, "a wallet seen earlier is still sampled in a day she did nothing in"
    assert samples[0]["total_rao"] == 250


def test_a_quiet_range_that_belongs_to_another_day_is_refused(tmp_path):
    """The gap the sampler's own day check exists for.

    The wrong-day guard reads the blocks that CARRIED something. A quiet range
    carries nothing, so that guard passes, and without this check the holdings
    would be filed under whatever day the run happened to claim. A holding in
    the wrong day is a door opened or closed on the wrong evidence.
    """
    store = a_store(tmp_path)
    stakes = {ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}}
    run_sampling(store, Holding(events={EPOCH: [pay_event([700, 0])]}, stakes=stakes))

    # Blocks that fall in day 0 under the fake's clock, run as though day 1.
    with pytest.raises(WrongDay) as refused:
        run_day(
            store,
            Holding(events={}, stakes=stakes),
            day=1,
            from_block=FIRST,
            to_block=FIRST + 2,
            ark_coldkey=ARK,
            designated_validator_hotkey=HOTKEY,
            sources={"archive": "wss://a"},
            netuid=NETUID,
            holding_stride=2,
            read_collateral=True,
        )
    assert "[0]" in str(refused.value) and "day 1" in str(refused.value)


def test_samples_use_a_time_from_the_run_s_own_day_not_the_last_one_seen(tmp_path):
    """A range that spills past the boundary must not file its holdings ahead.

    Block ranges do not land on day boundaries, so the LAST timestamp a run
    sees is often in the next day. Filing the samples from it would put a whole
    day's holdings one day early, and the door would read them against the
    wrong day's pay.
    """
    store = a_store(tmp_path)

    class Straddling(Holding):
        """A range whose FIRST block is in the previous day.

        That is the ordinary shape at a boundary, and it is what separates the
        two candidate timestamps: the first block of the range is in day 0, and
        the epoch that actually paid is in day 1. Taking the range's first
        block would file a day 1 holding under day 0.
        """

        def block_timestamp(self, block):
            self.asked.append(("time", block))
            if block <= FIRST:
                return ANCHOR + 10  # day 0
            return ANCHOR + DAY_SECONDS + (block - FIRST)  # day 1

    chain = Straddling(
        events={EPOCH: [pay_event([700, 0])]},
        stakes={ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}},
    )
    run_sampling(store, chain, day=1, stride=2)

    assert store.holding_samples(ALICE_COLD, 1), "filed in the day the run was reading"
    assert store.attributed_pay(ALICE_COLD, 1) == 700


def test_a_tail_sample_that_spills_into_the_next_day_is_not_folded_into_this_one(tmp_path):
    """The gap the test above does not close.

    That test proves the run picks the right REFERENCE timestamp. It does not
    prove every sample is checked against it: the sampler used to stamp every
    block in the stride with that one reference timestamp, so a sample block
    genuinely in the next day -- ranges do not land on day boundaries, and
    the LAST sample block is exactly where a run is most likely to have
    crossed one -- was filed under this day anyway. A holding read in the
    wrong day can move that day's minimum and change whether a grace
    condition is met. Found by codex, 2026-08-28.

    The spilled block is proven excluded from BOTH days by this call, not
    merely absent from day 0: an earlier version of this fix filed it under
    day 1 automatically, which a second round of adversarial review showed
    can mutate an already-completed day (see `_sample_holdings`'s own
    docstring). This call must touch neither.
    """
    store = a_store(tmp_path)
    stakes = {ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}}

    class TailSpill(Holding):
        """FIRST and EPOCH sit in day 0. LAST, the last sample block in the
        stride, is genuinely in day 1."""

        def block_timestamp(self, block):
            self.asked.append(("time", block))
            if block >= LAST:
                return ANCHOR + DAY_SECONDS + 5
            return ANCHOR + (block - FIRST) * 12

    chain = TailSpill(events={EPOCH: [pay_event([700, 0])]}, stakes=stakes)
    run_sampling(store, chain, day=0, stride=2)

    sampled_blocks = {s["block"] for s in store.holding_samples(ALICE_COLD, 0)}
    assert LAST not in sampled_blocks, (
        f"block {LAST} is genuinely in day 1 under this fake's clock, and got folded into "
        f"day 0's samples anyway: {sorted(sampled_blocks)}"
    )

    # Not filed anywhere by THIS call -- day 1 was never asked, so it has no
    # sampling record yet at all, not even the one row that would prove a
    # silent cross-day write happened.
    with pytest.raises(DayNotSampled):
        store.holding_samples(ALICE_COLD, 1)

    # Not lost forever either: a later, genuinely day-1 sampling call, on its
    # own terms, still finds it -- because the block's own real time always
    # named day 1, this call is simply the first one that was actually asked
    # about day 1.
    run_day(
        store,
        TailSpill(events={}, stakes=stakes),
        day=1,
        from_block=LAST,
        to_block=LAST,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://a"},
        netuid=NETUID,
        holding_stride=1,
        read_collateral=True,
    )
    assert LAST in {s["block"] for s in store.holding_samples(ALICE_COLD, 1)}, (
        "the same block, asked about under its own day, is not unreadable -- only this "
        "call, which was never asked about day 1, was barred from writing it there"
    )


def test_a_head_sample_that_spills_into_the_previous_day_is_not_folded_into_this_one(tmp_path):
    """The companion in the other direction, and the more dangerous one.

    The first sample block in a range can just as easily still be in the day
    before the one being sampled. In normal sequential processing that prior
    day has typically ALREADY been marked sampled by the time this run
    happens, so filing into it here would mutate a day already treated as
    settled -- worse than the tail case, which at least spills forward into a
    day nobody has touched yet.
    """
    store = a_store(tmp_path)

    class HeadSpill(Holding):
        """FIRST is genuinely in day 0. EPOCH and LAST are in day 1, the
        day this run is told to sample."""

        def block_timestamp(self, block):
            self.asked.append(("time", block))
            if block <= FIRST:
                return ANCHOR + 10
            return ANCHOR + DAY_SECONDS + (block - FIRST)

    chain = HeadSpill(
        events={EPOCH: [pay_event([700, 0])]},
        stakes={ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}},
    )
    run_sampling(store, chain, day=1, stride=2)

    sampled_blocks = {s["block"] for s in store.holding_samples(ALICE_COLD, 1)}
    assert FIRST not in sampled_blocks, (
        f"block {FIRST} is genuinely in day 0 under this fake's clock, and got folded into "
        f"day 1's samples anyway: {sorted(sampled_blocks)}"
    )

    # And not silently filed into day 0 either -- this run was asked about
    # day 1 only, and day 0 was never itself sampled, so it must still show
    # no sampling record at all.
    with pytest.raises(DayNotSampled):
        store.holding_samples(ALICE_COLD, 0)


def test_a_stride_that_lands_entirely_outside_the_target_day_is_refused(tmp_path):
    """The degenerate case the skip logic opens up, closed the same way every
    other impossible-to-honor request is closed here: refuse rather than mark
    a day sampled when nothing sampled actually landed in it.

    The stride is widened to 4 so EPOCH -- the block that supplies `day_time`
    -- falls OUTSIDE the sampled points {FIRST, LAST} entirely. With stride 2,
    EPOCH is always itself a sample point, so whatever day its timestamp
    names is guaranteed to satisfy the loop on its own iteration; this is the
    only shape that can actually reach the all-skipped branch.
    """
    store = a_store(tmp_path)
    stakes = {ALICE_COLD: {ALICE_HOT: (1, 4, 1_000)}}

    class AllSpill(Holding):
        """EPOCH -- not itself a sampled point at stride 4 -- is genuinely in
        day 0, which is what makes `day_time` say day 0 and the coarse
        sanity check pass. The two points actually sampled, FIRST and LAST,
        are both genuinely in day 1."""

        def block_timestamp(self, block):
            self.asked.append(("time", block))
            if block == EPOCH:
                return ANCHOR + 10
            return ANCHOR + DAY_SECONDS + (block - FIRST)

    chain = AllSpill(events={EPOCH: [pay_event([700, 0])]}, stakes=stakes)
    with pytest.raises(WrongDay) as refused:
        run_sampling(store, chain, day=0, stride=4)
    assert "zero real samples" in str(refused.value)
    assert store.sampling_of_day(0) is None, (
        "a refusal must not leave day 0 marked sampled: that would read as a day of "
        "wallets holding nothing, when every real sample actually named day 1"
    )


# ---- what the epoch did to each paid wallet's collateral lock ---------------


def test_a_wallet_whose_lock_did_not_move_is_recorded_as_read_and_still(tmp_path):
    """The row that separates 'looked at, nothing moved' from 'nobody looked'.

    Without it those two are the same absence, and the second is what used to be
    emitted as a confident zero for every miner in every day.
    """
    store = a_store(tmp_path)
    run(store, Chain(events={EPOCH: [pay_event([700, 300])]}))
    assert store.locked_pay(ALICE_COLD, 0) == (0, 0)
    assert store.locked_pay_was_read(ALICE_COLD, 0)


def test_a_lock_that_grew_across_the_epoch_is_pay_that_arrived_locked(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    chain.locks = {(ALICE_COLD, EPOCH - 1): 1_000, (ALICE_COLD, EPOCH): 1_600}
    run(store, chain)
    assert store.locked_pay(ALICE_COLD, 0) == (600, 0)


def test_a_lock_that_shrank_across_the_epoch_is_pay_that_unlocked(tmp_path):
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    chain.locks = {(ALICE_COLD, EPOCH - 1): 1_600, (ALICE_COLD, EPOCH): 1_000}
    run(store, chain)
    assert store.locked_pay(ALICE_COLD, 0) == (0, 600)


def test_the_lock_is_read_either_side_of_the_epoch_block(tmp_path):
    """Either side, not once. One reading cannot be a change."""
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, chain)
    asked = [row[1] for row in chain.asked if row[0] == "coldkey_collateral"]
    assert EPOCH in asked and EPOCH - 1 in asked


def test_only_wallets_the_epoch_paid_have_their_lock_read(tmp_path):
    """A wallet earning nothing has nothing captured and nothing released,
    because settle returns immediately on a zero emission, so asking would be
    one more question per wallet per epoch for a zero already known."""
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 0])]})
    run(store, chain)
    assert store.locked_pay_was_read(ALICE_COLD, 0)
    assert not store.locked_pay_was_read(BOB_COLD, 0)


def test_a_run_that_did_not_read_collateral_says_so_per_wallet(tmp_path):
    """The ruled fallback. Not a silence: a silence is what it replaces."""
    store = a_store(tmp_path)
    run_day(
        store,
        Chain(events={EPOCH: [pay_event([700, 300])]}),
        day=0,
        from_block=FIRST,
        to_block=LAST,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        sources={"archive": "wss://archive.example"},
        netuid=NETUID,
        holding_stride=None,
        read_collateral=False,
    )
    assert store.locked_pay_unreadable(ALICE_COLD, 0) is not None
    assert "did not read collateral" in store.locked_pay_unreadable(ALICE_COLD, 0)
    with pytest.raises(Refusal):
        store.locked_pay(ALICE_COLD, 0)


def test_a_miner_moving_collateral_in_the_epoch_block_holds_the_day_out(tmp_path):
    """The one case a state delta cannot separate.

    The event names a hotkey and the lock is keyed by a coldkey, and the coldkey
    is the signer rather than the hotkey's owner, so the miner's own transaction
    cannot be told apart from what the epoch did. Held out and named.
    """
    store = a_store(tmp_path)
    by_hand = {
        "event_id": "CollateralLocked",
        "ordinal_in_block": 9,
        "attributes": {"hotkey": ALICE_HOT, "locked": 500},
    }
    chain = Chain(events={EPOCH: [pay_event([700, 300]), by_hand]})
    chain.locks = {(ALICE_COLD, EPOCH - 1): 0, (ALICE_COLD, EPOCH): 500}
    run(store, chain)
    reason = store.locked_pay_unreadable(ALICE_COLD, 0)
    assert reason is not None and ALICE_HOT in reason
    with pytest.raises(Refusal):
        store.locked_pay(ALICE_COLD, 0)


def test_an_unreadable_epoch_poisons_the_whole_day_not_just_itself(tmp_path):
    """A day with one unreadable epoch has a sum that looks complete and is
    short by that epoch, which is the quiet undercount, not a partial answer."""
    store = a_store(tmp_path)
    store.record_locked_pay(
        coldkey=ALICE_COLD,
        block=EPOCH - 100,
        block_timestamp=ANCHOR + 10,
        locked_arrival_rao=250,
        unlocked_rao=0,
        block_hash="0xa",
    )
    store.mark_locked_pay_unreadable(
        coldkey=ALICE_COLD,
        block=EPOCH,
        block_timestamp=ANCHOR + 20,
        reason="a miner moved collateral by hand in this block",
    )
    with pytest.raises(Refusal):
        store.locked_pay(ALICE_COLD, 0)


def test_a_map_whose_declared_key_count_disagrees_stops_the_run(tmp_path):
    """The witness, wired rather than merely available.

    It was built and called by nothing for several commits: the reads went
    straight to the aggregate, which is the same shape as the defect this whole
    path exists to close. This proves it now runs.

    It matters most for exactly the read the runner relies on.
    ColdkeyMinerCollateral is ValueQuery, so an absent row, a real zero and a
    malformed question are all the same bytes.
    """
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    chain.arity["ColdkeyMinerCollateral"] = 3
    with pytest.raises(Refusal) as refused:
        run(store, chain)
    assert "2" in str(refused.value) and "3" in str(refused.value)


def test_the_arity_is_asked_once_per_epoch_not_once_per_wallet(tmp_path):
    """A metadata read per wallet would be one question per wallet per epoch
    for an answer that cannot differ between them."""
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, chain)
    assert len([r for r in chain.asked if r[0] == "collateral_arity"]) == 1


def test_positions_disagreeing_with_the_aggregate_stops_the_run(tmp_path):
    """The key-order witness, which only exists once somebody holds collateral.

    Arity cannot see this: MinerCollateral takes a hotkey and a coldkey next to
    each other and both are account ids, so a read with them swapped is well
    formed and comes back absent. The runtime keeps the aggregate as the running
    sum of the positions, so summing is what catches it.
    """
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    chain.locks = {(ALICE_COLD, EPOCH - 1): 0, (ALICE_COLD, EPOCH): 1_000}

    class Swapped(type(chain)):
        def miner_collateral(self, hotkey, coldkey, block):
            return None  # what a swapped-key read returns: well formed, absent

    swapped = Swapped(events={EPOCH: [pay_event([700, 300])]})
    swapped.locks = dict(chain.locks)
    with pytest.raises(Refusal) as refused:
        run(store, swapped)
    assert "1000" in str(refused.value)


def test_a_wallet_holding_nothing_costs_no_position_reads(tmp_path):
    """The legitimate case nearest the illegitimate one, and today it is every
    wallet on every subnet: the collateral maps are empty across the network, so
    this is the path that actually runs."""
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, chain)
    assert [r for r in chain.asked if r[0] == "miner_collateral"] == []
    assert store.locked_pay(ALICE_COLD, 0) == (0, 0)


def test_the_position_map_arity_is_checked_too_not_just_the_aggregate(tmp_path):
    """Found by sweeping: replacing the literal 3 with the declared value left
    the suite green, because the check then compared a value against itself.

    That is a third shape worth naming. It is not a guard nobody defends, nor a
    guard nobody reaches. It runs, it is reached, and it cannot fail. The only
    thing that finds it is a test that makes the two sides genuinely differ.
    """
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    chain.locks = {(ALICE_COLD, EPOCH - 1): 0, (ALICE_COLD, EPOCH): 1_000}
    chain.arity["MinerCollateral"] = 2
    with pytest.raises(Refusal) as refused:
        run(store, chain)
    assert "MinerCollateral" in str(refused.value)
    assert "3" in str(refused.value) and "2" in str(refused.value)


# ---- resuming an interrupted run --------------------------------------------


def test_a_finished_day_is_not_read_again(tmp_path):
    """The cheap half of resuming: what is done stays done."""
    store = a_store(tmp_path)
    chain = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, chain)
    asked_first = len(chain.asked)

    again = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, again)
    assert again.asked == [], (
        "a day already read was read again. Resuming a fortnight's outage would "
        "re-read every block of it, and on an endpoint that charges by depth that "
        "is the difference between minutes and hours"
    )
    assert asked_first > 0, "the first run asked nothing, so this proves nothing"


def test_a_day_that_stopped_partway_is_read_again_whole(tmp_path):
    """The other half. A run that stopped is not a run that finished.

    The store is deliberately left holding whatever the failed run wrote, and
    the second run must still produce the right answer over it rather than
    doubling anything, because rows are keyed by seat and block.
    """
    store = a_store(tmp_path)

    class StopsAfterTheEpoch(Chain):
        """Fails on a block AFTER the epoch, so the store keeps partial rows.

        A failure before the epoch would leave an empty store, and an empty
        store re-read gives the right answer for a reason that has nothing to do
        with resuming.
        """

        def events(self, block):
            if block > EPOCH:
                raise RuntimeError("the source went away mid-day")
            return super().events(block)

    with pytest.raises(Refusal):
        run(store, StopsAfterTheEpoch(events={EPOCH: [pay_event([700, 300])]}))
    # Read past attributed_pay on purpose: it refuses an unscanned day, which is
    # right and is exactly the state under test. The point here is that rows
    # SURVIVED the stop, so the resumed run is writing over something.
    kept = store._conn.execute(
        "SELECT COUNT(*) FROM attributed_pay WHERE coldkey = ?", (ALICE_COLD,)
    ).fetchone()[0]
    assert kept == 1, (
        "nothing was written before the stop, so this proves nothing about resuming "
        "over partial rows"
    )
    assert not store.day_was_scanned(0), "a day that stopped is not a day that was read"

    whole = Chain(events={EPOCH: [pay_event([700, 300])]})
    run(store, whole)
    assert whole.asked, "the resumed run skipped a day that never finished"
    assert store.attributed_pay(ALICE_COLD, 0) == 700, (
        "the resumed run did not reproduce the day's pay exactly. A partial run's "
        "rows plus a whole run's rows must be the whole run's rows, or an outage "
        "silently doubles somebody's pay"
    )


def test_an_interrupted_run_resumed_matches_one_that_was_never_interrupted(tmp_path):
    """Criterion 11, end to end and byte for byte.

    Two stores reach the same place by different routes: one read straight
    through, the other stopped partway and was resumed. What is compared is the
    rendered fixture, not the row counts, because a store can hold the same
    totals arranged differently and a fixture is what anybody downstream sees.
    """
    from noah.chain.fixture_build import build_fixture
    from noah.scoring.fixture_io import render_output

    def fixture_of(store):
        _held(store, ALICE_COLD, ALICE_HOT)
        _held(store, BOB_COLD, BOB_HOT)
        return render_output(
            build_fixture(
                store,
                name="resume",
                eval_day=0,
                day_epoch_ts=ANCHOR,
                dials={
                    "targetFp": "500000000",
                    "harshness": 1,
                    "capFp": "1000000000",
                    "halfLifeDays": "182",
                    "streakCeilingFp": "2000000000",
                    "streakRampDays": "365",
                    "ongoingHoldRao": "400000000000",
                },
                seats={ALICE_COLD: {ALICE_HOT: 0}, BOB_COLD: {BOB_HOT: 1}},
            )
        )

    class StopsAfterTheEpoch(Chain):
        def events(self, block):
            if block > EPOCH:
                raise RuntimeError("the source went away mid-day")
            return super().events(block)

    straight = a_store(tmp_path)
    run(straight, Chain(events={EPOCH: [pay_event([700, 300])]}))

    (tmp_path / "b").mkdir()
    resumed = a_store(tmp_path / "b")
    with pytest.raises(Refusal):
        run(resumed, StopsAfterTheEpoch(events={EPOCH: [pay_event([700, 300])]}))
    run(resumed, Chain(events={EPOCH: [pay_event([700, 300])]}))

    assert fixture_of(resumed) == fixture_of(straight)
