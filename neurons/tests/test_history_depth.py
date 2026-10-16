"""Issue #237, item G: what the startup history check demands of a node.

The check used to probe 300 blocks (one hour) back and call a pass "archive
capable", which a node keeping only an hour of state satisfied while every day
this validator has to read is 7,200 blocks long. It now probes about three
days back, and says what it needs instead of asking for an archive."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from neurons.validator import BLOCKS_PER_DAY, HISTORY_DEPTH  # noqa: E402
from noah.core.reader import ChainReader  # noqa: E402
from noah.core.startup import NotArchiveCapable, check_reads_history  # noqa: E402

HEAD = 5_000_000
OLD_DEPTH = 300


class NodeKeeping:
    """A client whose node keeps state for the newest `retention` blocks. Older
    state reads back as an empty answer, which is how a pruned node behaves."""

    def __init__(self, retention, head=HEAD):
        self.block = head
        self._retention = retention

    def query(self, item, params=None, block=None):
        return 20 if block >= self.block - self._retention else None


class TheDepthIsDays(unittest.TestCase):
    def test_the_depth_is_three_days_of_blocks(self):
        self.assertEqual(HISTORY_DEPTH, 3 * BLOCKS_PER_DAY)
        self.assertEqual(BLOCKS_PER_DAY, 7_200)

    def test_it_is_far_more_than_the_hour_it_used_to_be(self):
        self.assertGreater(HISTORY_DEPTH, 20 * OLD_DEPTH)


class ANodeIsHeldToThatDepth(unittest.TestCase):
    def check(self, retention):
        return check_reads_history(ChainReader(NodeKeeping(retention)), HISTORY_DEPTH)

    def test_a_node_keeping_only_an_hour_is_refused_though_it_passed_before(self):
        one_hour = 300 + 50
        self.assertIsNone(
            check_reads_history(ChainReader(NodeKeeping(one_hour)), OLD_DEPTH),
            "the old depth let an hour-deep node through; this pins why it was not enough",
        )
        with self.assertRaises(NotArchiveCapable):
            self.check(one_hour)

    def test_a_node_keeping_one_day_is_refused(self):
        with self.assertRaises(NotArchiveCapable):
            self.check(BLOCKS_PER_DAY)

    def test_the_boundary_is_exact(self):
        self.assertIsNone(self.check(HISTORY_DEPTH))
        with self.assertRaises(NotArchiveCapable):
            self.check(HISTORY_DEPTH - 1)

    def test_a_mainnet_node_keeping_ninety_days_passes_without_being_an_archive(self):
        self.assertIsNone(self.check(90 * BLOCKS_PER_DAY))

    def test_the_refusal_names_the_depth_it_needs_and_does_not_ask_for_an_archive(self):
        with self.assertRaises(NotArchiveCapable) as caught:
            self.check(BLOCKS_PER_DAY)
        message = str(caught.exception)
        self.assertIn(str(HISTORY_DEPTH), message)
        self.assertNotIn("archive node", message)


if __name__ == "__main__":
    unittest.main()
