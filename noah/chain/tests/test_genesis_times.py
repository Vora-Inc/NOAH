"""noah.chain.genesis_times: the fixed block-1 time for chains whose identity is
permanent (issue #237, item B)."""

import datetime
import unittest

from noah.chain.genesis_times import (
    BLOCK_ONE_TIME_BY_GENESIS_HASH,
    MAINNET_BLOCK_ONE_TIME,
    MAINNET_GENESIS_HASH,
    fixed_block_one_time,
)
from noah.chain.store import DAY_SECONDS


class FixedBlockOneTimeTests(unittest.TestCase):
    def test_mainnet_block_one_is_the_recorded_moment(self):
        moment = datetime.datetime.fromtimestamp(MAINNET_BLOCK_ONE_TIME, datetime.timezone.utc)
        self.assertEqual(moment.isoformat(), "2023-03-20T18:48:00+00:00")

    def test_mainnet_day_zero_is_the_utc_midnight_before_it(self):
        anchor = (MAINNET_BLOCK_ONE_TIME // DAY_SECONDS) * DAY_SECONDS
        moment = datetime.datetime.fromtimestamp(anchor, datetime.timezone.utc)
        self.assertEqual(moment.isoformat(), "2023-03-20T00:00:00+00:00")

    def test_a_listed_hash_answers_in_any_case_and_with_stray_space(self):
        self.assertEqual(fixed_block_one_time(MAINNET_GENESIS_HASH), MAINNET_BLOCK_ONE_TIME)
        self.assertEqual(
            fixed_block_one_time("  " + MAINNET_GENESIS_HASH.upper().replace("0X", "0x") + " "),
            MAINNET_BLOCK_ONE_TIME,
        )

    def test_an_unlisted_hash_has_no_fixed_time(self):
        self.assertIsNone(fixed_block_one_time("0x" + "00" * 32))

    def test_a_value_that_is_not_a_string_has_no_fixed_time(self):
        for value in (None, 0, b"0x00", ["0x00"]):
            self.assertIsNone(fixed_block_one_time(value))

    def test_every_listed_time_is_a_positive_whole_number_of_seconds(self):
        for genesis, moment in BLOCK_ONE_TIME_BY_GENESIS_HASH.items():
            self.assertRegex(genesis, r"^0x[0-9a-f]{64}$")
            self.assertIsInstance(moment, int)
            self.assertGreater(moment, 0)


if __name__ == "__main__":
    unittest.main()
