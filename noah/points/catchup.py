"""Verified missed-day accounting, explicitly not a publication receipt.

The ordinary scorer already replays all financial days. These records only let
the scheduler progress past stale weights after retaining that day's full proof.
"""

import json
import re
from pathlib import Path

from ..core.calculation_record import artifact, digest, durable_write, encoded, verify
from ..core.publish_intent import PublicationStatusUnknown
from . import amendment, ema_policy, onboarding
from .mode import MODE, check_publication_mode

MODES = (MODE, amendment.MODE, ema_policy.MODE, onboarding.MODE)
SCHEMA = "noah-points-accounted-day/1"


def require(ok, reason):
    if not ok:
        raise PublicationStatusUnknown("points accounting evidence: " + reason)


def natural(value):
    return type(value) is int and value >= 0


def validate_reference(ref):
    require(type(ref) is dict and set(ref) == {"path", "sha256", "day"}, "reference fields")
    require(
        natural(ref["day"]) and ref["path"] == f"day-{ref['day']}.accounted.json",
        "reference day/path",
    )
    require(
        type(ref["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]),
        "reference digest",
    )


def validate_record(record, manifest, context):
    """Pure structural/link validation; caller authenticates the supplied graph."""
    require(
        type(record) is dict
        and set(record)
        == {
            "schema",
            "status",
            "day",
            "height",
            "calculation_evidence",
            "points_policy",
            "identity",
            "observed",
            "next_pending",
        },
        "record fields",
    )
    require(
        record["schema"] == SCHEMA and record["status"] == "accounted_not_published",
        "record disposition",
    )
    day = record["day"]
    require(natural(day) and natural(record["height"]), "day/height")
    require(
        context.get("scoring_mode") in MODES and "starter_overlay" not in context,
        "ordinary points calculation required",
    )
    require(
        day == manifest["day"] == context["day"]
        and record["height"] == manifest["height"] == context["height"],
        "calculation day",
    )
    require(
        type(record["points_policy"]) is dict
        and record["points_policy"] == context.get("points_policy"),
        "policy",
    )
    identity = record["identity"]
    require(
        type(identity) is dict
        and set(identity) == {"genesis_hash", "netuid", "uid", "hotkey"}
        and natural(identity["netuid"])
        and natural(identity["uid"])
        and type(identity["hotkey"]) is str
        and bool(identity["hotkey"]),
        "publisher identity",
    )
    require(
        identity["genesis_hash"] == manifest["genesis_hash"] == context["genesis_hash"]
        and identity["netuid"] == manifest["netuid"] == context["netuid"]
        and identity["hotkey"] == context.get("validator_hotkey")
        and identity["uid"] == context.get("publisher_uid"),
        "calculation identity",
    )
    require(
        type(record["calculation_evidence"]) is dict
        and record["calculation_evidence"]["day"] == day
        and record["calculation_evidence"]["genesis_hash"] == identity["genesis_hash"]
        and record["calculation_evidence"]["sha256"] == digest(encoded(manifest)),
        "calculation reference",
    )
    obs, next_day = record["observed"], record["next_pending"]
    require(
        type(obs) is dict
        and set(obs) == {"block", "block_hash", "timestamp", "latest_closed_day"}
        and all(natural(obs[k]) for k in ("block", "timestamp", "latest_closed_day"))
        and type(obs["block_hash"]) is str
        and re.fullmatch(r"0x[0-9a-f]{64}", obs["block_hash"]),
        "finalized observation",
    )
    require(
        all(
            type(context.get(k)) is str and re.fullmatch(r"0x[0-9a-f]{64}", context[k])
            for k in ("from_block_hash", "to_block_hash")
        ),
        "finalized boundary hashes",
    )
    require(
        all(
            natural(context[k])
            for k in ("from_block", "to_block", "day_start_utc_unix", "day_end_utc_unix")
        )
        and context["from_block"] <= context["to_block"] < obs["block"]
        and context["day_end_utc_unix"] - context["day_start_utc_unix"] == 86400,
        "complete UTC day",
    )
    anchor = context["day_start_utc_unix"] - day * 86400
    require(
        obs["latest_closed_day"] == (obs["timestamp"] - anchor) // 86400 - 1
        and day < obs["latest_closed_day"],
        "only stale closed days may be accounted",
    )
    require(
        type(next_day) is dict
        and set(next_day) == {"day", "height"}
        and all(natural(v) for v in next_day.values())
        and next_day == {"day": day + 1, "height": context["to_block"] + 1},
        "next day boundary",
    )


def verify_record(directory, ref):
    validate_reference(ref)
    root = Path(directory)
    raw = (root / ref["path"]).read_bytes()
    require(digest(raw) == ref["sha256"], "record digest")
    record = json.loads(raw)
    manifest = verify(root, record["calculation_evidence"])
    context = artifact(root, manifest, "context")
    validate_record(record, manifest, context)
    require(record["day"] == ref["day"], "reference record day")
    canonical = json.loads((root / f"day-{ref['day']}.calculation.json").read_bytes())
    require(canonical["reference"] == record["calculation_evidence"], "canonical calculation")
    require(
        not any(
            (root / f"day-{ref['day']}.{kind}.json").exists() for kind in ("submitted", "confirmed")
        ),
        "accounting disposition conflicts with submitted receipt",
    )
    return record


def record_accounted(adapter, height):
    if getattr(adapter, "scoring_mode", "legacy") not in MODES:
        return None
    root = Path(adapter._submitted_record_dir)
    chain = adapter.runner_chain
    ref = adapter._calculation_reference
    manifest = verify(root, ref)
    context = artifact(root, manifest, "context")
    head = chain.head_block()
    stamp = chain.block_timestamp(head)
    require(natural(head) and natural(stamp), "finalized clock")
    latest = (stamp - adapter.day_anchor_ts) // 86400 - 1
    if manifest["day"] >= latest:
        return None
    require(
        adapter.publish_mode == "direct"
        and adapter._publish_intent is not None
        and adapter._publish_intent.load() is None,
        "unresolved send or unsupported route",
    )
    if adapter._publish_hold is not None:
        adapter._publish_hold.raise_if_held()
    check_publication_mode(adapter, ref)
    require(
        height == context["height"]
        and chain.block_hash(context["to_block"]) == context["to_block_hash"],
        "finalized calculation boundary",
    )
    next_pending = {"day": manifest["day"] + 1, "height": context["to_block"] + 1}
    require(
        (chain.block_timestamp(next_pending["height"]) - adapter.day_anchor_ts) // 86400
        == next_pending["day"],
        "next UTC day",
    )
    record = dict(
        schema=SCHEMA,
        status="accounted_not_published",
        day=manifest["day"],
        height=height,
        calculation_evidence=ref,
        points_policy=adapter.points_policy.manifest,
        identity=dict(
            genesis_hash=chain.genesis_hash(),
            netuid=adapter.netuid,
            uid=adapter.uid,
            hotkey=adapter.wallet_hotkey.ss58_address,
        ),
        observed=dict(
            block=head, block_hash=chain.block_hash(head), timestamp=stamp, latest_closed_day=latest
        ),
        next_pending=next_pending,
    )
    validate_record(record, manifest, context)
    path = f"day-{manifest['day']}.accounted.json"
    if (root / path).exists():
        old_ref = dict(path=path, sha256=digest((root / path).read_bytes()), day=manifest["day"])
        old = verify_record(root, old_ref)
        require(
            all(old[k] == record[k] for k in record if k != "observed"),
            "conflicting earlier accounting disposition",
        )
        require(
            chain.block_hash(old["observed"]["block"]) == old["observed"]["block_hash"],
            "earlier finalized observation changed",
        )
        return old_ref, next_pending
    require(
        not any(
            (root / f"day-{manifest['day']}.{kind}.json").exists()
            for kind in ("submitted", "confirmed")
        ),
        "already submitted or confirmed",
    )
    raw = encoded(record)
    durable_write(root / path, raw)
    return dict(path=path, sha256=digest(raw), day=manifest["day"]), next_pending


def validate_loop_references(refs, pending):
    require(type(refs) is list, "loop references")
    for ref in refs:
        validate_reference(ref)
    days = [ref["day"] for ref in refs]
    require(days == sorted(set(days)), "ordered unique loop references")
    if pending is not None:
        require(
            type(pending) is dict
            and set(pending) == {"day", "height"}
            and all(natural(v) for v in pending.values()),
            "pending shape",
        )
        require(not days or pending["day"] > days[-1], "pending precedes accounted day")


def validate_accounted_state(state, record):
    if state.get("phase") != "day_catchup_accounted":
        return
    refs = state.get("points_accounting")
    validate_loop_references(refs, state.get("day_catchup_pending"))
    require(
        bool(refs)
        and state.get("accounting_reference") == refs[-1]
        and record["day"] == refs[-1]["day"]
        and state.get("day_catchup_pending") == record["next_pending"],
        "accounted phase must advance exactly one verified day",
    )


def validate_loop(adapter, refs, pending, *, state=None):
    """A persisted operational advance must never outlive its accounting proof."""
    if getattr(adapter, "scoring_mode", "legacy") not in MODES:
        require(not refs, "points accounting is not a Legacy loop state")
        return
    validate_loop_references(refs, pending)
    if state is not None and state.get("phase") == "day_catchup_accounted":
        require(bool(refs), "accounted phase lacks evidence")
        validate_accounted_state(state, verify_record(adapter._submitted_record_dir, refs[-1]))
    days = []
    for ref in refs:
        record = verify_record(adapter._submitted_record_dir, ref)
        require(
            record["identity"]
            == dict(
                genesis_hash=adapter.runner_chain.genesis_hash(),
                netuid=adapter.netuid,
                uid=adapter.uid,
                hotkey=adapter.wallet_hotkey.ss58_address,
            ),
            "loop publisher identity",
        )
        days.append(ref["day"])
    require(days == sorted(set(days)), "ordered unique loop references")
    known = {ref["path"] for ref in refs}
    # The only unlinked record allowed is a crash after disposition fsync but
    # before the atomic next-pending write. The unchanged pending day retries it.
    if pending is not None:
        pending_path = f"day-{pending['day']}.accounted.json"
        path = Path(adapter._submitted_record_dir) / pending_path
        if path.exists():
            record = verify_record(
                adapter._submitted_record_dir,
                dict(path=pending_path, sha256=digest(path.read_bytes()), day=pending["day"]),
            )
            require(
                record["identity"]
                == dict(
                    genesis_hash=adapter.runner_chain.genesis_hash(),
                    netuid=adapter.netuid,
                    uid=adapter.uid,
                    hotkey=adapter.wallet_hotkey.ss58_address,
                )
                and record["height"] == pending["height"],
                "pending accounting identity",
            )
        known.add(pending_path)
    present = {p.name for p in Path(adapter._submitted_record_dir).glob("day-*.accounted.json")}
    require(present <= known, "loop omitted retained accounting evidence")
