"""Earlier daily readiness, with unchanged UTC inputs and publication guards.

The chain and clock are synthetic; real adapter, twin scorers, artifact writer
and direct publisher run locally. No node, wallet, or remote mutation is used.
"""

import json
from types import SimpleNamespace

import pytest

from ...chain.sources import SourceDisagreement
from ..chain_adapter import ChainAdapter
from ..loop import read_state, run_forever, tick
from ..pinning import pin_height
from ..reader import ChainUnreadable
from .test_automatic_null_day_close import (
    ANCHOR,
    GENESIS,
    SETTLE,
    TEMPO,
    Ledger,
    Source,
    make_adapter,
    profile,  # noqa: F401 -- reuse the explicit test profile fixture
)
from .test_proxy_route import VALIDATOR

# Midnight is inside a 360-block period, so the old throttle waits until7200.
CLOSE = 7100


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(Source, "block_timestamp", lambda self, h: ANCHOR + (h + 100) * 12)
    clock = [0.0]
    monkeypatch.setattr("noah.core.loop.time.monotonic", lambda: clock[0])
    ledger = Ledger()
    ledger.block = CLOSE - 1
    adapter = make_adapter(tmp_path, ledger)
    cache = {}
    last = [None, None]
    outcomes = []

    def step(height, now):
        ledger.block = height
        clock[0] = now
        result, last[0] = tick(
            adapter,
            settle_blocks=SETTLE,
            state_path=tmp_path / "loop.json",
            lock_path=tmp_path / "loop.lock",
            identity=f"{GENESIS}/2/{VALIDATOR}",
            last_attempted_height=last[0],
            last_attempted_tempo=last[1],
            refused_heights=set(),
            day_zero_cache={},
            day_catchup=cache,
        )
        last[1] = result.last_attempted_tempo
        outcomes.append(result)
        return result

    return SimpleNamespace(
        clock=clock,
        ledger=ledger,
        adapter=adapter,
        cache=cache,
        last=last,
        step=step,
        outcomes=outcomes,
        path=tmp_path,
    )


def receipt(path):
    return json.loads((path / "scores/day-0.submitted.json").read_text())


def test_closed_day_publishes_before_period_change_only_after_finalized_margin(scenario):
    s = scenario
    assert s.step(CLOSE - 1, 0).outcome == "refused"
    target = s.last[0]
    assert s.step(CLOSE, 10).acted is False
    assert s.step(CLOSE + SETTLE - 1, 310).acted is False
    assert not s.ledger.calls  # Even a finalized new-day block needs the margin.
    assert s.step(CLOSE + SETTLE, 610).outcome == "published"
    assert pin_height(head=s.ledger.block, tempo=TEMPO, settle_blocks=SETTLE) == target
    r = receipt(s.path)
    assert r["day"] == 0 and r["height"] == target
    assert r["submitted_weights_u16"] == r["applied_weights_readback"]["weights"]
    assert sum(r["submitted_weights_u16"].values()) == 65535
    assert s.cache["pending"] is None
    assert not (s.path / "intent.json").exists()
    assert s.step(CLOSE + SETTLE + 1, 920).acted is False
    assert len(s.ledger.calls) == 1


def test_early_and_period_scheduled_runs_have_identical_fixture_score_and_weights(
    scenario, tmp_path
):
    s = scenario
    assert s.step(CLOSE - 1, 0).outcome == "refused"
    assert s.step(CLOSE + SETTLE, 300).outcome == "published"
    early = receipt(s.path)
    early_manifest = json.loads((s.path / "scores/day-0.calculation.json").read_text())

    # Same data, remembered height and real pipeline; old period cadence.
    later = tmp_path / "later"
    later.mkdir()
    ledger = Ledger()
    ledger.block = CLOSE - 1
    adapter = make_adapter(later, ledger)
    adapter.closed_day_ready = lambda day: False
    run_forever(
        adapter,
        settle_blocks=SETTLE,
        state_path=later / "loop.json",
        lock_path=later / "loop.lock",
        iterations=2,
        sleep=lambda _: setattr(ledger, "block", 7200),
    )
    delayed = receipt(later)
    delayed_manifest = json.loads((later / "scores/day-0.calculation.json").read_text())
    assert early["height"] == delayed["height"]
    assert early["submitted_weights_u16"] == delayed["submitted_weights_u16"]
    for artifact in ("fixture", "agreed_score"):
        assert early_manifest["artifacts"][artifact] == delayed_manifest["artifacts"][artifact]
    assert (
        early_manifest["agreement"]
        == delayed_manifest["agreement"]
        == "python_typescript_identical"
    )


def test_pending_same_period_survives_restart_and_completed_day_is_not_resent(scenario):
    s = scenario
    s.step(CLOSE - 1, 0)
    assert read_state(s.path / "loop.json")["day_catchup_pending"]["day"] == 0
    s.ledger.block = CLOSE + SETTLE
    for _ in range(2):
        run_forever(
            make_adapter(s.path, s.ledger),
            settle_blocks=SETTLE,
            state_path=s.path / "loop.json",
            lock_path=s.path / "loop.lock",
            identity=f"{GENESIS}/2/{VALIDATOR}",
            iterations=1,
        )
    assert len(s.ledger.calls) == 1
    assert receipt(s.path)["applied_weights_readback"]["confirmed"]


def test_readiness_is_paced_and_read_failures_never_reach_pipeline(scenario, monkeypatch):
    s = scenario
    s.step(CLOSE - 1, 0)
    calls = []

    def fail(day):
        calls.append(day)
        raise SourceDisagreement("synthetic finalized-source disagreement")

    monkeypatch.setattr(s.adapter, "closed_day_ready", fail)
    result = s.step(CLOSE + SETTLE, 10)
    assert result.severity == "warning" and "SourceDisagreement" in result.detail
    for now in (11, 30, 100, 309):
        assert not s.step(CLOSE + SETTLE, now).acted
    assert calls == [0]
    assert not s.step(CLOSE + SETTLE, 310).acted
    assert calls == [0, 0]
    assert not s.ledger.calls and not (s.path / "intent.json").exists()


@pytest.mark.parametrize("failure", ["hash", "timestamp"])
def test_dual_source_readiness_disagreement_fails_closed(scenario, monkeypatch, failure):
    s = scenario
    s.step(CLOSE - 1, 0)
    source = s.adapter.runner_chain._sources["b"]
    if failure == "hash":
        monkeypatch.setattr(source, "block_hash", lambda h: "0x" + "b" * 64)
    else:
        monkeypatch.setattr(source, "block_timestamp", lambda h: ANCHOR + (h + 101) * 12)
    result = s.step(CLOSE + SETTLE, 300)
    assert result.severity == "warning"
    assert not s.ledger.calls


@pytest.mark.parametrize("timestamp", [0, -1, True, "1791936000", None])
def test_bad_timestamp_never_marks_day_ready(timestamp):
    adapter = SimpleNamespace(
        runner_chain=SimpleNamespace(
            head_block=lambda: 100,
            block_hash=lambda h: "hash",
            block_timestamp=lambda h: timestamp,
        ),
        settle_blocks=20,
        day_anchor_ts=ANCHOR,
    )
    with pytest.raises(ChainUnreadable):
        ChainAdapter.closed_day_ready(adapter, 0)


def test_backward_progress_and_remembered_refusal_are_not_bypassed(scenario, monkeypatch):
    s = scenario
    s.step(CLOSE - 1, 0)
    checks = []
    monkeypatch.setattr(s.adapter, "closed_day_ready", lambda day: checks.append(day) or True)
    result = s.step(CLOSE - TEMPO, 300)
    assert "EARLIER" in result.detail and not result.acted
    target = s.last[0]
    s.ledger.block = CLOSE + SETTLE
    result, _ = tick(
        s.adapter,
        settle_blocks=SETTLE,
        state_path=s.path / "loop.json",
        lock_path=s.path / "loop.lock",
        last_attempted_height=target,
        last_attempted_tempo=TEMPO,
        refused_heights={target},
        day_zero_cache={"resolved": True},
        day_catchup=s.cache,
    )
    assert result.severity == "error" and not result.acted
    assert checks == [] and not s.ledger.calls


def test_uncertain_send_stays_held_across_restart_until_exact_reconciliation(scenario):
    s = scenario
    s.step(CLOSE - 1, 0)

    def lose_reply(_):
        raise TimeoutError("synthetic lost response after inclusion")

    s.ledger.after_send = lose_reply
    assert s.step(CLOSE + SETTLE, 300).outcome == "day_catchup_abandoned"
    assert len(s.ledger.calls) == 1 and (s.path / "intent.json").exists()
    run_forever(
        make_adapter(s.path, s.ledger),
        settle_blocks=SETTLE,
        state_path=s.path / "loop.json",
        lock_path=s.path / "loop.lock",
        identity=f"{GENESIS}/2/{VALIDATOR}",
        iterations=1,
    )
    assert len(s.ledger.calls) == 1 and (s.path / "intent.json").exists()
    # Existing fail-closed policy is unchanged: reconcile the saved intent,
    # never clear it or blindly retry a possibly successful send.
    assert make_adapter(s.path, s.ledger).already_published(CLOSE - 1)
    assert len(s.ledger.calls) == 1 and not (s.path / "intent.json").exists()
    assert receipt(s.path)["applied_weights_readback"]["confirmed"]
