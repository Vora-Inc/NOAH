"""Resumable finalized collection, independent of completed-day readiness.

These additive tables never mark a day scored, sampled or complete. Daily
readers never use them as completion evidence. The updated store validates
their optional schema. Caller holds the validator writer lock.
"""

import hashlib
import json

from .sources import Refusal


class CollectionRefused(Refusal):
    """Progress cannot safely be reused; preserve it for investigation."""


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS epoch_collection (day INTEGER PRIMARY KEY, body TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS epoch_holding_cache "
    "(day INTEGER, block INTEGER, coldkey TEXT, proof TEXT, "
    "PRIMARY KEY(day,block,coldkey))",
)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class CollectionProgress:
    def __init__(self, store, chain, day, first, target, settings):
        self.store, self.chain, self.day = store, chain, day
        self.first, self.target = first, target
        self.settings = fingerprint(settings)
        self.conn = store._conn
        with self.conn:
            for statement in SCHEMA:
                self.conn.execute(statement)
        if target > chain.head_block():
            raise CollectionRefused("collection target is not finalized")
        self.first_hash = chain.block_hash(first)
        row = self.conn.execute("SELECT body FROM epoch_collection WHERE day=?", (day,)).fetchone()
        self.last = first - 1
        self.receipts = set()
        if row:
            doc = json.loads(row[0])
            if (
                doc["settings"] != self.settings
                or doc["first"] != first
                or doc["first_hash"] != self.first_hash
                or not first <= doc["last"] <= target
                or chain.block_hash(doc["last"]) != doc["last_hash"]
            ):
                raise CollectionRefused("collection identity, range or finalized hash changed")
            self.last = doc["last"]
            self.receipts = {tuple(x) for x in doc["receipts"]}

    def advance(self, block, block_hash, receipts):
        if block != self.last + 1:
            raise CollectionRefused("collection progress must be contiguous")
        doc = dict(
            first=self.first,
            first_hash=self.first_hash,
            last=block,
            last_hash=block_hash,
            settings=self.settings,
            receipts=sorted(receipts),
        )
        # Receipt writes complete first. Crash before this cursor causes replay,
        # using the existing event-identity/idempotency checks, never a skipped block.
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO epoch_collection VALUES (?,?)",
                (self.day, json.dumps(doc, sort_keys=True)),
            )
        self.last = block

    def sample_proof(self, coldkey, block_hash):
        # A newly discovered hotkey requires historical samples to be rechecked
        # for that wallet. Keep the full-day stride anchored at the original start.
        return fingerprint(
            [self.settings, block_hash, sorted(self.store.known_hotkeys_of(coldkey))]
        )

    def sampled(self, coldkey, block, block_hash):
        row = self.conn.execute(
            "SELECT proof FROM epoch_holding_cache WHERE day=? AND block=? AND coldkey=?",
            (self.day, block, coldkey),
        ).fetchone()
        return row is not None and row[0] == self.sample_proof(coldkey, block_hash)

    def mark_sample(self, coldkey, block, block_hash):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO epoch_holding_cache VALUES (?,?,?,?)",
                (self.day, block, coldkey, self.sample_proof(coldkey, block_hash)),
            )
