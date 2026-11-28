"""Actual SDK signing, fresh CLI processes, collector and offline adjudication.

Only synthetic proxy key material and an emulated local RPC are used.
"""

from .test_weekly_local_chain import TARGETS, _bonus_case


def test_proxy_three_reports_replay_and_earn_bonus_without_original_wallet(tmp_path):
    result = _bonus_case(tmp_path, 3, 3, proxy=True)
    reporter = next(row for row in result["decisions"] if row["uid"] == 0)
    assert result["adjudication_status"] == "COMPLETE", result
    assert result["collected_netuids"] == sorted(TARGETS)
    assert reporter["bonus"] is True
    assert all(row["outcome"] == "verified" for row in reporter["rounds"])
    assert result["pays"][0] == result["ordinary_pay"] + 20 * 10**9
    assert result["pays"][1] == result["ordinary_pay"]


def test_proxy_missing_reveal_does_not_earn_bonus(tmp_path):
    result = _bonus_case(tmp_path, 3, 2, omit_reveal_netuid=TARGETS[0], proxy=True)
    reporter = next(row for row in result["decisions"] if row["uid"] == 0)
    assert reporter["bonus"] is False
    assert result["pays"][0] == result["ordinary_pay"]
    assert sum(row["outcome"] == "verified" for row in reporter["rounds"]) == 2
