"""Boundary rules from plan §2.9, including the two build clarifications."""

import json
from pathlib import Path

from noah.reports.backing.constants import PERQUINTILL_ONE, RAO_STEP
from noah.reports.backing.scale import SimSwap
from noah.reports.backing.search import SaleProbeError, ThresholdSearch, invalid_sale_reason
from noah.reports.backing.u64f64 import calculate_price

QUOTE = PERQUINTILL_ONE // 2


def _sim(amount: int, *, tao: int | None = None, partial: bool = False, zero: bool = False) -> SimSwap:
    if zero:
        return SimSwap(0, 0, 0, 0, 0, 0)
    if partial:
        return SimSwap(1, amount // 2, 0, 0, 0, 0)
    fee = 0
    paid = 0 if tao is None else tao
    return SimSwap(paid, amount - fee, 0, fee, 0, 0)


def _run(x, y, probe, drop=0.10, ratio=(9, 10)):
    price = calculate_price(x, y, QUOTE)
    search = ThresholdSearch(
        x=x,
        y=y,
        quote_parts=QUOTE,
        fee_rate=33,
        price_before=price,
        probe=probe,
    )
    return search.search(ratio[0], ratio[1], drop)


def test_smallest_qualifying_step_keeps_boundary_receipts():
    x = y = 50_000_000_000
    calls = []

    def probe(amount):
        calls.append(amount)
        # Larger sales remove a bigger fraction of TAO, so the drop increases with k.
        return _sim(amount, tao=amount // 4)

    outcome = _run(x, y, probe)
    assert outcome.status == "OK"
    assert outcome.k is not None and outcome.k >= 1
    current = outcome.observations[outcome.k]
    assert current.qualifies(calculate_price(x, y, QUOTE), 9, 10)
    if outcome.k > 1:
        previous = outcome.observations[outcome.k - 1]
        assert previous.valid and not previous.qualifies(calculate_price(x, y, QUOTE), 9, 10)
    assert outcome.sim_calls <= 96
    assert outcome.sim_calls == len(calls)


def test_invalid_upper_probe_does_not_declare_the_threshold_unreachable():
    x = 2_000_000_000
    y = 50_000_000_000

    def probe(amount):
        k = amount // RAO_STEP
        if k > 30:
            return _sim(amount, partial=True)
        return _sim(amount, tao=min(y - 1, amount * 400))

    outcome = _run(x, y, probe)
    assert outcome.status == "OK", outcome.reason
    assert outcome.reason != "threshold_not_reached"
    price = calculate_price(x, y, QUOTE)
    assert outcome.observations[outcome.k].qualifies(price, 9, 10)
    if outcome.k > 1:
        assert not outcome.observations[outcome.k - 1].qualifies(price, 9, 10)


def test_input_cap_probe_is_not_enough_to_give_up():
    # k_cap is 8. The first probe lands on the cap and is a partial fill.
    x = 8_000
    y = 50_000_000_000
    assert (1000 * x) // RAO_STEP == 8

    def probe(amount):
        k = amount // RAO_STEP
        if k >= 8:
            return _sim(amount, partial=True)
        return _sim(amount, tao=min(y - 1, amount * 20))

    outcome = _run(x, y, probe)
    assert outcome.status == "OK"
    assert outcome.k is not None and outcome.k < 8


def test_last_valid_k_does_not_require_k_plus_one():
    x = y = 28_000_000
    def probe(amount):
        k = amount // RAO_STEP
        if k >= 4:
            return _sim(amount, zero=True)
        return _sim(amount, tao=amount // 5)

    outcome = _run(x, y, probe)
    assert outcome.status == "OK", outcome.reason
    assert outcome.k is not None and outcome.k < 4
    price = calculate_price(x, y, QUOTE)
    assert outcome.observations[outcome.k].qualifies(price, 9, 10)
    if outcome.k > 1:
        assert not outcome.observations[outcome.k - 1].qualifies(price, 9, 10)
    if outcome.k + 1 in outcome.observations:
        assert outcome.observations[outcome.k + 1].valid is False


def test_valid_k_max_below_the_threshold_is_not_reached():
    """A full fill that does not move the pool never qualifies.

    alpha_fee absorbs the input, so the sim is valid and the price is unchanged.
    k_cap itself is valid and still below the threshold.
    """
    x = y = 50_000_000_000

    def probe(amount):
        return SimSwap(0, 0, 0, amount, 0, 0)

    outcome = _run(x, y, probe)
    assert outcome.status == "UNAVAILABLE"
    assert outcome.reason == "threshold_not_reached"
    assert any(item.valid for item in outcome.observations.values())


def test_observed_invalid_k_minus_2_is_non_monotone():
    x = y = 40_000_000

    def probe(amount):
        k = amount // RAO_STEP
        if k == 1:
            return _sim(amount, zero=True)
        return _sim(amount, tao=amount // 2)

    outcome = _run(x, y, probe)
    assert outcome.status == "UNAVAILABLE"
    assert outcome.reason == "non_monotone_validity"


def test_budget_exhaustion_is_not_reported_as_not_reached():
    x = y = 80_000_000_000_000

    def probe(amount):
        return _sim(amount, tao=1)

    search = ThresholdSearch(
        x=x,
        y=y,
        quote_parts=QUOTE,
        fee_rate=33,
        price_before=calculate_price(x, y, QUOTE),
        probe=probe,
        budget=3,
    )
    outcome = search.search(9, 10, 0.10)
    assert outcome.reason == "search_budget_exceeded"


def test_rpc_failure_is_not_an_invalid_sale():
    x = y = 20_000_000_000

    def probe(amount):
        if amount >= 2 * RAO_STEP:
            raise SaleProbeError("rpc_transport")
        return _sim(amount, tao=amount // 5)

    try:
        _run(x, y, probe)
    except SaleProbeError as exc:
        assert exc.reason == "rpc_transport"
    else:
        raise AssertionError("an RPC failure was treated as a search result")


def test_synthetic_fixture_reasons():
    root = Path(__file__).resolve().parent / "fixtures" / "synthetic"
    zero = json.loads((root / "all-zero.json").read_text())
    partial = json.loads((root / "partial-fill.json").read_text())
    over = json.loads((root / "tao-exceeds-reserve.json").read_text())
    assert invalid_sale_reason(SimSwap(*zero["sim"]), zero["amount_rao"], 10**12, 10**12) == "all_zero_sim"
    assert invalid_sale_reason(SimSwap(*partial["sim"]), partial["amount_rao"], 10**12, 10**12) == "partial_fill"
    assert (
        invalid_sale_reason(SimSwap(*over["sim"]), 1000, 10**12, over["reserve_tao_rao"])
        == "tao_exceeds_reserve"
    )


def test_k_equals_one_does_not_require_k_minus_2():
    x = 2_000_000
    y = 20_000_000_000

    def probe(amount):
        return _sim(amount, tao=min(y - 1, amount * 4))

    outcome = _run(x, y, probe)
    assert outcome.status == "OK"
    assert outcome.k == 1
    assert 0 not in outcome.observations
    assert -1 not in outcome.observations
