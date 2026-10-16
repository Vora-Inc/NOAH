"""Issue #291: an open publish-intent marker stops automatic sending.

Same scenario the ledger test pins as a duplicate (`[10, 10]`): the sealed commit lands on
chain, the process dies before anything is recorded, the restart finds the day still pending
and the commit outside the day's scan window. With the marker the restart must send nothing,
say why in loop.json, and resume only after the chain shows the commit or a person reconciles.
Real loop.tick, real run_period, real sealed ChainAdapter; only the chain client and the
publish transport are fakes.
"""

from unittest import mock

from .. import loop
from ..publish_intent import REASON_PREFIX, PublishIntent
from ._adapter_helpers import TrivialHotkey, an_adapter
from .test_issue334_commit_ledger import T, LedgerChain, WithState, process


class MarkerChain(LedgerChain):
    def __init__(self, head, ledger_path, intent_path):
        self._intent_path = intent_path
        super().__init__(head, ledger_path)
        self.crash_after_landing = False

    def publish(self, vector, height):
        def land(chain, **kwargs):
            landed = LedgerChain.publish(self, vector, height)
            if self.crash_after_landing:
                raise SystemExit("process killed after the chain accepted the commit")
            return landed

        with mock.patch("noah.core.chain_adapter.publish_weights", side_effect=land):
            return self.adapter.publish(vector, height)


class PublishIntentStopsAutomaticSending(WithState):
    def test_open_marker_blocks_a_second_send_and_a_found_commit_clears_it(self):
        intent = self.dir / "publish-intent.json"
        ch = MarkerChain(T(11) + 2000, self.ledger, intent)
        ch.adapter._publish_intent = PublishIntent(intent)  # same object the constructor builds
        built, _, _ = an_adapter(publish_intent_path=intent)
        self.assertEqual(built._publish_intent.path, intent)
        pending = {"day": 10, "height": 78479}

        ch.crash_after_landing = True
        with self.assertRaises(SystemExit):
            process(ch, self.dir, pending, 78479, [T(11) + 2000])
        self.assertEqual(ch.committed_days, [10])
        self.assertTrue(intent.exists(), "the marker was written before the send")
        self.assertFalse(self.ledger.exists(), "the crash came before the ledger write")

        # Restart: without the marker this commits day 10 again (see the ledger test).
        ch.crash_after_landing = False
        out = process(ch, self.dir, dict(pending), 78479, [T(11) + 2100])
        self.assertEqual([o for _, o in out], ["waiting"])
        state = loop.read_state(self.dir / "loop.json")
        self.assertIn(REASON_PREFIX, state["reason"])
        process(ch, self.dir, dict(pending), 78479, [T(11) + 2101, T(11) + 2102])
        self.assertEqual(ch.committed_days, [10], ch.log)
        self.assertTrue(intent.exists())

        # A person finds nothing and moves the marker aside: sending resumes (a person's call).
        intent.rename(self.dir / "publish-intent.reconciled.json")
        process(ch, self.dir, dict(pending), 78479, [T(11) + 2200])
        self.assertEqual(ch.committed_days, [10, 10], "resumed only after reconcile")
        self.assertFalse(intent.exists(), "a confirmed commit clears its own marker")


class TheMarkerIsClearedOnlyWhenNothingWasAccepted(WithState):
    def test_which_failures_clear_the_marker_and_which_leave_it_open(self):
        from ..publish import CommitRejected, NotOnChain, PublicationRefused

        cases = [
            (CommitRejected("chain refused"), False),
            (PublicationRefused("refused before any submit"), False),
            (NotOnChain("accepted, not confirmed"), True),
            (RuntimeError("anything else"), True),
        ]
        for error, stays_open in cases:
            with self.subTest(error=type(error).__name__):
                intent = self.dir / f"intent-{type(error).__name__}.json"
                built, _, _ = an_adapter(publish_intent_path=intent)
                with mock.patch("noah.core.chain_adapter.publish_weights", side_effect=error):
                    with self.assertRaises(type(error)):
                        built.publish({}, 1000)
                self.assertEqual(intent.exists(), stays_open)


class TheMarkerNamesTheTransaction(WithState):
    """Task 3: after a send whose result is lost, the marker file holds what a person needs to
    identify the exact transaction. Real ChainAdapter.publish, real publish_weights; only the
    seal/submit transport is a fake."""

    def test_marker_after_a_lost_send_carries_the_transaction_identity(self):
        import hashlib
        import json

        from ._adapter_helpers import FakePublisher

        class LostSend(FakePublisher):
            def seal(self, uids, values, hotkey):
                return b"sealed-commit-bytes", 4242

            def submit(self, commit, reveal_round, netuid):
                return {"error": "TimeoutError: no answer", "block": None, "indeterminate": True}

            def send_context(self, address):
                return {"account_nonce_at_head": 7, "head_at_send": 1234, "era_period": 128}

        intent = self.dir / "publish-intent.json"
        built, _, _ = an_adapter(
            publisher=LostSend(),
            publish_intent_path=intent,
            day_window=lambda height: (10, 0, height),
        )
        from ..publish import NotOnChain

        with self.assertRaises(NotOnChain):
            built.publish({0: 100, 1: 200}, 78479)
        doc = json.loads(intent.read_text())
        self.assertEqual(doc["signer"], "5FakeHotkey")
        self.assertEqual(doc["hotkey"], "5FakeHotkey")
        self.assertEqual(doc["call"], "SubtensorModule.commit_timelocked_mechanism_weights")
        self.assertEqual(doc["netuid"], built.netuid)
        self.assertEqual(doc["commit_sha256"], hashlib.sha256(b"sealed-commit-bytes").hexdigest())
        self.assertEqual((doc["day"], doc["height"]), (10, 78479))
        self.assertEqual(doc["reveal_round"], 4242)
        self.assertEqual(doc["account_nonce_at_head"], 7)
        self.assertEqual(doc["head_at_send"], 1234)
        self.assertEqual(doc["era_period"], 128)
        self.assertTrue(doc["sent_at"] and doc["at"])
        self.assertNotIn("extrinsic_hash", doc, "submit_call exposes none before inclusion")

    def test_publisher_send_context_is_best_effort_and_never_raises(self):
        from ..submitter import Publisher

        class Client:
            block = 555

            def query(self, item, params=None, block=None):
                assert item == ("System", "Account")
                return {"nonce": 3, "data": {}}

        publisher = Publisher(Client(), TrivialHotkey(), netuid=2)
        got = publisher.send_context("5Addr")
        self.assertEqual((got["account_nonce_at_head"], got["head_at_send"]), (3, 555))

        class Broken:
            @property
            def block(self):
                raise RuntimeError("down")

            def query(self, *a, **k):
                raise RuntimeError("down")

        got = Publisher(Broken(), TrivialHotkey(), netuid=2).send_context("5Addr")
        self.assertIsNone(got["account_nonce_at_head"])
        self.assertIsNone(got["head_at_send"])
