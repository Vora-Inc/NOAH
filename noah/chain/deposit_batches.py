"""Strict receipt boundaries for flat transfer/remark and multisig transfer batches.

Only Utility.batch_all, optionally directly inside Multisig.as_multi, is
supported. ItemCompleted events delimit each call; list order alone never
pairs receipts. Unrecognized evidence returns None so the existing reader retains its
conservative behavior. A recognized transfer-only multisig batch with incomplete
evidence refuses; it must never fall back to aggregation.
"""

import hashlib

from ..deposits.owner_entitlement import (
    OwnerNotEntitled,
    OwnerReadUnreadable,
    confirm_owner_entitled_at_block,
)
from .ss58 import NotAnAddress, address_of, public_key_of


class _Unsupported(ValueError):
    pass


def _require(condition):
    if not condition:
        raise _Unsupported


def _name(call):
    return call.get("call_module"), call.get("call_function")


def _args(call):
    entries = call.get("call_args")
    _require(isinstance(entries, (list, tuple)))
    result = {}
    for entry in entries:
        _require(isinstance(entry, dict) and set(entry) >= {"name", "value"})
        name = entry["name"]
        _require(isinstance(name, str) and name not in result)
        result[name] = entry["value"]
    return result


def _number(value):
    _require(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= (1 << 64) - 1)
    return value


def _events(events, module, name):
    return [e for e in events if (e.get("module_id"), e.get("event_id")) == (module, name)]


def _unit_success(result):
    """Accept SCALE Result<(), _> success, including the live SDK's empty tuple.

    JSON recordings turn that tuple into a list. Only unit-valued Ok is success;
    populated values, errors and extra keys remain unsupported.
    """
    if not isinstance(result, dict) or set(result) != {"Ok"}:
        return False
    value = result["Ok"]
    return value is None or (type(value) in (list, tuple) and len(value) == 0)


def read_batch(
    extrinsic,
    events,
    *,
    ark_coldkey,
    designated_validator_hotkey,
    block,
    block_hash,
    parent_block_hash,
    owner_source,
):
    """Return separately identified receipts, or None without granting credit."""
    transfer_only = _transfer_only_multisig(
        extrinsic, events, ark_coldkey=ark_coldkey,
        designated_validator_hotkey=designated_validator_hotkey,
        block=block, block_hash=block_hash,
    )
    if transfer_only is not None:
        return transfer_only
    try:
        return _read_batch(
            extrinsic,
            events,
            ark_coldkey=ark_coldkey,
            designated_validator_hotkey=designated_validator_hotkey,
            block=block,
            block_hash=block_hash,
            parent_block_hash=parent_block_hash,
            owner_source=owner_source,
        )
    except (_Unsupported, NotAnAddress, KeyError, TypeError, ValueError, AttributeError):
        return None


def _read_batch(
    extrinsic,
    events,
    *,
    ark_coldkey,
    designated_validator_hotkey,
    block,
    block_hash,
    parent_block_hash,
    owner_source,
):
    # Imported here because deposits invokes this optional path after loading.
    from .deposits import Deposit

    call = extrinsic["call"]
    multisig = _name(call) == ("Multisig", "as_multi")
    if multisig:
        call = _args(call)["call"]
    _require(_name(call) == ("Utility", "batch_all"))
    batch_args = _args(call)
    _require(set(batch_args) == {"calls"})
    calls = batch_args["calls"]
    _require(isinstance(calls, (list, tuple)) and len(calls) >= 2 and len(calls) % 2 == 0)
    # Preserve the established single, directly signed receipt path exactly.
    _require(multisig or len(calls) > 2)

    ordinals = [_number(e["ordinal_in_block"]) for e in events]
    _require(ordinals == sorted(set(ordinals)))
    indices = {_number(e["extrinsic_idx"]) for e in events}
    _require(len(indices) == 1)
    index = next(iter(indices))
    _require(len(_events(events, "System", "ExtrinsicSuccess")) == 1)
    _require(not _events(events, "System", "ExtrinsicFailed"))
    utility = [e for e in events if e.get("module_id") == "Utility"]
    _require(
        [e["event_id"] for e in utility] == ["ItemCompleted"] * len(calls) + ["BatchCompleted"]
    )
    completed = utility[-1]["ordinal_in_block"]
    _require(completed < _events(events, "System", "ExtrinsicSuccess")[0]["ordinal_in_block"])

    executions = _events(events, "Multisig", "MultisigExecuted")
    if multisig:
        _require(len(executions) == 1)
        executed = executions[0]
        attrs = executed["attributes"]
        _require(_unit_success(attrs["result"]))
        _require(
            completed
            < executed["ordinal_in_block"]
            < _events(events, "System", "ExtrinsicSuccess")[0]["ordinal_in_block"]
        )
        coldkey = attrs["multisig"]
    else:
        _require(not executions)
        coldkey = extrinsic["address"]
    cold = public_key_of(coldkey)
    ark = public_key_of(ark_coldkey)

    # Every staking/remark event must belong to a completed item. Never ignore
    # an extra arrival outside the supported calls or merge it into a receipt.
    segments = []
    current = []
    for event in events:
        if (event.get("module_id"), event.get("event_id")) == ("Utility", "ItemCompleted"):
            segments.append(current)
            current = []
        elif event.get("module_id") == "SubtensorModule" or (
            event.get("module_id"),
            event.get("event_id"),
        ) == ("System", "Remarked"):
            current.append(event)
    _require(not current and len(segments) == len(calls))

    observations = []
    for offset in range(0, len(calls), 2):
        transfer, remark = calls[offset : offset + 2]
        _require(_name(transfer) == ("SubtensorModule", "transfer_stake"))
        _require(_name(remark) == ("System", "remark_with_event"))
        args = _args(transfer)
        _require(
            set(args)
            == {
                "destination_coldkey",
                "hotkey",
                "origin_netuid",
                "destination_netuid",
                "alpha_amount",
            }
        )
        _require(public_key_of(args["destination_coldkey"]) == ark)
        position = public_key_of(args["hotkey"])
        netuid = _number(args["destination_netuid"])
        _require(_number(args["origin_netuid"]) == netuid)
        amount = _number(args["alpha_amount"])
        _require(0 < amount < (1 << 64) - 1)  # MAX is a move-all sentinel, not a fixed amount.
        _require(netuid <= 65535)

        stake_events, remark_events = segments[offset : offset + 2]
        _require(
            all(
                e["module_id"] == "SubtensorModule"
                and e["event_id"] in {"StakeRemoved", "StakeAdded", "StakeTransferred"}
                for e in stake_events
            )
        )
        added = _events(stake_events, "SubtensorModule", "StakeAdded")
        transferred = _events(stake_events, "SubtensorModule", "StakeTransferred")
        _require(len(added) == len(transferred) == 1)
        _require(added[0]["ordinal_in_block"] < transferred[0]["ordinal_in_block"])
        fields = added[0]["attributes"]
        _require(isinstance(fields, (list, tuple)) and len(fields) == 6)
        _require(public_key_of(fields[0]) == ark and public_key_of(fields[1]) == position)
        _require(_number(fields[3]) == amount and _number(fields[4]) == netuid)
        moved = transferred[0]["attributes"]
        _require(isinstance(moved, (list, tuple)) and len(moved) == 6)
        _require(public_key_of(moved[0]) == cold and public_key_of(moved[1]) == ark)
        _require(public_key_of(moved[2]) == position)
        _require(_number(moved[3]) == _number(moved[4]) == netuid)
        # The last transfer-event field is TAO equivalent, not received alpha.
        _number(moved[5])
        removed = _events(stake_events, "SubtensorModule", "StakeRemoved")
        _require(len(removed) <= 1)
        if removed:
            spent = removed[0]["attributes"]
            _require(isinstance(spent, (list, tuple)) and len(spent) == 6)
            _require(public_key_of(spent[0]) == cold and public_key_of(spent[1]) == position)
            _require(_number(spent[3]) == amount and _number(spent[4]) == netuid)
            _require(removed[0]["ordinal_in_block"] < added[0]["ordinal_in_block"])

        _require(len(remark_events) == 1)
        event = remark_events[0]
        _require((event["module_id"], event["event_id"]) == ("System", "Remarked"))
        remark_args = _args(remark)
        _require(set(remark_args) == {"remark"})
        value = remark_args["remark"]
        _require(isinstance(value, str) and value.startswith("0x"))
        raw = bytes.fromhex(value[2:])
        _require(len(raw) == 32)
        _require(public_key_of(event["attributes"]["sender"]) == cold)
        _require(
            event["attributes"]["hash"] == "0x" + hashlib.blake2b(raw, digest_size=32).hexdigest()
        )
        observations.append((added[0], amount, netuid, fields[1], address_of(raw)))

    receipts = []
    for event, amount, netuid, validator, miner in observations:
        reason = None
        owner = None
        try:
            confirm_owner_entitled_at_block(
                owner_source,
                hotkey=miner,
                coldkey=coldkey,
                block_hash=parent_block_hash,
            )
            owner = coldkey
        except (OwnerNotEntitled, OwnerReadUnreadable) as exc:
            reason = str(exc)
        if public_key_of(validator) != public_key_of(designated_validator_hotkey):
            reason = (
                f"staked to {validator}, not the designated Ark Validator "
                f"{designated_validator_hotkey} -- does not qualify for mining credit"
            )
        receipts.append(
            Deposit(
                coldkey=coldkey,
                carried_by="StakeTransferred",
                amount_rao=amount,
                token_netuid=netuid,
                block=block,
                event_ordinal=event["ordinal_in_block"],
                extrinsic_index=index,
                block_hash=block_hash,
                hotkey=validator,
                origin_hotkey=miner if owner is not None else None,
                owner_at_deposit=owner,
                unattributed_reason=reason,
            )
        )
    return receipts


def _transfer_only_multisig(extrinsic, events, *, ark_coldkey,
                            designated_validator_hotkey, block, block_hash):
    """Read flat, successful multisig transfer-only batches without aggregation.

    This separate shape has no remarks. Named transfers retain their chain-stated
    origin hotkey; a plain transfer names no miner. Once recognized, incomplete
    evidence refuses instead of falling back to the old aggregated receipt path.
    """
    try:
        outer = extrinsic["call"]
        if _name(outer) != ("Multisig", "as_multi"):
            return None
        batch = _args(outer)["call"]
        if _name(batch) != ("Utility", "batch_all"):
            return None
        batch_args = _args(batch)
        calls = batch_args["calls"]
        if not isinstance(calls, (list, tuple)) or len(calls) < 2:
            return None
        supported = {("SubtensorModule", "transfer_stake"),
                     ("SubtensorModule", "transfer_stake_and_hotkey")}
        if not all(_name(call) in supported for call in calls):
            return None
        # Other batches retain their existing path, including transfers that
        # do not all arrive at this Ark. Do not turn unrelated calls into a
        # new whole-block refusal merely because their wrapper matches.
        ark = public_key_of(ark_coldkey)
        if not all(public_key_of(_args(call)["destination_coldkey"]) == ark for call in calls):
            return None
    except (_Unsupported, NotAnAddress, KeyError, TypeError, ValueError, AttributeError):
        return None

    from .deposits import Deposit, NotADepositEvent, UNNAMED_REASON_PREFIX

    try:
        _require(set(batch_args) == {"calls"})
        ordinals = [_number(event["ordinal_in_block"]) for event in events]
        _require(ordinals == sorted(set(ordinals)))
        indices = {_number(event["extrinsic_idx"]) for event in events}
        _require(len(indices) == 1)
        index = next(iter(indices))
        success = _events(events, "System", "ExtrinsicSuccess")
        _require(len(success) == 1 and not _events(events, "System", "ExtrinsicFailed"))
        utility = [event for event in events if event.get("module_id") == "Utility"]
        _require([event["event_id"] for event in utility] == ["ItemCompleted"]*len(calls)+["BatchCompleted"])
        executed = _events(events, "Multisig", "MultisigExecuted")
        _require(len(executed) == 1)
        execution = executed[0]["attributes"]
        _require(_unit_success(execution["result"]))
        # The live reader supplies the SDK-decoded inner call hash. Bind the
        # executed call to that same decoded batch, never to the outer signer.
        inner_hash = batch["call_hash"]
        _require(isinstance(inner_hash, str) and len(inner_hash) == 66 and inner_hash.startswith("0x"))
        _require(len(bytes.fromhex(inner_hash[2:])) == 32 and execution["call_hash"] == inner_hash)
        _require(utility[-1]["ordinal_in_block"] < executed[0]["ordinal_in_block"] < success[0]["ordinal_in_block"])
        giver = execution["multisig"]
        cold, ark = public_key_of(giver), public_key_of(ark_coldkey)
        segments, current = [], []
        for event in events:
            name = event.get("module_id"), event.get("event_id")
            if name == ("Utility", "ItemCompleted"):
                segments.append(current)
                current = []
            elif event.get("module_id") == "SubtensorModule" or name == ("System", "Remarked"):
                current.append(event)
        _require(not current and len(segments) == len(calls))
        receipts = []
        for call, segment in zip(calls, segments):
            named = _name(call)[1] == "transfer_stake_and_hotkey"
            args = _args(call)
            fields = {"destination_coldkey", "origin_netuid", "destination_netuid", "alpha_amount"}
            fields |= {"origin_hotkey", "destination_hotkey"} if named else {"hotkey"}
            _require(set(args) == fields and public_key_of(args["destination_coldkey"]) == ark)
            origin_hotkey = args["origin_hotkey"] if named else args["hotkey"]
            destination_hotkey = args["destination_hotkey"] if named else args["hotkey"]
            origin, destination = public_key_of(origin_hotkey), public_key_of(destination_hotkey)
            token = _number(args["destination_netuid"])
            _require(_number(args["origin_netuid"]) == token and token <= 65535)
            amount = _number(args["alpha_amount"])
            _require(0 < amount < (1 << 64)-1)
            carried_by = "StakeAndHotkeyTransferred" if named else "StakeTransferred"
            if segment and (segment[0].get("module_id"), segment[0].get("event_id")) == ("SubtensorModule", "BasketDeposited"):
                basket = segment[0]["attributes"]
                _require(isinstance(basket, dict) and set(basket) == {"hotkey", "shares", "tao"})
                _require(public_key_of(basket["hotkey"]) == origin)
                _number(basket["shares"])
                _number(basket["tao"])
                # Root-pool accounting may precede the transfer. It is neither
                # an Ark arrival nor a source of the receipt's alpha amount.
                segment = segment[1:]
            _require([(e["module_id"],e["event_id"]) for e in segment] ==
                     [("SubtensorModule",name) for name in ("StakeRemoved","StakeAdded",carried_by)])
            removed, added, transferred = [event["attributes"] for event in segment]
            _require(isinstance(removed,(list,tuple)) and len(removed) == 6)
            _require(isinstance(added,(list,tuple)) and len(added) == 6)
            _require(public_key_of(removed[0]) == cold and public_key_of(removed[1]) == origin)
            _require(_number(removed[3]) == amount and _number(removed[4]) == token)
            _require(public_key_of(added[0]) == ark and public_key_of(added[1]) == destination)
            # The arriving alpha is explicit in StakeAdded. Transfer event
            # amounts are TAO equivalents and must not replace that alpha.
            _require(_number(added[3]) == amount and _number(added[4]) == token)
            if named:
                _require(set(transferred) == {"origin_coldkey","destination_coldkey","origin_hotkey",
                         "destination_hotkey","origin_netuid","destination_netuid","amount"})
                _require(public_key_of(transferred["origin_coldkey"]) == cold and
                         public_key_of(transferred["destination_coldkey"]) == ark)
                _require(public_key_of(transferred["origin_hotkey"]) == origin and
                         public_key_of(transferred["destination_hotkey"]) == destination)
                _require(_number(transferred["origin_netuid"]) == token and
                         _number(transferred["destination_netuid"]) == token)
                _number(transferred["amount"])
            else:
                _require(isinstance(transferred,(list,tuple)) and len(transferred) == 6)
                _require(public_key_of(transferred[0]) == cold and public_key_of(transferred[1]) == ark)
                _require(public_key_of(transferred[2]) == destination)
                _require(_number(transferred[3]) == token and _number(transferred[4]) == token)
                _number(transferred[5])
            reason = None if named else (
                UNNAMED_REASON_PREFIX + " transfer-only batch names no miner hotkey; received but uncredited"
            )
            if destination != public_key_of(designated_validator_hotkey):
                reason = (f"staked to {added[1]}, not the designated Ark Validator "
                          f"{designated_validator_hotkey} -- does not qualify for mining credit")
                if not named:
                    reason += ". It also names no miner hotkey."
            receipts.append(Deposit(coldkey=giver, carried_by=carried_by,
                amount_rao=added[3], token_netuid=token, block=block,
                event_ordinal=segment[1]["ordinal_in_block"], extrinsic_index=index,
                block_hash=block_hash, hotkey=added[1], origin_hotkey=transferred["origin_hotkey"] if named else None,
                owner_at_deposit=giver if named else None, unattributed_reason=reason))
        return receipts
    except (_Unsupported, NotAnAddress, KeyError, TypeError, ValueError, AttributeError):
        raise NotADepositEvent(
            f"transfer-only multisig batch at block {block} has incomplete or conflicting "
            "call/completion/arrival/transfer evidence; individual receipts cannot be proved"
        ) from None
