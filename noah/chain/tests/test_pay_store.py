"""Attributed pay in the store: one row per seat per epoch, with its height.

The store keeps what was seen, not what it meant. A pay row is an observation
about one seat at one block, so the same epoch read twice cannot pay anybody
twice, and a figure the project publishes can be walked back to the block it
came from.
"""

import pytest

from noah.chain.pay import attribute_epoch
from noah.chain.store import (
    DayNotScanned,
    NotAnAmount,
    NotAnIdentifier,
    ObservationChanged,
    Store,
)

ANCHOR = 1767571200  # a Monday 00:00:00 UTC
DAY = 86_400
COLDKEY = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
HOTKEY = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"


def a_store(tmp_path, anchor=ANCHOR):
    return Store.create(tmp_path / "noah.db", day_anchor_ts=anchor)


def pay(store, **overrides):
    call = {
        "coldkey": COLDKEY,
        "hotkey": HOTKEY,
        "uid": 140,
        "block_timestamp": ANCHOR + 3 * DAY,
        "amount_rao": 35_551_889_238,
        "block": 8_875_512,
        "block_hash": "0xa",
    }
    call.update(overrides)
    return store.record_attributed_pay(**call)


def test_pay_lands_in_the_day_the_anchor_puts_it_in(tmp_path):
    store = a_store(tmp_path)
    assert pay(store) == 3


def test_the_day_is_derived_here_and_never_handed_in(tmp_path):
    """Two stores, two anchors, the same block. A guard on a day number that
    the caller supplies defends the label and not the data.
    """
    early = Store.create(tmp_path / "early.db", day_anchor_ts=ANCHOR)
    late = Store.create(tmp_path / "late.db", day_anchor_ts=ANCHOR + DAY)
    ts = ANCHOR + 3 * DAY
    assert pay(early, block_timestamp=ts) == 3
    assert pay(late, block_timestamp=ts) == 2


def test_a_day_that_was_scanned_with_no_pay_reads_zero(tmp_path):
    store = a_store(tmp_path)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay(COLDKEY, 3) == 0


def test_a_day_nobody_scanned_refuses_rather_than_reading_zero(tmp_path):
    """A miner paid in a day the reader never looked at is not a miner paid
    nothing, and the difference decides whether their streak survives."""
    store = a_store(tmp_path)
    with pytest.raises(DayNotScanned):
        store.attributed_pay(COLDKEY, 3)


def test_two_seats_held_by_one_wallet_in_one_day_are_summed(tmp_path):
    store = a_store(tmp_path)
    pay(store, uid=140, amount_rao=35_551_889_238)
    pay(store, uid=80, amount_rao=15_417_400_074, hotkey="5Hpvci")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay(COLDKEY, 3) == 35_551_889_238 + 15_417_400_074


def test_the_same_epoch_recorded_twice_does_not_pay_anybody_twice(tmp_path):
    """A run that died mid-day and was restarted re-reads epochs it already
    stored. Doing that must be free."""
    store = a_store(tmp_path)
    pay(store)
    pay(store)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay(COLDKEY, 3) == 35_551_889_238


def test_the_same_seat_and_block_coming_back_with_a_different_amount_refuses(tmp_path):
    """The dangerous half of idempotence. Ignoring the second write keeps the
    first answer and says nothing, so a source that changed its mind about a
    settled block is never noticed. The two answers cannot both be true.
    """
    store = a_store(tmp_path)
    pay(store, amount_rao=35_551_889_238)
    with pytest.raises(ObservationChanged) as excinfo:
        pay(store, amount_rao=35_551_889_239)
    message = str(excinfo.value)
    assert "35551889238" in message and "35551889239" in message


def test_the_same_seat_and_block_coming_back_under_a_different_wallet_refuses(tmp_path):
    store = a_store(tmp_path)
    pay(store)
    with pytest.raises(ObservationChanged):
        pay(store, coldkey="5SomebodyElse")


def test_the_same_seat_at_a_different_block_is_a_separate_payment(tmp_path):
    store = a_store(tmp_path)
    pay(store, block=8_875_512, amount_rao=100)
    pay(store, block=8_875_872, amount_rao=200, block_hash="0xb")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay(COLDKEY, 3) == 300


def test_a_published_figure_walks_back_to_the_blocks_it_came_from(tmp_path):
    """The claim the whole project rests on. A total is not evidence; the rows
    behind it, each with its height and hash, are."""
    store = a_store(tmp_path)
    pay(store, uid=140, amount_rao=100, block=8_875_512, block_hash="0xa")
    pay(store, uid=80, amount_rao=200, block=8_875_872, block_hash="0xb", hotkey="5Hpvci")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.pay_observations(COLDKEY, 3) == [
        {
            "uid": 140,
            "hotkey": HOTKEY,
            "amount_rao": 100,
            "block": 8_875_512,
            "block_hash": "0xa",
        },
        {
            "uid": 80,
            "hotkey": "5Hpvci",
            "amount_rao": 200,
            "block": 8_875_872,
            "block_hash": "0xb",
        },
    ]


def test_a_read_returns_only_that_wallet_and_only_that_day(tmp_path):
    store = a_store(tmp_path)
    pay(store, amount_rao=100)
    pay(store, coldkey="5Other", amount_rao=999, uid=7, block=8_875_513)
    pay(store, block_timestamp=ANCHOR + 4 * DAY, amount_rao=555, block=8_876_000)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    store.mark_day_scanned(4, from_block=3, to_block=4)
    assert store.attributed_pay(COLDKEY, 3) == 100
    assert store.attributed_pay(COLDKEY, 4) == 555


@pytest.mark.parametrize(
    "bad_amount",
    ["35551889238", "0x84a2f6b16", None, True, 1.0, -1],
    ids=["decimal-str", "hex", "null", "bool", "float", "negative"],
)
def test_an_amount_that_is_not_a_plain_integer_never_reaches_a_row(tmp_path, bad_amount):
    store = a_store(tmp_path)
    with pytest.raises(NotAnAmount):
        pay(store, amount_rao=bad_amount)


# ---- nothing on the money path raises an ordinary exception -----------------
#
# Refusal descends from BaseException so that a retry loop written as
# `except Exception` cannot swallow it and finish the run with a plausible
# number. Any ordinary exception escaping these methods hands that failure
# straight back, through a door the design was built to close.


@pytest.mark.parametrize(
    "field,bad",
    [
        ("coldkey", b"\x01\x02"),
        ("coldkey", None),
        ("coldkey", ""),
        ("coldkey", 5),
        ("hotkey", b"\x01\x02"),
        ("hotkey", None),
        ("block_hash", None),
        ("block_hash", ""),
    ],
    ids=[
        "coldkey-bytes",
        "coldkey-null",
        "coldkey-empty",
        "coldkey-int",
        "hotkey-bytes",
        "hotkey-null",
        "hash-null",
        "hash-empty",
    ],
)
def test_an_account_that_is_not_text_never_reaches_a_row(tmp_path, field, bad):
    """A wallet arriving as raw bytes is stored happily and then found by
    nobody, because every later read asks for the text form. The pay sits in
    the table and the participant reads zero. That is this module's own
    silent zero, manufactured by itself.
    """
    store = a_store(tmp_path)
    with pytest.raises(NotAnIdentifier):
        pay(store, **{field: bad})


@pytest.mark.parametrize(
    "field,bad",
    [
        ("uid", -1),
        ("uid", 1.5),
        ("uid", True),
        ("uid", "140"),
        ("block", 14.5),
        ("block", "8875512"),
    ],
    ids=["uid-negative", "uid-float", "uid-bool", "uid-str", "block-float", "block-str"],
)
def test_a_seat_number_or_height_that_is_not_a_plain_integer_never_reaches_a_row(
    tmp_path, field, bad
):
    store = a_store(tmp_path)
    with pytest.raises(NotAnAmount):
        pay(store, **{field: bad})


def test_a_block_height_of_zero_is_refused_rather_than_left_to_the_database(tmp_path):
    """The table rejects it with an IntegrityError, which is an ordinary
    exception and therefore swallowable. The refusal has to come first."""
    store = a_store(tmp_path)
    with pytest.raises(NotAnAmount):
        pay(store, block=0)


@pytest.mark.parametrize("bad_day", ["3", 3.0, None, True], ids=["str", "float", "null", "bool"])
def test_a_day_that_is_not_a_plain_integer_is_refused_on_read(tmp_path, bad_day):
    """The day column has integer affinity, so the database converts a string
    day and answers as though nothing happened. A module whose rule is never
    to coerce must not leave the coercion to its storage."""
    store = a_store(tmp_path)
    pay(store, amount_rao=500)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    with pytest.raises(NotAnAmount):
        store.attributed_pay(COLDKEY, bad_day)


def test_a_wallet_that_is_not_text_is_refused_on_read(tmp_path):
    store = a_store(tmp_path)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    with pytest.raises(NotAnIdentifier):
        store.attributed_pay(b"\x01", 3)


# ---- the settled-block comparison, field by field ---------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"hotkey": "5ADifferentHotkey"},
        {"block_hash": "0xdifferent"},
        {"block_timestamp": ANCHOR + 4 * DAY},
    ],
    ids=["hotkey", "block-hash", "day"],
)
def test_a_settled_block_that_changes_any_of_its_answers_refuses(tmp_path, change):
    """Dropping any single field from the comparison lets that field change
    silently. Each one is pinned separately so a tidy-up cannot quietly
    shorten the check."""
    store = a_store(tmp_path)
    pay(store)
    with pytest.raises(ObservationChanged):
        pay(store, **change)


def test_the_second_recording_of_one_epoch_still_answers_with_the_day(tmp_path):
    store = a_store(tmp_path)
    assert pay(store) == 3
    assert pay(store) == 3


# ---- order is part of the answer --------------------------------------------


def test_two_seats_paid_in_the_same_block_come_back_in_seat_order(tmp_path):
    """This project's trust model is two implementations agreeing byte for
    byte, so nothing may depend on the order a database happens to return."""
    store = a_store(tmp_path)
    for uid in (9, 3, 7):
        pay(store, uid=uid, amount_rao=uid, block=8_875_512, hotkey=f"5Hot{uid}")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert [row["uid"] for row in store.pay_observations(COLDKEY, 3)] == [3, 7, 9]


# ---- the two halves have to join without a hand-written middle --------------


def an_epoch(**overrides):
    call = {
        "emissions_by_mechanism": {0: {140: 100, 80: 200}},
        "hotkey_by_uid": {140: HOTKEY, 80: "5HpvciGmspgXHi8LkcNkMDfKc8ToqwVxxBifB1vA4RHQR3q5"},
        "coldkey_by_hotkey": {
            HOTKEY: COLDKEY,
            "5HpvciGmspgXHi8LkcNkMDfKc8ToqwVxxBifB1vA4RHQR3q5": COLDKEY,
        },
        "registration_block_by_uid": {140: 8_875_000, 80: 8_875_100},
        "epoch_block": 8_875_512,
        "subnet_owner_coldkey": "5FsnowOwnerWalletsnowOwnerWalletsnowOwnerWalletsn",
        "subnet_owner_hotkey": "5FownerHotkeyownerHotkeyownerHotkeyownerHotkeyoQ",
    }
    call.update(overrides)
    return attribute_epoch(**call)


def test_an_attributed_epoch_goes_into_the_store_without_a_hand_written_middle(tmp_path):
    """If the caller had to rebuild the seat rows itself to store them, the
    withholding of owner-associated seats would sit in the code path it had
    just bypassed, and nothing would notice."""
    store = a_store(tmp_path)
    store.record_epoch(an_epoch(), block_timestamp=ANCHOR + 3 * DAY, block_hash="0xa")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay(COLDKEY, 3) == 300


def test_seats_the_runtime_withheld_from_never_reach_the_store(tmp_path):
    store = a_store(tmp_path)
    owner_wallet = "5FsnowOwnerWalletsnowOwnerWalletsnowOwnerWalletsn"
    attribution = an_epoch(
        coldkey_by_hotkey={
            HOTKEY: owner_wallet,
            "5HpvciGmspgXHi8LkcNkMDfKc8ToqwVxxBifB1vA4RHQR3q5": COLDKEY,
        }
    )
    store.record_epoch(attribution, block_timestamp=ANCHOR + 3 * DAY, block_hash="0xa")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay(COLDKEY, 3) == 200
    assert store.attributed_pay(owner_wallet, 3) == 0


def test_storing_the_same_epoch_twice_is_free(tmp_path):
    store = a_store(tmp_path)
    for _ in range(2):
        store.record_epoch(an_epoch(), block_timestamp=ANCHOR + 3 * DAY, block_hash="0xa")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay(COLDKEY, 3) == 300


def test_the_stored_rows_carry_the_block_the_epoch_fired_at(tmp_path):
    """The height comes from the attribution, not from the caller, so a caller
    cannot file one epoch's pay under another epoch's block."""
    store = a_store(tmp_path)
    store.record_epoch(an_epoch(), block_timestamp=ANCHOR + 3 * DAY, block_hash="0xa")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert {row["block"] for row in store.pay_observations(COLDKEY, 3)} == {8_875_512}


# ---------------------------------------------------------------------------
# Pay kept apart by seat, for #193: the door now gates one hotkey at a time
# (D-072), so a wallet with several seats needs each seat's own figure rather
# than the wallet-wide sum `attributed_pay` above still answers.
# ---------------------------------------------------------------------------


def test_attributed_pay_by_seat_matches_the_wallet_sum_for_one_seat(tmp_path):
    store = a_store(tmp_path)
    pay(store)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay_by_seat(COLDKEY, 3) == {HOTKEY: 35_551_889_238}
    assert sum(store.attributed_pay_by_seat(COLDKEY, 3).values()) == store.attributed_pay(
        COLDKEY, 3
    )


def test_two_seats_held_by_one_wallet_are_kept_apart_rather_than_summed(tmp_path):
    """The reason this method exists: the 15,417,400,074 paid to 5Hpvci must
    not be folded into HOTKEY's own figure the way `attributed_pay` folds
    both into one wallet-wide number."""
    store = a_store(tmp_path)
    pay(store, uid=140, amount_rao=35_551_889_238)
    pay(store, uid=80, amount_rao=15_417_400_074, hotkey="5Hpvci")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay_by_seat(COLDKEY, 3) == {
        HOTKEY: 35_551_889_238,
        "5Hpvci": 15_417_400_074,
    }


def test_a_seat_paid_at_two_blocks_in_one_day_is_summed_within_the_seat(tmp_path):
    store = a_store(tmp_path)
    pay(store, uid=140, block=8_875_512, amount_rao=100, block_hash="0xa")
    pay(store, uid=140, block=8_875_872, amount_rao=200, block_hash="0xb")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay_by_seat(COLDKEY, 3) == {HOTKEY: 300}


def test_a_day_that_was_scanned_with_no_pay_is_an_empty_map(tmp_path):
    store = a_store(tmp_path)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay_by_seat(COLDKEY, 3) == {}


def test_attributed_pay_by_seat_also_refuses_a_day_nobody_scanned(tmp_path):
    """Grouped from the same rows `attributed_pay` sums, so it inherits the
    same refusal rather than reading an unscanned day as empty."""
    store = a_store(tmp_path)
    with pytest.raises(DayNotScanned):
        store.attributed_pay_by_seat(COLDKEY, 3)


def test_attributed_pay_by_seat_only_sees_this_wallets_own_seats(tmp_path):
    store = a_store(tmp_path)
    pay(store, coldkey=COLDKEY, uid=140, amount_rao=100)
    pay(store, coldkey="5SomebodyElse", uid=7, amount_rao=999, hotkey="5Theirs")
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay_by_seat(COLDKEY, 3) == {HOTKEY: 100}


# ---- #193 codex review Finding 4: a uid is not a stable identity -----------
#
# The runtime reassigns a vacated uid to a different hotkey. A map keyed by
# uid would silently move an earlier day's group to whoever holds that uid
# now, the moment such a reassignment happens between that day and the one
# being read. Grouped by hotkey instead, because hotkey is the identity an
# `attributed_pay` row carries that a uid reassignment does not change.


def test_attributed_pay_by_seat_is_keyed_by_hotkey_across_a_uid_reassignment(tmp_path):
    """HOTKEY holds uid 5 in day 2, then a different uid in day 3, the same
    ordinary reassignment #193 codex review Finding 4 describes: a slot
    vacated and re-registered under a new number. Grouping by uid would file
    day 2's pay under key 5 -- which by day 3 may belong to a different
    hotkey entirely -- rather than under the hotkey that actually earned it.
    """
    store = a_store(tmp_path)
    pay(
        store,
        uid=5,
        hotkey=HOTKEY,
        amount_rao=111,
        block_timestamp=ANCHOR + 2 * DAY,
        block=8_900_005,
    )
    pay(
        store,
        uid=9,
        hotkey=HOTKEY,
        amount_rao=222,
        block_timestamp=ANCHOR + 3 * DAY,
        block=8_900_009,
    )
    store.mark_day_scanned(2, from_block=1, to_block=2)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.attributed_pay_by_seat(COLDKEY, 2) == {HOTKEY: 111}
    assert store.attributed_pay_by_seat(COLDKEY, 3) == {HOTKEY: 222}


# ---------------------------------------------------------------------------
# `first_spendable_pay_day_for_hotkey` -- #112's "starting treatment" data
# source: the earliest day THIS hotkey seat ever had a real, nonzero mining
# payment. Unlike `attributed_pay`/`attributed_pay_by_seat`, this is a
# whole-history query (never scoped to one already-scanned day), because the
# question it answers -- "has this seat EVER been paid, and if so since
# when" -- has to be answerable the same way regardless of which day is
# being evaluated right now.
# ---------------------------------------------------------------------------


def test_first_spendable_pay_day_for_hotkey_is_none_when_never_paid(tmp_path):
    store = a_store(tmp_path)
    assert store.first_spendable_pay_day_for_hotkey(COLDKEY, HOTKEY) is None


def test_first_spendable_pay_day_for_hotkey_is_the_earliest_nonzero_day(tmp_path):
    store = a_store(tmp_path)
    pay(store, block_timestamp=ANCHOR + 5 * DAY, block=8_900_005, amount_rao=100)
    pay(store, block_timestamp=ANCHOR + 2 * DAY, block=8_900_002, amount_rao=200)
    pay(store, block_timestamp=ANCHOR + 8 * DAY, block=8_900_008, amount_rao=300)
    assert store.first_spendable_pay_day_for_hotkey(COLDKEY, HOTKEY) == 2


def test_first_spendable_pay_day_for_hotkey_ignores_a_zero_amount_payment(tmp_path):
    # A recorded zero-amount observation is not a real payment: it must not
    # count as this seat's first spendable pay day.
    store = a_store(tmp_path)
    pay(store, block_timestamp=ANCHOR + 1 * DAY, block=8_900_001, amount_rao=0)
    pay(store, block_timestamp=ANCHOR + 4 * DAY, block=8_900_004, amount_rao=50)
    assert store.first_spendable_pay_day_for_hotkey(COLDKEY, HOTKEY) == 4


def test_first_spendable_pay_day_for_hotkey_survives_a_cold_restart(tmp_path):
    """Required proof #6: this is a pure query against persisted chain
    history, never in-memory/session state. Closing the store and reopening
    the SAME database file from scratch (a validator restart, with nothing
    cached) must re-derive the identical historical day, asked at a day far
    LATER than when the real transition actually happened."""
    path = tmp_path / "restart.db"
    live = Store.create(path, day_anchor_ts=ANCHOR)
    pay(live, block_timestamp=ANCHOR + 3 * DAY, block=8_900_003, amount_rao=999)
    live_answer = live.first_spendable_pay_day_for_hotkey(COLDKEY, HOTKEY)
    assert live_answer == 3
    live._conn.close()

    # A fresh Store instance, opened later, with no cached state from `live`
    # at all -- simulating a cold validator restart well after the fact.
    restarted = Store.open(path, day_anchor_ts=ANCHOR)
    restarted_answer = restarted.first_spendable_pay_day_for_hotkey(COLDKEY, HOTKEY)
    assert restarted_answer == live_answer == 3


def test_first_spendable_pay_day_for_hotkey_survives_reregistration(tmp_path):
    """Required proof #7: the same hotkey re-registering under a new uid
    (uid vacated and reassigned, or the hotkey re-registering itself) must
    not reset or hide this seat's real payment history -- it is keyed by
    hotkey, not uid, the same design rationale `attributed_pay_by_seat`
    already establishes."""
    store = a_store(tmp_path)
    # First registration: uid 5, paid for real on day 3.
    pay(store, uid=5, block_timestamp=ANCHOR + 3 * DAY, block=8_900_003, amount_rao=500)
    assert store.first_spendable_pay_day_for_hotkey(COLDKEY, HOTKEY) == 3

    # The same hotkey re-registers under a different uid later (a vacated
    # slot reassigned, or a fresh registration event for the same hotkey).
    # No NEW pay is recorded here on purpose: the point under test is that
    # the OLD real payment is neither erased nor superseded by the event.
    pay(store, uid=41, block_timestamp=ANCHOR + 9 * DAY, block=8_900_009, amount_rao=0)
    assert store.first_spendable_pay_day_for_hotkey(COLDKEY, HOTKEY) == 3


def test_first_spendable_pay_day_for_hotkey_is_independent_per_seat_on_a_shared_wallet(
    tmp_path,
):
    """Required proof #8: one coldkey with two hotkey seats -- one already
    paid, one never paid -- must compute each seat's own answer with zero
    leakage in either direction."""
    store = a_store(tmp_path)
    paid_hotkey = HOTKEY
    unpaid_hotkey = "5UnpaidSiblingSeatAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    pay(
        store,
        hotkey=paid_hotkey,
        uid=140,
        block_timestamp=ANCHOR + 6 * DAY,
        block=8_900_006,
        amount_rao=1_000,
    )
    assert store.first_spendable_pay_day_for_hotkey(COLDKEY, paid_hotkey) == 6
    # The sibling seat, same wallet, never paid: independent, not None
    # because of the wallet's OTHER seat, and not accidentally seeing the
    # paid seat's day either.
    assert store.first_spendable_pay_day_for_hotkey(COLDKEY, unpaid_hotkey) is None


# ---- pay_summary: the bulk, day-scoped answer for every seat at once --------
#
# Mirrors test_deposits.py's own `deposit_summary` tests: bulk correctness
# across multiple wallets/seats, honest zero vs. not-yet-scanned, and
# `pairs` filtering. #12 group 3/4/6.

OWNER_COLDKEY = "5OwnerValidatorSharedIdentityAAAAAAAAAAAAAAAAA"
MINER_A_COLD = "5MinerAColdAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
MINER_B_COLD = "5MinerBColdAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
MINER_A_HOT = "5MinerAHotAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
MINER_B_HOT = "5MinerBHotAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def test_pay_summary_groups_every_seat_paid_that_day(tmp_path):
    store = a_store(tmp_path)
    pay(store, coldkey=OWNER_COLDKEY, hotkey=OWNER_COLDKEY, uid=0, amount_rao=111)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, amount_rao=222)
    pay(store, coldkey=MINER_B_COLD, hotkey=MINER_B_HOT, uid=9, amount_rao=333)
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = {(row["coldkey"], row["hotkey"]): row for row in store.pay_summary(day=3)}

    assert len(summary) == 3
    assert summary[(OWNER_COLDKEY, OWNER_COLDKEY)]["amount_rao"] == 111
    assert summary[(OWNER_COLDKEY, OWNER_COLDKEY)]["uid"] == 0
    assert summary[(MINER_A_COLD, MINER_A_HOT)]["amount_rao"] == 222
    assert summary[(MINER_A_COLD, MINER_A_HOT)]["uid"] == 7
    assert summary[(MINER_B_COLD, MINER_B_HOT)]["amount_rao"] == 333
    assert all(row["day"] == 3 for row in summary.values())


def test_pay_summary_sums_a_seat_paid_at_two_blocks_in_one_day(tmp_path):
    store = a_store(tmp_path)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, block=8_800_001, amount_rao=100)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, block=8_800_002, amount_rao=50)
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = {(row["coldkey"], row["hotkey"]): row for row in store.pay_summary(day=3)}

    assert summary[(MINER_A_COLD, MINER_A_HOT)]["amount_rao"] == 150


def test_pay_summary_uid_is_the_latest_block_within_the_day(tmp_path):
    """A seat paid at two blocks in the same day where its uid moved
    underneath it (an ordinary reassignment, not an edge case) reports the
    LATER block's uid, never the earlier one and never whichever SQLite
    happens to return first."""
    store = a_store(tmp_path)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, block=8_800_001, amount_rao=100)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=41, block=8_800_002, amount_rao=50)
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = {(row["coldkey"], row["hotkey"]): row for row in store.pay_summary(day=3)}

    assert summary[(MINER_A_COLD, MINER_A_HOT)]["uid"] == 41
    assert summary[(MINER_A_COLD, MINER_A_HOT)]["amount_rao"] == 150


def test_pay_summary_defaults_to_the_latest_scanned_day(tmp_path):
    store = a_store(tmp_path)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, block_timestamp=ANCHOR + 3 * DAY, amount_rao=100)
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = store.pay_summary()

    assert len(summary) == 1
    assert summary[0]["day"] == 3
    assert summary[0]["amount_rao"] == 100


def test_pay_summary_with_no_day_ever_scanned_refuses(tmp_path):
    store = a_store(tmp_path)
    with pytest.raises(DayNotScanned):
        store.pay_summary()


def test_pay_summary_for_an_explicit_day_nobody_scanned_refuses(tmp_path):
    store = a_store(tmp_path)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    with pytest.raises(DayNotScanned):
        store.pay_summary(day=9)


def test_pay_summary_with_explicit_pairs_returns_only_those_pairs_with_honest_zeros(tmp_path):
    store = a_store(tmp_path)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, amount_rao=222)
    pay(store, coldkey=MINER_B_COLD, hotkey=MINER_B_HOT, uid=9, amount_rao=333)
    store.mark_day_scanned(3, from_block=1, to_block=2)

    unpaid_hotkey = "5UnpaidSeatAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    summary = store.pay_summary(day=3, pairs=[(MINER_A_COLD, MINER_A_HOT), (MINER_A_COLD, unpaid_hotkey)])

    assert summary == [
        {"coldkey": MINER_A_COLD, "hotkey": MINER_A_HOT, "uid": 7, "amount_rao": 222, "day": 3},
        {"coldkey": MINER_A_COLD, "hotkey": unpaid_hotkey, "uid": None, "amount_rao": 0, "day": 3},
    ]


def test_pay_summary_a_day_scanned_with_no_pay_is_an_empty_list(tmp_path):
    store = a_store(tmp_path)
    store.mark_day_scanned(3, from_block=1, to_block=2)
    assert store.pay_summary(day=3) == []


# ---- locked_pay_summary: the bulk, day-scoped collateral-lock answer -------


def test_locked_pay_summary_reports_a_real_read_and_an_untouched_lock(tmp_path):
    store = a_store(tmp_path)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, block_timestamp=ANCHOR + 3 * DAY, amount_rao=100)
    store.record_locked_pay(
        coldkey=MINER_A_COLD,
        block=8_800_003,
        block_timestamp=ANCHOR + 3 * DAY,
        locked_arrival_rao=60,
        unlocked_rao=0,
        block_hash="0xa",
    )
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = {row["coldkey"]: row for row in store.locked_pay_summary(day=3)}

    assert summary[MINER_A_COLD] == {
        "coldkey": MINER_A_COLD,
        "day": 3,
        "status": "read",
        "locked_arrival_rao": 60,
        "unlocked_rao": 0,
        "reason": None,
    }


def test_locked_pay_summary_reports_unreadable_with_its_own_reason(tmp_path):
    store = a_store(tmp_path)
    store.mark_locked_pay_unreadable(
        coldkey=MINER_A_COLD,
        block=8_800_003,
        block_timestamp=ANCHOR + 3 * DAY,
        reason="the collateral map could not be read at this block",
    )
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = {row["coldkey"]: row for row in store.locked_pay_summary(day=3)}

    assert summary[MINER_A_COLD]["status"] == "unreadable"
    assert summary[MINER_A_COLD]["locked_arrival_rao"] is None
    assert summary[MINER_A_COLD]["unlocked_rao"] is None
    assert summary[MINER_A_COLD]["reason"] == "the collateral map could not be read at this block"


def test_locked_pay_summary_reports_not_read_for_a_wallet_paid_but_never_read(tmp_path):
    """The exact gap `locked_pay()` raises `LockedPayNotRead` for on a
    single wallet, reproduced here as an honest status instead of an
    exception -- one missing wallet must not fail the whole bulk read."""
    store = a_store(tmp_path)
    pay(store, coldkey=MINER_A_COLD, hotkey=MINER_A_HOT, uid=7, block_timestamp=ANCHOR + 3 * DAY, amount_rao=100)
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = {row["coldkey"]: row for row in store.locked_pay_summary(day=3)}

    assert summary[MINER_A_COLD]["status"] == "not_read"
    assert summary[MINER_A_COLD]["locked_arrival_rao"] is None
    assert summary[MINER_A_COLD]["unlocked_rao"] is None


def test_locked_pay_summary_real_zero_for_a_wallet_paid_nothing(tmp_path):
    """A wallet with no attributed_pay this day and no locked_pay row is a
    real, trustworthy (0, 0) -- settle returns before touching anything on
    a zero emission, so nothing could have moved."""
    store = a_store(tmp_path)
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = store.locked_pay_summary(day=3, coldkeys=[MINER_A_COLD])

    assert summary == [
        {
            "coldkey": MINER_A_COLD,
            "day": 3,
            "status": "read",
            "locked_arrival_rao": 0,
            "unlocked_rao": 0,
            "reason": None,
        }
    ]


def test_locked_pay_summary_with_no_day_ever_scanned_refuses(tmp_path):
    store = a_store(tmp_path)
    with pytest.raises(DayNotScanned):
        store.locked_pay_summary()


def test_locked_pay_summary_defaults_to_the_latest_scanned_day(tmp_path):
    store = a_store(tmp_path)
    store.record_locked_pay(
        coldkey=MINER_A_COLD,
        block=8_800_003,
        block_timestamp=ANCHOR + 3 * DAY,
        locked_arrival_rao=60,
        unlocked_rao=0,
        block_hash="0xa",
    )
    store.mark_day_scanned(3, from_block=1, to_block=2)

    summary = store.locked_pay_summary()

    assert len(summary) == 1
    assert summary[0]["day"] == 3
    assert summary[0]["locked_arrival_rao"] == 60
