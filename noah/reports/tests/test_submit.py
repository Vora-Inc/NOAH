"""Commit and reveal without a chain. The localnet demonstration is separate."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

from noah.reports.backing.chain_session import commitment_info
from noah.reports.backing.deposit_index import locate
from noah.reports.backing.finality import prove_ancestry
from noah.reports.backing.keys import ss58_encode
from noah.reports.backing.prepare import prepare
from noah.reports.backing.protocol.timing import RoundWindows
from noah.reports.backing.submit import Inclusion, SubmitError, run_phase

HOT = ss58_encode(b"\x33" * 32)
COLD = ss58_encode(b"\x44" * 32)
GENESIS = "0x" + "11" * 32
BLOCK = "0x" + "22" * 32


class Key:
    crypto_type = 1  # Synthetic SR25519 interface; no actual signing material.
    def __init__(self, raw: bytes):
        self.public_key = raw
        self.ss58_address = ss58_encode(raw)


class FakeChain:
    def __init__(self):
        self.genesis_hash = GENESIS
        self.now = 1_000_100
        self.registered = {(7, HOT)}
        self.stored = None
        self.sent = []
        self.known = {}
        self.visible = True
        self.final = True
        self.pause = None

    def genesis(self):
        return self.genesis_hash

    def finalized_time_ms(self):
        return self.now

    def is_registered(self, netuid, hotkey):
        return (netuid, hotkey) in self.registered

    def commitment(self, netuid, hotkey):
        return self.stored

    def sign(self, keypair, netuid, variant, payload):
        handle = {"netuid": netuid, "variant": variant, "payload": payload, "hash": "0x" + payload[:16].hex() + f"{netuid:04x}"}
        return handle["hash"], handle

    def broadcast(self, handle):
        if self.pause is not None:
            self.pause.set()
            time.sleep(0.3)
        self.sent.append(handle)
        if handle["variant"] == "Raw32":
            self.stored = handle["payload"]
        else:
            self.stored = b"\xff" * 32
        inclusion = Inclusion(handle["hash"], "0x" + "aa" * 32, 8, 1, self.now, True)
        self.known[handle["hash"]] = inclusion
        return inclusion

    def find(self, extrinsic_hash):
        if not self.visible:
            return None
        return self.known.get(extrinsic_hash)

    def finality(self, block_hash, block_number):
        return {
            "matches": self.final,
            "finalized_head": "0x" + "bb" * 32,
            "canonical_hash": block_hash if self.final else "0x" + "cc" * 32,
        }


def _engine():
    return {
        "status": "OK",
        "netuid": 4,
        "block_number": 50,
        "block_hash": BLOCK,
        "coverage": {"coverage_gap_exceeds_holder": False},
        "output1": {"status": "OK", "drop_ppm": 10},
        "output2": {"status": "OK", "alpha_required_rao": 20},
        "output3": {"status": "OK", "alpha_required_rao": 30},
    }


def _announcement():
    return {
        "announced_at_ms": 1_000_000,
        "ark_coldkey": COLD,
        "block_hash": BLOCK,
        "block_number": 50,
        "block_timestamp_ms": 1_000_000,
        "designated_validator": HOT,
        "genesis": GENESIS,
        "netuid": 4,
        "round": 20261114,
    }


def _windows():
    return {"accelerated": True, "commit_window_ms": 60_000, "reveal_window_ms": 60_000}


def _prepared(tmp_path):
    return prepare(
        release_prefix_bytes=b"x" * 16,
        announcement=_announcement(),
        reported_netuid=4,
        mining_netuid=82,
        deposit_block=10,
        deposit_event=3,
        hotkey=HOT,
        coldkey=COLD,
        endpoint="wss://archive.example:443",
        out=tmp_path / "prepared.json",
        compute_fn=lambda *_args: _engine(),
        salt=b"\x55" * 16,
    )


def _commit(tmp_path, chain, **overrides):
    if not (tmp_path / "prepared.json").exists():
        _prepared(tmp_path)
    args = dict(
        phase="commit",
        prepared_path=tmp_path / "prepared.json",
        announcement=_announcement(),
        mining_netuid=7,
        windows_doc=_windows(),
        keypair=Key(b"\x33" * 32),
        evidence=tmp_path / "evidence",
        chain=chain,
    )
    args.update(overrides)
    return run_phase(**args)


def test_commit_then_reveal_keeps_the_prepared_bytes_and_the_target(tmp_path):
    chain = FakeChain()
    first = _commit(tmp_path, chain)
    original = (tmp_path / "prepared.json").read_bytes()
    assert first.sent is True
    assert chain.sent[0]["netuid"] == 7
    assert chain.sent[0]["variant"] == "Raw32"
    chain.now = 1_060_100
    second = run_phase(
        phase="reveal",
        prepared_path=tmp_path / "prepared.json",
        announcement=_announcement(),
        mining_netuid=7,
        windows_doc=_windows(),
        keypair=Key(b"\x33" * 32),
        evidence=tmp_path / "evidence",
        chain=chain,
    )
    assert second.sent is True
    assert chain.sent[1]["netuid"] == 7
    assert chain.sent[1]["variant"] == "BigRaw"
    assert (tmp_path / "prepared.json").read_bytes() == original
    assert json.loads((tmp_path / "evidence" / "commitment-of-before-reveal.json").read_text())["raw32"] == "0x" + chain.sent[0]["payload"].hex()


def test_repeated_commit_and_reveal_do_not_send(tmp_path):
    chain = FakeChain()
    _commit(tmp_path, chain)
    again = _commit(tmp_path, chain)
    chain.now = 1_060_100
    run_phase(
        phase="reveal",
        prepared_path=tmp_path / "prepared.json",
        announcement=_announcement(),
        mining_netuid=7,
        windows_doc=_windows(),
        keypair=Key(b"\x33" * 32),
        evidence=tmp_path / "evidence",
        chain=chain,
    )
    repeat = run_phase(
        phase="reveal",
        prepared_path=tmp_path / "prepared.json",
        announcement=_announcement(),
        mining_netuid=7,
        windows_doc=_windows(),
        keypair=Key(b"\x33" * 32),
        evidence=tmp_path / "evidence",
        chain=chain,
    )
    assert again.sent is False
    assert repeat.sent is False
    assert [item["variant"] for item in chain.sent] == ["Raw32", "BigRaw"]


def test_repeat_after_the_window_does_not_send(tmp_path):
    chain = FakeChain()
    _commit(tmp_path, chain)
    chain.now = 9_000_000
    again = _commit(tmp_path, chain)
    assert again.sent is False
    assert len(chain.sent) == 1


def test_concurrent_commit_does_not_double_send(tmp_path):
    chain = FakeChain()
    chain.pause = threading.Event()
    _prepared(tmp_path)
    errors = []

    def once():
        try:
            run_phase(
                phase="commit",
                prepared_path=tmp_path / "prepared.json",
                announcement=_announcement(),
                mining_netuid=7,
                windows_doc=_windows(),
                keypair=Key(b"\x33" * 32),
                evidence=tmp_path / "evidence",
                chain=chain,
            )
        except SubmitError as exc:
            errors.append(str(exc))

    first = threading.Thread(target=once)
    first.start()
    assert chain.pause.wait(2)
    once()
    first.join()
    assert len(chain.sent) == 1
    assert errors == ["another command holds this phase"]


def test_unknown_outcome_is_not_sent_again(tmp_path):
    chain = FakeChain()
    chain.visible = False
    _prepared(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "commit.journal").write_text(json.dumps({
        "extrinsic_hash": "0xdead",
        "payload_sha256": json.loads((tmp_path / "prepared.json").read_text())["commitment_sha256"],
        "phase": "commit",
        "state": "submitted",
    }))
    with pytest.raises(SubmitError, match="outcome unknown"):
        _commit(tmp_path, chain)
    assert chain.sent == []
    assert json.loads((evidence / "commit.journal").read_text())["state"] == "unresolved"
    with pytest.raises(SubmitError, match="outcome unknown"):
        _commit(tmp_path, chain)
    assert chain.sent == []


def test_refuses_before_sending(tmp_path):
    chain = FakeChain()
    _prepared(tmp_path)
    common = dict(
        phase="commit",
        prepared_path=tmp_path / "prepared.json",
        announcement=_announcement(),
        mining_netuid=7,
        windows_doc=_windows(),
        keypair=Key(b"\x33" * 32),
        evidence=tmp_path / "evidence",
        chain=chain,
    )
    chain.genesis_hash = "0x" + "ee" * 32
    with pytest.raises(SubmitError, match="connected chain"):
        run_phase(**common)
    chain.genesis_hash = GENESIS
    bad = _announcement()
    bad["round"] = 20260101
    mismatched = dict(common)
    mismatched["announcement"] = bad
    with pytest.raises(SubmitError, match="round"):
        run_phase(**mismatched)
    wrong_signer = dict(common)
    wrong_signer["keypair"] = Key(b"\x99" * 32)
    with pytest.raises(SubmitError, match="signer"):
        run_phase(**wrong_signer)
    wrong_subnet = dict(common)
    wrong_subnet["mining_netuid"] = 9
    with pytest.raises(SubmitError, match="no uid"):
        run_phase(**wrong_subnet)
    early = dict(common)
    early["now_ms"] = 1
    with pytest.raises(SubmitError, match="outside"):
        run_phase(**early)
    chain.now = 1_060_100
    with pytest.raises(SubmitError, match="outside"):
        run_phase(**common)
    assert chain.sent == []
    assert not (tmp_path / "evidence" / "commit.journal").exists()


def test_edited_sidecar_does_not_change_the_target(tmp_path):
    chain = FakeChain()
    _prepared(tmp_path)
    document = json.loads((tmp_path / "prepared.json").read_text())
    document["mining_netuid"] = 4
    (tmp_path / "prepared.json").write_text(json.dumps(document))
    _commit(tmp_path, chain)
    assert chain.sent[0]["netuid"] == 7


def test_block_wide_index_differs_from_the_transaction_local_index():
    events = [
        {"phase": {"ApplyExtrinsic": 0}, "event_id": "ExtrinsicSuccess"},
        {"phase": {"ApplyExtrinsic": 1}, "event_id": "StakeAndHotkeyTransferred"},
    ]
    assert locate(events, 1, "StakeAndHotkeyTransferred") == (1, 0)
    observed = [
        {"phase": "Initialization", "extrinsic_idx": None, "event_id": "Issued"},
        {"phase": "ApplyExtrinsic", "extrinsic_idx": 2, "event_id": "StakeAndHotkeyTransferred"},
    ]
    assert locate(observed, 2, "StakeAndHotkeyTransferred") == (1, 0)


def test_finality_requires_the_inclusion_hash_not_just_the_height():
    headers = {
        "head": (3, "mid"),
        "mid": (2, "old"),
        "old": (1, "genesis"),
    }
    proof = prove_ancestry(
        head_hash="head",
        head_number=3,
        inclusion_hash="other",
        inclusion_number=2,
        header_of=lambda name: headers[name],
    )
    assert proof["matches"] is False
    assert proof["canonical_hash"] == "mid"
    ok = prove_ancestry(
        head_hash="head",
        head_number=3,
        inclusion_hash="mid",
        inclusion_number=2,
        header_of=lambda name: headers[name],
    )
    assert ok["matches"] is True
