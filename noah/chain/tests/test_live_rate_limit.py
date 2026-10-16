"""``LiveRunnerChain._ask`` waits and re-asks when the endpoint reports its
rate limit, and only then; every other failure is refused at once."""

import pytest

from noah.chain import live
from noah.chain.live import ChainUnavailable, LiveRunnerChain, _is_rate_limited
from noah.chain.sources import Refusal

LIMIT = "Historical work rate limit exceeded"
_HASH = "0x" + "ab" * 32
_EVENT = {
    "phase": "Initialization",
    "extrinsic_idx": None,
    "event": {"event_index": "050f", "module_id": "Balances", "event_id": "Issued"},
    "event_index": 5,
    "module_id": "Balances",
    "event_id": "Issued",
    "attributes": {"amount": 1},
    "topics": [],
}


@pytest.fixture
def pauses(monkeypatch):
    waits = []
    monkeypatch.setattr(live, "_pause", waits.append)
    return waits


def _chain(connection=None):
    return LiveRunnerChain(connection, netuid=2, endpoint="wss://archive.example")


def _flaky(failures, value="ok"):
    calls = []

    def question():
        calls.append(1)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return value

    return question, calls


def test_rate_limited_twice_then_succeeds(pauses):
    question, calls = _flaky([Exception(LIMIT), Exception(LIMIT)])
    assert _chain()._ask("a thing", question) == "ok"
    assert len(calls) == 3
    assert pauses == [5, 10]


def test_always_rate_limited_raises_after_seven_attempts(pauses):
    calls = []

    def question():
        calls.append(1)
        raise Exception(LIMIT)

    with pytest.raises(ChainUnavailable) as raised:
        _chain()._ask("the extrinsics of block 9", question)
    assert len(calls) == 7
    assert pauses == [5, 10, 20, 40, 60, 60]
    assert sum(pauses) == 195
    message = str(raised.value)
    assert message.startswith(
        "asking wss://archive.example for the extrinsics of block 9 failed: " + LIMIT
    )
    assert "retried 6 times over 195 seconds" in message


def test_other_failure_is_not_retried(pauses):
    question, calls = _flaky([ConnectionError("boom")])
    with pytest.raises(ChainUnavailable) as raised:
        _chain()._ask("a thing", question)
    assert len(calls) == 1
    assert pauses == []
    assert "retried" not in str(raised.value)


def test_refusal_passes_through_untouched(pauses):
    refusal = Refusal("sources disagree")
    question, calls = _flaky([refusal])
    with pytest.raises(Refusal) as raised:
        _chain()._ask("a thing", question)
    assert raised.value is refusal
    assert len(calls) == 1
    assert pauses == []


class _Connection:
    def __init__(self, replies):
        self.replies = list(replies)

    def get_block_hash(self, block):
        return _HASH

    def get_events(self, block_hash=None):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_events_retries_a_rate_limit_then_returns_the_list(pauses):
    event = dict(_EVENT)
    chain = _chain(_Connection([Exception(LIMIT), [event]]))
    result = chain.events(51696)
    assert len(result) == 1
    assert result[0]["ordinal_in_block"] == 0
    assert pauses == [5]


def test_events_empty_retry_and_rate_limit_retry_compose(pauses):
    event = dict(_EVENT)
    chain = _chain(_Connection([[], Exception(LIMIT), [event]]))
    assert len(chain.events(51696)) == 1
    assert pauses == [live._EVENTS_RETRY_PAUSE_SECONDS, 5]


def test_match_is_case_insensitive_and_narrow():
    assert _is_rate_limited(Exception("RATE LIMIT hit"))
    assert _is_rate_limited(Exception(LIMIT))
    assert not _is_rate_limited(Exception("rate of change too high"))


def test_rate_of_change_error_is_not_retried(pauses):
    question, calls = _flaky([Exception("rate of change too high")])
    with pytest.raises(ChainUnavailable):
        _chain()._ask("a thing", question)
    assert len(calls) == 1
    assert pauses == []
