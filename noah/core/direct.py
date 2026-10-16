"""Exact direct-publication evidence and restart reconciliation.

Runtime 475 d1718c99 stores Null weights unchanged; Yuma max-upscales them.
Confirm the decoded extrinsic, its scoped events, and the complete stored vector
at one finalized block. Event presence or equal inherited storage alone is never
proof of this send. These reads do not prove a subsequent Null epoch payout.
"""

from ..diagnostics import debug

from ..chain.ss58 import NotAnAddress, public_key_of
from .proxy_route import INNER_OK, classify_inner
from .reader import ChainUnreadable, unwrap
from .rescale import predict_stored


def same_address(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return public_key_of(left) == public_key_of(right)
    except (NotAnAddress, TypeError, ValueError):
        return left == right


def _args(call):
    args = call.get("call_args")
    if isinstance(args, dict):
        return args
    if isinstance(args, list):
        return {arg["name"]: arg["value"] for arg in args}
    raise ChainUnreadable("direct transaction call arguments could not be decoded")


def _address(value):
    if isinstance(value, dict):
        return value.get("Id")
    return value


def matches_call(extrinsic, intent):
    """Match the signer and full call, including proxy identity and optional nonce."""
    if not isinstance(extrinsic, dict):
        raise ChainUnreadable("direct transaction could not be decoded")
    if not same_address(_address(extrinsic.get("address")), intent["signer"]):
        return False
    nonce = intent.get("account_nonce_at_head")
    if nonce is not None and extrinsic.get("nonce") != nonce:
        return False
    call = extrinsic.get("call")
    if not isinstance(call, dict):
        raise ChainUnreadable("direct transaction has no decoded call")
    if intent["route"] == "proxy":
        if (call.get("call_module"), call.get("call_function")) != ("Proxy", "proxy"):
            return False
        args = _args(call)
        if not same_address(_address(args.get("real")), intent["hotkey"]):
            return False
        if args.get("force_proxy_type") is not None:
            return False
        call = args.get("call")
        if not isinstance(call, dict):
            raise ChainUnreadable("proxy inner call could not be decoded")
    elif not same_address(intent["signer"], intent["hotkey"]):
        return False
    if (call.get("call_module"), call.get("call_function")) != (
        "SubtensorModule",
        "set_mechanism_weights",
    ):
        return False
    args = _args(call)
    vector = {int(uid): weight for uid, weight in intent["vector"].items()}
    return (
        args.get("netuid") == intent["netuid"]
        and args.get("mecid") == intent["mecid"]
        and args.get("version_key") == intent["version_key"]
        and args.get("dests") == sorted(vector)
        and args.get("weights") == [vector[uid] for uid in sorted(vector)]
    )


def _event_index(record):
    index = record.get("extrinsic_idx")
    if type(index) is int:
        return index
    phase = record.get("phase")
    if isinstance(phase, dict) and type(phase.get("ApplyExtrinsic")) is int:
        return phase["ApplyExtrinsic"]
    return None


def _weights_event(record, intent):
    event = record.get("event", {})
    if (event.get("module_id"), event.get("event_id")) != ("SubtensorModule", "WeightsSet"):
        return False
    attrs = event.get("attributes")
    # This event has unnamed tuple fields in runtime 475.
    if not isinstance(attrs, (list, tuple)) or len(attrs) != 2:
        raise ChainUnreadable("WeightsSet attributes could not be decoded")
    return tuple(attrs) == (intent["netuid"] + 4096 * intent["mecid"], intent["uid"])


def confirm(reader, intent, result, *, finalized_chain):
    """Confirm against the adapter's existing dual-sourced finalized chain.

    The pinned Bittensor 11.1 SDK has no ``finalized_block`` property. Its
    current/best head is not a finality substitute. Reuse the ingestion readers'
    finalized head and agreed block hash before checking the SDK's decoded call.
    """
    block, index = result.get("block"), result.get("extrinsic_index")
    debug(
        "checking direct confirmation day=%s block=%s extrinsic_index=%s netuid=%s",
        intent.get("day"), block, index, intent.get("netuid"),
    )
    if type(block) is not int or block < 0 or type(index) is not int or index < 0:
        raise ChainUnreadable("direct submission lacks an inclusion block and extrinsic index")
    if finalized_chain.head_block() < block:
        raise ChainUnreadable("direct inclusion block is not finalized")
    info = reader.client.block_info(block)
    if info is None or info.number != block or not info.hash:
        raise ChainUnreadable("direct inclusion block could not be read")
    if finalized_chain.block_hash(block) != info.hash:
        raise ChainUnreadable("direct inclusion block differs from the finalized sources")
    if result.get("block_hash") and result["block_hash"] != info.hash:
        raise ChainUnreadable("direct inclusion block hash changed")
    if index >= len(info.extrinsics) or not matches_call(info.extrinsics[index], intent):
        raise ChainUnreadable("included transaction differs from the intended direct publication")
    hotkey = reader._query("Keys", [intent["netuid"], intent["uid"]], block=block)
    if not same_address(hotkey, intent["hotkey"]):
        raise ChainUnreadable("Noah no longer holds the intended publication seat")
    events = unwrap(reader.client.query(("System", "Events"), None, block=block))
    if not isinstance(events, list):
        raise ChainUnreadable("direct inclusion events could not be read")
    scoped = [
        record for record in events if isinstance(record, dict) and _event_index(record) == index
    ]
    outer = [record.get("event", {}) for record in scoped]
    if any(
        e.get("module_id") == "System" and e.get("event_id") == "ExtrinsicFailed" for e in outer
    ):
        raise ChainUnreadable("direct transaction failed on chain")
    if not any(
        e.get("module_id") == "System" and e.get("event_id") == "ExtrinsicSuccess" for e in outer
    ):
        raise ChainUnreadable("direct transaction has no scoped outer success")
    if intent["route"] == "proxy" and classify_inner(scoped)[0] != INNER_OK:
        raise ChainUnreadable("direct transaction has no scoped ProxyExecuted success")
    if not any(_weights_event(record, intent) for record in scoped):
        raise ChainUnreadable("no matching WeightsSet event for this direct transaction")
    consensus = reader.epoch_consensus(intent["netuid"], block=block)
    vector = {int(uid): weight for uid, weight in intent["vector"].items()}
    uids = sorted(vector)
    values = [vector[uid] for uid in uids]
    expected = values if consensus == "Null" else predict_stored(values).stored
    stored = reader.weights_content_for_seat(
        intent["netuid"] + 4096 * intent["mecid"], intent["uid"], block
    )
    if sorted(tuple(pair) for pair in stored) != list(zip(uids, expected)):
        raise ChainUnreadable(
            "complete stored weight vector differs from the intended runtime representation"
        )
    return {
        "confirmed": True,
        "block": block,
        "block_hash": info.hash,
        "extrinsic_index": index,
        "netuid": intent["netuid"],
        "mecid": intent["mecid"],
        "uid": intent["uid"],
        "hotkey": intent["hotkey"],
        "consensus": consensus,
        "weights": dict(zip(uids, expected)),
    }


def reconcile(reader, intent, *, finalized_chain):
    """Search an uncertain send without ever resubmitting it.

    The pre-send account nonce plus complete call identifies the candidate. It is
    not a pool nonce or a signed-extrinsic mortality proof. A missing match always
    leaves the intent open for operator reconciliation, including after the scan
    limit; it never authorizes a retry.
    """
    result = intent.get("result")
    if result and result.get("block") is not None and result.get("extrinsic_index") is not None:
        return confirm(reader, intent, result, finalized_chain=finalized_chain)
    start = intent.get("head_at_send")
    if type(start) is not int or type(intent.get("account_nonce_at_head")) is not int:
        raise ChainUnreadable("uncertain direct send lacks a pre-send head or signer nonce")
    head = finalized_chain.head_block()
    # Bounded RPC work. An older unresolved send needs an explicit operator readback.
    if head - start > 4096:
        raise ChainUnreadable("uncertain direct send exceeds the 4096-block reconciliation window")
    for block in range(start + 1, head + 1):
        info = reader.client.block_info(block)
        if info is None:
            raise ChainUnreadable("uncertain direct send block could not be read")
        for index, extrinsic in enumerate(info.extrinsics):
            if matches_call(extrinsic, intent):
                return confirm(
                    reader,
                    intent,
                    {"block": block, "extrinsic_index": index, "block_hash": info.hash},
                    finalized_chain=finalized_chain,
                )
    raise ChainUnreadable("uncertain direct send has no exact finalized match; do not resend")
