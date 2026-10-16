"""What the chain stores, against what we send it.

The anchor test here is a real reveal, not a constructed case. Everything else
is arithmetic that could be argued about; the fixture is what the chain actually
did with a vector we actually published.

The second thing held here is the threshold. The runtime carries two formulas and
switches between them at a largest weight of 32,768, and they do not agree. A
predictor using only the ordinary one is right on ordinary vectors and wrong on
exactly the vectors where one participant dominates the subnet.
"""

import unittest

from ..rescale import (
    THRESHOLD,
    U16_MAX,
    EmittedVectorRefused,
    _divide,
    _fixed,
    _multiply,
    _round_half_up,
    _to_u16,
    predict_stored,
)

# Committed at block 5,814 of the local Phase 0 chain, genesis 0x933bd644, and
# read back from storage at block 5,955 after the reveal had run.
COMMITTED = [20000, 12000, 9000, 7000, 6000, 5000, 4000, 1535, 1000]
STORED = [65535, 39321, 29491, 22937, 19661, 16384, 13107, 5030, 3277]


def multiply_first(values):
    """The formula the runtime uses below the threshold, applied unconditionally.

    Here so a test can show what a predictor ignoring the threshold would have
    produced, rather than asserting the difference in a comment.
    """
    fixed = [_fixed(v) for v in values]
    largest = max(fixed)
    scale = _fixed(U16_MAX)
    return [_to_u16(_round_half_up(_divide(_multiply(e, scale), largest))) for e in fixed]


class TheChainsOwnAnswer(unittest.TestCase):
    def test_a_real_reveal_is_reproduced_entry_for_entry(self):
        self.assertEqual(list(predict_stored(COMMITTED).stored), STORED)

    def test_the_sum_is_not_preserved_and_that_is_the_whole_point(self):
        # Ours sums to the full scale by construction; the stored one does not.
        # A check comparing the two sums would fail every period on a healthy
        # validator, which is the mistake this module exists to prevent.
        self.assertEqual(sum(COMMITTED), 65535)
        self.assertEqual(sum(predict_stored(COMMITTED).stored), 214743)

    def test_the_half_case_settles_the_rounding_rule(self):
        # 6000 scales to exactly 19660.5. Rounding half up gives 19661, which is
        # what the chain stored. Rounding half to even gives 19660. Without an
        # entry landing on a half, both rules fit the same evidence.
        self.assertEqual(predict_stored(COMMITTED).stored[4], 19661)


class TheThresholdIsLoadBearing(unittest.TestCase):
    """Both formulas, and proof that choosing wrongly changes the answer."""

    DOMINANT = [40000, 15535, 10000]

    def test_a_dominant_seat_takes_the_dividing_formula(self):
        self.assertTrue(predict_stored(self.DOMINANT).divides_first)

    def test_an_ordinary_vector_takes_the_multiplying_formula(self):
        self.assertFalse(predict_stored(COMMITTED).divides_first)

    def test_the_two_formulas_disagree_on_a_dominant_vector(self):
        """If they agreed, the threshold would be decoration and so would this.

        The naive formula overflows the runtime's fixed-point type here, so it
        does not merely round differently. It saturates, and the largest entry
        comes out below the full scale, which is the one thing a max-upscale is
        defined to never do.
        """
        chain = list(predict_stored(self.DOMINANT).stored)
        naive = multiply_first(self.DOMINANT)
        self.assertNotEqual(chain, naive)
        self.assertEqual(chain[0], U16_MAX)
        self.assertLess(naive[0], U16_MAX)

    def test_the_boundary_sits_where_the_runtime_puts_it(self):
        at = [THRESHOLD, 65535 - THRESHOLD]
        over = [THRESHOLD + 1, 65535 - THRESHOLD - 1]
        self.assertFalse(predict_stored(at).divides_first)
        self.assertTrue(predict_stored(over).divides_first)


class PropertiesThatMustHoldEitherWay(unittest.TestCase):
    def test_the_largest_weight_always_becomes_the_full_scale(self):
        # The defining property of a max-upscale, held across both formulas.
        for top in range(1, 65536, 61):
            rest = 65535 - top
            values = [top, rest // 2, rest - rest // 2] if rest else [top]
            with self.subTest(top=top):
                self.assertEqual(max(predict_stored(values).stored), U16_MAX)

    def test_zeros_keep_their_positions(self):
        values = [0, 30000, 0, 20000, 0, 15535]
        stored = predict_stored(values).stored
        self.assertEqual([i for i, v in enumerate(stored) if v == 0], [0, 2, 4])

    def test_an_all_zero_vector_stays_all_zero(self):
        """The activity keeper publishes zeroes to stay audible.

        Asserted with a non-zero control, because an implementation that always
        returned zeroes would pass the first half of this on its own.
        """
        self.assertEqual(list(predict_stored([0, 0, 0, 0]).stored), [0, 0, 0, 0])
        self.assertNotEqual(list(predict_stored([0, 0, 0, 1]).stored), [0, 0, 0, 0])

    def test_the_same_vector_gives_the_same_answer(self):
        self.assertEqual(predict_stored(COMMITTED).stored, predict_stored(COMMITTED).stored)

    def test_a_single_seat_takes_the_whole_scale(self):
        self.assertEqual(list(predict_stored([65535]).stored), [65535])


class WhatItRefuses(unittest.TestCase):
    def test_a_weight_above_the_chains_range_refuses(self):
        with self.assertRaises(EmittedVectorRefused):
            predict_stored([70000, 100])

    def test_a_negative_weight_refuses(self):
        with self.assertRaises(EmittedVectorRefused):
            predict_stored([-1, 100])

    def test_a_non_integer_refuses_rather_than_being_coerced(self):
        with self.assertRaises(EmittedVectorRefused):
            predict_stored([1.5, 100])

    def test_a_boolean_is_not_an_integer_here(self):
        # True would otherwise pass every integer check and scale as 1.
        with self.assertRaises(EmittedVectorRefused):
            predict_stored([True, 100])

    def test_an_empty_vector_refuses_rather_than_modelling_a_path_we_never_take(self):
        with self.assertRaises(EmittedVectorRefused):
            predict_stored([])


if __name__ == "__main__":
    unittest.main()
