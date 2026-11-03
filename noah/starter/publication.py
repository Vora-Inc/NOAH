"""Starter proof retention and verification at the actual publication boundary.

A starter refresh links the immutable ordinary calculation; it never overwrites
that day's score file. Recovery verifies the original inputs again. Chain sends
still use ChainAdapter's existing durable intent and finalized receipt path.
"""

import json
from pathlib import Path

from ..core.calculation_record import encoded, retain, verify
from ..core.publish_intent import PublicationStatusUnknown
from .allocation import digest, preview


def artifact(directory, manifest, name):
    # Callers obtain the manifest through verify(), which checks these paths/hash.
    return json.loads((Path(directory) / manifest["artifacts"][name]["path"]).read_text())


def retain_overlay(
    directory, source_reference, raw, previous=None, *, fallback=False, removed_uids=()
):
    source = verify(directory, source_reference)
    fixture = artifact(directory, source, "fixture")
    score = artifact(directory, source, "agreed_score")
    context = artifact(directory, source, "context")
    if "starter_overlay" in context:
        raise PublicationStatusUnknown("starter source must be the ordinary daily calculation")
    if fixture != raw["fixture"]:
        raise PublicationStatusUnknown("starter fixture differs from verified ordinary calculation")
    result = preview(raw, previous)
    from .second_opinion import check

    check(raw, previous, result)
    if encoded(result["ordinaryOutput"]) != encoded(score):
        raise PublicationStatusUnknown("starter ordinary output differs from twin-agreed score")
    vector = result["ordinaryFallbackWeightsU16"] if fallback else result["proposedWeightsU16"]
    removed = sorted(set(removed_uids))
    if removed and (
        not fallback or any(type(uid) is not int or str(uid) not in vector for uid in removed)
    ):
        raise PublicationStatusUnknown("invalid fallback identity removals")
    vector = {uid: (0 if int(uid) in removed else amount) for uid, amount in vector.items()}
    if not sum(vector.values()):
        raise PublicationStatusUnknown("cannot publish an empty starter/fallback vector")
    proof = dict(
        schema="noah-starter-publication/1",
        source=source_reference,
        input=raw,
        previous=previous,
        resultSha256=digest(result),
        mode="ordinary-fallback" if fallback else "starter-overlay",
        removedUids=removed,
    )
    return retain(
        directory,
        fixture=fixture,
        agreed_text=encoded(score).decode(),
        vector={int(k): v for k, v in vector.items()},
        context={**context, "starter_overlay": proof},
        canonical_day=False,
    )


def verify_overlay(directory, reference):
    """Return proof for a verified overlay, or None for an ordinary calculation."""
    manifest = verify(directory, reference)
    context = artifact(directory, manifest, "context")
    proof = context.get("starter_overlay")
    if proof is None:
        return None
    if set(proof) != {
        "schema",
        "source",
        "input",
        "previous",
        "resultSha256",
        "mode",
        "removedUids",
    }:
        raise PublicationStatusUnknown("malformed starter publication proof")
    if proof["schema"] != "noah-starter-publication/1" or proof["mode"] not in (
        "ordinary-fallback",
        "starter-overlay",
    ):
        raise PublicationStatusUnknown("unsupported starter publication policy")
    source = verify(directory, proof["source"])
    source_context = artifact(directory, source, "context")
    if (
        "starter_overlay" in source_context
        or {k: v for k, v in context.items() if k != "starter_overlay"} != source_context
    ):
        raise PublicationStatusUnknown("starter calculation configuration differs from source")
    fixture = artifact(directory, source, "fixture")
    score = artifact(directory, source, "agreed_score")
    result = preview(proof["input"], proof["previous"])
    from .second_opinion import check

    check(proof["input"], proof["previous"], result)
    vector = (
        result["ordinaryFallbackWeightsU16"]
        if proof["mode"] == "ordinary-fallback"
        else result["proposedWeightsU16"]
    )
    removed = proof["removedUids"]
    if (
        not isinstance(removed, list)
        or any(type(uid) is not int for uid in removed)
        or removed != sorted(set(removed))
        or any(str(uid) not in vector for uid in removed)
        or (removed and proof["mode"] != "ordinary-fallback")
    ):
        raise PublicationStatusUnknown("invalid fallback identity removals")
    vector = {uid: (0 if int(uid) in removed else amount) for uid, amount in vector.items()}
    if not sum(vector.values()):
        raise PublicationStatusUnknown("fallback has no eligible ordinary weight")
    if (
        proof["input"]["fixture"] != fixture
        or result["ordinaryOutput"] != score
        or artifact(directory, manifest, "fixture") != fixture
        or artifact(directory, manifest, "agreed_score") != score
        or proof["resultSha256"] != digest(result)
        or manifest["publishable_weights_u16"] != vector
        or manifest["day"] != source["day"]
        or manifest["netuid"] != source["netuid"]
        or manifest["genesis_hash"] != source["genesis_hash"]
    ):
        raise PublicationStatusUnknown("starter vector or evidence does not recompute")
    return proof


def check_before_send(adapter, reference, vector):
    proof = verify_overlay(adapter._submitted_record_dir, reference)
    if proof is None:
        return
    manifest = verify(adapter._submitted_record_dir, reference)
    if {str(k): v for k, v in vector.items()} != manifest["publishable_weights_u16"]:
        raise PublicationStatusUnknown("requested vector differs from starter proof")
    raw = proof["input"]
    snap = raw["snapshot"]
    if raw["schema"] != "noah-starter-epoch-preview/4":
        raise PublicationStatusUnknown("synthetic-clock previews cannot be published")
    chain = adapter.runner_chain
    head = chain.head_block()
    if (
        snap["genesisHash"] != chain.genesis_hash()
        or snap["netuid"] != adapter.netuid
        or chain.block_hash(snap["finalizedBlock"]) != snap["finalizedHash"]
    ):
        raise PublicationStatusUnknown("starter chain identity changed")
    if head < snap["finalizedBlock"]:
        raise PublicationStatusUnknown("starter observation is ahead of finalized head")
    if proof["mode"] == "starter-overlay":
        info = chain.starter_epoch_info(head)
        next_block = info["LastEpochBlock"] + info["Tempo"]
        if info["PendingEpochAt"]:
            next_block = min(next_block, info["PendingEpochAt"])
        if (
            head >= snap["nextEpochBlock"]
            or info["LastEpochBlock"] != snap["epoch"]
            or next_block != snap["nextEpochBlock"]
        ):
            raise PublicationStatusUnknown(
                "starter allocation expired or schedule changed before signing"
            )
    live = chain.seat_hotkeys(head)
    expected = {int(uid): hot for uid, hot in raw["scoredHotkeys"].items()}
    checked = (
        expected
        if proof["mode"] == "starter-overlay"
        else {uid: hot for uid, hot in expected.items() if vector.get(uid, 0)}
    )
    if any(live.get(uid) != hot for uid, hot in checked.items()):
        raise PublicationStatusUnknown("starter seat identities changed before signing")
    owners = chain.hotkey_owners(list(expected.values()), head)
    owner_uids = adapter.owner_uids()
    for row in snap["seats"]:
        if row["uid"] in proof["removedUids"] and (
            live.get(row["uid"]) == row["hotkey"]
            and owners.get(row["hotkey"]) == row["coldkey"]
            and row["uid"] not in owner_uids
        ):
            raise PublicationStatusUnknown("fallback would remove an unchanged ordinary identity")
    if any(
        owners.get(row["hotkey"]) != row["coldkey"]
        for row in snap["seats"]
        if row["uid"] in checked
    ):
        raise PublicationStatusUnknown("starter wallet ownership changed before signing")
    if any(vector.get(uid, 0) for uid in adapter.owner_uids()):
        raise PublicationStatusUnknown("starter publication would pay owner seat")
