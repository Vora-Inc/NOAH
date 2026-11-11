"""Actual production adapter dispatch/retention, with real Python/TS engines."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from noah.core.calculation_record import CalculationEvidenceRefused
from noah.core.chain_adapter import ChainAdapter
from noah.points import ema_policy
from noah.points.mode import source_mode
from noah.points.tests.test_ema_reference import EmaChain, ema_fixture


@pytest.fixture(autouse=True)
def profile(monkeypatch):
    # The maintained runner selects a valid profile in both private and public trees.
    if os.environ.get("NOAH_PARAMETERS_JSON"):
        return
    for root in Path(__file__).resolve().parents:
        candidate = root / "docs/parameters/profiles/m1-revised-v1.json"
        if candidate.is_file():
            monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(candidate))
            return
    raise AssertionError("test profile is absent from this source tree")


def adapter(tmp_path, fixture):
    a = object.__new__(ChainAdapter)
    a.runner_chain = EmaChain()
    a.netuid = 2
    a.day_anchor_ts = 0
    a.settle_blocks = 0
    a._submitted_record_dir = tmp_path
    a._day_window = lambda _: (2, 48, 71)
    a._scored_seat_hotkeys = (71, {int(k): v for k, v in fixture["pointsHotkeys"].items()})
    a._scored_seat_coldkeys = (71, {int(k): v for k, v in fixture["pointsOwners"].items()})
    a.last_confirmed_ark_pin = "ark"
    a.designated_validator_hotkey = "validator"
    a.wallet_hotkey = SimpleNamespace(ss58_address="validator")
    a.dials = fixture["dials"]
    a.points_policy = SimpleNamespace(manifest=dict(policy=ema_policy.POLICY, sha256="a" * 64))
    a.scoring_mode = ema_policy.MODE
    a._foreign_valuation_evidence = []
    a._points_lineage_capture = None
    a.publish_mode = "direct"
    a.publisher = SimpleNamespace()
    return a


def test_actual_adapter_scores_agrees_and_retains_v4_owner_context(tmp_path):
    f, _ = ema_fixture(tmp_path)
    a = adapter(tmp_path, f)
    score = a.score(f)
    assert score["schema"] == ema_policy.OUTPUT_SCHEMA and score["policy"] == ema_policy.POLICY
    vector = a.agree(f)
    assert vector == {int(k): v for k, v in score["weightsU16"].items()}
    reference = a.record_calculation(71, f, vector, vector)
    mode, context, _ = source_mode(tmp_path, reference)
    assert mode == ema_policy.MODE and context["scored_coldkeys"] == f["pointsOwners"]
    assert json.loads(a._agreed_calculation[1]) == score


def test_actual_adapter_retention_refuses_changed_authenticated_roster(tmp_path):
    f, _ = ema_fixture(tmp_path)
    a = adapter(tmp_path, f)
    vector = a.agree(f)
    a._scored_seat_coldkeys = (71, {0: "different"})
    with pytest.raises(CalculationEvidenceRefused, match="roster"):
        a.record_calculation(71, f, vector, vector)


def test_actual_starter_allocation_uses_retained_ema_ordinary_output(tmp_path):
    from noah.research.tests.test_starter_epoch import sync
    from noah.starter.allocation import preview
    from noah.starter.tests.test_epoch_clock import request_live

    f, _ = ema_fixture(tmp_path)
    a = adapter(tmp_path, f)
    vector = a.agree(f)
    reference = a.record_calculation(71, f, vector, vector)
    from noah.core.calculation_record import verify

    retained = verify(tmp_path, reference)
    raw = request_live()
    from noah.starter.publication import artifact

    raw["fixture"] = artifact(tmp_path, retained, "fixture")
    raw["scoredHotkeys"] = f["pointsHotkeys"]
    snap = raw["snapshot"]
    end = int(f["dayEpochTs"]) + (int(f["evalDay"]) + 1) * 86400
    snap.update(
        genesisHash=EmaChain().genesis_hash(),
        netuid=2,
        finalizedTimestamp=end,
        epochStartTimestamp=end - 100,
    )
    snap["miningPoolSample"]["endTimestamp"] = end
    snap["seats"] = [
        dict(
            uid=int(uid),
            coldkey=f["pointsOwners"][uid],
            hotkey=hot,
            observedBlock=snap["finalizedBlock"],
            spendableEarnedRao=20 * 10**9,
            pendingExpectedRao=0,
            ownerExcluded=False,
        )
        for uid, hot in f["pointsHotkeys"].items()
    ]
    raw["lifetimeEarnings"] = []
    sync(raw)
    result = preview(raw)
    assert result["ordinaryOutput"] == artifact(tmp_path, retained, "agreed_score")
    assert result["ordinaryOutput"]["schema"] == ema_policy.OUTPUT_SCHEMA
    from noah.starter.publication import retain_overlay, verify_overlay

    overlay = retain_overlay(tmp_path, reference, raw)
    assert verify_overlay(tmp_path, overlay)["mode"] == "starter-overlay"
