"""Severity is observable without changing publication or retry decisions."""

import logging
from unittest import mock

import pytest

from noah import diagnostics as D
from noah import runtime_events as E
from noah.chain.sources import Refusal
from noah.core.loop import TickResult, _tick_event_fields, run_forever
from noah.core.publish_intent import PublicationStatusUnknown
from noah.core.reader import ChainUnreadable
from noah.core.tests.test_loop import DayAwareChain, SchedulableChain


@pytest.fixture
def application_logger():
    logger = logging.getLogger("noah")
    saved = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = []
    yield logger
    logger.handlers, logger.level, logger.propagate = saved


@pytest.mark.parametrize("severity,priority", D.PRIORITIES.items())
def test_each_line_has_real_journal_priority(monkeypatch, capsys, severity, priority):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")
    D.write("first\nsecond", severity=severity)
    assert capsys.readouterr().out == f"<{priority}>first\n<{priority}>second\n"


def test_terminal_has_no_journal_protocol_prefix(monkeypatch, capsys):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    D.write("PUBLICATION STATUS UNKNOWN", severity="error")
    assert capsys.readouterr().out == "PUBLICATION STATUS UNKNOWN\n"


@pytest.mark.parametrize("outcome", sorted(D.ERROR_OUTCOMES - {"refused"}))
def test_terminal_failure_is_error_even_if_explicitly_info(outcome):
    result = TickResult(False, 1, outcome, "failure", severity="info")
    assert result.log_severity == "error"
    assert _tick_event_fields((result, None))["severity"] == "error"


def test_unclassified_refusal_and_failed_stage_remain_errors():
    assert TickResult(False, 1, "refused", "failure").log_severity == "error"
    assert D.severity_for(event="stage.failed", outcome="refused", severity="info") == "error"


def test_real_open_day_wait_is_info_without_changing_catchup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")
    chain = DayAwareChain(head=400, settle_blocks=1)
    chain.published_days.add(0)
    outcomes = []
    run_forever(
        chain,
        settle_blocks=1,
        state_path=tmp_path / "loop.json",
        lock_path=tmp_path / "loop.lock",
        iterations=1,
        on_tick=outcomes.append,
    )
    result = outcomes[0]
    assert result.outcome == "refused" and "DayNotComplete" in result.detail
    assert result.log_severity == "info"
    fields = _tick_event_fields((result, None))
    assert D.severity_for(
        event="stage.completed", outcome=fields["outcome"], severity=fields["severity"]
    ) == "info"
    assert "<6>[loop] refused:" in capsys.readouterr().out
    assert chain.publish_calls == []


@pytest.mark.parametrize(
    "error,severity,priority",
    [
        (PublicationStatusUnknown("PUBLICATION STATUS UNKNOWN"), "error", 3),
        (ChainUnreadable("temporary read failure"), "warning", 4),
    ],
)
def test_real_loop_read_failure_preserves_priority_and_does_not_send(
    tmp_path,
    monkeypatch,
    capsys,
    error,
    severity,
    priority,
):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")
    chain = SchedulableChain(head=25, tempo=20, settle_blocks=1)
    chain.already_published = mock.Mock(side_effect=error)
    outcomes = []
    run_forever(
        chain,
        settle_blocks=1,
        state_path=tmp_path / "loop.json",
        lock_path=tmp_path / "loop.lock",
        iterations=1,
        on_tick=outcomes.append,
    )
    assert outcomes[0].outcome == "waiting"
    assert outcomes[0].log_severity == severity
    assert capsys.readouterr().out.startswith(f"<{priority}>[loop] waiting:")
    assert chain.publish_calls == []


def test_expected_wait_stays_info(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")
    run_forever(
        SchedulableChain(head=0),
        settle_blocks=1,
        state_path=tmp_path / "loop.json",
        lock_path=tmp_path / "loop.lock",
        iterations=1,
    )
    assert capsys.readouterr().out.startswith("<6>[loop] waiting:")


def test_python_errors_and_tracebacks_have_priority_on_every_line(
    application_logger,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")
    D.configure("info")
    try:
        raise ValueError("composition failed")
    except ValueError:
        logging.getLogger("noah.core.submitter").exception("publication failed")
    lines = capsys.readouterr().err.splitlines()
    assert lines and all(line.startswith("<3>") for line in lines)


def test_debug_is_opt_in_and_stage_trace_omits_payload(application_logger, monkeypatch, capsys):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")
    monkeypatch.delenv("NOAH_EVENT_LOG", raising=False)
    D.configure("info")
    E.call("validator", "validator.scoring", lambda: "done")
    assert not capsys.readouterr().err
    D.configure("debug")
    E.call(
        "validator",
        "validator.scoring",
        lambda payload: payload,
        "secret-payload-sentinel",
        event_fields={"height": 144359, "endpoint": "secret-endpoint-sentinel"},
    )
    text = capsys.readouterr().err
    assert "<7>" in text and "144359" in text and "stage completed" in text
    assert "secret-payload-sentinel" not in text and "secret-endpoint-sentinel" not in text


def test_broken_log_output_never_breaks_business_decision(monkeypatch):
    with mock.patch("builtins.print", side_effect=OSError("closed journal")):
        assert D.write("failure", severity="error") is None


@pytest.mark.parametrize("error_type", [ValueError, Refusal])
def test_unhandled_entrypoint_failure_is_error_and_nonzero(monkeypatch, capsys, error_type):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")

    def fail():
        raise error_type("unexpected validator failure")

    assert D.run_entrypoint(fail) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines and all(line.startswith("<3>") for line in lines)
    assert any("unexpected validator failure" in line for line in lines)


def test_debug_handler_failure_does_not_change_stage_result(monkeypatch):
    with mock.patch.object(logging.Logger, "debug", side_effect=OSError("handler failed")):
        assert E.call("validator", "validator.scoring", lambda: 17) == 17
