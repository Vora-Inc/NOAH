from noah.deposits.assistant import ledger as L
from noah.deposits.assistant import session as S
from noah.deposits.assistant.amounts import Evidence
from noah.deposits.assistant.preview import render_status

from .fakes import ARK, COLD, MINER, OTHER_VAL, VAL, FakeChain, FakeKeypair, FakeSource, N, config


class Evid:
    def __init__(self, credited=300 * N, through_day="2026-09-29", complete=True):
        self.credited = credited
        self.through_day = through_day
        self.complete = complete

    def read(self):
        return Evidence(
            through_day=self.through_day,
            history_complete=self.complete,
            source="fake",
            period_start_day="2026-09-28",
            period_eligible_earnings_rao=100 * N,
            cumulative_eligible_earnings_rao=1000 * N,
            credited_contributions_rao=self.credited,
            entry_contribution_rao=10 * N,
        )


class Rig:
    """One miner's machine: ledger on disk, fake chain, scripted typing."""

    def __init__(self, tmp_path, chain=None, evid=None, source=None, keypair=None, **cfg):
        self.path = tmp_path / "l.db"
        self.chain = chain or FakeChain()
        self.evid = evid or Evid()
        self.source = source or FakeSource()
        self.keypair = keypair or FakeKeypair()
        self.config = config(**cfg)
        self.out, self.typed, self.signer_calls = [], [], 0
        self.reopen()

    def reopen(self):  # a restart: new process, same disk
        self.ledger = L.Ledger(self.path)

    def services(self):
        def signer():
            self.signer_calls += 1
            return self.keypair

        def ask(prompt):
            self.out.append(prompt)
            return self.typed.pop(0) if self.typed else ""

        return S.Services(
            connection=self.chain,
            source=self.source,
            evidence_provider=self.evid,
            ledger=self.ledger,
            signer_factory=signer,
            ask=ask,
            say=self.out.append,
        )

    def run(self, phrase="SEND 200 NOAH", **kw):
        self.typed = [phrase]
        return S.run_once(self.config, self.services(), **kw)

    def text(self):
        return "\n".join(map(str, self.out))


def test_preview_sends_nothing_and_records_calculation(tmp_path):
    r = Rig(tmp_path)
    out = S.preview(r.config, r.services())
    assert out == {"status": "ready", "amount_rao": 200 * N}
    assert r.chain.submitted == [] and r.signer_calls == 0
    assert r.ledger.for_miner(MINER) == []
    assert r.ledger.last_calculation(MINER)["status"] == "ready"
    for who in (MINER, ARK, VAL):
        assert who in r.text()


def test_preview_refuses_visibly_on_incomplete_history(tmp_path):
    r = Rig(tmp_path, evid=Evid(complete=False))
    out = S.preview(r.config, r.services())
    assert out["status"] == "refused" and out["code"] == "incomplete_history"
    assert "No deposit can be calculated" in r.text()


def test_run_once_happy_path_finalized_then_credited(tmp_path):
    r = Rig(tmp_path)
    res = r.run()
    assert res["status"] == L.FINALIZED  # finalized on chain, credit not yet shown by evidence
    [row] = r.ledger.for_miner(MINER)
    assert row["amount_rao"] == 200 * N and row["credited_before_rao"] == 300 * N
    assert len(r.chain.submitted) == 1 and r.chain.sign_eras == [{"period": S.ERA_PERIOD}]
    # submission/finalization is NOT credit; credit shows only when the evidence says so
    assert "Credit not yet confirmed" in render_status(r.config, None, r.ledger.for_miner(MINER))
    r.evid.credited = 500 * N
    S.reconcile_open(r.config, r.services())
    assert r.ledger.get(row["intent_id"])["state"] == L.CREDITED


def test_signer_opens_only_after_typed_confirmation(tmp_path):
    r = Rig(tmp_path)
    res = r.run(phrase="yes please")
    assert res["status"] == L.DECLINED
    assert r.signer_calls == 0 and r.chain.submitted == []
    assert r.ledger.for_miner(MINER)[0]["state"] == L.DECLINED


def test_refusal_creates_no_intent(tmp_path):
    r = Rig(tmp_path, evid=Evid(complete=False))
    assert r.run()["status"] == "refused"
    assert r.ledger.for_miner(MINER) == [] and r.signer_calls == 0


def test_nothing_due_sends_nothing(tmp_path):
    r = Rig(tmp_path, evid=Evid(credited=600 * N))
    assert r.run()["status"] == "nothing_due"
    assert r.chain.submitted == []


def test_same_coverage_cannot_be_paid_twice_even_after_restart(tmp_path):
    r = Rig(tmp_path)
    r.run()
    r.reopen()
    res = r.run()  # evidence has not caught up yet, so it would otherwise ask for 200 again
    assert res["status"] == "nothing_due" and len(r.chain.submitted) == 1


def test_not_final_stays_submitted_then_finalizes_on_later_check(tmp_path):
    r = Rig(tmp_path, chain=FakeChain(finality_lag=5))
    assert r.run()["status"] == L.SUBMITTED
    assert "does not prove credit" in render_status(r.config, None, r.ledger.for_miner(MINER))
    r.chain.advance(6)
    S.reconcile_open(r.config, r.services())
    assert r.ledger.for_miner(MINER)[0]["state"] == L.FINALIZED


def test_uncertain_send_that_landed_is_found_not_resent(tmp_path):
    chain = FakeChain()
    chain.submit_mode = "raise_after_inclusion"
    r = Rig(tmp_path, chain=chain)
    assert r.run()["status"] == L.UNCERTAIN
    chain.submit_mode = "ok"
    r.reopen()
    res = r.run()  # reconciles first, finds it, then counts it as already sent
    assert res["status"] == "nothing_due" and len(chain.submitted) == 1
    assert r.ledger.for_miner(MINER)[0]["state"] == L.FINALIZED


def test_uncertain_send_that_never_landed_blocks_until_window_ends_then_allows_retry(tmp_path):
    chain = FakeChain()
    chain.submit_mode = "raise_before_inclusion"
    r = Rig(tmp_path, chain=chain)
    assert r.run()["status"] == L.UNCERTAIN
    chain.submit_mode = "ok"
    assert r.run()["status"] == "blocked"  # too early to call it unsent
    assert len(chain.submitted) == 1
    chain.advance(S.ABANDON_AFTER_BLOCKS + 1)
    res = r.run()
    assert res["status"] == L.FINALIZED and len(chain.submitted) == 2
    states = [row["state"] for row in r.ledger.for_miner(MINER)]
    assert states == [L.ABANDONED, L.FINALIZED]


def test_crash_between_ledger_and_send_is_reconciled_against_chain(tmp_path):
    r = Rig(tmp_path)
    i = r.ledger.create_intent(
        fingerprint="x",
        miner_hotkey=MINER,
        mode="maintenance",
        amount_rao=200 * N,
        shape="earnings",
    )
    r.ledger.transition(i, L.APPROVED)
    r.ledger.transition(i, L.SUBMITTING, start_block=r.chain.head)
    S.reconcile_open(r.config, r.services())  # nothing on chain yet
    assert r.ledger.get(i)["state"] == L.UNCERTAIN
    assert r.chain.submitted == []


def test_interrupted_after_approval_before_sending_is_safe(tmp_path):
    r = Rig(tmp_path)
    i = r.ledger.create_intent(
        fingerprint="x", miner_hotkey=MINER, mode="m", amount_rao=1, shape="s"
    )
    r.ledger.transition(i, L.APPROVED)
    S.reconcile_open(r.config, r.services())
    assert r.ledger.get(i)["state"] == L.DECLINED


def test_signature_rejected_sends_nothing_and_allows_retry(tmp_path):
    r = Rig(tmp_path, keypair=FakeKeypair(reject=True))
    res = r.run()
    assert res["status"] == L.FAILED and r.chain.submitted == []
    assert "Nothing was sent" in r.text()
    r.keypair = FakeKeypair()
    assert r.run()["status"] == L.FINALIZED


def test_wrong_wallet_is_refused_before_signing(tmp_path):
    r = Rig(tmp_path, keypair=FakeKeypair(address=OTHER_VAL))
    assert r.run()["status"] == L.FAILED
    assert r.chain.submitted == [] and r.keypair.signed == 0


def test_chain_reported_failure_is_failed_not_credited(tmp_path):
    chain = FakeChain()
    chain.submit_mode = "extrinsic_fails"
    r = Rig(tmp_path, chain=chain)
    res = r.run()
    assert res["status"] == L.FAILED and "StakeUnavailable" in res["reason"]


def test_landing_on_the_wrong_validator_is_flagged_unqualified_not_finalized(tmp_path):
    chain = FakeChain()
    chain.wrong_validator = OTHER_VAL
    r = Rig(tmp_path, chain=chain)
    assert r.run()["status"] == L.UNQUALIFIED
    row = r.ledger.for_miner(MINER)[0]
    assert "does not qualify" in row["note"]
    r.reopen()
    assert r.run()["status"] == "blocked"  # money moved; no silent repeat


def test_cap_makes_partial_and_still_requires_confirmation(tmp_path):
    r = Rig(tmp_path, cap_rao=50 * N)
    res = r.run(phrase="SEND 50 NOAH")
    assert res["status"] == L.FINALIZED
    assert "goal is not reached" in r.text()


def test_staked_on_validator_shape_end_to_end_and_ownership_precheck(tmp_path):
    r = Rig(tmp_path, stake_position="validator")
    assert r.run()["status"] == L.FINALIZED
    r2 = Rig(
        tmp_path / "b" if False else tmp_path,
        chain=FakeChain(owners={MINER: OTHER_VAL}),
        stake_position="validator",
    )
    r2.path = tmp_path / "other.db"
    r2.reopen()
    res = r2.run()
    assert res["status"] == L.FAILED and "does not currently own" in res["reason"]
    assert r2.chain.submitted == []


def test_wrong_chain_refused(tmp_path):
    r = Rig(tmp_path, genesis_hash="0xdeadbeef")
    assert r.run()["status"] == "refused" and r.chain.submitted == []


def test_custom_amount_repeat_needs_a_new_label(tmp_path):
    r = Rig(tmp_path, mode="custom", amount_rao=5 * N)
    assert r.run(phrase="SEND 5 NOAH", label="a")["status"] == L.FINALIZED
    assert r.run(phrase="SEND 5 NOAH", label="a")["status"] == "blocked"
    assert r.run(phrase="SEND 5 NOAH", label="b")["status"] == L.FINALIZED
    assert len(r.chain.submitted) == 2


def test_percent_mode_end_to_end(tmp_path):
    r = Rig(tmp_path, mode="percent", percent_bp=5000)
    assert r.run(phrase="SEND 50 NOAH")["status"] == L.FINALIZED


def test_no_secrets_in_ledger_or_output(tmp_path):
    r = Rig(tmp_path)
    r.run()
    blob = r.text() + repr(r.ledger.for_miner(MINER))
    assert "mnemonic" not in blob.lower() and "seed" not in blob.lower()
    assert COLD in blob  # addresses are public; nothing else identifying a key is stored
