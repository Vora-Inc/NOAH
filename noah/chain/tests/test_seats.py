"""noah.chain.seats.current_seats: the live {coldkey: {hotkey: uid}} read,
ported from tools/chain-extract/extract.py:3540-3599's own reasoning, proven
against a real, dual-sourced AgreeingChain -- and, separately, the three
refusal cases that reasoning was hard-won for (a null Owner read; two uids
sharing one hotkey; a reorg between the Keys and Owner reads, found by codex
adversarial review of this port itself), each proven to actually fire.
"""

import unittest

from noah.chain.agreeing import AgreeingChain
from noah.chain.seats import (
    _NULL_ACCOUNT,
    DuplicateSeatHotkey,
    SeatOwnerUnreadable,
    SeatSnapshotUnstable,
    current_seats,
)

BLOCK = 900
SEAT0_HOT = "5EsZn96Zsp52JEHdoVN9D2mZ99DwTbiS2pHEDZUEZsey3tDj"
SEAT0_COLD = "5Ge5FwzR8sz5AVMwkBkjVsdfpJGj6MSSMSJ9HE7EKH3PfFBR"
SEAT1_HOT = "5CG7y1S6v9TmRDm32VAeUbyMpfSyRvrNmFLcF8kekVt3zaQs"
SEAT1_COLD = "5HNCnaAFBCUiUn4mkZkfXURwvSScyrBYVhMN9t7dvkHZGCrR"


class Fake:
    """Answers exactly the two questions current_seats asks: seat_hotkeys
    (the Keys map) and hotkey_owners (the Owner map, for a specific hotkey
    list)."""

    def __init__(self, seats=None, owners=None):
        self._seats = seats if seats is not None else {0: SEAT0_HOT, 1: SEAT1_HOT}
        self._owners = (
            owners if owners is not None else {SEAT0_HOT: SEAT0_COLD, SEAT1_HOT: SEAT1_COLD}
        )
        self.asked = []

    def canary(self, block):
        return 256

    def block_hash(self, block):
        return f"0x{block:064x}"

    def seat_hotkeys(self, block):
        self.asked.append(("seats", block))
        return dict(self._seats)

    def hotkey_owners(self, hotkeys, block):
        self.asked.append(("owners", block, tuple(sorted(hotkeys))))
        return {h: self._owners[h] for h in hotkeys if h in self._owners}


def chain(**kwargs):
    return AgreeingChain(
        sources={"archive": Fake(**kwargs), "latent": Fake(**kwargs)},
        endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
    )


class CurrentSeatsTests(unittest.TestCase):
    def test_composes_seat_hotkeys_and_hotkey_owners_into_the_seat_registry(self):
        reader = chain()
        self.assertEqual(
            current_seats(reader, BLOCK),
            {SEAT0_COLD: {SEAT0_HOT: 0}, SEAT1_COLD: {SEAT1_HOT: 1}},
        )

    def test_one_coldkey_holding_two_seats_keeps_both(self):
        """A coldkey owning several registered hotkeys keeps all of them --
        collapsing to one would misattribute the other seat's pay (the
        exact #193-codex-review finding extract.py's own comment names)."""
        reader = chain(owners={SEAT0_HOT: SEAT0_COLD, SEAT1_HOT: SEAT0_COLD})
        self.assertEqual(
            current_seats(reader, BLOCK),
            {SEAT0_COLD: {SEAT0_HOT: 0, SEAT1_HOT: 1}},
        )

    def test_asks_owner_only_for_the_hotkeys_keys_actually_named(self):
        reader = chain()
        current_seats(reader, BLOCK)
        # AgreeingChain.hotkey_owners consumes hotkeys into a tuple, sorted
        # here to make the assertion order-independent.
        archive = reader._sources["archive"]
        self.assertEqual(
            archive.asked[-1], ("owners", BLOCK, tuple(sorted([SEAT0_HOT, SEAT1_HOT])))
        )

    # -- the two refusals, preserved from extract.py's own hard-won cases --

    def test_refuses_a_null_owner_read_rather_than_misfile_the_seat(self):
        """[extract.py, codex review round-6 challenge on #552] A failed
        Owner read decodes as the all-zero-byte default account -- a real,
        syntactically valid string, not a missing value -- and filing the
        seat under it instead of refusing would silently misattribute a
        real, currently-held seat."""
        reader = chain(owners={SEAT0_HOT: _NULL_ACCOUNT, SEAT1_HOT: SEAT1_COLD})
        with self.assertRaises(SeatOwnerUnreadable):
            current_seats(reader, BLOCK)

    def test_falsification_the_null_owner_refusal_actually_fires(self):
        """Same discipline as every other refusal in this codebase's own
        history: proven by actually removing the guard and confirming a
        wrong answer comes back, not merely by reading the source. This
        reimplements current_seats with the null-owner check deleted,
        against the identical fixture the real guard's own test above
        uses, and shows the real seat silently misfiled under the fake
        null account -- exactly the failure the guard exists to prevent."""
        reader = chain(owners={SEAT0_HOT: _NULL_ACCOUNT, SEAT1_HOT: SEAT1_COLD})
        seat_hotkeys = reader.seat_hotkeys(BLOCK)
        owners = reader.hotkey_owners(sorted(set(seat_hotkeys.values())), BLOCK)
        seats_without_the_guard = {}
        for uid in sorted(seat_hotkeys):
            hotkey = seat_hotkeys[uid]
            coldkey = owners.get(hotkey)
            seats_without_the_guard.setdefault(coldkey, {})[hotkey] = uid
        self.assertIn(_NULL_ACCOUNT, seats_without_the_guard)
        self.assertEqual(seats_without_the_guard[_NULL_ACCOUNT], {SEAT0_HOT: 0})

    def test_refuses_two_uids_sharing_one_hotkey(self):
        """[extract.py, codex adversarial challenge round-7 on #552, P1,
        confidence 10/10] Owner is a 1:1 map at any given block, so two
        uids reporting the same hotkey in Keys is a genuine chain
        inconsistency -- the second used to silently overwrite the first's
        seat entry."""
        reader = chain(seats={0: SEAT0_HOT, 1: SEAT0_HOT})
        with self.assertRaises(DuplicateSeatHotkey):
            current_seats(reader, BLOCK)

    def test_falsification_the_duplicate_hotkey_refusal_actually_fires(self):
        """The exact reproduction extract.py's own round-7 challenge found:
        {7: "HK", 8: "HK"} silently produced {"CK": {"HK": 8}} -- uid 7's
        seat entry overwritten by uid 8's -- with the duplicate check
        deleted. Reimplemented here without the check, against the
        identical fixture the real guard's own test above uses."""
        reader = chain(seats={0: SEAT0_HOT, 1: SEAT0_HOT})
        seat_hotkeys = reader.seat_hotkeys(BLOCK)
        owners = reader.hotkey_owners(sorted(set(seat_hotkeys.values())), BLOCK)
        seats_without_the_guard = {}
        for uid in sorted(seat_hotkeys):
            hotkey = seat_hotkeys[uid]
            coldkey = owners.get(hotkey)
            seats_without_the_guard.setdefault(coldkey, {})[hotkey] = uid
        # uid 1 silently overwrote uid 0's entry -- exactly the corruption
        # DuplicateSeatHotkey exists to refuse instead of producing.
        self.assertEqual(seats_without_the_guard, {SEAT0_COLD: {SEAT0_HOT: 1}})

    def test_refuses_when_the_chain_reorgs_between_the_keys_and_owner_reads(self):
        """[codex adversarial review of this port, 2026-09-01, P1] extract.py's
        own original code pins both Keys and Owner to ONE resolved block hash
        (end_bh); this port's runner_chain protocol has no way to pass a hash
        through seat_hotkeys/hotkey_owners, so it brackets the whole read with
        a before/after block_hash comparison instead. A hash that changes
        between the two reads means the chain moved mid-read and the two maps
        may span two different forks."""
        calls = {"n": 0}

        def shared_block_hash(block):
            calls["n"] += 1
            return "0x" + ("aa" * 32 if calls["n"] <= 2 else "bb" * 32)

        class Shifting(Fake):
            def block_hash(self, block):
                return shared_block_hash(block)

        reader = AgreeingChain(
            sources={"archive": Shifting(), "latent": Shifting()},
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )
        with self.assertRaises(SeatSnapshotUnstable):
            current_seats(reader, BLOCK)

    def test_falsification_the_reorg_snapshot_refusal_actually_fires(self):
        """Reimplements current_seats with the before/after hash check
        deleted, against the identical fixture the real guard's own test
        above uses, and shows a seat map gets built anyway -- with no way
        to tell it might span two different chain states."""
        calls = {"n": 0}

        def shared_block_hash(block):
            calls["n"] += 1
            return "0x" + ("aa" * 32 if calls["n"] <= 2 else "bb" * 32)

        class Shifting(Fake):
            def block_hash(self, block):
                return shared_block_hash(block)

        reader = AgreeingChain(
            sources={"archive": Shifting(), "latent": Shifting()},
            endpoints={"archive": "wss://archive.example", "latent": "wss://latent.example"},
        )
        # Without the guard: just the seat_hotkeys/hotkey_owners composition,
        # no hash bracket at all -- the chain could have reorged in between
        # and nothing here would know.
        seat_hotkeys = reader.seat_hotkeys(BLOCK)
        owners = reader.hotkey_owners(sorted(set(seat_hotkeys.values())), BLOCK)
        seats_without_the_guard = {}
        for uid in sorted(seat_hotkeys):
            hotkey = seat_hotkeys[uid]
            coldkey = owners.get(hotkey)
            seats_without_the_guard.setdefault(coldkey, {})[hotkey] = uid
        # A seat map was produced with no error at all, even though (by this
        # fixture's own construction) the chain's own hash for BLOCK changed
        # partway through -- exactly what SeatSnapshotUnstable exists to
        # catch instead of silently producing.
        self.assertEqual(
            seats_without_the_guard,
            {SEAT0_COLD: {SEAT0_HOT: 0}, SEAT1_COLD: {SEAT1_HOT: 1}},
        )


if __name__ == "__main__":
    unittest.main()
