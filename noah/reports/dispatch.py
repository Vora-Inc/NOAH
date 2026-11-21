"""Stable command boundary; the report decoder runs in its own environment."""

import os
from pathlib import Path
import subprocess
import sys


def dispatch(role, args):
    worker = os.environ.get("NOAH_REPORT_PYTHON")
    if not worker:
        candidate = Path(__file__).resolve().parents[2] / ".venv-reports/bin/python"
        worker = str(candidate) if candidate.is_file() else sys.executable
    if not Path(worker).is_file():
        print("Report worker missing. Run scripts/install-reports.sh or set NOAH_REPORT_PYTHON.", file=sys.stderr)
        return 2
    # Execute exactly the file checked above, even for a relative basename.
    # Do not let subprocess resolve a different executable through PATH.
    worker = str(Path(worker).absolute())
    # No shell, secrets, or inherited module search path. Install both packages
    # into the selected worker; do not silently import another checkout's code.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return subprocess.call([worker, "-I", "-m", "noah.reports.cli", role, *args], env=env)


def validator_main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("usage: noah-validator {check,run,guard,status,score,reports} ...")
        print("  check    Read startup/chain checks (--env FILE); no publication")
        print("  run      Existing continuous validator (--env FILE); may publish")
        print("  guard    Existing starter guard (--env FILE); may publish fallback")
        print("  status   Read saved loop state (--state FILE); no RPC")
        print("  score    Twin-check a fixture offline (--fixture FILE); no publication")
        print("  reports  resolve-day, verify-resolution, collect, verify, adjudicate, adjudicate-verify, publish-week, export-public, status, watch, history, score-preview")
        return 0
    command = args.pop(0)
    if command == "reports":
        return dispatch("validator", args)
    if command == "score":
        return dispatch("validator", ["score-preview", *args])
    if command in ("run", "check", "guard"):
        if "--help" in args or "-h" in args:
            print(f"usage: noah-validator {command} [--env FILE]")
            print("Uses the existing validator configuration and safety checks.")
            print("check is read-only; run and guard may publish weights.")
            return 0
        from neurons.validator import main
        from noah.diagnostics import run_entrypoint
        flags = {"run": [], "check": ["--dry-run"], "guard": ["--starter-guard"]}[command]
        return run_entrypoint(lambda: main([*flags, *args]))
    if command == "status":
        import argparse
        import json
        p = argparse.ArgumentParser(prog="noah-validator status", description="Saved state is dated evidence, not a live health check.")
        p.add_argument("--state", type=Path, required=True, help="existing loop.json")
        options = p.parse_args(args)
        try:
            value = json.loads(options.state.read_text())
            print(json.dumps({"source": "saved_loop_state", "live_verified": False, "state": value}, indent=2))
            return 0
        except (OSError, ValueError):
            print("Saved loop state is missing or unreadable.", file=sys.stderr)
            return 2
    print("Unknown command; use noah-validator --help.", file=sys.stderr)
    return 2


def validator_entry():
    raise SystemExit(validator_main())
