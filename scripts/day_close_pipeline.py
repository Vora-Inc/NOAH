"""Run and inspect a validator day-close pipeline.

Read a pinned day's observations, score them, compare independent implementations,
prepare the submission vector, and optionally publish. Comparison establishes
agreement on supplied inputs, not independent truth of those chain inputs.
Optional emission reconciliation compares event distribution with storage and
has the limitations described by verification/hand/reconcile_incentive_live.py.

The default is a dry run. --publish requests sealed publication; --publish-direct
is an explicit non-production direct mode. They are mutually exclusive and
neither is a fallback for the other. Inclusion readback does not by itself prove
that a later sealed reveal completed.

Evidence is saved to --out-dir and the canonical day-<evalDay>.json location.
--fixture isolates canonical-equivalent synthetic evidence in a subdirectory;
it does not change --out-dir. Conflicting canonical results are announced and
overwritten by default. Use --on-canonical-conflict refuse to preserve prior
results and return exit code 6. Other named refusals return 1; success returns 0.

Use the validator environment and configure both chain endpoints and durable
state explicitly. --force-rescan is a deliberate re-read of a named day, not
the normal unattended mode. For available arguments run this script with --help.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

_SOFTWARE_NOAH_ROOT = Path(__file__).resolve().parents[1]
_HAND_DIR = _SOFTWARE_NOAH_ROOT / "verification" / "hand"
_SCRIPTS_DIR = _SOFTWARE_NOAH_ROOT / "scripts"
for _p in (str(_SOFTWARE_NOAH_ROOT), str(_HAND_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from noah.diagnostics import configure as configure_logging, write as diagnostic, run_entrypoint  # noqa: E402
from noah.chain.sources import Refusal  # noqa: E402  (path must be extended first, see above)
from noah.pathtext import derived_path_text, plain_path_text  # noqa: E402


class AlreadyPublishedOnChain(Exception):
    """``--publish`` was asked for a day whose sealed commit is already on chain.
    A second commit would be a duplicate, so nothing is sent (issue #294/#291)."""


class PipelineStepFailed(Exception):
    """One real step refused or raised. Carries which step and why, real,
    never swallowed into a generic message.

    Catches ``(Exception, Refusal)`` explicitly at every real chain call
    below, not a bare ``except Exception``: ``Refusal`` deliberately
    extends ``BaseException``, not ``Exception``
    (``noah/chain/sources.py``'s own docstring: "a refusal that [an
    ``except Exception``] loop can swallow is not a refusal at all"), so a
    plain ``except Exception`` here would never catch one at all -- the
    named refusal (``DayNotComplete``, an engine disagreement, etc.) would
    propagate as a raw, unhandled traceback instead of this step's own
    clear ``PipelineStepFailed`` report. #15's finding, checked against
    this file specifically: not a silent false pass here (an uncaught
    ``BaseException`` still crashes loudly, not silently), but real,
    named refusals deserve this script's own clear reporting, not a bare
    traceback -- so both are caught here, explicitly, by name.
    """

    def __init__(self, step, error):
        self.step = step
        self.error = error
        super().__init__(f"step {step!r} failed: {type(error).__name__}: {error}")


_UNATTRIBUTED_LIST_LIMIT = 20


def run_pipeline(
    adapter,
    height,
    *,
    publish=False,
    publish_direct=False,
    reconcile_from_block=None,
    reconcile_to_block=None,
):
    """Run all five real steps against one real pinned ``height``.

    Returns a dict with one key per step (``read``, ``score``,
    ``hand_verification``, ``weights``, ``emissions``), each holding that
    step's real result or ``{"skipped": "<reason>"}`` when a later step
    legitimately could not run (e.g. emissions reconciliation with no
    epoch block in range yet). Raises :class:`PipelineStepFailed` the
    moment any REQUIRED step (read, score, hand-verification) refuses --
    this never silently continues past a real disagreement or refusal.

    ``publish`` and ``publish_direct`` are mutually exclusive: at most one
    of the two real publish paths runs, and passing neither is the default
    dry run. Ticket #16 (B06), issue #56 comment 5720306237:
    ``publish_direct`` is never attempted because ``publish`` failed or
    was skipped -- a caller who wants it passes it, by name, itself. See
    this module's own docstring, step 4, for why the two are not
    interchangeable.
    """
    if publish and publish_direct:
        raise ValueError(
            "publish and publish_direct are mutually exclusive: this pipeline runs at most "
            "one real publish path per call, chosen explicitly, never both."
        )
    result = {}

    # 1. Read
    try:
        observations = adapter.read(height)
    except (Exception, Refusal) as error:
        raise PipelineStepFailed("read", error) from error
    # #386: keep the exact input handed to the scorer so the day-close can
    # record it (fixture_record.py). A deep copy: nothing downstream may
    # change what the record says was scored. Never allowed to fail the run.
    try:
        adapter.last_scored_fixture = copy.deepcopy(observations)
    except Exception:  # noqa: BLE001
        pass
    result["read"] = {
        "schema": observations.get("schema"),
        "evalDay": observations.get("evalDay"),
        "minerCount": len(observations.get("miners", [])),
    }
    # Rule B (#338): deposits that moved alpha into the Ark but credit nobody
    # (no miner hotkey named, or the wrong validator), with the reason each
    # was recorded with. The list mixes the two kinds; the reason text tells
    # them apart. It also lists legacy rows (stored before rule B, no reason
    # recorded), flagged ``legacy``, which the count includes. Visible here so a miner who sent one can be found.
    unattributed = getattr(adapter, "last_unattributed_deposits", None)
    if unattributed is not None:
        eval_day = observations.get("evalDay")
        today = [d for d in unattributed if str(d["day"]) == str(eval_day)]
        result["read"]["unattributedDeposits"] = {
            "count": len(today),
            "legacyCount": sum(1 for d in today if d.get("legacy")),
            "totalRao": str(sum(d["amount_rao"] for d in today)),
            "countThroughEvalDay": len(unattributed),
            "deposits": today[:_UNATTRIBUTED_LIST_LIMIT],
            "truncated": len(today) > _UNATTRIBUTED_LIST_LIMIT,
        }

    # 2. Score
    try:
        scored = adapter.score(observations)
    except (Exception, Refusal) as error:
        raise PipelineStepFailed("score", error) from error
    result["score"] = scored

    # 3. Independently verify (the hand tool, third leg beyond the internal twin-proof)
    import compare_score

    try:
        hand_report = compare_score.compare_fixture(observations)
    except compare_score.CrossCheckRefusal as error:
        raise PipelineStepFailed("hand_verification", error) from error
    if not hand_report["agree"]:
        raise PipelineStepFailed(
            "hand_verification",
            RuntimeError(
                f"the real engine and the independent hand calculation DISAGREE on real "
                f"data at height {height} -- fixture {hand_report['fixture_name']!r}, "
                f"{hand_report['miner_count']} miner(s). This is the exact risk issue #16 "
                f"names: two engines agreeing on the same wrong input is not enough, and "
                f"here even that is not the case -- the hand path itself disagrees."
            ),
        )
    result["hand_verification"] = {
        "agree": hand_report["agree"],
        "fixture_name": hand_report["fixture_name"],
        "miner_count": hand_report["miner_count"],
    }

    # 4. Weights: agree() (twin-proof vector), then EITHER publish() or
    #    publish_direct(), never both, only if asked for by name.
    try:
        agreed_vector = adapter.agree(observations)
    except (Exception, Refusal) as error:
        raise PipelineStepFailed("agree", error) from error
    weights_step = {"vector": agreed_vector, "published": None, "published_direct": None}
    # Issue #291: the vector run_period WOULD submit for this day, from the same
    # function (and the same adapter seat map) it uses. Not a record of any
    # submission: that is day-N.submitted.json, written at publish time.
    # `vector` above stays the engine's answer. A refusal here (an owner seat
    # would be paid, ...) is recorded as text, never hidden and never fatal.
    try:
        from noah.core.period import submission_vector

        _decision, submitted = submission_vector(adapter, height, agreed_vector)
        weights_step["publishable_vector"] = {str(uid): w for uid, w in sorted(submitted.items())}
    except (Exception, Refusal) as error:
        weights_step["publishable_vector_error"] = f"{type(error).__name__}: {error}"
    if publish:
        # Duplicate guard (same check the validator loop makes). If the answer cannot
        # be established the run refuses: a human re-run is not a reason to guess.
        try:
            already = adapter.already_published(height)
        except (Exception, Refusal) as error:
            raise PipelineStepFailed("publish", error) from error
        if already:
            raise PipelineStepFailed(
                "publish",
                AlreadyPublishedOnChain(
                    f"height {height}: the chain already shows a commit from this hotkey for "
                    f"this day. Nothing was sent; a second commit would be a duplicate. If you "
                    f"believe the day is NOT published, stop and follow the missed-day procedure "
                    f"(issue #294) before doing anything else."
                ),
            )
        to_send = _vector_to_send(adapter, height, agreed_vector, "publish")
        _retain_calculation(adapter, height, observations, agreed_vector, to_send)
        try:
            published = adapter.publish(to_send, height)
        except (Exception, Refusal) as error:
            raise PipelineStepFailed("publish", error) from error
        weights_step["published"] = published
        _record_submitted(adapter, weights_step, height, to_send, published)
        # Same ledger entry the validator path writes, so a later check attributes this
        # commit to its day. Never raises (the commit is already on chain).
        weights_step["ledger"] = adapter.record_commit(height, published)
    elif publish_direct:
        # A structurally separate branch, not a fallback from the one
        # above: reached only when the caller passed --publish-direct
        # itself, never because --publish (above) raised or was skipped.
        to_send = _vector_to_send(adapter, height, agreed_vector, "publish_direct")
        _retain_calculation(adapter, height, observations, agreed_vector, to_send)
        try:
            published_direct = adapter.publish_direct(to_send, height)
        except (Exception, Refusal) as error:
            raise PipelineStepFailed("publish_direct", error) from error
        weights_step["published_direct"] = published_direct
        _record_submitted(adapter, weights_step, height, to_send, published_direct)
    else:
        weights_step["skipped_publish"] = (
            "dry run (neither --publish nor --publish-direct given): the vector above is "
            "what WOULD be submitted; nothing was signed or sent."
        )
    result["weights"] = weights_step

    # 5. Emissions reconciliation, over the same day's real range if given.
    #
    # Deliberately NOT a required step (unlike read/score/hand_verification
    # above): steps 1-4 are this pipeline's own real, decided result for the
    # day, already complete and worth keeping the moment they succeed. A
    # real infrastructure interruption during this optional, separate scan
    # (e.g. ChainUnavailable -- a real Refusal, not a made-up one) must not
    # discard four already-real, already-succeeded steps' worth of evidence
    # -- found for real (ticket #16, 2026-09-17): a mid-run tunnel drop at
    # block 411 during this exact scan crashed the whole script and lost
    # steps 1-4's own real success along with it, before this fix. Caught
    # the identical soft way NoEpochBlockFound already was, distinguished
    # by its own real reason in the "skipped" message -- never silently
    # treated as "nothing to reconcile" when the real reason is "could not
    # ask."
    if reconcile_from_block is not None and reconcile_to_block is not None:
        import reconcile_incentive_live as ril

        try:
            block, events = ril.find_real_epoch_block(
                adapter.runner_chain, adapter.netuid, reconcile_from_block, reconcile_to_block
            )
        except ril.NoEpochBlockFound as error:
            result["emissions"] = {"skipped": str(error)}
        except (Exception, Refusal) as error:
            result["emissions"] = {
                "skipped": (
                    f"a real error interrupted the emissions-reconciliation scan (steps 1-4 "
                    f"above already succeeded and are kept): {type(error).__name__}: {error}"
                )
            }
        else:
            try:
                emissions_report = ril.reconcile_live(
                    adapter.runner_chain,
                    netuid=adapter.netuid,
                    block=block,
                    events=events,
                )
            except (Exception, Refusal) as error:
                result["emissions"] = {
                    "skipped": (
                        f"a real error interrupted reconciling the real epoch block "
                        f"{block} (steps 1-4 above already succeeded and are kept): "
                        f"{type(error).__name__}: {error}"
                    )
                }
            else:
                result["emissions"] = emissions_report
    else:
        result["emissions"] = {"skipped": "no reconcile_from_block/reconcile_to_block given"}

    return result


def _vector_to_send(adapter, height, agreed_vector, step):
    """Issue #291: what a manual publish sends is exactly what run_period sends,
    `noah.core.period.submission_vector`: every live seat named (an all-zero day
    becomes a full vector of zeroes via keeper.decide, never `{}`), a seat that
    changed hands zeroed, and a refusal (SeatOwnerUnknown, OwnerWouldBePaid,
    KeeperWouldPayOwner, InvalidScore) stops the run before anything is sent."""
    from noah.core.period import submission_vector

    try:
        _decision, vector = submission_vector(adapter, height, agreed_vector)
    except (Exception, Refusal) as error:
        raise PipelineStepFailed(step, error) from error
    return vector


def _retain_calculation(adapter, height, observations, agreed, vector):
    """Use the automatic path's durable evidence gate before a manual send."""
    record = getattr(adapter, "record_calculation", None)
    if callable(record):
        try:
            record(height, observations, agreed, vector)
        except (Exception, Refusal) as error:
            raise PipelineStepFailed("calculation_evidence", error) from error


def _record_submitted(adapter, weights_step, height, vector, published):
    """Issue #291: record exactly the vector this script just passed to publish
    (day-N.submitted.json). Never raises; an adapter without the method is skipped."""
    record = getattr(adapter, "record_submitted", None)
    if callable(record):
        weights_step["submitted_record"] = record(height, vector, published)


def evidence_output(result):
    """The score dict to write as evidence: the engine output unchanged, plus
    ``publishable_weights_u16`` (issue #291), the vector `run_period` WOULD
    submit for the same inputs, in whole numbers before the chain's max
    normalisation. It is neither a submission record (``day-N.submitted.json``)
    nor a chain read-back (``day-N.confirmed.json``). Added to a copy; absent
    when the vector could not be computed."""
    publishable = result.get("weights", {}).get("publishable_vector")
    if publishable is None:
        return result["score"]
    return {**result["score"], "publishable_weights_u16": publishable}


def main(argv=None) -> int:
    from noah.chain.sources import guard_endpoint_logging

    guard_endpoint_logging()
    argv = sys.argv[1:] if argv is None else argv
    from noah.chain.sources import QuietArgumentParser

    parser = QuietArgumentParser(description=__doc__)
    parser.add_argument("--height", type=int, required=True, help="the real pinned height to read")
    parser.add_argument("--out-dir", type=str, required=True, help="where to write evidence JSON")
    publish_group = parser.add_mutually_exclusive_group()
    publish_group.add_argument(
        "--publish",
        action="store_true",
        help="actually sign and submit, sealed -- the production path (irreversible)",
    )
    publish_group.add_argument(
        "--publish-direct",
        action="store_true",
        help=(
            "sign and submit using direct publication on an explicitly configured "
            "non-production chain; never use this option for sealed publication"
        ),
    )
    parser.add_argument(
        "--on-canonical-conflict",
        choices=["overwrite", "refuse"],
        default="overwrite",
        help=(
            "what to do when the canonical day-<evalDay>.json evidence file already exists "
            "and this run's real computed result differs from it. 'overwrite' (default, "
            "unchanged behavior for a human running this script by hand): print a loud, "
            "explicit line and overwrite it. 'refuse': never overwrite automatically -- "
            "exit 6 and leave the existing file completely untouched."
        ),
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help=(
            "this run's data is synthetic/fixture (e.g. a small sanity-check height against "
            "a test/fixture chain), not a real validator day close. Routes this run's "
            "canonical-equivalent evidence to persist_score_evidence.FIXTURE_SCORES_DIR (an "
            "isolated subdirectory, separate from live score evidence) instead of "
            "the real CANONICAL_SCORES_DIR. Does not change --out-dir behavior."
        ),
    )
    parser.add_argument(
        "--reconcile-from-block",
        type=int,
        default=None,
        help="start of the block range to search for a real epoch/pay event to reconcile",
    )
    parser.add_argument(
        "--reconcile-to-block",
        type=int,
        default=None,
        help="end of the block range to search for a real epoch/pay event to reconcile",
    )
    parser.add_argument(
        "--force-rescan",
        action="store_true",
        help=(
            "force a genuinely fresh rescan of this one day, even though its local history "
            "store already has a scanned_day marker for it -- a deliberate, one-time operator "
            "action on ONE NAMED DAY ONLY (noah/core/chain_adapter.py:build_chain_adapter's own "
            "may_resume docstring), never the default. Exists for exactly one case: a day "
            "whose store record predates a real fix to what this script reads (read_collateral "
            "flipping True being the first real case, 2026-09-22) -- without this, the "
            "unattended resume shortcut trusts the stale marker and skips the day forever, "
            "reusing whatever the pre-fix scan already wrote. Threaded straight through as "
            "may_resume=False on the SAME build_chain_adapter call this script already makes "
            "for both the dry-run/evidence-only path and the real --publish-direct path -- "
            "never a new default: every ordinary run, with or without --publish/--publish-"
            "direct, still leaves may_resume at its own default (True) unless this is passed "
            "explicitly. "
            "**--height resolves to exactly ONE day (noah.chain.dayblocks.day_block_range), "
            "and this flag rescans ONLY that one day -- it cannot reach any OTHER day's own "
            "already-recorded scanned_day marker, even one this same fixture's cumulative "
            "checks (noah.chain.fixture_build) depend on.** Real incident, 2026-09-23 (#16, "
            "#135, #112): a fix that changed what an EARLIER day's own scan would have found "
            "(a wallet's registration-time collateral lock, read only at its own real "
            "registration block) was deployed and --force-rescan was run for the day being "
            "closed, but not for the earlier day some wallets had actually registered in -- 3 "
            "of 4 affected coldkeys, whose registrations fell entirely in that earlier, "
            "never-rescanned day, kept failing with byte-identical pre-fix figures; a 4th, "
            "whose registrations straddled the boundary, looked fixed only because its portion "
            "in the rescanned day happened to be enough, while its earlier day's own real lock "
            "stayed silently unrecorded. Before relying on a fix here, identify EVERY day any "
            "affected wallet's own relevant history falls in (a wallet's firstActivityDay "
            "through the day being closed, not merely the day being closed) and pass this flag "
            "once per --height inside each of THOSE days too, oldest first -- one --force-"
            "rescan run closes exactly one day, never a range. Rule B (#338) note: this "
            "does NOT apply the rule-B deposit reader to days already stored. A stored "
            "deposit with no miner hotkey and no reason that the new reader now gives a "
            "reason raises ObservationChanged, and nothing is rewritten in place. To apply "
            "that change to old days, rebuild the store from the chain into a fresh store "
            "(the recovery tool's rebuild route; the from-empty path is not yet proven end "
            "to end)."
        ),
    )
    args = parser.parse_args(argv)

    if args.publish or args.publish_direct:
        # The manual publisher refuses a mismatched bittensor/scalecodec set
        # exactly as the validator unit does at startup (exit 2, like the signer refusal),
        # before any connection, lock, adapter or send.
        from noah.core.startup import UnsupportedLibraryVersion, check_library_versions

        try:
            check_library_versions()
        except UnsupportedLibraryVersion as error:
            diagnostic(f"ENVIRONMENT REFUSED\n  {error}", file=sys.stderr, severity="error")
            return 2

    from noah.core.settings import KEYS as SETTINGS_KEYS
    from noah.core.settings import Settings
    from noah.core.settings import SettingsError

    mapping = {k: v for k, v in os.environ.items() if k in SETTINGS_KEYS}
    settings = Settings.from_mapping(mapping)
    if args.publish or args.publish_direct:
        requested_mode = "direct" if args.publish_direct else "sealed"
        if settings.publish_mode != requested_mode:
            diagnostic(
                f"REFUSED: requested {requested_mode} publication but NOAH_PUBLISH_MODE is "
                f"{settings.publish_mode!r}. Nothing was sent.", file=sys.stderr, severity="error",
            )
            return 2
    from noah import secretscrub

    secretscrub.activate(settings)
    configure_logging(settings.log_level)
    guard_endpoint_logging()

    from neurons.validator import (
        _commit_ledger_path,
        _default_state_dir,
        _publish_backoff_path,
        _publish_hold_path,
        _submitted_record_dir,
        _publish_intent_path,
        connect,
    )

    client, genesis = connect(settings)
    import bittensor as bt

    from noah.core.chain_adapter import build_chain_adapter
    from noah.core.reader import ChainReader

    reader = ChainReader(client)
    wallet = bt.wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey)
    # The address every validator question is asked about: the validator's own (commit
    # identity) on the proxy route, the loaded hotkey otherwise (noah.core.proxy_route).
    from noah.core.proxy_route import commit_address

    try:
        validator_address = commit_address(settings, wallet.hotkey.ss58_address)
    except SettingsError as error:
        diagnostic(f"SIGNER REFUSED\n  {error}", file=sys.stderr, severity="error")
        client.close()
        return 2
    if args.publish or args.publish_direct:
        # The manual publisher holds the same signing guards as the validator
        # unit, checked before anything is built or sent.
        from noah.core.mainnet import check_signer_hotkey

        try:
            check_signer_hotkey(settings, wallet.hotkey.ss58_address)
        except SettingsError as error:
            diagnostic(f"SIGNER REFUSED\n  {error}", file=sys.stderr, severity="error")
            client.close()
            return 2
    adapter = None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The SAME state directory, ledger and publish-intent marker the validator uses (#291):
    # a manual send and an automatic one share one record of "a send may be in flight".
    state_dir = _default_state_dir()
    writer_lock_info = None
    if args.publish or args.publish_direct:
        # The writer lock the validator loop takes around every period (loop.py `held`,
        # `<state dir>/loop.lock`). Held for this whole run; refused if another writer has it.
        # The loop holds it only while a period runs, so this catches a validator mid-send;
        # the runbook's "stop the validator first" is what covers an idle one.
        from noah.core import writer_lock

        try:
            writer_lock_info = writer_lock.acquire(state_dir / "loop.lock")
        except writer_lock.LockHeld as error:
            diagnostic(
                f"REFUSED: the validator's writer lock {state_dir / 'loop.lock'} is held, so a "
                f"validator may be sending or another manual run is in progress. Nothing was "
                f"sent. Stop the validator first. "
                f"{error}",
                file=sys.stderr, severity="error",
            )
            client.close()
            return 2
    submit_client = None
    try:
        from noah.core.startup import NotRegistered, check_registered

        if args.publish or args.publish_direct:
            # Use the validator unit's startup gate: subnet, seat, rate limit,
            # consensus-aware permit, history and selected publication mode.
            # Use its dedicated submit client for either publication route.
            from neurons.validator import gate, open_submit_client

            results, _gate_uid = gate(reader, settings, validator_address)
            for label, outcome in results:
                diagnostic(f"  gate: {label}: {outcome}", severity="error" if outcome.startswith("NO") else "info")
            if any(outcome.startswith("NO") for _, outcome in results):
                diagnostic("THE GATE REFUSED. Nothing was sent.", file=sys.stderr, severity="error")
                return 1
            try:
                submit_client = open_submit_client(settings, genesis)
            except SettingsError as error:
                diagnostic(f"SUBMIT ENDPOINT REFUSED\n  {error}", file=sys.stderr, severity="error")
                return 3

        # Resolve the registered uid only for a requested publication.
        # Read/score dry runs need no publishing seat. A requested publish
        # refuses through check_registered if the validator has no seat.
        uid = None
        if args.publish or args.publish_direct:
            uid = check_registered(reader, validator_address, settings.netuid)

        adapter = build_chain_adapter(
            client=client,
            settings=settings,
            reader=reader,
            wallet=wallet,
            uid=uid,
            # `True`, matching `neurons/validator.py`'s own real call site --
            # the unattended loop's already-proven, live configuration, not a
            # new choice invented here. Confirmed live tonight (2026-09-22):
            # this script's own hardcoded `False` reached
            # `noah.chain.runner._locked_pay_reads`, which -- exactly as its
            # own docstring says it will when told not to read collateral --
            # held every paid wallet's locked/unlocked pay out as
            # "unknown rather than zero" and named it, rather than reading
            # the real figure. `noah/chain/fixture_build.py` then refused
            # outright (`MissingFromStore`) for any coldkey that also
            # carries a real entry deposit, which is exactly the shape a
            # real `--publish-direct` catch-up run needs correct: it cannot
            # publish a locked-pay figure this script never tried to read.
            # `False` here was never a considered choice about THIS call
            # site -- `noah/core/chain_adapter.py:build_chain_adapter`'s own
            # docstring ("read_collateral stays False here, unchanged") is
            # about ITS OWN unset-by-caller default, reasoned through for a
            # chain whose deployed runtime was once missing the collateral
            # pallet storage entirely; it also says explicitly that a caller
            # may pass `read_collateral=True` once that upgrade is
            # confirmed, and names `neurons/validator.py`'s own call site as
            # the one to change then. That call site already reads `True`
            # (and has, every real period, successfully) -- this script's
            # own separate, hardcoded `False` was simply never updated to
            # match once the real chain caught up.
            read_collateral=True,
            # `--force-rescan` (this ticket, 2026-09-22), threaded straight
            # through as `may_resume`. `not args.force_rescan` keeps this
            # script's own ordinary, unattended-loop-matching default
            # (`True`) untouched when the flag is absent -- exactly the
            # same call this script already makes for BOTH the dry-run/
            # evidence-only path and the real `--publish-direct` path,
            # since `read_collateral`'s own fix above sits on this same
            # unconditional call site. `may_resume=False` is never this
            # script's own default; see `--force-rescan`'s own help text
            # and `build_chain_adapter`'s own `may_resume` docstring for
            # why this stays an explicit, one-time operator choice.
            may_resume=not args.force_rescan,
            # The SAME ledger file the validator loop uses (state dir from NOAH_RUNTIME_ROOT),
            # so already_published sees the validator's commits and this run is recorded.
            commit_ledger_path=_commit_ledger_path(state_dir),
            publish_intent_path=_publish_intent_path(state_dir),
            publish_hold_path=_publish_hold_path(state_dir),
            publish_backoff_path=_publish_backoff_path(state_dir),
            submitted_record_dir=_submitted_record_dir(),
            submit_client=submit_client,
        )
        result = run_pipeline(
            adapter,
            args.height,
            publish=args.publish,
            publish_direct=args.publish_direct,
            reconcile_from_block=args.reconcile_from_block,
            reconcile_to_block=args.reconcile_to_block,
        )
        out_path = out_dir / f"day-close-height-{args.height}.json"
        out_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
        print(f"PIPELINE SUCCEEDED. Evidence: {derived_path_text(out_path)}")
        unattributed_report = result.get("read", {}).get("unattributedDeposits")
        if unattributed_report and unattributed_report["count"]:
            print(
                f"UNATTRIBUTED DEPOSITS on the eval day: {unattributed_report['count']} "
                f"({unattributed_report['totalRao']} rao) credit nobody (unnamed or wrong "
                f"validator; the reason says which). That count includes "
                f"{unattributed_report['legacyCount']} legacy row(s) stored before rule B "
                f"with no reason recorded (a legacy row whose staking hotkey is a seat's own "
                f"hotkey may still credit that seat). See read.unattributedDeposits in the "
                f"evidence for each reason."
            )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))

        if "score" in result:
            from persist_score_evidence import (
                CanonicalEvidenceConflict,
                fixture_score_evidence_path,
                write_canonical_score_evidence,
                write_score_evidence,
            )

            # Issue #112 (traced by #12, operator view): real chain
            # identity, stamped on every real score-output file so a
            # reader can refuse a mismatched one instead of inferring
            # freshness from a timestamp. netuid is the configured setting
            # (no live-discovery path exists anywhere in this codebase);
            # ark_address is adapter.last_confirmed_ark_pin, the real,
            # live, twice-reorg-checked on-chain SubnetIdentitiesV3 answer
            # this exact read() call already confirmed matches the
            # configured coldkey (see ChainAdapter.__init__'s own docstring
            # for that attribute); genesis_hash is the real genesis this
            # script's own connect() call above already verified the chain
            # against, before anything else ran. None of these is a second,
            # separate chain query -- both real reads already happened as
            # required steps of this same pipeline run.
            score_path = out_dir / f"score-height-{args.height}.json"
            write_score_evidence(
                evidence_output(result),
                score_path,
                netuid=settings.netuid,
                ark_address=adapter.last_confirmed_ark_pin,
                genesis_hash=genesis,
            )
            print(f"Score also persisted at: {plain_path_text(score_path)}")

            if args.fixture:
                # Isolate synthetic results from canonical live evidence even
                # when the two runs share an evaluation day.
                fixture_path = fixture_score_evidence_path(result["score"].get("evalDay"))
                write_score_evidence(
                    evidence_output(result),
                    fixture_path,
                    netuid=settings.netuid,
                    ark_address=adapter.last_confirmed_ark_pin,
                    genesis_hash=genesis,
                )
                print(
                    "Fixture run (--fixture): canonical score evidence NOT written -- "
                    f"isolated fixture copy at: {plain_path_text(fixture_path)}"
                )
                _print_settings_record(
                    result["score"].get("evalDay"), fixture_path, adapter, args
                )
                _print_fixture_record(result["score"].get("evalDay"), fixture_path, adapter, args)
            else:
                # Canonical, predictable location (ticket #16, this same
                # round): the real gap found live tonight was that a real
                # day close never automatically produced evidence at any
                # path a live reader (#12's operator view) could find
                # on its own -- only ever at whichever --out-dir a caller
                # picked by hand. Written ADDITIONALLY here, every time a
                # REAL score is actually computed (dry run or real -- a
                # dry run's score is still real for a closed historical
                # day; --fixture above is the only thing that skips this
                # block), regardless of --out-dir. See
                # persist_score_evidence.write_canonical_score_evidence
                # for the explicit-not-silent overwrite behavior, and
                # --on-canonical-conflict above for the refuse-instead-of-
                # overwrite mode an unattended caller should request.
                try:
                    canonical_result = write_canonical_score_evidence(
                        evidence_output(result),
                        on_conflict=args.on_canonical_conflict,
                        netuid=settings.netuid,
                        ark_address=adapter.last_confirmed_ark_pin,
                        genesis_hash=genesis,
                    )
                except CanonicalEvidenceConflict as conflict:
                    out_path = out_dir / f"day-close-height-{args.height}-CANONICAL-CONFLICT.json"
                    out_path.write_text(
                        json.dumps(
                            {"step": "canonical_score_evidence", "error": str(conflict)}, indent=2
                        )
                        + "\n"
                    )
                    diagnostic(f"PIPELINE REFUSED to write canonical score evidence: {conflict}", severity="error")
                    print(f"Evidence: {derived_path_text(out_path)}")
                    return 6
                print(f"Canonical score evidence: {_shown_outcome(canonical_result)}")
                _print_settings_record(
                    result["score"].get("evalDay"), canonical_result["path"], adapter, args
                )
                _print_fixture_record(
                    result["score"].get("evalDay"), canonical_result["path"], adapter, args
                )
        return 0
    except NotRegistered as error:
        out_path = out_dir / f"day-close-height-{args.height}-FAILED.json"
        out_path.write_text(
            json.dumps(
                {"step": "resolve_uid", "error": f"{type(error).__name__}: {error}"},
                indent=2,
            )
            + "\n"
        )
        diagnostic(f"PIPELINE FAILED before starting: {error}", severity="error")
        print(f"Evidence: {derived_path_text(out_path)}")
        return 1
    except PipelineStepFailed as failure:
        out_path = out_dir / f"day-close-height-{args.height}-FAILED.json"
        out_path.write_text(
            json.dumps(
                {"step": failure.step, "error": f"{type(failure.error).__name__}: {failure.error}"},
                indent=2,
            )
            + "\n"
        )
        diagnostic(f"PIPELINE FAILED at step {failure.step!r}: {failure.error}", severity="error")
        print(f"Evidence: {derived_path_text(out_path)}")
        return 1
    finally:
        if writer_lock_info is not None:
            from noah.core import writer_lock

            writer_lock.release(writer_lock_info)
        if submit_client is not None:
            try:
                submit_client.close()
            except Exception:
                pass
        (adapter or client).close()


def _shown_outcome(outcome):
    """A record outcome as it may be printed: the on-disk path is shown by name only when plain."""
    if isinstance(outcome, dict) and outcome.get("path"):
        return {**outcome, "path": derived_path_text(outcome["path"])}
    return outcome


def _print_settings_record(eval_day, day_evidence_path, adapter, args):
    """Write the per-day settings record (#290) beside the day's score
    evidence and print the outcome. Never raises and never changes the
    exit code: a failed record is announced loudly, not allowed to break
    scoring or publication."""
    from settings_record import record_day_settings

    outcome = record_day_settings(
        eval_day,
        day_evidence_path,
        dials=getattr(adapter, "dials", None),
        on_conflict=args.on_canonical_conflict,
    )
    if outcome["action"] in ("failed", "conflict-refused"):
        diagnostic(f"WARNING: SETTINGS RECORD NOT WRITTEN for day {eval_day}: {_shown_outcome(outcome)}", severity="warning")
    else:
        print(f"Settings record: {_shown_outcome(outcome)}")


def _print_fixture_record(eval_day, day_evidence_path, adapter, args):
    """Write the per-day fixture record (#386) beside the day's score
    evidence: the exact ``noah-fixture/6`` input this run scored. Never
    raises and never changes the exit code, like the settings record."""
    from fixture_record import record_day_fixture

    outcome = record_day_fixture(
        eval_day,
        day_evidence_path,
        getattr(adapter, "last_scored_fixture", None),
        on_conflict=args.on_canonical_conflict,
    )
    if outcome["action"] in ("failed", "conflict-refused"):
        diagnostic(f"WARNING: FIXTURE RECORD NOT WRITTEN for day {eval_day}: {_shown_outcome(outcome)}", severity="warning")
    else:
        print(f"Fixture record: {_shown_outcome(outcome)}")


if __name__ == "__main__":
    raise SystemExit(run_entrypoint(main))
