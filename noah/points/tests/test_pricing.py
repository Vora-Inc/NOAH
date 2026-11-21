import unittest

from noah.points.ledger import R
from noah.points.pricing import PriceInterval as I
from noah.points.pricing import Unpriced
from noah.points.pricing import daily_average_credit as quote


class Pricing(unittest.TestCase):
    def q(self, rows, **kwargs):
        return quote(25 * R, 51, 2 * 86400, rows)

    def test_native_does_not_depend_on_market_price(self):
        self.assertEqual(quote(50 * R, 82, 2 * 86400, []), 50 * R)

    def test_flat_foreign_equal_credit(self):
        self.assertEqual(self.q([I(86400, 172800, 50000000, 100000000)]), 50 * R)

    def test_time_weighted_not_unweighted_samples(self):
        rows = [I(86400, 108000, 25000000, 100000000), I(108000, 172800, 75000000, 100000000)]
        self.assertEqual(self.q(rows), 40 * R)

    def test_split_intervals_same_result(self):
        self.assertEqual(
            self.q([I(86400, 172800, 50000000, 100000000)]),
            self.q([I(86400, 100000, 50000000, 100000000), I(100000, 172800, 50000000, 100000000)]),
        )

    def test_gap_rejected(self):
        with self.assertRaises(Unpriced):
            self.q([I(86401, 172800, 50000000, 100000000)])

    def test_overlap_rejected(self):
        with self.assertRaises(Unpriced):
            self.q([I(86400, 100000, 50000000, 100000000), I(99000, 172800, 50000000, 100000000)])

    def test_future_rejected(self):
        with self.assertRaises(Unpriced):
            self.q([I(86400, 172801, 50000000, 100000000)])

    def test_missing_day_rejected(self):
        with self.assertRaises(Unpriced):
            self.q([])

    def test_root_equivalence(self):
        self.assertEqual(quote(2500000000, 0, 172800, [I(86400, 172800, 50000000, R)]), 50 * R)

    def test_zero_price_rejected(self):
        with self.assertRaises(Unpriced):
            self.q([I(86400, 172800, 0, 100000000)])


if __name__ == "__main__":
    unittest.main()
