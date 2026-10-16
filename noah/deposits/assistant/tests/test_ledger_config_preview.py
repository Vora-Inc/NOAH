import json
import multiprocessing
from pathlib import Path
from datetime import datetime, timezone

import pytest

from noah.chain.ss58 import address_of
from noah.deposits.assistant import ledger as L
from noah.deposits.assistant.amounts import (
    MAINTENANCE,
    DepositRefused,
    Evidence,
    Limits,
    propose,
)
from noah.deposits.assistant.amounts import (
    RAO_PER_NOAH as N,
)
from noah.deposits.assistant.config import load_config
from noah.deposits.assistant.evidence import JsonFileEvidence, StoreEvidence
from noah.deposits.assistant.preview import render_status, render_summary

MINER = address_of(bytes([1]) * 32)
COLD = address_of(bytes([2]) * 32)
ARK = address_of(bytes([3]) * 32)
VAL = address_of(bytes([4]) * 32)


def new(tmp_path):
    return L.Ledger(tmp_path / "l.db")


def make(ledger, fp="f1", miner=MINER, amount=5):
    return ledger.create_intent(
        fingerprint=fp, miner_hotkey=miner, mode="percent", amount_rao=amount, shape="x"
    )


def drive(ledger, i, *states):
    for s in states:
        ledger.transition(i, s)


def test_happy_path_and_illegal_edges(tmp_path):
    led = new(tmp_path)
    i = make(led)
    with pytest.raises(L.IllegalTransition):
        led.transition(i, L.SUBMITTED)
    drive(led, i, L.APPROVED, L.SUBMITTING, L.SUBMITTED, L.FINALIZED, L.CREDITED)
    with pytest.raises(L.IllegalTransition):
        led.transition(i, L.FAILED)


def test_completed_payment_cannot_repeat_even_after_restart(tmp_path):
    led = new(tmp_path)
    i = make(led)
    drive(led, i, L.APPROVED, L.SUBMITTING, L.SUBMITTED, L.FINALIZED)
    led.close()
    led2 = new(tmp_path)  # restart
    with pytest.raises(L.DuplicateDeposit):
        make(led2)


def test_open_intent_blocks_new_one_until_resolved(tmp_path):
    led = new(tmp_path)
    i = make(led, "a")
    drive(led, i, L.APPROVED, L.SUBMITTING, L.UNCERTAIN)
    with pytest.raises(L.OpenIntentExists):
        make(led, "b")
    led.transition(i, L.ABANDONED)
    make(led, "b")


def test_failed_and_declined_free_coverage_for_retry(tmp_path):
    led = new(tmp_path)
    i = make(led)
    drive(led, i, L.APPROVED, L.FAILED)
    j = make(led)
    led.transition(j, L.DECLINED)
    make(led)


def test_stale_awaiting_prompt_is_superseded(tmp_path):
    led = new(tmp_path)
    old = make(led, "a")
    new_id = make(led, "b")
    assert led.get(old)["state"] == L.DECLINED
    with pytest.raises(L.IllegalTransition):  # the old prompt can no longer proceed
        led.transition(old, L.APPROVED)
    assert led.get(new_id)["state"] == L.AWAITING


def _worker(path, queue):
    try:
        L.Ledger(path).create_intent(
            fingerprint="same", miner_hotkey=MINER + "x", mode="m", amount_rao=1, shape="s"
        )
        queue.put("ok")
    except L.Refusal:
        queue.put("refused")


def test_two_processes_only_one_wins(tmp_path):
    path = str(tmp_path / "race.db")
    L.Ledger(path).close()
    q = multiprocessing.Queue()
    procs = [multiprocessing.Process(target=_worker, args=(path, q)) for _ in range(4)]
    [p.start() for p in procs]
    [p.join() for p in procs]
    results = sorted(q.get() for _ in procs)
    assert results == ["ok", "refused", "refused", "refused"]


def test_timestamps_come_from_injected_clock_not_ordering(tmp_path):
    led = new(tmp_path)
    t = datetime(2020, 1, 1, tzinfo=timezone.utc)
    i = led.create_intent(
        fingerprint="z", miner_hotkey=MINER, mode="m", amount_rao=1, shape="s", now=t
    )
    assert led.get(i)["created_at"] == "2020-01-01T00:00:00Z"
    # a clock moved backwards does not matter: coverage, not time, is the key
    with pytest.raises(L.DuplicateDeposit):
        led.create_intent(
            fingerprint="z",
            miner_hotkey=MINER,
            mode="m",
            amount_rao=1,
            shape="s",
            now=datetime(2010, 1, 1, tzinfo=timezone.utc),
        )


def write_cfg(tmp_path, extra="", validator=VAL, miner=MINER):
    p = tmp_path / "c.ini"
    p.write_text(f"""[miner]
netuid = 2
miner_hotkey = {miner}
coldkey = {COLD}
[ark]
ark_coldkey = {ARK}
designated_validator_hotkey = {validator}
[deposit]
mode = percent
percent = 50
reserve_noah = 1
{extra}
""")
    return str(p)


def test_config_loads(tmp_path):
    c = load_config(write_cfg(tmp_path), environ={})
    assert c.percent_bp == 5000 and c.reserve_rao == N and c.validator_hotkey == VAL


def test_config_fails_closed_without_validator(tmp_path):
    p = write_cfg(tmp_path, validator="")
    with pytest.raises(DepositRefused) as e:
        load_config(p, environ={})
    assert e.value.code == "no_validator"
    assert load_config(p, environ={"NOAH_VALIDATOR_HOTKEY": VAL}).validator_hotkey == VAL


def test_config_refuses_secret_fields(tmp_path):
    with pytest.raises(DepositRefused) as e:
        load_config(write_cfg(tmp_path, "mnemonic = abandon abandon"), environ={})
    assert e.value.code == "secret_in_config"


@pytest.mark.parametrize('section,key', [
    ('signing','secret_seed'), ('signing','secret_value'),
    ('miner','secret_type'), ('deposit','secret_region'),
])
def test_proxy_metadata_exception_does_not_allow_key_material(tmp_path,section,key):
    p=Path(write_cfg(tmp_path))
    text=p.read_text()
    if section == 'signing':
        text += '\n[signing]\nmode = direct\n'
    else:
        text=text.replace('['+section+']','['+section+']\n'+key+' = forbidden')
    if section == 'signing':text += key+' = forbidden\n'
    p.write_text(text)
    with pytest.raises(DepositRefused) as error:load_config(p,environ={})
    assert error.value.code == 'secret_in_config'


def test_config_refuses_miner_equals_validator(tmp_path):
    with pytest.raises(DepositRefused):
        load_config(write_cfg(tmp_path, validator=MINER), environ={})


def test_json_evidence_reads_and_refuses(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(
        json.dumps(
            {
                "through_day": "d",
                "history_complete": True,
                "cumulative_eligible_earnings_rao": "100",
                "credited_contributions_rao": 0,
            }
        )
    )
    e = JsonFileEvidence(str(p)).read()
    assert e.cumulative_eligible_earnings_rao == 100 and e.period_eligible_earnings_rao is None
    with pytest.raises(DepositRefused):
        JsonFileEvidence(str(tmp_path / "nope.json")).read()
    p.write_text(json.dumps({"surprise": 1}))
    with pytest.raises(DepositRefused):
        JsonFileEvidence(str(p)).read()


def test_store_evidence_subtracts_entry_deposit():
    totals = {"d9": 60 * N, "d0": 10 * N}
    e = StoreEvidence(
        store=None,
        seats=None,
        coldkey=COLD,
        hotkey=MINER,
        entry_deposit_day="d0",
        through_day="d9",
        previous_through_day=None,
        emissions_reader=lambda s, seats, hk, entry_deposit_day, eval_day: 100 * N,
        deposited_reader=lambda s, c, h, through_day: totals[through_day],
    ).read()
    assert e.credited_contributions_rao == 50 * N and e.entry_contribution_rao == 10 * N
    assert e.cumulative_eligible_earnings_rao == 100 * N and e.history_complete


def test_summary_shows_three_addresses_distinctly(tmp_path):
    cfg = load_config(write_cfg(tmp_path), environ={})
    ev = Evidence(
        through_day="d",
        history_complete=True,
        cumulative_eligible_earnings_rao=100 * N,
        credited_contributions_rao=0,
    )
    prop = propose(MAINTENANCE, limits=Limits(available_rao=500 * N, reserve_rao=N), evidence=ev)
    text = render_summary(cfg, prop, available_rao=500 * N)
    for needle in (MINER, ARK, VAL, "50 NOAH", "a preview sends nothing", "Left after depositing"):
        assert needle in text
    assert text.count(VAL) == 1 and text.count(MINER) == 1


def test_status_distinguishes_stages(tmp_path):
    cfg = load_config(write_cfg(tmp_path), environ={})
    led = new(tmp_path)
    i = make(led)
    assert "Awaiting your approval" in render_status(cfg, None, led.for_miner(MINER))
    drive(led, i, L.APPROVED, L.SUBMITTING, L.SUBMITTED)
    assert "does not prove credit" in render_status(cfg, None, led.for_miner(MINER))
    drive(led, i, L.FINALIZED)
    assert "Credit not yet confirmed" in render_status(cfg, None, led.for_miner(MINER))
    drive(led, i, L.CREDITED)
    assert "credit confirmed" in render_status(cfg, None, led.for_miner(MINER))
