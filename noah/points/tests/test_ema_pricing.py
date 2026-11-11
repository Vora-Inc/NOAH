"""Pure policy /4 math; no claim about a selected on-chain EMA field."""
from fractions import Fraction
from itertools import product

import pytest

from noah.points.ledger import R, State, settle
from noah.points.pricing import RationalPrice as P
from noah.points.pricing import Unpriced, ema_spot_credit


def quote(q, ea, en, pa, pn, asset=3):
    return ema_spot_credit(q, asset, ema_asset=ea, ema_noah=en,
                           spot_asset=pa, spot_noah=pn, native_netuid=2)


@pytest.mark.parametrize("pa,pn,expected", [(P(1, 10), P(1, 20), 50),
    (P(1, 100), P(1, 20), 5), (P(1, 5), P(1, 20), 50),
    (P(1, 10), P(1, 40), 50)])
def test_handoff_examples(pa, pn, expected):
    assert quote(25 * R, P(1, 10), P(1, 20), pa, pn) == expected * R


def test_native_identity_needs_no_prices():
    assert ema_spot_credit(50 * R, 2, native_netuid=2) == 50 * R
    assert settle(State(), 100 * R, 50 * R).balance == 0


def test_root_and_foreign_satisfy_identical_obligation():
    foreign = quote(25 * R, P(1, 10), P(1, 20), P(1, 10), P(1, 20))
    root = quote(5 * R // 2, None, P(1, 20), None, P(1, 20), asset=0)
    assert root == foreign == 50 * R
    assert settle(State(), 100 * R, root) == settle(State(), 100 * R, foreign)


def test_compare_complete_ratios_not_component_minima():
    # EMA=2 and spot=3; independent component minima would yield 6.
    assert quote(7, P(6, 1), P(3, 1), P(3, 1), P(1, 1)) == 14


def test_single_final_floor_preserves_sub_rao_price_ratios():
    assert quote(1, P(1, 10**20), P(1, 10**20), P(3, 10**20), P(2, 10**20)) == 1
    assert quote(7, P(5, 3), P(7, 2), P(9, 4), P(13, 6)) == 3


def test_independent_fraction_oracle_and_split_nonincrease():
    values = [P(1, 100), P(1, 3), P(7, 2), P(2**64 - 1, 2**64)]
    for ea, en, pa, pn in product(values, repeat=4):
        rate = min(Fraction(ea.numerator, ea.denominator) / Fraction(en.numerator, en.denominator),
                   Fraction(pa.numerator, pa.denominator) / Fraction(pn.numerator, pn.denominator))
        for q in (0, 1, 13, 2**64 - 1):
            expected = (q * rate.numerator) // rate.denominator
            assert quote(q, ea, en, pa, pn) == expected
            a = q // 3
            assert quote(a, ea, en, pa, pn) + quote(q-a, ea, en, pa, pn) <= expected


@pytest.mark.parametrize("bad", [None, P(0, 1), P(1, 0), P(-1, 2), P(1, -2),
    P(True, 1), P(1, False), P(1.0, 1), P("1", 1), (1, 1)])
@pytest.mark.parametrize("position", range(4))
def test_invalid_or_unknown_price_never_becomes_zero_credit(bad, position):
    values = [P(1, 1)] * 4
    values[position] = bad
    with pytest.raises((ValueError, Unpriced)):
        quote(1, *values)


def test_root_cannot_borrow_an_alpha_ema():
    with pytest.raises(Unpriced):
        quote(1, P(2, 1), P(1, 1), None, P(1, 1), asset=0)


@pytest.mark.parametrize("amount", [-1, True, 1.0, "1"])
def test_invalid_amount_refused_even_native(amount):
    with pytest.raises(ValueError):
        ema_spot_credit(amount, 2, native_netuid=2)


def test_immutable_credit_drives_catchup_and_prepayment_without_streak_repair():
    # Value once at the receipt reference; later markets are not ledger inputs.
    credit = quote(75 * R, P(1, 10), P(1, 20), P(1, 10), P(1, 20))
    state = settle(State(earned=100 * R, balance=-50 * R), 100 * R, credit)
    assert credit == 150 * R and state.balance == 50 * R and state.streak == 1
    following = settle(state, 100 * R, 0)
    assert following.balance == 0 and following.streak == 0
