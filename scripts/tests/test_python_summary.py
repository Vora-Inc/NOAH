"""The dual-interpreter summary counts test output, never log path digits."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("binary_output", [False, True])
@pytest.mark.parametrize("validator_exit", [0, 1, 5])
def test_numeric_log_path_preserves_counts_and_failure(tmp_path, binary_output, validator_exit):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPTS / "run-tests-python.sh", scripts / "run-tests-python.sh")
    inventory = SCRIPTS / "check_test_suites.py"
    if inventory.exists():
        shutil.copyfile(inventory, scripts / inventory.name)
    suites = ["noah/core/tests", "noah/scoring/tests"]
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\n"
        + "".join(f'    "{suite}",\n' for suite in suites)
        + "]\n"
    )
    for suite in suites:
        directory = tmp_path / suite
        directory.mkdir(parents=True)
        (directory / "test_present.py").write_text("def test_present(): pass\n")
    interpreters = {}
    for role in ("validator", "miner-tool"):
        exit_code = validator_exit if role == "validator" else 0
        summary = (
            {0: "7 passed, 13 subtests passed", 1: "6 passed, 1 failed", 5: "no tests ran"}[exit_code]
            if role == "validator"
            else "11 passed, 2 skipped, 17 subtests passed"
        )
        output = (b"captured\x00output\n" if binary_output else b"") + (summary + "\n").encode()
        executable = tmp_path / role
        executable.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "if sys.argv[1:] == ['-c', 'import bittensor']: "
            f"sys.exit({0 if role == 'validator' else 1})\n"
            "if sys.argv[1:3] == ['-m', 'pytest']:\n"
            f"    sys.stdout.buffer.write({output!r})\n"
            f"    sys.exit({exit_code})\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        )
        executable.chmod(0o755)
        interpreters[role] = str(executable)
    env = dict(
        os.environ,
        VALIDATOR_PYTHON=interpreters["validator"],
        MINER_TOOL_PYTHON=interpreters["miner-tool"],
        RUN_TESTS_PYTHON_LOG_DIR=str(tmp_path / "logs-344-579"),
    )
    result = subprocess.run(
        ["bash", str(scripts / "run-tests-python.sh")],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == (0 if validator_exit == 0 else 1), result.stdout + result.stderr
    assert 'RESULT miner-tool STATE=PASS NOTE="11 passed, 2 skipped, 17 subtests passed"' in result.stdout
    if validator_exit == 0:
        assert 'RESULT validator STATE=PASS NOTE="7 passed, 13 subtests passed"' in result.stdout
        assert 'RESULT combined STATE=PASS NOTE="18 passed, 0 failed"' in result.stdout
    else:
        assert "RESULT validator STATE=FAIL" in result.stdout
        assert "RESULT combined STATE=FAIL" in result.stdout
    assert "integer expression expected" not in result.stderr
    assert "syntax error in expression" not in result.stderr
