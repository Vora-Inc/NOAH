"""V2.1 keeps the V2 rules and changes NOAH credit on the pinned day."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from noah.points import onboarding, state_files
from noah.points.engine import evaluate, replay_state
from noah.points.ledger import State
from noah.points.tests.test_no_pool import economics_fixture
from noah.scoring.fixture_io import parse_fixture

R = 10**9
SWITCH = 2


def test_points_state_requires_safe_directory_and_regular_files(tmp_path):
    root = tmp_path.resolve() / "scores"
    with state_files.directory(root, create=True) as scores:
        assert root.stat().st_mode & 0o077 == 0
        state_files.write(scores, "points-policy.json", b"V2")
        state_files.write(scores, "points-policy.json", b"V2")
        with pytest.raises(ValueError, match="state differs"):
            state_files.write(scores, "points-policy.json", b"V2.1")
        assert state_files.read(scores, "points-policy.json") == b"V2"
        state_files.write(scores, "points-policy.json", b"V2.1", replace=True)
        assert state_files.read(scores, "points-policy.json") == b"V2.1"
        assert (root / "points-policy.json").stat().st_mode & 0o077 == 0
        assert state_files.read(scores, "day-7.submitted.json") is None
        with pytest.raises(ValueError, match="file name"):
            state_files.read(scores, "../fixture.json")
        (root / "day-7.submitted.json").mkdir()
        with pytest.raises(ValueError, match="regular file"):
            state_files.read(scores, "day-7.submitted.json")
        (root / "day-8.submitted.json").symlink_to(root / "points-policy.json")
        with pytest.raises(OSError):
            state_files.read(scores, "day-8.submitted.json")
    alias = root.with_name("linked-scores")
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        state_files.read_file(alias, "points-policy.json")


def v6(raw, switch=SWITCH):
    out = copy.deepcopy(raw)
    out.update(pointsRules=onboarding.RULES, pointsExcludedUids=[], pointsNoahCreditSwitchDay=switch)
    return out


def v5(raw):
    out = copy.deepcopy(raw)
    out.update(pointsRules=onboarding.RULES, pointsExcludedUids=[])
    return out


def trim(raw, day):
    out = copy.deepcopy(raw)
    out["evalDay"] = str(day)
    for miner in out["miners"]:
        miner["days"] = {key: value for key, value in miner["days"].items() if int(key) <= day}
    return out


def state(raw):
    return evaluate(raw)["miners"][0]["state"]


def test_v5_still_credits_noah_when_v6_is_not_selected():
    raw = v5(economics_fixture([[(0, 40 * R, 10 * R)]]))
    scored = state(raw)
    assert evaluate(raw)["policy"] == onboarding.POLICY
    assert int(scored["contributed"]) == 50 * R
    assert int(scored["balance"]) == 50 * R
    assert "pointsNoahCreditSwitchDay" not in raw
    frozen = copy.deepcopy(raw)
    evaluate(raw)
    assert raw == frozen


def test_noah_only_miner_goes_half_then_zero_after_prepaid_runs_out():
    # Day 0 is still V5, so the prepaid NOAH balance is kept. From the switch
    # day, further NOAH does not refill it.
    raw = economics_fixture(
        [[(0, 50 * R, 0), (200 * R, 500 * R, 0), (0, 500 * R, 0), (0, 500 * R, 0)]]
    )
    before = state(trim(v6(raw, 1), 0))
    half = state(trim(v6(raw, 1), 2))
    zero = state(v6(raw, 1))
    assert int(before["balance"]) == 50 * R
    assert int(half["balance"]) == -50 * R
    assert int(half["missed"]) == 1
    assert int(half["points"]) == int(before["points"]) // 2
    assert int(zero["missed"]) == 2
    assert int(zero["points"]) == 0
    assert int(zero["balance"]) == -50 * R


def test_prepaid_balance_is_carried_across_the_switch():
    raw = economics_fixture([[(100 * R, 200 * R, 0), (0, 100 * R, 0)]])
    opened = state(trim(v6(raw, 1), 0))
    carried = state(v6(raw, 1))
    assert int(opened["balance"]) == 200 * R
    assert int(carried["balance"]) == 150 * R
    assert int(carried["missed"]) == 0
    assert int(carried["points"]) > 0
    same_day_v5 = state(v5(raw))
    assert int(same_day_v5["balance"]) == 250 * R


def test_foreign_only_miner_matches_v5_state():
    raw = economics_fixture(
        [[(100 * R, 0, 50 * R), (100 * R, 0, 80 * R), (50 * R, 0, 25 * R), (0, 0, 10 * R)]]
    )
    left = evaluate(v5(raw))
    right = evaluate(v6(raw))
    assert right["policy"] == onboarding.V6_POLICY
    assert left["policy"] == onboarding.POLICY
    assert left["miners"][0]["state"] == right["miners"][0]["state"]
    assert left["weightsU16"] == right["weightsU16"]


def test_mixed_miner_is_credited_for_the_foreign_part_only():
    raw = economics_fixture([[(0, 10 * R, 0), (0, 40 * R, 60 * R)]])
    scored = state(v6(raw, 1))
    assert int(scored["contributed"]) == 60 * R
    assert int(scored["balance"]) == 70 * R
    assert int(state(v5(raw))["contributed"]) == 110 * R
    assert int(state(v5(raw))["balance"]) == 110 * R


def test_lifetime_bonus_drops_past_noah_from_the_switch_day():
    raw = economics_fixture([[(0, 100 * R, 0), (100 * R, 0, 0), (0, 0, 0)]])
    v5_switch = state(trim(v5(raw), SWITCH))
    v6_switch = state(v6(raw))
    assert int(v5_switch["contribution"]) == 40 * 10**6
    assert int(v6_switch["contribution"]) == 0
    assert int(v6_switch["balance"]) == int(v5_switch["balance"])
    assert int(v6_switch["contributed"]) == 0
    assert int(v5_switch["contributed"]) == 100 * R


def test_deposits_the_day_before_of_and_after_the_switch():
    raw = economics_fixture(
        [[(0, 10 * R, 1 * R), (0, 20 * R, 2 * R), (0, 30 * R, 3 * R), (0, 40 * R, 4 * R)]]
    )
    before = state(trim(v6(raw), 1))
    on_day = state(trim(v6(raw), 2))
    after = state(v6(raw))
    assert before == state(trim(v5(raw), 1))
    assert evaluate(trim(v6(raw), 1)) == evaluate(trim(v5(raw), 1))
    assert int(on_day["contributed"]) == 6 * R
    assert int(on_day["balance"]) == 36 * R
    assert int(after["contributed"]) == 10 * R
    assert int(after["balance"]) == 40 * R
    assert int(state(v5(raw))["contributed"]) == 110 * R


def test_restart_replay_does_not_double_count_or_double_remove():
    raw = v6(economics_fixture([[(0, 15 * R, 5 * R), (20 * R, 9 * R, 4 * R), (0, 8 * R, 2 * R)]]))
    assert evaluate(raw) == evaluate(copy.deepcopy(raw))
    plain = {
        key: value
        for key, value in raw.items()
        if not str(key).startswith("points")
    }
    plain["schema"] = "noah-fixture/6"
    miner = parse_fixture(plain).miners[0]
    first = replay_state(miner, 2, rules=onboarding.RULES, noah_credit_switch_day=SWITCH)
    second = replay_state(miner, 2, rules=onboarding.RULES, noah_credit_switch_day=SWITCH)
    assert first == second
    assert first.contributed == 11 * R
    assert isinstance(first, State)
    assert first != replay_state(miner, 2, rules=onboarding.RULES)


def test_owner_seat_stays_zero_and_raw_noah_amounts_remain():
    raw = v6(economics_fixture([[(0, 25 * R, 5 * R)]]))
    raw["pointsExcludedUids"] = [0]
    frozen = copy.deepcopy(raw)
    scored = evaluate(raw)
    assert scored["miners"][0]["state"]["points"] == "0"
    assert raw == frozen
    assert raw["miners"][0]["days"]["0"]["noahDepositRao"] == str(25 * R)


def test_python_and_typescript_agree_on_the_same_v6_fixtures():
    cases = [
        economics_fixture([[(0, 50 * R, 0), (200 * R, 500 * R, 0), (0, 500 * R, 0)]]),
        economics_fixture([[(0, 200 * R, 0), (100 * R, 100 * R, 0)]]),
        economics_fixture([[(100 * R, 0, 50 * R), (100 * R, 0, 80 * R)]]),
        economics_fixture([[(0, 10 * R, 0), (0, 40 * R, 60 * R)]]),
        economics_fixture([[(0, 100 * R, 0), (100 * R, 0, 0), (0, 0, 0)]]),
        economics_fixture(
            [[(0, 10 * R, 1 * R), (0, 20 * R, 2 * R), (0, 30 * R, 3 * R), (0, 40 * R, 4 * R)]]
        ),
    ]
    raws = []
    for index, raw in enumerate(cases):
        item = v6(raw, switch=1 if index % 2 else SWITCH)
        if index == 5:
            item["pointsExcludedUids"] = [0]
        raws.append(item)
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
    assert [evaluate(raw) for raw in raws] == json.loads(proc.stdout)


def test_policy_hash_locks_the_switch_day_and_v6_is_not_the_default(tmp_path, monkeypatch):
    from noah.core.calculation_record import encoded
    from noah.points import amendment, ema_policy
    from noah.points.runtime import RuntimePolicy, validate_configuration
    from noah.points.tests.test_integration import captured

    pricing = {"sha256": "ab" * 32, "policy": "pricing"}
    assert onboarding.manifest(pricing)["sha256"] == onboarding.manifest(pricing)["sha256"]
    assert onboarding.manifest(pricing)["scoring_mode"] == onboarding.MODE
    first = onboarding.manifest(pricing, mode=onboarding.V6_MODE, switch_day=10)
    second = onboarding.manifest(pricing, mode=onboarding.V6_MODE, switch_day=11)
    assert first["sha256"] != onboarding.manifest(pricing)["sha256"]
    assert first["sha256"] != second["sha256"]
    assert first["noah_credit_switch_day"] == 10
    with pytest.raises(ValueError):
        onboarding.manifest(pricing, mode=onboarding.V6_MODE, switch_day=None)

    adapter, old = captured(tmp_path)
    original = encoded(old)
    prior = encoded(amendment.build(original))
    at = old["migration"]["cutoff_block"] + 2
    ema = ema_policy.build(prior, {"block": at, "hash": old["migration"]["cutoff_hash"]})
    adapter.runner_chain.head_block = lambda: at + 1
    for name, body in (
        ("points-policy.json", encoded(ema)),
        ("points-policy-predecessor.json", original),
        ("points-policy-ema-predecessor.json", prior),
    ):
        (tmp_path / name).write_bytes(body)
    adapter.scoring_mode = onboarding.MODE
    v5_policy = RuntimePolicy(ema, adapter)
    wrapper = onboarding.build_v6_policy(encoded(ema), old["migration"]["cutoff_day"] + 1)
    adapter.scoring_mode = onboarding.V6_MODE
    # The transition's exact finalized confirmation is covered separately.
    monkeypatch.setattr(onboarding, "require_v2_cutover_day", lambda *_: None)
    v6_policy = RuntimePolicy(wrapper, adapter)
    assert v6_policy.v6 and v6_policy.no_pool and v6_policy.switch_day == wrapper["switch_day"]
    assert v6_policy.manifest["policy"] == onboarding.V6_POLICY
    assert v6_policy.manifest["sha256"] != v5_policy.manifest["sha256"]
    assert v6_policy.migration == v5_policy.migration
    assert (tmp_path / "points-policy.json").read_bytes() == encoded(ema)
    v6_policy.persist()
    assert (tmp_path / "points-policy.json").read_bytes() == encoded(wrapper)
    assert (tmp_path / "points-policy-v5-predecessor.json").read_bytes() == encoded(ema)
    RuntimePolicy(wrapper, adapter)
    with pytest.raises(ValueError):
        onboarding.build_v6_policy(encoded(ema), old["migration"]["cutoff_day"])
    from types import SimpleNamespace
    from unittest.mock import patch

    from noah.core.settings import SettingsError

    with patch(
        "noah.scoring.parameters.load_parameters",
        return_value=SimpleNamespace(profile_version="mainnet-production-starter-25bps-v1"),
    ):
        assert (
            validate_configuration(SimpleNamespace(scoring_mode="legacy", points_policy=None))
            == "legacy"
        )
        with pytest.raises(SettingsError):
            validate_configuration(
                SimpleNamespace(scoring_mode=onboarding.V6_MODE, points_policy=None)
            )
        with pytest.raises(SettingsError):
            validate_configuration(
                SimpleNamespace(
                    scoring_mode=onboarding.V6_MODE, points_policy="/held/policy.json"
                )
            )


def test_burn_gate_accepts_v6_and_still_refuses_older_points_modes():
    from types import SimpleNamespace

    from noah.core.chain_adapter import ChainAdapter
    from noah.core.publish import PublicationRefused

    allowed = SimpleNamespace(
        burn_allocation_bps=9800,
        scoring_mode=onboarding.V6_MODE,
        _calculation_reference=None,
        _submitted_record_dir=None,
    )
    with pytest.raises(PublicationRefused, match="retained pre-burn"):
        ChainAdapter.prepare_burn_vector(allowed, {1: 1})
    refused = SimpleNamespace(
        burn_allocation_bps=9800,
        scoring_mode="points-402040-v4",
        _calculation_reference=None,
        _submitted_record_dir=None,
    )
    with pytest.raises(PublicationRefused, match="requires V2"):
        ChainAdapter.prepare_burn_vector(refused, {1: 1})


def test_deposit_advice_uses_execution_day_at_the_switch():
    from noah.points.deposit import advice

    raw = v5(trim(economics_fixture([[(40 * R, 0, 0), (0, 0, 0)]]), 0))
    before = advice(raw, switch_day=2)[0]
    on_switch = advice(raw, execution_day="1970-01-03", switch_day=2)[0]
    assert before["recommended_noah_deposit_rao"] == 20 * R
    assert on_switch["recommended_noah_deposit_rao"] == 0
    assert on_switch["recommended_foreign_or_tao_noah_equiv_rao"] == 20 * R
    assert on_switch["expected_balance_rao"] == -20 * R


def pending_refresh_fixture(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from noah.core.calculation_record import encoded, retain
    from noah.core.submitted_record import write_submitted_record
    from noah.core.version import version_key_for

    fixture = v5(economics_fixture([[(0, 0, 0), (0, 0, 0)]]))
    score = evaluate(fixture)
    prior = onboarding.manifest({"policy": "pricing", "sha256": "a" * 64, "cutoff_day": 0})
    successor = {**prior, "scoring_mode": onboarding.V6_MODE}
    context = dict(day=1, height=11, genesis_hash="0x" + "b" * 64,
                   netuid=82, ark_address="synthetic", scoring_mode=onboarding.MODE,
                   points_policy=prior, validator_hotkey="synthetic", publisher_uid=7,
                   from_block=7, to_block=11,
                   from_block_hash=f"0x{7:064x}", to_block_hash=f"0x{11:064x}",
                   day_start_utc_unix=86400, day_end_utc_unix=172800)
    vector = {int(uid): value for uid, value in score["weightsU16"].items()}
    ref = retain(tmp_path, fixture=fixture, agreed_text=encoded(score).decode(),
                 vector=vector, context=context)
    key = version_key_for(prior["policy"] + "/" + prior["sha256"])
    marker = dict(day=1, height=11, netuid=82, refresh_epoch=None, version_key=key,
                  calculation_evidence=ref, vector={str(k): v for k, v in vector.items()},
                  result=dict(block=15, extrinsic_index=1, block_hash="0x" + "a" * 64))
    path = write_submitted_record(tmp_path, day=1, height=11, vector=vector,
        published=dict(**marker["result"], calculation_evidence=ref, direct_intent=marker),
        mode="direct", hotkey="synthetic", netuid=82)
    adapter = SimpleNamespace(
        scoring_mode=onboarding.V6_MODE, netuid=82, _submitted_record_dir=tmp_path,
        points_policy=SimpleNamespace(manifest=successor, manifest_for_day=lambda _: prior),
        _check_direct_identity=Mock(), reader=object(), runner_chain=object(),
    )
    return adapter, ref, prior, path, key


def test_confirmed_v2_refresh_remains_bounded_and_accounting_does_not_send(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.points.mode import check_accounting_mode, check_publication_mode

    adapter, ref, _, path, _ = pending_refresh_fixture(tmp_path)
    reconciled = Mock()
    monkeypatch.setattr("noah.core.direct.reconcile", reconciled)
    assert check_accounting_mode(adapter, ref) == onboarding.MODE
    reconciled.assert_not_called()
    with pytest.raises(PublicationStatusUnknown, match="refresh-only"):
        check_publication_mode(adapter, ref)
    refresh = {"day": 1, "height": 11, "epoch": 20}
    assert check_publication_mode(adapter, ref, refresh=refresh) == onboarding.MODE
    reconciled.assert_called_once()
    path.unlink()
    with pytest.raises(PublicationStatusUnknown, match="exact confirmed"):
        check_publication_mode(adapter, ref, refresh=refresh)


def test_historical_version_key_refuses_a_new_higher_floor(tmp_path):
    from types import SimpleNamespace

    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.points.publication import publication_key

    _, _, prior, _, key = pending_refresh_fixture(tmp_path)
    chain = SimpleNamespace(starter_epoch_info=lambda _: {"WeightsVersionKey": key})
    assert publication_key(chain, 100, prior) == key
    chain.starter_epoch_info = lambda _: {"WeightsVersionKey": key + 1}
    with pytest.raises(PublicationStatusUnknown, match="below chain floor"):
        publication_key(chain, 101, prior)


def test_early_or_unconfirmed_cutover_refuses(tmp_path):
    from types import SimpleNamespace

    adapter = SimpleNamespace(
        day_anchor_ts=0,
        runner_chain=SimpleNamespace(head_block=lambda: 10, block_timestamp=lambda _: 86400),
    )
    with pytest.raises(ValueError, match="planned last V2 closed day"):
        onboarding.require_v2_cutover_day(adapter, {"switch_day": 2}, tmp_path)
    adapter.runner_chain.block_timestamp = lambda _: 2 * 86400
    with pytest.raises(ValueError, match="last pre-switch V2 daily confirmation"):
        onboarding.require_v2_cutover_day(adapter, {"switch_day": 2}, tmp_path)


def test_accounted_pre_switch_day_keeps_v2_policy_and_never_sends(tmp_path):
    from types import SimpleNamespace

    from noah.points import catchup

    adapter, ref, prior, path, _ = pending_refresh_fixture(tmp_path)
    path.unlink()  # This scenario has a closed calculation without a send.
    adapter.runner_chain = SimpleNamespace(
        head_block=lambda: 100,
        block_timestamp=lambda block: 4 * 86400 if block == 100 else 2 * 86400,
        block_hash=lambda block: f"0x{block:064x}",
        genesis_hash=lambda: ref["genesis_hash"],
    )
    adapter._calculation_reference = ref
    adapter._publish_intent = SimpleNamespace(load=lambda: None)
    adapter._publish_hold = None
    adapter.day_anchor_ts = 0
    adapter.wallet_hotkey = SimpleNamespace(ss58_address="synthetic")
    adapter.uid = 7
    adapter.publish_mode = "direct"
    disposition, pending = catchup.record_accounted(adapter, 11)
    assert pending == {"day": 2, "height": 12}
    record = catchup.verify_record(tmp_path, disposition)
    assert record["points_policy"] == prior
    assert record["status"] == "accounted_not_published"
    assert catchup.record_accounted(adapter, 11) == (disposition, pending)
    assert not path.exists()


@pytest.mark.parametrize("use_proxy", [False, True])
def test_preflight_validates_confirmed_source_without_a_wallet(tmp_path, monkeypatch, use_proxy):
    from unittest.mock import Mock

    from noah.chain.ss58 import address_of
    from noah.core.publish_intent import PublicationStatusUnknown
    from noah.points.mode import check_publication_mode

    adapter, ref, _, path, _ = pending_refresh_fixture(tmp_path)
    hotkey = address_of(bytes([7]) * 32)
    adapter.designated_validator_hotkey = address_of(bytes([8]) * 32)
    adapter.expected_signer_address = hotkey
    adapter.proxy_signer_address = address_of(bytes([9]) * 32) if use_proxy else None
    del adapter._check_direct_identity
    del adapter.reader
    raw = json.loads(path.read_bytes())
    route = "proxy" if use_proxy else "direct"
    signer = adapter.proxy_signer_address or hotkey
    raw["direct_intent"].update(mode="direct", mecid=0, hotkey=hotkey, route=route, signer=signer)
    path.write_text(json.dumps(raw))
    confirm = Mock()
    monkeypatch.setattr("noah.points.transition.confirm_paired_receipt", confirm)
    assert check_publication_mode(adapter, ref, refresh={"day": 1, "height": 11, "epoch": 20}) == onboarding.MODE
    confirm.assert_called_once_with(adapter.runner_chain, raw["direct_intent"])
    raw["direct_intent"]["hotkey"] = adapter.designated_validator_hotkey
    path.write_text(json.dumps(raw))
    with pytest.raises(PublicationStatusUnknown, match="last V2 publisher differs"):
        check_publication_mode(adapter, ref, refresh={"day": 1, "height": 11, "epoch": 20})
    confirm.assert_called_once()


@pytest.mark.parametrize("record", [
    {"day": True},
    {"day": 3},
    {"day": 2, "scoring_mode": onboarding.V6_MODE},
])
def test_first_selection_refuses_incompatible_retained_calculation(tmp_path, monkeypatch, record):
    from types import SimpleNamespace
    from unittest.mock import Mock

    (tmp_path / "points-policy.json").write_bytes(b"predecessor")
    doc = {"switch_day": 2, "predecessor_json": "predecessor"}
    (tmp_path / "day-2.calculation.json").write_text(json.dumps(record))
    monkeypatch.setattr(onboarding, "validate_v6_document", Mock())
    confirmation = Mock()
    monkeypatch.setattr(onboarding, "require_v2_cutover_day", confirmation)
    with pytest.raises(ValueError, match="retained calculation"):
        onboarding.check_v6_transition(SimpleNamespace(_submitted_record_dir=tmp_path), doc)
    confirmation.assert_not_called()
