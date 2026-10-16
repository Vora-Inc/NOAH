"""Read-only verification of ONE day's publication through the proxy route (#16).

Reads, never signs: no wallet, no key, no send. It takes the validator's own record of
what it sent (``scores/day-N.submitted.json``) and checks, on the chain, in order:

  a  proxy execution   the extrinsic at the recorded block/index was signed by the proxy
                       signer, wraps ``Proxy.proxy(real = the validator)``, and its
                       ``Proxy.ProxyExecuted`` result is Ok (the INNER result, not the
                       outer extrinsic's success).
  b  sealed commit     ``TimelockedWeightsCommitted`` from the validator on the netuid in
                       the same extrinsic, its reveal round equal to the recorded one, and
                       ``LastUpdate`` for the validator's uid advanced AT the commit block
                       (read at block-1 and at the block).
  c  validator side    ``publish-intent.json`` absent, a commit-ledger entry with the same
                       block, ``publish-backoff.json`` absent or zero failures, and the
                       submitted vector equal to the calculated publishable vector when
                       ``day-N.json`` carries one.
  d  reveal and effect the chain reveals at the START of the reveal epoch: the events
                       ``TimelockedWeightsRevealed`` then ``WeightsSet`` for the validator,
                       and the stored weights at that block equal
                       ``round(submitted * 65535 / max(submitted))``. The reveal SKIPS
                       SILENTLY on failure, so a missing reveal event after the reveal epoch
                       started is a FAIL, never "not yet". NOT YET only while the reveal
                       epoch has not started.

Output per check: PASS, FAIL, NOT YET or UNABLE, with the evidence (block numbers, event
names). Exit 0 only when every check passes; 1 on any FAIL; 2 when a read failed or a
name the chain's metadata does not carry is needed (UNABLE; both the constant and the
metadata's nearby names are printed); 3 when nothing failed but a check is NOT YET.

NOT CONFIRMED, stated plainly:
  * UNVERIFIED AGAINST A CHAIN until run once: the tests use a fake view; the real
    ``SubstrateView`` below has never read a chain.
  * Event and storage names are constants per issue #111's source reading ("per #111,
    unconfirmed until run on a chain"). They are also resolved against the runtime
    metadata at run time; any disagreement prints both and exits 2.
  * The epoch arithmetic that finds the reveal block
    (``epoch = (block + netuid + 1) // (tempo + 1)``, reveal at the start of epoch
    ``epoch + RevealPeriodEpochs``) is from the subtensor source, not measured. Pass
    ``--reveal-block`` to read a known block instead.
  * The commit's hash is printed but NOT compared: the chain's hash is not the sha256 the
    publish-intent marker records, and no record keeps the sealed bytes after success.
  * A pass proves NORMAL sending through the proxy, not interrupted-send recovery.

    python scripts/verify_proxy_publication.py --day 17 --validator <ss58> --signer <ss58> \
        --scores-dir <dir> --state-dir <dir> --endpoint ws://127.0.0.1:9945
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

PASS, FAIL, NOT_YET, UNABLE = "PASS", "FAIL", "NOT YET", "UNABLE"
EXIT_OK, EXIT_FAIL, EXIT_UNABLE, EXIT_NOT_YET = 0, 1, 2, 3

# ---- Names, per #111, UNCONFIRMED until run on a chain (also checked against metadata) ----
PALLET = "SubtensorModule"
EVENT_COMMITTED = (PALLET, "TimelockedWeightsCommitted")
EVENT_REVEALED = (PALLET, "TimelockedWeightsRevealed")
EVENT_WEIGHTS_SET = (PALLET, "WeightsSet")
EVENT_PROXY_EXECUTED = ("Proxy", "ProxyExecuted")
CALL_PROXY = ("Proxy", "proxy")
STORAGE_WEIGHTS = (PALLET, "Weights")
STORAGE_LAST_UPDATE = (PALLET, "LastUpdate")
STORAGE_TEMPO = (PALLET, "Tempo")
STORAGE_REVEAL_EPOCHS = (PALLET, "RevealPeriodEpochs")

REQUIRED_EVENTS = (EVENT_COMMITTED, EVENT_REVEALED, EVENT_WEIGHTS_SET, EVENT_PROXY_EXECUTED)
REQUIRED_STORAGE = (STORAGE_WEIGHTS, STORAGE_LAST_UPDATE, STORAGE_TEMPO, STORAGE_REVEAL_EPOCHS)

GLOBAL_MAX_SUBNET_COUNT = 4096  # storage index = netuid + mecid * 4096 (reader.py, emissions.py)
# The reveal block is computed from source arithmetic (unconfirmed): look a little before it
# and a good way after it, so a small error in the formula cannot turn a reveal into a FAIL.
REVEAL_SCAN_BEFORE = 5
REVEAL_SCAN_AFTER = 60


def _load_confirm():
    path = Path(__file__).resolve().parent / "confirm_published.py"
    spec = importlib.util.spec_from_file_location("confirm_published", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# small decoders, tolerant of the shapes the chain libraries hand back
# ---------------------------------------------------------------------------


def _plain(value):
    return getattr(value, "value", value)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _same_addr(a, b):
    from noah.chain.sources import Refusal
    from noah.chain.ss58 import public_key_of

    try:
        return public_key_of(a) == public_key_of(b)
    except (Exception, Refusal):  # noqa: BLE001 - not an address on either side: compare as text
        return a == b


def _event_fields(record):
    record = _plain(record)
    if not isinstance(record, dict):
        return None
    event = _plain(record.get("event", record))
    if not isinstance(event, dict):
        return None
    module, name = event.get("module_id"), event.get("event_id")
    if not (isinstance(module, str) and isinstance(name, str)):
        return None
    return module, name, _plain(event.get("attributes"))


def _extrinsic_idx(record):
    record = _plain(record)
    if not isinstance(record, dict):
        return None
    index = record.get("extrinsic_idx")
    if _is_int(index):
        return index
    phase = _plain(record.get("phase"))
    if isinstance(phase, dict) and _is_int(phase.get("ApplyExtrinsic")):
        return phase["ApplyExtrinsic"]
    return None


def _attrs(attributes):
    attributes = _plain(attributes)
    if isinstance(attributes, (list, tuple)):
        return [_plain(a) for a in attributes]
    if isinstance(attributes, dict):
        return [_plain(a) for a in attributes.values()]
    return [] if attributes is None else [attributes]


def _events_named(records, pair):
    for record in records:
        fields = _event_fields(record)
        if fields and (fields[0], fields[1]) == pair:
            yield record, _attrs(fields[2])


def _call_args(call):
    args = _plain(call.get("call_args")) if isinstance(call, dict) else None
    if isinstance(args, dict):
        return {k: _plain(v) for k, v in args.items()}
    out = {}
    for item in args or []:
        item = _plain(item)
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = _plain(item.get("value"))
    return out


def _addr_of(raw):
    raw = _plain(raw)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("Id", "id", "value", "ss58_address"):
            if key in raw:
                return _addr_of(raw[key])
    return None


def storage_index(netuid, mecid):
    return int(netuid) + int(mecid) * GLOBAL_MAX_SUBNET_COUNT


def reveal_start_block(commit_block, netuid, tempo, reveal_epochs):
    """First block of the reveal epoch (source arithmetic, UNCONFIRMED; see module docstring)."""
    step = int(tempo) + 1
    epoch = (int(commit_block) + int(netuid) + 1) // step
    return (epoch + int(reveal_epochs)) * step - (int(netuid) + 1)


# ---------------------------------------------------------------------------
# the chain, as these checks see it
# ---------------------------------------------------------------------------


class SubstrateView:
    """Read-only chain access. NEVER RUN AGAINST A CHAIN: written from the libraries'
    documented shapes (``noah/chain/live.py`` reads blocks the same way). No signing, no
    wallet. Any method may raise; every check turns that into UNABLE."""

    def __init__(self, endpoint):
        from async_substrate_interface.sync_substrate import SubstrateInterface

        from noah.chain.sources import connect_scrubbed

        self.endpoint = endpoint
        self._s = connect_scrubbed(lambda: SubstrateInterface(endpoint), endpoint, RuntimeError)

    def head(self):
        return int(self._s.get_block_number(self._s.get_chain_finalised_head()))

    def events(self, block):
        return list(_plain(self._s.get_events(block_hash=self._s.get_block_hash(block))))

    def extrinsics(self, block):
        data = self._s.get_block(block_hash=self._s.get_block_hash(block))
        return [_plain(e) for e in data["extrinsics"]]

    def storage(self, module, name, params, block=None):
        block_hash = None if block is None else self._s.get_block_hash(block)
        return _plain(self._s.query(module, name, list(params), block_hash=block_hash))

    def metadata_names(self):
        """``{"events": {pallet: [names]}, "storage": {pallet: [names]}}`` from the runtime."""
        names = {"events": {}, "storage": {}}
        for pallet in self._s.get_metadata().pallets:
            events = getattr(pallet, "events", None) or []
            storage = getattr(getattr(pallet, "storage", None), "items", None) or []
            names["events"][pallet.name] = [e.name for e in events]
            names["storage"][pallet.name] = [i.name for i in storage]
        return names


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _result(check, status, evidence):
    return {"check": check, "status": status, "evidence": evidence}


def metadata_problems(view):
    """Constants the runtime metadata does not carry: ``[(kind, pallet, name, nearby)]``."""
    meta = view.metadata_names()
    problems = []
    for kind, wanted in (("events", REQUIRED_EVENTS), ("storage", REQUIRED_STORAGE)):
        for pallet, name in wanted:
            have = meta.get(kind, {}).get(pallet, [])
            if name not in have:
                key = name[:5].lower()
                problems.append((kind, pallet, name, [n for n in have if key in n.lower()][:12]))
    return problems


def _record_fields(record):
    for key in ("block", "extrinsic_index", "netuid", "uid", "signer"):
        if record.get(key) is None:
            raise ValueError(f"the submitted record has no {key!r}; not a proxy-route record")
    return (
        record["block"],
        record["extrinsic_index"],
        record["netuid"],
        record["uid"],
        record["signer"],
        record.get("commit_identity") or record.get("hotkey"),
    )


def check_a(view, record):
    block, idx, _netuid, _uid, signer, validator = _record_fields(record)
    extrinsics = view.extrinsics(block)
    if not 0 <= idx < len(extrinsics):
        return _result("a proxy execution", FAIL, f"block {block} has no extrinsic {idx}")
    extrinsic = extrinsics[idx]
    address = _addr_of(extrinsic.get("address"))
    if not address or not _same_addr(address, signer):
        return _result(
            "a proxy execution",
            FAIL,
            f"block {block} extrinsic {idx} is signed by {address}, not the proxy signer {signer}",
        )
    call = _plain(extrinsic.get("call"))
    if (
        not isinstance(call, dict)
        or (call.get("call_module"), call.get("call_function")) != CALL_PROXY
    ):
        return _result(
            "a proxy execution",
            FAIL,
            f"block {block} extrinsic {idx} is not {CALL_PROXY[0]}.{CALL_PROXY[1]}",
        )
    real = _addr_of(_call_args(call).get("real"))
    if not real or not _same_addr(real, validator):
        return _result(
            "a proxy execution",
            FAIL,
            f"the proxy call's real account is {real}, not the validator {validator}",
        )
    from noah.core.proxy_route import INNER_OK, classify_inner

    own = [r for r in view.events(block) if _extrinsic_idx(r) == idx]
    outcome, detail = classify_inner(own)
    if outcome != INNER_OK:
        shown = (
            f" ({detail})"
            if detail is not None
            else " (no Proxy.ProxyExecuted in that extrinsic's events)"
        )
        return _result(
            "a proxy execution",
            FAIL,
            f"inner result at block {block} extrinsic {idx} is {outcome}{shown}",
        )
    return _result(
        "a proxy execution",
        PASS,
        f"block {block} extrinsic {idx}: signed by the proxy signer, Proxy.proxy(real=validator), ProxyExecuted Ok",
    )


def check_b(view, record, mecid):
    from noah.chain.emissions import netuid_and_mechanism

    block, idx, netuid, uid, _signer, validator = _record_fields(record)
    own = [r for r in view.events(block) if _extrinsic_idx(r) == idx]
    found = None
    for _record, attrs in _events_named(own, EVENT_COMMITTED):
        if len(attrs) >= 2 and _addr_of(attrs[0]) and _same_addr(_addr_of(attrs[0]), validator):
            if _is_int(attrs[1]) and netuid_and_mechanism(attrs[1])[0] == int(netuid):
                found = attrs
                break
    if found is None:
        return _result(
            "b sealed commit",
            FAIL,
            f"no {EVENT_COMMITTED[1]} from the validator on netuid {netuid} in block {block} extrinsic {idx}",
        )
    recorded_round = record.get("reveal_round")
    event_round = found[3] if len(found) > 3 else None
    if recorded_round is not None and event_round != recorded_round:
        return _result(
            "b sealed commit",
            FAIL,
            f"reveal round on chain {event_round} differs from the recorded {recorded_round}",
        )
    index = storage_index(netuid, mecid)
    before = view.storage(*STORAGE_LAST_UPDATE, [index], block=block - 1)
    after = view.storage(*STORAGE_LAST_UPDATE, [index], block=block)
    try:
        before_u, after_u = int(before[uid]), int(after[uid])
    except (TypeError, IndexError, KeyError, ValueError):
        return _result(
            "b sealed commit", UNABLE, f"LastUpdate[{index}] has no readable entry for uid {uid}"
        )
    if after_u < block or after_u <= before_u:
        return _result(
            "b sealed commit",
            FAIL,
            f"LastUpdate for uid {uid} did not advance at the commit: {before_u} at block {block - 1}, {after_u} at block {block}",
        )
    return _result(
        "b sealed commit",
        PASS,
        f"{EVENT_COMMITTED[1]} from the validator, round {event_round}, chain hash {found[2] if len(found) > 2 else '?'} "
        f"(not comparable to the marker's sha256); LastUpdate uid {uid}: {before_u} -> {after_u}",
    )


def check_c(record, scores_dir, state_dir, day):
    from noah.core.commit_ledger import CommitLedger, CommitLedgerRefused

    problems = []
    notes = []
    if (state_dir / "publish-intent.json").exists():
        problems.append("publish-intent.json is still present (an unconfirmed send)")
    try:
        entries = CommitLedger(state_dir / "commit-ledger.json").load()
    except CommitLedgerRefused as error:
        return _result("c validator side", UNABLE, f"commit ledger unreadable: {error}")
    entry = None if entries is None else _ledger_entry(entries, record)
    if entry is None:
        problems.append("no commit-ledger entry for this commit block")
    else:
        notes.append(f"ledger day {entry[0]} block {entry[1]['block']}")
    backoff = state_dir / "publish-backoff.json"
    if backoff.exists():
        try:
            failures = int(json.loads(backoff.read_text()).get("failures"))
        except Exception as error:  # noqa: BLE001
            return _result(
                "c validator side",
                UNABLE,
                f"publish-backoff.json unreadable ({type(error).__name__})",
            )
        if failures:
            problems.append(f"publish-backoff.json records {failures} failed send(s)")
    calculated = scores_dir / f"day-{day}.json"
    if calculated.exists():
        try:
            publishable = json.loads(calculated.read_text()).get("publishable_weights_u16")
        except Exception as error:  # noqa: BLE001
            return _result(
                "c validator side", UNABLE, f"day-{day}.json unreadable ({type(error).__name__})"
            )
        if publishable is not None:
            submitted = {int(u): int(w) for u, w in record["submitted_weights_u16"].items()}
            if {int(u): int(w) for u, w in publishable.items()} != submitted:
                problems.append(
                    f"the submitted vector differs from day-{day}.json publishable_weights_u16"
                )
            else:
                notes.append("submitted equals the calculated publishable vector")
    if problems:
        return _result("c validator side", FAIL, "; ".join(problems))
    return _result("c validator side", PASS, "; ".join(notes) or "marker absent, backoff clear")


def _ledger_entry(entries, record):
    # Matched on the commit block, the one fact both records share; the ledger's day is shown.
    for day, entry in sorted(entries.items()):
        if entry["block"] == record["block"]:
            return day, entry
    return None


def check_d(view, record, mecid, reveal_block):
    from noah.chain.emissions import netuid_and_mechanism

    confirm = _load_confirm()
    block, _idx, netuid, uid, _signer, validator = _record_fields(record)
    index = storage_index(netuid, mecid)
    head = view.head()
    if reveal_block is None:
        tempo = view.storage(*STORAGE_TEMPO, [netuid], block=block)
        epochs = view.storage(*STORAGE_REVEAL_EPOCHS, [netuid], block=block)
        if not _is_int(tempo) or not _is_int(epochs):
            return _result(
                "d reveal and effect",
                UNABLE,
                f"Tempo/RevealPeriodEpochs for netuid {netuid} did not read as whole numbers ({tempo!r}, {epochs!r})",
            )
        start = reveal_start_block(block, netuid, tempo, epochs)
        if head < start:
            return _result(
                "d reveal and effect",
                NOT_YET,
                f"the reveal epoch has not started: finalized head {head} is before its first block {start} (tempo {tempo}, reveal period {epochs} epoch(s); arithmetic from source, unconfirmed)",
            )
        first, last = (
            max(start - REVEAL_SCAN_BEFORE, block + 1),
            min(head, start + REVEAL_SCAN_AFTER),
        )
    else:
        if head < reveal_block:
            return _result(
                "d reveal and effect",
                NOT_YET,
                f"finalized head {head} is before the given reveal block {reveal_block}",
            )
        first = last = int(reveal_block)
        start = int(reveal_block)
    revealed_at = None
    for candidate in range(first, last + 1):
        records = view.events(candidate)
        for _rec, attrs in _events_named(records, EVENT_REVEALED):
            who = next((_addr_of(a) for a in attrs if _addr_of(a)), None)
            nets = [a for a in attrs if _is_int(a)]
            if (
                who
                and _same_addr(who, validator)
                and nets
                and netuid_and_mechanism(nets[0])[0] == int(netuid)
            ):
                revealed_at = (candidate, records)
                break
        if revealed_at:
            break
    if revealed_at is None:
        return _result(
            "d reveal and effect",
            FAIL,
            f"no {EVENT_REVEALED[1]} for the validator in blocks {first} to {last} although the reveal epoch started at {start}; "
            f"the reveal skips silently on failure, so this means NOT published (if the epoch arithmetic is wrong, pass --reveal-block)",
        )
    reveal_block_found, records = revealed_at
    sets = [
        a
        for _r, a in _events_named(records, EVENT_WEIGHTS_SET)
        if len(a) >= 2
        and _is_int(a[0])
        and netuid_and_mechanism(a[0])[0] == int(netuid)
        and a[1] == uid
    ]
    if not sets:
        return _result(
            "d reveal and effect",
            FAIL,
            f"{EVENT_REVEALED[1]} at block {reveal_block_found} but no {EVENT_WEIGHTS_SET[1]}(netuid {netuid}, uid {uid}) in that block",
        )
    entries = view.storage(*STORAGE_WEIGHTS, [index, uid], block=reveal_block_found)
    try:
        stored = {int(u): int(w) for u, w in (entries or [])}
    except (TypeError, ValueError):
        return _result(
            "d reveal and effect",
            UNABLE,
            f"Weights[{index}, {uid}] did not read as (uid, weight) pairs: {entries!r}",
        )
    submitted = {int(u): int(w) for u, w in record["submitted_weights_u16"].items()}
    verdict = confirm.compare(submitted, stored)
    if verdict["result"] != "MATCH":
        return _result(
            "d reveal and effect",
            FAIL,
            f"WeightsSet at block {reveal_block_found} but the stored weights differ from round(submitted x 65535 / max): mismatches {verdict['mismatches']}, extras {verdict['nonzero_extras']}",
        )
    return _result(
        "d reveal and effect",
        PASS,
        f"{EVENT_REVEALED[1]} then {EVENT_WEIGHTS_SET[1]} at block {reveal_block_found}; stored weights for uid {uid} equal round(submitted x 65535 / max) ({len(stored)} entries)",
    )


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run_checks(view, record, *, scores_dir, state_dir, day, mecid=0, reveal_block=None):
    """Returns ``(results, exit_code)``. A metadata disagreement short-circuits to exit 2."""
    problems = metadata_problems(view)
    if problems:
        results = [
            _result(
                "names vs runtime metadata",
                UNABLE,
                f"{kind} {pallet}.{name} (constant, per #111, unconfirmed) is NOT in the runtime metadata; metadata has nearby: {nearby or 'nothing similar'}",
            )
            for kind, pallet, name, nearby in problems
        ]
        return results, EXIT_UNABLE
    from noah.chain.sources import Refusal

    results = []
    for label, check in (
        ("a proxy execution", lambda: check_a(view, record)),
        ("b sealed commit", lambda: check_b(view, record, mecid)),
        ("c validator side", lambda: check_c(record, scores_dir, state_dir, day)),
        ("d reveal and effect", lambda: check_d(view, record, mecid, reveal_block)),
    ):
        try:
            results.append(check())
        except (Exception, Refusal) as error:  # noqa: BLE001 - reported per check, never a crash
            from noah.chain.sources import scrub_endpoint_text

            text = scrub_endpoint_text(error, getattr(view, "endpoint", None))
            results.append(_result(label, UNABLE, f"{type(error).__name__}: {text}"))
    statuses = {r["status"] for r in results}
    if FAIL in statuses:
        return results, EXIT_FAIL
    if UNABLE in statuses:
        return results, EXIT_UNABLE
    if NOT_YET in statuses:
        return results, EXIT_NOT_YET
    return results, EXIT_OK


def main(argv=None, view_factory=None):
    from noah.chain.sources import guard_endpoint_logging

    guard_endpoint_logging()
    from noah.chain.sources import QuietArgumentParser

    parser = QuietArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument(
        "--validator", required=True, help="the validator's public address (commit identity)"
    )
    parser.add_argument("--signer", required=True, help="the proxy signer's public address")
    parser.add_argument("--scores-dir", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--endpoint", default=os.environ.get("NOAH_CHAIN_ENDPOINT", "ws://127.0.0.1:9945")
    )
    parser.add_argument("--mecid", type=int, default=0)
    parser.add_argument(
        "--reveal-block",
        type=int,
        default=None,
        help="read this block as the reveal block instead of computing it",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    from noah.chain.sources import Refusal

    confirm = _load_confirm()
    scores = args.scores_dir or confirm.default_scores_dir()
    print("verify_proxy_publication: read-only; UNVERIFIED AGAINST A CHAIN until run once.")
    try:
        record = json.loads((scores / f"day-{args.day}.submitted.json").read_text())
        if record.get("schema") != "noah-submitted-weights/1":
            raise ValueError("not a submitted-weights record")
        if not _same_addr(record.get("signer") or "", args.signer):
            raise ValueError(
                "the record's signer is not the --signer given (or the record has none: not a proxy-route record)"
            )
        if not _same_addr(
            record.get("commit_identity") or record.get("hotkey") or "", args.validator
        ):
            raise ValueError("the record's commit identity is not the --validator given")
        view = (view_factory or SubstrateView)(args.endpoint)
    except (Exception, Refusal) as error:  # noqa: BLE001
        from noah.chain.sources import scrub_endpoint_text

        print(
            f"CANNOT VERIFY day {args.day}: {type(error).__name__}: "
            f"{scrub_endpoint_text(error, args.endpoint)}",
            file=sys.stderr,
        )
        return EXIT_UNABLE
    results, code = run_checks(
        view,
        record,
        scores_dir=scores,
        state_dir=args.state_dir,
        day=args.day,
        mecid=args.mecid,
        reveal_block=args.reveal_block,
    )
    for item in results:
        print(f"[{item['status']}] {item['check']}: {item['evidence']}")
    print(
        {
            EXIT_OK: "ALL CHECKS PASS",
            EXIT_FAIL: "AT LEAST ONE CHECK FAILED",
            EXIT_UNABLE: "UNABLE: a read or a name failed",
            EXIT_NOT_YET: "NOT YET: nothing failed, the reveal is still ahead",
        }[code]
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
