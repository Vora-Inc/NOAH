"""Build an unsigned Ark contribution under the supplied ceiling and consent.

The destination, subnet, delegated hotkey and amount are explicit inputs. This
module does not open a wallet, sign a call or submit a transaction."""

from dataclasses import dataclass

from ..chain.sources import Refusal
from .availability import Ceiling
from .disclosures import require_acknowledgment

# The only call this module knows how to name. Not a parameter, not a default,
# and not reachable from any input.
_TRANSFER_STAKE = "transfer_stake"
_PALLET = "SubtensorModule"


class NotSomethingToDeposit(Refusal):
    """An amount that is not a positive whole number of rao."""


class AboveTheCeiling(Refusal):
    """More than the chain will accept from this position, so nothing is built."""


class NotAChainAddress(Refusal):
    """An address or hotkey that is not usable text."""


@dataclass(frozen=True)
class UnsignedDeposit:
    """A transaction, described completely, that nothing here can authorise.

    Frozen because it is printed for a human to check against a block explorer
    before they sign. A structure that can be edited after it is displayed is a
    structure where the thing signed and the thing checked can differ.
    """

    pallet: str
    call: str
    destination_coldkey: str
    hotkey: str
    origin_netuid: int
    destination_netuid: int
    alpha_amount: int


def _address(value, what):
    if not isinstance(value, str) or not value.strip():
        raise NotAChainAddress(f"{what} must be a non-empty address, got {value!r}.")
    return value


def _netuid(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NotAChainAddress(f"{what} must be a whole subnet id, got {value!r}.")
    return value


def build_deposit(*, ark_address, hotkey, netuid, amount, ceiling, acknowledgment):
    """Build one unsigned contribution under the supplied safety checks; never sign or send."""
    ark_address = _address(ark_address, "the Ark address")
    hotkey = _address(hotkey, "the position's hotkey")
    netuid = _netuid(netuid, "the subnet id")

    if isinstance(amount, bool) or not isinstance(amount, int):
        raise NotSomethingToDeposit(
            f"the deposit amount came back as {amount!r}, which is not a whole number of "
            f"rao. Every amount on this path is read from the chain as an integer, so "
            f"anything else means a decode was skipped above."
        )
    if amount <= 0:
        raise NotSomethingToDeposit(
            f"a deposit of {amount} is not a deposit. Nothing is built for it, because a "
            f"transaction moving nothing still costs a fee and still asks someone to sign."
        )

    require_acknowledgment(acknowledgment)
    if not isinstance(ceiling, Ceiling):
        raise AboveTheCeiling(
            f"no deposit ceiling was supplied ({ceiling!r}). The ceiling is what the chain "
            f"will actually accept from this position, and building without one produces a "
            f"transaction that looks right and is refused on submission."
        )

    if amount > ceiling.amount:
        raise AboveTheCeiling(
            f"a deposit of {amount} rao is more than this position can send. The most it "
            f"can send is {ceiling.amount} rao, and the limit that binds is the "
            f"{ceiling.bound_by} one. Nothing is capped here on purpose: quietly reducing "
            f"the amount would let a deposit look like it met the day's ask while landing "
            f"below it."
        )

    return UnsignedDeposit(
        pallet=_PALLET,
        call=_TRANSFER_STAKE,
        destination_coldkey=ark_address,
        hotkey=hotkey,
        origin_netuid=netuid,
        destination_netuid=netuid,
        alpha_amount=amount,
    )
