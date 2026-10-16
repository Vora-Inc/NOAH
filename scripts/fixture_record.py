"""Persist the exact input fixture used to calculate a day's score.

The ``day-N.fixture.json`` name distinguishes inputs from score outputs.
The default overwrite policy reports and replaces conflicting evidence; the
refuse policy preserves it. Capturing inputs supports later independent score
reproduction.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def fixture_record_path(day_evidence_path) -> Path:
    """``.../day-N.json`` -> ``.../day-N.fixture.json`` (same directory)."""
    p = Path(day_evidence_path)
    return p.with_name(p.stem + ".fixture.json")


def render_fixture(fixture: dict) -> str:
    """Canonical text: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(fixture, sort_keys=True, indent=2) + "\n"


def record_day_fixture(eval_day, day_evidence_path, fixture, *, on_conflict="overwrite") -> dict:
    """Write ``fixture`` beside ``day_evidence_path``. Never raises.

    Returns ``{"action": "written"|"overwritten"|"unchanged"|"conflict-refused"|"failed",
    "path": str|None, "error": str|None}``; the caller prints it.
    """
    from noah.pathtext import plain_path_text

    path = None
    try:
        if on_conflict not in ("overwrite", "refuse"):
            raise ValueError(f"on_conflict must be 'overwrite' or 'refuse', got {on_conflict!r}")
        if not isinstance(fixture, dict):
            raise TypeError("no fixture object was available from the scoring run")
        if str(fixture.get("evalDay")) != str(eval_day):
            raise ValueError(
                f"fixture evalDay {fixture.get('evalDay')!r} does not match the scored day {eval_day!r}"
            )
        path = fixture_record_path(day_evidence_path)
        text = render_fixture(fixture)
        action = "written"
        if path.exists():
            if path.read_text(encoding="utf-8") == text:
                return {"action": "unchanged", "path": str(path), "error": None}
            if on_conflict == "refuse":
                return {
                    "action": "conflict-refused",
                    "path": str(path),
                    "error": f"fixture record for day {eval_day} at {plain_path_text(path)} "
                    "exists and differs; left untouched",
                }
            print(
                f"OVERWRITING FIXTURE RECORD for day {eval_day} at {plain_path_text(path)}: the "
                f"existing "
                "record differs from the input this run scored."
            )
            action = "overwritten"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return {"action": action, "path": str(path), "error": None}
    except Exception as failure:  # noqa: BLE001 -- must never block scoring or publishing
        return {
            "action": "failed",
            "path": str(path) if path else None,
            "error": f"{type(failure).__name__}: {failure}",
        }
