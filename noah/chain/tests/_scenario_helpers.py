"""Shared synthetic inputs for public participant tests."""

import json
from pathlib import Path

_REPO_ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "docs/parameters/profiles/m1-revised-v1.json").is_file()
)
_PROFILE = json.loads(
    (_REPO_ROOT / "docs" / "parameters" / "profiles" / "m1-revised-v1.json").read_text()
)
DIALS = {
    k: _PROFILE[k]
    for k in (
        "targetFp",
        "harshness",
        "capFp",
        "halfLifeDays",
        "streakCeilingFp",
        "streakRampDays",
        "ongoingHoldRao",
    )
}
N = 10**9
PAY = 100 * N
ENTRY = int(_PROFILE["entryDepositPerHotkeyRao"])

__all__ = ["DIALS", "ENTRY", "PAY"]
