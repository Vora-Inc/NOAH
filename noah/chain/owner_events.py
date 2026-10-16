"""Find subnet-owner transactions in decoded block events.

Match the configured subnet and event shape explicitly. Attribution uses chain
facts rather than operator labels; ambiguous or malformed evidence is refused."""

from dataclasses import dataclass

from .sources import Refusal
from .ss58 import NotAnAddress, public_key_of

_logger_name = __name__

# StakeTransferred: an unnamed six-tuple (macros/events.rs:293-303, spec 450).
_TRANSFER_FIELDS = 6
_T_ORIGIN, _T_DESTINATION, _T_HOTKEY, _T_FROM_NET, _T_TO_NET, _T_AMOUNT = range(6)

# StakeAndHotkeyTransferred: a seven-field named struct (macros/events.rs:752-769).
_NAMED_TRANSFER_FIELDS = (
    "origin_coldkey",
    "destination_coldkey",
    "origin_hotkey",
    "destination_hotkey",
    "origin_netuid",
    "destination_netuid",
    "amount",
)

_BALANCE_TRANSFER_FIELDS = frozenset({"from", "to", "amount"})


class NotAnOwnerEvent(Refusal):
    """An event that cannot be read as an owner-coldkey transfer, in a way
    that is not simply "no"."""


@dataclass(frozen=True)
class OwnerTransfer:
    """One event, in one block, that moved value where the subnet owner's
    own coldkey was a party.

    ``kind`` is one of ``"balance_transfer"``, ``"stake_transfer"``,
    ``"stake_and_hotkey_transfer"`` -- which of the three findable event
    shapes this row came from (see module docstring).

    ``direction`` is ``"out"`` when the owner's coldkey gave value up,
    ``"in"`` when the owner's coldkey received it, or ``"self"`` on the one
    degenerate case where the owner's own coldkey is on both sides of the
    same event (a transfer to itself) -- real, not filtered out, since it is
    still a real event naming the owner's coldkey.

    ``counterparty`` is the OTHER coldkey in the movement, in the exact
    string the chain sent (not ``public_key_of``'s normalised return, the
    same choice ``deposits.py``'s own ``coldkey``/``origin`` fields make,
    for the same reason: a caller comparing this against an address it
    already holds needs that string back). Equal to the owner's own address
    text when ``direction == "self"``.

    ``amount_rao`` and ``amount_denomination`` -- see the module docstring's
    own "what amount means" section. ``amount_denomination`` is always
    either ``"balance"`` (a real, literal currency movement) or
    ``"tao_equivalent_stake_value"`` (a stake-transfer event's own ``amount``
    field -- NEVER the alpha that moved).

    ``origin_netuid``/``destination_netuid`` and ``origin_hotkey``/
    ``destination_hotkey`` are ``None`` for a ``balance_transfer`` (that
    event carries neither) and set for the two stake-transfer kinds. For
    ``stake_transfer`` specifically, ``origin_hotkey`` and
    ``destination_hotkey`` are always equal -- that event never moves the
    hotkey, only ``stake_and_hotkey_transfer`` does.

    ``block``/``event_ordinal``/``extrinsic_index``/``block_hash`` identify
    exactly where this row came from, the same discipline ``deposits.py``'s
    own ``Deposit`` applies and for the same reason: the position in the
    block's own event list is what tells two transactions in one block
    apart, and it is read from the record, never counted from ``events``
    (see ``_ordinal_of``/``_extrinsic_of`` below).
    """

    kind: str
    direction: str
    counterparty: str
    amount_rao: int
    amount_denomination: str
    block: int
    event_ordinal: int
    extrinsic_index: int
    block_hash: str
    origin_netuid: int = None
    destination_netuid: int = None
    origin_hotkey: str = None
    destination_hotkey: str = None


def _key(value, what):
    try:
        return public_key_of(value)
    except NotAnAddress as exc:
        raise NotAnOwnerEvent(f"{what}: {exc}") from exc


def _whole(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAnOwnerEvent(
            f"{what} came back as {value!r}, which is not a decoded whole number."
        )
    if value < 0:
        raise NotAnOwnerEvent(f"{what} is negative ({value}).")
    return value


def _ordinal_of(record, block):
    """Where this event sat in its block -- read from the record, never
    counted. See ``deposits.py``'s identical ``_ordinal_of`` for why
    counting would make this depend on which events the caller passed in."""
    ordinal = record.get("ordinal_in_block")
    if ordinal is None:
        raise NotAnOwnerEvent(
            f"an event at block {block} does not say where it sat in that block. That "
            f"position is what tells two of the owner's transactions in one block apart, "
            f"and it cannot be counted from the events passed in here."
        )
    return _whole(ordinal, "the position of the event in its block")


def _extrinsic_of(record, block):
    index = record.get("extrinsic_idx")
    if index is None:
        raise NotAnOwnerEvent(
            f"an event at block {block} carries no extrinsic behind it. A transaction is "
            f"made by somebody sending one, and that index is real evidence of which."
        )
    return _whole(index, "the extrinsic this event came from")


def _direction_and_counterparty(owner_key, first, first_key, second, second_key):
    """Whether the owner's coldkey is on neither/one/both sides of a
    two-party event, and who the other party is.

    Returns ``None`` when the owner is on neither side (the caller's cue to
    skip this event entirely -- most events in most blocks). Otherwise
    ``(direction, counterparty_text)``.
    """
    owner_is_first = first_key == owner_key
    owner_is_second = second_key == owner_key
    if not owner_is_first and not owner_is_second:
        return None
    if owner_is_first and owner_is_second:
        return "self", first
    if owner_is_first:
        return "out", second
    return "in", first


def owner_transfer_events_in_block(events, owner_coldkey, block, block_hash):
    """Every event in this block that moved value where ``owner_coldkey``
    was a party, across the three findable shapes this module reads (see
    module docstring). Returns a list of :class:`OwnerTransfer`, in the
    order the events occurred (``events``' own order, which is the block's
    real event order -- never re-sorted).

    ``events`` is the block's decoded events, each carrying its own
    ``ordinal_in_block`` -- the exact same shape ``LiveRunnerChain.events``
    (``noah/chain/live.py:906``) returns and ``deposits_in_block`` already
    consumes; pass its return value straight through.

    Refuses (:class:`NotAnOwnerEvent`) on a structurally malformed event of
    one of the three recognised shapes -- an unrecognised field set, a
    wrong field count, an address that does not decode -- the same
    discipline every reader in this package applies. An event of any OTHER
    module/kind is silently skipped, exactly like ``deposits.py`` skips
    every non-``SubtensorModule`` event: this function does not know, and
    does not need to know, about the rest of a block's activity.
    """
    owner_key = _key(owner_coldkey, "the owner's coldkey")
    _whole(block, "the block height")
    if not isinstance(block_hash, str) or not block_hash.strip():
        raise NotAnOwnerEvent(f"the block hash came back as {block_hash!r}, not usable text.")
    if not isinstance(events, (list, tuple)):
        raise NotAnOwnerEvent(
            f"a block's events came in as {events!r}, which is not a list of events."
        )

    rows = []
    for record in events:
        if not isinstance(record, dict):
            raise NotAnOwnerEvent(
                f"an entry in block {block}'s events is {record!r}, which is not an event "
                f"record."
            )
        module = record.get("module_id")
        event_id = record.get("event_id")

        if module == "Balances" and event_id == "Transfer":
            attributes = record.get("attributes")
            if not isinstance(attributes, dict) or set(attributes) != _BALANCE_TRANSFER_FIELDS:
                raise NotAnOwnerEvent(
                    f"a Balances.Transfer at block {block} carried {attributes!r}, not the "
                    f"{{'from', 'to', 'amount'}} shape expected for this event."
                )
            from_key = _key(attributes["from"], "a Balances.Transfer's sender")
            to_key = _key(attributes["to"], "a Balances.Transfer's recipient")
            outcome = _direction_and_counterparty(
                owner_key, attributes["from"], from_key, attributes["to"], to_key
            )
            if outcome is None:
                continue
            direction, counterparty = outcome
            rows.append(
                OwnerTransfer(
                    kind="balance_transfer",
                    direction=direction,
                    counterparty=counterparty,
                    amount_rao=_whole(attributes["amount"], "a Balances.Transfer amount"),
                    amount_denomination="balance",
                    block=block,
                    event_ordinal=_ordinal_of(record, block),
                    extrinsic_index=_extrinsic_of(record, block),
                    block_hash=block_hash,
                )
            )
            continue

        if module == "SubtensorModule" and event_id == "StakeTransferred":
            attributes = record.get("attributes")
            if not isinstance(attributes, (list, tuple)) or len(attributes) != _TRANSFER_FIELDS:
                raise NotAnOwnerEvent(
                    f"a StakeTransferred event at block {block} carried {attributes!r}. This "
                    f"event is declared with exactly {_TRANSFER_FIELDS} unnamed fields."
                )
            origin = attributes[_T_ORIGIN]
            destination = attributes[_T_DESTINATION]
            origin_key = _key(origin, "a StakeTransferred event's origin coldkey")
            destination_key = _key(destination, "a StakeTransferred event's destination coldkey")
            outcome = _direction_and_counterparty(
                owner_key, origin, origin_key, destination, destination_key
            )
            if outcome is None:
                continue
            direction, counterparty = outcome
            hotkey = attributes[_T_HOTKEY]
            _key(hotkey, "a StakeTransferred event's hotkey")
            rows.append(
                OwnerTransfer(
                    kind="stake_transfer",
                    direction=direction,
                    counterparty=counterparty,
                    amount_rao=_whole(
                        attributes[_T_AMOUNT], "a StakeTransferred event's amount"
                    ),
                    amount_denomination="tao_equivalent_stake_value",
                    origin_netuid=_whole(
                        attributes[_T_FROM_NET], "a StakeTransferred event's origin netuid"
                    ),
                    destination_netuid=_whole(
                        attributes[_T_TO_NET], "a StakeTransferred event's destination netuid"
                    ),
                    origin_hotkey=hotkey,
                    destination_hotkey=hotkey,
                    block=block,
                    event_ordinal=_ordinal_of(record, block),
                    extrinsic_index=_extrinsic_of(record, block),
                    block_hash=block_hash,
                )
            )
            continue

        if module == "SubtensorModule" and event_id == "StakeAndHotkeyTransferred":
            attributes = record.get("attributes")
            if not isinstance(attributes, dict):
                raise NotAnOwnerEvent(
                    f"a StakeAndHotkeyTransferred event at block {block} carried "
                    f"{attributes!r}. This event is declared as a struct with named fields."
                )
            missing = [name for name in _NAMED_TRANSFER_FIELDS if name not in attributes]
            if missing:
                raise NotAnOwnerEvent(
                    f"a StakeAndHotkeyTransferred event at block {block} is missing "
                    f"{missing}."
                )
            origin = attributes["origin_coldkey"]
            destination = attributes["destination_coldkey"]
            origin_key = _key(
                origin, "a StakeAndHotkeyTransferred event's origin coldkey"
            )
            destination_key = _key(
                destination, "a StakeAndHotkeyTransferred event's destination coldkey"
            )
            outcome = _direction_and_counterparty(
                owner_key, origin, origin_key, destination, destination_key
            )
            if outcome is None:
                continue
            direction, counterparty = outcome
            origin_hotkey = attributes["origin_hotkey"]
            destination_hotkey = attributes["destination_hotkey"]
            _key(origin_hotkey, "a StakeAndHotkeyTransferred event's origin hotkey")
            _key(destination_hotkey, "a StakeAndHotkeyTransferred event's destination hotkey")
            rows.append(
                OwnerTransfer(
                    kind="stake_and_hotkey_transfer",
                    direction=direction,
                    counterparty=counterparty,
                    amount_rao=_whole(
                        attributes["amount"], "a StakeAndHotkeyTransferred event's amount"
                    ),
                    amount_denomination="tao_equivalent_stake_value",
                    origin_netuid=_whole(
                        attributes["origin_netuid"],
                        "a StakeAndHotkeyTransferred event's origin netuid",
                    ),
                    destination_netuid=_whole(
                        attributes["destination_netuid"],
                        "a StakeAndHotkeyTransferred event's destination netuid",
                    ),
                    origin_hotkey=origin_hotkey,
                    destination_hotkey=destination_hotkey,
                    block=block,
                    event_ordinal=_ordinal_of(record, block),
                    extrinsic_index=_extrinsic_of(record, block),
                    block_hash=block_hash,
                )
            )
            continue

    return rows
