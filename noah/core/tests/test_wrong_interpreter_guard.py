"""Ticket #16 (B06), 2026-09-15: #15 reported 52 failing tests in
``noah/core/tests``, all bittensor ``ModuleNotFoundError`` failures,
reproduced by stashing their own changes and still seeing the same
failures -- meaning it was on merged main itself.

Investigated directly, not assumed: reproduced the same 52 failures at the
merge-base commit (``4939738ac0875f2331013258b0e4f6d53d55acf1``, before
#16's or #14's recent work) by running ``noah/core/tests`` under the plain
``.venv`` (no bittensor) -- ``52 failed, 562 passed`` there, matching #15's
report exactly -- and confirmed the SAME tests pass cleanly under
``.venv-validator`` at the SAME commit (``614 passed``, zero failures).
This is a wiring/routing problem, not genuinely broken code:
``scripts/run-tests-python.sh`` already resolves and splits both
interpreters correctly (confirmed separately, ``RESULT combined
STATE=PASS NOTE="1714 passed, 0 failed"``); the 52 failures happen only
when this directory is collected directly, under the wrong interpreter,
bypassing that script.

The fix: ``noah/core/tests/conftest.py`` now fails collection with ONE
clear, actionable message the moment this directory is collected under an
interpreter without bittensor, instead of every individual test failing
with a deep, confusing ``ModuleNotFoundError`` that looks exactly like
broken product code. This proves that fix for real, by actually invoking
the wrong interpreter as a subprocess and checking its real output --
never inferred.
"""

import subprocess
from pathlib import Path

import pytest

_SOFTWARE_NOAH_ROOT = Path(__file__).resolve().parents[3]
_MINER_TOOL_PYTHON = _SOFTWARE_NOAH_ROOT / ".venv" / "bin" / "python"


@pytest.mark.skipif(
    not _MINER_TOOL_PYTHON.exists(),
    reason="the plain .venv (no bittensor) is not built in this environment",
)
def test_running_under_the_wrong_interpreter_fails_once_clearly_not_dozens_of_times():
    # A real subprocess, the real other interpreter, the real directory --
    # not a simulated import failure. If .venv ever gained bittensor this
    # would stop proving anything; guarded by the second assertion below.
    result = subprocess.run(
        [str(_MINER_TOOL_PYTHON), "-c", "import bittensor"],
        cwd=_SOFTWARE_NOAH_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0, (
        ".venv unexpectedly has bittensor installed -- this test's whole premise "
        "(the miner-tool interpreter lacks it) no longer holds in this environment."
    )

    result = subprocess.run(
        [str(_MINER_TOOL_PYTHON), "-m", "pytest", "-q", "noah/core/tests"],
        cwd=_SOFTWARE_NOAH_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = result.stdout + result.stderr

    assert result.returncode != 0
    assert "validator interpreter" in combined
    assert "scripts/run-tests-python.sh" in combined
    # The old failure mode (what #15 actually saw): dozens of individual
    # "ModuleNotFoundError: No module named 'bittensor'" tracebacks, one per
    # failing test, with no single explanation anywhere in the output. The
    # fix must replace that with ONE clear message, not merely add one
    # alongside the pile of confusing individual failures.
    assert "ModuleNotFoundError: No module named 'bittensor'" not in combined
    assert " failed" not in combined  # no per-test "N failed, M passed" summary at all
