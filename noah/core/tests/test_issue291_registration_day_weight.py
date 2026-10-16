"""Issue #291: a miner registered after the pinned height of its day, with a
confirmed entry deposit, is published at its scored weight (the day-15 case),
and the protection against paying an old holder's score to a replacement stays.

Everything here runs the real `run_period` through the real `ChainAdapter`
(real `AgreeingChain`, `run_day`, `build_fixture`, `exclude_unconfirmed_entries`,
both scoring engines). Only the chain's data and the publish transport are fakes.
The seat map is block-dependent (`ScheduledSeats`), and `seats_source` is the
real `noah.chain.seats.current_seats` at the day's last scanned block, exactly
as `build_chain_adapter` wires it.

Timeline (day window 898..902, pay epochs at 899 and 900):
  pinned height H = 850   (the first pinned height of a catch-up day)
  block 880               a newcomer takes uid 2
  scored seat map         = seats at block 902 (the day's last scanned block)
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ...chain.seats import current_seats
from ...chain.store import Store
from ...chain.tests._agreeing_helpers import (
    ARK,
    EPOCH,
    NETUID,
    SEAT0_COLD,
    SEAT0_HOT,
    SEAT1_COLD,
    SEAT1_HOT,
    FullSource,
    pay_event,
)
from ...chain.agreeing import AgreeingChain
from ..chain_adapter import ChainAdapter
from ..period import run_period, submission_vector
from ._adapter_helpers import (
    DIALS,
    FLOORED_ANCHOR,
    FakePublisher,
    FakeReader,
    _named_transfer,
    an_adapter,
)

# Synthetic, distinct public-key identities for the registration-day regression.
NEW_HOT = "5DveEmxm9ZiZ4Y3UpgkRsJ5Lzjf3bBtFbWJCpSnPpWoRBMuy"
NEW_COLD = "5DyGvvbrZd2vKPevHFk9W5yZ1wUeXM4mrbxSXSQrLkP87ehD"
OTHER_HOT = "5DwxarHJrbNjgxrCYyFHgh2T1LZr4GUWj48Kfwbd5dbGeSRy"
VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
ENTRY = 100_000_000_000
PINNED = 850
DAY_LAST_BLOCK = 902
NOW = EPOCH + 100


class ScheduledSeats(FullSource):
    """seat_hotkeys(block) answers the latest schedule entry at or before block."""

    def __init__(self, schedule, reg_by_hotkey=None, **kwargs):
        self._schedule = sorted(schedule.items())
        self._reg_by_hotkey = reg_by_hotkey or {}
        super().__init__(seats=dict(self._schedule[-1][1]), **kwargs)

    def seat_hotkeys(self, block):
        self.asked.append(("seats", block))
        current = {}
        for start, seats in self._schedule:
            if start <= block:
                current = seats
        return dict(current)

    def registration_blocks(self, block):
        self.asked.append(("registrations", block))
        return {uid: self._reg_by_hotkey.get(hk, 100) for uid, hk in self.seat_hotkeys(block).items()}

    def subnetwork_n(self, block):
        return len(self.seat_hotkeys(block))


def entry_deposit(coldkey, hotkey, index):
    return [
        {
            "ordinal_in_block": 10 + 2 * index,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": 3 + index,
            "module_id": "SubtensorModule",
            "event_id": "StakeAdded",
            "attributes": [ARK, VALIDATOR, ENTRY, ENTRY, NETUID, 0],
        },
        {
            "ordinal_in_block": 11 + 2 * index,
            "phase": "ApplyExtrinsic",
            "extrinsic_idx": 3 + index,
            "module_id": "SubtensorModule",
            "event_id": "StakeAndHotkeyTransferred",
            "attributes": dict(
                _named_transfer(coldkey, ARK, hotkey, NETUID, ENTRY),
                destination_hotkey=VALIDATOR,
            ),
        },
    ]


def run(schedule, owners, pays, depositors, tmp, old_snapshot=False, reg_by_hotkey=None):
    """One real period. ``pays`` is {block: [amount per uid]}; ``depositors`` is
    [(coldkey, hotkey)] with a confirmed entry deposit at block 899."""
    events = {block: [pay_event(amounts)] for block, amounts in pays.items()}
    events[EPOCH - 1] = events.get(EPOCH - 1, [])
    for i, (cold, hot) in enumerate(depositors):
        events[EPOCH - 1] = events[EPOCH - 1] + entry_deposit(cold, hot, i)
    all_owners = {SEAT0_HOT: SEAT0_COLD, SEAT1_HOT: SEAT1_COLD, NEW_HOT: NEW_COLD}
    all_owners.update(owners)
    kwargs = dict(
        schedule=schedule,
        events=events,
        head=NOW,
        first_emission_block=EPOCH - 2,
        reg_by_hotkey={NEW_HOT: 880, **(reg_by_hotkey or {})},
        owners=all_owners,
    )
    chain = AgreeingChain(
        sources={"a": ScheduledSeats(**kwargs), "b": ScheduledSeats(**kwargs)},
        endpoints={"a": "wss://a.example", "b": "wss://b.example"},
    )
    live = sorted(schedule[max(schedule)])
    store_path = Path(tmp) / "adapter.db"
    adapter, _, publisher = an_adapter(
        reader=FakeReader(current_block=NOW, tempo=1000, committed=set(), live_uids=live, owner_uids=set()),
        publisher=FakePublisher(),
        runner_chain=chain,
        seats_source=lambda day, store: current_seats(chain, store.scanned_range(day)[1]),
        holding_stride=1,
        day_window=lambda height: (0, EPOCH - 2, DAY_LAST_BLOCK),
        ark_coldkey=ARK,
        designated_validator_hotkey=VALIDATOR,
        dials=DIALS,
        day_anchor_ts=FLOORED_ANCHOR,
        read_collateral=True,
        store_factory=lambda: Store.create(store_path, day_anchor_ts=FLOORED_ANCHOR),
        settle_blocks=1,
        entry_deposit_rao=ENTRY,
        submitted_record_dir=Path(tmp) / "scores",
    )
    adapter.pinned_height = lambda: PINNED
    fixture = adapter.read(PINNED)
    agreed = adapter.agree(fixture)
    if old_snapshot:
        old = lambda self, height: self.runner_chain.seat_hotkeys(height)  # noqa: E731
        with mock.patch.object(ChainAdapter, "fixture_hotkeys", old):
            result = run_period(adapter)
    else:
        result = run_period(adapter)
    record = json.loads((Path(tmp) / "scores" / "day-0.submitted.json").read_text())
    submitted = {int(u): w for u, w in record["submitted_weights_u16"].items()}
    return dict(
        adapter=adapter, fixture=fixture, agreed=agreed, result=result,
        record=record, submitted=submitted, publisher=publisher,
    )


OLD_TWO = {0: SEAT0_HOT, 1: SEAT1_HOT}
NEW_THREE = {0: SEAT0_HOT, 1: SEAT1_HOT, 2: NEW_HOT}
DEPOSITORS = [(SEAT0_COLD, SEAT0_HOT), (SEAT1_COLD, SEAT1_HOT), (NEW_COLD, NEW_HOT)]
PAYS = {EPOCH: [700, 300, 500]}


class NewcomerRegisteredAfterThePinnedHeight(unittest.TestCase):
    """(a) the day-15 case and (b) the unchanged miners beside it."""

    def test_a_newcomer_with_a_confirmed_entry_is_published_at_its_scored_weight(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
        self.assertGreater(r["agreed"][2], 0, "the newcomer must actually be scored")
        self.assertEqual(r["submitted"][2], r["agreed"][2])

    def test_existing_miners_keep_exactly_their_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
        for uid in (0, 1):
            self.assertGreater(r["agreed"][uid], 0)
            self.assertEqual(r["submitted"][uid], r["agreed"][uid])

    def test_the_old_snapshot_rule_zeroed_the_newcomer_and_nobody_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp, old_snapshot=True)
        self.assertEqual(r["submitted"][2], 0)
        self.assertEqual(r["submitted"][0], r["agreed"][0])
        self.assertEqual(r["submitted"][1], r["agreed"][1])

    def test_fixture_hotkeys_is_the_scored_map_for_that_read_and_the_snapshot_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
            adapter = r["adapter"]
            self.assertEqual(adapter.fixture_hotkeys(PINNED), NEW_THREE)
            # a height no read ran at falls back to the chain snapshot there
            self.assertEqual(adapter.fixture_hotkeys(PINNED - 1), OLD_TWO)


class SeatReplacedAfterTheScoredMap(unittest.TestCase):
    """(c) the protection stays: hotkey changed between the scored map and now."""

    def test_the_replaced_uid_publishes_zero_and_the_others_do_not_change(self):
        schedule = {0: OLD_TWO, 880: NEW_THREE, 950: {0: SEAT0_HOT, 1: SEAT1_HOT, 2: OTHER_HOT}}
        with tempfile.TemporaryDirectory() as tmp:
            r = run(schedule, {OTHER_HOT: NEW_COLD}, PAYS, DEPOSITORS, tmp, reg_by_hotkey={OTHER_HOT: 950})
        self.assertGreater(r["agreed"][2], 0)
        self.assertEqual(r["submitted"][2], 0)
        self.assertEqual(r["submitted"][0], r["agreed"][0])
        self.assertEqual(r["submitted"][1], r["agreed"][1])


class HotkeyReplacedDuringTheDay(unittest.TestCase):
    """(d) A holds uid 2 from block 880 and is paid at 898; B (another coldkey)
    registers over it at 899 and is paid at 900. The scored seat map is the
    day-end one, so uid 2 is B's."""

    B_COLD = SEAT1_COLD  # any coldkey other than A's; B is a second hotkey of it

    def _run(self, tmp):
        a_seats = {0: SEAT0_HOT, 1: SEAT1_HOT, 2: NEW_HOT}
        b_seats = {0: SEAT0_HOT, 1: SEAT1_HOT, 2: OTHER_HOT}
        pays = {898: [100, 100, 400], EPOCH: [700, 300, 500]}
        depositors = DEPOSITORS + [(self.B_COLD, OTHER_HOT)]
        return run(
            {0: OLD_TWO, 880: a_seats, 899: b_seats},
            {OTHER_HOT: self.B_COLD},
            pays,
            depositors,
            tmp,
            reg_by_hotkey={OTHER_HOT: 899},
        )

    def test_uid_2_is_scored_for_b_only_and_a_pay_never_reaches_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp)
        rows = {(row["coldkey"], row["uid"]): row for row in r["fixture"]["miners"]}

        def paid(row):
            return int(row["days"]["0"]["paidRao"])

        # uid 2 is B's: its row carries only what B's hotkey earned (500). A's
        # pay (400, earned at block 898 while A held the seat) sits on A's
        # coldkey's unallocated row (uid None) and is never published.
        self.assertEqual(paid(rows[(self.B_COLD, 2)]), 500)
        self.assertEqual(paid(rows[(NEW_COLD, None)]), 400)
        self.assertNotIn((NEW_COLD, 2), rows)

    def test_published_vector_pays_uid_2_what_b_scored_and_nothing_for_a(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp)
        self.assertEqual(r["submitted"][2], r["agreed"][2])
        self.assertEqual(set(r["submitted"]), {0, 1, 2})  # no uid for A's null row


class ASubmissionRecordEqualsWhatRunPeriodSubmits(unittest.TestCase):
    def test_record_vector_equals_the_vector_handed_to_the_publisher(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
        sealed_uids, sealed_values = r["publisher"].sealed[0][1], r["publisher"].sealed[0][2]
        self.assertEqual(list(sealed_uids), sorted(r["submitted"]))
        self.assertEqual(
            [value == 0 for value in sealed_values],
            [r["submitted"][uid] == 0 for uid in sealed_uids],
        )
        self.assertEqual(r["record"]["day"], 0)
        self.assertEqual(r["record"]["uid"], 0)
        self.assertEqual(r["record"]["height"], PINNED)
        self.assertEqual(r["record"]["netuid"], NETUID)
        self.assertEqual(r["record"]["publish_mode"], "sealed")
        self.assertTrue(r["result"]["submitted_record"]["recorded"])

    def test_the_pipeline_helper_computes_the_same_vector_as_run_period_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
            _decision, again = submission_vector(r["adapter"], PINNED, r["agreed"])
        self.assertEqual(again, r["submitted"])

    def test_a_record_write_failure_never_fails_a_published_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "scores"
            # Fail only the post-publication operator record, after durable calculation.
            events = {EPOCH: [pay_event([700, 300])]}
            chain = AgreeingChain(
                sources={
                    "a": ScheduledSeats({0: OLD_TWO}, events=events, head=NOW, first_emission_block=EPOCH - 2),
                    "b": ScheduledSeats({0: OLD_TWO}, events=events, head=NOW, first_emission_block=EPOCH - 2),
                },
                endpoints={"a": "wss://a.example", "b": "wss://b.example"},
            )
            adapter, _, publisher = an_adapter(
                reader=FakeReader(current_block=NOW, tempo=1000, committed=set(), live_uids=[0, 1], owner_uids=set()),
                runner_chain=chain,
                seats_source=lambda day, store: current_seats(chain, store.scanned_range(day)[1]),
                holding_stride=1,
                day_window=lambda height: (0, EPOCH - 2, DAY_LAST_BLOCK),
                ark_coldkey=ARK,
                designated_validator_hotkey=VALIDATOR,
                dials=DIALS,
                day_anchor_ts=FLOORED_ANCHOR,
                read_collateral=True,
                store_factory=lambda: Store.create(Path(tmp) / "a.db", day_anchor_ts=FLOORED_ANCHOR),
                entry_deposit_rao=ENTRY,
                submitted_record_dir=blocker,
            )
            adapter.pinned_height = lambda: PINNED
            with mock.patch("noah.core.chain_adapter.write_submitted_record",
                            side_effect=OSError("post-send record failure")):
                result = run_period(adapter)
        self.assertEqual(len(publisher.sealed), 1)
        self.assertFalse(result["submitted_record"]["recorded"])


class TheDayCloseEvidence(unittest.TestCase):
    """The day-close script records the vector run_period would submit, from the
    same function and the same adapter, without touching the engine output."""

    @staticmethod
    def _pipeline():
        import importlib.util

        path = Path(__file__).resolve().parents[3] / "scripts" / "day_close_pipeline.py"
        spec = importlib.util.spec_from_file_location("day_close_pipeline_291", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_publishable_vector_in_the_evidence_equals_what_run_period_submitted(self):
        pipeline = self._pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
            out = pipeline.run_pipeline(r["adapter"], PINNED)
        evidence = pipeline.evidence_output(out)
        publishable = {int(u): w for u, w in evidence["publishable_weights_u16"].items()}
        self.assertEqual(publishable, r["submitted"])
        self.assertGreater(publishable[2], 0)
        # the engine output is untouched and still named weightsU16
        self.assertEqual(evidence["weightsU16"], out["score"]["weightsU16"])
        self.assertNotIn("publishable_weights_u16", out["score"])
        self.assertNotIn("submitted_weights_u16", evidence)

    def test_the_evidence_shows_the_zero_for_a_replaced_seat_that_the_engine_output_lacks(self):
        pipeline = self._pipeline()
        schedule = {0: OLD_TWO, 880: NEW_THREE, 950: {0: SEAT0_HOT, 1: SEAT1_HOT, 2: OTHER_HOT}}
        with tempfile.TemporaryDirectory() as tmp:
            r = run(schedule, {OTHER_HOT: NEW_COLD}, PAYS, DEPOSITORS, tmp, reg_by_hotkey={OTHER_HOT: 950})
            out = pipeline.run_pipeline(r["adapter"], PINNED)
        evidence = pipeline.evidence_output(out)
        self.assertGreater(evidence["weightsU16"]["2"], 0)  # calculated
        self.assertEqual(evidence["publishable_weights_u16"]["2"], 0)  # would be submitted
        self.assertEqual({int(u): w for u, w in evidence["publishable_weights_u16"].items()}, r["submitted"])

    def test_a_manual_publish_records_exactly_the_vector_it_passed_to_publish(self):
        pipeline = self._pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
            record_path = Path(tmp) / "scores" / "day-0.submitted.json"
            record_path.unlink()
            out = pipeline.run_pipeline(r["adapter"], PINNED, publish=True)
            record = json.loads(record_path.read_text())
        passed = r["publisher"].sealed[-1]
        self.assertEqual(sorted(int(u) for u in record["submitted_weights_u16"]), list(passed[1]))
        self.assertTrue(out["weights"]["submitted_record"]["recorded"])

    def test_a_vector_that_cannot_be_computed_is_recorded_as_text_and_no_key_is_added(self):
        pipeline = self._pipeline()

        class Bare:
            def read(self, height):
                return {"schema": "noah-fixture/6", "evalDay": "1", "miners": []}

            def score(self, observations):
                return {"schema": "noah-score-output/6", "evalDay": "1", "miners": [], "weightsU16": {"0": 1}}

            def agree(self, observations):
                return {0: 1}

        with mock.patch(
            "compare_score.compare_fixture",
            return_value={"agree": True, "fixture_name": "t", "miner_count": 0},
        ):
            out = pipeline.run_pipeline(Bare(), 1)
        self.assertIn("publishable_vector_error", out["weights"])
        self.assertEqual(pipeline.evidence_output(out), out["score"])


class TheManualPublishPathSendsWhatRunPeriodSends(unittest.TestCase):
    """day_close_pipeline --publish and --publish-direct hand the publisher
    `submission_vector`'s vector, the one run_period sends."""

    _loaded = None

    @classmethod
    def _pipeline(cls):
        if cls._loaded is None:
            cls._loaded = TheDayCloseEvidence._pipeline()
        return cls._loaded

    class Adapter:
        """Just enough adapter for run_pipeline: the engine answer and the
        chain facts submission_vector reads are set by the test."""

        def __init__(self, agreed, live, fixture_hotkeys, chain_hotkeys, owners=(), unknown=()):
            self.agreed, self.live, self.owners = agreed, live, set(owners)
            self._fixture, self._chain = fixture_hotkeys, chain_hotkeys
            self.sent = []

        def read(self, height):
            return {"schema": "noah-fixture/6", "evalDay": "1", "miners": []}

        def score(self, observations):
            return {"schema": "noah-score-output/6", "fixture": "t", "miners": [], "weightsU16": {}}

        def agree(self, observations):
            return dict(self.agreed)

        def live_uids(self, height):
            return self.live

        def owner_uids(self):
            return self.owners

        def fixture_hotkeys(self, height):
            return self._fixture

        def chain_hotkeys(self):
            return self._chain

        def already_published(self, height):
            return False

        def record_commit(self, height, published):
            return {"recorded": True}

        def publish(self, vector, height):
            self.sent.append(("publish", dict(vector)))
            return {"confirmed_on_chain": True, "block": 5}

        def publish_direct(self, vector, height):
            self.sent.append(("direct", dict(vector)))
            return {"applied_weights_readback": {}, "block": 5}

    def _go(self, adapter, **flags):
        pipeline = self._pipeline()
        with mock.patch(
            "compare_score.compare_fixture",
            return_value={"agree": True, "fixture_name": "t", "miner_count": 0},
        ):
            return pipeline, pipeline.run_pipeline(adapter, 1, **flags)

    def test_a_zero_miner_day_sends_every_live_seat_at_zero_never_an_empty_vector(self):
        same = {0: "a", 1: "b", 2: "c"}
        for flags, kind in (({"publish": True}, "publish"), ({"publish_direct": True}, "direct")):
            adapter = self.Adapter({}, [0, 1, 2], same, same)
            self._go(adapter, **flags)
            self.assertEqual(adapter.sent, [(kind, {0: 0, 1: 0, 2: 0})])

    def test_a_newcomer_registered_after_the_pinned_height_keeps_its_weight(self):
        adapter = self.Adapter({0: 100, 1: 50}, [0, 1], {0: "a", 1: "b"}, {0: "a", 1: "b"})
        # the scored map (what read() recorded) already holds both; the live map matches
        self._go(adapter, publish=True)
        self.assertEqual(adapter.sent, [("publish", {0: 100, 1: 50})])

    def test_a_replaced_seat_is_sent_as_zero(self):
        adapter = self.Adapter({0: 100, 1: 50}, [0, 1], {0: "a", 1: "b"}, {0: "a", 1: "other"})
        self._go(adapter, publish=True)
        self.assertEqual(adapter.sent, [("publish", {0: 100, 1: 0})])

    def test_an_unread_seat_owner_refuses_and_sends_nothing(self):
        from ..vector import SeatOwnerUnknown

        adapter = self.Adapter({0: 100}, [0, 1], {0: "a"}, {0: "a"})
        pipeline = self._pipeline()
        for flags in ({"publish": True}, {"publish_direct": True}):
            with self.assertRaises(pipeline.PipelineStepFailed) as caught:
                self._go(adapter, **flags)
            self.assertIsInstance(caught.exception.error, SeatOwnerUnknown)
            self.assertIn("seat 1", str(caught.exception))
        self.assertEqual(adapter.sent, [])

    def test_an_owner_seat_that_would_be_paid_refuses_and_sends_nothing(self):
        adapter = self.Adapter({0: 100}, [0], {0: "a"}, {0: "a"}, owners={0})
        pipeline = self._pipeline()
        with self.assertRaises(pipeline.PipelineStepFailed) as caught:
            self._go(adapter, publish=True)
        self.assertIn("are ours", str(caught.exception))
        self.assertEqual(adapter.sent, [])

    def test_the_manual_path_and_run_period_send_the_identical_vector(self):
        pipeline = self._pipeline()
        schedule = {0: OLD_TWO, 880: NEW_THREE, 950: {0: SEAT0_HOT, 1: SEAT1_HOT, 2: OTHER_HOT}}
        with tempfile.TemporaryDirectory() as tmp:
            r = run(schedule, {OTHER_HOT: NEW_COLD}, PAYS, DEPOSITORS, tmp, reg_by_hotkey={OTHER_HOT: 950})
            by_run_period = list(r["publisher"].sealed[-1][1:])
            before = len(r["publisher"].sealed)
            pipeline.run_pipeline(r["adapter"], PINNED, publish=True)
            by_manual = list(r["publisher"].sealed[-1][1:])
        self.assertEqual(len(r["publisher"].sealed), before + 1)
        self.assertEqual(by_manual, by_run_period)
        self.assertEqual(by_manual[1][2], 0)  # the replaced seat

    def test_the_day_15_case_through_the_manual_path_keeps_the_newcomers_weight(self):
        pipeline = self._pipeline()
        with tempfile.TemporaryDirectory() as tmp:
            r = run({0: OLD_TWO, 880: NEW_THREE}, {}, PAYS, DEPOSITORS, tmp)
            pipeline.run_pipeline(r["adapter"], PINNED, publish=True)
            uids, values = r["publisher"].sealed[-1][1:]
        self.assertEqual(list(uids), [0, 1, 2])
        self.assertGreater(dict(zip(uids, values))[2], 0)

    def test_a_dry_run_sends_nothing(self):
        adapter = self.Adapter({0: 100}, [0], {0: "a"}, {0: "a"})
        self._go(adapter)
        self.assertEqual(adapter.sent, [])


class TheDay15Reproduction(unittest.TestCase):
    """Regression weights with synthetic hotkeys for uids201,202,203. Uids1-200
    carry made-up hotkeys, identical in every map (they never changed hands).
    The pinned height 101159 is the start of the day; 202 (registered 101638)
    and 203 (registered 104452) are absent from a snapshot there. OLD is the
    snapshot rule (`runner_chain.seat_hotkeys(pinned)`), NEW is the adapter."""

    def setUp(self):
        fixture = Path(__file__).parent / "fixtures" / "issue291_day15_weights.json"
        data = json.loads(fixture.read_text())
        self.scored = {int(u): w for u, w in data["weightsU16"].items()}
        self.pinned, self.head = data["pinned_height"], data["head"]
        hotkeys = {int(u): h for u, h in data["hotkeys"].items()}
        self.at_pinned = {u: f"synthetic-{u}" for u in range(0, 201)}
        self.at_pinned[201] = hotkeys[201]
        self.now = {**self.at_pinned, 202: hotkeys[202], 203: hotkeys[203]}
        chain = AgreeingChain(
            sources={
                name: ScheduledSeats({0: self.at_pinned, 104452: self.now}, head=self.head)
                for name in ("a", "b")
            },
            endpoints={"a": "wss://a.example", "b": "wss://b.example"},
        )
        self.adapter, _, _ = an_adapter(
            reader=FakeReader(live_uids=list(range(204)), owner_uids=set()), runner_chain=chain
        )

    def _submit(self, adapter):
        _decision, vector = submission_vector(adapter, self.pinned, self.scored)
        return vector

    def test_old_rule_published_202_and_203_as_zero(self):
        old = lambda adapter, height: adapter.runner_chain.seat_hotkeys(height)  # noqa: E731
        with mock.patch.object(ChainAdapter, "fixture_hotkeys", old):
            vector = self._submit(self.adapter)
        self.assertEqual((vector[202], vector[203]), (0, 0))
        self.assertEqual(vector[201], self.scored[201])

    def test_new_rule_publishes_their_scored_weights_and_changes_nobody_else(self):
        # the seat map read(pinned) scored: the day's last scanned block
        self.adapter._scored_seat_hotkeys = (self.pinned, dict(self.now))
        vector = self._submit(self.adapter)
        self.assertEqual((vector[202], vector[203]), (287, 287))
        for uid in range(1, 202):
            self.assertEqual(vector[uid], self.scored[uid], uid)
        self.assertEqual(vector[0], 0)


if __name__ == "__main__":
    unittest.main()
