"""Mode isolation and cutover at actual retention/publication boundaries."""

import json
from types import SimpleNamespace

import pytest

from noah.core.calculation_record import encoded, retain
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.settings import BadSetting, Settings, SettingsError
from noah.core.tests.test_settings import COMPLETE
from noah.points.mode import MODE, check_publication_mode
from noah.points.pricing import PriceInterval, daily_average_credit
from noah.points.runtime import PROFILE, RuntimePolicy, _check_cutover_state, configure
from noah.points.tests.test_integration import captured
from noah.scoring.engine import score_fixture
from noah.scoring.fixture_io import parse_fixture
from noah.scoring.tests.test_engine import day
from noah.starter.allocation import preview
from noah.starter.publication import retain_overlay
from noah.starter.tests.test_epoch_clock import request_live

R = 10**9


def test_legacy_no_fault_streak_retains_identical_daily_proof(tmp_path):
    raw = request_live()
    fixture = raw["fixture"]
    fixture["evalDay"] = "2"
    fixture["miners"][0]["days"]["1"]["paidRao"] = "0"
    fixture["miners"][0]["days"]["1"]["noFault"] = True
    for miner in fixture["miners"]:
        miner["days"]["2"] = day("0", "0", no_fault=True)
    snap = raw["snapshot"]
    snap["finalizedTimestamp"] += 86400
    snap["epochStartTimestamp"] += 86400
    snap["miningPoolSample"]["endTimestamp"] += 86400
    score = score_fixture(parse_fixture(fixture))
    assert score["miners"][0]["payWeightFp"] == "244506"
    assert preview(raw)["ordinaryOutput"] == score
    ref = retain(
        tmp_path,
        fixture=fixture,
        agreed_text=encoded(score).decode(),
        vector={int(k): v for k, v in score["weightsU16"].items()},
        context=dict(
            day=2, height=99, genesis_hash=snap["genesisHash"], netuid=82, ark_address="ark"
        ),
    )
    # This formerly refused because starter silently changed the legacy noFault flag.
    assert retain_overlay(tmp_path, ref, raw)["sha256"]
    assert check_publication_mode(SimpleNamespace(_submitted_record_dir=tmp_path), ref) == "legacy"


def test_native_subnet_is_configuration_not_mainnet_constant(tmp_path):
    rows = [PriceInterval(86400, 172800, R, 2 * R)]
    assert daily_average_credit(R, 82, 172800, rows, native_netuid=2) == 2 * R
    assert daily_average_credit(R, 2, 172800, [], native_netuid=2) == R
    adapter, document = captured(tmp_path)
    adapter.netuid = 2
    document["lineages"]["netuid"] = 2
    from noah.points.lineages import digest
    document["lineages"]["source_sha256"] = digest({k:v for k,v in document["lineages"].items() if k != "source_sha256"})
    policy = RuntimePolicy(document, adapter)
    policy.collector = SimpleNamespace(
        chain=SimpleNamespace(block_hash=lambda b: "new-hash", block_timestamp=lambda b: 172800),
        prior_day=lambda block, netuid: rows,
    )
    adapter._foreign_valuation_evidence = []
    assert (
        policy.foreign_rule(adapter, None)(
            R, 82, document["migration"]["cutoff_block"] + 1, "new-hash"
        )
        == 2 * R
    )


def test_settings_mode_defaults_and_requires_paired_opt_in():
    assert Settings.from_mapping(COMPLETE).scoring_mode == "legacy"
    for values in (
        {"NOAH_SCORING_MODE": MODE},
        {"NOAH_POINTS_POLICY": "/reviewed/policy.json"},
        {"NOAH_SCORING_MODE": "unknown"},
    ):
        with pytest.raises(BadSetting):
            Settings.from_mapping({**COMPLETE, **values})
    assert (
        Settings.from_mapping(
            {**COMPLETE, "NOAH_SCORING_MODE": MODE, "NOAH_POINTS_POLICY": "/reviewed/policy.json"}
        ).scoring_mode
        == MODE
    )


def runtime_input(tmp_path, monkeypatch):
    adapter, document = captured(tmp_path / "scores")
    adapter.starter_entry_policy = SimpleNamespace(snapshot={"sha256": "a" * 64})
    adapter.epoch_collection = adapter.epoch_republication = True
    adapter.publisher = SimpleNamespace(version_key=1)
    adapter.runner_chain.starter_epoch_info = lambda h: {"WeightsVersionKey": 0}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(document))
    settings = SimpleNamespace(scoring_mode=MODE, points_policy=path)
    monkeypatch.setattr(
        "noah.scoring.parameters.load_parameters", lambda: SimpleNamespace(profile_version=PROFILE)
    )
    return adapter, document, settings


def test_preflight_validates_without_installing_policy(tmp_path, monkeypatch):
    adapter, document, settings = runtime_input(tmp_path, monkeypatch)
    configure(adapter, settings, persist=False)
    assert not adapter.points_policy.path.exists()
    assert not (tmp_path / "scores").exists()
    configure(adapter, settings)
    assert adapter.points_policy.path.read_bytes() == encoded(document)
    configure(adapter, settings)  # exact restart is idempotent


def test_refused_version_floor_does_not_pin_policy(tmp_path, monkeypatch):
    adapter, document, settings = runtime_input(tmp_path, monkeypatch)
    adapter.runner_chain.starter_epoch_info = lambda h: {"WeightsVersionKey": 2**64 - 1}
    with pytest.raises(SettingsError, match="floor"):
        configure(adapter, settings)
    assert not (tmp_path / "scores").exists()


def test_legacy_mode_refuses_points_profile_without_starter(tmp_path, monkeypatch):
    adapter, document, settings = runtime_input(tmp_path, monkeypatch)
    with pytest.raises(SettingsError, match="profile"):
        configure(adapter, SimpleNamespace(scoring_mode="legacy", points_policy=None))


def test_points_replay_must_follow_legacy_cutoff(tmp_path):
    from noah.chain.sources import Refusal

    adapter, document = captured(tmp_path)
    policy = RuntimePolicy(document, adapter)
    with pytest.raises(Refusal, match="cutoff"):
        policy.check_snapshot(adapter, document["fixture"], {})


def test_previous_mode_send_is_recoverable_but_cannot_be_republished(tmp_path, monkeypatch):
    from noah.core.direct import reconcile
    from noah.points.tests.test_outage import setup

    adapter, chain, raw, source, now = setup(tmp_path, monkeypatch)
    # Read the real confirmed marker; changing mode never changes recovery identity.
    from pathlib import Path

    active = json.loads((Path(adapter._submitted_record_dir) / "starter-active.json").read_text())
    marker = active["marker"]
    adapter.scoring_mode = "legacy"
    del adapter.points_policy
    adapter._check_direct_identity(marker)
    assert reconcile(adapter.reader, marker, finalized_chain=adapter.runner_chain)
    with pytest.raises(PublicationStatusUnknown, match="mode"):
        check_publication_mode(adapter, source)
    with pytest.raises(SettingsError, match="overlay"):
        _check_cutover_state(adapter, "legacy")


def test_only_same_policy_pending_send_can_restart(tmp_path, monkeypatch):
    from noah.points.tests.test_outage import setup

    adapter, chain, raw, source, now = setup(tmp_path, monkeypatch)
    adapter._publish_intent = SimpleNamespace(load=lambda: {"calculation_evidence": source})
    _check_cutover_state(adapter, MODE, "pinned-policy")
    with pytest.raises(SettingsError, match="pending"):
        _check_cutover_state(adapter, "legacy")
    with pytest.raises(SettingsError, match="pending"):
        _check_cutover_state(adapter, MODE, "different-policy")


def test_cutover_waits_for_fresh_points_day_without_reusing_legacy(tmp_path):
    from noah.starter.runtime import guard_tick, republish
    from noah.starter.tests.test_runtime import ready

    adapter, chain, raw, source = ready(tmp_path)
    adapter.scoring_mode = MODE
    adapter.points_policy = SimpleNamespace(manifest={"mode": MODE}, migration={"cutoff_day": 1})
    assert guard_tick(adapter)["status"] == "idle"
    count = len(chain.calls)
    assert republish(adapter, 200, 201) == "awaiting_points_first_day"
    assert len(chain.calls) == count
