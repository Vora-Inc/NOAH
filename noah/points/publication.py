"""Ordinary points publication safety, independent of starter allocation."""

from ..core.calculation_record import artifact, verify
from ..core.publish_intent import PublicationStatusUnknown
from .mode import check_publication_mode
from .outage import authorize


def check_before_send(adapter, reference, vector, *, refresh=None):
    """Bind amounts to finalized hotkey AND coldkey immediately before sending."""
    from ..core.report_runtime import check_before_send as check_reports
    check_reports(adapter, reference, vector, **({} if refresh is None else {"refresh": refresh}))
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
    from ..core.burn_publication import recheck_for_send
    burn_uid = None
    if any(vector.get(uid, 0) for uid in adapter.owner_uids()):
        burn_uid = recheck_for_send(adapter, reference, vector)
        if burn_uid is None:
            raise PublicationStatusUnknown("ordinary publication would pay an owner seat")
    paid = {uid for uid, weight in vector.items() if weight and uid != burn_uid}
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
