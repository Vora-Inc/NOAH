#!/usr/bin/env python3
"""Recompute a published scoring artifact from its input fixture.

Report differences instead of replacing the recorded result. The comparison
requires the original fixture and the same scoring contract and parameters."""

import json
import pathlib
import sys

from ..pathtext import plain_path_text
from .engine import score_fixture
from .fixture_io import FixtureError, parse_fixture, render_output

EXIT_AGREES = 0
EXIT_DISAGREES = 1
EXIT_NO_VERDICT = 2


def _load(path: pathlib.Path, what: str):
    if not path.exists():
        raise FixtureError(f"{what}: {plain_path_text(path)} does not exist. Nothing was compared.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as bad:
        raise FixtureError(
            f"{what}: {plain_path_text(path)} is not readable JSON ({type(bad).__name__})."
        ) from None


def first_difference(mine: str, theirs: str) -> str:
    """The first line where the two canonical renderings part company.

    A caller told only that two files differ has to diff them by hand to learn
    anything, and the published file may be large. The line number and both
    versions of it are what a miner needs to say what they disagree about.
    """
    a, b = mine.splitlines(), theirs.splitlines()
    for i in range(max(len(a), len(b))):
        line_a = a[i] if i < len(a) else "(published file ends here)"
        line_b = b[i] if i < len(b) else "(recomputed output ends here)"
        if line_a != line_b:
            return (
                f"  first difference at line {i + 1}\n"
                f"    published  : {line_b.strip()[:100]}\n"
                f"    recomputed : {line_a.strip()[:100]}"
            )
    return "  the two agree line by line but not byte for byte, so the difference is whitespace"


def audit(fixture_path: pathlib.Path, score_path: pathlib.Path, out=sys.stdout) -> int:
    data = _load(fixture_path, "the published inputs")
    published = _load(score_path, "the published figures")

    fx = parse_fixture(data)

    if not fx.miners:
        print("NO VERDICT: the published day has no miners in it. An empty population", file=out)
        print(
            "recomputes to an empty answer and matches any other empty answer, so a pass", file=out
        )
        print("here would mean nothing at all.", file=out)
        return EXIT_NO_VERDICT

    # A fixture and a score from different days compared cleanly would be the
    # worst possible pass: two files that agree because neither was the other's.
    named = published.get("fixture")
    if named is not None and named != fx.name:
        print("NO VERDICT: these two files are not a pair. The published figures were", file=out)
        print(f"computed from {named!r} and the published inputs are {fx.name!r}.", file=out)
        return EXIT_NO_VERDICT

    recomputed = render_output(score_fixture(fx))
    theirs = render_output(published) if isinstance(published, dict) else ""

    print(f"  day           {fx.name}, evaluation day {fx.eval_day}", file=out)
    print(f"  miners        {len(fx.miners)}", file=out)
    print("  engine        this tool's own, the one the differential proves", file=out)

    if recomputed == theirs:
        print("\nAGREES: the published figures follow exactly from the published inputs.", file=out)
        print(
            "This says nothing about whether those inputs are true. Checking that means", file=out
        )
        print("reading the chain.", file=out)
        return EXIT_AGREES

    print("\nDISAGREES: recomputing the published inputs does not produce the published", file=out)
    print("figures. One of them is wrong and this cannot say which.\n", file=out)
    print(first_difference(recomputed, theirs), file=out)
    return EXIT_DISAGREES


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(
            "usage: python3 -m noah.scoring.audit_published "
            "<published.fixture.json> <published.score.json>",
            file=sys.stderr,
        )
        return EXIT_NO_VERDICT
    try:
        return audit(pathlib.Path(argv[0]), pathlib.Path(argv[1]))
    except FixtureError as refusal:
        print(f"NO VERDICT: {refusal}", file=sys.stderr)
        return EXIT_NO_VERDICT
    except OSError as failure:
        # The OS error's text repeats the typed path.
        print(f"NO VERDICT: could not read an input ({type(failure).__name__})", file=sys.stderr)
        return EXIT_NO_VERDICT


if __name__ == "__main__":
    sys.exit(main())
