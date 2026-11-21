"""Bit-exact U64F64 operations used by subtensor's calculate_price.

Mirrors substrate-fixed 0.6.0 (rev d5f70362) ``FixedU128<U64>`` division and
multiplication, and ``safe_div`` from subtensor ``primitives/safe-math`` at
d967d135 (``checked_div(...).unwrap_or_default()``). ``calculate_price`` follows
``pallets/swap/src/pallet/balancer.rs`` lines 220-228: ``(w1/w2) * (y/x)`` with
``w2`` the quote Perquintill parts and ``w1 = 10^18 - w2`` (saturating).
"""

from __future__ import annotations

from .constants import PERQUINTILL_ONE

_MASK128 = (1 << 128) - 1
_MASK64 = (1 << 64) - 1


def saturating_from_num(value: int) -> int:
    """U64F64 bits for a non-negative integer. Saturates outside 0..2^64-1."""
    if value <= 0:
        return 0
    if value >= (1 << 64):
        return _MASK128
    return value << 64


def safe_div(lhs_bits: int, rhs_bits: int) -> int:
    """checked_div.unwrap_or_default(): 0 on a zero divisor or overflow."""
    if rhs_bits == 0:
        return 0
    quotient = (lhs_bits << 64) // rhs_bits
    if quotient > _MASK128:
        return 0
    return quotient


def saturating_mul(lhs_bits: int, rhs_bits: int) -> int:
    """Unsigned saturating fixed multiply: (a * b) >> 64, saturating at 2^128-1."""
    shifted = (lhs_bits * rhs_bits) >> 64
    if shifted > _MASK128:
        return _MASK128
    return shifted


def price_1e9(bits: int) -> int:
    """floor(P * 1e9), matching current_alpha_price at runtime/src/lib.rs:2874-2877."""
    scaled = saturating_mul(bits, saturating_from_num(1_000_000_000))
    return scaled >> 64


def base_weight_parts(quote_parts: int) -> int:
    if quote_parts >= PERQUINTILL_ONE:
        return 0
    return PERQUINTILL_ONE - quote_parts


def calculate_price(x: int, y: int, quote_parts: int) -> int:
    """Raw U64F64 bits of (w1/w2) * (y/x)."""
    w2 = saturating_from_num(quote_parts)
    w1 = saturating_from_num(base_weight_parts(quote_parts))
    x_fixed = saturating_from_num(x)
    y_fixed = saturating_from_num(y)
    return saturating_mul(safe_div(w1, w2), safe_div(y_fixed, x_fixed))


def drop_ppm(price_before: int, price_after: int) -> int:
    """floor((P_B - P_after) * 1_000_000 / P_B) on raw bits."""
    if price_before <= 0:
        raise ValueError("drop_ppm requires a positive price")
    return ((price_before - price_after) * 1_000_000) // price_before


def qualifies(price_before: int, price_after: int, numerator: int, denominator: int) -> bool:
    """Exact integer test P_after * denominator <= P_B * numerator."""
    return price_after * denominator <= price_before * numerator
