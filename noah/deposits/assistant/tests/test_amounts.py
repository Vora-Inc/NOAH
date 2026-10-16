import pytest

from noah.deposits.assistant.amounts import (
    BELOW_MINIMUM,
    CUSTOM,
    FIXED,
    MAINTENANCE,
    NOTHING_DUE,
    PARTIAL,
    PERCENT,
    READY,
    DepositRefused,
    Evidence,
    Limits,
    format_noah,
    parse_noah,
    propose,
)
from noah.deposits.assistant.amounts import (
    RAO_PER_NOAH as N,
)


def ev(**kw):
    base = dict(
        through_day="2026-09-29",
        history_complete=True,
        source="test",
        period_start_day="2026-09-28",
        period_eligible_earnings_rao=100 * N,
        cumulative_eligible_earnings_rao=1000 * N,
        credited_contributions_rao=300 * N,
        entry_contribution_rao=10 * N,
    )
    base.update(kw)
    return Evidence(**base)


def lim(**kw):
    base = dict(available_rao=1000 * N, reserve_rao=1 * N, cap_rao=None, minimum_rao=0)
    base.update(kw)
    return Limits(**base)


def test_parse_and_format():
    assert parse_noah("12.5", "x") == 12_500_000_000
    assert format_noah(12_500_000_000) == "12.5 NOAH"
    for bad in ("", None, "abc", "-1", "1.0000000001"):
        with pytest.raises(DepositRefused):
            parse_noah(bad, "x")


@pytest.mark.parametrize("mode", [CUSTOM, FIXED])
def test_explicit_amount(mode):
    p = propose(mode, limits=lim(), amount_rao=5 * N)
    assert (p.status, p.amount_rao) == (READY, 5 * N)


@pytest.mark.parametrize("amount", [None, 0, -1, True, "5"])
def test_explicit_amount_blank_or_bad_refuses(amount):
    with pytest.raises(DepositRefused):
        propose(CUSTOM, limits=lim(), amount_rao=amount)


def test_explicit_amount_over_cap_or_funds_refused_not_shrunk():
    with pytest.raises(DepositRefused) as e:
        propose(FIXED, limits=lim(cap_rao=3 * N), amount_rao=5 * N)
    assert e.value.code == "above_limit"
    with pytest.raises(DepositRefused):
        propose(CUSTOM, limits=lim(available_rao=5 * N, reserve_rao=1 * N), amount_rao=5 * N)


def test_percent_default_50():
    p = propose(PERCENT, limits=lim(), percent_bp=5000, evidence=ev())
    assert p.amount_rao == 50 * N and p.status == READY and not p.warnings


def test_percent_below_50_warns_and_above_allowed():
    low = propose(PERCENT, limits=lim(), percent_bp=3000, evidence=ev())
    assert low.amount_rao == 30 * N
    assert "does not lower the requirement" in low.warnings[0]
    high = propose(PERCENT, limits=lim(), percent_bp=8000, evidence=ev())
    assert high.amount_rao == 80 * N and not high.warnings


@pytest.mark.parametrize("bp", [None, 0, 10_001])
def test_percent_bad_percent_refused(bp):
    with pytest.raises(DepositRefused):
        propose(PERCENT, limits=lim(), percent_bp=bp, evidence=ev())


@pytest.mark.parametrize("mode", [PERCENT, MAINTENANCE])
def test_earnings_modes_refuse_missing_or_incomplete_evidence(mode):
    kw = dict(limits=lim(), percent_bp=5000)
    with pytest.raises(DepositRefused) as e:
        propose(mode, evidence=None, **kw)
    assert e.value.code == "missing_evidence"
    with pytest.raises(DepositRefused) as e:
        propose(mode, evidence=ev(history_complete=False), **kw)
    assert e.value.code == "incomplete_history"
    with pytest.raises(DepositRefused):
        propose(mode, evidence=ev(through_day=None), **kw)


def test_missing_figures_are_not_zero():
    with pytest.raises(DepositRefused):
        propose(
            PERCENT, limits=lim(), percent_bp=5000, evidence=ev(period_eligible_earnings_rao=None)
        )
    with pytest.raises(DepositRefused):
        propose(MAINTENANCE, limits=lim(), evidence=ev(credited_contributions_rao=None))
    with pytest.raises(DepositRefused):
        propose(MAINTENANCE, limits=lim(), evidence=ev(cumulative_eligible_earnings_rao=None))


def test_blank_available_never_means_everything():
    with pytest.raises(DepositRefused) as e:
        propose(PERCENT, limits=lim(available_rao=None), percent_bp=5000, evidence=ev())
    assert e.value.code == "missing_evidence"


def test_maintenance_only_the_remainder():
    # required 50% of 1000 = 500, credited 300 -> 200
    p = propose(MAINTENANCE, limits=lim(), evidence=ev())
    assert p.amount_rao == 200 * N


def test_maintenance_ignores_entry_contribution():
    a = propose(MAINTENANCE, limits=lim(), evidence=ev(entry_contribution_rao=10 * N))
    b = propose(MAINTENANCE, limits=lim(), evidence=ev(entry_contribution_rao=999 * N))
    assert a.amount_rao == b.amount_rao == 200 * N


def test_maintenance_nothing_due_when_already_covered_or_ahead():
    assert (
        propose(MAINTENANCE, limits=lim(), evidence=ev(credited_contributions_rao=500 * N)).status
        == NOTHING_DUE
    )
    ahead = propose(MAINTENANCE, limits=lim(), evidence=ev(credited_contributions_rao=900 * N))
    assert ahead.status == NOTHING_DUE and ahead.amount_rao == 0


def test_maintenance_rounds_requirement_up():
    p = propose(
        MAINTENANCE,
        limits=lim(),
        evidence=ev(cumulative_eligible_earnings_rao=3, credited_contributions_rao=0),
    )
    assert p.amount_rao == 2  # ceil(1.5)


def test_percent_zero_earnings_is_nothing_due_not_error():
    p = propose(PERCENT, limits=lim(), percent_bp=5000, evidence=ev(period_eligible_earnings_rao=0))
    assert p.status == NOTHING_DUE


def test_cap_limits_calculated_amount_to_partial_with_shortfall():
    p = propose(MAINTENANCE, limits=lim(cap_rao=50 * N), evidence=ev())
    assert (p.status, p.amount_rao, p.wanted_rao, p.shortfall_rao) == (
        PARTIAL,
        50 * N,
        200 * N,
        150 * N,
    )
    assert "goal is not reached" in p.warnings[-1]


def test_low_funds_limits_calculated_amount():
    p = propose(
        PERCENT, limits=lim(available_rao=21 * N, reserve_rao=1 * N), percent_bp=5000, evidence=ev()
    )
    assert (p.status, p.amount_rao) == (PARTIAL, 20 * N)


def test_no_spendable_funds_refused():
    with pytest.raises(DepositRefused) as e:
        propose(
            PERCENT,
            limits=lim(available_rao=1 * N, reserve_rao=1 * N),
            percent_bp=5000,
            evidence=ev(),
        )
    assert e.value.code == "insufficient_funds"


def test_below_minimum_skips():
    p = propose(PERCENT, limits=lim(minimum_rao=60 * N), percent_bp=5000, evidence=ev())
    assert (p.status, p.amount_rao) == (BELOW_MINIMUM, 0)


def test_unknown_mode_refused():
    with pytest.raises(DepositRefused):
        propose("everything", limits=lim())
