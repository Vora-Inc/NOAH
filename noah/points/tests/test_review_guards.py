"""Independent review reproductions at durable migration/publication boundaries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from noah.chain.foreign_value import ForeignDepositUnpriced
from noah.chain.sources import Refusal, SourceDisagreement, SourceLied, Unverified
from noah.core.calculation_record import encoded, retain
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.settings import SettingsError
from noah.points.engine import INPUT_SCHEMA, evaluate
from noah.points.mode import MODE, check_publication_mode
from noah.points.outage import authorize, record_failure, record_recovery
from noah.points.runtime import RuntimePolicy, configure
from noah.points.tests.test_engine import R, fixture
from noah.points.tests.test_integration import captured, next_day
from noah.points.tests.test_mode import runtime_input
from noah.points.tests.test_outage import setup
from noah.starter.runtime import republish


@pytest.mark.parametrize(
    "kind", [SourceDisagreement, SourceLied, Unverified, PublicationStatusUnknown, ValueError]
)
@pytest.mark.parametrize("wrapped", [False, True])
def test_contradiction_revokes_previous_outage_until_verified_recovery(
    tmp_path, monkeypatch, kind, wrapped
):
    adapter, chain, raw, source, now = setup(tmp_path, monkeypatch)
    assert record_failure(adapter, ForeignDepositUnpriced("offline"))
    assert authorize(adapter, source, now[0])
    error = kind("contradiction")
    if wrapped:
        # Implicit Python exception context must not hide an unsafe source.
        outer = ForeignDepositUnpriced("wrapper")
        outer.__context__ = error
        if kind is ValueError:
            # An unrecognized outer failure remains unsafe even if a nested
            # earlier error was classified as an ordinary availability failure.
            error.__cause__ = ForeignDepositUnpriced("earlier unavailable")
            outer = error
        error = outer
    record_failure(adapter, error)
    path = Path(adapter._submitted_record_dir) / "points-outage.json"
    assert json.loads(path.read_text())["reason"] == "UnsafeFailure"
    count = len(chain.calls)
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        republish(adapter, 560, 560)
    assert len(chain.calls) == count
    assert record_failure(adapter, ForeignDepositUnpriced("still offline")) is False
    with pytest.raises(PublicationStatusUnknown, match="no verified"):
        authorize(adapter, source, now[0])
    record_recovery(adapter)  # only an entire verified daily read removes the block
    assert record_failure(adapter, ForeignDepositUnpriced("later new outage"))
    assert authorize(adapter, source, now[0])


def test_legacy_restart_refuses_installed_points_even_without_active_overlay(tmp_path, monkeypatch):
    adapter, document, settings = runtime_input(tmp_path, monkeypatch)
    configure(adapter, settings)
    before = adapter.points_policy.path.read_bytes()
    monkeypatch.setattr(
        "noah.scoring.parameters.load_parameters",
        lambda: SimpleNamespace(profile_version="m1-revised-v1"),
    )
    with pytest.raises(SettingsError, match="reverse migration"):
        configure(adapter, SimpleNamespace(scoring_mode="legacy", points_policy=None))
    assert adapter.points_policy.path.read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["wrong_day", "not_last", "bool_time", "negative_time", "unfinalized_successor"]
)
def test_cutoff_requires_last_block_of_correct_utc_day_and_finalized_successor(tmp_path, damage):
    adapter, document = captured(tmp_path)
    m = document["migration"]
    end = adapter.day_anchor_ts + (m["cutoff_day"] + 1) * 86400
    if damage == "unfinalized_successor":
        adapter.runner_chain.head_block = lambda: m["cutoff_block"]
    else:
        cutoff = {"wrong_day": end, "not_last": end - 10, "bool_time": True, "negative_time": -1}[
            damage
        ]
        successor = end - 1 if damage == "not_last" else end
        adapter.runner_chain.block_timestamp = lambda b: (
            cutoff if b == m["cutoff_block"] else successor
        )
    with pytest.raises(ValueError, match="UTC day|successor"):
        RuntimePolicy(document, adapter)
    assert not (tmp_path / "points-policy.json").exists()


@pytest.mark.parametrize(
    "field",
    [
        "paidRao",
        "payUnlockedRao",
        "noahDepositRao",
        "foreignDepositNoahEquivRao",
        "entryDepositRao",
    ],
)
def test_returning_unmigrated_financial_lineage_refuses(tmp_path, field):
    adapter, document = captured(tmp_path)
    policy = RuntimePolicy(document, adapter)
    cutoff = policy.migration["cutoff_day"]
    raw = fixture([[(0, 0, 0)] * (cutoff + 2)])
    raw["miners"][0]["uid"] = 999
    if field == "entryDepositRao":
        raw["miners"][0][field] = str(R)
    else:
        raw["miners"][0]["days"][str(cutoff)][field] = str(R)
    with pytest.raises(Refusal, match="lineage"):
        policy.check_snapshot(adapter, raw, {999: "returning-hotkey"})


def test_same_wallet_new_hotkey_with_empty_old_rows_remains_allowed(tmp_path):
    adapter, document = captured(tmp_path)
    policy = RuntimePolicy(document, adapter)
    cutoff = policy.migration["cutoff_day"]
    raw = fixture([[(0, 0, 0)] * (cutoff + 2)])
    row = raw["miners"][0]
    row.update(
        uid=999,
        coldkey=document["migration"]["miners"][0]["coldkey"],
        registrationDay=str(cutoff + 1),
        entryDepositRao=str(R),
    )
    row["days"][str(cutoff + 1)]["noahDepositRao"] = str(R)
    row["days"][str(cutoff)]["paidLockedArrivalRao"] = str(R)
    assert int(row["firstActivityDay"]) <= cutoff
    assert policy.check_snapshot(adapter, raw, {999: "new-hotkey"})
    assert policy.fixture_migration(raw, {999: "new-hotkey"})["balances"] == []


@pytest.mark.parametrize("altered", [False, True])
def test_retained_publication_revalidates_frozen_history_not_only_genuine_seed(tmp_path, altered):
    adapter, document = captured(tmp_path)
    policy = RuntimePolicy(document, adapter)
    hotkeys = {r["uid"]: r["hotkey"] for r in policy.migration["miners"]}
    raw = next_day(document["fixture"])
    raw.update(schema=INPUT_SCHEMA, pointsMigration=policy.fixture_migration(raw, hotkeys))
    if altered:
        row = raw["miners"][3]
        first = min(row["days"], key=int)
        row["days"][first]["noahDepositRao"] = str(int(row["days"][first]["noahDepositRao"]) + R)
    score = evaluate(raw)
    context = dict(
        day=int(raw["evalDay"]),
        height=policy.migration["cutoff_block"] + 1,
        genesis_hash=policy.migration["genesis"],
        netuid=adapter.netuid,
        ark_address="ark",
        designated_validator_hotkey="val",
        scoring_mode=MODE,
        points_policy=policy.manifest,
        scored_hotkeys={str(k): v for k, v in hotkeys.items()},
    )
    ref = retain(
        tmp_path,
        fixture=raw,
        agreed_text=encoded(score).decode(),
        vector={int(k): v for k, v in score["weightsU16"].items()},
        context=context,
    )
    adapter.points_policy, adapter.scoring_mode = policy, MODE
    if altered:
        with pytest.raises(PublicationStatusUnknown, match="migration history"):
            check_publication_mode(adapter, ref)
    else:
        assert check_publication_mode(adapter, ref) == MODE
