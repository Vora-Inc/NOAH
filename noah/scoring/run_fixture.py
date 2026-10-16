"""Fixture runner: the entry point the differential test drives.

    python3 -m noah.scoring.run_fixture <input.json> <outdir>

Reads one ``noah-fixture/6`` file, validates it strictly (an unknown field is
an error), scores it, and writes ``<outdir>/<name>.score.json`` in the
canonical serialization implemented by ``render_output``: sorted keys, two-space
indent, trailing newline. The TypeScript implementation writes its own outputs
independently, and the differential compares the two byte for byte. See
``docs/architecture/SCORING_SOURCES.md`` for the current source map.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..pathtext import plain_path_text
from .engine import score_fixture
from .fixture_io import FixtureError, parse_fixture, render_output


def run(input_path: Path, outdir: Path) -> Path:
    with open(input_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    fx = parse_fixture(data)
    output = score_fixture(fx)
    outdir.mkdir(parents=True, exist_ok=True)
    out_path = outdir / f"{fx.name}.score.json"
    out_path.write_text(render_output(output), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python3 -m noah.scoring.run_fixture <input.json> <outdir>", file=sys.stderr)
        return 2
    try:
        out_path = run(Path(argv[0]), Path(argv[1]))
    except (FixtureError, json.JSONDecodeError, OSError) as exc:
        # A stable, machine-readable "refused[CODE]:" prefix when the
        # refusal carries one (FixtureError.code -- see that class's own
        # docstring): the twin proof's own refusal-fixture check greps for
        # this exact prefix from both implementations and compares the code,
        # never the free-text message that follows it. A refusal with no
        # code (json.JSONDecodeError, OSError, or a FixtureError raised
        # without one) prints the ordinary "error:" form unchanged.
        code = getattr(exc, "code", None)
        if code:
            print(f"refused[{code}]: {exc}", file=sys.stderr)
        elif isinstance(exc, OSError):
            # An OS error's text repeats the typed path.
            print(f"error: could not read or write a file ({type(exc).__name__})", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    print(plain_path_text(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
