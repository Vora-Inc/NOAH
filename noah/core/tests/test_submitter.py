"""Sealing and submitting, which is the only part of this component that signs.

Three properties carry the weight here. The schedule is read at one block. A
value that could not be read refuses rather than becoming zero. And the thing
that signs is the hotkey, because signing with the wallet produces a refusal
that sends you to look at the wrong thing entirely.
"""

import unittest

from ..submitter import SCHEDULE, Publisher, ScheduleUnreadable, _block_from_extrinsic_id
from ..wallet import CallNotPermitted, PERMITTED_CALLS


class Keypair:
    def __init__(self, ss58="5HOT"):
        self.ss58_address = ss58
        self.public_key = b"\x01" * 32


class Wallet:
    def __init__(self):
        self.hotkey = Keypair()
        self.coldkeypub = Keypair("5COLD")


class Client:
    def __init__(self, answers=None, blocks_asked=None):
        self.answers = answers if answers is not None else dict.fromkeys(SCHEDULE, 7)
        self.blocks_asked = blocks_asked if blocks_asked is not None else []
        self.block = 12_000

    def query(self, item, params=None, block=None):
        self.blocks_asked.append(block)
        return self.answers.get(item[1])

    def block_time(self):
        return 12.0

    def query_map(self, item, params=None):
        return []


class TheScheduleIsReadAtOneBlock(unittest.TestCase):
    """Six values straddling an epoch boundary give a sound, wrong reveal round.

    The failure appears an epoch later as a commit nobody opens, which is a long
    way from where it was caused.
    """

    def test_every_value_is_read_at_the_same_block(self):
        client = Client()
        Publisher(client, Keypair(), netuid=2)._schedule(11_900)
        self.assertEqual(set(client.blocks_asked), {11_900})
        self.assertEqual(len(client.blocks_asked), len(SCHEDULE))

    def test_all_six_values_are_read(self):
        got = Publisher(Client(), Keypair(), netuid=2)._schedule(11_900)
        self.assertEqual(set(got), set(SCHEDULE.values()))


class AnUnreadValueIsNotZero(unittest.TestCase):
    """Zero is a legitimate answer for several of these, which is the trap.

    The spike this grew from used `int(value or 0)`, which spells "nobody could
    read it" and "it is zero" the same way for all six.
    """

    def test_an_unread_schedule_value_refuses(self):
        answers = dict.fromkeys(SCHEDULE, 7)
        answers["PendingEpochAt"] = None
        with self.assertRaises(ScheduleUnreadable) as caught:
            Publisher(Client(answers), Keypair(), netuid=2)._schedule(11_900)
        self.assertIn("PendingEpochAt", str(caught.exception))

    def test_a_real_zero_is_kept(self):
        # Measured on the live chain: PendingEpochAt reads 0 legitimately.
        answers = dict.fromkeys(SCHEDULE, 7)
        answers["PendingEpochAt"] = 0
        got = Publisher(Client(answers), Keypair(), netuid=2)._schedule(11_900)
        self.assertEqual(got["pending_epoch_at"], 0)

    def test_the_refusal_names_which_value(self):
        answers = dict.fromkeys(SCHEDULE, 7)
        answers["Tempo"] = None
        with self.assertRaises(ScheduleUnreadable) as caught:
            Publisher(Client(answers), Keypair(), netuid=2)._schedule(11_900)
        self.assertIn("Tempo", str(caught.exception))


class TheHotkeySignsAndNotTheWallet(unittest.TestCase):
    """Signing with the wallet is refused by the chain in a misleading way.

    It says the hotkey is not registered in the subnet, for a hotkey that is
    plainly registered, because the signer was never the hotkey. Measured on
    2026-08-24 against a seat whose Uids entry read 15 at the time.
    """

    def test_a_wallet_is_reduced_to_its_hotkey(self):
        wallet = Wallet()
        self.assertIs(Publisher(Client(), wallet, netuid=2).wallet, wallet.hotkey)

    def test_a_keypair_is_used_as_given(self):
        key = Keypair()
        self.assertIs(Publisher(Client(), key, netuid=2).wallet, key)

    def test_the_coldkey_is_never_what_signs(self):
        wallet = Wallet()
        self.assertNotEqual(
            Publisher(Client(), wallet, netuid=2).wallet.ss58_address,
            wallet.coldkeypub.ss58_address,
        )


class OnlyOneCallMayBeSubmitted(unittest.TestCase):
    def test_the_permitted_call_is_the_one_this_composes(self):
        from ..submitter import CALL, MODULE
        from ..wallet import PERMITTED_CALLS

        self.assertIn((MODULE, CALL), PERMITTED_CALLS)

    def test_submit_actually_asks_before_it_signs(self):
        """That the check EXISTS is not the property; that submit reaches it is.

        A sweep removing the call from submit left every other test passing,
        because submit only ever composes the one permitted call, so nothing
        observable changed. This drives the refusal through submit itself.
        """
        from unittest import mock

        from .. import wallet as wallet_module

        client = Client()
        publisher = Publisher(client, Keypair(), netuid=2)
        with mock.patch.object(wallet_module, "PERMITTED_CALLS", frozenset()):
            with self.assertRaises(CallNotPermitted):
                publisher.submit(b"\x00" * 8, 1, 2)

    def test_it_asks_before_composing_anything(self):
        """A refusal must cost nothing and leave no half-formed extrinsic.

        If the permission check ran after composing, a refused call would still
        have built one, and the ordering is the whole reason the check is where
        it is.
        """
        from unittest import mock

        from .. import wallet as wallet_module

        composed = []

        class Watcher(Client):
            def compose(self, call):
                composed.append(call)
                return call

        publisher = Publisher(Watcher(), Keypair(), netuid=2)
        with mock.patch.object(wallet_module, "PERMITTED_CALLS", frozenset()):
            with self.assertRaises(CallNotPermitted):
                publisher.submit(b"\x00" * 8, 1, 2)
        self.assertEqual(composed, [])

    def test_nothing_else_is_permitted(self):
        from ..wallet import permitted

        for module, call in (
            ("Balances", "transfer_keep_alive"),
            ("SubtensorModule", "set_weights"),
            ("SubtensorModule", "add_stake"),
        ):
            with self.subTest(call=f"{module}.{call}"):
                with self.assertRaises(CallNotPermitted):
                    permitted(module, call)


class ASubmissionThatRaisesIsReportedAsIndeterminate(unittest.TestCase):
    """Issue #291: a raising submit_call may still have reached the node, so it is reported
    as indeterminate (publish_weights then raises NotOnChain and the marker stays open), not
    as a definite refusal."""

    def test_submit_call_raising_returns_indeterminate_true(self):
        class Raising(Client):
            def submit_call(self, call, wallet, **kwargs):
                raise TimeoutError("no answer from the node")

        got = Publisher(Raising(), Keypair(), netuid=2).submit(b"\x00" * 8, 1, 2)
        self.assertIs(got["indeterminate"], True)
        self.assertEqual(got["error"], "TimeoutError: no answer from the node")
        self.assertIsNone(got["block"])

    def test_a_refusal_the_chain_reported_is_not_indeterminate(self):
        class Refusing(Client):
            def submit_call(self, call, wallet, **kwargs):
                return FakeExtrinsicResult(error="Rejected", message="rate limited")

        got = Publisher(Refusing(), Keypair(), netuid=2).submit(b"\x00" * 8, 1, 2)
        self.assertNotIn("indeterminate", got)
        self.assertIn("Rejected", got["error"])


class SubmitDirectComposesTheOtherPermittedCall(unittest.TestCase):
    """Ticket #16 (B06), issue #56 comment 5720306237: the direct, open call,
    composed only by ``submit_direct``, never by ``submit`` and never as
    anything ``submit`` falls back to on its own.
    """

    def test_the_direct_call_is_the_second_permitted_entry(self):
        from ..submitter import DIRECT_CALL, DIRECT_MODULE

        self.assertIn((DIRECT_MODULE, DIRECT_CALL), PERMITTED_CALLS)
        # And it is genuinely a second entry, not the sealed one under a
        # different name -- the whole point of holding a pair rather than
        # a single call.
        from ..submitter import CALL, MODULE

        self.assertNotEqual((DIRECT_MODULE, DIRECT_CALL), (MODULE, CALL))

    def test_submit_direct_composes_set_mechanism_weights_with_the_given_values(self):
        composed = []

        class Watcher(Client):
            def submit_call(self, call, wallet, **kwargs):
                composed.append(call)
                return FakeExtrinsicResult(extrinsic_id="9044-0002")

        publisher = Publisher(Watcher(), Keypair(), netuid=2, mecid=0)
        result = publisher.submit_direct([0, 1, 2], [10, 20, 30], 2)

        self.assertEqual(len(composed), 1)
        self.assertEqual(composed[0].module, "SubtensorModule")
        self.assertEqual(composed[0].function, "set_mechanism_weights")
        self.assertEqual(composed[0].params["netuid"], 2)
        self.assertEqual(composed[0].params["mecid"], 0)
        self.assertEqual(composed[0].params["dests"], [0, 1, 2])
        self.assertEqual(composed[0].params["weights"], [10, 20, 30])
        self.assertEqual(composed[0].params["version_key"], 208370384225125)
        self.assertEqual(result, {"error": None, "block": 9044,
                                  "extrinsic_index": 2, "block_hash": None})

    def test_submit_direct_asks_before_it_signs(self):
        from unittest import mock

        from .. import wallet as wallet_module

        publisher = Publisher(Client(), Keypair(), netuid=2)
        with mock.patch.object(wallet_module, "PERMITTED_CALLS", frozenset()):
            with self.assertRaises(CallNotPermitted):
                publisher.submit_direct([0], [1], 2)

    def test_submit_direct_asks_before_composing_anything(self):
        from unittest import mock

        from .. import wallet as wallet_module

        composed = []

        class Watcher(Client):
            def submit_call(self, call, wallet, **kwargs):
                composed.append(call)
                return FakeExtrinsicResult(extrinsic_id="1-0")

        publisher = Publisher(Watcher(), Keypair(), netuid=2)
        with mock.patch.object(wallet_module, "PERMITTED_CALLS", frozenset()):
            with self.assertRaises(CallNotPermitted):
                publisher.submit_direct([0], [1], 2)
        self.assertEqual(composed, [])

    def test_a_failed_direct_submission_is_reported_not_raised(self):
        class FailingClient(Client):
            def submit_call(self, call, wallet, **kwargs):
                return FakeExtrinsicResult(error="SomeChainError", message="details")

        got = Publisher(FailingClient(), Keypair(), netuid=2).submit_direct([0], [1], 2)
        self.assertEqual(got["error"], "SomeChainError details")
        self.assertTrue(got["indeterminate"])

    def test_submit_never_calls_submit_direct_as_a_fallback(self):
        """The property that matters is structural, not a promise in a
        comment: submit()'s own source names no fallback to submit_direct,
        under any name, so a sealed-path failure cannot silently reach the
        open call no matter what exception submit() catches internally.
        """
        import inspect

        source = inspect.getsource(Publisher.submit)
        self.assertNotIn("submit_direct", source)

    def test_submit_direct_never_calls_submit_as_anything_either(self):
        import inspect

        source = inspect.getsource(Publisher.submit_direct)
        # "submit(" (not just "submit_direct" appearing in its own name)
        # would mean submit_direct composes the sealed call as some kind of
        # fallback-in-the-other-direction; it must not.
        self.assertNotIn("self.submit(", source)


class FakeExtrinsicResult:
    """The real shape bittensor's own ``ExtrinsicResult`` carries -- no
    ``block`` attribute at all, only ``block_hash`` and ``extrinsic_id``
    (``"block_number-index"``) -- confirmed directly against the installed
    11.1.0 (``bittensor/result.py``) before writing this fixture."""

    def __init__(self, *, error=None, message="", extrinsic_id=None, block_hash=None):
        self.error = error
        self.message = message
        self.extrinsic_id = extrinsic_id
        self.block_hash = block_hash


class TheBlockNumberIsParsedFromExtrinsicId(unittest.TestCase):
    """Ticket #16 (B06): ``submit()`` used to read a ``.block`` attribute
    that real ``ExtrinsicResult``s never carry, so every successful
    submission reported ``block: None`` -- not only the failed ones. Fixed
    by parsing the real inclusion block out of ``extrinsic_id`` instead.
    """

    def test_a_successful_submission_reports_its_real_block_number(self):
        class SubmittingClient(Client):
            def submit_call(self, call, wallet, **kwargs):
                return FakeExtrinsicResult(extrinsic_id="9044-0002", block_hash="0xabc")

        got = Publisher(SubmittingClient(), Keypair(), netuid=2).submit(b"\x00" * 8, 1, 2)
        self.assertEqual(got, {"error": None, "block": 9044, "extrinsic_index": 2})

    def test_measured_real_extrinsic_ids_from_stage_3_all_decode(self):
        # The exact three extrinsic_id values observed on the real private
        # chain during this ticket's own Stage 3 run (2026-09-16): a
        # transfer, a burned_register, and a weight commit.
        for extrinsic_id, expected_block in (
            ("8934-0002", 8934),
            ("8937-0002", 8937),
            ("9044-0002", 9044),
        ):
            with self.subTest(extrinsic_id=extrinsic_id):
                self.assertEqual(_block_from_extrinsic_id(extrinsic_id), expected_block)

    def test_a_failed_submission_still_reports_no_block(self):
        class FailingClient(Client):
            def submit_call(self, call, wallet, **kwargs):
                return FakeExtrinsicResult(error="SomeChainError", message="details")

        got = Publisher(FailingClient(), Keypair(), netuid=2).submit(b"\x00" * 8, 1, 2)
        self.assertEqual(got, {"error": "SomeChainError details", "block": None})

    def test_a_missing_extrinsic_id_reports_none_rather_than_raising(self):
        self.assertIsNone(_block_from_extrinsic_id(None))
        self.assertIsNone(_block_from_extrinsic_id(""))

    def test_a_malformed_extrinsic_id_reports_none_rather_than_raising(self):
        self.assertIsNone(_block_from_extrinsic_id("not-a-number"))


def a_row(*entries):
    """One storage row's value: a queue of 4-tuples, as the chain files it."""
    return list(entries)


class TheReadBackReturnsCiphertextsNotKeysOrWholeRows(unittest.TestCase):
    """read_map exists for exactly one comparison: ``commit in stored``.

    That only works if what comes back is a flat list of ciphertext bytes.
    Two shapes look plausible and are both wrong:

    A row's storage KEY is never the commit -- it is whatever the runtime
    indexes the row by -- so returning keys makes the read-back blind to
    every commit that was ever actually submitted, healthy or not.

    A row's VALUE is not one commit either. It is a QUEUE of
    ``(hotkey, commit_block, ciphertext, reveal_round)`` entries -- verified
    against a real chain in ``verification/chain_commit_spike.py``'s own
    ``find_commit()`` -- because more than one commit can land in the same
    row. Returning the queue wholesale compares our sealed bytes against a
    list of 4-tuples, which is the identical NotOnChain failure one layer
    deeper.
    """

    def test_read_map_does_not_return_the_rows_keys(self):
        class MapClient(Client):
            def query_map(self, item, params=None):
                return [("storage-key-a", a_row(("hk1", 100, b"commit-a", 200)))]

        got = Publisher(MapClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertNotIn("storage-key-a", got)

    def test_read_map_returns_the_ciphertext_field_not_the_whole_entry(self):
        class MapClient(Client):
            def query_map(self, item, params=None):
                return [("k1", a_row(("hk1", 100, b"the-ciphertext", 200)))]

        got = Publisher(MapClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertEqual(got, [b"the-ciphertext"])

    def test_multiple_entries_in_one_row_are_all_flattened_out(self):
        # Real rows can hold more than one commit at once; every entry in
        # the queue has to survive, not just the first.
        class MapClient(Client):
            def query_map(self, item, params=None):
                return [
                    (
                        "k1",
                        a_row(
                            ("hk1", 100, b"first", 200),
                            ("hk2", 101, b"second", 201),
                        ),
                    )
                ]

        got = Publisher(MapClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertEqual(got, [b"first", b"second"])

    def test_an_empty_row_contributes_nothing(self):
        class MapClient(Client):
            def query_map(self, item, params=None):
                return [("k1", []), ("k2", a_row(("hk1", 100, b"real", 200)))]

        got = Publisher(MapClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertEqual(got, [b"real"])

    def test_a_hex_string_ciphertext_is_normalized_to_bytes(self):
        class MapClient(Client):
            def query_map(self, item, params=None):
                return [("k1", a_row(("hk1", 100, "0xdeadbeef", 200)))]

        got = Publisher(MapClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertEqual(got, [bytes.fromhex("deadbeef")])

    def test_an_int_list_ciphertext_is_normalized_to_bytes(self):
        # Some decoders hand back a storage blob as a list of byte values
        # rather than a hex string or a real bytes object.
        class MapClient(Client):
            def query_map(self, item, params=None):
                return [("k1", a_row(("hk1", 100, [0xDE, 0xAD, 0xBE, 0xEF], 200)))]

        got = Publisher(MapClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertEqual(got, [bytes.fromhex("deadbeef")])

    def test_a_wrapped_row_and_a_wrapped_ciphertext_are_both_unwrapped(self):
        # The same client can hand back a plain value in one call and an
        # object carrying it under .value in another; this project has been
        # bitten by exactly that shape before (see reader.py's own history).
        # Both the row itself and an individual entry's ciphertext field can
        # arrive wrapped, independently.
        class Wrapped:
            def __init__(self, value):
                self.value = value

        class WrappedClient(Client):
            def query_map(self, item, params=None):
                row = Wrapped([("hk1", 100, Wrapped(b"real-commit-bytes"), 200)])
                return [("k1", row)]

        got = Publisher(WrappedClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertEqual(got, [b"real-commit-bytes"])

    def test_our_own_commit_is_found_among_others_after_a_real_submission(self):
        # The end-to-end shape read_stored_commits actually depends on:
        # OUR commit must be a member of what read_map returns, exactly as
        # `publish.py` checks it (`commit in stored`), against the real
        # per-row queue shape rather than a simplified one.
        class MapClient(Client):
            def query_map(self, item, params=None):
                return [
                    ("k1", a_row(("hk1", 100, b"someone-elses-commit", 200))),
                    ("k2", a_row(("hk2", 101, b"our-commit", 201))),
                ]

        stored = Publisher(MapClient(), Keypair(), netuid=2).read_map("TimelockedWeightCommits", 2)
        self.assertIn(b"our-commit", stored)


if __name__ == "__main__":
    unittest.main()
