#!/usr/bin/env python3
"""Compare production scoring with the independent hand implementation.

Both paths receive the same raw fixture. A validity disagreement is refused;
output disagreement is reported without choosing one side as correct. Empty
miner sets do not provide scoring evidence. Exit codes: 0 agreement, 1 mismatch,
2 no evidence or usage error, 3 refused comparison."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import score_by_hand  # noqa: E402  (sibling module, path extended above)

from noah.scoring.engine import score_fixture  # noqa: E402
from noah.scoring.fixture_io import FixtureError, parse_fixture  # noqa: E402


class CrossCheckRefusal(Exception):
    """One path refused where the other produced an answer -- reported as
    its own outcome, never silently treated as agreement or disagreement."""


def compare_fixture(raw_fixture: dict) -> dict:
    """Run ``raw_fixture`` through both paths and report.

    ``raw_fixture`` is a plain dict, never read from a file here -- the
    exact shape ``ChainAdapter.read()`` returns (and the exact shape every
    fixture under ``fixtures/shared/`` is, once loaded). Never mutates its
    argument.

    Returns ``{"agree": bool, "real_output": dict, "hand_output": dict,
    "fixture_name": str | None, "miner_count": int}`` on a genuine
    comparison. Raises :class:`CrossCheckRefusal` if one path refused
    (``FixtureError``) while the other did not -- real, reviewed code on
    both sides can refuse a malformed fixture; if only one side does, that
    disagreement is itself the finding, and it must not be swallowed into
    a bare "did not compare" result.
    """
    real_error = hand_error = None
    real_output = hand_output = None

    try:
        real_output = score_fixture(parse_fixture(raw_fixture))
    except FixtureError as error:
        real_error = error

    try:
        hand_output = score_by_hand.score_fixture_by_hand(raw_fixture)
    except Exception as error:  # score_by_hand raises plain KeyError/ValueError, never FixtureError
        hand_error = error

    if (real_error is None) != (hand_error is None):
        raise CrossCheckRefusal(
            f"the two paths disagree about whether this fixture is even valid input: "
            f"real reader {'refused: ' + str(real_error) if real_error else 'accepted it'}; "
            f"hand path {'refused: ' + str(hand_error) if hand_error else 'accepted it'}. "
            f"Neither side is trusted over the other here -- this is itself the finding."
        )
    if real_error is not None:
        raise CrossCheckRefusal(
            f"both paths refused this fixture (real reader: {real_error}; "
            f"hand path: {hand_error}), so there is nothing to compare."
        )

    return {
        "agree": real_output == hand_output,
        "real_output": real_output,
        "hand_output": hand_output,
        "fixture_name": raw_fixture.get("name"),
        "miner_count": len(raw_fixture.get("miners", [])),
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 compare_score.py <fixture.json>", file=sys.stderr)
        return 2
    with open(argv[0], "r", encoding="utf-8") as fh:
        raw_fixture = json.load(fh)

    try:
        report = compare_fixture(raw_fixture)
    except CrossCheckRefusal as refusal:
        print(f"REFUSED: {refusal}")
        return 3

    print(f"fixture       {report['fixture_name']}")
    print(f"miners        {report['miner_count']}")

    if report["miner_count"] == 0:
        print("\nNO EVIDENCE. This fixture names no miners, so the two paths agree about nothing.")
        return 2

    if report["agree"]:
        print("\nAGREE, byte for byte.")
        return 0

    print("\nDISAGREE. This is the finding, not a bug to chase quietly.")
    real_by_coldkey = {m["coldkey"]: m for m in report["real_output"]["miners"]}
    hand_by_coldkey = {m["coldkey"]: m for m in report["hand_output"]["miners"]}
    for coldkey in sorted(set(real_by_coldkey) | set(hand_by_coldkey)):
        real_row = real_by_coldkey.get(coldkey)
        hand_row = hand_by_coldkey.get(coldkey)
        if real_row != hand_row:
            print(f"  {coldkey}:")
            print(f"    real: {json.dumps(real_row, sort_keys=True)}")
            print(f"    hand: {json.dumps(hand_row, sort_keys=True)}")
    if report["real_output"]["weightsU16"] != report["hand_output"]["weightsU16"]:
        print(
            f"  weightsU16 real: {json.dumps(report['real_output']['weightsU16'], sort_keys=True)}"
        )
        print(
            f"  weightsU16 hand: {json.dumps(report['hand_output']['weightsU16'], sort_keys=True)}"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
