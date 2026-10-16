"""Application severity for terminals and systemd's journal stream protocol.

Keep transport debug logging disabled; these diagnostics use existing scrubbed
streams. Journal prefixes apply to every line, including exception continuations.
"""

import logging
import os
import sys

PRIORITIES = {"debug": 7, "info": 6, "warning": 4, "error": 3, "critical": 2}
ERROR_OUTCOMES = frozenset(
    {
        "refused",
        "rejected",
        "uncertain",
        "failed",
        "abandoned",
        "raised",
        "day_catchup_abandoned",
        "day_zero_catch_up_refused",
    }
)


def severity_for(*, event=None, outcome=None, severity=None):
    # The loop retains its historical "refused" outcome for an open day.
    # Only that typed DayNotComplete branch explicitly marks the wait as info;
    # actual failed stages and all unclassified refusals remain errors.
    if event != "stage.failed" and outcome == "refused" and severity == "info":
        return "info"
    if event == "stage.failed" or outcome in ERROR_OUTCOMES:
        return "error"
    if severity in PRIORITIES:
        return severity
    return "info"


def write(message, *, severity="info", file=None):
    """Print an application diagnostic; logging failure cannot change a decision."""
    try:
        from .secretscrub import scrub

        priority = PRIORITIES[severity]
        prefix = f"<{priority}>" if os.environ.get("JOURNAL_STREAM") else ""
        stream = sys.stdout if file is None else file
        text = scrub(str(message))
        for line in text.split("\n"):
            print(prefix + line, file=stream, flush=True)
    except Exception:
        pass


class _Handler(logging.Handler):
    def emit(self, record):
        try:
            write(self.format(record), severity=record.levelname.lower(), file=sys.stderr)
        except Exception:
            pass


def configure(level):
    """Configure only Noah application loggers, without enabling SDK/RPC chatter."""
    logger = logging.getLogger("noah")
    logger.setLevel(level.upper())
    if not any(isinstance(handler, _Handler) for handler in logger.handlers):
        handler = _Handler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


def debug(message, *args, logger="noah"):
    """Optional diagnostics must never interrupt a business operation."""
    try:
        logging.getLogger(logger).debug(message, *args)
    except Exception:
        pass


def run_entrypoint(main):
    """Give unexpected validator/manual-run failures journal error severity too."""
    from .chain.sources import Refusal

    try:
        return main()
    except (Exception, Refusal):
        import traceback

        write(traceback.format_exc(), severity="error", file=sys.stderr)
        return 1
