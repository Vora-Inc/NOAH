"""Attribute each epoch's emitted pay to the identities holding its seats then.

Use ownership and registration at the epoch block, include all mechanisms, and
apply the owner-withholding rules. Validate map completeness and registration
rather than interpreting a default address as proof that an entry exists."""

from dataclasses import dataclass

from .sources import Refusal

NO_RECORD_ACCOUNT = "5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM"


class SlotTurnedOver(Refusal):
    """A paid seat changed hands too close to the epoch to attribute its pay."""


class NoRegistrationRecord(Refusal):
    """A paid seat carries no honest record of when it was registered."""


class NotAnAmount(Refusal):
    """A pay amount, seat number or height that is not a plain, decoded integer."""


class NotAnAccount(Refusal):
    """An account that is not text, or that is the address meaning "no record"."""


class NoPayObserved(Refusal):
    """An epoch with no mechanism vector read at all. A read that did not happen."""


@dataclass(frozen=True)
class EpochAttribution:
    """One epoch's pay, resolved to wallets.

    ``paid`` and ``withheld`` are both tuples of ``(uid, hotkey, coldkey,
    amount)`` in seat order. Seat and hotkey are carried rather than collapsed
    because the store records one row per seat per block: if only wallet totals
    came back, whoever writes the caller would have to walk the seats again
    themselves, and the withholding of owner-associated seats would then live
    in the code path they had bypassed.
    """

    epoch_block: int
    paid: tuple
    withheld: tuple

    @property
    def paid_by_coldkey(self):
        """What each wallet earned this epoch, derived from the rows above."""
        totals = {}
        for _uid, _hotkey, coldkey, amount in self.paid:
            totals[coldkey] = totals.get(coldkey, 0) + amount
        return totals


def attribute_epoch(
    emissions_by_mechanism,
    hotkey_by_uid,
    coldkey_by_hotkey,
    registration_block_by_uid,
    epoch_block,
    subnet_owner_coldkey,
    subnet_owner_hotkey,
):
    """Attribute one epoch block's pay to the wallets that earned it, or refuse.

    ``emissions_by_mechanism`` maps a mechanism id to that mechanism's decoded
    ``{seat: amount}``. Every mechanism that fired in this block belongs in it.
    """
    _positive(epoch_block, "epoch block")
    _account(subnet_owner_coldkey, "the subnet owner wallet")
    _account(subnet_owner_hotkey, "the subnet owner hotkey")

    if not emissions_by_mechanism:
        raise NoPayObserved(
            f"no mechanism vector was read at epoch block {epoch_block}. An epoch with no "
            f"vector is a read that did not happen, not an epoch that paid nothing, and "
            f"recording it as nothing understates every wallet in the day."
        )

    total_by_uid = {}
    for mech_id in sorted(emissions_by_mechanism):
        for uid, amount in emissions_by_mechanism[mech_id].items():
            _whole(uid, f"seat number in mechanism {mech_id}")
            _whole(amount, f"pay for seat {uid} in mechanism {mech_id}")
            total_by_uid[uid] = total_by_uid.get(uid, 0) + amount

    paid = []
    withheld = []
    for uid in sorted(total_by_uid):
        amount = total_by_uid[uid]
        if amount == 0:
            # The vector carries an entry for every seat and most are zero, and
            # before the subnet begins emitting every one of them is. This is
            # the chain saying nothing was paid, which is a different thing from
            # a source failing to say anything at all.
            continue

        _check_registration(uid, registration_block_by_uid.get(uid), epoch_block)

        hotkey = _account(hotkey_by_uid.get(uid), f"the hotkey holding seat {uid}")
        coldkey = _account(
            coldkey_by_hotkey.get(hotkey), f"the wallet owning hotkey {hotkey} on seat {uid}"
        )

        row = (uid, hotkey, coldkey, amount)
        if _is_owner_associated(hotkey, coldkey, subnet_owner_coldkey, subnet_owner_hotkey):
            withheld.append(row)
        else:
            paid.append(row)

    return EpochAttribution(epoch_block=epoch_block, paid=tuple(paid), withheld=tuple(withheld))


def _whole(value, what):
    """A plain, already-decoded, non-negative integer, or a refusal."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAnAmount(
            f"{what} came back as {value!r}, which is not a decoded integer. Values reaching "
            f"attribution must already be decoded; an undecoded value is refused rather than "
            f"interpreted, because reading it wrong produces a number instead of an error."
        )
    if value < 0:
        raise NotAnAmount(f"{what} is negative ({value}).")
    return value


def _positive(value, what):
    _whole(value, what)
    if value == 0:
        raise NotAnAmount(f"{what} must be greater than zero, got 0.")
    return value


def _account(value, what):
    """An account that a later read can find again, or a refusal.

    The address that means "no record" is refused here rather than reasoned
    about, because it is the answer four different maps give when they hold
    nothing, and it is also a real address with a real holder. Letting it
    through either attributes a participant's pay to whoever holds that address
    or files their whole epoch as pay the runtime withheld.
    """
    if not isinstance(value, str) or not value:
        raise NotAnAccount(
            f"{what} came back as {value!r}, which is not a text account. An account that is "
            f"not text cannot be matched by any later read, so the row it lands in becomes "
            f"invisible instead of becoming an error."
        )
    if value == NO_RECORD_ACCOUNT:
        raise NotAnAccount(
            f"{what} came back as {NO_RECORD_ACCOUNT}, the address the chain answers with "
            f"when it holds no record at all. It is a valid-looking address that nobody "
            f"holds, so it cannot be told apart from a real wallet by looking at it."
        )
    return value


def _is_owner_associated(hotkey, coldkey, subnet_owner_coldkey, subnet_owner_hotkey):
    """Whether the runtime withheld this seat's pay instead of handing it over.

    The runtime builds its skip list from OwnedHotkeys(subnet owner coldkey),
    restricted to hotkeys that hold a seat on this subnet, plus the declared
    subnet owner hotkey if it holds one (run_coinbase.rs:659-690). Every seat
    considered here holds one by construction, so reading the owning wallet of
    each paid hotkey produces the same set.

    That equivalence rests on Owner and OwnedHotkeys never disagreeing in the
    direction that matters. Every non-test writer of either map was read:
    staking/helpers.rs:138-146 and 157-165, swap/swap_coldkey.rs:178-196,
    swap/swap_hotkey.rs:433 and 562-586, migrations/
    migrate_populate_owned_hotkeys.rs:59, macros/genesis.rs:80. All of them
    write both, or leave a stale entry in both. If a future runtime adds a
    writer that does not, this check drifts from the chain and has to be
    rewritten to read OwnedHotkeys directly. That is a pinned assumption for
    the runtime watcher, not a settled fact.
    """
    return hotkey == subnet_owner_hotkey or coldkey == subnet_owner_coldkey


def _check_registration(uid, registration_block, epoch_block):
    """Refuse a paid seat whose registration record cannot be trusted.

    Zero is checked before the comparison against the epoch block, and that
    order is the whole point. The registration map answers with a default of
    zero rather than with nothing, so a seat with no record and a seat
    registered at block zero arrive as the same bytes. A comparison against the
    epoch block accepts that zero happily and pays the seat to whoever holds it.
    """
    if registration_block is None:
        raise NoRegistrationRecord(
            f"seat {uid} was paid at block {epoch_block} but no registration block was read "
            f"for it. Pay cannot be attributed to a seat whose occupancy is unknown."
        )
    if isinstance(registration_block, bool) or not isinstance(registration_block, int):
        raise NoRegistrationRecord(
            f"seat {uid} at block {epoch_block}: registration block came back as "
            f"{registration_block!r}, which is not a decoded integer."
        )
    if registration_block <= 0:
        raise NoRegistrationRecord(
            f"seat {uid} was paid at block {epoch_block} but its registration block reads "
            f"{registration_block}. The chain stores this with a default of zero, so a seat "
            f"with no record cannot be told apart from one registered at block zero. Both "
            f"are refused."
        )
    if registration_block >= epoch_block:
        raise SlotTurnedOver(
            f"seat {uid} was paid at epoch block {epoch_block} and its registration block is "
            f"{registration_block}. The seat changed hands at or after the epoch it was paid "
            f"for, so which participant earned that pay is not on the chain. Nothing is "
            f"attributed for this epoch."
        )
