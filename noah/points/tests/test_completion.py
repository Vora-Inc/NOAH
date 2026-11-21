import copy
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from noah.points.engine import POLICY
from noah.points.ledger import Q, R, State, settle
from noah.points.migration import MigrationError, install, validate
from noah.points.pricing import PriceInterval
from noah.points.valuation import credit


class Debt(unittest.TestCase):
    def test_first_debt_clearing_without_new_earnings_starts_once(self):
        s = settle(State(), 5 * R, 0)
        t = settle(s, 0, 2500000000)
        self.assertEqual((t.balance, t.qualified, t.points, t.streak), (0, 1, 4 * Q, 0))
        for i in range(30):
            t = settle(t, 0, 0)
        self.assertEqual((t.qualified, t.points), (1, 4 * Q))

    def test_donation_without_earned_obligation_is_not_bootstrap(self):
        self.assertEqual(settle(State(), 0, 500 * R).points, 0)

    def test_partial_first_catchup_does_not_grant_start(self):
        s = settle(State(), 5 * R, 0)
        s = settle(s, 0, R)
        self.assertEqual((s.points, s.qualified), (0, 0))
        s = settle(s, 0, 1500000000)
        self.assertEqual(s.points, 4 * Q)

    def test_zero_new_obligation_with_old_debt_still_second_miss(self):
        s = settle(State(qualified=30, streak=30), 100 * R, 50 * R)
        a = settle(s, 100 * R, 0)
        b = settle(a, 0, 25 * R)
        self.assertEqual((a.points, b.points, b.balance), (40 * Q, 0, -25 * R))

    def test_catchup_mature_does_not_add_zero_earnings_day(self):
        s = settle(State(qualified=30), 100 * R, 0)
        t = settle(s, 0, 50 * R)
        self.assertEqual((t.qualified, t.streak, t.points), (30, 0, 40 * Q))


class Migration(unittest.TestCase):
    def plan(self):
        s = settle(State(), 100 * R, 75 * R)
        return dict(
            schema="noah-points-migration/1",
            policy=POLICY,
            genesis="0x" + "1" * 64,
            cutoff_hash="0x" + "2" * 64,
            cutoff_block=100,
            cutoff_day=10,
            source_sha256="3" * 64,
            consumed_receipt_ids=["already-migrated"],
            miners=[
                dict(
                    uid=1,
                    coldkey="5" + "A" * 47,
                    hotkey="5" + "B" * 47,
                    state=asdict(s),
                    legacy_coverage_rao=25 * R,
                    legacy_debt_rao=0,
                )
            ],
        )

    def apply(self, path, p):
        return install(
            path, p, expected_genesis="0x" + "1" * 64, expected_cutoff_hash="0x" + "2" * 64
        )

    def test_apply_restart_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "candidate.db"
            plan = self.plan()
            self.assertEqual(self.apply(p, plan), "installed")
            self.assertEqual(self.apply(p, plan), "already-installed")
            with sqlite3.connect(p) as db:
                self.assertEqual(db.execute("select count(*) from points_states").fetchone()[0], 1)

    def test_changed_cutoff_refused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "candidate.db"
            p = self.plan()
            self.apply(path, p)
            p["cutoff_day"] += 1
            with self.assertRaises(MigrationError):
                self.apply(path, p)
            self.assertEqual(self.apply(path, self.plan()), "already-installed")

    def test_modified_state_never_reset_on_restart(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "candidate.db"
            p = self.plan()
            self.apply(path, p)
            with sqlite3.connect(path) as db:
                db.execute("update points_states set body='{}'")
            with self.assertRaises(MigrationError):
                self.apply(path, p)

    def test_preserves_unrelated_database(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "other.db"
            with sqlite3.connect(path) as db:
                db.execute("create table history (value text)")
                db.execute("insert into history values ('keep')")
            with self.assertRaises(MigrationError):
                self.apply(path, self.plan())
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("select value from history").fetchone()[0], "keep")

    def test_duplicate_identity_and_reduced_coverage_refused(self):
        for change in ["duplicate", "coverage", "chain", "debt"]:
            p = self.plan()
            if change == "duplicate":
                p["miners"].append(copy.deepcopy(p["miners"][0]))
            if change == "coverage":
                p["miners"][0]["legacy_coverage_rao"] += 1
            if change == "chain":
                p["genesis"] = "0x" + "4" * 64
            if change == "debt":
                p["miners"][0]["legacy_coverage_rao"] = 0
                p["miners"][0]["legacy_debt_rao"] = 1
            with self.subTest(change=change), self.assertRaises(MigrationError):
                validate(p, expected_genesis="0x" + "1" * 64, expected_cutoff_hash="0x" + "2" * 64)

    def test_interruption_rolls_back_every_row(self):
        # Abort insertion with a SQLite trigger, demonstrating transaction atomicity.
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "candidate.db"
            with sqlite3.connect(p) as db:
                db.execute(
                    "create table points_migration (id integer primary key,digest text,body text)"
                )
                db.execute(
                    "create table points_states (hotkey text primary key,coldkey text,uid integer unique,body text)"
                )
                db.execute(
                    "create trigger fail before insert on points_states begin select raise(abort,'simulated crash'); end"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.apply(p, self.plan())
            with sqlite3.connect(p) as db:
                self.assertEqual(
                    db.execute("select count(*) from points_migration").fetchone()[0], 0
                )
                db.execute("drop trigger fail")
            self.assertEqual(self.apply(p, self.plan()), "installed")


class PricingSafeguards(unittest.TestCase):
    def args(self):
        return dict(
            receipt_id="event-1",
            amount_rao=25 * R,
            netuid=51,
            timestamp=172800,
            intervals=[PriceInterval(86400, 172800, 50000000, 100000000)],
        )

    def test_actual_received_at_daily_reference_no_quote_haircut(self):
        self.assertEqual(credit(**self.args())["credit_rao"], 50 * R)

    def test_large_deposit_no_pool_limit(self):
        a = self.args()
        a["amount_rao"] = 10**30
        self.assertEqual(credit(**a)["credit_rao"], 2 * 10**30)

    def test_no_token_whitelist(self):
        a = self.args()
        a["netuid"] = 99
        self.assertEqual(credit(**a)["credit_rao"], 50 * R)

    def test_missing_reference_pending_not_zero_credit(self):
        a = self.args()
        a["intervals"] = []
        self.assertEqual(credit(**a)["status"], "pending")

    def test_root_equal_value(self):
        a = self.args()
        a.update(
            netuid=0, amount_rao=2500000000, intervals=[PriceInterval(86400, 172800, 50000000, R)]
        )
        self.assertEqual(credit(**a)["credit_rao"], 50 * R)

    def test_invalid_amount_rejected(self):
        a = self.args()
        a["amount_rao"] = -1
        self.assertEqual(credit(**a)["status"], "rejected")
