"""Automatic daily evidence and restart linkage, using the real two scorers."""

import copy
import json
from pathlib import Path
from unittest import mock

import pytest

from ..calculation_record import CalculationEvidenceRefused, digest, encoded, retain, verify
from ..period import PeriodRefused, run_period
from ..publish import NotOnChain
from .test_direct_runtime475 import VECTOR, adapter
from .test_period import Recorder

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures/shared/new-wallet-early-days.json"


def context(day):
    return {
        "day": day,
        "height": 99,
        "genesis_hash": "0x" + "a" * 64,
        "netuid": 82,
        "ark_address": "public-test-ark",
    }


def calculation(tmp_path, day=10):
    built, _ = adapter(tmp_path)
    fixture = json.loads(FIXTURE.read_text())
    fixture["evalDay"] = str(day)
    if day == 11:
        for miner in fixture["miners"]:
            miner["days"]["11"] = copy.deepcopy(miner["days"]["10"])
    vector = built.agree(fixture)  # actual Python and actual TypeScript CLI
    reference = retain(
        tmp_path / "scores",
        fixture=fixture,
        agreed_text=built._agreed_calculation[1],
        vector=vector,
        context=context(day),
    )
    return built, fixture, vector, reference


def test_two_consecutive_calculations_keep_exact_inputs_and_agreed_results(tmp_path):
    for day in (10, 11):
        built, fixture, vector, ref = calculation(tmp_path, day)
        manifest = verify(tmp_path / "scores", ref)
        assert manifest["engine_weights_u16"] == {str(k): v for k, v in vector.items()}
        score_ref = manifest["artifacts"]["agreed_score"]
        assert (tmp_path / "scores" / score_ref["path"]).read_text() == built._agreed_calculation[1]
        assert json.loads((tmp_path / "scores" / f"day-{day}.fixture.json").read_text()) == fixture
        canonical = json.loads((tmp_path / "scores" / f"day-{day}.json").read_text())
        assert canonical["schema"] == "noah-score-output/6"
        assert canonical["miners"] == json.loads(built._agreed_calculation[1])["miners"]
        assert manifest["publication"] == "not_established"
        assert manifest["settlement"] == "not_observed"


def test_automatic_runner_gates_send_on_evidence_and_retains_turnover_vector():
    chain = Recorder(score={0: 40000, 1: 25535}, chain_hotkeys={0: "replacement", 1: "hk1"})
    observed = []

    def save(height, fixture, agreed, vector):
        assert not chain.published
        observed.append((agreed, vector))

    chain.record_calculation = save
    run_period(chain)
    assert observed == [({0: 40000, 1: 25535}, {0: 0, 1: 25535})]
    chain = Recorder()
    chain.record_calculation = mock.Mock(side_effect=OSError("private error sentinel"))
    with pytest.raises(PeriodRefused, match="calculation evidence") as caught:
        run_period(chain)
    assert not chain.published
    assert "private error sentinel" not in str(caught.value)


def test_conflict_and_partial_write_do_not_replace_prior_calculation(tmp_path):
    built, fixture, vector, ref = calculation(tmp_path)
    original = (tmp_path / "scores/day-10.json").read_bytes()
    with pytest.raises(CalculationEvidenceRefused):
        retain(
            tmp_path / "scores",
            fixture=fixture,
            agreed_text=built._agreed_calculation[1],
            vector={0: 1},
            context=context(10),
        )
    assert (tmp_path / "scores/day-10.json").read_bytes() == original
    assert verify(tmp_path / "scores", ref)["day"] == 10


def test_recovery_retains_calculation_link_and_refuses_lost_artifact(tmp_path):
    built, client = adapter(tmp_path)
    score = {"evalDay": "0", "weightsU16": {str(k): v for k, v in VECTOR.items()}}
    ref = retain(
        tmp_path / "scores",
        fixture={"evalDay": "0"},
        agreed_text=encoded(score).decode(),
        vector=VECTOR,
        context=context(0),
    )
    built._calculation_reference = ref
    client.after_send = lambda c: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(NotOnChain):
        built.publish_direct(VECTOR, 99)
    assert built._publish_intent.load()["calculation_evidence"] == ref
    manifest_path = tmp_path / "scores" / ref["path"]
    original = manifest_path.read_bytes()
    manifest_path.unlink()
    with pytest.raises(Exception, match="direct send remains unresolved"):
        adapter(tmp_path, client)[0].already_published(99)
    assert len(client.calls) == 1
    manifest_path.write_bytes(original)  # restore the exact backup artifact
    assert adapter(tmp_path, client)[0].already_published(99)
    record = json.loads((tmp_path / "scores/day-0.submitted.json").read_text())
    assert record["calculation_evidence"] == ref
    assert record["direct_intent"]["calculation_evidence"] == ref
    assert len(client.calls) == 1


def test_mutated_artifact_and_path_traversal_refuse(tmp_path):
    _, _, _, ref = calculation(tmp_path)
    manifest = verify(tmp_path / "scores", ref)
    path = tmp_path / "scores" / manifest["artifacts"]["fixture"]["path"]
    path.write_text("{}")
    with pytest.raises(CalculationEvidenceRefused, match="hash differs"):
        verify(tmp_path / "scores", ref)
    with pytest.raises(CalculationEvidenceRefused, match="invalid"):
        verify(tmp_path / "scores", {"sha256": digest(b"{}"), "path": "../private"})
