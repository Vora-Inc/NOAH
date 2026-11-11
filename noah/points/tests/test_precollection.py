"""Exact fixture eligibility and dual-reader pricing with synthetic ledger inputs.

RuntimePolicy pricing methods are exercised with an isolated policy state; the
migration/startup authentication contract has its separate integration suite.
No node, wallet, endpoint or retained production fixture is used here.
"""

import json
from types import SimpleNamespace

import pytest

from noah.chain.agreeing import AgreeingChain
from noah.chain.fixture_build import build_fixture, MissingFromStore
from noah.chain.foreign_value import ForeignDepositUnpriced
from noah.chain.holdings import Share
from noah.chain.live import LiveRunnerChain
from noah.chain.sources import Refusal, SourceDisagreement, SourceLied
from noah.chain.store import Store, NOT_READ
from noah.points.collector import Collector
from noah.points.ledger import R
from noah.points.runtime import RuntimePolicy

ANCHOR = 1_700_006_400  # a synthetic UTC midnight
DIALS = dict(
    targetFp="500000000",
    harshness=1,
    capFp="1000000000",
    halfLifeDays="182",
    streakCeilingFp="2000000000",
    streakRampDays="365",
    ongoingHoldRao="1",
)


def hash_of(block):
    return "0x" + f"{block:064x}"


def moment(block):
    return ANCHOR + (block - 1) * 3600


class Node:
    def __init__(self, bad=None):
        self.prices = []
        self.hashes = []
        self.bad = bad

    def get_block_hash(self, block):
        self.hashes.append(block)
        return hash_of(block)

    def get_chain_finalised_head(self):
        return hash_of(73)

    def get_block_number(self, h):
        return int(h, 16)

    def query(self, module, name, params, *, block_hash):
        assert (module, name, params) == ("Timestamp", "Now", [])
        return moment(int(block_hash, 16)) * 1000

    def runtime_call(self, api, method, params, *, block_hash):
        assert (api, method) == ("SwapRuntimeApi", "current_alpha_price")
        netuid = params[0][0]
        block = int(block_hash, 16)
        self.prices.append((block, netuid))
        if self.bad is not None:
            changed = self.bad(block, netuid)
            if changed is not None:
                return changed
        return (netuid + 1) * R + block


def policy(tmp_path, *, bad=None, native=2):
    nodes = [Node(), Node(bad)]
    endpoints = {n: f"wss://{n}.example:443" for n in ("a", "b")}
    chain = AgreeingChain(
        {
            n: LiveRunnerChain(node, netuid=native, endpoint=endpoints[n])
            for n, node in zip(endpoints, nodes)
        },
        endpoints,
    )
    p = object.__new__(RuntimePolicy)
    p.collector = Collector(chain, tmp_path, native)
    p.migration = {"cutoff_day": 0, "cutoff_block": 24}
    p.manifest = {"sha256": "a" * 64}
    p.old = {}
    adapter = SimpleNamespace(netuid=native, _foreign_valuation_evidence=[])
    return p, adapter, nodes


def ledger(tmp_path):
    s = Store.create(tmp_path / "history.db", day_anchor_ts=ANCHOR)
    s.record_emission_start(block=1, block_timestamp=ANCHOR, block_hash=hash_of(1))
    for day in range(3):
        s.mark_day_scanned(day, day * 24 + 1, (day + 1) * 24)
        s.mark_day_sampled(day, stride=1, from_block=day * 24 + 1, to_block=(day + 1) * 24)
    return s


def holding(s, cold="alice", hotkeys=("hot-a", "hot-b")):
    s.record_holding_sample(
        coldkey=cold,
        block_timestamp=moment(52),
        block=52,
        block_hash=hash_of(52),
        total_rao=len(hotkeys),
        locked_rao=NOT_READ,
        available_rao=NOT_READ,
        positions=[
            dict(
                hotkey=h,
                shares=Share("u64f64", 1, 0, "v1"),
                denominator=Share("u64f64", 1, 0, "v1"),
                total_hotkey_alpha_rao=1,
            )
            for h in hotkeys
        ],
    )


def deposit(s, *, day=1, asset=3, amount=R, hot="hot-a", cold="alice", ordinal=1, attributed=True):
    block = day * 24 + 3
    s.record_foreign_deposit(
        coldkey=cold,
        block_timestamp=moment(block),
        amount_rao=amount,
        token_netuid=asset,
        block=block,
        event_ordinal=ordinal,
        extrinsic_index=1,
        block_hash=hash_of(block),
        hotkey="validator",
        owner_at_deposit=cold,
        origin_hotkey=hot,
        unattributed_reason=None if attributed else "wrong-validator",
    )
    return (amount, asset, block, hash_of(block))


def native_deposit(s, *, day=1, hot="old-hot", amount=1):
    block = day * 24 + 5
    s.record_noah_deposit(
        coldkey="alice",
        block_timestamp=moment(block),
        amount_rao=amount,
        block=block,
        event_ordinal=100,
        extrinsic_index=1,
        block_hash=hash_of(block),
        carried_by="StakeTransferred",
        hotkey="validator",
        owner_at_deposit="alice",
        origin_hotkey=hot,
    )


def build(s, p, a, *, batch=True, **kw):
    kw.setdefault("registration_days", {"alice": {"hot-a": 0, "hot-b": 0}})
    return build_fixture(
        s,
        "precollection",
        2,
        ANCHOR,
        DIALS,
        kw.pop("seats", {"alice": {"hot-a": 0, "hot-b": 1}}),
        foreign_rule=p.foreign_rule(a, None),
        foreign_without_noah_from_day=1,
        foreign_precollect=p.precollect_foreign if batch else None,
        **kw,
    )


@pytest.mark.parametrize("count", [1, 8, 40])
@pytest.mark.parametrize("native", [2, 82])
def test_actual_runtime_store_fixture_equals_scalar_all_assets(tmp_path, count, native):
    s = ledger(tmp_path)
    holding(s)
    for i in range(count):
        deposit(s, asset=100 + i, ordinal=i)
    # Identical contributions at distinct ordinals both remain real credit.
    deposit(s, asset=100, ordinal=count + 1)
    deposit(s, day=2, asset=0, ordinal=1)
    deposit(s, day=2, asset=native, ordinal=2)
    scalar, a, _ = policy(tmp_path / "scalar", native=native)
    grouped, b, nodes = policy(tmp_path / "grouped", native=native)
    expected = build(s, scalar, a, batch=False)
    actual = build(s, grouped, b)
    assert actual == expected
    assert b._foreign_valuation_evidence == a._foreign_valuation_evidence
    assert len(b._foreign_valuation_evidence) == count + 3
    assert json.loads(json.dumps(actual)) == actual  # no opaque value escaped
    assert {p.name: p.read_bytes() for p in (tmp_path / "scalar").glob("*.json")} == {
        p.name: p.read_bytes() for p in (tmp_path / "grouped").glob("*.json")
    }
    assert all(node.prices for node in nodes)
    s.close()


def test_only_accepted_claimed_rows_add_price_dependencies(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    deposit(s, asset=3)
    deposit(s, asset=999, cold="held-out", ordinal=2)
    deposit(s, asset=998, cold="missing-holding", ordinal=3)
    deposit(s, asset=997, cold="invalid-entry", ordinal=4)
    deposit(s, asset=996, attributed=False, ordinal=5)
    # A future registered sibling has no claim to the earlier foreign receipt.
    deposit(s, asset=4, hot="hot-b", day=2, ordinal=1)
    grouped, a, nodes = policy(tmp_path / "batch", bad=lambda b, n: 0 if n >= 996 else None)
    scalar, b, _ = policy(tmp_path / "scalar", bad=lambda b, n: 0 if n >= 996 else None)
    args = dict(
        held_out=("held-out", "invalid-entry"),
        seats={"alice": {"hot-a": 0, "hot-b": 1}, "missing-holding": {"hot-a": 2}},
        registration_days={"alice": {"hot-a": 0, "hot-b": 2}},
    )
    assert build(s, grouped, a, **args) == build(s, scalar, b, batch=False, **args)
    assert {row["token_netuid"] for row in a._foreign_valuation_evidence} == {3, 4}
    assert not any(n >= 996 for node in nodes for _, n in node.prices)
    s.close()


@pytest.mark.parametrize("seats", [{}, {"alice": {"hot-a": 0, "hot-b": 1}}])
def test_vacated_null_row_and_current_siblings_match_scalar(tmp_path, seats):
    s = ledger(tmp_path)
    holding(s)
    native_deposit(s)
    for i, hot in enumerate(("old-hot", "hot-a", "hot-b")):
        deposit(s, hot=hot, asset=3 + i, ordinal=i)
    grouped, a, _ = policy(tmp_path / "batch")
    scalar, b, _ = policy(tmp_path / "scalar")
    expected = build(s, scalar, b, batch=False, seats=seats)
    assert build(s, grouped, a, seats=seats) == expected
    assert a._foreign_valuation_evidence == b._foreign_valuation_evidence
    assert any(row["uid"] is None for row in expected["miners"])
    assert len(a._foreign_valuation_evidence) == 3
    s.close()


def test_frozen_block_and_legacy_native_gate_not_repriced(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    old = deposit(s, day=0, asset=999)
    new = deposit(s, day=1, asset=3, ordinal=2)
    p, a, nodes = policy(tmp_path / "prices", bad=lambda b, n: 0 if n == 999 else None)
    p.old[(old[2], old[3], old[1], old[0])] = 123
    # Without native contribution on day0 the old gate excludes even the
    # frozen valuation; new foreign-only day1 is eligible without native.
    value = build(s, p, a)
    assert value["miners"][0]["days"]["0"]["foreignDepositNoahEquivRao"] == "0"
    assert len(a._foreign_valuation_evidence) == 1
    assert not any(n == 999 for node in nodes for _, n in node.prices)
    # Even an explicit precollection request at/before cutoff skips pricing.
    p.precollect_foreign([old, new])
    s.close()


@pytest.mark.parametrize("bad", [0, -1, True])
def test_invalid_reader_price_refuses_entire_group_and_eval_day(tmp_path, bad):
    s = ledger(tmp_path)
    holding(s)
    deposit(s, asset=3)
    deposit(s, asset=4, ordinal=2)
    p, a, _ = policy(tmp_path / "prices", bad=lambda b, n: bad if n == 4 and b == 10 else None)
    with pytest.raises(ForeignDepositUnpriced) as exc:
        build(s, p, a)
    assert exc.value.day == 2
    assert not list((tmp_path / "prices").glob("*.json"))
    assert a._foreign_valuation_evidence == []
    s.close()


def test_source_disagreement_is_unsafe_not_retryable_price_refusal(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    deposit(s)
    p, a, _ = policy(tmp_path / "prices", bad=lambda b, n: 10 * R if n == 3 and b == 10 else None)
    with pytest.raises(SourceDisagreement):
        build(s, p, a)
    assert not list((tmp_path / "prices").glob("*.json"))
    s.close()


def test_receipt_hash_mismatch_authenticates_all_before_any_price_work(tmp_path):
    p, _, nodes = policy(tmp_path / "prices")
    with pytest.raises(Refusal, match="receipt block hash"):
        p.precollect_foreign([(R, 3, 27, hash_of(27)), (R, 4, 51, hash_of(50))])
    assert not list((tmp_path / "prices").glob("*.json"))
    assert all(node.prices == [] for node in nodes)


def test_structural_failure_precedes_new_price_work(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    deposit(s)
    p, a, nodes = policy(tmp_path / "prices")
    # Late invalid entry is a whole-fixture structural refusal. Deferred
    # prospective valuation has not run; no zero placeholder is published.
    with pytest.raises(MissingFromStore):
        build(s, p, a, entry_deposits={"unanchored": {"hot-z": 1}})
    assert all(node.prices == [] for node in nodes)
    assert not list((tmp_path / "prices").glob("*.json"))
    s.close()


def test_frozen_value_and_prospective_evidence_order_match_scalar(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    native_deposit(s, day=0, hot="hot-a")
    old = deposit(s, day=0, asset=999)
    deposit(s, day=1, asset=4, hot="hot-b", ordinal=2)
    deposit(s, day=1, asset=3, hot="hot-a", ordinal=3)
    grouped, a, nodes = policy(tmp_path / "batch", bad=lambda b, n: 0 if n == 999 else None)
    scalar, b, _ = policy(tmp_path / "scalar", bad=lambda b, n: 0 if n == 999 else None)
    for p in (grouped, scalar):
        p.old[(old[2], old[3], old[1], old[0])] = 123
    actual = build(s, grouped, a)
    assert actual == build(s, scalar, b, batch=False)
    assert actual["miners"][0]["days"]["0"]["foreignDepositNoahEquivRao"] == "123"
    assert a._foreign_valuation_evidence == b._foreign_valuation_evidence
    assert [v["token_netuid"] for v in a._foreign_valuation_evidence] == [3, 4]
    assert not any(n == 999 for node in nodes for _, n in node.prices)
    s.close()


def test_zero_receipt_retains_original_rejected_credit_and_price_dependency(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    deposit(s, amount=0)
    grouped, a, nodes = policy(tmp_path / "batch")
    scalar, b, _ = policy(tmp_path / "scalar")
    assert build(s, grouped, a) == build(s, scalar, b, batch=False)
    assert a._foreign_valuation_evidence == b._foreign_valuation_evidence
    assert a._foreign_valuation_evidence[0]["status"] == "rejected"
    assert a._foreign_valuation_evidence[0]["credit_rao"] == 0
    assert all(any(n == 3 for _, n in node.prices) for node in nodes)
    s.close()


def test_lock_holdout_does_not_add_dependencies(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    deposit(s)
    deposit(s, cold="locked-unread", asset=999, ordinal=2)
    s.record_attributed_pay(
        coldkey="locked-unread",
        hotkey="hot-z",
        uid=3,
        block_timestamp=moment(28),
        amount_rao=1,
        block=28,
        block_hash=hash_of(28),
    )
    # Deliberately no matching locked-pay observation: this coldkey is held out.
    grouped, a, nodes = policy(tmp_path / "batch", bad=lambda b, n: 0 if n == 999 else None)
    scalar, b, _ = policy(tmp_path / "scalar", bad=lambda b, n: 0 if n == 999 else None)
    seats = {"alice": {"hot-a": 0, "hot-b": 1}, "locked-unread": {"hot-z": 3}}
    assert build(s, grouped, a, seats=seats) == build(s, scalar, b, batch=False, seats=seats)
    assert not any(n == 999 for node in nodes for _, n in node.prices)
    s.close()


def test_source_hash_changes_partway_through_group_refuses(tmp_path):
    p, _, nodes = policy(tmp_path / "prices")
    original = nodes[1].get_block_hash

    def changed(block):
        if len(nodes[1].prices) > 15 and block == 10:
            return hash_of(10_000 + block)
        return original(block)

    nodes[1].get_block_hash = changed
    with pytest.raises(Refusal):
        p.precollect_foreign([(R, 3, 27, hash_of(27)), (R, 4, 27, hash_of(27))])
    assert not list((tmp_path / "prices").glob("*.json"))


def test_bad_value_returned_by_scalar_rule_never_escapes_deferred_field(tmp_path):
    s = ledger(tmp_path)
    holding(s)
    deposit(s)
    for bad in (True, -1, 1.5):
        with pytest.raises(Refusal):
            build_fixture(
                s,
                "bad-rule",
                2,
                ANCHOR,
                DIALS,
                {"alice": {"hot-a": 0}},
                foreign_rule=lambda *args: bad,
                foreign_without_noah_from_day=1,
                foreign_precollect=lambda receipts: None,
            )
    s.close()


def test_duplicate_midnight_timestamps_preserve_scalar_window_cache_keys(tmp_path):
    def prepare(root):
        p, a, nodes = policy(root)
        for node in nodes:
            query = node.query

            def duplicate(module, name, params, *, block_hash, _query=query):
                block = int(block_hash, 16)
                if block in (25, 26):
                    return moment(25) * 1000
                return _query(module, name, params, block_hash=block_hash)

            node.query = duplicate
        return p, a

    scalar, a = prepare(tmp_path / "scalar")
    grouped, b = prepare(tmp_path / "grouped")
    receipts = [(R, 3, 25, hash_of(25)), (R, 3, 26, hash_of(26)), (R, 4, 26, hash_of(26))]
    expected = [scalar.foreign_rule(a, None)(*row) for row in receipts]
    grouped.precollect_foreign(receipts)
    actual = [grouped.foreign_rule(b, None)(*row) for row in receipts]
    assert actual == expected
    assert a._foreign_valuation_evidence == b._foreign_valuation_evidence
    old = {p.name: p.read_bytes() for p in (tmp_path / "scalar").glob("*.json")}
    new = {p.name: p.read_bytes() for p in (tmp_path / "grouped").glob("*.json")}
    assert old == new and len(old) == 3


@pytest.mark.parametrize("unsafe", ["structural", "source-disagreement", "planned-window", "group-window"])
def test_actual_deferred_failure_revokes_prior_outage_refresh_authority(
    tmp_path, monkeypatch, unsafe
):
    from pathlib import Path
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.points.outage import authorize, record_failure
    from noah.points.tests.test_outage import setup
    from noah.starter.runtime import republish

    authority, transport, _, source, now = setup(tmp_path / "authority", monkeypatch)
    data = tmp_path / "ledger"
    data.mkdir()
    s = ledger(data)
    holding(s)
    deposit(s)
    p, a, nodes = policy(tmp_path / "unavailable")
    for node in nodes:
        original = node.runtime_call

        def unavailable(api, method, params, *, block_hash, _original=original):
            if params[0][0] == 3:
                raise RuntimeError("synthetic unavailable historical state")
            return _original(api, method, params, block_hash=block_hash)

        node.runtime_call = unavailable
    with pytest.raises(ForeignDepositUnpriced) as missing:
        build(s, p, a)
    assert missing.value.day == 2
    assert record_failure(authority, missing.value) is True
    assert authorize(authority, source, now[0]) is not None
    marker = Path(authority._submitted_record_dir) / "points-outage.json"
    assert json.loads(marker.read_text())["reason"] == "ForeignDepositUnpriced"

    if unsafe == "structural":
        # The previous price failure still exists, but accepted-row assembly
        # now discovers a real structural refusal before precollection.
        with pytest.raises(MissingFromStore) as failure:
            build(s, p, a, entry_deposits={"unanchored": {"hot-z": 1}})
    elif unsafe == "source-disagreement":
        p, a, _ = policy(
            tmp_path / "disagreed", bad=lambda b, n: 10 * R if n == 3 and b == 10 else None
        )
        with pytest.raises(SourceDisagreement) as failure:
            build(s, p, a)
    else:
        p, a, _ = policy(tmp_path / "contradictory-window")
        original_window = p.collector._window
        calls = 0

        def contradictory_window(block):
            nonlocal calls
            calls += 1
            window = original_window(block)
            if calls > 1:
                window[-1] = "contradictory-finalized-hash"
            return window

        p.collector._window = contradictory_window
        with pytest.raises(SourceLied) as failure:
            if unsafe == "planned-window":
                # Normal deferred fixture path authenticates then pins its
                # computed window before the Collector independently rereads.
                build(s, p, a)
            else:
                # The standalone grouped API also refuses a contradiction
                # between assets, even when no planned window was supplied.
                p.collector.precollect(50, [3, 4])
        assert not list(p.collector.directory.glob("*.json"))
    assert not record_failure(authority, failure.value)
    assert json.loads(marker.read_text())["reason"] == "UnsafeFailure"
    before = len(transport.calls)
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        authorize(authority, source, now[0])
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        republish(authority, 560, 560)
    assert len(transport.calls) == before
    # A subsequent partial read cannot undo an unresolved contradiction.
    assert record_failure(authority, missing.value) is False
    assert json.loads(marker.read_text())["reason"] == "UnsafeFailure"
    s.close()
