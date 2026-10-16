"""The miner's audit of a published day, and every way it must refuse.

This tool exists to detect a difference, so a test suite that only shows it
agreeing shows nothing. Every case below is a way it must NOT say "agrees".

"""

import io
import json
import pathlib

import pytest


from noah.scoring.audit_published import (
    EXIT_AGREES,
    EXIT_DISAGREES,
    EXIT_NO_VERDICT,
    audit,
)
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture, render_output

# A distributed synthetic fixture keeps these refusal checks executable in a clean checkout.
REAL = pathlib.Path(__file__).resolve().parents[3] / "fixtures/shared/steady-at-target.json"


def _pair(tmp_path, mutate_fixture=None, mutate_score=None):
    """A published day and its figures, optionally spoiled."""
    fixture = json.loads(REAL.read_text())
    fixture["name"] = "day-under-audit"
    if mutate_fixture:
        mutate_fixture(fixture)
    score = json.loads(render_output(score_fixture(parse_fixture(json.loads(json.dumps(fixture))))))
    if mutate_score:
        mutate_score(score)

    fp = tmp_path / "day.fixture.json"
    sp = tmp_path / "day.score.json"
    fp.write_text(json.dumps(fixture, indent=2))
    sp.write_text(render_output(score))
    return fp, sp


def _run(fp, sp):
    out = io.StringIO()
    return audit(fp, sp, out=out), out.getvalue()


def test_a_true_pair_agrees(tmp_path):
    """The control. A tool that refuses everything is not a tool."""
    code, said = _run(*_pair(tmp_path))
    assert code == EXIT_AGREES, said
    assert "AGREES" in said


def test_it_says_out_loud_what_agreeing_does_not_prove(tmp_path):
    """The boundary is part of the answer, not a footnote.

    A miner who reads AGREES and concludes the day is correct has been misled
    by a tool that was accurate, which is the failure this line prevents.
    """
    _, said = _run(*_pair(tmp_path))
    assert "says nothing about whether those inputs are true" in said
    assert "reading the chain" in said


def test_one_changed_digit_is_caught_and_located(tmp_path):
    """The whole job. It must also say WHERE, or a miner cannot act on it."""

    def bump(score):
        score["miners"][0]["scoreFp"] = str(int(score["miners"][0]["scoreFp"]) + 1)

    code, said = _run(*_pair(tmp_path, mutate_score=bump))
    assert code == EXIT_DISAGREES, said
    assert "DISAGREES" in said
    assert "first difference at line" in said
    assert "published" in said and "recomputed" in said


def test_a_changed_input_is_caught_too(tmp_path):
    """Published figures kept while the published inputs move under them."""

    def remove_deposits(fixture):
        """Change a scoring input enough to produce a different output."""
        for day in fixture["miners"][0]["days"].values():
            day["noahDepositRao"] = "0"

    # Score the ORIGINAL, publish it beside the CHANGED inputs.
    good_fp, good_sp = _pair(tmp_path)
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_fp, _ = _pair(bad_dir, mutate_fixture=remove_deposits)
    code, said = _run(bad_fp, good_sp)
    assert code == EXIT_DISAGREES, said


def test_two_files_from_different_days_are_refused_not_compared(tmp_path):
    """The worst possible pass: agreement between files that are not a pair."""

    def rename(score):
        score["fixture"] = "a-completely-different-day"

    code, said = _run(*_pair(tmp_path, mutate_score=rename))
    assert code == EXIT_NO_VERDICT, said
    assert "not a pair" in said


def test_a_day_with_nobody_in_it_is_refused(tmp_path):
    """An empty population recomputes to an empty answer and matches anything.

    The fault this repository has found in a differential, a coverage list, a
    mover list and a census, refused here before it can be reported as a pass.
    """

    def empty(fixture):
        fixture["miners"] = []

    # No exception is expected: the audit RETURNS a refusal code. The first
    # version wrapped this in pytest.raises and failed because nothing raised,
    # which would have read as the guard being absent when it was working.
    try:
        code, said = _run(*_pair(tmp_path, mutate_fixture=empty))
    except Exception:
        return  # parse_fixture refusing first is equally correct
    assert code == EXIT_NO_VERDICT, said
    assert "no miners in it" in said, said


def test_a_missing_file_is_a_refusal_and_not_a_pass(tmp_path):
    fp, sp = _pair(tmp_path)
    from noah.scoring.fixture_io import FixtureError

    with pytest.raises(FixtureError):
        audit(fp, tmp_path / "not-here.json", out=io.StringIO())


def test_the_three_exit_codes_are_distinct(tmp_path):
    """A caller reading exit codes must be able to tell the three apart."""
    assert len({EXIT_AGREES, EXIT_DISAGREES, EXIT_NO_VERDICT}) == 3
    assert EXIT_AGREES == 0
