"""An installer refusal must leave existing operator environments intact."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"


@pytest.mark.parametrize("kind", ["directory", "file", "dangling_symlink"])
def test_unusable_environment_is_preserved(tmp_path, kind):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPT, scripts / "install.sh")
    venv = tmp_path / ".venv"
    if kind == "directory":
        venv.mkdir()
        (venv / "operator-data").write_text("must survive")
    elif kind == "file":
        venv.write_text("must survive")
    else:
        venv.symlink_to(tmp_path / "missing-environment")
    result = subprocess.run(
        ["bash", str(scripts / "install.sh")], capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "Move it aside yourself" in result.stderr
    assert os.path.lexists(venv)
    if kind == "directory":
        assert (venv / "operator-data").read_text() == "must survive"
    elif kind == "file":
        assert venv.read_text() == "must survive"
    else:
        assert venv.is_symlink()
