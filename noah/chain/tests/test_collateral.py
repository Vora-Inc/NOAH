"""What makes an answer about a collateral lock worth having.

Every check here exists because the obvious version of this read returns a
confident zero in a case where zero is false, and a zero from a collateral map
is the difference between a miner being paid and being held out.

The witness scheme is NOAH-MINER-TOOL's, arrived at by running the naive
version against a fresh subnet and watching it refuse every miner. Three checks,
each covering what the others cannot: arity from the chain's own metadata says
the question is well formed even when the map is empty; the aggregate
cross-check says the keys are in the right order whenever anybody holds
collateral; a control row, where one exists, remains the strongest single
signal.
"""

import pytest

from noah.chain.collateral import (
    LockDesynced,
    NotACollateralEntry,
    QuestionNotWellFormed,
    SettleEffect,
    lock_total_rao,
    locked_of,
    settle_effect,
    well_formed,
)

CK = "5Ark" + "c" * 44
HK_A, HK_B = "5Hot" + "a" * 44, "5Hot" + "b" * 44


# ---- the arity witness, which is the one that works on an empty map ---------


def test_a_question_with_the_declared_number_of_keys_is_well_formed():
    assert well_formed("ColdkeyMinerCollateral", 8_800_000, keys_given=2, keys_declared=2)


def test_too_few_keys_is_refused_and_it_is_the_dangerous_one():
    """Measured live on 2026-08-20: too few keys is ACCEPTED and answers 'not
    there' without complaining. So the broken question and the real absence are
    the same bytes, and only the arity check can tell them apart."""
    with pytest.raises(QuestionNotWellFormed) as refused:
        well_formed("MinerCollateral", 8_800_000, keys_given=2, keys_declared=3)
    assert "2" in str(refused.value) and "3" in str(refused.value)


def test_too_many_keys_is_refused_too():
    """This one raises at the node rather than answering, but a question that
    reached this check with the wrong count was still built wrong."""
    with pytest.raises(QuestionNotWellFormed):
        well_formed("MinerCollateral", 8_800_000, keys_given=4, keys_declared=3)


def test_the_witness_works_with_nothing_in_the_map():
    """The whole point, and the bug it replaces.

    A control row cannot be offered by an empty map, and a subnet where nobody
    holds collateral has an empty map. The naive guard refuses hardest exactly
    when everything is fine, which on Noah's own subnet is day one.
    """
    assert well_formed("ColdkeyMinerCollateral", 8_800_000, keys_given=2, keys_declared=2)


# ---- the cross-check, which is what catches keys in the wrong order ---------


def test_the_aggregate_agreeing_with_the_positions_gives_the_total():
    total = lock_total_rao(
        coldkey=CK, block=8_800_000, aggregate_rao=900, positions={HK_A: 400, HK_B: 500}
    )
    assert total == 900


def test_an_empty_wallet_agrees_with_an_empty_sum():
    assert lock_total_rao(coldkey=CK, block=8_800_000, aggregate_rao=0, positions={}) == 0


def test_the_aggregate_disagreeing_with_the_positions_is_refused():
    """Two maps, different key shapes, one runtime. A read that swapped the
    hotkey and coldkey is well formed, has the right arity, and returns absent,
    so neither the arity check nor a control row would catch it. This does."""
    with pytest.raises(LockDesynced) as refused:
        lock_total_rao(coldkey=CK, block=8_800_000, aggregate_rao=900, positions={HK_A: 400})
    assert "900" in str(refused.value) and "400" in str(refused.value)


def test_the_refusal_prints_both_readings_because_a_mismatch_has_two_causes():
    """Not only swapped keys. The runtime maintains the aggregate incrementally
    rather than recomputing it, with a saturating subtraction, so a mismatch can
    also be the chain's own bookkeeping. Chasing the wrong one wastes a day."""
    with pytest.raises(LockDesynced) as refused:
        lock_total_rao(coldkey=CK, block=8_800_000, aggregate_rao=0, positions={HK_A: 7})
    message = str(refused.value)
    assert HK_A in message, "the per-position reading has to be shown, not summarised"
    assert "bookkeeping" in message
    assert "DOWNWARD" in message, (
        "the saturating subtraction can floor a desync at zero, which understates the lock. "
        "A caller using this as a ceiling then permits more than the chain will accept, so "
        "the direction of the error has to be in the message and not only its existence"
    )


# ---- what the epoch's settle did, separated from what a miner did -----------


def test_a_lock_that_grew_across_an_epoch_captured_pay():
    effect = settle_effect(before_rao=100, after_rao=180, announced_rao=0)
    assert effect == SettleEffect(paid_locked_arrival_rao=80, pay_unlocked_rao=0)


def test_a_lock_that_shrank_across_an_epoch_released_pay():
    effect = settle_effect(before_rao=180, after_rao=100, announced_rao=0)
    assert effect == SettleEffect(paid_locked_arrival_rao=0, pay_unlocked_rao=80)


def test_a_lock_that_did_not_move_did_neither():
    assert settle_effect(before_rao=100, after_rao=100, announced_rao=0) == SettleEffect(0, 0)


def test_a_miner_topping_up_in_the_same_block_is_not_captured_pay():
    """The one thing that could confound a state delta.

    There are exactly two collateral extrinsics and both emit events, and there
    is no manual removal at all, so what a miner did announces itself and is
    subtracted. Without this, a top-up of eighty reads as eighty of pay
    captured, and the miner is held out of a day they earned normally.
    """
    effect = settle_effect(before_rao=100, after_rao=180, announced_rao=80)
    assert effect == SettleEffect(0, 0)


def test_a_top_up_and_a_capture_in_one_block_separate():
    effect = settle_effect(before_rao=100, after_rao=250, announced_rao=80)
    assert effect == SettleEffect(paid_locked_arrival_rao=70, pay_unlocked_rao=0)


def test_a_top_up_larger_than_the_movement_means_pay_was_released():
    """A miner adds eighty while the drain releases thirty: the lock rises by
    fifty and pay unlocked all the same."""
    effect = settle_effect(before_rao=100, after_rao=150, announced_rao=80)
    assert effect == SettleEffect(paid_locked_arrival_rao=0, pay_unlocked_rao=30)


# ---- what the store will and will not answer --------------------------------

import pytest as _pytest  # noqa: E402

from noah.chain.store import LockedPayNotRead, NotAnAmount, Store  # noqa: E402

ANCHOR = 1_786_320_000
DAY = 86_400


def a_store(tmp_path):
    return Store.create(tmp_path / "noah.db", day_anchor_ts=ANCHOR)


def was_paid(store, coldkey, day, block=8_800_000):
    """A day this wallet actually earned in.

    Needed by any test about a lock reading being absent, because a day with no
    pay has nothing to read and correctly answers zero. The refusal is for a
    day that DID pay and was never read.
    """
    store.mark_day_scanned(day, from_block=block - 10, to_block=block + 10)
    store.record_attributed_pay(
        coldkey=coldkey,
        hotkey=HK_A,
        uid=3,
        block_timestamp=ANCHOR + day * DAY + 50,
        amount_rao=1_000,
        block=block,
        block_hash="0xp",
    )


def test_a_lock_reading_comes_back_as_it_went_in(tmp_path):
    store = a_store(tmp_path)
    store.record_locked_pay(
        coldkey=CK,
        block=8_800_000,
        block_timestamp=ANCHOR + 100,
        locked_arrival_rao=700,
        unlocked_rao=0,
        block_hash="0xa",
    )
    assert store.locked_pay(CK, 0) == (700, 0)


def test_two_epochs_in_one_day_add_up(tmp_path):
    store = a_store(tmp_path)
    for block, locked in ((8_800_000, 700), (8_800_360, 300)):
        store.record_locked_pay(
            coldkey=CK,
            block=block,
            block_timestamp=ANCHOR + 100,
            locked_arrival_rao=locked,
            unlocked_rao=0,
            block_hash="0xa",
        )
    assert store.locked_pay(CK, 0) == (1000, 0)


def test_a_wallet_looked_at_with_nothing_moving_is_recorded_as_zero(tmp_path):
    """The row that separates 'nothing happened' from 'nobody looked'."""
    store = a_store(tmp_path)
    store.record_locked_pay(
        coldkey=CK,
        block=8_800_000,
        block_timestamp=ANCHOR + 100,
        locked_arrival_rao=0,
        unlocked_rao=0,
        block_hash="0xa",
    )
    assert store.locked_pay(CK, 0) == (0, 0)
    assert store.locked_pay_was_read(CK, 0)


def test_a_wallet_never_looked_at_refuses_rather_than_answering_zero(tmp_path):
    """The whole point of the row above.

    Answering zero here is precisely how a wallet with locked pay and a wallet
    nobody read became the same number in the emitted fixture.
    """
    store = a_store(tmp_path)
    was_paid(store, CK, 0)
    with _pytest.raises(LockedPayNotRead):
        store.locked_pay(CK, 0)
    assert not store.locked_pay_was_read(CK, 0)


def test_capture_and_release_in_one_row_is_refused(tmp_path):
    """The runtime's branches are exclusive, so a row with both netted two
    hotkeys against each other and lost both figures."""
    store = a_store(tmp_path)
    with _pytest.raises(NotAnAmount):
        store.record_locked_pay(
            coldkey=CK,
            block=8_800_000,
            block_timestamp=ANCHOR + 100,
            locked_arrival_rao=700,
            unlocked_rao=300,
            block_hash="0xa",
        )


def test_a_day_with_no_locked_arrival_anywhere_says_so(tmp_path):
    """The companion to the cumulative-unlock rule.

    That rule passes on any population where every figure is zero, which is
    every population this reader produced before the collateral read existed.
    A run reporting it satisfied has to be able to say whether it was tested.
    """
    store = a_store(tmp_path)
    store.record_locked_pay(
        coldkey=CK,
        block=8_800_000,
        block_timestamp=ANCHOR + 100,
        locked_arrival_rao=0,
        unlocked_rao=0,
        block_hash="0xa",
    )
    assert not store.any_locked_arrival()


def test_a_population_with_a_locked_arrival_says_so_too(tmp_path):
    store = a_store(tmp_path)
    store.record_locked_pay(
        coldkey=CK,
        block=8_800_000,
        block_timestamp=ANCHOR + 100,
        locked_arrival_rao=5,
        unlocked_rao=0,
        block_hash="0xa",
    )
    assert store.any_locked_arrival()


def test_readings_land_in_the_day_their_block_time_falls_in(tmp_path):
    store = a_store(tmp_path)
    store.record_locked_pay(
        coldkey=CK,
        block=8_800_000,
        block_timestamp=ANCHOR + DAY + 100,
        locked_arrival_rao=42,
        unlocked_rao=0,
        block_hash="0xa",
    )
    assert store.locked_pay(CK, 1) == (42, 0)
    was_paid(store, CK, 0, block=8_799_000)
    with _pytest.raises(LockedPayNotRead):
        store.locked_pay(CK, 0)


def test_the_database_refuses_the_impossible_row_even_written_directly(tmp_path):
    """The backstop under the Python guard, tested past it.

    Found by sweeping: with the table's CHECK turned into CHECK(1) the suite
    stayed green, because `record_locked_pay` refuses first and no test ever
    reached the database with a bad row. That is a constraint nobody had
    established was doing anything, and its whole value is for a write that does
    not come through that method — a later direct insert, a migration, a repair
    script. So this one goes around the method on purpose.
    """
    store = a_store(tmp_path)
    with _pytest.raises(Exception) as refused:
        with store._conn:
            store._conn.execute(
                "INSERT INTO locked_pay "
                "(coldkey, block, day, locked_arrival_rao, unlocked_rao, block_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (CK, 8_800_000, 0, 700, 300, "0xa"),
            )
    assert "CHECK constraint failed" in str(refused.value)


# ---- the struct, and the field most likely to be mistaken for a balance -----


def test_the_locked_field_is_what_a_position_holds():
    entry = {"locked": 500, "drain_ratio": 1 << 63, "min_locked": 900, "earned": 4_000}
    assert locked_of(entry, hotkey=HK_A, block=8_800_000) == 500


def test_an_absent_position_holds_nothing():
    """MinerCollateral is OptionQuery, so absence is honest and stays None."""
    assert locked_of(None, hotkey=HK_A, block=8_800_000) == 0


def test_a_bare_number_where_the_struct_belongs_is_refused():
    """The map does not hold a balance. It holds a four-field struct, and a
    reader that treats it as a number has picked one of the four by accident."""
    with pytest.raises(NotACollateralEntry) as refused:
        locked_of(500, hotkey=HK_A, block=8_800_000)
    assert "four" in str(refused.value) or "struct" in str(refused.value)


def test_a_struct_missing_the_locked_field_is_refused():
    with pytest.raises(NotACollateralEntry):
        locked_of({"min_locked": 900, "earned": 4_000}, hotkey=HK_A, block=8_800_000)


def test_min_locked_is_never_what_is_locked():
    """The trap named on its own, because it is the plausible mistake.

    min_locked is a floor on FUTURE drain, not a balance now: the drain never
    releases below it. A reader summing it overstates what is locked today, and
    the two fields sit next to each other in the same struct.
    """
    entry = {"locked": 100, "drain_ratio": 0, "min_locked": 900, "earned": 0}
    assert locked_of(entry, hotkey=HK_A, block=8_800_000) == 100


def test_the_drain_ratio_is_never_what_is_locked():
    """Fixed point, so it would land in a rao field wrong by eighteen
    quintillion rather than wrong by a little."""
    entry = {"locked": 7, "drain_ratio": 1 << 64, "min_locked": 0, "earned": 0}
    assert locked_of(entry, hotkey=HK_A, block=8_800_000) == 7
