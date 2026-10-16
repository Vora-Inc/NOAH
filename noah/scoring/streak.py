"""Classify participation days and advance the contribution streak.

A positive prior-day obligation is a hit when covered and otherwise a miss.
A no-fault day without spendable earnings is neutral; neutral days preserve the
existing run. The streak multiplier is bounded by its configured ceiling."""

from __future__ import annotations

from .constants import FP_SCALE

HIT = "hit"
MISS = "miss"
NEUTRAL = "neutral"


def classify_day(paid_rao: int, noah_deposit_rao: int, no_fault: bool, target_fp: int) -> str:
    """Hit, miss, or neutral for one day."""
    if paid_rao > 0:
        need = target_fp * paid_rao // FP_SCALE
        return HIT if noah_deposit_rao >= need else MISS
    return NEUTRAL if no_fault else MISS


def advance_run(k: int, classification: str) -> int:
    """Run length after one classified day."""
    if classification == HIT:
        return k + 1
    if classification == MISS:
        return 0
    if classification == NEUTRAL:
        return k
    raise ValueError(f"unknown classification: {classification!r}")


def streak_fp(k: int, ceiling_fp: int, ramp_days: int) -> int:
    """The streak factor for run length ``k``, floor at the division."""
    if ceiling_fp < FP_SCALE:
        raise ValueError(f"streak ceiling below 1.0: {ceiling_fp}")
    if ramp_days < 1:
        raise ValueError(f"streak ramp must be >= 1 day: {ramp_days}")
    return FP_SCALE + (ceiling_fp - FP_SCALE) * min(k, ramp_days) // ramp_days
