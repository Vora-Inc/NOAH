"""What must happen when the scoring rules change under a running validator.

The failure this prevents is the quietest one available: a validator running last
month's rules keeps publishing, its numbers differ from everybody else's, and
consensus punishes it for disagreeing. Nothing errors. It simply earns less and
nobody can say why without diffing two versions of the arithmetic.
"""

import unittest

from ..version import (
    OUR_VERSION_KEY,
    ScoringVersionTooOld,
    check_version_current,
    version_key_for,
)


class OurKey(unittest.TestCase):
    def test_our_key_is_a_positive_whole_number(self):
        self.assertIsInstance(OUR_VERSION_KEY, int)
        self.assertGreater(OUR_VERSION_KEY, 0)

    def test_the_key_is_derived_from_the_rules_and_is_stable(self):
        # Same rules, same key, every time and on every machine. If this were
        # derived from anything that varies, two validators running identical
        # code would tag their work differently.
        self.assertEqual(version_key_for("noah-fixture/4"), version_key_for("noah-fixture/4"))

    def test_different_rules_give_a_different_key(self):
        # The whole point: a change to the rules must be visible as a change to
        # the key, or the announcement mechanism announces nothing.
        self.assertNotEqual(version_key_for("noah-fixture/4"), version_key_for("noah-fixture/5"))


class RefusingWhenStale(unittest.TestCase):
    def test_a_current_validator_passes(self):
        self.assertIsNone(check_version_current(required=OUR_VERSION_KEY))

    def test_a_validator_ahead_of_the_subnet_passes(self):
        # Being newer is fine: the subnet raises its floor when it is ready, and
        # a validator that upgraded early is not wrong.
        self.assertIsNone(check_version_current(required=OUR_VERSION_KEY - 1))

    def test_a_stale_validator_refuses_rather_than_publishing_different_numbers(self):
        with self.assertRaises(ScoringVersionTooOld) as caught:
            check_version_current(required=OUR_VERSION_KEY + 1)
        message = str(caught.exception)
        self.assertIn(str(OUR_VERSION_KEY), message)
        self.assertIn(str(OUR_VERSION_KEY + 1), message)

    def test_the_refusal_says_upgrade_rather_than_describing_a_fault(self):
        with self.assertRaises(ScoringVersionTooOld) as caught:
            check_version_current(required=OUR_VERSION_KEY + 1)
        self.assertIn("upgrade", str(caught.exception).lower())

    def test_an_unreadable_requirement_refuses_rather_than_assuming_it_is_met(self):
        # A subnet that will not say what it requires is not a subnet whose
        # requirement we have met. Assuming otherwise is how a stale validator
        # keeps running through exactly the event this exists to catch.
        for answer in (None, "", "not-a-number"):
            with self.subTest(answer=answer):
                with self.assertRaises(ScoringVersionTooOld):
                    check_version_current(required=answer)


if __name__ == "__main__":
    unittest.main()
