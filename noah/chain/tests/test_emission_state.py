"""noah.chain.emission_state.observe_emission_state: reading whether the
subnet has begun emitting, and recording it through the matching
noah.chain.store.Store method (issue #567).

Layered coverage, per the issue's own verification bar: the decode guard
is proven in test_live_epoch_reads.py, dual-source agreement in
test_agreeing.py -- this file proves observe_emission_state's OWN logic
(which Store method to call, and with what block), the asymmetric
re-recording behaviour the issue's own text calls out as "the subtlety
that matters most," and -- the test the issue says a narrower one does not
satisfy -- a real multi-day, multi-coldkey walk through build_fixture,
with only an ABSENCE recorded and eval_day sitting exactly on the absent
day, proving noah.chain.nofault.no_fault_for's own inner
EmissionStateNotObserved raise stays unreachable once this reader has
wired a real value.
"""

import pytest

from noah.chain.emission_state import EmissionStateReorged, observe_emission_state
from noah.chain.fixture_build import build_fixture, withheld_from
from noah.chain.nofault import EmissionStateNotObserved
from noah.chain.store import Store

ANCHOR = 1767571200  # a Monday 00:00:00 UTC -- matches test_nofault.py's own anchor
DAY = 86_400

DIALS = {
    "targetFp": "500000000",
    "harshness": 1,
    "capFp": "1000000000",
    "halfLifeDays": "182",
    "streakCeilingFp": "2000000000",
    "streakRampDays": "365",
    "ongoingHoldRao": "0",
}


def a_store(tmp_path, name="noah.db"):
    return Store.create(tmp_path / name, day_anchor_ts=ANCHOR)


class FakeRunnerChain:
    """Enough of the dual-sourced runner_chain protocol for
    observe_emission_state: first_emission_block, block_hash,
    block_timestamp.

    Real production code hands observe_emission_state a real, dual-sourced
    noah.chain.agreeing.AgreeingChain -- proven separately, end to end,
    in test_agreeing.py (agreement/disagreement over first_emission_block
    itself) and test_live_epoch_reads.py (the decode guard underneath it).
    This fake is duck-typed to the identical three-method contract so
    observe_emission_state's OWN decision -- which Store method to call,
    and with what block/timestamp/hash -- is tested in isolation from
    dual-source agreement machinery this file does not re-prove.
    """

    def __init__(
        self,
        first_emission_block_by_query=None,
        times=None,
        hashes=None,
        hash_sequences=None,
        first_emission_block_sequences=None,
    ):
        # {observed_block: answer}, so a test can change the chain's own
        # answer between two calls -- the same way real time advancing (a
        # start call finally landing) would.
        self._answers = dict(first_emission_block_by_query or {})
        self._times = dict(times or {})
        self._hashes = dict(hashes or {})
        # {block: [hash_call_1, hash_call_2, ...]} -- lets a test make
        # block_hash answer DIFFERENTLY on successive calls for the SAME
        # block, simulating a reorg landing between observe_emission_
        # state's own bracketing reads. Exhausted sequences repeat their
        # last value, matching "the chain has settled again."
        self._hash_sequences = {k: list(v) for k, v in (hash_sequences or {}).items()}
        self._hash_calls = {}
        # {observed_block: [call_1_answer, call_2_answer, ...]} -- the
        # identical idea, for first_emission_block itself: lets a test
        # make the FACT answer differently between observe_emission_
        # state's own initial read and its later fresh re-check (see
        # emission_state.py's own "Reorg safety" docstring section for
        # why that specific composition is what round 2 of codex's
        # adversarial review found unguarded).
        self._answer_sequences = {
            k: list(v) for k, v in (first_emission_block_sequences or {}).items()
        }
        self._answer_calls = {}
        self.asked = []

    def set_answer(self, observed_block, answer):
        self._answers[observed_block] = answer

    def first_emission_block(self, block):
        self.asked.append(("first_emission_block", block))
        if block in self._answer_sequences:
            sequence = self._answer_sequences[block]
            call_index = self._answer_calls.get(block, 0)
            self._answer_calls[block] = call_index + 1
            return sequence[min(call_index, len(sequence) - 1)]
        return self._answers.get(block)

    def block_timestamp(self, block):
        self.asked.append(("block_timestamp", block))
        return self._times.get(block, ANCHOR + block)

    def block_hash(self, block):
        self.asked.append(("block_hash", block))
        if block in self._hash_sequences:
            sequence = self._hash_sequences[block]
            call_index = self._hash_calls.get(block, 0)
            self._hash_calls[block] = call_index + 1
            return sequence[min(call_index, len(sequence) - 1)]
        return self._hashes.get(block, f"0xblock{block}")


# ---- the started path: one reading, resolves every day ---------------------


def test_a_real_start_block_is_recorded_through_record_emission_start(tmp_path):
    store = a_store(tmp_path)
    chain = FakeRunnerChain(
        first_emission_block_by_query={9_000_000: 8_123_456},
        times={8_123_456: ANCHOR + 2 * DAY + 500},
        hashes={8_123_456: "0xstartblock"},
    )
    observe_emission_state(chain, store, observed_block=9_000_000)

    # Resolved through Store.record_emission_start, at the STARTED block's
    # own hash/timestamp -- not the observed_block's -- exactly what
    # Store.record_emission_start's own docstring requires ("the block the
    # chain itself stores").
    assert store.subnet_was_emitting(2) is True
    # And -- the whole point of "started" -- EVERY day resolves, including
    # ones long before and long after the observed block.
    assert store.subnet_was_emitting(0) is False
    assert store.subnet_was_emitting(1) is False
    assert store.subnet_was_emitting(9999) is True


def test_a_recorded_start_is_never_read_again_once_the_node_has_pruned_it(tmp_path):
    """Issue #237, item E. The start block ages out of a mainnet node's state
    after about 90 days, and asking for its timestamp then fails. Once the
    start is recorded, later reads answer from the store and never ask."""
    store = a_store(tmp_path)
    first = FakeRunnerChain(
        first_emission_block_by_query={9_000_000: 8_123_456},
        times={8_123_456: ANCHOR + 2 * DAY + 500},
        hashes={8_123_456: "0xstartblock"},
    )
    day = observe_emission_state(first, store, observed_block=9_000_000)

    class PrunedChain(FakeRunnerChain):
        def block_timestamp(self, block):
            self.asked.append(("block_timestamp", block))
            if block < 9_000_000 - 648_000:
                raise RuntimeError(f"State already discarded for block {block}")
            return ANCHOR + block

        def block_hash(self, block):
            self.asked.append(("block_hash", block))
            if block < 9_000_000 - 648_000:
                raise RuntimeError(f"State already discarded for block {block}")
            return f"0xblock{block}"

    later = PrunedChain(first_emission_block_by_query={9_800_000: 8_123_456})
    assert observe_emission_state(later, store, observed_block=9_800_000) == day
    assert ("block_timestamp", 8_123_456) not in later.asked
    assert ("block_hash", 8_123_456) not in later.asked
    assert store.subnet_was_emitting(2) is True


def test_a_recorded_start_that_the_chain_now_contradicts_is_refused(tmp_path):
    from noah.chain.store import EmissionStateChanged

    store = a_store(tmp_path)
    first = FakeRunnerChain(
        first_emission_block_by_query={9_000_000: 8_123_456},
        times={8_123_456: ANCHOR + 2 * DAY + 500},
    )
    observe_emission_state(first, store, observed_block=9_000_000)
    contradicting = FakeRunnerChain(first_emission_block_by_query={9_800_000: 8_999_999})
    with pytest.raises(EmissionStateChanged):
        observe_emission_state(contradicting, store, observed_block=9_800_000)
    assert store.recorded_emission_start()[1] == 8_123_456


def test_the_started_block_read_is_the_maps_own_value_not_the_observed_block():
    """observe_emission_state asks for the STARTED block's own hash/
    timestamp, a separate pair of reads from observed_block's -- proving
    it does not carelessly reuse whatever it already read for
    observed_block itself."""
    chain = FakeRunnerChain(first_emission_block_by_query={9_000_000: 8_123_456})
    store_calls = []

    class RecordingStore:
        def recorded_emission_start(self):
            return None

        def record_emission_start(self, block, block_timestamp, block_hash):
            store_calls.append((block, block_timestamp, block_hash))
            return 2

        def record_emission_not_started(self, block, block_timestamp, block_hash):
            raise AssertionError("the not-started path must not run when a start was found")

    observe_emission_state(chain, RecordingStore(), observed_block=9_000_000)
    assert store_calls == [(8_123_456, ANCHOR + 8_123_456, "0xblock8123456")]
    assert ("block_timestamp", 8_123_456) in chain.asked
    assert ("block_hash", 8_123_456) in chain.asked


def test_the_started_block_may_be_exactly_one_past_the_observed_block():
    """[codex adversarial review of this exact commit, 2026-09-01, P2] The
    runtime writes next_block_number = current_block_number.saturating_
    add(1) (subnets/subnet.rs:565-567): if the start call executes IN
    observed_block itself, FirstEmissionBlockNumber already reads
    observed_block + 1 by the time this asks. An earlier version of this
    module's own docstring claimed the map's value could never exceed
    observed_block -- wrong, and this is the exact boundary that disproves
    it. Still safe to read: see this module's own docstring, "Which block
    is observed," for why observed_block + 1 is always guaranteed to
    already exist by the time this runs in production."""
    store_calls = []

    class RecordingStore:
        def recorded_emission_start(self):
            return None

        def record_emission_start(self, block, block_timestamp, block_hash):
            store_calls.append((block, block_timestamp, block_hash))
            return 5

        def record_emission_not_started(self, block, block_timestamp, block_hash):
            raise AssertionError("the not-started path must not run when a start was found")

    observed_block = 9_000_000
    started_at = observed_block + 1
    chain = FakeRunnerChain(
        first_emission_block_by_query={observed_block: started_at},
        times={started_at: ANCHOR + 5 * DAY + 1},
        hashes={started_at: "0xexactlyone"},
    )
    observe_emission_state(chain, RecordingStore(), observed_block=observed_block)
    assert store_calls == [(started_at, ANCHOR + 5 * DAY + 1, "0xexactlyone")]


# ---- reorg safety: a converged answer refuses rather than trusts stale ----
#
# Round 1 (bracketing observed_block's own hash around the initial read)
# was found insufficient by round 2 of codex's own adversarial review: the
# two brackets (observed_block's, started_at's) closed INDEPENDENTLY, so a
# reorg landing strictly between the first first_emission_block call and
# started_at's own later reads showed up in NEITHER bracket. What actually
# closes it is re-deriving the answer this function is about to trust,
# fresh, immediately before trusting it -- see emission_state.py's own
# "Reorg safety" docstring section for the full account, including why
# codex's own round-2 repro is exactly the case a raw hash bracket on
# observed_block cannot see.


class _UnreachableStore:
    """A Store double that fails loudly if either recording method is
    ever reached -- every reorg-refusal test below must raise BEFORE
    either is called."""

    def recorded_emission_start(self):
        return None

    def record_emission_start(self, **kwargs):
        raise AssertionError("must not be reached")

    def record_emission_not_started(self, **kwargs):
        raise AssertionError("must not be reached")


def test_the_subnet_starting_between_the_first_read_and_the_fresh_recheck_refuses():
    """The not-started path's own half of round 2's gap: first_emission_
    block(observed_block) answers None on the first ask, then -- after
    this function has already gone on to read observed_block's own
    timestamp -- a fresh re-check answers a real block instead. That is
    exactly "None from an old fork when the new fork has started" (#567's
    own issue text, and codex's launch-1 review), caught here because the
    re-check disagrees with the first answer."""
    chain = FakeRunnerChain(first_emission_block_sequences={9_000_000: [None, 8_500_000]})
    with pytest.raises(EmissionStateReorged, match="9000000"):
        observe_emission_state(chain, _UnreachableStore(), observed_block=9_000_000)


def test_the_started_block_changing_between_the_first_read_and_the_fresh_recheck_refuses():
    """[codex adversarial challenge, round 2, 2026-09-01, P1] Round 2's
    own live repro, reduced to this fake: first_emission_block(observed_
    block) names one start block on the first ask, then a DIFFERENT one on
    the fresh re-check taken after started_at's own timestamp/hash have
    already been read -- exactly the composition round 1's independent
    brackets could not see, because started_at's OWN hash can legitimately
    agree with itself across both of ITS reads (the reorg need not touch a
    block that deep) while the SUBNET's own answer about when it started
    still moved underneath, between the two first_emission_block calls."""
    chain = FakeRunnerChain(
        first_emission_block_sequences={9_000_000: [8_123_456, 8_999_999]},
        hashes={8_123_456: "0xstart-a", 8_999_999: "0xstart-b"},
    )
    with pytest.raises(EmissionStateReorged, match="9000000"):
        observe_emission_state(chain, _UnreachableStore(), observed_block=9_000_000)


def test_a_reorg_between_the_two_started_block_hash_reads_refuses():
    """The complementary case the fresh re-check alone would NOT catch:
    first_emission_block names the SAME block number on both asks, but
    that block's own CONTENT (hash) shifted between reading its hash and
    reading its own timestamp -- a shallow reorg local to started_at
    itself. Caught by started_at's own hash bracket, kept alongside the
    re-check specifically because the two guard different things (see
    emission_state.py's own "Reorg safety" docstring section)."""
    chain = FakeRunnerChain(
        first_emission_block_by_query={9_000_000: 8_123_456},
        hash_sequences={8_123_456: ["0xstart-before", "0xstart-after-the-reorg"]},
    )
    with pytest.raises(EmissionStateReorged, match="8123456"):
        observe_emission_state(chain, _UnreachableStore(), observed_block=9_000_000)


def test_no_reorg_answers_consistently_and_proceeds_normally_not_started(tmp_path):
    """The ordinary not-started case, made explicit: the fresh re-check
    agrees with the first answer (both None, the default with no sequence
    configured), and the recording proceeds exactly as it did before this
    reorg guard existed."""
    store = a_store(tmp_path)
    chain = FakeRunnerChain(
        first_emission_block_by_query={9_500_000: None},
        times={9_500_000: ANCHOR + 5 * DAY + 500},
    )
    observe_emission_state(chain, store, observed_block=9_500_000)
    assert store.subnet_was_emitting(5) is False
    # Both first_emission_block calls (the initial read, the fresh
    # re-check) actually happened -- not merely a vacuous pass because
    # the second one was never asked.
    assert chain.asked.count(("first_emission_block", 9_500_000)) == 2


def test_no_reorg_answers_consistently_and_proceeds_normally_started(tmp_path):
    """The ordinary started case, made explicit: the fresh re-check
    agrees with the first answer (both name the same real block, the
    default with no sequence configured), and the recording proceeds
    exactly as it did before this reorg guard existed."""
    store = a_store(tmp_path)
    chain = FakeRunnerChain(
        first_emission_block_by_query={9_000_000: 8_123_456},
        times={8_123_456: ANCHOR + 2 * DAY + 500},
        hashes={8_123_456: "0xstartblock"},
    )
    observe_emission_state(chain, store, observed_block=9_000_000)
    assert store.subnet_was_emitting(2) is True
    assert chain.asked.count(("first_emission_block", 9_000_000)) == 2


# ---- the not-started path: resolves only at or before the observed day ----


def test_absence_is_recorded_through_record_emission_not_started(tmp_path):
    store = a_store(tmp_path)
    chain = FakeRunnerChain(
        first_emission_block_by_query={9_500_000: None},
        times={9_500_000: ANCHOR + 5 * DAY + 500},
        hashes={9_500_000: "0xabsent"},
    )
    observe_emission_state(chain, store, observed_block=9_500_000)

    # Resolves the observed day and every earlier one...
    assert store.subnet_was_emitting(0) is False
    assert store.subnet_was_emitting(5) is False
    # ...but NOT a later one: this is the asymmetry the issue's own text
    # calls out as the subtlety that matters most.
    assert store.subnet_was_emitting(6) is None


def test_calling_it_again_at_a_later_block_re_resolves_a_day_the_first_call_left_open(tmp_path):
    """The issue's own required behaviour: a reader that records the
    absence once and stops leaves tomorrow refusing again. Calling
    observe_emission_state a second time, at a fresher observed_block --
    exactly what a second, later ChainAdapter.read() call does in
    production -- must re-record the absence and resolve the day the
    first call could not reach."""
    store = a_store(tmp_path)
    chain = FakeRunnerChain(
        first_emission_block_by_query={9_500_000: None, 9_600_000: None},
        times={
            9_500_000: ANCHOR + 5 * DAY + 500,
            9_600_000: ANCHOR + 6 * DAY + 500,
        },
        hashes={9_500_000: "0xabsent5", 9_600_000: "0xabsent6"},
    )

    observe_emission_state(chain, store, observed_block=9_500_000)
    assert store.subnet_was_emitting(6) is None  # not yet resolved

    observe_emission_state(chain, store, observed_block=9_600_000)
    assert store.subnet_was_emitting(6) is False  # now resolved, by the second call


def test_the_start_call_finally_landing_closes_the_question_for_good(tmp_path):
    """The full lifecycle: several not-started calls, as real days pass,
    then the start call lands and the very next call closes it
    permanently -- proving the two Store methods hand off correctly
    through this reader rather than only in isolation."""
    store = a_store(tmp_path)
    chain = FakeRunnerChain(
        first_emission_block_by_query={9_500_000: None, 9_600_000: None},
        times={
            9_500_000: ANCHOR + 5 * DAY + 500,
            9_600_000: ANCHOR + 6 * DAY + 500,
        },
        hashes={9_500_000: "0xa5", 9_600_000: "0xa6"},
    )
    observe_emission_state(chain, store, observed_block=9_500_000)
    observe_emission_state(chain, store, observed_block=9_600_000)
    assert store.subnet_was_emitting(7) is None

    # Day 7's own read: the start call landed inside day 6, block 9_650_000.
    chain.set_answer(9_700_000, 9_650_001)
    chain._times[9_650_001] = ANCHOR + 6 * DAY + 40_000
    chain._hashes[9_650_001] = "0xrealstart"
    observe_emission_state(chain, store, observed_block=9_700_000)

    assert store.subnet_was_emitting(6) is True
    assert store.subnet_was_emitting(7) is True
    assert store.subnet_was_emitting(999) is True


# ---- falsify: without a recording, the refusal genuinely fires -------------


def test_without_calling_this_reader_the_store_stays_unresolved(tmp_path):
    """The control: nothing here has recorded anything, so the exact
    refusal build_fixture/no_fault_for raise in production is what a
    caller actually gets. Proves the 'red' half of remove-confirm-red-
    restore-confirm-green for this reader: this is what production looked
    like before #567 wired a real caller."""
    store = a_store(tmp_path)
    assert store.subnet_was_emitting(0) is None


# ---- the required test: a real multi-day, multi-coldkey walk --------------
#
# Per issue #567's own verification bar: a test that only exercises
# build_fixture, or only the started path, or only trips the two upfront
# gates (fixture_build.py:200/:399), does not cover this. This walk uses
# ONLY observe_emission_state's own not-started recording (no start ever
# recorded), with eval_day sitting EXACTLY on the day the absence was
# observed, and three coldkeys whose own `first` activity days genuinely
# differ -- proving noah.chain.nofault.no_fault_for's own inner raise
# (fixture_build.py:748) stays unreachable for every (coldkey, day) pair
# the walk actually visits, not merely for the upfront eval_day check.

COLD_A = "COLDKEY-A-genuinely-different-first-day-0"
COLD_B = "COLDKEY-B-genuinely-different-first-day-2"
COLD_C = "COLDKEY-C-genuinely-different-first-day-5"
EVAL_DAY = 5

# [codex adversarial review of this exact commit, 2026-09-01, Low] block
# numbers used to be derived from Python's own hash((coldkey, day)),
# which is per-process randomized (PYTHONHASHSEED) for strings and could
# in principle collide across two different (coldkey, day) pairs within
# one test run. A fixed index per coldkey makes every block number in
# this walk deterministic and collision-free across runs instead.
_COLD_INDEX = {COLD_A: 0, COLD_B: 1, COLD_C: 2}


def _quiet_pay(store, coldkey, day, amount):
    """One day's worth of pay-and-lock reading for `coldkey` -- `amount`
    may be zero (an ordinary quiet day the walk still has to cover, since
    Store.locked_pay refuses over any day in [first, eval_day] with no
    lock reading at all) or nonzero (the day that establishes `first`).
    """
    block = 9_000_000 + _COLD_INDEX[coldkey] * 10_000 + day
    store.record_attributed_pay(
        coldkey=coldkey,
        hotkey=f"HOTKEY-of-{coldkey}",
        uid=1,
        block_timestamp=ANCHOR + day * DAY + 100,
        amount_rao=amount,
        block=block,
        block_hash=f"0xpay-{coldkey}-{day}",
    )
    store.record_locked_pay(
        coldkey=coldkey,
        block=block,
        block_timestamp=ANCHOR + day * DAY + 100,
        locked_arrival_rao=0,
        unlocked_rao=0,
        block_hash=f"0xpay-{coldkey}-{day}",
    )


def _walk_store(tmp_path):
    """A store with three coldkeys whose activity genuinely starts on
    different days (0, 2, 5), every day from each one's own `first`
    through EVAL_DAY scanned and lock-readable, and -- the fact under
    test -- ONLY an absence recorded, through observe_emission_state
    itself, at a block that lands exactly in EVAL_DAY. No start is ever
    recorded anywhere in this store."""
    store = a_store(tmp_path)
    for day in range(0, EVAL_DAY + 1):
        store.mark_day_scanned(day, from_block=1, to_block=2)

    firsts = {COLD_A: 0, COLD_B: 2, COLD_C: 5}
    for coldkey, first in firsts.items():
        for day in range(first, EVAL_DAY + 1):
            _quiet_pay(store, coldkey, day, amount=1000 if day == first else 0)

    chain = FakeRunnerChain(
        first_emission_block_by_query={9_999_000: None},
        times={9_999_000: ANCHOR + EVAL_DAY * DAY + 70_000},
        hashes={9_999_000: "0xabsent-at-eval-day"},
    )
    observe_emission_state(chain, store, observed_block=9_999_000)
    return store, firsts


def test_the_absence_resolves_exactly_through_eval_day_and_no_further(tmp_path):
    """Confirms the setup itself before trusting the walk built on it:
    the recorded absence lands in EVAL_DAY, exactly."""
    store, _ = _walk_store(tmp_path)
    assert store.subnet_was_emitting(EVAL_DAY) is False
    assert store.subnet_was_emitting(EVAL_DAY - 1) is False
    assert store.subnet_was_emitting(EVAL_DAY + 1) is None


def test_a_real_multi_day_multi_coldkey_walk_never_reaches_no_fault_for_s_inner_raise(tmp_path):
    """The test the issue's own text asks for. Three coldkeys, genuinely
    divergent `first` values (0, 2, 5), EVAL_DAY sitting exactly on the
    one day an absence was recorded, no start ever recorded -- and
    build_fixture (which walks every day in range(first, eval_day + 1)
    for every coldkey, calling no_fault_for once per coldkey per day at
    fixture_build.py:748) completes without raising.

    If the loop bound, subnet_was_emitting's own downward monotonicity, or
    either upfront gate's placement ever regresses, this is the test that
    catches it: the upfront gate alone (fixture_build.py:200) cannot,
    because it only ever asks about EVAL_DAY once."""
    store, firsts = _walk_store(tmp_path)

    fixture = build_fixture(
        store,
        name="issue-567-multi-day-walk",
        eval_day=EVAL_DAY,
        day_epoch_ts=ANCHOR,
        dials=DIALS,
        seats={},  # no current seats: every coldkey gets the null-uid row
    )

    assert fixture["evalDay"] == str(EVAL_DAY)
    miners_by_coldkey = {row["coldkey"]: row for row in fixture["miners"]}
    assert set(miners_by_coldkey) == set(firsts)
    for coldkey, first in firsts.items():
        row = miners_by_coldkey[coldkey]
        assert row["uid"] is None
        assert row["firstActivityDay"] == str(first)
        # Every day from this coldkey's own first through EVAL_DAY is
        # present. Store.subnet_was_emitting resolves every one of them to
        # False (not emitting), so no_fault_for's own not-emitting branch
        # runs on every day: `not store.pay_recorded(coldkey, day)`.
        # `first` itself carries the nonzero pay this test used to
        # establish it (a disagreement pay wins, per no_fault_for's own
        # docstring), so noFault is False there; every later day recorded
        # only a zero-amount row, so pay_recorded is False and noFault is
        # True.
        for day in range(first, EVAL_DAY + 1):
            expected = day != first
            assert row["days"][str(day)]["noFault"] is expected, (coldkey, day)


def test_removing_the_recording_makes_the_identical_walk_refuse_again(tmp_path):
    """Falsify, on the walk itself: the identical population, with the
    one call to observe_emission_state removed, refuses exactly the way
    production did before #567 -- proving the walk's own success above
    is actually load-bearing on the recording, not an accident of the
    fixture shape."""
    store = a_store(tmp_path)
    for day in range(0, EVAL_DAY + 1):
        store.mark_day_scanned(day, from_block=1, to_block=2)
    firsts = {COLD_A: 0, COLD_B: 2, COLD_C: 5}
    for coldkey, first in firsts.items():
        for day in range(first, EVAL_DAY + 1):
            _quiet_pay(store, coldkey, day, amount=1000 if day == first else 0)
    # No observe_emission_state call here -- the "removed" half of the
    # falsify pair.

    with pytest.raises(EmissionStateNotObserved):
        build_fixture(
            store,
            name="issue-567-multi-day-walk-unresolved",
            eval_day=EVAL_DAY,
            day_epoch_ts=ANCHOR,
            dials=DIALS,
            seats={},
        )


def test_the_third_refusal_site_withheld_from_also_clears_on_the_identical_recording(tmp_path):
    """The issue's own scope statement: "One recording must satisfy THREE
    refusal sites... withheld_from is confirmed NOT live-reachable today
    but must still be covered by a real test, since a future caller could
    reach it." build_fixture's own upfront gate and no_fault_for's own
    inner raise are covered by the walk above; this covers the third
    site, fixture_build.py's own withheld_from, on the SAME store the
    walk already populated through the real reader -- proving one
    recording really does clear all three, not just the two build_fixture
    itself reaches."""
    store, firsts = _walk_store(tmp_path)

    report = withheld_from(
        store,
        eval_day=EVAL_DAY,
        seats={},
    )
    # Nobody here is missing: every coldkey has a lock reading for every
    # day in its own range (this walk's own _quiet_pay), and none carries
    # a collateral hold-out or an unreadable evaluation-day holding.
    assert report["withheld"] == []
    assert set(firsts) <= set(store.wallets_seen())


def test_removing_the_recording_makes_withheld_from_refuse_again_too(tmp_path):
    """Falsify for the third site specifically: the identical population,
    with observe_emission_state never called, makes withheld_from raise
    the same EmissionStateNotObserved build_fixture raises -- proving
    #399's own gate is genuinely gated on this reader too, not merely
    coincidentally passing alongside build_fixture's check."""
    store = a_store(tmp_path)
    for day in range(0, EVAL_DAY + 1):
        store.mark_day_scanned(day, from_block=1, to_block=2)
    firsts = {COLD_A: 0, COLD_B: 2, COLD_C: 5}
    for coldkey, first in firsts.items():
        for day in range(first, EVAL_DAY + 1):
            _quiet_pay(store, coldkey, day, amount=1000 if day == first else 0)

    with pytest.raises(EmissionStateNotObserved):
        withheld_from(store, eval_day=EVAL_DAY, seats={})
