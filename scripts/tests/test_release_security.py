"""Release checks for configuration-backup leakage and installer refusal."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from noah.deposits.assistant.config_edit import edit_file


ROOT = Path(__file__).resolve().parents[2]


class ReleaseSecurityTests(unittest.TestCase):
    def test_editor_backup_retains_recovery_data_but_is_gitignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            shutil.copyfile(ROOT / ".gitignore", root / ".gitignore")
            for name in ("noah-deposit.ini", "profiles/custom.ini"):
                config = root / name
                config.parent.mkdir(parents=True, exist_ok=True)
                original = "[chain]\nendpoint = wss://example.invalid/SYNTHETIC-MARKER\n"
                config.write_text(original)

                def editor(argv, **_):
                    Path(argv[-1]).write_text("[chain]\nendpoint = wss://example.invalid/updated\n")
                    return SimpleNamespace(returncode=0)

                edit_file(config, lambda _: None, run=editor)
                backups = list(config.parent.glob(config.name + ".before-edit-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), original)
                self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
                ignored = subprocess.run(
                    ["git", "-C", str(root), "check-ignore", "--quiet", str(backups[0])],
                    check=False,
                )
                self.assertEqual(ignored.returncode, 0)

    def test_missing_constraints_refuses_before_package_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/install.sh", root / "scripts/install.sh")
            python = root / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            # This existing environment passes only the interpreter-version probe.
            # Any install attempt instead creates a marker, without network access.
            marker = root / "unexpected-install"
            python.write_text('#!/bin/sh\nif [ "$1" = "-c" ] || [ "$1" = "--version" ]; then exit 0; fi\ntouch "$INSTALL_MARKER"\nexit 1\n')
            python.chmod(0o755)
            result = subprocess.run(
                ["bash", str(root / "scripts/install.sh")],
                env={**os.environ, "INSTALL_MARKER": str(marker)},
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing an unconstrained install", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
