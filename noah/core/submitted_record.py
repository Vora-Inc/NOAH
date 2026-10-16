"""The weights this validator actually submitted for a day (issue #291).

Three different facts are kept apart, each in its own place and under its own
name, and no tool may present one as another:

* CALCULATED: the engine's scores, ``weightsU16`` in ``scores/day-N.json``
  (written by the day-close script; sum-normalised engine output).
* SUBMITTED: this file, ``scores/day-N.submitted.json``. The vector
  `run_period` handed to the chain after the publish-time rule
  (`noah.core.vector.publishable_vector`), in whole numbers BEFORE the chain's
  own max-normalisation. Written by the validator itself, right after the
  publish call returned.
* CONFIRMED: ``scores/day-N.confirmed.json``, written by
  ``scripts/confirm_published.py`` from the chain's stored weights (that script
  is unverified against a chain: its tests stub the chain read).

The name ``day-N.submitted.json`` does not match the operator view's
``day-(\\d+)\\.json`` reader, so that reader is unaffected.

Written atomically (temp file, fsync, replace, directory fsync). Direct
publication retains its pending intent if this write fails; restart must
reconcile before sending again. The outer period also attempts this write
for operator visibility.
"""

import json
import os
import threading
from pathlib import Path

SCHEMA = "noah-submitted-weights/1"


def submitted_record_path(directory, day):
    return Path(directory) / f"day-{int(day)}.submitted.json"


def write_submitted_record(
    directory, *, day, height, vector, published, mode, hotkey, netuid, uid=None, signer=None, route=None
):
    """Write ``day-N.submitted.json`` and return its path. ``vector`` is
    ``{uid: int}``; ``published`` is the publish call's own result (block,
    extrinsic index and reveal round are recorded when it carries them)."""
    from datetime import datetime, timezone

    path = submitted_record_path(directory, day)
    payload = {
        "schema": SCHEMA,
        "day": int(day),
        "height": int(height),
        "netuid": int(netuid),
        # `hotkey` is the validator (the commit identity the chain sees); `signer` is the key
        # that signed and paid (a different account on the proxy route).
        "hotkey": hotkey,
        "commit_identity": hotkey,
        "signer": signer,
        "route": route,
        "uid": uid,
        "publish_mode": mode,
        "block": published.get("block"),
        "extrinsic_index": published.get("extrinsic_index"),
        "reveal_round": published.get("reveal_round"),
        "block_hash": published.get("block_hash"),
        "direct_intent": published.get("direct_intent"),
        "calculation_evidence": published.get("calculation_evidence"),
        "applied_weights_readback": published.get("applied_weights_readback"),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "submitted_weights_u16": {str(uid): int(w) for uid, w in sorted(vector.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
