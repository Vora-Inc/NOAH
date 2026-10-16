"""Require a confirmed entry contribution before scoring a miner seat.

Qualification is per (coldkey, miner hotkey), using cumulative attributed deposits
through the requested day and the current profile's required amount. A shared
coldkey does not transfer one seat's qualification to another, and a replacement
occupant does not inherit the previous miner's entry contribution."""

from __future__ import annotations

from ..chain.sources import Refusal
from ..config.profile import entry_deposit_rao


class EntryDepositNotConfirmed(Refusal):
    """This SEAT's real, observed cumulative NOAH deposits have not
    reached the required entry figure (read fresh via
    ``noah.config.profile.entry_deposit_rao()`` -- see
    :func:`confirm_entry_before_score`) as of the day being judged. No
    score and no weight follow from this."""


def confirm_entry_before_score(store, coldkey, hotkey, *, as_of_day):
    """Require this (coldkey, hotkey)'s cumulative confirmed contributions to meet entry policy."""
    required = entry_deposit_rao()
    observed = store.cumulative_noah_deposits_for_hotkey(coldkey, hotkey, as_of_day)
    if observed < required:
        raise EntryDepositNotConfirmed(
            f"{coldkey}'s hotkey {hotkey} has {observed} rao of real, observed NOAH deposits "
            f"attributed to THIS seat through day {as_of_day}, short of the required "
            f"entry-deposit figure of {required} rao for this hotkey. "
            f"No score and no weight follow for this "
            f"seat until its OWN real, confirmed deposit reaches it -- including a hotkey "
            f"that has taken over a uid a different hotkey previously held, which inherits "
            f"nothing through the shared uid, and including any OTHER seat this same coldkey "
            f"also holds, whose own deposit never vouches for this one."
        )
    return observed


def exclude_unconfirmed_entries(store, seats, *, as_of_day):
    """Remove only seats lacking their own entry evidence before downstream scoring."""
    kept = {}
    dropped = []
    for coldkey, hotkeys in seats.items():
        kept_hotkeys = {}
        for hotkey, uid in hotkeys.items():
            try:
                confirm_entry_before_score(
                    store,
                    coldkey,
                    hotkey,
                    as_of_day=as_of_day,
                )
            except EntryDepositNotConfirmed:
                dropped.append({"coldkey": coldkey, "hotkey": hotkey, "uid": uid})
                continue
            kept_hotkeys[hotkey] = uid
        if kept_hotkeys:
            kept[coldkey] = kept_hotkeys
    return kept, dropped
