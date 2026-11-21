"""Dormant cutoff identities retain their own balances when they regain a UID."""

import copy
from types import SimpleNamespace

import pytest

from noah.chain.fixture_build import build_fixture
from noah.chain.store import Store
from noah.core.calculation_record import encoded, retain
from noah.points.engine import INPUT_SCHEMA
from noah.points.lineages import capture, validate_document
from noah.points.prepare import build
from noah.points.run import agreed
from noah.points.runtime import RuntimePolicy
from noah.points.tests.test_engine import R, fixture
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture
from noah.starter.allocation import digest
from noah.starter.migration import FrozenEntries

COLD_A, COLD_B = "5" + "A" * 47, "5" + "B" * 47
HOT_A, HOT_B = "5" + "C" * 47, "5" + "D" * 47
GENESIS, HASH = "0x" + "1" * 64, "0x" + "2" * 64
PRICING = dict(
    method="previous-completed-utc-day-average",
    assets="all-subnet-tokens-and-root",
    credit="actual-received",
)


def prepared(
    tmp_path, *, prepaid=False, entry=False, foreign_only=False, active_b=False, empty_b=False
):
    store = Store.create(tmp_path / "history.db", day_anchor_ts=0)
    store.record_emission_start(block=1, block_timestamp=1, block_hash=HASH)
    for uid, cold, hot, amount in [
        (1, COLD_A, HOT_A, 2 * R),
        (2, COLD_B, HOT_B, 0 if foreign_only or empty_b else 100 * R),
    ]:
        store.record_attributed_pay(cold, hot, uid, 100, amount, 50, HASH)
        store.record_locked_pay(
            coldkey=cold,
            block=50,
            block_timestamp=100,
            locked_arrival_rao=0,
            unlocked_rao=0,
            block_hash=HASH,
        )

    def deposit(cold, hot, amount, block, day, ordinal):
        store.record_noah_deposit(
            coldkey=cold,
            block_timestamp=day * 86400 + 200,
            amount_rao=amount,
            block=block,
            event_ordinal=ordinal,
            extrinsic_index=1,
            block_hash=HASH,
            carried_by="StakeTransferred",
            hotkey="validator",
            origin_hotkey=hot,
            owner_at_deposit=cold,
        )

    deposit(COLD_A, HOT_A, R, 150, 1, 0)
    if not foreign_only and not empty_b:
        deposit(COLD_B, HOT_B, (75 if prepaid else 1) * R, 150, 1, 1)
    if entry:
        deposit(COLD_B, HOT_B, 10 * R, 60, 0, 0)
    if not empty_b:
        store.record_foreign_deposit(
            coldkey=COLD_B,
            block_timestamp=86600,
            amount_rao=50 * R,
            token_netuid=3,
            block=150,
            event_ordinal=2,
            extrinsic_index=1,
            block_hash=HASH,
            hotkey="validator",
            origin_hotkey=HOT_B,
            owner_at_deposit=COLD_B,
        )
    for day in range(2):
        store.mark_day_scanned(day, 1 + day * 100, (day + 1) * 100)
        store.mark_day_sampled(day, stride=100, from_block=1 + day * 100, to_block=(day + 1) * 100)
    payload = dict(
        schema="noah-frozen-entry/1",
        genesisHash=GENESIS,
        netuid=2,
        finalizedBlock=100,
        finalizedHash=HASH,
        asOfDay=0,
        legacyEntryRao=10 * R,
        records=[
            dict(
                coldkey=COLD_B,
                hotkey=HOT_B,
                registrationDay=0,
                amountRao=10 * R,
                allocation={"0": 10 * R},
            )
        ]
        if entry
        else (
            [dict(coldkey=COLD_B, hotkey=HOT_B, registrationDay=0, amountRao=0, allocation={})]
            if empty_b
            else []
        ),
    )
    snapshot = dict(payload=payload, sha256=digest(payload))
    registrations = {COLD_B: {HOT_B: 0}} if active_b else {COLD_A: {HOT_A: 0}}
    active_cold, active_hot = (COLD_B, HOT_B) if active_b else (COLD_A, HOT_A)
    seats = {active_cold: {active_hot: 1}}
    view = FrozenEntries(
        store,
        snapshot,
        genesis_hash=GENESIS,
        netuid=2,
        registration_days=registrations,
        as_of_day=1,
    )
    chain = SimpleNamespace(
        genesis_hash=lambda: GENESIS,
        head_block=lambda: 201,
        block_hash=lambda b: HASH,
        block_timestamp=lambda b: 172799 if b <= 200 else 172800,
        seat_hotkeys=lambda b: {1: active_hot},
        hotkey_owners=lambda keys, b: {HOT_A: COLD_A, HOT_B: COLD_B},
    )
    adapter = SimpleNamespace(
        runner_chain=chain,
        netuid=2,
        ark_coldkey="ark",
        designated_validator_hotkey="val",
        day_anchor_ts=0,
        _submitted_record_dir=tmp_path / "runtime",
        starter_entry_policy=SimpleNamespace(snapshot=snapshot),
    )
    dials = fixture([[(0, 0, 0)]])["dials"]
    # A zero holding is explicitly sampled for the active seat. It cannot
    # prevent accounting because the selected scoring profile has no hold gate.
    store.record_holding_sample(
        coldkey=active_cold,
        block=190,
        block_timestamp=86700,
        block_hash=HASH,
        total_rao=0,
        locked_rao=0,
        available_rao=0,
        positions=[],
    )
    source = build_fixture(
        view,
        "cutoff",
        1,
        0,
        dials,
        seats,
        foreign_rule=lambda *args: 50 * R,
        registration_days=registrations,
        entry_deposits=view.credited,
    )
    before = encoded(source)
    captured = capture(
        adapter,
        store,
        view,
        source,
        seats,
        {1: active_hot},
        lambda *args: 50 * R,
        view.credited,
        registrations,
        200,
    )
    assert encoded(source) == before
    legacy = score_fixture(parse_fixture(source))
    context = dict(
        day=1,
        height=200,
        genesis_hash=GENESIS,
        netuid=2,
        ark_address="ark",
        designated_validator_hotkey="val",
        source_agreement="required_by_ingestion",
        scored_hotkeys={"1": active_hot},
        to_block=200,
        to_block_hash=HASH,
        points_lineage_capture=captured,
        foreign_valuations=[]
        if foreign_only or empty_b
        else [
            dict(
                block=150,
                block_hash=HASH,
                token_netuid=3,
                amount_rao=str(50 * R),
                noah_equivalent_rao=str(50 * R),
            )
        ],
    )
    ref = retain(
        tmp_path / "evidence",
        fixture=source,
        agreed_text=encoded(legacy).decode(),
        vector={int(k): v for k, v in legacy["weightsU16"].items()},
        context=context,
    )
    policy = build(tmp_path / "evidence", ref, PRICING, ["150:2"])
    return adapter, policy, store, view


def returned(document, *, uid=99, deposit=0, hotkey=HOT_B, coldkey=COLD_B):
    raw = copy.deepcopy(document["fixture"])
    raw["evalDay"] = "2"
    saved = next(r["input"] for r in document["lineages"]["records"] if r["hotkey"] == HOT_B)
    row = copy.deepcopy(saved)
    row.update(uid=uid, coldkey=coldkey, registrationDay="2")
    row["days"]["2"] = dict(
        paidRao="0",
        paidLockedArrivalRao="0",
        payUnlockedRao="0",
        noahDepositRao=str(deposit),
        foreignDepositNoahEquivRao="0",
        noFault=False,
    )
    raw["miners"] = [row]
    return raw, {uid: hotkey}


@pytest.mark.parametrize("prepaid", [False, True])
@pytest.mark.parametrize("entry", [False, True])
def test_complete_dormant_lineage_reentry_keeps_exact_balance(tmp_path, prepaid, entry):
    adapter, document, store, view = prepared(tmp_path, prepaid=prepaid, entry=entry)
    assert len(document["migration"]["miners"]) == 1
    assert len(document["lineages"]["records"]) == 2
    assert all(r["input"]["uid"] is None for r in document["lineages"]["records"])
    policy = RuntimePolicy(document, adapter)
    expected = (25 if prepaid else -49) * R
    assert policy.baselines[COLD_B, HOT_B]["state"]["balance"] == expected
    assert policy.foreign_rule(adapter, None)(50 * R, 3, 150, HASH) == 50 * R
    raw, hotkeys = returned(document, deposit=3 * R)
    assert policy.check_snapshot(adapter, raw, hotkeys)
    raw.update(schema=INPUT_SCHEMA, pointsMigration=policy.fixture_migration(raw, hotkeys))
    output = agreed(raw)
    assert int(output["miners"][0]["state"]["balance"]) == expected + 3 * R
    assert int(output["miners"][0]["state"]["contributed"]) == ((75 if prepaid else 1) + 50 + 3) * R
    policy.persist()
    again = RuntimePolicy(document, adapter)
    assert again.fixture_migration(raw, hotkeys) == raw["pointsMigration"]
    assert agreed(raw) == output  # restart/replay never applies the bridge or receipt twice


@pytest.mark.parametrize(
    "damage",
    [
        "omit",
        "duplicate",
        "inventory",
        "history",
        "dormant-history",
        "combined-omit",
        "entry-pin",
        "owner",
        "hash",
    ],
)
def test_complete_lineage_rejects_missing_tampered_or_foreign_facts(tmp_path, damage):
    adapter, document, _, _ = prepared(tmp_path)
    if damage == "omit":
        document["lineages"]["records"].pop()
    elif damage == "duplicate":
        document["lineages"]["records"].append(document["lineages"]["records"][0])
    elif damage == "inventory":
        document["lineages"]["inventory"]["pairs"].pop()
    elif damage == "history":
        row = document["lineages"]["records"][0]
        row["input"]["days"]["1"]["noahDepositRao"] = "999"
    elif damage == "dormant-history":
        document["lineages"]["records"][1]["input"]["days"]["1"]["noahDepositRao"] = "999"
    elif damage == "combined-omit":
        document["lineages"]["records"].pop()
        document["lineages"]["inventory"]["pairs"].pop()
    elif damage == "entry-pin":
        document["lineages"]["entry_snapshot_sha256"] = "c" * 64
    elif damage == "owner":
        adapter.runner_chain.hotkey_owners = lambda keys, b: {HOT_A: COLD_A}
    else:
        document["lineages"]["inventory"]["pairs"][0]["block_hash"] = "wrong"
    with pytest.raises(ValueError):
        RuntimePolicy(document, adapter)


def test_new_owner_never_inherits_dormant_lineage(tmp_path):
    adapter, document, _, _ = prepared(tmp_path)
    policy = RuntimePolicy(document, adapter)
    raw, hotkeys = returned(document, coldkey="5" + "E" * 47)
    from noah.chain.sources import Refusal

    with pytest.raises(Refusal, match="lineage"):
        policy.check_snapshot(adapter, raw, hotkeys)
    assert policy.fixture_migration(raw, hotkeys)["balances"] == []


def test_source_inventory_receipt_replay_and_legacy_fixture_are_stable(tmp_path):
    _, document, store, _ = prepared(tmp_path)
    before = store.financial_lineage_inventory(1)
    store.record_attributed_pay(COLD_B, HOT_B, 2, 100, 100 * R, 50, HASH)
    assert store.financial_lineage_inventory(1) == before
    assert len(before["pairs"]) == 2
    assert before["counts"] == dict(earnings=2, native=2, foreign=1)
    assert validate_document(document, netuid=2)[COLD_B, HOT_B]["legacy_debt_rao"] == 49 * R


def test_dormant_foreign_only_keeps_original_native_gate(tmp_path):
    adapter, document, _, _ = prepared(tmp_path, foreign_only=True)
    policy = RuntimePolicy(document, adapter)
    baseline = policy.baselines[COLD_B, HOT_B]
    assert baseline["state"]["balance"] == baseline["state"]["contributed"] == 0
    assert document["lineages"]["foreign_valuations"] == []
    raw, hotkeys = returned(document, deposit=3 * R)
    assert policy.check_snapshot(adapter, raw, hotkeys)
    raw.update(schema=INPUT_SCHEMA, pointsMigration=policy.fixture_migration(raw, hotkeys))
    assert int(agreed(raw)["miners"][0]["state"]["contributed"]) == 3 * R


def test_entry_population_cannot_be_rewritten_even_with_recomputed_digest(tmp_path):
    adapter, document, _, _ = prepared(tmp_path)
    document["lineages"]["entry_pairs"].append(dict(coldkey=COLD_B, hotkey=HOT_B))
    from noah.points.lineages import digest

    document["lineages"]["source_sha256"] = digest(
        {k: v for k, v in document["lineages"].items() if k != "source_sha256"}
    )
    with pytest.raises(ValueError, match="entry identities"):
        RuntimePolicy(document, adapter)


def test_shared_coldkey_preserves_separate_hotkey_balances(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys.modules[__name__], "COLD_B", COLD_A)
    adapter, document, _, _ = prepared(tmp_path)
    policy = RuntimePolicy(document, adapter)
    assert policy.baselines[COLD_A, HOT_A]["state"]["balance"] == 0
    assert policy.baselines[COLD_A, HOT_B]["state"]["balance"] == -49 * R
    raw, hotkeys = returned(document, deposit=3 * R, coldkey=COLD_A)
    assert policy.check_snapshot(adapter, raw, hotkeys)
    raw.update(schema=INPUT_SCHEMA, pointsMigration=policy.fixture_migration(raw, hotkeys))
    assert int(agreed(raw)["miners"][0]["state"]["balance"]) == -46 * R
    from noah.chain.sources import Refusal

    with pytest.raises(Refusal, match="lineage"):
        policy.check_snapshot(adapter, raw, {99: "5" + "F" * 47})


@pytest.mark.parametrize("active_at_cutoff,empty_b", [(False, False), (True, False), (False, True)])
def test_real_reader_reentry_preserves_entry_exclusion_and_balance(
    tmp_path, active_at_cutoff, empty_b
):
    adapter, document, store, _ = prepared(
        tmp_path, entry=not empty_b, active_b=active_at_cutoff, empty_b=empty_b
    )
    policy = RuntimePolicy(document, adapter)
    store.record_noah_deposit(
        coldkey=COLD_B,
        block_timestamp=172900,
        amount_rao=3 * R,
        block=250,
        event_ordinal=0,
        extrinsic_index=1,
        block_hash=HASH,
        carried_by="StakeTransferred",
        hotkey="validator",
        origin_hotkey=HOT_B,
        owner_at_deposit=COLD_B,
    )
    store.mark_day_scanned(2, 201, 300)
    store.record_holding_sample(
        coldkey=COLD_B,
        block=290,
        block_timestamp=172999,
        block_hash=HASH,
        total_rao=0,
        locked_rao=0,
        available_rao=0,
        positions=[],
    )
    store.mark_day_sampled(2, stride=100, from_block=201, to_block=300)
    registrations = {COLD_B: {HOT_B: 2}}
    view = FrozenEntries(
        store,
        adapter.starter_entry_policy.snapshot,
        genesis_hash=GENESIS,
        netuid=2,
        registration_days=registrations,
        as_of_day=2,
    )
    assert view.credited == {}
    raw = build_fixture(
        view,
        "real-return",
        2,
        0,
        document["fixture"]["dials"],
        {COLD_B: {HOT_B: 99}},
        foreign_rule=policy.foreign_rule(adapter, None),
        foreign_without_noah_from_day=2,
        registration_days=registrations,
        entry_deposits=view.credited,
    )
    current = next(row for row in raw["miners"] if row["uid"] == 99)
    assert current["registrationDay"] == "2" and current["entryDepositRao"] == "0"
    if not empty_b:
        assert current["days"]["0"]["noahDepositRao"] == "0"  # frozen10 remains consumed
    else:
        assert current["firstActivityDay"] == "2"
    assert policy.check_snapshot(adapter, raw, {99: HOT_B})
    raw.update(schema=INPUT_SCHEMA, pointsMigration=policy.fixture_migration(raw, {99: HOT_B}))
    expected = policy.baselines[COLD_B, HOT_B]["state"]["balance"] + 3 * R
    assert int(agreed(raw)["miners"][0]["state"]["balance"]) == expected
    if empty_b:
        assert raw["pointsMigration"]["balances"] == []
        assert int(agreed(raw)["miners"][0]["state"]["contributed"]) == 3 * R


@pytest.mark.parametrize(
    "field,value",
    [
        ("records", None),
        ("inventory", []),
        ("entry_pairs", None),
        ("entry_snapshot_sha256", False),
        ("netuid", True),
        ("cutoff_day", True),
        ("foreign_valuations", {}),
        ("records", [{}]),
        ("entry_pairs", [{}]),
    ],
)
def test_malformed_capture_is_a_closed_validation_error(tmp_path, field, value):
    _, document, _, _ = prepared(tmp_path)
    document["lineages"][field] = value
    from noah.points.lineages import digest

    document["lineages"]["source_sha256"] = digest(
        {k: v for k, v in document["lineages"].items() if k != "source_sha256"}
    )
    with pytest.raises(ValueError):
        validate_document(document, netuid=2)
