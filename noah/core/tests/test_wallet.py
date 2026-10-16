"""What the signing key may and may not be used for.

"No custody" is easy to assert and hard to check. The version here is checkable:
the validator's blast radius is exactly the set of instructions it can compose,
so the guard names that set and refuses everything else. A key that can only
publish scores cannot move anybody's position, and that is a property a test can
hold rather than a sentence in a document.
"""

import unittest

from ..wallet import (
    PERMITTED_CALLS,
    CallNotPermitted,
    SigningKeyRefused,
    load_hotkey,
    permitted,
)


class Wallet:
    """Stands in for the wallet library, which is not needed to test the rules."""

    def __init__(self, hotkey=object(), name="noah-validator"):
        self.hotkey = hotkey
        self.name = name


class TheAllowlist(unittest.TestCase):
    def test_the_one_call_the_validator_needs_is_permitted(self):
        permitted("SubtensorModule", "commit_timelocked_mechanism_weights")

    def test_a_stake_transfer_is_refused_by_name(self):
        # The single most important case, and the reason this exists. It is
        # tested from OUTSIDE the allowlist on purpose: an allowlist cannot fail
        # inside its own list, so checking only permitted calls proves nothing.
        with self.assertRaises(CallNotPermitted) as caught:
            permitted("SubtensorModule", "transfer_stake")
        self.assertIn("transfer_stake", str(caught.exception))

    def test_every_fund_moving_call_this_chain_has_is_refused(self):
        for call in (
            "transfer_stake",
            "transfer_stake_and_hotkey",
            "move_stake",
            "add_stake",
            "remove_stake",
            "swap_coldkey",
            "swap_hotkey",
        ):
            with self.subTest(call=call):
                with self.assertRaises(CallNotPermitted):
                    permitted("SubtensorModule", call)

    def test_a_plain_balance_transfer_is_refused(self):
        with self.assertRaises(CallNotPermitted):
            permitted("Balances", "transfer_allow_death")

    def test_the_direct_weight_path_is_now_permitted_explicitly(self):
        # Issue #56 comment 5720306237: Drew, shown this exact call and this
        # exact prior refusal, decided to permit it for the network-isolated
        # private test subnet (production keeps the sealed path). This is
        # the deliberate, argued widening the old refusal test already named
        # -- not a fallback: nothing calls this call except a caller who
        # names Publisher.submit_direct on purpose (see test_submitter.py
        # and test_publish.py for the tests holding THAT absence).
        permitted("SubtensorModule", "set_mechanism_weights")

    def test_the_legacy_whole_subnet_call_stays_refused(self):
        # set_mechanism_weights (mechanism-aware, what was just permitted
        # above) and set_weights (the older, whole-subnet call of the same
        # shape without a mechanism id) are two different real calls. This
        # validator's own reveal-round math is already keyed to a mechanism
        # id, so the mechanism-aware call is the one that was actually
        # argued for; the legacy one was never asked for and stays refused.
        with self.assertRaises(CallNotPermitted):
            permitted("SubtensorModule", "set_weights")

    def test_the_same_call_name_on_another_module_is_still_refused(self):
        # The pair is what is permitted, not the name. A module swap is exactly
        # how an allowlist keyed on one half gets walked around.
        with self.assertRaises(CallNotPermitted):
            permitted("SomeOtherModule", "commit_timelocked_mechanism_weights")

    def test_an_unknown_call_is_refused_rather_than_allowed_through(self):
        # New chain releases add calls. The default for anything unrecognised is
        # no, because the alternative is that a runtime upgrade silently widens
        # what this key can do.
        with self.assertRaises(CallNotPermitted):
            permitted("SubtensorModule", "some_call_added_next_release")

    def test_the_allowlist_holds_exactly_the_known_two_entries(self):
        # If this grows again, somebody has widened what the signing key can
        # do and this test is where they have to say so deliberately -- by
        # updating the KNOWN SET below, not just a count. Holding the exact
        # set, not merely its length, is what keeps "no code path can move a
        # participant's position" provable rather than merely plausible: a
        # length check alone would pass just as happily if the second entry
        # had quietly become a fund-moving call instead of a weight-setting
        # one.
        self.assertEqual(
            PERMITTED_CALLS,
            {
                ("SubtensorModule", "commit_timelocked_mechanism_weights"),
                ("SubtensorModule", "set_mechanism_weights"),
            },
        )


class LoadingTheKey(unittest.TestCase):
    def test_a_wallet_with_a_hotkey_gives_it_up(self):
        key = object()
        self.assertIs(load_hotkey(Wallet(hotkey=key)), key)

    def test_a_wallet_with_no_hotkey_is_refused_by_name(self):
        with self.assertRaises(SigningKeyRefused) as caught:
            load_hotkey(Wallet(hotkey=None))
        self.assertIn("hotkey", str(caught.exception).lower())

    def test_a_wallet_that_raises_is_refused_rather_than_returning_nothing(self):
        class Broken:
            name = "locked"

            @property
            def hotkey(self):
                raise RuntimeError("keyfile is encrypted")

        with self.assertRaises(SigningKeyRefused):
            load_hotkey(Broken())


class NoKeyMaterialEscapes(unittest.TestCase):
    def test_a_refusal_never_quotes_what_it_was_given(self):
        """A refusal that echoes its input can put a secret in a log.

        The wallet object may hold key material, so the message must name the
        wallet and the problem without printing the thing it refused.
        """
        secret = "word " * 12

        class Leaky:
            name = "leaky"
            hotkey = None

            def __repr__(self):
                return f"<wallet mnemonic={secret}>"

        with self.assertRaises(SigningKeyRefused) as caught:
            load_hotkey(Leaky())
        self.assertNotIn(secret.strip(), str(caught.exception))

    def test_a_refused_call_names_the_call_and_not_the_arguments(self):
        with self.assertRaises(CallNotPermitted) as caught:
            permitted("Balances", "transfer_allow_death", {"dest": "5SECRET", "value": 42})
        message = str(caught.exception)
        self.assertIn("transfer_allow_death", message)
        self.assertNotIn("5SECRET", message)


if __name__ == "__main__":
    unittest.main()
