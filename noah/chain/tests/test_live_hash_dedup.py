"""A hash read directly after a source's own check read of the same block is the same read.

Every other hash read is fresh: no cache by height, nothing shared between
sources, and deliberate re-reads (new questions) start with a fresh check read.
"""

import pytest

from noah.chain import live
from noah.chain.agreeing import AgreeingChain
from noah.chain.live import LiveRunnerChain
from noah.chain.runner import run_day
from noah.chain.sources import SourceDisagreement, SourceLied
from noah.chain.store import Store


def _hash(n, salt=0):
    return "0x" + f"{n:062x}" + f"{salt:02x}"


class _Connection:
    """Counts every call by method name; answers hashes, heads and a quiet block."""

    def __init__(self, finalized=100, salt=0, replies=None):
        self.finalized = finalized
        self.salt = salt
        self.replies = replies or {}
        self.counts = {}
        self.hash_calls = []

    def _count(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1

    def get_chain_finalised_head(self):
        self._count("get_chain_finalised_head")
        return "head-hash"

    def get_block_number(self, block_hash):
        self._count("get_block_number")
        return self.finalized

    def get_block_hash(self, block):
        self._count("get_block_hash")
        self.hash_calls.append(block)
        reply = self.replies.get(block)
        if isinstance(reply, list):  # scripted: pop the next reply
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if block in self.replies:
            return reply
        return _hash(block, self.salt)

    def get_events(self, block_hash=None):
        self._count("get_events")
        return [
            {
                "phase": "Initialization",
                "extrinsic_idx": None,
                "event": {"event_index": "0000", "module_id": "System", "event_id": "X"},
                "event_index": 0,
                "module_id": "System",
                "event_id": "X",
                "attributes": {},
                "topics": [],
            }
        ]

    def get_block(self, block_hash=None):
        self._count("get_block")
        return {"extrinsics": [{"call": {"call_module": "Timestamp"}}]}

    def query(self, module, name, params, block_hash=None):
        self._count(f"query:{module}.{name}")
        if (module, name) == ("Timestamp", "Now"):
            return 1_787_000_000_000
        raise AssertionError(f"unexpected read {module}.{name}")


def _chain(connection):
    return LiveRunnerChain(connection, netuid=2, endpoint="wss://archive.example")


def _dual(a, b):
    return AgreeingChain(
        sources={"a": _chain(a), "b": _chain(b)},
        endpoints={"a": "wss://a.example", "b": "wss://b.example"},
    )


# ---- reads per quiet block, through the real AgreeingChain and run_day -----

_NETUID = 35
_ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _run_quiet_blocks(db, a, b, first, last):
    reader = _dual(a, b)
    store = Store.create(db, day_anchor_ts=1_787_000_000)
    run_day(
        store,
        reader,
        day=0,
        from_block=first,
        to_block=last,
        ark_coldkey=_ARK,
        designated_validator_hotkey=_VALIDATOR,
        sources={"a": "wss://a.example", "b": "wss://b.example"},
        netuid=_NETUID,
        holding_stride=None,
        read_collateral=False,
        attribute_deposits=True,
    )
    return reader


def _total(connection):
    return sum(connection.counts.values())


def test_a_canary_then_block_hash_is_one_connection_read():
    c = _Connection()
    chain = _chain(c)
    chain.canary(50)
    assert chain.block_hash(50) == _hash(50)
    assert c.hash_calls == [50]


def test_block_hash_twice_without_a_canary_is_two_reads():
    c = _Connection()
    chain = _chain(c)
    chain.block_hash(50)
    chain.block_hash(50)
    assert c.hash_calls == [50, 50]


def test_canary_then_a_different_block_does_not_use_the_memo():
    c = _Connection()
    chain = _chain(c)
    chain.canary(50)
    chain.block_hash(51)
    assert c.hash_calls == [50, 51]


def test_another_question_in_between_invalidates_the_memo():
    c = _Connection()
    chain = _chain(c)
    chain.canary(50)
    chain.head_block()
    chain.block_hash(50)
    assert c.hash_calls == [50, 50]


def test_events_in_between_invalidates_the_memo_and_events_reads_its_own_hash():
    c = _Connection()
    chain = _chain(c)
    chain.canary(50)
    chain.events(50)  # consumes the memo for its own hash read
    chain.block_hash(50)
    assert c.hash_calls == [50, 50]


def test_the_memo_is_one_shot():
    c = _Connection()
    chain = _chain(c)
    chain.canary(50)
    chain.block_hash(50)
    chain.block_hash(50)
    assert c.hash_calls == [50, 50]


def test_a_second_canary_always_reads_fresh():
    c = _Connection()
    chain = _chain(c)
    chain.canary(50)
    chain.canary(50)
    assert c.hash_calls == [50, 50]


def test_a_failed_canary_read_leaves_no_memo():
    c = _Connection(replies={50: [RuntimeError("hung up"), _hash(50)]})
    chain = _chain(c)
    chain.canary(49)
    with pytest.raises(live.ChainUnavailable):
        chain.canary(50)
    chain.block_hash(50)
    assert c.hash_calls == [49, 50, 50]


def test_two_instances_never_share_the_memo():
    a, b = _Connection(salt=1), _Connection(salt=2)
    ca, cb = _chain(a), _chain(b)
    ca.canary(50)
    assert cb.block_hash(50) == _hash(50, 2)
    assert b.hash_calls == [50]
    assert ca.block_hash(50) == _hash(50, 1)
    assert a.hash_calls == [50]


def test_dual_source_disagreement_is_still_refused_and_each_source_is_read_once():
    a, b = _Connection(salt=1), _Connection(salt=2)
    reader = _dual(a, b)
    with pytest.raises(SourceDisagreement):
        reader.block_hash(50)
    assert a.hash_calls == [50]
    assert b.hash_calls == [50]


def test_dual_source_malformed_canary_is_still_refused():
    a, b = _Connection(), _Connection(replies={50: "0x" + "00" * 32})
    reader = _dual(a, b)
    with pytest.raises(SourceLied):
        reader.block_hash(50)


def test_a_late_recheck_through_agreeing_chain_reads_fresh_and_sees_a_change():
    a, b = _Connection(), _Connection()
    reader = _dual(a, b)
    assert reader.block_hash(50) == _hash(50)
    assert a.hash_calls == [50] and b.hash_calls == [50]
    # The chain now answers differently on both sources (a reorg): the recheck
    # must see the new value, not the earlier one.
    a.replies[50] = b.replies[50] = _hash(50, 9)
    assert reader.block_hash(50) == _hash(50, 9)
    assert a.hash_calls == [50, 50] and b.hash_calls == [50, 50]


def test_a_recheck_where_one_source_changed_is_refused():
    a, b = _Connection(), _Connection()
    reader = _dual(a, b)
    reader.block_hash(50)
    a.replies[50] = _hash(50, 9)
    with pytest.raises(SourceDisagreement):
        reader.block_hash(50)
    assert a.hash_calls == [50, 50] and b.hash_calls == [50, 50]


def _hash_reads_per_block(db, blocks):
    a, b = _Connection(finalized=1000), _Connection(finalized=1000)
    _run_quiet_blocks(db, a, b, 100, 100 + blocks - 1)
    assert a.counts == b.counts
    return _total(a) / blocks, a


def test_reads_per_quiet_block_per_source_drop(tmp_path, monkeypatch):
    blocks = 20
    after, a = _hash_reads_per_block(tmp_path / "after.db", blocks)
    print(f"AFTER: per source reads per block = {after:.2f}; counts={a.counts}")

    def canary_without_dedup(self, block):
        return live.hash_canary(self._read_block_hash(block))

    monkeypatch.setattr(LiveRunnerChain, "canary", canary_without_dedup)
    before, a2 = _hash_reads_per_block(tmp_path / "before.db", blocks)
    print(f"BEFORE: per source reads per block = {before:.2f}; counts={a2.counts}")
    assert after < before
    assert a2.counts["get_block_hash"] > a.counts["get_block_hash"]
