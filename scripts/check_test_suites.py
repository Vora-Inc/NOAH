#!/usr/bin/env python3
"""Validate the declared test suites before the runner starts pytest."""

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
OMISSIONS_FILE = ".test-suite-omissions.json"
PUBLIC_ONLY_OMISSIONS = {"noah/ark/tests", "tests"}


def included_suites(root: Path, suites: list[str]) -> list[str]:
    if not suites or len(suites) != len(set(suites)):
        raise ValueError("test suite list is empty or contains duplicates")
    omitted = []
    manifest = root / OMISSIONS_FILE
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"schema_version", "omitted"}:
            raise ValueError("invalid test-suite omissions manifest")
        omitted = data["omitted"]
        if (
            data["schema_version"] != 1
            or not isinstance(omitted, list)
            or any(not isinstance(item, str) for item in omitted)
            or len(omitted) != len(set(omitted))
            or not set(omitted) <= PUBLIC_ONLY_OMISSIONS
            or not set(omitted) <= set(suites)
        ):
            raise ValueError("invalid or stale test-suite omissions manifest")
    included = []
    for suite in suites:
        relative = PurePosixPath(suite)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in suite.split("/")):
            raise ValueError(f"invalid test suite path: {suite!r}")
        path = root / suite
        if suite in omitted:
            if path.exists():
                raise ValueError(f"suite declared omitted is present: {suite}")
            continue
        if not path.exists():
            raise ValueError(f"required test suite is missing: {suite}")
        tests = [path] if path.is_file() else list(path.rglob("test_*.py"))
        if not any(test.is_file() and test.match("test_*.py") for test in tests):
            raise ValueError(f"required test suite contains no test files: {suite}")
        included.append(suite)
    if not included:
        raise ValueError("no test suites remain")
    return included


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("suites", nargs="+")
    args = parser.parse_args()
    try:
        included = included_suites(args.root, args.suites)
    except (OSError, ValueError) as error:
        print(f"FAIL: test suite inventory: {error}", file=sys.stderr)
        return 1
    for suite in args.suites:
        if suite not in included:
            print(f"Declared omitted from this distribution: {suite}", file=sys.stderr)
    print("\n".join(included))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
