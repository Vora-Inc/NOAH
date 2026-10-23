"""Offline end-to-end direct publication path, retained receipts and guards."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ..calculation_record import retain
from ..epoch_work import configure, maintain, republish
from ..publish import NotOnChain, PublicationRefused
from ..publish_intent import PublicationStatusUnknown
from ..settings import SettingsError
from .test_direct_runtime475 import VECTOR, Client, adapter, event


class EpochClient(Client):
    consensus = "Yuma"

    def __init__(self):
        super().__init__()
        self.blocks = {}
        self.nonce = 7
        self.last = 0
        self.epoch = 200
        self.lost_reply = False

    def query(self, key, params=None, block=None):
        if key == ("System", "Account"):
            return {"nonce": self.nonce}
        if key == ("System", "Events"):
            return self.blocks.get(block, {}).get(
                "events", [event("System", "ExtrinsicSuccess", {}, 0)]
            )
        if key[1] == "LastUpdate":
            return [self.last]
        if key[1] == "LastEpochBlock":
            return self.epoch
        if key[1] == "Weights":
            return self.blocks.get(block, {}).get("stored", self.stored)
        return super().query(key, params, block)

    def block_info(self, block):
        return SimpleNamespace(
            number=block,
            hash=f"0x{block:064x}",
            extrinsics=self.blocks.get(block, {}).get("extrinsics", []),
        )

    def submit_call(self, call, wallet, **kwargs):
        result = super().submit_call(call, wallet, **kwargs)
        inclusion = self.block + 1
        self.extrinsics[2]["nonce"] = self.nonce
        self.blocks[inclusion] = copy.deepcopy(
            dict(events=self.events, extrinsics=self.extrinsics, stored=self.stored)
        )
        self.nonce += 1
        self.last = inclusion
        self.finalized_head = inclusion + 1
        result.extrinsic_id = f"{inclusion}-2"
        result.block_hash = f"0x{inclusion:064x}"
        if self.lost_reply:
            raise TimeoutError("reply lost")
        return result


def ready(tmp_path):
    a, client = adapter(tmp_path, EpochClient())
    a.day_anchor_ts = 0
    a.runner_chain.genesis_hash = lambda: "genesis"
    a.runner_chain.block_timestamp = lambda h: 86400 + h
    a.runner_chain.seat_hotkeys = lambda h: {0: "validator", 3: "a", 4: "b", 5: "c"}
    a.owner_uids = lambda: {0}
    a._calculation_reference = retain(
        a._submitted_record_dir,
        fixture={"evalDay": 0},
        agreed_text=json.dumps(
            {"evalDay": 0, "weightsU16": {str(k): v for k, v in VECTOR.items()}}
        ),
        vector=VECTOR,
        context={
            "day": 0,
            "height": 99,
            "to_block": 99,
            "to_block_hash": f"0x{99:064x}",
            "genesis_hash": "genesis",
            "netuid": 82,
            "ark_address": a.ark_coldkey,
            "designated_validator_hotkey": a.designated_validator_hotkey,
            "scoring_dials": a.dials,
            "day_start_utc_unix": 0,
        },
    )
    a.publish_direct(VECTOR, 99)
    client.block = 201
    client.finalized_head = 201
    configure(a, SimpleNamespace(epoch_republication=True, epoch_collection=False))
    return a, client


def test_republish_same_vector_preserves_daily_receipt_and_retains_epoch_receipt(tmp_path):
    a, c = ready(tmp_path)
    p = tmp_path / "scores/day-0.submitted.json"
    before = p.read_bytes()
    assert republish(a, 200, 201) == "republished"
    assert len(c.calls) == 2
    assert p.read_bytes() == before
    refresh = json.loads(
        (tmp_path / "scores/epoch-publications/200/day-0.submitted.json").read_text()
    )
    assert refresh["direct_intent"]["refresh_epoch"] == 200
    assert refresh["submitted_weights_u16"] == {str(k): v for k, v in VECTOR.items()}
    assert refresh["applied_weights_readback"]["confirmed"] is True
    assert republish(a, 200, 203) == "already_published_this_epoch"
    assert len(c.calls) == 2


def test_lost_response_recovers_without_a_second_send(tmp_path):
    a, c = ready(tmp_path)
    c.lost_reply = True
    with pytest.raises(NotOnChain):
        republish(a, 200, 201)
    assert a._publish_intent.load() is not None
    c.lost_reply = False
    assert republish(a, 200, 203) == "already_published_this_epoch"
    assert a._publish_intent.load() is None
    assert len(c.calls) == 2


def test_midnight_does_not_extend_stale_daily_weights(tmp_path):
    a, c = ready(tmp_path)
    a.runner_chain.block_timestamp = lambda h: 172800 + h
    assert republish(a, 200, 201) == "awaiting_daily_publication"
    assert len(c.calls) == 1


def test_miner_replacement_refuses_even_if_same_uid(tmp_path):
    a, c = ready(tmp_path)
    a.runner_chain.seat_hotkeys = lambda h: {
        0: "validator",
        3: "a" if h == 99 else "replacement",
        4: "b",
        5: "c",
    }
    with pytest.raises(PublicationStatusUnknown, match="identities changed"):
        republish(a, 200, 201)
    assert len(c.calls) == 1


def test_changed_evidence_refuses_before_signing(tmp_path):
    a, c = ready(tmp_path)
    ref = a._calculation_reference
    (tmp_path / "scores" / ref["path"]).write_text("{}")
    with pytest.raises(SettingsError):
        republish(a, 200, 201)
    assert len(c.calls) == 1


def test_disabled_by_default_and_invalid_configuration_rejected():
    from neurons.tests.test_validator_entry import GOOD_ENV

    from ..settings import Settings

    settings = Settings.from_mapping(GOOD_ENV)
    a = SimpleNamespace()
    configure(a, settings)
    assert not a.epoch_collection and not a.epoch_republication
    with pytest.raises(SettingsError):
        Settings.from_mapping({**GOOD_ENV, "NOAH_EPOCH_COLLECTION": "yes"})
    with pytest.raises(SettingsError):
        Settings.from_mapping({**GOOD_ENV, "NOAH_EPOCH_REPUBLICATION": "true"})
    settings = Settings.from_mapping(
        {
            **GOOD_ENV,
            "NOAH_PUBLISH_MODE": "direct",
            "NOAH_EPOCH_COLLECTION": "true",
            "NOAH_EPOCH_REPUBLICATION": "true",
        }
    )
    configure(a, settings)
    assert a.epoch_collection and a.epoch_republication


def test_loop_maintenance_deduplicates_after_restart(tmp_path):
    a, c = ready(tmp_path)
    maintain(a, tmp_path / "loop.lock")
    assert len(c.calls) == 2
    maintain(a, tmp_path / "loop.lock")
    assert len(c.calls) == 2
    assert (
        json.loads((tmp_path / "scores/epoch-maintenance.json").read_text())["published_epoch"]
        == 200
    )


def test_publication_failure_does_not_stop_collection(tmp_path):
    a, c = ready(tmp_path)
    a.epoch_collection = True
    with patch("noah.core.epoch_work.republish", side_effect=PublicationStatusUnknown("blocked")):
        with patch(
            "noah.core.epoch_work.collect",
            return_value={"day": 1, "collected_through": 201, "target_head": 201},
        ) as collect:
            maintain(a, tmp_path / "loop.lock")
            collect.assert_called_once()
    assert len(c.calls) == 1


def test_hardware_is_never_requested_for_a_held_validator(tmp_path):
    a, c = ready(tmp_path)
    a._publish_hold = SimpleNamespace(raise_if_held=Mock(side_effect=SettingsError("held")))
    maintain(a, tmp_path / "loop.lock")
    assert len(c.calls) == 1


def test_rate_window_waits_before_daily_attempt_and_recovers(tmp_path):
    from ..loop import tick

    a, c = ready(tmp_path)
    a.rate_limit = lambda netuid: 20
    c.last = c.block - 1
    kwargs = dict(
        settle_blocks=1,
        state_path=tmp_path / "loop.json",
        lock_path=tmp_path / "loop.lock",
        last_attempted_height=99,
    )
    with patch("noah.core.loop.run_period") as daily:
        result, last = tick(a, **kwargs)
        assert result.outcome == "waiting" and last == 99
        daily.assert_not_called()
    assert not (tmp_path / "loop.json").exists()
    assert a._publish_intent.load() is None
    c.block += 20

    class ReachedDaily(Exception):
        pass

    with patch("noah.core.loop._day_zero_catch_up_height", side_effect=ReachedDaily):
        with pytest.raises(ReachedDaily):
            tick(a, **kwargs)
    assert len(c.calls) == 1


def test_unknown_epoch_attempt_is_not_resubmitted_on_later_epoch(tmp_path):
    a, c = ready(tmp_path)
    with patch.object(c, "submit_call", side_effect=TimeoutError("no result")) as send:
        maintain(a, tmp_path / "loop.lock")
        assert a._publish_intent.load() is not None
        c.epoch = 210
        c.block = c.finalized_head = 211
        maintain(a, tmp_path / "loop.lock")
        assert send.call_count == 1
    assert a._publish_intent.load() is not None


def test_receipt_write_failure_recovers_exact_refresh(tmp_path):
    a, c = ready(tmp_path)
    with patch.object(a, "_save_direct_confirmation", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            republish(a, 200, 201)
    assert a._publish_intent.load()["refresh_epoch"] == 200
    assert republish(a, 200, 203) == "already_published_this_epoch"
    assert len(c.calls) == 2


def test_changed_genesis_and_new_owner_refuse(tmp_path):
    a, c = ready(tmp_path)
    a.runner_chain.genesis_hash = lambda: "other-chain"
    with pytest.raises(PublicationStatusUnknown):
        republish(a, 200, 201)
    a.runner_chain.genesis_hash = lambda: "genesis"
    a.owner_uids = lambda: {3}
    with pytest.raises(PublicationStatusUnknown, match="owner seat"):
        republish(a, 200, 201)
    assert len(c.calls) == 1


def test_lock_prevents_collection_and_publication(tmp_path):
    from ..writer_lock import held

    a, c = ready(tmp_path)
    with held(tmp_path / "loop.lock"):
        maintain(a, tmp_path / "loop.lock")
    assert len(c.calls) == 1


def test_collection_chunks_persist_without_completing_open_day(tmp_path):
    from ...chain.tests.test_epoch_collection import FIRST, FinalizedChain
    from ...chain.tests.test_runner import ANCHOR, ARK, HOTKEY, NETUID, a_store
    from ..epoch_work import collect

    c = FinalizedChain(head=FIRST + 2000)
    c.block_timestamp = lambda b: ANCHOR + max(1, 86400 + (b - FIRST) * 12)
    c.endpoints = {"a": "a", "b": "b"}
    a = SimpleNamespace(
        runner_chain=c,
        day_anchor_ts=ANCHOR,
        netuid=NETUID,
        ark_coldkey=ARK,
        designated_validator_hotkey=HOTKEY,
        _store_factory=lambda: a_store(tmp_path),
        _holding_stride=100,
        _read_collateral=True,
    )
    with patch("noah.core.chain_adapter._confirm_ark_identity"):
        first = collect(a, c.head)
        second = collect(a, c.head)
    assert first["collected_through"] == FIRST + 1023
    assert second["collected_through"] == c.head
    assert len([x for x in c.asked if x[0] == "events"]) == 2001
    s = a_store(tmp_path)
    assert not s.day_was_scanned(1)
    s.close()


def test_changed_scoring_configuration_refuses_old_weights(tmp_path):
    a, c = ready(tmp_path)
    a.dials = {"targetFp": "1"}
    with pytest.raises(PublicationStatusUnknown, match="configuration changed"):
        republish(a, 200, 201)
    assert len(c.calls) == 1


def midnight_refresh(tmp_path, *, lost_response=False):
    a, c = ready(tmp_path)
    # Finalized head is before midnight at the last pre-send check; inclusion
    # crosses UTC midnight. Do not mock reconciliation or the publication guard.
    a.runner_chain.block_timestamp = lambda h: 86400 + h if h <= 201 else 172800 + h - 202
    c.lost_reply = lost_response
    if lost_response:
        with pytest.raises(NotOnChain):
            republish(a, 200, 201)
    else:
        assert republish(a, 200, 201) == "republished"
    c.lost_reply = False
    c.block = c.finalized_head
    a._day_window = lambda h: (1, 100, 201)
    return a, c


@pytest.mark.parametrize("lost_response", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_midnight_inclusion_allows_fresh_daily_once(tmp_path, lost_response, restart):
    a, c = midnight_refresh(tmp_path, lost_response=lost_response)
    if restart:
        previous = a
        a, _ = adapter(tmp_path, c)
        a.runner_chain = previous.runner_chain
        a._day_window = previous._day_window
        # Feature defaults off: recovery must still recognize persisted refreshes.
        assert not a.epoch_republication
    assert a._already_published_direct(201) is False
    assert a._publish_intent.load() is None
    assert len(c.calls) == 2
    assert (tmp_path / "scores/epoch-publications/200/day-0.submitted.json").exists()
    assert not (tmp_path / "scores/day-1.submitted.json").exists()
    # New daily publication uses a new retained calculation, not the refresh.
    a._calculation_reference = retain(
        a._submitted_record_dir,
        fixture={"evalDay": 1},
        agreed_text=json.dumps({"evalDay": 1, "weightsU16": {str(k): v for k, v in VECTOR.items()}}),
        vector=VECTOR,
        context={"day": 1, "height": 201, "netuid": 82, "genesis_hash": "genesis", "ark_address": a.ark_coldkey},
    )
    a.publish_direct(VECTOR, 201)
    assert len(c.calls) == 3
    assert a._already_published_direct(201) is True
    with pytest.raises(PublicationRefused, match="already confirmed"):
        a.publish_direct(VECTOR, 201)
    assert len(c.calls) == 3


@pytest.mark.parametrize("damage", [
    "missing_refresh", "missing_source", "changed_vector", "changed_identity",
    "changed_hash", "changed_calculation", "same_day", "other_mechanism",
    "second_transaction", "missing_event_index", "other_block",
])
def test_midnight_refresh_does_not_exempt_unknown_or_invalid_evidence(tmp_path, damage):
    a, c = midnight_refresh(tmp_path)
    path = tmp_path / "scores/epoch-publications/200/day-0.submitted.json"
    record = json.loads(path.read_text())
    marker = record["direct_intent"]
    if damage == "missing_refresh":
        path.unlink()
    elif damage == "missing_source":
        (tmp_path / "scores/day-0.submitted.json").unlink()
    elif damage == "changed_vector":
        marker["vector"]["3"] = 29999
    elif damage == "changed_identity":
        marker["hotkey"] = "other"
    elif damage == "changed_hash":
        marker["result"]["block_hash"] = "0xwrong"
    elif damage == "changed_calculation":
        (tmp_path / "scores" / marker["calculation_evidence"]["path"]).write_text("{}")
    elif damage == "same_day":
        marker["day"] = 1
    elif damage == "other_mechanism":
        c.blocks[202]["events"].append(event("SubtensorModule", "WeightsSet", [4178, 0], 3))
    elif damage == "second_transaction":
        c.blocks[202]["events"].append(event("SubtensorModule", "WeightsSet", [82, 0], 3))
    elif damage == "missing_event_index":
        c.blocks[202]["events"].append({"event": {
            "module_id": "SubtensorModule", "event_id": "WeightsSet", "attributes": [82, 0]
        }})
    elif damage == "other_block":
        c.blocks[203] = {"events": [event("SubtensorModule", "WeightsSet", [82, 0], 1)]}
    if path.exists():
        path.write_text(json.dumps(record))
    with pytest.raises(PublicationStatusUnknown, match="unattributed direct"):
        a._already_published_direct(201)
    assert len(c.calls) == 2
    assert not (tmp_path / "scores/day-1.submitted.json").exists()
