"""Collector boundary integration; ancestry itself has separate raw replay tests."""
import json
from pathlib import Path
import pytest
from noah.reports.backing.offline import EvidenceError, _evaluation_inputs
from noah.reports.common import ReportError
from noah.reports.tests.test_history import SyntheticChain, T0, _hash, _round, _run


def collected(tmp_path, monkeypatch, *, offset=1):
    chain = SyntheticChain(10)
    contract = _round(chain, 6)
    evaluation = {'block_number': 6 + offset, 'block_hash': _hash(6 + offset)[2:],
                  'timestamp_ms': T0 + (6 + offset) * 12000}
    proof = {'schema': 'synthetic-boundary-proof', 'evaluation': evaluation}
    def checked(source, boundary, **kwargs):
        assert boundary == evaluation
        assert kwargs == {'genesis_hash': contract['genesis_hash'], 'max_ancestor_distance': 4}
        return proof
    monkeypatch.setattr('noah.reports.backing.collector.prove_evaluation', checked)
    result = _run(chain, contract, monkeypatch, out_dir=tmp_path / 'packages',
                  evaluation=evaluation, evaluation_ancestor_limit=4)
    return result, evaluation, proof


def test_collection_stops_at_frozen_boundary_and_retains_proof(tmp_path, monkeypatch):
    result, boundary, proof = collected(tmp_path, monkeypatch)
    root = Path(result['directory'])
    coverage = json.loads((root / 'coverage.json').read_text())
    assert max(row['number'] for row in coverage) == boundary['block_number']
    assert result['evaluation_proof'] == proof
    assert json.loads((root / 'evaluation-proof.json').read_text()) == proof
    assert _evaluation_inputs(root) == {'evaluation': boundary, 'max_ancestor_distance': 4}
    assert all(row['collection']['through_ms'] == boundary['timestamp_ms'] for row in result['evidence'].values())


def test_open_window_is_not_retained_as_final(tmp_path, monkeypatch):
    with pytest.raises(ReportError, match='close the required round'):
        collected(tmp_path, monkeypatch, offset=0)
    assert not list(tmp_path.rglob('round.json'))


@pytest.mark.parametrize('missing', ['evaluation.json', 'evaluation-proof.json'])
def test_partial_boundary_artifacts_cannot_be_legacy(tmp_path, monkeypatch, missing):
    result, _, _ = collected(tmp_path, monkeypatch)
    root = Path(result['directory'])
    (root / missing).unlink()
    with pytest.raises(EvidenceError, match='unavailable'):
        _evaluation_inputs(root)


def test_external_boundary_artifact_refused(tmp_path, monkeypatch):
    result, _, _ = collected(tmp_path, monkeypatch)
    root = Path(result['directory'])
    source = root / 'evaluation.json'
    outside = tmp_path / 'outside.json'
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(EvidenceError, match='symlink'):
        _evaluation_inputs(root)


def test_legacy_collection_still_has_no_frozen_boundary(tmp_path, monkeypatch):
    chain = SyntheticChain(10)
    result = _run(chain, _round(chain, 6), monkeypatch, out_dir=tmp_path / 'packages')
    assert result['evaluation_proof'] is None
    assert _evaluation_inputs(Path(result['directory'])) is None


def test_decoded_cutoff_clock_must_match_frozen_storage_proof(tmp_path, monkeypatch):
    from noah.reports.tests.test_history import _timestamp
    chain = SyntheticChain(10)
    contract = _round(chain, 6)
    boundary = {'block_number': 8, 'block_hash': _hash(8)[2:], 'timestamp_ms': T0 + 8 * 12000}
    chain.extrinsics[_hash(8)][0] = _timestamp(boundary['timestamp_ms'] - 1)
    monkeypatch.setattr('noah.reports.backing.collector.prove_evaluation',
                        lambda *_args, **_kwargs: {'evaluation': boundary})
    with pytest.raises(ValueError, match='decoded evaluation'):
        _run(chain, contract, monkeypatch, out_dir=tmp_path,
             evaluation=boundary, evaluation_ancestor_limit=4)
