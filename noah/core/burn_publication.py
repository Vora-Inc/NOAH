"""Retain and recheck one burn split. Recovery recomputes it.

The pre-burn allocation, destination, setting and published vector stay in the
calculation record. A positive owner weight is paid only when that record
reproduces it. Same-day return to zero publishes a new receipt and leaves the
original receipt in place.
"""

import json
from pathlib import Path

from .burn_allocation import ENABLED, OFF, allocate, require_chain_limits
from .calculation_record import artifact, durable_write, encoded, retain, verify
from .publish import PublicationRefused
from .publish_intent import PublicationStatusUnknown

SCHEMA = "noah-burn-allocation/1"


def allocation_of(context):
    value = context.get("burn_allocation")
    if value is None:
        return None
    if type(value) is not dict or value.get("schema") != SCHEMA:
        raise PublicationRefused("burn proof is missing or corrupt")
    if 'burn_context' in value:
        from ..chain.burn_context import validate
        evidence = validate(value['burn_context'])
        if (evidence['netuid'] != context.get('netuid')
                or evidence['genesis'] != context.get('genesis_hash')):
            raise PublicationRefused('effective Burn proof chain/subnet differs from calculation')
    return value


def wrap_allocation(directory, source_reference, published, body):
    """New content-addressed record. The source record is not rewritten."""
    source = verify(directory, source_reference)
    context = artifact(directory, source, "context")
    if "burn_allocation" in context and body.get("bps") != OFF:
        raise PublicationRefused("burn proof already exists on this calculation")
    proof = dict(body, schema=SCHEMA, source=source_reference)
    _require_recompute(proof, published)
    return retain(
        directory,
        fixture=artifact(directory, source, "fixture"),
        agreed_text=encoded(artifact(directory, source, "agreed_score")).decode(),
        vector=published,
        context={**context, "burn_allocation": proof},
        canonical_day=False,
    )


def prove(vector, bps, destination, limits, *, burn_context=None):
    """Build the published vector and the proof body from the pre-burn vector."""
    destination_uid = None if destination is None else destination["uid"]
    published = allocate(vector, bps, destination_uid)
    if bps == ENABLED and published is not vector:
        require_chain_limits(published, **limits)
    body = {
        "schema": SCHEMA,
        "bps": bps,
        "destination": destination,
        "pre_burn_u16": {str(uid): weight for uid, weight in vector.items()},
        "limits": limits,
    }
    if burn_context is not None:
        from ..chain.burn_context import validate
        body['burn_context'] = validate(burn_context)
    return published, body


def recompute(directory, reference, vector):
    """Return ``(pre_burn, destination_uid or None)`` or raise.

    ``vector`` is the vector about to be sent. It must equal the retained
    publication, and that publication must equal the split of the retained
    pre-burn allocation.
    """
    manifest = verify(directory, reference)
    context = artifact(directory, manifest, "context")
    proof = allocation_of(context)
    if proof is None:
        if _owner_paid(vector, ()):
            raise PublicationRefused("owner weight has no burn proof")
        return None
    published = {int(uid): weight for uid, weight in manifest["publishable_weights_u16"].items()}
    if {str(uid): weight for uid, weight in vector.items()} != manifest["publishable_weights_u16"]:
        raise PublicationRefused("send vector differs from retained burn publication")
    pre = {int(uid): weight for uid, weight in proof["pre_burn_u16"].items()}
    _require_recompute(proof, published)
    source = verify(directory, proof["source"])
    if proof["bps"] == ENABLED:
        source_vector = {int(uid): weight for uid, weight in source["publishable_weights_u16"].items()}
        if source_vector != pre:
            raise PublicationRefused("burn proof does not match its pre-burn calculation")
        return proof["destination"]["uid"]
    if proof["bps"] != OFF or "replaces" not in proof:
        raise PublicationRefused("burn proof is missing or corrupt")
    replaced_context = artifact(directory, source, "context")
    replaced = allocation_of(replaced_context)
    if replaced is None:
        raise PublicationRefused("burn removal does not name a burn publication")
    replaced_pre = {int(uid): weight for uid, weight in replaced["pre_burn_u16"].items()}
    replaced_published = {int(uid): weight for uid, weight in source["publishable_weights_u16"].items()}
    if replaced["bps"] != ENABLED or replaced_pre != pre or allocate(pre, ENABLED, replaced["destination"]["uid"]) != replaced_published:
        raise PublicationRefused("burn removal does not reproduce the retained bonus allocation")
    if published != pre:
        raise PublicationRefused("burn removal changes the retained pre-burn allocation")
    return None


def _require_recompute(proof, published):
    if 'burn_context' in proof:
        from ..chain.burn_context import validate
        validate(proof['burn_context'])
    try:
        pre = {int(uid): weight for uid, weight in proof["pre_burn_u16"].items()}
        destination = proof.get("destination")
        uid = None if destination is None else destination["uid"]
        if proof["bps"] == ENABLED and destination is None:
            raise BurnAllocationError
        if allocate(pre, proof["bps"], uid) != published:
            raise BurnAllocationError
    except (KeyError, TypeError, ValueError) as error:
        raise PublicationRefused("burn proof is missing or corrupt") from error


class BurnAllocationError(ValueError):
    pass


def _owner_paid(vector, owner_uids):
    return any(vector.get(uid, 0) for uid in owner_uids)


def authenticate_destination(chain, netuid, reader, *, include_evidence=False):
    """The SubnetOwnerHotkey UID whose coldkey is SubnetOwner, at the current head."""
    block = chain.head_block()
    coldkey, hotkey = chain.subnet_owner(block)
    seats = chain.seat_hotkeys(block)
    matches = sorted(uid for uid, holder in seats.items() if holder == hotkey)
    if len(matches) != 1:
        raise PublicationRefused("burn destination is not exactly one registered owner hotkey")
    uid = matches[0]
    owner = chain.hotkey_owners([hotkey], block).get(hotkey)
    if owner != coldkey:
        raise PublicationRefused("burn destination coldkey is not SubnetOwner")
    evidence = None
    value = reader.recycle_or_burn(netuid)
    if value is None:
        # Absence alone never permits a burn. Both independently checked
        # readers and the author must prove the same reviewed runtime state.
        pin = chain.block_hash(block)
        evidence = chain.burn_context(block, netuid, expected_block_hash=pin)
        from ..chain.burn_context import validate
        validate(evidence)
        if (evidence['block'] != block or evidence['block_hash'] != pin
                or evidence['netuid'] != netuid
                or evidence['genesis'] != chain.genesis_hash()):
            raise PublicationRefused('effective Burn proof is for a different chain or pin')
        author = reader.burn_context(block, netuid, expected_block_hash=pin)
        validate(author)
        if author != evidence:
            raise PublicationRefused('effective Burn author and paired readers disagree')
    elif value != "Burn":
        raise PublicationRefused("burn destination requires explicit or proven effective Burn")
    destination = {"uid": uid, "hotkey": hotkey, "coldkey": coldkey}
    return (destination, evidence) if include_evidence else destination


def read_limits(reader, netuid, *, burn_context=None):
    if burn_context is not None:
        from ..chain.burn_context import validate
        validate(burn_context)
        if burn_context['netuid'] != netuid:
            raise PublicationRefused('effective Burn proof subnet differs')
    return {
        "max_weight_limit": reader.max_weights_limit(netuid),
        "min_allowed_weights": reader.min_allowed_weights(netuid),
        "epoch_consensus": reader.epoch_consensus(netuid),
        "recycle_or_burn": (burn_context['effective'] if burn_context is not None
                            else reader.recycle_or_burn(netuid)),
    }


def recheck_for_send(adapter, reference, vector):
    """Re-read destination and limits when this send still carries burn weight."""
    directory = adapter._submitted_record_dir
    manifest = verify(directory, reference)
    proof = allocation_of(artifact(directory, manifest, "context"))
    destination_uid = recompute(directory, reference, vector)
    if proof is None or proof["bps"] != ENABLED:
        return None
    if not any(vector.values()):
        return None
    destination, burn_context = authenticate_destination(
        adapter.runner_chain, adapter.netuid, adapter.reader, include_evidence=True)
    if destination != proof["destination"] or destination["uid"] != destination_uid:
        raise PublicationRefused("burn destination changed before signing")
    limits = read_limits(adapter.reader, adapter.netuid, burn_context=burn_context)
    require_chain_limits(vector, **limits)
    if limits["recycle_or_burn"] != "Burn" or limits["epoch_consensus"] != "Yuma":
        raise PublicationRefused("burn chain limits changed before signing")
    owners = set(adapter.owner_uids())
    paid_owners = {uid for uid in owners if vector.get(uid, 0)}
    if paid_owners != {destination["uid"]}:
        raise PublicationRefused("burn publication pays an owner seat that is not the destination")
    return destination["uid"]


def report_subject(directory, reference, vector):
    """Validate a burn wrapper and return the pre-burn report record and vector."""
    recompute(directory, reference, vector)
    manifest = verify(directory, reference)
    proof = allocation_of(artifact(directory, manifest, "context"))
    pre = {int(uid): weight for uid, weight in proof["pre_burn_u16"].items()}
    if proof["bps"] == ENABLED:
        return proof["source"], pre
    replaced = verify(directory, proof["source"])
    inner = allocation_of(artifact(directory, replaced, "context"))
    return inner["source"], pre


def transition_dir(directory, day):
    return Path(directory) / "burn-transitions" / f"day-{int(day)}"


def confirmed_transition(directory, day):
    """The one confirmed zero-burn receipt for this day, or None."""
    folder = transition_dir(directory, day)
    if not folder.is_dir():
        return None
    paths = sorted(folder.glob("*/day-*.submitted.json"))
    if not paths:
        return None
    if len(paths) != 1:
        raise PublicationStatusUnknown("more than one burn removal receipt exists")
    record = json.loads(paths[0].read_text())
    marker = record.get("direct_intent")
    if not isinstance(marker, dict) or marker.get("successor_kind") != "burn-zero":
        raise PublicationStatusUnknown("burn removal receipt is not a new publication")
    recompute(directory, marker.get("calculation_evidence"), {int(k): v for k, v in marker["vector"].items()})
    return record


def write_transition_receipt(directory, marker, published):
    """Immutable receipt. A different body for the same evidence is refused."""
    reference = marker["calculation_evidence"]
    day = marker["day"]
    folder = transition_dir(directory, day) / reference["sha256"]
    path = folder / f"day-{day}.submitted.json"
    from .submitted_record import SCHEMA
    payload = {
        "schema": SCHEMA,
        "day": day,
        "height": marker["height"],
        "netuid": marker["netuid"],
        "hotkey": marker["hotkey"],
        "commit_identity": marker["hotkey"],
        "signer": marker.get("signer"),
        "route": marker.get("route"),
        "uid": marker.get("uid"),
        "publish_mode": "direct",
        "block": published.get("block"),
        "extrinsic_index": published.get("extrinsic_index"),
        "block_hash": published.get("block_hash"),
        "direct_intent": marker,
        "calculation_evidence": reference,
        "applied_weights_readback": published.get("applied_weights_readback"),
        "submitted_weights_u16": marker["vector"],
        "successor_kind": "burn-zero",
    }
    durable_write(path, encoded(payload))
    return path


def same_day_zero(adapter, day, epoch, before_send):
    """Publish the retained pre-burn vector when burn has been set back to 0.

    Returns a status string when this day is a burn publication, or None when
    the caller should keep the normal refresh path.
    """
    if getattr(adapter, "burn_allocation_bps", 0) != OFF:
        return None
    directory = adapter._submitted_record_dir
    existing = confirmed_transition(directory, day)
    if existing is not None:
        return None
    from .submitted_record import submitted_record_path
    path = submitted_record_path(directory, day)
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    marker = record.get("direct_intent")
    if not isinstance(marker, dict):
        return None
    reference = marker.get("calculation_evidence")
    if reference is None:
        return None
    manifest = verify(directory, reference)
    proof = allocation_of(artifact(directory, manifest, "context"))
    if proof is None or proof.get("bps") != ENABLED:
        return None
    vector = {int(uid): weight for uid, weight in marker["vector"].items()}
    recompute(directory, reference, vector)
    pre = {int(uid): weight for uid, weight in proof["pre_burn_u16"].items()}
    # The maintenance attempt is recorded by the caller's before_send. A chain
    # waiting period must return before that, or this epoch cannot try again.
    current = int(adapter.current_block())
    last = int(adapter.last_update(adapter.uid, adapter.netuid))
    if current - last < int(adapter.rate_limit(adapter.netuid)):
        return "awaiting_rate_limit"
    removed = wrap_allocation(
        directory,
        reference,
        pre,
        {
            "bps": OFF,
            "destination": None,
            "pre_burn_u16": proof["pre_burn_u16"],
            "limits": None,
            "replaces": reference["sha256"],
        },
    )
    adapter._calculation_reference = removed
    before_send()
    adapter.publish_direct(
        pre,
        marker["height"],
        source_day=day,
        successor={"kind": "burn-zero", "replaces": reference["sha256"]},
    )
    return "burn_cleared"
