"""Real scoring/retention/allocation boundaries, simulated finalized reader only."""

import copy
import json
from types import SimpleNamespace

import pytest

from noah.chain.sources import Refusal
from noah.core.calculation_record import encoded, retain, verify
from noah.points.collector import Collector
from noah.points.deposit import advice
from noah.points.engine import INPUT_SCHEMA, evaluate
from noah.points.run import agreed
from noah.points.runtime import RuntimePolicy
from noah.points.tests.test_engine import R, fixture
from noah.starter.allocation import preview
from noah.starter.publication import retain_overlay
from noah.starter.tests.test_epoch_clock import request_live


class Prices:
    calls = 0

    def genesis_hash(self):
        return "g"

    def head_block(self):
        return 49

    def block_hash(self, b):
        return f"0x{b:064x}"

    def block_timestamp(self, b):
        return (b - 1) * 3600

    def alpha_price_at(self, b, *, netuid, expected_block_hash):
        assert expected_block_hash == self.block_hash(b)
        self.calls += 1
        return R // 20 if netuid == 82 else R // 2


def test_collector_full_day_restart_root(tmp_path):
    c = Prices()
    r = Collector(c, tmp_path, 82)
    rows = r.prior_day(49, 51)
    assert len(rows) == 24 and rows[0].start == 86400 and rows[-1].end == 172800
    assert c.calls == 48
    assert Collector(c, tmp_path, 82).prior_day(49, 51) == rows and c.calls == 48
    assert r.prior_day(49, 0)[0].asset_price_rao == R
    with pytest.raises(Refusal):
        r.prior_day(50, 51)


def test_corrupt_cache_is_refusal(tmp_path):
    c = Collector(Prices(), tmp_path, 82)
    c.prior_day(49, 51)
    path = next(tmp_path.glob("*.json"))
    v = json.loads(path.read_text())
    v["value"] = []
    path.write_text(json.dumps(v))
    with pytest.raises(Refusal):
        c.prior_day(49, 51)


def test_missing_price_never_cached_as_zero(tmp_path):
    class Broken(Prices):
        def alpha_price_at(self, *a, **kw):
            raise Refusal("offline")

    c = Collector(Broken(), tmp_path, 82)
    with pytest.raises(Refusal):
        c.prior_day(49, 51)
    assert not list(tmp_path.glob("*.json"))
    assert Collector(Prices(), tmp_path, 82).prior_day(49, 51)


def test_points_retention_and_starter_overlay_identical(tmp_path):
    raw = request_live()
    raw["fixture"]["schema"] = INPUT_SCHEMA
    ordinary = agreed(raw["fixture"])
    result = preview(raw)
    assert result["ordinaryOutput"] == ordinary
    ref = retain(
        tmp_path,
        fixture=raw["fixture"],
        agreed_text=encoded(ordinary).decode(),
        vector={int(k): v for k, v in ordinary["weightsU16"].items()},
        context=dict(
            day=ordinary["evalDay"],
            height=99,
            genesis_hash=raw["snapshot"]["genesisHash"],
            netuid=82,
            ark_address="ark",
            designated_validator_hotkey="val",
        ),
    )
    assert verify(tmp_path, ref)["engine_weights_u16"] == ordinary["weightsU16"]
    # The real retention path rejects different ordinary output before a send.
    retain_overlay(tmp_path, ref, raw)


def test_prepaid_advice_does_not_fake_consistency():
    raw = fixture([[(100 * R, 200 * R, 0), (100 * R, 0, 0)]])
    raw["schema"] = INPUT_SCHEMA
    r = advice(raw)[0]
    assert r["minimum_to_avoid_debt_rao"] == 0
    assert r["new_deposit_for_consistency_rao"] == 50 * R
    assert r["recommended_noah_deposit_rao"] == 50 * R
    raw = fixture([[(100 * R, 0, 0), (100 * R, 0, 0)]])
    raw["schema"] = INPUT_SCHEMA
    assert advice(raw)[0]["recommended_noah_deposit_rao"] == 50 * R
    assert advice(raw, include_catch_up=True)[0]["selected_noah_deposit_rao"] == 100 * R


def captured(tmp_path):
    """Deterministic synthetic cutoff; no live identities or private evidence."""
    import hashlib

    from noah.points.engine import POLICY
    from noah.scoring.engine import score_miner
    from noah.scoring.fixture_io import parse_fixture

    f = fixture([[(100 * R, 0, 0), (100 * R, 100 * R, 0), (0, 100 * R, 0)]] * 4)
    f["name"] = "synthetic-points-cutoff"
    for i, miner in enumerate(f["miners"]):
        miner["coldkey"] = "5" + "ABCD"[i] * 47
    parsed = parse_fixture(f)
    legacy = {
        r.uid: score_miner(r, parsed.dials, parsed.eval_day) for r in parsed.miners
    }
    rows = []
    for r in evaluate(f)["miners"]:
        uid = r["uid"]
        old = legacy[uid]
        rows.append(
            dict(
                uid=uid,
                coldkey=r["coldkey"],
                hotkey="5" + "EFGH"[uid] * 47,
                state={
                    **{k: int(v) for k, v in r["state"].items()},
                    "balance": old.coverage_reserve_rao - old.debt_rao,
                },
                legacy_coverage_rao=old.coverage_reserve_rao,
                legacy_debt_rao=old.debt_rao,
            )
        )
    m = dict(
        schema="noah-points-migration/1",
        policy=POLICY,
        genesis="0x" + "1" * 64,
        cutoff_hash="0x" + "2" * 64,
        cutoff_block=100,
        cutoff_day=int(f["evalDay"]),
        source_sha256=hashlib.sha256(
            json.dumps(f, sort_keys=True).encode()
        ).hexdigest(),
        consumed_receipt_ids=["synthetic-consumed-receipt"],
        miners=rows,
    )
    chain = SimpleNamespace(
        genesis_hash=lambda: m["genesis"],
        head_block=lambda: m["cutoff_block"] + 1,
        block_timestamp=lambda b: (int(f["dayEpochTs"]) + (m["cutoff_day"]+1)*86400
                                   - (1 if b <= m["cutoff_block"] else 0)),
        block_hash=lambda b: m["cutoff_hash"],
        seat_hotkeys=lambda b: {r["uid"]: r["hotkey"] for r in rows},
        hotkey_owners=lambda keys, b: {r["hotkey"]: r["coldkey"] for r in rows},
    )
    a = SimpleNamespace(
        runner_chain=chain,
        ark_coldkey="ark",
        designated_validator_hotkey="val",
        day_anchor_ts=int(f["dayEpochTs"]),
        _submitted_record_dir=tmp_path,
        netuid=82,
    )
    d = dict(
        schema="noah-points-policy/2",
        migration=m,
        fixture=f,
        historical_valuations=[],
        pricing=dict(
            method="previous-completed-utc-day-average",
            assets="all-subnet-tokens-and-root",
            credit="actual-received",
        ),
        ark_address="ark",
        validator_hotkey="val",
    )
    from noah.points.tests._lineage_capture import for_fixture
    d["lineages"] = for_fixture(f, m, netuid=82)
    return a, d



def next_day(f):
    f = copy.deepcopy(f)
    d = int(f["evalDay"]) + 1
    f["evalDay"] = str(d)
    for r in f["miners"]:
        r["days"][str(d)] = dict(
            paidRao="0",
            paidLockedArrivalRao="0",
            payUnlockedRao="0",
            noahDepositRao="0",
            foreignDepositNoahEquivRao="0",
            noFault=False,
        )
    return f


def test_migration_balances_identity_restart_and_new_identity(tmp_path):
    a, d = captured(tmp_path)
    p = RuntimePolicy(d, a)
    RuntimePolicy(d, a)
    f = next_day(d["fixture"])
    hot = {r["uid"]: r["hotkey"] for r in d["migration"]["miners"]}
    assert p.check_snapshot(a, f, hot)
    new = fixture([[(0, 0, 0)]])["miners"][0]
    new.update(
        uid=100,
        firstActivityDay=f["evalDay"],
        registrationDay=f["evalDay"],
        days={f["evalDay"]: new["days"]["0"]},
    )
    f["miners"].append(new)
    hot[100] = "new-hotkey"
    assert p.check_snapshot(a, f, hot)
    assert evaluate({**f, "schema": INPUT_SCHEMA})["miners"][-1]["state"]["points"] == "0"
    f["miners"][3]["entryDepositRao"] = "1"
    with pytest.raises(Refusal):
        p.check_snapshot(a, f, hot)


def test_wrong_ownership_prevents_startup(tmp_path):
    a, d = captured(tmp_path)
    a.runner_chain.hotkey_owners = lambda keys, b: {}
    with pytest.raises(ValueError, match="identity"):
        RuntimePolicy(d, a)


def test_changed_legacy_balance_prevents_startup(tmp_path):
    a, d = captured(tmp_path)
    d["migration"]["miners"][3]["legacy_coverage_rao"] = 0
    with pytest.raises(ValueError, match="reconciliation|prepaid|debt"):
        RuntimePolicy(d, a)


def test_real_adapter_points_publication_restart_and_guard(tmp_path, monkeypatch):
    from unittest.mock import patch

    from noah.core.tests._prepared_transport_fake import install
    from noah.starter.runtime import guard_tick
    from noah.starter.tests.test_runtime import ready, send_overlay

    install(monkeypatch)
    raw = request_live()
    raw["fixture"]["schema"] = INPUT_SCHEMA
    with patch("noah.starter.tests.test_runtime.request_live", return_value=raw):
        a, c, raw, source = ready(tmp_path)
    output = a.score(raw["fixture"])
    vector = a.agree(raw["fixture"])
    assert vector == {int(k): v for k, v in output["weightsU16"].items()}
    send_overlay(a, c, raw, source)
    assert dict(c.stored)[4] > 0
    c.epoch = c.block = c.finalized_head = 560
    assert guard_tick(a)["status"] == "replaced"
    assert dict(c.stored)[4] == 0
    assert guard_tick(a)["status"] == "ordinary"
    assert a._publish_intent.load() is None


def test_closed_day_outage_does_not_refresh_yesterdays_weights(tmp_path, monkeypatch):
    from unittest.mock import patch

    from noah.core.tests._prepared_transport_fake import install
    from noah.starter.runtime import republish
    from noah.starter.tests.test_runtime import ready

    install(monkeypatch)
    raw = request_live()
    raw["fixture"]["schema"] = INPUT_SCHEMA
    with patch("noah.starter.tests.test_runtime.request_live", return_value=raw):
        a, c, raw, source = ready(tmp_path)
    before = len(c.calls)
    # Advance UTC but deliberately have no new complete priced daily score.
    a.runner_chain.block_timestamp = lambda b: a.day_anchor_ts + 3 * 86400
    from noah.core.publish_intent import PublicationStatusUnknown

    with pytest.raises(PublicationStatusUnknown):
        republish(a, 200, 201)
    assert len(c.calls) == before


def test_runtime_foreign_hash_mismatch_preserves_pending_receipt(tmp_path):
    a, d = captured(tmp_path)
    p = RuntimePolicy(d, a)
    a._foreign_valuation_evidence = []
    with pytest.raises(Refusal):
        p.foreign_rule(a, None)(R, 0, d["migration"]["cutoff_block"] + 1, "0x" + "f" * 64)
    assert a._foreign_valuation_evidence == []


# Private fleet exporter integration remains in the private source test suite.


def test_startup_opt_in_no_default_and_chain_floor(tmp_path, monkeypatch):
    from noah.core.settings import SettingsError
    from noah.points.runtime import PROFILE, configure

    a, d = captured(tmp_path)
    a.starter_entry_policy = SimpleNamespace(snapshot={"sha256": "a" * 64})
    a.epoch_collection = True
    a.epoch_republication = True
    a.publisher = SimpleNamespace(version_key=1)
    a.runner_chain.head_block = lambda: d["migration"]["cutoff_block"] + 1
    a.runner_chain.starter_epoch_info = lambda h: {"WeightsVersionKey": 0}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(d))
    configure(a, SimpleNamespace(points_policy=None))
    assert not hasattr(a, "points_policy")
    monkeypatch.setattr(
        "noah.scoring.parameters.load_parameters", lambda: SimpleNamespace(profile_version=PROFILE)
    )
    configure(a, SimpleNamespace(points_policy=path, scoring_mode="points-402040-v2"))
    assert a.points_policy and a.publisher.version_key > 1
    a.runner_chain.starter_epoch_info = lambda h: {"WeightsVersionKey": 2**64 - 1}
    with pytest.raises(SettingsError, match="floor"):
        configure(a, SimpleNamespace(points_policy=path, scoring_mode="points-402040-v2"))


def test_prepare_reconciles_retained_legacy_then_startup(tmp_path):
    from noah.points.prepare import build
    from noah.scoring.engine import score_fixture
    from noah.scoring.fixture_io import parse_fixture

    a, d = captured(tmp_path / "state")
    f = d["fixture"]
    m = d["migration"]
    legacy = score_fixture(parse_fixture(f))
    context = dict(
        day=int(f["evalDay"]),
        height=m["cutoff_block"],
        genesis_hash=m["genesis"],
        netuid=82,
        ark_address="ark",
        designated_validator_hotkey="val",
        source_agreement="required_by_ingestion",
        scored_hotkeys={str(r["uid"]): r["hotkey"] for r in m["miners"]},
        to_block=m["cutoff_block"],
        to_block_hash=m["cutoff_hash"],
        foreign_valuations=d["historical_valuations"],
        points_lineage_capture=d["lineages"],
    )
    ref = retain(
        tmp_path / "evidence",
        fixture=f,
        agreed_text=encoded(legacy).decode(),
        vector={int(k): v for k, v in legacy["weightsU16"].items()},
        context=context,
    )
    policy = build(tmp_path / "evidence", ref, d["pricing"], m["consumed_receipt_ids"])
    assert policy["migration"] == m
    assert RuntimePolicy(policy, a).migration == m


def test_unfinalized_migration_refused(tmp_path):
    a, d = captured(tmp_path)
    a.runner_chain.head_block = lambda: d["migration"]["cutoff_block"] - 1
    with pytest.raises(ValueError, match="finalized"):
        RuntimePolicy(d, a)


def test_collector_memory_bounded_without_changing_complete_day_result(tmp_path):
    c = Collector(Prices(), tmp_path, 82)
    c.MAX_PRICES = 3
    c.MAX_TIMES = 3
    rows = c.prior_day(49, 51)
    assert len(rows) == 24 and sum(r.end - r.start for r in rows) == 86400
    assert len(c.prices) <= 3 and len(c.times) <= 3
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert Collector(Prices(), tmp_path, 82).prior_day(49, 51) == rows


@pytest.mark.parametrize("asset", [0, 51])
def test_foreign_only_newcomer_history_reaches_points_without_native_deposit(tmp_path, asset):
    from noah.chain.fixture_build import build_fixture
    from noah.chain.tests import test_foreign_credit as t

    store = t.a_store(tmp_path)
    for day in (1, 2, 3):
        t.paid(store, t.COLDKEY, day, 100 * R)
    t.put(store, day=3, amount=100 * R, token=asset)
    t.scan(store, 1, 3)
    args = dict(
        name="foreign-only",
        eval_day=3,
        day_epoch_ts=t.ANCHOR,
        dials={**t.DIALS, "targetFp": "500000000", "ongoingHoldRao": "0"},
        seats=t.SEATS,
        registration_days={t.COLDKEY: {t.HOT_A: 1}},
        foreign_rule=lambda *a: 100 * R,
    )
    old = build_fixture(store, **args)
    assert old["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"] == "0"
    new = build_fixture(store, **args, foreign_without_noah_from_day=3)
    assert new["miners"][0]["days"]["3"]["foreignDepositNoahEquivRao"] == str(100 * R)
    scored = agreed({**new, "schema": INPUT_SCHEMA})["miners"][0]["state"]
    assert int(scored["points"]) > 0 and scored["contributed"] == str(100 * R)
    assert all(d["noahDepositRao"] == "0" for d in new["miners"][0]["days"].values())
    store.close()


@pytest.mark.parametrize(
    "bps,expected", [(0, 0), (2500, 25 * R), (5000, 50 * R), (10000, 100 * R), (15000, 150 * R)]
)
def test_export_keeps_chosen_percentage_even_with_debt(bps, expected):
    raw = {**fixture([[(100 * R, 0, 0), (100 * R, 0, 0), (100 * R, 0, 0)]]), "schema": INPUT_SCHEMA}
    r = advice(raw, contribution_bps=bps)[0]
    assert r["selected_noah_deposit_rao"] == expected
    assert r["minimum_to_avoid_debt_rao"] == 150 * R


def test_explicit_amount_range_and_catchup():
    raw = {**fixture([[(100 * R, 0, 0), (200 * R, 0, 0), (300 * R, 0, 0)]]), "schema": INPUT_SCHEMA}
    assert advice(raw, amount_rao=123)[0]["selected_noah_deposit_rao"] == 123
    assert (
        advice(
            raw, earnings_from="1970-01-01", earnings_through="1970-01-03", contribution_bps=6000
        )[0]["selected_noah_deposit_rao"]
        == 360 * R
    )
    assert (
        advice(raw, amount_rao=123, include_catch_up=True)[0]["selected_noah_deposit_rao"]
        == 300 * R
    )
    with pytest.raises(ValueError):
        advice(raw, earnings_through="1970-01-04")
    with pytest.raises(ValueError):
        advice(raw, contribution_bps=-1)
