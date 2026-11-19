"""Retained calculation integrity tests using an explicitly synthetic chain."""

import hashlib
import json
from pathlib import Path

import pytest

from noah.reports.artifacts import check_retained_engine
from noah.reports.common import digest
from noah.reports.tests.test_history import SyntheticChain, _round, _run


@pytest.fixture
def retained(tmp_path, monkeypatch):
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    result = _run(chain, contract, monkeypatch, out_dir=tmp_path / "packages")
    return Path(result["directory"]), contract, result["engine"]


def test_collector_retains_full_engine_and_round_binding(retained):
    directory, contract, engine = retained
    raw = (directory / "engine.json").read_bytes()
    binding = json.loads((directory / "engine-binding.json").read_bytes())
    assert json.loads(raw) == engine
    assert type(json.loads(raw)["output2"]["alpha_required_rao"]) is int
    assert raw.endswith(b"\n")
    assert binding["engine_sha256"] == hashlib.sha256(raw).hexdigest()
    assert binding["round_sha256"] == digest(contract)
    assert check_retained_engine(directory, contract, engine) == (True, None)


@pytest.mark.parametrize("filename", ["engine.json", "engine-binding.json"])
@pytest.mark.parametrize("corruption", ["changed", "missing", "symlink"])
def test_corrupt_or_partial_engine_artifact_is_not_legacy(retained, filename, corruption):
    directory, contract, engine = retained
    path = directory / filename
    original = path.read_bytes()
    path.unlink()
    if corruption == "changed":
        path.write_bytes(original + b" ")  # Exact bytes, not only parsed JSON, are bound.
    elif corruption == "symlink":
        outside = directory.parent / "outside.json"
        outside.write_bytes(original)
        path.symlink_to(outside)
    accepted, reason = check_retained_engine(directory, contract, engine)
    assert not accepted and reason


def test_replacing_engine_and_hash_does_not_replace_reconstructed_result(retained):
    directory, contract, engine = retained
    path = directory / "engine.json"
    altered = json.loads(path.read_bytes())
    altered["output1"]["drop_ppm"] += 1
    raw = json.dumps(altered).encode()
    path.write_bytes(raw)
    binding_path = directory / "engine-binding.json"
    binding = json.loads(binding_path.read_bytes())
    binding["engine_sha256"] = hashlib.sha256(raw).hexdigest()
    binding_path.write_text(json.dumps(binding))
    assert check_retained_engine(directory, contract, engine) == (False, "retained_engine_mismatch")


def test_engine_binding_cannot_be_reused_for_a_different_round(retained):
    directory, contract, engine = retained
    changed = {**contract, "reveal_close_ms": contract["reveal_close_ms"] + 1000}
    assert check_retained_engine(directory, changed, engine) == (False, "retained_engine_binding_mismatch")


def test_legacy_package_is_explicitly_not_producer_ready(retained):
    directory, contract, engine = retained
    (directory / "engine.json").unlink()
    (directory / "engine-binding.json").unlink()
    assert check_retained_engine(directory, contract, engine) == (False, None)


def test_unavailable_threshold_retains_engine_without_inventing_projection(tmp_path, monkeypatch):
    from noah.reports.tests import test_history
    from noah.reports.backing.round_config import calculation_for_shadow
    from noah.reports.verification import verify_collected
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    engine = test_history._engine(contract)
    engine['output2'] = {'status': 'UNAVAILABLE', 'reason': 'threshold_not_reached'}
    monkeypatch.setattr(test_history, '_engine', lambda _: engine)
    result = _run(chain, contract, monkeypatch, out_dir=tmp_path / 'packages')
    directory = Path(result['directory'])
    assert json.loads((directory / 'engine.json').read_bytes()) == engine
    binding = json.loads((directory / 'engine-binding.json').read_bytes())
    assert binding['calculation_sha256'] is None
    assert check_retained_engine(directory, contract, engine) == (True, None)
    evidence = next(iter(result['evidence'].values()))
    assert verify_collected(contract, evidence)['status'] == 'UNVERIFIABLE'
    with pytest.raises(ValueError, match='not ready'):
        calculation_for_shadow(contract, engine)  # Miner preparation stays strict.
    engine['block_hash'] = '0x' + '99' * 32
    with pytest.raises(ValueError, match='block hash'):
        calculation_for_shadow(contract, engine, allow_unavailable_outputs=True)
