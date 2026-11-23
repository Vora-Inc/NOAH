"""Unsigned payment choices. Never increase a requested payment silently."""

from datetime import date

from ..scoring.fixture_io import parse_fixture
from .engine import INPUT_SCHEMA, AMENDED_INPUT_SCHEMA, evaluate, legacy_shape
from .ledger import natural
from . import ema_policy, onboarding


def advice(
    raw,
    *,
    contribution_bps=5000,
    amount_rao=None,
    include_catch_up=False,
    earnings_from=None,
    earnings_through=None,
    execution_day=None,
    switch_day=None,
):
    if raw.get("schema") not in (INPUT_SCHEMA, AMENDED_INPUT_SCHEMA, ema_policy.INPUT_SCHEMA):
        raise ValueError("points fixture required")
    natural(contribution_bps, "contribution basis points")
    if amount_rao is not None:
        natural(amount_rao, "requested amount")
    if type(include_catch_up) is not bool:
        raise ValueError("catch-up selection must be boolean")
    result = evaluate(raw)
    fx = parse_fixture(legacy_shape(raw))
    anchor_day = fx.day_epoch_ts // 86400

    def day_number(value, default):
        return (
            default
            if value is None
            else (date.fromisoformat(value) - date(1970, 1, 1)).days - anchor_day
        )

    retained_switch = raw.get(onboarding.SWITCH_FIELD)
    if switch_day is None:
        switch_day = retained_switch
    elif retained_switch is not None and retained_switch != switch_day:
        raise ValueError("deposit advice switch day differs from retained evidence")
    execution = day_number(execution_day, fx.eval_day + 1)
    if execution <= fx.eval_day:
        raise ValueError("execution precedes verified completed earnings")
    foreign_only = switch_day is not None and execution >= onboarding.validate_switch_day(switch_day)
    first = day_number(earnings_from, fx.eval_day)
    last = day_number(earnings_through, fx.eval_day)
    if first < 0 or first > last or last > fx.eval_day:
        raise ValueError("requested earnings range is outside verified completed days")
    by_uid = {r.uid: r for r in fx.miners}
    rows = []
    for row in result["miners"]:
        s = row["state"]
        miner = by_uid[row["uid"]]
        today = miner.days[fx.eval_day]
        earned = today.paid_rao + today.pay_unlocked_rao
        selected_earned = sum(
            miner.days[d].paid_rao + miner.days[d].pay_unlocked_rao
            for d in range(max(first, miner.first_activity_day), last + 1)
        )
        required = earned // 2
        debt_payment = max(0, required - int(s["balance"]))
        requested = (
            selected_earned * contribution_bps // 10000 if amount_rao is None else amount_rao
        )
        selected = max(requested, debt_payment) if include_catch_up else requested
        native = 0 if foreign_only else selected
        choices = (
            dict(requested_contribution_noah_equiv_rao=requested,
                 selected_contribution_noah_equiv_rao=selected,
                 recommended_foreign_or_tao_noah_equiv_rao=selected,
                 recommended_asset="foreign-subnet-alpha-or-root-tao",
                 noah_credit="zero")
            if foreign_only else {}
        )
        rows.append(
            dict(
                uid=row["uid"],
                coldkey=row["coldkey"],
                closed_day_earnings_rao=earned,
                selected_earnings_rao=selected_earned,
                earnings_from_day=first,
                earnings_through_day=last,
                balance_before_new_obligation_rao=int(s["balance"]),
                minimum_to_avoid_debt_rao=debt_payment,
                new_deposit_for_consistency_rao=required,
                requested_noah_deposit_rao=0 if foreign_only else requested,
                selected_noah_deposit_rao=native,
                recommended_noah_deposit_rao=native,
                catch_up_selected=include_catch_up,
                expected_balance_rao=int(s["balance"]) + native - required,
                meets_new_daily_target=native >= required,
                reason=("SN82 NOAH earns zero credit; use qualifying foreign alpha or root TAO" if foreign_only
                        else "explicit-catch-up" if include_catch_up else "operator-requested"),
                **choices,
            )
        )
    return rows
