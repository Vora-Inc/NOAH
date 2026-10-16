"""Score to pay.

    x(i) = floor( isqrt( score(i, e) ) * multiplier_fp(i) / 1e9 )

The square root compresses the top of the distribution, so a miner ten times
larger does not receive ten times the weight.

The weight vector is the largest-remainder quantization of ``x`` to u16: each
miner gets ``floor(x(i) * 65535 / sum_j x(j))``, residual units go one each to
the largest remainders, ties broken by lowest UID, and the vector sums to
exactly 65,535. If ``sum_j x(j) = 0``, no weights are set at all and the
validator's own fallback applies; the vector is never redirected to a UID. The
fixture output encodes that case as an empty weights object.

See ``docs/architecture/SCORING_SOURCES.md`` for the current source map.
"""

from __future__ import annotations

from math import isqrt

from .constants import FP_SCALE

U16_MAX: int = 65_535


def pay_weight(score: int, multiplier_fp: int) -> int:
    """x(i) = floor( isqrt(score) * multiplier_fp / 1e9 )."""
    return isqrt(score) * multiplier_fp // FP_SCALE


def weights_u16(x_by_uid: dict[int, int]) -> dict[int, int]:
    """Quantized u16 weight vector; ``{}`` when the no-weights fallback applies.

    Largest-remainder distribution: each UID gets
    ``floor(x * 65535 / total)``; the residual units go one each to the
    largest remainders ``(x * 65535) mod total``, ties broken by lowest UID.
    """
    total = sum(x_by_uid.values())
    if total == 0:
        return {}
    order = sorted(x_by_uid)  # lowest UID breaks ties
    base: dict[int, int] = {}
    rem: dict[int, int] = {}
    for uid in order:
        prod = x_by_uid[uid] * U16_MAX
        base[uid] = prod // total
        rem[uid] = prod % total
    residual = U16_MAX - sum(base.values())
    by_remainder = sorted(order, key=lambda u: (-rem[u], u))
    for uid in by_remainder[:residual]:
        base[uid] += 1
    assert sum(base.values()) == U16_MAX
    return base
