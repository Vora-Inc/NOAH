"""The Standard's multiplier, in exact integer arithmetic.

    multiplier = min((PCI / target) ^ harshness, cap)

The fixed-point convention:

  * Every fractional quantity is an unsigned integer scaled by
    ``FP_SCALE = 10^9``. So 1.0 is 1_000_000_000, an 0.80 target is
    800_000_000. This matches rao and the existing scoring code's
    ``FP_SCALE``.
  * The only rounding anywhere is floor, applied at every division, in a
    fixed order. Floor is chosen because it never awards score that was not
    earned; the bias is always against the miner, never for them.
  * ``harshness`` is a plain int, 1 through ``HARSHNESS_MAX``, or an exact
    rational (``fractions.Fraction``) with the same range, e.g.
    ``Fraction(3, 2)`` for a harshness of 1.5. A fractional power does *not*
    need exp/ln (which would need floats and libm agreement this codebase
    refuses to depend on): for a rational exponent ``p/q`` in lowest terms,
    split it into a whole part ``w = p // q`` and a proper-fraction
    remainder ``s = p % q``. The whole part reuses the existing
    multiply-and-floor loop below, unchanged. The remainder is computed in
    one exact floor using :func:`noah.scoring.decay.integer_nth_root`
    (Newton iteration on plain integers, already used by this codebase's
    half-life decay):

        floor(FP_SCALE * (ratio/FP_SCALE)^(s/q))
          == integer_nth_root(ratio**s * FP_SCALE**(q - s), q)

    because ``FP_SCALE^q * (ratio/FP_SCALE)^s == ratio^s * FP_SCALE^(q-s)``,
    and the floored qth root of that is exactly the fixed-point value
    sought. The whole-part result and the fraction-part result are then
    combined with one more floor-multiply, so the whole computation is still
    "floor at every step, in a fixed order" -- just one more step than the
    integer case. No floats appear anywhere, not even as an intermediate.
    Whole-number harshness (plain ``int``, or a ``Fraction`` that reduces to
    denominator 1) takes the exact original code path, so every existing
    result is unchanged bit for bit. The hard gate (score nothing below
    target) is the sentinel ``CLIFF``, never a large exponent faking one.
  * All intermediates in the whole-number power loop fit in 128 bits given
    the input bounds below, so a future Rust or on-validator port can use
    u128 with no big-integer library. Python integers are arbitrary-
    precision, so this reference implementation is exact by construction;
    the bounds are asserted so a vector that would overflow a u128 port
    fails loudly here first. The fractional remainder step above is exact
    in this arbitrary-precision Python reference for any denominator, but a
    large denominator makes ``ratio**s`` wide, so it does not carry the same
    two-multiplication u128 bound as the integer loop; a fixed-width port
    should bound the denominators it accepts.

Input bounds (asserted):
  * ``0 <= pci    < 2^64``
  * ``0 <  target <= FP_SCALE``  (a target above 1.0 is rejected: an index
    above 1.0 requires buying tokens beyond what you were paid, and a subnet
    demanding that of every miner has left the safety envelope this design is
    built around)
  * ``1 <= harshness <= HARSHNESS_MAX`` (as an exact value: an ``int`` or a
    ``Fraction``) or ``harshness is CLIFF``
  * ``0 <= cap < 2^64``

Overflow discipline for the power loop, so a u128 port is safe:
  * ``ratio`` is clamped to ``cap`` once it can no longer change the result:
    if ``ratio >= FP_SCALE`` (at or above 1.0) the powers are non-decreasing,
    so the first intermediate at or above ``cap`` decides the answer.
  * if ``ratio < FP_SCALE`` the powers are non-increasing and every
    intermediate stays below ``FP_SCALE``.
  * therefore every multiplication multiplies two values below
    ``max(cap, FP_SCALE) < 2^64``, and the product stays below 2^128.
  * for a fractional harshness, the same clamp-on-cap discipline applies to
    the whole-part loop; the single fraction-part root and the final
    combining floor-multiply are exact but are not claimed to stay inside
    128 bits for an arbitrary denominator (see above).

The cliff rule: below target scores zero, at or above target scores exactly
1.0, and the cap is ignored at the cliff. A subnet that wants over-commitment
rewarded uses a finite harshness with a cap instead.
"""

from __future__ import annotations

from fractions import Fraction

FP_SCALE: int = 1_000_000_000
HARSHNESS_MAX: int = 8

# Sentinel for the cliff. Deliberately not an integer so nobody can pass it
# by accident where an exponent is expected.
CLIFF: str = "cliff"


def _check_inputs(pci: int, target: int, harshness, cap: int) -> None:
    if not (0 <= pci < 1 << 64):
        raise ValueError(f"pci out of range: {pci}")
    if not (0 < target <= FP_SCALE):
        raise ValueError(f"target out of range: {target}")
    if not (0 <= cap < 1 << 64):
        raise ValueError(f"cap out of range: {cap}")
    if harshness is not CLIFF and harshness != CLIFF:
        if isinstance(harshness, bool):
            raise ValueError(f"harshness must be an int, Fraction, or CLIFF: {harshness!r}")
        elif isinstance(harshness, int):
            value: Fraction = Fraction(harshness, 1)
        elif isinstance(harshness, Fraction):
            value = harshness
        else:
            raise ValueError(f"harshness must be an int, Fraction, or CLIFF: {harshness!r}")
        if not (1 <= value <= HARSHNESS_MAX):
            raise ValueError(f"harshness out of range: {harshness}")


def _cliff(pci: int, target: int, cap: int) -> int:
    """The cliff: a hard gate. The cap is ignored at the cliff."""
    if pci < target:
        return 0
    return FP_SCALE


def multiplier(pci: int, target: int, harshness, cap: int) -> int:
    """min((pci/target)^harshness, cap), floor at every step, FP_SCALE-scaled.

    Every argument and the result are FP_SCALE fixed-point integers except
    ``harshness``, which is a plain integer exponent, an exact rational
    (``fractions.Fraction``) exponent, or ``CLIFF``.
    """
    _check_inputs(pci, target, harshness, cap)

    if harshness == CLIFF:
        return _cliff(pci, target, cap)

    # ratio = pci / target, floored. Bound: pci < 2^64, FP_SCALE = 1e9 < 2^30,
    # so pci * FP_SCALE < 2^94, safe in u128.
    ratio: int = (pci * FP_SCALE) // target

    if isinstance(harshness, Fraction):
        p, q = harshness.numerator, harshness.denominator
    else:
        p, q = harshness, 1

    if q == 1:
        # Whole-number harshness: the exact original code, byte for byte, so
        # every existing vector and property continues to hold unchanged.
        e = p
        if ratio >= FP_SCALE:
            # Powers are non-decreasing: cap decides as soon as it is reached.
            if ratio >= cap:
                return cap
            acc: int = ratio
            for _ in range(e - 1):
                acc = (acc * ratio) // FP_SCALE
                if acc >= cap:
                    return cap
            return acc  # acc < cap here, min() already decided
        else:
            # Powers are non-increasing and stay below FP_SCALE.
            acc = ratio
            for _ in range(e - 1):
                acc = (acc * ratio) // FP_SCALE
                if acc == 0:
                    break  # floor has hit zero; further powers stay zero
            return min(acc, cap)

    # Fractional harshness p/q, q > 1. Fraction always stores lowest terms,
    # so gcd(p, q) == 1 and (since harshness >= 1 was asserted above) w >= 1
    # and s != 0. See the module docstring for the exact-root identity used
    # for the s/q remainder.
    #
    # Deferred import: noah.scoring.engine imports this module at module
    # load time, so importing noah.scoring back from here at module load
    # time would be circular. Calling it here, only when actually needed,
    # is safe because by the time any code *calls* multiplier(), all module
    # loading has already finished.
    from noah.scoring.decay import integer_nth_root

    w, s = divmod(p, q)

    if ratio >= FP_SCALE:
        # Powers are non-decreasing: cap decides as soon as it is reached.
        if ratio >= cap:
            return cap
        whole: int = ratio
        for _ in range(w - 1):
            whole = (whole * ratio) // FP_SCALE
            if whole >= cap:
                return cap
        frac = integer_nth_root(ratio**s * FP_SCALE ** (q - s), q)
        result = (whole * frac) // FP_SCALE
        return cap if result >= cap else result
    else:
        # Powers are non-increasing and stay below FP_SCALE.
        whole = ratio
        for _ in range(w - 1):
            whole = (whole * ratio) // FP_SCALE
            if whole == 0:
                break  # floor has hit zero; further powers stay zero
        frac = integer_nth_root(ratio**s * FP_SCALE ** (q - s), q)
        result = (whole * frac) // FP_SCALE
        return min(result, cap)


def apply_multiplier(own_score: int, mult: int) -> int:
    """final score = own score x multiplier, floored.

    ``own_score`` is whatever integer scale the adopting subnet already uses;
    the result is on that same scale. Bound asserted so a u128 port is safe.
    """
    if not (0 <= own_score < 1 << 64):
        raise ValueError(f"own_score out of range: {own_score}")
    if not (0 <= mult < 1 << 64):
        raise ValueError(f"mult out of range: {mult}")
    return (own_score * mult) // FP_SCALE
