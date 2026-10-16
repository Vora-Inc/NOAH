"""Read per-position collateral and compare it with the coldkey aggregate.

Use the full (netuid, hotkey, coldkey) key and validate the sum across positions.
Do not infer collateral by inverting saturating availability arithmetic: a zero
available balance can hide a larger obligation. An empty set of reads is not
independent confirmation of a zero total."""

from ..chain.sources import Refusal


class CollateralDisagrees(Refusal):
    """Per-position reads and the coldkey-wide total do not describe one wallet."""


class PositionNotRead(Refusal):
    """A position's collateral was asked for and nobody read it. Not an answer of none."""


class NotACollateralAmount(Refusal):
    """A collateral figure that is not a plain, non-negative integer."""


def _amount(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotACollateralAmount(
            f"{what} came back as {value!r}, which is not a whole rao amount."
        )
    if value < 0:
        raise NotACollateralAmount(f"{what} came back as {value}, which cannot be negative.")
    return value


def position_collateral(*, reads, coldkey_collateral, hotkey):
    """One position's collateral, once every read is accounted for.

    ``reads`` maps every hotkey the coldkey stakes with on this subnet to the
    collateral read for it. Every one is needed, not just the position being
    deposited from, because the check is that they sum to the independent total.
    """
    coldkey_collateral = _amount(coldkey_collateral, "the coldkey's total collateral")
    for key, value in reads.items():
        _amount(value, f"the collateral read for {key}")
    # This also covers the empty case, and deliberately rather than by luck.
    #
    # An earlier version had a separate guard above this one refusing an empty
    # set of reads, on the reasoning that the sum of nothing is zero and agrees
    # with a zero total by arithmetic alone, which is two absences matching. The
    # reasoning is right and the guard was unreachable: no reads means this
    # hotkey is not among them, so the line below always refused first. Switching
    # it off changed no test, which is how it was found.
    #
    # Repaired by removal rather than by a more careful check. A guard that can
    # never be the one that fires is not a guard, and leaving it in would say
    # the emptiness case is defended on purpose when it is defended by a rule
    # about something else. What is kept is the sentence that matters: nobody
    # having asked is not the same as the chain answering none.
    if hotkey not in reads:
        raise PositionNotRead(
            f"the collateral for {hotkey} was asked for and no read of it was supplied "
            f"({len(reads)} position(s) were read). Nobody asking is not the same as the "
            f"chain answering none, and only one of those means the position is unpledged."
        )

    read_total = sum(reads.values())
    if read_total != coldkey_collateral:
        raise CollateralDisagrees(
            f"the per-position collateral reads sum to {read_total} while the coldkey's own "
            f"total reads {coldkey_collateral}. Both numbers are printed because a mismatch "
            f"has at least three causes and they want different responses.\n"
            f"  (1) The keys were wrong. A multi-key map asked with too few parts answers "
            f"'not there' without complaining, and netuid-hotkey-coldkey has two AccountIds "
            f"that swap cleanly, so a well-formed query can still name the wrong position.\n"
            f"  (2) The runtime's own bookkeeping desynced. The aggregate is maintained "
            f"incrementally and never recomputed (adjust_coldkey_miner_collateral, "
            f"collateral.rs:73-88), so it can drift from the rows it is meant to total.\n"
            f"  (3) A desync that would have gone negative was floored. That update uses "
            f"saturating_sub, so the aggregate can silently understate the truth. This is "
            f"the dangerous one HERE: an understated total raises the deposit ceiling, and "
            f"the chain then rejects a transaction this tool said was fine.\n"
            f"Nothing is used until the two agree. Which of the three it is cannot be "
            f"decided from these two numbers alone, and this refusal does not guess."
        )
    return reads[hotkey]
