"""What publishing must do, and what it must refuse to believe.

The sealing spike proved a chain accepts what we build. This is that path as a
component, and the difference is that none of its checks is optional. A script
protects whoever remembers to run it. A component protects the validator.
"""

import unittest

from ..commits import LIVE_MAP
from ..publish import (
    CommitRejected,
    NotOnChain,
    PublicationRefused,
    publish_weights,
    publish_weights_direct,
)


class FalseInt(int):
    """An int subclass whose truth test lies about its own nonzero value.

    isinstance(FalseInt(5), int) is True and it is not literally bool, so a
    check that only excludes bool would still accept it. Used to prove the
    sealing step refuses by exact type rather than merely by isinstance.
    """

    def __bool__(self):
        return False


class Chain:
    """A stand-in chain that records what it was asked to do."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.submitted = []
        self.stored = list(behaviour.get("stored", []))

    def current_block(self):
        return self.b.get("current_block", 1000)

    def last_update(self, uid, netuid):
        return self.b.get("last_update", 0)

    def rate_limit(self, netuid):
        return self.b.get("rate_limit", 100)

    def seal(self, uids, values, hotkey):
        if isinstance(self.b.get("seal"), Exception):
            raise self.b["seal"]
        # Recorded exactly, so a test can assert on what was handed to sealing
        # rather than on a string that happens to contain the right characters.
        self.sealed_uids = list(uids)
        self.sealed_values = list(values)
        return b"sealed:" + bytes(str(list(zip(uids, values))), "utf8"), 999

    def submit(self, commit, reveal_round, netuid):
        error = self.b.get("submit_error")
        if error:
            if self.b.get("submit_indeterminate"):
                return {"error": error, "block": None, "indeterminate": True}
            return {"error": error}
        self.submitted.append((commit, reveal_round))
        if self.b.get("store_on_submit", True):
            self.stored.append(commit)
        return {"error": None, "block": 1001}

    def read_map(self, name, netuid):
        if isinstance(self.b.get("stored_commits"), Exception):
            raise self.b["stored_commits"]
        # Only the live map holds anything; the legacy maps a lost reader would
        # land on are empty, which is what makes an empty answer here real.
        return list(self.stored) if name == LIVE_MAP else list(self.b.get(name, []))

    def submit_direct(self, uids, values, netuid):
        error = self.b.get("submit_direct_error")
        if error:
            return {"error": error, "block": None}
        self.direct_uids = list(uids)
        self.direct_values = list(values)
        self.submitted.append(("direct", uids, values))
        return {"error": None, "block": self.b.get("direct_block", 1001)}

    def confirm_direct(self, *, vector, result):
        self.confirm_direct_calls = getattr(self, "confirm_direct_calls", [])
        self.confirm_direct_calls.append((vector, result["block"]))
        applied = self.b.get("applied_weights", True)
        if isinstance(applied, Exception):
            raise applied
        return {"confirmed": applied, "block": result["block"]}


VECTOR = {0: 30000, 1: 20000, 2: 15535, 3: 0}


def run(chain, vector=VECTOR, **kw):
    return publish_weights(chain, vector=vector, uid=1, netuid=2, hotkey=b"\x01" * 32, **kw)


def run_direct(chain, vector=VECTOR, **kw):
    return publish_weights_direct(
        chain, vector=vector, uid=1, netuid=2, hotkey=b"\x01" * 32, **kw
    )


class TheHappyPath(unittest.TestCase):
    def test_it_publishes_and_reports_what_the_chain_holds(self):
        chain = Chain()
        result = run(chain)
        self.assertEqual(len(chain.submitted), 1)
        self.assertEqual(result["reveal_round"], 999)
        self.assertTrue(result["confirmed_on_chain"])

    def test_every_seat_reaches_sealing_including_the_ones_scoring_zero(self):
        """The failure this whole component exists to prevent.

        An earlier version of this test asserted the character "0" appeared in
        the sealed bytes, which is true whether or not the zero-scoring seat
        survives, because other values contain zeros. A mutation that dropped
        those seats passed it. The assertion is now on the seats and values
        handed to sealing, exactly.
        """
        chain = Chain()
        run(chain)
        self.assertEqual(chain.sealed_uids, [0, 1, 2, 3])
        self.assertEqual(chain.sealed_values, [30000, 20000, 15535, 0])

    def test_an_all_zero_vector_still_publishes_with_every_seat_named(self):
        # The activity keeper depends on this. The standard client refuses it
        # outright, and a version that merely dropped the zeros would seal an
        # empty vector, so the seat list is asserted rather than the outcome.
        chain = Chain()
        result = run(chain, vector={0: 0, 1: 0, 2: 0})
        self.assertEqual(len(chain.submitted), 1)
        self.assertEqual(chain.sealed_uids, [0, 1, 2])
        self.assertEqual(chain.sealed_values, [0, 0, 0])
        self.assertTrue(result["confirmed_on_chain"])


class ValuesThatAreNotPlainU16Integers(unittest.TestCase):
    """The last checkpoint before sealing and submitting to the chain.

    keeper.py validates its own output, but publish_weights is reachable by
    any caller whether or not it went through keeper.decide() first, so this
    is where a bad value must be caught regardless of what built the vector.
    """

    def test_a_float_value_is_refused_rather_than_truncated(self):
        with self.assertRaises(PublicationRefused):
            run(Chain(), vector={0: 1.9})

    def test_a_string_value_is_refused_rather_than_parsed(self):
        with self.assertRaises(PublicationRefused):
            run(Chain(), vector={0: "1"})

    def test_a_bool_value_is_refused_even_though_bool_is_an_int_subclass(self):
        with self.assertRaises(PublicationRefused):
            run(Chain(), vector={0: True})

    def test_a_custom_int_subclass_is_refused_even_when_isinstance_int_is_true(self):
        # isinstance(FalseInt(5), int) is True and it is not literally bool,
        # so only an exact-type check catches it before it reaches sealing.
        with self.assertRaises(PublicationRefused):
            run(Chain(), vector={0: FalseInt(5)})

    def test_a_value_above_the_u16_range_is_refused(self):
        with self.assertRaises(PublicationRefused):
            run(Chain(), vector={0: 65536})

    def test_a_negative_value_is_refused(self):
        with self.assertRaises(PublicationRefused):
            run(Chain(), vector={0: -1})

    def test_nothing_is_sealed_when_a_value_is_refused(self):
        chain = Chain()
        with self.assertRaises(PublicationRefused):
            run(chain, vector={0: 1.9})
        self.assertEqual(chain.submitted, [])
        self.assertFalse(hasattr(chain, "sealed_uids"))


class TheRateLimit(unittest.TestCase):
    def test_it_refuses_before_sealing_when_the_seat_may_not_publish_yet(self):
        # Sealing first would waste the work and, worse, produce a commit that
        # looks ready to send. The refusal comes first.
        chain = Chain(current_block=850, last_update=800, rate_limit=100)
        with self.assertRaises(PublicationRefused) as caught:
            run(chain)
        self.assertEqual(chain.submitted, [])
        self.assertIn("50", str(caught.exception))


class WhenTheChainSaysNo(unittest.TestCase):
    def test_a_rejected_submission_is_reported_and_not_treated_as_published(self):
        chain = Chain(submit_error="rate limit exceeded")
        with self.assertRaises(CommitRejected) as caught:
            run(chain)
        self.assertIn("rate limit", str(caught.exception))

    def test_a_submission_that_raised_is_unconfirmed_not_refused(self):
        """#291: a transport failure after the extrinsic may have reached the node is not
        a definite refusal; it must read as NotOnChain so the intent marker stays open."""
        chain = Chain(submit_error="TimeoutError: no answer", submit_indeterminate=True)
        with self.assertRaises(NotOnChain):
            run(chain)

    def test_a_sealing_failure_stops_the_publication(self):
        chain = Chain(seal=RuntimeError("drand unreachable"))
        with self.assertRaises(PublicationRefused):
            run(chain)
        self.assertEqual(chain.submitted, [])


class TheReadBack(unittest.TestCase):
    """The check that is meant to catch everything, and was itself broken once.

    It read the wrong storage location and reported a healthy publication as
    missing. It fails closed, so it would have halted a working validator every
    period while looking like caution.
    """

    def test_a_submission_the_chain_did_not_store_is_refused(self):
        # "The call succeeded" and "what arrived is what we sent" are different
        # facts, which is the sentence this whole check exists for.
        chain = Chain(store_on_submit=False)
        with self.assertRaises(NotOnChain) as caught:
            run(chain)
        self.assertIn("not", str(caught.exception).lower())

    def test_a_chain_holding_somebody_elses_commit_is_still_refused(self):
        chain = Chain(store_on_submit=False, stored=[b"somebody-elses-commit"])
        with self.assertRaises(NotOnChain):
            run(chain)

    def test_an_empty_store_is_named_as_a_suspect_read_rather_than_a_verdict(self):
        # Every candidate location empty says more about the read than the
        # chain, so it must not be reported as "the chain refused us".
        chain = Chain(store_on_submit=False, stored=[])
        with self.assertRaises(NotOnChain) as caught:
            run(chain)
        self.assertIn("read", str(caught.exception).lower())

    def test_a_read_back_that_cannot_run_refuses_rather_than_passing(self):
        chain = Chain(stored_commits=RuntimeError("node fell over"))
        with self.assertRaises(NotOnChain):
            run(chain)

    def test_the_read_back_is_not_optional(self):
        # There is no argument that turns it off. If one is ever added, this
        # test is where somebody has to say so out loud.
        import inspect

        signature = inspect.signature(publish_weights)
        names = [p.lower() for p in signature.parameters]
        for forbidden in ("skip_readback", "verify", "check", "confirm"):
            self.assertNotIn(forbidden, names)


if __name__ == "__main__":
    unittest.main()


class TheReadBackCannotBeLostInTheWrongStorage(unittest.TestCase):
    """The confirmation now checks WHERE it looked, not just what it found.

    Four commit maps exist on this runtime and all four answer a query. Three of
    them are not where commits are filed, so a reader pointed at one returns
    empty forever, which is identical to a correct read of a chain holding
    nothing. That is how a healthy publication was once reported missing.
    """

    def test_a_confirmed_empty_still_refuses_but_no_longer_blames_the_read(self):
        chain = Chain(store_on_submit=False)
        with self.assertRaises(NotOnChain) as caught:
            publish_weights(chain, vector=VECTOR, uid=3, netuid=2, hotkey="hk")
        message = str(caught.exception)
        self.assertIn("already checked", message)
        self.assertIn("needs a person", message)

    def test_commits_sitting_in_a_legacy_map_refuse_rather_than_reading_as_none(self):
        from ..commits import LEGACY_MAPS

        chain = Chain(store_on_submit=False, **{LEGACY_MAPS[0]: ["someone-elses"]})
        with self.assertRaises(NotOnChain) as caught:
            publish_weights(chain, vector=VECTOR, uid=3, netuid=2, hotkey="hk")
        self.assertIn(LEGACY_MAPS[0], str(caught.exception))

    def test_an_ordinary_publication_is_unaffected_by_the_extra_checking(self):
        chain = Chain()
        out = publish_weights(chain, vector=VECTOR, uid=3, netuid=2, hotkey="hk")
        self.assertTrue(out["confirmed_on_chain"])


class TheDirectPathIsSeparateAndNeverAFallback(unittest.TestCase):
    """Ticket #16 (B06), issue #56 comment 5720306237.

    ``publish_weights_direct`` exists ONLY because this validator's own
    private test subnet cannot reach the external drand beacon the sealed
    path needs -- production keeps publishing through ``publish_weights``.
    The property under test here is structural: nothing routes a sealed
    failure into this function, not "we didn't happen to trigger it".
    """

    def test_publish_weights_never_mentions_the_direct_path_in_its_own_source(self):
        import inspect

        from .. import publish as publish_module

        source = inspect.getsource(publish_module.publish_weights)
        self.assertNotIn("publish_weights_direct", source)
        self.assertNotIn("submit_direct", source)

    def test_the_happy_path_submits_directly_and_reports_applied_confirmed(self):
        chain = Chain()
        result = run_direct(chain)
        self.assertEqual(len(chain.submitted), 1)
        self.assertEqual(chain.direct_uids, [0, 1, 2, 3])
        self.assertEqual(chain.direct_values, [30000, 20000, 15535, 0])
        self.assertEqual(
            result["applied_weights_readback"], {"confirmed": True, "block": 1001}
        )
        self.assertEqual(result["seats"], 4)
        self.assertEqual(result["block"], 1001)

    def test_scoring_and_applied_weights_are_never_the_same_field(self):
        # The requirement this test exists to hold: a caller must be able
        # to tell "what we intended to publish" apart from "what the chain
        # confirms it applied" by looking at distinct, named keys, not by
        # interpreting one field two ways.
        chain = Chain()
        result = run_direct(chain)
        self.assertIn("applied_weights_readback", result)
        self.assertNotIn("vector", result)
        self.assertIsInstance(result["applied_weights_readback"], dict)

    def test_it_still_refuses_before_submitting_when_the_rate_limit_has_not_cleared(self):
        chain = Chain(current_block=850, last_update=800, rate_limit=100)
        with self.assertRaises(PublicationRefused):
            run_direct(chain)
        self.assertEqual(chain.submitted, [])

    def test_it_still_validates_every_weight_exactly_as_the_sealed_path_does(self):
        with self.assertRaises(PublicationRefused):
            run_direct(Chain(), vector={0: 1.9})
        with self.assertRaises(PublicationRefused):
            run_direct(Chain(), vector={0: 65536})

    def test_a_chain_refusal_is_reported_and_not_treated_as_published(self):
        chain = Chain(submit_direct_error="rate limit exceeded")
        with self.assertRaises(CommitRejected) as caught:
            run_direct(chain)
        self.assertIn("rate limit", str(caught.exception))

    def test_an_accepted_submission_with_no_applied_event_is_refused(self):
        # The call succeeded; the chain shows no WeightsSet event for this
        # seat in that block. Accepted and applied are different facts.
        chain = Chain(applied_weights=False)
        with self.assertRaises(NotOnChain) as caught:
            run_direct(chain)
        self.assertIn("no matching", str(caught.exception).lower())

    def test_a_read_back_that_cannot_run_refuses_rather_than_passing(self):
        chain = Chain(applied_weights=RuntimeError("node fell over"))
        with self.assertRaises(NotOnChain):
            run_direct(chain)

    def test_the_read_back_looks_at_the_real_block_the_submission_landed_in(self):
        chain = Chain(direct_block=4242)
        run_direct(chain)
        self.assertEqual(chain.confirm_direct_calls, [(VECTOR, 4242)])

    def test_the_read_back_is_not_optional_here_either(self):
        import inspect

        signature = inspect.signature(publish_weights_direct)
        names = [p.lower() for p in signature.parameters]
        for forbidden in ("skip_readback", "verify", "check", "confirm"):
            self.assertNotIn(forbidden, names)


if __name__ == "__main__":
    unittest.main()
