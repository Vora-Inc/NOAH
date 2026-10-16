"""Alpha price read pinned to a block, and the pure TAO valuation (issue #69)."""

import pytest

from noah.chain.live import AlphaPriceUnreadable, ChainUnavailable, LiveRunnerChain
from noah.chain.sources import Refusal
from noah.chain.valuation import tao_value_rao


class _Value:
    def __init__(self, value):
        self.value = value


class _ScaleObjLike:
    """Not an int, not a dict: only a ``.value`` attribute, like ScaleObj."""

    def __init__(self, value):
        self.value = value


class _Conn:
    def __init__(self, prices=None, fail=False, answer=None):
        self.prices = prices or {}
        self.fail = fail
        self.answer = answer
        self.calls = []

    def get_block_hash(self, block):
        return f"0xhash{block}"

    def runtime_call(self, api, method, params, block_hash=None):
        self.calls.append((api, method, params, block_hash))
        if any(isinstance(p, int) for p in params):
            raise ValueError(f"Invalid type for data: {params[0]!r} of type <class 'int'>")
        if self.fail:
            raise RuntimeError("State already discarded for block")
        if self.answer is not None:
            return self.answer
        return _ScaleObjLike(self.prices[block_hash])


def _chain(conn):
    return LiveRunnerChain(conn, netuid=7, endpoint="ws://test")


def test_price_read_passes_block_hash_and_netuid():
    conn = _Conn({"0xhash50": 1_000_000_000})
    assert _chain(conn).alpha_price_at(50) == 1_000_000_000
    assert conn.calls == [("SwapRuntimeApi", "current_alpha_price", [(7,)], "0xhash50")]


def test_bare_int_param_is_rejected_by_fake_like_real_library():
    # Guards the fake itself: a bare [netuid] must fail, as it does live.
    with pytest.raises(ValueError):
        _Conn({}).runtime_call("SwapRuntimeApi", "current_alpha_price", [7], block_hash="0x")


def test_price_differs_between_pinned_blocks():
    conn = _Conn({"0xhash50": 1_000_000_000, "0xhash100": 1_250_000_000})
    c = _chain(conn)
    assert c.alpha_price_at(50) == 1_000_000_000
    assert c.alpha_price_at(100) == 1_250_000_000


def test_pruned_block_refuses_no_fallback():
    conn = _Conn(fail=True)
    with pytest.raises(AlphaPriceUnreadable):
        _chain(conn).alpha_price_at(50)
    assert len(conn.calls) == 1


@pytest.mark.parametrize("bad", [None, 0, -5, "1000", 1.5, True, [1]])
def test_bad_answer_refuses(bad):
    class C(_Conn):
        def runtime_call(self, *a, **k):
            return bad

    with pytest.raises(AlphaPriceUnreadable):
        _chain(C()).alpha_price_at(50)


def test_refusal_is_a_refusal_and_chain_unavailable():
    assert issubclass(AlphaPriceUnreadable, ChainUnavailable)
    assert issubclass(AlphaPriceUnreadable, Refusal)


def test_value_normal_keeps_raw():
    assert tao_value_rao(2_000_000_000, 1_500_000_000) == (2_000_000_000, 3_000_000_000)


def test_value_floors():
    # 7 * 1_500_000_000 / 1e9 = 10.5 -> 10
    assert tao_value_rao(7, 1_500_000_000) == (7, 10)
    # just under one rao of TAO floors to zero, raw preserved
    assert tao_value_rao(1, 999_999_999) == (1, 0)


def test_value_zero_amount():
    assert tao_value_rao(0, 1_000_000_000) == (0, 0)


def test_value_large_exact():
    assert tao_value_rao(10**18, 3 * 10**9) == (10**18, 3 * 10**18)


@pytest.mark.parametrize(
    "amount,price", [(-1, 10**9), (1, 0), (1, -1), (1.0, 10**9), (1, 1.0), (True, 10**9), (1, None)]
)
def test_value_rejects_bad_inputs(amount, price):
    with pytest.raises(ValueError):
        tao_value_rao(amount, price)


@pytest.mark.parametrize("unknown", [None, "", 0, b"0x1"])
def test_unknown_block_hash_refuses_without_calling_runtime(unknown):
    class C(_Conn):
        def get_block_hash(self, block):
            return unknown

    conn = C({"0xhash50": 1})
    with pytest.raises(AlphaPriceUnreadable, match="unknown"):
        _chain(conn).alpha_price_at(10**9)
    assert conn.calls == []
