"""``LiveRunnerChain``: the real, single-connection chain client ``run_day``
was proved against a stub of (``noah/chain/tests/test_runner.py``,
``test_runner_pay.py``), and that ``noah/chain/agreeing.py``'s
``AgreeingChain`` wraps two of to answer for real.

``canary`` specifically (issue #514): the one extra question ``AgreeingChain``
asks of every source, at every block, before trusting anything else it says.
It is a two-line composition -- ``hash_canary(self.block_hash(block))`` -- of
two things already exhaustively tested elsewhere, so this does not re-derive
either one's own test coverage:

* ``block_hash``'s own failure handling (``ChainUnavailable`` via ``_ask``) is
  covered generally by this class's shared ``_ask`` discipline.
* ``hash_canary``'s own parsing edge cases (a leading sign, digit-group
  separators, full-width Unicode digits, a bare int that must not look
  pre-validated, a bool) are ``noah/chain/sources.py``'s own logic, moved
  there from ``neurons/miner.py``'s ``_hash_canary`` in this same issue, and
  are proven by that function's OWN test coverage
  (``neurons/tests/test_miner_entry.py``'s ``test_hash_canary_*``, which
  still call it via ``miner._hash_canary`` -- a re-export of the identical
  function, see that module's own comment).

What is new here, and specific to ``canary`` as a method rather than as a
free function, is the COMPOSITION: does it actually call ``block_hash``
(not some other read), does a real hash decode and convert correctly through
it, does a malformed or all-zero answer still reach
``check_source_at_block`` as something that refuses (rather than being
quietly accepted or masked here), and does a failed underlying read surface
as the SAME ``ChainUnavailable`` every other read on this class already
raises -- a failure mode ``hash_canary`` itself, a plain function with no
connection to fail, never had.

**AS OF 2026-08-29: a codex adversarial challenge of this file's first
version found a real gap in it.** Two of the tests below proved a malformed
or all-zero ``canary()`` output fails ``check_source_at_block`` when that
function is called DIRECTLY with ``canary()``'s own return value -- which
proves ``canary``'s own contract, but not that ``AgreeingChain``'s actual
default caller (``canary or (lambda client, block: client.canary(block))``,
invoked as a bound method on a real ``LiveRunnerChain``) reaches it the
same way. The two ``test_agreeing_chain_...`` tests near the bottom of this
file close that: a real ``AgreeingChain``, wrapping two real
``LiveRunnerChain``s, read through its own public ``block_hash`` -- the
actual call shape a live run uses, not a hand-assembled shortcut to the
same guard.
"""

import pytest

from noah.chain.agreeing import AgreeingChain
from noah.chain.live import ChainUnavailable, LiveRunnerChain
from noah.chain.sources import SourceLied, check_source_at_block

_REAL_HASH = "0x" + "ab" * 32
_ZERO_HASH = "0x" + "00" * 32
_IMPLAUSIBLY_SHORT_HASH = "0x1"


class _Connection:
    """Enough of a raw substrate connection for ``block_hash`` (and, through
    it, ``canary``): ``get_block_hash`` only. Every other protocol method
    canary never touches is deliberately absent, so a canary implementation
    that reached for anything else would fail loudly here rather than
    silently pass by accident."""

    def __init__(self, hashes, fail=None):
        self._hashes = dict(hashes)
        self._fail = fail
        self.calls = []

    def get_block_hash(self, block):
        self.calls.append(block)
        if self._fail == "hash":
            raise RuntimeError("the node hung up")
        return self._hashes[block]


def _chain(hashes, fail=None):
    return LiveRunnerChain(_Connection(hashes, fail=fail), netuid=35, endpoint="wss://node.example")


def test_canary_reuses_block_hash_not_a_second_read():
    """The composition this method IS: block_hash's own answer, decoded --
    and, per [codex adversarial challenge, 2026-08-29], genuinely only ONE
    underlying connection read, not two: an implementation that happened to
    call get_block_hash twice per canary() invocation would still pass a
    value-only assertion, so the connection's own call count is checked
    directly."""
    connection = _Connection({10: _REAL_HASH})
    chain = LiveRunnerChain(connection, netuid=35, endpoint="wss://node.example")

    result = chain.canary(10)

    assert result == int(_REAL_HASH, 16)
    assert connection.calls == [10], "canary() must read the connection exactly once per call"


def test_canary_accepts_the_real_32_byte_shape_without_a_0x_prefix_too():
    bare = _REAL_HASH[2:]
    chain = _chain({10: bare})
    assert chain.canary(10) == int(bare, 16)


def test_canary_is_read_fresh_per_block():
    chain = _chain({10: _REAL_HASH, 11: "0x" + "cd" * 32})
    assert chain.canary(10) == int(_REAL_HASH, 16)
    assert chain.canary(11) == int("cd" * 32, 16)


def test_canary_never_raises_on_a_malformed_hash_itself():
    """Matches hash_canary's own contract: a value of the wrong shape is
    returned unchanged, not refused here -- it is check_source_at_block,
    called by AgreeingChain immediately afterward, that turns an undecoded
    value into a named refusal, not a second, weaker check built into this
    method."""
    chain = _chain({10: _IMPLAUSIBLY_SHORT_HASH})
    assert chain.canary(10) == _IMPLAUSIBLY_SHORT_HASH


def test_canary_on_a_malformed_hash_still_fails_check_source_at_block():
    chain = _chain({10: _IMPLAUSIBLY_SHORT_HASH})
    with pytest.raises(SourceLied, match="not a decoded integer"):
        check_source_at_block("live", block=10, canary_value=chain.canary(10))


def test_canary_on_an_all_zero_hash_decodes_to_a_real_zero():
    """The C-012 shape: an all-zero 32-byte hash is a genuinely VALID shape
    -- canary() decodes it rather than refusing it -- and it is
    check_source_at_block's own cannot-be-zero guard, not this method, that
    catches it. Exactly the failure a pruned single source was found
    returning live on 2026-08-20."""
    chain = _chain({10: _ZERO_HASH})
    assert chain.canary(10) == 0


def test_canary_on_an_all_zero_hash_still_fails_check_source_at_block():
    chain = _chain({10: _ZERO_HASH})
    with pytest.raises(SourceLied, match="cannot be the honest answer"):
        check_source_at_block("live", block=10, canary_value=chain.canary(10))


def test_agreeing_chain_refuses_a_malformed_hash_through_the_real_bound_canary():
    """[codex adversarial challenge, 2026-08-29] Not check_source_at_block
    called directly with canary()'s own output -- AgreeingChain's ACTUAL
    default canary caller, invoked as a bound method on a real
    LiveRunnerChain, wrapped by a real AgreeingChain, read through the
    whole real pipeline via its own public block_hash. Proves the wiring,
    not just canary()'s own contract in isolation."""
    good = _chain({10: _REAL_HASH})
    bad = _chain({10: _IMPLAUSIBLY_SHORT_HASH})
    reader = AgreeingChain(
        sources={"good": good, "bad": bad},
        endpoints={"good": "wss://good.example", "bad": "wss://bad.example"},
    )
    with pytest.raises(SourceLied, match="not a decoded integer"):
        reader.block_hash(10)


def test_agreeing_chain_refuses_an_all_zero_hash_through_the_real_bound_canary():
    """The C-012 case, through the same real pipeline."""
    zero = _chain({10: _ZERO_HASH})
    other = _chain({10: _REAL_HASH})
    reader = AgreeingChain(
        sources={"zero": zero, "other": other},
        endpoints={"zero": "wss://zero.example", "other": "wss://other.example"},
    )
    with pytest.raises(SourceLied, match="cannot be the honest answer"):
        reader.block_hash(10)


def test_canary_propagates_a_failed_read_as_chain_unavailable():
    """The one failure mode hash_canary itself never had, because a plain
    function has no connection to fail: canary() calls self.block_hash(block),
    which already wraps a connection failure in ChainUnavailable (a Refusal)
    via _ask, and that refusal must reach the caller as itself -- the same
    "nothing is KNOWN rather than nothing FOUND" discipline every other read
    on this class already gets, not swallowed or reworded into a plain
    exception AgreeingChain's own _agreed would otherwise wrap as
    SourceFailed instead of letting the real refusal through."""
    chain = _chain({}, fail="hash")
    with pytest.raises(ChainUnavailable):
        chain.canary(10)


class _HeadConnection:
    """Track finalized-head calls and reject implicit-latest block-number reads."""

    def __init__(self, *, head_hash, block_number):
        self._head_hash = head_hash
        self._block_number = block_number
        self.calls = []

    def get_chain_finalised_head(self):
        self.calls.append(("get_chain_finalised_head",))
        return self._head_hash

    def get_block_number(self, block_hash):
        self.calls.append(("get_block_number", block_hash))
        if block_hash is None:
            raise AssertionError(
                "get_block_number called with block_hash=None -- the exact implicit-latest "
                "shape this fix exists to close, whether spelled as zero arguments or as an "
                "explicit None. Every call here must name a real, already-resolved block hash."
            )
        if block_hash != self._head_hash:
            raise AssertionError(
                f"get_block_number called with {block_hash!r}, expected the real head hash "
                f"{self._head_hash!r}."
            )
        return self._block_number


def test_head_block_never_calls_get_block_number_with_none():
    """Resolve the finalized head once and pass its explicit hash to the number read.
    The connection rejects implicit-latest reads even if another route could
    happen to return the expected height."""
    connection = _HeadConnection(head_hash="0x" + "cd" * 32, block_number=12345)
    chain = LiveRunnerChain(connection, netuid=35, endpoint="wss://node.example")

    result = chain.head_block()

    assert result == 12345
    assert connection.calls == [
        ("get_chain_finalised_head",),
        ("get_block_number", "0x" + "cd" * 32),
    ]


def test_head_block_propagates_a_failed_read_as_chain_unavailable():
    class _FailingConnection:
        def get_chain_finalised_head(self):
            raise RuntimeError("the node hung up")

    chain = LiveRunnerChain(_FailingConnection(), netuid=35, endpoint="wss://node.example")
    with pytest.raises(ChainUnavailable):
        chain.head_block()


class _ClosableConnection:
    """Enough of a raw connection for ``close``: tracks whether it was
    actually closed, exactly once, and by name -- not inferred from some
    other side effect."""

    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.closed = True
        self.close_calls += 1


def test_close_closes_the_underlying_connection():
    """LiveRunnerChain.close delegates to the underlying connection exactly once."""
    connection = _ClosableConnection()
    chain = LiveRunnerChain(connection, netuid=35, endpoint="wss://node.example")

    chain.close()

    assert connection.closed is True
    assert connection.close_calls == 1
