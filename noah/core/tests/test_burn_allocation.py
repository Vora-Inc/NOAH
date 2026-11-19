"""Burn split, proof, same-day removal, and modeled consensus.

Modeled incentive figures are not observations of any chain block.
"""

import json
from types import SimpleNamespace

import pytest

from noah.core.burn_allocation import (
    ENABLED,
    allocate,
    modeled_incentive,
    require_chain_limits,
)
from noah.core.burn_publication import (
    recompute,
    report_subject,
    wrap_allocation,
)
from noah.core.calculation_record import retain
from noah.core.publish import NotOnChain, PublicationRefused
from noah.core.rescale import predict_stored
from noah.points.ledger import State, settle
from noah.scoring.pay import weights_u16
from noah.scoring.reporting_bonus import bonus_pay

from .test_direct_runtime475 import VECTOR, adapter
from .test_epoch_work import EpochClient, configure


def test_setting_is_zero_or_9800_and_9800_requires_v2():
    from noah.core.settings import BadSetting, Settings
    from .test_settings import COMPLETE

    assert Settings.from_mapping(COMPLETE).burn_allocation_bps == 0
    enabled = Settings.from_mapping(dict(
        COMPLETE,
        NOAH_SCORING_MODE="points-204040-v5",
        NOAH_POINTS_POLICY="/unused/policy.json",
        NOAH_BURN_ALLOCATION_BPS="9800",
    ))
    assert enabled.burn_allocation_bps == 9800
    assert enabled.scoring_mode == "points-204040-v5"
    restored = Settings.from_mapping(dict(
        COMPLETE,
        NOAH_SCORING_MODE="points-204040-v5",
        NOAH_POINTS_POLICY="/unused/policy.json",
        NOAH_BURN_ALLOCATION_BPS="0",
    ))
    assert restored.burn_allocation_bps == 0
    assert restored.scoring_mode == "points-204040-v5"
    with pytest.raises(BadSetting):
        Settings.from_mapping(dict(COMPLETE, NOAH_BURN_ALLOCATION_BPS="9800"))
    with pytest.raises(BadSetting):
        Settings.from_mapping(dict(COMPLETE, NOAH_BURN_ALLOCATION_BPS="9900"))


def test_zero_burn_returns_the_same_object():
    vector = {1: 3, 2: 0}
    assert allocate(vector, 0, None) is vector


def test_empty_allocation_does_not_become_a_burn_vector():
    empty = {}
    zeros = {1: 0, 2: 0}
    assert allocate(empty, ENABLED, 7) is empty
    assert allocate(zeros, ENABLED, 7) is zeros


def test_enabled_split_preserves_miner_ratios_and_bonus_effect():
    full = bonus_pay(
        {"payWeightFp": str(100 * 10**9), "doorOpen": True, "state": {"maturity": 10**6, "missed": 0}},
        True,
    )
    partial = bonus_pay(
        {"payWeightFp": str(100 * 10**9), "doorOpen": True, "state": {"maturity": 10**4, "missed": 0}},
        True,
    )
    missed = bonus_pay(
        {"payWeightFp": str(100 * 10**9), "doorOpen": True, "state": {"maturity": 10**6, "missed": 2}},
        True,
    )
    assert full == 120 * 10**9
    assert partial == 100 * 10**9 + 200_000_000
    assert missed == 100 * 10**9
    base = weights_u16({1: full, 2: partial})
    published = allocate({**base, 9: 0}, ENABLED, 9)
    assert published[9] == 65535 * 9800 // 10000
    assert published[1] + published[2] == 65535 - published[9]
    assert abs(published[1] / published[2] - base[1] / base[2]) < 0.02
    stored = predict_stored([published[uid] for uid in sorted(published)]).stored
    assert max(stored) == 65535
    require_chain_limits(
        published,
        max_weight_limit=65535,
        min_allowed_weights=1,
        epoch_consensus="Yuma",
        recycle_or_burn="Burn",
    )
    with pytest.raises(Exception, match="MaxWeightsLimit"):
        require_chain_limits(
            published,
            max_weight_limit=1000,
            min_allowed_weights=1,
            epoch_consensus="Yuma",
            recycle_or_burn="Burn",
        )


def test_modeled_matching_rows_and_majority_minority_are_different():
    """Modeled only. Not a read of block 9,527,978."""
    miners = weights_u16({1: 2, 2: 1})
    burned = allocate({**miners, 0: 0}, ENABLED, 0)
    matching = modeled_incentive([burned, burned], [7048, 2952])
    mixed = modeled_incentive([burned, {**miners}], [7048, 2952])
    matched = matching[0] / sum(matching.values())
    diluted = mixed[0] / sum(mixed.values())
    # Kappa 1/2 clips the minority row down to the majority row, so the
    # modeled burn stays near 98% rather than falling to 98% times 70.48%.
    assert abs(matched - burned[0] / sum(burned.values())) < 1e-9
    assert matched > diluted > 0.97


def test_corrupt_or_missing_proof_cannot_authorize_owner_weight(tmp_path):
    pre = {1: 10, 2: 30, 7: 0}
    published = allocate(pre, ENABLED, 7)
    source = retain(
        tmp_path,
        fixture={"schema": "noah-fixture/6", "evalDay": 1},
        agreed_text=json.dumps({"schema": "noah-score-output/6", "evalDay": 1, "weightsU16": {"1": 10}}),
        vector=pre,
        context={"day": 1, "height": 1, "genesis_hash": "g", "netuid": 1, "ark_address": "ark"},
    )
    wrapped = wrap_allocation(
        tmp_path,
        source,
        published,
        {
            "bps": ENABLED,
            "destination": {"uid": 7, "hotkey": "owner-hot", "coldkey": "owner-cold"},
            "pre_burn_u16": {str(k): v for k, v in pre.items()},
            "limits": {"max_weight_limit": 65535, "min_allowed_weights": 1,
                       "epoch_consensus": "Yuma", "recycle_or_burn": "Burn"},
        },
    )
    assert recompute(tmp_path, wrapped, published) == 7
    with pytest.raises(PublicationRefused):
        wrap_allocation(
            tmp_path, source, published,
            {"bps": ENABLED, "destination": {"uid": 7, "hotkey": "owner-hot", "coldkey": "owner-cold"},
             "pre_burn_u16": {"1": 11, "2": 30, "7": 0},
             "limits": {"max_weight_limit": 65535, "min_allowed_weights": 1,
                        "epoch_consensus": "Yuma", "recycle_or_burn": "Burn"}},
        )
    context_path = tmp_path / json.loads((tmp_path / wrapped["path"]).read_text())["artifacts"]["context"]["path"]
    context_path.write_bytes(context_path.read_bytes().replace(b'"bps": 9800', b'"bps": 9801', 1))
    with pytest.raises(Exception, match="hash differs|corrupt"):
        recompute(tmp_path, wrapped, published)
    plain = retain(
        tmp_path,
        fixture={"schema": "noah-fixture/6", "evalDay": 2},
        agreed_text=json.dumps({"schema": "noah-score-output/6", "evalDay": 2, "weightsU16": {"1": 1}}),
        vector={1: 1},
        context={"day": 2, "height": 2, "genesis_hash": "g", "netuid": 1, "ark_address": "ark"},
    )
    assert recompute(tmp_path, plain, {1: 0, 7: 1}) is None


def test_actual_received_earnings_feed_contribution_not_the_withheld_share():
    from noah.chain.pay import attribute_epoch
    from noah.chain.tests.test_pay import COLDKEY, EPOCH_BLOCK, HOTKEY, SN_OWNER, SN_OWNER_HOTKEY, epoch

    result = attribute_epoch(**epoch(
        emissions_by_mechanism={0: {140: 2_000, 1: 98_000}},
        hotkey_by_uid={140: HOTKEY, 1: SN_OWNER_HOTKEY},
        coldkey_by_hotkey={HOTKEY: COLDKEY, SN_OWNER_HOTKEY: SN_OWNER},
        registration_block_by_uid={140: EPOCH_BLOCK - 10, 1: EPOCH_BLOCK - 10},
    ))
    assert result.paid_by_coldkey == {COLDKEY: 2_000}
    state = settle(State(), result.paid_by_coldkey[COLDKEY], 1_000)
    assert state.earned == 2_000
    assert state.balance == 0
    unburned = settle(State(), 100_000, 1_000)
    assert unburned.earned == 100_000
    assert state.earned != unburned.earned


def _burned_ready(tmp_path):
    client = EpochClient()
    client.consensus = "Yuma"
    built, client = adapter(tmp_path, client)
    built.day_anchor_ts = 0
    built.runner_chain.genesis_hash = lambda: "genesis"
    built.runner_chain.block_timestamp = lambda h: 86400 + h
    built.runner_chain.seat_hotkeys = lambda h: {0: "owner-hot", 3: "a", 4: "b", 5: "c"}
    built.runner_chain.subnet_owner = lambda block: ("owner-cold", "owner-hot")
    built.runner_chain.hotkey_owners = lambda keys, block: {key: "owner-cold" for key in keys}
    built.owner_uids = lambda: {0}
    built.reader.recycle_or_burn = lambda netuid: "Burn"
    built.reader.max_weights_limit = lambda netuid: 65535
    built.reader.min_allowed_weights = lambda netuid: 1
    built.reader.epoch_consensus = lambda netuid, block=None: "Yuma"
    pre = dict(VECTOR)
    burned = allocate(pre, ENABLED, 0)
    source = retain(
        built._submitted_record_dir,
        fixture={"schema": "noah-fixture/6", "evalDay": 0},
        agreed_text=json.dumps(
            {"schema": "noah-score-output/6", "evalDay": 0,
             "weightsU16": {str(k): v for k, v in pre.items()}}
        ),
        vector=pre,
        context={
            "day": 0, "height": 99, "to_block": 99, "to_block_hash": f"0x{99:064x}",
            "genesis_hash": "genesis", "netuid": 82,
            "ark_address": built.ark_coldkey,
            "designated_validator_hotkey": built.designated_validator_hotkey,
            "scoring_dials": built.dials, "day_start_utc_unix": 0,
        },
    )
    built._calculation_reference = wrap_allocation(
        built._submitted_record_dir,
        source,
        burned,
        {
            "bps": ENABLED,
            "destination": {"uid": 0, "hotkey": "owner-hot", "coldkey": "owner-cold"},
            "pre_burn_u16": {str(k): v for k, v in pre.items()},
            "limits": {"max_weight_limit": 65535, "min_allowed_weights": 1,
                       "epoch_consensus": "Yuma", "recycle_or_burn": "Burn"},
        },
    )
    built.publish_direct(burned, 99)
    client.block = 201
    client.finalized_head = 201
    configure(built, SimpleNamespace(epoch_republication=True, epoch_collection=False))
    built.burn_allocation_bps = 0
    return built, client, pre, burned


def test_same_day_burn_disable_keeps_original_and_refresh_uses_replacement(tmp_path):
    built, client, pre, burned = _burned_ready(tmp_path)
    original = (tmp_path / "scores/day-0.submitted.json").read_bytes()
    assert json.loads(original)["submitted_weights_u16"]["0"] == burned[0]
    assert built._publish_intent.load() is None
    from noah.core.epoch_work import republish
    assert republish(built, 200, 201) == "burn_cleared"
    assert (tmp_path / "scores/day-0.submitted.json").read_bytes() == original
    replacement = list((tmp_path / "scores/burn-transitions").glob("day-0/*/day-0.submitted.json"))
    assert len(replacement) == 1
    body = json.loads(replacement[0].read_text())
    assert body["submitted_weights_u16"] == {str(k): v for k, v in pre.items()}
    assert body["direct_intent"]["successor_kind"] == "burn-zero"
    assert republish(built, 200, 203) == "already_published_this_epoch"
    calls_after_clear = len(client.calls)
    client.epoch = 400
    client.last = 0
    assert republish(built, 400, 401) == "republished"
    assert len(client.calls) == calls_after_clear + 1
    sent = client.calls[-1][0]
    from .test_direct_runtime475 import inner_call
    weights = dict(zip(inner_call(sent).params["dests"], inner_call(sent).params["weights"]))
    assert weights == pre
    assert weights != burned


def test_lost_reply_during_burn_disable_does_not_send_twice(tmp_path):
    built, client, pre, burned = _burned_ready(tmp_path)
    from noah.core.epoch_work import republish
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        republish(built, 200, 201)
    assert built._publish_intent.load()["successor_kind"] == "burn-zero"
    assert json.loads((tmp_path / "scores/day-0.submitted.json").read_text())["submitted_weights_u16"]["0"] == burned[0]
    sent = len(client.calls)
    client.lost_reply = False
    assert republish(built, 200, 203) == "already_published_this_epoch"
    assert len(client.calls) == sent
    assert built._publish_intent.load() is None
    stored = json.loads(list((tmp_path / "scores/burn-transitions").glob("day-0/*/day-0.submitted.json"))[0].read_text())
    assert stored["submitted_weights_u16"] == {str(k): v for k, v in pre.items()}


def test_midnight_outage_publishes_the_confirmed_zero_burn_allocation(tmp_path):
    """After midnight, a missed next day must refresh the burn-off receipt.

    Before this selection fix, epoch_work.republish reached points.outage.republish_plain,
    selected day-N.submitted.json, and the transport received the superseded 9800 vector
    {0: 64224, 3: 1311}. The retained pre-burn allocation {0: 0, 3: 65535} was not sent.
    """
    from noah.core.epoch_work import republish
    from noah.core.reader import ChainUnreadable
    from noah.points.outage import record_failure

    built, client, pre, burned, day, hot, cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    client.block = client.finalized_head = 201
    assert republish(built, 200, 201) == "burn_cleared"
    previous = built
    built, _ = adapter(tmp_path, client)
    _restore_v2(built, previous)
    due = built.day_anchor_ts + (day + 2) * 86400
    built.runner_chain.block_timestamp = lambda h: due + 100
    assert record_failure(built, ChainUnreadable("offline pricing source unavailable"))
    client.last = 0
    client.block = client.finalized_head = 500
    sent_before = len(client.calls)
    assert republish(built, 600, 500) == "points_outage_refreshed"
    assert len(client.calls) == sent_before + 1
    from .test_direct_runtime475 import inner_call
    weights = dict(zip(inner_call(client.calls[-1][0]).params["dests"],
                       inner_call(client.calls[-1][0]).params["weights"]))
    assert weights == pre
    assert weights != burned
    assert weights[0] == 0 and weights[3] == 65535


def test_invalid_burn_removal_refuses_and_does_not_send_the_old_vector(tmp_path):
    from noah.core.epoch_work import republish
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.core.reader import ChainUnreadable
    from noah.points.outage import record_failure

    built, client, pre, burned, day, hot, cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    client.block = client.finalized_head = 201
    assert republish(built, 200, 201) == "burn_cleared"
    receipt = next((tmp_path / "scores/burn-transitions").glob(f"day-{day}/*/day-{day}.submitted.json"))
    body = json.loads(receipt.read_text())
    body["direct_intent"]["vector"] = {"0": str(burned[0]), "3": burned[3]}
    receipt.write_text(json.dumps(body))
    previous = built
    built, _ = adapter(tmp_path, client)
    _restore_v2(built, previous)
    due = built.day_anchor_ts + (day + 2) * 86400
    built.runner_chain.block_timestamp = lambda h: due + 100
    assert record_failure(built, ChainUnreadable("offline pricing source unavailable"))
    client.last = 0
    client.block = client.finalized_head = 500
    sent_before = len(client.calls)
    with pytest.raises((PublicationStatusUnknown, PublicationRefused)):
        republish(built, 600, 500)
    assert len(client.calls) == sent_before


def _v2_burned_day(tmp_path):
    from noah.chain.ss58 import address_of
    from noah.core.calculation_record import encoded, retain
    from noah.points.onboarding import MODE, POLICY, RULES
    from noah.points.run import agreed
    from noah.points.tests.test_engine import fixture

    client = EpochClient()
    client.consensus = "Yuma"
    built, client = adapter(tmp_path, client)
    raw = fixture([[(100 * 10**9, 0, 0), (0, 75 * 10**9, 0)]])
    raw["miners"][0]["uid"] = 3
    hot = cold = address_of(bytes([7]) * 32)
    raw["miners"][0]["coldkey"] = cold
    raw.update(
        schema="noah-points-fixture/3", pointsRules=RULES, pointsExcludedUids=[0],
        pointsReceipts=[], pointsPricing={}, pointsHotkeys={"3": hot}, pointsOwners={"3": cold},
        pointsPricingBoundary=dict(
            anchor={"block": 1, "hash": "0x" + "1" * 64}, effectiveBlock=2,
            policySha256="a" * 64, predecessorPolicySha256="b" * 64,
            emaSource="SubnetMovingPrice", genesis="0x" + "a" * 64, nativeNetuid=82,
        ),
    )
    score = agreed(raw)
    pre = {int(k): v for k, v in score["weightsU16"].items()}
    pre.setdefault(0, 0)
    burned = allocate(pre, ENABLED, 0)
    day = int(raw["evalDay"])
    built.scoring_mode = MODE
    built.starter_entry_policy = None
    built.epoch_republication = True
    built.burn_allocation_bps = ENABLED
    built.points_policy = SimpleNamespace(
        manifest={"policy": POLICY, "sha256": "a" * 64, "cutoff_day": 0}
    )
    built._day_window = lambda h: (day, 1, 99)
    built.day_anchor_ts = int(raw["dayEpochTs"])
    built.owner_uids = lambda: {0}
    _chain_stubs(built, hot, cold)
    anchor, closed = built.day_anchor_ts, day
    built.runner_chain.block_timestamp = lambda h, anchor=anchor, closed=closed: anchor + (closed + 1) * 86400 + 100
    source = retain(
        built._submitted_record_dir, fixture=raw, agreed_text=encoded(score).decode(), vector=pre,
        context=dict(
            day=day, height=99, to_block=99, to_block_hash=f"0x{99:064x}", netuid=82,
            genesis_hash="genesis", ark_address=built.ark_coldkey,
            designated_validator_hotkey=built.designated_validator_hotkey,
            scoring_mode=MODE, points_policy=built.points_policy.manifest, scoring_dials=built.dials,
            scored_hotkeys={"3": hot}, scored_coldkeys={"3": cold},
            day_start_utc_unix=built.day_anchor_ts + day * 86400,
        ),
    )
    built._calculation_reference = wrap_allocation(
        built._submitted_record_dir, source, burned,
        {"bps": ENABLED, "destination": {"uid": 0, "hotkey": "owner-hot", "coldkey": "owner-cold"},
         "pre_burn_u16": {str(k): v for k, v in pre.items()},
         "limits": {"max_weight_limit": 65535, "min_allowed_weights": 1,
                    "epoch_consensus": "Yuma", "recycle_or_burn": "Burn"}},
    )
    built.publish_direct(burned, 99)
    return built, client, pre, burned, day, hot, cold


def _restore_v2(built, previous):
    from noah.points.onboarding import MODE
    built.runner_chain = previous.runner_chain
    built.day_anchor_ts = previous.day_anchor_ts
    built.scoring_mode = MODE
    built.points_policy = previous.points_policy
    built.owner_uids = previous.owner_uids
    built.burn_allocation_bps = 0
    built.epoch_republication = True
    built.starter_entry_policy = None
    built.dials = previous.dials
    built.reader.recycle_or_burn = lambda netuid: "Burn"
    built.reader.max_weights_limit = lambda netuid: 65535
    built.reader.min_allowed_weights = lambda netuid: 1
    built.reader.epoch_consensus = lambda netuid, block=None: "Yuma"


def _chain_stubs(built, hot, cold):
    built.runner_chain.genesis_hash = lambda: "genesis"
    built.runner_chain.block_timestamp = lambda h: built.day_anchor_ts + 86400 + 100
    built.runner_chain.seat_hotkeys = lambda h: {0: "owner-hot", 3: hot}
    built.runner_chain.subnet_owner = lambda block: ("owner-cold", "owner-hot")
    built.runner_chain.hotkey_owners = lambda keys, h: {**{key: "owner-cold" for key in keys}, hot: cold}
    built.reader.recycle_or_burn = lambda netuid: "Burn"
    built.reader.max_weights_limit = lambda netuid: 65535
    built.reader.min_allowed_weights = lambda netuid: 1
    built.reader.epoch_consensus = lambda netuid, block=None: "Yuma"


def _report_burn(tmp_path, monkeypatch):
    """A real bonus record wrapped by 9800, with the chain stubs that send needs."""
    from noah.core.tests.test_report_toggle_recovery import _ready
    from noah.points import onboarding

    built, client, _source, bound, vector, _ordinary, replays, _policy = _ready(
        tmp_path, monkeypatch, onboarding.MODE, False, "bonus-20"
    )
    pre = dict(vector)
    burned = allocate(pre, ENABLED, 0)
    built.runner_chain.subnet_owner = lambda block: ("owner-cold", "owner-hot")
    seats = built.runner_chain.seat_hotkeys(0)
    built.runner_chain.seat_hotkeys = lambda height: {**seats, 0: "owner-hot"}
    owners = built.runner_chain.hotkey_owners
    built.runner_chain.hotkey_owners = lambda keys, height: {**owners(keys, height), "owner-hot": "owner-cold"}
    built.reader.recycle_or_burn = lambda netuid: "Burn"
    built.reader.max_weights_limit = lambda netuid: 65535
    built.reader.min_allowed_weights = lambda netuid: 1
    built.reader.epoch_consensus = lambda netuid, block=None: "Yuma"
    built.burn_allocation_bps = ENABLED
    built._calculation_reference = wrap_allocation(
        built._submitted_record_dir, bound, burned,
        {"bps": ENABLED, "destination": {"uid": 0, "hotkey": "owner-hot", "coldkey": "owner-cold"},
         "pre_burn_u16": {str(uid): weight for uid, weight in pre.items()},
         "limits": {"max_weight_limit": 65535, "min_allowed_weights": 1,
                    "epoch_consensus": "Yuma", "recycle_or_burn": "Burn"}},
    )
    return built, client, pre, burned, replays


def test_burn_confirmation_saves_the_finalized_send(tmp_path, monkeypatch):
    """Before the fix, publish_direct sent once and then verify_report rejected the burn wrapper."""
    built, client, pre, burned, replays = _report_burn(tmp_path, monkeypatch)
    before = len(replays)
    result = built.publish_direct(burned, 99)
    assert result["applied_weights_readback"]["confirmed"] is True
    assert built._publish_intent.load() is None
    assert len(client.calls) == 1
    assert len(replays) >= before + 2
    receipt = json.loads(next((tmp_path / "scores").glob("day-*.submitted.json")).read_text())
    assert receipt["submitted_weights_u16"] == {str(uid): weight for uid, weight in burned.items()}
    assert receipt["submitted_weights_u16"] != {str(uid): weight for uid, weight in pre.items()}
    with pytest.raises(PublicationRefused, match="already confirmed"):
        built.publish_direct(burned, 99)
    assert len(client.calls) == 1


def test_lost_burn_reply_is_confirmed_on_restart_without_a_second_send(tmp_path, monkeypatch):
    built, client, _pre, burned, _replays = _report_burn(tmp_path, monkeypatch)
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        built.publish_direct(burned, 99)
    assert built._publish_intent.load() is not None
    calls = len(client.calls)
    client.lost_reply = False
    assert built._already_published_direct(99) is True
    assert len(client.calls) == calls
    assert built._publish_intent.load() is None
    receipt = json.loads(next((tmp_path / "scores").glob("day-*.submitted.json")).read_text())
    assert receipt["submitted_weights_u16"]["0"] == burned[0]


def test_tampered_burn_proof_is_refused_without_another_send(tmp_path, monkeypatch):
    built, client, _pre, burned, _replays = _report_burn(tmp_path, monkeypatch)
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        built.publish_direct(burned, 99)
    calls = len(client.calls)
    reference = built._calculation_reference
    path = built._submitted_record_dir / reference["path"]
    body = bytearray(path.read_bytes())
    body[0] ^= 0x01
    path.write_bytes(body)
    client.lost_reply = False
    from noah.core.publish_intent import PublicationStatusUnknown
    with pytest.raises(PublicationStatusUnknown):
        built._already_published_direct(99)
    assert len(client.calls) == calls
    assert built._publish_intent.load() is not None
    assert not list((tmp_path / "scores").glob("day-*.submitted.json"))


def test_outage_burn_off_publishes_the_retained_allocation_before_a_replacement_exists(tmp_path):
    from noah.core.epoch_work import republish
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.core.reader import ChainUnreadable
    from noah.points.outage import record_failure
    from .test_direct_runtime475 import inner_call

    built, client, pre, burned, day, _hot, _cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    built.prepare_report_vector = lambda vector: (_ for _ in ()).throw(AssertionError("bonus applied again"))
    due = built.day_anchor_ts + (day + 2) * 86400
    expiry = due + 86400
    built.runner_chain.block_timestamp = lambda height: due + 100
    assert record_failure(built, ChainUnreadable("offline pricing source unavailable"))
    from noah.points.outage import authorize
    with pytest.raises(PublicationStatusUnknown, match="24 hours"):
        authorize(built, built._calculation_reference, expiry)
    client.last = 0
    client.block = client.finalized_head = 500
    calls = len(client.calls)
    outage_path = tmp_path / "scores/points-outage.json"
    saved_outage = outage_path.read_text()
    outage_path.write_text(saved_outage.replace("ChainUnreadable", "recovered", 1))
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        republish(built, 600, 500)
    assert len(client.calls) == calls
    outage_path.write_text(saved_outage)
    assert not list((tmp_path / "scores/burn-transitions").glob("*/*/*.submitted.json"))

    built.runner_chain.block_timestamp = lambda height: due + 100
    built.rate_limit = lambda netuid: 10000
    client.last = 500
    assert republish(built, 600, 500) == "awaiting_rate_limit"
    assert len(client.calls) == calls

    built.rate_limit = lambda netuid: 0
    client.last = 0
    original = (tmp_path / f"scores/day-{day}.submitted.json").read_bytes()
    assert republish(built, 600, 500) == "burn_cleared"
    assert len(client.calls) == calls + 1
    sent = inner_call(client.calls[-1][0])
    weights = dict(zip(sent.params["dests"], sent.params["weights"]))
    assert weights == pre
    assert weights != burned
    assert (tmp_path / f"scores/day-{day}.submitted.json").read_bytes() == original
    stored = json.loads(next((tmp_path / "scores/burn-transitions").glob(f"day-{day}/*/*.submitted.json")).read_text())
    assert stored["submitted_weights_u16"] == {str(uid): weight for uid, weight in pre.items()}
    assert stored["direct_intent"]["successor_kind"] == "burn-zero"


def test_lost_outage_burn_off_does_not_send_the_old_vector_twice(tmp_path):
    from noah.core.epoch_work import republish
    from noah.core.reader import ChainUnreadable
    from noah.points.outage import record_failure

    built, client, pre, burned, day, _hot, _cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    due = built.day_anchor_ts + (day + 2) * 86400
    built.runner_chain.block_timestamp = lambda height: due + 100
    assert record_failure(built, ChainUnreadable("offline pricing source unavailable"))
    client.last = 0
    client.block = client.finalized_head = 500
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        republish(built, 400, 500)
    calls = len(client.calls)
    client.lost_reply = False
    assert republish(built, client.last, client.block) == "already_published_this_epoch"
    assert len(client.calls) == calls
    stored = json.loads(next((tmp_path / "scores/burn-transitions").glob(f"day-{day}/*/*.submitted.json")).read_text())
    assert stored["submitted_weights_u16"] == {str(uid): weight for uid, weight in pre.items()}
    assert stored["submitted_weights_u16"] != {str(uid): weight for uid, weight in burned.items()}


def test_next_day_follows_a_burn_off_refresh_across_midnight(tmp_path):
    from noah.core.calculation_record import artifact, encoded, retain, verify
    from noah.core.epoch_work import republish
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.core.reader import ChainUnreadable
    from noah.points.outage import record_failure
    from .test_direct_runtime475 import inner_call
    from .test_epoch_work import adapter

    built, client, pre, burned, day, hot, cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    due = built.day_anchor_ts + (day + 2) * 86400
    built.runner_chain.block_timestamp = lambda height: due + 100
    assert record_failure(built, ChainUnreadable("offline pricing source unavailable"))
    client.last = 0
    client.block = client.finalized_head = 500
    assert republish(built, 600, 500) == "burn_cleared"
    client.epoch = 700
    client.last = 0
    client.block = client.finalized_head = 520
    assert republish(built, 700, 520) == "points_outage_refreshed"
    refresh = json.loads(next((tmp_path / "scores/epoch-publications").glob(f"*/day-{day}.submitted.json")).read_text())
    refresh_block = refresh["direct_intent"]["result"]["block"]
    assert refresh["submitted_weights_u16"] == {str(uid): weight for uid, weight in pre.items()}
    client.block = client.finalized_head = refresh_block + 5
    previous = built
    built, _ = adapter(tmp_path, client)
    _restore_v2(built, previous)
    built.runner_chain.block_timestamp = lambda height: built.day_anchor_ts + (day + 3) * 86400 + 100
    built._day_window = lambda height: (day + 2, 102, refresh_block - 1)
    calls = len(client.calls)
    assert built._already_published_direct(refresh_block + 5) is False
    assert len(client.calls) == calls
    receipt = next((tmp_path / "scores/epoch-publications").glob(f"*/day-{day}.submitted.json"))
    original_receipt = receipt.read_text()
    body = json.loads(original_receipt)
    body["direct_intent"]["vector"] = {"0": str(burned[0]), "3": burned[3]}
    receipt.write_text(json.dumps(body))
    with pytest.raises(PublicationStatusUnknown, match="unattributed direct"):
        built._already_published_direct(refresh_block + 5)
    assert len(client.calls) == calls
    receipt.write_text(original_receipt)
    assert built._already_published_direct(refresh_block + 5) is False
    manifest = verify(built._submitted_record_dir, previous._calculation_reference)
    fresh_day = day + 2
    fresh_vector = {3: 40000, 0: 0}
    fixture = dict(artifact(built._submitted_record_dir, manifest, "fixture"))
    score = dict(artifact(built._submitted_record_dir, manifest, "agreed_score"))
    fixture["evalDay"] = str(fresh_day)
    score["evalDay"] = str(fresh_day)
    built._calculation_reference = retain(
        built._submitted_record_dir,
        fixture=fixture,
        agreed_text=encoded(score).decode(),
        vector=fresh_vector,
        context=dict(
            day=fresh_day, height=520, to_block=520, to_block_hash=f"0x{520:064x}", netuid=82,
            genesis_hash="genesis", ark_address=built.ark_coldkey,
            designated_validator_hotkey=built.designated_validator_hotkey,
            scoring_mode=built.scoring_mode, points_policy=built.points_policy.manifest,
            scoring_dials=built.dials, scored_hotkeys={"3": hot}, scored_coldkeys={"3": cold},
            day_start_utc_unix=built.day_anchor_ts + fresh_day * 86400,
        ),
        canonical_day=False,
    )
    built.publish_direct(fresh_vector, 520)
    assert len(client.calls) == calls + 1
    sent = inner_call(client.calls[-1][0])
    assert dict(zip(sent.params["dests"], sent.params["weights"])) == fresh_vector
    assert built._already_published_direct(520) is True


def test_positive_miner_keeps_one_submitted_unit_during_burn():
    from collections import Counter
    from noah.points.engine import replay_state
    from noah.points.ledger import Q
    from noah.points.onboarding import RULES, initial_state
    from noah.points.tests.test_catchup_recovery import N

    points = {uid: 100 * Q for uid in range(1, 59)}
    points[59] = 400_000
    pre = weights_u16(points)
    assert Counter(pre[uid] for uid in range(1, 59)) == Counter({1130: 49, 1129: 9})
    assert pre[59] == 4 and sum(pre.values()) == 65535
    burned = allocate({**pre, 0: 0, 60: 0}, ENABLED, 0)
    assert burned[0] == 64224
    assert burned[59] == 1
    assert burned[60] == 0
    assert Counter(burned[uid] for uid in range(1, 59)) == Counter({23: 34, 22: 24})
    assert sum(burned[uid] for uid in range(1, 60)) == 1311
    assert sum(burned.values()) == 65535
    with pytest.raises(Exception, match="cannot cover one submitted unit"):
        allocate({uid: 1 for uid in range(1, 1313)}, ENABLED, 0)
    assert allocate({3: 65535}, 0, None) == {3: 65535}
    start = initial_state()
    idle = replay_state(
        N(first_activity_day=1, days={
            1: N(paid_rao=0, pay_unlocked_rao=0, noah_deposit_rao=0, foreign_deposit_noah_equiv_rao=0),
            2: N(paid_rao=0, pay_unlocked_rao=0, noah_deposit_rao=0, foreign_deposit_noah_equiv_rao=0),
        }),
        2,
        rules=RULES,
    )
    assert (idle.qualified, idle.earned, idle.maturity) == (0, 0, start.maturity)
    paid = replay_state(
        N(first_activity_day=1, days={
            1: N(paid_rao=2000, pay_unlocked_rao=0, noah_deposit_rao=0, foreign_deposit_noah_equiv_rao=0),
            2: N(paid_rao=2000, pay_unlocked_rao=0, noah_deposit_rao=1000, foreign_deposit_noah_equiv_rao=0),
            3: N(paid_rao=0, pay_unlocked_rao=0, noah_deposit_rao=1000, foreign_deposit_noah_equiv_rao=0),
        }),
        3,
        rules=RULES,
    )
    assert paid.qualified == 2 and paid.earned == 4000 and paid.maturity > start.maturity


def test_same_day_exit_waits_for_the_chain_period_then_sends_once(tmp_path):
    """Maintenance must not consume the epoch attempt while the chain is still waiting.

    Before this check, block 501 with last update 300 and a 250-block wait recorded
    attempted_epoch and sent nothing. Block 551 then refused with an already-attempted epoch.
    """
    from noah.core.epoch_work import maintain
    from .test_direct_runtime475 import inner_call

    built, client, pre, _burned, _day, _hot, _cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    built.rate_limit = lambda netuid: 250
    client.last = 300
    client.epoch = 400
    client.block = client.finalized_head = 501
    calls = len(client.calls)
    maintain(built, tmp_path / "lock")
    state = json.loads((tmp_path / "scores/epoch-maintenance.json").read_text())
    assert len(client.calls) == calls
    assert state.get("attempted_epoch") != 400
    assert state.get("published_epoch") != 400
    assert state["publication_status"] == "awaiting_rate_limit"
    assert built._publish_intent.load() is None
    assert not list((tmp_path / "scores/burn-transitions").glob("*/*/*.submitted.json"))
    client.block = client.finalized_head = 551
    maintain(built, tmp_path / "lock")
    state = json.loads((tmp_path / "scores/epoch-maintenance.json").read_text())
    assert len(client.calls) == calls + 1
    sent = inner_call(client.calls[-1][0])
    assert dict(zip(sent.params["dests"], sent.params["weights"])) == pre
    assert state["attempted_epoch"] == 400
    assert state["published_epoch"] == 400
    assert state["publication_status"] == "burn_cleared"
    assert built._publish_intent.load() is None


def test_stored_weight_limit_refuses_64224_and_accepts_65535(tmp_path):
    from noah.core.burn_allocation import BurnAllocationRefused
    from noah.points.ledger import Q

    points = {uid: 100 * Q for uid in range(1, 59)}
    points[59] = 400_000
    pre = {**weights_u16(points), 0: 0}
    burned = allocate(pre, ENABLED, 0)
    limits = dict(min_allowed_weights=1, epoch_consensus="Yuma", recycle_or_burn="Burn")
    with pytest.raises(BurnAllocationRefused, match="MaxWeightsLimit"):
        require_chain_limits(burned, max_weight_limit=64224, **limits)
    require_chain_limits(burned, max_weight_limit=65535, **limits)

    client = EpochClient()
    client.consensus = "Yuma"
    built, client = adapter(tmp_path, client)
    built.runner_chain.genesis_hash = lambda: "genesis"
    built.runner_chain.block_timestamp = lambda height: 86400 + height
    built.runner_chain.seat_hotkeys = lambda height: {0: "owner-hot"}
    built.runner_chain.subnet_owner = lambda block: ("owner-cold", "owner-hot")
    built.runner_chain.hotkey_owners = lambda keys, block: {key: "owner-cold" for key in keys}
    built.owner_uids = lambda: {0}
    built.reader.recycle_or_burn = lambda netuid: "Burn"
    built.reader.max_weights_limit = lambda netuid: 64224
    built.reader.min_allowed_weights = lambda netuid: 1
    built.reader.epoch_consensus = lambda netuid, block=None: "Yuma"
    built.burn_allocation_bps = ENABLED
    source = retain(
        built._submitted_record_dir,
        fixture={"schema": "noah-fixture/6", "evalDay": 0},
        agreed_text=json.dumps({"schema": "noah-score-output/6", "evalDay": 0, "weightsU16": {"1": 1}}),
        vector=pre,
        context={"day": 0, "height": 99, "to_block": 99, "to_block_hash": f"0x{99:064x}",
                 "genesis_hash": "genesis", "netuid": 82, "ark_address": built.ark_coldkey,
                 "designated_validator_hotkey": built.designated_validator_hotkey,
                 "scoring_dials": built.dials, "day_start_utc_unix": 0},
    )
    built._calculation_reference = wrap_allocation(
        built._submitted_record_dir, source, burned,
        {"bps": ENABLED, "destination": {"uid": 0, "hotkey": "owner-hot", "coldkey": "owner-cold"},
         "pre_burn_u16": {str(uid): weight for uid, weight in pre.items()},
         "limits": {"max_weight_limit": 64224, "min_allowed_weights": 1,
                    "epoch_consensus": "Yuma", "recycle_or_burn": "Burn"}},
    )
    with pytest.raises(BurnAllocationRefused, match="MaxWeightsLimit"):
        built.publish_direct(burned, 99)
    assert client.calls == []
    assert built._publish_intent.load() is None
    built.reader.max_weights_limit = lambda netuid: 65535
    result = built.publish_direct(burned, 99)
    assert len(client.calls) == 1
    assert result["applied_weights_readback"]["confirmed"] is True
    from .test_direct_runtime475 import inner_call
    sent = inner_call(client.calls[-1][0])
    assert dict(zip(sent.params["dests"], sent.params["weights"]))[0] == 64224


def test_report_subject_uses_pre_burn_allocation(tmp_path):
    pre = {1: 5, 2: 7}
    published = allocate({**pre, 4: 0}, ENABLED, 4)
    source = retain(
        tmp_path,
        fixture={"schema": "noah-fixture/6", "evalDay": 3},
        agreed_text=json.dumps({"schema": "noah-score-output/6", "evalDay": 3, "weightsU16": {"1": 5}}),
        vector={**pre, 4: 0},
        context={"day": 3, "height": 3, "genesis_hash": "g", "netuid": 1, "ark_address": "ark"},
    )
    wrapped = wrap_allocation(
        tmp_path, source, published,
        {"bps": ENABLED, "destination": {"uid": 4, "hotkey": "h", "coldkey": "c"},
         "pre_burn_u16": {str(k): v for k, v in {**pre, 4: 0}.items()},
         "limits": {"max_weight_limit": 65535, "min_allowed_weights": 1,
                    "epoch_consensus": "Yuma", "recycle_or_burn": "Burn"}},
    )
    subject, identity = report_subject(tmp_path, wrapped, published)
    assert subject["sha256"] == source["sha256"]
    assert identity == {**pre, 4: 0}


def _successor_barriers(built, tmp_path):
    from noah.core.publish_hold import PublishHold
    from noah.core.proxy_route import PublishFailureBackoff

    built._publish_hold = PublishHold(tmp_path / "publish-hold.json")
    built._failure_backoff = PublishFailureBackoff(tmp_path / "publish-backoff.json")


def test_burn_exit_maintenance_preserves_active_failure_backoff(tmp_path):
    from noah.core.epoch_work import maintain

    built, client, _pre, _burned, _day, _hot, _cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    client.block = client.finalized_head = 201
    _successor_barriers(built, tmp_path)
    built._failure_backoff.record_failure("previous definite rejection")
    original = built._failure_backoff.path.read_bytes()
    calls = len(client.calls)
    maintain(built, tmp_path / "lock")
    state = json.loads((tmp_path / "scores/epoch-maintenance.json").read_text())
    assert state["publication_status"] == "PublicationFailureLimit"
    assert state.get("published_epoch") != client.epoch
    assert len(client.calls) == calls
    assert built._failure_backoff.path.read_bytes() == original
    assert built._publish_intent.load() is None
    assert not list((tmp_path / "scores/burn-transitions").glob("*/*/*.submitted.json"))


def test_burn_exit_refuses_hold_before_opening_a_new_intent(tmp_path):
    from noah.core.epoch_work import republish
    from noah.core.publish_hold import PublicationOnHold

    built, client, _pre, _burned, _day, _hot, _cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    client.block = client.finalized_head = 201
    _successor_barriers(built, tmp_path)
    built._publish_hold.set("operator review")
    original = built._publish_hold.path.read_bytes()
    calls = len(client.calls)
    with pytest.raises(PublicationOnHold):
        republish(built, 200, 201)
    assert len(client.calls) == calls
    assert built._publish_hold.path.read_bytes() == original
    assert built._publish_intent.load() is None


@pytest.mark.parametrize("barrier", ["hold", "backoff"])
def test_burn_exit_rechecks_late_barrier_before_transport(tmp_path, monkeypatch, barrier):
    from unittest.mock import Mock
    from noah.core.epoch_work import maintain

    built, client, _pre, _burned, _day, _hot, _cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    client.block = client.finalized_head = 201
    _successor_barriers(built, tmp_path)
    send_context = built.publisher.send_context
    retained = {}

    def context_then_barrier(signer):
        result = send_context(signer)
        if barrier == "hold":
            built._publish_hold.set("operator hold arrived during preparation")
            path = built._publish_hold.path
        else:
            built._failure_backoff.record_failure("concurrent definite rejection")
            path = built._failure_backoff.path
        retained.update(path=path, data=path.read_bytes())
        return result

    monkeypatch.setattr(built.publisher, "send_context", context_then_barrier)
    transport = Mock(wraps=built.publisher.submit_direct)
    monkeypatch.setattr(built.publisher, "submit_direct", transport)
    calls = len(client.calls)
    maintain(built, tmp_path / "lock")
    state = json.loads((tmp_path / "scores/epoch-maintenance.json").read_text())
    assert state["publication_status"] == "PublicationRefused"
    assert state.get("published_epoch") != client.epoch
    transport.assert_not_called()  # No preparation/signing or send.
    assert len(client.calls) == calls
    assert retained["path"].read_bytes() == retained["data"]
    assert built._publish_intent.load() is None  # Definite refusal before transport.
    assert not list((tmp_path / "scores/burn-transitions").glob("*/*/*.submitted.json"))


def test_burn_exit_unknown_send_keeps_intent_before_backoff_and_reconciles_once(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from noah.core import epoch_work
    from noah.core.publish_intent import PublicationStatusUnknown

    built, client, pre, _burned, _day, _hot, _cold = _v2_burned_day(tmp_path)
    built.burn_allocation_bps = 0
    client.block = client.finalized_head = 201
    _successor_barriers(built, tmp_path)
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        epoch_work.republish(built, 200, 201)
    intent = built._publish_intent.path.read_bytes()
    calls = len(client.calls)
    built._failure_backoff.record_failure("separate unresolved operator investigation")
    backoff = built._failure_backoff.path.read_bytes()
    checked = Mock(wraps=built._failure_backoff.raise_if_blocked)
    monkeypatch.setattr(built._failure_backoff, "raise_if_blocked", checked)
    built._publish_hold.set("operator hold after an uncertain send")
    with pytest.raises(PublicationStatusUnknown, match="earlier send must be reconciled"):
        built.publish_direct(pre, 99, source_day=_day, successor={"kind": "burn-zero"})
    checked.assert_not_called()
    assert built._publish_intent.path.read_bytes() == intent
    reconcile = epoch_work.reconcile

    def unavailable(*args, **kwargs):
        raise PublicationStatusUnknown("paired confirmation unavailable")

    monkeypatch.setattr(epoch_work, "reconcile", unavailable)
    with pytest.raises(PublicationStatusUnknown, match="paired confirmation unavailable"):
        epoch_work.republish(built, 200, 203)
    checked.assert_not_called()
    assert built._publish_intent.path.read_bytes() == intent
    assert built._failure_backoff.path.read_bytes() == backoff
    assert len(client.calls) == calls
    monkeypatch.setattr(epoch_work, "reconcile", reconcile)
    client.lost_reply = False
    assert epoch_work.republish(built, 200, 203) == "already_published_this_epoch"
    assert len(client.calls) == calls
    assert built._publish_intent.load() is None
    assert built._failure_backoff.path.read_bytes() == backoff
    receipt = next((tmp_path / "scores/burn-transitions").glob("*/*/*.submitted.json"))
    assert json.loads(receipt.read_text())["submitted_weights_u16"] == {str(k): v for k, v in pre.items()}
    assert epoch_work.republish(built, 200, 203) == "already_published_this_epoch"
    assert len(client.calls) == calls
