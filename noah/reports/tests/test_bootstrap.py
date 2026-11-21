"""Bounded bootstrap and corruption tests on a synthetic chain."""
import gzip
import json
from pathlib import Path
from noah.reports.backing.bootstrap import extend_history, sample_history, status_history, verify_history
from noah.reports.backing.offline import verify_package
from noah.reports.tests.test_history import MINER, SyntheticChain, _hash, _receipts, _round, _run, _verdict


def _extend(chain, directory, start, end, contract):
    return extend_history(
        chain,
        directory,
        start,
        end,
        ark_coldkey=contract["ark_coldkey"],
        ark_validator_hotkey=contract["ark_validator_hotkey"],
        eligibility_policy_version=contract["eligibility_policy_version"],
    )

def test_gap_and_incompatible_checkpoint_do_not_advance(tmp_path):
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    directory = tmp_path / "history"
    assert _extend(chain, directory, 0, 2, contract)["advanced"] is True
    seen = {"blocks": 0}

    def _block_hash(number):
        seen["blocks"] += 1
        return _hash(number)

    chain.get_block_hash = _block_hash
    gap = _extend(chain, directory, 4, 5, contract)
    assert gap["advanced"] is False
    assert gap["error"] == "history_gap"
    assert gap["through_block"] == 2
    assert seen["blocks"] == 1  # genesis is re-read; the range is not

    seen["blocks"] = 0
    foreign = extend_history(
        chain,
        directory,
        3,
        5,
        ark_coldkey="ab" * 32,
        ark_validator_hotkey=contract["ark_validator_hotkey"],
        eligibility_policy_version=contract["eligibility_policy_version"],
    )
    assert foreign["advanced"] is False
    assert foreign["error"] == "history_context"
    assert foreign["through_block"] == 2
    assert seen["blocks"] == 0
    chain.headers[_hash(3)]["parentHash"] = "0x" + "ff" * 32
    parent = _extend(chain, directory, 3, 4, contract)
    assert parent["advanced"] is False
    assert parent["error"] == "block 3: parent mismatch"
    assert status_history(directory)["through_block"] == 2
    assert not (directory / "segments" / "000000000003-000000000004.jsonl.gz").exists()

def test_sample_is_not_coverage(tmp_path):
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    directory = tmp_path / "history"
    assert _extend(chain, directory, 0, 1, contract)["advanced"] is True
    before = (directory / "checkpoint.json").read_bytes()
    sample = sample_history(
        chain,
        directory,
        3,
        4,
        ark_coldkey=contract["ark_coldkey"],
        ark_validator_hotkey=contract["ark_validator_hotkey"],
        eligibility_policy_version=contract["eligibility_policy_version"],
    )
    assert sample["complete"] is True
    assert sample["coverage"] == "sample"
    assert (directory / "checkpoint.json").read_bytes() == before
    assert status_history(directory)["through_block"] == 1
    extended = _extend(chain, directory, 2, 4, contract)
    assert extended["advanced"] is True
    assert extended["through_block"] == 4

def test_bad_segment_does_not_become_a_miner_verdict(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in ("responses.jsonl.gz", "round.json", "hotkeys.json", "receipts.jsonl.gz", "metadata.hex"):
        (package / name).write_bytes(b"")
    segment = package / "segments" / "000000000000-000000000000.jsonl.gz"
    segment.parent.mkdir()
    with gzip.open(segment, "wb") as handle:
        handle.write(b"{}\n")
    checkpoint = {
        "segments": [{
            "file": "segments/000000000000-000000000000.jsonl.gz",
            "from_block": 0,
            "sha256": "0" * 64,
            "through_block": 0,
        }]
    }
    (package / "history-checkpoint.json").write_text(json.dumps(checkpoint))
    report = verify_package(package)
    assert report["status"] == "UNVERIFIABLE"
    assert report["verdicts"] == {}
    assert report["reason"] == "history_responses"

def test_later_ranges_do_not_reread_earlier_segments(tmp_path, monkeypatch):
    """Labeled synthetic. Counts decompressions, not a live chain."""
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    directory = tmp_path / "history"
    reads = []
    from noah.reports.backing import history as history_mod

    real = history_mod._gzip_sha256

    def counted(path):
        reads.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(history_mod, "_gzip_sha256", counted)
    assert _extend(chain, directory, 0, 1, contract)["advanced"] is True
    assert _extend(chain, directory, 2, 3, contract)["advanced"] is True
    assert _extend(chain, directory, 4, 5, contract)["advanced"] is True
    first = "000000000000-000000000001.jsonl.gz"
    assert reads.count(first) == 1
    assert reads.count("000000000002-000000000003.jsonl.gz") == 1
    assert reads.count("000000000004-000000000005.jsonl.gz") == 1
    assert verify_history(directory)["ok"] is True

def test_not_from_genesis_is_refused(tmp_path):
    chain = SyntheticChain(4)
    contract = _round(chain, 3)
    refused = _extend(chain, tmp_path / "history", 2, 3, contract)
    assert refused["advanced"] is False
    assert refused["error"] == "history_not_from_genesis"
    assert not (tmp_path / "history" / "checkpoint.json").exists()

def test_provider_error_text_is_not_retained(tmp_path):
    class Broken(SyntheticChain):
        def get_block(self, *args, **kwargs):
            raise RuntimeError('provider-user-and-password-must-not-be-retained')
    chain=Broken(8);r=_round(chain,6)
    result=_extend(chain,tmp_path/'history',0,5,r)
    assert not result['advanced']
    assert 'provider-user-and-password' not in json.dumps(result)
    assert 'RuntimeError' in result['error']


def test_checkpoint_staging_does_not_follow_legacy_temp_symlink(tmp_path):
    import os, stat
    from noah.reports.backing.bootstrap import _atomic_json
    target = tmp_path / 'checkpoint.json'
    victim = tmp_path / 'unrelated'; victim.write_bytes(b'preserve')
    old_temp = target.with_suffix('.json.tmp'); old_temp.symlink_to(victim)
    previous_umask = os.umask(0)
    try:
        _atomic_json(target, {'height': 7})
    finally:
        os.umask(previous_umask)
    assert victim.read_bytes() == b'preserve' and old_temp.is_symlink()
    assert json.loads(target.read_bytes()) == {'height': 7}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob('.checkpoint.json.*.tmp')) == []


def test_checkpoint_failed_replacement_preserves_old_file_and_cleans_stage(tmp_path, monkeypatch):
    import pytest
    from noah.reports.backing import bootstrap
    target = tmp_path / 'checkpoint.json'; target.write_bytes(b'original')
    def refuse(*_): raise OSError('synthetic replacement refusal')
    monkeypatch.setattr(bootstrap.os, 'replace', refuse)
    with pytest.raises(OSError):bootstrap._atomic_json(target, {'height': 7})
    assert target.read_bytes() == b'original'
    assert list(tmp_path.glob('.checkpoint.json.*.tmp')) == []
