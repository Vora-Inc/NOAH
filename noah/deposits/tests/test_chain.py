"""Reading the ceiling's three numbers off a chain, through a supplied source.

This module deliberately owns no multi-source machinery. NOAH-CHAIN-READER's
``AgreeingChain`` is where agreement lives, it is being extended with collateral
reads, and a second implementation of an agreement gate is a second thing to get
wrong. What this does own is the shape of the three questions and the witness
that makes an absence mean something.

The census (``sources_agree``, the deposit preflight contract) is what requires two independent
reads to agree before any flow is entered, so agreement is a precondition of the
flow rather than something re-litigated on every read.
"""

import pytest

from noah.chain.reads import NotDecoded
from noah.deposits.chain import (
    CollateralRowUnreadable,
    MapMissingFromMetadata,
    ceiling_from_chain,
)
from noah.deposits.collateral import CollateralDisagrees, NotACollateralAmount
from noah.deposits.reads import AvailabilityNotRead, PositionNotFound, QuestionNotWellFormed

COLD, HOT, NETUID = "5Cold", "5Hot", 42

#: A distinct "not supplied" so a test can ask for a chain that HAS no position.
#: Using None for that meant None also selected the default, so the two cases the
#: module exists to tell apart were the same value in the fake that tests it.
_UNSET = object()


class FakeSource:
    """A source that answers the three questions, and records what it was asked."""

    def __init__(self, *, stake_info=_UNSET, availability=_UNSET, collateral=None, maps=_UNSET):
        # `is_registered` IS ALWAYS SENT BY THE CHAIN and this double omitted it,
        # so every test here stood in a world more forgiving than the real one.
        # A record without it is not a record any node produces.
        self._stake_info = (
            {"stake": 1_000_000, "locked": 0, "is_registered": True}
            if stake_info is _UNSET
            else stake_info
        )
        self._availability = (
            {"total": 1_000_000, "locked": 0, "available": 900_000}
            if availability is _UNSET
            else availability
        )
        self._collateral = collateral
        self._maps = (
            {
                "MinerCollateral": 3,
                "ColdkeyMinerCollateral": 2,
                "ColdkeyCollateralHotkeys": 2,
            }
            if maps is _UNSET
            else maps
        )
        self.asked = []

    def storage_map_key_count(self, name):
        self.asked.append(("metadata", name))
        if name not in self._maps:
            raise MapMissingFromMetadata(f"{name} is not in this runtime's metadata.")
        return self._maps[name]

    def collateral_hotkeys(self, *, netuid, coldkey):
        self.asked.append(("collateral_hotkeys", netuid, coldkey))
        return []

    def coldkey_collateral(self, *, netuid, coldkey):
        self.asked.append(("aggregate", netuid, coldkey))
        return 0 if self._collateral is None else self._collateral_total()

    def _collateral_total(self):
        row = self._collateral
        if isinstance(row, dict):
            return row.get("locked", 0)
        return row

    def stake_info(self, *, hotkey, coldkey, netuid):
        self.asked.append(("stake_info", hotkey, coldkey, netuid))
        return self._stake_info

    def stake_availability(self, *, coldkey, netuid):
        self.asked.append(("availability", coldkey, netuid))
        return self._availability

    def miner_collateral(self, *, netuid, hotkey, coldkey):
        self.asked.append(("collateral", netuid, hotkey, coldkey))
        return self._collateral


def read(source=None, **kwargs):
    return ceiling_from_chain(
        source or FakeSource(**kwargs), coldkey=COLD, hotkey=HOT, netuid=NETUID
    )


def test_the_three_reads_become_the_ceiling_inputs():
    got = read()

    assert got.position_stake == 1_000_000
    assert got.position_collateral == 0
    assert got.coldkey_available == 900_000


def test_the_collateral_map_is_asked_with_all_three_of_its_keys():
    """A key built with too few parts answers 'not there' silently, so the
    question's shape is asserted rather than assumed."""
    source = FakeSource()
    read(source)

    assert ("collateral", NETUID, HOT, COLD) in source.asked


def test_the_metadata_witness_is_established_before_an_absence_is_believed():
    source = FakeSource()
    read(source)

    assert ("metadata", "MinerCollateral") in source.asked


def test_a_map_missing_from_the_runtime_refuses_rather_than_reading_absent():
    """A renamed map on a new runtime would otherwise look like no collateral.

    This is not hypothetical: the chain moved 447 to 448 under this project on
    2026-08-21, and a map disappearing is exactly what the runtime watcher's
    storage checks exist to catch.
    """
    with pytest.raises((MapMissingFromMetadata, QuestionNotWellFormed)):
        read(maps={"ColdkeyMinerCollateral": 2})


def test_a_map_whose_key_count_changed_refuses():
    """Three keys became two, so every query built here is now malformed."""
    with pytest.raises((MapMissingFromMetadata, QuestionNotWellFormed)):
        read(maps={"MinerCollateral": 2, "ColdkeyMinerCollateral": 2})


def test_a_position_the_chain_does_not_have_refuses():
    with pytest.raises(PositionNotFound):
        # THE SHAPE THE CHAIN ACTUALLY SENDS for a hotkey that is not a seat:
        # zero-filled, is_registered False, never None. Passing None here tested an
        # input the runtime never returns, so the guard could have stopped working
        # entirely and this would still have passed.
        read(stake_info={"stake": 0, "locked": 0, "is_registered": False})


def test_an_availability_the_chain_would_not_answer_refuses():
    with pytest.raises(AvailabilityNotRead):
        read(availability=None)


@pytest.mark.parametrize("not_a_row", ["1000", 250_000, 1.5, True])
def test_a_collateral_answer_that_is_not_a_row_refuses(not_a_row):
    """These two tests used to assert the opposite, and they were wrong.

    They encoded a bare amount as a valid collateral answer, because that was my
    model of the map until the runtime said otherwise: ``MinerCollateral`` maps
    to ``MinerCollateralState``, a four-field struct. A bare number reaching here
    now means a caller built the wrong question, and that is refused rather than
    subtracted.
    """
    with pytest.raises((NotDecoded, CollateralRowUnreadable)):
        read(collateral=not_a_row)


# ---- the row is a struct, not an amount --------------------------------------


A_ROW = {"locked": 250_000, "drain_ratio": 12345, "min_locked": 0, "earned": 999}


def test_the_locked_field_is_what_the_ceiling_subtracts():
    """``MinerCollateral`` maps to ``MinerCollateralState``, not to a balance.

    Four fields (``lib.rs:425-441``): ``locked`` is "alpha still locked
    (non-withdrawable) on this stake position", which is the one the ceiling
    needs. ``drain_ratio`` is a U64F64 fixed-point number and must never reach
    this arithmetic. ``min_locked`` is a floor on future drain rather than a
    present balance, and ``earned`` is observability.

    This was wrong until it was measured: the adapter handed the whole struct to
    an integer check. It could not fail today, because the map is empty across
    the live network, so every read takes the absence path and the struct is
    never seen. It would have failed the first time a real miner pledged
    anything, which is the soak.
    """
    got = read(collateral=A_ROW)

    assert got.position_collateral == 250_000


def test_a_row_whose_shape_changed_refuses_rather_than_reading_zero():
    """A renamed field under a runtime upgrade must not read as no collateral."""
    with pytest.raises((NotDecoded, CollateralRowUnreadable)):
        read(collateral={"drain_ratio": 1, "min_locked": 0, "earned": 0})


@pytest.mark.parametrize("bad", ["250000", None, 1.5, True, -1])
def test_a_locked_field_that_is_not_a_whole_amount_refuses(bad):
    """Refused by whichever guard reaches it first; both are named refusals.

    The value now travels through the independent-total check on its way to the
    ceiling, so a wire-format locked figure is caught there rather than here.
    What matters is that no such value becomes an amount.
    """
    with pytest.raises((NotDecoded, CollateralRowUnreadable, NotACollateralAmount)):
        read(collateral=dict(A_ROW, locked=bad))


def test_the_fixed_point_drain_ratio_never_reaches_the_arithmetic():
    """A U64F64 in a rao field would be wrong by eighteen quintillion.

    Asserted as a property of the result rather than by reading the source: the
    ceiling subtracts exactly the locked figure, whatever else the row carries.
    """
    got = read(collateral=dict(A_ROW, drain_ratio=2**64, earned=10**18))

    assert got.position_collateral == 250_000


# ---- the cross-check, which existed for hours without being called -----------


class CrossCheckSource(FakeSource):
    """A source that also answers the two coldkey-wide questions."""

    def __init__(self, *, rows=None, aggregate=0, hotkeys=None, **kwargs):
        super().__init__(**kwargs)
        self._rows = rows if rows is not None else {}
        self._aggregate = aggregate
        self._hotkeys = hotkeys if hotkeys is not None else []

    def collateral_hotkeys(self, *, netuid, coldkey):
        self.asked.append(("collateral_hotkeys", netuid, coldkey))
        return self._hotkeys

    def coldkey_collateral(self, *, netuid, coldkey):
        self.asked.append(("aggregate", netuid, coldkey))
        return self._aggregate

    def miner_collateral(self, *, netuid, hotkey, coldkey):
        self.asked.append(("collateral", netuid, hotkey, coldkey))
        return self._rows.get(hotkey)


def test_the_independent_total_is_actually_consulted():
    """The defect this test exists to prevent, and it was live for four commits.

    collateral.py's own docstring says the per-position zero "is checked against
    a total that came by another route". That was false as assembled: the module
    was built, documented, tested and mutation-swept, and no path called it.
    NOAH-CHAIN-READER found the identical shape in their own module and named the
    missing act: grep the guard for callers, which answers a different question
    from sweeping it.
    """
    source = CrossCheckSource(
        rows={HOT: {"locked": 250_000, "drain_ratio": 1, "min_locked": 0, "earned": 0}},
        aggregate=250_000,
        hotkeys=[HOT],
    )

    got = ceiling_from_chain(source, coldkey=COLD, hotkey=HOT, netuid=NETUID)

    assert ("aggregate", NETUID, COLD) in source.asked
    assert ("collateral_hotkeys", NETUID, COLD) in source.asked
    assert got.position_collateral == 250_000


def test_reads_that_do_not_sum_to_the_independent_total_refuse():
    """A malformed key reports no collateral, and the sum is what catches it."""
    source = CrossCheckSource(
        rows={HOT: {"locked": 100_000, "drain_ratio": 1, "min_locked": 0, "earned": 0}},
        aggregate=250_000,
        hotkeys=[HOT],
    )

    with pytest.raises(CollateralDisagrees):
        ceiling_from_chain(source, coldkey=COLD, hotkey=HOT, netuid=NETUID)


def test_every_hotkey_in_the_index_is_read_not_only_the_one_being_deposited_from():
    """The check is that they SUM, so a sibling position left unread breaks it."""
    other = "5Other"
    source = CrossCheckSource(
        rows={
            HOT: {"locked": 100_000, "drain_ratio": 1, "min_locked": 0, "earned": 0},
            other: {"locked": 150_000, "drain_ratio": 1, "min_locked": 0, "earned": 0},
        },
        aggregate=250_000,
        hotkeys=[HOT, other],
    )

    got = ceiling_from_chain(source, coldkey=COLD, hotkey=HOT, netuid=NETUID)

    assert ("collateral", NETUID, other, COLD) in source.asked
    assert got.position_collateral == 100_000


def test_the_clean_population_agrees_at_zero_and_computes():
    """Today's whole live network: empty index, zero aggregate, no rows.

    Two zeros agreeing is two absences matching, which this project refuses
    elsewhere. It is safe here rather than lucky, and collateral.py says why: the
    ceiling is the same number whether the reads were correct or empty, because
    there is nothing to subtract either way.
    """
    source = CrossCheckSource(rows={}, aggregate=0, hotkeys=[])

    got = ceiling_from_chain(source, coldkey=COLD, hotkey=HOT, netuid=NETUID)

    assert got.position_collateral == 0
