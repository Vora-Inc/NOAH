"""Account for prepaid contributions and uncovered prior obligations.

Credit and coverage reserves serve different purposes. Catch-up contributions
repay prior debt before becoming future coverage; decay-weighted repayment is
handled by the scoring engine. Use integer amounts throughout."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import FP_SCALE
from .fixture_io import Dials


def need_for_day(dials: Dials, pay_rao: int) -> int:
    """The deposit a day's spendable pay requires: ``floor(target x pay)``.

    Equal to the ``need`` inside ``streak.classify_day``; a test holds the two
    together.
    """
    return dials.target_fp * pay_rao // FP_SCALE


@dataclass(frozen=True)
class Reserve:
    """The three never-decaying, uncapped ledgers, in rao. ``coverage`` and
    ``debt`` are never both positive at once (a single day's surplus always
    drains one before banking into the other; see the module docstring and
    ``test_coverage_and_debt_are_never_both_positive``)."""

    credit: int = 0
    coverage: int = 0
    debt: int = 0


@dataclass(frozen=True)
class DayStep:
    """What one day did to the ledgers and to the day's scoring inputs."""

    reserve: Reserve
    credit_now: int  # min(D, pay), joins N immediately
    release: int  # released from the credit reserve, joins N
    draw: int  # taken from the coverage reserve to meet the day's need (forward)
    repay: int  # taken from today's surplus to pay down old debt (backward)
    index_sent: int  # joins S: min(D, need) + draw + repay


def reserve_step(reserve: Reserve, deposit_rao: int, pay_rao: int, need_rao: int) -> DayStep:
    """Advance the ledgers by one day. Pure; see the module docstring."""
    if deposit_rao < 0 or pay_rao < 0 or need_rao < 0:
        raise ValueError("deposit, pay and need must not be negative")
    credit_now = min(deposit_rao, pay_rao)
    credit, coverage, debt = reserve.credit, reserve.coverage, reserve.debt
    draw = release = repay = 0
    if pay_rao > 0 and deposit_rao < need_rao:
        draw = min(coverage, need_rao - deposit_rao)
        coverage -= draw
        release = min(draw, credit)
        credit -= release
        debt += need_rao - deposit_rao - draw
    else:
        surplus = deposit_rao - min(deposit_rao, need_rao)
        repay = min(surplus, debt)
        debt -= repay
    credit += deposit_rao - credit_now
    coverage += max(0, deposit_rao - need_rao) - repay
    return DayStep(
        reserve=Reserve(credit=credit, coverage=coverage, debt=debt),
        credit_now=credit_now,
        release=release,
        draw=draw,
        repay=repay,
        index_sent=min(deposit_rao, need_rao) + draw + repay,
    )
