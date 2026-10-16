"""Saved examples must fail review when missing, stale, or malformed."""

import importlib.util
import io
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_examples", ROOT / "scripts/check_examples.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_repository_examples_are_current():
    assert MODULE.check(ROOT / "fixtures/shared", io.StringIO()) == 0


@pytest.mark.parametrize("damage", ["missing", "changed", "malformed"])
def test_damaged_saved_answer_is_rejected(tmp_path, damage):
    name = "steady-at-target"
    shutil.copyfile(ROOT / f"fixtures/shared/{name}.json", tmp_path / f"{name}.json")
    target = tmp_path / f"out-python/{name}.score.json"
    target.parent.mkdir()
    if damage == "changed":
        data = json.loads((ROOT / f"fixtures/shared/out-python/{name}.score.json").read_text())
        data["fixture"] = "another-day"
        target.write_text(json.dumps(data))
    elif damage == "malformed":
        target.write_text("{")
    output = io.StringIO()
    assert MODULE.check(tmp_path, output) == 1
    assert name in output.getvalue()


def test_empty_directory_is_not_a_pass(tmp_path):
    assert MODULE.check(tmp_path, io.StringIO()) == 1
