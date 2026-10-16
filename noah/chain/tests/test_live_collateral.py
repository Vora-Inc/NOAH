"""``LiveRunnerChain``'s four collateral reads: ``collateral_arity``,
``coldkey_collateral``, ``collateral_hotkeys``, ``miner_collateral``.

Same discipline as ``test_live_holdings.py`` (issue #134's own test file):
these are raw reads with no resolution logic of their own -- every decision
about what a position MEANS belongs to ``noah/chain/collateral.py``,
already proven by its own test suite. What is specific to this file is the
COMPOSITION: does each method ask the chain the right question (right
storage item, right key order, matching the pinned runtime source cited in
``live.py``'s own docstrings), and does it correctly tell an ABSENT key
apart from a key that decodes to zero or empty.

The fake ``_Connection`` plays the role of a real
``async_substrate_interface.SubstrateInterface``, extending
``test_live_holdings.py``'s own fake with ``get_metadata_storage_function``
-- the one extra client method ``collateral_arity`` needs that none of the
four holding reads did.
"""

import pytest

from noah.chain.live import ChainUnavailable, LiveRunnerChain

NETUID = 35
COLDKEY = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
HOTKEY_1 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
HOTKEY_2 = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


class _MetadataEntry:
    """Stands in for the real client's metadata object, which exposes its
    decoded shape through ``.value`` -- the same duck-typed unwrap
    ``collateral_arity`` applies to any object carrying one."""

    def __init__(self, value):
        self.value = value


class _Connection:
    """Enough of a raw substrate connection for the four collateral reads.

    ``values`` maps ``(name, tuple(params))`` to whatever ``query()``
    should decode to -- ``None`` models the map's own ``OptionQuery``/
    ``ValueQuery`` absence, matching this project's real client (query()
    resolves a ``ValueQuery`` absence to the type's own default before this
    fake is ever reached; only ``MinerCollateral`` is genuinely
    ``OptionQuery`` and answers ``None`` for a real absence).
    ``metadata`` maps a storage item name to the raw hasher list
    ``get_metadata_storage_function`` should answer.
    """

    def __init__(self, *, values=None, metadata=None, fail=frozenset()):
        self._values = dict(values or {})
        self._metadata = dict(metadata or {})
        self._fail = fail
        self.queries = []
        # The FULL call each method actually made -- (module, storage_function,
        # params, block_hash) / (module, storage_function, block_hash) -- kept
        # separately from `queries` above (which only ever recorded
        # storage_function/params) so a regression that asked the wrong PALLET
        # or forgot to pin `block_hash` to the requested historical block (using
        # the connection's own current head instead) still fails a test even
        # though every value this fake hands back is keyed by storage_function/
        # params alone. [codex review, 2026-09-03, P3]: the original fake
        # discarded `module` and `block_hash` entirely, so all 24 tests in this
        # file would still pass a version of these four methods that queried
        # the wrong pallet or the live head rather than the pinned block --
        # exactly the "record head collateral against an old block" class of
        # bug this project's own reorg-protection discipline exists to catch.
        self.calls = []
        self.metadata_calls = []

    def get_block_hash(self, block):
        return f"0x{block:x}"

    def query(self, module, storage_function, params, block_hash=None):
        key = (storage_function, tuple(params))
        self.queries.append(key)
        self.calls.append((module, storage_function, tuple(params), block_hash))
        if storage_function in self._fail:
            raise RuntimeError("the node hung up")
        try:
            return self._values[key]
        except KeyError:
            raise AssertionError(f"test did not stub a query() value for {key}") from None

    def get_metadata_storage_function(self, module, storage_function, block_hash=None):
        self.metadata_calls.append((module, storage_function, block_hash))
        if storage_function in self._fail:
            raise RuntimeError("the node hung up")
        try:
            hashers = self._metadata[storage_function]
        except KeyError:
            raise AssertionError(f"test did not stub metadata for {storage_function}") from None
        return _MetadataEntry({"type": {"Map": {"hashers": hashers}}})


def _chain(connection, netuid=NETUID):
    return LiveRunnerChain(connection, netuid=netuid, endpoint="wss://node.example")


# ---- collateral_arity -------------------------------------------------------


def test_collateral_arity_reads_both_maps_hasher_count():
    connection = _Connection(
        metadata={
            "ColdkeyMinerCollateral": ["a", "b"],
            "MinerCollateral": ["a", "b", "c"],
        }
    )
    chain = _chain(connection)
    assert chain.collateral_arity(10) == {
        "ColdkeyMinerCollateral": 2,
        "MinerCollateral": 3,
    }


def test_collateral_arity_refuses_a_map_with_no_key_definition():
    connection = _Connection(metadata={"ColdkeyMinerCollateral": [], "MinerCollateral": []})
    # An empty hashers list is still a real (if degenerate) declared shape;
    # the missing-definition case is a metadata entry with no "Map" key at
    # all, modeled by a bare entry with no "type" carrying "Map".
    connection._metadata["ColdkeyMinerCollateral"] = None
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable, match="carries no key definition"):
        chain.collateral_arity(10)


def test_collateral_arity_propagates_a_connection_failure():
    connection = _Connection(fail={"ColdkeyMinerCollateral"})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable):
        chain.collateral_arity(10)


# ---- coldkey_collateral ------------------------------------------------------


def test_coldkey_collateral_decodes_a_real_bare_int():
    connection = _Connection(
        values={("ColdkeyMinerCollateral", (NETUID, COLDKEY)): 250_000}
    )
    chain = _chain(connection)
    assert chain.coldkey_collateral(COLDKEY, 10) == 250_000


def test_coldkey_collateral_none_is_the_honest_zero():
    connection = _Connection(values={("ColdkeyMinerCollateral", (NETUID, COLDKEY)): None})
    chain = _chain(connection)
    assert chain.coldkey_collateral(COLDKEY, 10) == 0


def test_coldkey_collateral_zero_is_a_real_answer():
    connection = _Connection(values={("ColdkeyMinerCollateral", (NETUID, COLDKEY)): 0})
    chain = _chain(connection)
    assert chain.coldkey_collateral(COLDKEY, 10) == 0


@pytest.mark.parametrize(
    "bogus",
    [True, False, "7", b"7", 7.9, -1, 2**64],
    ids=["true", "false", "numeric-string", "bytes", "float", "negative", "above-u64"],
)
def test_coldkey_collateral_refuses_bare_int_coercion_bait(bogus):
    connection = _Connection(values={("ColdkeyMinerCollateral", (NETUID, COLDKEY)): bogus})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable, match="not None"):
        chain.coldkey_collateral(COLDKEY, 10)


def test_coldkey_collateral_propagates_a_connection_failure():
    connection = _Connection(fail={"ColdkeyMinerCollateral"})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable):
        chain.coldkey_collateral(COLDKEY, 10)


# ---- collateral_hotkeys ------------------------------------------------------


def test_collateral_hotkeys_decodes_a_real_bare_ss58_list():
    connection = _Connection(
        values={("ColdkeyCollateralHotkeys", (NETUID, COLDKEY)): [HOTKEY_1, HOTKEY_2]}
    )
    chain = _chain(connection)
    assert chain.collateral_hotkeys(COLDKEY, 10) == [HOTKEY_1, HOTKEY_2]


def test_collateral_hotkeys_none_is_the_honest_empty_answer():
    connection = _Connection(values={("ColdkeyCollateralHotkeys", (NETUID, COLDKEY)): None})
    chain = _chain(connection)
    assert chain.collateral_hotkeys(COLDKEY, 10) == []


def test_collateral_hotkeys_empty_list_is_a_real_answer():
    connection = _Connection(values={("ColdkeyCollateralHotkeys", (NETUID, COLDKEY)): []})
    chain = _chain(connection)
    assert chain.collateral_hotkeys(COLDKEY, 10) == []


def test_collateral_hotkeys_refuses_a_non_list_decode():
    connection = _Connection(
        values={("ColdkeyCollateralHotkeys", (NETUID, COLDKEY)): {"unexpected": "shape"}}
    )
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable, match="not a list of hotkeys"):
        chain.collateral_hotkeys(COLDKEY, 10)


def test_collateral_hotkeys_refuses_a_bad_element_rather_than_dropping_it():
    """Issue #540's own live crash, reproduced here as a refusal instead:
    an element that does not decode as a genuine AccountId32 (an empty
    tuple, matching the real crash) must not be silently dropped -- that
    would shrink the hotkey set and understate pledged collateral."""
    connection = _Connection(
        values={("ColdkeyCollateralHotkeys", (NETUID, COLDKEY)): [HOTKEY_1, ()]}
    )
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable, match="not a genuine hotkey"):
        chain.collateral_hotkeys(COLDKEY, 10)


def test_collateral_hotkeys_propagates_a_connection_failure():
    connection = _Connection(fail={"ColdkeyCollateralHotkeys"})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable):
        chain.collateral_hotkeys(COLDKEY, 10)


# ---- miner_collateral ---------------------------------------------------------


def test_miner_collateral_returns_the_whole_row_unreduced():
    row = {"locked": 250_000, "drain_ratio": 0, "min_locked": 0, "earned": 0}
    connection = _Connection(
        values={("MinerCollateral", (NETUID, HOTKEY_1, COLDKEY)): row}
    )
    chain = _chain(connection)
    assert chain.miner_collateral(HOTKEY_1, COLDKEY, 10) == row


def test_miner_collateral_none_is_the_honest_option_query_absence():
    connection = _Connection(values={("MinerCollateral", (NETUID, HOTKEY_1, COLDKEY)): None})
    chain = _chain(connection)
    assert chain.miner_collateral(HOTKEY_1, COLDKEY, 10) is None


def test_miner_collateral_propagates_a_connection_failure():
    connection = _Connection(fail={"MinerCollateral"})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable):
        chain.miner_collateral(HOTKEY_1, COLDKEY, 10)


def test_miner_collateral_asks_the_key_in_netuid_hotkey_coldkey_order():
    """pallets/subtensor/src/lib.rs:3265-3281: MinerCollateral is an NMap
    over (netuid, hotkey, coldkey), in that order -- not (netuid, coldkey,
    hotkey), and not (hotkey, coldkey, netuid) the way Alpha/AlphaV2 above
    are keyed."""
    connection = _Connection(values={("MinerCollateral", (NETUID, HOTKEY_1, COLDKEY)): None})
    chain = _chain(connection)
    chain.miner_collateral(HOTKEY_1, COLDKEY, 10)
    assert ("MinerCollateral", (NETUID, HOTKEY_1, COLDKEY)) in connection.queries


# ---- the FULL call shape, not just the value it decoded ---------------------
#
# [codex review, 2026-09-03, P3] every test above stubs `_Connection.query`/
# `.get_metadata_storage_function` keyed by (storage_function, params) alone
# -- `module` and `block_hash` are accepted but never checked, so all 24
# tests above would still pass a version of these four methods that asked
# the wrong PALLET (silently wrong data, if some other pallet happened to
# declare a same-named item) or pinned no block at all / the connection's
# own live head instead of the historical block requested (exactly the
# "record head collateral against an old block" class of bug this whole
# file's block-pinned discipline exists to prevent -- see live.py's own
# module docstring, "every read in this file... is pinned to an explicit,
# reorg-checked block"). These four tests close that gap directly: one per
# method, asserting the COMPLETE call each one actually made.


def test_collateral_arity_asks_subtensor_module_pinned_to_the_right_block():
    connection = _Connection(
        metadata={"ColdkeyMinerCollateral": ["a", "b"], "MinerCollateral": ["a", "b", "c"]}
    )
    chain = _chain(connection)
    chain.collateral_arity(10)
    expected_hash = connection.get_block_hash(10)
    assert ("SubtensorModule", "ColdkeyMinerCollateral", expected_hash) in connection.metadata_calls
    assert ("SubtensorModule", "MinerCollateral", expected_hash) in connection.metadata_calls


def test_coldkey_collateral_asks_subtensor_module_pinned_to_the_right_block():
    connection = _Connection(values={("ColdkeyMinerCollateral", (NETUID, COLDKEY)): 5})
    chain = _chain(connection)
    chain.coldkey_collateral(COLDKEY, 10)
    expected_hash = connection.get_block_hash(10)
    assert (
        "SubtensorModule",
        "ColdkeyMinerCollateral",
        (NETUID, COLDKEY),
        expected_hash,
    ) in connection.calls


def test_collateral_hotkeys_asks_subtensor_module_pinned_to_the_right_block():
    connection = _Connection(values={("ColdkeyCollateralHotkeys", (NETUID, COLDKEY)): []})
    chain = _chain(connection)
    chain.collateral_hotkeys(COLDKEY, 10)
    expected_hash = connection.get_block_hash(10)
    assert (
        "SubtensorModule",
        "ColdkeyCollateralHotkeys",
        (NETUID, COLDKEY),
        expected_hash,
    ) in connection.calls


def test_miner_collateral_asks_subtensor_module_pinned_to_the_right_block():
    connection = _Connection(values={("MinerCollateral", (NETUID, HOTKEY_1, COLDKEY)): None})
    chain = _chain(connection)
    chain.miner_collateral(HOTKEY_1, COLDKEY, 10)
    expected_hash = connection.get_block_hash(10)
    assert (
        "SubtensorModule",
        "MinerCollateral",
        (NETUID, HOTKEY_1, COLDKEY),
        expected_hash,
    ) in connection.calls
