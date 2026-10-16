"""Read and record whether the subnet has begun emitting.

FirstEmissionBlockNumber is read at the pinned block. Absence is distinct from a
failed query. Once a start is observed, persist its day and block identity so later
history checks cannot silently omit the beginning of the subnet's earnings."""

from .sources import Refusal
from .store import EmissionStateChanged


class EmissionStateReorged(Refusal):
    """The chain moved between two reads inside one ``observe_emission_
    state`` call -- a reorg both sources have already converged on, not a
    source disagreement -- so the converged answer is refused rather than
    trusted stale. See this module's own docstring, "Reorg safety."
    """


def observe_emission_state(runner_chain, store, observed_block):
    """Read whether the subnet has begun emitting, as of ``observed_block``,
    and record it through the matching ``Store`` method.

    Returns whatever the ``Store`` method itself returns (the day the
    recording resolves through) -- not otherwise used by this function's
    own caller, but handed back rather than discarded, matching
    ``Store.record_emission_start``/``record_emission_not_started``'s own
    contract.

    ``runner_chain`` must answer ``first_emission_block``, ``block_hash``,
    and ``block_timestamp`` -- the real, dual-sourced ``AgreeingChain``
    every other real chain read in this reader goes through, in
    production. This function does no SOURCE-agreement work of its own
    (``first_emission_block``/``block_hash``/``block_timestamp`` already
    did that before this function ever sees an answer, exactly the same
    division of labour ``noah.chain.dayblocks``'s own functions already
    keep with ``runner_chain``) -- it does its own TIME-agreement work
    instead (see this module's own docstring, "Reorg safety"): every block
    it reads more than one fact about is bracketed by two hash reads, and
    the answer this function is ABOUT TO TRUST is re-derived fresh,
    immediately before trusting it, exactly once more.
    """
    started_at = runner_chain.first_emission_block(observed_block)

    recorded = None if started_at is None else store.recorded_emission_start()
    if recorded is not None:
        recorded_day, recorded_block, _recorded_hash = recorded
        reconfirmed = runner_chain.first_emission_block(observed_block)
        if reconfirmed != started_at:
            raise EmissionStateReorged(
                f"block {observed_block}'s own emission-state answer changed from "
                f"{started_at} to {reconfirmed!r} between the first read and a fresh "
                f"re-check taken immediately before trusting it -- the chain reorged "
                f"mid-read. Refused rather than trusted stale; nothing was recorded. "
                f"Re-run once the chain has settled."
            )
        if started_at != recorded_block:
            raise EmissionStateChanged(
                f"the subnet was already recorded as beginning to emit at block "
                f"{recorded_block} in day {recorded_day}, and the chain now says block "
                f"{started_at}. The chain refuses to set this twice, so one of these is "
                f"wrong and neither is used."
            )
        return recorded_day

    if started_at is not None:
        started_before = runner_chain.block_hash(started_at)
        started_timestamp = runner_chain.block_timestamp(started_at)
        started_after = runner_chain.block_hash(started_at)
        if started_after != started_before:
            raise EmissionStateReorged(
                f"the started block {started_at}'s own hash changed from {started_before} "
                f"to {started_after} while reading its own timestamp -- the chain reorged "
                f"mid-read. Refused rather than trusted stale; nothing was recorded. Re-run "
                f"once the chain has settled."
            )
        reconfirmed = runner_chain.first_emission_block(observed_block)
        if reconfirmed != started_at:
            raise EmissionStateReorged(
                f"block {observed_block}'s own emission-state answer changed from "
                f"{started_at} to {reconfirmed!r} between the first read and a fresh "
                f"re-check taken immediately before trusting it -- the chain reorged "
                f"mid-read. Refused rather than trusted stale; nothing was recorded. "
                f"Re-run once the chain has settled."
            )
        return store.record_emission_start(
            block=started_at,
            block_timestamp=started_timestamp,
            block_hash=started_after,
        )

    observed_timestamp = runner_chain.block_timestamp(observed_block)
    observed_hash = runner_chain.block_hash(observed_block)
    reconfirmed = runner_chain.first_emission_block(observed_block)
    if reconfirmed is not None:
        raise EmissionStateReorged(
            f"block {observed_block} answered None for its own emission-state fact, and "
            f"a fresh re-check taken immediately before trusting that now answers "
            f"{reconfirmed!r} instead -- the chain reorged mid-read (the subnet started "
            f"between the first read and now). Refused rather than trusted stale; nothing "
            f"was recorded. Re-run once the chain has settled."
        )
    return store.record_emission_not_started(
        block=observed_block,
        block_timestamp=observed_timestamp,
        block_hash=observed_hash,
    )
