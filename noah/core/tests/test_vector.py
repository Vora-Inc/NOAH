"""The vector the validator publishes, and the four things that shape it.

Every test here exists because of a specific way this step can go wrong, and
each one says which.

The engine decides what every miner earned. This module decides nothing about
that. All it does is take the engine's finished answer and make it into
something the chain will accept: name the seats that exist, drop the ones that
do not, and refuse outright rather than send a vector that pays the owner.
"""

import pytest

from noah.core.vector import OwnerWouldBePaid, publishable_vector
from noah.scoring.pay import weights_u16


def a_seat_map(uids, hotkey_prefix="hk"):
    """Every uid mapped to a distinct hotkey, which is the ordinary case."""
    return {uid: f"{hotkey_prefix}{uid}" for uid in uids}


def test_scored_seats_keep_exactly_the_weights_the_engine_gave_them():
    # The engine's answer passes through untouched. If this ever fails, the
    # validator has started doing arithmetic of its own, which it must not.
    scored = {1: 40000, 2: 25535}
    live = [1, 2]
    out = publishable_vector(
        scored=scored,
        live_uids=live,
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=a_seat_map(live),
        owner_uids=set(),
    )
    assert out == {1: 40000, 2: 25535}


def test_live_seats_the_engine_did_not_score_appear_at_zero():
    # A registered seat that earned nothing is still named, at zero. Validators
    # and held-out miners land here.
    scored = {1: 65535}
    live = [0, 1, 2, 3]
    out = publishable_vector(
        scored=scored,
        live_uids=live,
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=a_seat_map(live),
        owner_uids=set(),
    )
    assert out == {0: 0, 1: 65535, 2: 0, 3: 0}


def test_an_all_zero_day_publishes_a_full_vector_of_zeroes():
    # This is the launch window and it is the whole reason this module exists.
    # The engine returns nothing at all when every pay weight is zero. Sending
    # nothing would let the chain stop hearing from us, and a validator the
    # chain stops hearing from is dropped from consensus. So an empty answer
    # becomes a vector naming every live seat at zero, which keeps us audible
    # while paying nobody.
    assert weights_u16({1: 0, 2: 0}) == {}
    live = [0, 1, 2]
    out = publishable_vector(
        scored={},
        live_uids=live,
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=a_seat_map(live),
        owner_uids=set(),
    )
    assert out == {0: 0, 1: 0, 2: 0}
    assert sum(out.values()) == 0


def test_a_seat_that_changed_hands_is_zeroed_not_paid_to_whoever_took_it():
    # Weights apply at least two epochs after the height they were computed
    # from, and a seat can be reassigned in that window. The miner who earned
    # this is gone; the new occupant earned nothing.
    live = [1, 2]
    chain_now = a_seat_map(live)
    chain_now[2] = "somebody-else"
    out = publishable_vector(
        scored={1: 30000, 2: 35535},
        live_uids=live,
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=chain_now,
        owner_uids=set(),
    )
    assert out == {1: 30000, 2: 0}


def test_a_seat_that_no_longer_exists_is_dropped_entirely():
    # Zeroing is not enough here. The chain refuses the whole vector if any
    # named seat does not exist on the subnet, so a deregistered seat has to
    # leave rather than stay at zero.
    live = [1]
    out = publishable_vector(
        scored={1: 30000, 2: 35535},
        live_uids=live,
        fixture_hotkeys={1: "hk1", 2: "hk2"},
        chain_hotkeys={1: "hk1"},
        owner_uids=set(),
    )
    assert out == {1: 30000}
    assert 2 not in out


def test_weight_aimed_at_an_owner_seat_refuses_the_whole_vector():
    # Miner pay aimed at a seat the owner controls is counted by the chain as
    # burned, publicly, and that figure scales the subnet's income down. The
    # validator does not publish a corrected vector here. It refuses, because
    # a vector that got this wrong is evidence that something upstream is
    # wrong, and quietly fixing it would hide that.
    live = [0, 1]
    with pytest.raises(OwnerWouldBePaid) as refused:
        publishable_vector(
            scored={0: 100, 1: 65435},
            live_uids=live,
            fixture_hotkeys=a_seat_map(live),
            chain_hotkeys=a_seat_map(live),
            owner_uids={0},
        )
    assert "0" in str(refused.value)


def test_an_owner_seat_at_zero_is_fine_and_still_named():
    # The owner's seat exists and must appear, at zero, like any other seat
    # that earned nothing. Only a nonzero weight on it is a refusal.
    live = [0, 1]
    out = publishable_vector(
        scored={1: 65535},
        live_uids=live,
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=a_seat_map(live),
        owner_uids={0},
    )
    assert out == {0: 0, 1: 65535}


def test_the_refusal_cannot_be_swallowed_by_a_broad_except():
    # Refusals in this project descend from BaseException on purpose, so a
    # retry loop written as "except Exception" cannot turn a refusal into a
    # shrug. This test fails if someone re-parents it to Exception.
    live = [0]
    try:
        publishable_vector(
            scored={0: 65535},
            live_uids=live,
            fixture_hotkeys=a_seat_map(live),
            chain_hotkeys=a_seat_map(live),
            owner_uids={0},
        )
    except Exception:  # noqa: BLE001
        pytest.fail("the refusal was caught by except Exception")
    except OwnerWouldBePaid:
        pass
    else:
        pytest.fail("the owner guard did not fire")


def test_a_seat_missing_from_the_chain_map_is_refused_rather_than_assumed():
    # A live seat we cannot check ownership for is not the same as a live seat
    # whose owner matches. Guessing either way pays somebody on a fact nobody
    # read.
    live = [1, 2]
    with pytest.raises(BaseException) as refused:
        publishable_vector(
            scored={1: 65535},
            live_uids=live,
            fixture_hotkeys=a_seat_map(live),
            chain_hotkeys={1: "hk1"},
            owner_uids=set(),
        )
    assert "2" in str(refused.value)


# --- The invariant, which is acceptance criterion 17 -----------------------
#
# A seat whose pay weight is zero must never receive a nonzero chain weight.
#
# Today this holds for a reason that has nothing to do with this module: in
# largest-remainder quantization the leftover units are handed out in
# descending-remainder order, and there are always strictly fewer leftover
# units than there are seats with a nonzero remainder, so the handout never
# reaches a seat sitting at zero. That is a property of the quantizer, not of
# anything the validator does.
#
# Which is exactly why it is pinned here. The risk is not that somebody pads
# in the wrong order. It is that somebody changes the tie-break or the
# leftover distribution one day, this stops being true, and every other test
# in the repository stays green.

ZERO_HEAVY_SHAPES = [
    {0: 0, 1: 0, 2: 0},
    {0: 0, 1: 1, 2: 0},
    {0: 0, 1: 1, 2: 1},
    {0: 5, 1: 0, 2: 0, 3: 0, 4: 0},
    {i: (1 if i == 7 else 0) for i in range(12)},
    {i: (i % 3) for i in range(30)},
    {i: (10**9 if i in (1, 2) else 0) for i in range(64)},
]


@pytest.mark.parametrize("pay_weights", ZERO_HEAVY_SHAPES)
def test_no_seat_with_zero_pay_weight_is_ever_paid(pay_weights):
    quantized = weights_u16(pay_weights)
    live = sorted(pay_weights)
    published = publishable_vector(
        scored=quantized,
        live_uids=live,
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=a_seat_map(live),
        owner_uids=set(),
    )
    for uid, weight in published.items():
        if pay_weights[uid] == 0:
            assert weight == 0, f"seat {uid} earned nothing and was paid {weight}"


@pytest.mark.parametrize("pay_weights", ZERO_HEAVY_SHAPES)
def test_naming_the_unscored_seats_does_not_move_the_scored_ones(pay_weights):
    # Adding the seats that earned nothing must not change what the seats that
    # earned something receive.
    quantized = weights_u16(pay_weights)
    live = sorted(pay_weights)
    published = publishable_vector(
        scored=quantized,
        live_uids=live,
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=a_seat_map(live),
        owner_uids=set(),
    )
    for uid, weight in quantized.items():
        assert published[uid] == weight


def test_every_live_seat_is_named_exactly_once():
    live = [0, 3, 7, 9]
    out = publishable_vector(
        scored={3: 65535},
        live_uids=live + [3],  # a duplicate in the caller's list changes nothing
        fixture_hotkeys=a_seat_map(live),
        chain_hotkeys=a_seat_map(live),
        owner_uids=set(),
    )
    assert sorted(out) == live
