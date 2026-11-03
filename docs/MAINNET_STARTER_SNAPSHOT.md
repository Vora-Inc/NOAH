# Reproduce the Mainnet starter-entry snapshot

Mainnet subnet 82 preserves entry credit through **November 2, 2026 UTC
(day 1323)**. The new entry policy begins on **November 3 (day 1324)**. These
dates do not move when another validator installs this release. This page
reproduces that original snapshot from an operator's independently verified
history and two archival readers; it does not publish a private operator roster.

| Commitment | Required value |
| --- | --- |
| Genesis | `0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03` |
| Subnet | `82` |
| UTC day anchor | `1679270400` |
| Last block of day 1323 | `9421694` |
| Cutoff block hash | `0xe9181cbde053003de79d599acccfeca6f5972c64e557d8666c8dfc40633795f5` |
| Snapshot file SHA256 | `db45d0be4376933b85406870a18b3716ddb6d3b36bfc515da03e7b9d2e0e927c` |
| Internal payload SHA256 | `d87be4fecb5b6aee75c610d7672b46540249c8548f9acb481e4d1e42fc5115b6` |
| Records | `67`: `64` preserve `10000000000` rao each; `3` preserve zero |
| Canonical file size | `19663` bytes |

The internal digest hashes compact, sorted JSON of `payload` only. The file
digest hashes the complete envelope serialized by `noah.starter.migration.save_new`:
sorted keys, two-space indentation, and a final newline. Both must match.
The 67 records include every identity registered at the cutoff, not just the
64 with qualifying entry credit. Original receipt dates and allocations are
preserved; later payments cannot rewrite them.

## Prerequisites

Install the validator dependencies as described in the
[validator quickstart](VALIDATOR_QUICKSTART.md). Run the commands below from the
public package root, with its `noah` package available to the validator Python.

- Use two independently operated Mainnet archive RPCs. Different URL spellings
  alone do not establish independence. Both must serve the cutoff, block 9421695,
  registration facts, and the historical states needed to verify receipts.
- Use your own complete, verified Noah history database, from the current
  subnet's actual registration day through day 1323. Stop its writers for this
  preparation, or use a consistent backup restored to a private scratch location.
  Do not copy a running SQLite file with `cp`, invent scanned-day markers, or
  create an empty database to satisfy this recipe. Missing history must be
  recovered and verified separately; this recipe does not scan or repair it.
- Fill in a private `.env.validator` with those readers, `NOAH_HISTORY_DB`,
  Mainnet's genesis switch and subnet 82. Select
  `docs/parameters/profiles/mainnet-production-starter-25bps-v1.json`, generate
  matching dials, and enable direct publication plus both epoch features.
  Keep `NOAH_ENTRY_DEPOSIT_RAO=10000000000` for legacy recognition. The snapshot
  path may point to the new output file before that file exists.
- Keep generated facts and snapshot files private. They contain chain-derived
  coldkey/hotkey associations; this procedure does not need a signing key.

Do **not** use `noah.starter.preflight --freeze` to reproduce this migration.
That command selects the latest closed day, which would change the cutoff on a
later date. The following code fixes day 1323 and derives all identity facts;
there are no manually filled registration lists or digest overrides.

## Save the reproducer

Save this complete Python block as `reproduce_mainnet_starter_snapshot.py` in a
private preparation directory outside the source checkout. It reads chain and
history only. Its writes are confined to a newly created output directory; it
never overwrites a previous attempt or changes the configured history database.

<!-- mainnet-starter-reproducer:start -->
```python
import argparse
from collections import Counter
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from noah.chain.agreeing import AgreeingChain
from noah.chain.dayblocks import day_anchor
from noah.chain.history_check import confirm_history_contiguous
from noah.chain.store import Store
from noah.core.chain_adapter import _registration_days_for
from noah.core.settings import Settings, parse_env_file
from noah.scoring.parameters import load_parameters
from noah.starter.allocation import digest
from noah.starter.migration import freeze
from noah.starter.preflight import ReadOnlyHistory, inspect, seats_at
from scripts.mainnet_dials_from_profile import dials_from

GENESIS = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
ANCHOR = 1679270400
CUTOFF_DAY = 1323
CUTOFF_BLOCK = 9421694
CUTOFF_HASH = "0xe9181cbde053003de79d599acccfeca6f5972c64e557d8666c8dfc40633795f5"
PAYLOAD_SHA = "d87be4fecb5b6aee75c610d7672b46540249c8548f9acb481e4d1e42fc5115b6"
FILE_SHA = "db45d0be4376933b85406870a18b3716ddb6d3b36bfc515da03e7b9d2e0e927c"
FILE_BYTES = 19663
ENTRY = 10000000000
PROFILE = "mainnet-production-starter-25bps-v1"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def checked_settings(mapping):
    settings = Settings.from_mapping(mapping)
    require(settings.network == "finney" and settings.netuid == 82
            and settings.mainnet_enable == GENESIS, "wrong Mainnet identity")
    require(settings.entry_deposit_rao == ENTRY, "legacy recognition must remain ten NOAH")
    require(settings.publish_mode == "direct" and settings.epoch_collection
            and settings.epoch_republication, "direct epoch features are required")
    endpoints = (settings.chain_endpoint, settings.second_chain_endpoint)
    require(all(endpoints) and len(set(endpoints)) == 2, "two distinct readers are required")
    profile = load_parameters(mapping)
    require(profile.profile_version == PROFILE
            and profile.entry_deposit_per_hotkey_rao == ENTRY, "wrong starter profile")
    require(json.loads(Path(settings.dials_json).read_text()) == dials_from(profile),
            "dials differ from the selected profile")
    require(bool(settings.history_db), "an existing complete history DB is required")
    return settings


@contextmanager
def readers(settings):
    from async_substrate_interface import SubstrateInterface
    from noah.chain.live import LiveRunnerChain
    from noah.chain.sources import connect_scrubbed

    endpoints = {"primary": settings.chain_endpoint, "secondary": settings.second_chain_endpoint}
    with ExitStack() as stack:
        sources = {}
        for name, endpoint in endpoints.items():
            raw = connect_scrubbed(lambda: SubstrateInterface(
                url=endpoint, auto_discover=False, use_remote_preset=False,
                max_retries=0, retry_timeout=5), endpoint, ValueError)
            stack.callback(raw.close)
            sources[name] = LiveRunnerChain(raw, netuid=82, endpoint=endpoint)
        yield AgreeingChain(sources, endpoints)


def original_facts(chain, history):
    require(chain.genesis_hash() == GENESIS, "readers are on a different chain")
    require((day_anchor(chain) // 86400) * 86400 == ANCHOR
            and history.day_anchor_ts == ANCHOR, "UTC anchor differs")
    head = chain.head_block()
    require(head > CUTOFF_BLOCK, "cutoff successor is not finalized")
    require(chain.block_hash(CUTOFF_BLOCK) == CUTOFF_HASH, "cutoff hash differs")
    require((chain.block_timestamp(CUTOFF_BLOCK) - ANCHOR) // 86400 == CUTOFF_DAY
            and (chain.block_timestamp(CUTOFF_BLOCK + 1) - ANCHOR) // 86400 == CUTOFF_DAY + 1,
            "cutoff is not the last block of its UTC day")
    registration = chain.starter_epoch_info(head)["NetworkRegisteredAt"]
    first_day = (chain.block_timestamp(registration) - ANCHOR) // 86400
    confirm_history_contiguous(history, through_day=CUTOFF_DAY, from_day=first_day)
    ranges = {day: high for day, low, high in history.scanned_days()}
    require(ranges[CUTOFF_DAY] == CUTOFF_BLOCK, "stored cutoff day ends at a different block")
    registrations = _registration_days_for(
        chain, history, seats_at(chain, CUTOFF_BLOCK), CUTOFF_BLOCK)
    return dict(dayAnchorTimestamp=ANCHOR, subnetRegistrationDay=first_day,
                asOfDay=CUTOFF_DAY, registrationDays=registrations, genesisHash=GENESIS,
                netuid=82, finalizedBlock=CUTOFF_BLOCK, finalizedHash=CUTOFF_HASH)


def exact_snapshot(snapshot):
    payload = snapshot["payload"]
    require(snapshot["sha256"] == digest(payload) == PAYLOAD_SHA, "payload commitment differs")
    require(len(payload["records"]) == 67
            and Counter(row["amountRao"] for row in payload["records"]) == {ENTRY: 64, 0: 3},
            "preserved entry counts differ")
    wire = (json.dumps(snapshot, sort_keys=True, indent=2) + "\n").encode()
    require(hashlib.sha256(wire).hexdigest() == FILE_SHA and len(wire) == FILE_BYTES,
            "snapshot file commitment differs")
    return wire


def derive(chain, history, settings):
    facts = original_facts(chain, history)
    snapshot = freeze(history, facts["registrationDays"], genesis_hash=GENESIS, netuid=82,
                      finalized_block=CUTOFF_BLOCK, finalized_hash=CUTOFF_HASH,
                      as_of_day=CUTOFF_DAY)
    exact_snapshot(snapshot)
    inspect(chain, history, settings, snapshot)  # Existing full identity/receipt verification.
    return facts, snapshot


def run_module(module, arguments):
    subprocess.run([sys.executable, "-B", "-m", module, *arguments], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    env_bytes = args.env.read_bytes()
    mapping = parse_env_file(env_bytes.decode())
    settings = checked_settings(mapping)
    # A new directory prevents overwriting an existing snapshot, facts or receipt.
    args.out_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    old_umask = os.umask(0o077)
    try:
        with readers(settings) as chain, closing(sqlite3.connect(
                Path(settings.history_db).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            history = ReadOnlyHistory(Store(conn, ANCHOR), ANCHOR)
            facts, candidate = derive(chain, history, settings)
        facts_path = args.out_dir / "cutoff-facts.json"
        snapshot_path = args.out_dir / "starter-entry-mainnet-1323.json"
        with facts_path.open("x") as handle:
            json.dump(facts, handle, sort_keys=True, indent=2)
            handle.write("\n")
        run_module("noah.starter.migrate", ["--history-db", settings.history_db,
                   "--facts", str(facts_path), "--out", str(snapshot_path)])
        require(snapshot_path.read_bytes() == exact_snapshot(candidate),
                "migration CLI output differs from the independently checked candidate")
        require(args.env.read_bytes() == env_bytes, "configuration changed during preparation")
        run_module("noah.starter.preflight", ["--env", str(args.env),
                   "--snapshot", str(snapshot_path)])
        final_bytes = snapshot_path.read_bytes()
        require(final_bytes == exact_snapshot(json.loads(final_bytes)),
                "snapshot file is not the committed canonical serialization")
        require(args.env.read_bytes() == env_bytes, "configuration changed during verification")
        receipt = dict(status="verified-original-cutoff", cutoff_day=CUTOFF_DAY,
                       cutoff_block=CUTOFF_BLOCK, payload_sha256=PAYLOAD_SHA,
                       file_sha256=FILE_SHA, records=67, preserved_entries=64, zero_entries=3)
        with (args.out_dir / "VERIFIED.json").open("x") as handle:
            json.dump(receipt, handle, sort_keys=True, indent=2)
            handle.write("\n")
        print(json.dumps(receipt, sort_keys=True))
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    main()
```
<!-- mainnet-starter-reproducer:end -->

## Run and accept only a verified result

Use a **new** directory whose parent is private and already exists. Set
`NOAH_HISTORY_DB` in the environment file to the complete database used for this
preparation. `PYTHONPATH` below points to the public package root, not the directory
where the reproducer was saved.

```bash
PYTHONPATH="$PWD" .venv-validator/bin/python -B \
  /path/to/private/reproduce_mainnet_starter_snapshot.py \
  --env /path/to/private/.env.validator \
  --out-dir /path/to/private/original-mainnet-cutoff-1323
```

The code invokes these maintained tools after deriving and checking the fixed
facts; do not substitute yesterday's cutoff:

```bash
python -B -m noah.starter.migrate --history-db /path/to/complete/history.db \
  --facts /path/to/private/original-mainnet-cutoff-1323/cutoff-facts.json \
  --out /path/to/private/original-mainnet-cutoff-1323/starter-entry-mainnet-1323.json
python -B -m noah.starter.preflight --env /path/to/private/.env.validator \
  --snapshot /path/to/private/original-mainnet-cutoff-1323/starter-entry-mainnet-1323.json
```

Those two commands illustrate what the reproducer already runs; do not rerun the
migration over its existing output. Accept the result only if the entire process
exits zero and `VERIFIED.json` says `verified-original-cutoff` with both commitments
above. On failure, preserve the attempt and investigate the named mismatch or
missing history. A leftover snapshot without the verification receipt is not an
accepted result. Never change the constants or remove failed state to force a pass.

Set `NOAH_STARTER_ENTRY_SNAPSHOT` to the verified snapshot's permanent path and
back it up with the corresponding configuration and history. Follow the
[starter policy](STARTER_ENTRY.md) and [validator startup](VALIDATOR_QUICKSTART.md)
instructions for the independent live guard, health monitoring and updater.
This reproduction does not start those processes, load wallets, sign, publish,
restore canonical state or authorize an existing validator's migration.
