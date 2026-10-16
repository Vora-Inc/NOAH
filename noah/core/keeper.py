"""Choose the period's publication when scores are empty or nonpaying.

Keep an explicit reason with the decision. All-zero publication and upstream
failure are different outcomes even when their vectors resemble each other.
Refuse a vector that would pay an owner-controlled seat rather than silently
correcting an upstream attribution error."""

from dataclasses import dataclass

from .settings import SettingsError


class KeeperWouldPayOwner(SettingsError):
    """A publication would put weight on a seat we control."""


class InvalidScore(SettingsError):
    """A scored value was not a weight this keeper could publish."""


# The chain takes u16 weights. rescale.py enforces the same bound on the
# vector after this one decides it (predict_stored, noah/core/rescale.py);
# checking it here too means a value that violates it is caught at its
# source instead of one step further downstream.
U16_MAX = 65535


@dataclass(frozen=True)
class Decision:
    """What to publish this period, and why."""

    vector: dict
    keeping_alive: bool
    reason: str


def decide(*, scored, live_uids, owner_uids):
    """Apply the keeper policy to the scorer's output before publication.

    Preserve the explicit handling of empty/nonpaying vectors and owner exclusion."""
    live = sorted(set(live_uids))
    vector = {}
    for uid in live:
        value = scored.get(uid, 0)
        # `type(value) is not int`, not `isinstance`. isinstance(value, int) is
        # true for bool AND for any other class that subclasses int, so an
        # isinstance-plus-bool-exclusion check still admits a custom int
        # subclass -- one whose __bool__ lies about its own value would then
        # pass the owner-seat guard below (`if weight`) undetected, weight and
        # all. Exact-type is the only check neither bool nor an arbitrary
        # subclass can pass.
        if type(value) is not int:
            raise InvalidScore(
                f"seat {uid} scored {value!r}, which is not a plain integer "
                f"({type(value).__name__}). Every value on a scoring path must already be "
                f"int: never a float, a bool, a string that merely looks like one, or any "
                f"other int subclass. Coercing it here would publish a weight nobody "
                f"actually computed."
            )
        if not 0 <= value <= U16_MAX:
            raise InvalidScore(
                f"seat {uid} scored {value}, outside 0 to {U16_MAX}. The chain takes u16 "
                f"weights and cannot accept this value, whatever upstream intended by it."
            )
        vector[uid] = value

    would_be_paid = sorted(uid for uid, weight in vector.items() if weight and uid in owner_uids)
    if would_be_paid:
        raise KeeperWouldPayOwner(
            f"seats {would_be_paid} are ours and this publication would put weight on them. "
            f"The chain counts weight aimed at an owner-controlled seat as burned, publishes "
            f"the figure, and scales this subnet's own income down by it. Nothing is "
            f"published. This refuses rather than zeroing those seats quietly, because the "
            f"thing worth knowing is why anything put weight there at all."
        )

    if not live:
        return Decision(
            vector={},
            keeping_alive=False,
            reason=(
                "there are no live seats on this subnet, so there is nothing to publish. "
                "This is not keeping the validator alive; it is publishing nothing, and "
                "an extended run of these would eventually silence it."
            ),
        )

    if any(vector.values()):
        return Decision(
            vector=vector,
            keeping_alive=False,
            reason="somebody earned this period, so this is an ordinary publication.",
        )

    return Decision(
        vector=vector,
        keeping_alive=True,
        reason=(
            f"nobody earned anything this period, so every one of the {len(live)} live seats "
            f"is published at zero. This pays nobody and keeps the validator audible, which "
            f"is what stops the chain dropping it from consensus. Expected during the launch "
            f"period. Unexpected later, and worth asking why nobody earned rather than "
            f"assuming the keeper is doing its job."
        ),
    )
