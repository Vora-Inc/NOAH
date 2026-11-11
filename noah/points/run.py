"""Offline twin-checked candidate preview. Writes only the named output.

python -m noah.points.run fixture.json output.json
No wallet, RPC, signing, or deployment capability is imported.
"""

import json
import subprocess
import sys
from pathlib import Path

from .engine import evaluate


def typescript_text(raw):
    import tempfile

    script = Path(__file__).resolve().parents[2] / "verification/noah-ts/src/points.ts"
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "fixture.json"
        f.write_text(json.dumps(raw))
        r = subprocess.run(
            ["node", str(script), str(f)], capture_output=True, text=True, timeout=60
        )
    if r.returncode:
        raise ValueError("independent points engine failed: " + r.stderr[:500])
    return r.stdout


def agreed(raw):
    expected = evaluate(raw)
    if json.loads(typescript_text(raw)) != expected:
        raise ValueError("Python/TypeScript candidate disagreement")
    return expected


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m noah.points.run fixture.json output.json")
    result = agreed(json.loads(Path(sys.argv[1]).read_text()))
    # Exclusive create: never overwrite earlier evidence or an existing output.
    with Path(sys.argv[2]).open("x") as f:
        json.dump(result, f, sort_keys=True, indent=2)
        f.write("\n")
    print("PASS: exact Python/TypeScript agreement; offline preview only.")


if __name__ == "__main__":
    main()
