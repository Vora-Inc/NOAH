"""Approved ordinary onboarding, without an allowance or reserved emissions."""

import pytest

from noah.points import onboarding
from noah.points.ledger import settle

R = 10**9


def test_newcomer_earns_before_any_purchase_or_deposit():
    s = onboarding.initial_state()
    assert s.points == 200000
    assert settle(s, 0, 0, rules=onboarding.RULES).points == 200000


def test_first_actual_earnings_due_next_day_then_half_and_zero():
    s = onboarding.initial_state()
    s = settle(s, 2 * R, 0, rules=onboarding.RULES)
    assert (s.balance, s.points, s.missed) == (-R, 100000, 1)
    s = settle(s, R, 0, rules=onboarding.RULES)
    assert (s.balance, s.points, s.missed) == (-1500000000, 0, 2)
    s = settle(s, 0, 1500000000, rules=onboarding.RULES)
    assert s.points > 0 and s.streak == 0 and s.missed == 0


def test_prepaid_is_coverage_not_a_fabricated_daily_deposit():
    s = settle(onboarding.initial_state(), 100 * R, 100 * R, rules=onboarding.RULES)
    s = settle(s, 100 * R, 0, rules=onboarding.RULES)
    assert s.balance == 0 and s.missed == 0 and s.points > 0
    assert s.streak == 0


def test_ninety_compliant_days_mature_and_thirty_payments_full_consistency():
    s = onboarding.initial_state()
    for _ in range(90):
        s = settle(s, 100 * R, 75 * R, rules=onboarding.RULES)
    assert (s.points, s.maturity, s.contribution, s.consistency) == (
        100000000,
        1000000,
        40000000,
        40000000,
    )
    assert settle(s, 0, 0, rules=onboarding.RULES).qualified == 90


def test_native_and_foreign_only_equal_with_no_entry_requirement():
    from noah.points.run import agreed
    from noah.points.tests.test_engine import fixture

    a = fixture([[(100 * R, 0, 0), (0, 50 * R, 0)]])
    a.update(pointsRules=onboarding.RULES, pointsExcludedUids=[])
    b = fixture([[(100 * R, 0, 0), (0, 0, 50 * R)]])
    b.update(pointsRules=onboarding.RULES, pointsExcludedUids=[])
    assert agreed(a) == agreed(b)
    assert agreed(b)["miners"][0]["state"]["points"] == "200444"


def test_randomized_full_state_python_typescript_agreement():
    import json
    import random
    import subprocess
    from pathlib import Path

    from noah.points.engine import evaluate
    from noah.points.tests.test_engine import fixture

    rng = random.Random(204040)
    raws = []
    for case in range(150):
        raw = fixture(
            [
                [
                    (
                        rng.choice([0, 1, 100 * R]),
                        rng.choice([0, 1, 25 * R, 50 * R, 100 * R]),
                        rng.choice([0, 25 * R, 100 * R]),
                    )
                    for day in range(1 + case)
                ]
                for _ in range(1 + case % 5)
            ]
        )
        raw.update(pointsRules=onboarding.RULES, pointsExcludedUids=[0] if case % 2 else [])
        raws.append(raw)
    script = Path(__file__).resolve().parents[3] / "verification/noah-ts/src/points.ts"
    js = (
        f"import {{evaluatePoints}} from {json.dumps(script.as_uri())};"
        "let s='';for await(const c of process.stdin)s+=c;"
        "console.log(JSON.stringify(JSON.parse(s).map(x=>evaluatePoints(JSON.stringify(x))),"
        "(_,v)=>typeof v==='bigint'?v.toString():v));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", js],
        input=json.dumps(raws),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert [evaluate(r) for r in raws] == json.loads(proc.stdout)


def test_no_starter_overlay_or_guard_in_ordinary_mode():
    from types import SimpleNamespace

    from noah.core.chain_adapter import ChainAdapter
    from noah.starter.runtime import guard_tick

    a = SimpleNamespace(scoring_mode=onboarding.MODE, starter_entry_policy=object())
    vector = {1: 100, 2: 65435}
    assert ChainAdapter.prepare_starter_vector(a, vector) == vector
    assert guard_tick(a) == dict(status="disabled-no-pool", head=None)


def test_all_newcomers_receive_real_quantized_weight_among_mature_miners():
    from noah.scoring.pay import weights_u16

    scores = {i: 100000000 for i in range(72)}
    scores.update({i: 200000 for i in range(72, 82)})
    weights = weights_u16(scores)
    assert sum(weights.values()) == 65535
    assert all(weights[i] >= 1 for i in range(72, 82))


def test_tampered_rules_refused_by_both_engines():
    import pytest

    from noah.points.engine import evaluate
    from noah.points.run import typescript_text
    from noah.points.tests.test_engine import fixture

    f = fixture([[(0, 0, 0)]])
    f.update(pointsRules={**onboarding.RULES, "maturityStart": 20}, pointsExcludedUids=[])
    for fn in [evaluate, typescript_text]:
        with pytest.raises(ValueError):
            fn(f)


def test_runtime_preserves_ema_migration_but_changes_scoring_identity(tmp_path):
    from noah.core.calculation_record import encoded
    from noah.points import amendment, ema_policy
    from noah.points.runtime import RuntimePolicy
    from noah.points.tests.test_integration import captured

    a, old = captured(tmp_path)
    original = encoded(old)
    prior = encoded(amendment.build(original))
    at = old["migration"]["cutoff_block"] + 2
    doc = ema_policy.build(prior, {"block": at, "hash": old["migration"]["cutoff_hash"]})
    a.runner_chain.head_block = lambda: at + 1
    for name, body in [
        ("points-policy.json", encoded(doc)),
        ("points-policy-predecessor.json", original),
        ("points-policy-ema-predecessor.json", prior),
    ]:
        (tmp_path / name).write_bytes(body)
    before = RuntimePolicy(doc, a)
    a.scoring_mode = onboarding.MODE
    after = RuntimePolicy(doc, a)
    assert after.no_pool and after.migration == before.migration
    assert after.baselines == before.baselines
    assert after.manifest["policy"] == onboarding.POLICY
    assert after.manifest["sha256"] != before.manifest["sha256"]
    assert after.manifest["pricing_policy_sha256"] == before.manifest["sha256"]
    assert after.input_schema == before.input_schema
    assert (tmp_path / "points-policy.json").read_bytes() == encoded(doc)
    # Startup must not require any live allocator, allowance state or guard.
    from types import SimpleNamespace
    from unittest.mock import patch

    from noah.points.runtime import configure

    a.starter_entry_policy = None
    a.entry_history_policy = SimpleNamespace(
        snapshot={
            "sha256": old["lineages"]["entry_snapshot_sha256"],
            "payload": {"records": old["lineages"]["entry_pairs"]},
        }
    )
    a.epoch_collection = a.epoch_republication = True
    a.publisher = SimpleNamespace(version_key=0)
    a.runner_chain.starter_epoch_info = lambda h: {"WeightsVersionKey": 0}
    settings = SimpleNamespace(
        scoring_mode=onboarding.MODE, points_policy=tmp_path / "points-policy.json"
    )
    with patch(
        "noah.scoring.parameters.load_parameters",
        return_value=SimpleNamespace(profile_version=onboarding.PROFILE),
    ):
        configure(a, settings, persist=False)
    assert a.points_policy.migration == before.migration
    assert a.starter_entry_policy is None


def test_real_adapter_twin_retention_and_no_overlay(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from noah.points.mode import source_mode
    from noah.points.tests.test_ema_adapter import adapter
    from noah.points.tests.test_ema_reference import ema_fixture

    service = Path(__file__).resolve().parents[3]
    relative = "docs/parameters/profiles/m1-revised-v1.json"
    profile = next(p for p in (service / relative, service.parents[1] / relative) if p.is_file())
    monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(profile))
    f, _ = ema_fixture(tmp_path)
    f.update(pointsRules=onboarding.RULES, pointsExcludedUids=[])
    a = adapter(tmp_path, f)
    a.scoring_mode = onboarding.MODE
    a.points_policy.manifest = onboarding.manifest(a.points_policy.manifest)
    a.starter_entry_policy = SimpleNamespace(snapshot={})
    score = a.score(f)
    vector = a.agree(f)
    assert score["policy"] == onboarding.POLICY
    assert a.prepare_starter_vector(vector) == vector
    reference = a.record_calculation(71, f, vector, vector)
    assert source_mode(tmp_path, reference)[0] == onboarding.MODE
    assert not (tmp_path / "starter-state.json").exists()
    assert not (tmp_path / "starter-guard.json").exists()


def test_new_mode_requires_matching_profile_and_no_implicit_default(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from noah.core.settings import SettingsError
    from noah.points.runtime import validate_configuration

    monkeypatch.setattr(
        "noah.scoring.parameters.load_parameters",
        lambda: SimpleNamespace(profile_version=onboarding.PROFILE),
    )
    assert (
        validate_configuration(
            SimpleNamespace(scoring_mode=onboarding.MODE, points_policy="policy.json")
        )
        == onboarding.MODE
    )
    with pytest.raises(SettingsError):
        validate_configuration(SimpleNamespace(scoring_mode=onboarding.MODE, points_policy=None))


def test_reserved_pool_cannot_be_reintroduced_through_old_preview_tool():
    import pytest

    from noah.starter.allocation import preview
    from noah.starter.tests.test_epoch_clock import request_live

    raw = request_live()
    raw["fixture"].update(
        schema="noah-points-fixture/1", pointsRules=onboarding.RULES, pointsExcludedUids=[]
    )
    with pytest.raises(ValueError, match="no starter pool"):
        preview(raw)


def test_plain_epoch_outage_refresh_expires_and_never_builds_pool(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import pytest

    from noah.chain.foreign_value import ForeignDepositUnpriced
    from noah.core.calculation_record import encoded, retain
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.core.submitted_record import submitted_record_path
    from noah.points.outage import record_failure, republish_plain
    from noah.points.run import agreed
    from noah.points.tests.test_ema_reference import ema_fixture

    f, _ = ema_fixture(tmp_path)
    f.update(pointsRules=onboarding.RULES, pointsExcludedUids=[])
    score = agreed(f)
    manifest = {"policy": onboarding.POLICY, "sha256": "a" * 64, "cutoff_day": 0}
    day = int(f["evalDay"])
    now = [(day + 2) * 86400]
    context = {
        "day": day,
        "height": 71,
        "netuid": 2,
        "genesis_hash": "g",
        "ark_address": "ark",
        "designated_validator_hotkey": "validator",
        "scoring_mode": onboarding.MODE,
        "points_policy": manifest,
        "to_block": 71,
        "to_block_hash": "h71",
        "scored_hotkeys": f["pointsHotkeys"],
        "scored_coldkeys": f["pointsOwners"],
    }
    vector = {int(k): v for k, v in score["weightsU16"].items()}
    source = retain(
        tmp_path, fixture=f, agreed_text=encoded(score).decode(), vector=vector, context=context
    )
    marker = {
        "day": day,
        "height": 71,
        "calculation_evidence": source,
        "vector": {str(k): v for k, v in vector.items()},
    }
    path = submitted_record_path(tmp_path, day)
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps({"direct_intent": marker}))
    sent = []
    a = SimpleNamespace(
        _submitted_record_dir=tmp_path,
        scoring_mode=onboarding.MODE,
        points_policy=SimpleNamespace(manifest=manifest),
        day_anchor_ts=0,
        netuid=2,
        ark_coldkey="ark",
        designated_validator_hotkey="validator",
        reader=object(),
        runner_chain=SimpleNamespace(
            block_timestamp=lambda h: now[0],
            head_block=lambda: 100,
            genesis_hash=lambda: "g",
            block_hash=lambda h: "h71",
            seat_hotkeys=lambda h: {int(k): v for k, v in f["pointsHotkeys"].items()},
            hotkey_owners=lambda keys, h: {
                f["pointsHotkeys"][k]: v for k, v in f["pointsOwners"].items()
            },
        ),
        owner_uids=lambda: [],
        _check_direct_identity=lambda m: None,
        last_update=lambda *a: 1,
        uid=1,
        rate_limit=lambda n: 1,
        publish_direct=lambda *a, **k: sent.append((a, k)),
    )
    monkeypatch.setattr("noah.core.direct.reconcile", lambda *a, **k: None)
    assert record_failure(a, ForeignDepositUnpriced("offline"))
    assert republish_plain(a, 100, 100, before_send=lambda: None) == "points_outage_refreshed"
    assert sent[0][0][0] == vector
    assert not (tmp_path / "starter-state.json").exists()
    # A UID is a replaceable seat, never a permanent payment identity.
    original_seats = a.runner_chain.seat_hotkeys
    a.runner_chain.seat_hotkeys = lambda h: {int(k): "replacement" for k in f["pointsHotkeys"]}
    with pytest.raises(PublicationStatusUnknown, match="identit"):
        republish_plain(a, 101, 100, before_send=lambda: None)
    assert len(sent) == 1
    # Exercise the actual transport boundary too, not only the refresh helper.
    from noah.core.chain_adapter import ChainAdapter
    from noah.core.publish import PublicationRefused

    with pytest.raises(PublicationRefused, match="nothing submitted"):
        ChainAdapter.submit_direct(a, list(vector), list(vector.values()), 2)
    a.runner_chain.seat_hotkeys = original_seats
    original_owners = a.runner_chain.hotkey_owners
    a.runner_chain.hotkey_owners = lambda keys, h: {key: "replacement-owner" for key in keys}
    with pytest.raises(PublicationStatusUnknown, match="ownership"):
        republish_plain(a, 102, 100, before_send=lambda: None)
    assert len(sent) == 1
    a.runner_chain.hotkey_owners = original_owners
    a.owner_uids = lambda: list(vector)
    with pytest.raises(PublicationStatusUnknown, match="owner seat"):
        republish_plain(a, 103, 100, before_send=lambda: None)
    a.owner_uids = lambda: []

    def replace_after_preflight():
        a.runner_chain.seat_hotkeys = lambda h: {}

    with pytest.raises(PublicationStatusUnknown, match="identit"):
        republish_plain(a, 104, 100, before_send=replace_after_preflight)
    assert len(sent) == 1
    a.runner_chain.seat_hotkeys = original_seats
    now[0] += 86400
    from noah.points.outage import authorize

    with pytest.raises(PublicationStatusUnknown, match="24 hours"):
        authorize(a, source, now[0])
    assert republish_plain(a, 200, 200, before_send=lambda: None) == "awaiting_daily_publication"
    assert len(sent) == 1


def test_no_pool_profile_cannot_silently_run_legacy_scoring(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from noah.core.settings import SettingsError
    from noah.points.runtime import validate_configuration

    monkeypatch.setattr(
        "noah.scoring.parameters.load_parameters",
        lambda: SimpleNamespace(profile_version=onboarding.PROFILE),
    )
    with pytest.raises(SettingsError, match="parameter profile"):
        validate_configuration(SimpleNamespace(scoring_mode="legacy", points_policy=None))


def test_entry_history_load_does_not_start_allocator(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from noah.chain.migration_fields import digest
    from noah.core.epoch_work import configure

    payload = dict(
        schema="noah-frozen-entry/1",
        genesisHash="g",
        netuid=82,
        finalizedBlock=1,
        finalizedHash="h",
        asOfDay=0,
        legacyEntryRao=10_000_000_000,
        records=[],
    )
    snapshot = dict(payload=payload, sha256=digest(payload))
    path = tmp_path / "entry.json"
    path.write_text(json.dumps(snapshot))
    settings = SimpleNamespace(
        scoring_mode=onboarding.MODE,
        points_policy="p",
        starter_entry_snapshot=path,
        epoch_collection=True,
        epoch_republication=True,
        publish_mode="direct",
    )
    a = SimpleNamespace(_publish_intent=object(), _submitted_record_dir=tmp_path)
    monkeypatch.setattr(
        "noah.scoring.parameters.load_parameters",
        lambda: SimpleNamespace(profile_version=onboarding.PROFILE),
    )
    # Separate startup dispatch from the full policy authentication test above.
    monkeypatch.setattr("noah.points.runtime.configure", lambda *args: None)

    def forbidden(*args):
        raise AssertionError("live starter runtime was invoked")

    monkeypatch.setattr("noah.starter.runtime.configure", forbidden)
    configure(a, settings)
    assert a.entry_history_policy.snapshot == snapshot
    assert a.starter_entry_policy is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["entry.json"]


def test_publication_lock_remains_active_without_pool(tmp_path):
    from types import SimpleNamespace

    import pytest

    from noah.core.publication_lock import publication_lock
    from noah.core.writer_lock import LockHeld, held

    a = SimpleNamespace(
        scoring_mode=onboarding.MODE, starter_entry_policy=None, _submitted_record_dir=tmp_path
    )
    with held(tmp_path / "starter-publication.lock"):
        with pytest.raises(LockHeld):
            with publication_lock(a):
                pass


def test_new_mode_cannot_submit_without_retained_calculation():
    from types import SimpleNamespace

    import pytest

    from noah.core.chain_adapter import ChainAdapter
    from noah.core.publish import PublicationRefused

    def unexpected_transport(*args, **kwargs):
        raise AssertionError("transport reached without calculation proof")

    a = SimpleNamespace(
        scoring_mode=onboarding.MODE,
        _calculation_reference=None,
        publisher=SimpleNamespace(submit_direct=unexpected_transport),
        _publish_intent=SimpleNamespace(persist_prepared=lambda: None),
        runner_chain=object(),
    )
    with pytest.raises(PublicationRefused, match="retained calculation"):
        ChainAdapter.submit_direct(a, [1], [65535], 82)


def ordinary_published(tmp_path):
    from types import SimpleNamespace

    from noah.core.calculation_record import encoded, retain
    from noah.core.tests.test_direct_runtime475 import adapter
    from noah.core.tests.test_epoch_work import EpochClient
    from noah.points.run import agreed
    from noah.points.tests.test_engine import fixture

    a, client = adapter(tmp_path, EpochClient())
    f = fixture([[(100 * R, 0, 0), (0, 75 * R, 0)]])
    f["miners"][0]["uid"] = 3
    from noah.chain.ss58 import address_of

    hot = cold = address_of(bytes([7]) * 32)
    f["miners"][0]["coldkey"] = cold
    f.update(
        schema="noah-points-fixture/3",
        pointsRules=onboarding.RULES,
        pointsExcludedUids=[0],
        pointsReceipts=[],
        pointsPricing={},
        pointsHotkeys={"3": hot},
        pointsOwners={"3": cold},
        pointsPricingBoundary=dict(
            anchor={"block": 1, "hash": "0x" + "1" * 64},
            effectiveBlock=2,
            policySha256="a" * 64,
            predecessorPolicySha256="b" * 64,
            emaSource="SubnetMovingPrice",
            genesis="0x" + "a" * 64,
            nativeNetuid=82,
        ),
    )
    score = agreed(f)
    vector = {int(k): v for k, v in score["weightsU16"].items()}
    day = int(f["evalDay"])
    a.scoring_mode = onboarding.MODE
    a.starter_entry_policy = None
    a.epoch_republication = True
    a.points_policy = SimpleNamespace(
        manifest={"policy": onboarding.POLICY, "sha256": "a" * 64, "cutoff_day": 0}
    )
    a._day_window = lambda h: (day, 1, 99)
    a.day_anchor_ts = int(f["dayEpochTs"])
    a.runner_chain.genesis_hash = lambda: "genesis"
    a.runner_chain.block_timestamp = lambda h: a.day_anchor_ts + (day + 1) * 86400 + 100
    a.runner_chain.seat_hotkeys = lambda h: {3: hot}
    a.runner_chain.hotkey_owners = lambda keys, h: {hot: cold}
    a.owner_uids = lambda: {0}
    source = retain(
        a._submitted_record_dir,
        fixture=f,
        agreed_text=encoded(score).decode(),
        vector=vector,
        context=dict(
            day=day,
            height=99,
            to_block=99,
            to_block_hash=f"0x{99:064x}",
            netuid=82,
            genesis_hash="genesis",
            ark_address=a.ark_coldkey,
            designated_validator_hotkey=a.designated_validator_hotkey,
            scoring_mode=onboarding.MODE,
            points_policy=a.points_policy.manifest,
            scored_hotkeys={"3": hot},
            scored_coldkeys={"3": cold},
        ),
    )
    a._calculation_reference = source
    a.publish_direct(vector, 99)
    assert dict(client.stored)[3] == 65535
    assert a._publish_intent.load() is None
    assert not list((tmp_path / "scores").glob("starter-*.json"))
    return a, client, f, vector, day


def test_real_direct_publication_and_lost_reply_recovery_without_pool(tmp_path):
    import json

    from noah.core.direct import reconcile
    from noah.core.publish import NotOnChain

    a, client, _, vector, day = ordinary_published(tmp_path)
    daily = (tmp_path / f"scores/day-{day}.submitted.json").read_bytes()
    client.block = client.finalized_head = 201
    client.lost_reply = True
    with pytest.raises(NotOnChain):
        a.publish_direct(vector, 99, refresh_epoch=200, source_day=day)
    marker = a._publish_intent.load()
    assert marker is not None
    client.lost_reply = False
    evidence = reconcile(a.reader, marker, finalized_chain=a.runner_chain)
    a._save_direct_confirmation(marker, evidence)
    a._publish_intent.clear()
    assert len(client.calls) == 2
    assert (tmp_path / f"scores/day-{day}.submitted.json").read_bytes() == daily
    receipt = json.loads(
        (tmp_path / f"scores/epoch-publications/200/day-{day}.submitted.json").read_text()
    )
    assert receipt["direct_intent"]["result"]["block"] == 202
    assert not list((tmp_path / "scores").glob("starter-*.json"))


def ordinary_outage_after_midnight(tmp_path, lost_reply=False):
    from noah.core.publish import NotOnChain
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.core.reader import ChainUnreadable
    from noah.core.tests.test_direct_runtime475 import adapter
    from noah.points.outage import authorize, bounds, record_failure, republish_plain

    a, client, f, vector, day = ordinary_published(tmp_path)
    source = a._calculation_reference
    expiry = a.day_anchor_ts + (day + 3) * 86400
    assert bounds(a, day)[1] == expiry
    client.block = client.finalized_head = 201
    # Final check is one second BEFORE expiry. Inclusion is AFTER midnight.
    a.runner_chain.block_timestamp = lambda h: expiry - 1 if h <= 201 else expiry + 1 + h - 202
    assert record_failure(a, ChainUnreadable("offline pricing source unavailable"))
    client.lost_reply = lost_reply
    if lost_reply:
        with pytest.raises(NotOnChain):
            republish_plain(a, 200, 201, before_send=lambda: None)
    else:
        assert republish_plain(a, 200, 201, before_send=lambda: None) == "points_outage_refreshed"
    client.lost_reply = False
    client.block = client.finalized_head
    # Restart creates a new adapter, preserving only disk and chain facts.
    previous = a
    a, _ = adapter(tmp_path, client)
    a.runner_chain = previous.runner_chain
    a.day_anchor_ts = previous.day_anchor_ts
    a.scoring_mode = onboarding.MODE
    a.points_policy = previous.points_policy
    a.owner_uids = previous.owner_uids
    a._day_window = lambda h: (day + 2, 102, 201)
    with pytest.raises(PublicationStatusUnknown, match="expired"):
        authorize(a, source, expiry)
    return a, client, f, vector, day


@pytest.mark.parametrize("lost_reply", [False, True])
def test_ordinary_outage_midnight_restart_recovers_old_receipt_and_publishes_once(
    tmp_path, lost_reply
):
    import copy

    from noah.core.calculation_record import encoded, retain
    from noah.core.publish import PublicationRefused
    from noah.points.run import agreed

    a, client, f, vector, day = ordinary_outage_after_midnight(tmp_path, lost_reply)
    # Recovery must still verify the older send; expiry forbids NEW sends only.
    assert a._already_published_direct(201) is False
    assert a._publish_intent.load() is None
    assert len(client.calls) == 2
    assert (tmp_path / f"scores/epoch-publications/200/day-{day}.submitted.json").exists()
    assert not (tmp_path / f"scores/day-{day + 2}.submitted.json").exists()
    # Publish the newly completed day using fresh independently computed evidence.
    fresh = copy.deepcopy(f)
    fresh["evalDay"] = str(day + 2)
    for d in (day + 1, day + 2):
        fresh["miners"][0]["days"][str(d)] = dict(
            paidRao="0",
            paidLockedArrivalRao="0",
            payUnlockedRao="0",
            noahDepositRao="0",
            foreignDepositNoahEquivRao="0",
            noFault=False,
        )
    score = agreed(fresh)
    next_vector = {int(k): v for k, v in score["weightsU16"].items()}
    a._calculation_reference = retain(
        a._submitted_record_dir,
        fixture=fresh,
        agreed_text=encoded(score).decode(),
        vector=next_vector,
        context=dict(
            day=day + 2,
            height=201,
            to_block=201,
            to_block_hash=f"0x{201:064x}",
            netuid=82,
            genesis_hash="genesis",
            ark_address=a.ark_coldkey,
            designated_validator_hotkey=a.designated_validator_hotkey,
            scoring_mode=onboarding.MODE,
            points_policy=a.points_policy.manifest,
            scored_hotkeys=f["pointsHotkeys"],
            scored_coldkeys=f["pointsOwners"],
        ),
    )
    a.publish_direct(next_vector, 201)
    assert len(client.calls) == 3
    assert a._already_published_direct(201) is True
    with pytest.raises(PublicationRefused, match="already confirmed"):
        a.publish_direct(next_vector, 201)
    assert len(client.calls) == 3


@pytest.mark.parametrize(
    "damage",
    [
        "missing_receipt",
        "missing_source",
        "changed_vector",
        "changed_identity",
        "changed_hash",
        "changed_calculation",
        "changed_nonce",
        "same_day",
        "wrong_filename",
        "other_mechanism",
        "second_transaction",
        "missing_event_index",
    ],
)
def test_older_outage_receipt_never_exempts_unknown_or_tampered_send(tmp_path, damage):
    import json

    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.core.tests.test_direct_runtime475 import event

    a, client, _, _, day = ordinary_outage_after_midnight(tmp_path)
    path = tmp_path / f"scores/epoch-publications/200/day-{day}.submitted.json"
    record = json.loads(path.read_text())
    marker = record["direct_intent"]
    if damage == "missing_receipt":
        path.unlink()
    elif damage == "missing_source":
        (tmp_path / f"scores/day-{day}.submitted.json").unlink()
    elif damage == "changed_vector":
        marker["vector"]["3"] = 65534
    elif damage == "changed_identity":
        marker["hotkey"] = "other"
    elif damage == "changed_hash":
        marker["result"]["block_hash"] = "0xwrong"
    elif damage == "changed_calculation":
        (tmp_path / "scores" / marker["calculation_evidence"]["path"]).write_text("{}")
    elif damage == "changed_nonce":
        client.blocks[202]["extrinsics"][2]["nonce"] += 1
    elif damage == "same_day":
        marker["day"] = day + 2
    elif damage == "other_mechanism":
        client.blocks[202]["events"].append(event("SubtensorModule", "WeightsSet", [4178, 0], 3))
    elif damage == "second_transaction":
        client.blocks[202]["events"].append(event("SubtensorModule", "WeightsSet", [82, 0], 3))
    elif damage == "missing_event_index":
        client.blocks[202]["events"].append(
            {
                "event": {
                    "module_id": "SubtensorModule",
                    "event_id": "WeightsSet",
                    "attributes": [82, 0],
                }
            }
        )
    if path.exists():
        path.write_text(json.dumps(record))
    if damage == "wrong_filename":
        path.rename(path.with_name(f"day-{day - 1}.submitted.json"))
    with pytest.raises(PublicationStatusUnknown, match="unattributed direct"):
        a._already_published_direct(201)
    assert len(client.calls) == 2
    assert not (tmp_path / f"scores/day-{day + 2}.submitted.json").exists()
