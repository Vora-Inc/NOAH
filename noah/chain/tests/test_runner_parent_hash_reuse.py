"""The parent hash of a block is the previous block's hash, already read and checked in this scan.

With `attribute_deposits=True`, `run_day` needs the previous block's hash only
to name which state the owner read is pinned to. Inside one scan that hash was
read at the top of the previous iteration and re-read at its end, so it is
reused; the first block of a scan (or any gap) still reads it.
"""

import pytest

from noah.chain import runner
from noah.chain.runner import QuietBlockReorged
from noah.chain.tests.test_runner import (
    _MINER_HOTKEY,
    GIVER,
    Chain,
    _a_batch_all_deposit,
    _batch_all_extrinsics,
    a_store,
    run,
)

FIRST = 8_800_000
LAST = 8_800_004


def _hash_reads(chain, block):
    return sum(1 for entry in chain.asked if entry == ("hash", block))


def _go(tmp_path, chain, **kwargs):
    kwargs.setdefault("attribute_deposits", True)
    kwargs.setdefault("holding_stride", None)
    kwargs.setdefault("first", FIRST)
    kwargs.setdefault("last", LAST)
    return run(a_store(tmp_path), chain, **kwargs)


def test_parent_is_read_only_for_the_first_block_and_each_block_is_read_twice(tmp_path):
    chain = Chain()
    _go(tmp_path, chain)
    # The first block's parent is the one parent question; every other hash
    # is the first read plus the deliberate re-check at the end of its turn.
    assert _hash_reads(chain, FIRST - 1) == 1
    for block in range(FIRST, LAST + 1):
        assert _hash_reads(chain, block) == 2


def test_attribute_deposits_false_reads_are_unchanged(tmp_path):
    chain = Chain()
    _go(tmp_path, chain, attribute_deposits=False)
    assert _hash_reads(chain, FIRST - 1) == 0
    for block in range(FIRST, LAST + 1):
        assert _hash_reads(chain, block) == 2


def test_a_scan_starting_mid_range_reads_the_first_blocks_parent(tmp_path):
    chain = Chain()
    _go(tmp_path, chain, first=FIRST + 2)
    assert _hash_reads(chain, FIRST + 1) == 1
    assert _hash_reads(chain, FIRST) == 0


def test_the_parent_hash_given_to_deposits_is_the_previous_blocks_first_read(tmp_path, monkeypatch):
    seen = {}
    real = runner.deposits_in_block

    def spy(events, *, block, parent_block_hash=None, **kwargs):
        seen[block] = parent_block_hash
        return real(events, block=block, parent_block_hash=parent_block_hash, **kwargs)

    monkeypatch.setattr(runner, "deposits_in_block", spy)
    chain = Chain()
    _go(tmp_path, chain)
    for block in range(FIRST, LAST + 1):
        assert seen[block] == f"0x{block - 1:x}"


def test_a_deposit_block_owner_confirmation_gets_the_parent_hash(tmp_path):
    parent_hash = f"0x{FIRST:x}"
    chain = Chain(
        deposits={FIRST + 1: _a_batch_all_deposit()},
        extrinsics={FIRST + 1: _batch_all_extrinsics()},
        owners_at_hash={(_MINER_HOTKEY, parent_hash): GIVER},
    )
    store = a_store(tmp_path)
    run(store, chain, attribute_deposits=True, holding_stride=None, first=FIRST, last=LAST)
    assert ("owner_at", _MINER_HOTKEY, parent_hash) in chain.asked
    row = store._conn.execute(
        "SELECT origin_hotkey, owner_at_deposit, unattributed_reason FROM noah_deposit "
        "WHERE block = ?",
        (FIRST + 1,),
    ).fetchone()
    assert row == (_MINER_HOTKEY, GIVER, None)
    # The parent of the deposit block came from the first block's own reads.
    assert _hash_reads(chain, FIRST) == 2


def test_a_failed_recheck_stops_the_scan_and_nothing_is_carried(tmp_path):
    class Reorging(Chain):
        def block_hash(self, block):
            self.asked.append(("hash", block))
            if block == FIRST + 1 and _hash_reads(self, block) > 1:
                return "0xother"
            return f"0x{block:x}"

    chain = Reorging()
    store = a_store(tmp_path)
    with pytest.raises(QuietBlockReorged):
        run(store, chain, attribute_deposits=True, holding_stride=None, first=FIRST, last=LAST)
    assert ("events", FIRST + 2) not in chain.asked
    assert not store.day_was_scanned(0)
    # A new scan does not inherit the hash that failed: it reads its parent.
    fresh = Chain()
    run(
        a_store(tmp_path, "b.db"),
        fresh,
        attribute_deposits=True,
        holding_stride=None,
        first=FIRST + 1,
        last=FIRST + 1,
    )
    assert _hash_reads(fresh, FIRST) == 1
