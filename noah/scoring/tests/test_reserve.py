"""The prepaid contribution reserve (#290, Drew's answer on #305), plus the
catch-up debt ledger required by #282's approved launch rule 3 (2026-09-29,
verbatim): "A catch-up contribution must satisfy earlier uncovered
obligations before being treated exclusively as coverage for future
earnings." (#290 tracks the defect this closed; the originally reported
numbers -- a catch-up miner scoring roughly a quarter of an otherwise
identical regular one -- are the ``test_drew_290_*`` regression below.)

Two layers. ``reserve_step`` is tested directly, in exact integers with no
decay, because that is where the conservation identities are exact. The
engine is then tested end to end (real decay, real classification) on
hand-derived sequences and on seeded random ones.

The conservation identities, for any sequence of days (entry deposit
excluded), with ``credited = sum(credit_now + release)`` and
``S = sum(index_sent)``, ``N_total = sum(need_for_day(pay))`` over days with
``pay > 0``::

    credited + credit_reserve == total deposited   (nothing lost, none twice)
    S        + coverage_reserve == total deposited   (an index NOAH counts once)
    S        + debt             == N_total            (new: #282 rule 3)
    min(coverage_reserve, debt) == 0                  (new: never both positive)
    => S == min(total deposited, N_total), always, regardless of day order

Engine-level note on decay: the three identities above are exact only for
the UNDECAYED per-day ledgers ``reserve_step`` itself produces; they are
unchanged by (and hold before and after) the round-2 fix below, since that
fix touches only how the engine feeds a ``repay`` into its own decayed
``s``, never ``reserve_step`` or ``reserve.debt`` itself.

#290 round two (Drew found this by hand, 2026-09-29, a longer-delay,
realistic scenario the short example above did not cover): crediting a
``repay`` to the engine's decayed ``s`` at ITS OWN day's fresh, undecayed
weight -- which the first #290 fix did, matching how ``draw`` already
works -- is wrong for ``repay`` specifically, because ``draw`` and ``repay``
are not actually symmetric. ``draw`` always lands on the exact day the need
it satisfies arose (a shortfall day drawing on an already-banked earlier
surplus), so crediting it fresh, undiscounted, on that day is already
time-aligned with ``pd``'s own decay of that same day's need -- correct,
unchanged, still true. ``repay`` lands on a LATER day than the debt it
repays was incurred, so crediting it fresh at the repaying day's weight
implicitly re-dates old, already-decayed need back to full freshness, which
can push decayed PCI arbitrarily far above target the longer the delay
runs (unlike the bounded, single-day case originally examined) --
``noah/scoring/tests/test_mainnet_scenarios.py::
test_drew_290_round_2_long_delayed_catchup_no_longer_inflates_pci``
reproduces Drew's exact real-numbers repro (a 61-day gap pushing PCI to
0.63 against a 0.5 target, full 1.0 multiplier, before this fix).
``multiplier()`` capping at ``capFp`` (``noah/standard/multiplier.py``'s
``ratio >= cap`` branch) does NOT save this in general: once PCI clears
target the ratio is >= 1.0 and the multiplier hits the cap directly, so an
inflated-past-target PCI reaches the actual multiplier a miner is paid
whenever ``capFp`` is at or below where that inflated PCI lands -- exactly
the committed m1 profile's own ``capFp`` (1.0), the same one this file's
fixtures already use, which is why round two is a real, not theoretical,
production bug.

The fix (``engine.py``'s ``debt_weight``, a decaying mirror of
``reserve.debt`` kept only in the engine, since it needs the decay factor
``reserve.py`` deliberately does not have): credit a repay to ``s`` at debt's
own decayed weight, not its raw value, restoring the invariant PCI can never
exceed target -- see ``test_a_catchup_repay_is_capped_by_the_debts_own_decay``
and ``test_engine_invariants_over_random_sequences_with_a_large_half_life``,
both extended to check ``pci_fp <= target_fp`` directly, not just via the
capped multiplier.
"""

import random

import pytest

from noah.scoring.constants import FP_SCALE
from noah.scoring.engine import score_miner
from noah.scoring.fixture_io import Dials, parse_fixture
from noah.scoring.reserve import Reserve, need_for_day, reserve_step
from noah.scoring.streak import HIT, MISS, classify_day

N = 10**9  # one NOAH in rao
TARGET = 900_000_000  # 0.9

# --- exact, undecayed: reserve_step ----------------------------------------


def _dials(target_fp=TARGET, half_life="1000"):
    return Dials(
        target_fp=target_fp,
        harshness=2,
        cap_fp=FP_SCALE,
        half_life_days=int(half_life),
        streak_ceiling_fp=2 * FP_SCALE,
        streak_ramp_days=90,
        ongoing_hold_rao=0,
    )


def test_need_for_day_is_the_need_classify_day_uses():
    rng = random.Random(1)
    for _ in range(500):
        target = rng.randint(1, FP_SCALE)
        pay = rng.randint(1, 10**15)
        need = need_for_day(_dials(target_fp=target), pay)
        assert need == target * pay // FP_SCALE
        assert classify_day(pay, need, False, target) == HIT
        if need > 0:
            assert classify_day(pay, need - 1, False, target) == MISS


def test_ordinary_day_deposits_exactly_the_need():
    step = reserve_step(Reserve(), 90, 100, 90)
    assert (step.credit_now, step.release, step.draw, step.index_sent) == (90, 0, 0, 90)
    assert step.reserve == Reserve(0, 0)


def test_depositing_the_whole_pay_banks_only_the_excess_over_need():
    step = reserve_step(Reserve(), 100, 100, 90)
    assert (step.credit_now, step.release, step.draw, step.index_sent) == (100, 0, 0, 90)
    assert step.reserve == Reserve(credit=0, coverage=10)


def test_skipping_a_day_with_no_reserve_draws_nothing():
    step = reserve_step(Reserve(), 0, 100, 90)
    assert (step.credit_now, step.release, step.draw, step.index_sent) == (0, 0, 0, 0)
    # Before #282 rule 3 this shortfall was simply gone: reserve stayed
    # (0, 0). Now the 90 unmet is tracked as debt, for a later day's
    # surplus to repay before it can bank as future coverage.
    assert step.reserve == Reserve(credit=0, coverage=0, debt=90)


def test_reserve_short_by_ten_draws_all_it_has():
    # Need 90, reserve 80: the whole 80 is drawn, 10 short. The caller then
    # classifies D + draw = 80 < 90, a MISS -- unaffected by the new debt
    # ledger, which only ever repays retroactively into the index, never
    # reclassifies a day's own HIT/MISS (#282: streak stays distinguished
    # from compliance).
    step = reserve_step(Reserve(credit=80, coverage=80), 0, 100, 90)
    assert (step.draw, step.release, step.repay, step.index_sent) == (80, 80, 0, 80)
    # The 10 still unmet becomes debt, for a still-later surplus to repay.
    assert step.reserve == Reserve(credit=0, coverage=0, debt=10)
    assert classify_day(100, 0 + step.draw, False, TARGET) == MISS


def test_partial_deposit_draws_only_the_gap():
    step = reserve_step(Reserve(credit=500, coverage=500), 50, 100, 90)
    assert step.credit_now == 50
    assert (step.draw, step.release, step.index_sent) == (40, 40, 90)
    assert step.reserve == Reserve(credit=460, coverage=460)


def test_pay_zero_day_banks_the_whole_deposit_and_counts_nothing_now():
    step = reserve_step(Reserve(), 50, 0, 0)
    assert (step.credit_now, step.release, step.draw, step.index_sent) == (0, 0, 0, 0)
    assert step.reserve == Reserve(credit=50, coverage=50)


def test_pay_zero_day_never_draws_even_with_a_reserve():
    step = reserve_step(Reserve(credit=70, coverage=70), 0, 0, 0)
    assert (step.credit_now, step.release, step.draw, step.index_sent) == (0, 0, 0, 0)
    assert step.reserve == Reserve(70, 70)


def test_a_days_own_deposit_is_used_or_banked_never_both():
    # 95 against a need of 90: 90 counts now, only the 5 above it is banked.
    step = reserve_step(Reserve(), 95, 100, 90)
    assert step.index_sent + step.reserve.coverage == 95
    assert step.credit_now + step.release + step.reserve.credit == 95


def test_prepaid_above_pay_waits_in_the_credit_reserve():
    step = reserve_step(Reserve(), 1800, 100, 90)
    assert step.credit_now == 100
    assert step.reserve == Reserve(credit=1700, coverage=1710)


def test_negative_inputs_are_refused():
    for bad in ((-1, 0, 0), (0, -1, 0), (0, 0, -1)):
        with pytest.raises(ValueError):
            reserve_step(Reserve(), *bad)


# --- exact, undecayed: the catch-up debt ledger (#282 rule 3) --------------


def test_a_later_surplus_repays_an_earlier_debt_before_banking():
    # Day 1: shortfall of 90, nothing to draw on yet -> debt 90.
    d1 = reserve_step(Reserve(), 0, 100, 90)
    assert d1.reserve == Reserve(credit=0, coverage=0, debt=90)
    # Day 2: a 200 deposit against a 90 need. The 110 surplus repays the 90
    # debt FIRST; only the remaining 20 banks as coverage for a later day.
    d2 = reserve_step(d1.reserve, 200, 100, 90)
    assert d2.repay == 90
    assert d2.reserve == Reserve(credit=d2.reserve.credit, coverage=20, debt=0)
    # The index gained the day's own need plus the repay: 90 + 90.
    assert d2.index_sent == 180


def test_a_zero_pay_catchup_day_can_still_repay_old_debt():
    # Day 1: shortfall of 90 -> debt 90, exactly Drew's #290 repro (day 3
    # is a zero-matched-pay day and the whole deposit is still available to
    # repay day 1's debt, not just bank uselessly).
    d1 = reserve_step(Reserve(), 0, 100, 90)
    step = reserve_step(d1.reserve, 100, 0, 0)  # pay/need both 0
    assert (step.credit_now, step.draw) == (0, 0)
    assert step.repay == 90
    # The 90 repays the debt; the remaining 10 of the 100 deposit banks as
    # ordinary coverage, same as it would with no debt outstanding.
    assert step.reserve == Reserve(credit=100, coverage=10, debt=0)
    assert step.index_sent == 90  # min(D, 0) + draw(0) + repay(90)


def test_repay_never_exceeds_the_outstanding_debt():
    d1 = reserve_step(Reserve(), 0, 100, 90)  # debt 90
    step = reserve_step(d1.reserve, 500, 100, 90)  # surplus 410, debt only 90
    assert step.repay == 90
    assert step.reserve.debt == 0
    assert step.reserve.coverage == 410 - 90


def test_a_shortfall_day_still_draws_the_existing_coverage_reserve_first():
    # The pre-existing forward mechanism (an earlier surplus covering a
    # later shortfall) is untouched: no debt is created when the existing
    # coverage reserve can pay the whole gap.
    step = reserve_step(Reserve(coverage=90, credit=90), 0, 100, 90)
    assert (step.draw, step.reserve.debt) == (90, 0)


def test_coverage_and_debt_are_never_both_positive():
    # A single day's surplus always drains whichever of coverage/debt
    # applies before banking into the other -- proved generally by
    # test_coverage_and_debt_stay_mutually_exclusive_over_random_sequences
    # below; this pins the two smallest hand cases.
    shortfall_then_nothing = reserve_step(Reserve(), 0, 100, 90)
    assert shortfall_then_nothing.reserve.coverage == 0
    assert shortfall_then_nothing.reserve.debt == 90
    surplus_then_nothing = reserve_step(Reserve(), 100, 100, 90)
    assert surplus_then_nothing.reserve.coverage == 10
    assert surplus_then_nothing.reserve.debt == 0


def test_repay_never_changes_a_days_own_classification():
    # repay only ever fires when today's own deposit already meets or
    # exceeds today's own need (an existing HIT on classify_day's terms);
    # confirm classify_day agrees regardless of what repay does.
    d1 = reserve_step(Reserve(), 0, 100, 90)  # debt 90, a MISS day
    step = reserve_step(d1.reserve, 200, 100, 90)  # repays 90, still a HIT
    assert classify_day(100, 200, False, TARGET) == HIT
    assert step.repay == 90


def _random_days(rng, length):
    days = []
    for _ in range(length):
        pay = 0 if rng.random() < 0.15 else rng.randint(1, 200) * N
        kind = rng.random()
        if kind < 0.3:
            dep = 0
        elif kind < 0.55:
            dep = need_for_day(_dials(), pay)
        elif kind < 0.8:
            dep = rng.randint(0, 3 * max(pay, N))
        else:
            dep = rng.randint(0, 3000) * N  # a large prepayment
        days.append((dep, pay))
    return days


@pytest.mark.parametrize("seed", range(300))
def test_conservation_identities_hold_over_random_sequences(seed):
    rng = random.Random(seed)
    target = rng.choice([TARGET, 500_000_000, FP_SCALE, 333_333_333])
    dials = _dials(target_fp=target)
    reserve = Reserve()
    total_dep = credited = s = total_need = 0
    for dep, pay in _random_days(rng, rng.randint(1, 60)):
        need = need_for_day(dials, pay)
        step = reserve_step(reserve, dep, pay, need)
        reserve = step.reserve
        total_dep += dep
        total_need += need if pay > 0 else 0
        credited += step.credit_now + step.release
        s += step.index_sent
        # Reserves never negative; nothing released or drawn that was not held.
        assert reserve.credit >= 0 and reserve.coverage >= 0 and reserve.debt >= 0
        assert 0 <= step.release <= step.draw
        assert step.repay >= 0
        # Nothing lost, nothing counted twice.
        assert credited + reserve.credit == total_dep
        assert s + reserve.coverage == total_dep
        # #282 rule 3: the index also never loses track of unmet need --
        # what has not yet satisfied the index sits in debt instead.
        assert s + reserve.debt == total_need
        # The two forward/backward ledgers are never both positive: a
        # single day's surplus always drains one before banking the other.
        assert reserve.coverage == 0 or reserve.debt == 0
        # So: credited and S never exceed total deposited, and S never
        # exceeds total need accrued so far either -- combined with the
        # identity above, S == min(total deposited, total need accrued).
        assert credited <= total_dep
        assert s <= total_dep
        assert s <= total_need
        assert s == min(total_dep, total_need)
        # A draw only ever happens on a shortfall day with pay.
        if step.draw:
            assert pay > 0 and dep < need
        # A repay only ever happens when today's own need is already met.
        if step.repay:
            assert dep >= need


@pytest.mark.parametrize("seed", range(300))
def test_no_miss_while_the_coverage_reserve_can_pay_the_gap(seed):
    rng = random.Random(10_000 + seed)
    dials = _dials(target_fp=rng.choice([TARGET, 500_000_000, FP_SCALE]))
    reserve = Reserve()
    for dep, pay in _random_days(rng, 60):
        need = need_for_day(dials, pay)
        before = reserve.coverage
        step = reserve_step(reserve, dep, pay, need)
        reserve = step.reserve
        if pay > 0 and dep + step.draw < need:
            # A miss happened, so the coverage reserve was empty afterwards:
            # a miss is never charged to a miner who still had coverage.
            assert reserve.coverage == 0
            assert before == step.draw


# --- engine: real decay, real classification -------------------------------


def _fixture(deposits, pays, *, target=TARGET, half_life=45, entry=0, no_fault=(), first=1,
             ceiling=FP_SCALE * 101, ramp=100):
    """One miner, days ``first`` .. ``first + len - 1``. ``deposits[i]`` and
    ``pays[i]`` are in rao for day ``first + i``. The streak dials are chosen
    so the run length is read straight back: ceiling = 1 + 100, ramp = 100
    days gives streak = 1 + run exactly (up to a run of 100)."""
    days = {}
    for i, (dep, pay) in enumerate(zip(deposits, pays)):
        days[str(first + i)] = {
            "foreignDepositNoahEquivRao": "0",
            "noFault": (first + i) in no_fault,
            "noahDepositRao": str(dep),
            "paidLockedArrivalRao": "0",
            "paidRao": str(pay),
            "payUnlockedRao": "0",
        }
    last = first + len(deposits) - 1
    return {
        "schema": "noah-fixture/6",
        "name": "prepaid-test",
        "dayEpochTs": "1785715200",
        "evalDay": str(last),
        "dials": {
            "capFp": str(FP_SCALE),
            "halfLifeDays": str(half_life),
            "harshness": 2,
            "ongoingHoldRao": "0",
            "streakCeilingFp": str(ceiling),
            "streakRampDays": str(ramp),
            "targetFp": str(target),
        },
        "miners": [
            {
                "coldkey": "5Miner",
                "uid": 0,
                "firstActivityDay": str(first),
                "registrationDay": str(first),
                "entryDepositRao": str(entry),
                "holdRao": "0",
                "days": days,
            }
        ],
    }


def _score(raw, eval_day=None):
    fx = parse_fixture(raw)
    m = fx.miners[0]
    return score_miner(m, fx.dials, fx.eval_day if eval_day is None else eval_day)


def _run(result):
    """The streak run length, read back from the streak factor."""
    assert (result.streak_fp - FP_SCALE) % FP_SCALE == 0
    return (result.streak_fp - FP_SCALE) // FP_SCALE


# Miner P (prepaid) against miner O (ordinary): the same 100 NOAH pay every
# day and, by day 21, the same 1,800 NOAH deposited in total. verification/
# prepaid_repro.py prints the same table. m1-revised-v1 style dials, half-life
# 45, target 0.9.
PAY = 100 * N
NEED = 90 * N
LAST = 45


def _p_and_o():
    pays = [PAY] * LAST
    o = [0] + [NEED] * (LAST - 1)
    p = [0, 20 * NEED] + [0] * 19 + [NEED] * (LAST - 21)
    # prepaid_repro.py's dials: streak ceiling 2.0 over a 90-day ramp.
    kw = dict(entry=10 * N, ceiling=2 * FP_SCALE, ramp=90)
    return (
        _fixture(p, pays, **kw),
        _fixture(o, pays, **kw),
    )


# Task Lead's expected values (score in NOAH-equivalents to one decimal).
_P_SCORES = {3: 202.6, 10: 857.7, 21: 1913.9, 22: 2012.8, 30: 2810.3, 45: 4320.2}
_O_SCORES = {3: 192.5, 10: 848.0, 21: 1917.0, 22: 2015.9, 30: 2813.2, 45: 4322.8}


@pytest.mark.parametrize("day", sorted(_P_SCORES))
def test_prepaid_miner_p_scores_match_the_expected_table(day):
    p, o = _p_and_o()
    rp, ro = _score(p, day), _score(o, day)
    assert round(rp.score_fp / FP_SCALE, 1) == _P_SCORES[day]
    assert round(ro.score_fp / FP_SCALE, 1) == _O_SCORES[day]
    # Never penalised for the prepaid days: full multiplier, at target.
    assert rp.pci_fp == TARGET and rp.multiplier_fp == FP_SCALE
    assert ro.pci_fp == TARGET and ro.multiplier_fp == FP_SCALE


def test_prepaid_miner_p_keeps_its_streak_through_the_silent_days():
    p, o = _p_and_o()
    for day, streak in ((3, 1.022), (10, 1.100), (21, 1.222)):
        rp = _score(p, day)
        assert round(rp.streak_fp / FP_SCALE, 3) == streak
        assert _score(o, day).streak_fp == rp.streak_fp  # same run as ordinary O


def test_prepaid_miner_p_ledger_by_day_21():
    p, _ = _p_and_o()
    r = _score(p, 21)
    # 1,800 deposited (entry excluded), all of it credited, none left over.
    assert r.credit_reserve_rao == 0
    assert r.coverage_reserve_rao == 0
    # Day 2's 1,800 is credited in full by day 21: 100 the same day, 90 a
    # day from the reserve on days 3..20, 80 on day 21 (the last of it).
    credited = sum(_score(p, d).noah_credit_rao for d in range(2, 22))
    assert credited == 1800 * N


def test_prepaid_miner_p_ledger_mid_run():
    p, _ = _p_and_o()
    r = _score(p, 10)
    # Day 2 deposit 1800: coverage 1800 - 90 = 1710, drawn 90 on each of
    # days 3..10 (8 days). Credit reserve 1700 less 8 x 90 released.
    assert r.coverage_reserve_rao == 1710 * N - 8 * NEED
    assert r.credit_reserve_rao == 1700 * N - 8 * NEED


def test_miner_o_ordinary_is_untouched_by_the_reserve():
    _, o = _p_and_o()
    r = _score(o, 30)
    assert r.credit_reserve_rao == 0 and r.coverage_reserve_rao == 0


def test_miner_depositing_the_whole_pay_daily_banks_the_excess_and_keeps_pci_at_target():
    n = 30
    raw = _fixture([0] + [PAY] * (n - 1), [PAY] * n)
    r = _score(raw)
    # Each day banks 10 above the 90 needed: 29 days.
    assert r.coverage_reserve_rao == 29 * 10 * N
    assert r.credit_reserve_rao == 0
    # The index counts only the need: PCI is the target, not 1.0.
    assert r.pci_fp == TARGET
    assert r.multiplier_fp == FP_SCALE
    assert _run(r) == n - 1


def test_skipping_a_day_with_no_reserve_is_still_a_miss():
    # Day 2 and 3 pay the need; day 4 deposits nothing and nothing is banked.
    deps = [0, NEED, NEED, 0, NEED]
    pays = [PAY] * 5
    assert _run(_score(_fixture(deps, pays), 3)) == 2
    assert _run(_score(_fixture(deps, pays), 4)) == 0
    assert _run(_score(_fixture(deps, pays), 5)) == 1


def test_reserve_short_by_ten_draws_then_misses():
    # Day 2 deposits 90 + 80 = 170: need 90 met, 80 banked. Day 3 deposits
    # nothing: the 80 is drawn but the need is 90, a miss. Day 4 misses too.
    deps = [0, NEED + 80 * N, 0, 0]
    pays = [PAY] * 4
    r2, r3, r4 = (_score(_fixture(deps, pays), d) for d in (2, 3, 4))
    assert _run(r2) == 1
    assert r2.coverage_reserve_rao == 80 * N
    assert _run(r3) == 0  # 80 drawn against a need of 90: still a miss
    assert r3.coverage_reserve_rao == 0
    assert _run(r4) == 0
    assert r4.coverage_reserve_rao == 0


def test_a_pay_zero_day_banks_its_deposit_and_is_neutral_when_no_fault():
    # Day 1 has no pay. A day-1 deposit is banked (not scored, not credited
    # now) and covers day 2 when day 2's own deposit is missing.
    deps = [NEED, 0, 0]
    pays = [0, PAY, PAY]
    r1 = _score(_fixture(deps, pays), 1)
    assert r1.noah_credit_rao == 0
    assert (r1.credit_reserve_rao, r1.coverage_reserve_rao) == (NEED, NEED)
    # Day 2: prior pay day 1 = 0 (neutral, first day); D=0; no draw.
    r2 = _score(_fixture(deps, pays), 2)
    assert (r2.credit_reserve_rao, r2.coverage_reserve_rao) == (NEED, NEED)
    # Day 3 pairs with day 2's pay of 100: need 90, nothing deposited, draws.
    r3 = _score(_fixture(deps, pays), 3)
    assert (r3.credit_reserve_rao, r3.coverage_reserve_rao) == (0, 0)
    assert r3.noah_credit_rao == NEED
    assert _run(r3) == 1


def test_no_fault_day_is_neutral_and_draws_nothing():
    # Pay on days 1-3, none on day 4 (excused), pay again on days 5-7.
    # Day 2 prepays three needs' worth beyond its own: 360 total.
    pays = [PAY, PAY, PAY, 0, PAY, PAY, PAY]
    deps = [0, 4 * NEED, 0, 0, 0, 0, 0]
    raw = lambda: _fixture(deps, pays, no_fault={4})
    r2, r3, r4, r5, r6, r7 = (_score(raw(), d) for d in range(2, 8))
    assert (_run(r2), r2.coverage_reserve_rao) == (1, 3 * NEED)
    assert (_run(r3), r3.coverage_reserve_rao) == (2, 2 * NEED)
    assert (_run(r4), r4.coverage_reserve_rao) == (3, 1 * NEED)
    # Day 5 pairs with day 4's zero pay, excused: neutral, no draw.
    assert (_run(r5), r5.coverage_reserve_rao) == (3, 1 * NEED)
    # Day 6 pairs with day 5's pay: the last of the coverage.
    assert (_run(r6), r6.coverage_reserve_rao) == (4, 0)
    # Day 7: nothing left, a miss.
    assert (_run(r7), r7.coverage_reserve_rao) == (0, 0)


def test_a_pay_zero_day_that_is_not_excused_is_still_a_miss_despite_a_reserve():
    pays = [PAY, PAY, 0, PAY]
    deps = [0, 5 * NEED, 0, 0]
    r3 = _score(_fixture(deps, pays), 3)  # pairs with day 2 pay: draws
    r4 = _score(_fixture(deps, pays), 4)  # pairs with day 3 zero pay, not excused
    assert _run(r3) == 2
    assert _run(r4) == 0
    assert r4.coverage_reserve_rao == r3.coverage_reserve_rao  # no draw on it


def test_entry_deposit_never_feeds_the_reserves():
    deps = [0, NEED, NEED]
    pays = [PAY] * 3
    with_entry = _score(_fixture(deps, pays, entry=50 * N))
    without = _score(_fixture(deps, pays, entry=0))
    assert (with_entry.credit_reserve_rao, with_entry.coverage_reserve_rao) == (
        without.credit_reserve_rao,
        without.coverage_reserve_rao,
    )


def test_reserves_do_not_decay():
    # Half-life 1 halves N every day; the ledgers must not move with it.
    p, _ = _p_and_o()
    fast, slow = json_with_half_life(p, 1), json_with_half_life(p, 1000)
    for day in (5, 12, 21):
        a, b = _score(fast, day), _score(slow, day)
        assert (a.credit_reserve_rao, a.coverage_reserve_rao) == (
            b.credit_reserve_rao,
            b.coverage_reserve_rao,
        )


def json_with_half_life(raw, h):
    out = dict(raw)
    out["dials"] = dict(raw["dials"], halfLifeDays=str(h))
    return out


def test_a_miner_who_prepaid_k_days_of_need_has_no_miss_for_those_days():
    for k in (1, 3, 10, 25):
        # Day 2 pays its own need plus k days in advance, then silence.
        deps = [0, (k + 1) * NEED] + [0] * (k + 3)
        pays = [PAY] * len(deps)
        raw = _fixture(deps, pays)
        for d in range(2, k + 3):  # days 2 .. k+2 are all covered
            assert _run(_score(raw, d)) == d - 1, (k, d)
        # The first uncovered day is a miss.
        assert _run(_score(raw, k + 3)) == 0
        assert _score(raw, k + 2).coverage_reserve_rao == 0


def test_result_is_deterministic_and_has_no_lookahead():
    p, _ = _p_and_o()
    a = _score(p, 15)
    assert _score(p, 15) == a  # a restart replays the same history
    # Days after the eval day never change it.
    truncated = json_truncate(p, 15)
    assert _score(truncated, 15) == a


def json_truncate(raw, last):
    out = dict(raw)
    out["evalDay"] = str(last)
    m = dict(raw["miners"][0])
    m["days"] = {k: v for k, v in m["days"].items() if int(k) <= last}
    out["miners"] = [m]
    return out


@pytest.mark.parametrize("seed", range(12))
def test_engine_invariants_over_random_sequences_with_a_large_half_life(seed):
    rng = random.Random(50_000 + seed)
    length = 25
    seq = _random_days(rng, length)
    deps = [d for d, _ in seq]
    pays = [p for _, p in seq]
    raw = _fixture(deps, pays, half_life=1000)
    credited = 0
    for ev in range(1, length + 1):
        r = _score(raw, ev)
        assert r.credit_reserve_rao >= 0 and r.coverage_reserve_rao >= 0
        credited += r.noah_credit_rao  # entry is 0 here
        total_dep = sum(deps[:ev])
        # What was credited never exceeds what was deposited; what is still
        # in the credit reserve is the rest. Exact: the daily flows do not
        # decay, only the stock N does.
        assert credited + r.credit_reserve_rao == total_dep
        assert r.debt_rao >= 0
        assert 0 <= r.multiplier_fp <= FP_SCALE
        # #290 round 2: PCI never exceeds target, on any day, with or
        # without debt ever having been outstanding. Before the round-2
        # fix this was only provable while no repay had ever fired (see
        # the removed `if not debt_ever_seen` branch this replaced, and
        # the module docstring); the debt_weight fix restores it
        # unconditionally, which is the actual property #305/#282 rule 3
        # need ("always total deposited against total earned" can never
        # read as MORE than fully compliant).
        assert r.pci_fp <= TARGET
        assert r.debt_weight_rao <= r.debt_rao
    # Decayed base never exceeds the undecayed total credited.
    assert r.base_fp <= credited + 0


@pytest.mark.parametrize("seed", range(6))
def test_reserves_agree_between_small_and_large_half_life(seed):
    rng = random.Random(70_000 + seed)
    seq = _random_days(rng, 20)
    deps = [d for d, _ in seq]
    pays = [p for _, p in seq]
    a = _score(_fixture(deps, pays, half_life=2))
    b = _score(_fixture(deps, pays, half_life=1000))
    assert (a.credit_reserve_rao, a.coverage_reserve_rao) == (
        b.credit_reserve_rao,
        b.coverage_reserve_rao,
    )


# --- #290 regression: Drew's own reproduction, catch-up vs regular ---------
#
# Drew reproduced this himself directly against the scoring engine (real
# m1-revised-v1 dials via test_mainnet_scenarios._raw, target overridden to
# 0.5, no entry deposit; amounts are the RAW rao he used, not NOAH). Both
# miners deposit 100 total against the same 200 total pay -- #305's own
# stated measure, "always total deposited against total earned" -- but
# REGULAR meets each paid day's own need as it goes, while CATCHUP deposits
# nothing until a single lump sum on the third (zero-matched-pay) day.
# Before this fix: REGULAR pciFp 500000000/multiplierFp 1000000000 (1.0) vs
# CATCHUP pciFp 252525252/multiplierFp 255076011 -- day 2's shortfall was
# locked in at index_sent = 0 forever, and day 3's catch-up deposit, landing
# on a day with no matched pay of its own, banked uselessly instead of
# repaying it. That contradicted #282 rule 3 ("a catch-up contribution must
# satisfy earlier uncovered obligations before being treated exclusively as
# coverage for future earnings"), which this fix implements.


def test_drew_290_catchup_repays_day_2s_debt_on_the_zero_pay_day_3():
    regular = _fixture([0, 50, 50], [100, 100, 0], target=500_000_000, entry=0,
                        ceiling=2 * FP_SCALE, ramp=90)
    catchup = _fixture([0, 0, 100], [100, 100, 0], target=500_000_000, entry=0,
                        ceiling=2 * FP_SCALE, ramp=90)
    r, c = _score(regular), _score(catchup)

    assert r.pci_fp == 500_000_000
    assert r.multiplier_fp == FP_SCALE

    # Day 2 (need 50, deposit 0) still MISSES on its own terms -- #282
    # explicitly keeps streak/classification distinct from compliance, so
    # the fix does not, and must not, retroactively turn that MISS into a
    # HIT. It shows up as debt instead, repaid on day 3.
    d2 = _score(catchup, 2)
    assert d2.pci_fp < 500_000_000  # day 2 alone is still short
    # Day 2's need is 50 (target 0.5 x day 1's pay of 100); nothing was
    # drawn (no coverage exists yet), so the whole 50 becomes debt.
    assert d2.debt_rao == 50

    # By day 3, the whole 100 rao deposit first repays day 2's 50-rao debt
    # (day 3's own need, matched against day 2's pay, is separately 50; the
    # deposit covers that AND the old debt), fully reconciling the ledger.
    assert c.debt_rao == 0
    assert c.coverage_reserve_rao == 0
    assert c.credit_reserve_rao == 0

    # The actual economic effect #282 requires: the multiplier -- what
    # payWeight is computed from -- comes out equal between the two miners,
    # not merely closer.
    #
    # #290 round two (Drew found this by hand, a longer-delay case the
    # engine-level tests below reproduce and prove fixed): this test's own
    # first version asserted CATCHUP's PCI at 505_050_505, ABOVE target
    # (500_000_000) -- an undiscounted repay credit, landing at day 3's
    # fresh weight regardless of how long the debt had been outstanding,
    # silently violated the engine's own documented invariant that PCI
    # cannot exceed target. capFp == FP_SCALE happened to hide it here (the
    # multiplier still came out 1.0 either way), but a longer gap between
    # the shortfall and the catch-up inflates PCI further, without limit,
    # and a target/cap combination where the cap does not bind lets that
    # inflation reach the final multiplier directly -- see
    # ``test_a_catchup_repay_is_capped_by_the_debts_own_decay`` and
    # ``noah/scoring/tests/test_mainnet_scenarios.py::
    # test_drew_290_round_2_long_delayed_catchup_no_longer_inflates_pci``.
    # The fix (engine.py's ``debt_weight``) credits a repay at debt's own
    # decayed weight, not its raw value, so PCI can no longer exceed target
    # this way. Here the shortfall-to-catchup gap is only one day, so the
    # discount is tiny: PCI now lands exactly AT target, not above it.
    assert c.pci_fp == 500_000_000
    assert c.multiplier_fp == FP_SCALE
    assert c.multiplier_fp == r.multiplier_fp
    assert c.pay_weight_fp == r.pay_weight_fp
