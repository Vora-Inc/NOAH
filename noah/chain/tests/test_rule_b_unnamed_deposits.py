"""Rule B (Drew's decision #338) end to end through the real store, the real
``build_fixture`` and the entry gate: a deposit that names no miner hotkey
credits nobody, single-seat wallets included, and is listed as unattributed.

The deposits are produced by the real reader (``deposits_in_block``) from the
same hand-built call trees ``test_deposits_unnamed_reason.py`` pins, then
recorded exactly as ``run_day`` records them. Nothing here is a recorded real
wrapped Ark deposit (none exists).
"""

import pytest

from noah.chain.fixture_build import build_fixture
from noah.chain.store import (
    LEGACY_UNNAMED_REASON,
    UNNAMED_REASON_PREFIX,
    ObservationChanged,
    Store,
)
from noah.chain.tests.test_deposits_unnamed_reason import (
    ALPHA,
    GIVER,
    MINER,
    NAMED,
    NAMED_WITH_REMARK,
    PLAIN,
    PLAIN_WITH_REMARK,
    VALIDATOR,
    WRONG_VALIDATOR,
    batch_all,
    proxy,
    remark,
    run,
    transfer_stake,
    transfer_stake_and_hotkey,
    wrapped,
)
from noah.chain.tests._scenario_helpers import DIALS, ENTRY, PAY
from noah.chain.tests._fixture_helpers import (
    ANCHOR,
    DAY,
    held_many,
    holdings_sampled,
    paid,
)
from noah.deposits.entry_gate import EntryDepositNotConfirmed, confirm_entry_before_score

M2 = "5FotherHotkeyotherHotkeyotherHotkeyotherHotkeyoQ"
DEPOSIT_DAY = 2
LAST_DAY = 3

IDENTIFIED = [
    pytest.param(NAMED, transfer_stake_and_hotkey(), id="direct transfer_stake_and_hotkey"),
    pytest.param(
        PLAIN_WITH_REMARK, batch_all(transfer_stake(), remark()), id="batch_all[transfer, remark]"
    ),
    pytest.param(NAMED, proxy(transfer_stake_and_hotkey()), id="Proxy(transfer_and_hotkey)"),
    pytest.param(
        NAMED_WITH_REMARK,
        proxy(batch_all(transfer_stake_and_hotkey(), remark())),
        id="Proxy(batch_all[transfer_and_hotkey, remark])",
    ),
]
UNIDENTIFIED = [
    pytest.param(PLAIN, transfer_stake(), id="plain transfer_stake"),
    pytest.param(PLAIN, batch_all(transfer_stake()), id="batch_all without remark"),
    pytest.param(PLAIN, proxy(transfer_stake()), id="Proxy(plain transfer_stake)"),
    pytest.param(
        PLAIN_WITH_REMARK, proxy(batch_all(transfer_stake(), remark())), id="Proxy(batch_all)"
    ),
    pytest.param(
        PLAIN_WITH_REMARK,
        wrapped("Utility", "batch", transfer_stake(), remark()),
        id="Utility.batch",
    ),
    pytest.param(
        PLAIN_WITH_REMARK,
        wrapped("Utility", "force_batch", transfer_stake(), remark()),
        id="Utility.force_batch",
    ),
    pytest.param(PLAIN_WITH_REMARK, wrapped("Multisig", "as_multi", batch_all()), id="Multisig"),
]


def a_store(tmp_path):
    store = Store.create(tmp_path / "noah.db", day_anchor_ts=ANCHOR)
    store.record_emission_start(
        block=8_000_000, block_timestamp=ANCHOR + DAY + 10, block_hash="0xstart"
    )
    return store


def record(store, deposit, day=DEPOSIT_DAY):
    return store.record_noah_deposit(
        coldkey=deposit.coldkey,
        block_timestamp=ANCHOR + day * DAY + 200,
        amount_rao=deposit.amount_rao,
        block=deposit.block + day,
        event_ordinal=deposit.event_ordinal,
        extrinsic_index=deposit.extrinsic_index,
        block_hash=f"{deposit.block_hash}{day}",
        carried_by=deposit.carried_by,
        hotkey=deposit.hotkey,
        owner_at_deposit=deposit.owner_at_deposit,
        origin_hotkey=deposit.origin_hotkey,
        unattributed_reason=deposit.unattributed_reason,
    )


def scanned(store, hotkeys, *, pay_hotkeys=()):
    """Days 1..LAST_DAY read, every hotkey holding something; ``pay_hotkeys``
    also earn pay every day (so the store has evidence they exist)."""
    for day in range(1, LAST_DAY + 1):
        store.mark_day_scanned(day, from_block=1, to_block=2)
        held_many(store, GIVER, day, {h: 1 for h in hotkeys})
    holdings_sampled(store, *range(1, LAST_DAY + 1))
    for uid, hotkey in enumerate(pay_hotkeys, start=140):
        for day in range(1, LAST_DAY + 1):
            paid(store, GIVER, day, PAY, uid=uid, hotkey=hotkey)


def build(store, seats):
    return build_fixture(
        store,
        name="rule-b",
        eval_day=LAST_DAY,
        day_epoch_ts=ANCHOR,
        dials=DIALS,
        seats=seats,
    )


def credited(built):
    """``{uid: [noahDepositRao per day]}`` for every row, the null row as None."""
    return {
        m["uid"]: [int(m["days"][str(d)]["noahDepositRao"]) for d in sorted(map(int, m["days"]))]
        for m in built["miners"]
    }


SINGLE = {GIVER: {MINER: 140}}
MULTI = {GIVER: {MINER: 140, M2: 141}}


# ---- identified forms stay credited ---------------------------------------


@pytest.mark.parametrize("events, top_call", IDENTIFIED)
def test_identified_single_seat_wallet_credits_its_seat(tmp_path, events, top_call):
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    (deposit,) = run(events, top_call)
    record(store, deposit)
    rows = credited(build(store, SINGLE))
    assert rows == {140: [0, ALPHA, 0]}


@pytest.mark.parametrize("events, top_call", IDENTIFIED)
def test_identified_multi_seat_wallet_credits_only_the_named_seat(tmp_path, events, top_call):
    store = a_store(tmp_path)
    scanned(store, [MINER, M2], pay_hotkeys=[MINER, M2])
    (deposit,) = run(events, top_call)
    record(store, deposit)
    rows = credited(build(store, MULTI))
    assert rows == {140: [0, ALPHA, 0], 141: [0, 0, 0]}


# ---- unidentified forms credit nobody -------------------------------------


@pytest.mark.parametrize("events, top_call", UNIDENTIFIED)
def test_unidentified_single_seat_wallet_credits_nobody(tmp_path, events, top_call):
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    (deposit,) = run(events, top_call)
    assert deposit.unattributed_reason.startswith("No miner hotkey named:")
    record(store, deposit)
    assert credited(build(store, SINGLE)) == {140: [0, 0, 0]}
    assert store.noah_deposits(GIVER, DEPOSIT_DAY) == 0  # recorded, not qualifying


@pytest.mark.parametrize("events, top_call", UNIDENTIFIED)
def test_unidentified_multi_seat_wallet_credits_nobody(tmp_path, events, top_call):
    store = a_store(tmp_path)
    scanned(store, [MINER, M2], pay_hotkeys=[MINER, M2])
    (deposit,) = run(events, top_call)
    record(store, deposit)
    assert credited(build(store, MULTI)) == {140: [0, 0, 0], 141: [0, 0, 0]}


@pytest.mark.parametrize("events, top_call", UNIDENTIFIED)
def test_unidentified_deposit_with_no_evidence_of_any_seat_is_not_an_unaccounted_deposit(
    tmp_path, events, top_call
):
    """Two live seats, no pay and no registration day anywhere: the empty-
    evidence refusal (UnaccountedDeposit) must not fire for a deposit that
    credits nobody by rule. The unnamed deposit is also no first activity, so
    the wallet has no rows at all (before this rule it would have had rows,
    and the refusal would have been the only thing standing between it and a
    silent drop)."""
    store = a_store(tmp_path)
    scanned(store, [MINER, M2])
    (deposit,) = run(events, top_call)
    record(store, deposit)
    assert credited(build(store, MULTI)) == {}


def test_an_unidentified_deposit_on_a_vacated_only_hotkey_wallet_credits_no_null_row(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    (deposit,) = run(PLAIN, transfer_stake())
    record(store, deposit)
    rows = credited(build(store, {GIVER: {}}))
    assert rows == {None: [0, 0, 0]}


def test_a_named_deposit_on_a_vacated_only_hotkey_is_parked_on_the_null_row(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    (deposit,) = run(NAMED, transfer_stake_and_hotkey())
    record(store, deposit)
    assert credited(build(store, {GIVER: {}})) == {None: [0, ALPHA, 0]}


def test_a_legacy_row_with_no_hotkey_and_no_reason_credits_nobody(tmp_path):
    """A row stored before rule B, with neither an origin hotkey nor a reason
    (and a staking hotkey that is nobody's seat), is read per hotkey: nobody."""
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    store.record_noah_deposit(
        coldkey=GIVER,
        block_timestamp=ANCHOR + DEPOSIT_DAY * DAY + 200,
        amount_rao=ALPHA,
        block=8_800_200,
        event_ordinal=1,
        extrinsic_index=3,
        block_hash="0xlegacy",
        carried_by="StakeTransferred",
        hotkey=VALIDATOR,
    )
    assert store.noah_deposits(GIVER, DEPOSIT_DAY) == ALPHA  # the wallet-wide read still sees it
    assert credited(build(store, SINGLE)) == {140: [0, 0, 0]}
    assert credited(build(store, {GIVER: {}})) == {None: [0, 0, 0]}


# ---- entry ----------------------------------------------------------------


def test_an_unnamed_plain_transfer_does_not_satisfy_the_entry_gate(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    (deposit,) = run(PLAIN, transfer_stake())
    assert deposit.amount_rao == ALPHA
    # Re-record at the entry figure so only the naming differs from a real entry.
    store.record_noah_deposit(
        coldkey=GIVER,
        block_timestamp=ANCHOR + DEPOSIT_DAY * DAY + 200,
        amount_rao=ENTRY,
        block=8_800_200,
        event_ordinal=1,
        extrinsic_index=1,
        block_hash="0xe1",
        carried_by="StakeTransferred",
        hotkey=VALIDATOR,
        unattributed_reason=deposit.unattributed_reason,
    )
    with pytest.raises(EntryDepositNotConfirmed):
        confirm_entry_before_score(store, GIVER, MINER, as_of_day=LAST_DAY)
    assert store.cumulative_noah_deposits(GIVER, LAST_DAY) == 0
    assert credited(build(store, SINGLE)) == {140: [0, 0, 0]}


def test_a_named_deposit_at_the_entry_figure_does_satisfy_the_entry_gate(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    store.record_noah_deposit(
        coldkey=GIVER,
        block_timestamp=ANCHOR + DEPOSIT_DAY * DAY + 200,
        amount_rao=ENTRY,
        block=8_800_200,
        event_ordinal=1,
        extrinsic_index=1,
        block_hash="0xe1",
        carried_by="StakeAndHotkeyTransferred",
        hotkey=VALIDATOR,
        origin_hotkey=MINER,
        owner_at_deposit=GIVER,
    )
    assert confirm_entry_before_score(store, GIVER, MINER, as_of_day=LAST_DAY) == ENTRY


# ---- the store ------------------------------------------------------------


def test_unattributed_deposits_lists_the_reasoned_rows_with_their_facts(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    (plain,) = run(PLAIN, transfer_stake())
    (named,) = run(NAMED, transfer_stake_and_hotkey())
    (wrong,) = run(NAMED, transfer_stake_and_hotkey(), designated=WRONG_VALIDATOR)
    record(store, plain, day=2)
    record(store, wrong, day=3)
    named_row = record(store, named, day=1)
    assert named_row == 1
    listed = store.unattributed_deposits()
    assert [(d["day"], d["origin_hotkey"]) for d in listed] == [(2, None), (3, MINER)]
    first = listed[0]
    assert first["coldkey"] == GIVER
    assert first["amount_rao"] == ALPHA
    assert first["carried_by"] == "StakeTransferred"
    assert first["hotkey"] == VALIDATOR
    assert first["extrinsic_index"] == plain.extrinsic_index
    assert first["block"] == plain.block + 2
    assert first["reason"] == plain.unattributed_reason
    assert [d["day"] for d in store.unattributed_deposits(day=3)] == [3]
    assert [d["day"] for d in store.unattributed_deposits(through_day=2)] == [2]
    assert store.unattributed_deposits(coldkey="5SomebodyElse") == []
    assert len(store.unattributed_deposits(coldkey=GIVER)) == 2


def test_an_unnamed_deposit_is_not_a_first_activity_day(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    (plain,) = run(PLAIN, transfer_stake())
    record(store, plain, day=2)
    assert store.first_activity_day(GIVER) is None
    (named,) = run(NAMED, transfer_stake_and_hotkey())
    record(store, named, day=3)
    assert store.first_activity_day(GIVER) == 3


def test_an_unreasoned_legacy_deposit_is_still_a_first_activity_day(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    store.record_noah_deposit(
        coldkey=GIVER,
        block_timestamp=ANCHOR + 2 * DAY + 200,
        amount_rao=ALPHA,
        block=8_800_200,
        event_ordinal=1,
        extrinsic_index=3,
        block_hash="0xlegacy",
        carried_by="StakeTransferred",
        hotkey=VALIDATOR,
    )
    assert store.first_activity_day(GIVER) == 2


def test_a_reasoned_deposit_naming_a_hotkey_is_no_evidence_the_wallet_has_it(tmp_path):
    """A wrong-validator deposit names MINER but credits nobody, so it does
    not make MINER a hotkey this wallet has been seen with."""
    store = a_store(tmp_path)
    scanned(store, [MINER])
    (wrong,) = run(NAMED, transfer_stake_and_hotkey(), designated=WRONG_VALIDATOR)
    record(store, wrong, day=2)
    assert store.deposit_origin_hotkeys(GIVER, LAST_DAY) == set()
    (named,) = run(NAMED, transfer_stake_and_hotkey())
    record(store, named, day=3)
    assert store.deposit_origin_hotkeys(GIVER, LAST_DAY) == {MINER}


def test_rescanning_the_same_unnamed_deposit_is_idempotent(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    (first,) = run(PLAIN, transfer_stake())
    (again,) = run(PLAIN, transfer_stake())
    assert record(store, first) == record(store, again)
    assert len(store.unattributed_deposits()) == 1


def test_rescanning_an_old_unreasoned_row_now_that_it_has_a_reason_is_a_change(tmp_path):
    """A row stored before rule B has no reason. The reader now produces one for
    the same deposit, and the store refuses to quietly overwrite what it stored."""
    store = a_store(tmp_path)
    scanned(store, [MINER])
    (deposit,) = run(PLAIN, transfer_stake())
    store.record_noah_deposit(
        coldkey=deposit.coldkey,
        block_timestamp=ANCHOR + DEPOSIT_DAY * DAY + 200,
        amount_rao=deposit.amount_rao,
        block=deposit.block + DEPOSIT_DAY,
        event_ordinal=deposit.event_ordinal,
        extrinsic_index=deposit.extrinsic_index,
        block_hash=f"{deposit.block_hash}{DEPOSIT_DAY}",
        carried_by=deposit.carried_by,
        hotkey=deposit.hotkey,
    )
    with pytest.raises(ObservationChanged):
        record(store, deposit)


def test_a_wrong_validator_named_deposit_starts_no_history_at_wallet_or_seat_level(tmp_path):
    """A deposit that earns no credit must not start a seat's history either:
    first_activity_day and first_activity_day_for_hotkey agree."""
    store = a_store(tmp_path)
    scanned(store, [MINER])
    (wrong,) = run(NAMED, transfer_stake_and_hotkey(), designated=WRONG_VALIDATOR)
    assert wrong.origin_hotkey == MINER and wrong.unattributed_reason
    record(store, wrong, day=2)
    assert store.first_activity_day(GIVER) is None
    assert store.first_activity_day_for_hotkey(GIVER, MINER) is None
    assert credited(build(store, SINGLE)) == {}
    (named,) = run(NAMED, transfer_stake_and_hotkey())
    record(store, named, day=3)
    assert store.first_activity_day(GIVER) == 3
    assert store.first_activity_day_for_hotkey(GIVER, MINER) == 3


# ---- rescan compares the stable prefix only --------------------------------


def _record_with_reason(store, reason, *, origin=None, day=DEPOSIT_DAY):
    (deposit,) = run(PLAIN, transfer_stake())
    return store.record_noah_deposit(
        coldkey=deposit.coldkey,
        block_timestamp=ANCHOR + day * DAY + 200,
        amount_rao=deposit.amount_rao,
        block=deposit.block + day,
        event_ordinal=deposit.event_ordinal,
        extrinsic_index=deposit.extrinsic_index,
        block_hash=f"{deposit.block_hash}{day}",
        carried_by=deposit.carried_by,
        hotkey=deposit.hotkey,
        origin_hotkey=origin,
        unattributed_reason=reason,
    )


def test_a_reworded_unnamed_reason_is_the_same_observation_and_keeps_the_stored_text(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    old = f"{UNNAMED_REASON_PREFIX} this was a plain transfer_stake. Old advice."
    _record_with_reason(store, old)
    _record_with_reason(store, f"{UNNAMED_REASON_PREFIX} this was a plain transfer_stake. New.")
    (row,) = store.unattributed_deposits()
    assert row["reason"] == old  # not rewritten


def test_a_different_reason_class_still_raises(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    _record_with_reason(store, f"{UNNAMED_REASON_PREFIX} advice")
    with pytest.raises(ObservationChanged):
        _record_with_reason(
            store,
            "staked to 5X, not the designated Ark Validator 5Y -- does not qualify for "
            "mining credit",
        )


def test_a_reworded_reason_with_any_other_field_changed_still_raises(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    _record_with_reason(store, f"{UNNAMED_REASON_PREFIX} advice")
    with pytest.raises(ObservationChanged):
        _record_with_reason(store, f"{UNNAMED_REASON_PREFIX} advice", origin=MINER)


def test_an_old_wrong_validator_row_matches_the_new_text_with_the_unnamed_part_appended(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    fixed = (
        f"staked to {VALIDATOR}, not the designated Ark Validator {WRONG_VALIDATOR} "
        f"-- does not qualify for mining credit"
    )
    _record_with_reason(store, fixed)
    _record_with_reason(store, fixed + ". It also names no miner hotkey, so ...")
    (row,) = store.unattributed_deposits()
    assert row["reason"] == fixed


def test_a_row_that_gained_a_reason_from_none_still_raises_and_says_how_to_recover(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    _record_with_reason(store, None)
    with pytest.raises(ObservationChanged) as caught:
        _record_with_reason(store, f"{UNNAMED_REASON_PREFIX} advice")
    message = str(caught.value)
    assert "stored before rule B" in message
    assert "nothing is rewritten in place" in message
    assert "rebuild the store from the chain into a fresh store" in message


def test_the_reader_text_starts_with_the_stable_prefix():
    (plain,) = run(PLAIN, transfer_stake())
    assert plain.unattributed_reason.startswith(UNNAMED_REASON_PREFIX)


# ---- old rows: listed, and the legacy fallback pinned -----------------------


def test_a_legacy_row_is_listed_as_unattributed_with_a_synthesized_reason(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    _record_with_reason(store, None)  # stored before rule B: no origin, no reason
    _record_with_reason(store, f"{UNNAMED_REASON_PREFIX} advice", day=3)
    legacy, current = store.unattributed_deposits()
    assert legacy["legacy"] is True and legacy["reason"] == LEGACY_UNNAMED_REASON
    assert current["legacy"] is False and current["reason"].startswith(UNNAMED_REASON_PREFIX)


def test_a_named_deposit_is_never_listed_as_legacy(tmp_path):
    store = a_store(tmp_path)
    scanned(store, [MINER])
    (named,) = run(NAMED, transfer_stake_and_hotkey())
    record(store, named)
    assert store.unattributed_deposits() == []


def test_the_legacy_fallback_still_credits_a_seat_whose_own_hotkey_was_the_staking_hotkey(
    tmp_path,
):
    """An OLD row with no origin and no reason, staked to the seat's own hotkey,
    still credits that seat. Rows the rule-B reader writes never use this."""
    store = a_store(tmp_path)
    scanned(store, [MINER], pay_hotkeys=[MINER])
    store.record_noah_deposit(
        coldkey=GIVER,
        block_timestamp=ANCHOR + DEPOSIT_DAY * DAY + 200,
        amount_rao=ALPHA,
        block=8_800_200,
        event_ordinal=1,
        extrinsic_index=3,
        block_hash="0xlegacyown",
        carried_by="StakeTransferred",
        hotkey=MINER,
    )
    assert store.noah_deposits_for_hotkey(GIVER, MINER, DEPOSIT_DAY) == ALPHA
    assert credited(build(store, SINGLE)) == {140: [0, ALPHA, 0]}
    # The same row staked to a different hotkey credits nobody.
    assert store.noah_deposits_for_hotkey(GIVER, M2, DEPOSIT_DAY) == 0
