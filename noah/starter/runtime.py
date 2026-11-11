"""Opt-in starter updater and independently runnable stale-allocation guard."""

import json
import time
from pathlib import Path

from ..chain.sources import Refusal
from ..core.calculation_record import durable_write, encoded
from ..core.direct import reconcile
from ..core.publication_diagnostics import failure_details, guard_context
from ..core.publish_intent import PublicationStatusUnknown
from ..core.settings import SettingsError
from ..core.submitted_record import submitted_record_path
from ..core.writer_lock import LockHeld
from ..diagnostics import write as diagnostic
from .allocation import preview
from .entry import EntryPolicy
from .health import GUARD_MAX_AGE
from .locking import publication_lock
from .publication import retain_overlay, verify_overlay
from .reader import current_input


def identity(adapter):
    result = [
        adapter.runner_chain.genesis_hash(),
        adapter.netuid,
        adapter.wallet_hotkey.ss58_address,
        adapter.starter_entry_policy.snapshot["sha256"],
    ]
    policy = getattr(adapter, "points_policy", None)
    if policy is not None:
        result.append(policy.manifest)
    return result


def configure(adapter, settings):
    path = getattr(settings, "starter_entry_snapshot", None)
    if not path:
        return
    if not (
        settings.epoch_collection
        and settings.epoch_republication
        and settings.publish_mode == "direct"
    ):
        raise SettingsError("starter entry requires direct publication and both epoch features")
    if adapter._publish_intent is None or adapter._submitted_record_dir is None:
        raise SettingsError("starter entry requires durable intent and calculation storage")
    try:
        adapter.starter_entry_policy = EntryPolicy.load(path)
        from ..core.version import version_key_for
        from ..scoring.parameters import load_parameters
        from .allocation import POLICY_ID

        parameters = load_parameters()
        if parameters.profile_version not in ("mainnet-production-starter-25bps-v1", "mainnet-production-points-402040-v1", "mainnet-production-points-204040-v5"):
            raise SettingsError("starter entry requires its separately versioned parameter profile")
        if parameters.profile_version in ("mainnet-production-points-402040-v1", "mainnet-production-points-204040-v5") and not getattr(settings,"points_policy",None):
            raise SettingsError("points profile requires NOAH_POINTS_POLICY")
        key = version_key_for("noah-scoring/surplus-bonus-v1/" + POLICY_ID)
        head = adapter.runner_chain.head_block()
        floor = adapter.runner_chain.starter_epoch_info(head)["WeightsVersionKey"]
        if type(floor) is not int or (parameters.profile_version not in ("mainnet-production-points-402040-v1", "mainnet-production-points-204040-v5") and key < floor):
            raise SettingsError("starter publication version is below the chain floor")
        adapter.publisher.version_key = key
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SettingsError("starter entry migration could not be read or validated") from exc


def require_guard(adapter):
    path = Path(adapter._submitted_record_dir) / "starter-guard.json"
    try:
        health = json.loads(path.read_text())
        age = time.time() - health["observed_at"]
        if health["identity"] != identity(adapter) or not 0 <= age <= GUARD_MAX_AGE:
            raise ValueError("stale guard")
        if health["status"] not in ("idle", "watching", "ordinary", "replaced"):
            raise ValueError("unhealthy guard")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PublicationStatusUnknown("starter guard is absent, stale, or unhealthy") from exc


def recover(adapter):
    pending = adapter._publish_intent.load()
    if pending is not None:
        adapter._check_direct_identity(pending)
        evidence = reconcile(adapter.reader, pending, finalized_chain=adapter.runner_chain)
        adapter._save_direct_confirmation(pending, evidence)
        adapter._publish_intent.clear()


def ordinary_reference(adapter, reference):
    proof = verify_overlay(adapter._submitted_record_dir, reference)
    return proof["source"] if proof is not None else reference


def prepare_daily(adapter):
    """Apply the same starter gate to daily sends, avoiding an uncapped interval."""
    from ..core.epoch_work import collect
    from ..core.reader import ChainUnreadable

    # Pin once and collect before taking the short publication lock. The daily
    # tick precedes maintenance, and a later head would invalidate the cursor.
    # Bounded catch-up is a retryable read wait, not a permanently refused day.
    if (getattr(adapter, "_fresh_daily_recovery", None) is not None
            and adapter._publish_intent.load() is not None):
        raise PublicationStatusUnknown("fresh daily recovery has an unresolved publication")
    adapter._fresh_daily_recovery = None
    head = adapter.runner_chain.head_block()
    collected = collect(adapter, head)
    if collected["collected_through"] != head:
        raise ChainUnreadable("starter daily history catch-up is incomplete; wait for collection")
    with publication_lock(adapter):
        from ..points.daily_recovery import expired_active
        recovery = expired_active(adapter)
        if recovery is None:
            require_guard(adapter)
        source = adapter._calculation_reference
        raw = current_input(adapter, source, head)
        state_path = Path(adapter._submitted_record_dir) / "starter-state.json"
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        result = preview(raw, previous)
        if recovery is not None:
            # Only fresh ordinary weights replace an expired overlay. Never
            # advance pool allocation state before a confirmed publication.
            ref = retain_overlay(
                adapter._submitted_record_dir, source, raw, previous, fallback=True
            )
            adapter._calculation_reference = ref
            adapter._fresh_daily_recovery = dict(
                reference=ref, active_sha256=recovery["active_sha256"]
            )
            return {int(k): v for k, v in result["ordinaryFallbackWeightsU16"].items()}
        ref = retain_overlay(adapter._submitted_record_dir, source, raw, previous)
        durable_write(state_path, encoded(result["state"]), replace=True)
        adapter._calculation_reference = ref
        return {int(k): v for k, v in result["proposedWeightsU16"].items()}


def republish(adapter, epoch, head, *, before_send=lambda: None):
    require_guard(adapter)
    with publication_lock(adapter):
        recover(adapter)
        chain = adapter.runner_chain
        day = (chain.block_timestamp(head) - adapter.day_anchor_ts) // 86400 - 1
        path = submitted_record_path(adapter._submitted_record_dir, day)
        if not path.exists():
            if getattr(adapter, "points_policy", None):
                from ..points.outage import republish as outage_refresh
                return outage_refresh(adapter, epoch, head, before_send=before_send)
            return "awaiting_daily_publication"
        marker = json.loads(path.read_text())["direct_intent"]
        adapter._check_direct_identity(marker)
        reconcile(adapter.reader, marker, finalized_chain=chain)
        last = adapter.last_update(adapter.uid, adapter.netuid)
        if last >= epoch:
            active_path = Path(adapter._submitted_record_dir) / "starter-active.json"
            active = json.loads(active_path.read_text()) if active_path.exists() else None
            proof = (
                verify_overlay(adapter._submitted_record_dir, active["reference"])
                if active and active.get("reference")
                else None
            )
            if not (
                proof
                and proof["mode"] == "ordinary-fallback"
                and active["marker"]["result"]["block"] == last
            ):
                return "already_published_this_epoch"
            # A known guard replacement is not the new epoch's allocation.
            # Respect chain rate limits and retry after the safety send settles.
            if head - last < adapter.rate_limit(adapter.netuid):
                return "awaiting_rate_limit"
        source = ordinary_reference(adapter, marker["calculation_evidence"])
        from ..points.mode import check_publication_mode, source_mode
        points = getattr(adapter, "points_policy", None)
        mode, _, manifest = source_mode(adapter._submitted_record_dir, source)
        if points is not None and mode == "legacy" and manifest["day"] <= points.migration["cutoff_day"]:
            return "awaiting_points_first_day"
        check_publication_mode(adapter, source)
        raw = current_input(adapter, source, head)
        state_path = Path(adapter._submitted_record_dir) / "starter-state.json"
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        result = preview(raw, previous)
        ref = retain_overlay(adapter._submitted_record_dir, source, raw, previous)
        durable_write(state_path, encoded(result["state"]), replace=True)
        require_guard(adapter)
        adapter._calculation_reference = ref
        before_send()
        adapter.publish_direct(
            {int(k): v for k, v in result["proposedWeightsU16"].items()},
            marker["height"],
            refresh_epoch=epoch,
            source_day=day,
        )
        return "starter_overlay_published"


def guard_tick(adapter, *, remove_now=False):
    """One independent guard pass. Failures are recorded and never clear intent."""
    from ..points.onboarding import enabled
    if enabled(adapter):
        return dict(status="disabled-no-pool", head=None)
    root = Path(adapter._submitted_record_dir)
    status = "blocked"
    stage, head = "publication_lock", None
    try:
        with publication_lock(adapter):
            stage = "hold"
            if adapter._publish_hold is not None:
                adapter._publish_hold.raise_if_held()
            stage = "recover_pending"
            recover(adapter)
            chain = adapter.runner_chain
            stage = "read_head"
            head = chain.head_block()
            active = root / "starter-active.json"
            status = "idle"
            if active.exists():
                stage = "read_active"
                saved = json.loads(active.read_text())
                stage = "verify_overlay"
                proof = verify_overlay(root, saved["reference"]) if saved["reference"] else None
                status = "ordinary"
                if proof is not None and proof["mode"] == "starter-overlay":
                    status = "watching"
                    snap = proof["input"]["snapshot"]
                    stage = "read_epoch"
                    epoch = chain.starter_epoch_info(head)["LastEpochBlock"]
                    if remove_now or head >= snap["nextEpochBlock"] or epoch > snap["epoch"]:
                        marker = saved["marker"]
                        stage = "check_identity"
                        adapter._check_direct_identity(marker)
                        stage = "reconcile_active"
                        reconcile(adapter.reader, marker, finalized_chain=chain)
                        stage = "check_latest_update"
                        if (
                            adapter.last_update(adapter.uid, adapter.netuid)
                            > marker["result"]["block"]
                        ):
                            raise PublicationStatusUnknown("unrecognized newer weight publication")
                        from ..points.daily_recovery import STATUS, expired_active, validate_health
                        recovery = expired_active(adapter)
                        if recovery is not None:
                            health = dict(
                                identity=identity(adapter), observed_at=time.time(),
                                head=recovery["checked_head"], status=STATUS, recovery=recovery
                            )
                            validate_health(health, root, adapter.points_policy.manifest)
                            durable_write(
                                root / "starter-guard.json", encoded(health), replace=True
                            )
                            return health
                        stage = "compute_fallback"
                        result = preview(proof["input"], proof["previous"])
                        stage = "read_seats"
                        seats = chain.seat_hotkeys(head)
                        rows = proof["input"]["snapshot"]["seats"]
                        stage = "read_owners"
                        owners = chain.hotkey_owners([row["hotkey"] for row in rows], head)
                        stage = "read_owner_uids"
                        owner_uids = adapter.owner_uids()
                        removed = [
                            row["uid"]
                            for row in rows
                            if (
                                seats.get(row["uid"]) != row["hotkey"]
                                or owners.get(row["hotkey"]) != row["coldkey"]
                                or row["uid"] in owner_uids
                            )
                        ]
                        stage = "retain_fallback"
                        ref = retain_overlay(
                            root,
                            proof["source"],
                            proof["input"],
                            proof["previous"],
                            fallback=True,
                            removed_uids=removed,
                        )
                        adapter._calculation_reference = ref
                        stage = "publish_fallback"
                        adapter.publish_direct(
                            {
                                int(k): (0 if int(k) in removed else v)
                                for k, v in result["ordinaryFallbackWeightsU16"].items()
                            },
                            marker["height"],
                            refresh_epoch=epoch,
                            source_day=marker["day"],
                        )
                        status = "replaced"
        stage = "record_health"
        health = dict(identity=identity(adapter), observed_at=time.time(), head=head, status=status)
        durable_write(root / "starter-guard.json", encoded(health), replace=True)
        return health
    except LockHeld as exc:
        # The active sender owns the short lock. Do not poison its recent guard check.
        guard_context(exc, stage, head)
        raise
    except (Exception, Refusal) as exc:
        # Do not preserve a fresh-looking success heartbeat after a refused pass.
        guard_context(exc, stage, head)
        durable_write(
            root / "starter-guard.json",
            encoded(dict(observed_at=time.time(), status="blocked", **failure_details(exc))),
            replace=True,
        )
        raise


def run_guard(adapter, *, poll_seconds=10, iterations=None, sleep=time.sleep):
    if adapter.starter_entry_policy is None:
        raise SettingsError("starter guard requires an explicit entry snapshot")
    count = 0
    while iterations is None or count < iterations:
        try:
            health = guard_tick(adapter)
            diagnostic(f"[starter-guard] {health['status']} at finalized block {health['head']}")
        except (Exception, Refusal) as exc:
            details = failure_details(exc)
            diagnostic(
                f"[starter-guard] blocked: {details['error']}; "
                f"details={json.dumps(details, sort_keys=True)}",
                severity="error",
            )
        count += 1
        if iterations is None or count < iterations:
            sleep(poll_seconds)
