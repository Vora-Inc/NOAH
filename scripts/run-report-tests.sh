#!/usr/bin/env bash
# Report dependencies must never be installed into the full validator SDK env.
set -euo pipefail
REPORT_SERVICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_PYTHON="${NOAH_REPORT_PYTHON:-$REPORT_SERVICE_ROOT/.venv-reports/bin/python}"
unset PYTEST_ADDOPTS PYTHONPATH PYTHONHOME
"$REPORT_PYTHON" -I -c 'import importlib.util; import substrateinterface; assert importlib.util.find_spec("bittensor") is None, "reports must use isolated environment"'
cd "$REPORT_SERVICE_ROOT"
"$REPORT_PYTHON" -I -m pytest -q noah/reports/tests
