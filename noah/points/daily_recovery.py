"""Fresh ordinary replacement of a proven expired points starter overlay.

Expiry remains fixed. Waiting permits normal accounting to start, never another
send of expired weights or an unknown transaction retry.
"""

import json
import re
from pathlib import Path

from ..core.calculation_record import artifact, digest, verify
from ..core.direct import reconcile
from .catchup import MODES, natural, require
from .mode import check_publication_mode
from .onboarding import MODE as NO_POOL
from .outage import bounds

STATUS = "awaiting_fresh_daily"
SCHEMA = "noah-points-fresh-daily-recovery/1"


def validate_health(record, directory, policy_manifest):
    """Pure retained-byte/shape check for startup; no RPC, Node or file writes."""
    root = Path(directory)
    require(
        record.get("status") == STATUS and type(record.get("recovery")) is dict,
        "recovery health status",
    )
    proof = record["recovery"]
    require(
        set(proof)
        == {
            "schema",
            "active_sha256",
            "source",
            "source_day",
            "expires_at",
            "checked_head",
            "checked_hash",
            "checked_timestamp",
            "latest_closed_day",
            "active_block",
            "last_update",
        }
        and proof["schema"] == SCHEMA,
        "recovery health fields",
    )
    require(
        all(
            natural(proof[k])
            for k in (
                "source_day",
                "expires_at",
                "checked_head",
                "checked_timestamp",
                "latest_closed_day",
                "active_block",
                "last_update",
            )
        ),
        "recovery health numbers",
    )
    raw = (root / "starter-active.json").read_bytes()
    require(digest(raw) == proof["active_sha256"], "active publication changed")
    saved = json.loads(raw)
    manifest = verify(root, saved["reference"])
    context = artifact(root, manifest, "context")
    overlay = context.get("starter_overlay", {})
    require(
        overlay.get("mode") == "starter-overlay" and overlay.get("source") == proof["source"],
        "expired starter overlay required",
    )
    source = verify(root, proof["source"])
    original = artifact(root, source, "context")
    require(
        context.get("scoring_mode") in MODES
        and context["scoring_mode"] != NO_POOL
        and original.get("points_policy") == context.get("points_policy") == policy_manifest,
        "same pool points policy required",
    )
    identity = record.get("identity")
    require(
        type(identity) is list
        and len(identity) == 5
        and identity[4] == policy_manifest
        and identity[:2] == [source["genesis_hash"], source["netuid"]],
        "guard policy identity",
    )
    marker = saved["marker"]
    require(
        marker["calculation_evidence"] == saved["reference"]
        and marker["vector"] == manifest["publishable_weights_u16"]
        and marker["hotkey"] == identity[2]
        and marker["netuid"] == identity[1]
        and marker["day"] == manifest["day"] == source["day"] == proof["source_day"]
        and marker["result"]["block"] == proof["active_block"] == proof["last_update"],
        "active receipt identity",
    )
    start = original["day_start_utc_unix"]
    require(
        natural(start)
        and proof["expires_at"] == start + 3 * 86400
        and proof["checked_timestamp"] >= proof["expires_at"]
        and record["head"] == proof["checked_head"] >= proof["active_block"]
        and proof["latest_closed_day"]
        == (proof["checked_timestamp"] - (start - source["day"] * 86400)) // 86400 - 1,
        "fixed expiry and finalized day",
    )
    require(
        type(proof["checked_hash"]) is str
        and re.fullmatch(r"0x[0-9a-f]{64}", proof["checked_hash"]),
        "finalized hash",
    )
    return True


def expired_active(adapter, *, expected_sha256=None, own_unsigned_intent=None):
    """Return a verified recovery observation, or None if expiry is not the cause."""
    if getattr(adapter, "scoring_mode", "legacy") not in MODES or adapter.scoring_mode == NO_POOL:
        return None
    root = Path(adapter._submitted_record_dir)
    if not (root / "starter-active.json").exists():
        return None
    require(
        adapter.publish_mode == "direct" and adapter._publish_intent is not None,
        "unsupported recovery route",
    )
    pending = adapter._publish_intent.load()
    require(
        pending is None
        or (
            own_unsigned_intent is not None
            and pending.get("intent_id") == own_unsigned_intent["intent_id"]
            and pending.get("calculation_evidence") == own_unsigned_intent["reference"]
            and "prepared_transaction" not in pending
        ),
        "unresolved recovery send",
    )
    if adapter._publish_hold is not None:
        adapter._publish_hold.raise_if_held()
    raw = (root / "starter-active.json").read_bytes()
    if expected_sha256 is not None:
        require(digest(raw) == expected_sha256, "active changed before fresh replacement")
    saved = json.loads(raw)
    from ..starter.publication import verify_overlay

    overlay = verify_overlay(root, saved["reference"])
    if overlay is None or overlay["mode"] != "starter-overlay":
        return None
    check_publication_mode(adapter, saved["reference"])
    source = verify(root, overlay["source"])
    chain = adapter.runner_chain
    head = chain.head_block()
    stamp = chain.block_timestamp(head)
    require(natural(head) and natural(stamp), "finalized recovery clock")
    expiry = bounds(adapter, source["day"])[1]
    if stamp < expiry:
        return None
    marker = saved["marker"]
    require(marker["calculation_evidence"] == saved["reference"], "active receipt reference")
    adapter._check_direct_identity(marker)
    result = reconcile(adapter.reader, marker, finalized_chain=chain)
    current = chain.publication_state(adapter.netuid, adapter.uid, head, marker["mecid"])
    require(
        current["last_update"] == result["block"]
        and current["weights"]
        == [[uid, weight] for uid, weight in sorted(result["weights"].items())],
        "paired current publication differs from retained active receipt",
    )
    require((root / "starter-active.json").read_bytes() == raw, "active changed while checking")
    return dict(
        schema=SCHEMA,
        active_sha256=digest(raw),
        source=overlay["source"],
        source_day=source["day"],
        expires_at=expiry,
        checked_head=head,
        checked_hash=chain.block_hash(head),
        checked_timestamp=stamp,
        latest_closed_day=(stamp - adapter.day_anchor_ts) // 86400 - 1,
        active_block=result["block"],
        last_update=current["last_update"],
    )


def before_send(adapter, reference):
    """A prepared fresh fallback never carries an unchecked recovery exemption."""
    expected = getattr(adapter, "_fresh_daily_recovery", None)
    if expected is None:
        return
    require(expected["reference"] == reference, "recovery reference changed")
    observed = expired_active(
        adapter,
        expected_sha256=expected["active_sha256"],
        own_unsigned_intent=expected if expected.get("intent_id") else None,
    )
    require(observed is not None, "expired active recovery no longer applies")
    manifest = verify(adapter._submitted_record_dir, reference)
    require(manifest["day"] == observed["latest_closed_day"], "fresh day changed before send")
