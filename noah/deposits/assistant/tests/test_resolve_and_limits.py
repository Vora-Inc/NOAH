"""N1 (resolve a deposit whose credit is untracked) and pins for the documented limits N2/N3."""

import json

import pytest

from noah.deposits.assistant import cli
from noah.deposits.assistant import ledger as L
from noah.deposits.assistant import session as S
from noah.deposits.assistant.amounts import DepositRefused
from noah.deposits.assistant.preview import STATUS_LIMITS, render_status

from .fakes import MINER, N
from .test_session import Evid, Rig


def _no_evidence():
    raise DepositRefused("no file", "missing_evidence")


def stuck(tmp_path):
    """A custom deposit sent with no evidence file, then the miner switches to maintenance."""
    r = Rig(tmp_path, mode="custom", amount_rao=5 * N)
    good = r.evid
    r.evid = type("NoFile", (), {"read": staticmethod(_no_evidence)})()
    r.run(phrase="SEND 5 NOAH")
    row = r.ledger.for_miner(MINER)[0]
    assert row["state"] == L.FINALIZED and row["credited_before_rao"] is None
    r.evid = good  # credited total shown now: 300 NOAH
    r.config = r.config.__class__(**{**r.config.__dict__, "mode": "maintenance"})
    return r, row["intent_id"]


def resolve(r, intent, value, typed=None):
    r.typed = [typed if typed is not None else f"CONFIRM {intent[:12]} {value}"]
    return S.resolve_credit(r.config, r.services(), intent[:12], value)


def test_N1_stuck_case_is_refused_with_the_exact_command_then_resolvable(tmp_path):
    r, i = stuck(tmp_path)
    out = S.preview(r.config, r.services())
    assert out["code"] == "pending_unknown"
    assert f"resolve {i[:12]} --credited-before-rao" in r.text()
    assert resolve(r, i, 300 * N)["status"] == L.FINALIZED
    # now tracked: the 5 NOAH already sent is subtracted from the 200 still needed
    r.out.clear()
    assert S.preview(r.config, r.services())["amount_rao"] == 195 * N
    assert "awaiting credit: 5 NOAH" in r.text()


def test_N1_resolved_deposit_becomes_credited_when_credit_arrives(tmp_path):
    r, i = stuck(tmp_path)
    resolve(r, i, 300 * N)
    r.evid.credited = 305 * N
    S.reconcile_open(r.config, r.services())
    assert r.ledger.get(i)["state"] == L.CREDITED


def test_N1_audit_trail_records_miner_supplied(tmp_path):
    r, i = stuck(tmp_path)
    resolve(r, i, 300 * N)
    row = r.ledger.get(i)
    details = json.loads(row["details"])
    assert details["credited_before_source"] == "miner-supplied"
    assert details["credited_before_supplied_rao"] == 300 * N
    assert "supplied by the miner" in row["note"]
    assert "supplied by you" in r.text()


@pytest.mark.parametrize("bad", [-1, True, 1.5, "300"])
def test_N1_malformed_value_refused(tmp_path, bad):
    r, i = stuck(tmp_path)
    with pytest.raises(DepositRefused):
        resolve(r, i, bad)
    assert r.ledger.get(i)["credited_before_rao"] is None


def test_N1_value_above_current_credit_refused(tmp_path):
    r, i = stuck(tmp_path)
    with pytest.raises(DepositRefused, match="more than the credited total"):
        resolve(r, i, 301 * N)


def test_N1_value_below_an_earlier_deposits_start_refused(tmp_path):
    r = Rig(tmp_path, mode="custom", amount_rao=5 * N)
    r.run(phrase="SEND 5 NOAH", label="a")  # evidence readable: before = 300 NOAH
    r.evid = type("NoFile", (), {"read": staticmethod(_no_evidence)})()
    r.chain.advance(2)
    r.run(phrase="SEND 5 NOAH", label="b")
    r.evid = Evid()
    second = r.ledger.for_miner(MINER)[1]
    assert second["credited_before_rao"] is None
    with pytest.raises(DepositRefused, match="lower than the credited total recorded"):
        resolve(r, second["intent_id"], 100 * N)


def test_N1_wrong_confirmation_changes_nothing(tmp_path):
    r, i = stuck(tmp_path)
    assert resolve(r, i, 300 * N, typed="yes")["status"] == "declined"
    assert r.ledger.get(i)["credited_before_rao"] is None


def test_N1_unreadable_evidence_cannot_resolve(tmp_path):
    r, i = stuck(tmp_path)
    r.evid = type("NoFile", (), {"read": staticmethod(_no_evidence)})()
    with pytest.raises(DepositRefused):
        resolve(r, i, 300 * N)


@pytest.mark.parametrize("state", [L.CREDITED, L.ABANDONED, L.DECLINED, L.FAILED])
def test_N1_cannot_resolve_other_states(tmp_path, state):
    r = Rig(tmp_path)
    r.run()
    i = r.ledger.for_miner(MINER)[0]["intent_id"]
    r.ledger._db.execute("UPDATE intents SET state=?, credited_before_rao=NULL", (state,))
    with pytest.raises(DepositRefused, match="nothing to resolve"):
        resolve(r, i, 300 * N)


def test_N1_cannot_replace_an_existing_figure(tmp_path):
    r, i = stuck(tmp_path)
    resolve(r, i, 300 * N)
    with pytest.raises(DepositRefused, match="already has a credit figure"):
        resolve(r, i, 300 * N)


def test_N1_cli_resolve_subcommand(tmp_path, capsys):
    from .test_cli import factory, write

    # a finalized untracked deposit already in the cli ledger
    ledger = L.Ledger(tmp_path / "cli.db")
    i = ledger.create_intent(
        fingerprint="f", miner_hotkey=MINER, mode="custom", amount_rao=5 * N, shape="earnings"
    )
    for state in (L.APPROVED, L.SUBMITTING, L.SUBMITTED, L.FINALIZED):
        ledger.transition(i, state)
    ledger.close()
    base = factory(tmp_path)

    def build(config):
        s = base(config)
        s.ask = lambda prompt: f"CONFIRM {i[:12]} {300 * N}"
        return s

    code = cli.main(
        ["--config", write(tmp_path), "resolve", i[:12], "--credited-before-rao", str(300 * N)],
        build,
    )
    assert code == 0 and "supplied by you" in capsys.readouterr().out


# ---- N2 / N3: documented limits, pinned as today's behaviour


def test_N2_an_unrelated_credit_of_the_pending_size_confirms_early_and_status_says_so(tmp_path):
    r = Rig(tmp_path)
    r.run()  # 200 NOAH, credited_before 300
    assert r.ledger.for_miner(MINER)[0]["state"] == L.FINALIZED
    r.evid.credited = 500 * N  # could be this deposit, or any other credit of that size
    S.reconcile_open(r.config, r.services())
    assert r.ledger.for_miner(MINER)[0]["state"] == L.CREDITED
    status = render_status(r.config, None, r.ledger.for_miner(MINER))
    assert STATUS_LIMITS in status and "unrelated credit" in status


def test_N3_a_manual_deposit_in_the_window_is_flagged_not_resent(tmp_path):
    r = Rig(tmp_path, mode="custom", amount_rao=5 * N)
    r.chain.submit_mode = "raise_before_inclusion"
    r.run(phrase="SEND 5 NOAH")  # uncertain, not on chain
    # a manual deposit of a different amount lands from the same coldkey and miner hotkey
    r.chain.submit_mode = "ok"
    from noah.deposits.assistant import assistant_tx as tx
    from noah.deposits.availability import deposit_ceiling
    from noah.deposits.deposit_submission import BytesConvertingSigner
    from noah.deposits.disclosures import REQUIRED_DISCLOSURES, acknowledge

    from .fakes import ARK, NETUID, VAL, FakeKeypair

    ceiling = deposit_ceiling(
        position_stake=50 * N, position_collateral=0, coldkey_available=50 * N
    )
    plan = tx.plan_deposit(
        "earnings",
        ark_coldkey=ARK,
        miner_hotkey=MINER,
        validator_hotkey=VAL,
        netuid=NETUID,
        amount_rao=9 * N,
        ceiling=ceiling,
        acknowledgment=acknowledge(REQUIRED_DISCLOSURES),
    )
    r.chain.submit_extrinsic(
        r.chain.create_signed_extrinsic(
            call=tx.compose(r.chain, plan), keypair=BytesConvertingSigner(FakeKeypair())
        )
    )
    S.reconcile_open(r.config, r.services())
    row = r.ledger.for_miner(MINER)[0]
    assert row["state"] == L.UNQUALIFIED and "intended" in row["note"]  # flagged, holds coverage
    assert len(r.chain.submitted) == 2  # the manual one; the tool sent nothing more
