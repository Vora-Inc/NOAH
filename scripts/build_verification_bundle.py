#!/usr/bin/env python3
"""Build one per-day verification bundle under ``records/<network>/YYYY-MM-DD/`` (#386).

    python3 scripts/build_verification_bundle.py \\
        --network NAME --eval-day N --records-root DIR \\
        [--fixture day-N.fixture.json]   # CAPTURED input (exact); else RECONSTRUCTED from --store
        [--store history.db]             # opened only as a private COPY, never written
        --dials dials.json               # reconstruction only: the dials the day was scored under
        [--score day-N.json]             # the canonical score file; else computed from the fixture
        [--settings day-N.settings.json] # the day's recorded settings; else generated here
        [--profile profile.json] [--genesis-hash H] [--netuid N] [--ark-address A]
        [--validator-hotkey A] [--pinned-height N] [--publication-readback file.json]
        [--code-commit SHA] [--force]

Two ways to get the day's scoring input, and the bundle says which it used:

  captured       ``--fixture``: the exact object the day-close scored, written by
                 ``day_close_pipeline.py`` as ``day-N.fixture.json`` (#386). Exact.
  reconstructed  ``--store``: rebuilt from the history store with
                 ``noah.chain.fixture_build`` the way the replay harness does it.
                 The store does not hold the seat roster or each uid's
                 registration block, so those are re-derived (see ``LIMITS``).
                 The bundle then proves "this score follows from THIS input", and
                 the input is a reconstruction until a captured one exists.

The bundle is assembled in a scratch directory, scanned for secrets, verified
exactly as a stranger would verify it, and only then moved into place.
A secret hit or a failed verification places nothing and exits non-zero.
Exit codes: 0 built, 1 refused (verification, score mismatch, existing bundle
differs), 2 usage/input problem, 3 secret scan refusal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SOFTWARE_NOAH = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(_SOFTWARE_NOAH))

from noah import secret_scan  # noqa: E402
from noah.pathtext import plain_path_text  # noqa: E402
import verify_bundle  # noqa: E402

BUNDLE_SCHEMA = verify_bundle.BUNDLE_SCHEMA
INDEX_SCHEMA = "noah-records-index/1"
CHAIN_REFS_SCHEMA = "noah-chain-refs/1"
NOT_RECORDED = "not recorded"
ENTRY_DEPOSIT_RAO = 10_000_000_000  # the replay harness's constant for the entry deposit
IDENTITY_KEYS = ("netuid", "ark_address", "genesis_hash")  # added by persist_score_evidence, not the engine

LIMITS = (
    "RECONSTRUCTED input. The history store holds per-day pay, deposits, holdings and "
    "the day boundaries, but not the seat roster (coldkey/hotkey/uid) or each uid's "
    "registration block. Seats are taken as, per uid, the latest recorded pay row on or "
    "before the day; registration days assume uids were registered in block order. "
    "A day whose captured fixture exists supersedes this."
)

EXIT_BUILT, EXIT_REFUSED, EXIT_USAGE, EXIT_SECRET = 0, 1, 2, 3


class BundleRefusal(Exception):
    def __init__(self, message, code=EXIT_REFUSED):
        super().__init__(message)
        self.code = code


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2) + "\n"


# --------------------------------------------------------------------------
# Reconstruction (ported from the replay harness; same calls, same constants)
# --------------------------------------------------------------------------


def reconstruct_fixture(db_copy: Path, eval_day: int, dials: dict):
    """Rebuild the day's ``noah-fixture/6`` from a COPY of the history store.

    Returns ``(fixture, info)`` where ``info`` has the day anchor and the
    store's scan range for the day. Raises BundleRefusal with the engine's own
    message when the store cannot supply the day.
    """
    from noah.chain.fixture_build import build_fixture
    from noah.chain.sources import Refusal
    from noah.chain.store import Store
    from noah.core.chain_adapter import _entry_deposits_for, _ExcludingEntryDeposit
    from noah.deposits.entry_gate import exclude_unconfirmed_entries

    anchor = Store.peek_day_anchor_ts(str(db_copy))
    store = Store.open(str(db_copy), anchor, read_only=True)
    raw = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
    try:
        # This offline reconstruction has no historical price reader. A captured
        # fixture carries the exact already-valued input used by the validator.
        # Refuse instead of turning any earlier foreign receipt into silent zero.
        if raw.execute("SELECT 1 FROM foreign_deposit WHERE day <= ? LIMIT 1", (eval_day,)).fetchone():
            raise BundleRefusal(
                "foreign receipts require the captured --fixture from day close; "
                "offline store reconstruction has no exact-block price evidence",
                EXIT_REFUSED,
            )
        blocks = [r[0] for r in raw.execute("select block from registration_day order by block")]
        block_day = dict(raw.execute("select block, day from registration_day"))
        uid_reg_block = {i + 1: b for i, b in enumerate(blocks)}

        rows = raw.execute(
            "select uid, coldkey, hotkey from attributed_pay where day <= ? order by uid, block",
            (eval_day,),
        ).fetchall()
        latest = {uid: (ck, hk) for uid, ck, hk in rows}
        for uid, b in uid_reg_block.items():
            if uid not in latest and block_day[b] <= eval_day:
                r = raw.execute(
                    "select coldkey, hotkey from attributed_pay where uid=? order by block limit 1", (uid,)
                ).fetchone()
                if r:
                    latest[uid] = r
        seats = {}
        for uid, (ck, hk) in sorted(latest.items()):
            seats.setdefault(ck, {})[hk] = uid

        try:
            seats, dropped = exclude_unconfirmed_entries(store, seats, as_of_day=eval_day)
            regs = {}
            for ck, hks in seats.items():
                per = {}
                for hk, uid in hks.items():
                    blk = uid_reg_block.get(uid)
                    if blk is not None:
                        per[hk] = block_day[blk]
                if per:
                    regs[ck] = per
            ed, edays = _entry_deposits_for(store, regs, ENTRY_DEPOSIT_RAO, as_of_day=eval_day)
            fstore = _ExcludingEntryDeposit(store, ed, edays) if ed else store
            fregs = {ck: dict(p) for ck, p in regs.items()}
            for ck, p in edays.items():
                fregs.setdefault(ck, {}).update(p)
            fixture = build_fixture(
                fstore, name=f"height-day{eval_day}", eval_day=eval_day, day_epoch_ts=anchor,
                dials=dials, seats=seats, foreign_rule=None, entry_deposits=ed,
                registration_days=fregs,
            )
        except (Exception, Refusal) as why:
            raise BundleRefusal(
                f"the store cannot supply day {eval_day}: {type(why).__name__}: {why}", EXIT_REFUSED
            ) from why
        scan = raw.execute("select from_block, to_block from scanned_day where day=?", (eval_day,)).fetchone()
        emission = raw.execute("select day, block, block_hash from emission_start").fetchone()
        first_last = {}
        if scan:
            for label, blk in (("first", scan[0]), ("last", scan[1])):
                row = raw.execute("select block_hash from block_seen where block=?", (blk,)).fetchone()
                first_last[label] = {"block": blk, "block_hash": row[0] if row else NOT_RECORDED}
        info = {"anchor": anchor, "scan": scan, "first_last": first_last, "emission_start": emission,
                "dropped_unconfirmed": len(dropped)}
        return json.loads(json.dumps(fixture)), info
    finally:
        raw.close()
        store.close()


def store_day_info(db_copy: Path, eval_day: int) -> dict:
    """Day anchor and block range from a store, for captured-fixture builds."""
    from noah.chain.store import Store

    anchor = Store.peek_day_anchor_ts(str(db_copy))
    raw = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
    try:
        scan = raw.execute("select from_block, to_block from scanned_day where day=?", (eval_day,)).fetchone()
        first_last = {}
        if scan:
            for label, blk in (("first", scan[0]), ("last", scan[1])):
                row = raw.execute("select block_hash from block_seen where block=?", (blk,)).fetchone()
                first_last[label] = {"block": blk, "block_hash": row[0] if row else NOT_RECORDED}
        emission = raw.execute("select day, block, block_hash from emission_start").fetchone()
    finally:
        raw.close()
    return {"anchor": anchor, "scan": scan, "first_last": first_last, "emission_start": emission}


# --------------------------------------------------------------------------
# Pieces of the bundle
# --------------------------------------------------------------------------


def engine_score_text(fixture: dict) -> str:
    from noah.scoring.engine import score_fixture
    from noah.scoring.fixture_io import parse_fixture, render_output

    return render_output(score_fixture(parse_fixture(fixture)))


def pure_score_text(score_file: Path, expected: str) -> tuple:
    """Check a canonical score file against the engine output.

    The canonical file may carry flat chain-identity keys added after scoring
    (``persist_score_evidence``); the engine never produces them and the audit
    compares bytes, so the bundle's ``score.json`` is the engine output and the
    identity goes to chain-refs.json. Returns ``(identity, canonical_sha256)``.
    """
    raw = score_file.read_bytes()
    obj = json.loads(raw.decode("utf-8"))
    identity = {k: obj.pop(k) for k in IDENTITY_KEYS if k in obj}
    from noah.scoring.fixture_io import render_output

    if render_output(obj) != expected:
        raise BundleRefusal(
            f"the score file {plain_path_text(score_file)} (identity keys removed) differs from "
            f"the score recomputed "
            "from the input. Likely causes: (1) different dials or profile settings than the day was "
            "scored under; (2) a rule change between the code that scored the day and the code now "
            "running (check out the commit that scored it and pass it as --code-commit); (3) the "
            "reconstructed input is not the input the day was scored on. Nothing was built."
        )
    return identity, hashlib.sha256(raw).hexdigest()


def day_window(anchor: int, eval_day: int):
    start = datetime.fromtimestamp(anchor, timezone.utc) + timedelta(days=eval_day)
    return start, start + timedelta(days=1)


def git_head(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return NOT_RECORDED


def generated_settings(eval_day, dials, profile: Path, code_commit) -> str:
    """Settings generated at BUILD time (no recorded one supplied). Host paths are
    reduced to file names; ``code_version`` is the builder's checkout, not
    necessarily what ran the day."""
    from noah.scoring.parameters import ENV_VAR
    from settings_record import build_settings_record

    rec = build_settings_record(eval_day, dials=dials, env={ENV_VAR: str(profile)},
                                code_root=_SOFTWARE_NOAH.parent.parent)
    rec.pop("recorded_at", None)  # keeps a rebuild of the same day byte-identical
    rec["profile_path"] = profile.name
    if isinstance(rec.get("deployed_commit"), dict):
        rec["deployed_commit"]["path"] = Path(rec["deployed_commit"]["path"]).name
    if code_commit:
        rec["code_version"] = code_commit
    rec["generated_by"] = "build_verification_bundle (settings regenerated at build time, not recorded at day close)"
    return canonical(rec)


def readme_text(network, eval_day, utc_date, mode, has_published=False, code_commit=NOT_RECORDED) -> str:
    how = (
        "The scoring input is the exact object the day close scored (captured)."
        if mode == "captured"
        else "The scoring input was RECONSTRUCTED from the history store (see MANIFEST.json,\n"
        "fixture_provenance.limits). It is not a capture of what the day close held."
    )
    caveat = (
        "A reconstructed bundle never carries the validator's published score file when a\n"
        "rule changed after the day was scored; only captured days (input recorded at day\n"
        "close) can.\n"
        if mode != "captured"
        else ""
    )
    pub = (
        "score-published.json is the canonical score file exactly as the validator wrote it\n"
        "(it adds netuid, ark_address and genesis_hash). With those three keys removed it must\n"
        "equal score.json byte for byte; verify_bundle.py checks this.\n"
        if has_published
        else "No canonical published score file was recorded for this day (MANIFEST.json says\n"
        "\"not recorded\"); score.json is the recompute only.\n"
    )
    return f"""check out commit {code_commit} first (git checkout {code_commit}); scoring rules can change between versions.

Noah daily verification bundle
network {network}, evaluation day {eval_day}, UTC date {utc_date}

What this is: everything needed to recompute one day's score without trusting
the publisher. fixture.json is the scoring input (noah-fixture/6), score.json is
the output (noah-score-output/6), settings.json records the profile, dials and
code version, chain-refs.json says where on the chain the input came from, and
MANIFEST.json and SHA256SUMS fix every file's hash.

{how}

{pub}{caveat}
Scoring rules can change between code versions, so first check out the exact
code this bundle was built with: commit {code_commit}
(git checkout {code_commit}). A different version may legitimately score the
same input differently.

Verify, from a clean checkout of the public repository at that commit (Python 3,
no packages needed beyond the checkout):

  1. Files intact:        sha256sum -c SHA256SUMS
  2. Score follows input: cd services/noah && python3 -m noah.scoring.audit_published \\
                            <bundle>/fixture.json <bundle>/score.json
                          It prints AGREES and exits 0.
  3. All checks at once:  cd services/noah && python3 scripts/verify_bundle.py <bundle>
                          It prints PASS or FAIL.

Optional recompute: run the scorer on fixture.json yourself
(python3 -m noah.scoring.run_fixture) and compare with score.json byte for byte.

What a pass means: the score follows from the input, and these are the files
the manifest names. It does not prove the input is true to the chain. Compare
chain-refs.json with your own read of the chain for that. Values the build could
not take from a record say "not recorded"; none were filled in by guesswork.
"""


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_bundle(args) -> Path:
    eval_day = int(args.eval_day)
    from noah.scoring.parameters import ENV_VAR

    if args.profile and not os.environ.get(ENV_VAR):
        os.environ[ENV_VAR] = str(args.profile)  # the engine loads its profile from here
    scratch = Path(tempfile.mkdtemp(prefix="bundle-build-"))
    try:
        info = {"anchor": None, "scan": None, "first_last": {}, "emission_start": None}
        store_sha = None
        work_db = None
        if args.store:
            store_path = Path(args.store)
            if not store_path.is_file():
                raise BundleRefusal(f"--store {plain_path_text(store_path)} does not exist", EXIT_USAGE)
            store_sha = hashlib.sha256(store_path.read_bytes()).hexdigest()
            work_db = scratch / "store-copy.db"
            shutil.copyfile(store_path, work_db)  # the original is never opened

        dials = json.loads(Path(args.dials).read_text(encoding="utf-8")) if args.dials else None

        if args.fixture:
            mode = "captured"
            fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
            if work_db:
                info = store_day_info(work_db, eval_day)
            provenance = {"method": "captured", "source": "the fixture the day close scored (day-N.fixture.json)"}
        else:
            mode = "reconstructed"
            if not work_db or dials is None:
                raise BundleRefusal("reconstruction needs --store and --dials (or pass --fixture)", EXIT_USAGE)
            fixture, info = reconstruct_fixture(work_db, eval_day, dials)
            provenance = {"method": "reconstructed", "source_store_sha256": store_sha, "limits": LIMITS}
        if str(fixture.get("evalDay")) != str(eval_day):
            raise BundleRefusal(f"the fixture is for day {fixture.get('evalDay')!r}, not {eval_day}", EXIT_USAGE)
        if info["anchor"] is None:
            info["anchor"] = int(fixture["dayEpochTs"])
        if dials is None:
            dials = fixture.get("dials")

        score_text = engine_score_text(fixture)
        identity, canonical_sha, published_raw = {}, None, None
        if args.score:
            published_raw = Path(args.score).read_bytes()
            identity, canonical_sha = pure_score_text(Path(args.score), score_text)
            score_source = "canonical score file (chain-identity keys moved to chain-refs.json)"
        else:
            score_source = "computed from the fixture by the repository's scoring engine"

        start, end = day_window(info["anchor"], eval_day)
        utc_date = args.date or start.strftime("%Y-%m-%d")
        code_commit = args.code_commit or git_head(_SOFTWARE_NOAH.parent.parent)

        if args.settings:
            settings_text = Path(args.settings).read_text(encoding="utf-8")
            settings_source = "recorded at day close"
        else:
            if not args.profile:
                raise BundleRefusal("no --settings given: pass --profile so settings can be generated", EXIT_USAGE)
            settings_text = generated_settings(eval_day, dials, Path(args.profile), args.code_commit)
            settings_source = "generated at build time"

        readback = NOT_RECORDED
        if args.publication_readback:
            readback = json.loads(Path(args.publication_readback).read_text(encoding="utf-8"))

        def pick(flag, key):
            return flag if flag not in (None, "") else identity.get(key, NOT_RECORDED)

        chain_refs = {
            "schema": CHAIN_REFS_SCHEMA,
            "network": args.network,
            "eval_day": str(eval_day),
            "utc_date": utc_date,
            "day_epoch_ts": str(info["anchor"]),
            "day_window_utc": {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "genesis_hash": pick(args.genesis_hash, "genesis_hash"),
            "netuid": pick(args.netuid, "netuid"),
            "ark_address": pick(args.ark_address, "ark_address"),
            "validator_hotkey": args.validator_hotkey or NOT_RECORDED,
            "pinned_height": args.pinned_height if args.pinned_height is not None else NOT_RECORDED,
            "first_block": _block(info["first_last"].get("first")),
            "last_block": _block(info["first_last"].get("last")),
            "emission_start": (
                {"day": info["emission_start"][0], "block": info["emission_start"][1], "block_hash": info["emission_start"][2]}
                if info.get("emission_start") else NOT_RECORDED
            ),
            "publication_readback": readback,
            "canonical_score_file_sha256": canonical_sha or NOT_RECORDED,
            "notes": "Block timestamps are not held by the history store: day_window_utc is derived from day_epoch_ts. "
                     "Block hashes are present only for blocks the store sampled.",
        }

        bundle = scratch / "bundle"
        bundle.mkdir()
        (bundle / "fixture.json").write_text(canonical(fixture), encoding="utf-8")
        (bundle / "score.json").write_text(score_text, encoding="utf-8")
        if published_raw is not None:
            (bundle / verify_bundle.PUBLISHED).write_bytes(published_raw)  # verbatim, as the validator wrote it
        (bundle / "settings.json").write_text(settings_text, encoding="utf-8")
        (bundle / "chain-refs.json").write_text(canonical(chain_refs), encoding="utf-8")
        (bundle / "README.txt").write_text(
            readme_text(args.network, eval_day, utc_date, mode, published_raw is not None, code_commit),
            encoding="utf-8")

        manifest = {
            "schema": BUNDLE_SCHEMA,
            "network": args.network,
            "eval_day": str(eval_day),
            "utc_date": utc_date,
            "code_commit": code_commit,
            "fixture_provenance": provenance,
            "score_source": score_source,
            "settings_source": settings_source,
            "verification_command": (
                f"git checkout {code_commit} && cd services/noah && "
                "python3 scripts/verify_bundle.py <this-directory>"
            ),
            "published_score": verify_bundle.PUBLISHED if published_raw is not None else NOT_RECORDED,
            "files": [
                {"name": n, "sha256": verify_bundle.sha256_file(bundle / n), "size": (bundle / n).stat().st_size}
                for n in verify_bundle.data_files(bundle)
            ],
        }
        (bundle / "MANIFEST.json").write_text(canonical(manifest), encoding="utf-8")
        sums = "".join(f"{verify_bundle.sha256_file(bundle / n)}  {n}\n" for n in (*verify_bundle.data_files(bundle), "MANIFEST.json"))
        (bundle / "SHA256SUMS").write_text(sums, encoding="utf-8")

        hits = secret_scan.scan_paths([bundle])
        if hits:
            lines = "\n".join(f"  {f}" for f in hits)
            raise BundleRefusal(f"SECRET SCAN REFUSED THIS BUNDLE. Nothing was placed.\n{lines}", EXIT_SECRET)

        sink = _Sink()
        code = verify_bundle.verify(bundle, out=sink)
        if code != 0:
            raise BundleRefusal(f"the new bundle failed its own verification; nothing was placed.\n{sink.text}")

        return _place(bundle, Path(args.records_root), args.network, utc_date, eval_day, manifest, args.force)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


class _Sink:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def _block(entry):
    return entry if entry else {"block": NOT_RECORDED, "block_hash": NOT_RECORDED}


def _place(bundle: Path, root: Path, network, utc_date, eval_day, manifest, force) -> Path:
    net_dir = root / network
    dest = net_dir / utc_date
    new_manifest_sha = verify_bundle.sha256_file(bundle / "MANIFEST.json")
    if dest.exists():
        old = dest / "MANIFEST.json"
        if old.is_file() and verify_bundle.sha256_file(old) == new_manifest_sha:
            print(f"unchanged: {dest} already holds this exact bundle")
            return dest
        if not force:
            raise BundleRefusal(f"{dest} already holds a different bundle; pass --force to replace it")
        shutil.rmtree(dest)
    net_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, dest)
    index_path = net_dir / "INDEX.json"
    days = []
    if index_path.is_file():
        days = json.loads(index_path.read_text(encoding="utf-8")).get("days", [])
    days = [d for d in days if str(d.get("eval_day")) != str(eval_day)]
    days.append({
        "eval_day": str(eval_day), "utc_date": utc_date, "path": f"{utc_date}/",
        "manifest_sha256": new_manifest_sha,
        "fixture_provenance": manifest["fixture_provenance"]["method"],
    })
    days.sort(key=lambda d: int(d["eval_day"]))
    tmp = index_path.with_name("INDEX.json.tmp")
    tmp.write_text(canonical({"schema": INDEX_SCHEMA, "network": network, "days": days}), encoding="utf-8")
    os.replace(tmp, index_path)
    return dest


def parse_args(argv):
    from noah.chain.sources import QuietArgumentParser

    p = QuietArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--network", required=True)
    p.add_argument("--eval-day", required=True, type=int)
    p.add_argument("--records-root", required=True)
    p.add_argument("--store")
    p.add_argument("--fixture")
    p.add_argument("--dials")
    p.add_argument("--score")
    p.add_argument("--settings")
    p.add_argument("--profile")
    p.add_argument("--genesis-hash")
    p.add_argument("--netuid")
    p.add_argument("--ark-address")
    p.add_argument("--validator-hotkey")
    p.add_argument("--pinned-height", type=int)
    p.add_argument("--publication-readback")
    p.add_argument("--code-commit")
    p.add_argument("--date", help="override the UTC date directory (default: the evaluated day's UTC date)")
    p.add_argument("--force", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    from noah.chain.sources import guard_endpoint_logging

    guard_endpoint_logging()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        dest = build_bundle(args)
    except BundleRefusal as refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return refusal.code
    except (OSError, ValueError, KeyError) as why:
        print(f"REFUSED: {type(why).__name__}: {why}", file=sys.stderr)
        return EXIT_USAGE
    print(f"bundle written: {dest}")
    return EXIT_BUILT


if __name__ == "__main__":
    sys.exit(main())
