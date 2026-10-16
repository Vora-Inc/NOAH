"""Integer half-life decay of running totals.

Compute the one-day fixed-point factor with exact integer roots. Each daily step
floors after multiplication, preserving deterministic results across languages."""

from __future__ import annotations

from functools import lru_cache

from .constants import FP_SCALE


def integer_nth_root(n: int, k: int) -> int:
    """Largest ``x`` with ``x**k <= n``. Integer Newton iteration, no floats."""
    if k < 1:
        raise ValueError(f"root order must be >= 1: {k}")
    if n < 0:
        raise ValueError(f"radicand must be >= 0: {n}")
    if n == 0:
        return 0
    if k == 1:
        return n
    # Initial guess strictly above the root.
    x = 1 << (n.bit_length() // k + 1)
    while True:
        y = ((k - 1) * x + n // x ** (k - 1)) // k
        if y >= x:
            break
        x = y
    # Defensive polish: Newton can stop one off the floor root.
    while x**k > n:
        x -= 1
    while (x + 1) ** k <= n:
        x += 1
    return x


@lru_cache(maxsize=None)
def one_day_factor_fp(half_life_days: int) -> int:
    """``DECAY[1] = floor(2^(-1/H) * 1e9)`` for half-life ``H`` days."""
    if half_life_days < 1:
        raise ValueError(f"half-life must be >= 1 day: {half_life_days}")
    return integer_nth_root(FP_SCALE**half_life_days // 2, half_life_days)


def decay_step(total: int, factor_fp: int) -> int:
    """One day-close decay of a running total. Floor, as everywhere."""
    return total * factor_fp // FP_SCALE
