"""An operator-typed value can be an endpoint URL with an access key put in the wrong place, in
any shape. A refusal names the setting and the reason, never the value. Every shape below carries
a planted canary; none may reach an error text, a traceback, a record or a printed line."""

import io
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import neurons.validator as validator_module
from noah.chain.reads import Provenance
from noah.core.chain import ChainNotIdentified, identify
from noah.core.reader import ChainReader, ChainUnreadable
from noah.core.settings import Settings, SettingsError
from noah.core.startup import NotArchiveCapable, check_reads_history
from noah.scoring.parameters import ParametersError, load_parameters

from .test_settings import COMPLETE

CANARY = "CANARY" + "KEY123"
USER_PW = "u:" + CANARY
SHAPES = [
    f"wss://h.example/v2/{CANARY}",
    f"http://127.0.0.1:1/{CANARY}",
    f"ws://{USER_PW}@h.example:9944/x?k={CANARY}",
    f"wss:/h.example/{CANARY}",  # single slash
    f"wss://h.example/ {CANARY}",  # a space
    f"wsss://h.example/{CANARY}",  # a bad scheme
    f"h.example/{CANARY}",  # no scheme
    CANARY,  # a bare key
]


def formatted(error):
    """The traceback text, lowercased too: some settings are lowercased before they are checked."""
    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return text + text.lower().replace(CANARY.lower(), CANARY.lower())


class EverySettingRefusalHidesTheValue(unittest.TestCase):
    KEYS = (
        "NOAH_NETUID",
        "NOAH_NETWORK",
        "NOAH_LOG_LEVEL",
        "NOAH_SCORE_INTERVAL_BLOCKS",
        "NOAH_WEIGHT_INTERVAL_BLOCKS",
        "NOAH_SETTLE_BLOCKS",
        "NOAH_REQUIRE_TWIN_PROOF",
        "NOAH_HOLDING_STRIDE_BLOCKS",
        "NOAH_ENTRY_DEPOSIT_RAO",
        "NOAH_PUBLISH_MODE",
        "NOAH_ENDPOINT_2_FILE",
    )

    def test_each_setting_and_shape(self):
        for key in self.KEYS:
            for shape in SHAPES:
                with self.subTest(key=key, shape=shape):
                    with self.assertRaises(SettingsError) as caught:
                        Settings.from_mapping({**COMPLETE, key: shape})
                    self.assertNotIn(CANARY, formatted(caught.exception))
                    self.assertNotIn(CANARY.lower(), formatted(caught.exception).lower())
                    self.assertIn(key, str(caught.exception))

    def test_a_plain_absolute_path_is_still_named(self):
        with self.assertRaises(SettingsError) as caught:
            Settings.from_mapping({**COMPLETE, "NOAH_ENDPOINT_2_FILE": "/nonexistent/endpoint-2"})
        self.assertIn("/nonexistent/endpoint-2", str(caught.exception))


class TheParametersPathIsNeverRepeated(unittest.TestCase):
    def test_load_parameters(self):
        for shape in SHAPES:
            with self.subTest(shape=shape):
                with self.assertRaises(ParametersError) as caught:
                    load_parameters({"NOAH_PARAMETERS_JSON": shape})
                self.assertNotIn(CANARY, formatted(caught.exception))

    def test_settings_record(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import settings_record as sr

        for shape in SHAPES:
            with self.subTest(shape=shape):
                with self.assertRaises(ParametersError) as caught:
                    sr.build_settings_record(1, env={"NOAH_PARAMETERS_JSON": shape})
                self.assertNotIn(CANARY, formatted(caught.exception))
                with tempfile.TemporaryDirectory() as tmp:
                    result = sr.record_day_settings(
                        1, os.path.join(tmp, "day-1.json"), env={"NOAH_PARAMETERS_JSON": shape}
                    )
                self.assertNotIn(CANARY, repr(result))


class AServerThatEchoesTheRequestPathIsNotRepeated(unittest.TestCase):
    """A server's error body may repeat the request path (the key) with no scheme or host."""

    ECHO = f"HTTP 403 for GET /{CANARY}?x=1: forbidden"

    def test_identify(self):
        from bittensor.result import RpcConnectionError

        class Connection:
            def genesis_hash(self):
                raise RpcConnectionError(self.ECHO)

        Connection.ECHO = self.ECHO
        with self.assertRaises(ChainNotIdentified) as caught:
            identify(Connection())
        self.assertNotIn(CANARY, formatted(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_reader(self):
        from bittensor.result import RpcConnectionError

        echo = self.ECHO

        class Client:
            def query(self, *a, **k):
                raise RpcConnectionError(echo)

            query_map = query

            @property
            def block(self):
                raise RpcConnectionError(echo)

        reader = ChainReader(Client())
        for call in (
            reader.current_block,
            lambda: reader._query("X"),
            lambda: reader._query_map("X"),
        ):
            with self.assertRaises(ChainUnreadable) as caught:
                call()
            self.assertNotIn(CANARY, formatted(caught.exception))

    def test_archive_check(self):
        echo = self.ECHO

        class Chain:
            def historical_probe(self, depth):
                raise ChainUnreadable(echo)

        with self.assertRaises(NotArchiveCapable) as caught:
            check_reads_history(Chain(), 10)
        self.assertNotIn(CANARY, formatted(caught.exception))

    def test_validator_prints_pass_the_configured_endpoints_to_the_scrub(self):
        settings = mock.Mock(
            network="test",
            netuid=1,
            chain_endpoint=f"wss://h.example/{CANARY}",
            second_chain_endpoint=f"wss://i.example/{CANARY}2",
            submit_endpoint=None,
            wallet_name="w",
            wallet_hotkey="h",
            log_level="info",
        )
        refusal = SettingsError(f"server said: bad path /{CANARY} and /{CANARY}2")
        out = io.StringIO()
        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.object(validator_module, "load_settings", return_value=settings),
            mock.patch.object(validator_module, "connect", side_effect=refusal),
            redirect_stdout(out),
        ):
            validator_module.main([])
        self.assertIn("CHAIN REFUSED", out.getvalue())
        self.assertNotIn(CANARY, out.getvalue())






class TheMainnetAndProxyRefusalsHideTheValue(unittest.TestCase):
    def good(self, **override):
        from ._mainnet_helpers import good_mapping, write_profile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"NOAH_PARAMETERS_JSON": write_profile(self.tmp.name)}
        return Settings.from_mapping({**good_mapping(), **override})

    def test_every_operator_typed_setting_in_setting_problems(self):
        from noah.core.mainnet import setting_problems

        for key in (
            "NOAH_MAINNET_ENABLE",
            "NOAH_ARK_ADDRESS",
            "NOAH_VALIDATOR_HOTKEY",
            "NOAH_EXPECTED_SIGNER_ADDRESS",
            "NOAH_PROXY_SIGNER_ADDRESS",
        ):
            for shape in SHAPES:
                for text in (shape, shape.lower()):
                    with self.subTest(key=key, shape=text):
                        problems = "\n".join(setting_problems(self.good(**{key: text}), self.env))
                        self.assertNotIn(CANARY, problems)
                        self.assertNotIn(CANARY.lower(), problems.lower())
                        if (
                            key != "NOAH_PROXY_SIGNER_ADDRESS"
                        ):  # reported elsewhere (the signer checks)
                            self.assertIn(key, problems)

    def test_signer_and_proxy_checks_in_every_branch(self):
        from noah.core.mainnet import check_signer_hotkey
        from noah.core.proxy_route import (
            ProxyRouteRefused,
            check_proxy_signing_identity,
            identity_from_settings,
        )

        from ._mainnet_helpers import TEST_PROXY, OWNER_HOTKEY

        valid = "5" + "H" * 47
        other = "5" + "G" * 47
        for shape in SHAPES:
            for text in (shape, shape.lower()):
                with self.subTest(shape=text):
                    cases = [
                        # (expected signer, proxy signer, loaded hotkey): each reaches one message
                        (text, text, valid),  # proxy signer equals the commit identity
                        (text, other, text),  # the loaded hotkey IS the commit identity
                        (valid, text, other),  # the loaded hotkey is not the proxy signer
                        (text, TEST_PROXY, TEST_PROXY),  # finney: expected is not a valid address
                        (valid, text, text),  # finney: the proxy signer is dev / the validator
                        (OWNER_HOTKEY, text, text),  # finney: reaches the proxy signer check
                        (OWNER_HOTKEY, text, other),
                    ]
                    for expected, proxy, loaded in cases:
                        for network in ("finney", "test"):
                            settings = self.good(
                                NOAH_NETWORK=network,
                                NOAH_EXPECTED_SIGNER_ADDRESS=expected,
                                NOAH_PROXY_SIGNER_ADDRESS=proxy,
                            )
                            for call in (
                                lambda: check_signer_hotkey(settings, loaded),
                                lambda: check_proxy_signing_identity(settings, loaded),
                                lambda: identity_from_settings(settings),
                            ):
                                try:
                                    call()
                                except BaseException as refusal:  # noqa: BLE001
                                    self.assertNotIn(CANARY.lower(), formatted(refusal).lower())
        del ProxyRouteRefused

    def test_the_direct_signer_branch(self):
        from noah.core.mainnet import check_signer_hotkey

        for shape in SHAPES:
            for text in (shape, shape.lower()):
                settings = self.good(
                    NOAH_NETWORK="test",
                    NOAH_EXPECTED_SIGNER_ADDRESS=text,
                    NOAH_PROXY_SIGNER_ADDRESS="",
                )
                with self.subTest(shape=text):
                    with self.assertRaises(BaseException) as caught:
                        check_signer_hotkey(settings, "5" + "H" * 47)
                    self.assertNotIn(CANARY.lower(), formatted(caught.exception).lower())

    def test_address_parsing(self):
        from noah.chain.ss58 import NotAnAddress, public_key_of

        for shape in SHAPES:
            for text in (shape, shape.lower()):
                with self.subTest(shape=text):
                    with self.assertRaises(NotAnAddress) as caught:
                        public_key_of(text)
                    self.assertNotIn(CANARY.lower(), formatted(caught.exception).lower())


class TheMainnetDryRunHidesTheValue(unittest.TestCase):
    """The real entry point, offline: NOAH_NETWORK=finney, a typed value of every shape."""

    def test_dry_run(self):
        from ._mainnet_helpers import good_mapping

        root = Path(__file__).resolve().parents[3]
        for key in (
            "NOAH_MAINNET_ENABLE",
            "NOAH_ARK_ADDRESS",
            "NOAH_VALIDATOR_HOTKEY",
            "NOAH_EXPECTED_SIGNER_ADDRESS",
        ):
            for shape in SHAPES:
                with self.subTest(key=key, shape=shape):
                    mapping = {
                        **good_mapping(),
                        "NOAH_CHAIN_ENDPOINT": "ws://127.0.0.1:1",
                        "NOAH_ENDPOINT_2": "ws://127.0.0.1:2",
                        key: shape,
                    }
                    env = {**os.environ, "PYTHONPATH": str(root), "NOAH_LOG_LEVEL": "debug"}
                    env.update(mapping)
                    result = subprocess.run(
                        [sys.executable, "-m", "neurons.validator", "--dry-run"],
                        capture_output=True,
                        text=True,
                        env=env,
                        cwd=str(root),
                        timeout=120,
                    )
                    combined = (result.stdout + result.stderr).lower()
                    self.assertIn("refused", combined, combined[-500:])
                    self.assertNotIn(CANARY.lower(), combined)


class ALeadingDoubleSlashIsNotAPath(unittest.TestCase):
    def test_both_path_settings(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import settings_record as sr

        for shape in (f"//127.0.0.1/{CANARY}", f"//h.example/{CANARY}", f"/a//{CANARY}"):
            with self.subTest(shape=shape):
                with self.assertRaises(SettingsError) as caught:
                    Settings.from_mapping({**COMPLETE, "NOAH_ENDPOINT_2_FILE": shape})
                self.assertNotIn(CANARY, formatted(caught.exception))
                with self.assertRaises(ParametersError) as caught:
                    load_parameters({"NOAH_PARAMETERS_JSON": shape})
                self.assertNotIn(CANARY, formatted(caught.exception))
                with self.assertRaises(ParametersError) as caught:
                    sr.build_settings_record(1, env={"NOAH_PARAMETERS_JSON": shape})
                self.assertNotIn(CANARY, formatted(caught.exception))


class AParseFailureNamesNoOddPath(unittest.TestCase):
    """The file exists, so the failure comes from parsing and its label carries the path."""

    def test_parse_parameters_and_settings_record_labels(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import settings_record as sr

        with tempfile.TemporaryDirectory() as tmp:
            odd = os.path.join(tmp, f"{CANARY} profile.json")  # a space: not a plain path
            Path(odd).write_text("{}")
            with self.assertRaises(ParametersError) as caught:
                load_parameters({"NOAH_PARAMETERS_JSON": odd})
            self.assertNotIn(CANARY, formatted(caught.exception))
            with self.assertRaises(ParametersError) as caught:
                sr.build_settings_record(1, env={"NOAH_PARAMETERS_JSON": odd})
            self.assertNotIn(CANARY, formatted(caught.exception))

    def test_record_day_settings_failure_text_is_scrubbed(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import settings_record as sr

        error = RuntimeError(f"upstream said wss://h.example/{CANARY} is not valid")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sr, "build_settings_record", side_effect=error):
                result = sr.record_day_settings(1, os.path.join(tmp, "day-1.json"))
        self.assertEqual(result["action"], "failed")
        self.assertNotIn(CANARY, repr(result))


class TheProvenanceReprShowsLabelsOnly(unittest.TestCase):
    def test_repr(self):
        provenance = Provenance(
            sources={"a": f"wss://h.example/{CANARY}", "b": f"wss://i.example/{CANARY}2"}
        )
        self.assertNotIn(CANARY, repr(provenance))
        self.assertIn("wss://h.example", repr(provenance))
        self.assertEqual(
            provenance,
            Provenance(
                sources={"a": f"wss://h.example/{CANARY}", "b": f"wss://i.example/{CANARY}2"}
            ),
        )


if __name__ == "__main__":
    unittest.main()
