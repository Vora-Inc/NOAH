"""Independent integer scoring calculation for cross-checking fixture results.

This module shares no scoring implementation code with the production engine.
It expects fixture-shaped data; the comparison wrapper also invokes the strict
production parser and rejects disagreement about input validity."""

from __future__ import annotations

from math import isqrt

FP_SCALE = 1_000_000_000
U16_MAX = 65_535


# ---- integer nth root, by binary search (decay.py uses Newton iteration --
# a different method on purpose, per this module's own docstring) -----------


def _nth_root_floor(n: int, k: int) -> int:
    """Largest integer x with x**k <= n, found by binary search."""
    if n == 0:
        return 0
    lo, hi = 0, 1
    while hi**k <= n:
        hi *= 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid**k <= n:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _one_day_decay_factor(half_life_days: int) -> int:
    """DECAY[1] = floor(2^(-1/H) * 1e9) = floor((1e9^H / 2)^(1/H))."""
    return _nth_root_floor(FP_SCALE**half_life_days // 2, half_life_days)


def _decay_step(total: int, factor_fp: int) -> int:
    return total * factor_fp // FP_SCALE


# ---- the streak -------------------------------------------------------------


def _classify_day(paid_rao: int, noah_deposit_rao: int, no_fault: bool, target_fp: int) -> str:
    if paid_rao > 0:
        need = target_fp * paid_rao // FP_SCALE
        return "hit" if noah_deposit_rao >= need else "miss"
    return "neutral" if no_fault else "miss"


def _advance_run(k: int, classification: str) -> int:
    if classification == "hit":
        return k + 1
    if classification == "miss":
        return 0
    return k  # neutral


def _streak_factor(k: int, ceiling_fp: int, ramp_days: int) -> int:
    return FP_SCALE + (ceiling_fp - FP_SCALE) * min(k, ramp_days) // ramp_days


# ---- the standard's multiplier: min((pci/target)^harshness, cap) -----------
# Reimplemented as a plain repeated fixed-point multiply, with no early-exit
# clamping optimization (the engine's own version clamps mid-loop once the
# cap can no longer change; this always finishes the full power before
# comparing to the cap). Both must reach the identical floor because integer
# arithmetic is exact at every step; the two are simply different routes to
# it.


def _multiplier(pci_fp: int, target_fp: int, harshness: int, cap_fp: int) -> int:
    ratio_fp = pci_fp * FP_SCALE // target_fp
    acc_fp = FP_SCALE
    for _ in range(harshness):
        acc_fp = acc_fp * ratio_fp // FP_SCALE
    return min(acc_fp, cap_fp)


# ---- the door, the score, and pay ------------------------------------------


def _door_gate(hold_rao: int, ongoing_hold_rao: int) -> int:
    return 1 if hold_rao >= ongoing_hold_rao else 0


def _score(base_rao: int, streak_fp: int, slot_fp: int, gate_value: int) -> int:
    return (base_rao * streak_fp // FP_SCALE) * slot_fp // FP_SCALE * gate_value


def _pay_weight(score_value: int, multiplier_fp: int) -> int:
    return isqrt(score_value) * multiplier_fp // FP_SCALE


def _weights_u16(x_by_uid: dict) -> dict:
    """Largest-remainder u16 quantization, ties to the lowest uid -- the
    exact tie-break the spec names, reimplemented independently (a manual
    running-residual pass, rather than pay.py's own sort-by-remainder-then-
    slice approach) but required to reach the SAME numbers, because the
    rule itself, not the algorithm shape, is what the spec pins."""
    total = sum(x_by_uid.values())
    if total == 0:
        return {}
    uids_in_order = sorted(x_by_uid)
    base = {}
    remainder = {}
    for uid in uids_in_order:
        product = x_by_uid[uid] * U16_MAX
        base[uid] = product // total
        remainder[uid] = product % total
    assigned = sum(base.values())
    residual = U16_MAX - assigned
    # Largest remainder first; equal remainders go to the lowest uid, which
    # `uids_in_order` (already ascending) combined with a stable sort on
    # remainder alone already guarantees.
    ranking = sorted(uids_in_order, key=lambda u: remainder[u], reverse=True)
    for uid in ranking[:residual]:
        base[uid] += 1
    assert sum(base.values()) == U16_MAX
    return base


# ---- one miner, replayed day by day, from raw fixture JSON -----------------


def _int_of(value) -> int:
    """A contract integer: a plain int or a canonical decimal string."""
    return int(value)


def _score_miner_by_hand(miner: dict, dials: dict, eval_day: int) -> dict:
    half_life_days = _int_of(dials["halfLifeDays"])
    target_fp = _int_of(dials["targetFp"])
    harshness = int(dials["harshness"])
    cap_fp = _int_of(dials["capFp"])
    streak_ceiling_fp = _int_of(dials["streakCeilingFp"])
    streak_ramp_days = _int_of(dials["streakRampDays"])
    ongoing_hold_rao = _int_of(dials["ongoingHoldRao"])

    d1 = _one_day_decay_factor(half_life_days)

    first_activity_day = _int_of(miner["firstActivityDay"])
    registration_day = _int_of(miner["registrationDay"])
    entry_deposit_rao = _int_of(miner["entryDepositRao"])
    hold_rao = _int_of(miner["holdRao"])

    n = f = s = pd = 0
    total_regular = total_observed_prior_pay = 0
    run = 0
    credit_ledger = 0  # deposit not yet credited to n
    coverage_ledger = 0  # deposit above a day's need, not yet claimed by debt
    debt_ledger = 0  # a day's need left unmet, repaid by a LATER day's surplus
    debt_weight = 0
    noah_credit = foreign_credit = 0
    first_spendable_pay_day = None

    for day in range(first_activity_day, eval_day + 1):
        entry = miner["days"][str(day)]
        paid_rao = _int_of(entry["paidRao"])
        pay_unlocked_rao = _int_of(entry["payUnlockedRao"])
        noah_deposit_rao = _int_of(entry["noahDepositRao"])
        foreign_deposit_noah_equiv_rao = _int_of(entry["foreignDepositNoahEquivRao"])

        n = _decay_step(n, d1)
        f = _decay_step(f, d1)
        s = _decay_step(s, d1)
        pd = _decay_step(pd, d1)
        debt_weight = _decay_step(debt_weight, d1)

        # This day's OWN spendable pay -- used only to find this seat's own
        # first-ever spendable pay day (starting treatment), unshifted.
        spendable_pay = paid_rao + pay_unlocked_rao
        if first_spendable_pay_day is None and spendable_pay > 0:
            first_spendable_pay_day = day

        prior_day_key = str(day - 1)
        if prior_day_key in miner["days"] and day - 1 >= first_activity_day:
            prior_entry = miner["days"][prior_day_key]
            prior_spendable_pay = _int_of(prior_entry["paidRao"]) + _int_of(
                prior_entry["payUnlockedRao"]
            )
            prior_no_fault = bool(prior_entry["noFault"])
        else:
            prior_spendable_pay = 0
            prior_no_fault = True

        total_regular += noah_deposit_rao
        total_observed_prior_pay += prior_spendable_pay

        # A day's need, in one place: floor(target x prior spendable pay).
        need_rao = target_fp * prior_spendable_pay // FP_SCALE
        noah_credit = min(noah_deposit_rao, prior_spendable_pay)
        draw = repay = 0
        debt_before = debt_ledger
        if prior_spendable_pay > 0 and noah_deposit_rao < need_rao:
            draw = min(coverage_ledger, need_rao - noah_deposit_rao)
            coverage_ledger -= draw
            released = min(draw, credit_ledger)
            credit_ledger -= released
            noah_credit += released
            debt_ledger += need_rao - noah_deposit_rao - draw
        else:
            # Today's own need is already met by the deposit alone (or there
            # is none). The surplus above it repays any outstanding debt
            # from an earlier shortfall day FIRST, before any of it is
            # banked as coverage for a still-later day.
            surplus = noah_deposit_rao - min(noah_deposit_rao, need_rao)
            repay = min(surplus, debt_ledger)
            debt_ledger -= repay
        credit_ledger += noah_deposit_rao - min(noah_deposit_rao, prior_spendable_pay)
        coverage_ledger += max(0, noah_deposit_rao - need_rao) - repay
        debt_weight += max(0, debt_ledger - debt_before)
        credited_repay = (debt_weight * repay // debt_before) if debt_before > 0 else 0
        debt_weight -= credited_repay
        n += noah_credit
        if day == registration_day:
            n += entry_deposit_rao
            noah_credit += entry_deposit_rao

        room = n - f
        foreign_credit = min(foreign_deposit_noah_equiv_rao, room)
        f += foreign_credit
        s += min(noah_deposit_rao, need_rao) + draw + credited_repay
        pd += prior_spendable_pay

        classification = _classify_day(
            prior_spendable_pay, noah_deposit_rao + draw, prior_no_fault, target_fp
        )
        run = _advance_run(run, classification)

    base_fp = n + f
    gate_value = _door_gate(hold_rao, ongoing_hold_rao)
    pci_fp = (s * FP_SCALE // pd) if pd > 0 else 0

    if cap_fp > FP_SCALE and debt_ledger == 0 and coverage_ledger > 0:
        if total_observed_prior_pay > 0:
            surplus_ratio = total_regular * FP_SCALE // total_observed_prior_pay
            if surplus_ratio > target_fp:
                pci_fp = max(pci_fp, min(surplus_ratio, (1 << 64) - 1))

    in_starting_treatment = first_spendable_pay_day is None or eval_day <= first_spendable_pay_day
    if in_starting_treatment:
        mult_fp = FP_SCALE
    else:
        mult_fp = _multiplier(pci_fp, target_fp, harshness, cap_fp)

    streak_fp_value = _streak_factor(run, streak_ceiling_fp, streak_ramp_days)
    score_fp = _score(base_fp, streak_fp_value, FP_SCALE, gate_value)
    pay_weight_fp = _pay_weight(score_fp, mult_fp)

    uid = miner["uid"]

    return {
        "coldkey": miner["coldkey"],
        "uid": uid,
        "doorOpen": bool(gate_value),
        "pciFp": str(pci_fp),
        "multiplierFp": str(mult_fp),
        "noahCreditRao": str(noah_credit),
        "foreignCreditRao": str(foreign_credit),
        "baseFp": str(base_fp),
        "streakFp": str(streak_fp_value),
        "scoreFp": str(score_fp),
        "payWeightFp": str(pay_weight_fp),
        "_payWeightFpInt": pay_weight_fp,
    }


def _output_order(result: dict) -> tuple:
    if result["uid"] is None:
        return (1, 0, result["coldkey"])
    return (0, result["uid"], result["coldkey"])


def score_fixture_by_hand(raw_fixture: dict) -> dict:
    """The full, independent, ``noah-score-output/6``-shaped hand
    calculation for one fixture, read straight from its raw JSON dict (no
    ``noah.scoring.fixture_io`` involved)."""
    eval_day = _int_of(raw_fixture["evalDay"])
    dials = raw_fixture["dials"]

    results = sorted(
        (_score_miner_by_hand(m, dials, eval_day) for m in raw_fixture["miners"]),
        key=_output_order,
    )

    x_by_uid = {r["uid"]: r["_payWeightFpInt"] for r in results if r["uid"] is not None}
    weights = _weights_u16(x_by_uid)

    return {
        "schema": "noah-score-output/6",
        "fixture": raw_fixture["name"],
        "evalDay": str(eval_day),
        "miners": [{k: v for k, v in r.items() if not k.startswith("_")} for r in results],
        "weightsU16": {str(uid): w for uid, w in weights.items()},
    }
