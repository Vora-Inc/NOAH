"""The proxy publication route (issue #16): who commits, who signs.

TEST identities only: addresses derived from made-up seed strings, no secret key exists for
any of them, none is a real address. Fakes stand in for the chain client and the submit
transport; what runs for real is the allowlist, the identity checks, the wrapper
composition, the sealing call's arguments, the inner-result classification, the real
`ChainAdapter.publish`/`record_commit` and the failure limit.

NOT covered here (no chain): that a real chain accepts the wrapped call, that its reveal opens
our payload, and that bittensor's `submit_call` returns the events this reads. See the PR.
"""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ...chain.ss58 import address_of, public_key_of
from .. import proxy_route as pr
from ..publish import CommitRejected, NotOnChain, PublicationRefused
from ..settings import Settings
from ..wallet import CallNotPermitted, permitted, permitted_proxy_wrapper
from ._composed_call import ComposesCalls, inner_call


def addr(seed):
    return address_of(hashlib.sha256(seed.encode()).digest())


VALIDATOR = addr("noah-test-validator-identity")
SIGNER = addr("noah-test-proxy-signer")
OTHER = addr("noah-test-other-delegate")
STRANGER = addr("noah-test-stranger")


def settings(**extra):
    mapping = {
        "NOAH_NETWORK": "local",
        "NOAH_EXPECTED_SIGNER_ADDRESS": VALIDATOR,
        "NOAH_PROXY_SIGNER_ADDRESS": SIGNER,
    }
    mapping.update(extra)
    mapping = {k: v for k, v in mapping.items() if v is not None}
    return SimpleNamespace(
        expected_signer_address=mapping.get("NOAH_EXPECTED_SIGNER_ADDRESS"),
        proxy_signer_address=mapping.get("NOAH_PROXY_SIGNER_ADDRESS"),
        network=mapping["NOAH_NETWORK"],
    )


class CommitIdentityTests(unittest.TestCase):
    def test_it_carries_the_public_key_the_sealed_payload_needs(self):
        identity = pr.CommitIdentity(VALIDATOR)
        self.assertEqual(identity.ss58_address, VALIDATOR)
        self.assertEqual(identity.public_key, public_key_of(VALIDATOR))
        self.assertNotEqual(identity.public_key, public_key_of(SIGNER))

    def test_it_refuses_text_that_is_not_an_address(self):
        with self.assertRaises(pr.ProxyRouteRefused):
            pr.CommitIdentity("REPLACE-ME")

    def test_it_cannot_sign(self):
        self.assertFalse(hasattr(pr.CommitIdentity(VALIDATOR), "sign"))


class SigningIdentityChecks(unittest.TestCase):
    def test_the_dedicated_signer_loaded_is_accepted(self):
        pr.check_proxy_signing_identity(settings(), SIGNER)

    def test_the_validators_own_key_loaded_is_refused(self):
        with self.assertRaises(pr.ProxyRouteRefused) as raised:
            pr.check_proxy_signing_identity(settings(), VALIDATOR)
        self.assertIn("must never be loaded on a server", str(raised.exception))

    def test_a_third_key_loaded_is_refused(self):
        with self.assertRaises(pr.ProxyRouteRefused):
            pr.check_proxy_signing_identity(settings(), STRANGER)

    def test_a_proxy_signer_equal_to_the_validator_is_refused(self):
        with self.assertRaises(pr.ProxyRouteRefused):
            pr.check_proxy_signing_identity(settings(NOAH_PROXY_SIGNER_ADDRESS=VALIDATOR), VALIDATOR)

    def test_proxy_mode_needs_the_validators_address(self):
        with self.assertRaises(pr.ProxyRouteRefused):
            pr.commit_address(settings(NOAH_EXPECTED_SIGNER_ADDRESS=None), SIGNER)

    def test_validator_questions_are_asked_about_the_validator_not_the_loaded_key(self):
        self.assertEqual(pr.commit_address(settings(), SIGNER), VALIDATOR)

    def test_without_the_proxy_route_everything_is_as_before(self):
        off = settings(NOAH_PROXY_SIGNER_ADDRESS=None)
        self.assertIsNone(pr.identity_from_settings(off))
        self.assertEqual(pr.commit_address(off, "5Loaded"), "5Loaded")
        pr.check_proxy_signing_identity(off, "5Loaded")  # a no-op
        self.assertEqual(pr.gate_lines(SimpleNamespace(client=None), off), [])


class FakeClient:
    def __init__(self, proxies=None, free=5_000_000_000, fail=None):
        self.proxies = proxies
        self.free = free
        self.fail = fail
        self.reads = []

    def query(self, key, params=None, block=None):
        self.reads.append((key, params))
        if self.fail:
            raise self.fail
        if key == ("Proxy", "Proxies"):
            return self.proxies
        if key == ("System", "Account"):
            return {"data": {"free": self.free}}
        raise AssertionError(key)


def entry(delegate=SIGNER, proxy_type="NonFungible", delay=0):
    return {"delegate": delegate, "proxy_type": proxy_type, "delay": delay}


class DelegationAndBalance(unittest.TestCase):
    def test_the_required_delegation_is_fine(self):
        client = FakeClient([[entry()], 0])
        self.assertEqual(pr.delegation_problems(client, settings()), [])
        self.assertEqual(client.reads, [(("Proxy", "Proxies"), [VALIDATOR])], "read for the validator")

    def test_other_delegates_do_not_matter_and_shapes_are_normalised(self):
        shapes = [
            [[entry(OTHER, "Any"), entry({"Id": SIGNER}, {"NonFungible": None})], 0],
            [entry(OTHER), entry(SIGNER)],
            SimpleNamespace(value=[[entry()], 7]),
        ]
        for shape in shapes:
            with self.subTest(shape=type(shape).__name__):
                self.assertEqual(pr.delegation_problems(FakeClient(shape), settings()), [])

    def test_no_delegation_is_a_problem(self):
        for empty in ([[], 0], None, [[entry(OTHER)], 0]):
            with self.subTest(empty=empty):
                problems = pr.delegation_problems(FakeClient(empty), settings())
                self.assertEqual(len(problems), 1)
                self.assertIn("NO proxy delegation", problems[0])

    def test_wrong_type_or_a_delay_is_a_problem(self):
        for bad in (entry(proxy_type="Any"), entry(proxy_type="Staking"), entry(delay=5)):
            with self.subTest(bad=bad):
                problems = pr.delegation_problems(FakeClient([[bad], 0]), settings())
                self.assertEqual(len(problems), 1)
                self.assertIn("not NonFungible with delay 0", problems[0])

    def test_an_unreadable_or_unrecognised_answer_fails_closed(self):
        self.assertIn("could not read", pr.delegation_problems(FakeClient(fail=OSError("down")), settings())[0])
        self.assertIn("could not read", pr.delegation_problems(FakeClient("garbage"), settings())[0])
        self.assertIn("could not read", pr.delegation_problems(FakeClient([[{"x": 1}], 0]), settings())[0])

    def test_the_signer_balance_floor(self):
        self.assertIsNone(pr.balance_problem(FakeClient(free=pr.MIN_SIGNER_BALANCE_RAO), settings()))
        self.assertIn("below the floor", pr.balance_problem(FakeClient(free=1), settings()))
        self.assertIn("could not read", pr.balance_problem(FakeClient(fail=OSError("x")), settings()))

    def test_gate_lines_use_the_gates_yes_or_NO_shape(self):
        reader = SimpleNamespace(client=FakeClient([[entry()], 0]))
        self.assertEqual(
            pr.gate_lines(reader, settings()), [("proxy delegation", "yes"), ("proxy signer funded", "yes")]
        )
        bad = SimpleNamespace(client=FakeClient([[], 0], free=0))
        lines = dict(pr.gate_lines(bad, settings()))
        self.assertTrue(lines["proxy delegation"].startswith("NO."))
        self.assertTrue(lines["proxy signer funded"].startswith("NO."))


def event(module, name, **attributes):
    return {"event": {"module_id": module, "event_id": name, "attributes": attributes}}


class InnerResult(unittest.TestCase):
    def test_only_a_proxy_executed_ok_is_ok(self):
        events = [event("System", "ExtrinsicSuccess"), event("Proxy", "ProxyExecuted", result={"Ok": ()})]
        self.assertEqual(pr.classify_inner(events), (pr.INNER_OK, None))

    def test_outer_success_with_an_inner_error_is_an_error(self):
        err = {"Module": {"index": 7, "error": "0x0c000000"}}
        events = [event("System", "ExtrinsicSuccess"), event("Proxy", "ProxyExecuted", result={"Err": err})]
        self.assertEqual(pr.classify_inner(events), (pr.INNER_ERR, err))

    def test_an_extrinsic_failed_event_is_an_error(self):
        status, _ = pr.classify_inner([event("System", "ExtrinsicFailed", dispatch_error="BadOrigin")])
        self.assertEqual(status, pr.INNER_ERR)

    def test_no_events_or_no_proxy_executed_is_unknown_never_ok(self):
        for events in (None, [], [event("System", "ExtrinsicSuccess")], [event("Proxy", "ProxyExecuted", result=3)]):
            with self.subTest(events=events):
                self.assertEqual(pr.classify_inner(events)[0], pr.INNER_UNKNOWN)

    def test_wrapped_entries_are_read(self):
        wrapped = [SimpleNamespace(value=event("Proxy", "ProxyExecuted", result={"Ok": ()}))]
        self.assertEqual(pr.classify_inner(wrapped)[0], pr.INNER_OK)


class Allowlist(unittest.TestCase):
    def test_a_wrapper_for_the_validator_around_the_sealed_commit_is_permitted(self):
        permitted_proxy_wrapper(VALIDATOR, VALIDATOR, "SubtensorModule", "commit_timelocked_mechanism_weights")

    def test_a_wrapper_for_another_account_is_refused(self):
        with self.assertRaises(CallNotPermitted):
            permitted_proxy_wrapper(STRANGER, VALIDATOR, "SubtensorModule", "commit_timelocked_mechanism_weights")

    def test_a_wrapper_around_any_other_call_is_refused(self):
        for module, call in (("Balances", "transfer_keep_alive"), ("SubtensorModule", "add_stake"), ("Proxy", "add_proxy")):
            with self.subTest(call=f"{module}.{call}"):
                with self.assertRaises(CallNotPermitted):
                    permitted_proxy_wrapper(VALIDATOR, VALIDATOR, module, call)

    def test_proxy_management_calls_stay_refused_by_name(self):
        for call in ("add_proxy", "remove_proxy", "remove_proxies", "create_pure", "kill_pure", "proxy_announced", "announce"):
            with self.subTest(call=call):
                with self.assertRaises(CallNotPermitted):
                    permitted("Proxy", call)
        with self.assertRaises(CallNotPermitted):
            permitted("Proxy", "proxy")  # the wrapper is not a free entry of the allowlist


class RecordingSubmit(ComposesCalls):
    def __init__(self, result):
        self.result = result
        self.calls = []

    def submit_call(self, call, wallet):
        if call.module == "Proxy":
            inner_call(call)
        self.calls.append((call, wallet))
        return self.result


def publisher_with(result, identity=True):
    from ..submitter import Publisher

    signer_key = SimpleNamespace(ss58_address=SIGNER, public_key=public_key_of(SIGNER))
    submit = RecordingSubmit(result)
    publisher = Publisher(
        client=mock.Mock(),
        wallet=signer_key,
        netuid=82,
        submit_client=submit,
        commit_identity=pr.CommitIdentity(VALIDATOR) if identity else None,
    )
    return publisher, submit, signer_key


def result(events, error=None, extrinsic_id="108100-3"):
    return SimpleNamespace(error=error, message="", extrinsic_id=extrinsic_id, events=events)


class WrapperComposition(unittest.TestCase):
    def test_the_commit_is_wrapped_for_the_validator_and_signed_by_the_signer(self):
        ok = result([event("Proxy", "ProxyExecuted", result={"Ok": ()})])
        publisher, submit, signer_key = publisher_with(ok)
        outcome = publisher.submit(b"commit", 4242, 82)
        self.assertIsNone(outcome["error"])
        self.assertEqual((outcome["block"], outcome["extrinsic_index"]), (108100, 3))
        (call, wallet), = submit.calls
        self.assertIs(wallet, signer_key, "the proxy signer signs")
        self.assertEqual((call.module, call.function), ("Proxy", "proxy"))
        self.assertEqual(call.params["real"], VALIDATOR)
        self.assertIsNone(call.params["force_proxy_type"])
        inner = inner_call(call)
        self.assertEqual((inner.module, inner.function), ("SubtensorModule", "commit_timelocked_mechanism_weights"))
        self.assertEqual(inner.params["commit"], b"commit")
        self.assertEqual(inner.params["reveal_round"], 4242)
        self.assertEqual(inner.params["netuid"], 82)

    def test_a_composed_wrapper_for_another_account_is_refused_and_nothing_is_sent(self):
        """The wrapper check compares the COMPOSED call's `real` with the independently configured
        commit identity. A self-comparison (`permitted_proxy_wrapper(real, real, ...)`) would pass
        this and send; this test fails on that."""
        from bittensor._generated import calls

        original = calls.Proxy.proxy

        def wrong_real(real, force_proxy_type, call):
            return original(real=STRANGER, force_proxy_type=force_proxy_type, call=call)

        ok = result([event("Proxy", "ProxyExecuted", result={"Ok": ()})])
        publisher, submit, _ = publisher_with(ok)
        with mock.patch.object(calls.Proxy, "proxy", side_effect=wrong_real):
            with self.assertRaises(CallNotPermitted):
                publisher.submit(b"commit", 4242, 82)
        self.assertEqual(submit.calls, [], "nothing was sent")

    def test_without_an_identity_the_call_is_not_wrapped(self):
        publisher, submit, _ = publisher_with(result(None), identity=False)
        outcome = publisher.submit(b"commit", 1, 82)
        self.assertIsNone(outcome["error"])
        call = submit.calls[0][0]
        self.assertEqual((call.module, call.function), ("SubtensorModule", "commit_timelocked_mechanism_weights"))

    def test_an_inner_failure_is_a_definite_refusal_that_cost_a_fee(self):
        err = {"Module": {"index": 7, "error": "0x0c000000"}}
        failed = result([event("Proxy", "ProxyExecuted", result={"Err": err})])
        publisher, _, _ = publisher_with(failed)
        outcome = publisher.submit(b"commit", 1, 82)
        self.assertTrue(outcome["inner_failed"] and outcome["fee_spent"])
        self.assertIn("failed inside Proxy.proxy", outcome["error"])
        self.assertFalse(outcome.get("indeterminate"))
        self.assertEqual(outcome["block"], 108100)

    def test_events_that_cannot_be_read_are_indeterminate_not_success(self):
        for events in (None, [], [event("System", "ExtrinsicSuccess")]):
            with self.subTest(events=events):
                publisher, _, _ = publisher_with(result(events))
                outcome = publisher.submit(b"commit", 1, 82)
                self.assertTrue(outcome["indeterminate"])
                self.assertIn("unknown", outcome["error"])

    def test_an_outer_error_is_still_reported_as_before(self):
        publisher, _, _ = publisher_with(result(None, error="InvalidTransaction"))
        outcome = publisher.submit(b"commit", 1, 82)
        self.assertEqual(outcome["error"], "InvalidTransaction")
        self.assertFalse(outcome.get("indeterminate"))

    def test_a_raised_submit_is_indeterminate_as_before(self):
        publisher, submit, _ = publisher_with(None)
        submit.submit_call = mock.Mock(side_effect=TimeoutError("no answer"))
        self.assertTrue(publisher.submit(b"commit", 1, 82)["indeterminate"])


class SealedPayloadIdentity(unittest.TestCase):
    """The sealed bytes carry a public key. It must be the validator's, not the signer's.
    (The bytes themselves are encrypted with fresh randomness, so the check is on the
    arguments the sealing call receives.)"""

    def seal_args(self, hotkey):
        import bittensor_core as core

        publisher, _, _ = publisher_with(result(None))
        publisher.client.block = 108000
        publisher.client.block_time.return_value = 12.0
        publisher.client.query.return_value = 100
        with mock.patch.object(core, "get_encrypted_commit_v2", return_value=(b"sealed", 99)) as sealer:
            commit, reveal = publisher.seal([0, 1], [5, 6], hotkey)
        self.assertEqual((commit, reveal), (b"sealed", 99))
        return sealer.call_args.kwargs

    def test_the_adapter_hands_the_sealer_the_validators_key_not_the_signers(self):
        from ._adapter_helpers import an_adapter

        identity = pr.CommitIdentity(VALIDATOR)
        adapter, _, _ = an_adapter(wallet_hotkey=identity)
        captured = {}

        class SealOnly:
            def seal(self, uids, values, hotkey):
                captured["hotkey"] = hotkey
                return b"c", 1

        adapter.publisher = SealOnly()
        adapter.seal([0], [1], adapter.wallet_hotkey)
        self.assertEqual(bytes(captured["hotkey"].public_key), public_key_of(VALIDATOR))
        self.assertNotEqual(bytes(captured["hotkey"].public_key), public_key_of(SIGNER))
        kwargs = self.seal_args(captured["hotkey"])
        self.assertEqual(kwargs["hotkey"], public_key_of(VALIDATOR))

    def test_using_the_signers_key_would_seal_for_the_wrong_account(self):
        kwargs = self.seal_args(SimpleNamespace(public_key=public_key_of(SIGNER)))
        self.assertEqual(kwargs["hotkey"], public_key_of(SIGNER))
        self.assertNotEqual(kwargs["hotkey"], public_key_of(VALIDATOR), "the guard above is meaningful")


class ProxyAdapterPublish(unittest.TestCase):
    """Real ChainAdapter.publish and record_commit with a proxy-style publisher."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def build(self, publisher, **paths):
        from ._adapter_helpers import an_adapter

        return an_adapter(
            publisher=publisher,
            wallet_hotkey=pr.CommitIdentity(VALIDATOR),
            day_window=lambda height: (10, 0, height),
            **paths,
        )[0]

    def fake_publisher(self, outcomes):
        from ._adapter_helpers import FakePublisher

        class P(FakePublisher):
            signer_address = SIGNER
            commit_identity = pr.CommitIdentity(VALIDATOR)

            def __init__(self):
                super().__init__()
                self.submissions = 0

            def send_context(self, address):
                self.context_for = address
                return {"account_nonce_at_head": 41, "head_at_send": 99, "era_period": 128}

            def submit(self, commit, reveal_round, netuid):
                outcome = outcomes[min(self.submissions, len(outcomes) - 1)]
                self.submissions += 1
                if outcome["error"] is None:
                    self.stored.append(commit)
                return outcome

        return P()

    def test_records_name_the_signer_and_the_validator_separately(self):
        ok = {"error": None, "block": 10, "extrinsic_index": 2}
        publisher = self.fake_publisher([ok])
        intent_path, submitted_dir = self.dir / "publish-intent.json", self.dir / "scores"
        adapter = self.build(publisher, publish_intent_path=intent_path, submitted_record_dir=submitted_dir)
        # open marker annotated with both identities while the send is in flight
        with mock.patch.object(publisher, "submit", side_effect=lambda *a, **k: {"error": "t", "block": None, "indeterminate": True}):
            with self.assertRaises(NotOnChain):
                adapter.publish({0: 1, 1: 2}, 5000)
        marker = json.loads(intent_path.read_text())
        self.assertEqual(marker["hotkey"], VALIDATOR)
        self.assertEqual(marker["commit_identity"], VALIDATOR)
        self.assertEqual(marker["signer"], SIGNER)
        self.assertEqual(marker["route"], "proxy")
        self.assertEqual(marker["account_nonce_at_head"], 41, "the nonce is read for the signer")
        self.assertEqual(publisher.context_for, SIGNER)
        # a successful send: records carry both, marker cleared
        intent_path.unlink()
        published = adapter.publish({0: 1, 1: 2}, 5000)
        self.assertTrue(published["confirmed_on_chain"])
        adapter.record_submitted(5000, {0: 1, 1: 2}, published)
        record = json.loads(next(submitted_dir.glob("day-10.submitted.json")).read_text())
        self.assertEqual((record["hotkey"], record["commit_identity"], record["signer"], record["route"]), (VALIDATOR, VALIDATOR, SIGNER, "proxy"))

    def test_an_outer_ok_inner_failed_send_clears_the_marker_and_is_counted(self):
        failed = {"error": "the proxied commit failed inside Proxy.proxy: {...}", "block": 10, "inner_failed": True, "fee_spent": True}
        publisher = self.fake_publisher([failed])
        intent_path, backoff_path = self.dir / "publish-intent.json", self.dir / "publish-backoff.json"
        adapter = self.build(publisher, publish_intent_path=intent_path, publish_backoff_path=backoff_path)
        with self.assertRaises(CommitRejected):
            adapter.publish({0: 1}, 5000)
        self.assertFalse(intent_path.exists(), "a definite refusal: nothing was accepted")
        self.assertEqual(json.loads(backoff_path.read_text())["failures"], 1)

    def test_indeterminate_inner_leaves_the_marker_open_and_does_not_count(self):
        unknown = {"error": "events unreadable", "block": 10, "indeterminate": True}
        publisher = self.fake_publisher([unknown])
        intent_path, backoff_path = self.dir / "publish-intent.json", self.dir / "publish-backoff.json"
        adapter = self.build(publisher, publish_intent_path=intent_path, publish_backoff_path=backoff_path)
        with self.assertRaises(NotOnChain):
            adapter.publish({0: 1}, 5000)
        self.assertTrue(intent_path.exists())
        self.assertFalse(backoff_path.exists())

    def test_repeated_failures_stop_further_sends_until_a_person_resets(self):
        failed = {"error": "inner failed", "block": 10, "inner_failed": True, "fee_spent": True}
        publisher = self.fake_publisher([failed])
        backoff_path = self.dir / "publish-backoff.json"
        adapter = self.build(publisher, publish_backoff_path=backoff_path)
        clock = {"now": datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)}
        adapter._failure_backoff._clock = lambda: clock["now"]

        sent = []
        for attempt in range(1, 8):
            before = publisher.submissions
            try:
                adapter.publish({0: 1}, 5000 + attempt)
            except CommitRejected:
                pass
            except pr.PublicationFailureLimit as stopped:
                self.assertIn(pr.BACKOFF_REASON_PREFIX, str(stopped))
            sent.append(publisher.submissions - before)
            clock["now"] += timedelta(hours=3)  # well past any spacing
        self.assertEqual(sum(sent), pr.MAX_CONSECUTIVE_FAILURES, f"fees spent at most {pr.MAX_CONSECUTIVE_FAILURES} times: {sent}")

        adapter._failure_backoff.reset("signer delegation re-checked and funded")
        self.assertFalse(backoff_path.exists())
        self.assertTrue(list(self.dir.glob("publish-backoff.reset-*.json")), "archived, not deleted")
        with self.assertRaises(CommitRejected):
            adapter.publish({0: 1}, 9000)  # a person reset it: one more try is allowed

    def test_spacing_between_failures_doubles_and_blocks_an_early_retry(self):
        failed = {"error": "inner failed", "block": 10, "inner_failed": True}
        publisher = self.fake_publisher([failed])
        adapter = self.build(publisher, publish_backoff_path=self.dir / "b.json")
        now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        adapter._failure_backoff._clock = lambda: now
        with self.assertRaises(CommitRejected):
            adapter.publish({0: 1}, 5000)
        with self.assertRaises(pr.PublicationFailureLimit):
            adapter.publish({0: 1}, 5001)  # same instant: inside the 30 minute spacing
        self.assertEqual(publisher.submissions, 1)

    def test_a_confirmed_send_resets_the_count(self):
        ok = {"error": None, "block": 10, "extrinsic_index": 0}
        publisher = self.fake_publisher([ok])
        backoff = pr.PublishFailureBackoff(self.dir / "b.json")
        backoff.record_failure("earlier failure")
        adapter = self.build(publisher, publish_backoff_path=self.dir / "b.json")
        adapter._failure_backoff._clock = lambda: datetime.now(timezone.utc) + timedelta(hours=5)
        published = adapter.publish({0: 1}, 5000)
        adapter.record_commit(5000, published)
        self.assertFalse((self.dir / "b.json").exists())

    def test_a_corrupt_failure_record_blocks_sending(self):
        (self.dir / "b.json").write_text("not json")
        adapter = self.build(self.fake_publisher([{"error": None, "block": 1}]), publish_backoff_path=self.dir / "b.json")
        with self.assertRaises(pr.PublicationFailureLimit):
            adapter.publish({0: 1}, 5000)

    def test_a_hold_still_takes_precedence(self):
        from ..publish_hold import PublicationOnHold, PublishHold

        hold = self.dir / "h.json"
        PublishHold(hold).set("restored")
        adapter = self.build(self.fake_publisher([{"error": None, "block": 1}]), publish_hold_path=hold)
        with self.assertRaises(PublicationOnHold):
            adapter.publish({0: 1}, 5000)


class ScanKeyIsTheValidator(unittest.TestCase):
    def test_the_adapter_identifies_its_own_commits_by_the_validator_address(self):
        from ._adapter_helpers import an_adapter

        adapter, _, _ = an_adapter(wallet_hotkey=pr.CommitIdentity(VALIDATOR))
        self.assertEqual(adapter.wallet_hotkey.ss58_address, VALIDATOR)
        self.assertNotEqual(adapter.wallet_hotkey.ss58_address, SIGNER)


class SettingsParse(unittest.TestCase):
    def test_the_new_setting_is_known_and_parsed(self):
        mapping = {"NOAH_NETWORK": "local", "NOAH_PROXY_SIGNER_ADDRESS": SIGNER, "NOAH_EXPECTED_SIGNER_ADDRESS": VALIDATOR}
        from ._mainnet_helpers import good_mapping

        s = Settings.from_mapping({**good_mapping(), **mapping, "NOAH_NETWORK": "test"})
        self.assertEqual(s.proxy_signer_address, SIGNER)
        self.assertIsNone(Settings.from_mapping({**good_mapping(), "NOAH_NETWORK": "test", "NOAH_PROXY_SIGNER_ADDRESS": ""}).proxy_signer_address)


if __name__ == "__main__":
    unittest.main()




class FailureLimitThroughTheRealLoop(unittest.TestCase):
    """Real loop.tick and run_period: a tripped failure limit sends nothing and says why."""

    def test_a_tripped_limit_stops_sending_and_the_loop_says_so(self):
        from .. import loop
        from .test_issue334_commit_ledger import LedgerChain, T, WithState, process

        class Case(WithState):
            def runTest(inner):  # noqa: N805 - a throwaway TestCase to reuse WithState's setup
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.tearDown)

        class BackoffChain(LedgerChain):
            def publish(self, vector, height):
                landed = []

                def land(chain, **kwargs):
                    landed.append(1)
                    return LedgerChain.publish(self, vector, height)

                with mock.patch("noah.core.chain_adapter.publish_weights", side_effect=land):
                    return self.adapter.publish(vector, height)

        backoff_path = case.dir / "publish-backoff.json"
        chain = BackoffChain(T(11) + 2000, case.ledger)
        chain.adapter._failure_backoff = pr.PublishFailureBackoff(backoff_path)
        for _ in range(pr.MAX_CONSECUTIVE_FAILURES):
            chain.adapter._failure_backoff.record_failure("inner failed")
        pending = {"day": 10, "height": 78479}
        out = process(chain, case.dir, pending, 78479, [T(11) + 2000])
        self.assertEqual([o for _, o in out], ["waiting"])
        state = loop.read_state(case.dir / "loop.json")
        self.assertIn(pr.BACKOFF_REASON_PREFIX, state.get("reason", ""))
        process(chain, case.dir, dict(pending), 78479, [T(11) + 2100, T(11) + 2200])
        self.assertEqual(chain.committed_days, [], chain.log)

        # a person resets it: the very next tick sends
        chain.adapter._failure_backoff.reset("delegation re-checked")
        process(chain, case.dir, dict(pending), 78479, [T(11) + 2300])
        self.assertEqual(chain.committed_days, [10])


class BuildWiring(unittest.TestCase):
    """build_chain_adapter (real, with its connections faked as the existing tests do): the proxy
    route changes the identity and turns the failure limit on; without it nothing changes."""

    def adapter(self, backoff=True, **settings_extra):
        from .. import chain_adapter as ca
        from ._adapter_helpers import (
            ARK, DIALS, FLOORED_ANCHOR, NETUID, VALIDATOR_HOTKEY, FakeReader,
        )

        class FakeSubstrateInterface:
            def __init__(self, url, *, auto_discover=True):
                pass

            def close(self):
                pass

        class Settings:
            chain_endpoint = "ws://node.example"
            second_chain_endpoint = "ws://node2.example"
            netuid = NETUID
            settle_blocks = 1
            expected_signer_address = VALIDATOR
            proxy_signer_address = SIGNER

        for key, value in settings_extra.items():
            setattr(Settings, key, value)
        wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=SIGNER, public_key=public_key_of(SIGNER)))
        with mock.patch("async_substrate_interface.SubstrateInterface", FakeSubstrateInterface):
            return ca.build_chain_adapter(
                client=object(), settings=Settings(), reader=FakeReader(), wallet=wallet, uid=0,
                day_anchor_ts=FLOORED_ANCHOR, ark_coldkey=ARK, designated_validator_hotkey=VALIDATOR_HOTKEY,
                dials=DIALS, store_factory=lambda: None, holding_stride=1, entry_deposit_rao=100_000_000_000,
                publish_backoff_path=Path(tempfile.mkdtemp()) / "publish-backoff.json" if backoff else None,
            )

    def test_proxy_mode_uses_the_validators_identity_and_the_signer_for_signing(self):
        adapter = self.adapter()
        self.assertEqual(adapter.wallet_hotkey.ss58_address, VALIDATOR)
        self.assertEqual(adapter.wallet_hotkey.public_key, public_key_of(VALIDATOR))
        self.assertEqual(adapter.publisher.signer_address, SIGNER)
        self.assertEqual(adapter.publisher.commit_identity.ss58_address, VALIDATOR)
        self.assertIsNotNone(adapter._failure_backoff, "the failure limit is on in proxy mode")

    def test_proxy_mode_without_a_backoff_path_is_refused_not_silently_unlimited(self):
        with self.assertRaises(pr.ProxyRouteRefused) as raised:
            self.adapter(backoff=False)
        self.assertIn("failed-send limit", str(raised.exception))

    def test_without_the_proxy_route_a_missing_backoff_path_is_fine(self):
        self.assertIsNone(self.adapter(backoff=False, proxy_signer_address=None)._failure_backoff)

    def test_without_the_proxy_route_the_wallet_hotkey_is_the_identity_and_there_is_no_limit(self):
        adapter = self.adapter(proxy_signer_address=None)
        self.assertEqual(adapter.wallet_hotkey.ss58_address, SIGNER, "the loaded hotkey, as before")
        self.assertIsNone(adapter.publisher.commit_identity)
        self.assertIsNone(adapter._failure_backoff, "private-test behaviour unchanged")
