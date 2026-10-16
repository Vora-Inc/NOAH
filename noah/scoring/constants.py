"""The fixed-point convention.

Every fractional quantity is an unsigned integer scaled by ``FP_SCALE = 1e9``,
the same scale ``noah.standard.multiplier`` uses. The only rounding anywhere is
floor, applied at every division in the current scoring implementation.

Deliberately, no dial values live here. Target, harshness, cap, half-life,
streak ceiling and ramp, and the ongoing holding requirement all arrive as
fixture data, because every subnet chooses its own and code must never restate
one.
"""

from __future__ import annotations

FP_SCALE: int = 1_000_000_000

# Slot factor at version one: exactly 1. Reserved for the attestation job,
# which is not designed yet and changes nothing until it is.
SLOT_FP: int = FP_SCALE
