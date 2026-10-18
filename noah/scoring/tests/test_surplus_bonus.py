"""Above-target bonuses without changing debt, reserve or base-credit accounting."""

import json
import random
from dataclasses import replace
from pathlib import Path

import pytest

from noah.scoring.engine import score_miner
from noah.scoring.fixture_io import DayEntry, Dials, MinerInput, parse_fixture

N = 10**9
DIALS = Dials(N // 2, 2, 2 * N, 45, 2 * N, 90, 0)


def miner(pays, deposits, *, entry=10 * N, foreign=None, locked=None, unlocked=None):
    size = len(pays)
    return MinerInput(
        "miner", 0, 0, 0, entry, 0,
        {d: DayEntry(pays[d], (locked or [0] * size)[d],
                     (unlocked or [0] * size)[d], deposits[d],
                     (foreign or [0] * size)[d], True) for d in range(size)},
    )


@pytest.mark.parametrize("pct,expected", [
    (0, 0), (25, 250_000_000), (50, N), (55, 1_210_000_000),
    (60, 1_440_000_000), (70, 1_960_000_000), (71, 2 * N), (100, 2 * N),
])
def test_exact_bonus_curve(pct, expected):
    r = score_miner(miner([100 * N, 0], [0, pct * N]), DIALS, 1)
    assert r.multiplier_fp == expected


def test_existing_catchup_counts_but_repayment_alone_is_not_a_bonus():
    pays = [200 * N] + [0] * 61
    for deposit in [100 * N, 120 * N]:
        m = miner(pays, [0] * 61 + [deposit])
        r = score_miner(m, DIALS, 61)
        old = score_miner(m, replace(DIALS, cap_fp=N), 61)
        assert r.debt_rao == old.debt_rao == 0
        assert r.debt_weight_rao == old.debt_weight_rao == 0
        assert r.score_fp == old.score_fp
        if deposit == 100 * N:
            assert r == old  # no false freshness bonus on old debt
            assert r.pci_fp <= DIALS.target_fp
        else:
            assert r.multiplier_fp == 1_440_000_000


def test_unpaid_catchup_keeps_aged_compliance_penalty():
    m = miner([200 * N] + [0] * 61, [0] * 61 + [90 * N])
    r = score_miner(m, DIALS, 61)
    old = score_miner(m, replace(DIALS, cap_fp=N), 61)
    assert r == old
    assert r.debt_rao == 10 * N
    assert r.multiplier_fp < N


def test_bonus_does_not_count_reserve_draws_twice():
    m = miner([100 * N] * 4, [120 * N, 0, 0, 0])
    a, b, c = [score_miner(m, DIALS, d) for d in (1, 2, 3)]
    assert a.multiplier_fp == 2 * N
    assert b.pci_fp == 600_000_000
    assert b.multiplier_fp == 1_440_000_000
    assert b.coverage_reserve_rao == 20 * N
    assert c.debt_rao == 30 * N
    assert c.multiplier_fp < N


def test_equivalent_regular_prepaid_and_catchup_bonus():
    schedules = [[0, 60 * N, 60 * N], [120 * N, 0, 0], [0, 0, 120 * N]]
    results = [score_miner(miner([100 * N, 100 * N, 0], ds), DIALS, 2)
               for ds in schedules]
    assert {r.pci_fp for r in results} == {600_000_000}
    assert {r.multiplier_fp for r in results} == {1_440_000_000}
    assert {r.debt_rao for r in results} == {0}
    assert {r.coverage_reserve_rao for r in results} == {20 * N}


def test_entry_foreign_and_today_pay_do_not_inflate_bonus():
    m = miner([100 * N, 10_000 * N], [0, 50 * N],
              entry=100 * N, foreign=[0, 100 * N])
    assert score_miner(m, DIALS, 1).multiplier_fp == N
    m = replace(m, days={**m.days, 1: replace(m.days[1], noah_deposit_rao=60 * N)})
    assert score_miner(m, DIALS, 1).multiplier_fp == 1_440_000_000


def test_unspendable_pay_only_counts_after_unlock_and_prior_day_pairing():
    m = miner([0, 0, 0], [60 * N, 0, 0],
              locked=[100 * N, 0, 0], unlocked=[0, 100 * N, 0])
    assert score_miner(m, DIALS, 0).multiplier_fp == N
    assert score_miner(m, DIALS, 1).multiplier_fp == N
    assert score_miner(m, DIALS, 2).multiplier_fp == 1_440_000_000


def test_huge_prepayment_saturates_safely_and_no_earnings_stays_at_one():
    m = miner([1, 0], [10**40, 0])
    assert score_miner(m, DIALS, 0).multiplier_fp == N
    r = score_miner(m, DIALS, 1)
    assert r.pci_fp == (1 << 64) - 1
    assert r.multiplier_fp == 2 * N


def test_reserves_base_gate_streak_and_debt_unchanged_over_random_histories():
    rng = random.Random(20261017)
    fields = ("credit_reserve_rao", "coverage_reserve_rao", "debt_rao",
              "debt_weight_rao", "base_fp", "score_fp", "streak_fp",
              "noah_credit_rao", "foreign_credit_rao", "door_open")
    for _ in range(100):
        m = miner([rng.randrange(100) * N for _ in range(30)],
                  [rng.randrange(100) * N for _ in range(30)])
        for day in range(30):
            new = score_miner(m, DIALS, day)
            old = score_miner(m, replace(DIALS, cap_fp=N), day)
            assert all(getattr(new, f) == getattr(old, f) for f in fields)
            assert old.multiplier_fp <= new.multiplier_fp <= 2 * N
            if new.debt_rao:
                assert new == old


def test_public_profile_matches_shared_bonus_policy():
    root = Path(__file__).resolve().parents[3]
    profile = json.loads(
        (root / "docs/parameters/profiles/mainnet-production-v1.json").read_text()
    )
    fixture = json.loads((root / "fixtures/shared/surplus-bonus-2x.json").read_text())
    assert profile["profileVersion"] == "mainnet-production-bonus-2x-v1"
    assert profile["capFp"] == "2000000000"
    assert profile["entryDepositPerHotkeyRao"] == "10000000000"
    for field, value in fixture["dials"].items():
        assert profile[field] == value


def test_shared_bonus_fixture_has_exact_expected_multipliers():
    root = Path(__file__).resolve().parents[3]
    raw = json.loads((root / "fixtures/shared/surplus-bonus-2x.json").read_text())
    fx = parse_fixture(raw)
    assert [score_miner(m, fx.dials, fx.eval_day).multiplier_fp for m in fx.miners] == [
        N, 1_210_000_000, 1_440_000_000, 1_960_000_000, 2 * N, 2 * N,
    ]
