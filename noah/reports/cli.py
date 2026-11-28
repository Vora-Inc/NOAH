"""Report worker CLI. Miner transactions and validator reads are separate roles."""

import json
from pathlib import Path
import sys
import time

from noah.chain.sources import QuietArgumentParser, Refusal

from .backing.prepare import PrepareError
from .backing.submit import SubmitError
from .common import ReportError, immutable_json, load_round, read_json


def parser():
    root = QuietArgumentParser(prog="noah-report-worker", description=__doc__)
    roles = root.add_subparsers(dest="role", required=True)
    miner = roles.add_parser("miner", prog="noah-miner report", help="prepare and submit a miner's backing report")
    miner.add_argument("--config", default="noah-deposit.ini", help="existing miner INI configuration")
    actions = miner.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare", aliases=["preview"], help="calculate and save immutable report; no signing")
    prepare.add_argument("--deposit-block", type=int, help="historical deposit-policy rounds only")
    prepare.add_argument("--deposit-event", type=int, help="historical deposit-policy rounds only")
    submit = actions.add_parser("submit", help="advance commit/reveal once, or follow until reveal completes")
    submit.add_argument("--yes", action="store_true", help="authorize this round's commit and reveal transactions")
    submit.add_argument("--allow-mainnet-report", action="store_true", help="explicit production submission opt-in; requires report-only policy and mining subnet82")
    submit.add_argument("--follow", action="store_true", help="wait for windows; safe to resume with the same round")
    submit.add_argument("--timeout-seconds", type=int, default=18000, help="bound --follow; a timeout preserves all journals")
    state = actions.add_parser("status", help="show saved phase states and finalized receipts; no RPC or signing")
    preflight = actions.add_parser("preflight", help="read-only check of the endpoint, runtime, and Sunday roster block")
    preflight.add_argument("--file", type=Path, help="current weekly assignment file; otherwise the configured URL when set")
    scheduled = actions.add_parser("run", help="derive the daily frozen round and advance reporting")
    scheduled.add_argument("--schedule", type=Path, required=True, help="immutable operator-approved schedule JSON")
    scheduled.add_argument("--yes", action="store_true", help="authorize scheduled report transactions")
    scheduled.add_argument("--allow-mainnet-report", action="store_true")
    scheduled.add_argument("--follow", action="store_true")
    scheduled.add_argument("--timeout-seconds", type=int, default=18000)
    assigned = actions.add_parser("assignments", help="fetch this week's three targets and advance all of today's reports")
    assigned.add_argument("--file", type=Path, help="verified assignment file; otherwise the ini assignments_url")
    assigned.add_argument("--yes", action="store_true", help="authorize today's assigned commit and reveal transactions")
    assigned.add_argument("--allow-mainnet-report", action="store_true")
    assigned.add_argument("--follow", action="store_true")
    assigned.add_argument("--timeout-seconds", type=int, default=18000)
    fleet = actions.add_parser("fleet", help="run assigned reports serially for miners sharing one proxy")
    fleet.add_argument("--configs", type=Path, nargs='+', required=True)
    fleet.add_argument("--check", action="store_true", help="validate local config only; no network, keys or signing")
    fleet.add_argument("--yes", action="store_true")
    fleet.add_argument("--allow-mainnet-report", action="store_true")
    fleet.add_argument("--follow", action="store_true")
    # 15,600s from 00:01 UTC ends at 04:21 UTC, after a 04:16 reveal close.
    # Phase windows still come from the signed assignment, not from this budget.
    fleet.add_argument("--timeout-seconds", type=int, default=15600)
    for command in (prepare, submit, state):
        command.add_argument("--round", type=Path, required=True, help="operator's frozen round JSON")

    validator = roles.add_parser("validator", prog="noah-validator reports", help="collect, reconstruct and inspect; never publish weights")
    checks = validator.add_subparsers(dest="action", required=True)
    resolve = checks.add_parser("resolve-day", help="derive a frozen daily round from the configured schedule")
    resolve.add_argument("--invocation", type=Path, help="private JSON with schedule, day, endpoints and require_pair")
    resolve.add_argument("--schedule", type=Path)
    resolve.add_argument("--day", type=int)
    resolve.add_argument("--endpoint", action="append")
    resolve.add_argument("--require-pair", action="store_true")
    resolution = checks.add_parser("verify-resolution", help="offline reconstruct the retained daily boundary proof")
    resolution.add_argument("--schedule", type=Path, required=True)
    resolution.add_argument("--day", type=int, required=True)
    resolution.add_argument("--resolution", type=Path, required=True)
    adjudicate = checks.add_parser("adjudicate", help="collect and independently reconstruct both readers; never publish")
    adjudicate.add_argument("--invocation", "--request", dest="invocation", type=Path, required=True)
    adjudicate.add_argument("--output", "--out", dest="output", type=Path, required=True, help="new private evidence directory")
    replay = checks.add_parser("adjudicate-verify", help="offline reconstruct externally pinned report package")
    replay.add_argument("--request", type=Path, required=True, help="frozen semantic request JSON")
    replay.add_argument("--package", type=Path, required=True)
    replay.add_argument("--manifest-sha256", required=True)
    assignment = checks.add_parser("assignment-week", help="read the week-open roster; unsigned isolated worker")
    assignment.add_argument("--invocation", type=Path, required=True)
    daily_assignment = checks.add_parser("assignment-day", help="read a daily snapshot roster; unsigned isolated worker")
    daily_assignment.add_argument("--invocation", type=Path, required=True)
    publish = checks.add_parser("publish-week", help="write this week's signed assignment file from the chain roster")
    publish.add_argument("--template", type=Path, required=True)
    publish.add_argument("--endpoint", required=True)
    publish.add_argument("--out", type=Path, required=True)
    publish.add_argument("--week", required=True, help="Sunday YYYY-MM-DD")
    publish.add_argument("--signing-key-file", type=Path, required=True,
                         help="private assignment-only signing key file; never a validator hotkey")
    public_export = checks.add_parser("export-public", help="offline prepare approved sanitized website JSON; does not upload")
    from .public_export_cli import add_arguments
    add_arguments(public_export)
    collect = checks.add_parser("collect", help="collect one round from chain into retained evidence")
    watch = checks.add_parser("watch", help="repeat collection/verification until the round closes")
    watch.add_argument("--poll-seconds", type=int, default=30)
    watch.add_argument("--timeout-seconds", type=int, default=18000)
    for command in (collect, watch):
        command.add_argument("--round", type=Path, required=True)
        command.add_argument("--endpoint", action="append", required=True, help="RPC URLs; failover is not independent agreement")
        command.add_argument("--hotkeys", type=Path, required=True, help="trusted JSON list of miner hotkeys")
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--history", type=Path, help="segmented bootstrap ending at snapshot B-1")
        command.add_argument("--allow-genesis-scan", action="store_true", help="allow full scan for a small disposable chain")
    verify = checks.add_parser("verify", help="reconstruct a retained package with network disabled")
    verify.add_argument("--round", type=Path, required=True)
    verify.add_argument("--package", type=Path, required=True)
    verify.add_argument("--out", type=Path, help="new immutable verification file")
    status = checks.add_parser("status", help="show saved package results; no RPC")
    status.add_argument("--evidence", type=Path, required=True)
    history = checks.add_parser("history", help="bootstrap/resume one bounded history range; no scoring")
    history.add_argument("--round", type=Path, required=True)
    history.add_argument("--endpoint", required=True)
    history.add_argument("--dir", type=Path, required=True)
    history.add_argument("--through", type=int, required=True, help="inclusive end block, at most B-1")
    score = checks.add_parser("score-preview", help="offline twin-scored fixture with optional report-weight preview")
    score.add_argument("--fixture", type=Path, required=True)
    score.add_argument("--require-accurate-miner-report", "--apply-reports", dest="apply_reports", action="store_true", help="diagnostic gate only; does not change any runtime setting")
    score.add_argument("--context", type=Path, help="diagnostic chain/seat/registration context JSON")
    score.add_argument("--rounds", type=Path, help="trusted round JSON list")
    score.add_argument("--evidence", type=Path, help="retained raw packages; rebuilt before use")
    score.add_argument("--out", type=Path, help="new immutable preview file")
    return root


def _assignments(config, args):
    from .assignments import current_document, week_start_date
    from .backing.chain_session import SubstrateChain
    from .schedule_runner import (assignments_tick, assignment_identity,
                                  reconcile_assignments, validate_assignment_document)

    url = getattr(config, "assignments_url", None)
    if args.file is None and not url:
        emit({"action": "disabled", "sent": False, "targets": []})
        return 0
    if args.timeout_seconds < 1:
        raise ReportError("timeout must be positive seconds")
    key, identity = assignment_identity(config)
    authorized = args.yes
    if not authorized and sys.stdin.isatty():
        authorized = input("Authorize today's assigned commit and reveal transactions? Type SUBMIT: ") == "SUBMIT"
    deadline = time.monotonic() + args.timeout_seconds
    chain = SubstrateChain(config.endpoint)
    document = None
    try:
        while True:
            # In-flight work may finish its bounded RPCs; this is not a hard tick
            # cancellation deadline. Never start another tick
            # after the follow deadline. Journals remain the restart authority.
            if time.monotonic() >= deadline:
                return 1
            prior = reconcile_assignments(config, chain=chain, allow_mainnet=args.allow_mainnet_report)
            if prior is not None:
                result = {"action": "reconciled", "sent": False, "report": prior}
            else:
                now = chain.finalized_time_ms() // 1000
                if document is None or document['week_start'] != week_start_date(now).isoformat():
                    document = (validate_assignment_document(config, read_json(args.file), now) if args.file else
                                current_document(config.state_dir, url, now, trusted_public_key=key,
                                                 expected_template_identity=identity))
                    if document is None:
                        raise ReportError("weekly assignment file is unavailable")
                if time.monotonic() >= deadline:
                    return 1
                if getattr(config, "late_joiner_assignments", False) is True and url:
                    from datetime import datetime, timezone
                    from .assignments import fetch_supplement_prefix
                    fetch_supplement_prefix(
                        Path(config.state_dir) / "weekly-assignments", url, document,
                        datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
                        trusted_public_key=key)
                result = assignments_tick(
                    config, document, authorized=authorized, allow_mainnet=args.allow_mainnet_report, chain=chain)
            emit(result)
            action = result.get("action")
            if action in ("expired", "disabled", "partial"):
                return 1
            if not args.follow or action in ("complete", "not_assigned"):
                return 0
            time.sleep(min(15, max(0, deadline - time.monotonic())))
    finally:
        chain.substrate.close()


def _roster_hash(path):
    if path is None:
        return None
    document = read_json(path)
    from .miner import _roster_hash as roster_hash
    return roster_hash(document)


def _public_reason(exc):
    import re
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]+", reason):
        return reason
    return None


def emit(value):
    print(json.dumps(value, sort_keys=True, indent=2), flush=True)


def execute(args):
    if args.role == "miner":
        if args.action == 'fleet':
            from .fleet import run
            return run(args, emit)
        from noah.deposits.assistant.config import load_config
        from .miner import prepare_report, status, submit_tick
        config = load_config(args.config)
        if args.action == "run":
            from .schedule_runner import scheduled_tick
            from .adjudication_contract import strict_json, MAX_REQUEST_BYTES
            with args.schedule.open('rb') as handle:
                schedule = strict_json(handle.read(MAX_REQUEST_BYTES + 1))
            authorized = args.yes
            if not authorized and sys.stdin.isatty():
                authorized = input("Authorize this schedule's report transactions? Type SUBMIT: ") == "SUBMIT"
            if args.timeout_seconds < 1:
                raise ReportError('timeout must be positive seconds')
            deadline = time.monotonic() + args.timeout_seconds
            while True:
                if args.follow and time.monotonic() >= deadline:
                    return 1
                result = scheduled_tick(config, schedule, authorized=authorized, allow_mainnet=args.allow_mainnet_report)
                emit(result)
                if result.get('action') == 'expired':
                    return 1
                if not args.follow or result.get('action') == 'complete':
                    return 0
                if time.monotonic() >= deadline:
                    return 1
                time.sleep(min(15, max(0, deadline - time.monotonic())))
        if args.action == "assignments":
            return _assignments(config, args)
        if args.action == "preflight":
            from .miner import preflight
            emit(preflight(config, roster_block_hash=_roster_hash(args.file),
                           lookup_assignments=args.file is None))
            return 0
        contract = load_round(args.round)
        if args.action in ("prepare", "preview"):
            emit(prepare_report(config, contract, args.deposit_block, args.deposit_event))
            return 0
        if args.action == "status":
            emit(status(config, contract))
            return 0
        authorized = args.yes
        if args.timeout_seconds < 1:
            raise ReportError("timeout must be positive seconds")
        if not authorized and sys.stdin.isatty():
            authorized = input("Authorize this round's commit and reveal? Type SUBMIT: ") == "SUBMIT"
        deadline = time.monotonic() + args.timeout_seconds
        while True:
            if args.follow and time.monotonic() >= deadline:
                return 1
            result = submit_tick(config, contract, authorized=authorized, allow_mainnet=args.allow_mainnet_report)
            emit(result)
            if result["action"] == "expired":
                return 1
            if not args.follow or result["action"] == "complete":
                return 0
            if time.monotonic() >= deadline:
                return 1
            time.sleep(min(15, max(0, deadline - time.monotonic())))
    from . import validator
    if args.action in ('resolve-day', 'verify-resolution'):
        from .adjudication_contract import strict_json, MAX_REQUEST_BYTES, fields
        from .schedule import resolve_day, verify_resolution
        def document(path):
            with path.open('rb') as handle:
                return strict_json(handle.read(MAX_REQUEST_BYTES + 1))
        if args.action == 'verify-resolution':
            from .backing.offline import disable_network
            disable_network()
            emit(verify_resolution(document(args.schedule), args.day, document(args.resolution), require_pair=True))
        elif args.invocation:
            if args.schedule or args.day is not None or args.endpoint or args.require_pair:
                raise ReportError('use either private invocation or explicit schedule options')
            value = document(args.invocation)
            fields(value, 'schedule day endpoints require_pair', 'schedule invocation')
            if type(value['require_pair']) is not bool:
                raise ReportError('require_pair must be boolean')
            emit(resolve_day(value['schedule'], value['endpoints'], value['day'], require_pair=value['require_pair']))
        else:
            if args.schedule is None or args.day is None or not args.endpoint:
                raise ReportError('schedule, day and endpoint are required')
            emit(resolve_day(document(args.schedule), args.endpoint, args.day, require_pair=args.require_pair))
        return 0
    if args.action == "assignment-week":
        from .assignment_worker import build
        emit(build(read_json(args.invocation)))
        return 0
    if args.action == "assignment-day":
        from .assignment_worker import build_supplement_invocation
        emit(build_supplement_invocation(read_json(args.invocation)))
        return 0
    if args.action == "publish-week":
        from .assignments import publish_week, load_assignment_key
        template = read_json(args.template)
        trusted_key = template['assignment_signer_public_key']
        keypair = load_assignment_key(args.signing_key_file, trusted_public_key=trusted_key)
        emit(publish_week(template, args.endpoint, keypair, week_start=args.week,
                          out_dir=args.out, trusted_public_key=trusted_key))
        return 0
    if args.action == "export-public":
        from .public_export_cli import execute as export_public
        emit(export_public(args))
        return 0
    if args.action == "adjudicate":
        from .adjudication_contract import load_invocation
        from .adjudication import adjudicate
        emit(adjudicate(load_invocation(args.invocation), args.output))
        return 0
    if args.action == "adjudicate-verify":
        from .adjudication_contract import strict_json, MAX_REQUEST_BYTES
        from .adjudication import verify_adjudication
        from .backing.offline import disable_network
        with args.request.open('rb') as handle:
            request = strict_json(handle.read(MAX_REQUEST_BYTES + 1))
        disable_network()
        emit(verify_adjudication(args.package, request, args.manifest_sha256))
        return 0
    if args.action in ("collect", "watch"):
        contract, hotkeys = load_round(args.round), read_json(args.hotkeys)
        if args.action == "watch" and (args.poll_seconds < 1 or args.timeout_seconds < 1):
            raise ReportError("poll and timeout must be positive seconds")
        deadline = time.monotonic() + getattr(args, "timeout_seconds", 0)
        while True:
            result = validator.collection_tick(contract, args.endpoint, hotkeys, args.out,
                history=args.history, allow_genesis_scan=args.allow_genesis_scan)
            emit(result)
            if args.action == "collect":
                return 0 if result["verification"]["status"] == "RECONSTRUCTED" else 1
            verified = result["verification"]
            verdicts = verified["verdicts"]
            complete = bool(verdicts) and all(v["status"] != "UNVERIFIABLE" for v in verdicts.values())
            if complete:
                return 0
            if time.monotonic() >= deadline:
                return 1
            time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
    if args.action == "status":
        emit(validator.status(args.evidence))
        return 0
    if args.action == "history":
        result = validator.history_tick(load_round(args.round), args.endpoint, args.dir, args.through)
        emit(result)
        return 1 if result.get("error") else 0
    # Only the process executing offline commands has its network disabled.
    from noah.reports.backing.offline import disable_network
    disable_network()
    if args.action == "verify":
        result = validator.verify_report(load_round(args.round), args.package)
    else:
        if args.apply_reports and not all((args.context, args.rounds, args.evidence)):
            raise ReportError("--apply-reports requires --context, --rounds and --evidence")
        result = validator.score_preview(read_json(args.fixture),
            read_json(args.context) if args.context else None,
            read_json(args.rounds) if args.rounds else None, args.evidence,
            enabled=args.apply_reports)
    if args.out:
        immutable_json(args.out, result)
    emit(result)
    return 1 if result.get("status") == "UNVERIFIABLE" else 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except (ReportError, PrepareError, SubmitError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    except ImportError:
        print("Report worker dependency missing; run scripts/install-reports.sh.", file=sys.stderr)
        return 2
    except (Exception, Refusal) as exc:
        # Endpoint/provider errors can embed credentials. Do not print them.
        # A closed reason code has no URL and is safe to show.
        reason = _public_reason(exc)
        name = type(exc).__name__
        shown = f"{name}: {reason}" if reason else name
        print(f"Report operation refused ({shown}); preserve state and check inputs/endpoint.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped; saved report and submission journals are preserved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
