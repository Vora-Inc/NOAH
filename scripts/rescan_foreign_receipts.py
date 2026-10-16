"""Plan or explicitly apply receipt-only schema-17 recovery on a history store.

Default mode is offline and read-only, including for schema 17. --apply explicitly
migrates the selected store and replays original pending ranges through both readers.
Use a disposable copy first; no wallet, key, scoring or submission path is loaded.
Apply requires --publisher-lock and holds its exclusive nonblocking flock throughout
migration/replay. On a managed validator this MUST be the same publisher.lock used
by validator-run.sh, with that service stopped. A disposable copy uses its own
scratch publisher lock; a different lock does not coordinate with the live writer.
"""
import fcntl
import os
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from noah.chain.foreign_rescan import plan_foreign_rescans, rescan_foreign_receipts, verify_registration_floor
from noah.chain.sources import Refusal, QuietArgumentParser, guard_endpoint_logging
from noah.chain.store import Store
from noah.chain.foreign_rescan import build_dual_sourced_day_chain


def main(argv=None, *, out=None):
    guard_endpoint_logging()
    out = out or sys.stdout
    parser = QuietArgumentParser(description=__doc__)
    parser.add_argument('--history-db', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--publisher-lock', help='shared validator flock path, or own disposable-copy lock')
    parser.add_argument('--publisher-lock-fd', type=int, help='inherited release-engine lock descriptor; same path required')
    parser.add_argument('--through-day', type=int)
    parser.add_argument('--max-days', type=int, default=1)
    parser.add_argument('--netuid', type=int)
    parser.add_argument('--ark-address')
    parser.add_argument('--validator-hotkey')
    parser.add_argument('--floor-block', type=int)
    parser.add_argument('--expected-genesis')
    parser.add_argument('--author-env', default='NOAH_AUTHOR_WS')
    parser.add_argument('--reader-env', default='NOAH_READER_WS')
    args = parser.parse_args(argv)
    started = time.monotonic()
    def report(**data):
        print(json.dumps(dict(elapsed_seconds=round(time.monotonic()-started, 3), **data)),
              file=out, flush=True)
    chain = store = None
    lock_fd = None
    try:
        if args.max_days < 1 or (args.through_day is not None and args.through_day < 0):
            raise ValueError('max-days must be positive and through-day nonnegative')
        pending = plan_foreign_rescans(args.history_db)
        selected = [row for row in pending if args.through_day is None or row[0] <= args.through_day][:args.max_days]
        report(outcome='plan', pending_days=len(pending), selected=selected,
               blocks=sum(last-first+1 for _,first,last in selected if first is not None))
        if not args.apply or not selected:
            return 0
        if any(first is None or last is None for _,first,last in selected):
            raise ValueError('a pending day has no original scanned range; investigate it explicitly')
        if not all((args.netuid is not None and args.netuid > 0,
                    args.floor_block is not None and args.floor_block > 0,
                    args.ark_address, args.validator_hotkey, args.expected_genesis, args.publisher_lock)):
            raise ValueError('apply requires netuid, floor-block, Ark address, validator hotkey, expected-genesis and publisher-lock')
        if Path(args.publisher_lock).resolve() == Path(args.history_db).resolve():
            raise ValueError('publisher lock must be separate from the history database')
        if args.publisher_lock_fd is None:
            lock_fd = os.open(args.publisher_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        else:
            lock_fd = os.dup(args.publisher_lock_fd)
            descriptor, named = os.fstat(lock_fd), os.lstat(args.publisher_lock)
            if (not stat.S_ISREG(descriptor.st_mode) or not stat.S_ISREG(named.st_mode)
                    or (descriptor.st_dev, descriptor.st_ino) != (named.st_dev, named.st_ino)):
                raise ValueError('inherited publisher lock does not match the named regular file')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('publisher lock is held; stop the owning validator before recovery')
        chain = build_dual_sourced_day_chain(netuid=args.netuid,
                    author_env_var=args.author_env, reader_env_var=args.reader_env)
        if chain.genesis_hash() != args.expected_genesis:
            raise ValueError('both readers must match the expected chain genesis')
        verify_registration_floor(chain, netuid=args.netuid, floor_block=args.floor_block)
        anchor = Store.peek_day_anchor_ts(args.history_db)
        store = Store.create(args.history_db, day_anchor_ts=anchor)
        for day,first,last in selected:
            rescan_foreign_receipts(store,chain,day=day,from_block=first,to_block=last,
                ark_coldkey=args.ark_address,designated_validator_hotkey=args.validator_hotkey,
                netuid=args.netuid,floor_block=args.floor_block,progress=report)
        report(outcome='complete', remaining=store.pending_foreign_rescans())
        return 0
    except (Refusal, ValueError, OSError, sqlite3.Error) as exc:
        report(outcome='refused', reason=str(exc))
        return 1
    finally:
        if store is not None:
            store.close()
        if chain is not None:
            for source in chain._sources.values():
                try:
                    source._connection.close()
                except Exception:
                    pass
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
