"""Reserved report settings cannot silently enable unverified publication."""

import dataclasses
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

from ..settings import (BadSetting, Settings, SettingsError, UnknownSetting, parse_env_file,
                        require_report_gate_runtime_ready)
from .test_settings import COMPLETE


class ReportGateSettings(unittest.TestCase):
    def settings(self, **changes):
        return Settings.from_mapping(dict(COMPLETE, **changes))

    def test_absent_defaults_to_off_independently_of_scoring_mode(self):
        for selection in ({}, {"NOAH_SCORING_MODE": "points-204040-v5",
                               "NOAH_POINTS_POLICY": "/unused/points-policy.json"}):
            with self.subTest(selection=selection):
                mapping = dict(COMPLETE, **selection)
                mapping.pop("NOAH_REPORT_GATE_ENABLED", None)
                mapping.pop("NOAH_REPORT_GATE_POLICY", None)
                value = Settings.from_mapping(mapping)
                self.assertIs(value.report_gate_enabled, False)
                self.assertIsNone(value.report_gate_policy)
                self.assertEqual(value.scoring_mode, selection.get("NOAH_SCORING_MODE", "legacy"))
                self.assertEqual(value.points_policy, selection.get("NOAH_POINTS_POLICY"))

    def test_explicit_off_ignores_policy_path_without_reading(self):
        for spelling in ("false", "FALSE", " False "):
            with self.subTest(spelling=spelling), mock.patch(
                "builtins.open", side_effect=AssertionError("unexpected file read")
            ), mock.patch.object(Path, "open", side_effect=AssertionError("unexpected path read")):
                absent = self.settings()
                explicit = self.settings(NOAH_REPORT_GATE_ENABLED=spelling,
                                         NOAH_REPORT_GATE_POLICY="/nonexistent/preview.json")
                self.assertEqual(dataclasses.asdict(absent), dataclasses.asdict(explicit))

    def test_report_interpreter_override_is_runtime_only_with_default_off(self):
        mapping = dict(COMPLETE)
        mapping.pop("NOAH_REPORT_GATE_ENABLED", None)
        mapping.pop("NOAH_REPORT_GATE_POLICY", None)
        baseline = Settings.from_mapping(mapping)
        mapping["NOAH_REPORT_PYTHON"] = sys.executable
        with mock.patch("builtins.open", side_effect=AssertionError("unexpected file read")), \
             mock.patch.object(Path, "open", side_effect=AssertionError("unexpected path read")), \
             mock.patch.object(Path, "is_file", side_effect=AssertionError("unexpected worker inspection")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected worker execution")):
            actual = Settings.from_mapping(mapping)
            require_report_gate_runtime_ready(actual)
        self.assertEqual(dataclasses.asdict(actual), dataclasses.asdict(baseline))
        self.assertIs(actual.report_gate_enabled, False)
        self.assertIsNone(actual.report_gate_policy)
        self.assertNotIn("report_python", dataclasses.asdict(actual))

    def test_report_interpreter_allowlist_does_not_admit_typos(self):
        for name in ("NOAH_REPORT_PYTHNO", "NOAH_REPORT_PYTHON_EXTRA"):
            with self.subTest(name=name), self.assertRaises(UnknownSetting):
                self.settings(**{name: sys.executable})

    def test_unrecognized_flag_never_means_off(self):
        for value in ("", "yes", "no", "1", "0", "on", "off", "ture"):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                self.settings(NOAH_REPORT_GATE_ENABLED=value)

    def test_on_requires_nonblank_policy_path(self):
        for value in (None, "", "   "):
            mapping = dict(COMPLETE, NOAH_REPORT_GATE_ENABLED="true")
            if value is None:
                mapping.pop("NOAH_REPORT_GATE_POLICY", None)
            else:
                mapping["NOAH_REPORT_GATE_POLICY"] = value
            with self.subTest(value=value), self.assertRaisesRegex(SettingsError, "NOAH_REPORT_GATE_POLICY"):
                Settings.from_mapping(mapping)

    def test_on_only_carries_path_does_not_assert_policy_is_verified(self):
        with mock.patch.object(Path, "open", side_effect=AssertionError("unexpected file read")):
            value = self.settings(NOAH_REPORT_GATE_ENABLED="TRUE",
                                  NOAH_REPORT_GATE_POLICY=" /untrusted/preview.json ")
        self.assertIs(value.report_gate_enabled, True)
        self.assertEqual(value.report_gate_policy, "/untrusted/preview.json")

    def test_example_explicitly_keeps_reports_off(self):
        example = Path(__file__).resolve().parents[3] / ".env.validator.mainnet.example"
        mapping = parse_env_file(example.read_text())
        self.assertEqual(mapping["NOAH_REPORT_GATE_ENABLED"], "false")
        self.assertEqual(mapping["NOAH_REPORT_GATE_POLICY"], "")

    def test_adapter_refuses_on_before_clients_files_or_intents(self):
        from ..chain_adapter import build_chain_adapter

        settings = self.settings(NOAH_REPORT_GATE_ENABLED="true",
                                 NOAH_REPORT_GATE_POLICY="/untrusted/preview.json")
        with tempfile.TemporaryDirectory() as directory:
            intent = Path(directory) / "pending.json"
            original = b'{"pending":"preserve exactly"}\n'
            intent.write_bytes(original)
            with mock.patch.object(Path, "open", side_effect=AssertionError("unexpected file access")):
                with self.assertRaisesRegex(BadSetting, "requires direct publication"):
                    build_chain_adapter(client=None, settings=settings, reader=None,
                                        wallet=None, uid=0, publish_intent_path=intent)
            self.assertEqual(intent.read_bytes(), original)

    def test_validator_connect_refuses_on_before_any_network(self):
        from neurons.validator import connect

        for network in ("test", "local", "finney"):
            settings = self.settings(NOAH_REPORT_GATE_ENABLED="true", NOAH_NETWORK=network,
                                     NOAH_REPORT_GATE_POLICY="/untrusted/preview.json")
            opener = mock.Mock(side_effect=AssertionError("unexpected connection"))
            genesis = mock.Mock(side_effect=AssertionError("unexpected genesis connection"))
            with self.subTest(network=network), self.assertRaisesRegex(BadSetting, "requires direct publication"):
                connect(settings, open_client=opener, genesis_connection=genesis)
            opener.assert_not_called()
            genesis.assert_not_called()


if __name__ == "__main__":
    unittest.main()
