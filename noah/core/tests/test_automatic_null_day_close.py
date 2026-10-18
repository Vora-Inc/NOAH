"""Accelerated local day close: real loop/scorers/records, simulated chain only.

No node, RPC, wallet, clock mutation or live publication is used. The 58 synthetic
miners retain the requested 8/18/32 cohort sizes, not historical attribution or
observed earnings. Native finality and payouts require separate acceptance.
"""

import json
from types import SimpleNamespace

import pytest

from ...chain.agreeing import AgreeingChain
from ...chain.dayblocks import day_block_range
from ..calculation_record import verify
from ..chain_adapter import ChainAdapter
from ..loop import run_forever
from ..proxy_route import CommitIdentity
from ..reader import ChainReader
from ..submitter import Publisher
from ._composed_call import ComposesCalls, inner_call
from .test_direct_runtime475 import decoded, event
from .test_proxy_route import SIGNER, VALIDATOR

ANCHOR = 1_791_158_400  # Synthetic UTC-midnight anchor; not historical evidence.
DAY_BLOCKS = 7200
TEMPO = 360
SETTLE = 20
GENESIS = "0x" + "a" * 64
KEYS = {0: VALIDATOR, **{uid: f"synthetic-hotkey-{uid}" for uid in range(1, 59)}}
# Explicit synthetic fixture inputs; not operator policy defaults.
PROFILE = {
    "profileVersion": "synthetic-50pct-v1",
    "createdAt": "2026-09-29T00:00:00Z",
    "targetFp": "500000000",
    "harshness": 2,
    "capFp": "1000000000",
    "halfLifeDays": "45",
    "streakCeilingFp": "2000000000",
    "streakRampDays": "90",
    "ongoingHoldRao": "0",
    "entryDepositPerHotkeyRao": "10000000000",
}
DIALS = {
    k: v
    for k, v in PROFILE.items()
    if k not in ("profileVersion", "createdAt", "entryDepositPerHotkeyRao")
}


def fixture(day):
    rows = []
    for group, count, deposit in [
        ("Chris", 8, 50_000_000_000),
        ("Tony", 18, 25_000_000_000),
        ("Miner", 32, 12_500_000_000),
    ]:
        for _ in range(count):
            uid = len(rows) + 1
            rows.append(
                {
                    "coldkey": f"synthetic-{group}",
                    "uid": uid,
                    "firstActivityDay": "0",
                    "registrationDay": "0",
                    "entryDepositRao": "10000000000",
                    "holdRao": "500000000000",
                    "days": {
                        str(d): {
                            "paidRao": "50000000000",
                            "paidLockedArrivalRao": "0",
                            "payUnlockedRao": "0",
                            "noahDepositRao": str(deposit),
                            "foreignDepositNoahEquivRao": "0",
                            "noFault": False,
                        }
                        for d in range(day + 1)
                    },
                }
            )
    return {
        "schema": "noah-fixture/6",
        "name": "automatic-null-day-close",
        "evalDay": str(day),
        "dayEpochTs": str(ANCHOR),
        "dials": DIALS,
        "miners": rows,
    }


class Ledger(ComposesCalls):
    """SDK-shaped in-memory blocks; no signing or runtime execution."""

    def __init__(self):
        self.block = DAY_BLOCKS - 1
        self.nonce = 0
        self.blocks = {}
        self.calls = []
        self.after_send = None

    def block_info(self, block):
        return SimpleNamespace(
            number=block,
            hash=f"0x{block:064x}",
            extrinsics=self.blocks.get(block, {}).get("extrinsics", []),
        )

    def query(self, key, params=None, block=None):
        height = self.block if block is None else block
        prior = [h for h in self.blocks if h <= height]
        if key == ("System", "Account"):
            return {"nonce": self.nonce}
        if key == ("System", "Events"):
            return self.blocks.get(height, {}).get(
                "events", [event("System", "ExtrinsicSuccess", {}, 0)]
            )
        return {
            "Tempo": TEMPO,
            "SubnetEpochConsensus": "Null",
            "ValidatorPermit": [True] + [False] * (len(KEYS) - 1),
            "Keys": KEYS.get(params[1]) if key[1] == "Keys" else None,
            "SubnetOwner": "synthetic-owner",
            "SubnetOwnerHotkey": VALIDATOR,
            "Owner": "synthetic-miner",
            "LastUpdate": [max(prior, default=0)],
            "WeightsSetRateLimit": 0,
            "Weights": self.blocks[max(prior)]["weights"] if prior else [],
        }[key[1]]

    def query_map(self, key, params):
        assert key == ("SubtensorModule", "Keys")
        return list(KEYS.items())

    def submit_call(self, call, wallet, **kwargs):
        assert call.module == "Proxy" and wallet.ss58_address == SIGNER
        assert call.params["real"] == VALIDATOR
        assert kwargs == {"wait_for_inclusion": True, "wait_for_finalization": True}
        inner = inner_call(call)
        assert inner.function == "set_mechanism_weights" and inner.params["netuid"] == 2
        self.calls.append(call)
        self.block += 1
        self.blocks[self.block] = {
            "extrinsics": [{}, {}, {"address": SIGNER, "nonce": self.nonce, "call": decoded(call)}],
            "events": [
                event("SubtensorModule", "WeightsSet", [2, 0]),
                event("Proxy", "ProxyExecuted", {"result": {"Ok": None}}),
                event("System", "ExtrinsicSuccess", {}),
            ],
            "weights": list(zip(inner.params["dests"], inner.params["weights"])),
        }
        self.nonce += 1
        if self.after_send:
            self.after_send(self)
        return SimpleNamespace(
            error=None,
            success=True,
            extrinsic_id=f"{self.block}-2",
            block_hash=f"0x{self.block:064x}",
            events=self.blocks[self.block]["events"],
        )


class Source:
    def __init__(self, ledger):
        self.ledger = ledger

    def head_block(self):
        return self.ledger.block

    def block_hash(self, block):
        return GENESIS if block == 0 else f"0x{block:064x}"

    def block_timestamp(self, block):
        return ANCHOR + block * 12

    def canary(self, block):
        return 256

    def seat_hotkeys(self, block):
        return KEYS.copy()


def make_adapter(directory, ledger):
    chain = AgreeingChain(
        sources={"a": Source(ledger), "b": Source(ledger)},
        endpoints={"a": "https://first.invalid", "b": "https://second.invalid"},
    )
    built = ChainAdapter(
        reader=ChainReader(ledger),
        publisher=Publisher(
            ledger,
            SimpleNamespace(ss58_address=SIGNER),
            netuid=2,
            commit_identity=CommitIdentity(VALIDATOR),
        ),
        runner_chain=chain,
        netuid=2,
        uid=0,
        wallet_hotkey=CommitIdentity(VALIDATOR),
        settle_blocks=SETTLE,
        publish_mode="direct",
        day_anchor_ts=ANCHOR,
        dials=DIALS,
        day_window=lambda h: day_block_range(chain, ANCHOR, h),
        publish_intent_path=directory / "intent.json",
        submitted_record_dir=directory / "scores",
    )
    # Inputs are explicit synthetic observations. Everything after ingestion uses
    # production code, including record_calculation and exact confirmation.
    built.read = lambda height: fixture(day_block_range(chain, ANCHOR, height)[0])
    built.last_confirmed_ark_pin = "synthetic-ark"
    return built


def run(directory, ledger, heads):
    count = len(heads)
    heads = iter(heads)
    ledger.block = next(heads)
    results = []

    def advance(_):
        ledger.block = next(heads)

    run_forever(
        make_adapter(directory, ledger),
        settle_blocks=SETTLE,
        state_path=directory / "loop.json",
        lock_path=directory / "loop.lock",
        identity=f"{GENESIS}/2/{VALIDATOR}",
        iterations=count,
        sleep=advance,
        on_tick=results.append,
    )
    return results


@pytest.fixture(autouse=True)
def profile(monkeypatch, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(PROFILE, indent=2) + "\n")
    monkeypatch.setenv("NOAH_PARAMETERS_JSON", str(path))
    monkeypatch.delenv("NOAH_EVENT_LOG", raising=False)


def test_two_accelerated_day_closes_keep_exact_58_miner_calculations_and_no_resend(tmp_path):
    ledger = Ledger()
    for day in (0, 1):
        close = (day + 1) * DAY_BLOCKS
        results = run(tmp_path, ledger, [close - 1, close, close + TEMPO])
        assert results[0].outcome == "refused" and "DayNotComplete" in results[0].detail
        assert any(r.outcome == "published" for r in results), results
        assert len(ledger.calls) == day + 1, results
        record = json.loads((tmp_path / f"scores/day-{day}.submitted.json").read_text())
        manifest = verify(tmp_path / "scores", record["calculation_evidence"])
        score = json.loads(
            (tmp_path / "scores" / manifest["artifacts"]["agreed_score"]["path"]).read_text()
        )
        assert len(score["miners"]) == 58
        assert {
            g: sum(m["coldkey"] == f"synthetic-{g}" for m in score["miners"])
            for g in ("Chris", "Tony", "Miner")
        } == {"Chris": 8, "Tony": 18, "Miner": 32}
        assert manifest["agreement"] == "python_typescript_identical"
        assert manifest["settlement"] == "not_observed"
        assert record["route"] == "proxy" and record["signer"] == SIGNER
        assert record["applied_weights_readback"]["consensus"] == "Null"
        assert record["submitted_weights_u16"] == manifest["publishable_weights_u16"]
        assert record["submitted_weights_u16"] == {
            str(k): v for k, v in record["applied_weights_readback"]["weights"].items()
        }
        assert not (tmp_path / "intent.json").exists()
        original = (tmp_path / f"scores/day-{day}.submitted.json").read_bytes()
        assert make_adapter(tmp_path, ledger).already_published(close - 1)
        assert (tmp_path / f"scores/day-{day}.submitted.json").read_bytes() == original
        assert len(ledger.calls) == day + 1
    assert not (tmp_path / "loop.lock").exists()


def test_lost_reply_recovery_requires_restored_calculation_and_never_sends_twice(tmp_path):
    ledger = Ledger()

    def lose_reply(_):
        raise TimeoutError("simulated lost response after inclusion")

    ledger.after_send = lose_reply
    run(tmp_path, ledger, [DAY_BLOCKS - 1, DAY_BLOCKS])
    assert len(ledger.calls) == 1
    marker = json.loads((tmp_path / "intent.json").read_text())
    manifest = verify(tmp_path / "scores", marker["calculation_evidence"])
    artifact = tmp_path / "scores" / manifest["artifacts"]["agreed_score"]["path"]
    original = artifact.read_bytes()
    artifact.unlink()
    from ..publish_intent import PublicationStatusUnknown

    with pytest.raises(PublicationStatusUnknown):
        make_adapter(tmp_path, ledger).already_published(DAY_BLOCKS - 1)
    assert (tmp_path / "intent.json").exists() and len(ledger.calls) == 1
    artifact.write_bytes(original)
    assert make_adapter(tmp_path, ledger).already_published(DAY_BLOCKS - 1)
    assert len(ledger.calls) == 1 and not (tmp_path / "intent.json").exists()
    record = json.loads((tmp_path / "scores/day-0.submitted.json").read_text())
    assert record["calculation_evidence"] == marker["calculation_evidence"]
    assert record["applied_weights_readback"]["consensus"] == "Null"


@pytest.mark.parametrize("failure", ["evidence_write", "source_disagreement"])
def test_closed_day_refuses_before_send_when_evidence_or_reader_agreement_fails(
    tmp_path, monkeypatch, failure
):
    if failure == "evidence_write":
        from .. import calculation_record

        def refuse(*args, **kwargs):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(calculation_record, "durable_write", refuse)
    else:
        original = Source.block_hash
        counts = {"created": 0}
        init = Source.__init__

        def initialize(self, ledger):
            init(self, ledger)
            self.disagree = counts["created"] % 2 == 1
            counts["created"] += 1

        monkeypatch.setattr(Source, "__init__", initialize)
        monkeypatch.setattr(
            Source,
            "block_hash",
            lambda self, block: (
                "0x" + "b" * 64 if self.disagree and block > 0 else original(self, block)
            ),
        )
    ledger = Ledger()
    results = run(tmp_path, ledger, [DAY_BLOCKS - 1, DAY_BLOCKS])
    assert not ledger.calls
    assert not list((tmp_path / "scores").glob("*.submitted.json"))
    assert not (tmp_path / "intent.json").exists()
    assert all(r.outcome != "published" for r in results)
