"""Running the real TypeScript scoring engine as a subprocess, inline.

`scripts/twin-proof.sh` already does exactly this, fixture file by fixture
file, for the offline corpus differential: `node src/cli.ts run-fixture
<input> <outdir>` in `verification/noah-ts/`, then read the written
`<name>.score.json` back. This is the same two steps, wired for a caller
(`noah.core.chain_adapter.ChainAdapter.agree`) that needs one fixture's real
second opinion inline rather than a whole corpus scored to disk and diffed
by a shell script.

Nothing here re-implements the TypeScript engine and nothing here
re-implements the comparison either -- that stays
`noah.core.twin.require_agreement`'s job. This module's only responsibility
is handing it real, unmodified bytes from the real second implementation.
"""

import json
import subprocess
import tempfile
from pathlib import Path

NOAH_TS_DIR = Path(__file__).resolve().parents[2] / "verification" / "noah-ts"


class TypeScriptEngineFailed(Exception):
    """The real TypeScript engine could not be run, or refused the fixture."""


def render_with_ts_engine(fixture_dict, *, ts_dir=None, timeout=60):
    """Score one ``noah-fixture/6`` object with the real TypeScript CLI and
    return its own canonical output text, unmodified.

    ``fixture_dict`` is exactly what ``noah.chain.fixture_build.build_fixture``
    returns -- the same raw dict the Python side hands to
    ``noah.scoring.fixture_io.parse_fixture``. It is written to a temp file
    for the CLI to read and validate independently: nothing here trusts the
    Python side's own parse, which is the entire point of the twin proof.

    The returned text is the CLI's own file content, already canonical
    (sorted keys, two-space indent, trailing newline -- see
    ``verification/noah-ts/src/cli.ts``'s own docstring), not re-serialized
    here.
    """
    ts_dir = Path(ts_dir) if ts_dir is not None else NOAH_TS_DIR
    name = fixture_dict.get("name")
    if not name:
        raise TypeScriptEngineFailed(
            f"the fixture has no usable 'name' field ({fixture_dict.get('name')!r}), and the "
            f"TypeScript CLI writes its output as <name>.score.json -- nothing can be run."
        )

    with tempfile.TemporaryDirectory() as tmp:
        input_path = Path(tmp) / "fixture.json"
        input_path.write_text(json.dumps(fixture_dict))
        out_dir = Path(tmp) / "out"

        try:
            result = subprocess.run(
                ["node", "src/cli.ts", "run-fixture", str(input_path), str(out_dir)],
                cwd=str(ts_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TypeScriptEngineFailed(
                f"could not run the TypeScript engine ({type(error).__name__}: {error}). "
                f"This validator publishes nothing without a real second opinion; there is "
                f"no fallback to the Python engine alone."
            ) from error

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise TypeScriptEngineFailed(
                f"the TypeScript engine refused fixture {name!r} (exit {result.returncode}): "
                f"{detail or 'no output on stdout or stderr'}"
            )

        out_path = out_dir / f"{name}.score.json"
        try:
            return out_path.read_text()
        except OSError as error:
            raise TypeScriptEngineFailed(
                f"the TypeScript engine exited 0 for fixture {name!r} but {out_path} could "
                f"not be read back ({type(error).__name__}: {error})."
            ) from error
