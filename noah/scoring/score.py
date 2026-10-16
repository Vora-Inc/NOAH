"""The score.

    score(i, e) = floor( floor( base * streak_fp / 1e9 ) * slot_fp / 1e9 ) * gate

Integer arithmetic, floor at every division, in the order written. ``base`` is
the decayed running credit ``N + F``, in rao. The gate is the door: 1 if the
coldkey holds the ongoing requirement at the read height, else 0. It is a door
and not a dial, so holding more stake never raises a score. ``slot_fp`` is
exactly 1 at version one. See ``docs/architecture/SCORING_SOURCES.md`` for
the current source map.
"""

from __future__ import annotations

from .constants import FP_SCALE


def gate(hold_rao: int, ongoing_hold_rao: int) -> int:
    """The door check: 1 while the hold is met, 0 while it is short."""
    return 1 if hold_rao >= ongoing_hold_rao else 0


def score(base_rao: int, streak_fp: int, slot_fp: int, gate_value: int) -> int:
    """Floor at every division, in the order written."""
    return (base_rao * streak_fp // FP_SCALE) * slot_fp // FP_SCALE * gate_value
