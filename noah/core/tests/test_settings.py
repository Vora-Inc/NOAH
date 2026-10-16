"""What the settings loader must refuse.

Almost every test here is a refusal. That is the point of the module: the
dangerous outcome is not a setting that is missing, it is a setting that is
wrong in a way that looks fine, and the worst of those turn a safety gate off
while reading as if they turned it on.
"""

import unittest

from ..settings import (
    BadSetting,
    MissingSetting,
    Settings,
    UnknownSetting,
    parse_env_file,
)

COMPLETE = {
    "NOAH_WALLET_NAME": "noah-validator",
    "NOAH_WALLET_HOTKEY": "default",
    "NOAH_NETWORK": "test",
    "NOAH_NETUID": "119",
    "NOAH_CHAIN_ENDPOINT": "",
    "NOAH_LOG_LEVEL": "info",
    "NOAH_SCORE_INTERVAL_BLOCKS": "360",
    "NOAH_WEIGHT_INTERVAL_BLOCKS": "360",
    "NOAH_SETTLE_BLOCKS": "20",
    "NOAH_REQUIRE_TWIN_PROOF": "true",
    "NOAH_LOCAL_GENESIS": "",
    "NOAH_ENDPOINT_2": "",
    "NOAH_ARK_ADDRESS": "",
    "NOAH_VALIDATOR_HOTKEY": "",
    "NOAH_DIALS_JSON": "",
    "NOAH_HISTORY_DB": "",
    "NOAH_HOLDING_STRIDE_BLOCKS": "",
    "NOAH_ENTRY_DEPOSIT_RAO": "",
    "NOAH_PUBLISH_MODE": "",
    # #112: known to Settings' _refuse_unknown (so a real, documented line
    # in .env.validator is never refused as a typo), but not a Settings
    # dataclass field -- noah.scoring.parameters.load_parameters() (the
    # single canonical reader) reads this one directly via os.environ, the
    # identical "known but not a field" treatment NOAH_RUNTIME_ROOT already
    # gets (see noah.core.settings.KEYS's own comment on each).
    "NOAH_PARAMETERS_JSON": "",
    # #135 state-outside-src fix: known to Settings' _refuse_unknown (same
    # reason as NOAH_PARAMETERS_JSON above) but not a Settings dataclass
    # field -- read directly via os.environ.get by
    # scripts/persist_score_evidence.py and neurons/validator.py's own
    # _default_state_dir (see noah.core.settings.KEYS's own comment). Now
    # documented (with an empty default) in the shipped example, the
    # identical treatment every other optional-with-empty-default setting
    # above already gets.
    "NOAH_RUNTIME_ROOT": "",
}


def without(key):
    return {k: v for k, v in COMPLETE.items() if k != key}


def with_value(key, value):
    return {**COMPLETE, key: value}


class CompleteSettings(unittest.TestCase):
    def test_a_complete_mapping_loads(self):
        settings = Settings.from_mapping(COMPLETE)
        self.assertEqual(settings.wallet_name, "noah-validator")
        self.assertEqual(settings.wallet_hotkey, "default")
        self.assertEqual(settings.network, "test")
        self.assertEqual(settings.netuid, 119)
        self.assertIsNone(settings.chain_endpoint)
        self.assertEqual(settings.score_interval_blocks, 360)
        self.assertEqual(settings.settle_blocks, 20)
        self.assertIs(settings.require_twin_proof, True)

    def test_an_endpoint_is_kept_when_given(self):
        settings = Settings.from_mapping(with_value("NOAH_CHAIN_ENDPOINT", "ws://127.0.0.1:9944"))
        self.assertEqual(settings.chain_endpoint, "ws://127.0.0.1:9944")

    def test_the_local_genesis_is_optional_and_empty_means_unset(self):
        self.assertIsNone(Settings.from_mapping(COMPLETE).local_genesis)

    def test_a_stated_local_genesis_is_carried_through(self):
        fingerprint = "0x" + "9a" * 32
        settings = Settings.from_mapping(with_value("NOAH_LOCAL_GENESIS", fingerprint))
        self.assertEqual(settings.local_genesis, fingerprint)

    def test_surrounding_whitespace_is_not_a_different_value(self):
        settings = Settings.from_mapping(with_value("NOAH_NETWORK", "  test  "))
        self.assertEqual(settings.network, "test")


class TheNetuid(unittest.TestCase):
    """The subnet number, which has no safe default.

    Zero is not a placeholder on this chain. It is the root subnet. A loader
    that quietly substituted it for an unset value would aim a weight vector at
    the wrong subnet entirely, and the run would look ordinary.
    """

    def test_an_empty_netuid_is_refused_by_name(self):
        with self.assertRaises(MissingSetting) as caught:
            Settings.from_mapping(with_value("NOAH_NETUID", ""))
        self.assertIn("NOAH_NETUID", str(caught.exception))

    def test_an_absent_netuid_is_refused_by_name(self):
        with self.assertRaises(MissingSetting) as caught:
            Settings.from_mapping(without("NOAH_NETUID"))
        self.assertIn("NOAH_NETUID", str(caught.exception))

    def test_the_refusal_says_why_zero_is_not_used_as_a_default(self):
        with self.assertRaises(MissingSetting) as caught:
            Settings.from_mapping(with_value("NOAH_NETUID", ""))
        self.assertIn("root", str(caught.exception).lower())

    def test_zero_is_accepted_when_it_is_actually_asked_for(self):
        # Refusing an unset value is not the same as refusing the number. If a
        # human writes 0 on purpose the loader is not the place to argue.
        self.assertEqual(Settings.from_mapping(with_value("NOAH_NETUID", "0")).netuid, 0)

    def test_a_negative_netuid_is_refused(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_NETUID", "-1"))

    def test_a_non_numeric_netuid_is_refused(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_NETUID", "one"))


class TheTwinProofFlag(unittest.TestCase):
    """The safety gate, and the way a boolean read from text turns itself off.

    This is the flag that stops the validator publishing when the two scoring
    implementations disagree. The failure that matters is not someone switching
    it off deliberately. It is someone writing a value they believe means on,
    and a lenient parser reading it as off.
    """

    def test_plain_true_and_false_work(self):
        self.assertIs(
            Settings.from_mapping(with_value("NOAH_REQUIRE_TWIN_PROOF", "true")).require_twin_proof,
            True,
        )
        self.assertIs(
            Settings.from_mapping(
                with_value("NOAH_REQUIRE_TWIN_PROOF", "false")
            ).require_twin_proof,
            False,
        )

    def test_capitalised_true_is_still_true(self):
        # `value == "true"` would read this as False and silently disarm the
        # gate, which is the exact failure this test exists for.
        for spelling in ("True", "TRUE", "  True  "):
            with self.subTest(spelling=spelling):
                settings = Settings.from_mapping(with_value("NOAH_REQUIRE_TWIN_PROOF", spelling))
                self.assertIs(settings.require_twin_proof, True)

    def test_a_value_that_is_neither_is_refused_rather_than_guessed(self):
        for spelling in ("yes", "1", "on", "sure", ""):
            with self.subTest(spelling=spelling):
                with self.assertRaises((BadSetting, MissingSetting)):
                    Settings.from_mapping(with_value("NOAH_REQUIRE_TWIN_PROOF", spelling))

    def test_the_refusal_names_the_accepted_spellings(self):
        with self.assertRaises(BadSetting) as caught:
            Settings.from_mapping(with_value("NOAH_REQUIRE_TWIN_PROOF", "yes"))
        message = str(caught.exception)
        self.assertIn("true", message)
        self.assertIn("false", message)


class UnknownKeys(unittest.TestCase):
    """A typo in a safety flag's NAME is the same hazard as a typo in its value.

    A misspelled key leaves the real setting at whatever it would otherwise be,
    and nothing about the run says the line had no effect.
    """

    def test_a_misspelled_noah_key_is_refused(self):
        mapping = {**COMPLETE, "NOAH_REQUIRE_TWIN_PROF": "false"}
        with self.assertRaises(UnknownSetting) as caught:
            Settings.from_mapping(mapping)
        self.assertIn("NOAH_REQUIRE_TWIN_PROF", str(caught.exception))

    def test_the_refusal_suggests_the_key_that_was_meant(self):
        mapping = {**COMPLETE, "NOAH_REQUIRE_TWIN_PROF": "false"}
        with self.assertRaises(UnknownSetting) as caught:
            Settings.from_mapping(mapping)
        self.assertIn("NOAH_REQUIRE_TWIN_PROOF", str(caught.exception))

    def test_keys_that_are_not_ours_are_left_alone(self):
        mapping = {**COMPLETE, "PATH": "/usr/bin", "HOME": "/root"}
        Settings.from_mapping(mapping)


class RequiredAndRanges(unittest.TestCase):
    def test_every_required_key_is_refused_when_absent(self):
        optional = (
            "NOAH_CHAIN_ENDPOINT",
            "NOAH_LOCAL_GENESIS",
            # #566: each refused later, by name, from build_chain_adapter
            # (a real ChainAdapter cannot be built without them) -- never
            # here at startup, the identical treatment NOAH_ENDPOINT_2
            # (also optional here, also refused later by
            # SingleSourceChainRefused) already established.
            "NOAH_ENDPOINT_2",
            "NOAH_ARK_ADDRESS",
            "NOAH_DIALS_JSON",
            "NOAH_HISTORY_DB",
            "NOAH_HOLDING_STRIDE_BLOCKS",
            "NOAH_ENTRY_DEPOSIT_RAO",
            # #363: required on mainnet, refused later by name from the chain
            # adapter (chain_adapter.py), never at Settings startup.
            "NOAH_VALIDATOR_HOTKEY",
            # #112: not a Settings field at all (see COMPLETE's own comment
            # on it) -- its absence is never a Settings-level MissingSetting,
            # only noah.scoring.parameters.load_parameters()'s own refusal,
            # the moment a real caller actually needs the profile.
            "NOAH_PARAMETERS_JSON",
            # #135 state-outside-src fix: not a Settings field at all (see
            # COMPLETE's own comment on it), the identical treatment
            # NOAH_PARAMETERS_JSON above already gets -- its absence just
            # means each of its two direct os.environ.get readers uses its
            # own derived default.
            "NOAH_RUNTIME_ROOT",
            # Ticket #16 (B06): unset MUST mean "sealed" (see ThePublishMode
            # above), never a startup refusal -- the opposite treatment from
            # the #566/#570 settings above it, not the same one.
            "NOAH_PUBLISH_MODE",
        )
        for key in COMPLETE:
            if key in optional:
                continue
            with self.subTest(key=key):
                with self.assertRaises(MissingSetting):
                    Settings.from_mapping(without(key))

    def test_an_empty_wallet_name_is_missing_rather_than_empty(self):
        with self.assertRaises(MissingSetting):
            Settings.from_mapping(with_value("NOAH_WALLET_NAME", "   "))

    def test_an_unknown_network_is_refused(self):
        with self.assertRaises(BadSetting) as caught:
            Settings.from_mapping(with_value("NOAH_NETWORK", "mainnet"))
        self.assertIn("finney", str(caught.exception))

    def test_an_unknown_log_level_is_refused(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_LOG_LEVEL", "verbose"))

    def test_a_zero_interval_is_refused(self):
        # A zero interval means every block, which is a rate-limit ban rather
        # than a fast validator.
        for key in ("NOAH_SCORE_INTERVAL_BLOCKS", "NOAH_WEIGHT_INTERVAL_BLOCKS"):
            with self.subTest(key=key):
                with self.assertRaises(BadSetting):
                    Settings.from_mapping(with_value(key, "0"))

    def test_settle_blocks_may_be_zero_but_not_negative(self):
        self.assertEqual(
            Settings.from_mapping(with_value("NOAH_SETTLE_BLOCKS", "0")).settle_blocks, 0
        )
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_SETTLE_BLOCKS", "-1"))

    def test_a_decimal_block_count_is_refused_rather_than_truncated(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_SETTLE_BLOCKS", "20.5"))

    def test_an_underscored_number_is_refused(self):
        # Python reads "3_6_0" as 360. A settings file is not Python source and
        # a reader scanning that line would not see 360.
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_SETTLE_BLOCKS", "2_0"))

    def test_holding_stride_blocks_is_none_when_unset(self):
        # #566: optional at the Settings level -- refused later, by name,
        # from build_chain_adapter (ParticipantHoldingsNotWired), the same
        # treatment NOAH_ENDPOINT_2 already gets.
        self.assertIsNone(
            Settings.from_mapping(without("NOAH_HOLDING_STRIDE_BLOCKS")).holding_stride_blocks
        )
        self.assertIsNone(
            Settings.from_mapping(
                with_value("NOAH_HOLDING_STRIDE_BLOCKS", "")
            ).holding_stride_blocks
        )

    def test_holding_stride_blocks_parses_a_real_whole_number(self):
        self.assertEqual(
            Settings.from_mapping(
                with_value("NOAH_HOLDING_STRIDE_BLOCKS", "360")
            ).holding_stride_blocks,
            360,
        )

    def test_holding_stride_blocks_of_zero_or_less_is_refused(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_HOLDING_STRIDE_BLOCKS", "0"))

    def test_a_non_digit_holding_stride_blocks_is_refused(self):
        for bad in ("-1", "20.5", "2_0", "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(BadSetting):
                    Settings.from_mapping(with_value("NOAH_HOLDING_STRIDE_BLOCKS", bad))

    def test_entry_deposit_rao_is_none_when_unset(self):
        # #570: optional at the Settings level -- refused later, by name,
        # from build_chain_adapter (EntryDepositAmountNotWired), the same
        # treatment NOAH_HOLDING_STRIDE_BLOCKS already gets.
        self.assertIsNone(
            Settings.from_mapping(without("NOAH_ENTRY_DEPOSIT_RAO")).entry_deposit_rao
        )
        self.assertIsNone(
            Settings.from_mapping(with_value("NOAH_ENTRY_DEPOSIT_RAO", "")).entry_deposit_rao
        )

    def test_entry_deposit_rao_parses_a_real_whole_number(self):
        self.assertEqual(
            Settings.from_mapping(
                with_value("NOAH_ENTRY_DEPOSIT_RAO", "100000000000")
            ).entry_deposit_rao,
            100_000_000_000,
        )

    def test_entry_deposit_rao_of_zero_or_less_is_refused(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_ENTRY_DEPOSIT_RAO", "0"))

    def test_a_non_digit_entry_deposit_rao_is_refused(self):
        for bad in ("-1", "20.5", "2_0", "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(BadSetting):
                    Settings.from_mapping(with_value("NOAH_ENTRY_DEPOSIT_RAO", bad))


class ThePublishMode(unittest.TestCase):
    """Ticket #16 (B06), issue #56 comment 5720306237.

    Unlike NOAH_HOLDING_STRIDE_BLOCKS/NOAH_ENTRY_DEPOSIT_RAO above, unset
    here is not merely allowed -- it MUST mean "sealed", the literal
    behavior every existing deployment already has, because this is the
    setting production has never heard of and must keep behaving as if it
    still hasn't. A present, misspelled value is still refused exactly like
    NOAH_NETWORK's own _one_of, because a typo that reads as the default
    silently is the identical hazard this module's docstring already names
    for NOAH_REQUIRE_TWIN_PROOF.
    """

    def test_unset_defaults_to_sealed(self):
        self.assertEqual(Settings.from_mapping(without("NOAH_PUBLISH_MODE")).publish_mode, "sealed")

    def test_blank_defaults_to_sealed(self):
        self.assertEqual(
            Settings.from_mapping(with_value("NOAH_PUBLISH_MODE", "")).publish_mode, "sealed"
        )

    def test_explicit_sealed_is_accepted(self):
        self.assertEqual(
            Settings.from_mapping(with_value("NOAH_PUBLISH_MODE", "sealed")).publish_mode,
            "sealed",
        )

    def test_direct_is_accepted(self):
        self.assertEqual(
            Settings.from_mapping(with_value("NOAH_PUBLISH_MODE", "direct")).publish_mode,
            "direct",
        )

    def test_it_is_accepted_case_insensitively_like_the_other_one_of_settings(self):
        self.assertEqual(
            Settings.from_mapping(with_value("NOAH_PUBLISH_MODE", "DIRECT")).publish_mode,
            "direct",
        )

    def test_an_unrecognised_value_is_refused_rather_than_read_as_the_default(self):
        with self.assertRaises(BadSetting):
            Settings.from_mapping(with_value("NOAH_PUBLISH_MODE", "open"))


class EnvFileParsing(unittest.TestCase):
    def test_comments_and_blank_lines_are_ignored(self):
        parsed = parse_env_file(
            "# a comment\n\nNOAH_NETWORK=test\n   # an indented comment\nNOAH_NETUID=119\n"
        )
        self.assertEqual(parsed, {"NOAH_NETWORK": "test", "NOAH_NETUID": "119"})

    def test_an_empty_value_is_kept_as_empty(self):
        self.assertEqual(parse_env_file("NOAH_NETUID=\n"), {"NOAH_NETUID": ""})

    def test_surrounding_quotes_are_removed(self):
        parsed = parse_env_file('NOAH_CHAIN_ENDPOINT="ws://127.0.0.1:9944"\n')
        self.assertEqual(parsed["NOAH_CHAIN_ENDPOINT"], "ws://127.0.0.1:9944")

    def test_a_line_with_no_equals_is_refused_with_its_number(self):
        with self.assertRaises(BadSetting) as caught:
            parse_env_file("NOAH_NETWORK=test\nNOAH_NETUID 119\n")
        self.assertIn("line 2", str(caught.exception))

    def test_a_duplicated_key_is_refused_rather_than_last_one_winning(self):
        # Silently taking the last is how a file can say true at the top and
        # mean false at the bottom.
        with self.assertRaises(BadSetting) as caught:
            parse_env_file("NOAH_NETUID=119\nNOAH_NETUID=120\n")
        self.assertIn("NOAH_NETUID", str(caught.exception))

    def test_an_export_prefix_is_accepted(self):
        self.assertEqual(parse_env_file("export NOAH_NETUID=119\n"), {"NOAH_NETUID": "119"})

    def test_the_shipped_mainnet_example_file_parses(self):
        """Validate the example distributed with the public participant code."""
        from pathlib import Path

        example = Path(__file__).resolve().parents[3] / ".env.validator.mainnet.example"
        parsed = parse_env_file(example.read_text())
        settings = Settings.from_mapping(parsed)
        self.assertEqual(settings.netuid, 82)
        self.assertEqual(settings.network, "finney")


class NoKeyMaterial(unittest.TestCase):
    def test_settings_hold_names_and_never_secrets(self):
        """A wallet name is not a key. Nothing here should ever hold one."""
        settings = Settings.from_mapping(COMPLETE)
        fields = vars(settings)
        for suspicious in ("mnemonic", "seed", "private", "secret", "password"):
            for name in fields:
                self.assertNotIn(suspicious, name.lower())

    def test_the_repr_does_not_grow_a_secret_later(self):
        text = repr(Settings.from_mapping(COMPLETE))
        self.assertIn("noah-validator", text)
        for suspicious in ("mnemonic", "seed", "private"):
            self.assertNotIn(suspicious, text.lower())


if __name__ == "__main__":
    unittest.main()


from ..settings import SettingsError  # noqa: E402


class SettingsErrorsDoNotEchoALine(unittest.TestCase):
    """A settings line may hold an endpoint with an access key; no error text may repeat it."""

    CANARY = "CANARY" + "KEY123"

    def refused(self, text):
        from ..settings import parse_env_file

        with self.assertRaises(SettingsError) as caught:
            parse_env_file(text)
        message = str(caught.exception)
        self.assertNotIn(self.CANARY, message)
        return message

    def test_a_line_without_an_equals_sign(self):
        message = self.refused(f"NOAH_CHAIN_ENDPOINT wss://host.example/v2/{self.CANARY}\n")
        self.assertIn("line 1", message)

    def test_a_bare_carriage_return_inside_a_value(self):
        message = self.refused(f"NOAH_NETUID=1\rwss://host.example/v2/{self.CANARY}\n")
        self.assertIn("line 2", message)

    def test_an_unknown_name_that_is_a_url(self):
        from ..settings import Settings

        with self.assertRaises(SettingsError) as caught:
            Settings.from_mapping({**COMPLETE, f"NOAH_wss://host.example/{self.CANARY}": "1"})
        self.assertNotIn(self.CANARY, str(caught.exception))

    def test_a_duplicate_name_that_is_a_url(self):
        self.refused(f"wss://host.example/{self.CANARY}=1\nwss://host.example/{self.CANARY}=2\n")

    def test_a_bad_value_in_a_known_setting_is_shown_with_urls_reduced(self):
        from ..settings import Settings

        with self.assertRaises(SettingsError) as caught:
            Settings.from_mapping({**COMPLETE, "NOAH_NETUID": f"wss://host.example/{self.CANARY}"})
        self.assertNotIn(self.CANARY, str(caught.exception))


class EveryValueEchoIsScrubbed(unittest.TestCase):
    """A URL put into the wrong setting must not come back, key and all, in an error."""

    CANARY = "CANARY" + "KEY123"

    def error_for(self, key, value):
        from ..settings import Settings

        with self.assertRaises(SettingsError) as caught:
            Settings.from_mapping({**COMPLETE, key: value})
        return str(caught.exception)

    def test_each_echoing_setting(self):
        urls = (
            f"wss://h.example/v2/{self.CANARY}",
            f"http://127.0.0.1:1/{self.CANARY}",
            "ws://u:" + self.CANARY + "@h.example:9944/x?k=" + self.CANARY,
        )
        for key in (
            "NOAH_HOLDING_STRIDE_BLOCKS",
            "NOAH_ENTRY_DEPOSIT_RAO",
            "NOAH_NETUID",
            "NOAH_NETWORK",
            "NOAH_LOG_LEVEL",
            "NOAH_REQUIRE_TWIN_PROOF",
            "NOAH_SCORE_INTERVAL_BLOCKS",
            "NOAH_SETTLE_BLOCKS",
        ):
            for url in urls:
                with self.subTest(key=key, url=url):
                    self.assertNotIn(self.CANARY, self.error_for(key, url))

    def test_a_url_where_the_file_path_belongs(self):
        for url in (
            f"wss://h.example/v2/{self.CANARY}",
            f"http://127.0.0.1:1/{self.CANARY}",
            "ws://u:" + self.CANARY + "@h.example/x",
        ):
            with self.subTest(url=url):
                message = self.error_for("NOAH_ENDPOINT_2_FILE", url)
                self.assertNotIn(self.CANARY, message)
                self.assertIn("NOAH_ENDPOINT_2_FILE", message)

    def test_a_plain_path_is_still_named(self):
        message = self.error_for("NOAH_ENDPOINT_2_FILE", "/nonexistent/endpoint-2")
        self.assertIn("/nonexistent/endpoint-2", message)
