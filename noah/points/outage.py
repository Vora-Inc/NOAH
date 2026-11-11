"""Bounded ordinary-weight refresh during a verified read/pricing failure.

The deadline is derived from the saved DAILY source, never from retry/restart
wall time. The existing starter fallback proof, identity checks, intent and
receipt paths remain in force. No balances are changed here.
"""

import json
from pathlib import Path

from ..core.calculation_record import durable_write, encoded, verify
from ..core.publish_intent import PublicationStatusUnknown
from ..core.submitted_record import submitted_record_path
from ..diagnostics import write as diagnostic

SECONDS = 86400


def record_failure(adapter, error):
    if not getattr(adapter, "points_policy", None):
        return
    from ..chain.foreign_value import ForeignDepositUnpriced
    from ..chain.sources import SourceDisagreement, SourceLied, Unverified
    from ..core.reader import ChainUnreadable

    path = Path(adapter._submitted_record_dir) / "points-outage.json"

    def block():
        durable_write(
            path,
            encoded(
                dict(
                    schema="noah-points-outage/1",
                    policy=adapter.points_policy.manifest,
                    reason="UnsafeFailure",
                )
            ),
            replace=True,
        )

    seen = set()
    causes = [error]
    recognized = None
    while causes:
        cause = causes.pop()
        if cause is None or id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(
            cause, (PublicationStatusUnknown, SourceDisagreement, SourceLied, Unverified)
        ):
            block()
            return False
        if isinstance(cause, (ForeignDepositUnpriced, ChainUnreadable)):
            recognized = cause
        causes.extend((cause.__cause__, cause.__context__))
    if recognized is not None and isinstance(error, (ForeignDepositUnpriced, ChainUnreadable)):
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get("reason") == "UnsafeFailure":
                return False  # a later partial read cannot resolve a contradiction
        durable_write(
            Path(adapter._submitted_record_dir) / "points-outage.json",
            encoded(
                dict(
                    schema="noah-points-outage/1",
                    policy=adapter.points_policy.manifest,
                    reason=(
                        "ForeignDepositUnpriced"
                        if isinstance(recognized, ForeignDepositUnpriced)
                        else "ChainUnreadable"
                    ),
                )
            ),
            replace=True,
        )
        diagnostic(
            "[points] verified data unavailable; daily retry continues; ordinary refresh "
            "limited to 24 hours after missed publication",
            severity="error",
        )
        return True
    block()  # policy/programming/identity refusals revoke an earlier availability permit


def bounds(adapter, source_day):
    due = adapter.day_anchor_ts + (source_day + 2) * SECONDS
    return due, due + SECONDS


def authorize(adapter, source, now):
    manifest = verify(adapter._submitted_record_dir, source)
    from ..core.calculation_record import artifact

    context = artifact(adapter._submitted_record_dir, manifest, "context")
    if context.get("points_policy") != adapter.points_policy.manifest:
        raise PublicationStatusUnknown("outage source uses another points policy")
    if (
        context.get("ark_address") != adapter.ark_coldkey
        or context.get("designated_validator_hotkey") != adapter.designated_validator_hotkey
    ):
        raise PublicationStatusUnknown("outage destination changed")
    due, expiry = bounds(adapter, manifest["day"])
    if now < due:
        return  # today's daily score, not an outage refresh
    if now >= expiry:
        raise PublicationStatusUnknown("points outage refresh expired after 24 hours")
    path = Path(adapter._submitted_record_dir) / "points-outage.json"
    try:
        data = json.loads(path.read_text())
        if (
            data.get("schema") != "noah-points-outage/1"
            or data.get("policy") != adapter.points_policy.manifest
            or data.get("reason") not in ("ForeignDepositUnpriced", "ChainUnreadable")
        ):
            raise ValueError("not a matching availability failure")
    except (OSError, ValueError, TypeError) as exc:
        raise PublicationStatusUnknown("no verified points data outage") from exc
    return expiry


def republish(adapter, epoch, head, *, before_send):
    """Called inside the existing publication lock, after pending-send recovery."""
    from ..core.direct import reconcile
    from ..starter.publication import retain_overlay, verify_overlay

    root = Path(adapter._submitted_record_dir)
    now = adapter.runner_chain.block_timestamp(head)
    latest = (now - adapter.day_anchor_ts) // SECONDS - 1
    path = submitted_record_path(root, latest - 1)
    if not path.exists():
        return "awaiting_daily_publication"
    marker = json.loads(path.read_text())["direct_intent"]
    if marker.get("day") != latest - 1 or marker.get("refresh_epoch") is not None:
        raise PublicationStatusUnknown("outage source is not the prior daily publication")
    adapter._check_direct_identity(marker)
    reconcile(adapter.reader, marker, finalized_chain=adapter.runner_chain)
    reference = marker["calculation_evidence"]
    proof = verify_overlay(root, reference)
    if proof is None:
        active_path = root / "starter-active.json"
        active = json.loads(active_path.read_text()) if active_path.exists() else {}
        proof = verify_overlay(root, active["reference"]) if active.get("reference") else None
        if proof is None or proof["source"] != reference:
            raise PublicationStatusUnknown("points outage requires retained daily starter proof")
    expiry = authorize(adapter, proof["source"], now)
    last = adapter.last_update(adapter.uid, adapter.netuid)
    if last >= epoch:
        return "already_published_this_epoch"
    if head - last < adapter.rate_limit(adapter.netuid):
        return "awaiting_rate_limit"
    ref = retain_overlay(root, proof["source"], proof["input"], proof["previous"], fallback=True)
    vector = verify(root, ref)["publishable_weights_u16"]
    adapter._calculation_reference = ref
    diagnostic(
        f"[points] outage fallback: ordinary daily weights only; deadline UTC unix {expiry}; "
        "daily retry remains required",
        severity="error",
    )
    before_send()
    adapter.publish_direct(
        {int(k): v for k, v in vector.items()},
        marker["height"],
        refresh_epoch=epoch,
        source_day=marker["day"],
    )
    return "points_outage_refreshed"


def check_before_send(adapter, proof):
    if not getattr(adapter, "points_policy", None) or proof is None:
        return
    now = adapter.runner_chain.block_timestamp(adapter.runner_chain.head_block())
    expiry = authorize(adapter, proof["source"], now)
    if expiry is not None and proof["mode"] != "ordinary-fallback":
        raise PublicationStatusUnknown("data outage cannot renew starter allocations")


def record_recovery(adapter):
    if not getattr(adapter, "points_policy", None):
        return
    path = Path(adapter._submitted_record_dir) / "points-outage.json"
    if path.exists():
        durable_write(
            path,
            encoded(
                dict(
                    schema="noah-points-outage/1",
                    policy=adapter.points_policy.manifest,
                    reason="recovered",
                )
            ),
            replace=True,
        )


def republish_plain(adapter, epoch, head, *, before_send):
    """Same 24-hour policy, using an ordinary retained vector, with no pool proof."""
    from ..core.calculation_record import verify
    from .mode import check_publication_mode

    now = adapter.runner_chain.block_timestamp(head)
    latest = (now - adapter.day_anchor_ts) // SECONDS - 1
    path = submitted_record_path(adapter._submitted_record_dir, latest - 1)
    if not path.exists():
        return "awaiting_daily_publication"
    marker = json.loads(path.read_text())["direct_intent"]
    if marker.get("day") != latest - 1 or marker.get("refresh_epoch") is not None:
        raise PublicationStatusUnknown("outage source is not the prior daily publication")
    adapter._check_direct_identity(marker)
    reference = marker["calculation_evidence"]
    check_publication_mode(adapter, reference)
    authorize(adapter, reference, now)
    manifest = verify(adapter._submitted_record_dir, reference)
    if manifest["publishable_weights_u16"] != marker["vector"]:
        raise PublicationStatusUnknown("outage vector differs from retained calculation")
    from ..core.direct import reconcile

    reconcile(adapter.reader, marker, finalized_chain=adapter.runner_chain)
    if adapter.last_update(adapter.uid, adapter.netuid) >= epoch:
        return "already_published_this_epoch"
    if head - adapter.last_update(adapter.uid, adapter.netuid) < adapter.rate_limit(adapter.netuid):
        return "awaiting_rate_limit"
    adapter._calculation_reference = reference
    before_send()
    from .publication import check_before_send
    check_before_send(adapter, reference, {int(k): v for k, v in marker["vector"].items()})
    adapter.publish_direct(
        {int(k): v for k, v in marker["vector"].items()},
        marker["height"],
        refresh_epoch=epoch,
        source_day=marker["day"],
    )
    return "points_outage_refreshed"
