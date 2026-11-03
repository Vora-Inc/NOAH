"""Read-only monitoring command. Exit nonzero on stale, missing or blocked guard."""

import argparse
import json
import time
from pathlib import Path

# The serialized send waits for finalized inclusion while the guard cannot take
# the publication lock. Thirty seconds is shorter than a normal live-chain wait.
# Do not refresh a heartbeat on contention: a stuck publisher still becomes stale.
GUARD_MAX_AGE = 120


def inspect(directory, now=None):
    now = time.time() if now is None else now
    try:
        record = json.loads((Path(directory) / "starter-guard.json").read_text())
        if (
            record.get("status") not in ("idle", "watching", "ordinary", "replaced")
            or not 0 <= now - record["observed_at"] <= GUARD_MAX_AGE
            or not record.get("identity")
        ):
            raise ValueError("guard is stale or blocked")
        return dict(ok=True, status=record["status"], finalizedBlock=record["head"])
    except (OSError, ValueError, TypeError, KeyError):
        return dict(ok=False, status="missing-stale-or-blocked")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scores-dir", type=Path, required=True)
    a = p.parse_args()
    result = inspect(a.scores_dir)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
