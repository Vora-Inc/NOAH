#!/usr/bin/env bash
# Separate decoder environment; never install scalecodec over the validator SDK.
set -euo pipefail
SERVICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_ENV="$SERVICE_ROOT/.venv-reports"
REPORT_BOOTSTRAP_PYTHON="${REPORT_BOOTSTRAP_PYTHON:-python3}"
if [ -L "$REPORT_ENV" ]; then
  echo "Refusing a symlinked report environment." >&2
  exit 1
fi
"$REPORT_BOOTSTRAP_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "reports require Python 3.11+"'
if [ ! -d "$REPORT_ENV" ]; then
  "$REPORT_BOOTSTRAP_PYTHON" -m venv "$REPORT_ENV"
fi
test -x "$REPORT_ENV/bin/python"
"$REPORT_ENV/bin/python" -c 'import importlib.util; assert importlib.util.find_spec("bittensor") is None, "report environment must not contain the full validator SDK"'
"$REPORT_ENV/bin/python" -m pip install --require-hashes -r "$SERVICE_ROOT/report-requirements-lock.txt"
"$REPORT_ENV/bin/python" -m pip install --require-hashes -r "$SERVICE_ROOT/report-wallet-constraints.txt"
"$REPORT_ENV/bin/python" -m pip install --no-deps -e "$SERVICE_ROOT"
"$REPORT_ENV/bin/python" -m pip check
"$REPORT_ENV/bin/noah-miner" report --help
"$REPORT_ENV/bin/noah-validator" reports --help
