"""Damaged source and public distributions must not silently skip test suites."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_test_suites", SCRIPTS / "check_test_suites.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SUITES = ["noah/core/tests", "scripts/tests", "noah/scoring/tests", "noah/ark/tests", "tests"]


def make_suite(root, suite):
    directory = root / suite
    directory.mkdir(parents=True)
    (directory / "test_present.py").write_text("def test_present(): pass\n")


def declare_omissions(root, omitted):
    (root / MODULE.OMISSIONS_FILE).write_text(json.dumps({"schema_version": 1, "omitted": omitted}))


@pytest.mark.parametrize("missing", SUITES)
def test_source_requires_every_declared_suite(tmp_path, missing):
    for suite in SUITES:
        if suite != missing:
            make_suite(tmp_path, suite)
    with pytest.raises(ValueError, match="required test suite is missing"):
        MODULE.included_suites(tmp_path, SUITES)


def test_public_distribution_accepts_only_declared_omissions(tmp_path):
    for suite in SUITES[:3]:
        make_suite(tmp_path, suite)
    declare_omissions(tmp_path, SUITES[3:])
    assert MODULE.included_suites(tmp_path, SUITES) == SUITES[:3]


def test_public_distribution_still_requires_shipped_script_tests(tmp_path):
    make_suite(tmp_path, SUITES[0])
    declare_omissions(tmp_path, SUITES[3:])
    with pytest.raises(ValueError, match="missing: scripts/tests"):
        MODULE.included_suites(tmp_path, SUITES)


def test_empty_suite_is_not_a_pass(tmp_path):
    (tmp_path / "scripts/tests").mkdir(parents=True)
    with pytest.raises(ValueError, match="contains no test files"):
        MODULE.included_suites(tmp_path, ["scripts/tests"])


@pytest.mark.parametrize("omitted", [["scripts/tests"], ["typo"], ["tests", "tests"], [1]])
def test_invalid_omissions_are_refused(tmp_path, omitted):
    declare_omissions(tmp_path, omitted)
    with pytest.raises(ValueError, match="omissions manifest"):
        MODULE.included_suites(tmp_path, SUITES)


def test_omission_cannot_hide_a_present_suite(tmp_path):
    for suite in SUITES:
        make_suite(tmp_path, suite)
    declare_omissions(tmp_path, ["tests"])
    with pytest.raises(ValueError, match="declared omitted is present"):
        MODULE.included_suites(tmp_path, SUITES)


def test_malformed_manifest_is_refused(tmp_path):
    (tmp_path / MODULE.OMISSIONS_FILE).write_text("{")
    with pytest.raises(ValueError):
        MODULE.included_suites(tmp_path, SUITES)


@pytest.mark.parametrize("suites", [[], ["tests", "tests"], ["../tests"]])
def test_bad_suite_declarations_are_refused(tmp_path, suites):
    with pytest.raises(ValueError):
        MODULE.included_suites(tmp_path, suites)


@pytest.mark.parametrize(
    "public,missing", [(False, None), (False, "tests"), (True, None), (True, "scripts/tests")]
)
def test_actual_runner_checks_inventory_before_starting_pytest(tmp_path, monkeypatch, public, missing):
    outer_logs = tmp_path / "outer-runner-logs"
    outer_logs.mkdir()
    for role in ("validator", "miner-tool"):
        (outer_logs / f"{role}.log").write_text("outer runner output must survive\n")
    monkeypatch.setenv("RUN_TESTS_PYTHON_LOG_DIR", str(outer_logs))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("run-tests-python.sh", "check_test_suites.py"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\n"
        + "".join(f'    "{suite}",\n' for suite in SUITES)
        + "]\n"
    )
    if public:
        declare_omissions(tmp_path, SUITES[3:])
    for suite in SUITES[:3] if public else SUITES:
        if suite != missing:
            make_suite(tmp_path, suite)
    calls = tmp_path / "pytest-calls"
    interpreters = {}
    for role in ("validator", "miner-tool"):
        executable = tmp_path / role
        executable.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "if sys.argv[1:] == ['-c', 'import bittensor']: "
            f"sys.exit({0 if role == 'validator' else 1})\n"
            "if sys.argv[1:3] == ['-m', 'pytest']:\n"
            f"    open({str(calls)!r}, 'a').write('called\\n')\n"
            "    print('1 passed in 0.01s')\n"
            "    sys.exit(0)\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        )
        executable.chmod(0o755)
        interpreters[role] = str(executable)
    env = dict(
        os.environ,
        VALIDATOR_PYTHON=interpreters["validator"],
        MINER_TOOL_PYTHON=interpreters["miner-tool"],
        RUN_TESTS_PYTHON_LOG_DIR=str(tmp_path / "runner-logs"),
    )
    result = subprocess.run(
        ["bash", str(scripts / "run-tests-python.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    for role in ("validator", "miner-tool"):
        assert (outer_logs / f"{role}.log").read_text() == "outer runner output must survive\n"
    if missing:
        assert result.returncode != 0
        assert "test suite inventory failed" in result.stdout
        assert not calls.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert calls.read_text().splitlines() == ["called", "called"]
