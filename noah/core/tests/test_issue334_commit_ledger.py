"""Issue #334: the per-day sealed-commit ledger (residuals R1/R2 of the sealed
`already_published` window scan).

Tick-level tests drive the REAL `loop.tick`, REAL `run_period` and the REAL
sealed `ChainAdapter.already_published` + `ChainReader` (only the substrate
client, the day window and the publish transport are fakes). The fake publish
lands a TimelockedWeightsCommitted event at head+1 and returns a confirmed
result the way `publish_weights` does (block, reveal_round).
"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from ...chain.dayblocks import DayNotComplete
from .. import loop
from ..chain_adapter import DayWindowNotWired
from ..commit_ledger import SCHEMA_VERSION, CommitLedger, CommitLedgerRefused
from ..reader import ChainReader, ChainUnreadable
from ._adapter_helpers import TrivialHotkey, an_adapter
from .test_issue217_sealed_already_published import (
    DAY,
    OURS,
    TEMPO,
    EventClient,
    strict_window,
    to_of,
)
from .test_loop import SchedulableChain

T = to_of


class LedgerChain(SchedulableChain):
    """Real sealed adapter + real tick/run_period. `ledger_path=None` builds the
    adapter without a ledger (today's behaviour)."""

    def __init__(self, head, ledger_path, publish_mode="sealed"):
        super().__init__(head=head, tempo=TEMPO, settle_blocks=360)
        self.head_ref = [head]
        self.client = EventClient(head, TEMPO)
        kwargs = {}
        if ledger_path is not None:
            kwargs["commit_ledger_path"] = ledger_path
        self.adapter, _, _ = an_adapter(
            reader=ChainReader(self.client),
            wallet_hotkey=TrivialHotkey(ss58_address=OURS),
            publish_mode=publish_mode,
            day_window=strict_window(DAY, self.head_ref, None),
            settle_blocks=360,
            **kwargs,
        )
        self.log = []
        self.committed_days = []
        self.crash_before_ledger_write = False

    def set_head(self, h):
        self.head = h
        self.head_ref[0] = h
        self.client.block = h

    def already_published(self, height):
        return self.adapter.already_published(height)

    def read(self, height):
        self.adapter._day_window(height)  # DayNotComplete like the real read()
        return {"height": height}

    def publish(self, vector, height):
        day = self.adapter._day_window(height)[0]
        blk = self.head + 1
        self.client.commits.setdefault(blk, []).append(OURS)
        self.committed_days.append(day)
        self.log.append(f"day {day} (height {height}) committed at block {blk}")
        return {
            "confirmed_on_chain": True,
            "reveal_round": 1,
            "block": blk,
            "extrinsic_index": 2,
        }

    def record_commit(self, height, published):
        if self.crash_before_ledger_write:
            raise SystemExit("process killed between submit and ledger write")
        return self.adapter.record_commit(height, published)


def process(ch, state_dir, pending, last_h, heads):
    """One fresh validator process: seeded from `pending`, ticks at `heads`."""
    sp, lp = state_dir / "loop.json", state_dir / "lock"
    state = dict(last_attempted_height=last_h, last_attempted_tempo=TEMPO)
    dc = {"pending": pending, "checked_pinned": None}
    refused, dz, out = set(), {}, []
    for h in heads:
        ch.set_head(h)
        r, lah = loop.tick(
            ch,
            settle_blocks=360,
            state_path=sp,
            lock_path=lp,
            last_attempted_height=state["last_attempted_height"],
            last_attempted_tempo=state["last_attempted_tempo"],
            refused_heights=refused,
            identity="x",
            day_zero_cache=dz,
            day_catchup=dc,
        )
        state["last_attempted_height"] = lah
        out.append((h, r.outcome))
    return out


class WithState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.ledger = self.dir / "commit-ledger.json"


class R1_CatchUpCommitIsNotTakenForTheNextDay(WithState):
    PENDING = {"day": 10, "height": 78479}

    def run_restart(self, offset, ledger):
        ch = LedgerChain(T(11) + offset, ledger)
        heads = [T(11) + offset + i for i in range(4)] + list(
            range(T(11) + offset + 400, T(11) + 2 * DAY, 360)
        )
        process(ch, self.dir, dict(self.PENDING), 78479, heads)
        return ch

    def test_day_11_is_committed_after_restarts_inside_the_band(self):
        for offset in (1, 300, 719):
            with self.subTest(restart=f"T11+{offset}"):
                d = pathlib.Path(tempfile.mkdtemp())
                self.dir = d
                ch = self.run_restart(offset, d / "commit-ledger.json")
                self.assertIn(10, ch.committed_days, ch.log)
                self.assertIn(11, ch.committed_days, ch.log)
                self.assertEqual(ch.committed_days.count(11), 1)
                data = json.loads((d / "commit-ledger.json").read_text())
                self.assertEqual(sorted(map(int, data["days"]))[:2], [10, 11])
                self.assertEqual(len(ch.committed_days), len(set(ch.committed_days)))

    def test_without_a_ledger_the_old_behaviour_is_unchanged(self):
        """No ledger configured: today's scan, so day 11 is still lost (this
        pins the fallback; it is the R1 defect the ledger exists to fix)."""
        ch = self.run_restart(300, None)
        self.assertIn(10, ch.committed_days)
        self.assertNotIn(11, ch.committed_days)


class R2_LateCommitAndRestart(WithState):
    def test_late_commit_with_ledger_is_not_recommitted_after_restart(self):
        ch = LedgerChain(T(11) + 2000, self.ledger)
        pending = {"day": 10, "height": 78479}
        process(ch, self.dir, pending, 78479, [T(11) + 2000])
        self.assertEqual(ch.committed_days, [10])
        # state file lost / still says pending (crash after the state write
        # would be the same); the ledger survived.
        process(ch, self.dir, dict(pending), 78479, [T(11) + 2100])
        self.assertEqual(ch.committed_days, [10], ch.log)

    def test_crash_between_submit_and_ledger_write_is_the_old_failure_mode(self):
        """Documented residual: the commit is on chain, the ledger has no entry,
        and a restart with the day still pending commits again exactly as it did
        before the ledger existed (never worse: not an exception, not a skipped
        day)."""
        ch = LedgerChain(T(11) + 2000, self.ledger)
        ch.crash_before_ledger_write = True
        pending = {"day": 10, "height": 78479}
        with self.assertRaises(SystemExit):
            process(ch, self.dir, pending, 78479, [T(11) + 2000])
        self.assertEqual(ch.committed_days, [10])
        self.assertFalse(self.ledger.exists())
        ch.crash_before_ledger_write = False
        process(ch, self.dir, dict(pending), 78479, [T(11) + 2100])
        # day 10's commit sits at T11+2001: outside day 10's window
        # [T10+1, T11], so the old scan cannot see it: the duplicate happens.
        self.assertEqual(ch.committed_days, [10, 10])

    def test_ledger_write_failure_does_not_fail_the_published_day(self):
        ch = LedgerChain(T(11) + 300, self.ledger)
        real_replace = os.replace

        def failing_replace(src, dst):
            if pathlib.Path(dst).name == "commit-ledger.json":
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=failing_replace):
            out = process(ch, self.dir, {"day": 10, "height": 78479}, 78479, [T(11) + 300])
        self.assertEqual(ch.committed_days, [10])
        self.assertEqual(out[0][1], "published")
        self.assertFalse(self.ledger.exists())
        self.assertEqual([p.name for p in self.dir.glob("commit-ledger*.tmp*")], [])
        state = loop.read_state(self.dir / "loop.json")
        self.assertFalse(state["result"]["commit_ledger"]["recorded"])
        self.assertIn("disk full", state["result"]["commit_ledger"]["reason"])


def adapter_with_ledger(head, ledger_path, commits=None, day_len=DAY):
    client = EventClient(head, TEMPO, commits)
    head_ref = [head]
    adapter, _, _ = an_adapter(
        reader=ChainReader(client),
        wallet_hotkey=TrivialHotkey(ss58_address=OURS),
        publish_mode="sealed",
        day_window=strict_window(day_len, head_ref, None),
        settle_blocks=360,
        commit_ledger_path=ledger_path,
    )
    return adapter, client


def seed(path, entries, version=SCHEMA_VERSION, netuid=None, hotkey=OURS):
    from ._adapter_helpers import NETUID

    netuid = NETUID if netuid is None else netuid
    path.write_text(
        json.dumps(
            {
                "schema_version": version,
                "days": {
                    str(d): {
                        "block": b,
                        "extrinsic_index": 1,
                        "reveal_round": 1,
                        "hotkey": hotkey,
                        "netuid": netuid,
                        "at": "t",
                    }
                    for d, b in entries.items()
                },
            }
        )
    )


class LedgerFile(WithState):
    def test_corrupt_file_is_refused_not_ignored(self):
        self.ledger.write_text("{not json")
        a, _ = adapter_with_ledger(T(11) + 400, self.ledger)
        with self.assertRaises(CommitLedgerRefused):
            a.already_published(T(10))
        self.assertTrue(issubclass(CommitLedgerRefused, ChainUnreadable))

    def test_newer_schema_version_is_refused(self):
        seed(self.ledger, {10: T(10) + 5}, version=SCHEMA_VERSION + 1)
        a, _ = adapter_with_ledger(T(11) + 400, self.ledger)
        with self.assertRaisesRegex(CommitLedgerRefused, "newer"):
            a.already_published(T(10))

    def test_malformed_entry_is_refused(self):
        self.ledger.write_text(
            json.dumps({"schema_version": 1, "days": {"10": {"block": "x"}}})
        )
        with self.assertRaises(CommitLedgerRefused):
            CommitLedger(self.ledger).load()

    def test_record_never_overwrites_a_corrupt_file(self):
        self.ledger.write_text("garbage")
        with self.assertRaises(CommitLedgerRefused):
            CommitLedger(self.ledger).record(
                day=1, block=5, extrinsic_index=0, reveal_round=1, hotkey=OURS, netuid=1
            )
        self.assertEqual(self.ledger.read_text(), "garbage")

    def test_missing_file_is_exactly_todays_behaviour(self):
        c = T(10) + 400
        a, _ = adapter_with_ledger(T(10) + 700, self.ledger, commits={c: [OURS]})
        self.assertFalse(self.ledger.exists())
        self.assertTrue(a.already_published(T(10)))
        a2, _ = adapter_with_ledger(T(11) + 400, self.ledger, commits={c: [OURS]})
        self.assertFalse(a2.already_published(T(11)))

    def test_loop_waits_on_a_corrupt_ledger_and_commits_nothing(self):
        self.ledger.write_text("{")
        ch = LedgerChain(T(11) + 400, self.ledger)
        out = process(ch, self.dir, None, T(10) - 360, [T(11) + 400])
        self.assertEqual(ch.committed_days, [])
        self.assertNotEqual(out[0][1], "published")


class LedgerAttribution(WithState):
    def test_entry_whose_chain_event_is_absent_allows_recommit(self):
        seed(self.ledger, {10: T(10) + 50})  # reorged away: no event there
        client_commits = {T(10) + 7: [], T(10) + 9: []}  # events exist, no commit
        a, _ = adapter_with_ledger(T(11) + 400, self.ledger, commits=client_commits)
        self.assertFalse(a.already_published(T(10)))

    def test_entry_with_confirming_event_is_published_even_far_outside_the_window(self):
        late = T(12) + 50  # more than a full day after day 10's window [T10+1, T11]
        seed(self.ledger, {10: late})
        a, _ = adapter_with_ledger(T(12) + 400, self.ledger, commits={late: [OURS]})
        self.assertTrue(a.already_published(T(10)))

    def test_entry_block_beyond_head_is_unreadable_not_absent(self):
        seed(self.ledger, {10: T(11) + 50})
        a, _ = adapter_with_ledger(T(11) + 10, self.ledger)
        with self.assertRaises(ChainUnreadable):
            a.already_published(T(10))

    def test_commit_the_ledger_attributes_to_another_day_is_skipped(self):
        c = T(11) + 100  # day 10's catch-up, inside day 11's scan window
        seed(self.ledger, {10: c})
        a, _ = adapter_with_ledger(T(12) + 400, self.ledger, commits={c: [OURS]})
        self.assertFalse(a.already_published(T(11)))

    def test_unattributed_commit_in_window_still_counts(self):
        seed(self.ledger, {9: T(9) + 20})
        c = T(11) + 100
        a, _ = adapter_with_ledger(T(12) + 400, self.ledger, commits={c: [OURS]})
        self.assertTrue(a.already_published(T(11)))

    def test_other_day_skip_applies_to_that_block_only(self):
        c1, c2 = T(11) + 100, T(11) + 900
        seed(self.ledger, {10: c1})
        a, _ = adapter_with_ledger(T(12) + 400, self.ledger, commits={c1: [OURS], c2: [OURS]})
        self.assertTrue(a.already_published(T(11)))

    def test_entries_for_another_hotkey_or_netuid_are_ignored(self):
        c = T(11) + 100
        seed(self.ledger, {10: c}, hotkey="5" + "Z" * 47)
        a, _ = adapter_with_ledger(T(12) + 400, self.ledger, commits={c: [OURS]})
        self.assertTrue(a.already_published(T(11)))

    def test_day_not_complete_and_window_not_wired_still_raise(self):
        seed(self.ledger, {10: T(10) + 5})
        a, _ = adapter_with_ledger(T(11) - 50, self.ledger)
        with self.assertRaises(DayNotComplete):
            a.already_published(T(11))
        client = EventClient(T(11) + 400, TEMPO)
        b, _, _ = an_adapter(
            reader=ChainReader(client),
            wallet_hotkey=TrivialHotkey(ss58_address=OURS),
            publish_mode="sealed",
            day_window=None,
            settle_blocks=360,
            commit_ledger_path=self.ledger,
        )
        with self.assertRaises(DayWindowNotWired):
            b.already_published(T(10))

    def test_unreadable_block_still_raises_with_a_ledger(self):
        from .test_reader import node_down

        seed(self.ledger, {9: T(9) + 5})
        a, client = adapter_with_ledger(T(12) + 400, self.ledger)
        inner = client.query

        def query(item, params=None, block=None):
            if item[1] == "Events" and block == T(11) + 77:
                raise node_down()
            return inner(item, params, block)

        client.query = query
        with self.assertRaises(ChainUnreadable):
            a.already_published(T(11))


class LedgerGaps(WithState):
    def test_other_netuid_entry_is_ignored(self):
        c = T(11) + 100
        seed(self.ledger, {10: c}, netuid=999)
        a, _ = adapter_with_ledger(T(12) + 400, self.ledger, commits={c: [OURS]})
        self.assertTrue(a.already_published(T(11)))

    def test_event_absent_and_scan_finds_nothing_allows_recommit(self):
        seed(self.ledger, {10: T(10) + 50})
        a, _ = adapter_with_ledger(T(11) + 400, self.ledger)
        self.assertFalse(a.already_published(T(10)))

    def test_event_absent_but_scan_finds_unattributed_commit_is_published(self):
        seed(self.ledger, {10: T(10) + 50})  # reorged away
        a, _ = adapter_with_ledger(T(11) + 400, self.ledger, commits={T(10) + 300: [OURS]})
        self.assertTrue(a.already_published(T(10)))

    def test_head_boundary(self):
        b = T(11) + 50
        seed(self.ledger, {10: b})
        a, _ = adapter_with_ledger(b, self.ledger, commits={b: [OURS]})
        self.assertTrue(a.already_published(T(10)))
        seed(self.ledger, {10: b + 1})
        a, _ = adapter_with_ledger(b, self.ledger, commits={b + 1: [OURS]})
        with self.assertRaisesRegex(ChainUnreadable, "moved aside"):
            a.already_published(T(10))


class DirectModeUnchanged(WithState):
    def test_direct_mode_never_reads_or_writes_the_ledger(self):
        self.ledger.write_text("{corrupt")  # would raise if the sealed branch ran
        client = EventClient(T(11) + 400, TEMPO, weights_set={T(10) + 9: [0]})
        head_ref = [T(11) + 400]
        a, _, _ = an_adapter(
            reader=ChainReader(client),
            wallet_hotkey=TrivialHotkey(ss58_address=OURS),
            publish_mode="direct",
            day_window=strict_window(DAY, head_ref, None),
            settle_blocks=360,
            commit_ledger_path=self.ledger,
        )
        try:
            a.already_published(T(10))
        except CommitLedgerRefused:
            self.fail("direct mode touched the sealed commit ledger")
        except Exception:
            pass  # direct mode's own unrelated refusals are not under test
        self.assertEqual(self.ledger.read_text(), "{corrupt")


class RecordCommit(WithState):
    def test_period_result_carries_block_and_extrinsic_index_and_ledger_outcome(self):
        ch = LedgerChain(T(11) + 300, self.ledger)
        process(ch, self.dir, {"day": 10, "height": 78479}, 78479, [T(11) + 300])
        result = loop.read_state(self.dir / "loop.json")["result"]
        self.assertEqual(result["block"], T(11) + 301)
        self.assertEqual(result["extrinsic_index"], 2)
        self.assertTrue(result["commit_ledger"]["recorded"])
        entry = json.loads(self.ledger.read_text())["days"]["10"]
        self.assertEqual(entry["block"], T(11) + 301)
        self.assertEqual(entry["extrinsic_index"], 2)
        self.assertEqual(entry["hotkey"], OURS)

    def test_ledger_is_not_written_when_publish_fails(self):
        ch = LedgerChain(T(11) + 300, self.ledger)

        def boom(vector, height):
            raise RuntimeError("rejected")

        ch.publish = boom
        process(ch, self.dir, {"day": 10, "height": 78479}, 78479, [T(11) + 300])
        self.assertFalse(self.ledger.exists())

    def test_sealed_publish_weights_passes_block_and_extrinsic_index(self):
        from ..submitter import _extrinsic_index_from_id

        self.assertEqual(_extrinsic_index_from_id("8934-0002"), 2)
        self.assertIsNone(_extrinsic_index_from_id(None))
        self.assertIsNone(_extrinsic_index_from_id("garbage"))


if __name__ == "__main__":
    unittest.main()
