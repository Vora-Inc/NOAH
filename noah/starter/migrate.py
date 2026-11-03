"""Create a preserved-entry snapshot from an existing complete history database.

The JSON facts file binds identities and registration days to a finalized UTC
cutoff. The live adapter independently verifies these facts before using it.
This command opens the database read-only, and never contacts or signs a chain.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from ..chain.history_check import confirm_history_contiguous
from ..chain.store import Store
from .migration import freeze, save_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-db", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    facts = json.loads(args.facts.read_text())
    conn = sqlite3.connect(args.history_db.resolve().as_uri() + "?mode=ro", uri=True)
    store = Store(conn, facts["dayAnchorTimestamp"])
    try:
        confirm_history_contiguous(
            store, through_day=facts["asOfDay"], from_day=facts["subnetRegistrationDay"]
        )
        frozen = freeze(
            store,
            facts["registrationDays"],
            genesis_hash=facts["genesisHash"],
            netuid=facts["netuid"],
            finalized_block=facts["finalizedBlock"],
            finalized_hash=facts["finalizedHash"],
            as_of_day=facts["asOfDay"],
        )
        save_new(args.out, frozen)
    finally:
        store.close()
    print("Entry snapshot saved. Live chain verification is still required before activation.")


if __name__ == "__main__":
    main()
