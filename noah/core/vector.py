"""Validate publication vectors and account for seat turnover.

Preserve the relationship between scored identities and current UIDs. Do not
redirect a departed miner's weight to a new occupant of the same seat."""

from ..chain.sources import Refusal


class OwnerWouldBePaid(Refusal):
    """A seat the owner controls was about to receive miner weight."""


class SeatOwnerUnknown(Refusal):
    """A live seat whose current owner was not read, so it cannot be checked."""


def from_engine_output(output):
    """Read ``weightsU16`` out of a scoring run, whose keys are text.

    The engine serialises seat numbers as strings because its output is JSON
    that two implementations compare byte for byte. Everything below works in
    whole numbers, so the conversion happens here, once, at the boundary.
    """
    return {int(uid): weight for uid, weight in output["weightsU16"].items()}


def publishable_vector(*, scored, live_uids, fixture_hotkeys, chain_hotkeys, owner_uids):
    """The vector to commit, given the engine's answer and the subnet as it is now.

    ``scored`` is what the engine produced, seat number to weight. ``live_uids``
    is every seat registered on the subnet right now. ``fixture_hotkeys`` is who
    held each seat at the height that was scored, and ``chain_hotkeys`` is who
    holds it now; the two are compared rather than assumed equal. ``owner_uids``
    is every seat whose hotkey the owner controls.
    """
    live = sorted(set(live_uids))

    for uid in live:
        if uid not in chain_hotkeys:
            raise SeatOwnerUnknown(
                f"seat {uid} is registered on the subnet and its current owner was not "
                f"read. Treating that as unchanged would pay whoever holds it now, and "
                f"treating it as changed would withhold pay somebody earned. Read the "
                f"seat's hotkey at the commit height and try again."
            )

    vector = {}
    for uid in live:
        weight = scored.get(uid, 0)
        if weight and fixture_hotkeys.get(uid) != chain_hotkeys[uid]:
            # The seat changed hands between the height that was scored and now.
            weight = 0
        vector[uid] = weight

    would_be_paid = sorted(uid for uid in vector if uid in owner_uids and vector[uid])
    if would_be_paid:
        raise OwnerWouldBePaid(
            f"seats {would_be_paid} are owner-controlled and were about to receive miner "
            f"weight. The chain counts that as burned and publishes the figure, and the "
            f"figure scales this subnet's income down. Nothing is published. Find out why "
            f"the engine put weight there rather than removing it here."
        )

    return vector
