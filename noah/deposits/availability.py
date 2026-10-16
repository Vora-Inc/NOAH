"""Determine how much a stake position can contribute to the Ark.

Keep total, locked and available amounts distinct. Enforce the configured holding
floor and transaction limits before building an unsigned contribution."""

from dataclasses import dataclass

from ..chain.sources import Refusal


class NotAnAlphaAmount(Refusal):
    """A value on the ceiling path that is not a plain, non-negative integer."""


def _amount(value, what):
    """An alpha balance, or a refusal.

    ``bool`` is rejected before the ``int`` check because it is a subclass of
    ``int``, so ``True`` would otherwise arrive here as the amount 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAnAlphaAmount(
            f"{what} came back as {value!r}, which is not a plain integer of rao. Every "
            f"amount on this path is read from the chain as a whole number, so anything "
            f"else means a decode was skipped somewhere above."
        )
    if value < 0:
        raise NotAnAlphaAmount(
            f"{what} came back as {value}, and no chain read produces a negative alpha "
            f"balance. Clamping it to zero here would hide the fault that produced it."
        )
    return value


@dataclass(frozen=True)
class Ceiling:
    """The most that may leave this position, and which limit decided it.

    ``bound_by`` is carried rather than derived later because it is what makes a
    surprising number explicable to the person reading it: a miner who expected
    to deposit their whole position needs to know whether a lock elsewhere under
    their own coldkey is the reason, or a pledge on this position itself.
    """

    amount: int
    bound_by: str


def deposit_ceiling(*, position_stake, position_collateral, coldkey_available):
    """The binding ceiling, from the three figures the chain answers with.

    ``coldkey_available`` is the third element of the runtime's
    ``stake_availability``, which has already subtracted the conviction lock and
    the coldkey's total collateral (``lock.rs:750-759``). The two position
    figures are read separately, per the module docstring.
    """
    position_stake = _amount(position_stake, "the position's stake")
    position_collateral = _amount(position_collateral, "the position's miner collateral")
    coldkey_available = _amount(coldkey_available, "the coldkey's available alpha")

    # A pledge larger than the position means nothing may leave it, not that a
    # negative amount may. A negative would flow into the comparison against a
    # requested amount and quietly invert it.
    position_leg = max(0, position_stake - position_collateral)

    if position_leg < coldkey_available:
        bound_by = "position"
    elif coldkey_available < position_leg:
        bound_by = "coldkey"
    else:
        # Naming one of two equal limits would be a coin toss, and the display
        # exists to explain the number rather than to sound decisive.
        bound_by = "both"

    return Ceiling(amount=min(position_leg, coldkey_available), bound_by=bound_by)
