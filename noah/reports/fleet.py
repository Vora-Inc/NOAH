"""Serialize existing assignment workers sharing one report proxy on one host."""

import time
from datetime import datetime, timezone
from pathlib import Path

from .backing.keys import ss58_decode
from .common import ReportError, locked
from .proxy import _lane, route


def validate_fleet(configs):
    if not configs:
        raise ReportError("fleet requires at least one miner config")
    first = configs[0]
    expected = route(first)
    if expected is None:
        raise ReportError("fleet requires an explicit report proxy in every config")
    if not first.assignments_url or not first.assignment_signer_public_key:
        raise ReportError("fleet requires a trusted assignment source and public signing key")
    fields = (
        "endpoint",
        "netuid",
        "genesis_hash",
        "wallet_name",
        "hotkey_name",
        "wallet_path",
        "assignment_signer_public_key",
        "assignments_url",
    )
    seen = set()
    for config in configs:
        actual = route(config)
        if (
            actual is None
            or ss58_decode(actual["delegate"]) != ss58_decode(expected["delegate"])
            or Path(config.state_dir).resolve() != Path(first.state_dir).resolve()
            or any(getattr(config, f, None) != getattr(first, f, None) for f in fields)
        ):
            raise ReportError(
                "fleet must share one proxy, wallet, state directory, chain and assignment source"
            )
        hotkey = ss58_decode(config.miner_hotkey)
        if hotkey in seen:
            raise ReportError("duplicate miner hotkey in fleet")
        seen.add(hotkey)
    return {"miners": len(configs), "proxy": expected["delegate"], "netuid": first.netuid}


def fleet_tick(configs, tick):
    """No parallel calls. An uncertain send is blocked globally by sender_lane."""
    results = []
    for config in configs:
        try:
            result = tick(config)
        except Exception as exc:
            # Do not print SDK errors containing authenticated RPC endpoints.
            result = {
                "action": "error",
                "sent": False,
                "error_type": type(exc).__name__,
                "recovery": "preserve state and retry; unresolved proxy sends block other miners",
            }
        results.append({**result, "miner_hotkey": config.miner_hotkey})
    return results


def run(args, emit):
    from noah.deposits.assistant.config import load_config

    from .assignments import current_document, fetch_supplement_prefix, week_start_date
    from .backing.chain_session import SubstrateChain
    from .schedule_runner import assignment_identity, assignments_tick, reconcile_assignments

    configs = [load_config(str(p)) for p in args.configs]
    summary = validate_fleet(configs)
    if args.check:
        emit({"action": "config_checked", **summary, "chain_verified": False, "signed": False})
        return 0
    if args.timeout_seconds < 1:
        raise ReportError("timeout must be positive seconds")
    deadline = time.monotonic() + args.timeout_seconds
    key, identity = assignment_identity(configs[0])
    documents = {}
    with locked(_lane(configs[0]) / "fleet"):
        chain = SubstrateChain(configs[0].endpoint)
        try:
            refreshed = set()

            def tick(config):
                if time.monotonic() >= deadline:
                    return {"action": "timeout", "sent": False}
                prior = reconcile_assignments(
                    config, chain=chain, allow_mainnet=args.allow_mainnet_report
                )
                if prior is not None:
                    return {"action": "reconciled", "sent": False, "report": prior}
                now = chain.finalized_time_ms() // 1000
                week = week_start_date(now).isoformat()
                if week not in documents:
                    documents[week] = current_document(
                        config.state_dir,
                        config.assignments_url,
                        now,
                        trusted_public_key=key,
                        expected_template_identity=identity,
                    )
                if documents[week] is None:
                    del documents[week]
                    raise ReportError("weekly assignment file is unavailable")
                if getattr(config, "late_joiner_assignments", False) is True:
                    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
                    if day not in refreshed:
                        fetch_supplement_prefix(
                            Path(config.state_dir) / "weekly-assignments",
                            config.assignments_url, documents[week], day,
                            trusted_public_key=key,
                        )
                        refreshed.add(day)
                if time.monotonic() >= deadline:
                    return {"action": "timeout", "sent": False}
                return assignments_tick(
                    config,
                    documents[week],
                    authorized=args.yes,
                    allow_mainnet=args.allow_mainnet_report,
                    chain=chain,
                )

            while time.monotonic() < deadline:
                refreshed.clear()
                results = fleet_tick(configs, tick)
                emit({"action": "fleet_tick", **summary, "miners": results})
                actions = {r["action"] for r in results}
                if actions <= {"complete"}:
                    emit({"action": "complete", **summary, "validator_bonus_verified": False})
                    return 0
                if actions <= {
                    "complete",
                    "not_assigned",
                    "expired",
                    "partial",
                    "disabled",
                    "timeout",
                }:
                    return 1
                if not args.follow:
                    return (
                        1
                        if actions & {"error", "timeout", "partial", "expired", "not_assigned"}
                        else 0
                    )
                if not actions & {"commit", "reveal", "reconciled"}:
                    time.sleep(min(15, max(0, deadline - time.monotonic())))
            emit({"action": "timeout", "sent": False, "journals_preserved": True})
            return 1
        finally:
            chain.substrate.close()
