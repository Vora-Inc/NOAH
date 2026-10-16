"""Confirming a run, and refusing to call an empty one clean.

The interesting tests here are the ones about counting rather than about
arithmetic. A checker that reports "no exceptions" is only worth something
alongside "out of how many", and every way of reaching a clean report without
having checked anything is held closed below.
"""

import unittest

from ..landed import (
    FULL_SCALE,
    NothingToCheck,
    Report,
    check_previous_reveal,
    confirm_one,
    confirm_run,
)
from ..rescale import predict_stored

# The real reveal: committed at block 5,814 of the local Phase 0 chain, genesis
# 0x933bd644, read from storage at 5,955.
EMITTED = [20000, 12000, 9000, 7000, 6000, 5000, 4000, 1535, 1000]
STORED = [65535, 39321, 29491, 22937, 19661, 16384, 13107, 5030, 3277]


def good(block):
    return (block, EMITTED, STORED)


class OnePeriod(unittest.TestCase):
    def test_a_real_period_passes(self):
        self.assertTrue(confirm_one(block=5814, emitted=EMITTED, stored=STORED).ok)

    def test_comparing_against_our_own_numbers_would_have_failed_this(self):
        """The mistake this module exists to prevent, held as a test.

        A checker comparing the chain's vector to ours rejects a period that is
        completely healthy. Asserted here so nobody 'simplifies' the comparison
        back to the obvious one.
        """
        self.assertNotEqual(STORED, EMITTED)
        self.assertTrue(confirm_one(block=5814, emitted=EMITTED, stored=STORED).ok)

    def test_a_vector_that_does_not_total_the_scale_fails(self):
        short = list(EMITTED)
        short[-1] -= 1
        result = confirm_one(block=1, emitted=short, stored=predict_stored(short).stored)
        self.assertFalse(result.ok)
        self.assertIn("65535", result.reason)

    def test_a_chain_that_altered_one_weight_fails_and_names_the_seat(self):
        tampered = list(STORED)
        tampered[3] += 1
        result = confirm_one(block=1, emitted=EMITTED, stored=tampered)
        self.assertFalse(result.ok)
        self.assertIn("seat 3", result.reason)

    def test_a_chain_holding_a_different_number_of_seats_fails(self):
        result = confirm_one(block=1, emitted=EMITTED, stored=STORED[:-1])
        self.assertFalse(result.ok)
        self.assertIn("8 weights", result.reason)

    def test_an_unread_vector_is_not_a_pass(self):
        self.assertFalse(confirm_one(block=1, emitted=EMITTED, stored=None).ok)
        self.assertFalse(confirm_one(block=1, emitted=None, stored=STORED).ok)


class ARunCannotBeCleanWithoutCheckingSomething(unittest.TestCase):
    def test_an_empty_run_refuses_rather_than_passing(self):
        with self.assertRaises(NothingToCheck):
            confirm_run([])

    def test_a_run_of_unreadable_periods_is_not_clean(self):
        """Every read failed. The old shape of this bug reports success.

        Paired with a genuinely clean run in the same test, so this cannot pass
        by `clean` being hardcoded false.
        """
        blind = confirm_run([(b, None, None) for b in (1, 2, 3)])
        self.assertFalse(blind.clean)
        self.assertEqual(blind.checked, 0)
        self.assertEqual(blind.unchecked, 3)
        self.assertTrue(confirm_run([good(1), good(2)]).clean)

    def test_a_partly_unreadable_run_is_not_clean_even_with_no_failures(self):
        report = confirm_run([good(1), (2, None, None), good(3)])
        self.assertFalse(report.clean)
        self.assertEqual(report.checked, 2)
        self.assertEqual(report.unchecked, 1)
        self.assertEqual(report.failures, ())

    def test_nothing_checked_says_so_instead_of_reporting_no_exceptions(self):
        self.assertIn("NOTHING CHECKED", confirm_run([(1, None, None)]).summary())

    def test_a_clean_run_reports_how_many_and_which_blocks(self):
        summary = confirm_run([good(10), good(20), good(30)]).summary()
        self.assertIn("3 period(s) checked", summary)
        self.assertIn("blocks 10 to 30", summary)
        self.assertIn("No exceptions", summary)

    def test_no_failures_among_those_checked_is_worded_apart_from_none(self):
        summary = confirm_run([good(1), (2, None, None)]).summary()
        self.assertIn("not the same as none", summary)

    def test_every_period_handed_in_appears_in_the_results(self):
        # Nothing is silently dropped, which is the other way a run gets clean.
        entries = [good(1), (2, None, None), (3, EMITTED, list(STORED)[:-1])]
        report = confirm_run(entries)
        self.assertEqual(len(report.results), 3)
        self.assertEqual(report.blocks, (1, 2, 3))
        self.assertEqual(report.checked + report.unchecked, 3)

    def test_a_failure_is_counted_as_checked_not_as_unchecked(self):
        report = confirm_run([(1, EMITTED, list(STORED)[:-1])])
        self.assertEqual(report.checked, 1)
        self.assertEqual(report.unchecked, 0)
        self.assertEqual(len(report.failures), 1)

    def test_a_report_that_checked_nothing_is_never_clean(self):
        """Held on the report itself, not only through the run checker.

        The run checker refuses an empty run, which makes this unreachable by
        that route, and a mutation removing the guard survived the first sweep
        for exactly that reason. Report is a public type and a caller can build
        one directly. A report saying it checked nothing and found nothing wrong
        must not read as clean.

        Paired with its opposite so it cannot pass by `clean` being false always.
        """
        nothing = Report(checked=0, unchecked=0, blocks=(), failures=(), results=())
        something = Report(checked=1, unchecked=0, blocks=(7,), failures=(), results=())
        self.assertFalse(nothing.clean)
        self.assertTrue(something.clean)

    def test_a_long_clean_run_stays_clean(self):
        self.assertTrue(confirm_run([good(b) for b in range(1, 200)]).clean)


class TheScaleIsTheChainsNotOurs(unittest.TestCase):
    def test_the_scale_checked_is_the_u16_maximum(self):
        self.assertEqual(FULL_SCALE, 65535)
        self.assertEqual(sum(EMITTED), FULL_SCALE)


if __name__ == "__main__":
    unittest.main()


class Prior:
    """A chain that can be asked what it stored for the period before."""

    def __init__(self, answer):
        self.answer = answer

    def previous_period(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class WhetherToStopBeforePublishingAgain(unittest.TestCase):
    """Held here as well as in the loop, because the decision lives here.

    The loop's tests prove the wiring. These prove the rule, and a mutation
    sweep run only against this file could not see the loop's tests at all.
    """

    def test_a_proven_mismatch_says_stop(self):
        wrong = list(STORED)
        wrong[2] += 1
        check = check_previous_reveal(Prior((5814, EMITTED, wrong)))
        self.assertTrue(check.halt)
        self.assertFalse(check.verified)

    def test_a_match_says_carry_on_and_counts_as_verified(self):
        check = check_previous_reveal(Prior((5814, EMITTED, STORED)))
        self.assertFalse(check.halt)
        self.assertTrue(check.verified)

    def test_an_unreadable_previous_period_does_not_stop_the_validator(self):
        """Absence of the check is not evidence of a fault.

        Stopping here would trade a small risk for a much larger one: this
        validator stays audible only while its commits land, so a halt on a
        chain hiccup runs out the activity window and takes the subnet's pay
        to zero.
        """
        check = check_previous_reveal(Prior(RuntimeError("archive down")))
        self.assertFalse(check.halt)
        self.assertFalse(check.verified)

    def test_half_the_evidence_missing_is_not_a_mismatch(self):
        for answer in ((5814, EMITTED, None), (5814, None, STORED)):
            with self.subTest(answer=answer):
                check = check_previous_reveal(Prior(answer))
                self.assertFalse(check.halt)
                self.assertFalse(check.verified)

    def test_a_first_run_is_not_a_clean_check(self):
        check = check_previous_reveal(Prior(None))
        self.assertFalse(check.halt)
        self.assertFalse(check.verified)

    def test_the_stop_message_says_what_stopping_costs(self):
        wrong = list(STORED)
        wrong[0] -= 1
        reason = check_previous_reveal(Prior((5814, EMITTED, wrong))).reason
        self.assertIn("PUBLISHING STOPS HERE", reason)
        self.assertIn("pay to zero", reason)
