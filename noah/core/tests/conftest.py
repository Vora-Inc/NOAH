"""``noah/core/tests/**`` needs the real chain client (bittensor) -- see
``scripts/run-tests-python.sh``'s own header and issue #563 for why one
interpreter cannot run this whole suite: give it bittensor and it can run
this directory but can no longer prove the rest of the tree is clean of it;
withhold bittensor and this directory cannot even be imported.

Ticket #16 (B06), 2026-09-15: #15 reported 52 failing tests in this
directory, all bittensor ``ModuleNotFoundError`` failures, and confirmed
this predated their own work by stashing it and still seeing the same
failures. Investigated directly: reproduced the identical 52 failures at
the merge-base commit (``4939738ac0875f2331013258b0e4f6d53d55acf1``) by
running this directory under the plain ``.venv`` (no bittensor) --
``52 failed, 562 passed``, matching #15's report exactly -- and confirmed
the SAME tests pass cleanly under ``.venv-validator`` at that SAME commit
(``614 passed``, zero failures). This is a wiring/routing problem, not
broken code: ``scripts/run-tests-python.sh`` already resolves and splits
both interpreters correctly when it is the thing actually run
(``RESULT combined STATE=PASS NOTE="1714 passed, 0 failed"``) -- the 52
failures happen only when this directory is collected directly, bypassing
that script, under an interpreter that never had bittensor to begin with.

Before this fix, that wrong-interpreter run produced dozens of individual,
deep ``ModuleNotFoundError`` tracebacks -- one per failing test, each
looking exactly like broken product code, with no single place explaining
the real cause. This conftest fails collection with ONE clear, actionable
message instead, the moment this directory is collected under an
interpreter that cannot import bittensor -- before any individual test in
it even runs. See ``noah/core/tests/test_wrong_interpreter_guard.py`` for
the test proving this for real, by actually invoking the wrong interpreter
as a subprocess.
"""

import sys

import pytest

try:
    import bittensor  # noqa: F401
except ModuleNotFoundError as error:
    if error.name != "bittensor":
        raise
    pytest.exit(
        "\n"
        "noah/core/tests requires the validator interpreter (bittensor "
        f"installed) -- this interpreter does not have it ({sys.executable}).\n"
        "Run this directory directly with .venv-validator/bin/python, or run "
        "the whole suite with scripts/run-tests-python.sh, which resolves "
        "both interpreters and always sends this directory to the correct "
        "one. See that script's own header comment for the full reasoning "
        "(issue #563).",
        returncode=2,
    )

import noah.deposits.entry_gate as entry_gate  # noqa: E402
ENTRY_DEPOSIT_RAO_P020 = 100 * 1_000_000_000  # Historical fixture amount.


@pytest.fixture(autouse=True)
def default_entry_deposit_profile_figure(monkeypatch):
    """Ticket #112 (2026-09-19): ``noah.deposits.entry_gate.
    confirm_entry_before_score`` (wired into ``ChainAdapter.read()`` via
    ``exclude_unconfirmed_entries``, which this directory's own
    ``test_duplicate_coldkey_wiring.py`` exercises end to end) now reads
    the entry-deposit figure by calling ``noah.config.profile.
    entry_deposit_rao()`` directly, with no caller-supplied argument -- see
    ``noah/deposits/d143.py``'s own module-level comment and
    ``noah/deposits/tests/conftest.py``'s identical fixture for the full
    rationale. ``noah/config/profile.py`` is real (reads
    ``NOAH_PARAMETERS_JSON``); this patches the same import site so this
    directory's existing tests keep their historical 100-NOAH expectation
    unchanged without needing that env var or a real parameters file set.
    """
    monkeypatch.setattr(entry_gate, "entry_deposit_rao", lambda: ENTRY_DEPOSIT_RAO_P020)
