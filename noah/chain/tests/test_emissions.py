"""Turning a positional pay vector into seat numbers, or refusing to.

The chain does not put the seat number in the pay event. It builds a vector of
the epoch's terms sorted by seat and then drops the seat, so the only thing
connecting a value to a participant is its position in a list
(epoch/run_epoch.rs:95-116).

That position means "seat N" only while the seat map is dense over
0..SubnetworkN-1. Nothing in the event says whether it is. If the map is ever
short by one, every value after the gap belongs to the next participant along,
and the arithmetic still works perfectly.
"""

import pytest

from noah.chain.emissions import (
    NotAVector,
    SeatsDoNotMatchVector,
    SeatsNotContiguous,
    seats_from_vector,
)

HOTKEYS = {uid: f"5Hotkey{uid:04d}" for uid in range(8)}


def test_a_dense_map_and_a_matching_vector_reads_position_as_seat():
    seats = seats_from_vector(
        [10, 20, 30], hotkey_by_uid={0: "a", 1: "b", 2: "c"}, subnetwork_n=3, what="pay"
    )
    assert seats == {0: 10, 1: 20, 2: 30}


def test_a_subnet_with_no_seats_yet_reads_as_no_pay_rather_than_refusing():
    assert seats_from_vector([], hotkey_by_uid={}, subnetwork_n=0, what="pay") == {}


def test_a_gap_in_the_seat_map_is_refused_rather_than_shifting_everyone_after_it():
    """The whole reason this module exists.

    Seat 5 is gone. The chain would build a 7-long vector for 8 declared seats.
    Read positionally, seat 5 gets seat 6's pay and seat 6 gets seat 7's, all
    the way to the end, and every number still adds up.
    """
    present = {uid: HOTKEYS[uid] for uid in range(8) if uid != 5}
    with pytest.raises(SeatsNotContiguous) as refused:
        seats_from_vector([1, 2, 3, 4, 5, 6, 7], hotkey_by_uid=present, subnetwork_n=8, what="pay")
    assert "5" in str(refused.value)


def test_the_shifted_reading_a_gap_would_have_produced_is_named_in_the_refusal():
    """An operator needs to see what the wrong answer would have been."""
    present = {uid: HOTKEYS[uid] for uid in range(8) if uid != 5}
    with pytest.raises(SeatsNotContiguous) as refused:
        seats_from_vector([1, 2, 3, 4, 5, 6, 7], hotkey_by_uid=present, subnetwork_n=8, what="pay")
    message = str(refused.value)
    assert "seat" in message and "8" in message


def test_a_seat_at_or_above_the_declared_count_is_refused():
    present = {uid: HOTKEYS[uid] for uid in range(3)}
    present[6] = HOTKEYS[6]
    with pytest.raises(SeatsNotContiguous):
        seats_from_vector([1, 2, 3], hotkey_by_uid=present, subnetwork_n=3, what="pay")


def test_a_vector_shorter_than_the_declared_seat_count_is_refused():
    with pytest.raises(SeatsDoNotMatchVector):
        seats_from_vector(
            [1, 2], hotkey_by_uid={0: "a", 1: "b", 2: "c"}, subnetwork_n=3, what="pay"
        )


def test_a_vector_longer_than_the_declared_seat_count_is_refused():
    with pytest.raises(SeatsDoNotMatchVector):
        seats_from_vector(
            [1, 2, 3, 4], hotkey_by_uid={0: "a", 1: "b", 2: "c"}, subnetwork_n=3, what="pay"
        )


def test_two_seats_holding_one_hotkey_is_refused():
    """The runtime keys the epoch map by hotkey, so a duplicate collapses a seat.

    The registration path forbids it (subnets/registration.rs:61-64), which is
    exactly why nothing downstream would notice if it ever happened.
    """
    with pytest.raises(SeatsNotContiguous):
        seats_from_vector(
            [1, 2, 3], hotkey_by_uid={0: "a", 1: "b", 2: "b"}, subnetwork_n=3, what="pay"
        )


def test_a_vector_that_is_not_a_list_is_refused():
    with pytest.raises(NotAVector):
        seats_from_vector("0x00", hotkey_by_uid={0: "a"}, subnetwork_n=1, what="pay")


def test_an_undecoded_value_in_the_vector_is_refused():
    with pytest.raises(NotAVector):
        seats_from_vector(["0x2a"], hotkey_by_uid={0: "a"}, subnetwork_n=1, what="pay")


def test_a_boolean_is_not_a_pay_amount():
    with pytest.raises(NotAVector):
        seats_from_vector([True], hotkey_by_uid={0: "a"}, subnetwork_n=1, what="pay")


def test_a_negative_amount_is_refused():
    with pytest.raises(NotAVector):
        seats_from_vector([-1], hotkey_by_uid={0: "a"}, subnetwork_n=1, what="pay")


def test_a_declared_seat_count_that_is_not_a_whole_number_is_refused():
    with pytest.raises(SeatsDoNotMatchVector):
        seats_from_vector([1], hotkey_by_uid={0: "a"}, subnetwork_n="1", what="pay")


def test_the_seat_map_keys_must_be_whole_numbers():
    with pytest.raises(SeatsNotContiguous):
        seats_from_vector([1], hotkey_by_uid={"0": "a"}, subnetwork_n=1, what="pay")


def test_refusing_beats_the_wrong_answer_by_a_measurable_amount():
    """Naming the size of the error the guard prevents, in the test itself.

    Eight seats, seat 5 removed. Position 5 in the vector holds seat 6's pay.
    A positional read hands seat 5 a payment of 600 that belongs to seat 6, and
    the last seat silently earns nothing at all.
    """
    vector = [0, 0, 0, 0, 0, 600, 700, 800][:7]
    present = {uid: HOTKEYS[uid] for uid in range(8) if uid != 5}

    naive = dict(enumerate(vector))
    assert naive[5] == 600, "the shifted reading credits seat 5 with seat 6's pay"
    assert 7 not in naive, "and the last seat drops out of the day entirely"

    with pytest.raises(SeatsNotContiguous):
        seats_from_vector(vector, hotkey_by_uid=present, subnetwork_n=8, what="pay")


# ---------------------------------------------------------------------------
# The event does not carry a netuid. It carries a packed index.
# ---------------------------------------------------------------------------

from noah.chain.emissions import (  # noqa: E402
    PAY_EVENT,
    MechanismsMissing,
    NotAMechanismIndex,
    epoch_pay_vectors,
    netuid_and_mechanism,
    pay_event_present,
)

OUR_NETUID = 35


def incentive_event(storage_index, emissions):
    return {
        "module_id": "SubtensorModule",
        "event_id": "IncentiveAlphaEmittedToMiners",
        "attributes": {"netuid": storage_index, "emissions": emissions},
    }


def test_the_main_mechanism_packs_to_the_plain_netuid():
    assert netuid_and_mechanism(35) == (35, 0)


def test_a_second_mechanism_packs_above_the_subnet_count():
    """4096 is the stride. Mechanism 1 on subnet 35 arrives as 4131."""
    assert netuid_and_mechanism(4131) == (35, 1)
    assert netuid_and_mechanism(8227) == (35, 2)


def test_reading_the_packed_index_as_a_netuid_is_the_silent_half_of_the_pay():
    """Naming the failure this decode prevents.

    A reader that compares the event's netuid field against its own subnet
    number keeps mechanism 0 and discards every other mechanism as belonging to
    some other subnet. The remaining number is a real number, correctly summed,
    and short by however much the other mechanisms paid.
    """
    packed = 4131
    assert packed != OUR_NETUID, "a naive filter drops this event"
    assert netuid_and_mechanism(packed) == (OUR_NETUID, 1), "it is ours, on mechanism 1"


def test_an_index_that_is_not_a_whole_number_is_refused():
    with pytest.raises(NotAMechanismIndex):
        netuid_and_mechanism("35")


def test_a_negative_index_is_refused():
    with pytest.raises(NotAMechanismIndex):
        netuid_and_mechanism(-1)


def test_one_mechanism_reads_through_to_seat_amounts():
    vectors = epoch_pay_vectors(
        [incentive_event(OUR_NETUID, [10, 20, 30])],
        netuid=OUR_NETUID,
        mechanism_count=1,
        hotkey_by_uid={0: "a", 1: "b", 2: "c"},
        subnetwork_n=3,
        block=8_890_000,
    )
    assert vectors == {0: {0: 10, 1: 20, 2: 30}}


def test_both_mechanisms_are_kept_and_neither_is_summed_here():
    vectors = epoch_pay_vectors(
        [
            incentive_event(OUR_NETUID, [10, 20, 30]),
            incentive_event(OUR_NETUID + 4096, [1, 2, 3]),
        ],
        netuid=OUR_NETUID,
        mechanism_count=2,
        hotkey_by_uid={0: "a", 1: "b", 2: "c"},
        subnetwork_n=3,
        block=8_890_000,
    )
    assert vectors == {0: {0: 10, 1: 20, 2: 30}, 1: {0: 1, 1: 2, 2: 3}}


def test_another_subnets_pay_event_in_the_same_block_is_not_ours():
    vectors = epoch_pay_vectors(
        [incentive_event(OUR_NETUID, [10, 20, 30]), incentive_event(9, [99, 99, 99])],
        netuid=OUR_NETUID,
        mechanism_count=1,
        hotkey_by_uid={0: "a", 1: "b", 2: "c"},
        subnetwork_n=3,
        block=8_890_000,
    )
    assert vectors == {0: {0: 10, 1: 20, 2: 30}}


def test_a_missing_mechanism_is_refused_rather_than_summed_short():
    """Two mechanisms ran, one event was read. The sum would look fine."""
    with pytest.raises(MechanismsMissing) as refused:
        epoch_pay_vectors(
            [incentive_event(OUR_NETUID, [10, 20, 30])],
            netuid=OUR_NETUID,
            mechanism_count=2,
            hotkey_by_uid={0: "a", 1: "b", 2: "c"},
            subnetwork_n=3,
            block=8_890_000,
        )
    assert "1" in str(refused.value)


def test_a_mechanism_beyond_the_declared_count_is_refused():
    with pytest.raises(MechanismsMissing):
        epoch_pay_vectors(
            [
                incentive_event(OUR_NETUID, [10, 20, 30]),
                incentive_event(OUR_NETUID + 4096, [1, 2, 3]),
            ],
            netuid=OUR_NETUID,
            mechanism_count=1,
            hotkey_by_uid={0: "a", 1: "b", 2: "c"},
            subnetwork_n=3,
            block=8_890_000,
        )


def test_the_same_mechanism_twice_in_one_block_is_refused():
    """Which one is this epoch's pay is not on the chain, and it is not a guess."""
    with pytest.raises(MechanismsMissing):
        epoch_pay_vectors(
            [incentive_event(OUR_NETUID, [10, 20, 30]), incentive_event(OUR_NETUID, [9, 9, 9])],
            netuid=OUR_NETUID,
            mechanism_count=1,
            hotkey_by_uid={0: "a", 1: "b", 2: "c"},
            subnetwork_n=3,
            block=8_890_000,
        )


def test_a_block_with_no_pay_event_of_ours_is_not_an_epoch_block():
    assert (
        epoch_pay_vectors(
            [incentive_event(9, [1, 2, 3])],
            netuid=OUR_NETUID,
            mechanism_count=1,
            hotkey_by_uid={0: "a", 1: "b", 2: "c"},
            subnetwork_n=3,
            block=8_890_000,
        )
        == {}
    )


def test_a_declared_mechanism_count_of_zero_is_refused():
    """A subnet always runs at least one mechanism. Zero is a read that failed."""
    with pytest.raises(MechanismsMissing):
        epoch_pay_vectors(
            [incentive_event(OUR_NETUID, [10, 20, 30])],
            netuid=OUR_NETUID,
            mechanism_count=0,
            hotkey_by_uid={0: "a", 1: "b", 2: "c"},
            subnetwork_n=3,
            block=8_890_000,
        )


# ---------------------------------------------------------------------------
# The cheap question asked of every block: did our subnet pay here at all?
# ---------------------------------------------------------------------------


def test_an_ordinary_block_is_not_an_epoch_block():
    assert pay_event_present([], netuid=OUR_NETUID) is False


def test_our_main_mechanism_paying_makes_it_an_epoch_block():
    assert pay_event_present([incentive_event(OUR_NETUID, [1])], netuid=OUR_NETUID) is True


def test_only_our_second_mechanism_paying_still_makes_it_an_epoch_block():
    """The case a netuid comparison would miss entirely."""
    assert pay_event_present([incentive_event(OUR_NETUID + 4096, [1])], netuid=OUR_NETUID) is True


def test_another_subnets_epoch_is_not_ours():
    assert pay_event_present([incentive_event(9, [1])], netuid=OUR_NETUID) is False


def test_a_subnet_whose_number_matches_our_packed_index_is_not_ours():
    """Subnet 4131 exists in the same space as our mechanism 1.

    Packed index 4131 is subnet 35 mechanism 1. A plain subnet 4131 could never
    be paid, because the packing reserves everything at or above 4096, and this
    asserts we read it as ours rather than as a stranger's.
    """
    assert pay_event_present([incentive_event(4131, [1])], netuid=4131) is False
    assert pay_event_present([incentive_event(4131, [1])], netuid=35) is True


def test_an_unrelated_event_in_the_block_is_ignored():
    other = {"module_id": "Balances", "event_id": "Transfer", "attributes": {"netuid": 35}}
    assert pay_event_present([other], netuid=OUR_NETUID) is False


def test_a_pay_event_that_lost_its_subnet_field_is_refused_not_skipped():
    broken = {"module_id": "SubtensorModule", "event_id": PAY_EVENT, "attributes": {}}
    with pytest.raises(NotAMechanismIndex):
        pay_event_present([broken], netuid=OUR_NETUID)
