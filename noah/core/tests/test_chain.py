"""What the chain connection must refuse before the validator does anything.

The one incident this exists for: a read meant for a local chain reached the live
network, because a native node held the port the local one was expected on. The
endpoint answered plausibly. Right software, right version number, subnets
present. Only the block height gave it away, and a block height is a number a
person skims past.
"""

import unittest

from ..chain import (
    KNOWN_CHAINS,
    ChainNotIdentified,
    WrongChain,
    identify,
    require_chain,
)

LOCAL = "0x7f0279c35dd5d9f9f0bb7ca10e2fd0c5d405028fc52fe42b026ad94e038a51a5"
TESTNET = "0x8f9cf856bf558a14440e75569c9e58594757048d7b3a84b5d25f6bd978263105"
MAINNET = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"


class Fake:
    """A connection that answers with whatever genesis it was given."""

    def __init__(self, genesis):
        self._genesis = genesis

    def genesis_hash(self):
        if isinstance(self._genesis, Exception):
            raise self._genesis
        return self._genesis


class Identifying(unittest.TestCase):
    def test_a_known_chain_is_named(self):
        self.assertEqual(identify(Fake(LOCAL)), (LOCAL, "LOCAL"))
        self.assertEqual(identify(Fake(MAINNET)), (MAINNET, "MAINNET"))

    def test_an_unknown_genesis_is_named_unknown_rather_than_guessed(self):
        other = "0x" + "ab" * 32
        self.assertEqual(identify(Fake(other)), (other, "UNKNOWN"))

    def test_a_connection_that_cannot_answer_is_refused_not_treated_as_unknown(self):
        # A failure to identify and a chain we do not recognise are different
        # facts. Collapsing them would let a dead endpoint look like a stranger.
        #
        # bittensor.RpcConnectionError, not a bare ConnectionError: after
        # #510's investigation reached this file too (codex adversarial
        # review, 2026-08-29, "the same defect shape as #510, one step
        # earlier in preflight"), identify() catches the same explicit,
        # named transport-failure family noah.core.reader does, not
        # `except Exception`. A bare ConnectionError is not one of those
        # named types and would (correctly, post-fix) propagate as itself
        # rather than becoming ChainNotIdentified -- see
        # ARealBugDuringIdentificationCrashesRatherThanRefusing below for
        # that exact case held closed.
        #
        # Imported from bittensor.result, the module that genuinely defines
        # it, not the top-level bittensor package: a bare `import bittensor`
        # here was itself part of #510's regression against the sibling
        # worktree's signer audit (found and fixed 2026-08-29; see reader.py's
        # `_transport_failure_types` docstring for the full reasoning).
        from bittensor.result import RpcConnectionError

        with self.assertRaises(ChainNotIdentified):
            identify(Fake(RpcConnectionError("no route")))

    def test_an_empty_answer_is_refused_rather_than_passed(self):
        # Emptiness satisfies almost every check ever written, so it is named.
        for answer in (None, "", "0x", "not-a-hash"):
            with self.subTest(answer=answer):
                with self.assertRaises(ChainNotIdentified):
                    identify(Fake(answer))


class ARealBugDuringIdentificationCrashesRatherThanRefusing(unittest.TestCase):
    """Found by codex adversarial review of #510 (2026-08-29): identify()'s
    `except Exception` was "the same defect shape as #510, one step earlier
    in preflight" -- a validator bug reaching `connection.genesis_hash()`
    (e.g. a TypeError from a broken `Connection` adapter) used to read as
    the identical `ChainNotIdentified` -> "CHAIN REFUSED" message a real
    dead endpoint produces, before ChainReader is even constructed.

    A real transport failure must still refuse cleanly (see
    `test_a_connection_that_cannot_answer_is_refused_not_treated_as_unknown`
    above); a genuine bug must not.
    """

    def test_a_bug_in_genesis_hash_propagates_rather_than_reading_as_unidentified(self):
        with self.assertRaises(TypeError):
            identify(Fake(TypeError("connection.genesis_hash() called wrong")))


class Requiring(unittest.TestCase):
    def test_the_expected_chain_is_accepted_and_its_genesis_returned(self):
        self.assertEqual(require_chain(Fake(LOCAL), "LOCAL"), LOCAL)

    def test_the_incident_itself_is_refused_and_says_mainnet_out_loud(self):
        # Asking for LOCAL and being answered by MAINNET is the real event.
        with self.assertRaises(WrongChain) as caught:
            require_chain(Fake(MAINNET), "LOCAL")
        self.assertIn("MAINNET", str(caught.exception))

    def test_mainnet_is_refused_even_when_it_is_asked_for_by_name(self):
        # No caller may target it, whatever reason they give, so the refusal
        # cannot be argued around by changing the expectation.
        with self.assertRaises(WrongChain) as caught:
            require_chain(Fake(MAINNET), "MAINNET")
        self.assertIn("MAINNET", str(caught.exception))

    def test_asking_for_mainnet_refuses_without_touching_the_endpoint_at_all(self):
        """Pins the by-name refusal specifically, and it needed pinning.

        A sweep showed both mainnet guards were individually removable with
        every test still green, because each was covered by the other. Together
        they defend the case that matters and neither alone was load-bearing, so
        a later change could have deleted one for a good reason and left the
        remaining defence resting on a coincidence.

        This pins the first guard by its real purpose: when the caller asks for
        the live network, refuse before speaking to anything. The connection
        here explodes if consulted, so a WrongChain proves the refusal came
        first and a ChainNotIdentified would prove it did not.
        """
        exploding = Fake(AssertionError("the endpoint must not be consulted at all"))
        with self.assertRaises(WrongChain):
            require_chain(exploding, "MAINNET")

    def test_an_endpoint_answering_as_mainnet_refuses_with_its_own_louder_message(self):
        """Pins the second guard, which the ordinary mismatch would otherwise mask.

        Being answered by the live network when you asked for a local chain is
        the real incident, and it must not read as an ordinary wrong-chain
        mismatch. The louder wording is the guard's observable behaviour, so it
        is what the test holds it to.
        """
        with self.assertRaises(WrongChain) as caught:
            require_chain(Fake(MAINNET), "LOCAL")
        message = str(caught.exception)
        self.assertIn("STOP", message)
        self.assertIn("port", message)

    def test_the_wrong_known_chain_is_refused(self):
        with self.assertRaises(WrongChain):
            require_chain(Fake(TESTNET), "LOCAL")

    def test_an_unknown_chain_is_refused_rather_than_permitted(self):
        """An unrecognised chain refuses, and the message names what answered.

        This asserted the phrase "not a chain" until the local route gained a
        more specific message. The property being held is the refusal and the
        operator being told which genesis answered, not a particular sentence:
        holding the wording would have made a clearer message look like a
        regression.
        """
        stranger = "0x" + "cd" * 32
        with self.assertRaises(WrongChain) as caught:
            require_chain(Fake(stranger), "LOCAL")
        self.assertIn(stranger, str(caught.exception))

    def test_an_unknown_chain_is_refused_for_the_public_test_network_too(self):
        # The general path, which the local one no longer goes through.
        stranger = "0x" + "cd" * 32
        with self.assertRaises(WrongChain) as caught:
            require_chain(Fake(stranger), "TESTNET")
        self.assertIn("not a chain", str(caught.exception).lower())

    def test_an_expectation_that_is_not_a_known_chain_is_refused(self):
        with self.assertRaises(ValueError):
            require_chain(Fake(LOCAL), "STAGING")

    def test_a_dead_endpoint_is_refused_rather_than_passed(self):
        # See test_a_connection_that_cannot_answer_is_refused_not_treated_as_unknown
        # above for why this imports from bittensor.result specifically.
        from bittensor.result import RpcConnectionError

        with self.assertRaises(ChainNotIdentified):
            require_chain(Fake(RpcConnectionError("gone")), "LOCAL")


class AConfiguredLocalChain(unittest.TestCase):
    """A local chain gets a new identity every rebuild, so it is configured.

    The danger in this change is widening the guard while improving its
    ergonomics. Every test below exists to hold the line that the live network
    stays unreachable no matter what anybody configures.
    """

    def test_a_local_chain_matching_the_configured_genesis_is_accepted(self):
        fresh = "0x" + "9a" * 32
        self.assertEqual(require_chain(Fake(fresh), "LOCAL", local_genesis=fresh), fresh)

    def test_a_local_chain_that_does_not_match_what_was_configured_is_refused(self):
        with self.assertRaises(WrongChain):
            require_chain(Fake("0x" + "9a" * 32), "LOCAL", local_genesis="0x" + "bb" * 32)

    def test_asking_for_local_without_configuring_one_refuses(self):
        # The important one. Absence must refuse rather than accept anything:
        # "no expectation" turning into "any chain will do" is exactly the
        # widening this change must not cause.
        with self.assertRaises(WrongChain) as caught:
            require_chain(Fake("0x" + "9a" * 32), "LOCAL", local_genesis=None)
        self.assertIn("which local chain", str(caught.exception).lower())

    def test_configuring_the_live_network_as_local_is_still_refused(self):
        # Somebody could paste the live network's fingerprint into the setting,
        # by accident or to get past a refusal. It must not work.
        with self.assertRaises(WrongChain) as caught:
            require_chain(Fake(MAINNET), "LOCAL", local_genesis=MAINNET)
        self.assertIn("MAINNET", str(caught.exception))

    def test_configuring_the_live_network_does_not_even_reach_the_endpoint(self):
        exploding = Fake(AssertionError("must not be consulted"))
        with self.assertRaises(WrongChain):
            require_chain(exploding, "LOCAL", local_genesis=MAINNET)

    def test_a_configured_genesis_does_not_affect_the_public_test_network(self):
        # The test network's identity is fixed and must not become overridable
        # as a side effect of making the local one configurable.
        with self.assertRaises(WrongChain):
            require_chain(Fake("0x" + "9a" * 32), "TESTNET", local_genesis="0x" + "9a" * 32)

    def test_the_built_in_local_entry_still_works_when_nothing_is_configured(self):
        # Backwards compatible: the recorded local chain is still accepted
        # without configuration, so this change adds a route rather than
        # replacing one.
        self.assertEqual(require_chain(Fake(LOCAL), "LOCAL"), LOCAL)


class TheRealConnectionIsActuallyWiredToIdentify(unittest.TestCase):
    """Neither test_reader.py nor the classes above alone prove this composes.

    test_reader.py proves `Connection.genesis_hash()` returns None rather
    than crashing when the real client's `block_info(0)` comes back
    header-less. The classes above prove `identify()` refuses a None
    genesis cleanly. This drives the REAL `Connection` (not the hand-rolled
    `Fake` above) through `identify()` the way `neurons/validator.py`'s
    `connect()` actually does, which is the only thing that proves the two
    fixes actually reach each other -- found missing entirely (no test
    wired `Connection` to anything) by codex adversarial review
    (2026-08-29).
    """

    def test_a_header_less_genesis_response_is_refused_not_crashed(self):
        from ..reader import Connection

        class Client:
            def block_info(self, number):
                return None

        with self.assertRaises(ChainNotIdentified):
            identify(Connection(Client()))

    def test_a_real_answer_composes_through_to_a_known_chain(self):
        from ..reader import Connection

        class BlockInfo:
            hash = LOCAL

        class Client:
            def block_info(self, number):
                return BlockInfo()

        self.assertEqual(identify(Connection(Client())), (LOCAL, "LOCAL"))


class TheKnownTable(unittest.TestCase):
    def test_mainnet_is_in_the_table_so_its_refusal_can_name_it(self):
        # It is listed precisely so the refusal says MAINNET rather than
        # "unknown chain". A specific refusal survives a tired operator.
        self.assertIn(MAINNET, KNOWN_CHAINS)
        self.assertEqual(KNOWN_CHAINS[MAINNET], "MAINNET")

    def test_every_known_genesis_is_a_full_length_hash(self):
        for genesis in KNOWN_CHAINS:
            with self.subTest(genesis=genesis):
                self.assertTrue(genesis.startswith("0x"))
                self.assertEqual(len(genesis), 66)


if __name__ == "__main__":
    unittest.main()
