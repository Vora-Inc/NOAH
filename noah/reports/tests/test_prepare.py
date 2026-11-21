"""Local prepare. No chain and no signature."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from noah.reports.backing.keys import ss58_encode
from noah.reports.backing.prepare import PrepareError, load_prepared, prepare
from noah.reports.backing.protocol.preimage import PREIMAGE_LEN, decode

HOT = ss58_encode(b"\x11" * 32)
COLD = ss58_encode(b"\x22" * 32)
BLOCK = "0x" + "ab" * 32
GENESIS = "0x" + "cd" * 32


def _announcement(**overrides):
    fields = {
        "genesis": GENESIS,
        "round": 20261113,
        "block_number": 100,
        "block_hash": BLOCK,
        "block_timestamp_ms": 1_700_000_000_000,
        "netuid": 64,
    }
    fields.update(overrides)
    return fields


def _engine(**overrides):
    engine = {
        "status": "OK",
        "netuid": 64,
        "block_number": 100,
        "block_hash": BLOCK,
        "coverage": {"coverage_gap_exceeds_holder": False},
        "output1": {"status": "OK", "drop_ppm": 178},
        "output2": {"status": "OK", "alpha_required_rao": 5_000_000_000},
        "output3": {"status": "OK", "alpha_required_rao": 9_000_000_000},
    }
    engine.update(overrides)
    return engine


def _prepare(tmp_path, name="ready.json", compute_fn=None, **overrides):
    args = dict(
        release_prefix_bytes=b"x" * 16,
        announcement=_announcement(),
        reported_netuid=64,
        mining_netuid=82,
        deposit_block=40,
        deposit_event=2,
        hotkey=HOT,
        coldkey=COLD,
        endpoint='wss://' + 'user' + ':' + 'secret' + '@archive.example:443/path?' + 'token' + '=1',
        out=tmp_path / name,
        compute_fn=compute_fn or (lambda *_args: _engine()),
        salt=b"\x44" * 16,
    )
    args.update(overrides)
    return prepare(**args)


def test_reported_subnet_differs_from_mining_subnet_and_reloads(tmp_path):
    prepared = _prepare(tmp_path)
    assert prepared.mining_netuid == 82
    assert prepared.submission.netuid == 64
    assert len(prepared.submission.encode()) == PREIMAGE_LEN
    document = json.loads((tmp_path / "ready.json").read_text())
    assert document["deposit_reference_status"] == "miner-supplied-not-verified"
    assert document["mining_netuid_authenticated_by_commitment"] is False
    assert document["endpoint_label"] == "wss://archive.example:443"
    assert stat.S_IMODE(os.stat(tmp_path / "ready.json").st_mode) == 0o600
    loaded = load_prepared(tmp_path / "ready.json")
    assert loaded.commitment == prepared.commitment
    assert loaded.submission == prepared.submission
    assert loaded.mining_netuid == 82


def test_second_prepare_does_not_replace_salt(tmp_path):
    first = _prepare(tmp_path)
    original = (tmp_path / "ready.json").read_bytes()
    with pytest.raises(PrepareError, match="already exists"):
        _prepare(tmp_path, salt=b"\x99" * 16)
    assert (tmp_path / "ready.json").read_bytes() == original
    assert load_prepared(tmp_path / "ready.json").submission.salt == first.submission.salt


def test_concurrent_prepare_keeps_one_intact_file(tmp_path):
    barrier = threading.Barrier(2)
    errors = []
    winners = []

    def run(salt: bytes):
        def compute_fn(*_args):
            barrier.wait(timeout=5)
            return _engine()

        try:
            prepared = _prepare(tmp_path, name="race.json", compute_fn=compute_fn, salt=salt)
            winners.append(prepared.submission.salt)
        except PrepareError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=run, args=(bytes([value]) * 16,)) for value in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert errors == ["output already exists"]
    loaded = load_prepared(tmp_path / "race.json")
    assert loaded.submission.salt == winners[0]
    assert list(tmp_path.glob("*.tmp")) == []


def test_salt_uses_secrets_token_bytes(tmp_path, monkeypatch):
    seen = []

    def token_bytes(count):
        seen.append(count)
        return b"\x7e" * count

    monkeypatch.setattr("noah.reports.backing.prepare.secrets.token_bytes", token_bytes)
    prepared = _prepare(tmp_path, name="random.json", salt=None)
    assert 16 in seen
    assert prepared.submission.salt == b"\x7e" * 16


def test_invalid_inputs_write_nothing(tmp_path):
    out = tmp_path / "nope.json"
    with pytest.raises(PrepareError):
        _prepare(tmp_path, name="nope.json", announcement=_announcement(block_hash="0xabcd"))
    with pytest.raises(PrepareError):
        _prepare(tmp_path, name="nope.json", reported_netuid=0)
    with pytest.raises(PrepareError):
        _prepare(tmp_path, name="nope.json", deposit_block=100)
    with pytest.raises(PrepareError):
        _prepare(tmp_path, name="nope.json", hotkey="not-a-key")
    with pytest.raises(PrepareError):
        _prepare(tmp_path, name="nope.json", announcement=_announcement(round=20261340))
    with pytest.raises(PrepareError):
        _prepare(tmp_path, name="nope.json", deposit_event=70_000)
    with pytest.raises(PrepareError):
        _prepare(tmp_path, name="nope.json", reported_netuid=70_000)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_unavailable_and_coverage_gap_write_nothing(tmp_path):
    out = tmp_path / "gap.json"
    with pytest.raises(PrepareError, match="not ready"):
        _prepare(tmp_path, name="gap.json", compute_fn=lambda *_a: {"status": "UNAVAILABLE"})
    missing = _engine()
    del missing["coverage"]
    with pytest.raises(PrepareError, match="coverage gap"):
        _prepare(tmp_path, name="gap.json", compute_fn=lambda *_a: missing)
    with pytest.raises(PrepareError, match="coverage gap"):
        _prepare(
            tmp_path,
            name="gap.json",
            compute_fn=lambda *_a: _engine(coverage={"coverage_gap_exceeds_holder": True}),
        )
    assert not out.exists()


def test_separate_process_reloads_the_same_commitment(tmp_path):
    prepared = _prepare(tmp_path, name="reload.json")
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from noah.reports.backing.prepare import load_prepared\n"
        "loaded = load_prepared(Path(sys.argv[1]))\n"
        "print(json.dumps({\n"
        " 'commitment': loaded.commitment.hex(),\n"
        " 'netuid': loaded.submission.netuid,\n"
        " 'mining': loaded.mining_netuid,\n"
        " 'round': loaded.submission.round_yyyymmdd,\n"
        " 'ppm': loaded.submission.out1_ppm,\n"
        " 'out2': loaded.submission.out2_rao,\n"
        " 'out3': loaded.submission.out3_rao,\n"
        "}))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "reload.json")],
        check=True, capture_output=True, text=True, env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["commitment"] == prepared.commitment.hex()
    assert payload["netuid"] == 64
    assert payload["mining"] == 82
    assert payload["round"] == 20261113
    assert payload["ppm"] == 178
    assert decode(prepared.submission.encode()).out2_rao == payload["out2"]
