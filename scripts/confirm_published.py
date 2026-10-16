"""Compare a day's SUBMITTED weights with what the chain finally stored (#291).

Read-only: no signing, no sends, no wallet. It reads
``scores/day-N.submitted.json`` (written by the validator at publish time) and
the chain's finalized stored weights for the validator's uid, then writes
``scores/day-N.confirmed.json``. It never reads or writes ``day-N.json`` (the
calculated scores), so the three facts stay separate:

  calculated  day-N.json            (engine output)
  submitted   day-N.submitted.json  (vector committed, whole numbers)
  confirmed   day-N.confirmed.json  (this script: submitted vs finalized chain)

Rule, as verified on nine published days: stored[uid] == round(submitted[uid] *
65535 / max(submitted)). A uid the chain stored as nonzero that the submission
did not name, or named at 0, is an extra and makes the result MISMATCH. A uid
with expected 0 that has no stored entry counts as 0.

Run it before the next publication replaces the stored weights.

UNVERIFIED AGAINST A CHAIN: the tests replace ``read_finalized`` with a stub, so the
chain read here has not been exercised by any test, and on a sealed (commit-reveal)
chain the stored weights change only at the reveal, so a MATCH means something only
after the reveal. A MATCH from this script is not evidence about interrupted-send
recovery.

    python scripts/confirm_published.py --day 15 [--endpoint ws://127.0.0.1:20944]

Exit 0 on MATCH, 1 on MISMATCH, 2 when the inputs or the chain could not be read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

SCHEMA = "noah-confirmed-weights/1"
U16_MAX = 65535


def expected_stored(submitted):
    """The chain's max-normalisation of a submitted vector: ``{uid: int}``."""
    peak = max(submitted.values(), default=0)
    if peak == 0:
        return {uid: 0 for uid in submitted}
    # round() of the exact quotient, the rule as documented (readback.py uses
    # round(w * 65535 / max)); a tie goes to the even number, as Python's round does.
    return {uid: round(Fraction(w * U16_MAX, peak)) for uid, w in submitted.items()}


def compare(submitted, stored):
    """``submitted`` and ``stored`` are ``{uid: int}``. Returns the verdict dict."""
    expected = expected_stored(submitted)
    mismatches = [
        {"uid": uid, "expected": expected[uid], "stored": stored.get(uid, 0)}
        for uid in sorted(expected)
        if stored.get(uid, 0) != expected[uid]
    ]
    extras = {uid: w for uid, w in sorted(stored.items()) if uid not in submitted and w}
    return {
        "result": "MATCH" if not mismatches and not extras else "MISMATCH",
        "mismatches": mismatches,
        "nonzero_extras": {str(uid): w for uid, w in extras.items()},
        "expected_stored": {str(uid): w for uid, w in sorted(expected.items())},
    }


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_confirmed(
    record_path, record, stored, *, finalized_block, finalized_hash, last_update, uid_hotkey=None
):
    submitted = {int(u): w for u, w in record["submitted_weights_u16"].items()}
    verdict = compare(submitted, stored)
    # Proxy route: the weights must be stored for the uid the COMMIT IDENTITY holds. When the
    # record names a commit identity and the chain's key for that uid is read, a different key
    # is a MISMATCH however the numbers compare (the effect landed for someone else, or the
    # seat changed hands). `uid_hotkey=None` means "not read", recorded as such.
    commit_identity = record.get("commit_identity") or record.get("hotkey")
    identity = {
        "commit_identity": commit_identity,
        "signer": record.get("signer"),
        "route": record.get("route"),
        "uid_hotkey_on_chain": uid_hotkey,
        "identity_checked": uid_hotkey is not None and commit_identity is not None,
    }
    if identity["identity_checked"] and uid_hotkey != commit_identity:
        verdict = {**verdict, "result": "MISMATCH", "identity_mismatch": True}
    return {
        "schema": SCHEMA,
        "day": record["day"],
        "netuid": record["netuid"],
        "uid": record["uid"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "finalized_block": finalized_block,
        "finalized_hash": finalized_hash,
        "last_update_block": last_update,
        "submitted_record": {
            "file": Path(record_path).name,
            "sha256": hashlib.sha256(Path(record_path).read_bytes()).hexdigest(),
            "height": record.get("height"),
            "block": record.get("block"),
            "reveal_round": record.get("reveal_round"),
        },
        "rule": "stored = round(submitted * 65535 / max(submitted))",
        **identity,
        "stored_weights_u16": {str(u): w for u, w in sorted(stored.items())},
        **verdict,
    }


def scrub_url(text, url):
    """``text`` with ``url`` (and its userinfo, path and query) replaced by scheme://host.

    Standalone on purpose: this script imports nothing from noah. An RPC provider
    may put the API key in the URL and the connection library echoes that URL in
    some errors, so nothing printed may carry it.
    """
    from urllib.parse import urlsplit

    text = str(text)
    try:
        parts = urlsplit(url)
        has_host = parts.scheme and parts.hostname
        label = f"{parts.scheme}://{parts.hostname}" if has_host else "(endpoint)"
        pieces = [url, parts.username, parts.password, parts.query]
        if len(parts.path) > 1:
            pieces.append(parts.path)
    except (ValueError, TypeError, AttributeError):
        return text.replace(str(url), "(endpoint)") if url else text
    for piece in sorted((p for p in pieces if p), key=len, reverse=True):
        text = text.replace(piece, label if piece == url else "(hidden)")
    return text


def _connect(endpoint):
    import logging

    from async_substrate_interface.sync_substrate import SubstrateInterface

    # The library logs "Websocket connecting to <url>" at DEBUG.
    logging.getLogger("async_substrate_interface").setLevel(logging.WARNING)
    try:
        return SubstrateInterface(endpoint)
    except Exception as exc:  # noqa: BLE001 - re-raised without the URL
        raise RuntimeError(
            f"cannot connect to the node: {type(exc).__name__}: {scrub_url(exc, endpoint)}"
        ) from None


def read_finalized(endpoint, netuid, uid):
    """Stored ``Weights[netuid, uid]`` and ``LastUpdate`` at the finalized head."""
    substrate = _connect(endpoint)
    head = substrate.get_chain_finalised_head()
    block = substrate.get_block_number(head)
    entries = substrate.query("SubtensorModule", "Weights", [netuid, uid], block_hash=head).value
    last_update = substrate.query("SubtensorModule", "LastUpdate", [netuid], block_hash=head).value
    stored = {int(u): int(w) for u, w in entries}
    update = last_update[uid] if uid < len(last_update) else None
    return stored, block, str(head), update


def read_uid_hotkey(endpoint, netuid, uid):
    """``Keys[netuid, uid]`` (the hotkey the chain says holds that uid) at the finalized head.
    Read-only; used to confirm the stored weights belong to the commit identity's seat."""
    substrate = _connect(endpoint)
    head = substrate.get_chain_finalised_head()
    value = substrate.query("SubtensorModule", "Keys", [netuid, uid], block_hash=head).value
    if not isinstance(value, str):
        # Fail closed: an unreadable owner of the uid must not become "identity not checked".
        raise ValueError(
            f"Keys[{netuid}, {uid}] read back as {type(value).__name__}, not an address; the stored "
            f"weights cannot be tied to the validator's seat"
        )
    return value


def default_scores_dir():
    override = os.environ.get("NOAH_RUNTIME_ROOT", "").strip()
    if override:
        return Path(override) / "ticket-16" / "scores"
    return Path(__file__).resolve().parents[3].parent / "runtime" / "ticket-16" / "scores"


def main(argv=None):
    class QuietParser(argparse.ArgumentParser):
        # argparse echoes a mistyped value; an operator may have pasted a keyed URL there.
        def error(self, message):
            self.print_usage(sys.stderr)
            self.exit(2, f"{self.prog}: error: invalid or unrecognized arguments (not echoed)\n")

    parser = QuietParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--scores-dir", type=Path, default=None)
    parser.add_argument("--endpoint", default=os.environ.get("NOAH_CHAIN_ENDPOINT", "ws://127.0.0.1:20944"))
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    scores = args.scores_dir or default_scores_dir()
    record_path = scores / f"day-{args.day}.submitted.json"
    try:
        record = json.loads(record_path.read_text())
        if record.get("schema") != "noah-submitted-weights/1" or record.get("uid") is None:
            raise ValueError("not a submitted-weights record with a uid")
        stored, block, head, update = read_finalized(args.endpoint, record["netuid"], record["uid"])
        uid_hotkey = read_uid_hotkey(args.endpoint, record["netuid"], record["uid"])
    except Exception as error:  # noqa: BLE001 - reported, exit 2, nothing written
        # An OS error's text repeats the typed path; anything else is scrubbed of the endpoint.
        if isinstance(error, OSError):
            detail = "(details withheld)"
        else:
            detail = scrub_url(error, args.endpoint)
        print(
            f"CANNOT CONFIRM day {args.day}: {type(error).__name__}: {detail}",
            file=sys.stderr,
        )
        return 2
    confirmed = build_confirmed(
        record_path,
        record,
        stored,
        finalized_block=block,
        finalized_hash=head,
        last_update=update,
        uid_hotkey=uid_hotkey,
    )
    out = scores / f"day-{args.day}.confirmed.json"
    _atomic_write(out, json.dumps(confirmed, indent=2, sort_keys=True) + "\n")
    print(f"day {args.day}: {confirmed['result']} at finalized block {block}; wrote {out}")
    return 0 if confirmed["result"] == "MATCH" else 1


if __name__ == "__main__":
    sys.exit(main())
