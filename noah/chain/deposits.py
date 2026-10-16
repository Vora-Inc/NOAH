"""Decode Ark receipts from caller-supplied block events.

Callers are responsible for supplying sufficiently settled history.

A receipt transfers ownership of alpha to the Ark coldkey. Staking to a
validator hotkey alone does not transfer ownership. Join the transfer and
StakeAdded events from the same extrinsic: the transfer identifies the sender,
while StakeAdded supplies the arriving alpha amount and position. A missing or
ambiguous pair is refused rather than assigned a guessed owner or amount.

Keep each event's original block position; it identifies the receipt regardless
of how callers filter the event list. Miner attribution requires a named miner
hotkey and, where applicable, an ownership check at the deposit block. Receipts
with failed attribution or the wrong designated validator remain recorded but
do not qualify for mining credit. Token identity is retained in Deposit; callers
must apply the intended subnet policy before using a receipt as NOAH credit.
"""

import hashlib
import logging
from dataclasses import dataclass

from ..deposits.owner_entitlement import (
    OwnerNotEntitled,
    OwnerReadUnreadable,
    confirm_owner_entitled_at_block,
)
from .sources import Refusal
from .store import UNNAMED_REASON_PREFIX
from .ss58 import NotAnAddress, address_of, public_key_of

_logger = logging.getLogger(__name__)

# Both events are declared as unnamed tuples, so the count is the only
# confirmation available that an event was understood at all.
# StakeTransferred is a TUPLE of six unnamed fields (macros/events.rs:296-303).
_TRANSFER_FIELDS = 6
_T_ORIGIN, _T_DESTINATION, _T_HOTKEY, _T_FROM_NET, _T_TO_NET, _T_TAO = range(6)

# StakeAndHotkeyTransferred is a STRUCT of seven NAMED fields
# (macros/events.rs:752-769). It is a different event with a different shape and
# it is read by name, never by position. See the module docstring for why the
# positional reading of it looks like it works.
_NAMED_TRANSFER_FIELDS = (
    "origin_coldkey",
    "destination_coldkey",
    "origin_hotkey",
    "destination_hotkey",
    "origin_netuid",
    "destination_netuid",
    "amount",
)

_STAKE_FIELDS = 6
_S_COLDKEY, _S_HOTKEY, _S_TAO, _S_ALPHA, _S_NETUID, _S_FEE = range(6)


class NotADepositEvent(Refusal):
    """Events that cannot be read as deposits, in a way that is not simply "no"."""


# ---- utility.batch_all + system.remark_with_event (issue #112, comment
# 5751446658, 2026-09-20) --------------------------------------------------
#
# A deposit's staking hotkey (``Deposit.hotkey`` above) never changes on
# ``transfer_stake`` -- it stays whichever validator the miner originally
# delegated to -- so it cannot by itself say which miner a deposit credits
# once two miners share one wallet and one validator hotkey. The fix bundles
# ``transfer_stake`` with a ``system.remark_with_event`` naming the target
# miner's OWN hotkey inside one atomic ``utility.batch_all``.
#
# This is read from the block's own DECODED EXTRINSICS, not from events --
# ``system.remark_with_event``'s own event carries only a hash of the
# remark, never its bytes (see ``noah.chain.sources``'s whole reason to
# exist: a fact is only used once it can be read directly, never inferred).
# Callers that do not pass ``extrinsics`` to ``deposits_in_block`` get
# exactly today's behaviour: this whole path is skipped and every deposit
# comes back exactly as it always has, ``origin_hotkey``/``owner_at_deposit``
# both ``None``.

#: The exact (call_module, call_function) pair identifying one bundled call
#: as the wrapping batch, the attribution remark, or the transfer itself.
_UTILITY_BATCH_ALL = ("Utility", "batch_all")
_REMARK_WITH_EVENT = ("System", "remark_with_event")
_TRANSFER_STAKE = ("SubtensorModule", "transfer_stake")
#: A real deposit's remark is the miner hotkey's raw AccountId32 public key
#: -- always exactly this many bytes, never text and never padded.
_REMARK_HOTKEY_BYTES = 32


class AmbiguousDepositBatch(NotADepositEvent):
    """A ``batch_all`` bundled more than one ``remark_with_event`` call, or
    more than one ``transfer_stake`` call. Which remark pairs with which
    transfer is not on the chain, and pairing them by order would be a
    guess about somebody's permanent contribution -- the same discipline
    this module already applies to two transfers landing in one plain
    extrinsic (see ``NotADepositEvent``'s own "more than one transfer"
    refusal above)."""


class MalformedDepositRemark(NotADepositEvent):
    """A remark bundled beside a real deposit's ``transfer_stake`` was not a
    usable 32-byte miner hotkey, or its own committed bytes did not match
    the hash its paired ``System.Remarked`` event attests to."""


class MalformedOriginHotkey(NotADepositEvent):
    """A ``StakeAndHotkeyTransferred`` event's own ``origin_hotkey`` field
    did not decode as a real chain address.

    Deliberately not :class:`MalformedDepositRemark`: nothing here is a
    remark. ``StakeAndHotkeyTransferred``'s ``origin_hotkey`` is a named
    field the chain itself emits on ``transfer_stake_and_hotkey`` -- the
    call only the actual position owner can invoke on their own stake, so
    the field is a chain-enforced fact, never a self-reported claim needing
    a paired ``System.Remarked`` hash to confirm it was not tampered with
    in flight. A different failure needs its own name rather than
    borrowing one that describes a different mechanism."""


@dataclass(frozen=True)
class Deposit:
    """One contribution into the Ark, as observed.

    ``event_ordinal`` is where the stake event sat in the block's event list,
    and it is what tells two contributions in one block apart. The extrinsic
    index repeats within a block and is kept as evidence rather than as an
    identifier.

    ``carried_by`` is which of the two transfer events brought it. Both are
    contributions and both are credited: the destination coldkey decides
    ownership, and the hotkey only decides which validator the position is
    delegated to. But StakeTransferred keeps the hotkey and
    StakeAndHotkeyTransferred changes it, and the version-one flow moves an
    already-staked position without offering a validator choice anywhere, so a
    contribution arriving by the second event did not come through our own flow.
    That is not a problem and the chain is the truth either way. It is something
    an audit should be able to see without re-reading the chain, and it cannot
    unless the row remembers.

    ``hotkey`` is the position's hotkey, off the same ``StakeAdded`` arrival
    every other field here comes from. Scoring never asked for it, so it sat
    decoded and unused until a caller needed to confirm not just that the Ark
    received alpha from a coldkey, but that a specific prepared position was
    what moved: ``noah/deposits/verify.py``'s comparison is against a full
    transaction, hotkey included, and a deposit that cannot name one cannot be
    held against it.

    ``origin_hotkey`` and ``owner_at_deposit`` (issue #112, comment
    5751446658, 2026-09-20; issue #135, plan posted 2026-09-21 ~01:05 UTC):
    ``hotkey`` above is the position's VALIDATOR hotkey, which never changes
    on a ``StakeTransferred`` deposit and so cannot say which of several
    miners sharing one coldkey earned this specific contribution.
    ``origin_hotkey``'s SOURCE depends on ``carried_by``, not on batching:

    - ``StakeTransferred``: ``origin_hotkey`` is the miner hotkey a paired
      ``utility.batch_all`` + ``system.remark_with_event`` actually names,
      and ``owner_at_deposit`` is the coldkey confirmed (at the deposit's
      own parent block, never live) to have owned it -- ``coldkey`` above
      and ``owner_at_deposit`` are always equal whenever ``origin_hotkey``
      is set here, because a deposit is only ever attributed once that
      entitlement is confirmed
      (``noah.deposits.owner_entitlement.confirm_owner_entitled_at_block``).
    - ``StakeAndHotkeyTransferred``: ``origin_hotkey`` is read directly off
      that event's OWN ``origin_hotkey`` field, whether or not this
      extrinsic happens to be wrapped in a ``batch_all``, and no remark is
      ever consulted for this event type. ``owner_at_deposit`` is set to
      ``coldkey`` with no separate entitlement check: the chain itself only
      lets the actual position owner invoke ``transfer_stake_and_hotkey`` on
      their own stake, so the field is a chain-enforced fact, not a
      self-reported claim needing independent verification.

    Both stay ``None`` for a deposit that names no miner hotkey (see
    ``unattributed_reason`` below), or when the caller does not pass
    ``extrinsics``/``parent_block_hash``/``owner_source`` to
    ``deposits_in_block`` at all.

    ``unattributed_reason`` records a failed attribution: a batch this module attempted to attribute and
    could not -- an ambiguous batch, a malformed remark, or an unentitled
    signer -- is still a real deposit; the alpha genuinely moved to the
    Ark, and dropping the row would leave the Ark's own holdings grown with
    nothing accounting for the increase. Such a row is recorded exactly
    like any other (``coldkey``/``amount_rao``/``hotkey``/... are the real,
    already-known facts off the underlying ``StakeAdded``/``StakeTransferred``
    pair, unaffected by whether attribution succeeded), with
    ``origin_hotkey``/``owner_at_deposit`` left ``None`` and
    ``unattributed_reason`` set to the specific refusal's own message --
    "left uncredited with a clear explanation," a recorded fact, never a
    vanished one. The attribution rule extends this to every deposit
    that names no miner hotkey at all: a plain ``transfer_stake``, a
    ``batch_all`` with no remark the reader can see, or any other top-level
    wrapper the reader does not read through. It is recorded with a
    deterministic reason and credits nobody. ``None`` only for a deposit that
    names its miner hotkey (and went to the designated validator). See
    ``deposits_in_block``'s own docstring.
    """

    coldkey: str
    amount_rao: int
    token_netuid: int
    block: int
    event_ordinal: int
    extrinsic_index: int
    block_hash: str
    carried_by: str
    hotkey: str = None
    origin_hotkey: str = None
    owner_at_deposit: str = None
    unattributed_reason: str = None


def _key(value, what):
    try:
        return public_key_of(value)
    except NotAnAddress as exc:
        raise NotADepositEvent(f"{what}: {exc}") from exc


def _whole(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotADepositEvent(
            f"{what} came back as {value!r}, which is not a decoded whole number."
        )
    if value < 0:
        raise NotADepositEvent(f"{what} is negative ({value}).")
    return value


def _fields(record, count, block, what):
    attributes = record.get("attributes")
    if not isinstance(attributes, (list, tuple)) or len(attributes) != count:
        raise NotADepositEvent(
            f"{what} at block {block} carried {attributes!r}. This event is declared with "
            f"exactly {count} unnamed fields, so the count is the only confirmation that it "
            f"was understood. A different one means a different event, or a runtime that "
            f"changed under us."
        )
    return attributes


def _named_fields(record, block):
    """The seven named fields of StakeAndHotkeyTransferred, by name.

    Read by name and never by position, because the runtime gave these fields
    names and the names are the contract. The order is an implementation detail
    that costs nothing to change and would break this silently if relied on.

    The trap is specific and it is worth stating: ``destination_coldkey`` is the
    second field of BOTH transfer events. So a decoder that simply widened the
    positional reading to accept seven fields would find the Ark correctly, would
    produce deposits, and would read the giving hotkey where it meant the
    receiving one and the destination subnet where it meant the amount. A wrong
    number, from a field holding a real number, on a money path.
    """
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        raise NotADepositEvent(
            f"a hotkey-and-stake transfer at block {block} carried {attributes!r}. This event "
            f"is declared as a struct with named fields, so a list is either a different "
            f"event or a runtime that changed under us. It is not read positionally."
        )
    missing = [name for name in _NAMED_TRANSFER_FIELDS if name not in attributes]
    if missing:
        raise NotADepositEvent(
            f"a hotkey-and-stake transfer at block {block} is missing {missing}. Every field "
            f"this event declares must be present before any of them is trusted; a decoder "
            f"that copes with a missing name is a decoder reading a shape it does not know."
        )
    return attributes


def _named_origin_hotkey(value, *, extrinsic_index, block):
    """Validate a ``StakeAndHotkeyTransferred`` event's own ``origin_hotkey``
    field is a real chain address, keeping the original string -- not the
    public key ``public_key_of`` compares addresses by underneath -- for
    storage on the returned ``Deposit``, the same discipline this module
    already applies to ``hotkey`` off ``StakeAdded`` (see that field's own
    comment in the scanning loop below).

    Raises :class:`MalformedOriginHotkey`, not the plain
    :class:`NotADepositEvent` a structural decode failure elsewhere in this
    module raises: by the time this runs, the deposit itself is already
    known real (the coldkeys and amount already passed every earlier
    check), so a malformed ``origin_hotkey`` is this ONE extrinsic's own
    isolated attribution problem -- caught the same way
    :class:`MalformedDepositRemark` already is, never a reason to stop the
    whole block's scan.
    """
    try:
        public_key_of(value)
    except NotAnAddress as exc:
        raise MalformedOriginHotkey(
            f"the origin_hotkey field of the StakeAndHotkeyTransferred behind extrinsic "
            f"{extrinsic_index} of block {block} did not decode as a real chain address "
            f"({value!r}): {exc}"
        ) from exc
    return value


def _ordinal_of(record, block):
    """Where this event sat in its block, taken from the record and never counted.

    Counting would make it a property of the list the caller passed in. It is
    the identity of a contribution, so it has to be a property of the block.
    """
    ordinal = record.get("ordinal_in_block")
    if ordinal is None:
        raise NotADepositEvent(
            f"an event at block {block} does not say where it sat in that block. That "
            f"position is what tells two contributions made in one transaction apart, and it "
            f"cannot be counted from the events passed in here, because that would make it "
            f"depend on which ones the caller chose to pass."
        )
    return _whole(ordinal, "the position of the event in its block")


def _extrinsic_of(record, block):
    index = record.get("extrinsic_idx")
    if index is None:
        raise NotADepositEvent(
            f"an event at block {block} carries no extrinsic behind it. A contribution is "
            f"made by somebody sending a transaction, and the transaction is what joins the "
            f"two halves of one."
        )
    return _whole(index, "the extrinsic this event came from")


@dataclass(frozen=True)
class _OrdinaryDeposit:
    """Not a batch_all at all, or a batch_all with no remark beside its
    transfer -- today's shape, unchanged. Carries nothing because there is
    nothing new to carry."""


@dataclass(frozen=True)
class _AttributedDeposit:
    """A batch_all's remark was decoded, cross-checked, and its named
    hotkey's owner confirmed to be the depositing coldkey -- both fields are
    real chain facts, never invented."""

    origin_hotkey: str
    owner_at_deposit: str


def _call_leg_args(leg, block):
    """One bundled call's own ``call_args`` -- a list of ``{name, value}``
    entries in ``async_substrate_interface``'s own decoded shape -- as a
    plain ``{name: value}`` mapping. Refuses rather than reading blindly, the
    same discipline ``_named_fields`` above already applies to a top-level
    event's attributes."""
    args = leg.get("call_args")
    if not isinstance(args, (list, tuple)):
        raise NotADepositEvent(
            f"a call bundled in a batch_all at block {block} carried {args!r} as its own "
            f"call_args, not a list of {{'name': ..., 'value': ...}} entries."
        )
    named = {}
    for entry in args:
        if not isinstance(entry, dict) or "name" not in entry or "value" not in entry:
            raise NotADepositEvent(
                f"a call bundled in a batch_all at block {block} carried a call_arg {entry!r} "
                f"missing its own name or value."
            )
        named[entry["name"]] = entry["value"]
    return named


def _batch_all_legs(extrinsic_record, block):
    """The bundled calls of one ``utility.batch_all`` extrinsic, or ``None``
    if ``extrinsic_record`` is not a batch_all at all -- the ordinary,
    still-valid shape this function leaves completely alone."""
    if not isinstance(extrinsic_record, dict):
        raise NotADepositEvent(
            f"an extrinsic at block {block} is {extrinsic_record!r}, not a decoded record."
        )
    call = extrinsic_record.get("call")
    if not isinstance(call, dict):
        return None
    if (call.get("call_module"), call.get("call_function")) != _UTILITY_BATCH_ALL:
        return None
    args = call.get("call_args")
    if not isinstance(args, (list, tuple)) or not args or not isinstance(args[0], dict):
        raise NotADepositEvent(
            f"a utility.batch_all extrinsic at block {block} carries no usable call_args for "
            f"its own bundled calls."
        )
    calls = args[0].get("value")
    if not isinstance(calls, (list, tuple)):
        raise NotADepositEvent(
            f"a utility.batch_all extrinsic at block {block}'s own bundled calls came back as "
            f"{calls!r}, not a list."
        )
    return calls


def _top_level_call_name(extrinsics, extrinsic_index):
    """``(module, function)`` of the top-level call at ``extrinsic_index``, or
    ``None`` if it cannot be read. Never raises: it only words the reason an
    unnamed deposit is reported, so an odd shape must not stop the scan."""
    try:
        record = extrinsics[extrinsic_index]
        call = record.get("call")
        module, function = call.get("call_module"), call.get("call_function")
    except (AttributeError, IndexError, KeyError, TypeError):
        return None
    if not isinstance(module, str) or not isinstance(function, str):
        return None
    return (module, function)


_UNNAMED_ADVICE = (
    "Send with transfer_stake_and_hotkey, or in a top-level Utility.batch_all with a remark "
    "naming your miner hotkey."
)


def _unnamed_deposit_reason(extrinsics, extrinsic_index, block):
    """Deterministic reason text for a deposit that names no miner hotkey
    (rule B, decision #338): it credits nobody and is reported as unattributed.
    The text is stored and compared on rescan, so it must be a pure function
    of its inputs."""
    where = f"extrinsic {extrinsic_index} of block {block}"
    prefix = UNNAMED_REASON_PREFIX
    name = _top_level_call_name(extrinsics, extrinsic_index)
    if name == _TRANSFER_STAKE:
        return (
            f"{prefix} this was a plain transfer_stake ({where}). A plain "
            f"transfer_stake carries no miner hotkey, so the deposit credits nobody. "
            f"{_UNNAMED_ADVICE}"
        )
    if name == _UTILITY_BATCH_ALL:
        return (
            f"{prefix} the batch_all at {where} has no remark_with_event the "
            f"reader can see (a remark inside Proxy.proxy or another wrapper is not read), so "
            f"the deposit credits nobody. {_UNNAMED_ADVICE}"
        )
    label = f"{name[0]}.{name[1]}" if name else "an unreadable top-level call"
    return (
        f"{prefix} the transfer was sent inside {label} ({where}), a wrapper the "
        f"reader does not read through, so any remark inside it is not read and the deposit "
        f"credits nobody. Send directly with transfer_stake_and_hotkey, or in a top-level "
        f"Utility.batch_all with the remark. (Proxy.proxy around transfer_stake_and_hotkey is "
        f"also accepted.)"
    )


# ---- Proxy.proxy Ark self-funding during its own construction
# (issue #112-16) ------------------------------------------------------------
#
# A Proxy.proxy call whose real account is the Ark and whose nested call is
# SubtensorModule.add_stake creates stake owned by the Ark without transferring
# an existing position from a miner. Its StakeAdded event has no paired giver.
# Recognize this decoded shape separately from contribution transfers; do not
# infer a contribution from an arrival event alone.
#
# The check below is keyed to `real == ark_coldkey`, compared by public key
# like every other address comparison in this module -- never to today's
# specific authorized proxy signer (the extrinsic's own `address`), because
# whichever account currently holds proxy authority for the Ark can change
# without this stopping being the Ark's own construction stake. It is also
# never keyed to a block range: block numbers are specific to one chain's own
# construction history and would not hold for a fresh chain's identical
# pattern on its own construction day.
_PROXY_PROXY = ("Proxy", "proxy")
_ADD_STAKE = ("SubtensorModule", "add_stake")


def _proxy_ark_construction_stake(extrinsic_record, ark_key, block):
    """Whether ``extrinsic_record`` is the Ark's own construction-stake
    shape: ``Proxy.proxy`` whose own ``real`` is the Ark's coldkey, wrapping
    ``SubtensorModule.add_stake`` -- see this section's own comment above for
    the real block this closes and why it is not a deposit at all.

    Returns ``False`` for absolutely anything else, including a shape that is
    malformed or unexpected in a way that would otherwise deserve its own
    refusal -- deliberately. This function only ever narrows what the
    caller's own "nothing says who gave it up" refusal lets through
    unchanged; it never raises on its own account, because a mis-shaped
    extrinsic here is no more informative than no shape at all, and the
    caller's existing refusal already covers both identically. Never used to
    weaken that refusal for anything this does not positively, structurally
    match.
    """
    if not isinstance(extrinsic_record, dict):
        return False
    call = extrinsic_record.get("call")
    if not isinstance(call, dict):
        return False
    if (call.get("call_module"), call.get("call_function")) != _PROXY_PROXY:
        return False
    try:
        args = _call_leg_args(call, block)
    except NotADepositEvent:
        return False
    real = args.get("real")
    if not isinstance(real, str):
        return False
    try:
        if _key(real, "a Proxy.proxy call's own real account") != ark_key:
            return False
    except NotADepositEvent:
        return False
    inner = args.get("call")
    if not isinstance(inner, dict):
        return False
    return (inner.get("call_module"), inner.get("call_function")) == _ADD_STAKE


def _batch_all_attribution(
    extrinsics,
    extrinsic_index,
    *,
    block,
    coldkey,
    remarked_hash_by_extrinsic,
    owner_source,
    parent_block_hash,
):
    """Attribute one already-confirmed deposit (at ``extrinsic_index``) to a
    specific miner hotkey, from its own extrinsic's ``utility.batch_all``
    bundling -- see ``deposits_in_block``'s own docstring for the full
    contract this implements.

    Returns :class:`_OrdinaryDeposit` for a deposit not wrapped in this
    shape at all; the caller (``deposits_in_block``) records such a deposit as
    unattributed under rule B (#338). Returns
    :class:`_AttributedDeposit` once a remark's naming of a miner hotkey has
    been decoded, cross-checked against its own paired event, and confirmed
    (at ``parent_block_hash``) to have actually been owned by ``coldkey``.

    Raises :class:`AmbiguousDepositBatch` or :class:`MalformedDepositRemark`
    for an on-chain batch this cannot cleanly read as one attribution, and
    lets :class:`~noah.deposits.owner_entitlement.OwnerNotEntitled`/
    :class:`OwnerReadUnreadable` propagate unchanged from ``confirm_owner_
    entitled_at_block`` -- ``deposits_in_block`` is what decides what happens
    to those four (see its own docstring: logged and skipped, never silently
    turned into a guess). Any OTHER :class:`NotADepositEvent` raised here
    (an extrinsic index the block's own decoded extrinsics do not carry, a
    call record that is not even a mapping, ...) is left to propagate and
    stop the whole block's scan exactly like every structural refusal
    elsewhere in this module -- that shape means the caller handed in
    ``extrinsics`` that do not actually describe ``events``' own block,
    which is not a per-extrinsic, on-chain condition to skip past.
    """
    if not isinstance(extrinsics, (list, tuple)) or not (0 <= extrinsic_index < len(extrinsics)):
        raise NotADepositEvent(
            f"extrinsic {extrinsic_index} moved alpha into the Ark at block {block}, but the "
            f"block's own decoded extrinsics do not carry an entry at that index -- nothing "
            f"about the batch it may or may not be wrapped in can be checked."
        )
    legs = _batch_all_legs(extrinsics[extrinsic_index], block)
    if legs is None:
        return _OrdinaryDeposit()

    remark_legs = []
    transfer_legs = []
    for leg in legs:
        if not isinstance(leg, dict):
            raise NotADepositEvent(
                f"a call bundled in the batch_all at extrinsic {extrinsic_index} of block "
                f"{block} is {leg!r}, not a decoded record."
            )
        shape = (leg.get("call_module"), leg.get("call_function"))
        if shape == _REMARK_WITH_EVENT:
            remark_legs.append(leg)
        elif shape == _TRANSFER_STAKE:
            transfer_legs.append(leg)

    if not remark_legs:
        # Zero remarks beside this transfer is today's ordinary shape --
        # nothing here to attribute, nothing wrong either.
        return _OrdinaryDeposit()

    if len(remark_legs) != 1 or len(transfer_legs) != 1:
        raise AmbiguousDepositBatch(
            f"the batch_all at extrinsic {extrinsic_index} of block {block} bundles "
            f"{len(remark_legs)} remark_with_event call(s) and {len(transfer_legs)} "
            f"transfer_stake call(s); a real deposit's attribution needs exactly one of each, "
            f"and pairing them any other way would be a guess about somebody's permanent "
            f"contribution."
        )

    remark_value = _call_leg_args(remark_legs[0], block).get("remark")
    if not isinstance(remark_value, str) or not remark_value.startswith("0x"):
        raise MalformedDepositRemark(
            f"the remark_with_event at extrinsic {extrinsic_index} of block {block} did not "
            f"carry a 0x-prefixed hex remark ({remark_value!r})."
        )
    try:
        raw = bytes.fromhex(remark_value[2:])
    except ValueError as exc:
        raise MalformedDepositRemark(
            f"the remark at extrinsic {extrinsic_index} of block {block} is not valid hex: {exc}"
        ) from exc
    if len(raw) != _REMARK_HOTKEY_BYTES:
        raise MalformedDepositRemark(
            f"the remark at extrinsic {extrinsic_index} of block {block} is {len(raw)} raw "
            f"bytes, not the {_REMARK_HOTKEY_BYTES}-byte AccountId32 a real deposit's miner "
            f"hotkey needs -- refusing to guess a hotkey out of the wrong number of bytes."
        )

    committed_hash = remarked_hash_by_extrinsic.get(extrinsic_index)
    if committed_hash is None:
        raise MalformedDepositRemark(
            f"extrinsic {extrinsic_index} of block {block} carries a remark_with_event call "
            f"but no paired System.Remarked event, so nothing confirms the remark's bytes "
            f"were actually the ones committed."
        )
    real_hash = "0x" + hashlib.blake2b(raw, digest_size=32).hexdigest()
    if real_hash != committed_hash:
        raise MalformedDepositRemark(
            f"extrinsic {extrinsic_index} of block {block}: the remark's own bytes hash to "
            f"{real_hash}, but its paired System.Remarked event committed {committed_hash!r} "
            f"-- these must be the same remark and they are not."
        )

    try:
        origin_hotkey = address_of(raw)
    except NotAnAddress as exc:
        raise MalformedDepositRemark(
            f"the remark at extrinsic {extrinsic_index} of block {block} did not decode to a "
            f"real chain address: {exc}"
        ) from exc

    # Propagates OwnerNotEntitled/OwnerReadUnreadable unchanged -- see this
    # function's own docstring for why those are not caught here.
    confirm_owner_entitled_at_block(
        owner_source,
        hotkey=origin_hotkey,
        coldkey=coldkey,
        block_hash=parent_block_hash,
    )
    return _AttributedDeposit(origin_hotkey=origin_hotkey, owner_at_deposit=coldkey)


def deposits_in_block(
    events,
    ark_coldkey,
    block,
    block_hash,
    designated_validator_hotkey,
    *,
    extrinsics=None,
    parent_block_hash=None,
    owner_source=None,
):
    """Every contribution into the Ark in this block, in the order they happened.

    ``events`` is the block's decoded events, each carrying its own
    ``ordinal_in_block``: the position it occupied in the block's full event
    list. That position is the only thing that tells two contributions in one
    block apart, because the extrinsic index repeats within a block and the
    event's own metadata index is the same number for every event of its kind.
    Both were measured on block 8,889,759 and the recording is committed.

    The position is read from the record and never counted from ``events``.
    Counting makes the identity of a contribution depend on which events the
    caller chose to pass in, which is how the same contribution came to have two
    different identities depending on who was asking.

    ``extrinsics``, ``parent_block_hash`` and ``owner_source`` are optional
    and go together (issue #112, comment 5751446658, 2026-09-20): passing
    none of them (the default) reproduces exactly today's behaviour, every
    deposit's ``origin_hotkey``/``owner_at_deposit`` coming back ``None``.
    Passing ``extrinsics`` -- the SAME block's own decoded extrinsics, in
    ``async_substrate_interface``'s own ``extrinsic.value`` shape (each a
    dict with ``address`` and a ``call`` of ``call_module``/``call_function``/
    ``call_args``), indexed identically to how ``events``' own
    ``extrinsic_idx`` already indexes into it -- additionally looks at every
    extrinsic that produced a deposit here for a ``utility.batch_all``
    bundling exactly one ``system.remark_with_event`` (naming, in its raw
    bytes, the miner hotkey being credited) beside exactly one
    ``transfer_stake``. ``parent_block_hash`` (block N-1's hash, N being
    ``block``) and ``owner_source`` (anything offering ``owner_at(hotkey=,
    block_hash=)``, passed straight through to
    ``noah.deposits.owner_entitlement.confirm_owner_entitled_at_block``) are
    then used to confirm the depositing coldkey actually owned that hotkey
    AT THE DEPOSIT'S OWN BLOCK before trusting the remark's attribution --
    never at chain head, for the identical reason that module's own
    docstring gives.

    **The attribution rule: a deposit must name its miner hotkey.**
    When ``extrinsics`` is passed and nothing above attributed the deposit
    (a plain ``transfer_stake``, a ``batch_all`` with no remark the reader can
    see, or any other top-level call such as ``Proxy.proxy``, ``Utility.batch``
    or ``as_derivative`` around a plain transfer), the ``Deposit`` is still
    produced, with ``origin_hotkey``/``owner_at_deposit`` ``None`` and a
    deterministic ``unattributed_reason`` naming the shape ("No miner hotkey
    named: ..."), so it credits nobody. This holds for a single-seat wallet
    too. Credited: every form whose transfer event itself names the miner
    hotkey (``transfer_stake_and_hotkey``, directly or inside ``Proxy.proxy``,
    ``Utility.batch``, ``Multisig`` and the like, with or without a remark --
    the event's own ``origin_hotkey`` is read, not the wrapper), and a
    top-level ``batch_all[transfer_stake, remark]``.
    Without ``extrinsics`` nothing can be read and today's behaviour is
    unchanged.

    **A batch this cannot cleanly attribute never falls back to guessing.**
    Zero remarks beside a real transfer names no miner hotkey, so it is
    recorded unattributed with the rule-B reason above. More than one remark or
    more than one transfer-shaped call in the same batch is
    :class:`AmbiguousDepositBatch` -- which remark pairs with which transfer
    is not on the chain. A remark that is not exactly 32 raw bytes, or whose
    bytes do not hash to what its own paired ``System.Remarked`` event
    committed, is :class:`MalformedDepositRemark`. A depositing coldkey that
    did not actually own the named hotkey at this deposit's own block is
    :class:`~noah.deposits.owner_entitlement.OwnerNotEntitled` (or
    :class:`OwnerReadUnreadable` if the read itself could not be trusted).
    None of these four is ever guessed past -- ``origin_hotkey`` stays
    ``None`` -- but none of them drops the ``Deposit`` either. The real alpha genuinely moved to the Ark
    regardless of whether it could be attributed, and dropping the row
    would leave the Ark's own holdings grown with nothing accounting for
    the increase. The ``Deposit`` for that extrinsic is still produced,
    with every fact the underlying ``StakeAdded``/``StakeTransferred`` pair
    already gives (``coldkey``, ``amount_rao``, ``hotkey``, ...) intact,
    ``origin_hotkey``/``owner_at_deposit`` left ``None``, and
    ``unattributed_reason`` set to the specific refusal's own message --
    "received but unattributed," a recorded fact, never a vanished one.

    **Attribution source depends on which event carried the arrival, not on
    batching (issue #135, plan posted 2026-09-21 ~01:05 UTC).** Everything
    above (``utility.batch_all`` + ``system.remark_with_event``,
    ``_TRANSFER_STAKE``-leg matching, ``AmbiguousDepositBatch``,
    ``MalformedDepositRemark``, ``confirm_owner_entitled_at_block``) is
    exactly and only how a ``StakeTransferred`` deposit gets attributed,
    unchanged. A ``StakeAndHotkeyTransferred`` deposit (``carried_by ==
    "StakeAndHotkeyTransferred"``, emitted by ``transfer_stake_and_hotkey``)
    is never routed through any of that: its own event already names the
    miner in its ``origin_hotkey`` field, so this function reads that field
    directly -- whether or not the extrinsic happens to be wrapped in a
    ``batch_all`` -- and never consults a remark or calls
    ``confirm_owner_entitled_at_block`` for it, because the chain itself
    only lets the actual position owner invoke ``transfer_stake_and_hotkey``
    on their own stake: the field is a chain-enforced fact, not a
    self-reported claim. A malformed ``origin_hotkey`` on this event type is
    :class:`MalformedOriginHotkey` -- deliberately not
    :class:`MalformedDepositRemark`, since nothing here is a remark -- given
    the identical per-extrinsic, never-block-killing, never-dropped
    treatment as the four refusals below.

    Each of the four is still raised, by name, from the attribution step
    itself -- but, UNLIKE every other refusal in this module,
    ``deposits_in_block`` CATCHES it right there rather than letting it
    stop the whole function: a batch_all extrinsic is its own, independent
    transaction, distinct from the other giver/arrival invariants this
    function already enforces across a whole block, and a single malformed
    or unentitled one must not cost every OTHER, cleanly-attributable
    deposit in the same block its recognition. Each is ALSO logged, at
    ``WARNING`` through this module's own logger (``noah.chain.deposits``),
    with the full reason -- visible on stderr by Python's own default
    logging configuration if nothing else has configured a handler, but
    that log line is a courtesy, not the record: ``unattributed_reason`` on
    the returned ``Deposit`` (and, once a caller writes it through
    ``noah.chain.store.Store.record_noah_deposit``, on the stored row) is
    what persists durably; a caller that needs the reason kept must
    persist the ``Deposit`` it already has, not configure logging to catch
    a line that was only ever a courtesy.

    **AS OF ISSUE #112-15, GAP 2: a fifth, older case gets the identical
    treatment.** Before this ticket, an extrinsic whose events carried more
    than one arrival into the Ark (for example, one ``batch_all`` bundling
    two independent ``transfer_stake`` legs from the same signer)
    raised plain :class:`NotADepositEvent` and killed the WHOLE block's
    scan -- unlike the four above, which are each their own extrinsic's
    isolated failure by construction, this one used to fire while the
    shared ``givers``/``arrivals`` dicts were still being built, before this
    function's own per-extrinsic attribution loop (the one that gives the
    four above their isolation) ever runs. For a caller that passes
    ``extrinsics`` (the identical all-or-nothing gate the four refusals
    above already use; a caller that omits it is completely unaffected and
    still gets today's exact hard stop), it no longer raises: every
    arrival for that one extrinsic is summed into a single, real,
    still-verifiably-moved ``amount_rao`` (refusing outright, rather than
    guessing, if the arrivals do not all name the same hotkey/subnet
    position -- summing across two different positions would not be one
    holding), and the ORIGINAL guard's own message becomes this Deposit's
    ``unattributed_reason``, unchanged in wording. No batch_all decode is
    attempted for it: which remark would even pair with which of several
    arrivals is exactly what made this ambiguous in the first place.

    **AS OF ISSUE #112-16: one more case is excluded, not refused, and not
    recorded as a Deposit at all.** An arrival that is not a contribution
    can otherwise hit the "nothing says who gave it up" refusal:
    a proxy signing ``Proxy.proxy(real=<the Ark's own coldkey>, call=SubtensorModule.
    add_stake(...))``, the Ark staking its OWN funds into its OWN position
    rather than receiving a transferred miner position.
    ``add_stake`` is a direct TAO-to-alpha purchase into one position, not a
    transfer between two existing positions, so it never has a paired giver
    event to find -- not a gap in what this module can read, a different
    kind of arrival entirely. For a caller that passes ``extrinsics`` (the
    same all-or-nothing gate as every other newer refusal here; a caller
    that omits it still gets today's exact hard stop, no regression), this
    ONE positively-identified pattern -- confirmed by the extrinsic's own
    decoded structure, never by a block range, which is specific to one
    chain's own construction history -- is excluded outright: no
    :class:`NotADepositEvent`, no ``Deposit`` row, nothing to attribute.
    Anything else that would otherwise hit this same refusal -- a Proxy
    wrapper naming a different ``real``, a Proxy wrapper around a different
    call, or no Proxy wrapper at all -- still raises exactly as before. See
    :func:`_proxy_ark_construction_stake`.

    **``designated_validator_hotkey``: every contribution has to arrive DELEGATED TO THE
    DESIGNATED ARK VALIDATOR to count for mining, checked against
    ``Deposit.hotkey`` -- the position's own validator hotkey off the same
    ``StakeAdded`` arrival every other field here comes from -- never
    against ``coldkey``/``origin_hotkey``, which name the MINER, a
    completely separate fact from which validator the stake is delegated
    to.** Required, and validated exactly like ``ark_coldkey`` above, via
    :func:`_key`: a malformed configured hotkey refuses the whole call
    rather than silently checking nothing. This applies to EVERY ``Deposit``
    this function produces -- entry, ordinary, prepaid and catch-up all
    reach the Ark through this same reader, so one check here covers all
    four by construction -- and it runs whether or not ``extrinsics`` was
    passed, because ``hotkey`` is read off ``StakeAdded`` unconditionally.

    A mismatch never drops the row (the alpha genuinely moved to the Ark
    regardless of which validator it landed on) and never overrides
    ``origin_hotkey``/``owner_at_deposit`` (which miner sent it is a real,
    separate fact worth keeping even when the deposit does not qualify).
    It DOES set (overriding any other reason already computed for this
    extrinsic) ``unattributed_reason`` to a message naming the actual
    hotkey and the designated one -- the wrong-validator fact is real
    regardless of whether attribution also succeeded or also separately
    failed, so it is the one recorded. Wrong-validator deposits remain real
    Ark receipts but earn zero qualifying mining credit.
    """
    ark_key = _key(ark_coldkey, "the Ark's coldkey")
    designated_validator_key = _key(
        designated_validator_hotkey, "the designated Ark Validator's hotkey"
    )
    _whole(block, "the block height")
    if not isinstance(block_hash, str) or not block_hash.strip():
        raise NotADepositEvent(f"the block hash came back as {block_hash!r}, not usable text.")
    if not isinstance(events, (list, tuple)):
        raise NotADepositEvent(
            f"a block's events came in as {events!r}, which is not a list of events."
        )
    if extrinsics is not None and (parent_block_hash is None or owner_source is None):
        raise NotADepositEvent(
            f"deposits_in_block at block {block} was given `extrinsics` (a batch_all+remark "
            f"deposit may be in this block) without both `parent_block_hash` and "
            f"`owner_source` -- the historical owner-entitlement check needs both, so nothing "
            f"here decodes a remark this call could never actually confirm."
        )

    givers = {}  # extrinsic -> (coldkey, event_id, raw StakeAndHotkeyTransferred origin_hotkey or None)
    arrivals = {}  # extrinsic -> the FIRST (ordinal, alpha, netuid, hotkey) seen
    remarked_hash_by_extrinsic = {}  # extrinsic -> System.Remarked's own committed hash

    # issue #112-15, gap 2: an extrinsic whose events carry more than one
    # arrival into the Ark used to raise NotADepositEvent right here and kill
    # the whole block's scan, including when one batch_all bundles multiple
    # transfer_stake legs beside one remark_with_event.
    # `arrivals` above still keeps only the FIRST tuple seen per extrinsic --
    # unchanged, and still what every OTHER extrinsic's ordinary path reads --
    # but a SECOND (or later) arrival for the same extrinsic no longer raises:
    # it is appended here instead, and the extrinsic is flagged so the
    # deposit-building loop below folds the whole group into one
    # unattributed, "received but unattributed" Deposit rather than losing
    # the block. `ambiguous_arrival_reasons` carries the ORIGINAL guard's own
    # message, verbatim, as this extrinsic's eventual `unattributed_reason`.
    arrival_repeats = {}  # extrinsic -> [every (ordinal, alpha, netuid, hotkey) seen]
    ambiguous_arrival_reasons = {}  # extrinsic -> the original guard's own message

    for position, record in enumerate(events):
        if not isinstance(record, dict):
            raise NotADepositEvent(
                f"entry {position} in block {block} is {record!r}, which is not an event record."
            )
        if (
            extrinsics is not None
            and record.get("module_id") == "System"
            and record.get("event_id") == "Remarked"
        ):
            attributes = record.get("attributes")
            remark_extrinsic = record.get("extrinsic_idx")
            if (
                isinstance(attributes, dict)
                and isinstance(attributes.get("hash"), str)
                and isinstance(remark_extrinsic, int)
                and not isinstance(remark_extrinsic, bool)
            ):
                remarked_hash_by_extrinsic[remark_extrinsic] = attributes["hash"]
        if record.get("module_id") != "SubtensorModule":
            continue
        event = record.get("event_id")

        named_origin_hotkey = None
        if event == "StakeTransferred":
            fields = _fields(record, _TRANSFER_FIELDS, block, "a transfer event")
            origin, destination = fields[_T_ORIGIN], fields[_T_DESTINATION]
        elif event == "StakeAndHotkeyTransferred":
            named = _named_fields(record, block)
            origin, destination = named["origin_coldkey"], named["destination_coldkey"]
            # Kept RAW (undecoded) here and validated only once this
            # extrinsic's deposit is being attributed, below -- a malformed
            # origin_hotkey is that one deposit's own isolated attribution
            # problem (MalformedOriginHotkey), never a reason to stop the
            # whole block's scan the way a malformed coldkey is.
            named_origin_hotkey = named["origin_hotkey"]
        else:
            origin = destination = None

        if destination is not None:
            if _key(destination, "the wallet a transfer went to") != ark_key:
                continue
            extrinsic = _extrinsic_of(record, block)
            if extrinsic in givers:
                if extrinsic in ambiguous_arrival_reasons:
                    # This extrinsic already tripped the "more than one
                    # arrival" flag below (real block 1288's own event
                    # order: its second StakeAdded lands before its second
                    # StakeTransferred), so it is already headed for one
                    # merged, unattributed Deposit rather than a normal one
                    # -- a second transfer here is part of that SAME already-
                    # flagged ambiguity, not a new, independent one, and
                    # keeping the first-seen giver (unchanged, never
                    # overwritten) is a recorded fact, not a guess. Any
                    # OTHER extrinsic's ordinary "two different transfers"
                    # collision is untouched: it still raises immediately.
                    _key(origin, "the wallet that gave alpha up")
                    continue
                raise NotADepositEvent(
                    f"extrinsic {extrinsic} in block {block} carries more than one transfer "
                    f"into the Ark. Which giver goes with which amount is not on the chain, "
                    f"and pairing them by order would be a guess about somebody's permanent "
                    f"contribution."
                )
            _key(origin, "the wallet that gave alpha up")
            givers[extrinsic] = (origin, event, named_origin_hotkey)
            continue

        if event == "StakeAdded":
            fields = _fields(record, _STAKE_FIELDS, block, "a stake event")
            if _key(fields[_S_COLDKEY], "the wallet a stake landed on") != ark_key:
                continue
            extrinsic = _extrinsic_of(record, block)
            ordinal = _ordinal_of(record, block)
            alpha = _whole(fields[_S_ALPHA], "the alpha that arrived")
            if alpha == 0:
                raise NotADepositEvent(
                    f"an arrival into the Ark at block {block} says nothing arrived. The "
                    f"chain cannot announce a stake of nothing, so this is a value that "
                    f"failed to decode rather than a contribution that did not happen."
                )
            netuid = _whole(fields[_S_NETUID], "the subnet the alpha arrived on")
            # Validated, like every other address here, and then kept in the form
            # the chain sent rather than in `_key`'s normalised return: `coldkey`
            # a few lines below does the same with `origin`, and for the same
            # reason -- a caller comparing this against an SS58 string it already
            # holds (`verify.py`'s `intended`) needs that string back, not the
            # public key `_key` compares addresses by underneath.
            _key(fields[_S_HOTKEY], "the position the alpha arrived on")
            hotkey = fields[_S_HOTKEY]
            if extrinsic in arrivals:
                if extrinsics is None:
                    # Unchanged: a caller that never opted into the new
                    # extrinsics/parent_block_hash/owner_source machinery
                    # gets EXACTLY today's behaviour, the identical
                    # all-or-nothing contract this function's own docstring
                    # already promises for the four newer refusals above.
                    raise NotADepositEvent(
                        f"extrinsic {extrinsic} in block {block} carries more than one arrival "
                        f"into the Ark, so no amount can be matched to a giver with certainty."
                    )
                # issue #112-15, gap 2: no longer raised for a caller that DID
                # opt in -- see this function's own docstring, "AS OF ISSUE
                # #112-15, GAP 2," and the comment beside `arrival_repeats`'s
                # own declaration above for why. The ORIGINAL message is
                # kept, verbatim, as this extrinsic's eventual
                # `unattributed_reason` rather than as an exception that
                # kills the block.
                ambiguous_arrival_reasons[extrinsic] = (
                    f"extrinsic {extrinsic} in block {block} carries more than one arrival "
                    f"into the Ark, so no amount can be matched to a giver with certainty."
                )
                arrival_repeats.setdefault(extrinsic, [arrivals[extrinsic]]).append(
                    (ordinal, alpha, netuid, hotkey)
                )
                continue
            arrivals[extrinsic] = (ordinal, alpha, netuid, hotkey)

    for extrinsic in sorted(set(givers) | set(arrivals)):
        if extrinsic not in arrivals:
            raise NotADepositEvent(
                f"extrinsic {extrinsic} in block {block} transferred alpha into the Ark and "
                f"there is no stake event beside it, so the amount is unknown. The transfer "
                f"event carries only a TAO-equivalent, and inventing an alpha figure would "
                f"credit the wrong number to somebody permanently."
            )
        if extrinsic not in givers:
            # issue #112-16: the ONE positively-identified, on-chain-checkable
            # exception to this refusal -- see the "Proxy.proxy Ark
            # self-funding" section above for the real block this closes.
            # Gated identically to every other newer refusal in this module:
            # a caller that has not opted into `extrinsics` gets today's
            # exact behaviour, no regression.
            if extrinsics is not None and 0 <= extrinsic < len(extrinsics) and (
                _proxy_ark_construction_stake(extrinsics[extrinsic], ark_key, block)
            ):
                # The Ark funding itself during its own construction, not a
                # deposit -- verified via the Proxy wrapper's own `real`
                # field naming the Ark's coldkey, wrapping `add_stake`
                # (a direct TAO-to-alpha purchase into one position, which
                # never has a giver event to find). Cleanly excluded: no
                # Deposit record, no refusal.
                del arrivals[extrinsic]
                continue
            raise NotADepositEvent(
                f"alpha arrived at the Ark in extrinsic {extrinsic} of block {block} and "
                f"nothing says who gave it up. Recording it against nobody loses the "
                f"contribution and inventing an owner credits the wrong person, and neither "
                f"can be undone."
            )

    deposits = []
    for extrinsic, (ordinal, alpha, netuid, hotkey) in sorted(
        arrivals.items(), key=lambda item: item[1][0]
    ):
        origin_hotkey = None
        owner_at_deposit = None
        unattributed_reason = None
        if extrinsic in ambiguous_arrival_reasons:
            # issue #112-15, gap 2: this extrinsic's own multiple arrivals
            # are folded into ONE unattributed Deposit here -- see
            # `arrival_repeats`'s own declaration above for why, and its
            # own docstring comment for the real block (1288) this closes.
            # No batch_all decode is attempted at all: which remark (if any)
            # would even pair with which of these arrivals is exactly the
            # ambiguity this guard exists to refuse, so trying anyway would
            # only replace one guess with another.
            repeats = arrival_repeats[extrinsic]
            distinct_positions = {(netuid, hotkey) for _, _, netuid, hotkey in repeats}
            if len(distinct_positions) != 1:
                raise NotADepositEvent(
                    f"extrinsic {extrinsic} in block {block} carries {len(repeats)} arrivals "
                    f"into the Ark across {len(distinct_positions)} different hotkey/subnet "
                    f"positions ({sorted(distinct_positions)}) -- summing them into one amount "
                    f"would mix positions that are not the same holding, which is a guess this "
                    f"module refuses to make."
                )
            alpha = sum(one_alpha for _, one_alpha, _, _ in repeats)
            unattributed_reason = ambiguous_arrival_reasons[extrinsic]
            _logger.warning(
                "deposits_in_block: extrinsic %s in block %s left uncredited: %s",
                extrinsic,
                block,
                unattributed_reason,
            )
        elif extrinsics is not None and givers[extrinsic][1] == "StakeAndHotkeyTransferred":
            # Attribution source depends on which real transfer event
            # carried the arrival, not on batching (issue #135, plan posted
            # 2026-09-21 ~01:05 UTC). StakeAndHotkeyTransferred names the
            # miner directly in its own origin_hotkey field, chain-enforced
            # (only the actual position owner can invoke
            # transfer_stake_and_hotkey on their own stake) -- so, unlike
            # StakeTransferred below, this never goes through
            # _batch_all_attribution/a remark at all, whether or not this
            # extrinsic happens to be wrapped in a batch_all, and never
            # calls confirm_owner_entitled_at_block: there is no
            # self-reported claim here needing independent entitlement
            # confirmation.
            try:
                origin_hotkey = _named_origin_hotkey(
                    givers[extrinsic][2], extrinsic_index=extrinsic, block=block
                )
            except MalformedOriginHotkey as exc:
                # Isolated to this one extrinsic, exactly like
                # AmbiguousDepositBatch/MalformedDepositRemark below --
                # never re-raised past this deposit, never dropped either;
                # see that branch's own comment for why both are true.
                _logger.warning(
                    "deposits_in_block: extrinsic %s in block %s left uncredited: %s",
                    extrinsic,
                    block,
                    exc,
                )
                unattributed_reason = str(exc)
            else:
                owner_at_deposit = givers[extrinsic][0]
        elif extrinsics is not None:
            try:
                outcome = _batch_all_attribution(
                    extrinsics,
                    extrinsic,
                    block=block,
                    coldkey=givers[extrinsic][0],
                    remarked_hash_by_extrinsic=remarked_hash_by_extrinsic,
                    owner_source=owner_source,
                    parent_block_hash=parent_block_hash,
                )
            except (
                AmbiguousDepositBatch,
                MalformedDepositRemark,
                OwnerNotEntitled,
                OwnerReadUnreadable,
            ) as exc:
                # Deliberately NOT re-raised, unlike every other refusal in
                # this module: a batch_all extrinsic is its own transaction,
                # and one malformed or unentitled attribution attempt must
                # not cost every OTHER, cleanly-attributable deposit in this
                # same block its recognition.
                #
                # Retain the receipt even when attribution fails:
                # the alpha genuinely moved to the Ark regardless of whether
                # it could be attributed, so the Deposit below is still
                # produced -- "received but unattributed" -- with
                # `origin_hotkey` left None and `unattributed_reason` set to
                # this refusal's own message. Still logged at WARNING
                # through this module's own logger too (see
                # deposits_in_block's own docstring for what that does and
                # does not guarantee) -- the log line and the recorded
                # reason are both kept, neither replaces the other.
                _logger.warning(
                    "deposits_in_block: extrinsic %s in block %s left uncredited: %s",
                    extrinsic,
                    block,
                    exc,
                )
                unattributed_reason = str(exc)
            else:
                if isinstance(outcome, _AttributedDeposit):
                    origin_hotkey = outcome.origin_hotkey
                    owner_at_deposit = outcome.owner_at_deposit
                # _OrdinaryDeposit() -> today's shape, all three stay None.

        # The attribution rule: every qualifying deposit must name its
        # miner hotkey. Anything that reached here with no hotkey and no reason
        # is an unnamed deposit: record it as unattributed, with a visible
        # reason, so it credits nobody (single-seat wallets included). Without
        # `extrinsics` nothing can be read, so that mode is left as it was.
        if extrinsics is not None and origin_hotkey is None and unattributed_reason is None:
            unattributed_reason = _unnamed_deposit_reason(extrinsics, extrinsic, block)
            _logger.warning(
                "deposits_in_block: extrinsic %s in block %s left uncredited: %s",
                extrinsic,
                block,
                unattributed_reason,
            )

        # Issue #282 item 4/4a, #302: delegation means the stake arrived in
        # the Ark delegated to the DESIGNATED validator, checked against
        # this position's own hotkey -- never the sender/origin hotkey,
        # which names the miner, a separate fact matched independently
        # above. Applies unconditionally (every Deposit this function
        # produces, whether or not `extrinsics` was supplied) and overrides
        # whatever `unattributed_reason` the attribution step above already
        # computed (including None): a wrong-validator deposit is not a
        # qualifying deposit regardless of whether the miner who sent it is
        # also known. The row is still produced, still carries the real
        # `origin_hotkey`/`owner_at_deposit` it worked out above -- nothing
        # here removes a fact, only the reason it does not count changes.
        if _key(hotkey, "the position's own hotkey") != designated_validator_key:
            also_unnamed = (unattributed_reason or "").startswith(UNNAMED_REASON_PREFIX)
            unattributed_reason = (
                f"staked to {hotkey}, not the designated Ark Validator "
                f"{designated_validator_hotkey} -- does not qualify for mining credit"
            )
            if also_unnamed:
                # Fixing the validator alone would not make it credit. The
                # appended part is advice; a rescan compares only the fixed part.
                unattributed_reason += (
                    ". It also names no miner hotkey, so sending it to the right validator "
                    "alone would not make it credit. "
                    + _unnamed_deposit_reason(extrinsics, extrinsic, block)
                )

        deposits.append(
            Deposit(
                coldkey=givers[extrinsic][0],
                carried_by=givers[extrinsic][1],
                amount_rao=alpha,
                token_netuid=netuid,
                block=block,
                event_ordinal=ordinal,
                extrinsic_index=extrinsic,
                block_hash=block_hash,
                hotkey=hotkey,
                origin_hotkey=origin_hotkey,
                owner_at_deposit=owner_at_deposit,
                unattributed_reason=unattributed_reason,
            )
        )
    return deposits
