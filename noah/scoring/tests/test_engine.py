"""Unit tests for the scoring chain, against hand-computed integers only."""

import pytest

from noah.chain.store import Store
from noah.scoring import FixtureError, parse_fixture, score_fixture
from noah.scoring.constants import FP_SCALE
from noah.scoring.decay import decay_step, integer_nth_root, one_day_factor_fp
from noah.scoring.decay_table import DECAY_FACTOR
from noah.scoring.pay import pay_weight, weights_u16
from noah.scoring.streak import HIT, MISS, NEUTRAL, advance_run, classify_day, streak_fp
from noah.standard.multiplier import multiplier

# --- helpers ----------------------------------------------------------------

DIALS = {
    "targetFp": "500000000",
    "harshness": 1,
    "capFp": "1000000000",
    "halfLifeDays": "182",
    "streakCeilingFp": "2000000000",
    "streakRampDays": "365",
    "ongoingHoldRao": "400000000000",
}


def make_fixture(miners, eval_day="1", dials=None):
    return {
        "schema": "noah-fixture/6",
        "name": "inline-test",
        "dayEpochTs": "1785715200",
        "evalDay": eval_day,
        "dials": dict(DIALS if dials is None else dials),
        "miners": miners,
    }


def miner(coldkey, uid, hold, days, entry="0", reg=None, faw=None):
    if faw is None:
        faw = str(min(int(k) for k in days))
    return {
        "coldkey": coldkey,
        "uid": uid,
        "holdRao": hold,
        "firstActivityDay": faw,
        "entryDepositRao": entry,
        "registrationDay": faw if reg is None else reg,
        "days": days,
    }


def day(paid, dep, foreign="0", no_fault=False, locked="0", unlocked="0"):
    return {
        "paidRao": paid,
        "paidLockedArrivalRao": locked,
        "payUnlockedRao": unlocked,
        "noahDepositRao": dep,
        "foreignDepositNoahEquivRao": foreign,
        "noFault": no_fault,
    }


# --- decay ------------------------------------------------------------------


def test_one_day_factor_is_floor_of_true_value():
    # f = floor((FP^H / 2)^(1/H)) exactly: f^H <= FP^H/2 < (f+1)^H.
    for h in (1, 2, 4, 13, 26, 52, 104, 182, 365):
        f = one_day_factor_fp(h)
        assert f**h <= FP_SCALE**h // 2
        assert (f + 1) ** h > FP_SCALE**h // 2


def test_one_day_factor_matches_published_table():
    # 182 here is the published table's own generation setting -- P-023's
    # ruled half-life (D-139, DURATION conversion: 26 weeks -> 182 days) --
    # a repo-artifact consistency check, not restated as a registry value.
    assert one_day_factor_fp(182) == DECAY_FACTOR[1]


def test_half_life_one_halves_immediately():
    assert one_day_factor_fp(1) == FP_SCALE // 2
    assert decay_step(1000, one_day_factor_fp(1)) == 500


def test_integer_nth_root_edges():
    assert integer_nth_root(0, 5) == 0
    assert integer_nth_root(31, 5) == 1
    assert integer_nth_root(32, 5) == 2
    assert integer_nth_root(10**18, 2) == 10**9


# --- streak -----------------------------------------------------------------


def test_classification_table():
    t = 500_000_000  # dial value used as test data
    assert classify_day(80, 40, False, t) == HIT  # exactly at the ask
    assert classify_day(80, 39, False, t) == MISS  # one below
    assert classify_day(0, 0, True, t) == NEUTRAL  # no-fault zero-pay day
    assert classify_day(0, 0, False, t) == MISS  # at-fault zero-pay day
    assert classify_day(0, 40, True, t) == NEUTRAL  # paid=0 rules, deposit or not


def test_run_advancement():
    assert advance_run(5, HIT) == 6
    assert advance_run(5, MISS) == 0
    assert advance_run(5, NEUTRAL) == 5


def test_streak_fp_ramp():
    ceil, ramp = 2 * FP_SCALE, 52
    assert streak_fp(0, ceil, ramp) == FP_SCALE
    assert streak_fp(1, ceil, ramp) == FP_SCALE + FP_SCALE // 52
    assert streak_fp(52, ceil, ramp) == ceil
    assert streak_fp(500, ceil, ramp) == ceil  # clamped at the ramp length


# --- pay --------------------------------------------------------------------


def test_pay_weight_floor():
    assert pay_weight(0, FP_SCALE) == 0
    assert pay_weight(5, FP_SCALE) == 2  # isqrt(5) = 2
    assert pay_weight(499_999_999, 999_999_996) == 22359  # floor(22360 * 0.999999996)


def test_quantization_four_way_tie_lowest_uids_win():
    w = weights_u16({0: 7, 1: 7, 2: 7, 3: 7})
    assert w == {0: 16384, 1: 16384, 2: 16384, 3: 16383}
    assert sum(w.values()) == 65535


def test_quantization_zero_sum_is_fallback():
    assert weights_u16({}) == {}
    assert weights_u16({0: 0, 1: 0}) == {}


def test_quantization_sums_exactly():
    w = weights_u16({3: 1, 9: 2, 11: 4, 40: 65535})
    assert sum(w.values()) == 65535


# --- engine end-to-end ------------------------------------------------------


def test_single_day_hit_hand_computed():
    # #16/#135: day d's deposit is judged against day d-1's pay, not its
    # own. Day 1 supplies the pay with no deposit (there is no day 0 for
    # day 1's own deposit to be judged against); day 2 (eval) supplies the
    # deposit actually judged here, against day 1's pay.
    days = {"1": day("10", "0"), "2": day("0", "5")}
    fx = parse_fixture(make_fixture([miner("5A", 0, "400000000000", days)], "2"))
    out = score_fixture(fx)
    m = out["miners"][0]
    # N = min(5, prior day's pay 10) = 5; base = 5; pci = 5e8; hit -> run 1.
    assert m["noahCreditRao"] == "5"
    assert m["baseFp"] == "5"
    assert m["pciFp"] == "500000000"
    assert m["streakFp"] == str(FP_SCALE + FP_SCALE // 365)
    # pci (5e8) sits exactly at target (5e8), so the reference multiplier is
    # exactly 1.0 -- no grace involved.
    assert m["multiplierFp"] == str(FP_SCALE)
    # score = floor(5 * 1019230769 / 1e9) = 5; x = isqrt(5) = 2.
    assert m["scoreFp"] == "5"
    assert m["payWeightFp"] == "2"
    assert out["weightsU16"] == {"0": 65535}


def test_door_closed_zeroes_score_only_and_boundary_opens():
    # #16/#135: the deposit is judged against the PRIOR day's pay, so day 1
    # carries the pay alone and the deposit under test lands on day 2 (eval).
    days = {"1": day("80000000000", "0"), "2": day("0", "40000000000")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5Short", 0, "399999999999", dict(days)),
                miner("5Exact", 1, "400000000000", dict(days)),
            ],
            "2",
        )
    )
    out = score_fixture(fx)
    shorted, exact = out["miners"]
    assert shorted["doorOpen"] is False
    assert shorted["scoreFp"] == "0"
    assert shorted["payWeightFp"] == "0"
    # The door zeroes the score, never the record: credit still granted.
    assert shorted["noahCreditRao"] == "40000000000"
    assert shorted["baseFp"] == "40000000000"
    assert exact["doorOpen"] is True
    assert int(exact["scoreFp"]) > 0
    # Every live UID appears in the weight vector, zeros included.
    assert out["weightsU16"] == {"0": 0, "1": 65535}


def test_ongoing_hold_rao_of_zero_is_accepted_by_the_fixture_parser():
    """No validation anywhere rejects ``ongoingHoldRao: "0"`` -- confirming,
    at the fixture-parsing layer, the claim ``test_score.py`` proves at the
    ``gate()`` layer: the floor is already a real, adjustable dial that can
    be set to zero/disabled with no code change."""
    fx = parse_fixture(make_fixture([], dials={**DIALS, "ongoingHoldRao": "0"}))
    assert fx.dials.ongoing_hold_rao == 0


def test_floor_disabled_zero_holding_miner_still_gets_doorOpen_true():
    """Drew's own direct ruling (relayed 2026-09-19): the 400-NOAH ongoing
    floor must be a real, adjustable setting that can be set to
    zero/disabled, not deleted. With ``ongoingHoldRao: "0"`` in the dials,
    a miner with a LITERALLY ZERO real holding (never merely "under the
    historical 400-NOAH figure") must still pass the door: ``hold_rao (0)
    >= ongoing_hold_rao (0)`` is true, so the gate opens unconditionally."""
    # #16/#135: the deposit is judged against the PRIOR day's pay, so day 1
    # carries the pay alone and the deposit under test lands on day 2 (eval).
    days = {"1": day("80000000000", "0"), "2": day("0", "40000000000")}
    fx = parse_fixture(
        make_fixture(
            [miner("5ZeroHolding", 0, "0", dict(days))],
            "2",
            dials={**DIALS, "ongoingHoldRao": "0"},
        )
    )
    out = score_fixture(fx)
    m = out["miners"][0]
    assert m["doorOpen"] is True
    assert int(m["scoreFp"]) > 0
    assert out["weightsU16"] == {"0": 65535}


def test_foreign_never_exceeds_noah_room():
    # deposit 60, prior day's pay 100 -> N = 60; foreign 500 -> capped at
    # room 60. #16/#135: the deposit and foreign credit under test land on
    # day 2 (eval), judged against day 1's pay.
    days = {"1": day("100", "0"), "2": day("0", "60", "500")}
    fx = parse_fixture(make_fixture([miner("5M", 0, "500000000000", days)], "2"))
    m = score_fixture(fx)["miners"][0]
    assert m["noahCreditRao"] == "60"
    assert m["foreignCreditRao"] == "60"
    assert m["baseFp"] == "120"  # N + F


def test_substitutor_scores_zero():
    # Zero NOAH, floods foreign, 190 days of history: base 0, PCI 0,
    # multiplier 0, weights empty (no-weights fallback).
    days = {str(w): day("100", "0", "999999") for w in range(1, 191)}
    fx = parse_fixture(make_fixture([miner("5Sub", 0, "500000000000", days)], "190"))
    out = score_fixture(fx)
    m = out["miners"][0]
    assert m["baseFp"] == "0"
    assert m["foreignCreditRao"] == "0"
    assert m["pciFp"] == "0"
    assert m["multiplierFp"] == "0"
    assert m["scoreFp"] == "0"
    assert out["weightsU16"] == {}


def test_missing_day_is_a_validation_error():
    # An absent day is a validation error, never an implied miss.
    days = {"1": day("10", "5"), "3": day("10", "5")}  # day 2 absent
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5G", 0, "400000000000", days)], "3"))


def test_day_outside_first_activity_range_rejected():
    # No day may be present outside firstActivityDay..evalDay.
    days = {"1": day("10", "5"), "2": day("10", "5")}
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5G", 0, "400000000000", days, faw="2")], "2"))


def test_no_fault_with_nonzero_pay_rejected():
    # A no-fault flag alongside nonzero pay is a validation error.
    days = {"1": day("1", "0", no_fault=True)}
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5G", 0, "400000000000", days)]))


def test_day_key_with_trailing_newline_rejected():
    # `_DECIMAL`'s `$` anchors at the true end of a string OR just before a
    # single trailing "\n" -- match() alone would accept "1\n" as canonical,
    # int("1\n") converts it to day 1, and it would silently overwrite (or
    # be overwritten by) an actual "1" key sharing the same fixture, losing
    # one day's real content with no error. #193 codex adversarial
    # challenge, round 9 (against e669cdc): the sibling of a gap already
    # fixed once in tools/chain-extract/extract.py's own day-key check
    # (codex review, round 7) but left open in this file's copy of the
    # identical regex.
    days = {"1\n": day("900", "0")}
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5N", 0, "1", days, faw="1")]))


def test_name_with_trailing_newline_rejected():
    # `_NAME`'s `$` has the identical match()-vs-fullmatch() gap as
    # `_DECIMAL` above -- match() alone accepted "probe\n" as a valid
    # fixture name. TypeScript's fixture.ts safeName() never had this gap
    # (JS's `$` has no trailing-newline exception), so leaving it open here
    # made Python accept a name TypeScript correctly rejects: the opposite
    # split from the one #524 closed. Issue #554, found by a codex review
    # against 1c20dc4.
    fx = make_fixture([miner("5N", 0, "1", {"1": day("900", "0")}, faw="1")])
    fx["name"] = "probe\n"
    with pytest.raises(FixtureError):
        parse_fixture(fx)


def test_colliding_canonical_and_noncanonical_day_keys_rejected():
    # Two distinct JSON keys ("1" and "1\n") that both canonicalize to the
    # same integer day must not silently collapse into one entry.
    days = {"1": day("100", "0"), "1\n": day("900", "0")}
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5N", 0, "1", days, faw="1")]))


def test_entry_deposit_credited_cap_exempt_pci_excluded():
    # The registration day has zero pay, so only the cap exemption can
    # grant the entry deposit as credit, and neither leg of the index may
    # see it. #16/#135: a real deposit-vs-pay comparison now needs a day
    # WITH real pay (day 2) immediately before the deposit under test
    # (day 3, eval) -- day 1 (registration) itself has no prior day, so it
    # cannot carry the comparison on its own the way a single pay+deposit
    # day used to.
    days = {"1": day("0", "0"), "2": day("10", "0"), "3": day("0", "5")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5Reg", 0, "400000000000", dict(days), entry="1000"),
                miner("5Ctl", 1, "400000000000", dict(days)),
            ],
            "3",
        )
    )
    out = score_fixture(fx)
    reg, ctl = out["miners"]
    d1 = one_day_factor_fp(int(DIALS["halfLifeDays"]))
    # The entry credit lands on day 1 and decays through both day 2 and
    # day 3's day-closes before the evaluation day's baseFp is read.
    expected_entry_contribution = decay_step(decay_step(1000, d1), d1)
    assert int(reg["baseFp"]) - int(ctl["baseFp"]) == expected_entry_contribution
    assert reg["pciFp"] == ctl["pciFp"] == "500000000"


def test_entry_deposit_opens_matching_room():
    # The entry deposit is own-token credit, so foreign deposits can match
    # it from day one even with zero pay.
    days = {"1": day("0", "0", foreign="600")}
    fx = parse_fixture(make_fixture([miner("5Reg", 0, "400000000000", days, entry="1000")]))
    m = score_fixture(fx)["miners"][0]
    assert m["foreignCreditRao"] == "600"
    assert m["baseFp"] == "1600"


def test_registration_day_outside_range_with_entry_deposit_rejected():
    # A registrationDay outside firstActivityDay..evalDay with a nonzero
    # entry deposit is a validation error, never silent.
    days = {"3": day("10", "5"), "4": day("10", "5")}
    for reg in ("2", "5"):  # one below the range, one beyond evalDay
        with pytest.raises(FixtureError):
            parse_fixture(
                make_fixture([miner("5R", 0, "1", dict(days), entry="1000", reg=reg)], "4")
            )


def test_registration_day_outside_range_with_zero_deposit_allowed():
    # A later registration on an older coldkey pays no second entry
    # deposit: it arrives with entryDepositRao "0", and an out-of-range
    # registrationDay with a zero deposit parses and credits nothing.
    days = {"3": day("10", "5"), "4": day("10", "5")}
    fx = parse_fixture(make_fixture([miner("5R", 0, "1", dict(days), entry="0", reg="5")], "4"))
    ctl = parse_fixture(make_fixture([miner("5R", 0, "1", dict(days), entry="0", reg="3")], "4"))
    assert score_fixture(fx) == score_fixture(ctl)


def test_first_activity_day_beyond_eval_day_rejected():
    # A firstActivityDay beyond evalDay is an explicit validation error.
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5F", 0, "1", {"3": day("10", "5")}, faw="3")], "2"))


def test_registration_at_eval_day_entry_credit_in_output():
    # When the registration day IS the evaluation day, the noahCreditRao
    # output includes the entry credit. An earlier registration day leaves
    # the evaluation day's output at the plain min(deposit, paid) leg.
    # #16/#135: the deposit judged is always compared to the PRIOR day's
    # pay, so both sub-cases below carry a real pay day immediately before
    # the deposit under test, instead of pairing pay and deposit same-day.
    at_eval = parse_fixture(
        make_fixture(
            [
                miner(
                    "5R",
                    0,
                    "400000000000",
                    {"1": day("10", "0"), "2": day("0", "5")},
                    entry="1000",
                    reg="2",
                    faw="1",
                )
            ],
            "2",
        )
    )
    m = score_fixture(at_eval)["miners"][0]
    assert m["noahCreditRao"] == "1005"  # min(5, prior day's pay 10) + the entry credit
    assert m["pciFp"] == "500000000"  # the entry credit never enters PCI

    earlier = parse_fixture(
        make_fixture(
            [
                miner(
                    "5R",
                    0,
                    "400000000000",
                    {"1": day("0", "0"), "2": day("10", "0"), "3": day("0", "5")},
                    entry="1000",
                    reg="1",
                    faw="1",
                )
            ],
            "3",
        )
    )
    m = score_fixture(earlier)["miners"][0]
    assert m["noahCreditRao"] == "5"  # the eval day's own leg; the entry credit landed on day 1


def test_starting_treatment_covers_the_first_spendable_pay_day_itself():
    # #112: day 1 is this seat's own first-ever day of real spendable pay
    # (paid 10, deposited 0), and it is also the day being evaluated, so
    # starting treatment holds the multiplier at a flat FP_SCALE (1.0) for
    # this whole day -- regardless of the measured pci_fp (0, since nothing
    # was deposited) and regardless of the cap dial. This is the exact
    # circularity #112 fixes: the old no-grace behavior (removed the same
    # night, ce2707c) would have measured pci_fp 0 straight into a
    # multiplier of 0 here, zeroing a brand-new miner's weight no matter how
    # much they deposit, on their very first day.
    dials = dict(DIALS, capFp="3000000000")
    days = {"1": day("10", "0")}  # paid 10, deposited 0 -> pci_fp 0
    fx = parse_fixture(make_fixture([miner("5New", 0, "400000000000", days)], dials=dials))
    m = score_fixture(fx)["miners"][0]
    assert m["pciFp"] == "0"
    assert m["multiplierFp"] == str(FP_SCALE)


def test_starting_treatment_ends_the_day_after_first_spendable_pay():
    # The day AFTER the first spendable pay day, real PCI-based scoring
    # applies, permanently -- even though this miner never deposits and its
    # pci_fp is still 0. Same miner as the test above, evaluated one day
    # later with a second, still-undeposited pay day: now genuinely
    # measured, and punished for it.
    dials = dict(DIALS, capFp="3000000000")
    days = {"1": day("10", "0"), "2": day("10", "0")}
    fx = parse_fixture(make_fixture([miner("5New", 0, "400000000000", days)], "2", dials=dials))
    m = score_fixture(fx)["miners"][0]
    assert m["pciFp"] == "0"
    assert m["multiplierFp"] == "0"


def test_no_grace_boundary_is_gone():
    # The old grace boundary (evalDay - firstActivityDay == halfLifeDays)
    # no longer changes anything: a wallet with pci_fp 0 scores the
    # identical multiplier whether it is one day old or older than the old
    # memory window.
    half_life = int(DIALS["halfLifeDays"])

    def build(first, eval_day):
        days = {str(w): day("10", "0") for w in range(first, eval_day + 1)}
        return parse_fixture(make_fixture([miner("5W", 0, "400000000000", days)], str(eval_day)))

    younger = score_fixture(build(1, half_life))["miners"][0]  # age H-1
    older = score_fixture(build(1, half_life + 1))["miners"][0]  # age H
    assert younger["multiplierFp"] == "0"  # PCI 0, measured
    assert older["multiplierFp"] == "0"  # PCI 0, measured -- identical


def test_output_ordered_by_uid_null_last_by_coldkey():
    # Output miners run ascending by uid, null uids last by coldkey.
    days = {"1": day("10", "5")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5NullB", None, "400000000000", dict(days)),
                miner("5Uid7", 7, "400000000000", dict(days)),
                miner("5NullA", None, "400000000000", dict(days)),
                miner("5Uid2", 2, "400000000000", dict(days)),
            ]
        )
    )
    out = score_fixture(fx)
    assert [m["coldkey"] for m in out["miners"]] == ["5Uid2", "5Uid7", "5NullA", "5NullB"]


def test_output_carries_each_rows_own_uid():
    # uid is carried through to output correctly, live or null. Kept on
    # three DISTINCT coldkeys (the interim-D-138 shape this test was
    # rewritten to during the window that ruling refused a shared coldkey)
    # rather than reverted back to one multi-seat coldkey now that D-138 is
    # reopened (issue #112): the encoding behavior under test -- uid
    # round-trips correctly, live or null -- does not depend on which shape
    # exercises it, and the multi-seat case itself is covered directly by
    # test_duplicate_coldkey_with_distinct_uids_now_parses_and_scores and
    # test_a_coldkey_with_a_numbered_row_and_a_null_row_now_parses_fine.
    days = {"1": day("10", "5")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5A", 10, "1", dict(days)),
                miner("5B", 11, "1", dict(days)),
                miner("5C", None, "1", dict(days)),
            ]
        )
    )
    out = score_fixture(fx)
    assert [m["uid"] for m in out["miners"]] == [10, 11, None]
    # A plain JSON int or null, never a decimal string, matching the input
    # contract's own encoding for uid.
    assert all(isinstance(m["uid"], int) or m["uid"] is None for m in out["miners"])


def test_null_uid_excluded_from_weights():
    # #16/#135: a real, nonzero score needs a real deposit-vs-prior-pay
    # match, so day 1 carries the pay and the deposit lands on day 2 (eval).
    days = {"1": day("10", "0"), "2": day("0", "5")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5Live", 4, "400000000000", dict(days)),
                miner("5Dereg", None, "400000000000", dict(days)),
            ],
            "2",
        )
    )
    out = score_fixture(fx)
    assert out["weightsU16"] == {"4": 65535}
    assert len(out["miners"]) == 2  # still reported


# --- locked pay is deferred, unlocks enter ------------------------------------


def test_locked_arrival_touches_no_total():
    # paidLockedArrivalRao is recorded and deferred. It enters no total, so
    # a miner with locked arrivals scores identically to one without them.
    locked = {"1": day("10", "5", locked="999")}
    plain = {"1": day("10", "5")}
    out_locked = score_fixture(
        parse_fixture(make_fixture([miner("5A", 0, "400000000000", locked)]))
    )
    out_plain = score_fixture(parse_fixture(make_fixture([miner("5A", 0, "400000000000", plain)])))
    assert out_locked == out_plain


def test_unlock_enters_denominator_and_cap():
    # A day's denominator addition and pay cap are both
    # paidRao + payUnlockedRao. An unlock-only day's spendable pay grants
    # credit against a deposit that is judged the NEXT day (#16/#135: the
    # deposit is compared to the PRIOR day's pay); without the unlock the
    # same deposit is capped at zero.
    days = {"1": day("0", "0", locked="10"), "2": day("0", "0", unlocked="10"), "3": day("0", "5")}
    m = score_fixture(parse_fixture(make_fixture([miner("5U", 0, "400000000000", days)], "3")))[
        "miners"
    ][0]
    # Day 3: cap = min(5, day 2's spendable pay 0 + 10) = 5; S = 5, Pd = 10
    # -> PCI 0.5.
    assert m["noahCreditRao"] == "5"
    assert m["baseFp"] == "5"
    assert m["pciFp"] == "500000000"

    ctl_days = {"1": day("0", "0"), "2": day("0", "5")}
    ctl = score_fixture(
        parse_fixture(make_fixture([miner("5U", 0, "400000000000", ctl_days)], "2"))
    )["miners"][0]
    assert ctl["noahCreditRao"] == "0"
    assert ctl["baseFp"] == "0"
    assert ctl["pciFp"] == "0"  # zero decayed pay


def test_classification_sees_unlock_as_pay():
    # Lock state does not change a day's classification. The pay the
    # classification sees is paidRao + payUnlockedRao, and (#16/#135) it is
    # the PRIOR day's spendable pay the deposit is judged against. Day 1's
    # own real pay judges day 2's deposit (hit 1); day 2's own unlock (its
    # spendable pay) judges day 3's deposit (hit 2) -- day 1 itself, being
    # first_activity_day, has no prior day and classifies neutral, neither
    # extending nor resetting the run.
    hit_days = {
        "1": day("10", "5", locked="10"),
        "2": day("0", "5", unlocked="10"),
        "3": day("0", "5"),
    }
    m = score_fixture(parse_fixture(make_fixture([miner("5H", 0, "400000000000", hit_days)], "3")))[
        "miners"
    ][0]
    assert m["streakFp"] == str(FP_SCALE + (FP_SCALE * 2) // 365)  # run 2

    # Day 1 deposits nothing: a day-1 deposit lands on a day with no prior pay
    # and is banked in the prepaid reserve (#290), which would cover day 2.
    miss_days = {"1": day("10", "0", locked="10"), "2": day("0", "0", unlocked="10")}
    m = score_fixture(
        parse_fixture(make_fixture([miner("5M", 0, "400000000000", miss_days)], "2"))
    )["miners"][0]
    assert m["streakFp"] == str(FP_SCALE)  # run reset


def test_cumulative_unlock_exceeding_locked_rejected():
    # Cumulative payUnlockedRao through any day may never exceed
    # cumulative paidLockedArrivalRao through that day.
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5V", 0, "1", {"1": day("0", "0", unlocked="1")})]))
    # Exceeding at an intermediate day is an error even when a later locked
    # arrival would balance the totals.
    days = {
        "1": day("0", "0", locked="5"),
        "2": day("0", "0", unlocked="6"),
        "3": day("0", "0", locked="5"),
    }
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5V", 0, "1", days)], "3"))
    # At exactly the cumulative locked total the fixture is valid.
    ok = {
        "1": day("0", "0", locked="5"),
        "2": day("0", "0", locked="1", unlocked="6"),
    }
    parse_fixture(make_fixture([miner("5V", 0, "1", ok)], "2"))


# --- strict validation --------------------------------------------------------


def test_unknown_field_rejected_everywhere():
    good = make_fixture([miner("5A", 0, "1", {"1": day("1", "1")})])

    bad_top = dict(good)
    bad_top["extra"] = 1
    with pytest.raises(FixtureError):
        parse_fixture(bad_top)

    bad_dials = make_fixture([miner("5A", 0, "1", {"1": day("0", "0")})])
    bad_dials["dials"]["sweepDeadline"] = "3"
    with pytest.raises(FixtureError):
        parse_fixture(bad_dials)

    bad_miner = make_fixture([dict(miner("5A", 0, "1", {"1": day("0", "0")}), intent="sweep")])
    with pytest.raises(FixtureError):
        parse_fixture(bad_miner)

    bad_day = make_fixture([miner("5A", 0, "1", {"1": dict(day("1", "1"), settleable=True)})])
    with pytest.raises(FixtureError):
        parse_fixture(bad_day)


def test_missing_field_rejected():
    for absent in ("holdRao", "firstActivityDay", "entryDepositRao", "registrationDay"):
        m = miner("5A", 0, "1", {"1": day("0", "0")})
        del m[absent]
        with pytest.raises(FixtureError):
            parse_fixture(make_fixture([m]))


def test_day_beyond_eval_day_rejected():
    with pytest.raises(FixtureError):
        parse_fixture(make_fixture([miner("5A", 0, "1", {"2": day("1", "1")})], "1"))


def test_bad_schema_and_bad_numbers_rejected():
    good = make_fixture([miner("5A", 0, "1", {"1": day("0", "0")})])
    for mutate in (
        lambda d: d.update(schema="noah-fixture/1"),
        lambda d: d.update(schema="noah-fixture/2"),
        lambda d: d.update(schema="noah-fixture/3"),  # version 3 is invalid now
        lambda d: d.update(evalDay="007"),  # leading zeros: not canonical
        lambda d: d.update(evalDay="-1"),
    ):
        bad = json_roundtrip(good)
        mutate(bad)
        with pytest.raises(FixtureError):
            parse_fixture(bad)


def json_roundtrip(d):
    import json

    return json.loads(json.dumps(d))


# --- duplicate uid validation, and D-138 reopened for duplicate coldkeys ----
#
# parse_fixture still rejects a duplicate uid: it is the per-seat identity
# both engines key their output on, and stays unique across the whole
# fixture regardless of how many rows a coldkey has. D-138 itself is
# reopened (issue #112, 2026-09-19): "Noah recognises and scores exactly one
# hotkey per coldkey" no longer holds -- up to 32 hotkeys may share one
# coldkey, each scored independently -- so a coldkey appearing in more than
# one row of ``miners`` is ordinary input again, not a shape this parser
# refuses. No replacement cross-seat validation is added in its place, by
# Drew's own direction: every row is still validated independently by every
# other rule, and there is deliberately no rule comparing two rows of the
# same coldkey against each other.


def test_duplicate_uid_across_two_different_coldkeys_is_rejected():
    fx = make_fixture(
        [
            miner("5A", 3, "1", {"1": day("10", "5")}),
            miner("5B", 3, "1", {"1": day("10", "5")}),
        ]
    )
    with pytest.raises(FixtureError, match="duplicate uid"):
        parse_fixture(fx)


def test_duplicate_coldkey_with_distinct_uids_now_parses_and_scores():
    """D-138 reopened (issue #112): two live seats for one coldkey -- 'the
    several-seats case' the 2026-08-30 ruling used to close -- is legitimate
    input again. Against the interim D-138 code (this test's own prior name
    and behavior, `test_duplicate_coldkey_with_distinct_uids_is_rejected`),
    this exact fixture used to be refused; it must now parse AND score both
    seats independently, neither one silently dropped or merged into the
    other."""
    fx = make_fixture(
        [
            miner("5Two", 0, "1", {"1": day("10", "5")}),
            miner("5Two", 1, "1", {"1": day("10", "5")}),
        ]
    )
    fixture = parse_fixture(fx)
    assert [m.coldkey for m in fixture.miners] == ["5Two", "5Two"]
    assert [m.uid for m in fixture.miners] == [0, 1]
    out = score_fixture(fixture)
    assert [m["uid"] for m in out["miners"]] == [0, 1]


def test_a_coldkey_with_a_numbered_row_and_a_null_row_now_parses_fine():
    """The second shape #193 used to allow -- one live seat plus a vacated
    seat's null-uid row for the SAME coldkey -- is likewise legitimate again
    under D-138 reopened (issue #112): a coldkey may have a live seat's own
    row and a separate row parking history no live seat claims, and this
    parser draws no distinction between them beyond each row's own rules."""
    fx = make_fixture(
        [
            miner("5Two", 3, "100", {"1": day("10", "5")}),
            miner("5Two", None, "0", {"1": day("40", "5")}),
        ]
    )
    fixture = parse_fixture(fx)
    assert [m.uid for m in fixture.miners] == [3, None]


def test_a_second_null_row_for_one_coldkey_now_parses_fine():
    """Two null-uid rows for one coldkey are likewise two rows for one
    coldkey, and D-138 reopened (issue #112) lifts the whole-coldkey refusal
    that used to catch this shape along with every other duplicate-coldkey
    one. No replacement rule is added for it (Drew's own direction: no
    cross-seat validation in its place)."""
    fx = make_fixture(
        [
            miner("5Two", None, "10", {"1": day("10", "5")}),
            miner("5Two", None, "20", {"1": day("20", "5")}),
        ]
    )
    fixture = parse_fixture(fx)
    assert [m.uid for m in fixture.miners] == [None, None]


def test_a_lone_seat_parses_fine():
    """The single-row case is unaffected either way it stood: exactly one
    row for a coldkey has always parsed, before D-138, under it, and now
    that it is reopened."""
    fixture = parse_fixture(make_fixture([miner("5Solo", 0, "1", {"1": day("1", "1")})]))
    assert len(fixture.miners) == 1


# --- cross-engine input-acceptance: a bare integer literal above 2**53-1 ----
#
# Real codex adversarial finding against this ticket's own frozen
# candidate: verification/noah-ts's own fixture parser refuses a bare JSON
# number literal above Number.MAX_SAFE_INTEGER (its own big()/smallInt(),
# src/fixture.ts), because JSON.parse itself would already have silently
# rounded such a literal before that check ever runs. This module's own
# Python int is arbitrary-precision and used to read the identical literal
# back out exactly, correctly -- accepting a fixture the TypeScript twin
# refuses outright, a genuine cross-engine input-acceptance divergence the
# 18-fixture shared canonical set (every value in it well under this
# bound) never exercised.


def test_a_bare_json_int_above_javascript_safe_integer_bound_is_refused():
    too_big = 2**53  # one past JavaScript's own Number.MAX_SAFE_INTEGER (2**53 - 1)
    fixture = make_fixture([miner("5X", 1, "1000000000000", {"1": day(too_big, "0")})])
    with pytest.raises(FixtureError, match="safe integer"):
        parse_fixture(fixture)


def test_exactly_the_safe_integer_bound_itself_is_still_accepted():
    """The bound is inclusive -- Number.MAX_SAFE_INTEGER itself round-trips
    exactly through a JS double, so this is the last value both engines
    agree is legal as a bare literal."""
    at_bound = 2**53 - 1
    fixture = make_fixture([miner("5X", 1, "1000000000000", {"1": day(at_bound, "0")})])
    parse_fixture(fixture)  # does not raise


def test_the_identical_value_as_a_canonical_decimal_string_is_still_accepted():
    """The safe-integer bound applies only to a BARE number literal -- the
    canonical decimal STRING form (which verification/noah-ts's own big()
    also accepts at any size, via BigInt) stays unconstrained on both
    engines, exactly as before this fix."""
    too_big = 2**53
    fixture = make_fixture([miner("5X", 1, "1000000000000", {"1": day(str(too_big), "0")})])
    parse_fixture(fixture)  # does not raise


# ---------------------------------------------------------------------------
# #112 "starting treatment": required real, adversarial proofs.
#
# The grace period removed the same night (ce2707c) excused every miner for
# a fixed calendar window regardless of whether they had ever been paid.
# Removing it exposed a real circularity: a miner never yet paid measures
# pci_fp 0, and multiplier(0, target, harshness>0, cap) is 0 for any
# harshness > 0 -- so a brand-new miner's weight is 0 no matter how much
# they deposit. Starting treatment closes this by keying the exemption to
# the SEAT's own real payment history (a flat FP_SCALE through the day of
# its own first spendable payment) instead of a calendar window.
# ---------------------------------------------------------------------------


def test_newcomer_joining_an_established_group_scores_by_its_own_history():
    # Required proof #1: an established seat, 200 days of real history,
    # deliberately underdepositing (25 against a 100 pay cap, well below
    # the 0.5 target) so its multiplier is genuinely measured and punished,
    # not coincidentally 1.0. A brand-new seat joins on the SAME evalDay
    # with no pay yet at all. Each seat's OWN history decides starting
    # treatment -- the newcomer is neither punished nor helped by how old
    # its neighbor is.
    established_days = {str(d): day("100", "25") for d in range(1, 201)}
    newcomer_days = {"200": day("0", "0")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5Established", 0, "400000000000", established_days, faw="1"),
                miner("5Newcomer", 1, "400000000000", newcomer_days, faw="200"),
            ],
            "200",
        )
    )
    out = score_fixture(fx)
    established = next(m for m in out["miners"] if m["coldkey"] == "5Established")
    newcomer = next(m for m in out["miners"] if m["coldkey"] == "5Newcomer")
    # Newcomer: never paid -> starting treatment, flat FP_SCALE.
    assert newcomer["pciFp"] == "0"
    assert newcomer["multiplierFp"] == str(FP_SCALE)
    # Established: long past its own transition (day 1), genuinely measured
    # and punished for underdepositing -- not a flat value of any kind.
    assert 0 < int(established["multiplierFp"]) < FP_SCALE


def test_entirely_new_group_scores_identically_with_or_without_an_established_peer():
    # Required proof #2: the exact same brand-new seat, scored alone (an
    # "entirely new group" -- no established peer, no other coldkey, in the
    # fixture at all) versus alongside the established peer above. The
    # engine scores each MinerInput row completely independently of every
    # other row, so the two must be byte-for-byte identical -- proving
    # starting treatment does not depend on the subnet or group being new,
    # only on this seat's own history.
    newcomer_days = {"200": day("0", "0")}
    alone = parse_fixture(
        make_fixture([miner("5Newcomer", 1, "400000000000", newcomer_days, faw="200")], "200")
    )
    established_days = {str(d): day("100", "25") for d in range(1, 201)}
    grouped = parse_fixture(
        make_fixture(
            [
                miner("5Established", 0, "400000000000", established_days, faw="1"),
                miner("5Newcomer", 1, "400000000000", newcomer_days, faw="200"),
            ],
            "200",
        )
    )
    alone_result = score_fixture(alone)["miners"][0]
    grouped_result = next(
        m for m in score_fixture(grouped)["miners"] if m["coldkey"] == "5Newcomer"
    )
    assert alone_result["multiplierFp"] == str(FP_SCALE)
    assert alone_result == grouped_result


# Required proof #3 (first payment landing at the very end of a day: full
# multiplier for that whole day, real scoring starts clean the next day) is
# covered above by test_starting_treatment_covers_the_first_spendable_pay_day_itself
# and test_starting_treatment_ends_the_day_after_first_spendable_pay.


def test_post_transition_steady_contribution_at_target_scores_high():
    # Required proof #4: after the transition day, a seat that keeps
    # depositing exactly at the target ratio scores the real, HIGH
    # multiplier -- genuinely measured, not cushioned, not held at a flat
    # value. #16/#135: a deposit is judged against the PRIOR day's pay, so
    # a leading pay-only day (day 1, no deposit yet -- there is no day 0
    # for day 1's own deposit to be judged against) establishes a real
    # prior-pay day before the two steady 500-against-1000 deposits.
    dials = dict(DIALS, halfLifeDays="1")
    days = {
        "1": day("1000", "0"),
        "2": day("1000", "500"),  # judged against day 1's pay: exactly 0.5
        "3": day("1000", "500"),  # judged against day 2's pay: exactly 0.5
    }
    fx = parse_fixture(
        make_fixture([miner("5Steady", 0, "400000000000", days)], "3", dials=dials)
    )
    m = score_fixture(fx)["miners"][0]
    d1 = one_day_factor_fp(1)
    s = pd = 0
    prior_paid = 0  # no day before day 1: prior pay reads as 0
    for paid, dep in ((1000, 0), (1000, 500), (1000, 500)):
        s = decay_step(s, d1) + dep
        pd = decay_step(pd, d1) + prior_paid
        prior_paid = paid
    expected_pci = s * FP_SCALE // pd
    assert m["pciFp"] == str(expected_pci)
    assert expected_pci == int(dials["targetFp"])  # exactly at target
    # Day 1 was the transition day (this seat's first spendable pay); day 3
    # (eval) is genuinely measured -- high/full, exactly 1.0 here.
    assert m["multiplierFp"] == str(FP_SCALE)


def test_post_transition_stops_contributing_entirely_scores_near_zero():
    # Required proof #5: after the transition day, a seat that stops
    # depositing ENTIRELY while pay keeps arriving scores the real,
    # punished value -- no cushioning, no floor under it. #16/#135: a
    # leading pay-only day (day 1, no deposit -- there is no day 0 for its
    # own deposit to be judged against) establishes the real prior-pay day
    # the one real deposit (day 2) is judged against; pay keeps arriving
    # every day after while the deposit never repeats.
    dials = dict(DIALS, halfLifeDays="1", harshness=4)
    replay = [(1000, 0), (1000, 500), (1000, 0), (1000, 0), (1000, 0), (1000, 0)]
    days = {str(i + 1): day(str(paid), str(dep)) for i, (paid, dep) in enumerate(replay)}
    fx = parse_fixture(
        make_fixture([miner("5Quits", 0, "400000000000", days)], "6", dials=dials)
    )
    m = score_fixture(fx)["miners"][0]
    d1 = one_day_factor_fp(1)
    s = pd = 0
    prior_paid = 0  # no day before day 1: prior pay reads as 0
    for paid, dep in replay:
        s = decay_step(s, d1) + dep
        pd = decay_step(pd, d1) + prior_paid
        prior_paid = paid
    expected_pci = s * FP_SCALE // pd
    expected_mult = multiplier(expected_pci, int(dials["targetFp"]), 4, int(dials["capFp"]))
    assert m["pciFp"] == str(expected_pci)
    assert m["multiplierFp"] == str(expected_mult)
    # Near/at zero, real punishment, no cushioning: well under a tenth of
    # the target ratio once harshness 4 has been applied.
    assert int(m["multiplierFp"]) < FP_SCALE // 10


def _days_from_store(store, coldkey, hotkey, day_range, deposit_by_day):
    """Build one seat's fixture ``days`` dict from a store's own recorded
    attributed pay, combined with a caller-supplied deposit schedule (this
    store does not itself track NOAH deposits; only pay is under test
    here). Mirrors, in miniature and only for this test, what a real
    fixture builder (``noah/chain/fixture_build.py``, out of this task's
    scope) does from the same underlying ``attributed_pay`` table.
    """
    days = {}
    for d in day_range:
        store.mark_day_scanned(d, from_block=1, to_block=2)
        paid = store.attributed_pay_by_seat(coldkey, d).get(hotkey, 0)
        days[str(d)] = day(str(paid), str(deposit_by_day.get(d, 0)))
    return days


def test_restart_proof_engine_scoring_is_identical_live_and_after_restart(tmp_path):
    """Required proof #6, end to end: score the exact same real history
    twice -- once against a live store, once against the same store
    reopened cold (a simulated validator restart, no cached state) -- for
    an eval day well AFTER the real transition happened. Both the store's
    own durable answer and the engine's scored output must be identical."""
    coldkey = "5RestartProofColdkeyAAAAAAAAAAAAAAAAAAAAAAAAAA"
    hotkey = "5RestartProofHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    path = tmp_path / "restart_engine.db"
    anchor = 1767571200
    day_secs = 86_400
    deposits = {3: 100}  # well under the 0.5 target on a pay of 1000

    live = Store.create(path, day_anchor_ts=anchor)
    live.record_attributed_pay(
        coldkey=coldkey,
        hotkey=hotkey,
        uid=1,
        block_timestamp=anchor + 3 * day_secs,
        amount_rao=1000,
        block=1000,
        block_hash="0xa",
    )
    live_first_spendable = live.first_spendable_pay_day_for_hotkey(coldkey, hotkey)
    assert live_first_spendable == 3
    live_days = _days_from_store(live, coldkey, hotkey, range(1, 11), deposits)
    live_fx = parse_fixture(
        make_fixture([miner(coldkey, 1, "400000000000", live_days, faw="1")], "10")
    )
    live_result = score_fixture(live_fx)["miners"][0]
    live._conn.close()

    restarted = Store.open(path, day_anchor_ts=anchor)
    restarted_first_spendable = restarted.first_spendable_pay_day_for_hotkey(coldkey, hotkey)
    assert restarted_first_spendable == live_first_spendable == 3
    restarted_days = _days_from_store(restarted, coldkey, hotkey, range(1, 11), deposits)
    assert restarted_days == live_days
    restarted_fx = parse_fixture(
        make_fixture([miner(coldkey, 1, "400000000000", restarted_days, faw="1")], "10")
    )
    restarted_result = score_fixture(restarted_fx)["miners"][0]
    assert restarted_result == live_result
    # Eval day 10 is well after the real transition (day 3): confirm this is
    # genuinely post-transition, measured scoring, not a starting-treatment
    # 1.0 by coincidence.
    assert int(restarted_result["multiplierFp"]) < FP_SCALE


def test_reregistration_proof_engine_scoring_does_not_restore_starting_treatment(tmp_path):
    """Required proof #7: the same hotkey re-registers under a new uid
    after already receiving real spendable pay under the old one. The
    store's own answer -- and therefore the engine's scoring for a later
    eval day -- must not revert to starting treatment; pay history is kept
    by hotkey, not uid."""
    coldkey = "5ReregColdkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    hotkey = "5ReregHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    anchor = 1767571200
    day_secs = 86_400
    store = Store.create(tmp_path / "rereg.db", day_anchor_ts=anchor)
    store.record_attributed_pay(
        coldkey=coldkey,
        hotkey=hotkey,
        uid=5,
        block_timestamp=anchor + 2 * day_secs,
        amount_rao=1000,
        block=2000,
        block_hash="0xb",
    )
    # Re-registration: the same hotkey resurfaces under a different uid on a
    # later day; deliberately no NEW pay recorded at that event, so any
    # "restored starting treatment" could only come from the re-registration
    # itself, never from a second real payment.
    store.record_attributed_pay(
        coldkey=coldkey,
        hotkey=hotkey,
        uid=77,
        block_timestamp=anchor + 6 * day_secs,
        amount_rao=0,
        block=6000,
        block_hash="0xc",
    )
    assert store.first_spendable_pay_day_for_hotkey(coldkey, hotkey) == 2

    days = _days_from_store(store, coldkey, hotkey, range(1, 11), {2: 100})
    fx = parse_fixture(make_fixture([miner(coldkey, 77, "400000000000", days, faw="1")], "10"))
    result = score_fixture(fx)["miners"][0]
    # Eval day 10 is well after the real (day 2) transition: genuinely
    # measured, not held at the flat starting-treatment value the
    # re-registration event might otherwise have restored.
    assert int(result["multiplierFp"]) < FP_SCALE


def test_shared_wallet_two_seats_transition_independently_with_no_leakage():
    """Required proof #8: one coldkey, two hotkey seats. One seat has
    already transitioned (and is genuinely measured and punished); the
    sibling seat has never been paid (still starting treatment). #112,
    D-138 reopened, allows exactly this shape -- more than one row per
    coldkey -- and each row must be scored completely independently, with
    zero leakage in either direction."""
    paid_seat_days = {
        "1": day("1000", "100"),  # this seat's own transition day, underdeposited
        "2": day("1000", "0"),  # post-transition, stops depositing entirely
    }
    unpaid_seat_days = {"1": day("0", "0"), "2": day("0", "0")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5Shared", 10, "400000000000", paid_seat_days, faw="1"),
                miner("5Shared", 11, "400000000000", unpaid_seat_days, faw="1"),
            ],
            "2",
        )
    )
    out = score_fixture(fx)
    paid_seat = next(m for m in out["miners"] if m["uid"] == 10)
    unpaid_seat = next(m for m in out["miners"] if m["uid"] == 11)
    # The paid seat transitioned on day 1 and is genuinely measured and
    # punished by day 2 (underdeposited, then stopped entirely).
    assert int(paid_seat["multiplierFp"]) < FP_SCALE
    # The sibling seat, same coldkey, never paid: still starting treatment,
    # completely unaffected by its sibling's own real pay -- no leakage.
    assert unpaid_seat["pciFp"] == "0"
    assert unpaid_seat["multiplierFp"] == str(FP_SCALE)


# ---------------------------------------------------------------------------
# #16/#135: the noon-schedule pairing correction. Drew's comment 5764297828
# on #135 (2026-09-21): the daily automated contribution runs at noon UTC
# and pays out the PRIOR UTC day's eligible earnings, so a deposit made on
# day d must be judged against day d-1's spendable pay, never day d's own.
# Required verification below, per Drew's item 6 checklist and the #16 task
# brief: item 1 (new miner), item 3 (the worked Tuesday/Wednesday example,
# decoupled from Wednesday's own earnings), item 4 (shared wallet composes
# with the shift), item 7 (zero-earnings/no-fault rules preserved), item 8
# (a controlled-day-index test of the date-selection logic itself).
# ---------------------------------------------------------------------------


def test_unfunded_miner_gets_no_entry_credit_and_no_noah_credit():
    """Required proof, item 1 (unfunded half): a registered miner with no
    real confirmed entry deposit (entryDepositRao "0") and no deposit
    activity at all never gets NOAH credit from either mechanism -- the
    entry-deposit exemption never fires with nothing behind it, and the
    day-to-day pairing has nothing to pair either."""
    fx = parse_fixture(make_fixture([miner("5Unfunded", 0, "400000000000", {"1": day("0", "0")})]))
    m = score_fixture(fx)["miners"][0]
    assert m["noahCreditRao"] == "0"
    assert m["baseFp"] == "0"
    assert m["pciFp"] == "0"


def test_new_miner_confirmed_entry_deposit_qualifies():
    """Required proof, item 1 (funded half): a newly registered miner's
    confirmed entry deposit is credited on its registration day, exempt
    from that day's pay cap (there is no prior day's pay to cap it against
    yet either way) -- the real mechanism that lets a properly entered
    miner receive its first allocation, per Drew's starting-treatment
    direction. Contrasted directly against the identical unfunded miner
    above: same days, only entryDepositRao differs."""
    fx = parse_fixture(
        make_fixture([miner("5Funded", 0, "400000000000", {"1": day("0", "0")}, entry="1000")])
    )
    m = score_fixture(fx)["miners"][0]
    assert m["noahCreditRao"] == "1000"
    assert int(m["baseFp"]) > 0
    # Never yet paid: starting treatment holds the multiplier at 1.0, the
    # exact mechanism that lets this miner receive a first allocation.
    assert m["multiplierFp"] == str(FP_SCALE)


def test_wednesday_recognizes_tuesdays_ninety_percent_regardless_of_wednesdays_own_pay():
    """The core required proof (#16/#135, Drew's own worked example, item 3
    of his verification checklist): a miner earns 100 eligible NOAH on
    Tuesday and contributes 90 on Wednesday. Wednesday's close (evalDay =
    Wednesday) must recognize this as 90% of TUESDAY's 100, regardless of
    what Wednesday's OWN earnings are.

    This is a controlled-day-index (fixture) test of the date-selection
    logic itself -- day 1 = Monday (registration), day 2 = Tuesday (the
    real 100 NOAH pay), day 3 = Wednesday (eval; the 90 NOAH deposit,
    contributed the day after Tuesday's close per the noon schedule). It is
    NOT proof of real elapsed time, real chain emissions, or the live
    noon-automation schedule itself -- those are #14/#111's domain.

    Three sub-cases, Wednesday's OWN pay at 200, 20, and 0: the recognized
    ratio (pciFp) and the credited amount (noahCreditRao) must be IDENTICAL
    in all three, proving the pairing is genuinely decoupled from
    Wednesday's own pay -- not an accident of similar magnitudes.
    """
    for wednesdays_own_pay in ("200", "20", "0"):
        days = {
            "1": day("0", "0"),  # Monday: registration, no real pay yet
            "2": day("100", "0"),  # Tuesday: the 100 NOAH real eligible pay
            "3": day(wednesdays_own_pay, "90"),  # Wednesday (eval): 90 deposited
        }
        fx = parse_fixture(
            make_fixture([miner("5Worked", 0, "400000000000", days, entry="0")], "3")
        )
        m = score_fixture(fx)["miners"][0]
        # 90 recognized against Tuesday's 100, never against Wednesday's own
        # pay (200, 20, or 0 -- all three give the identical answer).
        assert m["noahCreditRao"] == "90", wednesdays_own_pay
        # #290: the index counts a deposit towards need once; the 40 above
        # this day's need (50 of Tuesday's 100 at the 0.5 target) waits in the
        # prepaid reserve. Tuesday's pay is still the leg it is measured
        # against: 50 / 100.
        assert m["pciFp"] == "500000000", wednesdays_own_pay


def test_no_fault_zero_pay_day_shifts_with_the_pay_leg_it_excuses():
    """Required proof, items 7 and 8 (zero-earnings days preserved; the
    no_fault resolution from #16's own assessment comment, confirmed here):
    classify_day's `no_fault` argument answers whether THE DAY WHOSE PAY IS
    BEING JUDGED AS ZERO is excused. Under the shift that day is now d-1,
    not d, so `no_fault` must be read from day d-1's own record, not day
    d's -- this test fails (wrongly MISSes or wrongly NEUTRALs) if that
    shift is not applied together with the pay leg.

    Day 1: real pay 10 (no deposit yet -- establishes a prior day).
    Day 2: deposit 5, judged against day 1's real pay 10 -- HIT, run -> 1.
        Day 2's OWN pay is 0 and its OWN noFault is true -- this is the
        fact that must excuse whichever LATER day uses day 2 as its prior.
    Day 3: judged against day 2's pay (0) and day 2's OWN no_fault (true,
        excused) -- must be NEUTRAL, not MISS: the run must still be 1.
        Day 3's OWN pay is also 0, also excused (true), so it too can excuse
        day 4's judgment.
    Day 4 (eval): judged against day 3's pay (0) and day 3's OWN no_fault
        (true, excused) -- must also be NEUTRAL: the run must still be 1,
        not reset to 0.
    """
    days_through_3 = {
        "1": day("10", "0"),
        "2": day("0", "5", no_fault=True),
        "3": day("0", "0", no_fault=True),
    }
    fx3 = parse_fixture(make_fixture([miner("5NF", 0, "400000000000", dict(days_through_3))], "3"))
    m3 = score_fixture(fx3)["miners"][0]
    # After day 3 (excused, neutral): run is still 1, unchanged since day 2.
    assert m3["streakFp"] == str(FP_SCALE + FP_SCALE // 365)

    days_through_4 = dict(days_through_3, **{"4": day("0", "0", no_fault=False)})
    fx4 = parse_fixture(make_fixture([miner("5NF", 0, "400000000000", days_through_4)], "4"))
    m4 = score_fixture(fx4)["miners"][0]
    # Day 4 is judged against day 3's pay (0) and day 3's OWN no_fault
    # (true): still NEUTRAL, run still 1 -- confirming day 4's own noFault
    # value (false) is correctly ignored; only day 3's own flag matters here.
    assert m4["streakFp"] == str(FP_SCALE + FP_SCALE // 365)

    # Contrast: if the day supplying the zero pay was NOT excused, the
    # judged day is a behavioral miss and the run resets to 0.
    days_at_fault = {
        "1": day("10", "0"),
        "2": day("0", "5", no_fault=False),
        "3": day("0", "0", no_fault=False),
    }
    fx_fault = parse_fixture(
        make_fixture([miner("5F", 0, "400000000000", days_at_fault)], "3")
    )
    m_fault = score_fixture(fx_fault)["miners"][0]
    assert m_fault["streakFp"] == str(FP_SCALE)  # run reset to 0


def test_shared_wallet_deposit_does_not_leak_to_sibling_seat_under_shifted_pairing():
    """Required proof, item 4: one coldkey, two hotkey seats, each with its
    own real pay and deposit history, scored under the day-d/day-(d-1)
    pairing. One seat's deposit must not qualify or credit the other --
    confirming the already-merged multi-seat coldkey fix (5880726) composes
    correctly with this new fix, not a regression."""
    # Seat A: earns 100 on day 1, deposits 90 on day 2 (eval) -- a real hit.
    seat_a_days = {"1": day("100", "0"), "2": day("0", "90")}
    # Seat B: earns 100 on day 1 too, but deposits NOTHING on day 2 -- a
    # miss, and critically must not be silently credited by seat A's 90.
    seat_b_days = {"1": day("100", "0"), "2": day("0", "0")}
    fx = parse_fixture(
        make_fixture(
            [
                miner("5Shared", 20, "400000000000", seat_a_days, faw="1"),
                miner("5Shared", 21, "400000000000", seat_b_days, faw="1"),
            ],
            "2",
        )
    )
    out = score_fixture(fx)
    seat_a = next(m for m in out["miners"] if m["uid"] == 20)
    seat_b = next(m for m in out["miners"] if m["uid"] == 21)
    assert seat_a["noahCreditRao"] == "90"
    # 90 deposited, 50 of it counts towards the index (target 0.5 of 100);
    # the other 40 is seat A's own prepaid reserve, not seat B's.
    assert seat_a["pciFp"] == "500000000"
    assert seat_b["noahCreditRao"] == "0"
    assert seat_b["pciFp"] == "0"
