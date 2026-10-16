"""A keyed RPC URL (``wss://host/<key>``) must not reach the validator's printed
output: the startup lines and the mainnet preflight show endpoint labels only,
while the preflight's genesis checks still dial the full URL.
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import neurons.validator as validator_module  # noqa: E402
from noah.core.chain import MAINNET  # noqa: E402
from noah.core.settings import Settings, SettingsError  # noqa: E402

KEY_ONE, KEY_TWO, KEY_SUBMIT = "SECRETKEYONE", "SECRETKEYTWO", "SECRETKEYSUBMIT"
URL_ONE = f"wss://one.example/{KEY_ONE}"
URL_TWO = f"wss://two.example/{KEY_TWO}?apikey={KEY_TWO}"
URL_SUBMIT = "wss://user:" + KEY_SUBMIT + "@submit.example/" + KEY_SUBMIT


def _settings():
    return mock.Mock(
        network="finney",
        netuid=1,
        chain_endpoint=URL_ONE,
        second_chain_endpoint=URL_TWO,
        submit_endpoint=URL_SUBMIT,
        wallet_name="w",
        wallet_hotkey="h",
        log_level="info",
    )


class StartupPrintsShowLabelsOnly(unittest.TestCase):
    def test_main_startup_lines_hide_the_keys(self):
        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.object(validator_module, "load_settings", return_value=_settings()),
            mock.patch.object(validator_module, "connect", side_effect=SettingsError("stop")),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                validator_module.main([])
        printed = out.getvalue()
        self.assertIn("wss://one.example", printed)
        self.assertIn("wss://two.example", printed)
        self.assertIn("wss://submit.example", printed)
        for secret in (KEY_ONE, KEY_TWO, KEY_SUBMIT):
            self.assertNotIn(secret, printed)


class PreflightShowsLabelsAndStillDialsTheFullUrl(unittest.TestCase):
    def test_preflight_lines_hide_the_keys_and_genesis_uses_the_full_url(self):
        dialled = []

        class Answering:
            def __init__(self, url):
                dialled.append(url)
                self._url = url

            def genesis_hash(self):
                if "two" in self._url:
                    raise SettingsError(f"could not ask {self._url} (boom)")
                return MAINNET

        out = io.StringIO()
        with redirect_stdout(out):
            try:
                validator_module.mainnet_preflight(
                    _settings(),
                    mock.Mock(),
                    MAINNET,
                    "5" + "H" * 47,
                    env={},
                    genesis_connection=Answering,
                    state_dir=Path("/nonexistent-state"),
                )
            except Exception:  # lines after the endpoint section are not under test
                pass
        printed = out.getvalue()
        self.assertEqual(dialled[:3], [URL_ONE, URL_TWO, URL_SUBMIT])
        self.assertIn("NOAH_CHAIN_ENDPOINT", printed)
        self.assertIn("wss://one.example", printed)
        self.assertIn("unanswered", printed)
        for secret in (KEY_ONE, KEY_TWO, KEY_SUBMIT):
            self.assertNotIn(secret, printed)
        self.assertIn("independent read endpoints", printed)
        self.assertIn("2 distinct", printed)


class PreflightDistinctnessUsesTheFullUrl(unittest.TestCase):
    def _lines(self, second):
        settings = _settings()
        settings.chain_endpoint = "wss://same.example/KEYA"
        settings.second_chain_endpoint = second
        settings.submit_endpoint = None

        class Genesis:
            def __init__(self, url):
                pass

            def genesis_hash(self):
                return MAINNET

        out = io.StringIO()
        with redirect_stdout(out):
            try:
                validator_module.mainnet_preflight(
                    settings,
                    mock.Mock(),
                    MAINNET,
                    "5" + "H" * 47,
                    env={},
                    genesis_connection=Genesis,
                    state_dir=Path("/nonexistent-state"),
                )
            except Exception:
                pass
        return out.getvalue()

    def test_two_keys_on_one_host_are_two_endpoints(self):
        self.assertIn("2 distinct", self._lines("wss://same.example/KEYB"))

    def test_the_same_url_twice_is_one_endpoint(self):
        self.assertIn("1 distinct", self._lines("wss://same.example/KEYA"))


class OpeningTheClientDoesNotEchoTheUrl(unittest.TestCase):
    def test_bt_client_failure_is_a_scrubbed_settings_error(self):
        import traceback

        bt = mock.Mock()
        bt.subtensor.side_effect = ValueError(f"{URL_ONE} isn't a valid URI")
        with mock.patch.dict("sys.modules", {"bittensor": bt}):
            with self.assertRaises(SettingsError) as caught:
                validator_module._open_bt_client(URL_ONE)
        error = caught.exception
        printed = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self.assertNotIn(KEY_ONE, printed)
        self.assertIn("wss://one.example", str(error))


class AdapterRefusalPrintsNoKey(unittest.TestCase):
    def test_chain_adapter_refused_line_hides_the_url(self):
        wallet = mock.Mock()
        wallet.hotkey.ss58_address = "5" + "H" * 47
        bt = mock.Mock()
        bt.wallet.return_value = wallet
        settings = mock.Mock(
            network="test",
            netuid=1,
            chain_endpoint="wss://one.example",
            second_chain_endpoint=None,
            submit_endpoint=None,
            wallet_name="w",
            wallet_hotkey="h",
            settle_blocks=1,
            log_level="info",
        )
        reader = mock.Mock()
        reader.current_block.return_value = 100
        refusal = SettingsError(f"cannot open {URL_ONE} ({URL_TWO} isn't a valid URI)")
        out = io.StringIO()
        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.object(validator_module, "load_settings", return_value=settings),
            mock.patch.object(validator_module, "connect", return_value=(object(), "0xg")),
            mock.patch.object(validator_module, "ChainReader", return_value=reader),
            mock.patch.object(validator_module, "gate", return_value=([("a", "ok")], 3)),
            mock.patch.object(validator_module, "build_chain_adapter", side_effect=refusal),
            mock.patch.object(validator_module, "_default_state_dir", return_value=Path("/st")),
            mock.patch.dict("sys.modules", {"bittensor": bt}),
            redirect_stdout(out),
        ):
            code = validator_module.main([])
        self.assertEqual(code, 3)
        self.assertIn("CHAIN ADAPTER REFUSED", out.getvalue())
        for secret in (KEY_ONE, KEY_TWO):
            self.assertNotIn(secret, out.getvalue())


class PathEchoInRefusalsIsScrubbedWithTheConfiguredUrls(unittest.TestCase):
    """A server's error body may repeat only the request path (the key). The refusal prints pass the
    configured endpoints to the scrub, so the path pieces of those URLs are removed."""

    def run_main(self, **patches):
        wallet = mock.Mock()
        wallet.hotkey.ss58_address = "5" + "H" * 47
        bt = mock.Mock()
        bt.wallet.return_value = wallet
        settings = mock.Mock(
            network="test",
            netuid=1,
            chain_endpoint=URL_ONE,
            second_chain_endpoint=None,
            submit_endpoint=URL_SUBMIT,
            wallet_name="w",
            wallet_hotkey="h",
            settle_blocks=1,
            log_level="info",
        )
        reader = mock.Mock()
        reader.current_block.return_value = 100
        out = io.StringIO()
        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.object(validator_module, "load_settings", return_value=settings),
            mock.patch.object(validator_module, "connect", return_value=(object(), "0xg")),
            mock.patch.object(validator_module, "ChainReader", return_value=reader),
            mock.patch.object(validator_module, "gate", return_value=([("a", "ok")], 3)),
            mock.patch.object(validator_module, "_default_state_dir", return_value=Path("/st")),
            mock.patch.dict("sys.modules", {"bittensor": bt}),
            mock.patch.multiple(validator_module, **patches),
            redirect_stdout(out),
        ):
            code = validator_module.main([])
        return code, out.getvalue()

    ECHO = f"server said: no such path /{KEY_ONE} or /{KEY_SUBMIT}"

    def test_submit_endpoint_refused(self):
        refusing = mock.Mock(side_effect=SettingsError(self.ECHO))
        code, printed = self.run_main(open_submit_client=refusing)
        self.assertEqual(code, 3)
        self.assertIn("SUBMIT ENDPOINT REFUSED", printed)
        for secret in (KEY_ONE, KEY_SUBMIT):
            self.assertNotIn(secret, printed)

    def test_chain_adapter_refused(self):
        code, printed = self.run_main(
            open_submit_client=mock.Mock(return_value=None),
            build_chain_adapter=mock.Mock(side_effect=SettingsError(self.ECHO)),
        )
        self.assertEqual(code, 3)
        self.assertIn("CHAIN ADAPTER REFUSED", printed)
        for secret in (KEY_ONE, KEY_SUBMIT):
            self.assertNotIn(secret, printed)


class SettingsRefusedPrintsNoKey(unittest.TestCase):
    def test_settings_refused_line_hides_the_url(self):
        out = io.StringIO()
        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.object(
                validator_module,
                "load_settings",
                side_effect=SettingsError(f"cannot read {URL_ONE} ({URL_TWO})"),
            ),
            redirect_stdout(out),
        ):
            code = validator_module.main([])
        self.assertEqual(code, 2)
        self.assertIn("SETTINGS REFUSED", out.getvalue())
        for secret in (KEY_ONE, KEY_TWO):
            self.assertNotIn(secret, out.getvalue())

    def test_a_url_in_a_numeric_setting_through_the_real_loader(self):
        out = io.StringIO()
        with (
            mock.patch.object(validator_module, "check_library_versions", return_value=None),
            mock.patch.dict(
                "os.environ",
                {"NOAH_HOLDING_STRIDE_BLOCKS": URL_ONE, "NOAH_NETWORK": "local"},
                clear=False,
            ),
            redirect_stdout(out),
        ):
            validator_module.main([])
        self.assertNotIn(KEY_ONE, out.getvalue())


class RealBittensorClient(unittest.TestCase):
    def test_wrong_scheme_is_rejected_without_echoing_the_url(self):
        import traceback

        try:
            import bittensor  # noqa: F401
        except ImportError:
            self.skipTest("bittensor not importable in this interpreter")
        url = "http://127.0.0.1:1/" + "CANARY" + "KEY123"
        with self.assertRaises(SettingsError) as caught:
            validator_module._open_bt_client(url)
        error = caught.exception
        printed = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self.assertNotIn("CANARYKEY123", printed)


class SettingsReprShowsLabelsOnly(unittest.TestCase):
    def test_repr_hides_keys_and_equality_still_sees_the_full_url(self):
        env = {
            "NOAH_WALLET_NAME": "w",
            "NOAH_WALLET_HOTKEY": "h",
            "NOAH_NETWORK": "local",
            "NOAH_NETUID": "2",
            "NOAH_CHAIN_ENDPOINT": URL_ONE,
            "NOAH_ENDPOINT_2": URL_TWO,
            "NOAH_SUBMIT_ENDPOINT": URL_SUBMIT,
            "NOAH_LOG_LEVEL": "info",
            "NOAH_SCORE_INTERVAL_BLOCKS": "100",
            "NOAH_WEIGHT_INTERVAL_BLOCKS": "100",
            "NOAH_SETTLE_BLOCKS": "20",
            "NOAH_REQUIRE_TWIN_PROOF": "true",
            "NOAH_LOCAL_GENESIS": "0x933b",
        }
        settings = Settings.from_mapping(env)
        text = repr(settings)
        self.assertIn("wss://one.example", text)
        for secret in (KEY_ONE, KEY_TWO, KEY_SUBMIT):
            self.assertNotIn(secret, text)
        self.assertEqual(settings, Settings.from_mapping(env))
        self.assertNotEqual(
            settings,
            Settings.from_mapping({**env, "NOAH_CHAIN_ENDPOINT": "wss://one.example/OTHER"}),
        )
        self.assertEqual(settings.chain_endpoint, URL_ONE)


if __name__ == "__main__":
    unittest.main()
