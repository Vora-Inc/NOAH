"""Test storage keys, parameter order and absent-versus-zero handling for
LiveRunnerChain holding reads. Injected connections model the SDK storage API;
these tests require neither live chain access nor SCALE metadata."""

import pytest

from noah.chain.holdings import Share, ShareNotRead
from noah.chain.live import ChainUnavailable, LiveRunnerChain
from noah.chain.reads import Provenance
from noah.chain.runner import _sample_holdings
from noah.chain.store import Store

NETUID = 35
COLDKEY = "5DvNNyigjiHUeq85VZoV24ZqbTUxesZ31fX3rriXLk3LAC7U"
HOTKEY_1 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
HOTKEY_2 = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
ANCHOR = 1_767_571_200


class _FakeStorageKey:
    """A minimal stand-in for the real ``StorageKey`` -- see this module's
    own docstring for why a real one (needing real, decoded chain metadata)
    is out of scope for a unit test and not what these tests are proving."""

    def __init__(self, name, params):
        self.name = name
        self.params = tuple(params)

    def to_hex(self):
        return (self.name, self.params)


class _Connection:
    """Enough of a raw substrate connection for the four holding reads.

    ``values`` maps ``(name, tuple(params))`` to whatever ``query()`` should
    decode to. ``present`` is the set of the SAME keys whose raw storage key
    genuinely exists -- read by ``get_storage_by_key``, entirely separately
    from ``values``, exactly the way the real client's raw RPC is
    independent of its typed decode. A key in ``values`` but not in
    ``present`` models the ordinary "ValueQuery default" case: ``query()``
    still answers (with the type's own zero), but the raw key does not
    exist.
    """

    def __init__(
        self, *, values=None, present=None, fail=frozenset(), declared=None, metadata_fail=None
    ):
        # ``declared``: block_hash -> the set of SubtensorModule storage items
        # the runtime metadata at THAT hash declares (a callable of the hash
        # is also accepted). Default: every item exists, the pre-473 shape.
        self._declared = declared
        self._metadata_fail = metadata_fail
        self.metadata_asked = []
        self._values = dict(values or {})
        self._present = set(present or set())
        self._fail = fail
        self.queries = []
        self.presence_checks = []

    def get_block_hash(self, block):
        return f"0x{block:x}"

    def get_metadata_storage_function(self, module, name, block_hash=None):
        """The real client's contract: the runtime for ``block_hash`` is loaded
        and the entry returned, or ``None`` for an item it does not declare."""
        self.metadata_asked.append((module, name, block_hash))
        if self._metadata_fail is not None:
            raise self._metadata_fail
        if self._declared is None:
            return object()
        declared = self._declared(block_hash) if callable(self._declared) else self._declared
        return object() if name in declared else None

    def query(self, module, storage_function, params, block_hash=None):
        key = (storage_function, tuple(params))
        self.queries.append(key)
        if storage_function in self._fail:
            raise RuntimeError("the node hung up")
        try:
            return self._values[key]
        except KeyError:
            raise AssertionError(f"test did not stub a query() value for {key}") from None

    def create_storage_key(self, module, storage_function, params, block_hash=None):
        return _FakeStorageKey(storage_function, params)

    def get_storage_by_key(self, block_hash, storage_key):
        self.presence_checks.append(storage_key)
        name, params = storage_key
        return "0xpresent" if (name, params) in self._present else None


def _chain(connection, netuid=NETUID):
    return LiveRunnerChain(connection, netuid=netuid, endpoint="wss://node.example")


def _v1(bits):
    return {"bits": bits}


def _v2(mantissa, exponent):
    return {"mantissa": mantissa, "exponent": exponent}


# ---- staking_hotkeys --------------------------------------------------------


def test_staking_hotkeys_decodes_a_real_bare_ss58_list():
    connection = _Connection(values={("StakingHotkeys", (COLDKEY,)): [HOTKEY_1, HOTKEY_2]})
    chain = _chain(connection)
    assert chain.staking_hotkeys(COLDKEY, 10) == [HOTKEY_1, HOTKEY_2]


def test_staking_hotkeys_empty_is_a_real_answer_not_a_refusal():
    connection = _Connection(values={("StakingHotkeys", (COLDKEY,)): []})
    chain = _chain(connection)
    assert chain.staking_hotkeys(COLDKEY, 10) == []


def test_staking_hotkeys_refuses_a_non_list_decode():
    connection = _Connection(values={("StakingHotkeys", (COLDKEY,)): {"unexpected": "shape"}})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable, match="not a list of hotkeys"):
        chain.staking_hotkeys(COLDKEY, 10)


def test_staking_hotkeys_propagates_a_connection_failure():
    connection = _Connection(fail={"StakingHotkeys"})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable):
        chain.staking_hotkeys(COLDKEY, 10)


# ---- alpha_share -------------------------------------------------------------


def test_alpha_share_reads_the_v1_map_when_only_it_has_a_key():
    connection = _Connection(
        values={
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): _v1(1),
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(0, 0),
        },
        present={("Alpha", (HOTKEY_1, COLDKEY, NETUID))},
    )
    chain = _chain(connection)
    assert chain.alpha_share(HOTKEY_1, COLDKEY, 10) == Share(
        encoding="u64f64", raw=1, exponent=0, source="v1"
    )


def test_alpha_share_reads_the_v2_map_when_only_it_has_a_key():
    connection = _Connection(
        values={
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): _v1(0),
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(3, -2),
        },
        present={("AlphaV2", (HOTKEY_1, COLDKEY, NETUID))},
    )
    chain = _chain(connection)
    assert chain.alpha_share(HOTKEY_1, COLDKEY, 10) == Share(
        encoding="safefloat", raw=3, exponent=-2, source="v2"
    )


def test_alpha_share_absent_from_both_is_a_real_zero_not_a_refusal():
    connection = _Connection(
        values={
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): _v1(0),
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(0, 0),
        },
        present=set(),
    )
    chain = _chain(connection)
    assert chain.alpha_share(HOTKEY_1, COLDKEY, 10) == Share(
        encoding="safefloat", raw=0, exponent=0, source="absent"
    )


def test_alpha_share_a_present_zero_is_the_answer_not_a_fallthrough_to_v2():
    """The load-bearing case this whole file exists to prove: a wallet's v1
    share can genuinely be zero AND present (the key exists) while v2 for
    the SAME hotkey/coldkey pair also happens to hold a real, nonzero value
    -- an inconsistent chain state that should never occur in practice, but
    is exactly the shape a bug that ignores presence (and falls through to
    v2 whenever v1 decodes to zero) would get wrong silently. If presence
    were not read correctly here, this would return the v2 Share instead of
    an honest v1-sourced zero, which is precisely the "flattening the two
    number systems" defect ``holdings.py``'s own module docstring and the
    2026-08-20 recording both exist to prevent.
    """
    connection = _Connection(
        values={
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): _v1(0),
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(999, 0),
        },
        present={("Alpha", (HOTKEY_1, COLDKEY, NETUID))},
    )
    chain = _chain(connection)
    assert chain.alpha_share(HOTKEY_1, COLDKEY, 10) == Share(
        encoding="u64f64", raw=0, exponent=0, source="v1"
    ), "a present v1 zero must win, not be treated as absent and fall through to v2"


def test_alpha_share_asks_the_right_storage_items_in_the_right_key_order():
    connection = _Connection(
        values={
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): _v1(0),
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(0, 0),
        },
        present=set(),
    )
    chain = _chain(connection)
    chain.alpha_share(HOTKEY_1, COLDKEY, 10)
    assert ("Alpha", (HOTKEY_1, COLDKEY, NETUID)) in connection.queries
    assert ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)) in connection.queries
    assert ("Alpha", (HOTKEY_1, COLDKEY, NETUID)) in connection.presence_checks
    assert ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)) in connection.presence_checks


def test_alpha_share_propagates_a_connection_failure():
    connection = _Connection(fail={"Alpha"})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable):
        chain.alpha_share(HOTKEY_1, COLDKEY, 10)


def test_alpha_share_refuses_a_malformed_v1_shape():
    connection = _Connection(
        values={
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): "not a mapping",
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(0, 0),
        },
        present={("Alpha", (HOTKEY_1, COLDKEY, NETUID))},
    )
    chain = _chain(connection)
    with pytest.raises(ShareNotRead):
        chain.alpha_share(HOTKEY_1, COLDKEY, 10)


# ---- hotkey_shares -------------------------------------------------------------


def test_hotkey_shares_absent_from_both_returns_none_not_a_refusal():
    """[codex adversarial challenge, round 2, 2026-08-31, P1]: ``StakingHotkeys``
    is not scoped to ``self.netuid`` -- a wallet who stakes to a hotkey ONLY
    on a different subnet still has that hotkey listed, and while sampling
    THIS subnet, both this wallet's share and this hotkey's total on THIS
    subnet are honestly, ordinarily absent. Refusing that (a round-1 "fix"
    briefly did) would abort the whole sample for any wallet that has ever
    staked on more than one subnet -- see this method's own docstring for
    the full two-round history and why ``share_present=False`` is correct,
    not merely the least-bad option."""
    connection = _Connection(
        values={
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)): _v1(0),
            ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)): _v2(0, 0),
        },
        present=set(),
    )
    chain = _chain(connection)
    assert chain.hotkey_shares(HOTKEY_1, 10) is None


def test_hotkey_shares_reads_the_v1_map_when_present():
    connection = _Connection(
        values={
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)): _v1(2),
            ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)): _v2(0, 0),
        },
        present={("TotalHotkeyShares", (HOTKEY_1, NETUID))},
    )
    chain = _chain(connection)
    assert chain.hotkey_shares(HOTKEY_1, 10) == Share(
        encoding="u64f64", raw=2, exponent=0, source="v1"
    )


def test_hotkey_shares_reads_the_v2_map_when_present():
    connection = _Connection(
        values={
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)): _v1(0),
            ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)): _v2(6, 0),
        },
        present={("TotalHotkeySharesV2", (HOTKEY_1, NETUID))},
    )
    chain = _chain(connection)
    assert chain.hotkey_shares(HOTKEY_1, 10) == Share(
        encoding="safefloat", raw=6, exponent=0, source="v2"
    )


def test_hotkey_shares_a_present_total_of_zero_refuses():
    """resolve_denominator's own rule: a total that reads zero WHILE the key
    is genuinely present cannot be honest (a wallet's share is a fraction of
    it), so this must raise rather than return a zero-valued Share -- and
    must not confuse "present and zero" with "absent" (which legitimately
    returns None, tested above)."""
    connection = _Connection(
        values={
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)): _v1(0),
            ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)): _v2(0, 0),
        },
        present={("TotalHotkeyShares", (HOTKEY_1, NETUID))},
    )
    chain = _chain(connection)
    with pytest.raises(ShareNotRead):
        chain.hotkey_shares(HOTKEY_1, 10)


def test_hotkey_shares_asks_the_right_storage_items_in_the_right_key_order():
    connection = _Connection(
        values={
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)): _v1(2),
            ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)): _v2(0, 0),
        },
        present={("TotalHotkeyShares", (HOTKEY_1, NETUID))},
    )
    chain = _chain(connection)
    chain.hotkey_shares(HOTKEY_1, 10)
    assert ("TotalHotkeyShares", (HOTKEY_1, NETUID)) in connection.queries
    assert ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)) in connection.queries


# ---- hotkey_alpha ---------------------------------------------------------------


def test_hotkey_alpha_decodes_a_bare_int_live_confirmed_shape():
    """AlphaBalance decodes as a bare int through this project's read path
    (measured live, 2026-08-20 -- see this method's own docstring), unlike
    the structurally similar MechId -- not through
    ``_mechanism_count_scalar``'s list-unwrap guard."""
    connection = _Connection(values={("TotalHotkeyAlpha", (HOTKEY_1, NETUID)): 234_544_720_269})
    chain = _chain(connection)
    assert chain.hotkey_alpha(HOTKEY_1, 10) == 234_544_720_269


def test_hotkey_alpha_unwraps_a_scale_value_wrapper():
    class _ScaleValue:
        def __init__(self, value):
            self.value = value

    connection = _Connection(values={("TotalHotkeyAlpha", (HOTKEY_1, NETUID)): _ScaleValue(5)})
    chain = _chain(connection)
    assert chain.hotkey_alpha(HOTKEY_1, 10) == 5


def test_hotkey_alpha_refuses_a_list_wrapped_decode():
    """If a future runtime ever DID make AlphaBalance decode as MechId does
    (a length-1 list), this must not silently misread it -- it should fail
    loudly by name rather than pick an arbitrary element."""
    connection = _Connection(values={("TotalHotkeyAlpha", (HOTKEY_1, NETUID)): [5]})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable, match="not the already-plain"):
        chain.hotkey_alpha(HOTKEY_1, 10)


@pytest.mark.parametrize(
    "bogus",
    [True, False, "7", b"7", 7.9, -1, 2**64],
    ids=["true", "false", "numeric-string", "bytes", "float", "negative", "above-u64"],
)
def test_hotkey_alpha_refuses_bare_int_coercion_bait(bogus):
    """[codex adversarial review, 2026-08-31, P2]: the first version of this
    method used a bare ``int(_plain(...))`` cast, which silently accepts
    exactly these shapes (``bool`` is an ``int`` subclass; ``int("7")``,
    ``int(b"7")``, and ``int(7.9)`` all succeed) even though none is the
    already-plain int, in ``AlphaBalance``'s own ``u64`` range, this storage
    item's live-confirmed real shape produces. This is the same class of
    blind coercion ``_is_plain_mechid_int``/``_is_plain_byte`` already guard
    against elsewhere in this file, for the identical reason. ``2**64``
    [codex adversarial challenge, round 2, 2026-08-31, P2] is the first
    version of the FIXED guard's own gap: it checked ``value >= 0`` but not
    an upper bound, so a value ``AlphaBalance`` (a wrapped ``u64``) can
    never honestly hold still passed through into money arithmetic."""
    connection = _Connection(values={("TotalHotkeyAlpha", (HOTKEY_1, NETUID)): bogus})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable, match="not the already-plain"):
        chain.hotkey_alpha(HOTKEY_1, 10)


def test_hotkey_alpha_zero_is_a_real_answer():
    connection = _Connection(values={("TotalHotkeyAlpha", (HOTKEY_1, NETUID)): 0})
    chain = _chain(connection)
    assert chain.hotkey_alpha(HOTKEY_1, 10) == 0


def test_hotkey_alpha_accepts_the_real_maximum_u64_value():
    """The other side of the ``2**64`` boundary tested above: the largest
    value ``AlphaBalance`` can actually hold must still be accepted, not
    caught by an overzealous bound."""
    connection = _Connection(values={("TotalHotkeyAlpha", (HOTKEY_1, NETUID)): 2**64 - 1})
    chain = _chain(connection)
    assert chain.hotkey_alpha(HOTKEY_1, 10) == 2**64 - 1


def test_hotkey_alpha_propagates_a_connection_failure():
    connection = _Connection(fail={"TotalHotkeyAlpha"})
    chain = _chain(connection)
    with pytest.raises(ChainUnavailable):
        chain.hotkey_alpha(HOTKEY_1, 10)


# ---- end to end: LiveRunnerChain -> _sample_holdings -> holdings.py --------


def test_sample_holdings_end_to_end_across_both_number_systems(tmp_path):
    """The full path issue #134 exists to unblock: a real ``LiveRunnerChain``
    (wrapping a fake connection shaped like the real chain) feeding
    ``_sample_holdings`` (``noah/chain/runner.py``), which is the exact,
    already-proven caller of these four methods, which itself calls
    ``holdings.py``'s ``holding_for_coldkey`` -- nothing in this test
    reimplements that arithmetic.

    Two hotkeys, deliberately in DIFFERENT number systems -- H1 wholly v1
    (share and denominator both old ``U64F64``), H2 wholly v2 (both new
    ``SafeFloat``) -- matching the real, live-recorded shape holdings.py's
    own module docstring describes ("the mixed pairing is the ordinary
    case"). Hand-computed expectation, matching
    ``positions_for_coldkey``'s own floor-division arithmetic
    (``pool_alpha * share_num * denom_den // (share_den * denom_num)``):

    H1 (v1): share=1/2**64, denominator=2/2**64 -> ratio 1/2. pool=1000.
             alpha = 1000 * 1 // 2 = 500.
    H2 (v2): share=3 (mantissa 3, exp 0), denominator=6 (mantissa 6, exp 0)
             -> ratio 3/6 = 1/2. pool=2000. alpha = 2000 * 3 // 6 = 1000.
    total = 500 + 1000 = 1500.
    """
    connection = _Connection(
        values={
            ("Now", ()): (ANCHOR + 400) * 1000,
            ("StakingHotkeys", (COLDKEY,)): [HOTKEY_1, HOTKEY_2],
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): _v1(1),
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(0, 0),
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)): _v1(2),
            ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)): _v2(0, 0),
            ("TotalHotkeyAlpha", (HOTKEY_1, NETUID)): 1000,
            ("Alpha", (HOTKEY_2, COLDKEY, NETUID)): _v1(0),
            ("AlphaV2", (HOTKEY_2, COLDKEY, NETUID)): _v2(3, 0),
            ("TotalHotkeyShares", (HOTKEY_2, NETUID)): _v1(0),
            ("TotalHotkeySharesV2", (HOTKEY_2, NETUID)): _v2(6, 0),
            ("TotalHotkeyAlpha", (HOTKEY_2, NETUID)): 2000,
        },
        present={
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)),
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)),
            ("AlphaV2", (HOTKEY_2, COLDKEY, NETUID)),
            ("TotalHotkeySharesV2", (HOTKEY_2, NETUID)),
        },
    )
    chain = _chain(connection)

    store = Store.create(tmp_path / "noah.db", day_anchor_ts=ANCHOR)
    store.record_noah_deposit(
        coldkey=COLDKEY,
        block_timestamp=ANCHOR + 300,
        amount_rao=1,
        block=8_800_000,
        event_ordinal=0,
        extrinsic_index=0,
        block_hash="0xdeposit",
        carried_by="StakeAdded",
    )

    provenance = Provenance(sources={"live": "wss://node.example"})
    _sample_holdings(
        store,
        chain,
        day=0,
        from_block=8_800_001,
        to_block=8_800_001,
        stride=1,
        head=8_800_002,
        provenance=provenance,
        day_time=ANCHOR + 400,
    )

    samples = store.holding_samples(COLDKEY, day=0)
    assert len(samples) == 1
    assert samples[0]["total_rao"] == 1500, "both number systems must resolve into one real total"


def test_sample_holdings_does_not_abort_for_a_wallet_staked_only_on_another_subnet(tmp_path):
    """[codex adversarial challenge, round 2, 2026-08-31, P1], reproduced
    through the FULL caller, not just ``hotkey_shares`` in isolation: a
    wallet stakes to a hotkey on some OTHER subnet, so ``StakingHotkeys``
    (global, not netuid-scoped) still lists that hotkey for this coldkey.
    While sampling holdings on THIS subnet, the wallet's own share and this
    hotkey's total are both honestly absent here. A version of this code
    that refused an absent denominator unconditionally (``share_present=
    True``) would raise and abort the ENTIRE day's sample for this wallet
    -- a healthy, ordinary participant, not a corrupted or reorged read.
    The fix must let the run complete, crediting this hotkey with zero and
    moving on, exactly as it would for a hotkey nobody stakes to at all.
    """
    connection = _Connection(
        values={
            ("Now", ()): (ANCHOR + 400) * 1000,
            ("StakingHotkeys", (COLDKEY,)): [HOTKEY_1],
            ("Alpha", (HOTKEY_1, COLDKEY, NETUID)): _v1(0),
            ("AlphaV2", (HOTKEY_1, COLDKEY, NETUID)): _v2(0, 0),
            ("TotalHotkeyShares", (HOTKEY_1, NETUID)): _v1(0),
            ("TotalHotkeySharesV2", (HOTKEY_1, NETUID)): _v2(0, 0),
        },
        present=set(),
    )
    chain = _chain(connection)

    store = Store.create(tmp_path / "noah.db", day_anchor_ts=ANCHOR)
    store.record_noah_deposit(
        coldkey=COLDKEY,
        block_timestamp=ANCHOR + 300,
        amount_rao=1,
        block=8_800_000,
        event_ordinal=0,
        extrinsic_index=0,
        block_hash="0xdeposit",
        carried_by="StakeAdded",
    )

    provenance = Provenance(sources={"live": "wss://node.example"})
    _sample_holdings(
        store,
        chain,
        day=0,
        from_block=8_800_001,
        to_block=8_800_001,
        stride=1,
        head=8_800_002,
        provenance=provenance,
        day_time=ANCHOR + 400,
    )

    samples = store.holding_samples(COLDKEY, day=0)
    assert len(samples) == 1
    assert samples[0]["total_rao"] == 0, "no share on this subnet, so nothing to credit here"
