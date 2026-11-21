"""Atomic per-identity daily settlement against the candidate migration DB.

This is a local ledger, not a publisher. A pending price returns HELD for that
identity without treating it as a zero deposit. Callers may settle other
identities independently. Never publish a held record as a fresh calculation.
"""

import json
import sqlite3
from dataclasses import asdict

from .ledger import State, settle
from .migration import digest, encoded


class SettlementError(ValueError):
    pass


def apply_day(path, record, *, expected_migration_digest):
    if set(record) != {"day", "hotkey", "coldkey", "earned_rao", "receipts", "source_sha256"}:
        raise SettlementError("invalid daily record")
    for k in ["day", "earned_rao"]:
        if type(record[k]) is not int or record[k] < 0:
            raise SettlementError("invalid daily integer")
    if not isinstance(record["receipts"], list):
        raise SettlementError("invalid receipts")
    import re

    if not isinstance(record["source_sha256"], str) or not re.fullmatch(
        "[0-9a-f]{64}", record["source_sha256"]
    ):
        raise SettlementError("missing source digest")
    ids = set()
    total = 0
    pending = []
    for r in record["receipts"]:
        if set(r) != {"id", "block", "credit_rao", "status", "valuation_sha256"}:
            raise SettlementError("invalid receipt fields")
        if not isinstance(r["id"], str) or not r["id"] or r["id"] in ids:
            raise SettlementError("duplicate receipt in daily input")
        if type(r["block"]) is not int or r["block"] < 0:
            raise SettlementError("invalid receipt block")
        ids.add(r["id"])
        if r["status"] not in ["accepted", "pending", "rejected"]:
            raise SettlementError("invalid valuation status")
        if type(r["credit_rao"]) is not int or r["credit_rao"] < 0:
            raise SettlementError("invalid credit")
        if r["status"] != "accepted" and r["credit_rao"] != 0:
            raise SettlementError("unaccepted receipt cannot carry credit")
        if not isinstance(r["valuation_sha256"], str) or not re.fullmatch(
            "[0-9a-f]{64}", r["valuation_sha256"]
        ):
            raise SettlementError("missing valuation digest")
        if r["status"] == "pending":
            pending.append(r["id"])
        if r["status"] == "accepted":
            total += r["credit_rao"]
    # mode=rw: never create a candidate DB by accidentally naming a live path.
    from pathlib import Path

    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True) as db:
        db.execute("BEGIN IMMEDIATE")
        migration = db.execute("SELECT digest,body FROM points_migration WHERE id=1").fetchone()
        if migration is None or migration[0] != expected_migration_digest:
            raise SettlementError("wrong migration baseline")
        row = db.execute(
            "SELECT coldkey,body FROM points_states WHERE hotkey=?", (record["hotkey"],)
        ).fetchone()
        if row is None or row[0] != record["coldkey"]:
            raise SettlementError("identity mismatch or unregistered lineage")
        baseline = json.loads(migration[1])
        consumed = set(baseline["consumed_receipt_ids"])
        if any(
            r["id"] in consumed or r["block"] <= baseline["cutoff_block"]
            for r in record["receipts"]
        ):
            raise SettlementError("receipt predates migration or was already consumed")
        if pending:
            return dict(status="held", publishable=False, pending_receipts=pending)
        db.execute(
            "CREATE TABLE IF NOT EXISTS points_days (hotkey TEXT NOT NULL,day INTEGER NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(hotkey,day))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS points_receipts (id TEXT PRIMARY KEY,hotkey TEXT NOT NULL,day INTEGER NOT NULL,body TEXT NOT NULL)"
        )
        old = db.execute(
            "SELECT digest,result FROM points_days WHERE hotkey=? AND day=?",
            (record["hotkey"], record["day"]),
        ).fetchone()
        fingerprint = digest(record)
        if old:
            if old[0] != fingerprint:
                raise SettlementError(
                    "changed replay requires explicit correction, not double credit"
                )
            return dict(status="already-settled", publishable=True, state=json.loads(old[1]))
        last = db.execute(
            "SELECT max(day) FROM points_days WHERE hotkey=?", (record["hotkey"],)
        ).fetchone()[0]
        if last is None:
            last = json.loads(migration[1])["cutoff_day"]
        if record["day"] != last + 1:
            raise SettlementError("missing or out-of-order daily history")
        for receipt in record["receipts"]:
            if db.execute("SELECT 1 FROM points_receipts WHERE id=?", (receipt["id"],)).fetchone():
                raise SettlementError("receipt already used")
        state = settle(State(**json.loads(row[1])), record["earned_rao"], total)
        body = encoded(asdict(state)).decode()
        for receipt in record["receipts"]:
            db.execute(
                "INSERT INTO points_receipts VALUES (?,?,?,?)",
                (receipt["id"], record["hotkey"], record["day"], encoded(receipt).decode()),
            )
        db.execute(
            "INSERT INTO points_days VALUES (?,?,?,?)",
            (record["hotkey"], record["day"], fingerprint, body),
        )
        db.execute("UPDATE points_states SET body=? WHERE hotkey=?", (body, record["hotkey"]))
    return dict(status="settled", publishable=True, state=asdict(state))
