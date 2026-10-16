"""A null storage reply that the client library turns into a default value is no answer.

A public archive endpoint can answer a storage read with a null that the client
library turns into a default value. For a block that exists, some defaults are
impossible (a zero time, no events, no extrinsics), so ``LiveRunnerChain``
re-asks with the rate-limit waits and then refuses. The audit table of every
read kind and its guard is the comment block above ``_NULL_REPLY_WAITS`` in
``noah/chain/live.py``. Reads whose default can be a real answer are not
guarded here.
"""

import logging

import pytest

from noah.chain import live
from noah.chain.agreeing import AgreeingChain
from noah.chain.live import ChainUnavailable, LiveRunnerChain
from noah.chain.runner import run_day
from noah.chain.store import Store

NOW_MS = 1_787_000_000_000
EVENT = {
    "phase": "Initialization",
    "extrinsic_idx": None,
    "event": {"event_index": "0000", "module_id": "System", "event_id": "X"},
    "event_index": 0,
    "module_id": "System",
    "event_id": "X",
    "attributes": {},
    "topics": [],
}


def _hash(n):
    return "0x" + f"{n:064x}"


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    waits = []
    monkeypatch.setattr(live, "_pause", waits.append)
    monkeypatch.setattr(live, "_guard_logged", set())
    return waits


class _Connection:
    """Scripted replies per kind: a list is consumed, its last item then repeats."""

    def __init__(self, times=(NOW_MS,), events=(None,), extrinsics=(None,)):
        self.times = list(times)
        self.events = list(events)
        self.extrinsics = list(extrinsics)
        self.calls = {"time": 0, "events": 0, "extrinsics": 0}

    @staticmethod
    def _next(script):
        return script.pop(0) if len(script) > 1 else script[0]

    def get_chain_finalised_head(self):
        return "head"

    def get_block_number(self, block_hash):
        return 100

    def get_block_hash(self, block):
        return _hash(block)

    def query(self, module, name, params, block_hash=None):
        assert (module, name) == ("Timestamp", "Now")
        self.calls["time"] += 1
        return self._next(self.times)

    def get_events(self, block_hash=None):
        self.calls["events"] += 1
        reply = self._next(self.events)
        return [EVENT] if reply is None else reply

    def get_block(self, block_hash=None):
        self.calls["extrinsics"] += 1
        reply = self._next(self.extrinsics)
        return {"extrinsics": [{"call": "timestamp.set"}] if reply is None else reply}


def _chain(connection):
    return LiveRunnerChain(connection, netuid=2, endpoint="wss://archive.example")


# ---- timestamp -------------------------------------------------------------


def test_timestamp_zero_then_real_value_is_returned_after_one_pause(pauses):
    c = _Connection(times=[0, NOW_MS])
    assert _chain(c).block_timestamp(50) == NOW_MS // 1000
    assert c.calls["time"] == 2
    assert pauses == [live._NULL_REPLY_WAITS[0]]


def test_timestamp_always_zero_is_refused_naming_block_kind_and_reply(pauses):
    c = _Connection(times=[0])
    with pytest.raises(ChainUnavailable) as caught:
        _chain(c).block_timestamp(9151585)
    message = str(caught.value)
    assert "9151585" in message and "timestamp" in message and "0" in message
    assert c.calls["time"] == len(live._NULL_REPLY_WAITS) + 1
    assert pauses == list(live._NULL_REPLY_WAITS)


def test_timestamp_zero_at_genesis_is_accepted(pauses):
    c = _Connection(times=[0])
    assert _chain(c).block_timestamp(0) == 0
    assert c.calls["time"] == 1 and pauses == []


def test_timestamp_none_for_a_later_block_is_no_answer(pauses):
    c = _Connection(times=[None, NOW_MS])
    assert _chain(c).block_timestamp(5) == NOW_MS // 1000


def test_a_real_timestamp_is_read_once_without_pause(pauses):
    c = _Connection()
    assert _chain(c).block_timestamp(50) == NOW_MS // 1000
    assert c.calls["time"] == 1 and pauses == []


# ---- events and extrinsics -------------------------------------------------


def test_events_empty_then_real_is_returned(pauses):
    c = _Connection(events=[[], [EVENT]])
    assert len(_chain(c).events(50)) == 1
    assert c.calls["events"] == 2


def test_events_always_empty_is_refused(pauses):
    c = _Connection(events=[[]])
    with pytest.raises(ChainUnavailable, match="block 50"):
        _chain(c).events(50)
    assert c.calls["events"] == live._EVENTS_ATTEMPTS


def test_extrinsics_empty_then_real_is_returned(pauses):
    c = _Connection(extrinsics=[[], [{"call": "x"}]])
    assert _chain(c).extrinsics(50) == [{"call": "x"}]
    assert c.calls["extrinsics"] == 2
    assert pauses == [live._NULL_REPLY_WAITS[0]]


def test_extrinsics_always_empty_is_refused_naming_block_and_kind(pauses):
    c = _Connection(extrinsics=[[]])
    with pytest.raises(ChainUnavailable) as caught:
        _chain(c).extrinsics(50)
    assert "extrinsics" in str(caught.value) and "block 50" in str(caught.value)
    assert c.calls["extrinsics"] == len(live._NULL_REPLY_WAITS) + 1


def test_extrinsics_empty_at_genesis_is_accepted(pauses):
    c = _Connection(extrinsics=[[]])
    assert _chain(c).extrinsics(0) == []
    assert c.calls["extrinsics"] == 1


# ---- the log line ----------------------------------------------------------


def test_the_log_line_fires_once_per_kind_and_names_no_endpoint(pauses, caplog):
    caplog.set_level(logging.WARNING, logger=live.__name__)
    for _ in range(2):
        _chain(_Connection(times=[0, NOW_MS])).block_timestamp(50)
    _chain(_Connection(extrinsics=[[], [{"call": "x"}]])).extrinsics(50)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("read guard")]
    assert lines == [
        "read guard: timestamp at block 50 returned 0; re-asking",
        "read guard: extrinsics at block 50 returned []; re-asking",
    ]
    assert not any("archive.example" in line for line in lines)


def test_the_log_line_fires_for_events_once(pauses, caplog):
    caplog.set_level(logging.WARNING, logger=live.__name__)
    for _ in range(2):
        _chain(_Connection(events=[[], [EVENT]])).events(50)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("read guard")]
    assert lines == ["read guard: events at block 50 returned []; re-asking"]


# ---- the full path: two real chains, a real AgreeingChain, run_day ----------

_ARK = "5HK2LZRrvj581VjyxuFxb6ehFZoNi5PqAg3Y2HsmYPhGYYXr"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _scan(db, reader_times):
    a = _Connection()
    b = _Connection(times=reader_times)
    chain = AgreeingChain(
        sources={"a": _chain(a), "b": _chain(b)},
        endpoints={"a": "wss://a.example", "b": "wss://b.example"},
    )
    store = Store.create(db, day_anchor_ts=1_787_000_000)
    run_day(
        store,
        chain,
        day=0,
        from_block=10,
        to_block=13,
        ark_coldkey=_ARK,
        designated_validator_hotkey=_VALIDATOR,
        sources={"a": "wss://a.example", "b": "wss://b.example"},
        netuid=35,
        holding_stride=None,
        read_collateral=False,
    )
    return a, b


def test_a_single_zero_from_the_reader_does_not_stop_the_scan(tmp_path, pauses):
    a, b = _scan(tmp_path / "s.db", [0, NOW_MS])
    assert b.calls["time"] > 1 and pauses[:1] == [live._NULL_REPLY_WAITS[0]]


def test_a_reader_that_always_answers_zero_stops_the_scan(tmp_path, pauses):
    with pytest.raises(ChainUnavailable, match="timestamp"):
        _scan(tmp_path / "s.db", [0])


def test_the_re_ask_schedule_is_the_bounded_rate_limit_schedule(pauses):
    assert live._NULL_REPLY_WAITS == (5, 10, 20, 40, 60, 60)
    c = _Connection(times=[0])
    with pytest.raises(ChainUnavailable):
        _chain(c).block_timestamp(50)
    assert c.calls["time"] == 7 and pauses == [5, 10, 20, 40, 60, 60]
