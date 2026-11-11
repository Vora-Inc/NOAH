"""Actual retained proofs/Node and durable adapter paths; simulated chain only."""

import copy
import json
from types import SimpleNamespace as N
from unittest.mock import patch

import pytest

from noah.core.calculation_record import artifact, encoded, retain, verify
from noah.core.period import AccountedNotPublished
from noah.core.publish_intent import PublicationStatusUnknown
from noah.points import catchup, onboarding
from noah.points import daily_recovery as recovery
from noah.points.tests.test_ema_adapter import profile  # noqa: F401 - shared autouse fixture
from noah.points.tests.test_outage import setup
from noah.starter.allocation import preview
from noah.starter.publication import retain_overlay
from noah.starter.runtime import guard_tick, prepare_daily, require_guard


def retained(a, raw, day, *, contribution=True, canonical=True):
    raw = copy.deepcopy(raw)
    raw["fixture"]["evalDay"] = str(day)
    if day > 1:
        raw["snapshot"]["finalizedTimestamp"] = a.day_anchor_ts + (day + 1) * 86400 + 3600
        raw["snapshot"]["epochStartTimestamp"] = raw["snapshot"]["finalizedTimestamp"] - 1000
        raw["snapshot"]["miningPoolSample"]["endTimestamp"] = raw["snapshot"]["finalizedTimestamp"]
    for miner in raw["fixture"]["miners"]:
        for d in range(2, day + 1):
            miner["days"][str(d)] = copy.deepcopy(miner["days"]["1"])
            if d == 2 and contribution:
                miner["days"][str(d)]["noahDepositRao"] = "20000000000"
    if not contribution:
        for miner in raw["fixture"]["miners"]:
            miner["entryDepositRao"] = "0"
            for row in miner["days"].values():
                row["noahDepositRao"] = "0"
                row["foreignDepositNoahEquivRao"] = "0"
    if day > 1:
        for seat, miner in zip(raw["snapshot"]["seats"], raw["fixture"]["miners"]):
            seat["spendableEarnedRao"] = sum(
                int(d["paidRao"]) + int(d["payUnlockedRao"]) for d in miner["days"].values()
            )
        from noah.research.tests.test_starter_epoch import sync

        sync(raw)
    output = preview(raw)["ordinaryOutput"]
    first, last = (day - 1) * 100, day * 100 - 1
    context = dict(
        day=day,
        height=last,
        from_block=first,
        to_block=last,
        from_block_hash=f"0x{first:064x}",
        to_block_hash=f"0x{last:064x}",
        genesis_hash=a.runner_chain.genesis_hash(),
        netuid=a.netuid,
        ark_address=a.ark_coldkey,
        designated_validator_hotkey=a.designated_validator_hotkey,
        validator_hotkey=a.wallet_hotkey.ss58_address,
        publisher_uid=a.uid,
        scoring_dials=a.dials,
        scoring_mode=a.scoring_mode,
        points_policy=a.points_policy.manifest,
        day_start_utc_unix=a.day_anchor_ts + day * 86400,
        day_end_utc_unix=a.day_anchor_ts + (day + 1) * 86400,
    )
    ref = retain(
        a._submitted_record_dir,
        fixture=raw["fixture"],
        agreed_text=encoded(output).decode(),
        vector={int(k): v for k, v in output["weightsU16"].items()},
        context=context,
        canonical_day=canonical,
    )
    return raw, ref


def expired(tmp_path, monkeypatch):
    a, c, raw, old_source, now = setup(tmp_path, monkeypatch)
    raw, source = retained(a, raw, 1)
    overlay = retain_overlay(a._submitted_record_dir, source, raw)
    path = a._submitted_record_dir / "starter-active.json"
    active = json.loads(path.read_bytes())
    active["reference"] = overlay
    active["marker"]["calculation_evidence"] = overlay
    path.write_bytes(encoded(active))
    now[0] = a.day_anchor_ts + 4 * 86400 + 3600
    a.runner_chain.block_timestamp = lambda b: (
        now[0] if b == c.finalized_head else a.day_anchor_ts + (b // 100 + 1) * 86400
    )
    a.runner_chain.publication_state = lambda n, u, b, m=0: dict(
        weights=sorted([list(p) for p in c.stored]), last_update=c.last
    )
    return a, c, raw, source, now


def test_expired_overlay_waits_without_send_then_real_fresh_fallback(tmp_path, monkeypatch):
    a, c, raw, _, _ = expired(tmp_path, monkeypatch)
    calls = len(c.calls)
    active = (a._submitted_record_dir / "starter-active.json").read_bytes()
    health = guard_tick(a)
    assert health["status"] == recovery.STATUS
    assert recovery.validate_health(health, a._submitted_record_dir, a.points_policy.manifest)
    assert len(c.calls) == calls and a._publish_intent.load() is None
    with pytest.raises(PublicationStatusUnknown):
        require_guard(a)  # Not a generic healthy-guard exception.
    from noah.starter.health import inspect

    assert inspect(a._submitted_record_dir)["ok"] is False
    fresh_raw, fresh = retained(a, raw, 3)
    a._calculation_reference = fresh
    state = a._submitted_record_dir / "starter-state.json"
    state_before = state.read_bytes() if state.exists() else None
    with (
        patch("noah.core.epoch_work.collect", return_value={"collected_through": 560}),
        patch("noah.starter.runtime.current_input", return_value=fresh_raw),
    ):
        vector = prepare_daily(a)
    assert (state.read_bytes() if state.exists() else None) == state_before
    assert (a._submitted_record_dir / "starter-active.json").read_bytes() == active
    from noah.starter.publication import check_before_send, verify_overlay

    assert (
        verify_overlay(a._submitted_record_dir, a._calculation_reference)["mode"]
        == "ordinary-fallback"
    )
    assert verify_overlay(a._submitted_record_dir, a._calculation_reference)["source"] == fresh
    check_before_send(a, a._calculation_reference, vector)
    assert len(c.calls) == calls
    a._day_window = lambda h: (3, 200, 299)
    a.publish_direct(vector, 299)
    assert a._fresh_daily_recovery is None and a._publish_intent.load() is None
    daily = (a._submitted_record_dir / "day-3.submitted.json").read_bytes()
    assert json.loads(daily)["direct_intent"]["prepared_transaction"]
    assert guard_tick(a)["status"] == "ordinary"
    c.block = c.finalized_head = c.epoch = 920
    a.publish_direct(vector, 299, refresh_epoch=920, source_day=3)
    assert len(c.calls) == calls + 2
    assert (a._submitted_record_dir / "day-3.submitted.json").read_bytes() == daily


@pytest.mark.parametrize(
    "change", ["unknown_intent", "current_weights", "last_update", "policy", "active_bytes"]
)
def test_recovery_never_accepts_unresolved_or_changed_active(tmp_path, monkeypatch, change):
    a, c, raw, _, _ = expired(tmp_path, monkeypatch)
    before = len(c.calls)
    if change == "unknown_intent":
        monkeypatch.setattr(a._publish_intent, "load", lambda: {"intent_id": "uncertain"})
    elif change in ("current_weights", "last_update"):
        a.runner_chain.publication_state = lambda *args: dict(weights=[], last_update=999)
    elif change == "policy":
        a.points_policy.manifest = {**a.points_policy.manifest, "sha256": "different"}
    else:
        p = a._submitted_record_dir / "starter-active.json"
        value = json.loads(p.read_bytes())
        value["marker"]["vector"] = {"0": 65535}
        p.write_bytes(encoded(value))
    with pytest.raises(Exception):
        recovery.expired_active(a)
    assert len(c.calls) == before


def test_fresh_recovery_expiring_before_signing_refuses(tmp_path, monkeypatch):
    a, c, raw, _, now = expired(tmp_path, monkeypatch)
    fresh_raw, fresh = retained(a, raw, 3)
    a._calculation_reference = fresh
    with (
        patch("noah.core.epoch_work.collect", return_value={"collected_through": 560}),
        patch("noah.starter.runtime.current_input", return_value=fresh_raw),
    ):
        vector = prepare_daily(a)
    now[0] += 86400
    from noah.starter.publication import check_before_send

    with pytest.raises(PublicationStatusUnknown, match="fresh day changed"):
        check_before_send(a, a._calculation_reference, vector)
    assert a._publish_intent.load() is None


def test_actual_retained_stale_day_has_distinct_idempotent_disposition(tmp_path, monkeypatch):
    a, c, raw, _, now = expired(tmp_path, monkeypatch)
    raw, ref = retained(a, raw, 2)
    a._calculation_reference = ref
    before = len(c.calls)
    disposition, pending = a.account_stale_day(199)
    assert pending == {"day": 3, "height": 200}
    record = catchup.verify_record(a._submitted_record_dir, disposition)
    assert record["status"] == "accounted_not_published"
    assert not (a._submitted_record_dir / "day-2.submitted.json").exists()
    now[0] += 100
    assert a.account_stale_day(199) == (disposition, pending)
    assert len(c.calls) == before and a._publish_intent.load() is None
    catchup.validate_loop(a, [disposition], pending)
    with pytest.raises(PublicationStatusUnknown, match="omitted"):
        catchup.validate_loop(a, [], pending)
    catchup.validate_loop(a, [], {"day": 2, "height": 199})  # crash-before-loopwrite


@pytest.mark.parametrize(
    "change", ["intent", "hash", "next_day", "policy", "publisher", "write_failure"]
)
def test_accounting_refusals_never_send_or_advance(tmp_path, monkeypatch, change):
    a, c, raw, _, _ = expired(tmp_path, monkeypatch)
    _, ref = retained(a, raw, 2)
    a._calculation_reference = ref
    before = len(c.calls)
    if change == "intent":
        monkeypatch.setattr(a._publish_intent, "load", lambda: {"intent_id": "unknown"})
    elif change == "hash":
        a.runner_chain.block_hash = lambda b: "0x" + "f" * 64
    elif change == "next_day":
        a.runner_chain.block_timestamp = lambda b: a.day_anchor_ts + 4 * 86400
    elif change == "policy":
        a.points_policy.manifest = {"sha256": "wrong"}
    elif change == "publisher":
        a.uid += 1
    else:
        monkeypatch.setattr(
            catchup, "durable_write", lambda *a, **kw: (_ for _ in ()).throw(OSError("fsync"))
        )
    with pytest.raises(Exception):
        a.account_stale_day(199)
    assert not (a._submitted_record_dir / "day-2.accounted.json").exists()
    assert not (a._submitted_record_dir / "day-2.submitted.json").exists()
    assert len(c.calls) == before


def test_loop_disposition_write_precedes_memory_and_survives_restart(tmp_path, monkeypatch):
    from noah.core import loop

    ref = dict(path="day-2.accounted.json", day=2, sha256="a" * 64)
    error = AccountedNotPublished(ref, {"day": 3, "height": 200})
    memory = dict(pending={"day": 2, "height": 199}, checked_pinned=560, accounted=[])
    initial = copy.deepcopy(memory)
    args = dict(
        state_path=tmp_path / "loop.json",
        identity="test",
        refused_heights=set(),
        day_catchup=memory,
        last_attempted_height=199,
        last_attempted_tempo=10,
        tempo=10,
    )
    with patch.object(loop, "_write_state", side_effect=OSError("disk unavailable")):
        with pytest.raises(OSError):
            loop._accounted_result(error, **args)
    assert memory == initial
    result, terminal = loop._accounted_result(error, **args)
    assert result.outcome == "day_catchup_accounted" and terminal == 199
    saved = loop.read_state(args["state_path"])
    assert (
        saved["points_accounting"] == [ref] and saved["day_catchup_pending"] == error.next_pending
    )
    assert saved["phase"] != "done" and "result" not in saved
    with pytest.raises(PublicationStatusUnknown):
        catchup.validate_loop_references([ref], {"day": 2, "height": 199})


@pytest.mark.parametrize("rules", [None, onboarding.RULES])
@pytest.mark.parametrize(
    "d49,d50,expected",
    [
        (0, 100, (200, 100, 0, 1, 1, 0)),
        (50, 50, (200, 100, 0, 2, 2, 0)),
        (0, 0, (200, 0, -100, 0, 0, 2)),
    ],
)
def test_full_day50_keeps_unpublished49_financial_effects(rules, d49, d50, expected):
    from noah.points.engine import replay_state

    def row(deposit):
        return N(
            paid_rao=100,
            pay_unlocked_rao=0,
            noah_deposit_rao=deposit,
            foreign_deposit_noah_equiv_rao=0,
        )

    miner = N(first_activity_day=48, days={48: row(0), 49: row(d49), 50: row(d50)})
    state = replay_state(miner, 50, rules=rules)
    assert (
        state.earned,
        state.contributed,
        state.balance,
        state.qualified,
        state.streak,
        state.missed,
    ) == expected
    if d49 == 50:
        assert state.maturity == (10000 + 990000 // 7921 if rules else 100000 + 900000 // 29)
    del miner.days[49]
    with pytest.raises(KeyError):
        replay_state(miner, 50, rules=rules)


@pytest.mark.parametrize("restart", [False, True])
def test_fresh_recovery_lost_reply_reconciles_without_resending(tmp_path, monkeypatch, restart):
    from noah.core.publish import NotOnChain

    a, c, raw, _, _ = expired(tmp_path, monkeypatch)
    fresh_raw, fresh = retained(a, raw, 3)
    a._calculation_reference = fresh
    with (
        patch("noah.core.epoch_work.collect", return_value={"collected_through": 560}),
        patch("noah.starter.runtime.current_input", return_value=fresh_raw),
    ):
        vector = prepare_daily(a)
    a._day_window = lambda h: (3, 200, 299)
    c.lost_reply = True
    with pytest.raises(NotOnChain):
        a.publish_direct(vector, 299)
    marker = a._publish_intent.load()
    capability = copy.deepcopy(a._fresh_daily_recovery)
    assert marker["prepared_transaction"] and capability["intent_id"] == marker["intent_id"]
    calls = len(c.calls)
    with pytest.raises(PublicationStatusUnknown, match="unresolved"):
        prepare_daily(a)
    assert a._fresh_daily_recovery == capability and a._publish_intent.load() == marker
    if restart:
        # A new process has no attempt-local capability; durable exact-call
        # reconciliation is authoritative, not that in-memory capability.
        a._fresh_daily_recovery = None
    c.lost_reply = False
    assert a._already_published_direct(299)
    assert len(c.calls) == calls and a._publish_intent.load() is None
    assert a._fresh_daily_recovery is None
    assert guard_tick(a)["status"] == "ordinary"


def test_zero_fresh_fallback_is_not_publishable(tmp_path, monkeypatch):
    a, c, raw, _, _ = expired(tmp_path, monkeypatch)
    raw, source = retained(a, raw, 3, contribution=False)
    # A fresh full-day input with no contribution has an actual zero vector.
    with pytest.raises(PublicationStatusUnknown, match="empty starter/fallback vector"):
        retain_overlay(a._submitted_record_dir, source, raw, fallback=True)
    assert a._publish_intent.load() is None


def test_directory_fsync_failure_does_not_advance_accounting_memory(tmp_path, monkeypatch):
    from noah.core import loop

    ref = dict(path="day-2.accounted.json", day=2, sha256="a" * 64)
    error = AccountedNotPublished(ref, {"day": 3, "height": 200})
    memory = dict(pending={"day": 2, "height": 199}, checked_pinned=560, accounted=[])
    before = copy.deepcopy(memory)
    fsync = loop.os.fsync
    calls = []

    def fail_directory(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("directory flush unavailable")
        fsync(fd)

    args = dict(
        state_path=tmp_path / "loop.json",
        identity="test",
        refused_heights=set(),
        day_catchup=memory,
        last_attempted_height=199,
        last_attempted_tempo=10,
        tempo=10,
    )
    with patch.object(loop.os, "fsync", side_effect=fail_directory):
        with pytest.raises(OSError):
            loop._accounted_result(error, **args)
    assert memory == before
    # Atomic replacement may already be visible. Both same-process retry and
    # restart retain the exact next pending day, with no submitted outcome.
    assert loop.read_state(args["state_path"])["day_catchup_pending"] == error.next_pending
    loop._accounted_result(error, **args)
    assert memory["pending"] == error.next_pending and memory["accounted"] == [ref]


def test_recovery_reads_full_paired_historical_row_and_last_update():
    from noah.chain.agreeing import AgreeingChain
    from noah.chain.live import ChainUnavailable, LiveRunnerChain
    from noah.chain.sources import SourceDisagreement

    class Connection:
        def __init__(self):
            self.row = [[3, 40000], [1, 25535]]
            self.calls = []

        def get_block_hash(self, block):
            return "0x" + "ab" * 32

        def query(self, module, name, params, block_hash):
            self.calls.append((module, name, params, block_hash))
            return self.row if name == "Weights" else [0, 19]

    left, right = Connection(), Connection()
    chain = AgreeingChain(
        {
            "left": LiveRunnerChain(left, netuid=2, endpoint="wss://left.example"),
            "right": LiveRunnerChain(right, netuid=2, endpoint="wss://right.example"),
        },
        endpoints={"left": "wss://left.example", "right": "wss://right.example"},
    )
    assert chain.publication_state(2, 1, 20, 1) == dict(
        weights=[[1, 25535], [3, 40000]], last_update=19
    )
    for connection in (left, right):
        assert connection.calls == [
            ("SubtensorModule", "Weights", [4098, 1], "0x" + "ab" * 32),
            ("SubtensorModule", "LastUpdate", [2], "0x" + "ab" * 32),
        ]
    right.row = [[3, 40001], [1, 25534]]
    with pytest.raises(SourceDisagreement):
        chain.publication_state(2, 1, 20, 1)
    right.row = [[3, 40000], [3, 25535]]
    with pytest.raises(ChainUnavailable):
        chain.publication_state(2, 1, 20, 1)


def test_real_period_twins_retain_zero_stale_day_without_starter_or_send(tmp_path, monkeypatch):
    from noah.core.period import run_period

    a, c, raw, _, _ = expired(tmp_path, monkeypatch)
    raw, _ = retained(a, raw, 2, contribution=False, canonical=False)
    a._day_window = lambda height: (2, 100, 199)
    a.pinned_height = lambda: 199
    a.already_published = lambda height: False  # no day2 receipt in this fixture
    a.read = lambda height: raw["fixture"]  # actual collector already completed
    a.live_uids = lambda height: [3, 4]
    a.fixture_hotkeys = lambda height: {3: "hotkey-0", 4: "hotkey-1"}
    a.chain_hotkeys = lambda: {3: "hotkey-0", 4: "hotkey-1"}
    a.last_confirmed_ark_pin = "ark"
    before = len(c.calls)
    monkeypatch.setattr(
        a, "prepare_starter_vector", lambda *_: pytest.fail("stale day reached starter")
    )
    with pytest.raises(AccountedNotPublished) as first:
        run_period(a)  # real Python + Node, keeper, evidence and accounting hook
    ref = first.value.reference
    record = catchup.verify_record(a._submitted_record_dir, ref)
    graph = verify(a._submitted_record_dir, record["calculation_evidence"])
    assert not any(graph["publishable_weights_u16"].values())
    assert artifact(a._submitted_record_dir, graph, "context")["publisher_uid"] == a.uid
    # Crash after record fsync/before loop advance: full reretention is identical.
    with pytest.raises(AccountedNotPublished) as repeated:
        run_period(a)
    assert repeated.value.reference == ref
    assert len(c.calls) == before and a._publish_intent.load() is None
    assert not (a._submitted_record_dir / "day-2.submitted.json").exists()


@pytest.mark.parametrize("already_accounted", [False, True])
def test_confirmed_only_record_conflicts_with_accounting(tmp_path, monkeypatch, already_accounted):
    a, c, raw, _, _ = expired(tmp_path, monkeypatch)
    _, reference = retained(a, raw, 2)
    a._calculation_reference = reference
    disposition = a.account_stale_day(199)[0] if already_accounted else None
    (a._submitted_record_dir / "day-2.confirmed.json").write_text("{}")
    with pytest.raises(PublicationStatusUnknown):
        a.account_stale_day(199)
    if disposition:
        with pytest.raises(PublicationStatusUnknown):
            catchup.verify_record(a._submitted_record_dir, disposition)
    assert a._publish_intent.load() is None
