"""Require the stored history to contain every needed calendar day.

Scoring must not silently turn a missing scan into a zero-activity day. A recorded
scan marker is evidence of ingestion, not proof that a node retains old state."""

from .sources import Refusal

DAY_ZERO_FIRST_BLOCK = 1


class HistoryGap(Refusal):
    """The store's record of scanned days has a hole, an overlap or a wrong start.

    ``problems`` lists every one found, in plain sentences, so an operator sees
    the whole picture from one run. Nothing was changed and nothing was invented.
    A ``Refusal``, so the scoring loop records it and stops that period rather than
    crashing, exactly as it does for a day that was never scanned.
    """

    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__(
            f"this store's history is not whole ({len(self.problems)} problem"
            f"{'' if len(self.problems) == 1 else 's'}): " + " ".join(self.problems)
        )


def _span(first, last):
    return f"day {first}" if first == last else f"days {first} through {last}"


def history_problems(store, *, through_day, from_day=None, check_blocks=True):
    """Every way this store's scanned days fail to form one unbroken record from
    ``from_day`` through ``through_day``, as plain sentences. Empty means whole.

    ``from_day`` is the first day the store is required to hold. Left out, it is
    the earliest day the store holds, which checks the record it has for holes
    but cannot notice days missing before it. A caller that knows where its own
    history begins (day 0, or its entry-deposit day) must say so, because a
    store restored from a later backup looks whole from its own first row.

    ``check_blocks=False`` checks only that every day has a recorded scan, and
    skips the questions about the block ranges (backward, overlapping, not
    starting at block 1 on day 0). Scoring uses it: a missing day is what
    silently begins a miner again, while the block joins are the operator
    script's question (``scripts/check_history_contiguity.py``).

    Days after ``through_day`` are ignored.
    """
    if isinstance(through_day, bool) or not isinstance(through_day, int) or through_day < 0:
        raise ValueError(f"through_day must be a whole day number, not {through_day!r}.")
    if from_day is not None and (
        isinstance(from_day, bool) or not isinstance(from_day, int) or from_day < 0
    ):
        raise ValueError(f"from_day must be a whole day number, not {from_day!r}.")

    rows = {day: (low, high) for day, low, high in store.scanned_days() if day <= through_day}
    if not rows:
        return [
            f"the store records no scanned day at or before day {through_day}, so it "
            f"holds none of the history that was asked for."
        ]

    first_required = min(rows) if from_day is None else from_day
    if first_required > through_day:
        raise ValueError(f"from_day {first_required} is after through_day {through_day}.")

    problems = []
    previous_high = None
    missing_from = None
    for day in range(first_required, through_day + 1):
        row = rows.get(day)
        if row is None:
            if missing_from is None:
                missing_from = day
            continue
        if missing_from is not None:
            problems.append(_missing_sentence(missing_from, day - 1))
            missing_from = None
        if not check_blocks:
            continue
        low, high = row
        if low > high:
            problems.append(f"day {day} records a range that runs backward ({low} to {high}).")
        if day == 0 and low != DAY_ZERO_FIRST_BLOCK:
            problems.append(
                f"day 0 begins at block {low}, but the chain's first block is "
                f"{DAY_ZERO_FIRST_BLOCK}."
            )
        if previous_high is not None:
            if low > previous_high + 1:
                problems.append(
                    f"day {day} begins at block {low}, but the day before it ended at block "
                    f"{previous_high}. Blocks {previous_high + 1} through {low - 1} are in "
                    f"no recorded day."
                )
            elif low <= previous_high:
                problems.append(
                    f"day {day} begins at block {low}, but the day before it ended at block "
                    f"{previous_high}. The two days record some of the same blocks."
                )
        previous_high = high
    if missing_from is not None:
        problems.append(_missing_sentence(missing_from, through_day))
    return problems


def _missing_sentence(first, last):
    return (
        f"{_span(first, last)} {'has' if first == last else 'have'} no recorded scan. "
        f"Only a node that still holds that state can supply it, and none is invented here."
    )


def missing_days(store, *, through_day, from_day):
    """Return unscanned day numbers from from_day through through_day, inclusive."""
    if isinstance(through_day, bool) or not isinstance(through_day, int) or through_day < 0:
        raise ValueError(f"through_day must be a whole day number, not {through_day!r}.")
    if isinstance(from_day, bool) or not isinstance(from_day, int) or from_day < 0:
        raise ValueError(f"from_day must be a whole day number, not {from_day!r}.")
    if from_day > through_day:
        raise ValueError(f"from_day {from_day} is after through_day {through_day}.")
    held = {day for day, _, _ in store.scanned_days() if day <= through_day}
    return [day for day in range(from_day, through_day + 1) if day not in held]


def confirm_history_contiguous(store, *, through_day, from_day=None):
    """Return ``(first_day, last_day)`` if the store holds an unbroken record of
    every day from ``from_day`` through ``through_day``; raise :class:`HistoryGap`
    naming every problem if it does not."""
    problems = history_problems(store, through_day=through_day, from_day=from_day)
    if problems:
        raise HistoryGap(problems)
    days = [day for day, _, _ in store.scanned_days() if day <= through_day]
    return (min(days) if from_day is None else from_day, through_day)


def required_first_day(store):
    """``(day, reason)``: the first day this store must hold, and why, or
    ``None`` when the store records nothing that says where its history begins.

    Only facts the store itself recorded are used, never a default or a guess:
    the day the subnet began emitting (``Store.recorded_emission_start``) and
    the earliest registration block's day (``Store.earliest_registration_day``).
    Whichever is earlier is the first day. Nothing on the chain is asked.
    """
    found = []
    started = store.recorded_emission_start()
    if started is not None:
        found.append(
            (started[0], f"the store records the subnet beginning to emit on day {started[0]}")
        )
    registered = store.earliest_registration_day()
    if registered is not None:
        found.append((registered, f"the store records a seat registered on day {registered}"))
    return min(found) if found else None


def require_whole_history(store, *, through_day):
    """Refuse, with :class:`HistoryGap`, to go on if this store cannot show every
    day from the day its own records say the subnet's history began, through
    ``through_day``, joined end to start.

    This is what keeps a restart, a restore or a replacement from quietly
    beginning again. A store missing its early days scores as if each miner
    started later: the first day of activity moves forward and the earlier
    credit disappears without any error. The refusal names the day the record
    should begin and where the store's own record really begins, so the answer
    is to restore a fuller copy, never to score what is left.

    When the store records neither an emission start nor a registration, it
    has nothing that says where the history began, so only holes inside the
    record it holds are checked. That is the state of a brand-new store on a
    subnet that has not begun emitting, and it must be allowed to start. What
    that cannot notice is stated in the module docstring.

    Reads the store only.
    """
    required = required_first_day(store)
    problems = []
    from_day = None
    if required is not None and required[0] <= through_day:
        from_day = required[0]
        held = [day for day, _, _ in store.scanned_days() if day <= through_day]
        if not held or min(held) > from_day:
            began = f"day {min(held)}" if held else "no day at all"
            problems.append(
                f"The record must begin at day {from_day}, because {required[1]}, but this "
                f"store's own record begins at {began}."
            )
    problems.extend(
        history_problems(store, through_day=through_day, from_day=from_day, check_blocks=False)
    )
    if problems:
        raise HistoryGap(problems)
