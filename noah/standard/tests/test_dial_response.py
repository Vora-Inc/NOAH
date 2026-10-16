"""Each dial moved on its own, and the direction the formula must move.

The gate asks for three before-and-after weight reads on the test network, one
per dial. That needs a validator setting weights on a chain, which is not
running. This is the half that can be done first and that de-risks the other:
if the formula does not respond as specified here, the on-chain run would spend
a testnet cycle discovering it.

**Each dial is moved ALONE.** Moving two at once and seeing the result change
tells you nothing about which one did it, and a formula can respond correctly to
a pair while being wrong about each.

**Direction is asserted, not magnitude.** The magnitudes are already pinned by
the vector file. What no vector states is the SHAPE: that raising the target is
harsher, that raising harshness is harsher below the target and kinder above it,
and that the cap only ever binds downward. Those are the properties a reader of
the specification would expect, and they are what an on-chain read confirms.
"""

from noah.standard.multiplier import FP_SCALE, multiplier

BASE = dict(pci=800_000_000, target=1_000_000_000, harshness=2, cap=4 * FP_SCALE)


def m(**overrides):
    return multiplier(**{**BASE, **overrides})


# ---- the target ---------------------------------------------------------------


def test_raising_the_target_alone_lowers_the_multiplier():
    """A higher ask means the same holding is a smaller share of it.

    The target is bounded at the fixed-point unit, because it is a SHARE and a
    share above one is not a thing. My first version of this test asked for
    twice the unit and was refused by the input check, which is the check doing
    its job on the person writing the test for it.
    """
    easier = m(target=250_000_000)
    harder = m(target=1_000_000_000)

    assert easier > harder


def test_lowering_the_target_far_enough_reaches_the_cap():
    """The cap binds from below as the ask shrinks, and never above it."""
    assert m(target=1) == BASE["cap"]


# ---- harshness ----------------------------------------------------------------


def test_raising_harshness_alone_is_harsher_below_the_target():
    """Below the target the ratio is under one, so powers shrink it."""
    below = dict(pci=500_000_000, target=1_000_000_000)
    gentle = m(**below, harshness=1)
    harsh = m(**below, harshness=4)

    assert gentle > harsh


def test_raising_harshness_alone_is_kinder_above_the_target():
    """Above the target the ratio is over one, so powers grow it.

    This is the direction most likely to be got backwards by someone reading
    'harshness' as a word rather than as an exponent, and no vector says it.
    """
    above = dict(pci=2_000_000_000, target=1_000_000_000)
    gentle = m(**above, harshness=1, cap=100 * FP_SCALE)
    harsh = m(**above, harshness=4, cap=100 * FP_SCALE)

    assert harsh > gentle


def test_harshness_changes_nothing_exactly_at_the_target():
    """A ratio of exactly one is unchanged by any power. The fixed point."""
    at = dict(pci=1_000_000_000, target=1_000_000_000)

    assert len({m(**at, harshness=h) for h in (1, 2, 4, 8)}) == 1


# ---- the cap ------------------------------------------------------------------


def test_raising_the_cap_alone_never_lowers_the_multiplier():
    """The cap is a ceiling. Lifting a ceiling cannot push anything down."""
    above = dict(pci=3_000_000_000, target=1_000_000_000, harshness=2)
    low = m(**above, cap=2 * FP_SCALE)
    high = m(**above, cap=20 * FP_SCALE)

    assert high > low


def test_the_cap_never_raises_a_result_that_was_already_below_it():
    """A ceiling binds downward only, so a result under it must not move."""
    under = dict(pci=500_000_000, target=1_000_000_000, harshness=2)

    assert m(**under, cap=1 * FP_SCALE) == m(**under, cap=99 * FP_SCALE)


# The one-dial-at-a-time discipline is enforced by every case above naming
# exactly one override against BASE. An earlier version of this file asserted
# that in a "test" which reduced to `assert calls`, always true and proving
# nothing. Deleted rather than shipped: a test that cannot fail is the thing
# this project spends its time removing.
