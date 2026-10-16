"""Reclassify schema-17 receipts from complete, independently agreed block reads.

This recovery changes receipts and their migration readiness markers only. It does
not rebuild pay, holdings, registrations, or scoring. Partial failures leave the
original range pending; a retry rereads that entire range.
"""

import sqlite3
from pathlib import Path

from ..deposits.live_chain_source import live_chain_source_from_environment
from .agreeing import AgreeingChain
from .live import LiveRunnerChain
from .deposits import deposits_in_block
from .runner import _ParentBlockOwnerSource, _block_time, _positive, _record
from .sources import Refusal
from .store import _validate_store_schema


class ForeignRescanRefused(Refusal):
    """The original receipt history could not be completely proved."""


def build_dual_sourced_day_chain(*, netuid, author_env_var="NOAH_AUTHOR_WS",
                                 reader_env_var="NOAH_READER_WS"):
    """Build independent readers using the public, endpoint-validating connector."""
    sources = {}
    try:
        for role, variable in (("author", author_env_var), ("reader", reader_env_var)):
            source = live_chain_source_from_environment(variable)
            sources[role] = LiveRunnerChain(source._connection, netuid=netuid,
                                            endpoint=source.endpoint)
        return AgreeingChain(sources, {role: source.endpoint for role, source in sources.items()})
    except BaseException:
        for source in sources.values():
            try:
                source._connection.close()
            except Exception:
                pass
        raise


def plan_foreign_rescans(path):
    """Read original pending ranges without opening a writer or migrating schema 17."""
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        _validate_store_schema(conn, version)
        if version == 17:
            return list(conn.execute(
                "SELECT d.day,s.from_block,s.to_block FROM ("
                "SELECT day FROM scanned_day UNION SELECT day FROM noah_deposit "
                "UNION SELECT day FROM foreign_deposit) d "
                "LEFT JOIN scanned_day s ON s.day=d.day ORDER BY d.day"))
        return list(conn.execute(
            "SELECT d.day,s.from_block,s.to_block FROM day_needs_rescan d "
            "LEFT JOIN scanned_day s ON s.day=d.day ORDER BY d.day"))


def verify_registration_floor(chain, *, netuid, floor_block):
    """Check the operator's floor against both readers at one finalized hash."""
    head = chain.head_block()
    pin = chain.block_hash(head)
    for source in chain._sources.values():
        raw = source._ask(
            "the current subnet registration floor",
            lambda: source._connection.query(
                "SubtensorModule", "NetworkRegisteredAt", [netuid], block_hash=pin))
        floor = raw.value if hasattr(raw, "value") else raw
        if type(floor) is not int or floor != floor_block:
            raise ForeignRescanRefused("configured floor does not match both readers' current subnet registration")
    if chain.block_hash(head) != pin:
        raise ForeignRescanRefused("finalized registration-floor block changed during verification")


def rescan_foreign_receipts(store, chain, *, day, from_block, to_block,
                            ark_coldkey, designated_validator_hotkey, netuid,
                            floor_block, progress=None):
    """Read every original block, reconcile every old receipt, then clear one marker.

    The original prefix below the current subnet's registration is read and checked
    but cannot create credit. An existing receipt below that floor refuses before
    any RPC or write; use the established pre-floor investigation/remediation first.
    An already resolved day is a no-op. Missing original bounds are not guessed.
    """
    if not isinstance(chain, AgreeingChain):
        raise ForeignRescanRefused("receipt recovery requires two independent agreeing readers")
    _positive(day + 1, "day plus one")
    _positive(from_block, "first block")
    _positive(to_block, "last block")
    _positive(floor_block, "subnet registration floor")
    _positive(netuid, "scored subnet")
    if not ark_coldkey or not designated_validator_hotkey:
        raise ForeignRescanRefused("Ark address and designated validator are required")
    pending = {row[0]: row[1:] for row in store.pending_foreign_rescans()}
    if day not in pending:
        return 0
    store.require_day_rescan_range(day, from_block, to_block)
    if pending[day] != (from_block, to_block):
        raise ForeignRescanRefused("receipt recovery requires the exact original scanned range")
    if from_block > to_block:
        raise ForeignRescanRefused("original scanned range is reversed")
    for table in ("noah_deposit", "foreign_deposit"):
        if store._conn.execute(
            f"SELECT 1 FROM {table} WHERE day=? AND block<? LIMIT 1", (day, floor_block)
        ).fetchone():
            raise ForeignRescanRefused(
                f"day {day} has an existing {table} receipt below subnet floor {floor_block}; "
                "investigate pre-floor history before receipt recovery")
    if to_block < floor_block:
        raise ForeignRescanRefused("original range lies entirely before the current subnet registration")
    # Empty receipt sets also need the normal validator's historical Ark identity gate.
    from ..core.chain_adapter import _confirm_ark_identity
    _confirm_ark_identity(chain, netuid=netuid, ark_coldkey=ark_coldkey,
                          from_block=max(from_block, floor_block), to_block=to_block)
    head = chain.head_block()
    if to_block > head:
        raise ForeignRescanRefused("original scanned range extends beyond the finalized head")
    observed = set()
    previous = None
    last_time = None
    if progress:
        progress(day=day, from_block=from_block, to_block=to_block, outcome="scanning")
    for block in range(from_block, to_block + 1):
        block_hash = chain.block_hash(block)
        events = chain.events(block)
        timestamp = _block_time(chain, block, last_time)
        last_time = (block, timestamp)
        if store.day_of(timestamp) != day:
            raise ForeignRescanRefused(f"block {block} does not belong to original day {day}")
        deposits = []
        if block >= floor_block:
            extrinsics = chain.extrinsics(block)
            parent_hash = previous[1] if previous else chain.block_hash(block - 1)
            deposits = list(deposits_in_block(
                events, ark_coldkey=ark_coldkey, block=block, block_hash=block_hash,
                designated_validator_hotkey=designated_validator_hotkey,
                extrinsics=extrinsics, parent_block_hash=parent_hash,
                owner_source=_ParentBlockOwnerSource(chain, block - 1)))
        # Empty and below-floor blocks receive the same final hash check as a receipt.
        if chain.block_hash(block) != block_hash:
            raise ForeignRescanRefused(f"block {block} changed during receipt recovery")
        for deposit in deposits:
            _record(store, deposit, timestamp, netuid)
            observed.add((deposit.block, deposit.event_ordinal))
        previous = (block, block_hash)
        if progress and (block - from_block + 1) % 256 == 0:
            progress(day=day, height=block, from_block=from_block, to_block=to_block,
                     outcome="scanning")
    store.mark_day_scanned(day, from_block, to_block, observed_receipts=observed)
    if progress:
        progress(day=day, height=to_block, from_block=from_block, to_block=to_block,
                 outcome="observed", receipts=len(observed))
    return len(observed)
