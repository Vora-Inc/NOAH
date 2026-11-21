"""Pinned, exactly-once migration into a NEW candidate state database.

Never reads or updates the live history DB. Existing incompatible DBs and
changed manifests are rejected. The caller supplies trusted expected chain and
cutoff pins; a self-signed input file is not proof of chain authenticity.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import fields

from .engine import POLICY
from .ledger import Q, State, maturity


class MigrationError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def hash32(value):
    return isinstance(value, str) and re.fullmatch("0x[0-9a-f]{64}", value) is not None


def validate(plan, *, expected_genesis, expected_cutoff_hash):
    required = {
        "schema",
        "policy",
        "genesis",
        "cutoff_hash",
        "cutoff_block",
        "cutoff_day",
        "source_sha256",
        "miners",
        "consumed_receipt_ids",
    }
    if not isinstance(plan, dict) or set(plan) != required:
        raise MigrationError("unexpected migration fields")
    if plan["schema"] != "noah-points-migration/1" or plan["policy"] != POLICY:
        raise MigrationError("unsupported policy/schema")
    if not hash32(plan["genesis"]) or not hash32(plan["cutoff_hash"]):
        raise MigrationError("invalid chain pins")
    if plan["genesis"] != expected_genesis or plan["cutoff_hash"] != expected_cutoff_hash:
        raise MigrationError("wrong chain/cutoff")
    for k in ["cutoff_block", "cutoff_day"]:
        if type(plan[k]) is not int or plan[k] < 0:
            raise MigrationError("invalid cutoff")
    if not isinstance(plan["source_sha256"], str) or not re.fullmatch(
        "[0-9a-f]{64}", plan["source_sha256"]
    ):
        raise MigrationError("missing source digest")
    if not isinstance(plan["miners"], list) or not plan["miners"]:
        raise MigrationError("empty roster")
    ids = plan["consumed_receipt_ids"]
    if (
        not isinstance(ids, list)
        or any(not isinstance(x, str) or not x for x in ids)
        or len(ids) != len(set(ids))
    ):
        raise MigrationError("invalid migrated receipt identities")
    uids = set()
    keys = set()
    state_fields = {f.name for f in fields(State)}
    for r in plan["miners"]:
        if set(r) != {
            "uid",
            "coldkey",
            "hotkey",
            "state",
            "legacy_coverage_rao",
            "legacy_debt_rao",
        }:
            raise MigrationError("unexpected miner fields")
        if type(r["uid"]) is not int or r["uid"] < 0 or r["uid"] in uids:
            raise MigrationError("duplicate or invalid uid")
        uids.add(r["uid"])
        for k in ["coldkey", "hotkey"]:
            if not isinstance(r[k], str) or not re.fullmatch("[1-9A-HJ-NP-Za-km-z]{47,49}", r[k]):
                raise MigrationError("invalid identity encoding")
        if r["hotkey"] in keys:
            raise MigrationError("duplicate hotkey")
        keys.add(r["hotkey"])
        s = r["state"]
        if set(s) != state_fields or any(type(v) is not int for v in s.values()):
            raise MigrationError("invalid state fields")
        if any(v < 0 for k, v in s.items() if k != "balance"):
            raise MigrationError("negative state counter")
        if (
            s["maturity"] != maturity(s["qualified"])
            or s["points"] > 100 * Q
            or s["last_compliant"] > 100 * Q
        ):
            raise MigrationError("invalid points/maturity")
        if s["baseline"] != 40 * Q or s["contribution"] > 20 * Q or s["consistency"] > 40 * Q:
            raise MigrationError("invalid components")
        for k in ["legacy_coverage_rao", "legacy_debt_rao"]:
            if type(r[k]) is not int or r[k] < 0:
                raise MigrationError("invalid legacy balance")
        if r["legacy_coverage_rao"] and r["legacy_debt_rao"]:
            raise MigrationError("inconsistent legacy balance")
        if max(s["balance"], 0) != r["legacy_coverage_rao"]:
            raise MigrationError("would change prepaid coverage; explicit adjustment required")
        if max(-s["balance"], 0) != r["legacy_debt_rao"]:
            raise MigrationError("would change existing debt; explicit review required")
    return digest(plan)


def install(path, plan, *, expected_genesis, expected_cutoff_hash):
    fingerprint = validate(
        plan, expected_genesis=expected_genesis, expected_cutoff_hash=expected_cutoff_hash
    )
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and tables != {"points_migration", "points_states"}:
            raise MigrationError("refusing a non-candidate database")
        db.execute(
            "CREATE TABLE IF NOT EXISTS points_migration (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL, body TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS points_states (hotkey TEXT PRIMARY KEY, coldkey TEXT NOT NULL, uid INTEGER UNIQUE NOT NULL, body TEXT NOT NULL)"
        )
        existing = db.execute("SELECT digest,body FROM points_migration WHERE id=1").fetchone()
        if existing:
            if existing != (fingerprint, encoded(plan).decode()):
                raise MigrationError("different migration already installed")
            records = {
                r[0]: r[1:] for r in db.execute("SELECT hotkey,coldkey,uid,body FROM points_states")
            }
            expected = {
                r["hotkey"]: (r["coldkey"], r["uid"], encoded(r["state"]).decode())
                for r in plan["miners"]
            }
            if records != expected:
                raise MigrationError("stored migration state changed; refusing to reset")
            return "already-installed"
        if db.execute("SELECT count(*) FROM points_states").fetchone()[0]:
            raise MigrationError("orphan candidate states")
        db.execute(
            "INSERT INTO points_migration VALUES (1,?,?)", (fingerprint, encoded(plan).decode())
        )
        for r in plan["miners"]:
            db.execute(
                "INSERT INTO points_states VALUES (?,?,?,?)",
                (r["hotkey"], r["coldkey"], r["uid"], encoded(r["state"]).decode()),
            )
    return "installed"
