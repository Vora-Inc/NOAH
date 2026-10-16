"""Historical decision (2026-09-13, issue #16 comment 5656515778): "skip the
duplicated wallet, do not halt the period." Under that former rule, a
coldkey's several hotkeys were excluded together. Neither this function nor
anything it calls ever guesses which of the duplicated coldkey's hotkeys
"really" belongs -- Drew explicitly rejected a stand-in ordering (e.g. seat
number) for this exact situation (comment 5651001974: "A third option --
guess which hotkey came first using some stand-in (like seat number) -- is
not real. Drew already rejected that for exactly this situation, so it is
not offered here again.").

This test covers the retained helper as a pure, offline function. The #33
chain and validator modules now exist, but D-138's reopening made several
hotkeys on one coldkey legitimate; ``ChainAdapter.read()`` no longer calls
this helper (issue #135). These tests do not establish the current scoring
policy. The live-path behavior is covered in
``noah/core/tests/test_duplicate_coldkey_wiring.py``.
"""

from ..duplicate_coldkeys import exclude_duplicate_coldkeys


def test_no_duplicates_everyone_passes_through_unchanged():
    seats = {
        "5Alice": {"5AliceHot": 0},
        "5Bob": {"5BobHot": 1},
        "5Carol": {"5CarolHot": 2},
    }
    kept, dropped = exclude_duplicate_coldkeys(seats)
    assert kept == seats
    assert kept is not seats  # a fresh structure, never the caller's own mutated in place
    assert dropped == []


def test_one_coldkey_with_two_hotkeys_both_dropped_others_unaffected():
    seats = {
        "5Alice": {"5AliceHot": 0},
        "5Duplicated": {"5DupHotA": 1, "5DupHotB": 2},
        "5Carol": {"5CarolHot": 3},
    }
    kept, dropped = exclude_duplicate_coldkeys(seats)

    assert kept == {"5Alice": {"5AliceHot": 0}, "5Carol": {"5CarolHot": 3}}
    assert "5Duplicated" not in kept

    dropped_hotkeys = {(d["coldkey"], d["hotkey"], d["uid"]) for d in dropped}
    assert dropped_hotkeys == {
        ("5Duplicated", "5DupHotA", 1),
        ("5Duplicated", "5DupHotB", 2),
    }
    assert len(dropped) == 2  # one record per dropped hotkey, not per coldkey


def test_a_coldkey_with_three_or_more_hotkeys_all_dropped():
    seats = {
        "5Alice": {"5AliceHot": 0},
        "5TripleTrouble": {"5T1": 1, "5T2": 2, "5T3": 3},
    }
    kept, dropped = exclude_duplicate_coldkeys(seats)

    assert kept == {"5Alice": {"5AliceHot": 0}}
    assert len(dropped) == 3
    assert {d["hotkey"] for d in dropped} == {"5T1", "5T2", "5T3"}
    assert all(d["coldkey"] == "5TripleTrouble" for d in dropped)


def test_several_duplicated_coldkeys_at_once_each_fully_excluded():
    seats = {
        "5Alice": {"5AliceHot": 0},
        "5DupOne": {"5D1a": 1, "5D1b": 2},
        "5DupTwo": {"5D2a": 3, "5D2b": 4, "5D2c": 5},
        "5Bob": {"5BobHot": 6},
    }
    kept, dropped = exclude_duplicate_coldkeys(seats)

    assert kept == {"5Alice": {"5AliceHot": 0}, "5Bob": {"5BobHot": 6}}
    assert len(dropped) == 5
    assert {d["coldkey"] for d in dropped} == {"5DupOne", "5DupTwo"}


def test_empty_input():
    kept, dropped = exclude_duplicate_coldkeys({})
    assert kept == {}
    assert dropped == []


def test_a_coldkey_with_zero_hotkeys_is_not_a_duplicate_and_passes_through():
    """An empty hotkey map for a coldkey is not "more than one hotkey" --
    nothing to exclude, nothing to drop. (Not expected from a real seat
    read, but the function's own rule -- "more than one" -- should not
    misfire on it.)"""
    seats = {"5NoSeatsYet": {}}
    kept, dropped = exclude_duplicate_coldkeys(seats)
    assert kept == {"5NoSeatsYet": {}}
    assert dropped == []


def test_mutating_the_returned_kept_map_never_mutates_the_input():
    seats = {"5Alice": {"5AliceHot": 0}}
    kept, _ = exclude_duplicate_coldkeys(seats)
    kept["5Alice"]["5AliceHot"] = 999
    kept["5Injected"] = {"5Nope": 1}
    assert seats == {"5Alice": {"5AliceHot": 0}}


def test_no_ordering_or_winner_selection_is_ever_attempted():
    """Confirms, by construction, that the function's own source contains no
    code path that could be read as choosing a winner among a duplicated
    coldkey's hotkeys -- no sort, no min/max, no first/last, no index
    access into a duplicated coldkey's hotkey mapping. This is a direct,
    mechanical check of the implementation's own text, not just its
    behavior, because the decision record is explicit that no stand-in
    ordering is acceptable even as an implementation detail nobody
    observes: "it does not guess."
    """
    import inspect

    from .. import duplicate_coldkeys

    source = inspect.getsource(duplicate_coldkeys)
    forbidden = ("sorted(", "sort(", "min(", "max(", "[0]", "[-1]", ".pop(")
    for token in forbidden:
        assert token not in source, (
            f"found {token!r} in duplicate_coldkeys.py -- this could read as an "
            f"ordering/winner decision among a duplicated coldkey's hotkeys, which "
            f"Drew's decision record explicitly forbids, even as a stand-in nobody "
            f"was supposed to rely on."
        )


def test_two_coldkeys_each_with_exactly_one_hotkey_sharing_no_state():
    """A basic sanity check that per-coldkey hotkey counts are judged
    independently -- two ordinary, non-duplicated coldkeys are never
    confused with each other or accidentally combined."""
    seats = {"5A": {"5AH": 0}, "5B": {"5BH": 1}}
    kept, dropped = exclude_duplicate_coldkeys(seats)
    assert kept == seats
    assert dropped == []
