"""Which height to score at, and the two ways I got it wrong first.

Both properties below were claimed in a docstring before they were checked, and
neither held. They are here as tests because a pinning rule that is nearly right
produces a validator that disagrees with its peers, earns less, and logs
nothing.
"""

import unittest

from ..pinning import NoSettledPeriod, period_bounds, period_of, pin_height, settle_periods_for

TEMPO = 20


class EveryHeadInsideAPeriodPinsTheSameThing(unittest.TestCase):
    """The first mistake: a margin measured in blocks flips mid-period.

    `head // tempo` is constant across a period; a comparison of the head
    against a block margin is not. Two validators a block apart on either side
    of that flip pin different heights, publish different numbers, and are both
    punished for disagreeing.
    """

    def test_the_answer_does_not_move_within_a_period(self):
        """Margins that are NOT whole periods are the ones that expose this.

        A block-based rule with a margin of exactly one period flips on the
        period boundary and looks constant. Twenty-five blocks over a
        twenty-block period flips in the middle of one, which is where two
        validators a block apart end up disagreeing.
        """
        for start in (8_900, 8_920, 9_000):
            for settle in (13, 20, 25, 37):
                with self.subTest(period_starting=start, settle_blocks=settle):
                    pinned = {
                        pin_height(head=h, tempo=TEMPO, settle_blocks=settle)
                        for h in range(start, start + TEMPO)
                    }
                    self.assertEqual(len(pinned), 1)

    def test_consecutive_periods_pin_consecutive_boundaries(self):
        first = pin_height(head=8_900, tempo=TEMPO, settle_blocks=20)
        second = pin_height(head=8_920, tempo=TEMPO, settle_blocks=20)
        self.assertEqual(second - first, TEMPO)

    def test_a_pinned_height_is_always_the_last_block_of_a_period(self):
        for head in range(8_900, 8_960):
            with self.subTest(head=head):
                pinned = pin_height(head=head, tempo=TEMPO, settle_blocks=20)
                self.assertEqual((pinned + 1) % TEMPO, 0)


class TheMarginIsDeliveredAtTheWorstHeadNotTheBest(unittest.TestCase):
    """The second mistake: dividing the margin by the period delivers less.

    Ceiling division of twenty blocks by a twenty-block period gives one period,
    and at the first head of that window the pinned period ended ONE block
    earlier. The number was accepted and the margin was not delivered.
    """

    def test_the_requested_margin_holds_at_every_head(self):
        for settle in (0, 1, 2, 5, 20, 25, 40, 100):
            with self.subTest(settle_blocks=settle):
                worst = min(
                    head - pin_height(head=head, tempo=TEMPO, settle_blocks=settle)
                    for head in range(8_900, 8_960)
                )
                self.assertGreaterEqual(worst, settle)

    def test_a_margin_of_one_period_is_not_enough_for_a_period_long_margin(self):
        # The specific arithmetic that was wrong. Held so it cannot come back.
        self.assertEqual(settle_periods_for(settle_blocks=20, tempo=20), 2)

    def test_a_margin_shorter_than_a_period_still_waits_at_least_one(self):
        self.assertEqual(settle_periods_for(settle_blocks=0, tempo=20), 1)
        self.assertEqual(settle_periods_for(settle_blocks=1, tempo=20), 1)

    def test_a_larger_margin_never_pins_a_later_height(self):
        heights = [pin_height(head=9_000, tempo=TEMPO, settle_blocks=s) for s in range(0, 80)]
        self.assertEqual(heights, sorted(heights, reverse=True))


class WhatItRefuses(unittest.TestCase):
    def test_a_chain_too_young_to_have_a_settled_period_refuses(self):
        with self.assertRaises(NoSettledPeriod):
            pin_height(head=5, tempo=TEMPO, settle_blocks=20)

    def test_it_does_not_reach_further_back_to_find_an_answer(self):
        # Answering here would hand back a height some validators had scored and
        # others had not, which is the disagreement pinning exists to prevent.
        with self.assertRaises(NoSettledPeriod) as caught:
            pin_height(head=5, tempo=TEMPO, settle_blocks=20)
        self.assertIn("does NOT reach further back", str(caught.exception))

    def test_a_period_of_zero_blocks_refuses(self):
        with self.assertRaises(NoSettledPeriod):
            pin_height(head=9_000, tempo=0, settle_blocks=20)

    def test_a_negative_margin_refuses(self):
        with self.assertRaises(NoSettledPeriod):
            pin_height(head=9_000, tempo=TEMPO, settle_blocks=-1)


class LocatingAPeriod(unittest.TestCase):
    def test_a_height_maps_to_the_period_holding_it(self):
        self.assertEqual(period_of(height=8_899, tempo=TEMPO), 444)
        self.assertEqual(period_of(height=8_900, tempo=TEMPO), 445)

    def test_bounds_are_inclusive_and_meet_without_a_gap(self):
        first, last = period_bounds(period=444, tempo=TEMPO)
        self.assertEqual((first, last), (8_880, 8_899))
        self.assertEqual(period_bounds(period=445, tempo=TEMPO)[0], last + 1)

    def test_a_pinned_height_is_the_last_block_of_its_own_period(self):
        pinned = pin_height(head=8_905, tempo=TEMPO, settle_blocks=20)
        self.assertEqual(
            period_bounds(period=period_of(height=pinned, tempo=TEMPO), tempo=TEMPO)[1], pinned
        )


if __name__ == "__main__":
    unittest.main()
