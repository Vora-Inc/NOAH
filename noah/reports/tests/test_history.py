"""Suffix history against one full scan. The chain here is synthetic and small.

Labeled synthetic. Not a live chain and not a Finney measurement.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from noah.reports.backing.collector import _collect
from noah.reports.backing.history import make_checkpoint
from noah.reports.backing.keys import ss58_encode
from noah.reports.backing.offline import verify_package
from noah.reports.backing.protocol.policy import ENGINE_PIN
from noah.reports.verification import verify_collected

T0 = 1_794_683_453_500
ARK = bytes.fromhex("66" * 32)
MINER = bytes.fromhex("11" * 32)
OTHER = bytes.fromhex("22" * 32)
FORBIDDEN = {"VERIFIED", "MISSING", "MISMATCH"}


class _Call:
    def __init__(self, value):
        self.value = value


class _Query:
    def __init__(self, value):
        self.value = value

    def to_hex(self):
        return "0x00"


class SyntheticChain:
    """Enough of a node for one bounded history walk. Not retained chain bytes."""

    def __init__(self, length: int):
        self.headers = {}
        self.events = {}
        self.extrinsics = {}
        previous = "0x" + "00" * 32
        for number in range(length):
            digest = _hash(number)
            self.headers[digest] = {
                "number": hex(number),
                "parentHash": previous,
            }
            self.extrinsics[digest] = [_timestamp(T0 + number * 12_000)]
            self.events[digest] = []
            previous = digest
        self.head = _hash(length - 1)
        self.header_numbers = []
        self._plant_deposit(_hash(1))

    def rpc_request(self, method, params=None, result_handler=None):
        params = params or []
        if method == "chain_getFinalizedHead":
            return {"result": self.head}
        if method == "chain_getHeader":
            self.header_numbers.append(int(self.headers[params[0].lower()]["number"], 16))
            return {"result": self.headers[params[0].lower()]}
        if method == "state_getMetadata":
            return {"result": "0x6d657461"}
        if method == "state_getRuntimeVersion":
            return {"result": {"specName": "node-subtensor", "specVersion": 490}}
        if method == "chain_getBlock":
            return {"result": {"block": {"extrinsics": [], "header": self.headers[params[0].lower()]}}}
        if method == "chain_getBlockHash" and params and params[0] in (0, "0x0"):
            return {"result": _hash(0)}
        raise KeyError(method)

    def get_block(self, block_hash):
        return {"extrinsics": [_Call(item) for item in self.extrinsics[block_hash.lower()]]}

    def query(self, module, storage, params=None, block_hash=None, **_kwargs):
        return _Query(self.events.get(str(block_hash).lower(), []))

    def get_metadata_storage_function(self, _module, _name):
        return _Query({"type": {"Map": {"hashers": ["Blake2_128Concat"]}}})

    def create_storage_key(self, *_args, **_kwargs):
        return _Query(None)

    def get_block_hash(self, number):
        return _hash(number)

    def _plant_deposit(self, block_hash):
        ark = ss58_encode(ARK)
        origin = ss58_encode(MINER)
        hot = ss58_encode(OTHER)
        self.events[block_hash] = [
            {
                "attributes": {
                    "amount": 5_000_000,
                    "destination_coldkey": ark,
                    "destination_hotkey": hot,
                    "destination_netuid": 1,
                    "origin_coldkey": origin,
                    "origin_hotkey": origin,
                    "origin_netuid": 1,
                },
                "event_id": "StakeAndHotkeyTransferred",
                "extrinsic_idx": 0,
                "module_id": "SubtensorModule",
                "phase": "ApplyExtrinsic",
            },
            {
                "attributes": (ark, hot, 0, 5_000_000, 1, 0),
                "event_id": "StakeAdded",
                "extrinsic_idx": 0,
                "module_id": "SubtensorModule",
                "phase": "ApplyExtrinsic",
            },
        ]
        self.extrinsics[block_hash].append({"address": origin, "call": {"call_function": "transfer_stake_and_hotkey", "call_module": "SubtensorModule"}})


def _hash(number: int) -> str:
    return "0x" + (bytes([number + 1]) * 32).hex()


def _timestamp(moment: int) -> dict:
    return {"address": None, "call": {"call_args": [{"name": "now", "value": moment}], "call_function": "set", "call_module": "Timestamp"}}


def _round(chain: SyntheticChain, number: int) -> dict:
    opened = T0 + number * 12_000
    return {
        "ark_coldkey": ARK.hex(),
        "ark_validator_hotkey": OTHER.hex(),
        "block_hash": _hash(number)[2:],
        "block_number": number,
        "block_timestamp_ms": opened,
        "calculation_version": ENGINE_PIN,
        "commit_close_ms": opened + 1_000,
        "commit_open_ms": opened,
        "eligibility_policy_version": "attributed-deposit-before-b-v1",
        "genesis_hash": _hash(0)[2:],
        "mining_netuid": 82,
        "release_prefix": "24" * 16,
        "reported_netuid": 1,
        "reveal_close_ms": opened + 12_000,
        "reveal_open_ms": opened + 1_000,
        "round_date": "2026-11-14",
        "schema": "noah.backing.round.v1",
        "scoring_release": "fixture-release-not-deployed",
    }


def _engine(contract):
    return {
        "ark_coldkey": "0x" + contract["ark_coldkey"],
        "block_hash": "0x" + contract["block_hash"],
        "block_number": contract["block_number"],
        "coverage": {"coverage_gap_exceeds_holder": False},
        "netuid": contract["reported_netuid"],
        "output1": {"drop_ppm": 10, "status": "OK"},
        "output2": {"alpha_required_rao": 2_000_000, "status": "OK"},
        "output3": {"alpha_required_rao": 3_000_000, "status": "OK"},
        "status": "OK",
    }


def _run(chain, contract, monkeypatch, out_dir=None, **extra):
    monkeypatch.setattr("noah.reports.backing.collector.compute", lambda *args, **kwargs: (_engine(contract), {}))
    chain.head = _hash(contract["block_number"] + 2)
    # The finalized head must exist. Extend the chain object head only if that block was built.
    if chain.head not in chain.headers:
        chain.head = max(chain.headers, key=lambda item: int(chain.headers[item]["number"], 16))
    if not hasattr(chain, "_response_log"):
        chain._response_log = [{"byte_length": 1, "method": "chain_getHeader", "params": ["0x" + "ab" * 32], "result": {"number": "0x2"}, "sha256": "ab"}]
    return _collect(
        chain,
        "synthetic",
        [],
        contract,
        out_dir,
        [MINER.hex()],
        lambda: 0,
        chain_source=object(),
        **extra,
    )


def _load_saved(directory: Path):
    checkpoint_path = next(directory.rglob("history-checkpoint.json"))
    checkpoint = json.loads(checkpoint_path.read_text())
    with gzip.open(checkpoint_path.parent / "responses.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return checkpoint, rows


def _receipts(result):
    return result["evidence"][MINER.hex()]["deposit_history"]["receipts"]


def _verdict(result, contract):
    return verify_collected(contract, result["evidence"][MINER.hex()])


def _saved_pair(directory: Path):
    checkpoint_path = next(directory.rglob("history-checkpoint.json"))
    checkpoint = json.loads(checkpoint_path.read_text())
    with gzip.open(checkpoint_path.parent / "responses.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return checkpoint, rows


def test_suffix_matches_a_full_scan(tmp_path, monkeypatch):
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    full = _run(chain, contract, monkeypatch)
    bootstrap = _run(chain, _round(chain, 3), monkeypatch, out_dir=tmp_path / "bootstrap")
    checkpoint, rows = _saved_pair(tmp_path / "bootstrap")
    assert checkpoint["through_block"] == 2
    assert checkpoint["responses_sha256"]
    chain.header_numbers.clear()
    suffix = _run(chain, contract, monkeypatch, history_checkpoint=checkpoint, prior_responses=rows)
    assert 0 not in chain.header_numbers
    assert _receipts(suffix) == _receipts(full)
    assert suffix["evidence"][MINER.hex()]["deposit_history"]["from_block"] == 0
    assert _verdict(suffix, contract)["status"] == _verdict(full, contract)["status"]
    assert _verdict(suffix, contract)["reason"] == _verdict(full, contract)["reason"]
    assert bootstrap["evidence"][MINER.hex()]["deposit_history"]["from_block"] == 0


def test_restart_uses_the_saved_checkpoint(tmp_path, monkeypatch):
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    full = _run(chain, contract, monkeypatch)
    _run(chain, _round(chain, 3), monkeypatch, out_dir=tmp_path / "saved")
    time.sleep(1.1)
    checkpoint_path = next((tmp_path / "saved").rglob("history-checkpoint.json"))
    reread = subprocess.run(
        [sys.executable, "-c", "import json,pathlib,sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())['responses_sha256'])", str(checkpoint_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    checkpoint, rows = _saved_pair(tmp_path / "saved")
    assert checkpoint["responses_sha256"] == reread.stdout.strip()
    resumed = _run(chain, contract, monkeypatch, history_checkpoint=checkpoint, prior_responses=rows)
    assert _receipts(resumed) == _receipts(full)
    resumed_verdict = _verdict(resumed, contract)
    full_verdict = _verdict(full, contract)
    assert (resumed_verdict["status"], resumed_verdict["reason"]) == (full_verdict["status"], full_verdict["reason"])


def test_incompatible_checkpoint_does_not_scan_from_genesis(monkeypatch):
    chain = SyntheticChain(8)
    contract = _round(chain, 6)
    base = {
        "ark_coldkey": contract["ark_coldkey"],
        "ark_validator_hotkey": contract["ark_validator_hotkey"],
        "eligibility_policy_version": contract["eligibility_policy_version"],
        "from_block": 0,
        "genesis_hash": contract["genesis_hash"],
        "head_hash": _hash(2),
        "observed_from_ms": T0,
        "receipts": [],
        "responses_sha256": "0" * 64,
        "schema": "backing-history-checkpoint.v1",
        "through_block": 2,
    }
    cases = (
        {**base, "from_block": 4},
        {**base, "ark_coldkey": "ab" * 32},
        {**base, "eligibility_policy_version": "other-policy"},
    )
    for broken in cases:
        chain.header_numbers.clear()
        result = _run(chain, contract, monkeypatch, history_checkpoint=broken, prior_responses=[])
        verdict = _verdict(result, contract)
        assert chain.header_numbers == []
        assert verdict["status"] == "UNVERIFIABLE"
        assert verdict["status"] not in {"MISSING", "MISMATCH"}
