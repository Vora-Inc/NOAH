"""Asking a chain the ceiling's three questions, through a source someone supplies.

**What this deliberately does not own: agreement between sources.** That lives in
``noah/chain/agreeing.py``, it is being extended with collateral reads by the
session that wrote it, and a second implementation of an agreement gate is a
second thing to get wrong. The census (``sources_agree``, the deposit preflight contract) is
what requires two independent reads to agree before any flow is entered, so
agreement is a precondition of the flow rather than something re-argued on every
read.

What this does own is the shape of the three questions and the witness that makes
an absence mean something.

**The witness is the metadata's key count, and that is a deliberate choice.**
``MinerCollateral`` rows are removed when they reach zero, so the usual
control-row idiom has nothing to point at on a clean population. That is not a
launch-day edge: measured live at block 8,896,439 on spec 448, the map is empty
across subnets 0 through 39, so a control row is unavailable on the entire live
network today. The documented fault is an arity fault, too few key parts being
accepted and answered "not there" without complaint, and the runtime declares
each map's key count in its own metadata. So the question's shape is checked
against the chain's own description of the map, which works when the map is
empty.

Everything here reads. Nothing signs, holds, queues, or moves anything.
"""

from ..chain.sources import Refusal
from .collateral import position_collateral
from .reads import CollateralRead, ceiling_inputs

#: The collateral map and how many key parts it takes: netuid, hotkey, coldkey
#: (``pallets/subtensor/src/lib.rs:3265-3273``, spec 447).
COLLATERAL_MAP = "MinerCollateral"
COLLATERAL_MAP_KEYS = 3

#: The coldkey-wide aggregate, netuid and coldkey (``lib.rs:3282-3290``). Read for
#: the independent-total cross-check rather than for the ceiling itself.
#:
#: **ValueQuery**, unlike ``MinerCollateral``, so an absent row reads as zero and
#: is indistinguishable from a real zero or from a broken read. That asymmetry is
#: why the arity witness above is required rather than preferred: the cheaper,
#: more trusted read is the one that cannot report its own absence.
AGGREGATE_MAP = "ColdkeyMinerCollateral"
AGGREGATE_MAP_KEYS = 2

#: The index of which hotkeys hold a collateral row for a coldkey
#: (``lib.rs``, ``ColdkeyCollateralHotkeys``). Bounds the sum side to an
#: enumeration instead of a sweep over every hotkey the coldkey stakes with.
HOTKEY_INDEX_MAP = "ColdkeyCollateralHotkeys"
HOTKEY_INDEX_MAP_KEYS = 2


class MapMissingFromMetadata(Refusal):
    """A storage map this tool reads is absent from the runtime, or changed shape."""


class CollateralRowUnreadable(Refusal):
    """A collateral row is present but does not carry a readable locked figure."""


#: The one field of ``MinerCollateralState`` the ceiling needs: "alpha still
#: locked (non-withdrawable) on this stake position" (``lib.rs:425-441``).
#: The row also carries ``drain_ratio`` (U64F64 fixed point, which must never
#: reach rao arithmetic), ``min_locked`` (a floor on FUTURE drain, not a present
#: balance) and ``earned`` (observability). Only ``locked`` is a present balance.
LOCKED_FIELD = "locked"


def _locked_from_row(raw):
    """The locked figure out of a collateral row, or a refusal.

    ``MinerCollateral`` maps to a struct rather than to a balance, and this was
    wrong here until it was measured: the whole row went to an integer check.
    It could not fail, because the map is empty across the live network today so
    every read takes the absence path and the struct is never seen. It would have
    failed the first time a miner pledged anything.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        if LOCKED_FIELD not in raw:
            raise CollateralRowUnreadable(
                f"a collateral row came back carrying {sorted(raw)} and no {LOCKED_FIELD!r} "
                f"field. A renamed field under a runtime upgrade must not read as no "
                f"collateral, because that raises the deposit ceiling."
            )
        return raw[LOCKED_FIELD]
    got = getattr(raw, LOCKED_FIELD, None)
    if got is None:
        raise CollateralRowUnreadable(
            f"a collateral row came back as {type(raw).__name__} with no readable "
            f"{LOCKED_FIELD!r}. The ceiling subtracts a present balance and this row does "
            f"not offer one."
        )
    return got


def _witness_the_map(source):
    """Confirm the collateral map exists and still takes the keys we build.

    Refuses rather than reading, because a renamed or reshaped map answers
    "not there" for every query and that reads as "no collateral pledged", which
    raises the ceiling. The chain moved 447 to 448 under this project on
    2026-08-21, so a map changing under a runtime upgrade is a live concern
    rather than a theoretical one.
    """
    for name, expected in (
        (COLLATERAL_MAP, COLLATERAL_MAP_KEYS),
        (AGGREGATE_MAP, AGGREGATE_MAP_KEYS),
    ):
        found = source.storage_map_key_count(name)
        if found != expected:
            raise MapMissingFromMetadata(
                f"the runtime declares {name} as taking {found} key part(s); this tool builds "
                f"queries with {expected}. Every query it makes would now be malformed, and a "
                f"malformed query answers 'not there' without complaining, which reads as no "
                f"collateral and raises the deposit ceiling. Nothing is read until this agrees."
            )
    return True


def cross_checked_collateral(source, *, coldkey, hotkey, netuid):
    """Every hotkey's locked collateral for ``coldkey`` on ``netuid``, held
    against the chain's own independent aggregate, or a refusal.

    This is the read ``ceiling_from_chain`` below performs to get one
    position's collateral. ``noah/deposits/live_services.py``'s
    ``RealServices._confirm_no_locked_pay`` reuses it too, to ask a
    wallet-wide question of the identical reads, rather than calling
    ``source.coldkey_collateral()`` on its own -- this module's own docstring
    names that call "the dangerous one": the aggregate is maintained
    incrementally with a saturating subtraction and can silently understate
    the truth. Both callers get the cross-check or neither does; there is no
    third path that reads the aggregate without it.

    Returns ``(well_formed, rows, reads, owed)``. ``rows`` is every hotkey's
    RAW answer (``None`` for a genuinely absent row, so a caller can still
    tell absent from a present zero); ``reads`` is the same rows reduced to
    their locked figures, keyed by hotkey; ``owed`` is ``hotkey``'s own
    figure. ``sum(reads.values())`` is the coldkey-wide total, safe to use
    once this returns without raising, because raising is exactly what a
    disagreement with the chain's own aggregate produces
    (``noah/deposits/collateral.py``'s ``position_collateral``, called here
    rather than duplicated -- THE CROSS-CHECK, and it is called here because
    for four commits it was not: collateral.py was built, documented, tested
    and mutation-swept while nothing in any real path consulted it, so its
    own docstring's claim that the per-position zero "is checked against a
    total that came by another route" was false as assembled. A guard
    nothing calls is not a guard, and sweeping it proves only that it would
    work if it ran).
    """
    well_formed = _witness_the_map(source)
    # Every hotkey holding a row for this coldkey, not only the one being
    # deposited from, because the check is that they SUM to the independent
    # total. Reading one and comparing it to a coldkey-wide figure would compare
    # two different quantities and call the difference a fault.
    index = source.collateral_hotkeys(netuid=netuid, coldkey=coldkey)
    rows = {
        key: source.miner_collateral(netuid=netuid, hotkey=key, coldkey=coldkey)
        for key in dict.fromkeys(list(index) + [hotkey])
    }
    # Absence is converted to zero EXPLICITLY, and a present row's locked figure
    # is passed through untouched for validation. An earlier version wrote
    # ``_locked_from_row(row) or 0``, which is the silent-default idiom this
    # package refuses everywhere else: it turns a bad value into a plausible
    # zero, and a plausible zero raises the ceiling.
    reads = {key: (0 if row is None else _locked_from_row(row)) for key, row in rows.items()}

    owed = position_collateral(
        reads=reads,
        coldkey_collateral=source.coldkey_collateral(netuid=netuid, coldkey=coldkey),
        hotkey=hotkey,
    )
    return well_formed, rows, reads, owed


def ceiling_from_chain(source, *, coldkey, hotkey, netuid, allow_unregistered_position=False):
    """The ceiling's three numbers, read through ``source``.

    ``source`` answers four questions: ``storage_map_key_count(name)``,
    ``stake_info(hotkey=, coldkey=, netuid=)``,
    ``stake_availability(coldkey=, netuid=)`` and
    ``miner_collateral(netuid=, hotkey=, coldkey=)``. Whether those answers came
    from one endpoint or from several that agreed is the source's business and
    the census's, not this function's.

    ``allow_unregistered_position`` is passed straight to ``ceiling_inputs``; see
    there. Default off.
    """
    well_formed, rows, _reads, owed = cross_checked_collateral(
        source, coldkey=coldkey, hotkey=hotkey, netuid=netuid
    )

    stake_info = source.stake_info(hotkey=hotkey, coldkey=coldkey, netuid=netuid)
    availability = source.stake_availability(coldkey=coldkey, netuid=netuid)

    return ceiling_inputs(
        stake_info=stake_info,
        availability=availability,
        collateral=(
            CollateralRead(present=rows.get(hotkey) is not None, question_well_formed=well_formed),
            owed,
        ),
        allow_unregistered_position=allow_unregistered_position,
    )
