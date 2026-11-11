import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from noah.points.journal import SettlementError, apply_day
from noah.points.ledger import R
from noah.points.migration import digest, install
from noah.points.tests import test_completion


class Journal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "candidate.db"
        self.plan = test_completion.Migration().plan()
        install(
            self.path,
            self.plan,
            expected_genesis=self.plan["genesis"],
            expected_cutoff_hash=self.plan["cutoff_hash"],
        )
        self.pin = digest(self.plan)

    def record(self, day=11):
        return dict(
            day=day,
            hotkey=self.plan["miners"][0]["hotkey"],
            coldkey=self.plan["miners"][0]["coldkey"],
            earned_rao=100 * R,
            receipts=[
                dict(
                    id=f"blockhash/event-{day}",
                    block=100 + day,
                    credit_rao=50 * R,
                    status="accepted",
                    valuation_sha256="4" * 64,
                )
            ],
            source_sha256="5" * 64,
        )

    def apply(self, r):
        return apply_day(self.path, r, expected_migration_digest=self.pin)

    def test_restart_and_exactly_once(self):
        r = self.record()
        a = self.apply(r)
        b = self.apply(r)
        self.assertEqual((a["status"], b["status"]), ("settled", "already-settled"))
        self.assertEqual(a["state"], b["state"])

    def test_duplicate_across_days_rejected(self):
        r = self.record()
        self.apply(r)
        r["day"] = 12
        with self.assertRaises(SettlementError):
            self.apply(r)

    def test_changed_replay_rejected(self):
        r = self.record()
        self.apply(r)
        r["earned_rao"] += 1
        with self.assertRaises(SettlementError):
            self.apply(r)

    def test_gap_and_wrong_identity_rejected(self):
        for kind in ["gap", "identity", "duplicate"]:
            r = self.record()
            if kind == "gap":
                r["day"] = 12
            if kind == "identity":
                r["coldkey"] = "different"
            if kind == "duplicate":
                r["receipts"].append(copy.deepcopy(r["receipts"][0]))
            with self.subTest(kind=kind), self.assertRaises(SettlementError):
                self.apply(r)

    def test_unpriced_receipt_does_not_create_debt_or_bonus(self):
        with sqlite3.connect(self.path) as db:
            before = db.execute("select body from points_states").fetchone()[0]
        r = self.record()
        r["receipts"][0].update(status="pending", credit_rao=0)
        held = self.apply(r)
        self.assertEqual((held["status"], held["publishable"]), ("held", False))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(before, db.execute("select body from points_states").fetchone()[0])
        self.assertEqual(self.apply(self.record())["status"], "settled")

    def test_pending_input_cannot_invent_credit(self):
        r = self.record()
        r["receipts"][0]["status"] = "pending"
        with self.assertRaises(SettlementError):
            self.apply(r)

    def test_wrong_baseline_refused(self):
        with self.assertRaises(SettlementError):
            apply_day(self.path, self.record(), expected_migration_digest="0" * 64)

    def test_crash_during_receipt_insert_is_atomic(self):
        with sqlite3.connect(self.path) as db:
            before = db.execute("select body from points_states").fetchone()[0]
            db.execute(
                "create table points_receipts (id text primary key,hotkey text,day integer,body text)"
            )
            db.execute(
                "create trigger fail before insert on points_receipts begin select raise(abort,'interrupted'); end"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(self.record())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("select body from points_states").fetchone()[0], before)
            db.execute("drop trigger fail")
        self.assertEqual(self.apply(self.record())["status"], "settled")

    def test_progressed_db_never_reset_by_reinstall(self):
        self.apply(self.record())
        with self.assertRaises(ValueError):
            install(
                self.path,
                self.plan,
                expected_genesis=self.plan["genesis"],
                expected_cutoff_hash=self.plan["cutoff_hash"],
            )

    def test_migrated_receipt_and_old_block_rejected(self):
        for kind in ["old-block", "old-id"]:
            r = self.record()
            if kind == "old-block":
                r["receipts"][0]["block"] = 100
            else:
                r["receipts"][0]["id"] = "already-migrated"
            with self.subTest(kind=kind), self.assertRaises(SettlementError):
                self.apply(r)
