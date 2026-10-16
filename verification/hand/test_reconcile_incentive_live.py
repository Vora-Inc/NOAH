"""Offline proof for ``reconcile_incentive_live.py``'s real wiring, before
any live chain access: a real, dual-sourced ``AgreeingChain`` (the same
``a_gated_chain`` fixture ``noah/chain/tests/test_agreeing.py`` already
uses to drive ``run_day`` itself), fed a real epoch/pay event, reconciled
against a fake ``Incentive`` storage read.

Run under ``.venv-validator`` (imports ``noah.chain.agreeing``, which is
fine here -- unlike ``noah/core/tests``, this directory is not fenced off
from bittensor):

    .venv-validator/bin/python -m pytest verification/hand/test_reconcile_incentive_live.py -v
"""

import sys
import unittest
from pathlib import Path

_SOFTWARE_NOAH_ROOT = Path(__file__).resolve().parents[2]
_HAND_DIR = Path(__file__).resolve().parent
for _p in (str(_SOFTWARE_NOAH_ROOT), str(_HAND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reconcile_incentive_live as ril  # noqa: E402

from noah.chain.agreeing import AgreeingChain  # noqa: E402
from noah.chain.sources import Refusal  # noqa: E402
from noah.chain.tests._agreeing_helpers import (  # noqa: E402
    EPOCH,
    NETUID,
    FullSource,
    a_gated_chain,
    pay_event,
)


class IncentiveSource(FullSource):
    """A real ``FullSource`` (answers every question ``run_day`` asks),
    plus the new ``incentive(block)`` -- configurable per instance so a
    test can make the two sources disagree about it specifically, the
    same "make exactly one source disagree about exactly one fact" shape
    this file's own docstring already establishes for every other one."""

    def __init__(self, incentive_by_block=None, **kwargs):
        super().__init__(**kwargs)
        self._incentive_by_block = incentive_by_block or {}

    def incentive(self, block):
        return self._incentive_by_block.get(block, [])


def a_gated_chain_with_incentive(archive_incentive, latent_incentive, **kwargs):
    """Two independent ``IncentiveSource``s, agreeing on everything
    ``FullSource`` answers but free to disagree about ``incentive`` --
    built the identical way ``a_gated_chain`` builds its own two sources,
    just with the one new capability wired in."""
    return AgreeingChain(
        sources={
            "archive": IncentiveSource(incentive_by_block=archive_incentive, **kwargs),
            "latent": IncentiveSource(incentive_by_block=latent_incentive, **kwargs),
        },
        endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
    )


class TestFindRealEpochBlock(unittest.TestCase):
    def test_finds_the_real_pay_event_in_range(self):
        chain = a_gated_chain(
            events={EPOCH: [pay_event([700, 300])]},
            head=EPOCH + 10,
            n=2,
        )
        block, events = ril.find_real_epoch_block(chain, NETUID, EPOCH - 5, EPOCH + 5)
        self.assertEqual(block, EPOCH)
        self.assertIn("attributes", events[0])

    def test_raises_when_nothing_in_range_paid(self):
        chain = a_gated_chain(events={}, head=EPOCH + 10, n=2)
        with self.assertRaises(ril.NoEpochBlockFound):
            ril.find_real_epoch_block(chain, NETUID, EPOCH - 5, EPOCH + 5)


class TestReconcileLiveEndToEnd(unittest.TestCase):
    def test_a_genuinely_matching_case_reports_agree(self):
        # Real event: seat 0 gets 700 rao, seat 1 gets 300 rao (pool 1000).
        # Storage's own PerU16 shares, chosen to imply exactly {0: 700, 1: 300}
        # at pool_total_rao=1000: 700/1000*65535 ~= 45874, 300/1000*65535 ~= 19660.
        # Both sources agree on Incentive, matching the ordinary healthy case.
        incentive = {EPOCH: [45874, 19660]}
        chain = a_gated_chain_with_incentive(
            incentive, incentive, events={EPOCH: [pay_event([700, 300])]}, head=EPOCH + 10, n=2
        )
        events = chain.events(EPOCH)

        report = ril.reconcile_live(chain, netuid=NETUID, block=EPOCH, events=events)

        self.assertEqual(report["block"], EPOCH)
        self.assertEqual(report["mechanisms_paid"], [0])
        self.assertTrue(report["agree"])
        self.assertTrue(report["mechanism_reports"][0]["agree"])

    def test_a_real_mismatch_between_event_and_storage_is_caught_not_hidden(self):
        # Storage says seat 1 got almost nothing -- a real disagreement
        # between the EVENT and STORAGE, both sources agreeing WITH EACH
        # OTHER about what storage actually says.
        incentive = {EPOCH: [65000, 100]}
        chain = a_gated_chain_with_incentive(
            incentive, incentive, events={EPOCH: [pay_event([700, 300])]}, head=EPOCH + 10, n=2
        )
        events = chain.events(EPOCH)

        report = ril.reconcile_live(chain, netuid=NETUID, block=EPOCH, events=events)

        self.assertFalse(report["agree"])
        rows = report["mechanism_reports"][0]["rows"]
        by_seat = {row["seat"]: row for row in rows}
        self.assertFalse(by_seat[1]["agree"])

    def test_the_two_sources_disagreeing_about_incentive_itself_is_caught(self):
        """The genuinely new case this round's dual-sourced read exists
        for: not an event-vs-storage mismatch, but the two INDEPENDENT
        SOURCES disagreeing with EACH OTHER about what Incentive storage
        even says. A single-sourced read could not catch this at all --
        it would just trust whichever one answered. Must refuse rather
        than silently pick one side's number.
        """
        chain = a_gated_chain_with_incentive(
            {EPOCH: [45874, 19660]},  # archive says this
            {EPOCH: [1, 2]},  # latent disagrees entirely
            events={EPOCH: [pay_event([700, 300])]},
            head=EPOCH + 10,
            n=2,
        )
        events = chain.events(EPOCH)

        with self.assertRaises(Refusal):
            ril.reconcile_live(chain, netuid=NETUID, block=EPOCH, events=events)


if __name__ == "__main__":
    unittest.main()
