"""What the twin-proof gate must refuse.

Two independently written scoring engines must produce identical bytes before
anything is published. The subtle requirement is the one this project has already
been caught by once: a gate that is skipped when the second implementation is
absent is not a gate. Its absence must refuse, loudly, rather than quietly
reducing the validator to one opinion.
"""

import unittest

from ..twin import (
    SecondOpinionUnavailable,
    TwinProofFailed,
    require_agreement,
)


def both_say(text):
    return lambda _fixture: text


def raises(error):
    def second(_fixture):
        raise error

    return second


FIXTURE = {"schema": "noah-fixture/4", "name": "x"}


class WhenTheyAgree(unittest.TestCase):
    def test_identical_bytes_pass_and_the_output_comes_back(self):
        out = require_agreement(FIXTURE, first=both_say("same"), second=both_say("same"))
        self.assertEqual(out, "same")


class WhenTheyDisagree(unittest.TestCase):
    def test_any_difference_refuses(self):
        with self.assertRaises(TwinProofFailed):
            require_agreement(FIXTURE, first=both_say("a"), second=both_say("b"))

    def test_a_single_differing_byte_refuses(self):
        # Not a tolerance. One byte is a consensus split.
        with self.assertRaises(TwinProofFailed):
            require_agreement(
                FIXTURE, first=both_say("weight:1000"), second=both_say("weight:1001")
            )

    def test_the_refusal_shows_where_they_first_differ(self):
        with self.assertRaises(TwinProofFailed) as caught:
            require_agreement(FIXTURE, first=both_say("abcdef"), second=both_say("abcXef"))
        message = str(caught.exception)
        self.assertIn("3", message)  # the offset of the first difference

    def test_the_refusal_declines_to_name_either_engine_as_the_right_one(self):
        """Neither is the reference, and the message must say so.

        An earlier version of this asserted the word "correct" was absent, which
        failed against a message reading "neither is treated as correct". That
        was testing my wording rather than my meaning: the property that matters
        is that the refusal refuses to pick a side, not that a particular word
        is missing.
        """
        with self.assertRaises(TwinProofFailed) as caught:
            require_agreement(FIXTURE, first=both_say("a"), second=both_say("b"))
        lowered = str(caught.exception).lower()
        self.assertIn("neither", lowered)
        # It must not send the operator to fix a named one.
        for verdict in ("the primary is right", "the second is right", "use the primary"):
            self.assertNotIn(verdict, lowered)


class WhenTheSecondIsMissing(unittest.TestCase):
    """The failure this project has already been caught by once.

    A safety method was looked up softly and skipped when absent, which made it
    optional on a money path, and removing it left every test passing. So the
    absence of the second implementation must refuse by name.
    """

    def test_a_second_implementation_that_will_not_run_refuses(self):
        with self.assertRaises(SecondOpinionUnavailable):
            require_agreement(
                FIXTURE, first=both_say("a"), second=raises(FileNotFoundError("no node"))
            )

    def test_it_is_not_treated_as_agreement(self):
        # The dangerous version: absent second opinion, first one published.
        with self.assertRaises(SecondOpinionUnavailable) as caught:
            require_agreement(FIXTURE, first=both_say("a"), second=raises(OSError("boom")))
        self.assertNotIsInstance(caught.exception, TwinProofFailed)

    def test_the_refusal_says_the_validator_is_down_to_one_opinion(self):
        with self.assertRaises(SecondOpinionUnavailable) as caught:
            require_agreement(FIXTURE, first=both_say("a"), second=raises(OSError("boom")))
        self.assertIn("one", str(caught.exception).lower())

    def test_a_second_that_returns_nothing_refuses_rather_than_matching_nothing(self):
        # Emptiness satisfies almost every check. An empty second output must
        # not be allowed to agree with an empty first one by coincidence.
        for empty in ("", None):
            with self.subTest(empty=empty):
                with self.assertRaises(SecondOpinionUnavailable):
                    require_agreement(FIXTURE, first=both_say(""), second=both_say(empty))

    def test_a_first_that_returns_nothing_also_refuses(self):
        with self.assertRaises(SecondOpinionUnavailable):
            require_agreement(FIXTURE, first=both_say(""), second=both_say("a"))


class TheGateCannotBeTurnedOff(unittest.TestCase):
    def test_there_is_no_argument_that_skips_the_comparison(self):
        import inspect

        names = [p.lower() for p in inspect.signature(require_agreement).parameters]
        for forbidden in ("skip", "optional", "enabled", "strict", "allow_missing"):
            self.assertNotIn(forbidden, names)


if __name__ == "__main__":
    unittest.main()
