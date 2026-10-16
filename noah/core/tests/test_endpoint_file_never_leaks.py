"""NOAH_ENDPOINT_2_FILE holds a URL with an access key. Through the real settings
path, the key (a planted canary) must not reach stdout, stderr, any exception text
or formatted traceback (cause and context chain included), or any settings repr.
"""

import io
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import neurons.validator as validator_module
from noah.core.chain import MAINNET
from noah.core.chain_adapter import ChainAdapterRefused, build_chain_adapter
from noah.core.settings import Settings, SettingsError

from .test_settings import COMPLETE

CANARY = "CANARY" + "KEY123"
ROOT = Path(__file__).resolve().parents[3]
CONTENTS = {
    "keyed wss": f"wss://second.example/v2/{CANARY}\n",
    "bad scheme": f"http://127.0.0.1:1/{CANARY}\n",
}


def formatted(error):
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


class _InvalidURI(Exception):
    pass


class EndpointFileNeverLeaks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def settings_for(self, content, **extra):
        path = os.path.join(self.dir.name, "endpoint-2")
        with open(path, "w") as handle:
            handle.write(content)
        mapping = {
            **COMPLETE,
            "NOAH_ENDPOINT_2": "",
            "NOAH_ENDPOINT_2_FILE": path,
            "NOAH_CHAIN_ENDPOINT": "wss://first.example",
            **extra,
        }
        return Settings.from_mapping(mapping), mapping

    def test_settings_repr_and_str_never_hold_the_file_content(self):
        for label, content in CONTENTS.items():
            with self.subTest(label):
                settings, _ = self.settings_for(content)
                self.assertEqual(settings.second_chain_endpoint, content.strip())  # functional
                for text in (repr(settings), str(settings), f"{settings}", f"{settings!r}"):
                    self.assertNotIn(CANARY, text)
                self.assertNotIn(self.dir.name, repr(settings))

    def test_main_startup_banner_and_refusal_print_no_canary(self):
        for label, content in CONTENTS.items():
            with self.subTest(label):
                settings, _ = self.settings_for(content)
                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(
                        validator_module, "check_library_versions", return_value=None
                    ),
                    mock.patch.object(validator_module, "load_settings", return_value=settings),
                    mock.patch.object(
                        validator_module,
                        "connect",
                        side_effect=SettingsError(f"cannot open {settings.second_chain_endpoint}"),
                    ),
                    redirect_stdout(out),
                    redirect_stderr(err),
                ):
                    validator_module.main([])
                self.assertIn("first.example", out.getvalue())
                self.assertNotIn(CANARY, out.getvalue() + err.getvalue())

    def test_mainnet_preflight_prints_no_canary_and_still_dials_the_full_url(self):
        for label, content in CONTENTS.items():
            with self.subTest(label):
                settings, _ = self.settings_for(content, NOAH_NETWORK="finney")
                dialled = []

                class Genesis:
                    def __init__(self, url):
                        dialled.append(url)
                        self.url = url

                    def genesis_hash(self):
                        if self.url == settings.second_chain_endpoint:
                            raise SettingsError(f"{self.url} isn't a valid URI")
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
                            state_dir=Path(self.dir.name),
                        )
                    except Exception:  # lines after the endpoint section are not under test
                        pass
                self.assertIn(settings.second_chain_endpoint, dialled)
                self.assertIn("independent read endpoints", out.getvalue())
                self.assertNotIn(CANARY, out.getvalue())

    def test_adapter_construction_failure_has_no_canary_anywhere(self):
        for label, content in CONTENTS.items():
            with self.subTest(label):
                settings, _ = self.settings_for(content)

                class Failing:
                    def __init__(self, url, *, auto_discover=True):
                        if CANARY in url:
                            raise _InvalidURI(f"{url} isn't a valid URI: scheme isn't ws or wss")

                    def close(self):
                        pass

                with mock.patch("async_substrate_interface.SubstrateInterface", Failing):
                    with self.assertRaises(ChainAdapterRefused) as caught:
                        build_chain_adapter(
                            client=object(),
                            settings=settings,
                            reader=object(),
                            wallet=object(),
                            uid=0,
                        )
                text = formatted(caught.exception)
                self.assertNotIn(CANARY, text)
                self.assertIsNone(caught.exception.__cause__)

    def test_offline_dry_run_against_a_refused_port_prints_no_canary(self):
        """The real entry point, in a subprocess, at debug level, against 127.0.0.1:1."""
        for label, content in CONTENTS.items():
            with self.subTest(label):
                _, mapping = self.settings_for(
                    content,
                    NOAH_NETWORK="local",
                    NOAH_LOCAL_GENESIS="0x" + "ab" * 32,
                    NOAH_CHAIN_ENDPOINT="ws://127.0.0.1:1",
                    NOAH_LOG_LEVEL="debug",
                )
                env = {**os.environ, "PYTHONPATH": str(ROOT)}
                env.update({k: v for k, v in mapping.items() if v != ""})
                result = subprocess.run(
                    [sys.executable, "-m", "neurons.validator", "--dry-run"],
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=str(ROOT),
                    timeout=180,
                )
                combined = result.stdout + result.stderr
                self.assertIn("endpoint 2", combined, combined[-800:])
                self.assertNotIn(CANARY, combined)


if __name__ == "__main__":
    unittest.main()
