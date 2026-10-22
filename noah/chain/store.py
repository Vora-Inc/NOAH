"""The local store: chain facts as they were observed, with the heights they came from.

Two ideas run through everything here.

The day anchor is the store's identity, not a setting handed in at read time.
Every day in here is counted from it, so using a store under a different anchor
would move pay and deposits into different days. Deposits are permanent, so that
error would be permanent too. The anchor is checked on every path that can build
a Store, and the day is derived here rather than trusted from a caller: a guard
on the label protects nothing if the bucketing happens somewhere else.

And a day nobody read is not a day with nothing in it. This module refuses
silent zeros from the chain; it must not manufacture its own. A read of a day
that was never scanned is a refusal, never an empty result.

D-139 (2026-08-31): the scoring period is one UTC CALENDAR DAY, 00:00 UTC to
the next 00:00 UTC -- not "24 hours from whenever this chain's genesis block
happened to land". Bucketing purely by ``(ts - anchor) // DAY_SECONDS`` is
midnight-aligned ONLY when ``anchor`` itself already sits exactly at a UTC
midnight; a raw genesis timestamp (e.g. 18:32 UTC) is not one. The anchor
this store persists is therefore always FLOORED to the UTC midnight at or
before whatever raw timestamp it is built or opened with (``_floor_to_day``,
applied in ``__init__`` so no call site can bypass it by constructing a
Store any other way -- the same discipline the day-derivation guard above
already states). Given that, ``(ts - anchor) // DAY_SECONDS`` is exact:
every midnight is anchor plus a whole multiple of ``DAY_SECONDS``, so floor
division distributes over subtracting a multiple of the divisor precisely.
The one consequence worth naming: the first day this store ever anchors to
is SHORT in real observed activity (only however many hours separate the
raw genesis moment from the following midnight) -- expected, not a bug;
every day after it is a genuine full UTC calendar day. The anchor is a
permanent identity (see below); this flooring is baked in at creation, not
recomputed later.
"""

import os
import sqlite3
from pathlib import Path

from ..pathtext import plain_path_text
from .holdings import Share, positions_for_coldkey
from .sources import Refusal

DAY_SECONDS = 86_400
SCHEMA_VERSION = 18  # issue #16: foreign deposits split from NOAH ones at the ledger write.
# SCHEMA_VERSION 18 (issue #16, foreign-token credit): `foreign_deposit` gains the same
# attribution columns `noah_deposit` has (hotkey, owner_at_deposit, origin_hotkey,
# unattributed_reason) and a new table, `day_needs_rescan`, lists the days a store
# scanned with the OLDER reader. That reader put EVERY deposit into `noah_deposit`
# whatever token it was in, and the row cannot say which token arrived, so those rows
# cannot be trusted as NOAH. `create()` over an older store adds the columns and marks
# every already-scanned day; a marked day reads as NOT scanned (so scoring refuses by
# name, and the runner re-reads it from the chain's own events). Nothing is deleted
# until the re-read finds the event again and files it where it belongs.
#
# SCHEMA_VERSION 17 (issue #237): one additive table, registration_day (below).
# `create()` adds it to an existing store and stamps this version; `open()`
# refuses a store still at 16 until a `create()` has run over it. Nothing else
# changed, so no data has to be rebuilt -- which matters, because a mainnet
# node keeps only about 90 days of state and a store's own history cannot be
# read back from the chain.
#
# SCHEMA_VERSION 16 adds recorded attribution failures: a deposit whose batch_all+remark
# attribution attempt fails (ambiguous batch, malformed remark, or an
# unentitled signer) is NOT dropped -- the alpha genuinely moved to the Ark,
# and dropping the row would leave its holdings grown with nothing
# accounting for the increase. `noah_deposit` gains one more nullable
# column, `unattributed_reason` (text): the specific refusal's own message,
# recorded on the row rather than only logged, so readers can distinguish
# an unattributed receipt from qualifying mining credit. Same precedent as `owner_at_deposit`/`origin_hotkey` just
# below: a store built under an older schema lacks this column, and
# `open()`'s own `PRAGMA user_version` check refuses it (SchemaTooOld)
# rather than reading it and failing deep inside a query with an ordinary
# "no such column" exception a retry loop could swallow. Rebuild the store
# rather than reading it under this code.
#
# SCHEMA_VERSION 15 (issue #112 comment 5751446658, 2026-09-20): a deposit's
# staking hotkey never changes on transfer_stake -- it stays whichever
# validator the miner originally delegated to -- so it cannot by itself
# identify which miner a deposit credits once two miners share one wallet
# and one validator hotkey. `noah_deposit` gains two more nullable columns:
# `owner_at_deposit` (the coldkey that owned the credited miner's hotkey AT
# THE DEPOSIT'S OWN BLOCK, read historically -- see
# `noah.deposits.owner_entitlement.confirm_owner_entitled_at_block`) and
# `origin_hotkey` (the miner hotkey this deposit is actually attributed to,
# once the remark naming it can be decoded). Both are nullable and default
# to None -- the remark-decoding piece that would populate `origin_hotkey`
# for real is a separate, still-pending piece of work (#111), and this
# phase only gives it somewhere real to write. Same precedent as the
# `hotkey` column D-138 reopened added just below: a store built under an
# older schema lacks these columns, and `open()`'s own `PRAGMA user_version`
# check refuses it (SchemaTooOld) rather than reading it and failing deep
# inside a query with an ordinary "no such column" exception a retry loop
# could swallow. Rebuild the store rather than reading it under this code.
_MAX_SQLITE_INT = 2**63 - 1


def _floor_to_day(ts):
    """The UTC midnight at or before ``ts``, in epoch seconds.

    Exact for every input because Unix epoch 0 is itself a UTC midnight:
    ``(ts // DAY_SECONDS) * DAY_SECONDS`` always lands on a whole multiple of
    ``DAY_SECONDS``, i.e. a real UTC midnight, with no anchor or timezone
    table involved.
    """
    return (ts // DAY_SECONDS) * DAY_SECONDS


class AnchorMismatch(Refusal):
    """A store was used under a different day anchor than it was built with."""


class NotANoahStore(Refusal):
    """The file is not a Noah store, or its identity was never finished."""


class StoreUnavailable(Refusal):
    """The store exists but could not be read right now. Not the same as missing."""


class DayNotScanned(Refusal):
    """A day was read that nobody has scanned. Absence of data, not absence of deposits."""


class MalformedScanRange(Refusal):
    """A day's own recorded scan range is not a real range of any block.

    Added under ticket B02 (issue #10, 2026-09-10), a real codex adversarial
    finding: ``mark_day_scanned`` used to accept whatever ``from_block``/
    ``to_block`` a caller handed it -- zero, negative, or reversed
    (``from_block`` after ``to_block``) -- and record it exactly as given.
    ANY row present for a day, malformed or not, is what
    ``noah.deposits.d143.confirm_eval_day_closed`` reads as real evidence
    that scanning crossed that day's own boundary before trusting a
    permanent, one-time deposit record. A well-formed range is a cheap,
    real check this store can make on data it already owns, at the one
    place anything is ever written into ``scanned_day`` -- see
    ``mark_day_scanned``'s own docstring.
    """


class TimestampBeforeAnchor(Refusal):
    """A block timestamp older than the store's own day anchor."""


class ObservationChanged(Refusal):
    """The chain was asked twice about one settled block and answered differently."""


#: Every reason the reader records for a deposit that names no miner hotkey
#: starts with this text (rule B, #338). The text after it is advice and may be
#: reworded, so a rescan compares only this stable prefix.
UNNAMED_REASON_PREFIX = "No miner hotkey named:"

#: Marks the end of the fixed part of a wrong-validator reason; anything the
#: reader appends after it (for example that the deposit also names no miner)
#: is advice, not part of the reason's class.
_WRONG_VALIDATOR_MARK = " -- does not qualify for mining credit"

#: What :meth:`Store.unattributed_deposits` shows for a row stored before rule
#: B: no origin hotkey and no reason recorded.
LEGACY_UNNAMED_REASON = "legacy row: no miner hotkey recorded (stored before rule B)"


# A day the older reader scanned reads as NOT scanned until it is read again (issue #16).
_NOT_STALE = "AND day NOT IN (SELECT day FROM day_needs_rescan)"

_FOREIGN_ATTRIBUTION_COLUMNS = ("hotkey", "owner_at_deposit", "origin_hotkey", "unattributed_reason")


def _schema_shapes():
    """Expected table layouts, derived from the same schema used for new stores."""
    model = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA:
            model.execute(statement)
        names = [r[0] for r in model.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: list(model.execute(f'PRAGMA table_info("{name}")')) for name in names}
    finally:
        model.close()


def _validate_store_schema(conn, version):
    if version not in (17, SCHEMA_VERSION):
        raise SchemaTooOld(f"store schema {version} is unsupported; only 17 -> 18 is migratable")
    expected = _schema_shapes()
    if version == 17:
        del expected["day_needs_rescan"]
        expected["foreign_deposit"] = expected["foreign_deposit"][:8]
    actual_names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    # Optional incremental-collection cache. Require both exact table shapes;
    # unknown tables and partial/corrupt extensions still refuse to open.
    if actual_names & {"epoch_collection", "epoch_holding_cache"}:
        from .collection import SCHEMA as COLLECTION_SCHEMA

        model = sqlite3.connect(":memory:")
        try:
            for statement in COLLECTION_SCHEMA:
                model.execute(statement)
            for name in ("epoch_collection", "epoch_holding_cache"):
                expected[name] = list(model.execute(f'PRAGMA table_info("{name}")'))
        finally:
            model.close()
    if actual_names != set(expected):
        raise SchemaTooOld("store has an incomplete or unexpected schema; migration refused")
    for name, shape in expected.items():
        if list(conn.execute(f'PRAGMA table_info("{name}")')) != shape:
            raise SchemaTooOld(f"store table {name} has an incomplete or unexpected shape; migration refused")


def _migrate_foreign_split(conn, old_version):
    """Only 17 -> 18, under an explicit transaction begun before any DDL."""
    if old_version != 17 or not conn.in_transaction:
        raise SchemaTooOld("foreign split requires schema 17 and an active migration transaction")
    _validate_store_schema(conn, 17)
    for column in _FOREIGN_ATTRIBUTION_COLUMNS:
        conn.execute(f"ALTER TABLE foreign_deposit ADD COLUMN {column} TEXT CHECK ({column} IS NULL OR length({column}) > 0)")
    conn.execute("ALTER TABLE foreign_deposit ADD COLUMN block_timestamp INTEGER CHECK (block_timestamp IS NULL OR (block_timestamp > 0 AND typeof(block_timestamp) = 'integer'))")
    conn.execute("CREATE TABLE day_needs_rescan (day INTEGER NOT NULL PRIMARY KEY CHECK (day >= 0))")
    # Empty scans matter too: absence under the old reader is not token proof.
    conn.execute("INSERT INTO day_needs_rescan (day) SELECT day FROM scanned_day")
    conn.execute("INSERT OR IGNORE INTO day_needs_rescan (day) SELECT day FROM foreign_deposit")
    conn.execute("INSERT OR IGNORE INTO day_needs_rescan (day) SELECT day FROM noah_deposit")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _validate_store_schema(conn, SCHEMA_VERSION)


def _reason_class(reason):
    """The part of an unattributed reason a rescan must find unchanged."""
    if reason is None:
        return None
    if reason.startswith(UNNAMED_REASON_PREFIX):
        return UNNAMED_REASON_PREFIX
    if reason.startswith("staked to ") and _WRONG_VALIDATOR_MARK in reason:
        return reason[: reason.index(_WRONG_VALIDATOR_MARK) + len(_WRONG_VALIDATOR_MARK)]
    return reason


class NotAnAmount(Refusal):
    """A value on the money path that is not a plain, in-range, non-negative integer."""


class NotAnIdentifier(Refusal):
    """A wallet, hotkey or block hash that is not the text every other read asks for."""


class StoreRejected(Refusal):
    """The database refused a row. Reported as a refusal, never as an ordinary error."""


class EmissionStateChanged(Refusal):
    """The subnet's one and only start was recorded twice, differently."""


class RegistrationDayChanged(Refusal):
    """One registration block was recorded as falling in two different days."""


class DeclarationChanged(Refusal):
    """A published declaration of Noah's own failure was rewritten after the fact."""


class BlockHashChanged(Refusal):
    """One block height was read under two different hashes."""


class DayNotSampled(Refusal):
    """Holdings were read for a day nobody sampled. Absence of data, not of holdings."""


class ColdkeyNotSampled(Refusal):
    """A wallet's holding was asked for in a day sampled for other wallets, but
    never for this one wallet. Absence of a reading for one wallet is not a
    reading of zero for it, so it may not be answered as though every hotkey
    it might hold read as nothing.
    """


class LockedPayNotRead(Refusal):
    """A wallet's locked-pay figures asked for on a day that never read them."""


class SplitNotRead(Refusal):
    """The locked and available split of a holding was never read.

    A distinct state from "read and found to be zero". A zero in the locked
    column is a claim that nothing is locked, which on a wallet that has locked
    something is a false statement in the store forever, and absence rendered as
    a value is the shape this project has corrected more than any other.
    """


class _NotRead:
    """The sentinel. A class rather than None so it cannot arrive by accident
    from a caller that forgot an argument or a dictionary that lacked a key."""

    __slots__ = ()

    def __repr__(self):
        return "NOT_READ"


NOT_READ = _NotRead()


class HoldingNotCoherent(Refusal):
    """A holding's three portions do not describe one position."""


class SchemaTooOld(Refusal):
    """The store was built by an older version of this code and lacks its tables."""


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS identity (day_anchor_ts INTEGER NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS foreign_deposit (
        block           INTEGER NOT NULL CHECK (block > 0),
        event_ordinal   INTEGER NOT NULL CHECK (event_ordinal >= 0),
        extrinsic_index INTEGER NOT NULL CHECK (extrinsic_index >= 0),
        coldkey         TEXT    NOT NULL,
        day            INTEGER NOT NULL CHECK (day >= 0),
        amount_rao      INTEGER NOT NULL CHECK (amount_rao >= 0
                                                AND typeof(amount_rao) = 'integer'),
        token_netuid    INTEGER NOT NULL CHECK (token_netuid >= 0),
        block_hash      TEXT    NOT NULL,
        -- Same attribution as a NOAH deposit (issue #16): the validator hotkey the
        -- position sits on, the miner hotkey the deposit names, who owned that miner
        -- at the deposit's block, and why it credits nobody if it does not.
        hotkey          TEXT    CHECK (hotkey IS NULL OR length(hotkey) > 0),
        owner_at_deposit TEXT   CHECK (owner_at_deposit IS NULL OR length(owner_at_deposit) > 0),
        origin_hotkey   TEXT    CHECK (origin_hotkey IS NULL OR length(origin_hotkey) > 0),
        unattributed_reason TEXT CHECK (unattributed_reason IS NULL
                                        OR length(unattributed_reason) > 0),
        block_timestamp INTEGER CHECK (block_timestamp IS NULL OR (block_timestamp > 0 AND typeof(block_timestamp) = 'integer')),
        PRIMARY KEY (block, event_ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS day_needs_rescan (
        day INTEGER NOT NULL PRIMARY KEY CHECK (day >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attributed_pay (
        uid        INTEGER NOT NULL CHECK (uid >= 0),
        block      INTEGER NOT NULL CHECK (block > 0),
        coldkey    TEXT    NOT NULL,
        hotkey     TEXT    NOT NULL,
        day       INTEGER NOT NULL CHECK (day >= 0),
        amount_rao INTEGER NOT NULL CHECK (amount_rao >= 0
                                           AND typeof(amount_rao) = 'integer'),
        block_hash TEXT    NOT NULL,
        PRIMARY KEY (uid, block)
    )
    """,
    "CREATE INDEX IF NOT EXISTS attributed_pay_by_wallet_day ON attributed_pay (coldkey, day)",
    """
    CREATE TABLE IF NOT EXISTS locked_pay (
        coldkey            TEXT    NOT NULL,
        block              INTEGER NOT NULL CHECK (block > 0),
        day               INTEGER NOT NULL CHECK (day >= 0),
        locked_arrival_rao INTEGER NOT NULL CHECK (locked_arrival_rao >= 0
                                        AND typeof(locked_arrival_rao) = 'integer'),
        unlocked_rao       INTEGER NOT NULL CHECK (unlocked_rao >= 0
                                        AND typeof(unlocked_rao) = 'integer'),
        block_hash         TEXT    NOT NULL,
        PRIMARY KEY (coldkey, block),
        -- One of the two is always zero. The runtime's two branches are
        -- mutually exclusive (collateral.rs:508-527): below the floor pay is
        -- captured and nothing is released, above it the drain releases and
        -- nothing is captured. A row with both is a reading that netted a
        -- capture on one hotkey against a release on another, which is the
        -- shape that loses both figures. Placed here rather than beside the
        -- columns because a table constraint has to follow every column
        -- definition; above them it reads fine and will not parse.
        CHECK (locked_arrival_rao = 0 OR unlocked_rao = 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS locked_pay_by_wallet_day ON locked_pay (coldkey, day)",
    """
    CREATE TABLE IF NOT EXISTS locked_pay_unreadable (
        coldkey TEXT    NOT NULL,
        day    INTEGER NOT NULL CHECK (day >= 0),
        block   INTEGER NOT NULL CHECK (block > 0),
        reason  TEXT    NOT NULL CHECK (length(reason) > 0),
        PRIMARY KEY (coldkey, block)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS noah_deposit (
        block           INTEGER NOT NULL CHECK (block > 0),
        event_ordinal   INTEGER NOT NULL CHECK (event_ordinal >= 0),
        extrinsic_index INTEGER NOT NULL CHECK (extrinsic_index >= 0),
        coldkey         TEXT    NOT NULL,
        day            INTEGER NOT NULL CHECK (day >= 0),
        amount_rao      INTEGER NOT NULL CHECK (amount_rao >= 0
                                                AND typeof(amount_rao) = 'integer'),
        block_hash      TEXT    NOT NULL,
        -- Which of the two transfer events brought it. Both are contributions,
        -- because the destination coldkey decides ownership and the hotkey only
        -- decides which validator the position is delegated to. But one keeps
        -- the hotkey and the other changes it, and the version-one flow offers
        -- no validator choice, so a contribution carried by the second did not
        -- come through our own flow. Recorded because an audit cannot tell them
        -- apart later without re-reading the chain.
        carried_by      TEXT    NOT NULL CHECK (length(carried_by) > 0),
        -- D-138 reopened (issue #112): which hotkey the deposit's alpha
        -- arrived on. NULL, not required, in this phase -- a coldkey could
        -- only ever hold one seat before D-138 reopened, so nothing wrote
        -- this before now, and old rows (and any deposit recorded without
        -- one) have nothing real to backfill it with. A future phase that
        -- scores per-seat needs this populated; this phase only stores it
        -- when the caller has it (see `record_noah_deposit`).
        hotkey          TEXT    CHECK (hotkey IS NULL OR length(hotkey) > 0),
        -- issue #112 comment 5751446658 (2026-09-20): the coldkey that owned
        -- the credited miner's hotkey AT THE DEPOSIT'S OWN BLOCK, read
        -- historically and passed in already-resolved -- this table only
        -- stores whatever the caller hands it (see `record_noah_deposit`).
        owner_at_deposit TEXT   CHECK (owner_at_deposit IS NULL OR length(owner_at_deposit) > 0),
        -- The miner hotkey this deposit is actually attributed to, once the
        -- remark naming it can be decoded. NULL until the extrinsic-body
        -- decoder (#111, held pending chain confirmation) exists to
        -- populate it for real.
        origin_hotkey   TEXT    CHECK (origin_hotkey IS NULL OR length(origin_hotkey) > 0),
        -- Attribution failure reason: set when a batch_all+remark
        -- attribution attempt was made and failed (ambiguous batch,
        -- malformed remark, or an unentitled signer) -- the specific
        -- refusal's own message, so the row is visibly "received but
        -- unattributed" rather than silently dropped. NULL for every
        -- ordinary deposit and every deposit attributed successfully.
        unattributed_reason TEXT CHECK (unattributed_reason IS NULL
                                        OR length(unattributed_reason) > 0),
        PRIMARY KEY (block, event_ordinal)
    )
    """,
    "CREATE INDEX IF NOT EXISTS foreign_deposit_by_wallet_day ON foreign_deposit (coldkey, day)",
    "CREATE INDEX IF NOT EXISTS noah_deposit_by_wallet_day ON noah_deposit (coldkey, day)",
    """
    CREATE TABLE IF NOT EXISTS holding_sample (
        coldkey       TEXT    NOT NULL,
        block         INTEGER NOT NULL CHECK (block > 0),
        day          INTEGER NOT NULL CHECK (day >= 0),
        total_rao     INTEGER NOT NULL CHECK (total_rao >= 0
                                              AND typeof(total_rao) = 'integer'),
        -- NULL means the split was NOT READ, which is a different fact from
        -- read-and-found-zero. A zero here would claim nothing is locked, false
        -- forever on any wallet that has locked something. Both are NULL or
        -- neither is: half an answer is worse than none, because nothing
        -- downstream can tell which half is real.
        locked_rao    INTEGER CHECK (locked_rao IS NULL OR (locked_rao >= 0
                                              AND typeof(locked_rao) = 'integer')),
        available_rao INTEGER CHECK (available_rao IS NULL OR (available_rao >= 0
                                              AND typeof(available_rao) = 'integer')),
        CHECK ((locked_rao IS NULL) = (available_rao IS NULL)),
        PRIMARY KEY (coldkey, block)
    )
    """,
    "CREATE INDEX IF NOT EXISTS holding_sample_by_wallet_day ON holding_sample (coldkey, day)",
    """
    CREATE TABLE IF NOT EXISTS holding_position (
        coldkey            TEXT    NOT NULL,
        block              INTEGER NOT NULL CHECK (block > 0),
        hotkey             TEXT    NOT NULL,
        shares_encoding    TEXT    NOT NULL CHECK (shares_encoding IN ('u64f64', 'safefloat')),
        shares_raw         TEXT    NOT NULL,
        shares_exponent    INTEGER NOT NULL,
        shares_source      TEXT    NOT NULL CHECK (shares_source IN ('v1', 'v2', 'absent')),
        denom_encoding     TEXT    NOT NULL CHECK (denom_encoding IN ('u64f64', 'safefloat')),
        denom_raw          TEXT    NOT NULL,
        denom_exponent     INTEGER NOT NULL,
        denom_source       TEXT    NOT NULL CHECK (denom_source IN ('v1', 'v2')),
        total_hotkey_alpha_rao TEXT NOT NULL,
        PRIMARY KEY (coldkey, block, hotkey)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS block_seen (
        block      INTEGER NOT NULL PRIMARY KEY CHECK (block > 0),
        block_hash TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sampled_day (
        day       INTEGER NOT NULL PRIMARY KEY CHECK (day >= 0),
        stride     INTEGER NOT NULL CHECK (stride > 0),
        from_block INTEGER NOT NULL CHECK (from_block > 0),
        to_block   INTEGER NOT NULL CHECK (to_block > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS emission_start (
        only_row   INTEGER NOT NULL PRIMARY KEY CHECK (only_row = 1),
        day       INTEGER NOT NULL CHECK (day >= 0),
        block      INTEGER NOT NULL CHECK (block > 0),
        block_hash TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS emission_absent (
        only_row   INTEGER NOT NULL PRIMARY KEY CHECK (only_row = 1),
        day       INTEGER NOT NULL CHECK (day >= 0),
        block      INTEGER NOT NULL CHECK (block > 0),
        block_hash TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS no_fault_declaration (
        day       INTEGER NOT NULL CHECK (day >= 0),
        block      INTEGER NOT NULL CHECK (block > 0),
        block_hash TEXT    NOT NULL,
        cause      TEXT    NOT NULL,
        PRIMARY KEY (day, block)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS registration_day (
        block INTEGER NOT NULL PRIMARY KEY CHECK (block > 0),
        day   INTEGER NOT NULL CHECK (day >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scanned_day (
        day       INTEGER NOT NULL PRIMARY KEY CHECK (day >= 0),
        from_block INTEGER NOT NULL,
        to_block   INTEGER NOT NULL
    )
    """,
)


def _whole_amount(value, what):
    """A value fit to sit on the money path, or a refusal."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAnAmount(f"{what} must be a plain integer, got {value!r}.")
    if value < 0:
        raise NotAnAmount(f"{what} must not be negative, got {value}.")
    if value > _MAX_SQLITE_INT:
        raise NotAnAmount(f"{what} exceeds what the store can hold exactly: {value}.")
    return value


def _positive(value, what):
    """A height, which is a whole amount that cannot also be zero.

    Checked here rather than left to the table's own constraint. The database
    reports a broken constraint as an ordinary exception, and an ordinary
    exception on this path is one a retry loop can swallow.
    """
    _whole_amount(value, what)
    if value == 0:
        raise NotAnAmount(f"{what} must be greater than zero, got 0.")
    return value


def _identifier(value, what):
    """A wallet, hotkey or block hash, or a refusal.

    Anything that is not text is refused rather than stored. A wallet arriving
    as raw bytes stores without complaint and is then found by nobody, because
    every later read asks for the text form: the pay sits in the table and the
    participant reads zero. That is this module's own silent zero, manufactured
    by itself rather than by an endpoint.
    """
    if isinstance(value, str) and value.strip():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            # Text to Python, unencodable to the database. It passes a type
            # check and then fails inside the driver as an ordinary exception,
            # well outside anything watching for one.
            raise NotAnIdentifier(
                f"{what} contains characters the store cannot hold: {exc}."
            ) from exc
        return value
    if True:
        raise NotAnIdentifier(
            f"{what} must be a non-empty text account, got {value!r}. A value that is not "
            f"text is stored under a key no later read will ask for, so the row becomes "
            f"invisible instead of becoming an error."
        )
    return value


def _a_share(value, what, allowed_sources):
    """A quantity that arrived carrying the number system it was read from."""
    if not isinstance(value, Share):
        raise NotAnAmount(
            f"{what} came in as {value!r}. A bare number cannot say which of this chain's "
            f"two share encodings it came from, and the two are not comparable, so it "
            f"cannot be stored as one."
        )
    if value.encoding not in ("u64f64", "safefloat"):
        raise NotAnIdentifier(f"{what} names the encoding {value.encoding!r}, which is neither.")
    if value.source not in allowed_sources:
        raise NotAnIdentifier(
            f"{what} says it came from {value.source!r}, which is not one of "
            f"{', '.join(allowed_sources)}."
        )
    # `.source` alone is not enough to trust: `Share` is a plain, publicly
    # constructible dataclass, and this is the one place that validates it
    # before a coherence check anywhere downstream (record_holding_sample's
    # own recompute, holding_by_hotkey's read-side reconstruction) trusts
    # `.source` at face value. `resolve_share`'s own absent construction
    # (holdings.py) is always `raw=0, exponent=0` -- absence there means the
    # wallet was confirmed to hold no position on this hotkey (a real
    # answer, not a failed read: see resolve_share's own docstring), and a
    # confirmed-nothing position has nothing to carry. A Share claiming
    # source="absent" while carrying nonzero raw or exponent is not that
    # shape -- codex adversarial review, 2026-09-03: such a share passed
    # this validation unexamined, then recomputed to a real, large,
    # positive alpha at read time despite being invisible to every
    # write-time coherence check that only looked at `.source`.
    if value.source == "absent" and (value.raw != 0 or value.exponent != 0):
        raise NotAnAmount(
            f"{what} claims to be absent but carries raw={value.raw!r}, "
            f"exponent={value.exponent!r} -- a real absent read is always raw=0, "
            f"exponent=0 (resolve_share's own construction). Anything else is not an "
            f"absent read wearing the label; it is real, uncounted value hiding behind it."
        )
    if isinstance(value.exponent, bool) or not isinstance(value.exponent, int):
        raise NotAnAmount(f"{what} has an exponent of {value.exponent!r}, not a whole number.")
    return value


def _wide_amount(value, what):
    """A value too wide for a database integer, kept exactly rather than narrowed.

    Share-pool components are 128-bit fixed point. Storing one in a column that
    holds 64 bits would silently change a participant's position, so these are
    kept as exact decimal text and are checked here on the way in.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAnAmount(f"{what} must be a plain integer, got {value!r}.")
    if value < 0:
        raise NotAnAmount(f"{what} must not be negative, got {value}.")
    if value >= 2**128:
        raise NotAnAmount(
            f"{what} is {value}, wider than any value this chain stores. It did not come "
            f"from here, so it is a misdecode rather than a large number."
        )
    return value


def _anchor_mismatch(stored, given, verb):
    return AnchorMismatch(
        f"this store was built with day anchor {stored} and was {verb} with {given}. "
        f"Every day in it is counted from the anchor it was built with, so using it "
        f"under another one would move pay and deposits into different days. Deposits "
        f"are permanent, so that error would be too. Rebuild the store instead."
    )


class Store:
    """A store of observed chain facts."""

    def __init__(self, conn, day_anchor_ts):
        # The check lives here, on the only path that can produce the object, so
        # a helper that builds a Store some other way cannot walk past it.
        day_anchor_ts = _whole_amount(int(day_anchor_ts), "day anchor")
        # D-139: the day is derived here rather than trusted from a caller (the
        # module docstring's own rule), so a caller that constructs a Store
        # directly, without going through create()/open(), still gets a
        # midnight-aligned anchor rather than whatever raw timestamp it handed
        # in. Idempotent against create()'s own flooring below: a value that is
        # already floored floors to itself.
        day_anchor_ts = _floor_to_day(day_anchor_ts)
        stored = self._stored_anchor(conn, "<open connection>")
        if stored is None:
            conn.close()
            raise NotANoahStore("this connection carries no store identity.")
        if stored != day_anchor_ts:
            conn.close()
            raise _anchor_mismatch(stored, day_anchor_ts, "used")
        self._conn = conn
        self.day_anchor_ts = stored

    # ---- identity -----------------------------------------------------------

    @staticmethod
    def _stored_anchor(conn, path):
        """The anchor this store carries, or None if the file has no identity yet."""
        try:
            row = conn.execute("SELECT day_anchor_ts FROM identity").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None  # an empty or brand-new file
            # AS OF D-139 (2026-08-31), TESTNET'S OWN INDEPENDENT REVIEW: a
            # real pre-D-139 store has an `identity` table that EXISTS, just
            # under the OLD column name (`week_anchor_ts`, not
            # `day_anchor_ts`), so the SELECT above fails with "no such
            # column", not "no such table" -- before this, that fell through
            # to the generic branch below and was reported as
            # `StoreUnavailable`, a TRANSIENT refusal. A caller that retries
            # transient errors would retry forever against a store that can
            # never open under any number of retries: the schema itself is
            # wrong, not the moment it was read at. Checked ahead of the
            # generic branch, and by name (`SchemaTooOld` already existed for
            # exactly this) rather than folded into it.
            if "no such column" in str(exc):
                raise SchemaTooOld(
                    f"{plain_path_text(path)} has an identity table but not the column this "
                    f"version of the "
                    f"code reads ({exc}). This is an older store, built before D-139 -- "
                    f"rebuild it, or read it with the version of this code it was built "
                    f"with."
                ) from exc
            # Locked, unreadable, out of disk. A store that exists and cannot be
            # read right now must never be reported as one that was never made:
            # that sends the operator to rebuild a store which is fine and full.
            raise StoreUnavailable(f"{plain_path_text(path)} could not be read: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise NotANoahStore(
                f"{plain_path_text(path)} is not a readable Noah store: {exc}"
            ) from exc
        if row is None:
            raise NotANoahStore(
                f"{plain_path_text(path)} has an identity table with no anchor in it, which means "
                f"a "
                f"previous create did not finish. Rebuild the store."
            )
        return row[0]

    @classmethod
    def create(cls, path, day_anchor_ts, timeout=5.0):
        """Build a store, or reopen one that already carries this same anchor.

        ``day_anchor_ts`` is floored to the UTC midnight at or before it
        BEFORE it is compared to what is already stored or written to a new
        file (D-139): the value that ends up as this store's permanent
        identity is always midnight-aligned, regardless of what raw moment
        (e.g. a chain's own genesis block, at whatever time of day it
        happened to land) a caller reads and hands in.
        """
        day_anchor_ts = _floor_to_day(int(day_anchor_ts))
        if os.path.exists(path) and os.path.getsize(path):
            return cls.open(path, day_anchor_ts=day_anchor_ts, timeout=timeout)
        conn = sqlite3.connect(path, timeout=timeout)
        try:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                # Recheck after taking the write lock: another creator may have won.
                stored = cls._stored_anchor(conn, path)
                if stored is not None:
                    raise NotANoahStore("store appeared during creation; reopen it explicitly")
                if conn.execute("PRAGMA user_version").fetchone()[0] != 0:
                    raise SchemaTooOld("new store has an unexpected schema version")
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute("INSERT INTO identity (day_anchor_ts) VALUES (?)", (day_anchor_ts,))
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return cls(conn, day_anchor_ts)
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise NotANoahStore(f"store could not be opened or migrated atomically: {exc}") from exc
        except BaseException:
            conn.close()
            raise

    @classmethod
    def open(cls, path, day_anchor_ts, timeout=5.0, *, read_only=False):
        """Open/migrate under its anchor; read-only callers never migrate or write.

        A read-only schema-17 open names the required migration and refuses.
        Operators migrate explicitly through a write-capable workflow first.
        """
        if not os.path.exists(path):
            raise NotANoahStore(f"{plain_path_text(path)} does not exist. It was never created.")
        conn = (sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                uri=True, timeout=timeout)
                if read_only else sqlite3.connect(path, timeout=timeout))
        try:
            # Identity refusal precedes any migration, including DDL.
            store = cls(conn, day_anchor_ts)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 17 and read_only:
                raise SchemaTooOld("schema 17 requires explicit migration; read-only open changes nothing")
            if version == 17:
                conn.execute("BEGIN IMMEDIATE")
                with conn:
                    # The lock may have waited behind another migrator.
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version == 17:
                        _migrate_foreign_split(conn, version)
                    else:
                        _validate_store_schema(conn, version)
            else:
                _validate_store_schema(conn, version)
            return store
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise NotANoahStore(f"store could not be opened or migrated atomically: {exc}") from exc
        except BaseException:
            conn.close()
            raise

    @classmethod
    def peek_day_anchor_ts(cls, path):
        """The day anchor already stored in ``path``'s own identity table --
        for a caller (e.g. a read-only status tool) that needs to open an
        ALREADY-POPULATED store without knowing its anchor in advance, and
        must never guess or default one. Raises the same real refusals
        ``open``/``create`` already raise for a bad path
        (``NotANoahStore``, ``SchemaTooOld`` never applies here since this
        reads the identity table directly, before any schema-version
        check). This never creates or changes anything -- the connection
        it opens to read is closed again before returning, and a caller
        still calls ``Store.open(path, day_anchor_ts=<this return value>)``
        itself for the full, schema-checked object.
        """
        if not os.path.exists(path):
            raise NotANoahStore(f"{plain_path_text(path)} does not exist. It was never created.")
        conn = sqlite3.connect(path)
        try:
            stored = cls._stored_anchor(conn, path)
        finally:
            conn.close()
        if stored is None:
            raise NotANoahStore(
                f"{plain_path_text(path)} carries no store identity. It was never created."
            )
        return stored

    def close(self):
        self._conn.close()

    # ---- the anchor decides the day ----------------------------------------

    def day_of(self, block_timestamp):
        """Which UTC calendar day a moment falls in: 00:00 UTC to the next 00:00
        UTC (D-139), counted from this store's own anchor.

        Exact and midnight-aligned because the anchor itself is always
        floored to a UTC midnight before it is ever stored (see ``create()``
        and ``__init__``): every real midnight is the anchor plus a whole
        multiple of ``DAY_SECONDS``, so plain floor-division subtraction
        lands on the true calendar boundary, not one offset by whatever time
        of day this store happened to be created. A timestamp on the same
        calendar day as the anchor but earlier than the anchor's own raw
        creation moment (impossible on a real chain, since nothing was
        observed before genesis, but not excluded by the anchor alone once
        it is floored) still falls in day 0: a UTC day is a day, complete,
        whether or not this store has any real activity for its first few
        hours.
        """
        block_timestamp = _whole_amount(block_timestamp, "block timestamp")
        if block_timestamp < self.day_anchor_ts:
            raise TimestampBeforeAnchor(
                f"block timestamp {block_timestamp} is before this store's day anchor "
                f"{self.day_anchor_ts}. There is no day for it."
            )
        return (block_timestamp - self.day_anchor_ts) // DAY_SECONDS

    # ---- observations in, conclusions out ------------------------------------
    #
    # Everything below stores what was seen and where it was seen. Nothing stores
    # what it meant. A rule that is still open cannot silently become a number in
    # a row, because the row outlives the rule and the chain will not answer the
    # same question twice.

    def record_foreign_deposit(
        self,
        coldkey,
        block_timestamp,
        amount_rao,
        token_netuid,
        block,
        event_ordinal,
        extrinsic_index,
        block_hash,
        hotkey=None,
        owner_at_deposit=None,
        origin_hotkey=None,
        unattributed_reason=None,
    ):
        """Record a deposit made in another subnet's token, exactly as observed.

        ``hotkey``/``owner_at_deposit``/``origin_hotkey``/``unattributed_reason``
        (schema 18, issue #16) carry the same attribution a NOAH deposit has, so a
        foreign receipt credits the same miner the same way, and credits nobody
        for the same reasons (no miner named, wrong validator).

        Identified by the block and the event's ordinal within that block.

        Not by the extrinsic. Measured on block 8,889,759, two stake events share
        extrinsic fourteen, and the event's own metadata index is the same number
        for every event of its kind. So the ordinal in the block's event list is
        the only thing that tells two contributions in one block apart, and
        keying on anything else collapses them into one row.
        """
        _positive(block_timestamp, "finalized block timestamp")
        day = self.day_of(block_timestamp)
        _identifier(coldkey, "wallet")
        _identifier(block_hash, "block hash")
        _whole_amount(amount_rao, "amount")
        _whole_amount(token_netuid, "token id")
        _whole_amount(event_ordinal, "the event's position in the block")
        _whole_amount(extrinsic_index, "the extrinsic this event came from")
        _positive(block, "block height")
        for value, what in ((hotkey, "hotkey"), (owner_at_deposit, "owner at deposit"),
                            (origin_hotkey, "origin hotkey"),
                            (unattributed_reason, "unattributed reason")):
            if value is not None:
                _identifier(value, what)
        row = (extrinsic_index, coldkey, day, amount_rao, token_netuid, block_hash,
               hotkey, owner_at_deposit, origin_hotkey, unattributed_reason, block_timestamp)
        seen = self._conn.execute(
            "SELECT extrinsic_index,coldkey,day,amount_rao,token_netuid,block_hash,"
            "hotkey,owner_at_deposit,origin_hotkey,unattributed_reason,block_timestamp "
            "FROM foreign_deposit WHERE block=? AND event_ordinal=?", (block, event_ordinal)
        ).fetchone()
        stale = self._conn.execute("SELECT 1 FROM day_needs_rescan WHERE day=?", (day,)).fetchone() is not None
        enrich_legacy = (seen is not None and stale and tuple(seen[:6]) == row[:6]
                         and all(value is None for value in seen[6:]))
        if seen is not None and not enrich_legacy:
            same = (tuple(seen[:9]) == row[:9] and seen[10] == row[10]
                    and _reason_class(seen[9]) == _reason_class(row[9]))
            if not same:
                raise ObservationChanged(f"foreign deposit at block {block} event {event_ordinal} changed its settled receipt facts")
            return day
        native = self._conn.execute(
            "SELECT extrinsic_index,coldkey,day,amount_rao,block_hash,hotkey,"
            "owner_at_deposit,origin_hotkey,unattributed_reason FROM noah_deposit "
            "WHERE block=? AND event_ordinal=?", (block, event_ordinal)
        ).fetchone()
        if native is not None:
            native_read = (extrinsic_index,coldkey,day,amount_rao,block_hash,hotkey,
                           owner_at_deposit,origin_hotkey,unattributed_reason)
            if (not stale or tuple(native[:8]) != native_read[:8]
                    or _reason_class(native[8]) != _reason_class(native_read[8])):
                raise ObservationChanged(f"NOAH receipt at block {block} event {event_ordinal} was not reproduced exactly before foreign reclassification")
        try:
            with self._conn:
                if native is not None:
                    self._conn.execute("DELETE FROM noah_deposit WHERE block=? AND event_ordinal=?", (block,event_ordinal))
                if enrich_legacy:
                    self._conn.execute(
                        "UPDATE foreign_deposit SET hotkey=?,owner_at_deposit=?,origin_hotkey=?,"
                        "unattributed_reason=?,block_timestamp=? WHERE block=? AND event_ordinal=?",
                        (hotkey,owner_at_deposit,origin_hotkey,unattributed_reason,block_timestamp,block,event_ordinal))
                else:
                    self._conn.execute(
                        "INSERT INTO foreign_deposit (block,event_ordinal,extrinsic_index,coldkey,day,"
                        "amount_rao,token_netuid,block_hash,hotkey,owner_at_deposit,origin_hotkey,"
                        "unattributed_reason,block_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (block,event_ordinal,*row))
        except sqlite3.DatabaseError as exc:
            raise StoreRejected(f"the store refused the deposit at block {block} event {event_ordinal}: {exc}") from exc
        return day

    def pending_foreign_rescans(self):
        """Original scanned ranges remain available while their readiness is revoked."""
        return list(self._conn.execute(
            "SELECT d.day,s.from_block,s.to_block FROM day_needs_rescan d "
            "LEFT JOIN scanned_day s ON s.day=d.day ORDER BY d.day"))

    def require_day_rescan_range(self, day, from_block, to_block):
        """Reject a partial migrated-day replay before reading or writing any receipts."""
        if self._conn.execute("SELECT 1 FROM day_needs_rescan WHERE day=?", (day,)).fetchone() is None:
            return False
        previous = self._conn.execute("SELECT from_block,to_block FROM scanned_day WHERE day=?", (day,)).fetchone()
        if previous is not None and (from_block > previous[0] or to_block < previous[1]):
            raise DayNotScanned("foreign migration requires replay of the entire original scanned range")
        return True

    def _confirm_rescan(self, day, from_block, to_block, observed_receipts):
        if not self.require_day_rescan_range(day, from_block, to_block):
            return
        if observed_receipts is None:
            raise DayNotScanned("foreign migration requires actual receipt reconciliation before scan completion")
        recorded = set(self._conn.execute(
            "SELECT block,event_ordinal FROM noah_deposit WHERE day=? UNION "
            "SELECT block,event_ordinal FROM foreign_deposit WHERE day=?", (day,day)))
        if recorded != set(observed_receipts):
            raise ObservationChanged("foreign rescan did not reproduce the complete recorded receipt event set")

    def mark_day_scanned(self, day, from_block, to_block, *, observed_receipts=None):
        """Record that a day was actually read, over a stated range of blocks.

        ``from_block``/``to_block`` are validated here, not merely stored --
        added under ticket B02 (issue #10, 2026-09-10; see
        :class:`MalformedScanRange`'s own docstring for the finding this
        closes). Each must be a real, positive block height (the same
        ``_positive`` check every other block height on this module's own
        money path already gets), and ``from_block`` must not be AFTER
        ``to_block`` -- a reversed pair cannot describe a real scan of any
        block range, and this is the one place in this store anything is
        ever written into ``scanned_day``, so a check made here closes the
        gap for every caller, not just the one this finding named.
        """
        from_block = _positive(from_block, "from_block")
        to_block = _positive(to_block, "to_block")
        if from_block > to_block:
            raise MalformedScanRange(
                f"day {day}'s scan range is from_block={from_block}, to_block={to_block} -- "
                f"from_block is after to_block, which cannot be a real scan of any block "
                f"range. Nothing was recorded."
            )
        self._confirm_rescan(day, from_block, to_block, observed_receipts)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO scanned_day (day, from_block, to_block) VALUES (?, ?, ?)",
                (_whole_amount(day, "day"), from_block, to_block),
            )
            # A day read again with the current reader is no longer in doubt (issue #16).
            self._conn.execute("DELETE FROM day_needs_rescan WHERE day = ?", (day,))

    def _require_scanned(self, day):
        row = self._conn.execute("SELECT 1 FROM scanned_day WHERE day = ? " + _NOT_STALE, (day,)).fetchone()
        if row is None:
            raise DayNotScanned(
                f"day {day} has never been scanned, so this store cannot say whether "
                f"anything was deposited in it. Absence of data is not absence of "
                f"deposits, and reading it as zero would punish somebody who did deposit."
            )

    def raw_foreign_deposits(self, coldkey, day):
        """The deposits as they were observed. Never converted, never summarised."""
        self._require_scanned(day)
        rows = self._conn.execute(
            "SELECT amount_rao, token_netuid, block, block_hash FROM foreign_deposit "
            "WHERE coldkey = ? AND day = ? ORDER BY block, event_ordinal",
            (coldkey, day),
        ).fetchall()
        return [
            {"amount_rao": a, "token_netuid": n, "block": b, "block_hash": h} for a, n, b, h in rows
        ]

    def foreign_deposit_equivalent(self, coldkey, day, rule):
        """Own-token-equivalent for the day, computed now, from the rule given now.

        The rule's answers are checked before they are summed. This project's
        trust rests on two independent implementations agreeing byte for byte, and
        floating point does not round identically across languages, so nothing but
        exact non-negative integers is allowed anywhere near a payment.
        """
        total = 0
        for row in self.raw_foreign_deposits(coldkey, day):
            converted = rule(row["amount_rao"], row["token_netuid"], row["block"], row["block_hash"])
            total += _whole_amount(converted, "converted amount")
        return total

    def foreign_deposit_equivalent_for_hotkey(self, coldkey, hotkey, day, rule):
        """The day's foreign-token value that THIS SEAT earned, in NOAH, by ``rule``.

        A foreign receipt must explicitly name this miner and its owner, retain
        a destination validator and exact timestamp, and have no refusal reason.
        There is no legacy destination-as-miner fallback for foreign assets.
        The decoder supplies the designated-validator decision at ingestion.

        The rule's answers are checked before they are summed, as in
        :meth:`foreign_deposit_equivalent`. A rule that refuses (a price it cannot
        read) propagates; it is never read as zero.
        """
        _identifier(coldkey, "wallet")
        _identifier(hotkey, "hotkey")
        self._require_scanned(day)
        rows = self._conn.execute(
            "SELECT amount_rao, token_netuid, block, block_hash FROM foreign_deposit "
            "WHERE coldkey = ? AND day = ? AND unattributed_reason IS NULL "
            "AND origin_hotkey = ? AND owner_at_deposit = coldkey "
            "AND hotkey IS NOT NULL AND block_timestamp IS NOT NULL "
            "ORDER BY block, event_ordinal",
            (coldkey, day, hotkey),
        ).fetchall()
        total = 0
        for amount, token_netuid, block, block_hash in rows:
            total += _whole_amount(rule(amount, token_netuid, block, block_hash), "converted amount")
        return total

    def record_attributed_pay(
        self, coldkey, hotkey, uid, block_timestamp, amount_rao, block, block_hash
    ):
        """Record that one seat was paid at one block, and to whom.

        The key is the seat and the block, because that is what the chain
        actually settled. Recording the same epoch twice is free, so a run that
        died mid-day and restarted cannot pay anybody twice.

        The same seat and block coming back with a different answer is not free.
        Ignoring the second write would keep the first answer and say nothing,
        which is how a source that changed its mind about a settled block goes
        unnoticed. Both answers cannot be true, so neither is used.
        """
        day = self.day_of(block_timestamp)
        _identifier(coldkey, "wallet")
        _identifier(hotkey, "hotkey")
        _identifier(block_hash, "block hash")
        _whole_amount(amount_rao, "amount")
        _whole_amount(uid, "seat number")
        _positive(block, "block height")
        row = (coldkey, hotkey, day, amount_rao, block_hash)
        with self._conn:
            seen = self._conn.execute(
                "SELECT coldkey, hotkey, day, amount_rao, block_hash FROM attributed_pay "
                "WHERE uid = ? AND block = ?",
                (uid, block),
            ).fetchone()
            if seen is not None:
                if tuple(seen) != row:
                    raise ObservationChanged(
                        f"seat {uid} at block {block} was already recorded as "
                        f"{self._describe(seen)} and has now come back as "
                        f"{self._describe(row)}. A settled block does not change its mind, so "
                        f"one of these reads is wrong and neither is used. Check the sources "
                        f"before rebuilding."
                    )
                return day
            try:
                self._conn.execute(
                    "INSERT INTO attributed_pay "
                    "(uid, block, coldkey, hotkey, day, amount_rao, block_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid, block, coldkey, hotkey, day, amount_rao, block_hash),
                )
            except sqlite3.DatabaseError as exc:
                # The guards above should have caught anything the table would
                # reject. This is the backstop, and it exists to change the kind
                # of the failure: the database raises an ordinary exception, and
                # an ordinary exception on the money path is one a retry loop
                # written as `except Exception` will swallow.
                raise StoreRejected(
                    f"the store refused the pay row for seat {uid} at block {block}: {exc}"
                ) from exc
        return day

    @staticmethod
    def _describe(row):
        coldkey, hotkey, day, amount_rao, block_hash = row
        return (
            f"{amount_rao} to wallet {coldkey} via hotkey {hotkey} in day {day} "
            f"at block hash {block_hash}"
        )

    def record_epoch(self, attribution, block_timestamp, block_hash):
        """Record every seat one epoch actually paid, from an attributed epoch.

        The block comes from the attribution rather than from the caller, so
        one epoch's pay cannot be filed under another epoch's height.

        This exists so that nobody has to hand-build the seat rows. Rebuilding
        them from the raw reads would mean redoing the walk that decides which
        seats the runtime withheld from, in a code path that had bypassed the
        decision, and a mistake there pays somebody out of alpha that was
        burned.
        """
        for uid, hotkey, coldkey, amount_rao in attribution.paid:
            self.record_attributed_pay(
                coldkey=coldkey,
                hotkey=hotkey,
                uid=uid,
                block_timestamp=block_timestamp,
                amount_rao=amount_rao,
                block=attribution.epoch_block,
                block_hash=block_hash,
            )
        return self.day_of(block_timestamp)

    def record_locked_pay(
        self, *, coldkey, block, block_timestamp, locked_arrival_rao, unlocked_rao, block_hash
    ):
        """Record what one epoch's settle did to one wallet's collateral lock.

        Both figures are recorded even when both are zero, because "this wallet
        was looked at and nothing moved" and "this wallet was never looked at"
        are the two things the fixture must not confuse, and a missing row is
        the second.
        """
        _identifier(coldkey, "the wallet whose lock was read")
        _positive(block, "the epoch block the lock was read at")
        _whole_amount(locked_arrival_rao, "the pay that arrived locked")
        _whole_amount(unlocked_rao, "the pay that unlocked")
        if locked_arrival_rao and unlocked_rao:
            raise NotAnAmount(
                f"{coldkey} at block {block} is recorded as having {locked_arrival_rao} "
                f"arrive locked AND {unlocked_rao} unlock in the same epoch. The runtime's "
                f"two branches are exclusive, so a row carrying both is a reading that netted "
                f"a capture on one hotkey against a release on another and lost both figures."
            )
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO locked_pay "
                "(coldkey, block, day, locked_arrival_rao, unlocked_rao, block_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    coldkey,
                    block,
                    self.day_of(block_timestamp),
                    locked_arrival_rao,
                    unlocked_rao,
                    block_hash,
                ),
            )

    def locked_pay(self, coldkey, day):
        """What arrived locked and what unlocked for this wallet in this day.

        Returns a pair. Raises rather than answering if this wallet's lock was
        never read in this day, because a zero that was never looked at is the
        exact figure this whole path exists to stop being emitted.
        """
        # Asked before the sum, because a day holding one unreadable epoch has
        # a sum that looks complete and is short by that epoch. Answering it
        # would be the quiet undercount this whole path exists to prevent.
        unreadable = self.locked_pay_unreadable(coldkey, day)
        if unreadable is not None:
            raise LockedPayNotRead(
                f"{coldkey} cannot be given locked-pay figures for day {day}. {unreadable}"
            )
        rows = self._conn.execute(
            "SELECT COALESCE(SUM(locked_arrival_rao), 0), COALESCE(SUM(unlocked_rao), 0), "
            "COUNT(*) FROM locked_pay WHERE coldkey = ? AND day = ?",
            (coldkey, day),
        ).fetchone()
        # A day this wallet earned nothing in has nothing to read, and zero is
        # the chain's own answer rather than a stand-in: settle returns before
        # touching anything on a zero emission (collateral.rs:495-497), so
        # nothing can be captured or released by an epoch that paid nobody. The
        # refusal below is for a day that DID pay and was never read, which is
        # a different thing entirely and the one this path exists for.
        if rows[2] == 0 and self.attributed_pay(coldkey, day) == 0:
            return 0, 0
        if rows[2] == 0:
            raise LockedPayNotRead(
                f"{coldkey} has no collateral reading in day {day}. A wallet whose lock was "
                f"never read is not a wallet with no locked pay, and answering zero here is "
                f"how the two became indistinguishable in the first place."
            )
        return rows[0], rows[1]

    def locked_pay_unreadable_before(self, coldkey, day):
        """Why this wallet's locked pay cannot be read on some day BEFORE ``day``, or None."""
        row = self._conn.execute(
            "SELECT reason FROM locked_pay_unreadable WHERE coldkey = ? AND day < ? "
            "ORDER BY day, block LIMIT 1",
            (coldkey, day),
        ).fetchone()
        return row[0] if row else None

    def locked_arrival_before(self, coldkey, day):
        """Total pay recorded as arriving locked for this wallet on days before ``day``.

        The opening balance for a wallet whose registration lock (a
        ``locked_pay`` arrival the runner records on the registration day) lands
        before its first activity day: the fixture's cumulative rule starts at
        that first activity day, so without this the earlier arrival is outside
        the walked span and the first unlock looks like it came from nothing.
        Like :meth:`locked_pay`, it refuses (``LockedPayNotRead``) rather than
        summing a day marked unreadable as zero.
        """
        unreadable = self.locked_pay_unreadable_before(coldkey, day)
        if unreadable is not None:
            raise LockedPayNotRead(
                f"{coldkey} cannot be given an opening locked-arrival figure before day "
                f"{day}. {unreadable}"
            )
        total = self._conn.execute(
            "SELECT COALESCE(SUM(locked_arrival_rao), 0) FROM locked_pay "
            "WHERE coldkey = ? AND day < ?",
            (coldkey, day),
        ).fetchone()[0]
        return _whole_amount(total, "the opening locked arrival")

    def mark_locked_pay_unreadable(self, *, coldkey, block, block_timestamp, reason):
        """Record that this wallet's locked pay cannot be read for this day.

        The ruled fallback, and it is a record rather than a silence: a day
        that cannot be read has to say so by name, or it is indistinguishable
        from a day where nothing happened.
        """
        _identifier(coldkey, "the wallet whose lock could not be read")
        _positive(block, "the block the reading failed at")
        if not isinstance(reason, str) or not reason.strip():
            raise NotAnAmount(
                f"holding {coldkey} out of day's locked pay needs a reason somebody can "
                f"read, got {reason!r}. A hold-out without one is a gap wearing a table."
            )
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO locked_pay_unreadable (coldkey, day, block, reason) "
                "VALUES (?, ?, ?, ?)",
                (coldkey, self.day_of(block_timestamp), block, reason),
            )

    def clear_locked_pay_unreadable(self, coldkey, block):
        """Erase a stale "this wallet's lock could not be read" record for
        this ONE block, because a later, genuinely successful reading for
        the same coldkey at this exact block has just superseded it.

        Ticket #16/#135, 2026-09-22: a day scanned before ``read_collateral``
        flipped ``True`` (commit 9a2f73b) writes a real
        ``locked_pay_unreadable`` row for every paid coldkey via
        ``mark_locked_pay_unreadable`` above. Nothing previously cleared
        that row on a later, successful rescan -- it lives in its own
        table, entirely separate from ``locked_pay`` -- so a real, correct
        reading written by a forced rescan (``may_resume=False``) was
        silently shadowed by the stale marker forever: ``locked_pay``/
        ``_lock_hold_out`` both check ``locked_pay_unreadable`` FIRST,
        before ever looking at whether a real reading also exists.

        **Scoped to ``block``, not ``day`` -- a real regression an
        independent Verifier caught live, 2026-09-22, before this shipped.**
        The table's real primary key is ``(coldkey, block)``, not
        ``(coldkey, day)``: a single day can carry several epoch blocks,
        and a real coldkey on the real server was found unreadable at TWO
        separate blocks (1129 and 1489) within the same day. A day-scoped
        delete fired on every successful per-coldkey commit -- this
        function's own first version -- meant a LATER block's success in
        the same day silently erased an EARLIER block's still-genuinely-
        unresolved failure marker for the whole day, so
        ``store.locked_pay()`` would then return a confident but silently
        incomplete sum instead of correctly raising ``LockedPayNotRead`` --
        exactly the quiet-undercount failure mode this whole mechanism
        exists to prevent. Scoping to the exact block just reread clears
        only that block's own marker: a forced rescan revisits the exact
        same block numbers a stale pre-fix scan marked, so this still
        closes the original incident, and a genuinely still-unreadable
        block elsewhere in the same day keeps its own marker, re-affirmed
        (or, if it too now reads, cleared) only when THAT block is itself
        recommitted. See ``noah.chain.tests.test_agreeing.
        test_a_still_unreadable_block_in_the_same_day_survives_a_different_blocks_successful_rescan``
        for the reproduction.

        Called only from ``noah.chain.runner._commit_locked_pay``'s own
        success branch, immediately after ``record_locked_pay`` writes a
        real reading for this exact coldkey and block -- never
        speculatively, and never for a block that has not actually just
        been read.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM locked_pay_unreadable WHERE coldkey = ? AND block = ?",
                (coldkey, block),
            )

    def locked_pay_unreadable(self, coldkey, day):
        """Why this wallet's locked pay cannot be read this day, or None."""
        row = self._conn.execute(
            "SELECT reason FROM locked_pay_unreadable WHERE coldkey = ? AND day = ? "
            "ORDER BY block LIMIT 1",
            (coldkey, day),
        ).fetchone()
        return row[0] if row else None

    def locked_pay_was_read(self, coldkey, day):
        """Whether this wallet's lock was read in this day at all."""
        return bool(
            self._conn.execute(
                "SELECT 1 FROM locked_pay WHERE coldkey = ? AND day = ? LIMIT 1",
                (coldkey, day),
            ).fetchone()
        )

    def any_locked_arrival(self):
        """Whether ANY wallet in this store ever had pay arrive locked.

        The companion to the cumulative-unlock rule. That rule passes trivially
        on a population where every figure is zero, which is every population
        this reader produced before the collateral read existed, so a run that
        reports it as satisfied has to be able to say whether it was tested.
        """
        return bool(
            self._conn.execute(
                "SELECT 1 FROM locked_pay WHERE locked_arrival_rao > 0 LIMIT 1"
            ).fetchone()
        )

    def locked_pay_summary(self, day=None, coldkeys=None):
        """Every wallet's collateral-lock reading for one day, in one pass --
        the bulk companion to :meth:`locked_pay`, built for #12 group 3/4/6's
        operator view summary the identical reason :meth:`pay_summary`
        (right below) and :meth:`deposit_summary` were: roughly 200 seats on
        one screen must never cost 200 individual calls.

        Collateral is locked per WALLET, not per seat -- ``locked_pay`` (and
        its unreadable-marker table) carry no ``hotkey``/``uid`` column at
        all, unlike ``attributed_pay``. So this takes ``coldkeys`` (plain
        wallets), never ``pairs``, and returns one row per coldkey rather
        than one row per (coldkey, hotkey).

        ``day``, if omitted, is this store's own :meth:`latest_scanned_day`;
        raises :exc:`DayNotScanned` if nothing has ever been scanned (there
        is then no day to answer for). Given explicitly or defaulted, the
        day is then required to have been scanned (:meth:`_require_scanned`)
        before anything is read, exactly like every other day-scoped read in
        this file.

        Each returned dict is ``{"coldkey", "day", "status",
        "locked_arrival_rao", "unlocked_rao", "reason"}``, where ``status``
        is one of three real, distinct answers -- the exact three
        :meth:`locked_pay` already distinguishes for one wallet at a time,
        reproduced here without calling it once per wallet (which would put
        this method's own cost back at O(wallets) despite the bulk
        queries below):

        - ``"read"``: this wallet's lock was actually read this day.
          ``locked_arrival_rao``/``unlocked_rao`` are the real summed
          figures (``reason`` is ``None``). This includes the real,
          trustworthy zero :meth:`locked_pay` itself returns for a wallet
          that was paid nothing this day -- settle returns before touching
          anything on a zero emission, so nothing could have moved, and a
          wallet with no ``attributed_pay`` row this day is answered
          ``(0, 0)`` here for the identical reason, without a row in
          ``locked_pay`` to read.
        - ``"unreadable"``: this wallet's lock could not be read this day.
          ``locked_arrival_rao``/``unlocked_rao`` are ``None`` (not a
          fabricated 0); ``reason`` names why, exactly as
          :meth:`locked_pay_unreadable` reports it.
        - ``"not_read"``: this wallet WAS paid (real ``attributed_pay`` > 0)
          this day, but its lock was never read and no unreadable reason
          was ever recorded for it -- the one case :meth:`locked_pay`
          raises :exc:`LockedPayNotRead` for, reproduced here as an honest
          status rather than an exception, because one missing wallet among
          200 must not fail the other 199.

        If ``coldkeys`` is ``None``, this answers for every wallet this
        store has ever recorded EITHER a ``locked_pay``/
        ``locked_pay_unreadable`` reading for on this day, OR real pay for
        this day -- the union that decides between the three statuses
        above. A coldkey passed explicitly that this store knows nothing
        about at all still comes back with the honest ``"read"``, ``(0,
        0)`` zero, the same answer :meth:`locked_pay` would give it.

        Runs a small, fixed number of queries (three), never one per
        wallet, regardless of how many wallets are asked about or how many
        this store has ever seen.
        """
        if day is None:
            day = self.latest_scanned_day()
            if day is None:
                raise DayNotScanned(
                    "no day has ever been scanned, so this store cannot say what any "
                    "wallet's collateral lock read as. Absence of a scanned day is not "
                    "absence of a lock reading, and reading it as zero would hide "
                    "collateral that was never actually looked at."
                )
        else:
            _whole_amount(day, "day")
        self._require_scanned(day)

        if coldkeys is not None:
            coldkeys = [_identifier(coldkey, "wallet") for coldkey in coldkeys]

        locked_rows = self._conn.execute(
            "SELECT coldkey, SUM(locked_arrival_rao), SUM(unlocked_rao) FROM locked_pay "
            "WHERE day = ? GROUP BY coldkey",
            (day,),
        ).fetchall()
        locked = {coldkey: (arrival, unlocked) for coldkey, arrival, unlocked in locked_rows}

        unreadable_rows = self._conn.execute(
            "SELECT coldkey, reason FROM locked_pay_unreadable WHERE day = ? "
            "ORDER BY coldkey, block",
            (day,),
        ).fetchall()
        unreadable = {}
        for coldkey, reason in unreadable_rows:
            unreadable.setdefault(coldkey, reason)

        paid_rows = self._conn.execute(
            "SELECT DISTINCT coldkey FROM attributed_pay WHERE day = ? AND amount_rao > 0",
            (day,),
        ).fetchall()
        paid = {coldkey for (coldkey,) in paid_rows}

        keys = (
            coldkeys
            if coldkeys is not None
            else sorted(set(locked) | set(unreadable) | paid)
        )

        result = []
        for coldkey in keys:
            if coldkey in unreadable:
                result.append(
                    {
                        "coldkey": coldkey,
                        "day": day,
                        "status": "unreadable",
                        "locked_arrival_rao": None,
                        "unlocked_rao": None,
                        "reason": unreadable[coldkey],
                    }
                )
            elif coldkey in locked:
                arrival, unlocked = locked[coldkey]
                result.append(
                    {
                        "coldkey": coldkey,
                        "day": day,
                        "status": "read",
                        "locked_arrival_rao": arrival,
                        "unlocked_rao": unlocked,
                        "reason": None,
                    }
                )
            elif coldkey in paid:
                result.append(
                    {
                        "coldkey": coldkey,
                        "day": day,
                        "status": "not_read",
                        "locked_arrival_rao": None,
                        "unlocked_rao": None,
                        "reason": (
                            f"{coldkey} was paid real pay in day {day} but its collateral "
                            f"lock was never read and no unreadable reason was recorded "
                            f"for it."
                        ),
                    }
                )
            else:
                result.append(
                    {
                        "coldkey": coldkey,
                        "day": day,
                        "status": "read",
                        "locked_arrival_rao": 0,
                        "unlocked_rao": 0,
                        "reason": None,
                    }
                )
        return result

    def pay_observations(self, coldkey, day):
        """Every seat-and-block this wallet was paid at, in the order they fired.

        This is the evidence behind a published figure. A total on its own
        cannot be checked by anybody; these rows can.
        """
        _identifier(coldkey, "wallet")
        _whole_amount(day, "day")
        self._require_scanned(day)
        rows = self._conn.execute(
            "SELECT uid, hotkey, amount_rao, block, block_hash FROM attributed_pay "
            "WHERE coldkey = ? AND day = ? ORDER BY block, uid",
            (coldkey, day),
        ).fetchall()
        return [
            {"uid": u, "hotkey": hk, "amount_rao": a, "block": b, "block_hash": h}
            for u, hk, a, b, h in rows
        ]

    def attributed_pay(self, coldkey, day):
        """What this wallet was paid in this day, summed from the rows above."""
        return sum(row["amount_rao"] for row in self.pay_observations(coldkey, day))

    def attributed_pay_by_seat(self, coldkey, day):
        """What this wallet was paid in this day, kept apart by seat.

        Added for #193: the door now gates one hotkey at a time (D-072), and a
        wallet with several seats needs each seat's own figure rather than the
        wallet-wide sum `attributed_pay` still answers for callers that have not
        moved off it. Grouped from the same rows `attributed_pay` sums.

        **Grouped by hotkey, not by uid.** #193 codex review Finding 4: a uid
        is only good for the epoch it was read at. The runtime reassigns a
        vacated uid to a different hotkey, which is ordinary Bittensor
        behavior, not an edge case, so a map keyed by uid silently changes
        whose pay an earlier day's group belongs to the moment that
        reassignment happens: read later, it either misses that day's pay
        entirely (nothing paid the new holder of the uid that day) or, worse,
        credits it to whoever holds the uid now. `hotkey` is the one identity
        an `attributed_pay` row carries (`record_attributed_pay` has always
        required it, the same as `uid`) that does not move underneath a
        wallet between the day the pay was recorded and the day a fixture
        asks for it, so it is the key that keeps a day's group attached to
        the seat that actually earned it.

        A seat absent from the returned map was not paid this day, which
        `attributed_pay` already expresses as a 0 folded into its sum; this
        keeps that seat out entirely rather than manufacture a 0 for a hotkey
        this wallet may not even hold this day.
        """
        totals = {}
        for row in self.pay_observations(coldkey, day):
            totals[row["hotkey"]] = totals.get(row["hotkey"], 0) + row["amount_rao"]
        return {hotkey: totals[hotkey] for hotkey in sorted(totals)}

    def pay_summary(self, day=None, pairs=None):
        """Every (coldkey, hotkey) seat's real pay for one day, in one pass --
        the bulk companion to :meth:`attributed_pay`/:meth:`attributed_pay_by_seat`,
        built for #12 group 3/4/6's operator view summary the identical
        reason :meth:`deposit_summary` was (#135 S1): roughly 200 seats on one
        screen must never cost 200 individual reads.

        Unlike :meth:`deposit_summary` (an unbounded cumulative total),
        ``attributed_pay`` is inherently day-scoped -- there is no wallet-wide
        "cumulative pay" concept this store keeps, only what each day paid --
        so this method is day-scoped too. ``day``, if omitted, is this
        store's own :meth:`latest_scanned_day`; raises :exc:`DayNotScanned`
        if nothing has ever been scanned (there is then no day to answer
        for). Given explicitly or defaulted, the day is then required to
        have been scanned (:meth:`_require_scanned`) before anything is
        read, exactly like :meth:`pay_observations` already requires for a
        single wallet. Because the day used can come from either source,
        every returned row carries its own ``"day"`` -- unlike
        `deposit_summary`'s rows, which never need to say which day since
        that method answers across all of them at once.

        ``pairs``, if given, is a list of ``(coldkey, hotkey)`` tuples --
        ``hotkey`` here matched on `attributed_pay`'s own real ``hotkey``
        column directly (no `origin_hotkey`/legacy-fallback matching rule:
        that machinery exists only for `noah_deposit`'s two historical
        deposit shapes, and `attributed_pay` has always carried a real,
        required `hotkey` -- see :meth:`first_activity_day_for_hotkey`'s own
        docstring for why the two tables are matched differently). A pair
        this store recorded no pay for on this day still comes back, reading
        an honest ``amount_rao`` of ``0`` and a ``None`` ``uid`` -- the same
        real, trustworthy zero `attributed_pay` itself would answer for that
        pair, never a sign the pair is missing from the store.

        If ``pairs`` is ``None``, this instead answers for every (coldkey,
        hotkey) pair `attributed_pay` recorded any row for on this day.

        Each row's ``uid`` is the seat number the most recent block that day
        recorded for that (coldkey, hotkey) pair -- picked the identical way
        `deposit_summary` picks `last_validator_hotkey` (a `ROW_NUMBER()`
        window ordered by `block DESC`), because a uid is only good for the
        epoch it was read at (`attributed_pay_by_seat`'s own docstring, #193
        codex review Finding 4) and a hotkey paid at more than one block in
        one day must not have its later uid silently overwritten by
        whichever row SQLite happens to return first. Role (subnet-owner/
        validator vs. miner) is NOT decided here: this store keeps no notion
        of "role", only seats and uids. Callers must interpret each recorded
        uid using the corresponding chain identity; this store does not infer
        owner, validator or miner roles from a seat number.

        Runs two SQL queries regardless of how many pairs are asked about
        (or, with ``pairs=None``, how many the store recorded pay for that
        day): one ``GROUP BY (coldkey, hotkey)`` SUM for ``amount_rao``, and
        one window-function query for each pair's latest-block ``uid`` --
        the identical two-query shape `deposit_summary` uses for its own two
        derived fields.

        Returns a list of dicts, each ``{"coldkey", "hotkey", "uid",
        "amount_rao", "day"}``.
        """
        if day is None:
            day = self.latest_scanned_day()
            if day is None:
                raise DayNotScanned(
                    "no day has ever been scanned, so this store cannot say what any "
                    "seat was paid. Absence of a scanned day is not absence of pay, and "
                    "reading it as zero would understate what every seat actually "
                    "received."
                )
        else:
            _whole_amount(day, "day")
        self._require_scanned(day)

        if pairs is not None:
            pairs = [
                (_identifier(coldkey, "wallet"), _identifier(hotkey, "hotkey"))
                for coldkey, hotkey in pairs
            ]

        sum_rows = self._conn.execute(
            "SELECT coldkey, hotkey, SUM(amount_rao) FROM attributed_pay "
            "WHERE day = ? GROUP BY coldkey, hotkey",
            (day,),
        ).fetchall()
        pay = {(coldkey, hotkey): total for coldkey, hotkey, total in sum_rows}

        uid_rows = self._conn.execute(
            "SELECT coldkey, hotkey, uid FROM ("
            "  SELECT coldkey, hotkey, uid,"
            "         ROW_NUMBER() OVER ("
            "             PARTITION BY coldkey, hotkey ORDER BY block DESC"
            "         ) AS rn"
            "  FROM attributed_pay WHERE day = ?"
            ") WHERE rn = 1",
            (day,),
        ).fetchall()
        seat = {(coldkey, hotkey): uid for coldkey, hotkey, uid in uid_rows}

        keys = sorted(pay.keys()) if pairs is None else pairs
        return [
            {
                "coldkey": coldkey,
                "hotkey": hotkey,
                "uid": seat.get((coldkey, hotkey)),
                "amount_rao": pay.get((coldkey, hotkey), 0),
                "day": day,
            }
            for coldkey, hotkey in keys
        ]

    # ---- whether the subnet had begun emitting, and any declaration ----------
    #
    # Emission begins once, at the start call, and the chain keeps the block it
    # began at rather than a flag, under a guard against it ever being set
    # twice. So one recorded start answers every day, past and future, by
    # arithmetic, and nothing needs recording as it happens.
    #
    # An earlier version of this store recorded the state per day instead, on
    # the belief that the chain could not be asked afterwards. That was wrong
    # about this particular fact, and it cost two defects: a consistency check
    # that treated the transition day itself as a contradiction and could wedge
    # the store permanently, and a per-day row whose day came from its caller
    # rather than from a block, which amounted to a quiet switch for excusing
    # everybody in a day of the operator's choosing.

    def _one_row(self, table):
        return self._conn.execute(
            f"SELECT day, block, block_hash FROM {table} WHERE only_row = 1"
        ).fetchone()

    def recorded_emission_start(self):
        """The recorded ``(day, block, block_hash)`` of the emission start, or
        ``None`` if none was ever recorded.

        Lets a caller answer "did emission start where the chain still says it
        did" from this store alone. The start block ages out of a pruned node's
        state after about 90 days, and its timestamp cannot be read again then
        (issue #237).
        """
        row = self._one_row("emission_start")
        return None if row is None else tuple(row)

    def record_emission_start(self, block, block_timestamp, block_hash):
        """Record the block the subnet began emitting at.

        Pass the block the chain itself stores, which is the block after the one
        the start call executed in. The two can fall in different days when the
        start call lands in a day's last block, and the stored one is the block
        emission actually begins at.
        """
        day = self.day_of(block_timestamp)
        _positive(block, "block height")
        _identifier(block_hash, "block hash")

        seen = self._one_row("emission_start")
        if seen is not None:
            if tuple(seen) != (day, block, block_hash):
                raise EmissionStateChanged(
                    f"the subnet was already recorded as beginning to emit at block "
                    f"{seen[1]} in day {seen[0]}, and has now been recorded as beginning at "
                    f"block {block} in day {day}. The chain refuses to set this twice, so "
                    f"one of these reads is wrong and neither is used."
                )
            return day

        absent = self._one_row("emission_absent")
        if absent is not None and block <= absent[1]:
            # Compared on blocks, never on days. Observing "not started yet" and
            # then "started" inside one day is the ordinary transition, not a
            # contradiction, and a day-level comparison calls it one.
            raise EmissionStateChanged(
                f"the subnet was observed as not yet emitting at block {absent[1]}, and is "
                f"now recorded as having begun at block {block}, which is not later. "
                f"Emission cannot begin at a height where it had already been seen not to "
                f"have begun."
            )
        self._write(
            "INSERT INTO emission_start (only_row, day, block, block_hash) VALUES (1, ?, ?, ?)",
            (day, block, block_hash),
            f"the emission start at block {block}",
        )
        return day

    def record_emission_not_started(self, block, block_timestamp, block_hash):
        """Record that at this block, the subnet had not begun emitting.

        Only the latest such observation is kept, because emission begins once:
        an earlier absence is implied by a later one and adds nothing.
        """
        day = self.day_of(block_timestamp)
        _positive(block, "block height")
        _identifier(block_hash, "block hash")

        started = self._one_row("emission_start")
        if started is not None and started[1] <= block:
            raise EmissionStateChanged(
                f"the subnet is recorded as beginning to emit at block {started[1]}, so it "
                f"cannot also have been seen not yet emitting at block {block}, which is not "
                f"earlier."
            )
        seen = self._one_row("emission_absent")
        if seen is not None and seen[1] >= block:
            return seen[0]  # a later absence already covers this one
        self._write(
            "INSERT OR REPLACE INTO emission_absent (only_row, day, block, block_hash) "
            "VALUES (1, ?, ?, ?)",
            (day, block, block_hash),
            f"the absence of emission at block {block}",
        )
        return day

    def subnet_was_emitting(self, day):
        """Whether the subnet was emitting in this day, or None if nobody looked.

        The day emission began in counts as an emitting day. Emission starting
        partway through does not make the whole day free, and the cost of that
        choice is recorded rather than hidden: a miner idle for the rest of that
        day is counted at fault.

        None means unobserved. This module will not invent an answer, because
        guessing it was emitting costs somebody their streak and guessing the
        other way hands everybody a free pass.
        """
        _whole_amount(day, "day")
        started = self._one_row("emission_start")
        if started is not None:
            return day >= started[0]
        absent = self._one_row("emission_absent")
        if absent is not None and day <= absent[0]:
            # Emission begins once, so an absence in a later day settles every
            # earlier one too. Refusing here would halt a run over a day whose
            # answer is not in doubt.
            return False
        return None

    def record_no_fault_declaration(self, day, block, block_timestamp, block_hash, cause):
        """Record a dated declaration by Noah covering one day.

        This is the only discretionary lever in the design, so it is a standing
        public record of Noah's own failure, and it is held to the same standard
        as a pay row: it cannot be rewritten afterwards, and it cannot be dated
        before the day it excuses. A declaration made in advance would be a
        blanket forward pardon, which is the quiet discretion the rule exists to
        prevent.
        """
        _whole_amount(day, "day")
        _positive(block, "block height")
        _identifier(block_hash, "block hash")
        _identifier(cause, "declared cause")
        declared_in = self.day_of(block_timestamp)
        if declared_in < day:
            raise NotAnAmount(
                f"this declaration covers day {day} but was made in day {declared_in}, "
                f"before the day it excuses had finished. A declaration names a day and a "
                f"cause after the fact; one made in advance excuses a day nobody has seen "
                f"yet."
            )
        # Keyed by the block it was published at, not by the day it covers. A
        # day can break more than once, and since the owner ruled that the
        # software publishes its own failure as a matter of course rather than
        # a person deciding, several declarations in one day is ordinary.
        # Keying by day alone would refuse the second one and jam the reader on
        # exactly the days that went worst. A declaration is a record of an
        # event, not a verdict about a day.
        row = (block_hash, cause)
        seen = self._conn.execute(
            "SELECT block_hash, cause FROM no_fault_declaration WHERE day = ? AND block = ?",
            (day, block),
        ).fetchone()
        if seen is not None:
            if tuple(seen) != row:
                raise DeclarationChanged(
                    f"the declaration for day {day} published at block {block} already "
                    f"reads {seen[1]!r} and is now being written as {cause!r}. A published "
                    f"account of our own failure is not rewritten afterwards. Publish a "
                    f"further declaration instead; a day may carry several."
                )
            return day
        self._write(
            "INSERT INTO no_fault_declaration (day, block, block_hash, cause) VALUES (?, ?, ?, ?)",
            (day, block, block_hash, cause),
            f"the no-fault declaration for day {day}",
        )
        return day

    def no_fault_declarations(self, day):
        """Every declaration covering this day, in the order they were published."""
        _whole_amount(day, "day")
        rows = self._conn.execute(
            "SELECT day, block, block_hash, cause FROM no_fault_declaration "
            "WHERE day = ? ORDER BY block",
            (day,),
        ).fetchall()
        return [{"day": w, "block": b, "block_hash": h, "cause": c} for w, b, h, c in rows]

    def no_fault_declaration(self, day):
        """The first declaration covering this day, or None.

        The rule asks whether the day is covered at all, so the first is
        enough for it. Anything reporting on what went wrong wants
        ``no_fault_declarations`` and all of them.
        """
        declarations = self.no_fault_declarations(day)
        return declarations[0] if declarations else None

    def _write(self, statement, values, what):
        """Run one write, reporting a database refusal as a refusal.

        The guards above should catch anything the tables would reject. This
        changes the kind of any failure that gets past them: the database raises
        an ordinary exception, and an ordinary exception on this path is one a
        retry loop written as `except Exception` will swallow.
        """
        try:
            with self._conn:
                self._conn.execute(statement, values)
        except sqlite3.DatabaseError as exc:
            raise StoreRejected(f"the store refused {what}: {exc}") from exc

    # ---- deposits into the Ark, in this subnet's own token --------------------
    #
    # A deposit is irreversible and it is the thing this project exists to
    # measure, so it is identified by exactly where it happened: the block and
    # the position within it. One wallet can send the same amount twice in a
    # single block, and those two are told apart by nothing else. A key without
    # the position records one of them and silently drops the other, which costs
    # the depositor credit for a contribution nobody can give back.

    def record_noah_deposit(
        self,
        coldkey,
        block_timestamp,
        amount_rao,
        block,
        event_ordinal,
        extrinsic_index,
        block_hash,
        carried_by,
        hotkey=None,
        owner_at_deposit=None,
        origin_hotkey=None,
        unattributed_reason=None,
    ):
        """Record one deposit into the Ark, exactly as observed.

        Keyed by the block and the event's ordinal within it, for the reason
        given on the foreign-token version of this: the extrinsic index repeats
        within a block and would collapse two contributions into one.

        ``hotkey``, added for D-138 reopened (issue #112): which hotkey the
        deposit's alpha arrived on, off the same ``StakeAdded`` arrival
        ``noah.chain.deposits.Deposit`` already carries it from. Optional and
        defaulting to None -- a coldkey could only ever hold one seat before
        D-138 reopened, so no caller before this one had a real hotkey to
        give, and every existing caller of this method still works unchanged.
        Stored but not yet read by anything (see :meth:`noah_deposit_hotkeys`
        for the one accessor this phase adds); a later phase scores per-seat
        from it.

        ``owner_at_deposit``/``origin_hotkey``, added for issue #112 comment
        5751446658 (2026-09-20): a deposit's staking hotkey (``hotkey``,
        above) never changes on ``transfer_stake`` -- it stays whichever
        validator the miner originally delegated to -- so it cannot, by
        itself, say which miner the deposit credits once two miners share
        one wallet and one validator hotkey. ``owner_at_deposit`` is the
        coldkey that owned the credited miner's hotkey AT THE DEPOSIT'S OWN
        BLOCK (read historically -- see
        ``noah.deposits.owner_entitlement.confirm_owner_entitled_at_block``;
        this method only stores whatever value is passed in). ``origin_hotkey``
        is the miner hotkey this deposit is actually attributed to, once the
        remark naming it can be decoded -- ``None`` for now, since that
        decoder is a separate, still-pending piece of work (#111). Both
        optional and defaulting to None, so every existing caller of this
        method still works unchanged.

        ``unattributed_reason``, added in schema 16: set
        when a batch_all+remark attribution attempt was made and failed
        (ambiguous batch, malformed remark, or an unentitled signer) --
        recorded here rather than dropping the deposit, because the alpha
        genuinely moved to the Ark regardless of whether it could be
        attributed to a specific miner hotkey. ``origin_hotkey`` stays
        ``None`` on such a row; ``hotkey`` (the position's own, off
        ``StakeAdded``) is unaffected -- it is a real fact independent of
        whether the remark's attribution succeeded. Optional and defaulting
        to None, so every existing caller of this method still works
        unchanged.
        """
        day = self.day_of(block_timestamp)
        _identifier(coldkey, "wallet")
        _identifier(block_hash, "block hash")
        _whole_amount(amount_rao, "amount")
        _whole_amount(event_ordinal, "the event's position in the block")
        _whole_amount(extrinsic_index, "the extrinsic this event came from")
        _positive(block, "block height")
        _identifier(carried_by, "the event that carried the deposit")
        if hotkey is not None:
            _identifier(hotkey, "hotkey")
        if owner_at_deposit is not None:
            _identifier(owner_at_deposit, "owner at deposit")
        if origin_hotkey is not None:
            _identifier(origin_hotkey, "origin hotkey")
        if unattributed_reason is not None:
            _identifier(unattributed_reason, "the reason a deposit went unattributed")

        if self._conn.execute("SELECT 1 FROM foreign_deposit WHERE block=? AND event_ordinal=?", (block,event_ordinal)).fetchone():
            raise ObservationChanged("an already recorded foreign event cannot become a NOAH deposit")
        old_extrinsic = self._conn.execute("SELECT extrinsic_index FROM noah_deposit WHERE block=? AND event_ordinal=?", (block,event_ordinal)).fetchone()
        if old_extrinsic is not None and old_extrinsic[0] != extrinsic_index:
            raise ObservationChanged("a recorded NOAH receipt changed its extrinsic index")

        row = (
            coldkey,
            day,
            amount_rao,
            block_hash,
            carried_by,
            hotkey,
            owner_at_deposit,
            origin_hotkey,
            unattributed_reason,
        )
        seen = self._conn.execute(
            "SELECT coldkey, day, amount_rao, block_hash, carried_by, hotkey, "
            "owner_at_deposit, origin_hotkey, unattributed_reason FROM noah_deposit "
            "WHERE block = ? AND event_ordinal = ?",
            (block, event_ordinal),
        ).fetchone()
        if seen is not None:
            same_but_reworded = (
                tuple(seen[:8]) == row[:8]
                and seen[8] is not None
                and row[8] is not None
                and _reason_class(seen[8]) == _reason_class(row[8])
            )
            if tuple(seen) != row and not same_but_reworded:
                old_unnamed_row = seen[7] is None and seen[8] is None
                raise ObservationChanged(
                    f"the deposit at block {block} event {event_ordinal} was already "
                    f"recorded as {seen[2]} from wallet {seen[0]} in day {seen[1]}, and has "
                    f"now come back as {amount_rao} from wallet {coldkey} in day {day}, "
                    f"carried by {seen[4]} then and {carried_by} now, hotkey {seen[5]!r} then "
                    f"and {hotkey!r} now, owner at deposit {seen[6]!r} then and "
                    f"{owner_at_deposit!r} now, origin hotkey {seen[7]!r} then and "
                    f"{origin_hotkey!r} now, unattributed reason {seen[8]!r} then and "
                    f"{unattributed_reason!r} now. A settled block does not change its mind, "
                    f"so one of these reads is wrong and neither is used."
                    + (
                        " This row was stored before rule B, with no miner hotkey and no "
                        "reason; nothing is rewritten in place. To apply the rule-B reader "
                        "to old days, rebuild the store from the chain into a fresh store "
                        "(the recovery tool's rebuild route; the from-empty path is not yet "
                        "proven end to end)."
                        if old_unnamed_row and row[8] is not None
                        else ""
                    )
                )
            return day
        self._write(
            "INSERT INTO noah_deposit (block, event_ordinal, extrinsic_index, coldkey, "
            "day, amount_rao, block_hash, carried_by, hotkey, owner_at_deposit, "
            "origin_hotkey, unattributed_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                block,
                event_ordinal,
                extrinsic_index,
                coldkey,
                day,
                amount_rao,
                block_hash,
                carried_by,
                hotkey,
                owner_at_deposit,
                origin_hotkey,
                unattributed_reason,
            ),
            f"the deposit at block {block} event {event_ordinal}",
        )
        return day

    def deposit_observations(self, coldkey, day):
        """Every deposit this wallet made in this day, in the order they landed."""
        _identifier(coldkey, "wallet")
        _whole_amount(day, "day")
        self._require_scanned(day)
        rows = self._conn.execute(
            "SELECT amount_rao, block, event_ordinal, extrinsic_index, block_hash, carried_by "
            "FROM noah_deposit WHERE coldkey = ? AND day = ? ORDER BY block, event_ordinal",
            (coldkey, day),
        ).fetchall()
        return [
            {
                "amount_rao": a,
                "block": b,
                "event_ordinal": o,
                "extrinsic_index": i,
                "block_hash": h,
                "carried_by": c,
            }
            for a, b, o, i, h, c in rows
        ]

    def unattributed_deposits(self, day=None, through_day=None, coldkey=None):
        """Every NOAH deposit recorded with an ``unattributed_reason`` -- one
        that moved alpha into the Ark but credits nobody (rule B, #338: no
        miner hotkey named, or sent to the wrong validator) -- oldest first.

        Also lists a legacy row (stored before rule B: no origin hotkey and no
        reason) with the synthesized reason :data:`LEGACY_UNNAMED_REASON` and
        ``legacy`` true, so a human can tell it from a row the rule-B reader
        recorded. A legacy row whose staking ``hotkey`` is a seat's own hotkey
        may still credit that seat through the legacy fallback in
        :meth:`noah_deposits_for_hotkey`; listing it here does not remove that.

        Read-only. ``day`` selects one day; ``through_day`` selects every day
        up to and including it; ``coldkey`` narrows to one wallet. With no
        argument it lists them all. Each item carries the raw facts and the
        reason text exactly as recorded.
        """
        clauses = ["(unattributed_reason IS NOT NULL OR origin_hotkey IS NULL)"]
        params = []
        if day is not None:
            _whole_amount(day, "day")
            self._require_scanned(day)
            clauses.append("day = ?")
            params.append(day)
        if through_day is not None:
            _whole_amount(through_day, "day")
            clauses.append("day <= ?")
            params.append(through_day)
        if coldkey is not None:
            _identifier(coldkey, "wallet")
            clauses.append("coldkey = ?")
            params.append(coldkey)
        rows = self._conn.execute(
            "SELECT coldkey, day, block, extrinsic_index, amount_rao, carried_by, hotkey, "
            "origin_hotkey, unattributed_reason FROM noah_deposit WHERE "
            + " AND ".join(clauses)
            + " ORDER BY day, block, event_ordinal",
            params,
        ).fetchall()
        keys = (
            "coldkey",
            "day",
            "block",
            "extrinsic_index",
            "amount_rao",
            "carried_by",
            "hotkey",
            "origin_hotkey",
            "reason",
        )
        listed = []
        for row in rows:
            item = dict(zip(keys, row))
            item["legacy"] = item["reason"] is None
            if item["legacy"]:
                item["reason"] = LEGACY_UNNAMED_REASON
            listed.append(item)
        return listed

    def noah_deposit_hotkeys(self, coldkey, day):
        """The hotkey recorded for each of this wallet's deposits in this day,
        in the same block/event order :meth:`deposit_observations` returns
        them in, and ``None`` for any deposit recorded without one.

        A separate accessor rather than a new key on
        :meth:`deposit_observations`'s own return value: this phase (D-138
        reopened, issue #112) only stores the hotkey a deposit arrived on so
        a later phase can attribute a contribution to a specific seat --
        every existing caller of :meth:`deposit_observations` asks for
        exactly the shape it already returns, and this phase does not change
        what any of them reads. See ``record_noah_deposit``'s own docstring.
        """
        _identifier(coldkey, "wallet")
        _whole_amount(day, "day")
        self._require_scanned(day)
        rows = self._conn.execute(
            "SELECT hotkey FROM noah_deposit WHERE coldkey = ? AND day = ? "
            "ORDER BY block, event_ordinal",
            (coldkey, day),
        ).fetchall()
        return [row[0] for row in rows]

    def noah_deposit_credited_hotkeys(self, coldkey, day):
        """The miner hotkey each of this wallet's deposits in this day is
        actually credited to, in the same block/event order
        :meth:`deposit_observations` returns them in.

        A separate accessor from :meth:`noah_deposit_hotkeys`, which returns
        the raw ``hotkey`` column unchanged (the position's STAKING
        VALIDATOR, per issue #112 comment 5751446658, 2026-09-20 -- never
        the credited miner once a batch_all+remark deposit has an
        ``origin_hotkey``). This one applies the identical origin_hotkey-
        first, legacy-hotkey-fallback rule :meth:`noah_deposits_for_hotkey`
        documents in full: a row's credited hotkey is its own
        ``origin_hotkey`` if set; otherwise, if ``unattributed_reason`` is
        also unset, its own (legacy-shape) ``hotkey`` column; otherwise
        (an on-purpose-unattributed batch_all deposit, or a row with
        neither) ``None`` -- a value that can never equal a real hotkey a
        caller asks about, so such a row is never credited to anyone.

        Built for ``noah.deposits.d143.entry_deposit_block``, which used to
        filter its own per-seat walk directly against the raw ``hotkey``
        column via :meth:`noah_deposit_hotkeys` -- the identical bug
        :meth:`noah_deposits_for_hotkey` was fixed for, in a second, separate
        code path that does not go through that method. See this project's
        `noah/chain/deposits.py` module docstring and `Deposit`'s own
        docstring for the underlying column semantics this mirrors.
        """
        _identifier(coldkey, "wallet")
        _whole_amount(day, "day")
        self._require_scanned(day)
        rows = self._conn.execute(
            "SELECT CASE "
            "  WHEN origin_hotkey IS NOT NULL AND unattributed_reason IS NULL THEN origin_hotkey "
            "  WHEN unattributed_reason IS NULL THEN hotkey "
            "  ELSE NULL "
            "END FROM noah_deposit WHERE coldkey = ? AND day = ? "
            "ORDER BY block, event_ordinal",
            (coldkey, day),
        ).fetchall()
        return [row[0] for row in rows]

    def known_hotkeys_of(self, coldkey):
        """Every hotkey this store has recorded against this wallet's deposits
        (the validator hotkey the alpha arrived on, and the miner hotkey a
        remark attributed it to), sorted. Read-only; used to name the hotkeys a
        holding sample must be able to prove ``StakingHotkeys`` did not omit.
        """
        rows = self._conn.execute(
            "SELECT hotkey FROM noah_deposit WHERE coldkey = ? AND hotkey IS NOT NULL "
            "UNION SELECT origin_hotkey FROM noah_deposit "
            "WHERE coldkey = ? AND origin_hotkey IS NOT NULL",
            (coldkey, coldkey),
        ).fetchall()
        return sorted(row[0] for row in rows)

    def noah_deposits(self, coldkey, day):
        """What this wallet contributed in this day that QUALIFIES for
        mining credit -- summed directly by SQL, never by summing
        :meth:`deposit_observations`'s own raw rows (issue #282 item
        4/4a, #302: a row whose ``unattributed_reason`` is set -- a
        deposit that genuinely moved to the Ark but staked to the wrong
        validator, or otherwise could not be attributed -- is a real
        holding this store never discards, but it does not qualify and
        must not inflate this total, single-seat wallet or not -- this
        settles the OPEN DECISION
        ``test_s7_a_wallet_with_one_seat_keeps_the_wallet_wide_reading``
        used to flag, uniformly with the multi-seat case
        ``test_s7_a_deposit_naming_no_seat_credits_no_seat_of_a_shared_
        wallet`` already covers. ``deposit_observations`` itself stays the
        RAW, unfiltered per-row read on purpose: a caller that needs to
        show every real holding, qualifying or not, reads that instead of
        this)."""
        _identifier(coldkey, "wallet")
        _whole_amount(day, "day")
        self._require_scanned(day)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_rao), 0) FROM noah_deposit "
            "WHERE coldkey = ? AND day = ? AND unattributed_reason IS NULL",
            (coldkey, day),
        ).fetchone()
        return row[0]

    def deposit_origin_hotkeys(self, coldkey, through_day):
        """Every hotkey some deposit of this wallet, on or before ``through_day``,
        was attributed to (``origin_hotkey``), as a set.

        One of the facts that says how many hotkeys a wallet has ever had: a
        wallet whose deposits name two hotkeys is a shared wallet even on a day
        when only one of them still holds a seat. Reads what was recorded and
        nothing else; a legacy row with no ``origin_hotkey`` names none, and a
        row carrying an ``unattributed_reason`` (a deposit that does not
        qualify, rule B #338) names none either: it credits nobody, so it is
        no evidence that this wallet has that hotkey.
        """
        _identifier(coldkey, "wallet")
        _whole_amount(through_day, "day")
        rows = self._conn.execute(
            "SELECT DISTINCT origin_hotkey FROM noah_deposit "
            "WHERE coldkey = ? AND day <= ? AND origin_hotkey IS NOT NULL "
            "AND unattributed_reason IS NULL",
            (coldkey, through_day),
        ).fetchall()
        return {row[0] for row in rows}

    def noah_deposits_for_hotkey(self, coldkey, hotkey, day):
        """What THIS SPECIFIC SEAT's own deposits contributed on this day.

        Credits a row to ``hotkey`` (the seat being asked about) if EITHER:

        - the row's ``origin_hotkey`` names it exactly -- a batch_all +
          remark deposit correctly attributed to this miner (issue #112,
          comment 5751446658, 2026-09-20: ``hotkey`` on such a row is the
          STAKING VALIDATOR's hotkey, never the credited miner, so it is
          never matched here once ``origin_hotkey`` is set); or
        - ``origin_hotkey`` is ``NULL`` AND ``unattributed_reason`` is
          ``NULL`` AND the row's own ``hotkey`` column names it -- a
          legacy-shape row recorded before ``origin_hotkey`` existed, back
          when a coldkey could hold only one seat and that seat's own
          hotkey WAS the staking hotkey, by the old, now-corrected design.

        The second branch is a legacy-compatibility fallback and is still
        live: a row written by the rule-B reader (#338) never uses it (such a
        row either has an ``origin_hotkey`` or a reason), but an OLD row with no
        origin and no reason whose staking ``hotkey`` equals this seat's own
        hotkey still credits that seat. Production deposits stage on the
        designated validator, never on a miner hotkey, so this only ever
        matches old or synthetic rows.

        A row whose ``unattributed_reason`` is set is NEVER matched by
        either branch (issue #282 item 4/4a, #302: as of the wrong-
        validator check, ``unattributed_reason`` can now be set on a row
        that ALSO carries a real ``origin_hotkey`` -- a deposit correctly
        attributed to a real miner that nonetheless landed on the wrong
        validator, "wrong-validator deposits remain real Ark receipts but
        earn zero qualifying mining credit" -- so the SQL below excludes
        ``unattributed_reason IS NOT NULL`` up front, before either branch,
        never relying on the two fields being mutually exclusive the way a
        batch_all-only failure once made them).

        Never a coldkey-wide total split or guessed across seats (#112,
        D-138 reopened: up to 32 hotkeys per coldkey, each funded by its own
        independent entry deposit -- D-072, "each seat maintaining its
        own... one holding may never satisfy multiple seats"). See
        :meth:`first_activity_day_for_hotkey` for the identical boundary
        applied to the companion "when did this seat first appear" question
        (unaffected by this correction -- see that method's own docstring).
        """
        _identifier(coldkey, "wallet")
        _identifier(hotkey, "hotkey")
        self._require_scanned(day)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_rao), 0) FROM noah_deposit "
            "WHERE coldkey = ? AND day = ? AND unattributed_reason IS NULL AND ("
            "  origin_hotkey = ?"
            "  OR (origin_hotkey IS NULL AND hotkey = ?)"
            ")",
            (coldkey, day, hotkey, hotkey),
        ).fetchone()
        return row[0]

    def cumulative_noah_deposits(self, coldkey, through_day):
        """Every QUALIFYING rao this wallet has deposited into the Ark,
        summed through ``through_day`` inclusive -- a running total across
        days, unlike :meth:`noah_deposits`, which answers for one day in
        isolation.

        Excludes any row with ``unattributed_reason`` set (issue #282 item
        4/4a, #302) -- most notably a real deposit that landed on the
        WRONG validator (checked at scan time by
        ``noah.chain.deposits.deposits_in_block``'s own
        ``designated_validator_hotkey``): the alpha genuinely moved to the
        Ark and the row is still recorded (never dropped), but it does not
        count toward this wallet's running qualifying total. Wrong-validator
        deposits remain real Ark receipts but earn zero qualifying mining
        credit. A caller
        that wants the coldkey's RAW total, including non-qualifying rows
        (an audit/display view, never a qualification decision), needs a
        different read -- this method's own meaning is "counts toward
        qualification," not "ever received."

        This is the read Rule 1 ("entry before score", #7
        issuecomment-5623324414: "no score and no weight for a miner until
        its 100 NOAH entry deposit is confirmed in the Ark") needs: whether
        the RUNNING total has reached the ruled entry figure by a given day,
        not merely what landed on one day. See
        ``noah.deposits.entry_gate.confirm_entry_before_score``, which is
        built on this.

        A direct SQL sum over ``day <= through_day`` rather than a per-day
        loop through :meth:`deposit_observations`, which would need every
        one of those days individually marked scanned
        (``_require_scanned``); a caller asking "has this wallet ever
        deposited enough" is asking about the wallet's recorded history, not
        re-confirming that every single day since genesis was read. Exactly
        the same convention :meth:`first_activity_day` already uses for the
        identical reason (see that method's own docstring): the answer is
        only as complete as what has actually been scanned, and a caller
        relying on it for a permanent decision must satisfy itself that the
        relevant days were covered.
        """
        _identifier(coldkey, "wallet")
        _whole_amount(through_day, "day")
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_rao), 0) FROM noah_deposit "
            "WHERE coldkey = ? AND day <= ? AND unattributed_reason IS NULL",
            (coldkey, through_day),
        ).fetchone()
        return row[0]

    def cumulative_noah_deposits_for_hotkey(self, coldkey, hotkey, through_day):
        """Every rao THIS SPECIFIC SEAT's own real, on-chain deposits have
        contributed to the Ark through ``through_day`` inclusive -- never
        the coldkey's aggregate across every seat it holds.

        Mirrors :meth:`cumulative_noah_deposits` exactly, scoped by hotkey
        too, for the reason Rule 1 ("no score and no weight ... until its
        entry deposit is confirmed in the Ark", #7) now needs it:
        ``noah.deposits.entry_gate.confirm_entry_before_score`` checks each
        SEAT's own confirmation once a coldkey can legitimately hold up to
        32 of them, so a well-funded seat's deposit can never silently
        vouch for a different, unfunded seat of the same wallet.

        Uses the identical ``origin_hotkey``-first, legacy-``hotkey``-
        fallback matching :meth:`noah_deposits_for_hotkey` documents in
        full -- see that method's own docstring for the exact rule,
        including issue #282 item 4/4a, #302's own correction: a row with
        ``unattributed_reason`` set (which now includes a wrong-validator
        deposit that DID get a real ``origin_hotkey``) is excluded before
        either branch is tried, never matched by either. A row recorded
        with no hotkey and no origin_hotkey at all is not counted for any
        seat, the identical boundary that method draws, for the identical
        reason.
        """
        _identifier(coldkey, "wallet")
        _identifier(hotkey, "hotkey")
        _whole_amount(through_day, "day")
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_rao), 0) FROM noah_deposit "
            "WHERE coldkey = ? AND day <= ? AND unattributed_reason IS NULL AND ("
            "  origin_hotkey = ?"
            "  OR (origin_hotkey IS NULL AND hotkey = ?)"
            ")",
            (coldkey, through_day, hotkey, hotkey),
        ).fetchone()
        return row[0]

    # The exact matching expression `cumulative_noah_deposits_for_hotkey` and
    # `noah_deposits_for_hotkey` document in full: a row counts for a miner
    # hotkey when `origin_hotkey` names it directly, or -- only when no
    # attribution attempt was ever made or recorded (`origin_hotkey IS
    # NULL`) -- when the legacy `hotkey` column names it instead. Either
    # way, `unattributed_reason IS NULL` is required first (issue #282 item
    # 4/4a, #302): a wrong-validator deposit can now carry a real
    # `origin_hotkey` AND `unattributed_reason` at once, and such a row is
    # never credited to anyone here, exactly like those two methods.
    # `deposit_summary` below builds this once so both of its queries agree
    # with each other and with them.
    _MINER_HOTKEY_EXPR = (
        "CASE WHEN unattributed_reason IS NOT NULL THEN NULL "
        "WHEN origin_hotkey IS NOT NULL THEN origin_hotkey "
        "ELSE hotkey END"
    )
    _MINER_HOTKEY_MATCH_WHERE = (
        "unattributed_reason IS NULL AND (origin_hotkey IS NOT NULL OR hotkey IS NOT NULL)"
    )

    def deposit_summary(self, pairs=None):
        """Every (coldkey, miner hotkey) seat's cumulative real deposit into
        the Ark, and the validator hotkey it is currently staked through --
        for #12's operator view summary view across every miner at once
        (issue #135), which needs this for roughly 200 seats in one screen
        and explicitly must never issue 200 individual calls to get it.

        ``pairs``, if given, is a list of ``(coldkey, hotkey)`` tuples --
        ``hotkey`` here meaning the MINER's own hotkey, matched exactly the
        way :meth:`cumulative_noah_deposits_for_hotkey` matches it (see that
        method's own docstring for the full rule and its rationale): a row
        counts for a pair when its `origin_hotkey` names that hotkey, or --
        only when no attribution was ever recorded and none was attempted
        and failed -- when its legacy `hotkey` column does. A pair this
        store has no matching deposit for still comes back, reading an
        honest ``cumulative_deposit_rao`` of ``0`` and a ``None``
        ``last_validator_hotkey``, exactly the zero :meth:`cumulative_noah_deposits_for_hotkey`
        itself would answer for the identical pair -- this is a real,
        trustworthy zero, not a sign the pair is missing from the store.

        If ``pairs`` is ``None``, this instead answers for every pair the
        store has ever recorded a CREDITED deposit for -- every row the
        matching rule above actually attributes to some miner hotkey. A row
        with `unattributed_reason` set, or with neither `origin_hotkey` nor
        `hotkey` at all, is not credited to anyone and never contributes a
        pair here, the identical boundary
        :meth:`cumulative_noah_deposits_for_hotkey` draws for a single pair.

        Unlike that method, this takes no ``through_day``: it is the
        unbounded cumulative total, mirroring :meth:`cumulative_noah_deposits`
        (also unbounded) rather than the day-scoped
        :meth:`noah_deposits_for_hotkey`. And unlike
        `GET /entry-deposit-evidence/<coldkey>/<hotkey>` (this project's
        other reader of this exact matching rule), this method does NOT
        report an ``as_of_day`` itself, on purpose: whether anything has
        ever been scanned is one single store-wide fact, not a per-row one,
        so a caller gets it once from :meth:`latest_scanned_day` instead of
        this method silently recomputing (and risking disagreeing with) it
        on every row. A caller that must tell "nothing has been scanned
        yet" apart from "scanned, and confirmed zero for every seat" needs
        to ask :meth:`latest_scanned_day` first, exactly as
        :meth:`cumulative_noah_deposits_for_hotkey`'s own docstring already
        requires -- this method changes nothing about that boundary.

        Runs exactly two SQL queries regardless of how many pairs are asked
        about (or, with ``pairs=None``, how many the store has ever seen):
        one ``GROUP BY (coldkey, miner hotkey)`` SUM for
        ``cumulative_deposit_rao``, and one window-function query
        (``ROW_NUMBER() OVER (PARTITION BY coldkey, miner hotkey ORDER BY
        day DESC, block DESC, event_ordinal DESC)``) that picks each pair's
        most-recently-recorded validator hotkey (the raw `hotkey` column --
        which validator the alpha is CURRENTLY staked through, as opposed to
        `origin_hotkey`, which never changes once attributed). Ordered by
        `day` first and, within a day, by `block`/`event_ordinal` -- the
        identical pair this file already orders same-day rows by elsewhere
        (see `noah_deposits`/`deposit_observations`) -- so "most recent" is
        deterministic rather than left to whatever order SQLite happens to
        return ties in. Both queries scan the whole table once each; this
        is deliberately NOT one query per pair, which is the entire point
        #12 raised this ticket to avoid.

        Returns a list of dicts, each ``{"coldkey", "hotkey",
        "cumulative_deposit_rao", "last_validator_hotkey"}`` -- this file's
        existing convention for a multi-field read with no dedicated row
        type (see `deposit_observations`, `_position_row`, and the plain
        dicts `scanned_day` reads build).
        """
        if pairs is not None:
            pairs = [
                (_identifier(coldkey, "wallet"), _identifier(hotkey, "hotkey"))
                for coldkey, hotkey in pairs
            ]

        sum_rows = self._conn.execute(
            f"SELECT coldkey, {self._MINER_HOTKEY_EXPR} AS miner_hotkey, SUM(amount_rao) "
            f"FROM noah_deposit WHERE {self._MINER_HOTKEY_MATCH_WHERE} "
            f"GROUP BY coldkey, miner_hotkey"
        ).fetchall()
        cumulative = {(coldkey, miner_hotkey): total for coldkey, miner_hotkey, total in sum_rows}

        latest_rows = self._conn.execute(
            f"SELECT coldkey, miner_hotkey, validator_hotkey FROM ("
            f"  SELECT coldkey, {self._MINER_HOTKEY_EXPR} AS miner_hotkey, "
            f"         hotkey AS validator_hotkey,"
            f"         ROW_NUMBER() OVER ("
            f"             PARTITION BY coldkey, {self._MINER_HOTKEY_EXPR}"
            f"             ORDER BY day DESC, block DESC, event_ordinal DESC"
            f"         ) AS rn"
            f"  FROM noah_deposit WHERE {self._MINER_HOTKEY_MATCH_WHERE}"
            f") WHERE rn = 1"
        ).fetchall()
        last_validator = {
            (coldkey, miner_hotkey): validator_hotkey
            for coldkey, miner_hotkey, validator_hotkey in latest_rows
        }

        keys = sorted(cumulative.keys()) if pairs is None else pairs
        return [
            {
                "coldkey": coldkey,
                "hotkey": hotkey,
                "cumulative_deposit_rao": cumulative.get((coldkey, hotkey), 0),
                "last_validator_hotkey": last_validator.get((coldkey, hotkey)),
            }
            for coldkey, hotkey in keys
        ]

    def deposit_summary_by_day(self, day=None, pairs=None):
        """Every (coldkey, miner hotkey) seat's real deposit CREDITED on ONE
        day -- the day-scoped sibling of :meth:`deposit_summary` (issue #12:
        real, day-specific deposit reporting, additive to, and never a
        replacement for, that method's own unbounded LIFETIME total, which
        stays lifetime-only and unchanged by this method's existence).

        Built the same way :meth:`pay_summary` turned `attributed_pay`'s own
        real ``day`` column into a bulk, day-scoped summary of
        `attributed_pay`: the identical two-query shape, applied here to
        `noah_deposit`'s own real ``day`` column, combined with
        :meth:`deposit_summary`'s own matching rule (``_MINER_HOTKEY_EXPR``/
        ``_MINER_HOTKEY_MATCH_WHERE``, the ``origin_hotkey``-first,
        legacy-``hotkey``-fallback rule those two class attributes already
        document in full) rather than reinventing it -- see
        :meth:`deposit_summary`'s own docstring for the complete matching
        rule and why a row with ``unattributed_reason`` set is never
        credited to anyone by either method.

        ``day``, if omitted, defaults to this store's own
        :meth:`latest_scanned_day`, exactly like :meth:`pay_summary`;
        raises :exc:`DayNotScanned` if nothing has ever been scanned (there
        is then no day to answer for). Given explicitly or defaulted, the
        day is then required to have been scanned (:meth:`_require_scanned`)
        before anything is read, exactly like :meth:`pay_summary` requires.

        ``pairs``, if given, is a list of ``(coldkey, hotkey)`` tuples --
        ``hotkey`` here meaning the MINER's own hotkey, matched exactly the
        way :meth:`deposit_summary` matches it. A pair with no deposit
        CREDITED to it on this day still comes back, reading an honest
        ``credited_rao`` of ``0`` and a ``None`` ``last_validator_hotkey`` --
        a real, trustworthy zero, never a sign the pair is missing from the
        store or that the day was not scanned.

        If ``pairs`` is ``None``, this instead answers for every pair the
        matching rule actually credits a deposit to ON THIS DAY -- never
        every pair the store has EVER credited across its whole history
        (that unbounded scope belongs to `deposit_summary` alone). A day
        genuinely scanned with no credited deposit at all returns an empty
        list, exactly like :meth:`pay_summary` does for a quiet day.

        Runs exactly two SQL queries regardless of how many pairs are asked
        about (or, with ``pairs=None``, how many the store credited a
        deposit to that day): one ``GROUP BY (coldkey, miner hotkey) WHERE
        day = ?`` SUM for ``credited_rao``, and one window-function query
        (``ROW_NUMBER() OVER (PARTITION BY coldkey, miner hotkey ORDER BY
        block DESC, event_ordinal DESC)``, itself scoped to this same
        ``day``) that picks each pair's latest-block validator hotkey THIS
        DAY -- unlike `deposit_summary`'s own window function, this one
        needs no ``day DESC`` tiebreak first, since every row it considers
        already shares the identical ``day``.

        Returns a list of dicts, each ``{"coldkey", "hotkey",
        "credited_rao", "last_validator_hotkey", "day"}`` -- deliberately
        DIFFERENT field names from both sibling methods' own amount fields
        (`deposit_summary`'s ``cumulative_deposit_rao``, `pay_summary`'s
        ``amount_rao``): a caller reading a bulk response must never mistake
        this day-scoped CREDITED-deposit figure for either the unbounded
        lifetime total or a day's PAY.
        """
        if day is None:
            day = self.latest_scanned_day()
            if day is None:
                raise DayNotScanned(
                    "no day has ever been scanned, so this store cannot say what any "
                    "seat was credited with depositing. Absence of a scanned day is not "
                    "absence of deposits, and reading it as zero would understate what "
                    "every seat actually deposited."
                )
        else:
            _whole_amount(day, "day")
        self._require_scanned(day)

        if pairs is not None:
            pairs = [
                (_identifier(coldkey, "wallet"), _identifier(hotkey, "hotkey"))
                for coldkey, hotkey in pairs
            ]

        sum_rows = self._conn.execute(
            f"SELECT coldkey, {self._MINER_HOTKEY_EXPR} AS miner_hotkey, SUM(amount_rao) "
            f"FROM noah_deposit WHERE day = ? AND ({self._MINER_HOTKEY_MATCH_WHERE}) "
            f"GROUP BY coldkey, miner_hotkey",
            (day,),
        ).fetchall()
        credited = {(coldkey, miner_hotkey): total for coldkey, miner_hotkey, total in sum_rows}

        latest_rows = self._conn.execute(
            f"SELECT coldkey, miner_hotkey, validator_hotkey FROM ("
            f"  SELECT coldkey, {self._MINER_HOTKEY_EXPR} AS miner_hotkey, "
            f"         hotkey AS validator_hotkey,"
            f"         ROW_NUMBER() OVER ("
            f"             PARTITION BY coldkey, {self._MINER_HOTKEY_EXPR}"
            f"             ORDER BY block DESC, event_ordinal DESC"
            f"         ) AS rn"
            f"  FROM noah_deposit WHERE day = ? AND ({self._MINER_HOTKEY_MATCH_WHERE})"
            f") WHERE rn = 1",
            (day,),
        ).fetchall()
        last_validator = {
            (coldkey, miner_hotkey): validator_hotkey
            for coldkey, miner_hotkey, validator_hotkey in latest_rows
        }

        keys = sorted(credited.keys()) if pairs is None else pairs
        return [
            {
                "coldkey": coldkey,
                "hotkey": hotkey,
                "credited_rao": credited.get((coldkey, hotkey), 0),
                "last_validator_hotkey": last_validator.get((coldkey, hotkey)),
                "day": day,
            }
            for coldkey, hotkey in keys
        ]

    # ---- the day a participant first appears --------------------------------

    def first_activity_day(self, coldkey):
        """The earliest day this wallet was paid anything or contributed anything.

        A day the wallet was paid zero is not an appearance. The chain records
        a zero for every seat it did not pay, and treating one as a first
        appearance would begin somebody's history in a day they did nothing,
        which lengthens every streak calculation that follows it.

        Deposits count whatever token they were made in, because contributing
        is appearing whether or not the wallet ever mined. This wallet-level
        day only places a wallet in the fixture's span; entry qualification is
        per seat and reads only NOAH (:meth:`first_activity_day_for_hotkey`
        and :meth:`noah_deposits_for_hotkey`; issue #16). A NOAH deposit that
        carries an ``unattributed_reason`` (names no miner hotkey, or went to
        the wrong validator; rule B #338) credits nobody and does not count as
        an appearance: see :meth:`unattributed_deposits` for those.

        This is the earliest day among what has actually been scanned. It
        cannot see activity in a day nobody read, so a caller assembling a
        fixture has to satisfy itself that the days below this one were
        covered.
        """
        _identifier(coldkey, "wallet")
        days = [
            self._conn.execute(
                "SELECT MIN(day) FROM attributed_pay WHERE coldkey = ? AND amount_rao > 0",
                (coldkey,),
            ).fetchone()[0],
            self._conn.execute(
                "SELECT MIN(day) FROM noah_deposit WHERE coldkey = ? "
                "AND unattributed_reason IS NULL",
                (coldkey,),
            ).fetchone()[0],
            self._conn.execute(
                "SELECT MIN(day) FROM foreign_deposit WHERE coldkey = ?", (coldkey,)
            ).fetchone()[0],
        ]
        seen = [w for w in days if w is not None]
        return min(seen) if seen else None

    def first_activity_day_for_hotkey(self, coldkey, hotkey):
        """The earliest day THIS SPECIFIC SEAT (not the whole wallet) was
        paid anything or had a deposit attributed to it (#112, D-138
        reopened: up to 32 hotkeys per coldkey, each with its own real
        entry history).

        Two different tables, two different columns, on purpose:

        - ``attributed_pay`` is matched on its own real ``hotkey`` column
          directly, unchanged. A row there comes from real on-chain
          emission paid straight to the miner's own registered UID/hotkey
          -- no staking validator in between, so there is no validator/miner
          conflation to correct here (#112, comment 5751446658,
          2026-09-20).
        - Rule B (#338): a row carrying an ``unattributed_reason`` (unnamed, or
          named but staked to the wrong validator) earns no credit, so it does
          not start this seat's history either, matching :meth:`first_activity_day`.
        - ``noah_deposit`` uses the identical ``origin_hotkey``-first,
          legacy-``hotkey``-fallback rule :meth:`noah_deposits_for_hotkey`
          documents in full: a row counts for this seat if its own
          ``origin_hotkey`` names it, or (only when ``unattributed_reason``
          is also unset) its legacy-shape ``hotkey`` column does. A row's
          raw ``hotkey`` column there names the STAKING VALIDATOR once
          ``origin_hotkey`` is set, never the credited miner, so it is
          never matched directly -- the same bug already fixed in that
          method and in :meth:`noah_deposit_credited_hotkeys`, here for a
          third read path.

        Mirrors :meth:`first_activity_day` exactly, scoped by hotkey too,
        for one reason: :meth:`first_activity_day` answers a WALLET-level
        question ("does this coldkey have any real history at all"), which
        stopped being the right question for a per-seat re-registration
        guard the moment a coldkey could legitimately hold more than one
        live seat at once. An older, already-active seat's own real history
        must never block a genuinely NEW seat of the same wallet from its
        own fresh entry credit -- see
        ``noah.core.chain_adapter._entry_deposits_for``'s own docstring for
        where this is used and why.

        Foreign deposits are not counted here, the same boundary
        :meth:`first_activity_day` draws (issue #16: entry reads only real
        NOAH). A deposit recorded with no hotkey and no origin_hotkey
        at all (every deposit made before this reopening, when a coldkey
        could hold only one seat) is not counted here either, deliberately:
        guessing which of a coldkey's several seats an unattributed deposit
        belongs to would be exactly the heuristic, arrival-order
        attribution this reopening exists to rule out, not a fix for it.
        """
        _identifier(coldkey, "wallet")
        _identifier(hotkey, "hotkey")
        days = [
            self._conn.execute(
                "SELECT MIN(day) FROM attributed_pay WHERE coldkey = ? AND hotkey = ? "
                "AND amount_rao > 0",
                (coldkey, hotkey),
            ).fetchone()[0],
            self._conn.execute(
                "SELECT MIN(day) FROM noah_deposit WHERE coldkey = ? "
                "AND unattributed_reason IS NULL AND ("
                "  origin_hotkey = ?"
                "  OR (origin_hotkey IS NULL AND hotkey = ?)"
                ")",
                (coldkey, hotkey, hotkey),
            ).fetchone()[0],
        ]
        seen = [w for w in days if w is not None]
        return min(seen) if seen else None

    def first_spendable_pay_day_for_hotkey(self, coldkey, hotkey):
        """The earliest day THIS hotkey seat ever had a real, nonzero mining
        payment recorded, or ``None`` if it has never been paid (#112: the
        "starting treatment" rule for ``noah.scoring.engine`` -- a seat's
        multiplier is held at a flat 1.0 from its first real activity through
        the day of its own first-ever real, spendable payment, never
        restored afterward).

        A pure query against persisted chain history (``attributed_pay``),
        never in-memory or session state: called again after a validator
        restart, with nothing cached, it re-derives the identical day,
        because the fact is durable before the restart and stays durable
        after it. Keyed by hotkey, not uid, for the same reason
        :meth:`attributed_pay_by_seat` and :meth:`first_activity_day_for_hotkey`
        are -- a uid is only good for the epoch it was read at, and this
        question ("when did THIS hotkey seat first get paid, ever") must
        survive uid reassignment and re-registration alike: the hotkey
        persists across both; the uid and the registration event do not.
        One-way by construction -- nothing clears or overwrites an
        ``attributed_pay`` row, so a day this once returns can never later
        return `None` or a later day for the same hotkey.

        **PROVISIONAL, precisely bounded (issue #111, resolved 2026-09-20,
        the recorded design decision):**
        this treats any nonzero ``attributed_pay_by_seat`` amount as
        "spendable." That is CORRECT for the standard case -- registration
        collateral only, no voluntary floor set via ``set_min_collateral``:
        captured share is always 0, the whole incentive is credited as
        ordinary stake, and it is genuinely spendable. It only overcounts
        (calls a day "first spendable" when it was not fully spendable) in
        the narrow case where a miner has voluntarily set their own
        collateral floor AND their early epochs were entirely captured
        toward it -- collateral capture/release has no chain event of its
        own, it only shows as a delta in the ``MinerCollateral[(netuid,
        hotkey, coldkey)]`` row between blocks, and is genuinely per-hotkey
        on-chain (this store's own ``locked_pay`` table just does not record
        it that granularly yet -- a separate, existing gap, not this
        method's or this task's to close). Do not read this docstring as
        "TBD": the bound is now known and narrow, not open-ended.
        """
        _identifier(coldkey, "wallet")
        _identifier(hotkey, "hotkey")
        row = self._conn.execute(
            "SELECT MIN(day) FROM attributed_pay WHERE coldkey = ? AND hotkey = ? "
            "AND amount_rao > 0",
            (coldkey, hotkey),
        ).fetchone()
        return row[0]

    # ---- holdings, kept as samples rather than as a verdict -------------------
    #
    # Whether the holding requirement reads a participant's whole position or
    # only the part they could actually move, and over what stretch of time, is
    # not settled. Both readings are defensible and they record different
    # things, so nothing here concludes anything.
    #
    # Every component is kept in the number system the chain stored it in.
    # Shares and totals live in two of them, migration happens one key at a
    # time, and the new one is a mantissa with an exponent whose mantissa alone
    # carries no magnitude at all. Flattening either to a bare number loses the
    # scale, and for the new one loses the size of the number outright, with no
    # way to recover it afterwards.

    def mark_day_sampled(self, day, stride, from_block, to_block):
        """Record that a day's holdings were actually sampled, and how often.

        Separate from marking a day scanned, because they are different
        activities. Scanning reads every block in a range for deposits, so
        finding none means none. Sampling visits a stride of blocks, so a day
        nobody sampled has no holdings recorded and must not read as a day of
        empty wallets.
        """
        _whole_amount(day, "day")
        _positive(stride, "sampling stride")
        _positive(from_block, "first sampled block")
        _positive(to_block, "last sampled block")
        if to_block < from_block:
            raise NotAnAmount(
                f"the sampled range for day {day} ends at block {to_block}, before it "
                f"starts at {from_block}."
            )
        self._write(
            "INSERT OR REPLACE INTO sampled_day (day, stride, from_block, to_block) "
            "VALUES (?, ?, ?, ?)",
            (day, stride, from_block, to_block),
            f"the sampling record for day {day}",
        )
        return day

    def sampling_of_day(self, day):
        """How a day's holdings were sampled, or None."""
        _whole_amount(day, "day")
        row = self._conn.execute(
            "SELECT day, stride, from_block, to_block FROM sampled_day WHERE day = ?",
            (day,),
        ).fetchone()
        if row is None:
            return None
        return {"day": row[0], "stride": row[1], "from_block": row[2], "to_block": row[3]}

    def _require_sampled(self, day):
        if self.sampling_of_day(day) is None:
            raise DayNotSampled(
                f"day {day} was never sampled for holdings, so this store cannot say what "
                f"anybody held in it. Reading that as an empty position would fail a "
                f"participant who held throughout, because a holding is a gate they either "
                f"pass or do not."
            )

    def _pin_block_hash(self, block, block_hash):
        """One block height, one hash, across every wallet read at it.

        Without this each row carries its own hash and nothing compares them,
        so a reorg between two wallets' reads leaves no trace anywhere.
        """
        seen = self._conn.execute(
            "SELECT block_hash FROM block_seen WHERE block = ?", (block,)
        ).fetchone()
        if seen is not None:
            if seen[0] != block_hash:
                raise BlockHashChanged(
                    f"block {block} was already read at hash {seen[0]} and has now been read "
                    f"at hash {block_hash}. One height has one hash, so the chain moved "
                    f"underneath these reads and they cannot be mixed."
                )
            return
        self._write(
            "INSERT INTO block_seen (block, block_hash) VALUES (?, ?)",
            (block, block_hash),
            f"the hash of block {block}",
        )

    def record_holding_sample(
        self,
        coldkey,
        block_timestamp,
        block,
        block_hash,
        total_rao,
        locked_rao,
        available_rao,
        positions,
    ):
        """Record one wallet's position as it stood at one block."""
        day = self.day_of(block_timestamp)
        _identifier(coldkey, "wallet")
        _identifier(block_hash, "block hash")
        _whole_amount(total_rao, "total holding")
        _positive(block, "block height")

        # Not read is a property of the PAIR. One number beside one absence is a
        # caller holding half an answer and not knowing which half.
        split_read = (locked_rao is not NOT_READ, available_rao is not NOT_READ)
        if split_read[0] != split_read[1]:
            raise SplitNotRead(
                f"the holding for {coldkey} at block {block} has a locked portion of "
                f"{locked_rao!r} beside an available portion of {available_rao!r}. Whether "
                f"the split was read is a property of the pair, and half an answer is worse "
                f"than none because nothing downstream can tell which half is real."
            )

        if not split_read[0]:
            locked_rao = None
            available_rao = None
        else:
            _whole_amount(locked_rao, "locked portion")
            _whole_amount(available_rao, "available portion")

        if split_read[0] and locked_rao + available_rao != total_rao:
            raise HoldingNotCoherent(
                f"the holding for {coldkey} at block {block} does not add up: a total of "
                f"{total_rao} against a locked portion of {locked_rao} and an available "
                f"portion of {available_rao}, which come to {locked_rao + available_rao}. "
                f"One of these three reads is wrong, so none of them is stored."
            )

        rows = self._position_rows(coldkey, block, positions)

        # The three portions adding up is an identity any caller can satisfy and
        # says nothing about the chain. This says something: a total credited on
        # nothing confirmed anywhere is a wallet credited on nobody's word, and
        # nothing later can check it.
        #
        # Each position's own shares carry the source they were read from --
        # "v1", "v2", or "absent" (`_a_share`'s vocabulary, built in
        # `holdings.resolve_share`, and canonically shaped -- raw=0, exponent=0
        # -- by `_a_share`'s own validation above). "absent" is NOT "no read
        # happened": per resolve_share's own docstring, it means the read DID
        # happen and confirmed the wallet holds no position on this hotkey --
        # a real, valid answer of nothing, not a failure. A row's raw numerator
        # or exponent do not say whether a position is genuinely populated, and
        # used to stand in for it here, wrongly: a real v2 (SafeFloat) share is
        # `normalize`d into a narrow, nonzero mantissa band before it is ever
        # written (holdings.py's own module docstring), so a real, correctly-
        # read share carries a nonzero raw digit and a nonzero exponent almost
        # always -- whether the position it describes computes to a large
        # alpha or to exactly zero rao. A wallet that moved its whole holding
        # elsewhere leaves precisely that: a real v1/v2 read whose share,
        # against the pool's real total, floors to zero (live-reproduced
        # 2026-09-02, block 7941: mantissa ~2e20, exponent -34, real total
        # alpha 4794627743202, share computes to ~2e-14 of the pool --
        # correctly zero at rao's 9 decimals). That is a true, coherent zero,
        # not a confirmed-nothing read wearing a confusing shape, so it is
        # stored, never refused for it.
        #
        # `real_read` is derived from `rows` (the already-validated, already-
        # serialized tuples), never re-read from `positions` (the caller's own
        # mutable input) -- codex adversarial review, 2026-09-03: rereading a
        # mutable argument after validation trusts whatever it answers with
        # the SECOND time, not what `_position_rows` actually validated.
        real_read = any(row[6] != "absent" for row in rows)
        if total_rao > 0 and not rows:
            raise HoldingNotCoherent(
                f"the holding for {coldkey} at block {block} is {total_rao} with no positions "
                f"behind it. A figure with no components is a wallet credited on nobody's "
                f"word, and nothing later can check it."
            )
        if total_rao > 0 and rows and not real_read:
            raise HoldingNotCoherent(
                f"the holding for {coldkey} at block {block} is {total_rao} with "
                f"{len(rows)} position(s) behind it, and every one confirms nothing -- no "
                f"position behind it was ever populated. A total credited on positions that "
                f"all confirm empty is a wallet credited on nobody's word, the same as a "
                f"total with no positions at all."
            )

        # `total_rao` is trusted from the caller everywhere else in this store
        # (see `test_holding_by_hotkey_matches_positions_for_coldkey_for_one_seat`'s
        # own comment: that trust is normally `holding_for_coldkey`'s own sum,
        # not something this store re-derives). A recorded zero is the one
        # exception, checked here by literally recomputing it -- the same
        # `positions_for_coldkey` arithmetic `holding_by_hotkey` runs on these
        # exact stored components at read time (`pool_alpha * share / denom`,
        # floored) -- rather than trusted like every other total. Found by
        # codex adversarial review, 2026-09-03: the source-only check above
        # cannot tell a real dust position (D2's case, genuinely zero) apart
        # from a real, non-dust position whose caller simply mislabeled the
        # total as zero -- both have a real, non-absent source. A recorded
        # zero that recomputes positive is exactly the silent zero this
        # function exists to refuse; only computing the real answer can catch
        # it, and it is cheap only in this one direction because it only ever
        # has to prove "positive", never match an exact figure.
        if total_rao == 0 and rows:
            entries = [
                {
                    "hotkey": row[2],
                    "share": Share(
                        encoding=row[3], raw=int(row[4]), exponent=row[5], source=row[6]
                    ),
                    "denominator": Share(
                        encoding=row[7], raw=int(row[8]), exponent=row[9], source=row[10]
                    ),
                    "pool_alpha_rao": int(row[11]),
                }
                for row in rows
            ]
            computed_rao = sum(position.alpha_rao for position in positions_for_coldkey(entries))
            if computed_rao > 0:
                raise HoldingNotCoherent(
                    f"the holding for {coldkey} at block {block} is recorded as zero, but "
                    f"its {len(rows)} position(s) compute to {computed_rao} rao when run "
                    f"through the same arithmetic `holding_by_hotkey` would later use to "
                    f"read them back. A stored zero that recomputes positive reads a "
                    f"participant who holds as though they do not."
                )

        # Before the idempotence check, not after. A re-read of one height under a
        # different hash has the same holding in it, so it looks unchanged and
        # would return early, and the reorg that produced it would leave no trace.
        self._pin_block_hash(block, block_hash)

        sample = (day, total_rao, locked_rao, available_rao)
        seen = self._conn.execute(
            "SELECT day, total_rao, locked_rao, available_rao FROM holding_sample "
            "WHERE coldkey = ? AND block = ?",
            (coldkey, block),
        ).fetchone()
        if seen is not None:
            stored = self._conn.execute(
                "SELECT hotkey, shares_encoding, shares_raw, shares_exponent, shares_source, "
                "denom_encoding, denom_raw, denom_exponent, denom_source, "
                "total_hotkey_alpha_rao FROM holding_position "
                "WHERE coldkey = ? AND block = ? ORDER BY hotkey",
                (coldkey, block),
            ).fetchall()
            if tuple(seen) != sample or [tuple(r) for r in stored] != sorted(r[2:] for r in rows):
                raise ObservationChanged(
                    f"the holding for {coldkey} at block {block} was already recorded as "
                    f"{seen[1]} in day {seen[0]}, and has now come back as {total_rao} in "
                    f"day {day}, or with different components. A settled block does not "
                    f"change its mind, so one of these reads is wrong and neither is used."
                )
            return day

        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO holding_sample "
                    "(coldkey, block, day, total_rao, locked_rao, available_rao) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (coldkey, block, day, total_rao, locked_rao, available_rao),
                )
                self._conn.executemany(
                    "INSERT INTO holding_position "
                    "(coldkey, block, hotkey, shares_encoding, shares_raw, shares_exponent, "
                    "shares_source, denom_encoding, denom_raw, denom_exponent, denom_source, "
                    "total_hotkey_alpha_rao) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.DatabaseError as exc:
            raise StoreRejected(
                f"the store refused the holding for {coldkey} at block {block}: {exc}"
            ) from exc
        return day

    @staticmethod
    def _position_rows(coldkey, block, positions):
        """Every position's components, checked and widened to exact text."""
        if isinstance(positions, (str, bytes)) or not isinstance(positions, (list, tuple)):
            raise NotAnAmount(
                f"the positions for {coldkey} at block {block} came in as {positions!r}, "
                f"which is not a list of positions."
            )
        return [Store._position_row(coldkey, block, p) for p in positions]

    @staticmethod
    def _position_row(coldkey, block, position):
        """One position's components, kept in the number system each came from."""
        if not isinstance(position, dict):
            raise NotAnAmount(f"a position came in as {position!r}, which is not a position.")
        for field in ("hotkey", "shares", "denominator", "total_hotkey_alpha_rao"):
            if field not in position:
                raise NotAnAmount(
                    f"a position for {coldkey} at block {block} has no {field}. A missing "
                    f"field is refused here rather than raised from somewhere deeper, "
                    f"because an ordinary error on this path is one a retry loop swallows."
                )
        hotkey = _identifier(position["hotkey"], "hotkey")
        shares = _a_share(position["shares"], f"shares for {hotkey}", ("v1", "v2", "absent"))
        denominator = _a_share(position["denominator"], f"total shares for {hotkey}", ("v1", "v2"))
        return (
            coldkey,
            block,
            hotkey,
            shares.encoding,
            str(_wide_amount(shares.raw, f"shares for {hotkey}")),
            shares.exponent,
            shares.source,
            denominator.encoding,
            str(_wide_amount(denominator.raw, f"total shares for {hotkey}")),
            denominator.exponent,
            denominator.source,
            str(_wide_amount(position["total_hotkey_alpha_rao"], f"total alpha for {hotkey}")),
        )

    def locked_rao(self, coldkey, day):
        """The locked portion, or a refusal if nobody read one.

        Refuses rather than returning zero or None, because a caller handed a
        number cannot tell whether anybody looked, and that is the whole reason
        the not-read state exists.
        """
        for sample in self.holding_samples(coldkey, day):
            if not sample["split_was_read"]:
                raise SplitNotRead(
                    f"the holding samples for {coldkey} in day {day} record a total but "
                    f"no locked and available split, because the split is not read yet. "
                    f"The door reads the total and does not need this."
                )
        return sum(sample["locked_rao"] for sample in self.holding_samples(coldkey, day))

    def holding_samples(self, coldkey, day):
        """Every sample taken of this wallet in this day, in block order."""
        _identifier(coldkey, "wallet")
        _whole_amount(day, "day")
        self._require_sampled(day)
        samples = self._conn.execute(
            "SELECT block, total_rao, locked_rao, available_rao FROM holding_sample "
            "WHERE coldkey = ? AND day = ? ORDER BY block",
            (coldkey, day),
        ).fetchall()
        out = []
        for block, total, locked, available in samples:
            split_was_read = locked is not None
            positions = self._conn.execute(
                "SELECT hotkey, shares_encoding, shares_raw, shares_exponent, shares_source, "
                "denom_encoding, denom_raw, denom_exponent, denom_source, "
                "total_hotkey_alpha_rao FROM holding_position "
                "WHERE coldkey = ? AND block = ? ORDER BY hotkey",
                (coldkey, block),
            ).fetchall()
            block_hash = self._conn.execute(
                "SELECT block_hash FROM block_seen WHERE block = ?", (block,)
            ).fetchone()
            out.append(
                {
                    "block": block,
                    "block_hash": block_hash[0] if block_hash else None,
                    "total_rao": total,
                    "locked_rao": locked,
                    "available_rao": available,
                    "split_was_read": split_was_read,
                    "positions": [
                        {
                            "hotkey": h,
                            "shares": Share(encoding=se, raw=int(sr), exponent=sx, source=ss),
                            "denominator": Share(encoding=de, raw=int(dr), exponent=dx, source=ds),
                            "total_hotkey_alpha_rao": int(ta),
                        }
                        for h, se, sr, sx, ss, de, dr, dx, ds, ta in positions
                    ],
                }
            )
        return out

    def holding_by_hotkey(self, coldkey, day):
        """Each hotkey's own share of this wallet's holding, kept apart by seat.

        Added for #193: the door now gates one hotkey at a time (D-072), so a
        wallet with several seats needs each seat's own figure rather than the
        `holding_sample.total_rao` column, which is the coldkey-wide sum
        `holding_for_coldkey` produced under the superseded rule.

        **Recomputed on every read, never a stored column.** `holding_position`
        keeps each hotkey's raw share, denominator and pool components in the
        number system the chain stored them in (`record_holding_sample`'s own
        reason for existing), not a derived alpha figure, so there is no
        per-hotkey total sitting in the schema to go stale or to migrate: the
        components already stored are run back through the same arithmetic
        `holding_for_coldkey` always used, via `positions_for_coldkey`, which
        #193 split out of it for exactly this reuse. A stored column would
        duplicate an answer that is already one cheap local computation away
        from components this store commits to keeping anyway.

        **The lowest figure across the day's SAMPLES, never the sum, and never
        continuous coverage.** This is the same rule the door itself uses on a
        coldkey's whole position (`noah/core/door.py:holding_for_day`, ruled
        2026-08-23: the lowest point in the day, because that is the one
        reading that cannot be gamed by topping up right before the sample).
        A wallet sampled several times in a day is one wallet read several
        times, not several wallets, so summing multiplied a stable balance by
        however many times the stride happened to land on it: a 100-rao
        balance sampled three times summed to 300, which could flip whether
        the door opens on a sampling accident rather than on anything the
        wallet did. Every block this coldkey was sampled at counts toward the
        minimum for EVERY hotkey, including a block where a given hotkey is
        absent from that block's positions: absence there is a reading of
        zero for that hotkey at that block, the same reasoning
        `record_holding_sample` already rests its coherence checks on (one
        block's positions are this coldkey's whole breakdown), not a gap, so
        it can be the day's low point the same as any other block. Ties
        broken by the earliest block, matching the door's own tie rule, so
        two readers of the same rows agree.

        **Named, honest limitation: this is sampled coverage, not continuous
        coverage.** The minimum taken here is the minimum of the SAMPLED
        blocks this day -- ordinarily on an hourly-or-coarser stride
        (`mark_day_sampled`'s own `stride` argument) -- never a minimum over
        every block in the day. A holding that dips below a floor strictly
        BETWEEN two samples and recovers before the next one is invisible to
        this method: nothing was read at the moment of the dip, so nothing
        here can report it. This module does not claim otherwise anywhere in
        its own docstrings, and no code path here narrows that gap; doing so
        would need a finer sampling stride upstream (wherever
        `mark_day_sampled`/`record_holding_sample` are called from), which is
        out of this store's own scope. See
        `noah/deposits/tests/test_d143_corrections.py::TestSampledHoldingMinimumLimitation`
        for a constructed proof of exactly this gap.

        A hotkey that never appears in any of the day's samples is absent
        from the returned map. A caller who already knows this wallet had at
        least one sample this day (this method having returned rather than
        refused) reads that absence as a confirmed zero, never as unread: the
        same `StakingHotkeys[coldkey]` reasoning as before, now carried
        through a minimum rather than a sum.

        **Refuses when this wallet has no sample at all in this day**, even
        one sampled for other wallets. `holding_samples` itself answers that
        case with an empty list rather than a refusal, because it is a raw
        accessor and an honest empty list is the right answer to "what rows
        exist for this wallet". But this method's map cannot hand that same
        empty answer on unqualified: an empty map is also what a wallet reads
        when every hotkey it holds was sampled and confirmed to hold nothing,
        and those are different facts. Only one of them may become a
        published zero, so the wallet-never-sampled case is raised here
        rather than folded into the same empty map.
        """
        samples = self.holding_samples(coldkey, day)
        if not samples:
            raise ColdkeyNotSampled(
                f"{coldkey} has no holding sample in day {day}, though the day itself was "
                f"sampled for other wallets. A wallet with no reading is not a wallet holding "
                f"nothing, and answering zero here is how the two become indistinguishable."
            )

        by_block = []
        for sample in samples:
            entries = [
                {
                    "hotkey": position["hotkey"],
                    "share": position["shares"],
                    "denominator": position["denominator"],
                    "pool_alpha_rao": position["total_hotkey_alpha_rao"],
                }
                for position in sample["positions"]
            ]
            at_block = {
                position.hotkey: position.alpha_rao for position in positions_for_coldkey(entries)
            }
            by_block.append((sample["block"], at_block))

        hotkeys = set()
        for _, at_block in by_block:
            hotkeys.update(at_block)

        totals = {}
        for hotkey in hotkeys:
            # The lowest reading across every sampled block, including blocks
            # where this hotkey is absent from that block's positions: absence
            # there is a reading of zero, not a gap. Ties go to the earliest
            # block, matching door.py's own rule, so two readers of the same
            # rows always agree.
            _, lowest = min(
                ((block, at_block.get(hotkey, 0)) for block, at_block in by_block),
                key=lambda reading: (reading[1], reading[0]),
            )
            totals[hotkey] = lowest
        return totals

    def pay_recorded(self, coldkey, day):
        """Whether any pay row exists for this wallet in this day.

        A different question from "what were they paid", and answerable without
        the day having been scanned: this asks what the store holds, not what
        the chain did. Used where a pay row and another record disagree, so that
        the disagreement can be seen rather than resolved by whichever was
        consulted first.
        """
        _identifier(coldkey, "wallet")
        _whole_amount(day, "day")
        row = self._conn.execute(
            "SELECT 1 FROM attributed_pay WHERE coldkey = ? AND day = ? AND amount_rao > 0",
            (coldkey, day),
        ).fetchone()
        return row is not None

    def wallets_seen(self):
        """Every wallet this store has observed doing anything, in one order.

        Sorted rather than left in whatever order the tables produce, because
        two implementations have to agree byte for byte on what comes out of
        this and nothing may depend on a query plan.
        """
        wallets = set()
        for table in ("attributed_pay", "noah_deposit", "foreign_deposit"):
            wallets.update(
                row[0] for row in self._conn.execute(f"SELECT DISTINCT coldkey FROM {table}")
            )
        return sorted(wallets)

    def day_was_scanned(self, day):
        """Whether this day has been read to the end.

        A question, not a refusal, because the runner asks it to decide whether
        to resume. Reading a day's contents still refuses when it was never
        scanned; this is the check that stops that happening.
        """
        _whole_amount(day, "day")
        row = self._conn.execute("SELECT 1 FROM scanned_day WHERE day = ? " + _NOT_STALE, (day,)).fetchone()
        return row is not None

    def scanned_range(self, day):
        """The exact ``(from_block, to_block)`` recorded for this day's own
        scan, or ``None`` if it was never scanned.

        Reads back precisely what ``mark_day_scanned`` wrote -- the two
        integers passed to that call, never a derived, rounded, or estimated
        value. Added for ``neurons/miner.py``'s own day-boundary search
        (issue #478): finding day N's own first block needs a block already
        KNOWN to fall in day N-1, and the most recent, cheapest such anchor
        this store can hand back is day (N-1)'s own last scanned block --
        exactly the ``to_block`` this returns. ``day_was_scanned`` already
        answers "was it scanned" as a plain yes/no for ``run_day``'s own
        resume check; this is the one caller that needs the range itself,
        not merely the fact that scanning happened.
        """
        _whole_amount(day, "day")
        row = self._conn.execute(
            "SELECT from_block, to_block FROM scanned_day WHERE day = ? " + _NOT_STALE, (day,)
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    def scanned_days(self):
        """Every recorded scan as ``(day, from_block, to_block)``, oldest day
        first. Exactly what ``mark_day_scanned`` was given; nothing derived.

        The read a history check needs to see the whole record at once, rather
        than asking about one day it already has in hand (issue #239).
        """
        rows = self._conn.execute(
            "SELECT day, from_block, to_block FROM scanned_day WHERE 1 = 1 " + _NOT_STALE + " ORDER BY day"
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def registration_block_day(self, block):
        """The day this registration block fell in, as recorded when it was
        first read, or ``None`` if it was never recorded.

        A seat's registration block does not move, so its day is a fact to
        record once. Asking the chain again for that block's timestamp fails
        once the block is older than a pruned node's state (issue #237).
        """
        row = self._conn.execute(
            "SELECT day FROM registration_day WHERE block = ?", (_positive(block, "block"),)
        ).fetchone()
        return None if row is None else row[0]

    def record_registration_block_day(self, block, block_timestamp):
        """Record the day of a registration block, once. Returns the day.

        Recording the same block again with the same time is a no-op. A
        different day for a block already recorded is refused, and the record
        is left as it was.
        """
        block = _positive(block, "block")
        day = self.day_of(block_timestamp)
        seen = self.registration_block_day(block)
        if seen is not None:
            if seen != day:
                raise RegistrationDayChanged(
                    f"registration block {block} was recorded as falling in day {seen} and "
                    f"has now been read as falling in day {day}. A block does not move, so "
                    f"one of these reads is wrong and neither is used."
                )
            return seen
        self._write(
            "INSERT INTO registration_day (block, day) VALUES (?, ?)",
            (block, day),
            f"the registration day of block {block}",
        )
        return day

    def earliest_registration_day(self):
        """The earliest day any recorded registration block fell in, or ``None``
        if no registration block was ever recorded.

        One of the two facts the store itself holds about when the subnet's
        history begins (the other is the emission start). A history check uses
        it as the first day the store must hold, so a store missing its early
        days is refused rather than read as if the miners began later.
        """
        row = self._conn.execute("SELECT MIN(day) FROM registration_day").fetchone()
        return None if row is None or row[0] is None else row[0]

    def registration_block_on_day(self, day):
        """A real, already-recorded registration block that fell on ``day``,
        or ``None`` if this store has never recorded one there (issue #309).

        Exists so a caller recovering a day this store never scanned can
        resume a real chain read from a block it already knows falls inside
        that day, rather than guessing one or asking the chain to search for
        it: any registration block recorded for ``day`` is such a block by
        construction (``record_registration_block_day`` computed ``day``
        from this exact block's own timestamp, the same real read
        ``noah.chain.dayblocks.day_block_range`` would make of it again).
        The lowest recorded block is returned when more than one seat
        registered the same day, for a deterministic answer; any of them
        resolves to the identical day range.
        """
        _whole_amount(day, "day")
        row = self._conn.execute(
            "SELECT MIN(block) FROM registration_day WHERE day = ?", (day,)
        ).fetchone()
        return None if row is None or row[0] is None else row[0]

    def scanned_day_containing(self, block):
        """The ``(day, from_block, to_block)`` of the scanned day whose recorded
        range holds ``block``, or ``None`` if no scanned day does.

        A restarted or replacement validator uses this to find days it has
        already read without asking the chain anything. A node that keeps only
        recent state cannot answer questions about old blocks, so the store's
        own record is the only source for them (issue #237).
        """
        row = self._conn.execute(
            "SELECT day, from_block, to_block FROM scanned_day "
            "WHERE from_block <= ? AND ? <= to_block " + _NOT_STALE + " ORDER BY day LIMIT 1",
            (int(block), int(block)),
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    def latest_scanned_day(self):
        """The highest ``day`` this store has ever finished scanning, or
        ``None`` if no day has ever been scanned at all.

        Exists to close a real honesty gap for callers of
        :meth:`cumulative_noah_deposits_for_hotkey` (and
        :meth:`cumulative_noah_deposits`): that method is a raw SQL SUM over
        whatever ``noah_deposit`` rows happen to exist, with no check that
        any day was actually scanned. A fresh store, or one that has fallen
        behind, would answer ``0`` for every single wallet -- indistinguishable
        from a wallet the store genuinely observed making zero deposits.
        A caller that needs to tell "nothing has been scanned yet" apart
        from "scanned, and confirmed zero" (in particular, a read-only
        remote interface reporting a miner's entry-deposit recognition to a
        party with no other way to check) must ask this first, and only
        trust a cumulative-sum answer once this returns something other
        than ``None``.

        Neither ``day_was_scanned`` nor ``scanned_range`` answers this:
        both take a specific day already in hand and say whether/how it was
        scanned; this is the one query that says how far scanning has
        actually reached, without the caller guessing a day to ask about.
        Gaps in ``scanned_day`` are fine and expected (a day can be
        rescanned or skipped) -- this returns the maximum ``day`` value
        present, not proof every day up to it was scanned.
        """
        row = self._conn.execute("SELECT MAX(day) FROM scanned_day WHERE 1 = 1 " + _NOT_STALE).fetchone()
        return row[0] if row is not None else None
