#!/usr/bin/env python3
"""Check that every bundled scoring input has a current saved answer."""

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noah.scoring.audit_published import audit  # noqa: E402
from noah.scoring.fixture_io import FixtureError  # noqa: E402


def check(directory: Path, out=sys.stdout) -> int:
    fixtures = sorted(directory.glob("*.json"))
    if not fixtures:
        print(f"FAIL: no scoring examples found in {directory}", file=out)
        return 1
    failures = 0
    for fixture in fixtures:
        output = directory / "out-python" / f"{fixture.stem}.score.json"
        detail = io.StringIO()
        try:
            result = audit(fixture, output, out=detail)
        except (FixtureError, OSError, ValueError) as error:
            result = 2
            detail.write(str(error))
        if result:
            failures += 1
            print(f"FAIL: {fixture.name}\n{detail.getvalue().strip()}", file=out)
    if failures:
        print(f"FAIL: {failures} of {len(fixtures)} saved examples need attention.", file=out)
        return 1
    print(f"PASS: {len(fixtures)} saved examples agree.", file=out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=ROOT / "fixtures/shared")
    return check(parser.parse_args().fixtures)


if __name__ == "__main__":
    raise SystemExit(main())
