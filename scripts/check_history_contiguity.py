"""Read-only check that a ``history.db`` holds every day it should, with no holes.

Point it at a live store, a backup, or a store restored from one, before
trusting it (issues #237, #239). It asks no chain anything, so it works on a
machine with no node at all, and it changes nothing in the store.

    python scripts/check_history_contiguity.py --history-db PATH --through-yesterday
    python scripts/check_history_contiguity.py --history-db PATH --through-day 1200 --from-day 0

``--through-yesterday`` is the newest completed UTC day by this machine's clock,
counted from the store's own stored anchor. ``--from-day`` is the first day the
store is required to hold; leave it out and only holes inside the record the
store has are found, not days missing before it. A restored backup whose first
row is later than the history it should carry looks whole without it.

Exit status: 0 whole; 1 the record has problems (each is printed); 2 the file
could not be used as a store at all.

Scoring makes the day-presence half of this check itself, from the first day the
store's own emission start or earliest registration gives (``build_fixture``,
``noah.chain.history_check.require_whole_history``); this script adds whether
each day's block range joins the next, and lets the operator name ``--from-day``.

A missing day is reported, never repaired. It can be re-read only from a node
that still holds that state; after that it is gone, and the answer is to restore
a fuller backup, not to start the record over.
"""

import sys
import time
from pathlib import Path

_SOFTWARE_NOAH_ROOT = Path(__file__).resolve().parents[1]
if str(_SOFTWARE_NOAH_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_NOAH_ROOT))

from noah.chain.history_check import history_problems  # noqa: E402
from noah.chain.sources import Refusal  # noqa: E402
from noah.chain.store import Store  # noqa: E402
from noah.pathtext import plain_path_text  # noqa: E402


def main(argv=None, *, now=time.time, out=None) -> int:
    from noah.chain.sources import guard_endpoint_logging

    guard_endpoint_logging()
    out = out if out is not None else sys.stdout
    from noah.chain.sources import QuietArgumentParser

    parser = QuietArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--history-db", required=True, help="path to the history.db to check")
    when = parser.add_mutually_exclusive_group(required=True)
    when.add_argument("--through-day", type=int, help="the last day the store must hold")
    when.add_argument(
        "--through-yesterday",
        action="store_true",
        help="the newest completed UTC day by this machine's clock",
    )
    parser.add_argument(
        "--from-day", type=int, default=None, help="the first day the store must hold"
    )
    args = parser.parse_args(argv)

    path = args.history_db
    try:
        anchor = Store.peek_day_anchor_ts(path)
        store = Store.open(path, day_anchor_ts=anchor, read_only=True)
    except Refusal as error:
        print(f"UNUSABLE: {error}", file=out)
        return 2
    try:
        if args.through_yesterday:
            through_day = store.day_of(int(now())) - 1
            if through_day < 0:
                print("UNUSABLE: no UTC day has completed yet by this clock.", file=out)
                return 2
        else:
            through_day = args.through_day
        try:
            problems = history_problems(store, through_day=through_day, from_day=args.from_day)
        except ValueError as error:
            print(f"UNUSABLE: {error}", file=out)
            return 2
        days = store.scanned_days()
    finally:
        store.close()

    if problems:
        print(f"HISTORY NOT WHOLE through day {through_day}: {plain_path_text(path)}", file=out)
        for problem in problems:
            print(f"  - {problem}", file=out)
        return 1
    held = [day for day, _, _ in days if day <= through_day]
    first = args.from_day if args.from_day is not None else min(held)
    print(
        f"HISTORY WHOLE: days {first} through {through_day} are each recorded and join "
        f"end to start ({len(held)} scanned day(s) held): {plain_path_text(path)}",
        file=out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
