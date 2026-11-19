"""Threshold search on the 0.001-alpha grid.

The closed-form estimate only chooses the first probe. An invalid probe, including
an oversized estimate, the input cap, or a partial fill, is not proof that the
threshold is unreachable. RPC and decode failures are not sale-validity signals:
they propagate and must not shrink ``k_max``. An observed invalid smaller positive
amount beside a valid larger amount is ``non_monotone_validity``, including ``k-2``.
``k-2 < 1`` is not a required check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .constants import FEE_DENOMINATOR, MAX_INPUT_MULTIPLIER, PERQUINTILL_ONE, RAO_STEP, SIM_BUDGET_PER_THRESHOLD
from .scale import SimSwap
from .u64f64 import calculate_price, drop_ppm, qualifies


class SearchFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SaleProbeError(Exception):
    """A simulation could not be obtained. Not an invalid sale."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Observation:
    k: int
    amount: int
    valid: bool
    reason: str | None
    sim: SimSwap | None
    price_bits: int | None

    def qualifies(self, price_before: int, numerator: int, denominator: int) -> bool:
        if not self.valid or self.price_bits is None:
            return False
        return qualifies(price_before, self.price_bits, numerator, denominator)


@dataclass
class ThresholdOutcome:
    status: str
    reason: str | None = None
    k: int | None = None
    sim_calls: int = 0
    k_max: int | None = None
    observations: dict[int, Observation] = field(default_factory=dict)
    k_plus_required: bool | None = None


def invalid_sale_reason(sim: SimSwap, amount: int, x: int, y: int) -> str | None:
    if amount > MAX_INPUT_MULTIPLIER * x:
        return "above_input_cap"
    if amount > 0 and all(value == 0 for value in sim.as_tuple()):
        return "all_zero_sim"
    if sim.alpha_amount + sim.alpha_fee != amount:
        return "partial_fill"
    if sim.tao_amount >= y:
        return "tao_exceeds_reserve"
    return None


def estimate_k(x: int, quote_parts: int, fee_rate: int, drop: float) -> int:
    """Float starting step only. Never an answer and never proof of unavailability."""
    if x <= 0 or quote_parts <= 0 or quote_parts >= PERQUINTILL_ONE:
        return 1
    weight = quote_parts / PERQUINTILL_ONE
    fee = fee_rate / FEE_DENOMINATOR
    if fee >= 1 or not 0 < weight < 1:
        return 1
    try:
        estimate = x * ((1 - drop) ** (-weight) - 1) / (1 - fee)
    except (OverflowError, ZeroDivisionError, ValueError):
        return 1
    if not math.isfinite(estimate) or estimate <= 0:
        return 1
    return max(1, math.ceil(1.02 * estimate / RAO_STEP))


class ThresholdSearch:
    def __init__(
        self,
        *,
        x: int,
        y: int,
        quote_parts: int,
        fee_rate: int,
        price_before: int,
        probe,
        budget: int = SIM_BUDGET_PER_THRESHOLD,
    ):
        self.x = x
        self.y = y
        self.quote_parts = quote_parts
        self.fee_rate = fee_rate
        self.price_before = price_before
        self.probe = probe
        self.budget = budget
        self.calls = 0
        self.observations: dict[int, Observation] = {}

    def k_cap(self) -> int:
        return (MAX_INPUT_MULTIPLIER * self.x) // RAO_STEP

    def observe(self, k: int) -> Observation:
        cached = self.observations.get(k)
        if cached is not None:
            return cached
        if k < 1:
            raise SearchFailure("non_monotone_validity")
        if self.calls >= self.budget:
            raise SearchFailure("search_budget_exceeded")
        self.calls += 1
        amount = k * RAO_STEP
        try:
            sim = self.probe(amount)
        except SaleProbeError:
            raise
        except SearchFailure:
            raise
        if not isinstance(sim, SimSwap):
            raise SaleProbeError("decode_error")
        reason = invalid_sale_reason(sim, amount, self.x, self.y)
        if reason is None:
            price_bits = calculate_price(self.x + sim.alpha_amount, self.y - sim.tao_amount, self.quote_parts)
            observation = Observation(k, amount, True, None, sim, price_bits)
        else:
            observation = Observation(k, amount, False, reason, sim, None)
        self.observations[k] = observation
        self._reject_non_monotone()
        return observation

    def _reject_non_monotone(self) -> None:
        valid = [k for k, item in self.observations.items() if item.valid and k >= 1]
        invalid = [k for k, item in self.observations.items() if not item.valid and k >= 1]
        if valid and invalid and min(invalid) < max(valid):
            raise SearchFailure("non_monotone_validity")

    def _largest_valid(self, low_inclusive: int, high_exclusive_invalid: int) -> int:
        """Largest valid k in [low_inclusive, high_exclusive_invalid). The high bound is invalid."""
        lo = low_inclusive - 1
        hi = high_exclusive_invalid
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.observe(mid).valid:
                lo = mid
            else:
                hi = mid
        return lo

    def _smallest_qualifying(self, k_hi: int, numerator: int, denominator: int) -> int:
        lo = 0
        hi = k_hi
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            observation = self.observe(mid)
            if not observation.valid:
                raise SearchFailure("non_monotone_validity")
            if observation.qualifies(self.price_before, numerator, denominator):
                hi = mid
            else:
                lo = mid
        return hi

    def search(self, numerator: int, denominator: int, drop: float) -> ThresholdOutcome:
        try:
            return self._search(numerator, denominator, drop)
        except SearchFailure as exc:
            return ThresholdOutcome(
                status="UNAVAILABLE",
                reason=exc.reason,
                sim_calls=self.calls,
                observations=dict(self.observations),
            )

    def _search(self, numerator: int, denominator: int, drop: float) -> ThresholdOutcome:
        cap = self.k_cap()
        if cap < 1:
            raise SearchFailure("input_cap_too_small")
        k_est = estimate_k(self.x, self.quote_parts, self.fee_rate, drop)
        k_try = min(max(k_est, 1), cap)
        first = self.observe(k_try)
        k_max: int | None = None
        if not first.valid:
            k_max = self._largest_valid(1, k_try)
            if k_max < 1:
                raise SearchFailure("no_valid_sale")
            top = self.observations[k_max]
            if not top.qualifies(self.price_before, numerator, denominator):
                raise SearchFailure("threshold_not_reached")
            k_hi = k_max
        elif first.qualifies(self.price_before, numerator, denominator):
            k_hi = k_try
        else:
            k_hi, k_max = self._widen(k_try, cap, numerator, denominator)
        k = self._smallest_qualifying(k_hi, numerator, denominator)
        self._prove(k, k_max, cap, numerator, denominator)
        return ThresholdOutcome(
            status="OK",
            k=k,
            sim_calls=self.calls,
            k_max=k_max,
            observations=dict(self.observations),
            k_plus_required=self._k_plus_required(k, k_max, cap),
        )

    def _widen(self, k_valid_nonqual: int, cap: int, numerator: int, denominator: int) -> tuple[int, int | None]:
        probe = k_valid_nonqual
        k_max: int | None = None
        while True:
            if probe >= cap:
                raise SearchFailure("threshold_not_reached")
            nxt = min(cap, probe * 2)
            if nxt == probe:
                raise SearchFailure("threshold_not_reached")
            observation = self.observe(nxt)
            if not observation.valid:
                k_max = self._largest_valid(probe, nxt)
                if k_max < 1 or not self.observations[k_max].qualifies(self.price_before, numerator, denominator):
                    raise SearchFailure("threshold_not_reached")
                return k_max, k_max
            if observation.qualifies(self.price_before, numerator, denominator):
                return nxt, k_max
            probe = nxt

    def _k_plus_required(self, k: int, k_max: int | None, cap: int) -> bool:
        if k >= cap:
            return False
        if k_max is not None and k >= k_max:
            return False
        if k + 1 in self.observations and not self.observations[k + 1].valid:
            return False
        return True

    def _prove(self, k: int, k_max: int | None, cap: int, numerator: int, denominator: int) -> None:
        current = self.observations[k]
        if not current.valid or not current.qualifies(self.price_before, numerator, denominator):
            raise SearchFailure("non_monotone_boundary")
        if k > 1:
            previous = self.observe(k - 1)
            if not previous.valid:
                raise SearchFailure("non_monotone_validity")
            if previous.qualifies(self.price_before, numerator, denominator):
                raise SearchFailure("non_monotone_boundary")
        if k - 2 >= 1:
            earlier = self.observe(k - 2)
            if not earlier.valid:
                raise SearchFailure("non_monotone_validity")
            if earlier.qualifies(self.price_before, numerator, denominator):
                raise SearchFailure("non_monotone_boundary")
        if self._k_plus_required(k, k_max, cap):
            nxt = self.observe(k + 1)
            if not nxt.valid:
                # Observed after the fact: a larger amount is invalid while k is valid.
                # That contradicts the monotone-validity assumption, unless this amount
                # is the last valid sale. k+1 invalid means k is the last valid amount,
                # so k+1 is not required. The non-monotone check would already have
                # fired if some larger k were valid.
                return
            if not nxt.qualifies(self.price_before, numerator, denominator):
                raise SearchFailure("non_monotone_boundary")


def boundary_drop(observation: Observation, price_before: int) -> int | None:
    if observation.price_bits is None:
        return None
    return drop_ppm(price_before, observation.price_bits)
