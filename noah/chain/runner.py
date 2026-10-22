"""Running the reader over one day, and being able to prove what it did.

Four things are one thing here, which is why they are built together rather
than added afterwards: resuming without redoing finished work, producing the
same bytes on a second run, recording what was read and from where, and knowing
which reads went through the agreement gate.

Provenance added to a working runner records what the code thinks it did.
Recorded as the run happens, it records what it did. This project has shipped
the first kind twice, and both times the record read correctly and described
something that had not occurred.

**There is no way to ask a run to continue past a refusal.** Not a flag, not a
keyword, not an environment variable. Such a thing always arrives as a
convenience during testing and it is how a system like this begins publishing
numbers nobody stands behind. If one is ever genuinely wanted, that is a
conversation, not a parameter.

**A day is marked as read only after it has been read to the end.** A day
marked finished when it was not is worse than one left undone: the first is
treated as complete forever, and the second is simply done again.
"""

from dataclasses import dataclass

from ..runtime_events import history_state, observe_accounting, observe_epoch, observe_receipt
from .collateral import (
    collateral_locked_at_registration,
    collateral_moved_by_hand,
    lock_total_rao,
    locked_of,
    settle_effect,
    well_formed,
)
from .deposits import Deposit, deposits_in_block
from .emissions import epoch_pay_vectors, pay_event_present
from .holdings import holding_for_coldkey
from .pay import attribute_epoch
from .reads import Provenance, ReadContext
from .sources import Refusal
from .store import NOT_READ


class RunRefused(Refusal):
    """A run stopped. Nothing partial was left behind that reads as finished."""


class BlockTimeNotRead(Refusal):
    """A block's time came back as a default, or did not move forward."""


class WrongDay(Refusal):
    """A range was read as one day and every block in it belongs to another."""


class EpochBlockReorged(Refusal):
    """The epoch block hash changed during its read burst; discard that burst.

    Previously committed blocks may remain stored. The day is not marked complete,
    and a retry must reread the unresolved range."""


class DepositBlockReorged(Refusal):
    """A deposit block changed between reading its events and recording its receipts.

    Refuse the inconsistent burst rather than combine values from different forks."""


class QuietBlockReorged(Refusal):
    """A block that appeared empty changed before its emptiness was confirmed.

    Even quiet blocks require a hash recheck: otherwise a stale fork can hide a deposit
    or pay event. Refuse the scan and leave the day incomplete for a later reread."""


class StakingHotkeysIncomplete(Refusal):
    """``StakingHotkeys`` left out a hotkey on which the wallet holds a position.

    Spec 473 (PR 3209) stops ``StakingHotkeys`` being a reliable full list of a
    wallet's positions. The sample enumerates hotkeys from it, so a position on
    a hotkey it omits would silently count as zero. For the hotkeys this run
    KNOWS about (the designated validator hotkey and every hotkey the store has
    recorded against the wallet) the sample reads the position directly and
    refuses, rather than adding it or dropping it, when one exists.

    The limit that remains, stated plainly: stake on a hotkey nobody here knows
    about, missing from ``StakingHotkeys``, cannot be detected. Sampled holding
    VALUES do not decide anything while ``ongoingHoldRao`` is 0, so the limit
    is not economic today; it must be revisited if that value is ever raised.
    """


class HoldingSampleReorged(Refusal):
    """A sample block changed before all wallet observations could be committed."""


class _ParentBlockOwnerSource:
    """Binds one deposit-scanning block's own PARENT block number to
    ``chain``, so the result satisfies the plain, two-keyword-only
    ``owner_at(hotkey=, block_hash=)`` duck type
    ``noah.deposits.owner_entitlement.confirm_owner_entitled_at_block``
    calls -- while still letting a dual-sourced ``noah.chain.agreeing.
    AgreeingChain`` check its own canary at the right block first
    (``AgreeingChain.owner_at`` takes ``block`` for exactly that, see its
    own docstring).

    ``chain`` is asked for ``owner_at`` fresh per hotkey/block_hash pair --
    it is never cached here -- because a source read failing (a network
    blip, a node restart) must surface as whatever ``chain.owner_at`` itself
    raises (``noah.chain.live.ChainUnavailable`` or, dual-sourced,
    ``noah.chain.sources.SourceFailed``/``Unverified``/``SourceDisagreement``
    -- all ``Refusal``\\ s), not as a cached, possibly-stale answer standing
    in for one. See ``run_day``'s own docstring, "An owner read that cannot
    reach the chain is never silently treated as ordinary attribution or an
    ordinary refusal" for what that failure does once it reaches here:
    nothing in this class catches it, so it propagates exactly like any
    other ``Refusal`` this function's own ``except Refusal: raise`` already
    passes through unchanged -- the day is not marked scanned.
    """

    def __init__(self, chain, parent_block):
        self._chain = chain
        self._parent_block = parent_block

    def owner_at(self, *, hotkey, block_hash):
        return self._chain.owner_at(hotkey=hotkey, block=self._parent_block, block_hash=block_hash)


def run_day(
    store,
    chain,
    day,
    from_block,
    to_block,
    ark_coldkey,
    designated_validator_hotkey,
    sources,
    netuid,
    holding_stride,
    read_collateral,
    may_resume=True,
    attribute_deposits=False,
    progressive=False,
    finalize=True,
):
    """Read a block range into the day history and return its provenance.

    Read deposit, pay, registration and optional holding evidence at their own blocks.
    Recheck block identity before committing a read burst. Existing completed-day
    markers may skip work when may_resume is true; partial writes can remain after a
    refusal, but the day must not be marked complete until the scan succeeds."""
    try:
        provenance = Provenance(sources=sources)
        provenance.configured("the Ark's address", "the run's settings")
        provenance.configured("the day anchor", "the store's own identity")

        _positive(day + 1, "day")  # a day may be zero; a negative one may not
        if day < 0:
            raise RunRefused(f"day {day} is not a day.")
        _positive(from_block, "the first block")
        _positive(to_block, "the last block")
        if to_block < from_block:
            raise RunRefused(
                f"the range for day {day} ends at block {to_block}, before it starts at "
                f"{from_block}."
            )

        history_state(day=day, from_block=from_block, to_block=to_block, outcome="scanning")
        if may_resume and store.day_was_scanned(day):
            # Resuming. The day is finished, so nothing is asked of the chain
            # at all, which is what makes a resumed run cheap and what a test
            # asserts by counting the questions.
            provenance.configured(f"day {day}", "already read; this run skipped it")
            history_state(day=day, from_block=from_block, to_block=to_block,
                          outcome="already_scanned")
            return provenance

        needs_rescan = store.require_day_rescan_range(day, from_block, to_block)
        if not finalize and not progressive:
            raise RunRefused("partial collection requires progressive mode")
        progress = None
        if progressive and not needs_rescan:
            from .collection import CollectionProgress
            progress = CollectionProgress(store, chain, day, from_block, to_block, {
                "version": 1, "genesis": chain.genesis_hash(), "netuid": netuid,
                "ark": ark_coldkey, "validator": designated_validator_hotkey,
                "holding_stride": holding_stride, "read_collateral": read_collateral,
                "attribute_deposits": attribute_deposits,
            })
            for boundary in (from_block, to_block):
                if store.day_of(_block_time(chain, boundary, None)) != day:
                    raise RunRefused("progressive range crosses its UTC day")
            if finalize:
                from .dayblocks import day_block_range
                if day_block_range(chain, store.day_anchor_ts, from_block) != (
                    day, from_block, to_block
                ):
                    raise RunRefused("only the entire closed day can be finalized")
        head = chain.head_block()
        last_time = None
        day_time = None
        days_seen = set()
        observed_receipts = set()
        # The previous iteration's block number and hash, kept only once that
        # iteration finished with every re-check passing.
        previous = None
        start = from_block
        if progress is not None:
            start = progress.last + 1
            observed_receipts = set(progress.receipts)
            if progress.last >= from_block:
                previous = (progress.last, chain.block_hash(progress.last))
                last_time = (progress.last, _block_time(chain, progress.last, None))
                day_time = last_time[1]
                days_seen.add(day)
        for block in range(start, to_block + 1):
            context = ReadContext(at_block=block, head_block=head)
            block_hash = chain.block_hash(block)
            events = chain.events(block)

            if attribute_deposits and block > 0:
                extrinsics = chain.extrinsics(block)
                # The parent hash only names which state the owner read is
                # pinned to. Within one scan it is the previous block's hash,
                # already read and re-checked in the previous iteration, so it
                # is reused; the first block, or any gap, reads it.
                if previous is not None and previous[0] == block - 1:
                    parent_block_hash = previous[1]
                else:
                    parent_block_hash = chain.block_hash(block - 1)
                owner_source = _ParentBlockOwnerSource(chain, block - 1)
            else:
                extrinsics = None
                parent_block_hash = None
                owner_source = None

            deposits = list(
                deposits_in_block(
                    events,
                    ark_coldkey=ark_coldkey,
                    block=block,
                    block_hash=block_hash,
                    designated_validator_hotkey=designated_validator_hotkey,
                    extrinsics=extrinsics,
                    parent_block_hash=parent_block_hash,
                    owner_source=owner_source,
                )
            )
            is_epoch = pay_event_present(events, netuid=netuid)
            registered_hotkeys = collateral_locked_at_registration(events)

            # The timestamp decides which day something falls in, so a block
            # with nothing in it does not need one. Fetching it for every block
            # was one wasted round trip per block, which over a day is more
            # traffic than everything else the runner does put together.
            if deposits or is_epoch or registered_hotkeys:
                attribution = None
                try:
                    timestamp = _block_time(chain, block, last_time)
                    last_time = (block, timestamp)
                    seen_day = store.day_of(timestamp)
                    days_seen.add(seen_day)
                    # The first moment observed that belongs to the day being
                    # read. Ranges do not land on day boundaries, so the LAST
                    # timestamp in a run is often in the next day, and
                    # holdings filed from it would land there.
                    if seen_day == day and day_time is None:
                        day_time = timestamp

                    # Reads only, never a write. is_epoch alone decides
                    # whether this runs; a block with deposits but no pay
                    # event never touches it.
                    attribution = (
                        _epoch_attribution(chain, events, netuid, block, provenance, context)
                        if is_epoch
                        else None
                    )

                    # Reads only too, and gated the identical way the old,
                    # unified `_record_epoch` gated `_record_locked_pay`: on
                    # `attribution is not None` (equivalently, the epoch's
                    # pay vectors were non-empty), not on `attribution.paid`
                    # -- a real, all-zero attribution still reads collateral
                    # once, the same as it always has.
                    locked_pay_reads = (
                        _locked_pay_reads(
                            chain,
                            block,
                            provenance,
                            context,
                            coldkeys=sorted(
                                {coldkey for _uid, _hotkey, coldkey, _amount in attribution.paid}
                            ),
                            moved_by_hand=collateral_moved_by_hand(events),
                            read_collateral=read_collateral,
                        )
                        if attribution is not None
                        else None
                    )

                    registration_lock_reads = (
                        _registration_lock_reads(
                            chain,
                            block,
                            provenance,
                            context,
                            registered_hotkeys=registered_hotkeys,
                        )
                        if registered_hotkeys and read_collateral
                        else None
                    )
                except Refusal:
                    current_hash = chain.block_hash(block)
                    provenance.read("blockHash", context)
                    if current_hash == block_hash:
                        raise
                    _raise_reorged(
                        block,
                        block_hash,
                        current_hash,
                        deposits,
                        epoch_pay_at_risk=attribution is not None and bool(attribution.paid),
                    )

                if deposits or attribution is not None or registration_lock_reads is not None:
                    epoch_pay_at_risk = attribution is not None and bool(attribution.paid)
                    _check_not_reorged(
                        chain,
                        block,
                        block_hash,
                        deposits,
                        epoch_pay_at_risk=epoch_pay_at_risk,
                        provenance=provenance,
                        context=context,
                    )

                for deposit in deposits:
                    _record(store, deposit, timestamp, netuid)
                    if seen_day == day:
                        observed_receipts.add((deposit.block, deposit.event_ordinal))
                    provenance.read("noahDepositRao", context)

                if attribution is not None:
                    store.record_epoch(
                        attribution, block_timestamp=timestamp, block_hash=block_hash
                    )
                    observe_epoch(attribution, block_hash)
                    _commit_locked_pay(store, block, block_hash, timestamp, locked_pay_reads)

                if registration_lock_reads is not None:
                    _commit_locked_pay(
                        store, block, block_hash, timestamp, registration_lock_reads
                    )
            else:
                # This block read as carrying nothing. Every guard above
                # only runs because `deposits or is_epoch` was true; a
                # block that never trips that gate gets none of them --
                # and a reorg landing while `events` was being read can
                # make a stale fork's block look quiet while the
                # canonical fork's actual block carries a real deposit or
                # pay event. Unconditional, for every block, not just the
                # ones with findings: emptiness itself is exactly what a
                # reorg can fake, so there is no "handful in seven
                # thousand" exemption here the way there is above. See
                # QuietBlockReorged's own docstring for the full finding.
                current_hash = chain.block_hash(block)
                provenance.read("blockHash", context)
                if current_hash != block_hash:
                    raise QuietBlockReorged(
                        f"block {block}'s hash was {block_hash!r} when it read as carrying "
                        f"neither a deposit nor a pay event, and is {current_hash!r} now. "
                        f"A block that looks empty can be a stale fork's view of a block "
                        f"the canonical chain actually put something in. This pass "
                        f"recorded nothing for this block and did not mark the day "
                        f"scanned. Re-run with the chain settled past this block; a "
                        f"caller that skips reading on an EXISTING marker rather than "
                        f"forcing a fresh read will not see the retry take effect."
                    )

            if progress is not None:
                progress.advance(block, block_hash, observed_receipts)
            previous = (block, block_hash)
            provenance.read("blockRead", context)
            if (block - from_block + 1) % 256 == 0:
                history_state(day=day, height=block, from_block=from_block, to_block=to_block,
                              head_block=head, outcome="scanning")

        _sample_holdings(
            store,
            chain,
            day,
            from_block,
            to_block,
            holding_stride,
            head,
            provenance,
            day_time,
            designated_validator_hotkey=designated_validator_hotkey,
            progress=progress,
            finalize=finalize,
        )

        if not finalize:
            history_state(day=day, height=to_block, from_block=from_block,
                          to_block=to_block, outcome="collecting")
            return provenance

        if not days_seen:
            days_seen = {
                store.day_of(_block_time(chain, from_block, None)),
                store.day_of(_block_time(chain, to_block, None)),
            }

        if day not in days_seen:
            raise WrongDay(
                f"blocks {from_block} to {to_block} were read as day {day}, and everything "
                f"in them falls in day{'s' if len(days_seen) > 1 else ''} "
                f"{sorted(days_seen)}. Marking day {day} as read would leave it complete "
                f"and empty for good, so the range is wrong or the anchor is."
            )

        # Last, and only once the whole range is behind us. Unconditional,
        # regardless of may_resume: the read that just happened is real either
        # way, and recording "read through here" is honest either way. What
        # may_resume decides is only whether a LATER call is allowed to treat
        # this marker as license to skip reading again.
        store.mark_day_scanned(day, from_block=from_block, to_block=to_block,
                               observed_receipts=observed_receipts)
        history_state(day=day, height=to_block, from_block=from_block, to_block=to_block,
                      outcome="observed")
        return provenance
    except Refusal:
        history_state(day=day, from_block=from_block, to_block=to_block, outcome="refused")
        raise
    except Exception as exc:
        history_state(day=day, from_block=from_block, to_block=to_block, outcome="failed")
        raise RunRefused(
            f"reading day {day} stopped: {exc}. This pass did not mark the day "
            f"scanned; re-run once the cause above is fixed. A caller that skips "
            f"reading on an EXISTING marker rather than forcing a fresh read will not "
            f"see the retry take effect -- this pass leaves an earlier marker, if any, "
            f"untouched."
        ) from exc


def _prove_staking_hotkeys_complete(store, chain, coldkey, block, listed, designated):
    """Refuse if a known hotkey the wallet holds a position on is not in ``listed``.

    The position is read through the same metadata-gated ``alpha_share`` every
    other holding read uses: a read that fails or cannot be confirmed raises
    (and so refuses the sample); an absent key at a declared item is
    ``Share(source="absent", raw=0)``, a real "no position" (the chain deletes
    positions under 500 rao at spec 473). Anything that is not a ``Share`` is
    not an answer and refuses.
    """
    from .holdings import Share

    known = set(store.known_hotkeys_of(coldkey))
    if designated:
        known.add(designated)
    for hotkey in sorted(known - set(listed)):
        share = chain.alpha_share(hotkey, coldkey, block)
        if not isinstance(share, Share):
            raise StakingHotkeysIncomplete(
                f"the position of {coldkey} on known hotkey {hotkey} at block {block} came back "
                f"as {share!r}, not a share, so StakingHotkeys' omission of it cannot be checked."
            )
        if share.raw != 0:
            raise StakingHotkeysIncomplete(
                f"{coldkey} holds a nonzero position on {hotkey} at block {block}, but "
                f"StakingHotkeys does not list that hotkey. The sample would have counted it "
                f"as zero, so nothing is recorded for this block."
            )


def _sample_holdings(
    store,
    chain,
    day,
    from_block,
    to_block,
    stride,
    head,
    provenance,
    day_time,
    designated_validator_hotkey=None,
    progress=None,
    finalize=True,
):
    """Sample every observed wallet at a fixed stride through the requested day.

    Retain each hotkey position so per-seat holding minima can be computed. Verify
    block identity before writing a sample and refuse an empty set of in-day samples."""
    if stride is None:
        # A deliberate "do not sample", not a default. A caller reading from a
        # source that holds no positions at all -- a recording of block events,
        # for instance -- must say so rather than have this invent zeros, and a
        # zero holding is a claim that a wallet holds nothing.
        provenance.configured("holdRao", "not sampled in this run")
        return

    _positive(stride, "the holding sampling stride")

    if day_time is not None:
        # A real, confirmed timestamp: the main pass already saw some block
        # in the range that genuinely belongs to `day`. By construction
        # (run_day sets day_time only when `seen_day == day`) this can
        # never actually disagree with `day`; kept as a set for one shared
        # comparison below rather than a second, parallel code path.
        sampled_days = {store.day_of(day_time)}
    else:
        sampled_days = {
            store.day_of(_block_time(chain, from_block, None)),
            store.day_of(_block_time(chain, to_block, None)),
        }

    if day not in sampled_days:
        raise WrongDay(
            f"holdings for blocks {from_block} to {to_block} would be filed in "
            f"day{'s' if len(sampled_days) > 1 else ''} {sorted(sampled_days)} while the "
            f"run is reading day {day}. The block range and the day disagree, so the "
            f"samples are not written."
        )

    wallets = store.wallets_seen()
    if not wallets:
        # Nobody earned and nobody contributed. Sampling nothing is the honest
        # answer, and marking the day sampled says the question was asked.
        if finalize:
            store.mark_day_sampled(day, stride=stride, from_block=from_block, to_block=to_block)
        return

    # Chained across the loop, the same way the main block pass chains
    # `last_time`: a source that repeats or caches one timestamp across
    # several sample blocks would otherwise pass `_block_time` a fresh `None`
    # every iteration and never trip its non-decreasing check, silently
    # filing stale evidence under whichever day the stale time happened to
    # name. Found by the same adversarial pass as the skip logic above.
    last_sample_time = None
    wrote_any = False
    for block in range(from_block, to_block + 1, stride):
        context = ReadContext(at_block=block, head_block=head)
        block_hash = chain.block_hash(block)

        rows = []
        block_timestamp = None
        in_target_day = False
        try:
            # This block's OWN real time, not the range's reference
            # timestamp above: a sample near either end of the range can
            # genuinely be in the adjacent day, and `record_holding_sample`
            # derives the day a sample counts toward from exactly this
            # value.
            block_timestamp = _block_time(chain, block, last_sample_time)
            if store.day_of(block_timestamp) == day:
                in_target_day = True
                for coldkey in wallets:
                    if progress is not None and progress.sampled(coldkey, block, block_hash):
                        continue
                    positions = []
                    listed = chain.staking_hotkeys(coldkey, block)
                    _prove_staking_hotkeys_complete(
                        store, chain, coldkey, block, listed, designated_validator_hotkey
                    )
                    for hotkey in listed:
                        share = chain.alpha_share(hotkey, coldkey, block)
                        denominator = chain.hotkey_shares(hotkey, block)
                        if denominator is None:
                            continue
                        positions.append(
                            {
                                "hotkey": hotkey,
                                "share": share,
                                "denominator": denominator,
                                "pool_alpha_rao": chain.hotkey_alpha(hotkey, block),
                            }
                        )
                    held = holding_for_coldkey(positions)
                    # Keyed by hotkey so the write below can reuse the exact
                    # Share objects already read above, rather than asking
                    # the chain for them a second time -- see this
                    # function's own docstring, "A second, smaller defect".
                    already_read = {p["hotkey"]: p for p in positions}
                    write_positions = [
                        {
                            "hotkey": p.hotkey,
                            "shares": already_read[p.hotkey]["share"],
                            "denominator": already_read[p.hotkey]["denominator"],
                            "total_hotkey_alpha_rao": p.pool_alpha_rao,
                        }
                        for p in held.positions
                    ]
                    rows.append((coldkey, held.total_rao, write_positions))
            # Genuinely in the adjacent day under its own real time (the
            # `else` this `if` has no explicit branch for): not this run's
            # to write -- see this function's own docstring -- but still
            # inside the try, and still subject to both checks below, the
            # same "unconditional, for every block" discipline
            # QuietBlockReorged's own docstring already argues for the
            # main loop: a reorg can make a block that WOULD have named
            # `day` look like it names a different one, and only a check
            # that runs regardless of the day this read produced can catch
            # that.
        except Refusal:
            # One re-check settles whether this was really a reorg, the
            # identical pattern run_day's own except Refusal uses: if the
            # hash has already moved, that IS what happened; if it has not,
            # this sample block genuinely has the problem just raised, and
            # it propagates unchanged.
            current_hash = chain.block_hash(block)
            provenance.read("blockHash", context)
            if current_hash == block_hash:
                raise
            raise HoldingSampleReorged(
                f"block {block}'s hash was {block_hash!r} while this sample's own timestamp "
                f"and wallets were being read, and is {current_hash!r} now. This pass "
                f"recorded no holdings for this block and did not mark the day sampled. "
                f"Re-run once the chain has settled past this block; a caller that skips "
                f"reading on an EXISTING marker rather than forcing a fresh read will not "
                f"see the retry take effect."
            ) from None

        # A second check, after the whole burst above has finished WITHOUT
        # raising -- the case that burst does not (and cannot) catch on its
        # own: the reorg'd fork's data was internally consistent enough
        # that nothing above raised anything, so the timestamp and/or the
        # holdings extracted from it are simply, silently wrong rather than
        # visibly broken. Nothing commits before this runs, mirroring
        # _check_not_reorged -- and this runs for EVERY block, in or out of
        # `day`, for the identical reason QuietBlockReorged's own
        # unconditional recheck does.
        current_hash = chain.block_hash(block)
        provenance.read("blockHash", context)
        if current_hash != block_hash:
            raise HoldingSampleReorged(
                f"block {block}'s hash was {block_hash!r} while this sample's own timestamp "
                f"and wallets were being read, and is {current_hash!r} now, even though "
                f"every read above completed without error. This pass recorded no holdings "
                f"for this block and did not mark the day sampled. Re-run once the chain "
                f"has settled past this block; a caller that skips reading on an EXISTING "
                f"marker rather than forcing a fresh read will not see the retry take "
                f"effect."
            )

        # Chained across the loop only once both checks above have passed
        # for a genuine, checked read -- a block whose read burst raised
        # (and was reclassified as a reorg) never reaches here, the same as
        # every other exit from this iteration.
        last_sample_time = (block, block_timestamp)

        if not in_target_day:
            continue
        wrote_any = True

        for coldkey, total_rao, write_positions in rows:
            store.record_holding_sample(
                coldkey=coldkey,
                block_timestamp=block_timestamp,
                block=block,
                block_hash=block_hash,
                total_rao=total_rao,
                locked_rao=NOT_READ,
                available_rao=NOT_READ,
                positions=write_positions,
            )
            if progress is not None:
                progress.mark_sample(coldkey, block, block_hash)
            provenance.read("holdRao", context)

    if not wrote_any:
        raise WrongDay(
            f"holdings for blocks {from_block} to {to_block}, strided by {stride}, produced no "
            f"sample block whose own real time actually falls in day {day}: every stride "
            f"point named a different day. Marking day {day} sampled from zero real samples "
            f"would read as a day of wallets holding nothing, when nobody was actually asked."
        )

    if finalize:
        store.mark_day_sampled(day, stride=stride, from_block=from_block, to_block=to_block)


def _block_time(chain, block, last_time):
    """A block's time, or a refusal. Never a default and never standing still."""
    timestamp = chain.block_timestamp(block)
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise BlockTimeNotRead(
            f"the time of block {block} came back as {timestamp!r}, which is not a decoded "
            f"whole number of seconds."
        )
    if timestamp <= 0:
        raise BlockTimeNotRead(
            f"the time of block {block} came back as {timestamp}. That is the default this "
            f"storage item answers with when the read did not happen, not a moment in time, "
            f"and it would date the block to 1970. Check the source before running again."
        )
    if last_time is not None:
        earlier_block, earlier_time = last_time
        if timestamp <= earlier_time:
            raise BlockTimeNotRead(
                f"block {block} is dated {timestamp}, at or before block {earlier_block} at "
                f"{earlier_time}. Block times rise, so one of these two reads is not about "
                f"the block it claims to be about."
            )
    return timestamp


def _epoch_attribution(chain, events, netuid, block, provenance, context):
    """Read one epoch block's pay through to wallets, and return the attribution."""
    hotkey_by_uid = chain.seat_hotkeys(block)
    provenance.read("keys", context)
    subnetwork_n = chain.subnetwork_n(block)
    provenance.read("subnetworkN", context)
    mechanism_count = chain.mechanism_count(block)
    provenance.read("mechanismCountCurrent", context)

    vectors = epoch_pay_vectors(
        events,
        netuid=netuid,
        mechanism_count=mechanism_count,
        hotkey_by_uid=hotkey_by_uid,
        subnetwork_n=subnetwork_n,
        block=block,
    )
    provenance.read("incentiveAlphaEmitted", context)
    if not vectors:
        return None

    # Every amount is non-negative, so a seat with anything anywhere is exactly
    # a seat whose total is not zero. Summing here to find them would duplicate
    # the sum attribution does, in a second place, where the two could drift.
    paid_hotkeys = sorted(
        {
            hotkey_by_uid[seat]
            for vector in vectors.values()
            for seat, amount in vector.items()
            if amount > 0 and seat in hotkey_by_uid
        }
    )

    registration_block_by_uid = chain.registration_blocks(block)
    provenance.read("blockAtRegistration", context)
    coldkey_by_hotkey = chain.hotkey_owners(paid_hotkeys, block)
    provenance.read("owner", context)
    subnet_owner_coldkey, subnet_owner_hotkey = chain.subnet_owner(block)
    provenance.read("subnetOwner", context)

    return attribute_epoch(
        emissions_by_mechanism=vectors,
        hotkey_by_uid=hotkey_by_uid,
        coldkey_by_hotkey=coldkey_by_hotkey,
        registration_block_by_uid=registration_block_by_uid,
        epoch_block=block,
        subnet_owner_coldkey=subnet_owner_coldkey,
        subnet_owner_hotkey=subnet_owner_hotkey,
    )


@dataclass(frozen=True)
class _LockedPayReads:
    """What this epoch block's collateral reads found, before anything commits.

    Returned from inside ``run_day``'s reorg-guarded ``try``, the same shape
    ``_epoch_attribution`` returns an ``EpochAttribution`` in: reads only, no
    store call anywhere in ``_locked_pay_reads``. Carries exactly what the
    later, hash-re-checked commit phase (``_commit_locked_pay``) needs to
    write from, so nothing here gets re-read after ``_check_not_reorged``
    passes.

    ``unreadable_reason`` covers both of the old, unified ``_record_locked_pay``'s
    early returns -- collateral not read this run, or a same-block hand-move
    -- since either one holds every paid coldkey out with the identical
    reason, so one string serves all of them rather than a reason per
    coldkey. ``settled`` is empty in that case, and holds one ``(before_rao,
    after_rao)`` pair per coldkey only on the ordinary,
    collateral-was-actually-read path.
    """

    coldkeys: tuple
    unreadable_reason: object
    settled: dict


def _locked_pay_reads(
    chain, block, provenance, context, *, coldkeys, moved_by_hand, read_collateral
):
    """Read collateral changes for the wallets paid at an epoch block.

    Do not net unrelated manual collateral changes into earnings. Keep unreadable
    lock evidence explicit so later scoring cannot interpret it as zero."""
    if not read_collateral:
        # This run did not read collateral at all, and says so per wallet rather
        # than leaving the figures absent. Absent and zero were the same bytes
        # in the emitted fixture, which is the defect this path exists to close;
        # replacing one silence with another would close nothing.
        provenance.configured(
            "paidLockedArrivalRao", "not read in this run: the run was told not to"
        )
        return _LockedPayReads(
            coldkeys=tuple(coldkeys),
            unreadable_reason=(
                f"This run did not read collateral, so what arrived locked and what "
                f"unlocked at block {block} are unknown rather than zero. Held out of "
                f"this day's locked-pay figures and named."
            ),
            settled={},
        )
    if moved_by_hand:
        # A miner changed a lock by hand in the same block the epoch fired, and
        # the event does not say which wallet, so the movement cannot be split
        # from what the epoch did. Every wallet paid here is marked unreadable
        # for its day rather than given a delta that quietly includes the
        # miner's own transaction. Named, so the hold-out is a fact somebody can
        # look up rather than a gap.
        return _LockedPayReads(
            coldkeys=tuple(coldkeys),
            unreadable_reason=(
                f"At block {block} a miner changed collateral by hand in the same block "
                f"this epoch paid ({', '.join(sorted(moved_by_hand))}). The event does not "
                f"carry a coldkey and the lock is keyed by one, so what the epoch did "
                f"cannot be separated from what the miner did. This wallet is held out of "
                f"this day's locked-pay figures rather than scored on a guess."
            ),
            settled={},
        )
    # The witness, asked once per epoch and before any figure is used. Built
    # earlier in this package and, for a while, called by nothing: the reads
    # went straight to the aggregate. That is the same shape as the defect this
    # whole path exists to close, so it is wired here rather than available.
    #
    # It matters most for exactly the read the runner relies on.
    # ColdkeyMinerCollateral is ValueQuery, so an absent row, a real zero and a
    # malformed question are the same bytes; MinerCollateral is OptionQuery and
    # can say it found nothing. The cheap read a reader trusts more is the one
    # that cannot report its own absence.
    declared = chain.collateral_arity(block)
    provenance.read("collateralKeyArity", context)
    well_formed(
        "ColdkeyMinerCollateral",
        block,
        keys_given=2,  # netuid, coldkey
        keys_declared=declared["ColdkeyMinerCollateral"],
    )

    settled = {
        coldkey: _read_collateral_settle(chain, block, provenance, context, coldkey, declared)
        for coldkey in coldkeys
    }

    return _LockedPayReads(coldkeys=tuple(coldkeys), unreadable_reason=None, settled=settled)


def _read_collateral_settle(chain, block, provenance, context, coldkey, declared):
    """Validate before/after aggregate collateral against the individual positions."""
    before = chain.coldkey_collateral(coldkey, block - 1)
    after = chain.coldkey_collateral(coldkey, block)
    provenance.read("coldkeyMinerCollateral", context)

    # The second witness, and it only exists once somebody holds collateral.
    # The aggregate is ValueQuery, so it cannot report its own absence, and
    # arity cannot see a question with the right number of keys in the wrong
    # ORDER: MinerCollateral takes a hotkey and a coldkey next to each other
    # and both are account ids, so a swap is well formed and reads as
    # absent. The runtime keeps the aggregate as the running sum of the
    # positions, so summing them is what catches it.
    #
    # Skipped where both readings are zero, which today is every wallet on
    # every subnet: there are no positions to sum, and asking would be one
    # more question per hotkey per wallet per epoch to confirm a zero the
    # aggregate already gave. The consequence is stated rather than hidden:
    # on a population with no collateral this check never runs, so it is
    # untested against a real value until somebody pledges one.
    if before or after:
        for at_block, expected in ((block - 1, before), (block, after)):
            well_formed(
                "MinerCollateral",
                at_block,
                keys_given=3,  # netuid, hotkey, coldkey
                keys_declared=declared["MinerCollateral"],
            )
            hotkeys = chain.collateral_hotkeys(coldkey, at_block)
            lock_total_rao(
                coldkey=coldkey,
                block=at_block,
                aggregate_rao=expected,
                positions={
                    hotkey: locked_of(
                        chain.miner_collateral(hotkey, coldkey, at_block),
                        hotkey=hotkey,
                        block=at_block,
                    )
                    for hotkey in hotkeys
                },
            )
        provenance.read("minerCollateral", context)

    return before, after


def _registration_lock_reads(chain, block, provenance, context, *, registered_hotkeys):
    """Read the lock created by a registration without treating returned principal as pay."""
    owners = chain.hotkey_owners(sorted(registered_hotkeys), block)
    provenance.read("owner", context)
    coldkeys = sorted(set(owners.values()))

    declared = chain.collateral_arity(block)
    provenance.read("collateralKeyArity", context)
    well_formed(
        "ColdkeyMinerCollateral",
        block,
        keys_given=2,  # netuid, coldkey
        keys_declared=declared["ColdkeyMinerCollateral"],
    )

    settled = {
        coldkey: _read_collateral_settle(chain, block, provenance, context, coldkey, declared)
        for coldkey in coldkeys
    }

    return _LockedPayReads(coldkeys=tuple(coldkeys), unreadable_reason=None, settled=settled)


def _commit_locked_pay(store, block, block_hash, timestamp, reads):
    """Write what ``_locked_pay_reads`` already found. Never a chain read.

    Called from ``run_day`` only where ``store.record_epoch`` is -- after
    ``_check_not_reorged`` has passed for this block -- so the same late gate
    that protects the epoch-pay commit protects this one too. A reorg that
    silently corrupted the collateral reads (rather than raising, which the
    ``try`` around ``_locked_pay_reads`` already catches) is exactly as able
    to misattribute a lock movement to the wrong fork as it is to
    misattribute epoch pay, and nothing exempts locked pay from the guard
    epoch pay already has.
    """
    if reads.unreadable_reason is not None:
        for coldkey in reads.coldkeys:
            store.mark_locked_pay_unreadable(
                coldkey=coldkey,
                block=block,
                block_timestamp=timestamp,
                reason=reads.unreadable_reason,
            )
            observe_accounting(coldkey, block, block_hash)
        return
    for coldkey in reads.coldkeys:
        before, after = reads.settled[coldkey]
        effect = settle_effect(before_rao=before, after_rao=after, announced_rao=0)
        store.record_locked_pay(
            coldkey=coldkey,
            block=block,
            block_timestamp=timestamp,
            locked_arrival_rao=effect.paid_locked_arrival_rao,
            unlocked_rao=effect.pay_unlocked_rao,
            block_hash=block_hash,
        )
        store.clear_locked_pay_unreadable(coldkey, block)
        observe_accounting(coldkey, block, block_hash, effect)


def _raise_reorged(block, block_hash, current_hash, deposits, epoch_pay_at_risk):
    """Raise the refusal corresponding to the data at risk in a changed block."""
    if deposits:
        also_epoch = " and this block's epoch pay" if epoch_pay_at_risk else ""
        raise DepositBlockReorged(
            f"block {block}'s hash was {block_hash!r} moments earlier and is "
            f"{current_hash!r} now, before the {len(deposits)} deposit(s) already read "
            f"from this block{also_epoch} could be recorded. This pass recorded none of "
            f"them and did not mark the day scanned. Re-run with the chain settled past "
            f"this block; a caller that skips reading on an EXISTING marker rather than "
            f"forcing a fresh read will not see the retry take effect."
        )
    raise EpochBlockReorged(
        f"block {block}'s hash was {block_hash!r} moments earlier and is {current_hash!r} "
        f"now. This pass recorded none of this epoch's pay and did not mark the day "
        f"scanned. Re-run with the chain settled past this block; a caller that skips "
        f"reading on an EXISTING marker rather than forcing a fresh read will not see "
        f"the retry take effect."
    )


def _check_not_reorged(chain, block, block_hash, deposits, epoch_pay_at_risk, provenance, context):
    """Re-read the block's hash and refuse if it no longer matches. Never a write.

    ``run_day``'s own bookend for the case its ``except Refusal`` handler
    cannot catch: the read burst above raised nothing at all, because the
    reorg'd fork's data was internally consistent enough to satisfy every
    check along the way, and the block-level state extracted from it is
    simply, silently wrong rather than visibly broken. Always the LAST read
    for this block before anything commits.
    """
    current_hash = chain.block_hash(block)
    provenance.read("blockHash", context)
    if current_hash != block_hash:
        _raise_reorged(block, block_hash, current_hash, deposits, epoch_pay_at_risk)


def _record(store, deposit, block_timestamp, netuid):
    """File one deposit where its token belongs (issue #16).

    Alpha of the scored subnet is a NOAH deposit. Alpha of any other subnet is a
    foreign deposit and never touches ``noah_deposit``: that table feeds entry
    qualification and compliance, which only genuine NOAH may satisfy. Both carry the
    same miner and designated-validator attribution."""
    if not isinstance(deposit, Deposit):
        raise RunRefused(f"a deposit came back as {deposit!r}, which is not one.")
    if deposit.token_netuid != netuid:
        store.record_foreign_deposit(
            coldkey=deposit.coldkey,
            block_timestamp=block_timestamp,
            amount_rao=deposit.amount_rao,
            token_netuid=deposit.token_netuid,
            block=deposit.block,
            event_ordinal=deposit.event_ordinal,
            extrinsic_index=deposit.extrinsic_index,
            block_hash=deposit.block_hash,
            hotkey=deposit.hotkey,
            origin_hotkey=deposit.origin_hotkey,
            owner_at_deposit=deposit.owner_at_deposit,
            unattributed_reason=deposit.unattributed_reason,
        )
        observe_receipt(deposit)
        return
    store.record_noah_deposit(
        coldkey=deposit.coldkey,
        block_timestamp=block_timestamp,
        amount_rao=deposit.amount_rao,
        block=deposit.block,
        event_ordinal=deposit.event_ordinal,
        extrinsic_index=deposit.extrinsic_index,
        block_hash=deposit.block_hash,
        carried_by=deposit.carried_by,
        hotkey=deposit.hotkey,
        origin_hotkey=deposit.origin_hotkey,
        owner_at_deposit=deposit.owner_at_deposit,
        unattributed_reason=deposit.unattributed_reason,
    )

    observe_receipt(deposit)


def _positive(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunRefused(f"{what} came in as {value!r}, which is not a whole number.")
    if value <= 0:
        raise RunRefused(f"{what} must be greater than zero, got {value}.")
    return value
