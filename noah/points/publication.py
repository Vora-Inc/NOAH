"""Ordinary points publication safety, independent of starter allocation."""

from ..core.calculation_record import artifact, verify
from ..core.publish_intent import PublicationStatusUnknown
from .mode import check_publication_mode
from .outage import authorize


def check_before_send(adapter, reference, vector):
    """Bind amounts to finalized hotkey AND coldkey immediately before sending."""
    check_publication_mode(adapter, reference)
    root = adapter._submitted_record_dir
    manifest = verify(root, reference)
    context = artifact(root, manifest, "context")
    chain = adapter.runner_chain
    head = chain.head_block()
    authorize(adapter, reference, chain.block_timestamp(head))
    if (
        manifest["netuid"] != adapter.netuid
        or manifest["genesis_hash"] != chain.genesis_hash()
        or {str(k): v for k, v in vector.items()} != manifest["publishable_weights_u16"]
        or type(context.get("to_block")) is not int
        or head < context["to_block"]
        or chain.block_hash(context["to_block"]) != context.get("to_block_hash")
    ):
        raise PublicationStatusUnknown("ordinary calculation chain identity or vector changed")
    hotkeys = context.get("scored_hotkeys")
    coldkeys = context.get("scored_coldkeys")
    if not isinstance(hotkeys, dict) or not isinstance(coldkeys, dict):
        raise PublicationStatusUnknown("ordinary calculation lacks retained miner identities")
    paid = {uid for uid, weight in vector.items() if weight}
    live = chain.seat_hotkeys(head)
    if any(not hotkeys.get(str(uid)) or live.get(uid) != hotkeys[str(uid)] for uid in paid):
        raise PublicationStatusUnknown("miner identities changed before signing")
    owners = chain.hotkey_owners([hotkeys[str(uid)] for uid in paid], head)
    if any(
        not coldkeys.get(str(uid)) or owners.get(hotkeys[str(uid)]) != coldkeys[str(uid)]
        for uid in paid
    ):
        raise PublicationStatusUnknown("miner wallet ownership changed before signing")
    if paid.intersection(adapter.owner_uids()):
        raise PublicationStatusUnknown("ordinary publication would pay an owner seat")
