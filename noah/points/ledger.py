"""Offline scoring candidate. All monetary inputs are integer NOAH-equivalent rao.

No network, wallet, persistence, publication, or live-validator code. Historical
foreign credits must already be authenticated and attributed by the reader.
"""

from dataclasses import dataclass, replace

Q = 10**6
R = 10**9


def natural(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def maturity(qualified):
    natural(qualified, "qualified days")
    return 0 if qualified == 0 else Q // 10 + 9 * Q * min(qualified - 1, 29) // 290


@dataclass(frozen=True)
class State:
    earned: int = 0
    contributed: int = 0
    balance: int = 0
    streak: int = 0
    qualified: int = 0
    missed: int = 0
    points: int = 0
    last_compliant: int = 0
    contribution: int = 0
    consistency: int = 0
    baseline: int = 40 * Q
    maturity: int = 0


def settle(start, earned, deposit, *, rules=None):
    """One complete UTC day. earned is yesterday's eligible earned NOAH.

    Same-day native and accepted foreign credits are summed once as deposit.
    Previously deposited credits cover obligations, but never count again as a
    new daily deposit. Entry amounts are removed by the caller before this call.
    """
    if rules is not None:
        from .onboarding import validate

        validate(rules)
    natural(earned, "earned")
    natural(deposit, "deposit")
    due = earned // 2
    total_earned = start.earned + earned
    total_deposits = start.contributed + deposit
    balance = start.balance + deposit - due
    common = dict(earned=total_earned, contributed=total_deposits, balance=balance)
    if balance < 0:
        missed = start.missed + 1
        return replace(
            start,
            **common,
            missed=missed,
            streak=0,
            points=start.last_compliant // 2 if missed == 1 else 0,
            contribution=0,
            consistency=0,
        )
    # Clearing the first genuine obligation is the starter-to-ordinary bridge.
    # No new emissions may be due after starter weight has already stopped.
    first_catchup = start.qualified == 0 and start.balance < 0 and balance >= 0 and total_earned > 0
    qualified = start.qualified + int(due > 0 or first_catchup)
    streak = (
        int(due > 0 and deposit >= due)
        if start.balance < 0
        else (start.streak + 1 if due > 0 and deposit >= due else 0 if due > 0 else start.streak)
    )
    bonus = (
        min(20 * Q, max(0, 80 * Q * total_deposits // total_earned - 40 * Q)) if total_earned else 0
    )
    if rules is not None:
        bonus = (
            min(40 * Q, max(0, 160 * Q * total_deposits // total_earned - 80 * Q))
            if total_earned
            else 0
        )
        factor = Q // 100 + 99 * Q * min(max(qualified - 1, 0), 89) ** 2 // (100 * 89**2)
        baseline = 20 * Q
    else:
        factor, baseline = maturity(qualified), 40 * Q
    consistency = 40 * Q * min(streak, 30) ** 2 // 900
    points = (baseline + bonus + consistency) * factor // Q
    return State(
        **common,
        streak=streak,
        qualified=qualified,
        missed=0,
        points=points,
        last_compliant=points,
        contribution=bonus,
        consistency=consistency,
        maturity=factor,
        baseline=baseline,
    )


def historical_foreign_credit(amount_rao, foreign_price_rao, noah_price_rao):
    """Reproduce the reader's recorded, same-block, two-floor conversion.

    TAO prices are scaled by 1e9 per whole token. Root TAO has price 1e9.
    This is historical reconciliation, NOT a manipulation-resistant live oracle.
    """
    for n, v in [
        ("amount", amount_rao),
        ("foreign price", foreign_price_rao),
        ("NOAH price", noah_price_rao),
    ]:
        natural(v, n)
    if foreign_price_rao == 0 or noah_price_rao == 0:
        raise ValueError("prices must be positive")
    tao_rao = amount_rao * foreign_price_rao // R
    return tao_rao * R // noah_price_rao


def migration_review(candidate_balance, existing_coverage, existing_debt):
    """Report needed protection; never apply or silently mint migration credit."""
    natural(existing_coverage, "existing coverage")
    natural(existing_debt, "existing debt")
    if type(candidate_balance) is not int:
        raise ValueError("candidate balance must be an integer")
    if existing_coverage and existing_debt:
        raise ValueError("inconsistent existing coverage and debt")
    topup = max(0, existing_coverage - candidate_balance) if existing_coverage else 0
    return dict(
        protection_needed_rao=topup,
        old_debt_rao=existing_debt,
        new_debt_rao=max(0, -candidate_balance),
        debt_change_rao=max(0, -candidate_balance) - existing_debt,
    )
