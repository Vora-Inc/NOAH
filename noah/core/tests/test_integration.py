"""The whole loop against a stand-in chain, with failures forced on demand.

Every other test file exercises one component. This one wires the real ones
together, fakes nothing but the chain, and drives the result into states a live
drill cannot schedule: a seat changing hands mid-period, a publication the chain
accepts and does not store, two engines disagreeing, a period nobody earned in.

That last property is the point. On a real chain you wait for a seat to change
hands. Here it happens on demand, every run, which is what makes the live drills
reproducible rather than anecdotal.
"""

import unittest

from ..keeper import decide
from ..period import AlreadyPublished, HeightNotPinned, PeriodRefused, run_period
from ..publish import NotOnChain, publish_weights
from ..startup import check_can_publish_yet
from ..twin import require_agreement
from ..vector import OwnerWouldBePaid, publishable_vector


class StandInChain:
    """A chain that can be driven into any state the loop must survive.

    Deliberately not a mock of the components: the real keeper, vector builder,
    twin gate and publisher run against it. Only the chain is pretend.
    """

    def __init__(self, **world):
        self.w = world
        self.stored = []
        self.published = []

    # --- what the period loop asks of it -------------------------------
    def pinned_height(self):
        return self.w.get("height", 5000)

    def already_published(self, height):
        return self.w.get("already", False)

    def read(self, height):
        return {"height": height, "seats": self.w.get("seats_then", {0: "A", 1: "B"})}

    def score(self, observations):
        return self.w.get("scores", {0: 40000, 1: 25535})

    def agree(self, observations):
        # require_agreement compares two RENDERED TEXT outputs and returns
        # whichever text they agreed on (or raises); it is not itself a
        # seat-to-weight dict. A real adapter renders each engine's vector,
        # verifies the two renderings agree byte for byte, and returns the
        # vector THAT rendering was built from -- not a separately fetched
        # vector that only happens to match it, which would be the exact gap
        # the original finding was about, just moved one layer down.
        #
        # This stand-in does the same, for real: each engine's answer is a
        # vector (engine_one_scores / engine_two_scores, both defaulting to
        # `scores`), rendered to text for the comparison, and the value
        # actually returned is reconstructed from the text require_agreement
        # proved was agreed on -- never a separate `self.w.get("scores")`
        # lookup performed after the fact. `engine_one` / `engine_two` still
        # work as a raw text override, for tests that want to force
        # disagreement without constructing two differing vectors.
        default = self.w.get("scores", {0: 40000, 1: 25535})
        one_vector = self.w.get("engine_one_scores", default)
        two_vector = self.w.get("engine_two_scores", default)
        one_text = self.w.get("engine_one", repr(sorted(one_vector.items())))
        two_text = self.w.get("engine_two", repr(sorted(two_vector.items())))
        agreed_text = require_agreement(
            observations, first=lambda _f: one_text, second=lambda _f: two_text
        )
        return one_vector if agreed_text == one_text else two_vector

    def live_uids(self, height):
        return sorted(self.w.get("seats_now", {0: "A", 1: "B"}))

    def owner_uids(self):
        return self.w.get("owner_uids", set())

    def fixture_hotkeys(self, height):
        # Who held each seat AT the scored height. Reuses seats_then, the same
        # world key `read()` already draws on for the same fact.
        return self.w.get("seats_then", {0: "A", 1: "B"})

    def chain_hotkeys(self):
        # Who holds each seat NOW. Reuses seats_now, the same world key
        # live_uids() already draws on -- that method just discards the
        # hotkeys and keeps only the uids.
        return self.w.get("seats_now", {0: "A", 1: "B"})

    def publish(self, vector, height):
        # The real publisher, against this chain's own rate limit and store.
        return publish_weights(self, vector=vector, uid=0, netuid=2, hotkey=b"\x00" * 32)

    # --- what the publisher asks of it ---------------------------------
    def current_block(self):
        return self.w.get("current_block", 10_000)

    def last_update(self, uid, netuid):
        return self.w.get("last_update", 0)

    def rate_limit(self, netuid):
        return self.w.get("rate_limit", 100)

    def seal(self, uids, values, hotkey):
        self.sealed = list(zip(uids, values))
        return b"c:" + str(self.sealed).encode(), 4242

    def submit(self, commit, reveal_round, netuid):
        if self.w.get("submit_error"):
            return {"error": self.w["submit_error"]}
        self.published.append(commit)
        if self.w.get("chain_stores", True):
            self.stored.append(commit)
        return {"error": None, "block": 1}

    def read_map(self, name, netuid):
        from ..commits import LIVE_MAP

        return list(self.stored) if name == LIVE_MAP else []


class AnOrdinaryPeriod(unittest.TestCase):
    def test_it_runs_end_to_end_and_the_chain_holds_what_we_sent(self):
        chain = StandInChain()
        result = run_period(chain)
        self.assertTrue(result["confirmed_on_chain"])
        self.assertEqual(result["height"], 5000)
        self.assertEqual(chain.sealed, [(0, 40000), (1, 25535)])

    def test_a_seat_that_earned_nothing_is_still_named_on_chain(self):
        chain = StandInChain(scores={0: 65535}, seats_now={0: "A", 1: "B", 2: "C"})
        run_period(chain)
        self.assertEqual(chain.sealed, [(0, 65535), (1, 0), (2, 0)])


class FailuresALiveDrillCannotSchedule(unittest.TestCase):
    def test_a_period_nobody_earned_in_still_publishes_every_seat(self):
        chain = StandInChain(scores={})
        result = run_period(chain)
        self.assertTrue(result["keeping_alive"])
        self.assertEqual(chain.sealed, [(0, 0), (1, 0)])

    def test_two_engines_disagreeing_publishes_nothing(self):
        chain = StandInChain(engine_one="a", engine_two="b")
        with self.assertRaises(PeriodRefused):
            run_period(chain)
        self.assertEqual(chain.published, [])

    def test_the_published_vector_is_the_one_agree_actually_verified(self):
        """Not the separately-derived one chain.score() would have answered.

        chain.score() would answer with `scores` (999) if it were ever asked
        for publication -- it is not, but this proves the published vector
        is not merely defaulting to it either. What the two engines actually
        rendered and were proven to agree on (5) is what reaches the chain.
        This is the integration-level version of the same property
        test_period.py checks with a simpler fake, now run against the real
        require_agreement.
        """
        chain = StandInChain(scores={0: 999}, engine_one_scores={0: 5}, engine_two_scores={0: 5})
        run_period(chain)
        # Both live seats (0 and 1, the default) are named; seat 1 earned
        # nothing in the agreed vector, so it is published at zero.
        self.assertEqual(chain.sealed, [(0, 5), (1, 0)])

    def test_a_chain_that_accepts_and_does_not_store_is_caught(self):
        # The failure the read-back exists for, forced deliberately.
        chain = StandInChain(chain_stores=False)
        with self.assertRaises(PeriodRefused) as caught:
            run_period(chain)
        self.assertIsInstance(caught.exception.__cause__, NotOnChain)

    def test_a_freshly_registered_validator_is_refused_and_told_to_wait(self):
        chain = StandInChain(current_block=10_050, last_update=10_000, rate_limit=100)
        with self.assertRaises(PeriodRefused) as caught:
            run_period(chain)
        self.assertIn("50", str(caught.exception))

    def test_a_period_already_published_does_no_work_at_all(self):
        chain = StandInChain(already=True)
        with self.assertRaises(AlreadyPublished):
            run_period(chain)
        self.assertEqual(chain.published, [])

    def test_an_unpinnable_height_never_reaches_the_chain(self):
        class NoHeight(StandInChain):
            def pinned_height(self):
                raise RuntimeError("no epoch block")

        chain = NoHeight()
        with self.assertRaises(HeightNotPinned):
            run_period(chain)
        self.assertEqual(chain.published, [])


class TheSeatThatChangedHands(unittest.TestCase):
    """Weights apply at least two periods after the height they were scored at.

    A seat can be reassigned in that window while passing every validity check
    the chain makes. The miner who earned the weight is gone and whoever holds
    the slot now earned nothing.
    """

    def test_a_reassigned_seat_is_zeroed_rather_than_paid_to_its_new_holder(self):
        vector = publishable_vector(
            scored={0: 40000, 1: 25535},
            live_uids=[0, 1],
            fixture_hotkeys={0: "A", 1: "B"},
            chain_hotkeys={0: "A", 1: "SOMEBODY-ELSE"},
            owner_uids=set(),
        )
        self.assertEqual(vector, {0: 40000, 1: 0})

    def test_weight_landing_on_our_own_seat_refuses_the_whole_publication(self):
        with self.assertRaises(OwnerWouldBePaid):
            publishable_vector(
                scored={0: 40000, 1: 25535},
                live_uids=[0, 1],
                fixture_hotkeys={0: "A", 1: "B"},
                chain_hotkeys={0: "A", 1: "B"},
                owner_uids={1},
            )

    def test_run_period_itself_zeroes_the_reassigned_seat_not_just_publishable_vector(self):
        """The gap this class's other test cannot see.

        The test above proves publishable_vector zeroes a reassigned seat when
        called directly. It says nothing about whether run_period ever calls
        it -- and until this was wired up, it did not: run_period fed the
        agreed vector straight to keeper.decide(), which only ever sees a uid
        and has no way to know that uid's hotkey changed. This drives the
        turnover through run_period end to end, the way a live period would.
        """
        chain = StandInChain(seats_then={0: "A", 1: "B"}, seats_now={0: "A", 1: "SOMEBODY-ELSE"})
        run_period(chain)
        self.assertEqual(chain.sealed, [(0, 40000), (1, 0)])


class TheComponentsAgreeWithEachOther(unittest.TestCase):
    """Each component is tested alone elsewhere. This checks they compose."""

    def test_the_keeper_and_the_vector_builder_name_the_same_seats(self):
        seats = {0: "A", 1: "B", 2: "C"}
        scored = {0: 65535}
        keeper = decide(scored=scored, live_uids=sorted(seats), owner_uids=set())
        built = publishable_vector(
            scored=scored,
            live_uids=sorted(seats),
            fixture_hotkeys=seats,
            chain_hotkeys=seats,
            owner_uids=set(),
        )
        self.assertEqual(sorted(keeper.vector), sorted(built))
        self.assertEqual(keeper.vector, built)

    def test_the_rate_limit_check_and_the_publisher_agree_on_the_boundary(self):
        # Two components read the same rule. If they disagreed by one block the
        # validator would refuse a period the chain would have accepted.
        chain = StandInChain(current_block=10_100, last_update=10_000, rate_limit=100)
        self.assertEqual(check_can_publish_yet(chain, uid=0, netuid=2), 0)
        run_period(chain)  # must not raise


if __name__ == "__main__":
    unittest.main()
