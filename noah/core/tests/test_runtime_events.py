"""Application diagnostics must preserve business calls, results and exceptions."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from noah import runtime_events as E
from noah.core.period import PeriodRefused, run_period
from noah.core.tests.test_period import Recorder
from noah.deposits.assistant.tests.test_session import Rig


@pytest.fixture(autouse=True)
def isolated_events(monkeypatch):
    monkeypatch.delenv("NOAH_EVENT_LOG", raising=False)
    monkeypatch.setattr(E, "_sink", None)
    token = E._context.set(None)
    yield
    if E._sink:
        E._sink.flush(2)
    E._context.reset(token)


def enable(monkeypatch, path, failing=False):
    sink = E.EventSink(path.resolve())
    if failing:

        def refuse(record):
            raise OSError("sensitive exception sentinel")

        monkeypatch.setattr(sink, "_append", refuse)
    monkeypatch.setattr(E, "_sink", sink)
    return sink


def records(sink):
    assert sink.flush(2)
    return [json.loads(line) for line in sink.path.read_text().splitlines()]


def test_disabled_has_no_sink_or_file_work(monkeypatch):
    monkeypatch.setattr(E, "EventSink", lambda *_: pytest.fail("disabled event sink constructed"))
    expected = object()
    assert E.call("miner", "miner.preview", lambda: expected) is expected
    assert E._sink is None


@pytest.mark.parametrize("event,outcome,severity,expected", [
    ("stage.failed", "raised", "info", "error"),
    ("publication.state", "uncertain", None, "error"),
    ("stage.completed", "day_catchup_abandoned", None, "error"),
    ("stage.completed", "waiting", "error", "error"),
    ("stage.completed", "waiting", "warning", "warning"),
    ("stage.completed", "day_catchup_pending", None, "info"),
    ("publication.state", "finalized", "invalid", "info"),
])
def test_event_severity_is_explicit_and_validated(monkeypatch, tmp_path, event, outcome, severity, expected):
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    E.emit("validator", event, "validator.publication", outcome=outcome, severity=severity)
    assert records(sink)[0]["severity"] == expected


@pytest.mark.parametrize("mode", ["sealed", "direct"])
@pytest.mark.parametrize("failure", [None, "read", "score", "agree", "publish"])
def test_period_same_calls_vectors_results_and_refusals(monkeypatch, tmp_path, mode, failure):
    outcomes = []
    for sink_mode in ("disabled", "enabled", "failing"):
        monkeypatch.setattr(E, "_sink", None)
        sink = (
            None
            if sink_mode == "disabled"
            else enable(monkeypatch, tmp_path / (sink_mode + ".jsonl"), sink_mode == "failing")
        )
        settings = {"publish_mode": mode}
        if failure:
            key = "publish_direct" if failure == "publish" and mode == "direct" else failure
            settings[key] = ValueError("private-endpoint-and-wallet-sentinel")
        chain = Recorder(**settings)
        try:
            result = run_period(chain)
        except PeriodRefused as error:
            result = (type(error).__name__, str(error), type(error.__cause__).__name__)
        outcomes.append((result, chain.calls, chain.published))
        if sink:
            assert sink.flush(2)
            if sink_mode == "enabled":
                text = sink.path.read_text()
                assert "private-endpoint-and-wallet-sentinel" not in text
                rows = records(sink)
                assert all(row["public_finality_verified"] is False for row in rows)
                assert rows[0]["event"] == "stage.entered"
                if failure:
                    assert rows[-1]["event"] == "stage.failed"
                else:
                    assert rows[-1]["stage"] == "validator.publication"
    assert outcomes[0] == outcomes[1] == outcomes[2]


def test_miner_ledger_and_send_identical_for_disabled_enabled_failed_sink(monkeypatch, tmp_path):
    results = []
    for mode in ("disabled", "enabled", "failing"):
        root = tmp_path / mode
        root.mkdir()
        monkeypatch.setattr(E, "_sink", None)
        sink = (
            None
            if mode == "disabled"
            else enable(monkeypatch, root / "events.jsonl", mode == "failing")
        )
        rig = Rig(root)
        result = rig.run()
        [row] = rig.ledger.for_miner(rig.config.miner_hotkey)
        results.append(
            (
                result["status"],
                row["state"],
                row["amount_rao"],
                rig.signer_calls,
                rig.chain.submitted,
                rig.chain.sign_eras,
            )
        )
        rig.ledger.close()
        if sink:
            assert sink.flush(2)
            if mode == "enabled":
                rows = records(sink)
                contributions = [r for r in rows if r["event"] == "contribution.observed"]
                assert [r["outcome"] for r in contributions] == [
                    "approved",
                    "submitting",
                    "submitted",
                    "finalized",
                ]
                assert len({r["intent_id"] for r in contributions}) == 1
                assert all(r["public_finality_verified"] is False for r in contributions)
                assert rows[0]["stage"] == "miner.submission"
                assert rows[-1]["outcome"] == "finalized"
    assert results[0] == results[1] == results[2]


def test_exact_exception_identity_even_with_outcome_metadata(monkeypatch, tmp_path):
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    error = RuntimeError("never serialize this secret")

    def fail():
        raise error

    with pytest.raises(RuntimeError) as caught:
        E.call("validator", "validator.scoring", fail, event_fields={"outcome": "ready"})
    assert caught.value is error
    assert records(sink)[-1]["outcome"] == "raised"
    assert "secret" not in sink.path.read_text()


def test_projection_and_fresh_context_omit_secrets_and_guessed_identity(monkeypatch, tmp_path):
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    E.bind("miner", genesis_hash="0x" + "1" * 64, netuid=2)

    @E.traced("miner", "miner.preview", fresh=True)
    def run():
        E.emit(
            "miner",
            "stage.completed",
            "miner.preview",
            endpoint="SECRET",
            wallet="SECRET",
            outcome="SECRET",
            tx_hash="SECRET",
            netuid=True,
            height=2**53,
            genesis_hash=object(),
            intent_id="SECRET",
        )

    run()
    rows = records(sink)
    assert all("genesis_hash" not in r and "netuid" not in r for r in rows)
    assert "SECRET" not in sink.path.read_text()
    assert "height" not in rows[1]
    assert all(r["evidence"] == "application_event" for r in rows)


def test_concurrent_emit_has_unique_ordered_sequences(monkeypatch, tmp_path):
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    threads = [
        threading.Thread(
            target=lambda: [
                E.emit("validator", "stage.entered", "validator.tick") for _ in range(10)
            ]
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = records(sink)
    seq = [r["source_sequence"] for r in rows]
    assert seq == sorted(set(seq))
    assert len({r["event_id"] for r in rows}) == len(rows)
    assert len({r["run_id"] for r in rows}) == 1


def test_queue_full_does_not_wait_for_stalled_writer(monkeypatch, tmp_path):
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    entered, release = threading.Event(), threading.Event()
    captured = []

    def stalled(record):
        entered.set()
        release.wait(5)
        captured.append(record)

    monkeypatch.setattr(sink, "_append", stalled)
    E.emit("validator", "stage.entered", "validator.tick")
    assert entered.wait(1)
    try:
        start = time.monotonic()
        for _ in range(E.QUEUE_SIZE + 20):
            E.emit("validator", "stage.entered", "validator.tick")
        assert time.monotonic() - start < 1
        assert sink.pending.qsize() <= E.QUEUE_SIZE
        assert sink.dropped >= 20
    finally:
        release.set()
    assert sink.flush(2)
    E.emit("validator", "stage.entered", "validator.tick")
    assert sink.flush(2)
    rows = captured
    assert rows[-1]["source_dropped"] >= 20
    assert rows[-1]["source_sequence"] > len(rows)


def test_cap_symlink_and_partial_tail_refuse_without_mutation(monkeypatch, tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged\n")
    link = tmp_path / "link"
    link.symlink_to(sentinel)
    parent = tmp_path / "parent"
    parent.symlink_to(tmp_path, target_is_directory=True)
    partial = tmp_path / "partial"
    partial.write_text('{"unfinished":')
    for path in (link, parent / "sentinel", partial):
        sink = enable(monkeypatch, path if path == partial else Path(str(path)))
        # Preserve lexical path: the public writer must refuse symlink parents/files.
        sink.path = path
        E.emit("miner", "stage.entered", "miner.preview")
        assert sink.flush(2)
        assert sink.dropped == 1
    assert sentinel.read_text() == "unchanged\n"
    assert partial.read_text() == '{"unfinished":'
    monkeypatch.setattr(E, "MAX_BYTES", 16)
    sink = enable(monkeypatch, tmp_path / "capped")
    E.emit("miner", "stage.entered", "miner.preview")
    assert sink.flush(2)
    assert sink.path.stat().st_size == 0
    assert sink.dropped == 1


def test_restart_append_retains_records_and_changes_run_id(tmp_path):
    path = tmp_path.resolve() / "events.jsonl"
    code = (
        "from noah.runtime_events import emit; emit('validator','stage.entered','validator.tick')"
    )
    env = {**os.environ, "NOAH_EVENT_LOG": str(path)}
    for _ in range(2):
        subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=5)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["run_id"] != rows[1]["run_id"]
    assert [r["source_sequence"] for r in rows] == [1, 1]


def test_registration_and_receipt_use_observed_public_ids_without_credit_claim(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from noah.chain.ss58 import address_of

    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    hotkey = address_of(bytes([19]) * 32)
    E.bind("validator", genesis_hash="0x" + "a" * 64, netuid=82)
    E.observe_seats({"private-coldkey-is-not-emitted": {hotkey: 4}}, day=3, height=40)
    E.observe_receipt(
        SimpleNamespace(
            block=40,
            token_netuid=99,
            amount_rao=1000,
            origin_hotkey=hotkey,
            block_hash="0x" + "b" * 64,
            event_ordinal=7,
            extrinsic_index=2,
        )
    )
    rows = records(sink)
    assert rows[0]["seat_hotkey"] == hotkey and rows[0]["uid"] == 4
    assert rows[1]["miner_hotkey"] == hotkey and rows[1]["token_netuid"] == 99
    assert rows[1]["outcome"] == "received"
    assert rows[1]["event_ordinal"] == 7 and rows[1]["extrinsic_index"] == 2
    assert "private-coldkey" not in sink.path.read_text()
    E.emit(
        "validator",
        "registration.observed",
        "validator.ingestion",
        seat_hotkey=hotkey[:-1] + ("1" if hotkey[-1] != "1" else "2"),
    )
    assert "seat_hotkey" not in records(sink)[-1]


def test_tick_projection_excludes_detail_and_restores_context(monkeypatch, tmp_path):
    from noah.core.loop import TickResult, _tick_event_fields

    sink = enable(monkeypatch, tmp_path / "events.jsonl")

    @E.traced("validator", "validator.tick", result_fields=_tick_event_fields)
    def run():
        E.bind("validator", day=9)
        return TickResult(False, 4, "waiting", "SECRET"), None

    result = run()
    assert result[0].detail == "SECRET"
    rows = records(sink)
    assert rows[-1]["outcome"] == "waiting" and rows[-1]["period"] == 4
    assert rows[-1]["day"] == 9
    assert E._context.get() is None
    assert "SECRET" not in sink.path.read_text()


def test_realistic_200_seat_burst_retained_with_stage_boundaries(monkeypatch, tmp_path):
    from noah.chain.ss58 import address_of

    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    E.emit("validator", "stage.entered", "validator.ingestion")
    seats = {"unused-wallet": {address_of(i.to_bytes(32, "big")): i for i in range(200)}}
    E.observe_seats(seats, day=1, height=100)
    E.emit("validator", "stage.completed", "validator.ingestion")
    rows = records(sink)
    assert len(rows) == 202 and sink.dropped == 0
    assert [r["uid"] for r in rows[1:-1]] == list(range(200))
    assert [r["source_sequence"] for r in rows] == list(range(1, 203))


def test_result_projection_failure_does_not_change_return(monkeypatch, tmp_path):
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    result = object()

    def fail(_):
        raise RuntimeError("sensitive projection failure")

    assert E.call("validator", "validator.tick", lambda: result, result_fields=fail) is result
    assert records(sink)[-1]["outcome"] == "returned"
    assert "sensitive" not in sink.path.read_text()


def test_rotation_retains_bounded_complete_files_and_continues_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "MAX_BYTES", 1300)
    monkeypatch.setattr(E, "BACKUPS", 2)
    sink = enable(monkeypatch, tmp_path / "rotating.jsonl")
    for _ in range(30):
        E.emit("validator", "stage.entered", "validator.tick")
    assert sink.flush(2)
    paths = [sink.path.with_name(sink.path.name + suffix) for suffix in (".2", ".1", "")]
    rows = []
    for path in paths:
        assert path.stat().st_size <= 1300
        assert path.read_bytes().endswith(b"\n")
        rows.extend(json.loads(line) for line in path.read_text().splitlines())
    assert [r["source_sequence"] for r in rows] == sorted(r["source_sequence"] for r in rows)
    assert rows[-1]["source_sequence"] == 30
    assert not sink.path.with_name(sink.path.name + ".3").exists()
    health = sink.health()
    assert health["source_written"] == 30 and health["source_dropped"] == 0
    assert health["rotations"] > 0 and int(health["free_bytes_decimal"]) > 0


def test_terminal_drop_is_visible_without_a_later_successful_event(monkeypatch, tmp_path):
    sink = enable(monkeypatch, tmp_path / "events.jsonl", failing=True)
    E.emit("validator", "stage.entered", "validator.tick")
    assert sink.flush(2)
    health_path = sink.path.with_name(sink.path.name + ".health.json")
    health = json.loads(health_path.read_text())
    assert health["source_dropped"] == 1 and health["status"] == "degraded"
    assert health["last_error"] == "write_failed"
    assert "sensitive exception sentinel" not in health_path.read_text()
    assert E.sink_health()["losses_detected"] is True


def test_health_refreshes_while_idle_and_reports_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "HEALTH_INTERVAL", 0)
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    with monkeypatch.context() as patch:
        patch.setattr(sink, "_append", lambda r: (_ for _ in ()).throw(OSError("SECRET")))
        E.emit("validator", "stage.entered", "validator.tick")
        assert sink.flush(2)
    E.emit("validator", "stage.entered", "validator.tick")
    assert sink.flush(2)
    health_path = sink.path.with_name(sink.path.name + ".health.json")
    first = json.loads(health_path.read_text())
    assert first["status"] == "ready" and first["losses_detected"] is True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        latest = json.loads(health_path.read_text())
        if latest["recorded_at"] != first["recorded_at"]:
            break
        time.sleep(0.02)
    assert latest["recorded_at"] != first["recorded_at"]
    assert latest["source_dropped"] == 1 and latest["source_written"] == 1


def test_rotation_refuses_symlink_archive_without_touching_target(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "MAX_BYTES", 700)
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    E.emit("validator", "stage.entered", "validator.tick")
    assert sink.flush(2)
    original = sink.path.read_bytes()
    target = tmp_path / "unrelated"
    target.write_text("untouched")
    sink.path.with_name(sink.path.name + ".1").symlink_to(target)
    E.emit("validator", "stage.entered", "validator.tick")
    assert sink.flush(2)
    assert target.read_text() == "untouched" and sink.path.read_bytes() == original
    assert sink.dropped == 1


def test_large_money_uses_exact_decimal_text_and_no_unsafe_js_number(monkeypatch, tmp_path):
    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    amount = 2**100 + 17
    E.emit("validator", "contribution.observed", "validator.ingestion", amount_rao=amount)
    [row] = records(sink)
    assert row["amount_rao_decimal"] == str(amount)
    assert "amount_rao" not in row


def test_direct_events_distinguish_uncertainty_and_reconciled_finality(monkeypatch, tmp_path):
    from noah.core.publish import NotOnChain
    from noah.core.tests.test_direct_runtime475 import VECTOR, adapter

    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    built, client = adapter(tmp_path)
    client.after_send = lambda c: (_ for _ in ()).throw(TimeoutError("SECRET"))
    with pytest.raises(NotOnChain):
        built.publish_direct(VECTOR, 99)
    assert adapter(tmp_path, client)[0].already_published(99)
    rows = [r for r in records(sink) if r["event"] == "publication.state"]
    assert [r["outcome"] for r in rows] == ["submitting", "uncertain", "reconciled"]
    assert len({r["intent_id"] for r in rows}) == 1
    assert rows[-1]["height"] == 101 and rows[-1]["extrinsic_index"] == 2
    assert all(r["public_finality_verified"] is False for r in rows)
    assert len(client.calls) == 1 and "SECRET" not in sink.path.read_text()


def test_tick_wait_is_fresh_chain_identified_and_not_a_prior_day_success(monkeypatch, tmp_path):
    from noah.core.loop import tick
    from noah.core.tests.test_loop import SchedulableChain
    from noah.core.tests.test_proxy_route import VALIDATOR

    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    E.bind("validator", day=99, artifact_sha256="f" * 64, intent_id="a" * 32)
    chain = SchedulableChain(head=0, tempo=10)
    for _ in range(2):
        result, _ = tick(
            chain,
            settle_blocks=20,
            state_path=tmp_path / "state",
            lock_path=tmp_path / "lock",
            identity=f"{'0x' + 'b' * 64}/82/{VALIDATOR}",
        )
        assert result.outcome == "waiting" and not result.acted
    waits = [r for r in records(sink) if r["event"] == "eligibility.state"]
    assert len(waits) == 2 and waits[0]["attempt_id"] != waits[1]["attempt_id"]
    assert all(r["genesis_hash"] == "0x" + "b" * 64 and r["netuid"] == 82 for r in waits)
    assert all(r["reason_code"] == "not_settled" and r["settle_blocks"] == 20 for r in waits)
    assert all(not {"day", "artifact_sha256", "intent_id"} & r.keys() for r in waits)
    assert not chain.publish_calls


def test_history_progress_resume_and_refusal_preserve_reader_calls(monkeypatch, tmp_path):
    from noah.chain.runner import RunRefused
    from noah.chain.tests.test_runner import Chain, a_store, run

    observations = []
    for mode in ("disabled", "enabled", "failed"):
        folder = tmp_path / mode
        folder.mkdir()
        monkeypatch.setattr(E, "_sink", None)
        sink = (
            None
            if mode == "disabled"
            else enable(monkeypatch, folder / "events.jsonl", mode == "failed")
        )
        E.bind("validator", day=0)
        store = a_store(folder)
        chain = Chain()
        run(store, chain, last=8_800_256, holding_stride=None)
        read_calls = list(chain.asked)
        run(store, chain, last=8_800_256, holding_stride=None)
        assert chain.asked == read_calls  # completed-history reuse adds no reads
        with pytest.raises(RunRefused):
            run(store, chain, first=4, last=3)
        observations.append((read_calls, store.day_was_scanned(0)))
        store.close()
        if sink:
            assert sink.flush(2)
        if mode == "enabled":
            rows = [r for r in records(sink) if r["event"] == "history.state"]
            assert [r["outcome"] for r in rows] == [
                "scanning",
                "scanning",
                "observed",
                "scanning",
                "already_scanned",
                "refused",
            ]
            assert rows[1]["height"] == 8_800_255
            assert all(r["public_finality_verified"] is False for r in rows)
    assert observations[0] == observations[1] == observations[2]


def test_attribution_and_entry_qualification_are_separate_safe_observations(monkeypatch, tmp_path):
    from noah.chain.deposits import Deposit
    from noah.chain.ss58 import address_of

    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    coldkey, hotkey, ark = [address_of(bytes([n]) * 32) for n in (21, 22, 23)]
    E.bind("validator", day=3, ark_address=ark)
    common = dict(
        coldkey=coldkey,
        amount_rao=2**63 + 3,
        token_netuid=82,
        block=40,
        event_ordinal=7,
        extrinsic_index=2,
        block_hash="0x" + "b" * 64,
        carried_by="v2",
    )
    E.observe_receipt(Deposit(**common, origin_hotkey=hotkey, owner_at_deposit=coldkey))
    E.observe_receipt(Deposit(**common, unattributed_reason="SECRET keyed URL and wallet"))
    E.observe_entry_qualification(
        {coldkey: {hotkey: 4}},
        [{"coldkey": coldkey, "hotkey": hotkey, "uid": 5}],
        {coldkey: {hotkey: 100}},
        {coldkey: {hotkey: 2}},
    )
    rows = records(sink)
    attribution = [r for r in rows if r["event"] == "deposit.attribution"]
    assert [r["outcome"] for r in attribution] == ["attributed", "unqualified"]
    assert attribution[0]["amount_rao_decimal"] == str(2**63 + 3)
    assert attribution[0]["origin_coldkey"] == coldkey and attribution[0]["ark_address"] == ark
    assert attribution[1]["reason_code"] == "attribution_unavailable"
    entries = [r for r in rows if r["event"] == "entry.qualification"]
    assert [(r["uid"], r["outcome"]) for r in entries] == [(4, "qualified"), (5, "unqualified")]
    assert entries[0]["entry_day"] == 2 and entries[0]["amount_rao"] == 100
    assert "SECRET" not in sink.path.read_text()


def test_preparation_after_real_calculation_links_final_vector_without_sending(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from noah.core.calculation_record import verify
    from noah.core.tests.test_calculation_record import FIXTURE
    from noah.core.tests.test_direct_runtime475 import adapter

    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    built, client = adapter(tmp_path)
    fixture = json.loads(FIXTURE.read_text())
    agreed = built.agree(fixture)
    built._day_window = lambda height: (10, 1, height)
    built.day_anchor_ts = 0
    built.runner_chain = SimpleNamespace(
        genesis_hash=lambda: "0x" + "b" * 64, block_hash=lambda height: f"0x{height:064x}"
    )
    vector = {uid: 0 for uid in agreed}
    reference = built.record_calculation(99, fixture, agreed, vector)
    rows = records(sink)
    prepared = [r for r in rows if r["event"] == "publication.prepared"]
    assert len(prepared) == 1 and prepared[0]["route"] == "proxy"
    assert prepared[0]["artifact_sha256"] == reference["sha256"]
    assert prepared[0]["changed_uid_count"] == sum(v != 0 for v in agreed.values())
    assert verify(tmp_path / "scores", reference)["publishable_weights_u16"] == {
        str(k): v for k, v in vector.items()
    }
    assert client.calls == []


def test_epoch_and_accounting_observations_do_not_claim_publication_or_zero_on_unavailable(
    monkeypatch, tmp_path
):
    from noah.chain.collateral import SettleEffect
    from noah.chain.pay import EpochAttribution
    from noah.chain.ss58 import address_of

    sink = enable(monkeypatch, tmp_path / "events.jsonl")
    coldkey, hotkey = [address_of(bytes([n]) * 32) for n in (24, 25)]
    E.bind("validator", day=3, artifact_sha256="a" * 64, intent_id="b" * 32)
    amount = 2**63 + 7
    E.observe_epoch(
        EpochAttribution(40, ((4, hotkey, coldkey, amount),), ((5, hotkey, coldkey, 17),)),
        "0x" + "b" * 64,
    )
    E.observe_accounting(coldkey, 40, "0x" + "b" * 64, SettleEffect(amount, 0))
    E.observe_accounting(coldkey, 41, "0x" + "c" * 64)
    rows = records(sink)
    assert [r["outcome"] for r in rows] == ["observed", "withheld", "observed", "unavailable"]
    assert rows[0]["amount_rao_decimal"] == str(amount)
    assert rows[2]["locked_arrival_rao_decimal"] == str(amount) and rows[2]["unlocked_rao"] == 0
    assert "locked_arrival_rao" not in rows[3] and "unlocked_rao" not in rows[3]
    assert all("artifact_sha256" not in r and r["public_finality_verified"] is False for r in rows)
    assert all(r["scan_day"] == 3 and "day" not in r and "intent_id" not in r for r in rows)
