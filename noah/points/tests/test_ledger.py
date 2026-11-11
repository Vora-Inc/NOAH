import unittest

from noah.points.ledger import (
    Q,
    R,
    State,
    historical_foreign_credit,
    maturity,
    migration_review,
    settle,
)


class Accounting(unittest.TestCase):
    def test_exact_half_native_independent_of_tao_market(self):
        # Same actual 100 earned and 50 deposited must work at any external market price.
        outcomes = [
            settle(State(), 100 * R, 50 * R) for market_price in [1, 40000000, 50000000, 10**12]
        ]
        self.assertTrue(
            all(s.balance == 0 and s.streak == 1 and s.qualified == 1 for s in outcomes)
        )

    def test_single_unit_for_ratio_coverage_consistency(self):
        s = settle(State(), 100 * R, 60 * R)
        self.assertEqual(
            (s.earned, s.contributed, s.balance, s.contribution, s.streak),
            (100 * R, 60 * R, 10 * R, 8 * Q, 1),
        )

    def test_foreign_equivalent_matches_native_state(self):
        foreign = historical_foreign_credit(25 * R, 100000000, 50000000)
        self.assertEqual(foreign, 50 * R)
        self.assertEqual(settle(State(), 100 * R, foreign), settle(State(), 100 * R, 50 * R))

    def test_root_equivalent_matches_native(self):
        self.assertEqual(historical_foreign_credit(2500000000, R, 50000000), 50 * R)

    def test_mixed_assets_one_obligation(self):
        foreign = historical_foreign_credit(10 * R, 100000000, 50000000)
        self.assertEqual(
            settle(State(), 100 * R, 30 * R + foreign), settle(State(), 100 * R, 50 * R)
        )

    def test_prepaid_not_double_counted(self):
        s = settle(State(), 100 * R, 100 * R)
        t = settle(s, 100 * R, 0)
        self.assertEqual((t.balance, t.contributed, t.streak, t.missed), (0, 100 * R, 0, 0))
        self.assertLess(settle(t, 100 * R, 0).balance, 0)

    def test_prepaid_never_repriced(self):
        s = settle(State(), 100 * R, 100 * R)
        self.assertEqual(settle(s, 60 * R, 0).balance, 20 * R)

    def test_missed_transfer_resets_streak_while_covered(self):
        s = settle(State(qualified=29, streak=29), 100 * R, 100 * R)
        t = settle(s, 100 * R, 0)
        self.assertEqual((t.missed, t.streak, t.consistency, t.qualified), (0, 0, 0, 31))

    def test_two_uncovered_days_half_then_zero(self):
        s = settle(State(qualified=30, streak=30), 100 * R, 50 * R)
        a = settle(s, 100 * R, 0)
        b = settle(a, 100 * R, 0)
        self.assertEqual((a.points, b.points), (s.points // 2, 0))
        self.assertEqual((a.qualified, b.qualified), (s.qualified, s.qualified))

    def test_debt_catchup_no_retroactive_streak(self):
        s = settle(State(qualified=30, streak=30), 100 * R, 50 * R)
        s = settle(settle(s, 100 * R, 0), 100 * R, 0)
        t = settle(s, 100 * R, 150 * R)
        self.assertEqual((t.balance, t.streak, t.points), (0, 1, 40 * Q + 44444))
        self.assertEqual(settle(t, 100 * R, 50 * R).streak, 2)

    def test_partial_catchup_stays_zero(self):
        s = settle(settle(State(), 100 * R, 0), 100 * R, 0)
        self.assertEqual(settle(s, 100 * R, 50 * R).points, 0)

    def test_no_earnings_no_maturity_or_score(self):
        s = settle(State(), 0, 100000 * R)
        self.assertEqual((s.points, s.qualified, s.streak), (0, 0, 0))

    def test_zero_obligation_neutral_streak(self):
        s = settle(State(qualified=12, streak=12), 100 * R, 50 * R)
        t = settle(s, 0, 0)
        self.assertEqual((s.qualified, s.streak), (t.qualified, t.streak))

    def test_no_earnings_does_not_erase_debt(self):
        self.assertEqual(settle(settle(State(), 100 * R, 0), 0, 0).points, 0)

    def test_invalid_monetary_values(self):
        for value in [-1, 1.1, True, "1", None]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    settle(State(), value, 0)
                with self.assertRaises(ValueError):
                    settle(State(), 0, value)

    def test_invalid_foreign_prices(self):
        for p in [-1, 0, True, 0.1]:
            with self.subTest(p=p):
                with self.assertRaises(ValueError):
                    historical_foreign_credit(R, p, R)
                with self.assertRaises(ValueError):
                    historical_foreign_credit(R, R, p)

    def test_two_floor_foreign_rounding(self):
        self.assertEqual(historical_foreign_credit(3, 500000000, 600000000), 1)

    def test_fifty_percent_rounds_down_once_per_day(self):
        s = settle(State(), 101, 50)
        self.assertEqual((s.balance, s.streak), (0, 1))

    def test_zero_rounded_due_does_not_create_maturity(self):
        self.assertEqual(settle(State(), 1, 1).qualified, 0)

    def test_state_is_immutable(self):
        s = State()
        t = settle(s, R, R)
        self.assertEqual(s, State())
        self.assertNotEqual(s, t)


class Points(unittest.TestCase):
    def test_maturity_boundaries(self):
        self.assertEqual(
            [maturity(d) for d in [0, 1, 2, 7, 14, 21, 30, 31]],
            [0, 100000, 131034, 286206, 503448, 720689, 1000000, 1000000],
        )

    def test_maturity_increases_smoothly(self):
        self.assertTrue(all(maturity(d) < maturity(d + 1) for d in range(1, 30)))

    def test_bonus_cannot_bypass_maturity(self):
        s = settle(State(), 100 * R, 10**25)
        self.assertLess(s.points, 13 * Q)
        self.assertEqual(s.contribution, 20 * Q)

    def test_full_points_limit(self):
        self.assertEqual(settle(State(qualified=30, streak=30), 100 * R, 10**25).points, 100 * Q)

    def test_mature_minimum_contributor_gets_eighty(self):
        self.assertEqual(settle(State(qualified=30, streak=30), 100 * R, 50 * R).points, 80 * Q)

    def test_contribution_curve(self):
        for deposited, bonus in [(50, 0), (60, 8), (75, 20), (100, 20), (1000, 20)]:
            self.assertEqual(
                settle(State(qualified=30), 100 * R, deposited * R).contribution, bonus * Q
            )

    def test_quadratic_consistency(self):
        for streak, points in [(9, 3600000), (18, 14400000), (30, 40000000)]:
            self.assertEqual(
                settle(State(qualified=30, streak=streak - 1), 100 * R, 50 * R).consistency, points
            )

    def test_big_payment_only_one_day(self):
        s = settle(State(qualified=8, streak=4), 100 * R, 10**25)
        self.assertEqual((s.qualified, s.streak), (9, 5))

    def test_under_target_deposit_not_streak_even_if_covered(self):
        s = State(earned=100 * R, contributed=100 * R, balance=50 * R, qualified=10, streak=10)
        self.assertEqual(settle(s, 100 * R, 49 * R).streak, 0)

    def test_no_calendar_waiting_maturity(self):
        s = State()
        for i in range(60):
            s = settle(s, 0, 0)
        self.assertEqual((s.qualified, s.maturity, s.points), (0, 0, 0))

    def test_real_maturity_survives_missed_days(self):
        s = State(qualified=15)
        self.assertEqual(settle(settle(s, 100 * R, 0), 100 * R, 0).qualified, 15)


class Migration(unittest.TestCase):
    def test_no_silent_credit_write(self):
        self.assertEqual(migration_review(10, 20, 0)["protection_needed_rao"], 10)

    def test_floor_includes_debt_if_old_credit_positive(self):
        self.assertEqual(migration_review(-10, 20, 0)["protection_needed_rao"], 30)

    def test_no_credit_invention_for_existing_debt(self):
        r = migration_review(-10, 0, 20)
        self.assertEqual((r["protection_needed_rao"], r["debt_change_rao"]), (0, -10))

    def test_conflicting_legacy_balance_rejected(self):
        with self.assertRaises(ValueError):
            migration_review(0, 10, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
