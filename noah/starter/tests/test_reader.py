import json
import sqlite3
from types import SimpleNamespace

import pytest

from noah.chain.collection import fingerprint
from noah.chain.sources import Refusal
from noah.core.calculation_record import encoded, retain
from noah.starter.allocation import preview
from noah.starter.reader import current_input
from noah.starter.tests.test_epoch_clock import request_live


def prepared(tmp_path):
    raw = request_live()
    s = raw["snapshot"]
    anchor = int(raw["fixture"]["dayEpochTs"])
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE attributed_pay(coldkey,hotkey,amount_rao,block)")
    db.execute("CREATE TABLE epoch_collection(day,body)")
    db.execute(
        "INSERT INTO attributed_pay VALUES (?,?,?,?)", ("established", "hotkey-0", 20000000000, 98)
    )
    db.execute(
        "INSERT INTO attributed_pay VALUES (?,?,?,?)", ("departed", "retired", 5000000000, 98)
    )
    store = SimpleNamespace(
        _conn=db, close=lambda: None, scanned_days=lambda: [(0, 1, 49), (1, 50, 99)]
    )

    def ts(b):
        if b < 50:
            return anchor + 100
        if b < 100:
            return anchor + 86400 + 100
        return anchor + 172800 + (b - 100) * 12

    def info(b):
        return dict(
            consensus="Yuma",
            LastEpochBlock=200,
            LastMechansimStepBlock=200 if b >= 200 else 100,
            LastYumaStepBlock=200 if b >= 200 else 100,
            Tempo=360,
            PendingEpochAt=0,
            BlocksSinceLastStep=1,
            NetworkRegisteredAt=1,
        )

    chain = SimpleNamespace(
        genesis_hash=lambda: s["genesisHash"],
        block_timestamp=ts,
        block_hash=lambda b: f"0x{b:064x}",
        starter_epoch_info=info,
        mechanism_count=lambda b: 1,
        seat_hotkeys=lambda b: {0: "hotkey-0", 1: "hotkey-1"},
        subnetwork_n=lambda b: 2,
        hotkey_owners=lambda keys, b: {"hotkey-0": "established", "hotkey-1": "wallet-1"},
        incentive=lambda b: [65535, 0],
        events=lambda b: [
            dict(
                module_id="SubtensorModule",
                event_id="IncentiveAlphaEmittedToMiners",
                attributes={"netuid": 82, "emissions": [100000000000, 0]},
            )
        ],
    )
    a = SimpleNamespace(
        _submitted_record_dir=tmp_path,
        runner_chain=chain,
        netuid=82,
        day_anchor_ts=anchor,
        _store_factory=lambda: store,
        owner_uids=lambda: set(),
        ark_coldkey="ark",
        designated_validator_hotkey="validator",
        _holding_stride=100,
        _read_collateral=True,
        dials=raw["fixture"]["dials"],
    )
    settings = dict(
        version=1,
        genesis=s["genesisHash"],
        netuid=82,
        ark="ark",
        validator="validator",
        holding_stride=100,
        read_collateral=True,
        attribute_deposits=True,
    )
    cursor = dict(
        first=100,
        last=201,
        first_hash=chain.block_hash(100),
        last_hash=chain.block_hash(201),
        settings=fingerprint(settings),
    )
    db.execute("INSERT INTO epoch_collection VALUES (?,?)", (2, json.dumps(cursor)))
    score = preview(raw)["ordinaryOutput"]
    ref = retain(
        tmp_path,
        fixture=raw["fixture"],
        agreed_text=encoded(score).decode(),
        vector={int(k): v for k, v in score["weightsU16"].items()},
        context=dict(
            day=1,
            height=99,
            to_block=99,
            to_block_hash=chain.block_hash(99),
            genesis_hash=s["genesisHash"],
            netuid=82,
            ark_address="ark",
            designated_validator_hotkey="validator",
            scoring_dials=a.dials,
            day_start_utc_unix=anchor + 86400,
        ),
    )
    return a, ref, store, cursor


def test_reader_uses_real_epoch_mining_events_and_preserves_departed_wallet_history(tmp_path):
    a, ref, store, cursor = prepared(tmp_path)
    raw = current_input(a, ref, 201)
    assert raw["snapshot"]["miningPoolSample"]["amountRao"] == 100000000000
    assert raw["snapshot"]["nextEpochBlock"] == 560
    assert raw["snapshot"]["epochSeconds"] == 1200
    assert any(
        x["hotkey"] == "retired" and x["earnedRao"] == 5000000000 for x in raw["lifetimeEarnings"]
    )
    preview(raw)


@pytest.mark.parametrize(
    "field,value", [("last", 200), ("first", 101), ("last_hash", "wrong"), ("settings", "wrong")]
)
def test_reader_refuses_incomplete_or_mismatched_collection(tmp_path, field, value):
    a, ref, store, cursor = prepared(tmp_path)
    cursor[field] = value
    store._conn.execute("UPDATE epoch_collection SET body=?", (json.dumps(cursor),))
    with pytest.raises(Refusal, match="collection"):
        current_input(a, ref, 201)


def test_reader_does_not_treat_lost_history_as_a_new_allowance(tmp_path):
    a, ref, store, cursor = prepared(tmp_path)
    store.scanned_days = lambda: [(1, 50, 99)]
    with pytest.raises(Refusal, match="history"):
        current_input(a, ref, 201)


@pytest.mark.parametrize("last_yuma", [0, 100, 199, 201])
def test_reader_requires_actual_yuma_settlement_after_consensus_transition(tmp_path, last_yuma):
    a, ref, store, cursor = prepared(tmp_path)
    original = a.runner_chain.starter_epoch_info
    # Current consensus and the last general settlement both look healthy, but
    # Null produced the last payout (including a switch in its own block).
    a.runner_chain.starter_epoch_info = lambda block: {
        **original(block), "LastYumaStepBlock": last_yuma
    }
    with pytest.raises(Refusal, match="not Yuma"):
        current_input(a, ref, 201)


@pytest.mark.parametrize(
    "change", ["dials", "ark_coldkey", "designated_validator_hotkey", "day_anchor_ts"]
)
def test_reader_refuses_changed_scoring_configuration(tmp_path, change):
    a, ref, store, cursor = prepared(tmp_path)
    setattr(a, change, {} if change == "dials" else 0 if change == "day_anchor_ts" else "changed")
    with pytest.raises(Refusal, match="configuration"):
        current_input(a, ref, 201)
