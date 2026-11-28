"""One local sender, many real miner identities. No keys or network needed."""

import copy

import pytest

from noah.reports.common import ReportError

from .test_miner_commands import case as case
from .test_proxy_reporting import DELEGATE, OTHER


def configs(case):
    c = case[0]
    c.report_proxy_address = DELEGATE
    c.wallet_name, c.hotkey_name, c.wallet_path = "proxy", "default", "/synthetic/wallets"
    c.assignment_signer_public_key = "aa" * 32
    c.assignments_url = "https://assignments.invalid/"
    result = []
    from noah.reports.backing.keys import ss58_encode

    for n in range(1, 19):
        item = copy.copy(c)
        item.miner_hotkey = ss58_encode(bytes([n]) * 32)
        result.append(item)
    return result


def test_fleet_accepts_18_distinct_miners_one_sender(case):
    from noah.reports.fleet import validate_fleet

    assert validate_fleet(configs(case))["miners"] == 18


@pytest.mark.parametrize(
    "field,value",
    [
        ("report_proxy_address", OTHER),
        ("state_dir", "/different"),
        ("wallet_name", "other"),
        ("endpoint", "ws://other.invalid"),
        ("genesis_hash", "99" * 32),
        ("assignment_signer_public_key", "bb" * 32),
        ("report_proxy_address", None),
    ],
)
def test_fleet_refuses_inconsistent_config_before_any_submission(case, field, value):
    from noah.reports.fleet import validate_fleet

    group = configs(case)
    setattr(group[-1], field, value)
    with pytest.raises(ReportError):
        validate_fleet(group)


def test_fleet_refuses_duplicate_hotkeys(case):
    from noah.reports.fleet import validate_fleet

    group = configs(case)
    group.append(group[0])
    with pytest.raises(ReportError):
        validate_fleet(group)


def test_fleet_runs_every_miner_serially_and_preserves_one_failure(case):
    from noah.reports.fleet import fleet_tick

    group = configs(case)
    called = []

    def tick(c):
        called.append(c.miner_hotkey)
        if c is group[4]:
            raise ReportError("uncertain submission")
        return {"action": "commit", "commit": "finalized", "sent": True}

    result = fleet_tick(group, tick)
    assert called == [c.miner_hotkey for c in group]
    assert len(result) == 18 and result[4]["action"] == "error"
    assert all(r["miner_hotkey"] == c.miner_hotkey for r, c in zip(result, group))


def test_fleet_default_timeout_outlasts_a_001_start_through_0416():
    from noah.reports import cli

    args = cli.parser().parse_args(["miner", "fleet", "--configs", "miner.ini", "--check"])
    # 00:01 UTC plus 15,600 seconds is 04:21 UTC, after a 04:16 reveal close.
    assert args.timeout_seconds == 15600


def test_parser_offline_check_does_not_load_default_config(tmp_path, monkeypatch):
    from noah.reports import cli, fleet

    monkeypatch.setattr(fleet, "run", lambda args, emit: 19)
    args = cli.parser().parse_args(
        ["miner", "fleet", "--configs", str(tmp_path / "one.ini"), "--check"]
    )
    assert cli.execute(args) == 19


def test_fleet_fetches_daily_once_for_a_shared_tick(case, monkeypatch):
    from types import SimpleNamespace
    from noah.deposits.assistant import config
    from noah.reports import assignments, fleet, schedule_runner
    from noah.reports.backing import chain_session

    miners = configs(case)[:3]
    for miner in miners:
        miner.late_joiner_assignments = True
    monkeypatch.setattr(config, "load_config", lambda path: miners[int(path)])
    chain = SimpleNamespace(finalized_time_ms=lambda: 1795479600000,
                            substrate=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(chain_session, "SubstrateChain", lambda endpoint: chain)
    monkeypatch.setattr(schedule_runner, "assignment_identity", lambda _: ("key", {}))
    monkeypatch.setattr(schedule_runner, "reconcile_assignments", lambda *args, **kwargs: None)
    monkeypatch.setattr(assignments, "current_document", lambda *args, **kwargs: {})
    fetched, ticked = [], []
    monkeypatch.setattr(assignments, "fetch_supplement_prefix", lambda *args, **kwargs: fetched.append(args))
    monkeypatch.setattr(schedule_runner, "assignments_tick",
                        lambda miner, *args, **kwargs: ticked.append(miner) or {"action": "complete"})
    args = SimpleNamespace(configs=["0", "1", "2"], check=False, follow=False,
                           timeout_seconds=10, yes=False, allow_mainnet_report=False)
    assert fleet.run(args, lambda _: None) == 0
    assert ticked == miners
    assert len(fetched) == 1
