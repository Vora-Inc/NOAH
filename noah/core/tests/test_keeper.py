"""When the activity keeper engages, what it publishes, and when it stops.

The keeper exists because a validator the chain stops hearing from is dropped
from consensus, and the launch period is the one time every score is legitimately
zero. So the danger and the thing that protects against it arrive together.

The rule it must never break: nothing the keeper publishes may put weight on a
seat we control. The chain counts that as destroyed value, publishes the figure,
and scales the subnet's own income down by it.
"""

import unittest

from ..keeper import InvalidScore, KeeperWouldPayOwner, decide


class WhenItEngages(unittest.TestCase):
    def test_a_period_where_nobody_earned_engages_the_keeper(self):
        outcome = decide(scored={}, live_uids=[0, 1, 2], owner_uids=set())
        self.assertTrue(outcome.keeping_alive)
        self.assertEqual(outcome.vector, {0: 0, 1: 0, 2: 0})

    def test_an_all_zero_answer_engages_it_too(self):
        # The engine can return zeros rather than nothing at all, and both mean
        # the same thing: no one earned.
        outcome = decide(scored={0: 0, 1: 0}, live_uids=[0, 1], owner_uids=set())
        self.assertTrue(outcome.keeping_alive)

    def test_it_names_every_live_seat_including_ones_the_engine_never_mentioned(self):
        outcome = decide(scored={}, live_uids=[3, 9, 14], owner_uids=set())
        self.assertEqual(sorted(outcome.vector), [3, 9, 14])

    def test_it_says_why_in_words_an_operator_can_act_on(self):
        outcome = decide(scored={}, live_uids=[0], owner_uids=set())
        self.assertIn("nobody earned", outcome.reason.lower())


class WhenItStops(unittest.TestCase):
    def test_a_single_earner_stops_the_keeper(self):
        outcome = decide(scored={0: 5, 1: 0}, live_uids=[0, 1], owner_uids=set())
        self.assertFalse(outcome.keeping_alive)
        self.assertEqual(outcome.vector, {0: 5, 1: 0})

    def test_stopping_still_names_every_live_seat(self):
        # Seats that earned nothing stay in the vector at zero. This is the
        # behaviour the standard client breaks by deleting them.
        outcome = decide(scored={0: 5}, live_uids=[0, 1, 2], owner_uids=set())
        self.assertEqual(outcome.vector, {0: 5, 1: 0, 2: 0})

    def test_the_smallest_possible_earning_stops_it(self):
        outcome = decide(scored={1: 1}, live_uids=[0, 1], owner_uids=set())
        self.assertFalse(outcome.keeping_alive)


class TheOwnerSeatRule(unittest.TestCase):
    """The rule that must never break, in either mode."""

    def test_a_keeper_publication_over_an_owner_seat_is_allowed_at_zero(self):
        # Zero weight on our own seat is not payment. Naming it is required,
        # because every live seat must appear.
        outcome = decide(scored={}, live_uids=[0, 1], owner_uids={1})
        self.assertEqual(outcome.vector, {0: 0, 1: 0})

    def test_any_weight_on_an_owner_seat_refuses_even_when_earned(self):
        with self.assertRaises(KeeperWouldPayOwner) as caught:
            decide(scored={1: 500}, live_uids=[0, 1], owner_uids={1})
        self.assertIn("1", str(caught.exception))

    def test_the_refusal_explains_the_cost_rather_than_just_forbidding_it(self):
        with self.assertRaises(KeeperWouldPayOwner) as caught:
            decide(scored={1: 500}, live_uids=[0, 1], owner_uids={1})
        lowered = str(caught.exception).lower()
        self.assertIn("burn", lowered)

    def test_it_refuses_rather_than_quietly_zeroing_the_owner_seat(self):
        # Silently correcting would hide that something upstream put weight
        # there, which is the thing actually worth knowing.
        with self.assertRaises(KeeperWouldPayOwner):
            decide(scored={0: 100, 1: 500}, live_uids=[0, 1], owner_uids={1})


class ScoresThatAreNotPlainIntegers(unittest.TestCase):
    """int(x) would truncate a float, parse a string, and accept a bool.

    Every value on a scoring path is required to already be a plain integer
    [AGENTS.md #1]. A value that is not one reaching the keeper means scoring
    itself already broke that rule, and silently coercing it here would
    publish a weight nobody actually computed instead of surfacing the fault.
    """

    def test_a_float_score_is_refused_rather_than_truncated(self):
        # int(1.9) == 1 would publish a weight that was never actually 1.
        with self.assertRaises(InvalidScore):
            decide(scored={0: 1.9}, live_uids=[0], owner_uids=set())

    def test_a_string_score_is_refused_rather_than_parsed(self):
        # int("1") == 1 would accept a value that was never an integer type
        # at all, only something that happened to look like one.
        with self.assertRaises(InvalidScore):
            decide(scored={0: "1"}, live_uids=[0], owner_uids=set())

    def test_a_bool_score_is_refused_even_though_bool_is_an_int_subclass(self):
        # int(True) == 1 in Python. A scoring engine has no legitimate reason
        # to hand back a bool, so this must not quietly become a weight of 1.
        with self.assertRaises(InvalidScore):
            decide(scored={0: True}, live_uids=[0], owner_uids=set())

    def test_a_score_above_the_u16_weight_range_is_refused(self):
        with self.assertRaises(InvalidScore):
            decide(scored={0: 65536}, live_uids=[0], owner_uids=set())

    def test_a_negative_score_is_refused(self):
        with self.assertRaises(InvalidScore):
            decide(scored={0: -1}, live_uids=[0], owner_uids=set())

    def test_the_refusal_names_the_seat_and_the_value(self):
        with self.assertRaises(InvalidScore) as caught:
            decide(scored={7: 1.9}, live_uids=[7], owner_uids=set())
        message = str(caught.exception)
        self.assertIn("7", message)
        self.assertIn("1.9", message)

    def test_a_custom_int_subclass_is_refused_even_when_isinstance_int_is_true(self):
        """isinstance(x, int) is true for bool AND for any other int subclass.

        A custom int whose __bool__ lies about its own value would otherwise
        slip past this check (isinstance says yes, it is not literally bool)
        and then defeat the owner-seat guard below: `if weight` on a
        FalseInt(5) is False even though the seat's real weight is 5, so a
        nonzero weight on an owner-controlled seat would go unnoticed and
        publish. type(value) is not int is the only check neither bypasses.
        """

        class FalseInt(int):
            def __bool__(self):
                return False

        with self.assertRaises(InvalidScore):
            decide(scored={0: FalseInt(5)}, live_uids=[0], owner_uids=set())

    def test_the_custom_int_subclass_refusal_actually_prevents_the_owner_payout(self):
        """The end-to-end proof, not just the type check in isolation.

        Before this was type-exact, this exact input silently produced
        keeping_alive=True with weight 5 sitting on an owner-controlled seat,
        because `if weight` (bool(FalseInt(5)) is False) never saw it. Now it
        must refuse instead of quietly publishing.
        """

        class FalseInt(int):
            def __bool__(self):
                return False

        with self.assertRaises(InvalidScore):
            decide(scored={1: FalseInt(5)}, live_uids=[0, 1], owner_uids={1})


class NothingToPublish(unittest.TestCase):
    def test_a_subnet_with_no_live_seats_publishes_nothing_and_says_so(self):
        # Publishing an empty vector is not keeping alive, it is publishing
        # nothing, and the difference must not be blurred.
        outcome = decide(scored={}, live_uids=[], owner_uids=set())
        self.assertEqual(outcome.vector, {})
        self.assertFalse(outcome.keeping_alive)
        self.assertIn("no live seats", outcome.reason.lower())


if __name__ == "__main__":
    unittest.main()
