"""Opt-in finalized collection and republication; daily economics stay unchanged.

Called by the existing loop under its writer lock. Never runs another signer or
service. Epoch receipts live separately from the original daily publication.
"""

import json
from pathlib import Path

from ..chain.dayblocks import _find_day_start, _validated_block_time
from ..chain.runner import run_day
from ..chain.sources import Refusal
from ..chain.store import DAY_SECONDS
from ..diagnostics import write as diagnostic
from .calculation_record import durable_write, encoded, verify
from .direct import reconcile
from .publish_intent import PublicationStatusUnknown
from .settings import SettingsError
from .writer_lock import LockHeld, held


def configure(adapter, settings):
    # Settings validates these before any connections or wallets are opened.
    adapter.epoch_collection = settings.epoch_collection
    adapter.epoch_republication = settings.epoch_republication


def collect(adapter, head):
    """Collect at most 1024 new blocks, plus missing fixed-stride holdings."""
    from .chain_adapter import _confirm_ark_identity

    chain = adapter.runner_chain
    day = (
        _validated_block_time(chain, head, "collection head") - adapter.day_anchor_ts
    ) // DAY_SECONDS
    first = (
        1
        if day == 0
        else _find_day_start(chain, day, adapter.day_anchor_ts, lower_block=None, upper_block=head)
    )
    store = adapter._store_factory()
    try:
        # The feature's tables are created by run_day. A pre-upgrade database need
        # not have them; don't swallow any other database error as an empty cursor.
        table = store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='epoch_collection'"
        ).fetchone()
        row = (
            store._conn.execute("SELECT body FROM epoch_collection WHERE day=?", (day,)).fetchone()
            if table
            else None
        )
        last = json.loads(row[0])["last"] if row else first - 1
        target = min(head, max(first - 1, last) + 1024)
        _confirm_ark_identity(
            chain,
            netuid=adapter.netuid,
            ark_coldkey=adapter.ark_coldkey,
            from_block=first,
            to_block=target,
        )
        run_day(
            store,
            chain,
            day=day,
            from_block=first,
            to_block=target,
            ark_coldkey=adapter.ark_coldkey,
            designated_validator_hotkey=adapter.designated_validator_hotkey,
            sources=chain.endpoints,
            netuid=adapter.netuid,
            holding_stride=adapter._holding_stride,
            read_collateral=adapter._read_collateral,
            attribute_deposits=True,
            progressive=True,
            finalize=False,
        )
        return {"day": day, "collected_through": target, "target_head": head}
    finally:
        store.close()


def republish(adapter, epoch, head, *, before_send=lambda: None):
    """Republish only today's verified closed-day result, with unchanged seats."""
    from .submitted_record import submitted_record_path

    chain = adapter.runner_chain
    day = (
        _validated_block_time(chain, head, "publication head") - adapter.day_anchor_ts
    ) // DAY_SECONDS - 1
    if adapter._publish_intent is None:
        raise PublicationStatusUnknown("epoch publication needs a durable intent")
    pending = adapter._publish_intent.load()
    if pending is not None:
        adapter._check_direct_identity(pending)
        evidence = reconcile(adapter.reader, pending, finalized_chain=chain)
        adapter._save_direct_confirmation(pending, evidence)
        adapter._publish_intent.clear()
    path = submitted_record_path(adapter._submitted_record_dir, day)
    if not path.exists():
        return "awaiting_daily_publication"
    record = json.loads(path.read_text())
    marker = record.get("direct_intent")
    if not isinstance(marker, dict) or marker.get("day") != day:
        raise PublicationStatusUnknown("daily source lacks an exact direct receipt")
    adapter._check_direct_identity(marker)
    reference = marker.get("calculation_evidence")
    if reference is None:
        raise PublicationStatusUnknown("daily source lacks retained calculation evidence")
    manifest = verify(adapter._submitted_record_dir, reference)
    if (
        manifest["day"] != day
        or manifest["netuid"] != adapter.netuid
        or manifest["genesis_hash"] != chain.genesis_hash()
        or manifest["publishable_weights_u16"] != marker["vector"]
    ):
        raise PublicationStatusUnknown("daily calculation identity or vector differs")
    reconcile(adapter.reader, marker, finalized_chain=chain)
    context_ref = manifest["artifacts"]["context"]
    context = json.loads((Path(adapter._submitted_record_dir) / context_ref["path"]).read_text())
    if (
        context.get("ark_address") != adapter.ark_coldkey
        or context.get("designated_validator_hotkey") != adapter.designated_validator_hotkey
        or context.get("scoring_dials") != adapter.dials
        or context.get("day_start_utc_unix") != adapter.day_anchor_ts + day * DAY_SECONDS
    ):
        raise PublicationStatusUnknown("daily calculation configuration changed")
    if chain.block_hash(context["to_block"]) != context["to_block_hash"]:
        raise PublicationStatusUnknown("scored finalized block changed")
    scored_seats = chain.seat_hotkeys(context["to_block"])
    live_seats = chain.seat_hotkeys(chain.head_block())
    if scored_seats != live_seats:
        raise PublicationStatusUnknown(
            "miner identities changed; require a fresh daily calculation"
        )
    vector = {int(k): v for k, v in marker["vector"].items()}
    if any(vector.get(uid, 0) for uid in adapter.owner_uids()):
        raise PublicationStatusUnknown("republication would pay an owner seat")
    # A daily publication or a reconciled refresh within this epoch counts.
    if adapter.last_update(adapter.uid, adapter.netuid) >= epoch:
        return "already_published_this_epoch"
    latest_day = (
        _validated_block_time(chain, chain.head_block(), "send head") - adapter.day_anchor_ts
    ) // DAY_SECONDS - 1
    if latest_day != day:
        return "awaiting_daily_publication"
    adapter._calculation_reference = reference
    before_send()
    adapter.publish_direct(vector, marker["height"], refresh_epoch=epoch, source_day=day)
    return "republished"


def confirmed_refresh_block(adapter, block, scoring_day):
    """Attribute EVERY matching event to a verified previous-day refresh.

    A send checked before midnight may land afterward. It is neither an unknown
    send nor today's daily result. Never exempt a whole block based on just one
    known transaction: another transaction/mechanism in it must still refuse.
    This recovery also applies with the feature switched off after a restart.
    """
    from ..chain.emissions import netuid_and_mechanism
    from .direct import _event_index
    from .reader import unwrap
    from .submitted_record import submitted_record_path

    root = Path(adapter._submitted_record_dir)
    indices = set()
    source_day = scoring_day - 1
    try:
        for path in (root / "epoch-publications").glob(f"*/day-{source_day}.submitted.json"):
            record = json.loads(path.read_text())
            marker = record["direct_intent"]
            result = marker.get("result", {})
            if result.get("block") != block:
                continue
            epoch = marker.get("refresh_epoch")
            if (type(epoch) is not int or epoch <= 0 or path.parent.name != str(epoch)
                    or marker.get("day") != source_day):
                return False
            adapter._check_direct_identity(marker)
            reference = marker.get("calculation_evidence")
            if reference is None:
                return False
            manifest = verify(root, reference)
            if (manifest["day"] != source_day
                    or manifest["netuid"] != adapter.netuid
                    or manifest["genesis_hash"] != adapter.runner_chain.genesis_hash()
                    or manifest["publishable_weights_u16"] != marker["vector"]):
                return False
            source = json.loads(submitted_record_path(root, source_day).read_text())["direct_intent"]
            adapter._check_direct_identity(source)
            if (source.get("day") != source_day or source.get("refresh_epoch") is not None
                    or source.get("calculation_evidence") != reference
                    or source.get("vector") != marker["vector"]):
                return False
            reconcile(adapter.reader, source, finalized_chain=adapter.runner_chain)
            evidence = reconcile(adapter.reader, marker, finalized_chain=adapter.runner_chain)
            indices.add(evidence["extrinsic_index"])
        if not indices:
            return False
        events = unwrap(adapter.reader.client.query(("System", "Events"), None, block=block))
        if not isinstance(events, list):
            return False
        matched = []
        for record in events:
            event = record.get("event", {})
            if event.get("event_id") != "WeightsSet":
                continue
            attrs = event.get("attributes")
            if (netuid_and_mechanism(attrs[0])[0] == adapter.netuid
                    and int(attrs[1]) == adapter.uid):
                if (event.get("module_id") != "SubtensorModule"
                        or netuid_and_mechanism(attrs[0])[1] != adapter.publisher.mecid):
                    return False
                matched.append(_event_index(record))
        return len(matched) == len(indices) and set(matched) == indices
    except Exception:
        # Missing, corrupt, mismatched or unreadable evidence cannot permit a send.
        return False


def maintain(adapter, lock_path):
    """Bounded maintenance after the normal daily tick; never hides a failed day."""
    if not (
        getattr(adapter, "epoch_collection", False)
        or getattr(adapter, "epoch_republication", False)
    ):
        return
    try:
        with held(lock_path):
            if adapter._publish_hold is not None:
                adapter._publish_hold.raise_if_held()
            chain = adapter.runner_chain
            head = chain.head_block()
            epoch = adapter.reader._query("LastEpochBlock", [adapter.netuid], block=head)
            if type(epoch) is not int or not 0 < epoch <= head:
                raise SettingsError("finalized epoch boundary is unavailable")
            path = Path(adapter._submitted_record_dir) / "epoch-maintenance.json"
            identity = [chain.genesis_hash(), adapter.netuid, adapter.wallet_hotkey.ss58_address]
            state = json.loads(path.read_text()) if path.exists() else {"identity": identity}
            if state.get("identity") != identity:
                raise SettingsError("epoch maintenance state belongs to another validator")
            if (
                getattr(adapter, "epoch_republication", False)
                and state.get("published_epoch") != epoch
            ):

                def before_send():
                    if state.get("attempted_epoch") == epoch:
                        raise PublicationStatusUnknown("this epoch already had a send attempt")
                    state["attempted_epoch"] = epoch
                    durable_write(path, encoded(state), replace=True)

                try:
                    outcome = republish(adapter, epoch, head, before_send=before_send)
                    state["publication_status"] = outcome
                    if outcome != "awaiting_daily_publication":
                        state["published_epoch"] = epoch
                except (Exception, Refusal) as error:
                    state["publication_status"] = type(error).__name__
                    diagnostic(
                        f"[epoch] publication refused ({type(error).__name__})", severity="error"
                    )
                durable_write(path, encoded(state), replace=True)
            if (
                getattr(adapter, "epoch_collection", False)
                and state.get("collected_epoch") != epoch
            ):
                result = collect(adapter, head)
                state.update(result)
                if result["collected_through"] == head:
                    state["collected_epoch"] = epoch
                durable_write(path, encoded(state), replace=True)
                diagnostic(
                    f"[epoch] collected finalized history through {result['collected_through']}"
                )
    except LockHeld:
        return
    except (Exception, Refusal) as error:
        # Fail closed without preventing the existing daily scheduler's next tick.
        # Unknown sends retain the shared intent and block ALL later sends.
        diagnostic(
            f"[epoch] maintenance refused ({type(error).__name__}); "
            "daily processing continues; inspect retained state",
            severity="error",
        )
