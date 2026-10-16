"""Reference table for deterministic half-life decay.

Generate factors using the scoring contract's integer arithmetic. Validate table
identity and requested parameters before using a recorded factor."""

from __future__ import annotations

from .constants import FP_SCALE
from .decay import integer_nth_root

_HALF_LIFE_DAYS = 182

#: The last age this table carries. Ages beyond this decay to 0, per the
#: closed form itself (2^(-a/182) underflows to 0 once a is large enough
#: that floor(... * 1e9) rounds to 0) -- not stored, since the table's own
#: docstring already states the tail is 0 and a caller indexing past the
#: end is a caller bug this module need not carry padding to hide.
_MAX_AGE = 5441

DECAY_FACTOR: tuple[int, ...] = tuple(
    integer_nth_root(FP_SCALE**_HALF_LIFE_DAYS // (2**age), _HALF_LIFE_DAYS)
    for age in range(_MAX_AGE + 1)
)
