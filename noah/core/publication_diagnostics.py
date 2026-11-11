"""Bounded diagnostics: fixed labels and integers, never exception text or payloads."""

from contextlib import contextmanager

from ..chain.sources import Refusal

GUARD_STAGES = frozenset({
    "unknown", "publication_lock", "hold", "recover_pending", "read_head",
    "read_active", "verify_overlay", "read_epoch", "check_identity",
    "reconcile_active", "check_latest_update", "compute_fallback", "read_seats",
    "read_owners", "read_owner_uids", "retain_fallback", "publish_fallback",
    "record_health",
})
PUBLICATION_STAGES = frozenset({
    "rate_limit", "validate_vector", "submit", "confirm",
    "pre_send_evidence", "pre_send_overlay", "pre_send_guard",
})

PREPARATION_PREDICATES = frozenset({
    "identity", "runtime", "nonce", "era", "mortality", "birth_hash", "call_bytes",
})


@contextmanager
def publication_phase(name):
    """Annotate the original exception without changing its type or cause."""
    try:
        yield
    except (Exception, Refusal) as exc:
        try:
            if name in PUBLICATION_STAGES:
                exc._noah_publication_phase = name
        except Exception:
            pass
        raise


def guard_context(exc, stage, head):
    try:
        exc._noah_guard_stage = stage if stage in GUARD_STAGES else "unknown"
        exc._noah_guard_head = head
    except Exception:
        pass


def failure_details(exc):
    """Diagnostic construction must not replace the original refusal."""
    try:
        return _failure_details(exc)
    except (Exception, Refusal):
        return dict(error="UnclassifiedError", cause="UnclassifiedError",
                    reason="unclassified", stage="unknown")


def _failure_details(exc):
    """Follow a bounded explicit cause chain; unknown types get a fixed label."""
    from .publish import CommitRejected, NotOnChain, PublicationRefused
    from .publish_intent import PublicationStatusUnknown
    from .reader import ChainUnreadable
    from .settings import SettingsError
    from .startup import CannotPublishYet
    from .writer_lock import LockHeld

    kinds = (
        (CannotPublishYet, "CannotPublishYet", "rate_limited"),
        (LockHeld, "LockHeld", "publication_lock_held"),
        (NotOnChain, "NotOnChain", "publication_unconfirmed"),
        (PublicationStatusUnknown, "PublicationStatusUnknown", "publication_status_unknown"),
        (CommitRejected, "CommitRejected", "publication_rejected"),
        (PublicationRefused, "PublicationRefused", "publication_refused"),
        (ChainUnreadable, "ChainUnreadable", "chain_unreadable"),
        (SettingsError, "SettingsError", "settings_refused"),
        (Refusal, "Refusal", "chain_refused"),
        (TimeoutError, "TimeoutError", "read_timeout"),
        (OSError, "OSError", "io_error"),
        (ValueError, "ValueError", "invalid_evidence"),
        (KeyError, "KeyError", "invalid_evidence"),
        (TypeError, "TypeError", "invalid_evidence"),
    )

    def kind(error):
        return next(((label, reason) for cls, label, reason in kinds
                     if isinstance(error, cls)), ("UnclassifiedError", "unclassified"))

    def number(value):
        return type(value) is int and 0 <= value <= 2**64 - 1

    chain, seen = [], set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    stage = getattr(exc, "_noah_guard_stage", "unknown")
    result = dict(error=kind(exc)[0], cause=kind(chain[-1])[0],
                  reason=kind(exc)[1],
                  stage=stage if type(stage) is str and stage in GUARD_STAGES else "unknown")
    head = getattr(exc, "_noah_guard_head", None)
    if number(head):
        result["head"] = head
    for error in chain:
        phase = getattr(error, "_noah_publication_phase", None)
        if type(phase) is str and phase in PUBLICATION_STAGES:
            result["publication_stage"] = phase
        predicate = getattr(error, "_noah_preparation_predicate", None)
        if type(predicate) is str and predicate in PREPARATION_PREDICATES:
            result["preparation_predicate"] = predicate
            heads = getattr(error, "_noah_preparation_heads", None)
            if (type(heads) is dict and set(heads) == {"before", "prepared", "after"}
                    and all(number(value) for value in heads.values())):
                result["preparation_heads"] = dict(heads)
            refreshed = getattr(error, "_noah_reader_refresh", None)
            if type(refreshed) is str and refreshed in {"closed", "failed"}:
                result["reader_refresh"] = refreshed
        if isinstance(error, CannotPublishYet):
            result["reason"] = "rate_limited"
            values = {name: getattr(error, name, None) for name in
                      ("current_block", "last_update", "rate_limit", "remaining_blocks")}
            if all(number(v) for v in values.values()):
                result["cooldown"] = values
    return result
