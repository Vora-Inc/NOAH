"""One scoring period, end to end, and the order the steps must happen in.

The loop is where the components stop being separate. Most of what can go wrong
here is ordering: doing the right things in the wrong sequence produces a
validator that works in testing and loses a period in production.
"""

import unittest

from ..keeper import InvalidScore
from ..landed import PreviousRevealMismatch
from ..period import (
    AlreadyPublished,
    HeightNotPinned,
    PeriodRefused,
    run_period,
)


class Recorder:
    """Records the order every step was called in, so ordering can be asserted."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.calls = []
        self.published = []
        # Ticket #16 (B06): a plain attribute, exactly like a real
        # ChainAdapter's own self.publish_mode -- not a method, and not
        # present at all unless a test passes it, so
        # test_an_absent_publish_mode_attribute_behaves_as_sealed below can
        # prove run_period's own getattr fallback for real rather than
        # merely for a Recorder that always defines it.
        if "publish_mode" in behaviour:
            self.publish_mode = behaviour["publish_mode"]

    def _note(self, name):
        self.calls.append(name)

    # --- the loop's collaborators -------------------------------------
    def pinned_height(self):
        self._note("pin")
        if isinstance(self.b.get("pinned_height"), Exception):
            raise self.b["pinned_height"]
        return self.b.get("pinned_height", 1000)

    def already_published(self, height):
        self._note("already?")
        return self.b.get("already_published", False)

    def read(self, height):
        self._note(f"read@{height}")
        if isinstance(self.b.get("read"), Exception):
            raise self.b["read"]
        return self.b.get("read", {"height": height})

    def score(self, observations):
        self._note("score")
        if isinstance(self.b.get("score"), Exception):
            raise self.b["score"]
        return self.b.get("score", {0: 100, 1: 0})

    def agree(self, observations):
        self._note("twin")
        if isinstance(self.b.get("agree"), Exception):
            raise self.b["agree"]
        return self.b.get("agree", self.b.get("score", {0: 100, 1: 0}))

    def live_uids(self, height):
        self._note("seats")
        return self.b.get("live_uids", [0, 1])

    def owner_uids(self):
        return self.b.get("owner_uids", set())

    def fixture_hotkeys(self, height):
        self._note("fixture_hotkeys")
        default = {uid: f"hk{uid}" for uid in self.b.get("live_uids", [0, 1])}
        return self.b.get("fixture_hotkeys", default)

    def chain_hotkeys(self):
        self._note("chain_hotkeys")
        default = {uid: f"hk{uid}" for uid in self.b.get("live_uids", [0, 1])}
        return self.b.get("chain_hotkeys", default)

    def previous_period(self):
        self._note("prior")
        prior = self.b.get("previous_period")
        if isinstance(prior, Exception):
            raise prior
        return prior

    def publish(self, vector, height):
        self._note("publish")
        if isinstance(self.b.get("publish"), Exception):
            raise self.b["publish"]
        self.published.append((dict(vector), height))
        return {"confirmed_on_chain": True, "reveal_round": 7}

    def publish_direct(self, vector, height):
        self._note("publish_direct")
        if isinstance(self.b.get("publish_direct"), Exception):
            raise self.b["publish_direct"]
        self.published.append((dict(vector), height))
        return {
            "applied_weights_readback": {"confirmed": True, "block": 4242},
            "seats": len(vector),
            "block": 4242,
        }


def run(chain, **kw):
    return run_period(chain, **kw)


class TheOrderOfOperations(unittest.TestCase):
    def test_the_height_is_pinned_before_anything_is_read(self):
        # Every validator must read the same state. Reading first and pinning
        # afterwards means two validators scoring different data and blaming
        # each other's arithmetic.
        chain = Recorder()
        run(chain)
        self.assertEqual(chain.calls[0], "pin")
        self.assertTrue(chain.calls[chain.calls.index("read@1000")] == "read@1000")

    def test_everything_is_read_at_the_pinned_height_not_at_the_head(self):
        chain = Recorder(pinned_height=850)
        run(chain)
        self.assertIn("read@850", chain.calls)
        self.assertNotIn("read@1000", chain.calls)

    def test_the_twin_proof_runs_before_anything_is_published(self):
        chain = Recorder()
        run(chain)
        self.assertLess(chain.calls.index("twin"), chain.calls.index("publish"))

    def test_a_failed_twin_proof_publishes_nothing(self):
        chain = Recorder(agree=RuntimeError("engines disagree"))
        with self.assertRaises(PeriodRefused):
            run(chain)
        self.assertEqual(chain.published, [])


class ADuplicateColdkeyScoringRefusalIsObservableNotSwallowed(unittest.TestCase):
    """Issue #33's own sixth requirement from #16: a duplicate-coldkey
    hotkey being dropped from scoring (D-138, ``noah.scoring.fixture_io.
    parse_fixture``'s own ``D138_DUPLICATE_COLDKEY`` refusal) must have an
    observable-condition/log record point the operator view could read
    later, not silently vanish.

    Traced end to end rather than assumed: a real ``ChainAdapter.score()``
    (``noah/core/chain_adapter.py``) calling a duplicate-coldkey fixture
    through ``noah.scoring.fixture_io.parse_fixture`` raises exactly this
    ``FixtureError``; nothing in this codebase catches or downgrades it
    before it reaches here. This proves the next link: `run_period`'s own
    generic ``except Exception`` around ``chain.score()`` (`period.py`,
    step 3) wraps it as `PeriodRefused` WITHOUT losing the code or the
    named coldkey, and `noah.core.loop.tick`'s own `_write_state` (already
    covered by many cases in `test_loop.py`, e.g. ``phase == "refused"``)
    persists whatever string reaches here, durably, to `state_path` -- the
    file a later operator view reads. No new resolver logic is added
    here (that stays in #16); this only confirms the existing refuse-and-
    record path already carries the detail through rather than swallowing
    it.
    """

    def test_a_d138_duplicate_coldkey_refusal_from_score_reaches_period_refused_intact(self):
        from ...scoring.fixture_io import FixtureError

        coldkey = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
        # The exact message shape noah.scoring.fixture_io.parse_fixture's own
        # D-138 check raises (see test_fixture_build.py's own
        # ``FixtureError``/``D138_DUPLICATE_COLDKEY`` coverage for the real
        # call site) -- constructed here, not imported, because this test's
        # own job is the WIRING (does the detail survive from chain.score()
        # through to PeriodRefused's own message?), which does not need a
        # real, fully-built duplicate-coldkey fixture to prove.
        chain = Recorder(
            score=FixtureError(
                f"fixture.miners: coldkey(s) ['{coldkey}'] appear in more than one row. "
                f"D-138 rules exactly one recognised mining hotkey seat per wallet coldkey.",
                code="D138_DUPLICATE_COLDKEY",
            )
        )
        with self.assertRaises(PeriodRefused) as caught:
            run(chain)
        detail = str(caught.exception)
        # PeriodRefused's own message embeds `type(error).__name__` and
        # `str(error)` (period.py, step 3) -- not `error.code`, which
        # `FixtureError.__str__` does not include -- so what actually
        # survives into the persisted state (`noah.core.loop.tick`'s own
        # `_write_state`, already covered by test_loop.py's many
        # `phase == "refused"` cases) is the exception's CLASS NAME and its
        # human-readable message, checked here.
        self.assertIn("FixtureError", detail)
        self.assertIn("D-138", detail)
        self.assertIn(coldkey, detail)
        self.assertEqual(chain.published, [])


class ThePublishedVectorIsTheAgreedOne(unittest.TestCase):
    """chain.score() and chain.agree() are two separate calls.

    If the vector that gets published came from chain.score() rather than
    from what chain.agree() actually verified, a passing twin proof would
    prove only that the two engines agreed with EACH OTHER somewhere, not
    that they agreed on the SPECIFIC vector this function is about to put on
    chain. That gap defeats the whole safety model this repository's
    AGENTS.md names: two independent engines producing identical bytes.
    """

    def test_the_published_vector_is_what_agree_verified_not_a_separate_score_call(self):
        # chain.score() answers with one vector; chain.agree() answers with a
        # different one, standing in for two calls that quietly stopped
        # computing the same thing. Only the agreed vector may reach the chain.
        chain = Recorder(score={0: 999, 1: 999}, agree={0: 100, 1: 0})
        run(chain)
        published, _height = chain.published[0]
        self.assertEqual(published, {0: 100, 1: 0})

    def test_a_seat_the_engine_never_mentioned_still_comes_from_the_agreed_answer(self):
        chain = Recorder(score={0: 999}, agree={0: 5}, live_uids=[0, 1])
        run(chain)
        published, _height = chain.published[0]
        self.assertEqual(published, {0: 5, 1: 0})


class NotPublishingTwice(unittest.TestCase):
    def test_a_period_already_published_is_refused_before_any_work(self):
        # Rescoring and republishing the same period wastes the rate-limit
        # window that the NEXT period needs.
        chain = Recorder(already_published=True)
        with self.assertRaises(AlreadyPublished):
            run(chain)
        self.assertEqual(chain.published, [])
        self.assertNotIn("score", chain.calls)

    def test_the_check_happens_before_scoring_not_after(self):
        chain = Recorder(already_published=True)
        with self.assertRaises(AlreadyPublished):
            run(chain)
        self.assertNotIn("read@1000", chain.calls)


class WhenTheHeightCannotBePinned(unittest.TestCase):
    def test_an_unpinnable_height_refuses_rather_than_using_the_head(self):
        # Falling back to the head is the tempting repair and the wrong one: it
        # produces a validator that quietly scores different data from everyone
        # else, which consensus punishes without ever raising an error.
        chain = Recorder(pinned_height=RuntimeError("no epoch block"))
        with self.assertRaises(HeightNotPinned):
            run(chain)
        self.assertEqual(chain.published, [])

    def test_a_height_of_none_is_refused_rather_than_treated_as_zero(self):
        chain = Recorder(pinned_height=None)
        with self.assertRaises(HeightNotPinned):
            run(chain)


class WhatItPublishes(unittest.TestCase):
    def test_it_publishes_the_keeper_decision_not_the_raw_scores(self):
        # The raw answer omits seats that earned nothing. What reaches the chain
        # must name every live seat.
        chain = Recorder(score={0: 100}, live_uids=[0, 1, 2])
        run(chain)
        published, _height = chain.published[0]
        self.assertEqual(published, {0: 100, 1: 0, 2: 0})

    def test_a_period_where_nobody_earned_still_publishes(self):
        chain = Recorder(score={}, live_uids=[0, 1])
        result = run(chain)
        self.assertEqual(chain.published[0][0], {0: 0, 1: 0})
        self.assertTrue(result["keeping_alive"])

    def test_the_result_reports_the_height_it_scored(self):
        chain = Recorder(pinned_height=777)
        self.assertEqual(run(chain)["height"], 777)

    def test_a_publication_the_chain_did_not_confirm_is_not_reported_as_done(self):
        chain = Recorder(publish=RuntimeError("not on chain"))
        with self.assertRaises(PeriodRefused):
            run(chain)


class ASeatThatChangedHandsIsZeroedNotJustInPublishableVectorDirectly(unittest.TestCase):
    """publishable_vector's own tests prove it zeroes a reassigned seat.

    They do not prove run_period ever calls it. A uid does not know it
    changed hotkeys; only a component comparing fixture_hotkeys against
    chain_hotkeys can catch that, and if the loop never calls that
    component, the seat's new holder is paid for the previous holder's
    work regardless of how well-tested that component is in isolation.
    This drives the turnover through run_period itself.
    """

    def test_a_reassigned_seat_is_zeroed_end_to_end(self):
        chain = Recorder(
            score={0: 100, 1: 200},
            live_uids=[0, 1],
            fixture_hotkeys={0: "hk0", 1: "hk1"},
            chain_hotkeys={0: "hk0", 1: "SOMEBODY-ELSE"},
        )
        run(chain)
        published, _height = chain.published[0]
        self.assertEqual(published, {0: 100, 1: 0})

    def test_a_seat_that_did_not_change_hands_is_paid_in_full(self):
        # The control: nothing above may zero a seat that did NOT turn over.
        chain = Recorder(
            score={0: 100, 1: 200},
            live_uids=[0, 1],
            fixture_hotkeys={0: "hk0", 1: "hk1"},
            chain_hotkeys={0: "hk0", 1: "hk1"},
        )
        run(chain)
        published, _height = chain.published[0]
        self.assertEqual(published, {0: 100, 1: 200})


class AnInvalidScoreOnATurnedOverSeatStillRefusesThePeriod(unittest.TestCase):
    """publishable_vector's turnover rule is truthy-taken, not type-checked.

    `if weight and fixture_hotkeys.get(uid) != chain_hotkeys[uid]: weight = 0`
    treats any truthy value as real weight to zero -- including an invalid
    one. If turnover-zeroing ran BEFORE decide()'s int/u16-range check,
    a seat that both scored something invalid (a float, an out-of-range int)
    AND changed hands would have that invalid value silently replaced by a
    valid 0, and decide() would never see the original value to refuse it
    with. The rest of a scoring run that has already proven itself corrupted
    would then publish as if nothing were wrong. decide() has to validate
    `agreed` before turnover zeroing ever touches it, not after.
    """

    def test_a_non_int_score_on_a_reassigned_seat_still_raises_invalid_score(self):
        chain = Recorder(
            score={0: 1.9, 1: 100},
            live_uids=[0, 1],
            fixture_hotkeys={0: "hk0", 1: "hk1"},
            chain_hotkeys={0: "SOMEBODY-ELSE", 1: "hk1"},
        )
        with self.assertRaises(InvalidScore):
            run(chain)
        self.assertEqual(chain.published, [])

    def test_an_out_of_range_score_on_a_reassigned_seat_still_raises_invalid_score(self):
        chain = Recorder(
            score={0: 70_000, 1: 100},
            live_uids=[0, 1],
            fixture_hotkeys={0: "hk0", 1: "hk1"},
            chain_hotkeys={0: "SOMEBODY-ELSE", 1: "hk1"},
        )
        with self.assertRaises(InvalidScore):
            run(chain)
        self.assertEqual(chain.published, [])

    def test_a_bool_score_on_a_reassigned_seat_still_raises_invalid_score(self):
        # type(True) is not int, even though isinstance(True, int) is true --
        # decide()'s own exact-type check exists specifically for this.
        chain = Recorder(
            score={0: True, 1: 100},
            live_uids=[0, 1],
            fixture_hotkeys={0: "hk0", 1: "hk1"},
            chain_hotkeys={0: "SOMEBODY-ELSE", 1: "hk1"},
        )
        with self.assertRaises(InvalidScore):
            run(chain)
        self.assertEqual(chain.published, [])


if __name__ == "__main__":
    unittest.main()


# The real reveal, from block 5,814 of the local Phase 0 chain, genesis
# 0x933bd644, read back from storage at 5,955.
SEALED = [20000, 12000, 9000, 7000, 6000, 5000, 4000, 1535, 1000]
STORED = [65535, 39321, 29491, 22937, 19661, 16384, 13107, 5030, 3277]


class TheLastPeriodIsCheckedBeforeThisOneIsPublished(unittest.TestCase):
    """A commit is opened later, so a period can only check the one before it.

    The whole value is in the timing. Checking after publishing finds the same
    fault and can no longer stop anything.
    """

    def test_a_mismatch_last_period_stops_this_one_publishing_at_all(self):
        wrong = list(STORED)
        wrong[3] += 1
        chain = Recorder(previous_period=(5814, SEALED, wrong))
        with self.assertRaises(PreviousRevealMismatch):
            run(chain)
        self.assertEqual(chain.published, [])
        self.assertNotIn("publish", chain.calls)

    def test_the_check_happens_before_publishing_and_not_after(self):
        chain = Recorder(previous_period=(5814, SEALED, STORED))
        run(chain)
        self.assertLess(chain.calls.index("prior"), chain.calls.index("publish"))

    def test_a_matching_last_period_publishes_and_says_it_was_verified(self):
        chain = Recorder(previous_period=(5814, SEALED, STORED))
        out = run(chain)
        self.assertTrue(out["previous_verified"])
        self.assertEqual(len(chain.published), 1)

    def test_the_chains_rescaling_is_not_mistaken_for_a_mismatch(self):
        """The stored vector is never the sealed one, and must not read as wrong.

        A check comparing them directly would halt every period on a healthy
        validator, and it fails closed, which reads as caution.
        """
        self.assertNotEqual(SEALED, STORED)
        self.assertTrue(run(Recorder(previous_period=(5814, SEALED, STORED)))["previous_verified"])

    def test_an_unreadable_last_period_still_publishes_but_claims_nothing(self):
        """Halting is not the safe direction here, so absence does not halt.

        A validator that stops committing goes inactive and takes the subnet's
        pay to zero, so stopping on a failed read trades a small risk for a
        much larger one.
        """
        chain = Recorder(previous_period=RuntimeError("archive down"))
        out = run(chain)
        self.assertEqual(len(chain.published), 1)
        self.assertFalse(out["previous_verified"])
        self.assertIn("UNVERIFIED", out["previous_reason"])

    def test_a_first_run_with_no_previous_period_publishes_and_claims_nothing(self):
        out = run(Recorder(previous_period=None))
        self.assertFalse(out["previous_verified"])
        self.assertIn("no previous period", out["previous_reason"])

    def test_a_period_nobody_could_read_is_not_a_mismatch(self):
        # Half the evidence missing is not evidence of a fault.
        chain = Recorder(previous_period=(5814, SEALED, None))
        out = run(chain)
        self.assertEqual(len(chain.published), 1)
        self.assertFalse(out["previous_verified"])

    def test_the_halt_says_what_stopping_costs(self):
        """A refusal that stops a validator must say what the stopping does.

        Left alone, this halt runs out the activity window and takes the whole
        subnet's pay to zero, so the message says so rather than leaving an
        operator to discover it.
        """
        wrong = list(STORED)
        wrong[0] -= 1
        with self.assertRaises(PreviousRevealMismatch) as caught:
            run(Recorder(previous_period=(5814, SEALED, wrong)))
        message = str(caught.exception)
        self.assertIn("pay to zero", message)
        self.assertIn("do not\nleave this sitting.".replace("\n", " "), " ".join(message.split()))


class ThePublishModeSelectsWhichPathIsCalled(unittest.TestCase):
    """Ticket #16 (B06), issue #56 comment 5720306237.

    Two separate delegate methods exist on ``chain`` (``publish`` and
    ``publish_direct``) precisely so this can be proven by WHICH ONE was
    called, not by inspecting a single call's arguments.
    """

    def test_the_default_sealed_path_is_unchanged_when_publish_mode_is_absent(self):
        # No publish_mode attribute at all -- exactly what every existing
        # fake chain in this project looked like before this feature
        # existed. run_period must still behave exactly as it always did.
        chain = Recorder()
        self.assertFalse(hasattr(chain, "publish_mode"))
        run(chain)
        self.assertIn("publish", chain.calls)
        self.assertNotIn("publish_direct", chain.calls)

    def test_an_explicit_sealed_publish_mode_calls_publish_not_publish_direct(self):
        chain = Recorder(publish_mode="sealed")
        run(chain)
        self.assertIn("publish", chain.calls)
        self.assertNotIn("publish_direct", chain.calls)

    def test_a_direct_publish_mode_calls_publish_direct_not_publish(self):
        chain = Recorder(publish_mode="direct")
        run(chain)
        self.assertIn("publish_direct", chain.calls)
        self.assertNotIn("publish", chain.calls)

    def test_direct_mode_still_publishes_the_turnover_checked_vector_for_the_real_seat(self):
        # Requirement 5: this does not touch WHICH vector gets published or
        # who signs it. Same fixture as ASeatThatChangedHandsIsZeroedNot...
        # above, run through the direct path instead of the sealed one.
        chain = Recorder(
            publish_mode="direct",
            score={0: 100, 1: 200},
            live_uids=[0, 1],
            fixture_hotkeys={0: "hk0", 1: "hk1"},
            chain_hotkeys={0: "hk0", 1: "SOMEBODY-ELSE"},
        )
        run(chain)
        published, height = chain.published[0]
        self.assertEqual(published, {0: 100, 1: 0})
        self.assertEqual(height, 1000)

    def test_a_direct_publish_failure_refuses_the_period_the_same_way_a_sealed_one_does(self):
        chain = Recorder(publish_mode="direct", publish_direct=RuntimeError("node fell over"))
        with self.assertRaises(PeriodRefused):
            run(chain)


class ThereIsNoAutomaticFallbackBetweenThePublishPaths(unittest.TestCase):
    """Structural, not behavioral -- matching noah/core/tests/test_publish.py's
    own ``TheDirectPathIsSeparateAndNeverAFallback`` and noah/core/tests/
    test_chain_adapter.py's own ``test_publish_and_publish_direct_are_never_
    wired_to_each_other``. The property under test is that reaching
    ``chain.publish_direct`` takes ``chain.publish_mode`` naming it BEFORE
    step 6 runs, never a runtime reaction to ``chain.publish`` failing.
    """

    def test_run_periods_own_source_never_calls_publish_direct_from_inside_the_publish_except(self):
        import ast
        import inspect

        from .. import period as period_module

        source = inspect.getsource(period_module.run_period)
        tree = ast.parse(source)
        (function,) = tree.body

        # Find the try/except that wraps the publish call, and confirm its
        # OWN except handler(s) never mention publish_direct -- the shape an
        # automatic fallback from a sealed failure would actually take.
        publish_try = None
        for node in ast.walk(function):
            if isinstance(node, ast.Try):
                try_source = ast.get_source_segment(source, node)
                handler_source = ast.get_source_segment(source, node.handlers[0])
                if "chain.publish" in try_source and "raise PeriodRefused" in handler_source:
                    publish_try = node
                    break
        self.assertIsNotNone(publish_try, "could not find run_period's publish try/except")
        for handler in publish_try.handlers:
            handler_source = ast.get_source_segment(source, handler)
            self.assertNotIn("publish_direct", handler_source)

    def test_the_mode_is_read_exactly_once_before_the_publish_call_is_chosen(self):
        # A second, independent way to hold the same property: the source
        # reads chain.publish_mode via getattr exactly once, in code (not in
        # a comment), and every call site deciding between
        # chain.publish/chain.publish_direct branches on the SAME
        # already-read local ("mode"), never re-deriving or re-reacting to
        # anything mid-flight.
        import ast
        import inspect

        from .. import period as period_module

        source = inspect.getsource(period_module.run_period)
        self.assertIn('mode = getattr(chain, "publish_mode", "sealed")', source)

        tree = ast.parse(source)
        (function,) = tree.body
        getattr_reads = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and any(
                isinstance(arg, ast.Constant) and arg.value == "publish_mode"
                for arg in node.args
            )
        ]
        self.assertEqual(len(getattr_reads), 1)


class TheReturnValueIsHonestAboutWhichModePublished(unittest.TestCase):
    """Requirement 4: sealed-path-specific keys never appear (as None or
    otherwise) on a direct-mode result, and direct-mode's own real keys
    never appear on a sealed-mode result. Never one field standing in for
    two different facts.
    """

    def test_sealed_mode_result_shape_is_unchanged(self):
        chain = Recorder()
        result = run(chain)
        self.assertEqual(result["publish_mode"], "sealed")
        self.assertTrue(result["confirmed_on_chain"])
        self.assertEqual(result["reveal_round"], 7)
        self.assertNotIn("applied_weights_readback", result)
        # Issue #334: the sealed result now also carries the commit's own block
        # and extrinsic index (they used to be dropped), for the per-day ledger.
        self.assertIn("block", result)
        self.assertIn("extrinsic_index", result)

    def test_direct_mode_result_carries_its_own_real_keys_not_sealed_ones(self):
        chain = Recorder(publish_mode="direct")
        result = run(chain)
        self.assertEqual(result["publish_mode"], "direct")
        self.assertEqual(
            result["applied_weights_readback"], {"confirmed": True, "block": 4242}
        )
        self.assertEqual(result["block"], 4242)
        # Never fabricated: no reveal_round exists on this path, and nothing
        # stands in for the sealed-only confirmed_on_chain meaning either.
        self.assertNotIn("reveal_round", result)
        self.assertNotIn("confirmed_on_chain", result)

    def test_direct_mode_still_reports_height_seats_and_keeping_alive_the_same_way(self):
        chain = Recorder(publish_mode="direct", score={}, live_uids=[0, 1])
        result = run(chain)
        self.assertEqual(result["height"], 1000)
        self.assertEqual(result["seats"], 2)
        # An all-zero scoring pass is the activity keeper's own
        # keeping_alive=True case (see WhatItPublishes's identical sealed-
        # path assertion above) -- unaffected by which path publishes it.
        self.assertTrue(result["keeping_alive"])


class Step5RunsIdenticallyRegardlessOfPublishMode(unittest.TestCase):
    """The investigation this ticket asked for: check_previous_reveal asks a
    question about the chain's OWN storage rescale of whatever was
    published for the previous period, which is not specific to how that
    previous period was published (see period.py's own module docstring,
    "Step 5 asks the identical question regardless of which path
    publishes"). So step 5 is NOT special-cased per mode -- it runs the
    same way, with the same protection, for a direct-mode run as for a
    sealed one.
    """

    def test_the_check_still_runs_before_publish_direct_and_not_after(self):
        chain = Recorder(publish_mode="direct", previous_period=(5814, SEALED, STORED))
        run(chain)
        self.assertLess(chain.calls.index("prior"), chain.calls.index("publish_direct"))

    def test_a_mismatch_still_halts_a_direct_mode_publish_the_same_as_a_sealed_one(self):
        wrong = list(STORED)
        wrong[3] += 1
        chain = Recorder(publish_mode="direct", previous_period=(5814, SEALED, wrong))
        with self.assertRaises(PreviousRevealMismatch):
            run(chain)
        self.assertEqual(chain.published, [])
        self.assertNotIn("publish_direct", chain.calls)

    def test_an_unreadable_prior_check_still_publishes_direct_but_claims_nothing(self):
        chain = Recorder(publish_mode="direct", previous_period=RuntimeError("archive down"))
        out = run(chain)
        self.assertEqual(len(chain.published), 1)
        self.assertFalse(out["previous_verified"])
        self.assertIn("UNVERIFIED", out["previous_reason"])
