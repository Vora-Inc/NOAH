#!/usr/bin/env python3
"""Verify one per-day verification bundle (#386). Run by anyone, no chain needed.

    python3 scripts/verify_bundle.py <bundle-dir>

Needs only the Python standard library plus this repository's own
``noah.scoring.audit_published``. Run from the repository root with
``PYTHONPATH=.``. Four checks, in order:

  1. the directory holds exactly the expected files, nothing else;
  2. (optional ``score-published.json``, identity keys removed, equals ``score.json``)
     ``SHA256SUMS`` and ``MANIFEST.json`` agree with the file bytes (and, if the
     bundle sits in a ``records/<network>/`` tree with an ``INDEX.json``, the
     index lists this manifest's hash);
  3. the secret scan finds nothing;
  4. ``audit_published`` recomputes the score from ``fixture.json`` and it
     matches ``score.json`` byte for byte.

Exit codes: 0 PASS, 1 FAIL (a check failed), 2 NO VERDICT (usage error or the
audit code could not be imported).

What PASS means: the published score follows from the published input and the
files are the ones the manifest names. It does not say the input is true to the
chain; ``chain-refs.json`` says where to check that.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
from contextlib import contextmanager
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from noah import secret_scan  # noqa: E402

EXIT_PASS, EXIT_FAIL, EXIT_NO_VERDICT = 0, 1, 2
_CANNOT_RUN = -1
BUNDLE_SCHEMA = "noah-day-bundle/1"
DATA_FILES = ("fixture.json", "score.json", "settings.json", "chain-refs.json", "README.txt")
PUBLISHED = "score-published.json"  # optional: the canonical score file as the validator wrote it
IDENTITY_KEYS = ("netuid", "ark_address", "genesis_hash")
MANIFEST = "MANIFEST.json"
SUMS = "SHA256SUMS"
EXPECTED = set(DATA_FILES) | {MANIFEST, SUMS}
# Match the secret scanner's per-file limit; at most eight bundle files + index.
MAX_FILE_BYTES = 32 * 1024 * 1024


def read_regular(path: Path, *, directory_fd=None) -> bytes:
    """Bounded, nonblocking, no-follow read; never read a FIFO/device/symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError("bundle input must be a bounded regular file")
        data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("bundle input exceeds size limit")
        return data


@contextmanager
def snapshot_bundle(bundle: Path):
    """Verify private copies so later parsing/audit cannot race source swaps."""
    fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names = set(os.listdir(fd))
        expected = EXPECTED | ({PUBLISHED} if PUBLISHED in names else set())
        if names != expected:
            raise ValueError("bundle file set differs from the expected names")
        with tempfile.TemporaryDirectory(prefix="noah-verify-") as temporary:
            snapshot = Path(temporary) / "bundle"
            snapshot.mkdir()
            for name in sorted(names):
                (snapshot / name).write_bytes(read_regular(name, directory_fd=fd))
            parent_fd = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            try:
                try:
                    index = read_regular("INDEX.json", directory_fd=parent_fd)
                except FileNotFoundError:
                    pass
                else:
                    (snapshot.parent / "INDEX.json").write_bytes(index)
            finally:
                os.close(parent_fd)
            yield snapshot
    finally:
        os.close(fd)


def read_text(path: Path) -> str:
    return read_regular(path).decode("utf-8")


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON object key")
        result[name] = value
    return result


def read_json(path: Path):
    return json.loads(read_text(path), object_pairs_hook=unique_object)


def data_files(bundle: Path) -> tuple:
    """The bundle's data files: the fixed five, plus the published score if present."""
    return DATA_FILES + ((PUBLISHED,) if (bundle / PUBLISHED).is_file() else ())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(read_regular(path)).hexdigest()


def parse_sums(text: str) -> dict:
    """``sha256sum`` format: ``<hex>  <name>`` per line."""
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if (not sep or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or name in out):
            raise ValueError("malformed or duplicate SHA256SUMS entry")
        out[name] = digest
    return out


def check_files(bundle: Path) -> list:
    problems = []
    present = {p.name for p in bundle.iterdir()}
    expected = EXPECTED | ({PUBLISHED} if PUBLISHED in present else set())
    for name in sorted(expected - present):
        problems.append(f"missing file: {name}")
    for name in sorted(present - expected):
        problems.append(f"unexpected file: {name}")
    return problems


def check_hashes(bundle: Path) -> list:
    problems = []
    try:
        sums = parse_sums(read_text(bundle / SUMS))
        manifest = read_json(bundle / MANIFEST)
    except (OSError, ValueError) as why:
        return [f"cannot read {SUMS} or {MANIFEST}: {why}"]
    if not isinstance(manifest, dict):
        return [f"{MANIFEST}: expected an object"]
    if manifest.get("schema") != BUNDLE_SCHEMA:
        problems.append(f"{MANIFEST}: schema is {manifest.get('schema')!r}, expected {BUNDLE_SCHEMA!r}")
    files = data_files(bundle)
    want_in_sums = (set(files) | {MANIFEST})
    if set(sums) != want_in_sums:
        return [f"{SUMS}: unexpected, missing, or invalid file names"]
    entries = manifest.get("files")
    if (not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries)
            or any(not isinstance(e.get("name"), str) for e in entries)):
        return [f"{MANIFEST}: invalid file list"]
    listed = {e["name"]: e for e in entries}
    if len(listed) != len(entries) or set(listed) != set(files):
        return [f"{MANIFEST}: unexpected, duplicate, missing, or invalid file names"]
    for name, digest in sums.items():
        if sha256_file(bundle / name) != digest:
            problems.append(f"{SUMS}: hash mismatch for {name}")
    has_pub = PUBLISHED in files
    if has_pub == (manifest.get("published_score") == "not recorded"):
        problems.append(f"{MANIFEST}: published_score field disagrees with whether {PUBLISHED} is present")
    for name, entry in listed.items():
        f = bundle / name
        if not f.is_file():
            continue
        if entry.get("sha256") != sha256_file(f):
            problems.append(f"{MANIFEST}: hash mismatch for {name}")
        if entry.get("size") != f.stat().st_size:
            problems.append(f"{MANIFEST}: size mismatch for {name}")
    for name in files:
        if name in sums and name in listed and sums[name] != listed[name].get("sha256"):
            problems.append(f"{name}: {SUMS} and {MANIFEST} disagree")
    # Day consistency across the files a stranger reads.
    try:
        fx = read_json(bundle / "fixture.json")
        sc = read_json(bundle / "score.json")
        if str(manifest.get("eval_day")) != str(fx.get("evalDay")) or str(fx.get("evalDay")) != str(sc.get("evalDay")):
            problems.append(
                f"eval day differs: manifest {manifest.get('eval_day')!r}, fixture {fx.get('evalDay')!r}, score {sc.get('evalDay')!r}"
            )
    except (OSError, ValueError) as why:
        problems.append(f"cannot compare eval days: {why}")
    if has_pub:
        problems += check_published(bundle)
    index = bundle.resolve().parent / "INDEX.json"
    if index.is_file():
        try:
            days = read_json(index).get("days", [])
            mine = [d for d in days if str(d.get("eval_day")) == str(manifest.get("eval_day"))]
            actual = sha256_file(bundle / MANIFEST)
            if not mine:
                problems.append(f"INDEX.json has no entry for day {manifest.get('eval_day')}")
            elif mine[0].get("manifest_sha256") != actual:
                problems.append("INDEX.json manifest_sha256 does not match this MANIFEST.json")
        except (OSError, ValueError) as why:
            problems.append(f"cannot read INDEX.json: {why}")
    return problems


def check_published(bundle: Path) -> list:
    """The published canonical score, minus the chain-identity keys the validator
    added after scoring, must equal score.json byte for byte when rendered the
    canonical way."""
    try:
        from noah.scoring.fixture_io import render_output
    except ImportError as why:
        return [f"cannot check {PUBLISHED}: audit code not importable ({why})"]
    try:
        pub = read_json(bundle / PUBLISHED)
        mine = read_text(bundle / "score.json")
    except (OSError, ValueError) as why:
        return [f"cannot read {PUBLISHED} or score.json: {why}"]
    if not isinstance(pub, dict):
        return [f"{PUBLISHED} is not a JSON object"]
    for k in IDENTITY_KEYS:
        pub.pop(k, None)
    if render_output(pub) != mine:
        return [f"{PUBLISHED} (identity keys removed) differs from score.json"]
    return []


def run_audit(bundle: Path, out) -> int:
    try:
        from noah.scoring.audit_published import audit
        from noah.scoring.fixture_io import FixtureError
    except ImportError as why:
        print(f"NO VERDICT: cannot import the repository's audit code ({why}).", file=out)
        print("Run from the repository root with PYTHONPATH=. to import noah.", file=out)
        return _CANNOT_RUN
    try:
        return audit(bundle / "fixture.json", bundle / "score.json", out=out)
    except FixtureError as refusal:
        print(f"NO VERDICT: {refusal}", file=out)
        return 2  # the audit refused this input: a bundle failure, not a tooling gap


def verify(bundle, out=sys.stdout) -> int:
    bundle = Path(bundle)
    if not bundle.exists():
        print("NO VERDICT: the given bundle is not a directory.", file=out)
        return EXIT_NO_VERDICT
    try:
        with snapshot_bundle(bundle) as snapshot:
            return verify_snapshot(snapshot, out)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        # Do not echo attacker-controlled path names or file contents.
        print("FAIL: unsafe, oversized, unreadable, or malformed bundle input.", file=out)
        return EXIT_FAIL


def verify_snapshot(bundle, out=sys.stdout) -> int:
    bundle = Path(bundle)
    if not bundle.is_dir():
        print("NO VERDICT: the given bundle is not a directory.", file=out)
        return EXIT_NO_VERDICT
    failures = []

    problems = check_files(bundle)
    print(f"[1/4] file set        {'ok' if not problems else 'FAILED'}", file=out)
    failures += problems
    if problems:
        for p in problems:
            print(f"      - {p}", file=out)
        return _finish(failures, out)

    problems = check_hashes(bundle)
    print(f"[2/4] hashes+manifest {'ok' if not problems else 'FAILED'}", file=out)
    for p in problems:
        print(f"      - {p}", file=out)
    failures += problems

    hits = secret_scan.scan_paths([bundle])
    print(f"[3/4] secret scan     {'ok' if not hits else 'FAILED'}", file=out)
    for f in hits:
        print(f"      - {f}", file=out)  # path:line: kind, never the matched text
    failures += [f"secret scan: {f}" for f in hits]

    print("[4/4] audit (recompute the score from the input):", file=out)
    audit_out = io.StringIO()
    code = run_audit(bundle, audit_out)
    for line in audit_out.getvalue().splitlines():
        print(f"      {line}", file=out)
    if code == _CANNOT_RUN and not failures:
        print("\nNO VERDICT: the audit could not run.", file=out)
        return EXIT_NO_VERDICT
    if code != 0:
        failures.append("audit did not agree")
    return _finish(failures, out)


def _finish(failures, out) -> int:
    if failures:
        print(f"\nFAIL: {len(failures)} problem(s).", file=out)
        return EXIT_FAIL
    print("\nPASS: files are the ones the manifest names, no secrets, and the score recomputes from the input.", file=out)
    return EXIT_PASS


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 scripts/verify_bundle.py <bundle-dir>", file=sys.stderr)
        return EXIT_NO_VERDICT
    return verify(argv[0])


if __name__ == "__main__":
    sys.exit(main())
