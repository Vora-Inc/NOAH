"""Attributing one epoch's pay to the coldkeys that earned it.

The chain pays a slot, not a person. Turning "slot 140 was paid" into "this
wallet was paid" needs three more reads at the same block: which hotkey held
the slot, which coldkey owns that hotkey, and when the slot was last handed
over. Every one of those can change between epochs, so all of them are read at
the epoch block and never at the end of the range.
"""

import pytest

from noah.chain.pay import (
    NoPayObserved,
    NoRegistrationRecord,
    NotAnAccount,
    NotAnAmount,
    SlotTurnedOver,
    attribute_epoch,
)

# The account every one of these maps answers with when it holds no record.
# Substrate decodes trailing zeroes into a real, valid, spendable-looking
# address, so "no record" arrives looking exactly like a wallet.
ZERO_ACCOUNT = "5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM"

EPOCH_BLOCK = 8_875_512
HOTKEY = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
COLDKEY = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
SN_OWNER = "5FsnowLaLaLaLaLaLaLaLaLaLaLaLaLaLaLaLaLaLaLaLaLaL"
SN_OWNER_HOTKEY = "5FownerHotkeyownerHotkeyownerHotkeyownerHotkeyoQ"


def epoch(**overrides):
    """One paid slot at one epoch block, with every read supplied."""
    call = {
        "emissions_by_mechanism": {0: {140: 35_551_889_238}},
        "hotkey_by_uid": {140: HOTKEY},
        "coldkey_by_hotkey": {HOTKEY: COLDKEY},
        "registration_block_by_uid": {140: EPOCH_BLOCK - 5_000},
        "epoch_block": EPOCH_BLOCK,
        "subnet_owner_coldkey": SN_OWNER,
        "subnet_owner_hotkey": SN_OWNER_HOTKEY,
    }
    call.update(overrides)
    return call


def test_a_paid_slot_is_attributed_to_the_coldkey_that_held_it_at_that_block():
    """Amounts are real: recorded from netuid 35, block 8,875,512, 2026-08-19."""
    result = attribute_epoch(**epoch())
    assert result.paid_by_coldkey == {COLDKEY: 35_551_889_238}


def test_two_slots_owned_by_one_coldkey_are_summed_into_one_row():
    second_hotkey = "5HpvciGmspgXHi8LkcNkMDfKc8ToqwVxxBifB1vA4RHQR3q5"
    result = attribute_epoch(
        **epoch(
            emissions_by_mechanism={0: {140: 35_551_889_238, 80: 15_417_400_074}},
            hotkey_by_uid={140: HOTKEY, 80: second_hotkey},
            coldkey_by_hotkey={HOTKEY: COLDKEY, second_hotkey: COLDKEY},
            registration_block_by_uid={140: EPOCH_BLOCK - 5_000, 80: EPOCH_BLOCK - 90},
        )
    )
    assert result.paid_by_coldkey == {COLDKEY: 35_551_889_238 + 15_417_400_074}


# ---- the slot that changed hands --------------------------------------------
#
# A slot is a seat, not a person. `replace_neuron` stamps the new occupant's
# registration block over the old one in place (subtensor pallets/subtensor/
# src/subnets/uids.rs:117), so the only record that the seat changed hands is
# that block number, read at the same block as the pay.


def test_a_slot_that_turned_over_in_the_epoch_block_is_refused_not_guessed():
    """The epoch fired and the seat changed hands in the same block. Whether the
    outgoing or the incoming participant earned that pay is not on the chain,
    and either answer pays somebody for work they did not do.
    """
    with pytest.raises(SlotTurnedOver) as excinfo:
        attribute_epoch(**epoch(registration_block_by_uid={140: EPOCH_BLOCK}))
    message = str(excinfo.value)
    assert "140" in message
    assert "8875512" in message.replace(",", "").replace("_", "")


def test_the_same_epoch_with_an_earlier_registration_attributes_normally():
    """The other half of the same criterion: the refusal is about the collision,
    not about reading the registration block at all."""
    result = attribute_epoch(**epoch(registration_block_by_uid={140: EPOCH_BLOCK - 1}))
    assert result.paid_by_coldkey == {COLDKEY: 35_551_889_238}


def test_a_registration_block_of_zero_on_a_paid_slot_is_refused():
    """The trap this project has already been caught by once.

    BlockAtRegistration is declared ValueQuery with a default of 0 (subtensor
    pallets/subtensor/src/lib.rs:972 and 2732-2740, spec 450; was :862 and
    :2640-2648 at spec 447/448, re-verified 2026-08-28, unchanged -- this is
    DefaultBlockAtRegistration and the BlockAtRegistration storage declaration).
    In Substrate that means a
    key with no record returns 0 rather than nothing, so "this seat has no
    registration record" and "this seat registered at block 0" arrive as the
    same bytes. `trim_to_max_allowed_uids` deletes the record outright
    (uids.rs:256), so the zero turns up on seats that were real an hour ago.

    A comparison against the epoch block walks straight past it, which is
    exactly how the previous guard in this repository came to be inert in
    production while passing every test written for it.
    """
    with pytest.raises(NoRegistrationRecord) as excinfo:
        attribute_epoch(**epoch(registration_block_by_uid={140: 0}))
    assert "140" in str(excinfo.value)


def test_a_registration_block_after_the_epoch_block_is_refused():
    """Read at the epoch block's own hash, a seat cannot have been registered in
    a block that has not happened yet. Something answered about the wrong
    height."""
    with pytest.raises(SlotTurnedOver):
        attribute_epoch(**epoch(registration_block_by_uid={140: EPOCH_BLOCK + 1}))


def test_a_paid_slot_with_no_registration_read_at_all_is_refused():
    """Absence in the map handed in is not permission to skip the check."""
    with pytest.raises(NoRegistrationRecord):
        attribute_epoch(**epoch(registration_block_by_uid={}))


# ---- pay the chain announced but never handed over --------------------------
#
# The event is emitted inside the epoch (subtensor pallets/subtensor/src/epoch/
# run_epoch.rs:114) carrying the raw per-seat vector. The withholding happens
# afterwards, in distribute_dividends_and_incentives (coinbase/run_coinbase.rs:
# 735), which skips any seat held by a hotkey associated with the subnet owner
# and burns or recycles that amount instead. So the event says a seat was paid
# when nobody was paid, and a reader that trusts the event credits pay that was
# destroyed.


def test_pay_aimed_at_the_subnet_owner_hotkey_is_not_attributed_to_anyone():
    result = attribute_epoch(
        **epoch(
            hotkey_by_uid={140: SN_OWNER_HOTKEY},
            coldkey_by_hotkey={SN_OWNER_HOTKEY: SN_OWNER},
        )
    )
    assert result.paid_by_coldkey == {}


def test_pay_aimed_at_a_hotkey_owned_by_the_subnet_owner_coldkey_is_not_attributed():
    """The runtime builds its skip list from OwnedHotkeys(subnet owner coldkey)
    (run_coinbase.rs:659-690). Reading Owner(hotkey) is the same set: the two
    maps are only ever written together, at swap_coldkey.rs:188 with 194-195 and
    at swap_hotkey.rs:433 with 438-446. set_hotkey_owner writes Owner alone
    (staking/helpers.rs:157-165) but has no other caller.
    """
    result = attribute_epoch(**epoch(coldkey_by_hotkey={HOTKEY: SN_OWNER}))
    assert result.paid_by_coldkey == {}


def test_withheld_pay_is_recorded_rather_than_silently_dropped():
    """It is not pay, but the reader must be able to show it decided that."""
    result = attribute_epoch(**epoch(coldkey_by_hotkey={HOTKEY: SN_OWNER}))
    assert result.withheld == ((140, HOTKEY, SN_OWNER, 35_551_889_238),)


def test_one_owner_slot_does_not_suppress_the_others():
    other_hotkey = "5HpvciGmspgXHi8LkcNkMDfKc8ToqwVxxBifB1vA4RHQR3q5"
    other_coldkey = "5CS7V5Qh6G1KJPc9Tn58dnUgqf2YddM5v16fmHCp9fgLHroD"
    result = attribute_epoch(
        **epoch(
            emissions_by_mechanism={0: {140: 35_551_889_238, 80: 15_417_400_074}},
            hotkey_by_uid={140: SN_OWNER_HOTKEY, 80: other_hotkey},
            coldkey_by_hotkey={SN_OWNER_HOTKEY: SN_OWNER, other_hotkey: other_coldkey},
            registration_block_by_uid={140: EPOCH_BLOCK - 5_000, 80: EPOCH_BLOCK - 90},
        )
    )
    assert result.paid_by_coldkey == {other_coldkey: 15_417_400_074}


# ---- reads that did not arrive ----------------------------------------------


def test_a_paid_slot_with_no_hotkey_at_that_block_is_refused():
    """Somebody was paid and the reader cannot say who. Dropping the row loses
    their pay quietly; the whole run stops instead.

    The message has to name which of the two reads was missing, because
    collapsing them into one branch is a change no other assertion notices.
    """
    with pytest.raises(NotAnAccount) as excinfo:
        attribute_epoch(**epoch(hotkey_by_uid={}))
    message = str(excinfo.value)
    assert "the hotkey holding seat 140" in message


def test_a_paid_hotkey_with_no_owner_read_is_refused():
    with pytest.raises(NotAnAccount) as excinfo:
        attribute_epoch(**epoch(coldkey_by_hotkey={}))
    assert "the wallet owning hotkey" in str(excinfo.value)


@pytest.mark.parametrize(
    "wire_amount",
    ["0x84a2f6b16", "35551889238", None, [], True, 35_551_889_238.0],
    ids=["hex", "decimal-str", "null", "empty-list", "bool", "float"],
)
def test_an_amount_that_is_not_a_decoded_integer_is_refused(wire_amount):
    """Decoding happens at the edge. A value that reaches attribution still
    wearing its wire format has not been decoded, and int("0x84a2f6b16") is not
    a conversion, it is an exception or a wrong number depending on the shape.
    """
    with pytest.raises(NotAnAmount):
        attribute_epoch(**epoch(emissions_by_mechanism={0: {140: wire_amount}}))


def test_a_negative_amount_is_refused():
    with pytest.raises(NotAnAmount):
        attribute_epoch(**epoch(emissions_by_mechanism={0: {140: -1}}))


def test_a_seat_paid_zero_is_not_a_row():
    """The event vector carries an entry for every seat, most of them zero. A
    zero here is the chain saying nothing was paid, which is different from the
    zero this reader refuses elsewhere: that one is a source failing to answer.
    """
    result = attribute_epoch(**epoch(emissions_by_mechanism={0: {140: 0}}))
    assert result.paid_by_coldkey == {}


# ---- the chain pays the sum, and announces the parts ------------------------
#
# A subnet can run more than one mechanism. The pay event fires once inside
# each mechanism's own epoch (subtensor pallets/subtensor/src/subnets/
# mechanism.rs:301 calling persist_mechanism_epoch_terms, which is where the
# event is emitted at epoch/run_epoch.rs:114), and every one of those events
# carries a full vector indexed by seat number over the same seats. What the
# participant is actually paid is the sum the runtime itself accumulates at
# mechanism.rs:325-327.
#
# So one event is a partial view of one block's pay. Reading a single event, or
# recording each event as though it were a separate payment, both produce a
# wallet paid a fraction of what it earned. Summing happens here, inside the
# attribution, so that no caller can be written that gets it wrong.


def test_two_mechanisms_paying_one_seat_in_one_block_are_summed():
    result = attribute_epoch(
        **epoch(emissions_by_mechanism={0: {140: 1_000_000}, 1: {140: 1_000_000}})
    )
    assert result.paid_by_coldkey == {COLDKEY: 2_000_000}


def test_a_seat_paid_by_only_one_of_two_mechanisms_still_gets_that_pay():
    result = attribute_epoch(**epoch(emissions_by_mechanism={0: {140: 1_000_000}, 1: {140: 0}}))
    assert result.paid_by_coldkey == {COLDKEY: 1_000_000}


def test_an_epoch_with_no_mechanism_read_at_all_is_refused():
    """No vector is not an epoch that paid nothing. It is a read that did not
    happen, and a caller that treats it as zero understates every wallet."""
    with pytest.raises(NoPayObserved):
        attribute_epoch(**epoch(emissions_by_mechanism={}))


def test_a_mechanism_that_paid_nothing_at_all_is_not_a_refusal():
    """Before the subnet begins emitting, every seat is genuinely paid nothing,
    and those days are real days that have to be recorded. A blanket refusal
    on an all-zero epoch would make the subnet's own first days unreadable."""
    result = attribute_epoch(**epoch(emissions_by_mechanism={0: {140: 0, 80: 0}}))
    assert result.paid_by_coldkey == {}
    assert result.withheld == ()


# ---- the account that means "no record" -------------------------------------
#
# Keys, Owner, SubnetOwner and SubnetOwnerHotkey are all declared ValueQuery
# against a default that decodes trailing zeroes into an AccountId (lib.rs:
# 2552-2561 with 1029-1033, 1361-1362 with 623-627, 2210-2216 with 836-840).
# None of them can return absence. A seat whose key was deleted answers with the
# default address, and that address is real: a participant holds stake at it
# (read live 2026-08-20). One string, two meanings, no way to tell from the
# value which one you have.
#
# So a guard written as `if hotkey is None` defends against a bug in our own
# decoder and against nothing the chain can actually do. That is the same
# inert-guard shape this repository already shipped once.


def test_a_seat_whose_hotkey_reads_as_the_no_record_account_is_refused():
    with pytest.raises(NotAnAccount) as excinfo:
        attribute_epoch(
            **epoch(
                hotkey_by_uid={140: ZERO_ACCOUNT},
                coldkey_by_hotkey={ZERO_ACCOUNT: COLDKEY},
            )
        )
    assert "140" in str(excinfo.value)


def test_a_hotkey_whose_owning_wallet_reads_as_the_no_record_account_is_refused():
    """Without this the pay is credited to whoever holds the default address,
    and the participant who earned it shows zero."""
    with pytest.raises(NotAnAccount):
        attribute_epoch(**epoch(coldkey_by_hotkey={HOTKEY: ZERO_ACCOUNT}))


def test_a_subnet_owner_that_reads_as_the_no_record_account_is_refused():
    """Left alone, this is worse than it looks. Every hotkey with no owner
    record also reads as that same account, so the exclusion test matches, and
    a real participant's whole epoch is filed as pay the runtime withheld.
    """
    with pytest.raises(NotAnAccount):
        attribute_epoch(**epoch(subnet_owner_coldkey=ZERO_ACCOUNT))


def test_a_subnet_owner_hotkey_that_reads_as_the_no_record_account_is_refused():
    with pytest.raises(NotAnAccount):
        attribute_epoch(**epoch(subnet_owner_hotkey=ZERO_ACCOUNT))


@pytest.mark.parametrize("bad", [b"\x01\x02", None, "", 5], ids=["bytes", "null", "empty", "int"])
def test_a_hotkey_that_is_not_text_is_refused(bad):
    with pytest.raises(NotAnAccount):
        attribute_epoch(**epoch(hotkey_by_uid={140: bad}, coldkey_by_hotkey={bad: COLDKEY}))


@pytest.mark.parametrize(
    "bad",
    ["8875512", 8_875_512.0, None, True, 0, -1],
    ids=["str", "float", "null", "bool", "zero", "negative"],
)
def test_an_epoch_block_that_is_not_a_plain_positive_integer_is_refused(bad):
    """The one input that used to escape as an ordinary TypeError. Refusal
    descends from BaseException on purpose, so that a retry loop cannot swallow
    it; an ordinary exception on this path walks straight back out again."""
    with pytest.raises(NotAnAmount):
        attribute_epoch(**epoch(epoch_block=bad))


def test_seat_numbers_that_are_not_integers_are_refused_rather_than_crashing():
    with pytest.raises(NotAnAmount):
        attribute_epoch(
            **epoch(
                emissions_by_mechanism={0: {140: 100, "140": 100}},
                hotkey_by_uid={140: HOTKEY, "140": HOTKEY},
                registration_block_by_uid={140: EPOCH_BLOCK - 5, "140": EPOCH_BLOCK - 5},
            )
        )


# ---- what the store needs, the attribution has to hand over -----------------


def test_the_result_carries_the_seat_and_hotkey_behind_every_wallet_total():
    """The store records one row per seat per block, with the hotkey it came
    through. If attribution only handed back wallet totals, whoever writes the
    caller would have to redo the seat walk themselves, and the exclusion of
    owner-associated seats would live in the code path they were bypassing.
    """
    other_hotkey = "5HpvciGmspgXHi8LkcNkMDfKc8ToqwVxxBifB1vA4RHQR3q5"
    other_coldkey = "5CS7V5Qh6G1KJPc9Tn58dnUgqf2YddM5v16fmHCp9fgLHroD"
    result = attribute_epoch(
        **epoch(
            emissions_by_mechanism={0: {140: 100, 80: 200}, 1: {140: 5}},
            hotkey_by_uid={140: HOTKEY, 80: other_hotkey},
            coldkey_by_hotkey={HOTKEY: COLDKEY, other_hotkey: other_coldkey},
            registration_block_by_uid={140: EPOCH_BLOCK - 5_000, 80: EPOCH_BLOCK - 90},
        )
    )
    assert result.paid == (
        (80, other_hotkey, other_coldkey, 200),
        (140, HOTKEY, COLDKEY, 105),
    )


def test_the_seat_rows_come_back_in_seat_order_whatever_order_they_arrived_in():
    """Two implementations have to agree byte for byte, so nothing may depend
    on the order a dictionary happened to be built in."""
    other_hotkey = "5HpvciGmspgXHi8LkcNkMDfKc8ToqwVxxBifB1vA4RHQR3q5"
    other_coldkey = "5CS7V5Qh6G1KJPc9Tn58dnUgqf2YddM5v16fmHCp9fgLHroD"
    common = {
        "hotkey_by_uid": {140: HOTKEY, 80: other_hotkey},
        "coldkey_by_hotkey": {HOTKEY: COLDKEY, other_hotkey: other_coldkey},
        "registration_block_by_uid": {140: EPOCH_BLOCK - 5_000, 80: EPOCH_BLOCK - 90},
    }
    forward = attribute_epoch(**epoch(emissions_by_mechanism={0: {140: 1, 80: 2}}, **common))
    reverse = attribute_epoch(**epoch(emissions_by_mechanism={0: {80: 2, 140: 1}}, **common))
    assert (
        forward.paid
        == reverse.paid
        == (
            (80, other_hotkey, other_coldkey, 2),
            (140, HOTKEY, COLDKEY, 1),
        )
    )


@pytest.mark.parametrize(
    "wire",
    ["8875000", 8_875_000.0, [], {}, b"8875000"],
    ids=["decimal-str", "float", "list", "dict", "bytes"],
)
def test_a_registration_block_that_is_not_a_decoded_integer_is_refused(wire):
    """Nothing was reaching this. Without it the next line compares the value
    against zero, and comparing text to a number raises an ordinary exception
    rather than a refusal, which is precisely what a retry loop written broadly
    swallows.
    """
    with pytest.raises(NoRegistrationRecord, match="not a decoded integer"):
        attribute_epoch(**epoch(registration_block_by_uid={140: wire}))


def test_a_missing_registration_block_says_it_was_missing(tmp_path=None):
    """Redundant with the check above in the sense that either would refuse a
    missing value, but they say different things and an operator needs the
    difference: one means nobody read it, the other means it came back
    unreadable."""
    with pytest.raises(NoRegistrationRecord, match="no registration block was read"):
        attribute_epoch(**epoch(registration_block_by_uid={}))
