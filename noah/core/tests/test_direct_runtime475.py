"""Offline production-route tests. No live keys, RPC, finality or payout claims."""

import copy
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from .. import direct
from ..proxy_route import CommitIdentity
from ..publish import CommitRejected, NotOnChain, PublicationRefused
from ..publish_intent import PublicationStatusUnknown
from ..reader import ChainReader, ChainUnreadable
from ..rescale import predict_stored
from ..startup import NoValidatorPermit, check_has_validator_permit
from ..submitter import Publisher
from ._adapter_helpers import an_adapter
from ._composed_call import ComposedCall, ComposesCalls, inner_call
from .test_proxy_route import SIGNER, VALIDATOR

VECTOR = {0: 0, 3: 30000, 4: 20000, 5: 15535}


def event(module, name, attrs, index=2):
    return {
        "extrinsic_idx": index,
        "event": {"module_id": module, "event_id": name, "attributes": attrs},
    }


def decoded(call):
    if isinstance(call, ComposedCall):
        call = call.original
    return {
        "call_module": call.module,
        "call_function": call.function,
        "call_args": [
            {"name": k, "value": decoded(v) if isinstance(v, ComposedCall) or hasattr(v, "module") else v}
            for k, v in call.params.items()
        ],
    }


class Client(ComposesCalls):
    block = 100
    finalized_head = 101
    consensus = "Null"
    permit = True

    def __init__(self):
        self.calls = []
        self.events = []
        self.extrinsics = []
        self.stored = []
        self.after_send = None

    def block_info(self, block):
        return SimpleNamespace(
            number=block, hash=f"0x{block:064x}", extrinsics=self.extrinsics if block == 101 else []
        )

    def query(self, key, params=None, block=None):
        if key == ("System", "Account"):
            return {"nonce": 7}
        if key == ("System", "Events"):
            return self.events if block == 101 else [event("System", "ExtrinsicSuccess", {}, 0)]
        return {
            "SubnetEpochConsensus": self.consensus,
            "Keys": VALIDATOR,
            "LastUpdate": [0],
            "WeightsSetRateLimit": 0,
            "Tempo": 10,
            "Weights": self.stored,
            "ValidatorPermit": [self.permit],
            "SubnetOwnerHotkey": VALIDATOR,
        }[key[1]]

    def submit_call(self, call, wallet, **kwargs):
        self.calls.append((call, wallet, kwargs))
        assert call.module == "Proxy" and wallet.ss58_address == SIGNER
        assert kwargs == {"wait_for_inclusion": True, "wait_for_finalization": True}
        inner = inner_call(call)
        self.stored = list(zip(inner.params["dests"], inner.params["weights"]))
        if self.consensus == "Yuma":
            self.stored = list(
                zip(inner.params["dests"], predict_stored(inner.params["weights"]).stored)
            )
        self.extrinsics = [
            {"address": "irrelevant", "call": {}},
            {"address": "irrelevant", "call": {}},
            {"address": SIGNER, "nonce": 7, "call": decoded(call)},
        ]
        self.events = [
            event("SubtensorModule", "WeightsSet", [82, 0]),
            event("Proxy", "ProxyExecuted", {"result": {"Ok": None}}),
            event("System", "ExtrinsicSuccess", {}),
        ]
        if self.after_send:
            self.after_send(self)
        return SimpleNamespace(
            error=None,
            success=True,
            extrinsic_id="101-2",
            block_hash=f"0x{101:064x}",
            events=self.events,
        )


def adapter(tmp_path, client=None):
    client = client or Client()
    publisher = Publisher(
        client,
        SimpleNamespace(ss58_address=SIGNER),
        netuid=82,
        commit_identity=CommitIdentity(VALIDATOR),
    )
    built, _, _ = an_adapter(
        reader=ChainReader(client),
        publisher=publisher,
        wallet_hotkey=CommitIdentity(VALIDATOR),
        netuid=82,
        publish_mode="direct",
        day_window=lambda h: (h // 100, 0, h),
        publish_intent_path=tmp_path / "intent.json",
        submitted_record_dir=tmp_path / "scores",
    )
    built.runner_chain.head_block = lambda: client.finalized_head
    built.runner_chain.block_hash = lambda block: f"0x{block:064x}"
    return built, client


def test_direct_proxy_null_confirms_exact_unchanged_vector_and_durable_record(tmp_path):
    built, client = adapter(tmp_path)
    result = built.publish_direct(VECTOR, 99)
    proof = result["applied_weights_readback"]
    assert proof["weights"] == VECTOR
    assert proof["consensus"] == "Null"
    assert proof["extrinsic_index"] == 2
    assert built._publish_intent.load() is None
    record = json.loads((tmp_path / "scores/day-0.submitted.json").read_text())
    assert record["direct_intent"]["vector"] == {str(k): v for k, v in VECTOR.items()}
    assert record["applied_weights_readback"]["confirmed"] is True
    rebuilt, _ = adapter(tmp_path, client)
    assert rebuilt.already_published(99)
    with pytest.raises(PublicationRefused):
        rebuilt.publish_direct(VECTOR, 99)
    assert len(client.calls) == 1


def test_yuma_uses_runtime_rounding(tmp_path):
    built, client = adapter(tmp_path)
    client.consensus = "Yuma"
    proof = built.publish_direct(VECTOR, 99)["applied_weights_readback"]
    assert list(proof["weights"].values()) == list(predict_stored(VECTOR.values()).stored)
    assert proof["weights"] != VECTOR


def test_lost_response_reconciles_actual_proxy_route_without_second_send(tmp_path):
    built, client = adapter(tmp_path)

    def lost_reply(client):
        raise TimeoutError("response lost after inclusion")

    client.after_send = lost_reply
    with pytest.raises(NotOnChain, match="unknown"):
        built.publish_direct(VECTOR, 99)
    assert built._publish_intent.load()["vector"] == {str(k): v for k, v in VECTOR.items()}
    rebuilt, _ = adapter(tmp_path, client)
    assert rebuilt.already_published(99)
    assert rebuilt._publish_intent.load() is None
    assert len(client.calls) == 1


def test_inner_composition_failure_never_submits_and_keeps_intent(tmp_path):
    built, client = adapter(tmp_path)
    with mock.patch.object(client, "compose", side_effect=ValueError("invalid RuntimeCall")):
        with pytest.raises(ValueError, match="invalid RuntimeCall"):
            built.publish_direct(VECTOR, 99)
    assert client.calls == []
    assert built._publish_intent.load()["vector"] == {str(k): v for k, v in VECTOR.items()}
    with pytest.raises(PublicationStatusUnknown):
        adapter(tmp_path, client)[0].publish_direct(VECTOR, 99)
    assert client.calls == []


def test_crash_after_send_before_local_response_record_recovers(tmp_path):
    built, client = adapter(tmp_path)
    with mock.patch.object(
        built._publish_intent, "annotate", side_effect=OSError("disk interrupted")
    ):
        with pytest.raises(OSError):
            built.publish_direct(VECTOR, 99)
    rebuilt, _ = adapter(tmp_path, client)
    assert rebuilt.already_published(99)
    assert len(client.calls) == 1


def test_record_write_failure_keeps_marker_and_recovers(tmp_path):
    built, client = adapter(tmp_path)
    with mock.patch.object(built, "_save_direct_confirmation", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            built.publish_direct(VECTOR, 99)
    assert built._publish_intent.load() is not None
    rebuilt, _ = adapter(tmp_path, client)
    assert rebuilt.already_published(99)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.events.__setitem__(0, event("SubtensorModule", "WeightsSet", [4178, 0])),
        lambda c: c.events.__setitem__(0, event("OtherModule", "WeightsSet", [82, 0])),
        lambda c: c.events.__setitem__(0, event("SubtensorModule", "WeightsSet", [82, 0], index=3)),
        lambda c: c.stored.append((99, 1)),
        lambda c: c.stored.__setitem__(1, (3, 65535)),
        lambda c: c.extrinsics[2].update(address=VALIDATOR),
        lambda c: c.extrinsics[2].update(nonce=8),
        lambda c: setattr(c, "finalized_head", 100),
        lambda c: setattr(c, "consensus", None),
    ],
)
def test_false_confirmations_remain_unknown_and_block_resend(tmp_path, change):
    built, client = adapter(tmp_path)
    client.after_send = change
    with pytest.raises(NotOnChain):
        built.publish_direct(VECTOR, 99)
    with pytest.raises(PublicationStatusUnknown):
        adapter(tmp_path, client)[0].publish_direct(VECTOR, 99)
    assert len(client.calls) == 1
    assert built._publish_intent.load() is not None


def test_inner_proxy_failure_is_definite_and_not_confirmed(tmp_path):
    built, client = adapter(tmp_path)
    client.after_send = lambda c: c.events.__setitem__(
        1, event("Proxy", "ProxyExecuted", {"result": {"Err": "NoPermission"}})
    )
    with pytest.raises(CommitRejected):
        built.publish_direct(VECTOR, 99)
    assert built._publish_intent.load() is None
    assert not list((tmp_path / "scores").glob("*"))


def test_missing_proxy_result_is_unknown(tmp_path):
    built, client = adapter(tmp_path)
    client.after_send = lambda c: c.events.pop(1)
    with pytest.raises(NotOnChain):
        built.publish_direct(VECTOR, 99)
    assert built._publish_intent.load() is not None


def test_marker_write_failure_prevents_send(tmp_path):
    built, client = adapter(tmp_path)
    with mock.patch.object(built._publish_intent, "open", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            built.publish_direct(VECTOR, 99)
    assert client.calls == []


def test_same_vector_without_matching_transaction_does_not_recover(tmp_path):
    built, client = adapter(tmp_path)

    def lose_identity(c):
        c.extrinsics[2]["nonce"] = 8
        raise TimeoutError()

    client.after_send = lose_identity
    with pytest.raises(NotOnChain):
        built.publish_direct(VECTOR, 99)
    with pytest.raises(PublicationStatusUnknown):
        built.already_published(99)
    assert len(client.calls) == 1


def test_null_has_no_owner_permit_exemption():
    client = Client()
    client.permit = False
    with pytest.raises(NoValidatorPermit, match="Null requires a permit"):
        check_has_validator_permit(ChainReader(client), 0, 82)


def test_unknown_consensus_refuses_owner_exemption():
    client = Client()
    client.permit = False
    client.consensus = None
    with pytest.raises(ChainUnreadable):
        check_has_validator_permit(ChainReader(client), 0, 82)


def test_other_mechanism_and_proxy_target_are_not_our_transaction(tmp_path):
    built, client = adapter(tmp_path)
    built.publish_direct(VECTOR, 99)
    marker = json.loads((tmp_path / "scores/day-0.submitted.json").read_text())["direct_intent"]
    tx = copy.deepcopy(client.extrinsics[2])
    tx["call"]["call_args"][0]["value"] = SIGNER
    assert direct.matches_call(tx, marker) is False
    tx = copy.deepcopy(client.extrinsics[2])
    inner = tx["call"]["call_args"][2]["value"]
    next(a for a in inner["call_args"] if a["name"] == "mecid")["value"] = 1
    assert direct.matches_call(tx, marker) is False


def test_missing_nonce_refuses_before_send(tmp_path):
    built, client = adapter(tmp_path)
    with mock.patch.object(built.publisher, "send_context", return_value={"head_at_send": 100}):
        with pytest.raises(PublicationRefused, match="readable signer nonce"):
            built.publish_direct(VECTOR, 99)
    assert client.calls == []


def test_atomic_intent_open_cannot_overwrite_an_existing_send(tmp_path):
    from ..publish_intent import PublishIntent

    intent = PublishIntent(tmp_path / "intent")
    intent.open(day=0, height=99, hotkey=VALIDATOR, netuid=82, exclusive=True)
    original = intent.path.read_bytes()
    with pytest.raises(PublicationStatusUnknown):
        intent.open(day=1, height=199, hotkey=VALIDATOR, netuid=82, exclusive=True)
    assert intent.path.read_bytes() == original


def test_changing_mode_does_not_clear_an_unknown_direct_send(tmp_path):
    built, client = adapter(tmp_path)
    client.after_send = lambda c: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(NotOnChain):
        built.publish_direct(VECTOR, 99)
    built.publish_mode = "sealed"
    with pytest.raises(PublicationStatusUnknown, match="before switching"):
        built.already_published(99)
    assert built._publish_intent.load() is not None


@pytest.mark.parametrize("contents", ["null", "[]", "true", "{broken"])
def test_corrupt_submitted_record_holds_without_sending(tmp_path, contents):
    built, client = adapter(tmp_path)
    scores = tmp_path / "scores"
    scores.mkdir()
    (scores / "day-0.submitted.json").write_text(contents)
    with pytest.raises(PublicationStatusUnknown):
        built.publish_direct(VECTOR, 99)
    assert not client.calls


def test_confirmation_uses_finalized_sources_without_sdk_finalized_property(tmp_path):
    built, client = adapter(tmp_path)
    assert not hasattr(client, "finalized_block")  # SDK 11.1.0 has no such property.
    # Best head being ahead must not permit confirmation beyond finalized sources.
    client.block = 1000
    client.finalized_head = 100
    with pytest.raises(NotOnChain):
        built.publish_direct(VECTOR, 99)
    assert built._publish_intent.load() is not None
    client.finalized_head = 101
    assert built.already_published(99)
    assert built._publish_intent.load() is None
    assert len(client.calls) == 1


def test_confirmation_refuses_sdk_block_hash_disagreeing_with_finalized_sources(tmp_path):
    built, client = adapter(tmp_path)
    built.runner_chain.block_hash = lambda block: "0x" + "ff" * 32
    with pytest.raises(NotOnChain, match="finalized sources"):
        built.publish_direct(VECTOR, 99)
    assert built._publish_intent.load() is not None
    assert len(client.calls) == 1


def test_missing_old_receipt_refuses_from_verified_hint_without_full_scan(tmp_path):
    built, client = adapter(tmp_path)
    client.block = 161321
    built._day_window = lambda height: (0, 1, 358)
    original_query = client.query
    event_reads = []

    def query(key, params=None, block=None):
        if key == ("SubtensorModule", "LastUpdate"):
            return [155539]
        if key == ("System", "Events"):
            event_reads.append(block)
            assert block == 155539, "must not scan from block 359"
            return [event("SubtensorModule", "WeightsSet", [82, 0])]
        return original_query(key, params, block)

    client.query = query
    with pytest.raises(PublicationStatusUnknown, match="unattributed direct"):
        built.publish_direct(VECTOR, 358)
    assert event_reads == [155539]
    assert client.calls == []
    assert built._publish_intent.load() is None
    assert not list((tmp_path / "scores").glob("*"))

    # The existing short-Day-0 helper catches the same refusal and falls
    # through to ordinary pending-day work; it does not mark Day 0 published.
    from ..loop import _day_zero_catch_up_height

    cache = {}
    built.day_anchor_ts = 0
    with mock.patch("noah.core.loop.day_block_range", return_value=(0, 1, 358)):
        assert _day_zero_catch_up_height(built, tempo=360, day_zero_cache=cache) is None
    assert cache == {}
    assert event_reads == [155539, 155539]


@pytest.mark.parametrize("hint", [None, True, "100", 100.0, -1, 0, 99, 102])
def test_unusable_activity_hint_retains_complete_history_scan(tmp_path, hint):
    built, client = adapter(tmp_path)
    client.block = 101
    with mock.patch.object(built.reader, "last_update", return_value=hint), \
            mock.patch.object(built.reader, "weights_set_for_seat") as probe, \
            mock.patch.object(built.reader, "weights_set_blocks_for_seat", return_value=[]) as scan:
        assert built.already_published(99) is False
    probe.assert_not_called()
    scan.assert_called_once_with(82, 0, 100, 101)
    assert client.calls == []


@pytest.mark.parametrize("candidate_events", [
    [],
    [event("System", "ExtrinsicSuccess", {})],
    [event("SubtensorModule", "WeightsSet", [2, 0])],
    [event("SubtensorModule", "WeightsSet", [82, 1])],
])
def test_activity_without_matching_event_cannot_skip_full_scan(tmp_path, candidate_events):
    built, client = adapter(tmp_path)
    client.block = 102
    client.events = candidate_events
    # The candidate is at 101 but the full range also contains 100 and 102.
    with mock.patch.object(built.reader, "last_update", return_value=101), \
            mock.patch.object(built.reader, "weights_set_blocks_for_seat", return_value=[100]) as scan:
        with pytest.raises(PublicationStatusUnknown, match="unattributed direct"):
            built.already_published(99)
    scan.assert_called_once_with(82, 0, 100, 102)
    assert client.calls == []


@pytest.mark.parametrize("fails", ["last_update", "weights_set_for_seat"])
def test_unreadable_hint_retains_authoritative_history_scan(tmp_path, fails):
    built, client = adapter(tmp_path)
    client.block = 101
    with mock.patch.object(built.reader, "last_update", return_value=100), \
            mock.patch.object(built.reader, fails, side_effect=ChainUnreadable("hint failed")), \
            mock.patch.object(built.reader, "weights_set_blocks_for_seat", return_value=None) as scan:
        with pytest.raises(PublicationStatusUnknown, match="unattributed direct"):
            built.already_published(99)
    scan.assert_called_once_with(82, 0, 100, 101)
    assert client.calls == []


@pytest.mark.parametrize("lost_response", [False, True])
def test_receipt_and_open_intent_reconciliation_precede_activity_hint(tmp_path, lost_response):
    built, client = adapter(tmp_path)
    if lost_response:
        client.after_send = lambda client: (_ for _ in ()).throw(TimeoutError("lost"))
        with pytest.raises(NotOnChain):
            built.publish_direct(VECTOR, 99)
    else:
        built.publish_direct(VECTOR, 99)
    with mock.patch.object(built.reader, "last_update") as hint:
        assert built.already_published(99) is True
    hint.assert_not_called()
    assert built._publish_intent.load() is None
    assert len(client.calls) == 1
