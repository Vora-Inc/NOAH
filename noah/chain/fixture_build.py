"""Build scoring fixtures from verified history and the current seat map.

The store and requested fixture must share a day anchor. Required days must be
present. Entry qualification and attribution are per miner hotkey, including when
several seats share a coldkey. Unreadable inputs produce refusals or explicitly
withheld rows, not fabricated zero balances."""

from .foreign_value import ForeignDepositUnpriced
from .history_check import require_whole_history
from .nofault import EmissionStateNotObserved, no_fault_for
from .sources import Refusal
from .store import ColdkeyNotSampled, DayNotScanned

SCHEMA = "noah-fixture/6"


class MissingFromStore(Refusal):
    """A fixture field that cannot be read and must not be invented."""


class UnaccountedDeposit(Refusal):
    """The attributed fixture rows do not account for the stored contribution total.

    Abort the fixture build instead of treating this as an isolated missing-input row.
    Unexplained value must remain visible for investigation."""


def _text(value, what):
    if not isinstance(value, str) or not value.strip():
        raise MissingFromStore(f"{what} came in as {value!r}, which is not usable text.")
    return value


def _whole(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise MissingFromStore(f"{what} came in as {value!r}, which is not a whole number.")
    if value < 0:
        raise MissingFromStore(f"{what} is negative ({value}).")
    return value


def _seat_overrides(mapping, what):
    """Validate overrides as {coldkey: {hotkey: value}} before looking up a seat."""
    for coldkey, per_hotkey in mapping.items():
        _text(coldkey, what)
        if not isinstance(per_hotkey, dict):
            raise MissingFromStore(
                f"{what} {coldkey!r} came in as {per_hotkey!r}, which is not a "
                f"{{hotkey: value}} mapping. Entry deposits and registration days are "
                f"per-seat now (D-138 reopened, issue #112): every coldkey's own entry "
                f"in this override must itself be a mapping from that coldkey's hotkey "
                f"to its own value."
            )
        for hotkey in per_hotkey:
            _text(hotkey, f"a hotkey key under {what} {coldkey!r}")
    return mapping


def build_fixture(
    store,
    name,
    eval_day,
    day_epoch_ts,
    dials,
    seats,
    *,
    held_out=(),
    foreign_rule=None,
    entry_deposits=None,
    registration_days=None,
):
    """Build a fixture from complete stored history and validated seat inputs.

    The requested anchor must equal the store anchor. Required days and each seat's
    entry evidence must be available; missing reads are not replaced by zero."""
    _text(name, "the fixture name")
    _whole(eval_day, "the evaluation day")
    _whole(day_epoch_ts, "the day anchor")
    if day_epoch_ts != store.day_anchor_ts:
        raise MissingFromStore(
            f"this fixture says its day anchor is {day_epoch_ts} and the store was built "
            f"with {store.day_anchor_ts}. Every day in the store is counted from the "
            f"anchor it was built with, so a fixture under another one puts the same pay in "
            f"different days."
        )

    # The evaluation day has to have been READ. Checked here, before anything
    # walks the wallets, because the walk is what used to do the checking and it
    # only happens when there is a wallet to walk: an empty store produced a
    # well-formed fixture saying nobody earned anything, over a day nothing had
    # looked at. That guard fired when there was something to lose and stayed
    # silent when everything was missing, which is the wrong way round, and it
    # is what criterion 2 means by a stopped run writing no fixture.
    if not store.day_was_scanned(eval_day):
        raise DayNotScanned(
            f"a fixture was asked for evaluation day {eval_day} and this store has never "
            f"read that day. An empty fixture over an unread day is the same bytes as a "
            f"fixture over a day in which nobody earned anything, and only one of those is "
            f"a result."
        )

    # Whether the subnet was emitting has to be resolvable too, for the same
    # reason and checked the same way: once per fixture, upfront, rather than
    # once per coldkey deep inside the walk below (where `no_fault_for` asks
    # this same question of `Store.subnet_was_emitting`, per day, to decide
    # `noFault`). `subnet_was_emitting` takes no coldkey argument -- whether
    # nobody has recorded the subnet's first emission block (or its
    # confirmed absence) is a fact about the STORE, not about any one
    # wallet, so every coldkey in the walk would hit the identical question
    # and get the identical answer. Checking only `eval_day`, exactly the
    # scope the guard above already uses, is enough to cover every earlier
    # day any coldkey's own range could reach: recording the subnet's start
    # makes every day resolve (before and after it alike), and recording
    # only its absence through some day makes every day AT OR BEFORE that
    # one resolve too, so a resolved `eval_day` -- being the latest day
    # anyone's range ever reaches -- means every day at or before it is
    # resolved as well. A run where this was never recorded would otherwise
    # fail this exact question once per coldkey, deep inside
    # `_rows_for_coldkey`, as a wall of identical per-wallet refusals that
    # reads like ordinary filtering instead of the one clear setup gap it
    # actually is.
    if store.subnet_was_emitting(eval_day) is None:
        raise EmissionStateNotObserved(
            f"nobody has recorded when the subnet began emitting (or confirmed it had not, "
            f"through some day), so evaluation day {eval_day} cannot be judged for "
            f"no-fault. Read the subnet's first emission block and record it: one reading "
            f"answers every day for every coldkey."
        )

    require_whole_history(store, through_day=eval_day)

    held_out = frozenset(held_out)
    entry_deposits = entry_deposits or {}
    registration_days = registration_days or {}
    everybody = sorted(
        set(seats)
        | set(store.wallets_seen())
        | set(_seat_overrides(entry_deposits, "an entry_deposits"))
        | set(_seat_overrides(registration_days, "a registration_days"))
    )
    miners = []
    entry_deposit_refusals = []
    for coldkey in everybody:
        if coldkey in held_out:
            continue
        try:
            first = _first_activity_day(store, coldkey, eval_day, entry_deposits, registration_days)
        except MissingFromStore as exc:
            entry_deposit_refusals.append(f"{coldkey}: {exc}")
            continue
        if first is None or first > eval_day:
            continue
        carries_entry_deposit = any(entry_deposits.get(coldkey, {}).values())
        # The hold-out, finally reachable. It used to be an argument no caller
        # passed, which is why every miner was emitted with zeroes instead, and
        # the test covering it passed the argument itself so nothing noticed.
        # The store refuses to answer for a wallet whose lock was never read or
        # whose day holds an unreadable epoch, and that refusal is the hold-out.
        lock_hold_out_reason = _lock_hold_out(store, coldkey, first, eval_day)
        if lock_hold_out_reason is not None:
            if carries_entry_deposit:
                entry_deposit_refusals.append(f"{coldkey}: {lock_hold_out_reason}")
            continue
        # `_rows_for_coldkey` can still refuse -- a holding sample missing
        # for the evaluation day, or a cumulative lock reading that does
        # not add up -- over facts about THIS coldkey alone, never about
        # anyone else in `everybody`. `_try_rows_for_coldkey` turns that
        # refusal into a skip for this one coldkey instead of letting it
        # propagate out of this loop and abort every other coldkey's rows
        # in the same batch, the same isolation `_lock_hold_out` above
        # already gives the collateral-lock case. `withheld_from` names a
        # coldkey skipped this way; it calls the identical helper, so the
        # two can never compute a different answer to "who's missing."
        rows, reason = _try_rows_for_coldkey(
            store,
            coldkey,
            first,
            eval_day,
            seats,
            foreign_rule,
            entry_deposits,
            registration_days,
        )
        if reason is not None:
            # Same fail-closed extension as `_lock_hold_out` just above,
            # for the identical reason: a coldkey's own missing holding
            # sample or cumulative-lock inconsistency stays isolated for
            # every coldkey with NO entry deposit, unchanged, but must not
            # silently take a REAL entry deposit down with it.
            if carries_entry_deposit:
                entry_deposit_refusals.append(f"{coldkey}: {reason}")
            continue
        miners.extend(rows)

    if entry_deposit_refusals:
        raise MissingFromStore("this fixture cannot be built: " + "; ".join(entry_deposit_refusals))

    return {
        "schema": SCHEMA,
        "name": name,
        "dayEpochTs": str(day_epoch_ts),
        "evalDay": str(eval_day),
        "dials": dials,
        "miners": miners,
    }


def withheld_from(
    store,
    eval_day,
    seats,
    held_out=(),
    *,
    foreign_rule=None,
    entry_deposits=None,
    registration_days=None,
):
    """Describe miners withheld from a fixture and the recorded reason for each."""
    _whole(eval_day, "the evaluation day")
    # Same upfront check `build_fixture` makes, for the same reason: whether
    # the subnet was emitting is a fact about the STORE, not about any one
    # coldkey, so leaving it to surface per-coldkey inside the loop below
    # would raise on whichever coldkey the sort order reaches first, with no
    # explanation of why every other one would have failed identically.
    if store.subnet_was_emitting(eval_day) is None:
        raise EmissionStateNotObserved(
            f"nobody has recorded when the subnet began emitting (or confirmed it had not, "
            f"through some day), so evaluation day {eval_day} cannot be judged for "
            f"no-fault. Read the subnet's first emission block and record it: one reading "
            f"answers every day for every coldkey."
        )
    held_out = frozenset(held_out)
    entry_deposits = entry_deposits or {}
    registration_days = registration_days or {}
    missing = []
    for coldkey in sorted(held_out):
        missing.append(
            {
                "coldkey": coldkey,
                "reason": (
                    f"{coldkey} is explicitly held out: its pay moved through a "
                    f"collateral lock this reader cannot yet split into locked "
                    f"and unlocked figures, so it is left out of the fixture "
                    f"entirely rather than scored on figures this reader cannot "
                    f"produce."
                ),
            }
        )
    for coldkey in sorted(
        set(seats)
        | set(store.wallets_seen())
        | set(_seat_overrides(entry_deposits, "an entry_deposits"))
        | set(_seat_overrides(registration_days, "a registration_days"))
    ):
        if coldkey in held_out:
            continue
        try:
            first = _first_activity_day(store, coldkey, eval_day, entry_deposits, registration_days)
        except MissingFromStore as exc:
            missing.append({"coldkey": coldkey, "reason": str(exc)})
            continue
        if first is None or first > eval_day:
            continue
        reason = _lock_hold_out(store, coldkey, first, eval_day)
        if reason is None:
            _, reason = _try_rows_for_coldkey(
                store,
                coldkey,
                first,
                eval_day,
                seats,
                foreign_rule,
                entry_deposits,
                registration_days,
            )
        if reason is not None:
            missing.append({"coldkey": coldkey, "reason": reason})
    return {
        "withheld": missing,
        "locked_arrival_observed": store.any_locked_arrival(),
    }


def _first_activity_day(store, coldkey, eval_day, entry_deposits, registration_days):
    """Find the earliest attributed activity for the seat, including entry contribution.

    Do not inherit an unrelated hotkey's earlier activity merely because it shares
    a coldkey."""
    first = store.first_activity_day(coldkey)

    per_hotkey_deposits = {
        hotkey: _whole(value, f"an entry deposit for {coldkey}'s hotkey {hotkey}")
        for hotkey, value in entry_deposits.get(coldkey, {}).items()
    }
    per_hotkey_registrations = {
        hotkey: _whole(value, f"a registration day for {coldkey}'s hotkey {hotkey}")
        for hotkey, value in registration_days.get(coldkey, {}).items()
    }

    # `firstActivityDay` stays a coldkey-level fact: folded in
    # here from whichever of this coldkey's SEATS carries the earliest
    # anchored entry deposit, alongside whatever the store observed
    # directly. Sorted for a deterministic refusal order when more than one
    # seat's entry deposit is bad; it never changes which one(s) refuse.
    for hotkey in sorted(per_hotkey_deposits):
        entry_deposit_rao = per_hotkey_deposits[hotkey]
        if not entry_deposit_rao:
            continue
        # `_rows_for_coldkey`'s own documented default: an entry deposit
        # with no explicit registration day takes `first` as its
        # registration day instead. Read that default HERE too, not just
        # applied later, so a future-day `first` cannot silently defeat
        # the very check that exists to catch a future registration day.
        registration_day = per_hotkey_registrations.get(hotkey)
        effective_registration_day = registration_day if registration_day is not None else first
        if effective_registration_day is None:
            raise MissingFromStore(
                f"{coldkey}'s hotkey {hotkey} has a nonzero entry deposit but no "
                f"registration day to anchor it, and nothing else establishes this "
                f"coldkey's first activity day either. An entry deposit is real, "
                f"money-bearing input; dropping this seat as though it were genuinely "
                f"idle would discard it with no trace. Supply a registration day for "
                f"it, or drop the entry deposit."
            )
        if effective_registration_day > eval_day:
            raise MissingFromStore(
                f"{coldkey}'s hotkey {hotkey} has a nonzero entry deposit registered "
                f"for day {effective_registration_day}, which is beyond evalDay "
                f"{eval_day}. A registration cannot land after the day being "
                f"evaluated. Honoring it would silently drop this seat's money-bearing "
                f"entry deposit with no trace, indistinguishable from a seat that "
                f"genuinely has not started yet. Correct the registration day (or, if "
                f"it was never set explicitly, the store's recorded first activity day "
                f"for this coldkey) so it falls at or before evalDay."
            )
        first = (
            effective_registration_day if first is None else min(first, effective_registration_day)
        )
    return first


def _lock_hold_out(store, coldkey, first, eval_day):
    """Why this wallet cannot be scored on locked pay this span, or None."""
    # A registration lock read before `first` is carried into day `first` as an
    # opening balance (`_rows_for_coldkey`), so one marked unreadable there
    # would be a silently short opening figure: hold the wallet out instead.
    earlier = store.locked_pay_unreadable_before(coldkey, first)
    if earlier is not None:
        return earlier
    for day in range(first, eval_day + 1):
        named = store.locked_pay_unreadable(coldkey, day)
        if named is not None:
            return named
        # Only a day that actually paid needs a reading. A day with no pay has
        # nothing to capture and nothing to release, so requiring one there
        # would hold out every wallet that ever sat out a day.
        if store.attributed_pay(coldkey, day) and not store.locked_pay_was_read(coldkey, day):
            return (
                f"{coldkey} has no collateral reading for day {day}. Pay that arrives "
                f"locked is not pay that arrived spendable, and this reader will not publish "
                f"a zero it did not read. Every other wallet is scored and paid normally."
            )
    return None


def _rows_for_coldkey(
    store,
    coldkey,
    first,
    eval_day,
    seats,
    foreign_rule,
    entry_deposits,
    registration_days,
):
    """Produce one fixture row per current seat and separate unclaimed historical pay."""
    hotkey_uids = seats.get(coldkey, {})

    # Gathered before the loop so the cumulative rule can be checked across the
    # whole span rather than one day at a time, and ONCE per coldkey rather
    # than once per seat: cumulative unlocks may never exceed cumulative locked
    # arrivals, and that is a fact about the wallet, not about any one seat.
    locked_by_day = {}
    arrived = unlocked = 0
    # Locked arrivals recorded before `first` (the registration lock, when the
    # wallet's first activity day is later than its registration day) are
    # carried into day `first` as an opening balance, so this cumulative rule
    # and the parser's (scoring/fixture_io.py) both see them. The engine
    # ignores paidLockedArrivalRao until an unlock, so scores do not move.
    opening = store.locked_arrival_before(coldkey, first)
    for day in range(first, eval_day + 1):
        locked_by_day[day] = store.locked_pay(coldkey, day)
        if day == first and opening:
            locked_by_day[day] = (locked_by_day[day][0] + opening, locked_by_day[day][1])
        arrived += locked_by_day[day][0]
        unlocked += locked_by_day[day][1]
        if unlocked > arrived:
            raise MissingFromStore(
                f"through day {day}, {coldkey} has {unlocked} of pay recorded as unlocking "
                f"against {arrived} recorded as having arrived locked. Pay cannot unlock "
                f"before it arrives, so one of the two readings is wrong and neither may be "
                f"published. Note that this rule passes on its own for any wallet whose "
                f"figures are all zero, which is why the run also reports whether the "
                f"population contained a locked arrival at all."
            )

    # The coldkey-level half of every day's record, computed once and copied
    # into each row below. `paidRao` is deliberately absent: it is the one
    # field that is not this coldkey's alone to answer.
    shared_days = {}
    for day in range(first, eval_day + 1):
        shared_days[day] = {
            "paidLockedArrivalRao": str(locked_by_day[day][0]),
            "payUnlockedRao": "0",
            "noFault": no_fault_for(store, coldkey, day),
        }

    def foreign_for(hotkey, day):
        """A seat's foreign-token credit for the day, in NOAH rao (issue #16).

        Per seat, never wallet-wide: a wallet-wide figure copied onto every seat's row
        would credit one receipt once per seat. Only receipts naming this seat, sent to
        the designated validator, count. No rule means no foreign valuation is wired,
        and the figure is a stated zero."""
        if foreign_rule is None:
            return 0
        if store.cumulative_noah_deposits_for_hotkey(coldkey, hotkey, day) == 0:
            # No genuine NOAH from this seat through this day, so the engine's cap leaves
            # it no room: its foreign receipts earn nothing and are not even priced, so a
            # receipt nobody can earn from cannot hold up a day.
            return 0
        try:
            return store.foreign_deposit_equivalent_for_hotkey(coldkey, hotkey, day, foreign_rule)
        except ForeignDepositUnpriced as refusal:
            # Retry the evaluation being built, even when a historical receipt is
            # the missing input. The receipt keeps its original UTC day.
            refusal.day = eval_day
            raise

    per_hotkey_deposits = entry_deposits.get(coldkey, {})
    per_hotkey_registrations = registration_days.get(coldkey, {})

    # Grouped by hotkey unconditionally, whether or not this coldkey
    # currently holds any seat: a day's pay stays attached to whoever earned
    # it regardless of what `seats` says NOW, and the null row built below
    # (for every coldkey that either has no current seat, or has pay a
    # current seat cannot claim) reads straight out of this same dict rather
    # than a second, separately-computed figure.
    pay_by_seat = {
        day: store.attributed_pay_by_seat(coldkey, day) for day in range(first, eval_day + 1)
    }

    cumulative_pay_hotkeys = set()
    ever_seen_through = {}
    for day in range(first, eval_day + 1):
        cumulative_pay_hotkeys |= set(pay_by_seat[day])
        seen_by_day = set(cumulative_pay_hotkeys)
        seen_by_day |= store.deposit_origin_hotkeys(coldkey, through_day=day)
        for hotkey, reg_day in per_hotkey_registrations.items():
            if reg_day <= day:
                seen_by_day.add(hotkey)
        for hotkey in per_hotkey_deposits:
            effective_reg_day = per_hotkey_registrations.get(hotkey, first)
            if effective_reg_day <= day:
                seen_by_day.add(hotkey)
        ever_seen_through[day] = seen_by_day

    current_hotkeys = set(hotkey_uids)

    all_ever_hotkeys = ever_seen_through[eval_day] | current_hotkeys
    single_hotkey_forever = len(all_ever_hotkeys) <= 1
    only_ever_hotkey = next(iter(all_ever_hotkeys), None)

    vacated_hotkeys = sorted(ever_seen_through[eval_day] - current_hotkeys)

    def _vacated(day, per_hotkey):
        if single_hotkey_forever:
            if only_ever_hotkey is not None and only_ever_hotkey not in current_hotkeys:
                return per_hotkey(only_ever_hotkey, day)
            return 0
        seen_by_day = ever_seen_through[day]
        if len(seen_by_day) > 1:
            return sum(per_hotkey(hotkey, day) for hotkey in vacated_hotkeys)
        if len(seen_by_day) == 1:
            (only_hotkey,) = seen_by_day
            if only_hotkey not in current_hotkeys:
                # That one hotkey is not a live seat today: its own
                # deposits belong on this, the null row, not a live seat
                # that did not exist yet as of this day.
                return per_hotkey(only_hotkey, day)
        return 0

    def _vacated_deposit(day):
        return _vacated(
            day, lambda hotkey, d: store.noah_deposits_for_hotkey(coldkey, hotkey, d)
        )

    vacated_deposits = {day: _vacated_deposit(day) for day in range(first, eval_day + 1)}

    rows = []
    if hotkey_uids:
        try:
            holding_by_hotkey = store.holding_by_hotkey(coldkey, eval_day)
        except ColdkeyNotSampled as exc:
            # The store already told the two cases apart (see
            # Store.holding_by_hotkey): a wallet with no sample at all this
            # day refuses here, and only here. Below this point every
            # absence from `holding_by_hotkey` is the other case, a hotkey
            # the store looked at and found holding nothing, which is a real
            # answer, not a gap.
            raise MissingFromStore(
                f"no holding was read for {coldkey} in day {eval_day}: {exc}. The holding "
                f"decides whether every one of this wallet's seats is credited at all, so a "
                f"wallet with no reading this day cannot be scored on any of them."
            ) from exc

        # Sorted by uid, not by hotkey text: uid is the field both scoring
        # engines already sort their output by, and the fixture's own row
        # order is not otherwise part of the contract, so this keeps the two
        # aligned.
        for hotkey, uid in sorted(hotkey_uids.items(), key=lambda pair: pair[1]):
            # A hotkey missing here is one `holding_by_hotkey` sampled and
            # found holding nothing across the whole day (the
            # wallet-never-sampled case already raised above), so it is
            # credited a real zero rather than refused: the ordinary shape of
            # an inactive-but-registered seat.
            hold_rao = holding_by_hotkey.get(hotkey, 0)
            seat_entry_deposit_rao = _whole(
                per_hotkey_deposits.get(hotkey, 0),
                f"an entry deposit for {coldkey}'s hotkey {hotkey}",
            )
            seat_registration_day = _whole(
                per_hotkey_registrations.get(hotkey, first),
                f"a registration day for {coldkey}'s hotkey {hotkey}",
            )
            days = {}
            for day in range(first, eval_day + 1):
                record = dict(shared_days[day], paidRao=str(pay_by_seat[day].get(hotkey, 0)))
                own_deposit = str(store.noah_deposits_for_hotkey(coldkey, hotkey, day))
                # Whether this row claims its own seat's deposits on this day. The foreign
                # figure follows exactly the same decision as the NOAH one (issue #16).
                claims_own = True
                if not single_hotkey_forever:
                    seen_by_day = ever_seen_through[day]
                    if len(seen_by_day) > 1:
                        pass
                    elif len(seen_by_day) == 1 and hotkey not in seen_by_day:
                        claims_own = False
                    elif len(seen_by_day) == 0:
                        # No hotkey identified at all as of this day: nobody has
                        # been established as the owner of whatever reading
                        # exists, so no row -- this one included -- claims it.
                        claims_own = False
                    # else: exactly one hotkey existed as of this day and it IS
                    # this one: its own named deposits, nothing wallet-wide.
                record["noahDepositRao"] = own_deposit if claims_own else "0"
                record["foreignDepositNoahEquivRao"] = str(
                    foreign_for(hotkey, day) if claims_own else 0
                )
                days[str(day)] = record
            rows.append(
                {
                    "coldkey": coldkey,
                    "uid": uid,
                    "holdRao": str(
                        _whole(hold_rao, f"the holding for {coldkey}'s hotkey {hotkey}")
                    ),
                    "firstActivityDay": str(first),
                    "entryDepositRao": str(seat_entry_deposit_rao),
                    "registrationDay": str(seat_registration_day),
                    "days": days,
                }
            )

    orphaned_paid = {
        day: sum(
            amount for hotkey, amount in pay_by_seat[day].items() if hotkey not in current_hotkeys
        )
        for day in range(first, eval_day + 1)
    }
    if not hotkey_uids or any(orphaned_paid.values()) or any(vacated_deposits.values()):
        days = {}
        for day in range(first, eval_day + 1):
            record = dict(shared_days[day], paidRao=str(orphaned_paid[day]))
            # `vacated_deposits[day]` already decides, per day, exactly what
            # this row is owed: nothing when this day's own single known
            # hotkey is a live seat (that row claims it instead), that day's
            # wallet-wide reading when it is a hotkey this coldkey no longer
            # holds, or the sum of every vacated hotkey's own attributed
            # deposits on a day two or more hotkeys are known to have
            # existed. A deposit is credited to a live seat's row or parked
            # here, never both, and one that names no hotkey credits nobody.
            record["noahDepositRao"] = str(vacated_deposits[day])
            record["foreignDepositNoahEquivRao"] = str(
                _vacated(day, foreign_for) if foreign_rule is not None else 0
            )
            days[str(day)] = record
        rows.append(
            {
                "coldkey": coldkey,
                "uid": None,
                "holdRao": "0",
                "firstActivityDay": str(first),
                "entryDepositRao": "0",
                "registrationDay": str(first),
                "days": days,
            }
        )

    if rows and not single_hotkey_forever:
        for day in range(first, eval_day + 1):
            if ever_seen_through[day]:
                continue
            expected = store.noah_deposits(coldkey, day)
            credited = sum(int(row["days"][str(day)]["noahDepositRao"]) for row in rows)
            if credited != expected:
                raise UnaccountedDeposit(
                    f"{coldkey}'s rows for day {day} credit {credited} rao of "
                    f"noahDepositRao between them, but the store says {expected} rao was "
                    f"actually deposited that day, on a day nobody -- no pay, no attributed "
                    f"deposit, no registration -- has been identified as this coldkey's yet. "
                    f"A deposit that cannot yet be matched to a specific hotkey must still "
                    f"land on the unallocated null row, not vanish."
                )
    return rows


def _try_rows_for_coldkey(
    store, coldkey, first, eval_day, seats, foreign_rule, entry_deposits, registration_days
):
    """`_rows_for_coldkey`, with its refusal turned into a return value.

    The one place both `build_fixture` (which wants the rows) and
    `withheld_from` (which wants only the reason, when there is one) ask this
    question, so a coldkey `build_fixture` skips and a coldkey `withheld_from`
    names can never be two different sets computed by two separately
    maintained pieces of logic that quietly drift apart. Returns ``(rows,
    None)`` on success or ``(None, reason)`` on refusal; exactly one side is
    ever not ``None``.
    """
    try:
        rows = _rows_for_coldkey(
            store,
            coldkey,
            first,
            eval_day,
            seats,
            foreign_rule,
            entry_deposits,
            registration_days,
        )
    except MissingFromStore as exc:
        return None, str(exc)
    return rows, None
