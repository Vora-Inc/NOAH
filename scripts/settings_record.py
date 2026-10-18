"""Persist the settings used to calculate a day's score.

The ``day-N.settings.json`` companion distinguishes configuration from the
``day-N.json`` score output. Readers should match exact filenames rather than
treating every JSON file in the score directory as a score.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

SCHEMA = "noah-day-settings/1"
MARKER_NAME = ".deployed-commit"


def _locate_code_root(start=None):
    """Locate the code root by walking up from this file. Prefer an explicit
    deployed-commit marker, then a Git checkout, then a directory holding
    both the noah package and scripts. Return None when no root is found."""
    here = Path(start if start is not None else __file__).resolve().parent
    ancestors = [here, *here.parents]
    for ancestor in ancestors:
        if (ancestor / MARKER_NAME).is_file() or (ancestor / "services" / "noah" / MARKER_NAME).is_file():
            return ancestor
    for ancestor in ancestors:
        if (ancestor / ".git").exists():
            return ancestor
    for ancestor in ancestors:
        if (ancestor / "noah").is_dir() and (ancestor / "scripts").is_dir():
            return ancestor
    return None


_CODE_ROOT = _locate_code_root()


def _root_or_refuse(code_root):
    if code_root is not None:
        return Path(code_root)
    if _CODE_ROOT is None:
        raise RuntimeError(
            f"cannot tell where the code root is: no ancestor of {__file__} holds "
            f"{MARKER_NAME}, a .git directory, or a noah package beside scripts. "
            f"Refusing to report the deployed commit as absent."
        )
    return _CODE_ROOT


def settings_record_path(day_evidence_path) -> Path:
    """``…/day-N.json`` -> ``…/day-N.settings.json`` (same directory)."""
    p = Path(day_evidence_path)
    return p.with_name(p.stem + ".settings.json")


def _jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Fraction):
        return int(value) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def read_deployed_marker(code_root=None):
    """Read the deployment marker from the supported source layouts.
    Return its path, commit and deployment timestamp, or "absent" when unreadable."""
    root = _root_or_refuse(code_root)
    for candidate in (root / MARKER_NAME, root / "services" / "noah" / MARKER_NAME):
        if not candidate.is_file():
            continue
        try:
            fields = {}
            for line in candidate.read_text(encoding="utf-8").splitlines():
                key, sep, val = line.partition("=")
                if sep:
                    fields[key.strip()] = val
            return {
                "path": str(candidate),
                "commit": fields.get("commit", "absent"),
                "deployed_at": fields.get("deployed_at", "absent"),
            }
        except OSError:
            return "absent"
    return "absent"


def _code_version(code_root: Path, marker) -> str:
    if isinstance(marker, dict) and marker.get("commit") not in (None, "absent"):
        return marker["commit"]
    try:
        out = subprocess.run(
            ["git", "-C", str(code_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def build_settings_record(eval_day, *, dials=None, env=None, code_root=None, now=None) -> dict:
    """Assemble the record. Raises on a bad/missing profile; callers use
    :func:`record_day_settings`, which does not.

    ``dials`` is the scoring-dials object the engine actually used (the
    adapter's ``Dials``); the profile is read ONCE -- the same bytes are
    hashed and parsed, so the hash always describes what was parsed.
    """
    from noah.scoring.parameters import ENV_VAR, ParametersError, parse_parameters

    mapping = os.environ if env is None else env
    path = mapping.get(ENV_VAR, "").strip()
    if not path:
        raise ParametersError(f"{ENV_VAR} is not set; no profile to record")
    from noah.pathtext import plain_path_text

    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as failure:
        # The OSError text repeats the operator-typed path, which may be a keyed URL by mistake.
        raise ParametersError(
            f"{ENV_VAR} names {plain_path_text(path)}, which could not be read "
            f"({type(failure).__name__})"
        ) from None
    parameters = parse_parameters(
        json.loads(raw_bytes.decode("utf-8")), where=f"{ENV_VAR} ({plain_path_text(path)})"
    )
    root = _root_or_refuse(code_root)
    marker = read_deployed_marker(root)
    when = now or datetime.now(timezone.utc)
    return {
        "schema": SCHEMA,
        "evalDay": str(eval_day),
        "recorded_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile_path": path,
        "profile_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "profile": _jsonable(parameters),
        "scoring_dials": _jsonable(dials) if dials is not None else "unavailable",
        "deployed_commit": marker,
        "code_version": _code_version(root, marker),
    }


def _comparable(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "recorded_at"}


def record_day_settings(
    eval_day, day_evidence_path, *, dials=None, on_conflict="overwrite",
    env=None, code_root=None, now=None,
) -> dict:
    """Write the settings record beside ``day_evidence_path``. Never raises.

    Returns ``{"action": "written"|"overwritten"|"unchanged"|"conflict-refused"|"failed",
    "path": str|None, "error": str|None}``; the caller prints it.
    """
    from noah.pathtext import plain_path_text

    path = None
    try:
        if on_conflict not in ("overwrite", "refuse"):
            raise ValueError(f"on_conflict must be 'overwrite' or 'refuse', got {on_conflict!r}")
        path = settings_record_path(day_evidence_path)
        record = build_settings_record(
            eval_day, dials=dials, env=env, code_root=code_root, now=now
        )
        text = json.dumps(record, indent=2, sort_keys=True) + "\n"
        action = "written"
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = None
            if isinstance(old, dict) and _comparable(old) == _comparable(record):
                return {"action": "unchanged", "path": str(path), "error": None}
            if on_conflict == "refuse":
                return {
                    "action": "conflict-refused",
                    "path": str(path),
                    "error": f"settings record for day {eval_day} at {plain_path_text(path)} "
                    "exists and differs; left untouched",
                }
            print(
                f"OVERWRITING SETTINGS RECORD for day {eval_day} at {plain_path_text(path)}: the "
                f"existing "
                "record differs from the settings this run was scored under."
            )
            action = "overwritten"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return {"action": action, "path": str(path), "error": None}
    except Exception as failure:  # noqa: BLE001 -- must never block scoring or publishing
        from noah.chain.sources import scrub_endpoint_text

        return {
            "action": "failed",
            "path": str(path) if path else None,
            "error": f"{type(failure).__name__}: {scrub_endpoint_text(failure)}",
        }
