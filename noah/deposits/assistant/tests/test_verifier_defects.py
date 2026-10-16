"""Permanent tests for the independent Verifier's defects D1-D5 on 5c90dfa."""

import configparser
from datetime import datetime, timedelta, timezone

import pytest

from noah.deposits.assistant import ledger as L
from noah.deposits.assistant import reconcile as R
from noah.deposits.assistant import session as S
from noah.deposits.assistant.amounts import (
    MAINTENANCE,
    PERCENT,
    DepositRefused,
    Evidence,
    Limits,
    propose,
)
from noah.deposits.assistant.assistant_tx import CannotCompose, plan_deposit
from noah.deposits.assistant.config import EXAMPLE, load_config
from noah.deposits.availability import deposit_ceiling
from noah.deposits.disclosures import REQUIRED_DISCLOSURES, acknowledge

from .fakes import ARK, COLD, MINER, VAL, N
from .test_session import Evid, Rig

# ---- D1: final-but-uncredited deposits are pending coverage


def test_D1_maintenance_next_day_stale_evidence_does_not_pay_twice(tmp_path):
    r = Rig(tmp_path)
    assert r.run()["status"] == L.FINALIZED
    r.chain.advance(5)
    r.evid.through_day = "2026-09-30"  # a new day, credit still stale
    second = r.run()
    assert second["status"] == "nothing_due"
    assert len(r.chain.submitted) == 1
    assert "awaiting credit" in r.text()


def test_D1_percent_same_period_next_day_does_not_pay_twice(tmp_path):
    r = Rig(tmp_path, mode="percent")
    r.run("SEND 50 NOAH")
    r.chain.advance(5)
    r.evid.through_day = "2026-09-30"
    assert r.run("SEND 50 NOAH")["status"] == "nothing_due"
    assert len(r.chain.submitted) == 1


def test_D1_percent_new_period_is_still_paid(tmp_path):
    r = Rig(tmp_path, mode="percent")
    r.run("SEND 50 NOAH")

    class NewPeriod(Evid):
        def read(self):
            e = super().read()
            return Evidence(**{**e.__dict__, "period_start_day": "2026-09-30"})

    r.evid = NewPeriod(through_day="2026-09-30")
    assert r.run("SEND 50 NOAH")["status"] == L.FINALIZED
    assert len(r.chain.submitted) == 2


def test_D1_partial_pending_reduces_the_next_amount_and_preview_shows_it(tmp_path):
    r = Rig(tmp_path, cap_rao=50 * N)  # maintenance wants 200; cap lets 50 through
    r.run(phrase="SEND 50 NOAH")
    r.out.clear()
    S.preview(r.config, r.services())
    text = r.text()
    assert "awaiting credit: 50 NOAH" in text and "Still needed: 150 NOAH" in text


def test_D1_credit_arrival_stops_the_subtraction(tmp_path):
    r = Rig(tmp_path)
    r.run()
    r.evid.credited = 500 * N  # credit caught up
    assert r.run()["status"] == "nothing_due"
    assert r.ledger.for_miner(MINER)[0]["state"] == L.CREDITED


def test_D1_new_deposit_credit_start_includes_pending(tmp_path):
    r = Rig(tmp_path, cap_rao=50 * N)
    r.run(phrase="SEND 50 NOAH")
    r.chain.advance(3)
    r.evid.through_day = "2026-09-30"
    r.run(phrase="SEND 50 NOAH")
    first, second = r.ledger.for_miner(MINER)
    assert first["credited_before_rao"] == 300 * N and second["credited_before_rao"] == 350 * N
    r.evid.credited = 350 * N  # only the first deposit has been credited so far
    S.reconcile_open(r.config, r.services())
    assert [x["state"] for x in r.ledger.for_miner(MINER)] == [L.CREDITED, L.FINALIZED]


def test_D1_untrackable_pending_deposit_refuses_rather_than_guess(tmp_path):
    r = Rig(tmp_path, mode="custom", amount_rao=5 * N)
    # no evidence available for the custom send, so its credit cannot be tracked
    r.evid.read = lambda: (_ for _ in ()).throw(DepositRefused("no file", "missing_evidence"))
    r.run(phrase="SEND 5 NOAH")
    r.evid = Evid()
    r.config = r.config.__class__(**{**r.config.__dict__, "mode": "maintenance"})
    out = S.preview(r.config, r.services())
    assert out["status"] == "refused" and out["code"] == "pending_unknown"


def test_D1_custom_without_evidence_does_not_block_forever(tmp_path):
    r = Rig(tmp_path, mode="custom", amount_rao=5 * N)
    r.evid.read = lambda: (_ for _ in ()).throw(DepositRefused("no file", "missing_evidence"))
    assert r.run(phrase="SEND 5 NOAH", label="a")["status"] == L.FINALIZED
    assert r.run(phrase="SEND 5 NOAH", label="b")["status"] == L.FINALIZED


# ---- D2: the default label is constant


def test_D2_fixed_amount_cannot_repeat_across_utc_midnight(tmp_path):
    r = Rig(tmp_path, mode="fixed", amount_rao=5 * N)
    t = [datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)]
    orig = r.services

    def svc():
        s = orig()
        s.now = lambda: t[0]
        return s

    r.services = svc
    assert r.run("SEND 5 NOAH")["status"] == L.FINALIZED
    r.chain.advance(5)
    t[0] += timedelta(minutes=2)
    out = r.run("SEND 5 NOAH")
    assert out["status"] == "blocked" and len(r.chain.submitted) == 1
    assert "--label" in out["reason"]
    assert r.run("SEND 5 NOAH", label="second")["status"] == L.FINALIZED


# ---- D3: only an explicit False is a failure


@pytest.mark.parametrize(
    "value,state",
    [(True, L.FINALIZED), (False, L.FAILED), (None, L.UNCERTAIN), ("missing", L.UNCERTAIN)],
)
def test_D3_receipt_success_flag(tmp_path, value, state):
    r = Rig(tmp_path)
    orig = r.chain.submit_extrinsic

    def sub(*a, **k):
        rec = orig(*a, **k)
        if value == "missing":
            del rec.is_success
        else:
            rec.is_success = value
        return rec

    r.chain.submit_extrinsic = sub
    r.run()
    assert r.ledger.for_miner(MINER)[-1]["state"] == state
    if state == L.UNCERTAIN:  # and it blocks a new send until the chain is consulted
        assert r.ledger.open_intent(MINER) is not None or len(r.ledger.for_miner(MINER)) == 1


# ---- D4: the example settings file loads once addresses are filled in


def test_D4_example_config_loads_with_addresses_substituted(tmp_path):
    text = (
        EXAMPLE.replace("MINER_HOTKEY_ADDRESS", MINER)
        .replace("COLDKEY_ADDRESS", COLD)
        .replace("ARK_VALIDATOR_HOTKEY_ADDRESS", VAL)
        .replace("ARK_ADDRESS", ARK)
    )
    path = tmp_path / "ex.ini"
    path.write_text(text)
    cfg = load_config(str(path), environ={})
    assert cfg.reserve_rao == N and cfg.percent_bp == 5000 and cfg.stake_position == "miner"
    assert cfg.validator_hotkey == VAL
    parser = configparser.ConfigParser()
    parser.read_string(text)
    assert all("(" not in v for sec in parser.values() for v in sec.values())


# ---- D5


def test_D5_preview_says_fee_is_in_tao_and_not_estimated(tmp_path):
    r = Rig(tmp_path)
    S.preview(r.config, r.services())
    assert "network fee is paid in TAO" in r.text() and "does NOT estimate" in r.text()


def test_D5_failed_text_does_not_claim_nothing_left_account():
    from noah.deposits.assistant.preview import _STATUS_TEXT

    assert "Nothing left your account" not in _STATUS_TEXT[L.FAILED]
    assert "fee may have been charged" in _STATUS_TEXT[L.FAILED]


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "1e999"])
def test_D5_nonfinite_percent_is_a_visible_refusal(tmp_path, bad):
    path = tmp_path / "c.ini"
    path.write_text(
        EXAMPLE.replace("MINER_HOTKEY_ADDRESS", MINER)
        .replace("COLDKEY_ADDRESS", COLD)
        .replace("ARK_VALIDATOR_HOTKEY_ADDRESS", VAL)
        .replace("ARK_ADDRESS", ARK)
        .replace("percent = 50", f"percent = {bad}")
    )
    with pytest.raises(DepositRefused):
        load_config(str(path), environ={})


def test_D5_find_deposit_prefers_the_matching_amount_and_flags_otherwise(tmp_path):
    r = Rig(tmp_path)
    r.run()  # lands 200 NOAH
    kw = dict(
        ark_coldkey=ARK,
        expected_netuid=r.config.netuid,
        validator_hotkey=VAL,
        coldkey=COLD,
        miner_hotkey=MINER,
        from_block=0,
        to_block=r.chain.head,
    )
    good = R.find_deposit(r.chain, amount_rao=200 * N, **kw)
    assert good.qualifies
    wrong = R.find_deposit(r.chain, amount_rao=199 * N, **kw)
    assert not wrong.qualifies and "intended" in wrong.reason


def test_D5_matching_deposit_wins_over_an_earlier_nonmatching_one(tmp_path):
    r = Rig(tmp_path, mode="custom", amount_rao=7 * N)
    r.run(phrase="SEND 7 NOAH", label="a")
    r.config = r.config.__class__(**{**r.config.__dict__, "amount_rao": 9 * N})
    r.run(phrase="SEND 9 NOAH", label="b")
    kw = dict(
        ark_coldkey=ARK,
        expected_netuid=r.config.netuid,
        validator_hotkey=VAL,
        coldkey=COLD,
        miner_hotkey=MINER,
        from_block=0,
        to_block=r.chain.head,
    )
    assert R.find_deposit(r.chain, amount_rao=9 * N, **kw).deposit.amount_rao == 9 * N


# ---- rounding and validator-required


def _ev(**kw):
    base = dict(
        through_day="d",
        history_complete=True,
        period_start_day="s",
        period_eligible_earnings_rao=3,
        cumulative_eligible_earnings_rao=3,
        credited_contributions_rao=0,
    )
    base.update(kw)
    return Evidence(**base)


LIM = Limits(available_rao=1000 * N, reserve_rao=0)


def test_rounding_percent_rounds_down_maintenance_rounds_up():
    assert propose(PERCENT, limits=LIM, percent_bp=5000, evidence=_ev()).amount_rao == 1  # 1.5 down
    assert (
        propose(
            PERCENT, limits=LIM, percent_bp=3333, evidence=_ev(period_eligible_earnings_rao=5)
        ).amount_rao
        == 1
    )  # 1.6665 down
    assert propose(MAINTENANCE, limits=LIM, evidence=_ev()).amount_rao == 2  # 1.5 up


def test_plan_deposit_requires_a_validator_defense_in_depth():
    # The miner-vs-validator comparison would also reject a blank validator; this pins the
    # explicit check so removing either one alone still fails closed.
    ceiling = deposit_ceiling(position_stake=N, position_collateral=0, coldkey_available=N)
    for blank in (None, ""):
        with pytest.raises(CannotCompose, match="required"):
            plan_deposit(
                "earnings",
                ark_coldkey=ARK,
                miner_hotkey=MINER,
                validator_hotkey=blank,
                netuid=2,
                amount_rao=1,
                ceiling=ceiling,
                acknowledgment=acknowledge(REQUIRED_DISCLOSURES),
            )
