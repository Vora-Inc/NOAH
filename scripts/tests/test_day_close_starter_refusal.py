"""Unsupported starter manual paths must refuse before reading or writing state."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SOFTWARE_ROOT = Path(__file__).resolve().parents[2]
if str(SOFTWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOFTWARE_ROOT))
_spec = importlib.util.spec_from_file_location("starter_refusal_pipeline", SOFTWARE_ROOT / "scripts/day_close_pipeline.py")
pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipeline)


class TestUnsupportedStarterPipeline(unittest.TestCase):
    def test_cli_refuses_every_mode_before_external_or_canonical_side_effects(self):
        from noah.core.settings import Settings

        for mode in ([], ["--publish"], ["--publish-direct"]):
            for fixture in (False, True):
                with self.subTest(mode=mode, fixture=fixture), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    existing = root / "day-20.json"
                    existing.write_text("preserved evidence")
                    output = root / "new-output"
                    args = ["--height", "123", "--out-dir", str(output), *mode]
                    if fixture:
                        args += ["--fixture"]
                    with mock.patch.dict(os.environ, {
                        "NOAH_STARTER_ENTRY_SNAPSHOT": str(root / "unread-migration.json"),
                        "NOAH_RUNTIME_ROOT": str(root),
                    }, clear=True), mock.patch.object(Settings, "from_mapping") as settings, \
                            mock.patch("neurons.validator.connect") as connect, \
                            mock.patch("bittensor.wallet") as wallet, \
                            mock.patch.object(Path, "mkdir") as mkdir, \
                            mock.patch.object(pipeline, "run_pipeline") as run, \
                            mock.patch.object(pipeline, "diagnostic") as report:
                        self.assertEqual(pipeline.main(args), 2)
                    for call in (settings, connect, wallet, mkdir, run):
                        call.assert_not_called()
                    self.assertFalse(output.exists())
                    self.assertEqual(existing.read_text(), "preserved evidence")
                    self.assertIn("starter-entry policy", report.call_args.args[0])
                    self.assertNotIn(str(root), report.call_args.args[0])

    def test_direct_api_refuses_environment_or_attached_policy_before_read_or_mutation(self):
        for configured_by in ("environment", "adapter"):
            for flags in ({}, {"publish": True}, {"publish_direct": True}):
                with self.subTest(configured_by=configured_by, flags=flags):
                    adapter = SimpleNamespace(read=mock.Mock(), record_calculation=mock.Mock(),
                                              publish=mock.Mock(), publish_direct=mock.Mock())
                    adapter.read = mock.Mock(side_effect=AssertionError("must not read"))
                    adapter.record_calculation = mock.Mock()
                    if configured_by == "adapter":
                        adapter.starter_entry_policy = object()
                    env = {"NOAH_STARTER_ENTRY_SNAPSHOT": "migration.json"} if configured_by == "environment" else {}
                    with mock.patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(pipeline.PipelineStepFailed) as caught:
                            pipeline.run_pipeline(adapter, 123, **flags)
                    self.assertEqual(caught.exception.step, "starter_policy")
                    adapter.read.assert_not_called()
                    adapter.record_calculation.assert_not_called()
                    adapter.publish.assert_not_called()
                    adapter.publish_direct.assert_not_called()
                    self.assertFalse(hasattr(adapter, "last_scored_fixture"))

    def test_empty_starter_setting_preserves_ordinary_pipeline(self):
        for value in ("", " "):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {"NOAH_STARTER_ENTRY_SNAPSHOT": value}, clear=True):
                adapter = SimpleNamespace(read=mock.Mock(side_effect=RuntimeError("ordinary read reached")))
                with self.assertRaises(pipeline.PipelineStepFailed) as caught:
                    pipeline.run_pipeline(adapter, 123)
                self.assertEqual(caught.exception.step, "read")


