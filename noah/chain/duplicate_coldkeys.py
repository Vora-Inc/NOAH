"""Historical duplicate-coldkey filter, unused by the current validator.

The current validator permits multiple hotkeys per coldkey and deliberately
does not call this helper. This retained pure function excludes every hotkey
of a coldkey with multiple seats; it never chooses one seat as a winner.

Input is ``{coldkey: {hotkey: uid}}``. Output is a copy of the kept seat map
and one dropped record per excluded hotkey, in input iteration order. The
function does not mutate its input or read the chain.
"""

from __future__ import annotations


def exclude_duplicate_coldkeys(seats):
    """Exclude every hotkey of a coldkey that holds more than one, unchanged
    otherwise. Returns ``(kept, dropped)``; see this module's own docstring
    for the exact shapes. Never mutates ``seats``."""
    dropped = []
    kept = {}
    for coldkey, hotkeys in seats.items():
        if len(hotkeys) > 1:
            for hotkey, uid in hotkeys.items():
                dropped.append({"coldkey": coldkey, "hotkey": hotkey, "uid": uid})
        else:
            kept[coldkey] = dict(hotkeys)
    return kept, dropped
