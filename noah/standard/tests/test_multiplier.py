"""Vector and property tests for the Standard's multiplier.

The JSON vectors in ``vectors/multiplier_vectors.json`` are the canonical set
every port has to reproduce. The TypeScript implementation runs the identical
file, and the two must agree on every vector, bit for bit.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal, getcontext
from fractions import Fraction
from pathlib import Path

from noah.standard.multiplier import (
    CLIFF,
    FP_SCALE,
    HARSHNESS_MAX,
    apply_multiplier,
    multiplier,
)

VECTORS = json.loads((Path(__file__).parent / "vectors" / "multiplier_vectors.json").read_text())


def _h(raw):
    return CLIFF if raw == "cliff" else raw


class TestVectors(unittest.TestCase):
    def test_multiplier_vectors(self):
        for v in VECTORS["multiplier_vectors"]:
            with self.subTest(v["name"]):
                got = multiplier(int(v["pci"]), int(v["target"]), _h(v["harshness"]), int(v["cap"]))
                self.assertEqual(got, int(v["expected_multiplier"]), v["name"])

    def test_apply_vectors(self):
        for v in VECTORS["apply_vectors"]:
            with self.subTest(v["name"]):
                got = apply_multiplier(int(v["own_score"]), int(v["mult"]))
                self.assertEqual(got, int(v["expected"]), v["name"])


class TestProperties(unittest.TestCase):
    """Determinism-adjacent invariants a port must also satisfy."""

    def test_never_exceeds_cap(self):
        # Finite harshness respects the cap. The cliff ignores it by
        # definition: its only outputs are 0 and FP_SCALE.
        for pci in (0, 1, FP_SCALE // 3, FP_SCALE, 5 * FP_SCALE, (1 << 63)):
            for target in (1, FP_SCALE // 2, FP_SCALE):
                for cap in (0, FP_SCALE // 2, FP_SCALE, 4 * FP_SCALE):
                    for h in range(1, HARSHNESS_MAX + 1):
                        self.assertLessEqual(multiplier(pci, target, h, cap), cap)
                    self.assertIn(multiplier(pci, target, CLIFF, cap), (0, FP_SCALE))

    def test_monotone_in_pci(self):
        # More sent, same paid, never a lower multiplier.
        target, cap = FP_SCALE // 2, 4 * FP_SCALE
        for h in list(range(1, HARSHNESS_MAX + 1)) + [CLIFF]:
            prev = -1
            for pci in range(0, 2 * FP_SCALE, FP_SCALE // 20):
                m = multiplier(pci, target, h, cap)
                self.assertGreaterEqual(m, prev, f"h={h} pci={pci}")
                prev = m

    def test_at_target_is_full_score(self):
        # An index exactly at target must give exactly 1.0 at every
        # harshness, the cliff included.
        for target in (1, FP_SCALE // 4, 4 * FP_SCALE // 5, FP_SCALE):
            for h in list(range(1, HARSHNESS_MAX + 1)) + [CLIFF]:
                self.assertEqual(multiplier(target, target, h, FP_SCALE), FP_SCALE)

    def test_harsher_never_pays_more_below_target(self):
        target, cap = 4 * FP_SCALE // 5, FP_SCALE
        for pci in range(0, target, FP_SCALE // 25):
            prev = None
            for h in range(1, HARSHNESS_MAX + 1):
                m = multiplier(pci, target, h, cap)
                if prev is not None:
                    self.assertLessEqual(m, prev, f"pci={pci} h={h}")
                prev = m

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, 0, 1, FP_SCALE)  # zero target
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, FP_SCALE + 1, 1, FP_SCALE)  # target above 1.0
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, FP_SCALE, 0, FP_SCALE)  # harshness 0
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, FP_SCALE, HARSHNESS_MAX + 1, FP_SCALE)
        with self.assertRaises(ValueError):
            multiplier(1 << 64, FP_SCALE, 1, FP_SCALE)  # pci over bound
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, FP_SCALE, True, FP_SCALE)  # bool is not an int here


def _decimal_oracle(pci: int, target: int, p: int, q: int, cap: int) -> int:
    """Independent reference for a rational harshness ``p/q``.

    This is a TEST-ONLY oracle. It is deliberately built a different way
    than the production code: it uses :mod:`decimal` at high precision for
    the fractional-exponent step (``Decimal.__pow__`` with a Decimal
    exponent, which is implemented with correctly-rounded ln/exp under the
    hood), instead of the production code's exact integer nth root. The
    production code never uses ``decimal`` or floats anywhere.

    It mirrors the production algorithm's *structure* (floor the ratio
    first, then the same floor-composed whole-part loop, cap-clamped the
    same way, then one more floor-multiply to fold in the fractional part)
    so it is checking the same floor-composition discipline, not a
    from-scratch textbook power -- a naive ``(pci/target)**(p/q)`` computed
    directly from the unfloored ratio disagrees with the production result
    by a handful of FP_SCALE units even for a whole-number harshness,
    because the production code deliberately floors at every step (see the
    module docstring). This oracle floors in the same places, so its
    disagreement with the production result (if any) isolates a real bug in
    the fractional-exponent step rather than restating that known, harmless
    floor-composition drift.
    """
    getcontext().prec = 60
    ratio = (pci * FP_SCALE) // target
    non_decreasing = ratio >= FP_SCALE
    w, s = divmod(p, q)

    if non_decreasing and ratio >= cap:
        return cap

    whole = Decimal(ratio)
    for _ in range(w - 1):
        whole = (whole * Decimal(ratio)) // Decimal(FP_SCALE)
        if non_decreasing and whole >= cap:
            return cap
        if not non_decreasing and whole == 0:
            break

    if s == 0:
        frac = Decimal(FP_SCALE)
    else:
        frac_pow = (Decimal(ratio) / Decimal(FP_SCALE)) ** (Decimal(s) / Decimal(q))
        frac = Decimal(int(Decimal(FP_SCALE) * frac_pow))  # floor: positive value, truncate

    result = int((whole * frac) // Decimal(FP_SCALE))
    if non_decreasing:
        return cap if result >= cap else result
    return min(result, cap)


class TestFractionalHarshness(unittest.TestCase):
    """Exact-rational harshness (D-*, "decimal capability" widening).

    ``harshness`` now also accepts a ``fractions.Fraction`` such as
    ``Fraction(3, 2)`` (1.5) or ``Fraction(5, 2)`` (2.5), computed exactly
    via integer nth roots -- see the module docstring. Plain ``int``
    harshness, and a ``Fraction`` that reduces to an integer, must still
    take the byte-identical original code path.
    """

    def test_harshness_one_point_five_matches_oracle(self):
        h = Fraction(3, 2)
        for pci, target, cap in (
            (4 * FP_SCALE, FP_SCALE, 100 * FP_SCALE),  # exact: 4^1.5 = 8
            (9 * FP_SCALE, FP_SCALE, 100 * FP_SCALE),  # exact: 9^1.5 = 27
            (600_000_000, 800_000_000, FP_SCALE),
            (3 * FP_SCALE, FP_SCALE // 2, FP_SCALE),
        ):
            with self.subTest(pci=pci, target=target, cap=cap):
                got = multiplier(pci, target, h, cap)
                want = _decimal_oracle(pci, target, 3, 2, cap)
                self.assertEqual(got, want)

    def test_harshness_two_point_five_matches_oracle(self):
        h = Fraction(5, 2)
        for pci, target, cap in (
            (4 * FP_SCALE, FP_SCALE, 1000 * FP_SCALE),  # exact: 4^2.5 = 32
            (600_000_000, 800_000_000, FP_SCALE),
            (3 * FP_SCALE, FP_SCALE // 2, 4 * FP_SCALE),
        ):
            with self.subTest(pci=pci, target=target, cap=cap):
                got = multiplier(pci, target, h, cap)
                want = _decimal_oracle(pci, target, 5, 2, cap)
                self.assertEqual(got, want)

    def test_exact_perfect_power_cases(self):
        # Bases chosen so the true rational power is an integer, so the
        # exact answer is knowable by hand, independent of any oracle.
        target = FP_SCALE
        self.assertEqual(multiplier(4 * FP_SCALE, target, Fraction(3, 2), 100 * FP_SCALE), 8 * FP_SCALE)
        self.assertEqual(multiplier(9 * FP_SCALE, target, Fraction(3, 2), 100 * FP_SCALE), 27 * FP_SCALE)
        self.assertEqual(multiplier(4 * FP_SCALE, target, Fraction(5, 2), 1000 * FP_SCALE), 32 * FP_SCALE)
        self.assertEqual(multiplier(8 * FP_SCALE, target, Fraction(4, 3), 1000 * FP_SCALE), 16 * FP_SCALE)

    def test_ratio_zero(self):
        for h in (Fraction(3, 2), Fraction(5, 2), Fraction(11, 4)):
            self.assertEqual(multiplier(0, FP_SCALE, h, FP_SCALE), 0)

    def test_ratio_equals_target_exactly(self):
        # At target, every harshness -- integer, fractional, or the cliff --
        # must give exactly FP_SCALE (full score), regardless of cap, as
        # long as cap does not clip it from below.
        for target in (1, FP_SCALE // 4, 4 * FP_SCALE // 5, FP_SCALE):
            for h in (Fraction(3, 2), Fraction(5, 2), Fraction(11, 4), Fraction(13, 6)):
                self.assertEqual(multiplier(target, target, h, 10 * FP_SCALE), FP_SCALE)

    def test_ratio_at_or_above_cap(self):
        # Ratio itself at/above the cap decides immediately, before any
        # exponentiation, exactly as in the integer path.
        target = FP_SCALE
        for h in (Fraction(3, 2), Fraction(5, 2)):
            self.assertEqual(multiplier(5 * FP_SCALE, target, h, 3 * FP_SCALE), 3 * FP_SCALE)
            self.assertEqual(multiplier(3 * FP_SCALE, target, h, 3 * FP_SCALE), 3 * FP_SCALE)

    def test_cap_clips_mid_computation(self):
        # harshness 7/2 has a whole part of 3, so the whole-part loop runs
        # more than one multiplication -- exercise the early cap-exit inside
        # that loop, and confirm it agrees with the equivalent capped
        # integer harshness (4) once the true power is well past the cap.
        pci, target, cap = 2 * FP_SCALE, FP_SCALE, 3 * FP_SCALE
        got = multiplier(pci, target, Fraction(7, 2), cap)
        self.assertEqual(got, cap)
        self.assertEqual(got, multiplier(pci, target, 4, cap))

    def test_whole_number_fraction_matches_integer_path_exactly(self):
        # Fraction(n, 1) (e.g. 2.0 expressed as a Fraction) must reproduce
        # the plain-int result bit for bit: same code path, not just a
        # close numeric answer.
        for pci in (0, 1, FP_SCALE // 3, FP_SCALE, 5 * FP_SCALE, (1 << 63)):
            for target in (1, FP_SCALE // 2, FP_SCALE):
                for cap in (0, FP_SCALE // 2, FP_SCALE, 4 * FP_SCALE):
                    for h in range(1, HARSHNESS_MAX + 1):
                        with self.subTest(pci=pci, target=target, cap=cap, h=h):
                            self.assertEqual(
                                multiplier(pci, target, Fraction(h, 1), cap),
                                multiplier(pci, target, h, cap),
                            )

    def test_random_fractions_match_oracle(self):
        import random

        rng = random.Random(112_16)
        checked = 0
        for _ in range(300):
            pci = rng.randint(0, 5 * FP_SCALE)
            target = rng.randint(1, FP_SCALE)
            cap = rng.choice([0, FP_SCALE // 2, FP_SCALE, 2 * FP_SCALE, 5 * FP_SCALE])
            p = rng.randint(2, 16)
            q = rng.randint(2, 8)
            if p < q:
                p, q = q, p
            frac = Fraction(p, q)
            if not (1 <= frac <= HARSHNESS_MAX):
                continue
            checked += 1
            got = multiplier(pci, target, frac, cap)
            want = _decimal_oracle(pci, target, frac.numerator, frac.denominator, cap)
            self.assertEqual(got, want, f"pci={pci} target={target} h={frac} cap={cap}")
        self.assertGreater(checked, 100)  # sanity: the range filter didn't skip everything

    def test_rejects_float_harshness(self):
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, FP_SCALE, 1.5, FP_SCALE)

    def test_rejects_out_of_range_fraction(self):
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, FP_SCALE, Fraction(1, 2), FP_SCALE)  # below 1
        with self.assertRaises(ValueError):
            multiplier(FP_SCALE, FP_SCALE, Fraction(HARSHNESS_MAX * 2 + 1, 2), FP_SCALE)  # above max

    def test_never_exceeds_cap_fractional(self):
        for pci in (0, 1, FP_SCALE // 3, FP_SCALE, 5 * FP_SCALE, (1 << 63)):
            for target in (1, FP_SCALE // 2, FP_SCALE):
                for cap in (0, FP_SCALE // 2, FP_SCALE, 4 * FP_SCALE):
                    for h in (Fraction(3, 2), Fraction(5, 2), Fraction(11, 4)):
                        self.assertLessEqual(multiplier(pci, target, h, cap), cap)


if __name__ == "__main__":
    unittest.main()
