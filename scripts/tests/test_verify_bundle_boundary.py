"""Offline adversarial controls for participant-supplied verification bundles."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture, render_output

spec = importlib.util.spec_from_file_location("boundary_verifier", ROOT / "scripts/verify_bundle.py")
vb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vb)


class BundleBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.bundle = self.parent / "bundle"
        self.bundle.mkdir()
        fixture = json.loads((ROOT / "fixtures/shared/new-wallet-early-days.json").read_text())
        values = {"fixture.json": json.dumps(fixture),
                  "score.json": render_output(score_fixture(parse_fixture(fixture))),
                  "settings.json": "{}", "chain-refs.json": "{}", "README.txt": "Public verification"}
        for name, text in values.items():
            (self.bundle / name).write_text(text)
        manifest = {"schema": vb.BUNDLE_SCHEMA, "eval_day": fixture["evalDay"],
                    "published_score": "not recorded", "files": []}
        for name in vb.DATA_FILES:
            data = (self.bundle / name).read_bytes()
            manifest["files"].append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        (self.bundle / vb.MANIFEST).write_text(json.dumps(manifest))
        self.sums()

    def sums(self):
        (self.bundle / vb.SUMS).write_text("".join(
            hashlib.sha256((self.bundle / name).read_bytes()).hexdigest() + "  " + name + "\n"
            for name in (*vb.DATA_FILES, vb.MANIFEST)))

    def result(self):
        out = io.StringIO()
        return vb.verify(self.bundle, out), out.getvalue()

    def test_valid_bundle_recomputes(self):
        self.assertEqual(self.result()[0], 0, self.result()[1])

    def test_sums_traversal_and_absolute_names_are_not_opened(self):
        outside = self.parent / "outside.txt"
        outside.write_text("not bundle input")
        for name in ("../outside.txt", str(outside), "sub/../../outside.txt"):
            with self.subTest(name=name):
                self.sums()
                with (self.bundle / vb.SUMS).open("a") as stream:
                    stream.write("0" * 64 + "  " + name + "\n")
                with patch.object(vb, "sha256_file", side_effect=AssertionError("must reject names first")):
                    self.assertTrue(vb.check_hashes(self.bundle))
                self.assertEqual(self.result()[0], 1)

    def test_manifest_invalid_names_duplicates_and_types_refuse_before_hashing(self):
        original = json.loads((self.bundle / vb.MANIFEST).read_text())
        for entries in ([{"name": "../outside.txt"}], original["files"] + original["files"][:1],
                        [{"name": []}], "not-list"):
            with self.subTest(entries=entries):
                (self.bundle / vb.MANIFEST).write_text(json.dumps(dict(original, files=entries)))
                self.sums()
                with patch.object(vb, "sha256_file", side_effect=AssertionError("must reject names first")):
                    self.assertTrue(vb.check_hashes(self.bundle))
                self.assertEqual(self.result()[0], 1)

    def test_duplicate_sums_and_json_keys_fail(self):
        sums = self.bundle / vb.SUMS
        sums.write_text(sums.read_text() + sums.read_text().splitlines()[0] + "\n")
        self.assertEqual(self.result()[0], 1)
        (self.bundle / vb.MANIFEST).write_text('{"schema":"a","schema":"b"}')
        self.sums()
        self.assertEqual(self.result()[0], 1)

    def test_symlink_data_and_index_refuse_without_reading_target(self):
        target = self.parent / "outside.txt"
        target.write_text("outside")
        data = self.bundle / "README.txt"
        data.unlink()
        data.symlink_to(target)
        self.assertEqual(self.result()[0], 1)
        data.unlink()
        data.write_text("Public verification")
        (self.parent / "INDEX.json").symlink_to(target)
        self.assertEqual(self.result()[0], 1)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO control")
    def test_fifo_fails_promptly_in_separate_process(self):
        sums = self.bundle / vb.SUMS
        sums.unlink()
        os.mkfifo(sums)
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/verify_bundle.py"), str(self.bundle)],
                              capture_output=True, text=True, timeout=3)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_size_limit_refuses_before_read(self):
        with patch.object(vb, "MAX_FILE_BYTES", 2):
            self.assertEqual(self.result()[0], 1)

    def test_parser_uses_private_snapshot_after_original_changes(self):
        original = vb.verify_snapshot
        def swap_then_verify(snapshot, out):
            self.assertNotEqual(snapshot, self.bundle)
            (self.bundle / "score.json").write_text("tampered after bounded capture")
            return original(snapshot, out)
        with patch.object(vb, "verify_snapshot", side_effect=swap_then_verify):
            self.assertEqual(self.result()[0], 0)

    def test_directory_and_symlink_bundle_refuse(self):
        data = self.bundle / "README.txt"
        data.unlink()
        data.mkdir()
        self.assertEqual(self.result()[0], 1)
        alias = self.parent / "alias"
        alias.symlink_to(self.bundle, target_is_directory=True)
        self.assertEqual(vb.verify(alias, io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
