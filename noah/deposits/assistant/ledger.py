# ruff: noqa: E501
"""The durable receipt ledger: intent, submitted, finalized, credited.

One SQLite file on the miner's machine. The database itself, not the program,
refuses a second live payment for the same coverage, so two processes, a restart
or a retry cannot repeat a completed (or still-possible) payment.

States, and what each one does and does not prove:

    awaiting_approval  shown to the miner, nothing typed yet. Nothing signed.
    approved           the miner typed the confirmation. Nothing signed yet.
    submitting         written BEFORE the transaction is handed to the chain. A
                       crash here means the payment may or may not have been sent.
    submitted          the chain accepted it into a block. Not proof of credit.
    finalized          the block is final and the deposit was read back from the
                       chain as a qualifying, correctly attributed deposit.
    credited           independent evidence shows the qualifying credit counted.
    unqualified        landed on chain but the reader would NOT credit it (for example
                       wrong validator). Money moved; it keeps holding its coverage so a
                       retry cannot send the same amount again unnoticed.
    uncertain          sent or maybe sent, and the outcome could not be read. Blocks
                       every new payment until reconciled against the chain.
    declined / failed / abandoned
                       terminal and non-paying; they free the coverage for a retry.
                       ``abandoned`` is only reached once enough blocks have passed
                       that the transaction cannot still land.
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime, timezone

from ...runtime_events import emit as emit_event
from ...chain.sources import Refusal

AWAITING, APPROVED, SUBMITTING, SUBMITTED = (
    "awaiting_approval",
    "approved",
    "submitting",
    "submitted",
)
FINALIZED, CREDITED, UNCERTAIN = "finalized", "credited", "uncertain"
UNQUALIFIED = "unqualified"
DECLINED, FAILED, ABANDONED = "declined", "failed", "abandoned"

OPEN_STATES = (AWAITING, APPROVED, SUBMITTING, SUBMITTED, UNCERTAIN)
NON_PAYING = (DECLINED, FAILED, ABANDONED)

_ALLOWED = {
    AWAITING: (APPROVED, DECLINED),
    APPROVED: (SUBMITTING, FAILED, DECLINED),
    SUBMITTING: (SUBMITTED, FAILED, UNCERTAIN),
    SUBMITTED: (FINALIZED, UNCERTAIN, FAILED, UNQUALIFIED),
    UNCERTAIN: (SUBMITTED, FINALIZED, ABANDONED),
    FINALIZED: (CREDITED,),
}

_COLUMNS = (
    "intent_id",
    "fingerprint",
    "miner_hotkey",
    "mode",
    "amount_rao",
    "shape",
    "state",
    "created_at",
    "updated_at",
    "extrinsic_hash",
    "block_hash",
    "block_number",
    "start_block",
    "credited_before_rao",
    "note",
    "details",
)


class DuplicateDeposit(Refusal):
    def __init__(self, message, existing):
        super().__init__(message)
        self.existing = existing


class OpenIntentExists(Refusal):
    def __init__(self, message, existing):
        super().__init__(message)
        self.existing = existing


class IllegalTransition(Refusal):
    pass


def _stamp(now):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS intents ("
            "intent_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, miner_hotkey TEXT NOT NULL, "
            "mode TEXT NOT NULL, amount_rao INTEGER NOT NULL, shape TEXT NOT NULL, state TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, extrinsic_hash TEXT, block_hash TEXT, "
            "block_number INTEGER, start_block INTEGER, credited_before_rao INTEGER, note TEXT, details TEXT)"
        )
        # The database enforces both rules, so no race between processes can break them.
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_live_per_coverage ON intents(fingerprint) "
            "WHERE state NOT IN ('declined','failed','abandoned')"
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_miner ON intents(miner_hotkey) "
            "WHERE state IN ('awaiting_approval','approved','submitting','submitted','uncertain')"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS calculations (miner_hotkey TEXT PRIMARY KEY, "
            "calculated_at TEXT NOT NULL, body TEXT NOT NULL)"
        )

    @contextlib.contextmanager
    def execution_lock(self):
        """Serialize direct and mixed signing; never queue a later transaction.

        Lock order is canonical ledger, then mixed journal, then proxy signer.
        All workers spending one owner must mount the same canonical state.
        """
        lock_path = Path(self.path + '.execution.lock')
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(lock_path, flags, 0o600), 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal('Another deposit command is running; no transaction was queued.')
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _mixed_table(self):
        self._db.execute('CREATE TABLE IF NOT EXISTS mixed_reservations '
                         '(id TEXT PRIMARY KEY, journal TEXT NOT NULL, status TEXT NOT NULL)')

    def check_direct_execution(self):
        self._mixed_table()
        if self._db.execute("SELECT id FROM mixed_reservations WHERE status NOT IN ('complete','closed_failed') LIMIT 1").fetchone():
            raise Refusal('An incomplete mixed deposit holds this owner; inspect its original configuration, label and journal.')

    def check_mixed_execution(self, ident, journal, row_exists):
        self._mixed_table()
        if self._db.execute('SELECT intent_id FROM intents WHERE state IN (?,?,?,?,?) LIMIT 1', OPEN_STATES).fetchone():
            raise Refusal('An earlier direct/proxy deposit is still open; run its status command before starting mixed deposits.')
        rows = self._db.execute('SELECT * FROM mixed_reservations').fetchall()
        for row in rows:
            if row['id'] == ident:
                if row['journal'] != str(Path(journal).resolve()) or not row_exists:
                    raise Refusal('Mixed reservation requires its original durable journal; missing state must be investigated.')
            elif row['status'] not in ('complete', 'closed_failed'):
                raise Refusal('Another incomplete mixed deposit holds this owner; inspect its original label.')
        if row_exists and not any(row['id'] == ident for row in rows):
            raise Refusal('Mixed journal has no canonical reservation; investigate before continuing.')

    def reserve_mixed(self, ident, journal):
        self._mixed_table()
        self._db.execute('INSERT OR IGNORE INTO mixed_reservations VALUES (?, ?, ?)',
                         (ident, str(Path(journal).resolve()), 'open'))

    def complete_mixed(self, ident):
        self._db.execute("UPDATE mixed_reservations SET status='complete' WHERE id=?", (ident,))

    def close_failed_mixed(self, ident):
        self._db.execute("UPDATE mixed_reservations SET status='closed_failed' WHERE id=?", (ident,))

    def close(self):
        self._db.close()

    # ---- reads
    def get(self, intent_id):
        row = self._db.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def for_miner(self, miner_hotkey):
        rows = self._db.execute(
            "SELECT * FROM intents WHERE miner_hotkey=? ORDER BY created_at, rowid", (miner_hotkey,)
        ).fetchall()
        return [dict(r) for r in rows]

    def open_intent(self, miner_hotkey):
        row = self._db.execute(
            "SELECT * FROM intents WHERE miner_hotkey=? AND state IN (?,?,?,?,?)",
            (miner_hotkey, *OPEN_STATES),
        ).fetchone()
        return dict(row) if row else None

    def awaiting_credit(self, miner_hotkey):
        """Deposits that are final on chain but not yet shown as credited."""
        rows = self._db.execute(
            "SELECT * FROM intents WHERE miner_hotkey=? AND state='finalized' ORDER BY created_at, rowid",
            (miner_hotkey,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_credited_before(self, intent_id, value_rao, *, now=None):
        """Record a MINER-SUPPLIED credited total (human step, see ``session.resolve_credit``).

        Only for a finalized intent that has no figure yet. The audit trail is kept in
        ``details`` (source, value, time) and ``note``; the tool never infers this value.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT state, credited_before_rao, details FROM intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if row is None or row["state"] != FINALIZED or row["credited_before_rao"] is not None:
                raise IllegalTransition("This deposit can no longer take a supplied credit figure.")
            details = json.loads(row["details"] or "{}")
            details["credited_before_source"] = "miner-supplied"
            details["credited_before_supplied_at"] = _stamp(now)
            details["credited_before_supplied_rao"] = value_rao
            self._db.execute(
                "UPDATE intents SET credited_before_rao=?, details=?, updated_at=?, note=? "
                "WHERE intent_id=?",
                (
                    value_rao,
                    json.dumps(details, sort_keys=True),
                    _stamp(now),
                    f"credited total before this deposit was supplied by the miner: {value_rao} rao",
                    intent_id,
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def last_calculation(self, miner_hotkey):
        row = self._db.execute(
            "SELECT calculated_at, body FROM calculations WHERE miner_hotkey=?", (miner_hotkey,)
        ).fetchone()
        return None if row is None else {"calculated_at": row[0], **json.loads(row[1])}

    # ---- writes
    def record_calculation(self, miner_hotkey, body, now=None):
        self._db.execute(
            "INSERT INTO calculations VALUES (?,?,?) ON CONFLICT(miner_hotkey) DO UPDATE SET "
            "calculated_at=excluded.calculated_at, body=excluded.body",
            (miner_hotkey, _stamp(now), json.dumps(body, sort_keys=True)),
        )

    def create_intent(
        self,
        *,
        fingerprint,
        miner_hotkey,
        mode,
        amount_rao,
        shape,
        details=None,
        credited_before_rao=None,
        now=None,
    ):
        """A new awaiting_approval intent, or a refusal naming what blocks it."""
        intent_id = uuid.uuid4().hex
        stamp = _stamp(now)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            clash = self._db.execute(
                "SELECT * FROM intents WHERE fingerprint=? AND state NOT IN ('declined','failed','abandoned')",
                (fingerprint,),
            ).fetchone()
            if clash:
                raise DuplicateDeposit(
                    f"This coverage already has a deposit in state '{clash['state']}' "
                    f"(intent {clash['intent_id'][:8]}). A second one is not started.",
                    dict(clash),
                )
            # A forgotten, never-approved prompt must not block the miner forever.
            self._db.execute(
                "UPDATE intents SET state='declined', updated_at=?, note='superseded by a newer run' "
                "WHERE miner_hotkey=? AND state='awaiting_approval'",
                (stamp, miner_hotkey),
            )
            blocker = self._db.execute(
                "SELECT * FROM intents WHERE miner_hotkey=? AND state IN (?,?,?,?,?)",
                (miner_hotkey, *OPEN_STATES),
            ).fetchone()
            if blocker:
                raise OpenIntentExists(
                    f"An earlier deposit (intent {blocker['intent_id'][:8]}) is still '{blocker['state']}'. "
                    f"Run `status` to reconcile it with the chain before starting another.",
                    dict(blocker),
                )
            self._db.execute(
                "INSERT INTO intents (intent_id, fingerprint, miner_hotkey, mode, amount_rao, shape, state, "
                "created_at, updated_at, credited_before_rao, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent_id,
                    fingerprint,
                    miner_hotkey,
                    mode,
                    amount_rao,
                    shape,
                    AWAITING,
                    stamp,
                    stamp,
                    credited_before_rao,
                    json.dumps(details or {}, sort_keys=True),
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return intent_id

    def transition(self, intent_id, to_state, *, now=None, **fields):
        """Move one intent to ``to_state`` atomically, only along an allowed edge."""
        bad = set(fields) - set(_COLUMNS)
        if bad:
            raise ValueError(f"unknown ledger fields {sorted(bad)}")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT state FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise IllegalTransition(f"No deposit intent {intent_id!r}.")
            if to_state not in _ALLOWED.get(row["state"], ()):
                raise IllegalTransition(
                    f"A deposit in state '{row['state']}' cannot move to '{to_state}'."
                )
            assignments = ", ".join(f"{k}=?" for k in fields)
            self._db.execute(
                f"UPDATE intents SET state=?, updated_at=?{', ' + assignments if assignments else ''} "
                f"WHERE intent_id=?",
                (to_state, _stamp(now), *fields.values(), intent_id),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        emit_event("miner", "contribution.observed", "miner.ledger",
                   outcome=to_state, intent_id=intent_id, height=fields.get("block_number"),
                   tx_hash=fields.get("extrinsic_hash"))
