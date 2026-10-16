"""Score daily participation from a validated fixture.

Process days in order with fixed-point decay, prior-day obligations, prepaid
coverage and catch-up debt. Entry contribution is applied on registration day;
foreign credit is bounded by accumulated NOAH credit. The holding gate, streak
and contribution multiplier determine pay weight before deterministic u16
normalization. The engine does not fetch chain state or sign transactions."""

from __future__ import annotations

from dataclasses import dataclass

from ..standard.multiplier import multiplier
from .constants import FP_SCALE, SLOT_FP
from .decay import decay_step, one_day_factor_fp
from .fixture_io import SCHEMA_OUTPUT, Dials, Fixture, MinerInput
from .pay import pay_weight, weights_u16
from .reserve import Reserve, need_for_day, reserve_step
from .score import gate as door_gate
from .score import score as score_fn
from .streak import advance_run, classify_day
from .streak import streak_fp as streak_fp_fn


@dataclass(frozen=True)
class MinerResult:
    coldkey: str
    uid: int | None
    door_open: bool
    pci_fp: int
    multiplier_fp: int
    noah_credit_rao: int
    foreign_credit_rao: int
    base_fp: int
    streak_fp: int
    score_fp: int
    pay_weight_fp: int
    credit_reserve_rao: int = 0
    coverage_reserve_rao: int = 0
    debt_rao: int = 0
    debt_weight_rao: int = 0


def score_miner(m: MinerInput, dials: Dials, eval_day: int) -> MinerResult:
    d1 = one_day_factor_fp(dials.half_life_days)

    n = f = s = pd = 0  # N, F (credit) and S, Pd (the index's two legs)
    run = 0
    reserve = Reserve()
    debt_weight = 0
    noah_credit = foreign_credit = 0
    first_spendable_pay_day = None

    for d in range(m.first_activity_day, eval_day + 1):
        dd = m.days[d]  # every day present, guaranteed by the parser
        # Decay one day, then add, identically for the base credit and for
        # both legs of the index.
        n = decay_step(n, d1)
        f = decay_step(f, d1)
        s = decay_step(s, d1)
        pd = decay_step(pd, d1)
        debt_weight = decay_step(debt_weight, d1)
        # The day's pay, everywhere below, is the SPENDABLE pay: what
        # arrived spendable plus what unlocked this day. Pay that arrived
        # locked (paidLockedArrivalRao) touches no total until its unlock
        # shows up in a later day's payUnlockedRao. A token that could not
        # have been deposited cannot cap what may be deposited, and it does
        # not belong in the index's denominator yet.
        spendable_pay = dd.paid_rao + dd.pay_unlocked_rao
        if first_spendable_pay_day is None and spendable_pay > 0:
            first_spendable_pay_day = d
        prior_day = m.days.get(d - 1)
        if prior_day is None:
            # No day before `first_activity_day` exists in this fixture --
            # this is `d == first_activity_day`, with no real prior day to
            # compare against yet. Treated as zero pay, and as no-fault
            # (NEUTRAL rather than MISS): a miner cannot be faulted on a
            # comparison day with no real history behind it at all, the
            # same reasoning starting treatment already applies to this
            # exact gap.
            prior_spendable_pay = 0
            prior_no_fault = True
        else:
            prior_spendable_pay = prior_day.paid_rao + prior_day.pay_unlocked_rao
            prior_no_fault = prior_day.no_fault
        debt_before = reserve.debt
        step = reserve_step(
            reserve,
            dd.noah_deposit_rao,
            prior_spendable_pay,
            need_for_day(dials, prior_spendable_pay),
        )
        reserve = step.reserve
        debt_weight += max(0, reserve.debt - debt_before)
        credited_repay = (debt_weight * step.repay // debt_before) if debt_before > 0 else 0
        debt_weight -= credited_repay
        assert debt_weight <= reserve.debt, "invariant violated: debt_weight exceeded raw debt"
        # The immediate credit is unchanged (min(deposit, prior pay)); what
        # a shortfall day releases from the credit reserve joins it.
        noah_credit = step.credit_now + step.release
        n += noah_credit
        if d == m.registration_day:
            n += m.entry_deposit_rao
            noah_credit += m.entry_deposit_rao
        room = n - f
        assert room >= 0, "invariant violated: F exceeded N"
        # Foreign matching is unaffected by the noon-schedule pairing above:
        # it matches room opened by own-token credit, same day, as before.
        foreign_credit = min(dd.foreign_deposit_noah_equiv_rao, room)
        f += foreign_credit
        s += step.index_sent - step.repay + credited_repay
        pd += prior_spendable_pay
        run = advance_run(
            run,
            classify_day(
                prior_spendable_pay,
                dd.noah_deposit_rao + step.draw,
                prior_no_fault,
                dials.target_fp,
            ),
        )

    base = n + f
    g = door_gate(m.hold_rao, dials.ongoing_hold_rao)
    pci_fp = s * FP_SCALE // pd if pd > 0 else 0

    in_starting_treatment = (
        first_spendable_pay_day is None or eval_day <= first_spendable_pay_day
    )
    if in_starting_treatment:
        mult_fp = FP_SCALE
    else:
        mult_fp = multiplier(pci_fp, dials.target_fp, dials.harshness, dials.cap_fp)

    sfp = streak_fp_fn(run, dials.streak_ceiling_fp, dials.streak_ramp_days)
    sc = score_fn(base, sfp, SLOT_FP, g)
    x = pay_weight(sc, mult_fp)

    return MinerResult(
        coldkey=m.coldkey,
        uid=m.uid,
        door_open=bool(g),
        pci_fp=pci_fp,
        multiplier_fp=mult_fp,
        noah_credit_rao=noah_credit,
        foreign_credit_rao=foreign_credit,
        base_fp=base,
        streak_fp=sfp,
        score_fp=sc,
        pay_weight_fp=x,
        credit_reserve_rao=reserve.credit,
        coverage_reserve_rao=reserve.coverage,
        debt_rao=reserve.debt,
        debt_weight_rao=debt_weight,
    )


def _output_order(r: MinerResult) -> tuple:
    """Ascending uid, null uids last by coldkey."""
    if r.uid is None:
        return (1, 0, r.coldkey)
    return (0, r.uid, r.coldkey)


def score_fixture(fx: Fixture) -> dict:
    """Compute one ``noah-score-output/6`` object for a parsed fixture."""
    results = sorted(
        (score_miner(m, fx.dials, fx.eval_day) for m in fx.miners),
        key=_output_order,
    )
    x_by_uid = {r.uid: r.pay_weight_fp for r in results if r.uid is not None}
    weights = weights_u16(x_by_uid)
    return {
        "schema": SCHEMA_OUTPUT,
        "fixture": fx.name,
        "evalDay": str(fx.eval_day),
        "miners": [
            {
                "coldkey": r.coldkey,
                "uid": r.uid,
                "doorOpen": r.door_open,
                "pciFp": str(r.pci_fp),
                "multiplierFp": str(r.multiplier_fp),
                "noahCreditRao": str(r.noah_credit_rao),
                "foreignCreditRao": str(r.foreign_credit_rao),
                "baseFp": str(r.base_fp),
                "streakFp": str(r.streak_fp),
                "scoreFp": str(r.score_fp),
                "payWeightFp": str(r.pay_weight_fp),
            }
            for r in results
        ],
        "weightsU16": {str(uid): w for uid, w in weights.items()},
    }
