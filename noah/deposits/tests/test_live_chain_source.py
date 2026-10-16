"""Offline tests for ``LiveChainSource`` -- entirely against a fake
connection object. No real ``async_substrate_interface`` construction, no
real network connection, and no import of ``async_substrate_interface``
happens anywhere in this file.
"""

import pytest

from noah.deposits.live_chain_source import (
    ENDPOINT_ENV_VARS,
    STAKE_API,
    LiveChainSource,
    LiveChainSourceMisconfigured,
    LiveChainSourceUnavailable,
    live_chain_source_from_environment,
)

HOTKEY, COLDKEY, NETUID = "5Hot", "5Cold", 42


class TestEndpointEnvVarOrder:
    def test_ws_vars_come_before_rpc_vars(self):
        """Regression guard for issue #15's own live QA crash: a caller
        (``inspect_transfer_call_metadata._select_endpoint_env_var``) that
        picks the FIRST set var from this tuple must land on a WS var when
        both kinds are set -- the connection factory
        ``live_chain_source_from_environment`` builds by default requires a
        ws/wss URL, and the real QA environment sets all four, with the RPC
        ones as ``http://``."""
        ws_positions = [ENDPOINT_ENV_VARS.index(v) for v in ("NOAH_AUTHOR_WS", "NOAH_READER_WS")]
        rpc_positions = [ENDPOINT_ENV_VARS.index(v) for v in ("NOAH_AUTHOR_RPC", "NOAH_READER_RPC")]
        assert max(ws_positions) < min(rpc_positions)


class _Wrapped:
    """Mimics ``async_substrate_interface``'s own ``.value``-wrapped scale
    decode results, which every read in this module must unwrap."""

    def __init__(self, value):
        self.value = value


class FakeConnection:
    def __init__(self, *, blocks, stake_by_block=None, fail_on=None):
        # blocks: {block_number: (hash, timestamp_ms)}
        self._blocks = blocks
        self._stake_by_block = stake_by_block or {}
        self._fail_on = fail_on or set()
        self.queries = []
        self.runtime_calls = []

    def get_chain_finalised_head(self):
        """The real, proven-to-return call this codebase's own audit
        established as the correct way to name the head explicitly --
        never a parameterless ``get_block_number(None)``/"give me latest"
        call. Returns the highest known block's own hash."""
        if "get_chain_finalised_head" in self._fail_on:
            raise RuntimeError("connection dropped")
        highest = max(self._blocks)
        return self._blocks[highest][0]

    def get_block_number(self, block_hash):
        """Refuses an implicit "latest" call loudly: a caller must always
        name an explicit block hash (fetched once through
        ``get_chain_finalised_head`` -- see that method and
        ``LiveChainSource.head_block``'s own docstring), never ``None``.
        This is this ticket's own regression guard proving nothing in this
        codebase still needs the parameterless shape."""
        if block_hash is None:
            raise AssertionError(
                "get_block_number called with no explicit block hash -- this is exactly the "
                "parameterless 'give me latest' shape that has hung the real chain; nothing in "
                "this codebase should ever call it this way."
            )
        for number, (bhash, _ts) in self._blocks.items():
            if bhash == block_hash:
                return number
        raise RuntimeError(f"unknown block_hash {block_hash!r}")

    def get_block_hash(self, block):
        if "get_block_hash" in self._fail_on:
            raise RuntimeError("connection dropped")
        return self._blocks[block][0]

    def query(self, pallet, name, keys, *, block_hash=None):
        self.queries.append((pallet, name, keys, block_hash))
        if pallet == "Timestamp" and name == "Now":
            for block, (bhash, ts_ms) in self._blocks.items():
                if bhash == block_hash:
                    return _Wrapped(ts_ms)
            raise RuntimeError("unknown block_hash")
        raise AssertionError(f"unexpected query {pallet}.{name}")

    def runtime_call(self, api, method, params, *, block_hash=None):
        self.runtime_calls.append((api, method, params, block_hash))
        assert api == STAKE_API
        assert method == "get_stake_info_for_hotkey_coldkey_netuid"
        return _Wrapped(self._stake_by_block[block_hash])


def a_connection():
    return FakeConnection(
        blocks={1: ("0xhash1", 1_800_000_000_000), 2: ("0xhash2", 1_800_000_300_000)},
        stake_by_block={"0xhash1": {"stake": 500_000, "locked": 0, "is_registered": True}},
    )


class TestHeadAndHash:
    def test_head_block_is_the_highest_known_block(self):
        source = LiveChainSource(a_connection(), endpoint="ws://node")
        assert source.head_block() == 2

    def test_head_block_never_makes_an_implicit_latest_call(self):
        """Regression guard: FakeConnection.get_block_number raises loudly
        if ever called with no explicit block hash (see its own
        docstring). head_block() succeeding here proves it always names
        the finalized head hash explicitly first."""
        connection = a_connection()
        assert LiveChainSource(connection, endpoint="ws://node").head_block() == 2

    def test_stub_get_block_number_refuses_an_implicit_no_hash_call(self):
        """Documents the guard directly: the stub itself refuses the
        parameterless 'give me latest' shape this ticket's audit found and
        fixed in LiveChainSource.head_block."""
        connection = a_connection()
        with pytest.raises(AssertionError):
            connection.get_block_number(None)

    def test_head_block_is_refused_when_the_finalized_head_call_fails(self):
        connection = FakeConnection(
            blocks={1: ("0xhash1", 0)}, fail_on={"get_chain_finalised_head"}
        )
        source = LiveChainSource(connection, endpoint="ws://node")
        with pytest.raises(LiveChainSourceUnavailable):
            source.head_block()

    def test_block_hash_is_unwrapped(self):
        source = LiveChainSource(a_connection(), endpoint="ws://node")
        assert source.block_hash(1) == "0xhash1"

    def test_a_connection_failure_is_a_named_refusal(self):
        connection = FakeConnection(blocks={1: ("0xhash1", 0)}, fail_on={"get_block_hash"})
        source = LiveChainSource(connection, endpoint="ws://node")
        with pytest.raises(LiveChainSourceUnavailable):
            source.block_hash(1)


class TestBlockTimestamp:
    def test_converts_milliseconds_to_whole_seconds(self):
        source = LiveChainSource(a_connection(), endpoint="ws://node")
        assert source.block_timestamp(1) == 1_800_000_000
        assert source.block_timestamp(2) == 1_800_000_300


class TestHoldingAt:
    def test_reads_the_stake_figure_pinned_to_a_block_number(self):
        source = LiveChainSource(a_connection(), endpoint="ws://node")
        stake = source.holding_at(hotkey=HOTKEY, coldkey=COLDKEY, netuid=NETUID, block=1)
        assert stake == 500_000

    def test_reads_the_stake_figure_pinned_to_a_block_hash(self):
        source = LiveChainSource(a_connection(), endpoint="ws://node")
        stake = source.holding_at(
            hotkey=HOTKEY, coldkey=COLDKEY, netuid=NETUID, block_hash="0xhash1"
        )
        assert stake == 500_000

    def test_refuses_neither_block_nor_block_hash(self):
        source = LiveChainSource(a_connection(), endpoint="ws://node")
        with pytest.raises(LiveChainSourceUnavailable):
            source.holding_at(hotkey=HOTKEY, coldkey=COLDKEY, netuid=NETUID)

    def test_refuses_both_block_and_block_hash(self):
        source = LiveChainSource(a_connection(), endpoint="ws://node")
        with pytest.raises(LiveChainSourceUnavailable):
            source.holding_at(
                hotkey=HOTKEY, coldkey=COLDKEY, netuid=NETUID, block=1, block_hash="0xhash1"
            )

    def test_refuses_a_stake_reply_with_no_stake_field(self):
        connection = FakeConnection(
            blocks={1: ("0xhash1", 0)}, stake_by_block={"0xhash1": {"locked": 0}}
        )
        source = LiveChainSource(connection, endpoint="ws://node")
        with pytest.raises(LiveChainSourceUnavailable):
            source.holding_at(hotkey=HOTKEY, coldkey=COLDKEY, netuid=NETUID, block=1)

    def test_refuses_a_negative_stake_figure(self):
        connection = FakeConnection(
            blocks={1: ("0xhash1", 0)}, stake_by_block={"0xhash1": {"stake": -1}}
        )
        source = LiveChainSource(connection, endpoint="ws://node")
        with pytest.raises(LiveChainSourceUnavailable):
            source.holding_at(hotkey=HOTKEY, coldkey=COLDKEY, netuid=NETUID, block=1)


class TestLiveChainSourceFromEnvironment:
    def test_refuses_an_unrecognized_env_var(self):
        with pytest.raises(LiveChainSourceMisconfigured):
            live_chain_source_from_environment("NOAH_TOTALLY_WRONG_VAR")

    def test_refuses_when_the_env_var_is_unset(self, monkeypatch):
        env_var = ENDPOINT_ENV_VARS[0]
        monkeypatch.delenv(env_var, raising=False)
        with pytest.raises(LiveChainSourceMisconfigured):
            live_chain_source_from_environment(env_var)

    def test_builds_from_a_real_url_using_the_injected_factory(self, monkeypatch):
        env_var = ENDPOINT_ENV_VARS[0]
        monkeypatch.setenv(env_var, "ws://example-private-server:9944")
        fake_connection = a_connection()
        source = live_chain_source_from_environment(
            env_var, connection_factory=lambda: fake_connection
        )
        assert isinstance(source, LiveChainSource)
        assert source.endpoint == "ws://example-private-server:9944"
        assert source.head_block() == 2

    def test_builds_from_a_wss_url_using_the_injected_factory(self, monkeypatch):
        """``wss://`` (secure WebSocket) is accepted, not just plain ``ws://``."""
        env_var = ENDPOINT_ENV_VARS[0]
        monkeypatch.setenv(env_var, "wss://example-private-server:9944")
        fake_connection = a_connection()
        source = live_chain_source_from_environment(
            env_var, connection_factory=lambda: fake_connection
        )
        assert source.endpoint == "wss://example-private-server:9944"

    def test_refuses_an_http_scheme_url_with_a_named_reason(self, monkeypatch):
        """Regression guard for issue #15's own live QA crash: an env var
        set to an ``http://`` URL (an RPC-shaped endpoint, e.g.
        ``NOAH_AUTHOR_RPC``/``NOAH_READER_RPC``) must be refused here, by
        name, rather than reaching the default connection factory and
        crashing inside ``websockets`` with
        ``InvalidURI: ... scheme isn't ws or wss`` -- the exact real trace
        this ticket's own QA run hit. No connection factory runs at all
        (the default lazy import of ``async_substrate_interface`` is never
        even reached): this is checked before ``connection_factory`` is
        called.
        """
        env_var = "NOAH_AUTHOR_RPC"
        monkeypatch.setenv(env_var, "http://127.0.0.1:9944")

        def unreachable_factory():
            raise AssertionError("connection_factory must not be called for an http:// URL")

        with pytest.raises(LiveChainSourceMisconfigured, match="ws://|wss://"):
            live_chain_source_from_environment(env_var, connection_factory=unreachable_factory)

    def test_refuses_an_https_scheme_url_too(self, monkeypatch):
        env_var = "NOAH_READER_RPC"
        monkeypatch.setenv(env_var, "https://127.0.0.1:9945")
        with pytest.raises(LiveChainSourceMisconfigured):
            live_chain_source_from_environment(env_var)


class AssistantConnection:
    """Decoded RPC shapes consumed by the participant assistant's adapter."""

    def __init__(self, *, subnet_key=2, wrapped=True):
        self.subnet_key = subnet_key
        self.wrapped = wrapped
        self.fail = False

    def result(self, value):
        if self.fail:
            raise RuntimeError("unavailable wss://reader.example/private-access")
        return _Wrapped(value) if self.wrapped else value

    def get_metadata_storage_function(self, pallet, name):
        assert pallet == "SubtensorModule"
        arity = 3 if name == "MinerCollateral" else 2
        return self.result({"type": {"Map": {"hashers": ["Blake2_128Concat"] * arity}}})

    def runtime_call(self, api, method, args):
        assert api == STAKE_API
        if method == "get_stake_info_for_hotkey_coldkey_netuid":
            assert args == [HOTKEY, COLDKEY, 2]
            return self.result({"stake": 1000, "is_registered": True})
        assert method == "get_stake_availability_for_coldkeys"
        assert args == [[COLDKEY], [2]]
        return self.result(
            {COLDKEY: {self.subnet_key: {"stake": 1000, "locked": 100, "available": 900}}}
        )

    def query(self, pallet, name, keys):
        assert pallet == "SubtensorModule"
        expected = {
            "MinerCollateral": ([2, HOTKEY, COLDKEY], {"locked": 100}),
            "ColdkeyMinerCollateral": ([2, COLDKEY], 100),
            "ColdkeyCollateralHotkeys": ([2, COLDKEY], [HOTKEY]),
            "SubtokenEnabled": ([2], True),
            "TransferToggle": ([2], True),
        }
        args, value = expected[name]
        assert keys == args
        return self.result(value)


@pytest.mark.parametrize("subnet_key", [2, "2"])
@pytest.mark.parametrize("wrapped", [True, False])
def test_assistant_adapter_feeds_real_ceiling_checks(subnet_key, wrapped):
    from noah.deposits.chain import ceiling_from_chain

    source = LiveChainSource(
        AssistantConnection(subnet_key=subnet_key, wrapped=wrapped), endpoint="wss://reader.example"
    )
    inputs = ceiling_from_chain(source, coldkey=COLDKEY, hotkey=HOTKEY, netuid=2)
    assert (inputs.position_stake, inputs.position_collateral, inputs.coldkey_available) == (
        1000,
        100,
        900,
    )
    assert source.subtoken_enabled(2) is True
    assert source.transfer_toggle(2) is True


def test_assistant_adapter_refuses_failed_read_without_exposing_endpoint():
    connection = AssistantConnection()
    connection.fail = True
    source = LiveChainSource(connection, endpoint="wss://reader.example/private-access")
    with pytest.raises(LiveChainSourceUnavailable) as failure:
        source.stake_info(hotkey=HOTKEY, coldkey=COLDKEY, netuid=2)
    assert "private-access" not in str(failure.value)
