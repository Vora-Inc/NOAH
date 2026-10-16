"""Read per-seat entry requirements and confirm the depositor holds stake.

Entry amounts and ongoing requirements are supplied at runtime. The flat entry
contribution is the same for every seat; requirements are not summed across a
coldkey's seats.

A depositor must hold the position being transferred, but need not own the
validator hotkey to which that position is delegated. confirm_position_held
checks the decoded position_stake value already read for the transfer ceiling.
An unreadable value and a known zero balance produce distinct refusals. Account
identifiers in those messages describe the queried position; this check does
not compare hotkey ownership addresses.
"""

from dataclasses import dataclass

from ..chain.sources import Refusal

#: Every part of the entry structure. Named so a missing one is a refusal rather
#: than a zero, because a requirement defaulted to zero is a requirement anyone
#: meets and it would read as good news.
ENTRY_PARTS = ("cost_to_register", "flat_first_deposit", "ongoing_requirement")


class EntryStructureNotRead(Refusal):
    """A part of the entry structure, or the holding, was not read."""


class PositionNotHeld(Refusal):
    """The wallet does not hold the stake position it is depositing, or the read of it failed."""


@dataclass(frozen=True)
class HoldingShown:
    """One seat's holding beside the structure it is judged against."""

    holding: int
    cost_to_register: int
    flat_first_deposit: int
    ongoing_requirement: int
    short_of_requirement: int

    @property
    def meets_ongoing_requirement(self):
        return self.short_of_requirement == 0


def _amount(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EntryStructureNotRead(
            f"{what} came back as {value!r}. It is read at run time rather than written "
            f"into this tool, so an unreadable one is refused instead of defaulted: a "
            f"requirement defaulted to zero is a requirement everyone meets, and it would "
            f"read as good news."
        )
    return value


def confirm_position_held(*, hotkey, coldkey, netuid, position_stake):
    """That this wallet actually holds this stake position, before anything is
    built on the assumption it does.

    ``position_stake`` is ``noah/deposits/reads.py``'s ``ceiling_inputs`` own
    reading of it -- ``coldkey``'s row in ``hotkey``'s alpha share pool on
    ``netuid``, the identical figure ``do_transfer_stake``'s own precondition
    compares the requested amount against (this module's own docstring has
    the citations). Passed in rather than re-read here, because the deposit
    ceiling already reads it and a second read of the same fact is a second
    thing that could disagree with the first for no reason.

    The chain refuses a shortfall with an error naming neither key. This
    refuses with both, before a signature rather than after one.
    """
    not_an_int = isinstance(position_stake, bool) or not isinstance(position_stake, int)
    if not_an_int or position_stake < 0:
        raise PositionNotHeld(
            f"the chain's stake figure for coldkey {coldkey} on hotkey {hotkey}, netuid "
            f"{netuid}, came back as {position_stake!r}, not a decoded whole number of rao. "
            f"Nobody having read this cleanly is not the same as the chain saying zero, and "
            f"everything after this assumes the answer is a real balance."
        )
    if position_stake == 0:
        raise PositionNotHeld(
            f"the chain shows coldkey {coldkey} holding 0 rao of stake on hotkey {hotkey}, "
            f"netuid {netuid}. A deposit moves an existing staked position into the Ark "
            f"[D-049]; it does not create one, and this coldkey holds none here to move -- "
            f"whether because nothing was ever staked from it on this hotkey, or because the "
            f"stake sitting there belongs to a different coldkey. Depositing requires holding "
            f"the position being deposited, not owning the hotkey it happens to sit on."
        )
    return True


def holding_against_entry(*, holding, structure):
    """One seat's holding beside the structure, every figure read rather than written."""
    if not isinstance(structure, dict):
        raise EntryStructureNotRead(f"the entry structure came back as {structure!r}.")

    missing = [part for part in ENTRY_PARTS if part not in structure]
    if missing:
        raise EntryStructureNotRead(
            f"the entry structure is missing {missing}. Every part is required and none is "
            f"defaulted, because a part nobody read is not a part that is zero."
        )

    held = _amount(holding, "the seat's holding")
    parts = {
        part: _amount(structure[part], f"the {part.replace('_', ' ')}") for part in ENTRY_PARTS
    }

    return HoldingShown(
        holding=held,
        short_of_requirement=max(0, parts["ongoing_requirement"] - held),
        **parts,
    )
