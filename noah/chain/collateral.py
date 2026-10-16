"""Validate collateral observations before using them as spendable-pay evidence.

Check storage-map key arity against metadata and compare the coldkey aggregate
with its individual positions. A decoded default zero alone does not prove that
an otherwise malformed query read the intended key. Manual collateral changes
and registration principal require separate classification from earnings."""

from dataclasses import dataclass

from .sources import Refusal


class QuestionNotWellFormed(Refusal):
    """The query has not been shown to match the storage map's declared key shape."""


class LockDesynced(Refusal):
    """A wallet's aggregate lock disagreeing with the positions under it."""


class NotACollateralEntry(Refusal):
    """A collateral position that is not the struct the chain stores."""


@dataclass(frozen=True)
class SettleEffect:
    """What an epoch did to one lock: pay captured, or pay released."""

    paid_locked_arrival_rao: int
    pay_unlocked_rao: int


def well_formed(field, block, *, keys_given, keys_declared):
    """Whether the question asked has the shape the chain says the map takes.

    The check that survives an empty map, which is the case the control-key
    idiom cannot serve and the case that matters most at launch.
    """
    if keys_given != keys_declared:
        raise QuestionNotWellFormed(
            f"reading {field} at block {block}: the question supplied {keys_given} key(s) "
            f"and the chain declares this map takes {keys_declared}. A question with too "
            f"few keys is accepted and answers 'not there' without complaining, so a "
            f"malformed question and a real absence are the same bytes and nothing this "
            f"read said can be used."
        )
    return True


# The four fields the chain stores per position (`lib.rs:420-436`). Only the
# first is a balance held today.
LOCKED, DRAIN_RATIO, MIN_LOCKED, EARNED = "locked", "drain_ratio", "min_locked", "earned"


def locked_of(entry, *, hotkey, block):
    """What this position holds locked right now, or nothing.

    **`MinerCollateral` does not map to a balance.** It maps to a four-field
    struct, and only `locked` is alpha held today. The other three are each a
    plausible wrong answer:

    - `min_locked` is a floor on FUTURE drain, not a balance now. The drain
      never releases below it, so a reader summing it overstates what is
      locked. It sits next to `locked` in the same struct and is the mistake
      most likely to look right.
    - `drain_ratio` is `U64F64` fixed point. In a rao field it is wrong by
      about eighteen quintillion rather than wrong by a little, which at least
      fails loudly.
    - `earned` is lifetime observability, not a position.

    Absence stays absence. This map is `OptionQuery` (`lib.rs:3265-3281`, spec 450,
    was `:3092-3101` at spec 447), so a missing row genuinely means no collateral,
    unlike the per-wallet aggregate, which is `ValueQuery` and cannot report its
    own absence at all. That
    asymmetry runs against the intuition: the aggregate is the cheaper read and
    the one a reader trusts more, and it is the one that cannot say it found
    nothing.
    """
    if entry is None:
        return 0
    if not isinstance(entry, dict):
        raise NotACollateralEntry(
            f"the collateral for {hotkey} at block {block} came back as {entry!r}. This map "
            f"holds a four-field struct, not a balance, so a reader taking it as a number "
            f"has picked one of the four by accident. Absence is passed as None."
        )
    if LOCKED not in entry:
        raise NotACollateralEntry(
            f"the collateral for {hotkey} at block {block} has no {LOCKED!r} field, only "
            f"{sorted(entry)}. Nothing else in this struct is alpha held today: "
            f"{MIN_LOCKED!r} is a floor on future drain, {DRAIN_RATIO!r} is fixed point, and "
            f"{EARNED!r} is lifetime observability."
        )
    value = entry[LOCKED]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NotACollateralEntry(
            f"the locked alpha for {hotkey} at block {block} is {value!r}, which is not a "
            f"decoded whole number."
        )
    return value


def lock_total_rao(*, coldkey, block, aggregate_rao, positions):
    """A wallet's locked alpha, from two maps that must agree.

    ``aggregate_rao`` is ``ColdkeyMinerCollateral`` for the wallet.
    ``positions`` is ``MinerCollateral``'s locked figure per hotkey. The runtime
    keeps the first as the sum of the second, so disagreement means one of the
    two readings is wrong and neither may be used.
    """
    summed = sum(positions.values())
    if summed != aggregate_rao:
        shown = ", ".join(f"{hotkey}={amount}" for hotkey, amount in sorted(positions.items()))
        raise LockDesynced(
            f"for {coldkey} at block {block} the wallet's total locked collateral reads "
            f"{aggregate_rao}, and its positions sum to {summed} ({shown or 'no positions'}). "
            f"Three causes, which need chasing differently, which is why both readings are "
            f"printed rather than summarised. The position read may have its hotkey and "
            f"coldkey the wrong way round, which is well formed and returns absent. The "
            f"chain's own bookkeeping may have desynced, because the aggregate is maintained "
            f"incrementally rather than recomputed. Or a desync that would have gone negative "
            f"was floored at zero by that subtraction's saturation, in which case the "
            f"aggregate is not merely wrong but wrong DOWNWARD, understating what is locked. "
            f"That third one is the dangerous direction for anyone using this figure as a "
            f"ceiling: it reads lower than truth, so it permits more than the chain will."
        )
    return aggregate_rao


# Events that move a lock by a miner's own hand rather than by the epoch. There
# is no removal event because there is no manual removal: a lock only drains
# through earned emission.
BY_HAND = ("CollateralLocked", "MinCollateralSet")


def collateral_moved_by_hand(events):
    """The hotkeys whose collateral a miner changed by hand in this block.

    Returned as hotkeys rather than wallets, because **the event does not carry
    the coldkey** (`macros/events.rs:773-782`) and the lock is keyed by one. Nor
    can the coldkey be recovered from the hotkey's owner: `add_collateral` locks
    against the *signer*, and a nominator staking to somebody else's hotkey is a
    different coldkey from that hotkey's owner.

    So a block carrying one of these cannot have its lock movement attributed,
    and the wallets paid in it are marked unreadable for that day rather than
    given a delta that silently includes what the miner did. That is the ruled
    fallback and not a workaround: hold out, mark not-read, and name it.
    """
    moved = set()
    for event in events:
        if event.get("event_id") in BY_HAND:
            attributes = event.get("attributes") or {}
            hotkey = attributes.get("hotkey")
            if hotkey is not None:
                moved.add(hotkey)
            else:
                # A movement we cannot even name is still a movement, and
                # treating it as absent is the failure this module exists for.
                moved.add(f"<unnamed hotkey in {event.get('event_id')}>")
    return moved


def collateral_locked_at_registration(events):
    """Identify miner hotkeys whose registration locked collateral in this block.

    Match the chain event shape; unrelated balance or collateral changes are not
    registration evidence."""
    registered = set()
    for event in events:
        if event.get("event_id") != "NeuronRegistered":
            continue
        attributes = event.get("attributes")
        if isinstance(attributes, (tuple, list)) and len(attributes) == 3:
            registered.add(attributes[2])
    if not registered:
        return set()
    locked = set()
    for event in events:
        if event.get("event_id") != "CollateralLocked":
            continue
        attributes = event.get("attributes")
        hotkey = attributes.get("hotkey") if isinstance(attributes, dict) else None
        if hotkey in registered:
            locked.add(hotkey)
    return locked


def settle_effect(*, before_rao, after_rao, announced_rao):
    """What the epoch's settle did, with what the miner did taken out.

    ``announced_rao`` is the net change the block's own collateral events
    account for. Subtracted rather than ignored: a miner topping up in the same
    block otherwise reads as pay captured, and that wallet is held out of a day
    it earned normally.
    """
    moved = after_rao - before_rao - announced_rao
    if moved > 0:
        return SettleEffect(paid_locked_arrival_rao=moved, pay_unlocked_rao=0)
    if moved < 0:
        return SettleEffect(paid_locked_arrival_rao=0, pay_unlocked_rao=-moved)
    return SettleEffect(paid_locked_arrival_rao=0, pay_unlocked_rao=0)
