from types import SimpleNamespace

import pytest

from noah.core.calculation_record import encoded, retain, verify
from noah.core.publish_intent import PublicationStatusUnknown
from noah.starter.allocation import preview
from noah.starter.publication import check_before_send, retain_overlay, verify_overlay
from noah.starter.tests.test_epoch_clock import request_live


def prepared(tmp_path):
    raw = request_live()
    result = preview(raw)
    score = result["ordinaryOutput"]
    context = dict(
        day=1,
        height=999,
        genesis_hash=raw["snapshot"]["genesisHash"],
        netuid=82,
        ark_address="ark",
        designated_validator_hotkey="validator",
    )
    source = retain(
        tmp_path,
        fixture=raw["fixture"],
        agreed_text=encoded(score).decode(),
        vector={int(k): v for k, v in score["weightsU16"].items()},
        context=context,
    )
    return raw, result, source


def test_retained_overlay_does_not_overwrite_the_daily_result(tmp_path):
    raw, result, source = prepared(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("day-*")}
    ref = retain_overlay(tmp_path, source, raw)
    assert verify_overlay(tmp_path, ref)["source"] == source
    assert verify(tmp_path, ref)["publishable_weights_u16"] == result["proposedWeightsU16"]
    assert {p.name: p.read_bytes() for p in tmp_path.glob("day-*")} == before
    assert verify_overlay(tmp_path, source) is None
    fallback = retain_overlay(tmp_path, source, raw, fallback=True)
    assert verify_overlay(tmp_path, fallback)["mode"] == "ordinary-fallback"
    assert (
        verify(tmp_path, fallback)["publishable_weights_u16"]
        == result["ordinaryFallbackWeightsU16"]
    )


def sending_adapter(tmp_path, raw, head=1000):
    snap = raw["snapshot"]
    chain = SimpleNamespace(
        starter_epoch_info=lambda b: dict(
            LastEpochBlock=snap["epoch"],
            Tempo=snap["nextEpochBlock"] - snap["epoch"],
            PendingEpochAt=0,
        ),
        head_block=lambda: head,
        genesis_hash=lambda: snap["genesisHash"],
        block_hash=lambda b: snap["finalizedHash"],
        seat_hotkeys=lambda b: {int(k): v for k, v in raw["scoredHotkeys"].items()},
        hotkey_owners=lambda keys, b: {s["hotkey"]: s["coldkey"] for s in snap["seats"]},
    )
    return SimpleNamespace(
        _submitted_record_dir=tmp_path, netuid=82, runner_chain=chain, owner_uids=lambda: set()
    )


def test_expired_starter_rejected_but_explicit_recomputed_fallback_allowed(tmp_path):
    raw, result, source = prepared(tmp_path)
    ref = retain_overlay(tmp_path, source, raw)
    vector = {int(k): v for k, v in result["proposedWeightsU16"].items()}
    check_before_send(sending_adapter(tmp_path, raw), ref, vector)
    with pytest.raises(PublicationStatusUnknown, match="expired"):
        check_before_send(sending_adapter(tmp_path, raw, 1261), ref, vector)
    fallback = retain_overlay(tmp_path, source, raw, fallback=True)
    check_before_send(
        sending_adapter(tmp_path, raw, 1261),
        fallback,
        {int(k): v for k, v in result["ordinaryFallbackWeightsU16"].items()},
    )


def test_wrong_vector_or_changed_identity_refused_before_signing(tmp_path):
    raw, result, source = prepared(tmp_path)
    ref = retain_overlay(tmp_path, source, raw)
    vector = {int(k): v for k, v in result["proposedWeightsU16"].items()}
    bad = dict(vector)
    bad[0] -= 1
    bad[1] += 1
    with pytest.raises(PublicationStatusUnknown, match="requested vector"):
        check_before_send(sending_adapter(tmp_path, raw), ref, bad)
    a = sending_adapter(tmp_path, raw)
    a.runner_chain.seat_hotkeys = lambda b: {0: "other", 1: "hotkey-1"}
    with pytest.raises(PublicationStatusUnknown, match="identities changed"):
        check_before_send(a, ref, vector)


def test_fixture_changed_since_daily_agreement_refused(tmp_path):
    raw, result, source = prepared(tmp_path)
    raw["fixture"]["name"] = "edited"
    with pytest.raises(PublicationStatusUnknown, match="fixture differs"):
        retain_overlay(tmp_path, source, raw)
