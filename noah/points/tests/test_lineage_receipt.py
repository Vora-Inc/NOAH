"""Position-owner receipt proofs preserve balances without inventing hotkey ownership."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from noah.chain.agreeing import AgreeingChain
from noah.chain.sources import Refusal, SourceDisagreement
from noah.chain.ss58 import address_of
from noah.chain.store import Store
from noah.points.lineage_receipt import verify
from noah.points.lineages import digest, validate_document
from noah.points.run import agreed
from noah.points.runtime import RuntimePolicy
from noah.points.tests._lineage_capture import document

COLD, HOT, ARK, DEST, OTHER, ACTIVE, ACTIVE_HOT = [address_of(bytes([n]) * 32) for n in range(1, 8)]
HASH = "0x" + "ab" * 32
AMOUNT = 428_688_688


def proof():
    return dict(
        kind="native-stake-and-hotkey-transfer",
        day=0,
        event_ordinal=136,
        extrinsic_index=2,
        amount_rao=str(AMOUNT),
        destination_hotkey=DEST,
    )


def events():
    return [
        dict(
            ordinal_in_block=135,
            extrinsic_idx=2,
            module_id="SubtensorModule",
            event_id="StakeAndHotkeyTransferred",
            attributes=dict(
                origin_coldkey=COLD,
                destination_coldkey=ARK,
                origin_hotkey=HOT,
                destination_hotkey=DEST,
                origin_netuid=2,
                destination_netuid=2,
                amount=123,
            ),
        ),
        dict(
            ordinal_in_block=136,
            extrinsic_idx=2,
            module_id="SubtensorModule",
            event_id="StakeAdded",
            attributes=[ARK, DEST, 1, AMOUNT, 2, 0],
        ),
    ]


class Chain:
    def __init__(self):
        self.rows = events()

    def head_block(self):
        return 101

    def genesis_hash(self):
        return "0x" + "11" * 32

    def block_hash(self, block):
        return HASH

    def block_timestamp(self, block):
        return 86400 if block == 101 else 86399 if block == 100 else 100

    def seat_hotkeys(self, block):
        return {0: ACTIVE_HOT}

    def hotkey_owners(self, keys, block):
        return {ACTIVE_HOT: ACTIVE, HOT: OTHER}

    def owner_at(self, **kwargs):
        raise AssertionError("position ownership is not global hotkey ownership")

    def events(self, block):
        assert block == 50
        return copy.deepcopy(self.rows)

    def extrinsics(self, block):
        assert block == 50
        return []


def policy():
    doc = document(netuid=2)
    # Public synthetic identities only; the amount and event shape reproduce
    # the actual dormant partial contribution, with no invented registration.
    doc = json.loads(
        json.dumps(doc).replace("5" + "A" * 47, ACTIVE).replace("5" + "B" * 47, ACTIVE_HOT)
    )
    doc.update(ark_address=ARK, validator_hotkey=DEST)
    doc["migration"]["source_sha256"] = hashlib.sha256(
        json.dumps(doc["fixture"], sort_keys=True).encode()
    ).hexdigest()
    row = copy.deepcopy(doc["fixture"]["miners"][0])
    row.update(uid=None, coldkey=COLD, holdRao="0")
    row["days"]["0"]["noahDepositRao"] = str(AMOUNT)
    doc["lineages"]["records"].append(dict(coldkey=COLD, hotkey=HOT, input=row))
    anchor = dict(coldkey=COLD, hotkey=HOT, first_day=0, block=50, block_hash=HASH, receipt=proof())
    doc["lineages"]["inventory"]["pairs"].append(anchor)
    doc["lineages"]["inventory"]["counts"]["native"] = 1
    doc["migration"]["consumed_receipt_ids"] = [f"{doc['migration']['genesis']}/{HASH}/136"]
    seal(doc)
    return doc, anchor


def seal(doc):
    doc["lineages"]["source_sha256"] = digest(
        {k: v for k, v in doc["lineages"].items() if k != "source_sha256"}
    )


def test_runtime_accepts_exact_position_receipt_despite_different_hotkey_owner(tmp_path):
    doc, anchor = policy()
    adapter = SimpleNamespace(
        runner_chain=Chain(),
        netuid=2,
        ark_coldkey=ARK,
        designated_validator_hotkey=DEST,
        day_anchor_ts=0,
        _submitted_record_dir=tmp_path,
    )
    before = copy.deepcopy(doc)
    runtime = RuntimePolicy(doc, adapter)
    saved = runtime.baselines[COLD, HOT]
    assert saved["state"]["balance"] == saved["state"]["contributed"] == AMOUNT
    assert saved["legacy_debt_rao"] == 0 and saved["input"]["entryDepositRao"] == "0"
    assert doc == before and not runtime.path.exists()
    # Re-entry on another UID carries the old partial contribution once. It
    # neither invents an entry allocation nor pays the receipt a second time.
    future = copy.deepcopy(doc["fixture"])
    returned = copy.deepcopy(saved["input"])
    returned.update(uid=9, registrationDay="1")
    returned["days"]["1"] = {k: "0" for k in returned["days"]["0"] if k != "noFault"}
    returned["days"]["1"]["noFault"] = False
    future.update(evalDay="1", miners=[returned])
    runtime.check_snapshot(adapter, future, {9: HOT})
    future.update(
        schema="noah-points-fixture/1", pointsMigration=runtime.fixture_migration(future, {9: HOT})
    )
    output = agreed(future)
    assert int(output["miners"][0]["state"]["balance"]) == AMOUNT
    assert int(output["miners"][0]["state"]["contributed"]) == AMOUNT
    # Without this exact authenticated proof, the old owner guard still refuses.
    del anchor["receipt"]
    seal(doc)
    with pytest.raises(ValueError, match="historical lineage attribution"):
        RuntimePolicy(doc, adapter)


def test_store_attaches_proof_only_to_selected_native_position_transfer_anchor(tmp_path):
    store = Store.create(tmp_path / "history.db", day_anchor_ts=0)
    store.record_noah_deposit(
        coldkey=COLD,
        block_timestamp=100,
        amount_rao=AMOUNT,
        block=50,
        event_ordinal=136,
        extrinsic_index=2,
        block_hash=HASH,
        carried_by="StakeAndHotkeyTransferred",
        hotkey=DEST,
        origin_hotkey=HOT,
        owner_at_deposit=COLD,
    )
    store.mark_day_scanned(0, 1, 100)
    got = store.financial_lineage_inventory(0)
    assert got["pairs"][0]["receipt"] == proof()
    # A later earnings anchor continues to require the existing ownership proof.
    store.record_attributed_pay(COLD, HOT, 1, 101, 10, 60, HASH)
    assert "receipt" not in store.financial_lineage_inventory(0)["pairs"][0]
    store.close()


@pytest.mark.parametrize(
    "damage",
    [
        "cold",
        "hot",
        "ark",
        "destination",
        "asset",
        "amount",
        "ordinal",
        "index",
        "hash",
        "day",
        "type",
        "unconsumed",
        "unreadable",
        "reorg",
    ],
)
def test_exact_receipt_mismatch_refuses(damage):
    doc, anchor = policy()
    chain = Chain()
    if damage == "cold":
        chain.rows[0]["attributes"]["origin_coldkey"] = OTHER
    elif damage == "hot":
        chain.rows[0]["attributes"]["origin_hotkey"] = OTHER
    elif damage == "ark":
        chain.rows[0]["attributes"]["destination_coldkey"] = OTHER
        chain.rows[1]["attributes"][0] = OTHER
    elif damage == "destination":
        chain.rows[0]["attributes"]["destination_hotkey"] = OTHER
        chain.rows[1]["attributes"][1] = OTHER
    elif damage == "asset":
        chain.rows[0]["attributes"].update(origin_netuid=3, destination_netuid=3)
        chain.rows[1]["attributes"][4] = 3
    elif damage == "amount":
        chain.rows[1]["attributes"][3] += 1
    elif damage == "ordinal":
        chain.rows[1]["ordinal_in_block"] += 1
    elif damage == "index":
        for event in chain.rows:
            event["extrinsic_idx"] += 1
    elif damage == "hash":
        anchor["block_hash"] = "0x" + "cd" * 32
    elif damage == "day":
        chain.block_timestamp = lambda block: 86401
    elif damage == "type":
        chain.rows[0]["event_id"] = "StakeTransferred"
    elif damage == "unconsumed":
        doc["migration"]["consumed_receipt_ids"] = []
    elif damage == "unreadable":

        def fail(block):
            raise ValueError("unreadable historical body")

        chain.events = fail
    elif damage == "reorg":
        calls = []

        def changed(block):
            calls.append(block)
            return "0x" + "cd" * 32 if calls.count(50) > 1 else HASH

        chain.block_hash = changed
    with pytest.raises((ValueError, Refusal)):
        verify(chain, anchor, doc, netuid=2, day_anchor_ts=0)


@pytest.mark.parametrize(
    "key,value",
    [
        ("kind", "remark"),
        ("day", True),
        ("event_ordinal", -1),
        ("extrinsic_index", "2"),
        ("amount_rao", "0428688688"),
        ("amount_rao", "0"),
        ("destination_hotkey", "invalid"),
        ("destination_hotkey", OTHER),
        ("extra", 1),
    ],
)
def test_pure_recovery_validator_rejects_malformed_or_unbound_receipt(key, value):
    doc, anchor = policy()
    anchor["receipt"][key] = value
    seal(doc)
    with pytest.raises(ValueError):
        validate_document(doc, netuid=2)


def test_raw_proof_tamper_is_rejected_before_chain_reads():
    doc, anchor = policy()
    anchor["receipt"]["amount_rao"] = "1"
    with pytest.raises(ValueError, match="source digest"):
        validate_document(doc, netuid=2)


@pytest.mark.parametrize("field", ["events", "extrinsics", "hash", "timestamp"])
def test_two_readers_must_agree_on_exact_proof_inputs(field):
    doc, anchor = policy()
    left, right = Chain(), Chain()
    if field == "events":
        right.rows[1]["attributes"][3] += 1
    elif field == "extrinsics":
        right.extrinsics = lambda block: [{"different": True}]
    elif field == "hash":
        right.block_hash = lambda block: "0x" + "cd" * 32
    elif field == "timestamp":
        right.block_timestamp = lambda block: 101
    chain = AgreeingChain(
        {"left": left, "right": right},
        {"left": "ws://reader-a:9944", "right": "ws://reader-b:9944"},
        canary=lambda source, block: 1,
    )
    with pytest.raises(SourceDisagreement):
        verify(chain, anchor, doc, netuid=2, day_anchor_ts=0)


def test_position_proof_success_through_actual_agreement_wrapper():
    doc, anchor = policy()
    left, right = Chain(), Chain()
    chain = AgreeingChain(
        {"left": left, "right": right},
        {"left": "ws://reader-a:9944", "right": "ws://reader-b:9944"},
        canary=lambda source, block: 1,
    )
    verify(chain, anchor, doc, netuid=2, day_anchor_ts=0)
    assert chain.checks > 0


@pytest.mark.parametrize("timestamp", [True, "100", 100.0, 0, -1])
def test_receipt_timestamp_must_be_a_positive_decoded_integer(timestamp):
    doc, anchor = policy()
    chain = Chain()
    chain.block_timestamp = lambda block: timestamp
    with pytest.raises(ValueError, match="boundary"):
        verify(chain, anchor, doc, netuid=2, day_anchor_ts=0)


def test_matching_global_owner_does_not_bypass_explicit_receipt(tmp_path):
    doc, anchor = policy()
    chain = Chain()
    chain.hotkey_owners = lambda keys, block: {ACTIVE_HOT: ACTIVE, HOT: COLD}
    anchor["receipt"]["amount_rao"] = str(AMOUNT + 1)
    seal(doc)
    adapter = SimpleNamespace(
        runner_chain=chain,
        netuid=2,
        ark_coldkey=ARK,
        designated_validator_hotkey=DEST,
        day_anchor_ts=0,
        _submitted_record_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="finalized receipt"):
        RuntimePolicy(doc, adapter)
