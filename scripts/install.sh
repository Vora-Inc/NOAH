#!/usr/bin/env bash
# Set up a repository checkout with two Python environments and Node dependencies.
# Run ./scripts/install.sh; usable environments are reused and unusable existing
# paths are preserved for the operator to inspect.
# .venv installs dev tools without Bittensor. .venv-validator installs dev and
# validator extras. This separation tests an SDK import boundary; it does not
# establish that arbitrary software in an environment cannot sign transactions.
# Node 24 or newer is required. Commit hooks are installed when configured.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
VENV_VALIDATOR="$REPO_ROOT/.venv-validator"
TS_DIR="$REPO_ROOT/verification/noah-ts"
CONSTRAINTS="$REPO_ROOT/constraints.txt"
MIN_NODE_MAJOR=24

echo "Noah subnet: installing into $REPO_ROOT"
echo

# ---------------------------------------------------------------- Python ----

python_is_new_enough() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_new_enough "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

create_venv() {
  local venv="$1"
  if [ -x "$venv/bin/python" ] && python_is_new_enough "$venv/bin/python"; then
    echo "      reusing $venv ($("$venv/bin/python" --version 2>&1))"
    return 0
  fi
  if [ -e "$venv" ] || [ -L "$venv" ]; then
    echo "ERROR: existing $venv is not a usable Python 3.10+ environment." >&2
    echo "       Move it aside yourself, then rerun. Nothing was deleted." >&2
    exit 1
  fi
  if command -v uv >/dev/null 2>&1; then
    echo "      creating $venv with uv"
    uv venv --python 3.11 "$venv"
  elif PYTHON_BIN="$(find_python)"; then
    echo "      creating $venv with $PYTHON_BIN"
    "$PYTHON_BIN" -m venv "$venv"
  else
    echo "ERROR: no Python 3.10 or newer was found on this machine." >&2
    echo "       Install Python 3.10+ (or install uv, https://docs.astral.sh/uv/)" >&2
    echo "       and run this script again." >&2
    exit 1
  fi
}

install_extras() {
  local venv="$1" extras="$2"
  if [ ! -f "$CONSTRAINTS" ]; then
    echo "ERROR: reviewed constraints.txt is missing; refusing an unconstrained install." >&2
    exit 1
  fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "$venv/bin/python" --constraint "$CONSTRAINTS" --editable ".$extras"
  else
    "$venv/bin/python" -m pip install --quiet --upgrade pip
    "$venv/bin/python" -m pip install --quiet --constraint "$CONSTRAINTS" --editable ".$extras"
  fi
}

# Capture probe output separately so a failed import cannot be masked by echo.

echo "[1/6] Python virtualenv (.venv -- dev tools, no bittensor)"
create_venv "$VENV"

echo
echo "[2/6] Python package and dev tools (.venv)"
install_extras "$VENV" "[dev]"
PYTEST_VERSION="$("$VENV/bin/python" -m pytest --version 2>&1 | head -1)" || {
  echo "ERROR: pytest did not install correctly into $VENV:" >&2
  echo "       $PYTEST_VERSION" >&2
  exit 1
}
echo "      installed: $PYTEST_VERSION"
if "$VENV/bin/python" -c 'import bittensor' >/dev/null 2>&1; then
  echo "ERROR: bittensor is importable in $VENV. It must not be -- that absence is the" >&2
  echo "       deposit tool's custody boundary, and scripts/run-tests-python.sh refuses" >&2
  echo "       to trust this venv for anything else if it can reach bittensor. Something" >&2
  echo "       outside this script installed it here; move that environment aside and rerun." >&2
  exit 1
fi

echo
echo "[3/6] Python virtualenv (.venv-validator -- dev tools plus bittensor)"
create_venv "$VENV_VALIDATOR"

echo
echo "[4/6] Python package, dev tools and bittensor (.venv-validator)"
install_extras "$VENV_VALIDATOR" "[dev,validator]"
BITTENSOR_VERSION="$("$VENV_VALIDATOR/bin/python" -c 'import bittensor; print(bittensor.__version__)' 2>&1)" || {
  echo "ERROR: bittensor did not install correctly into $VENV_VALIDATOR:" >&2
  echo "       $BITTENSOR_VERSION" >&2
  exit 1
}
echo "      installed: bittensor $BITTENSOR_VERSION"
"$VENV_VALIDATOR/bin/noah-miner" --help >/dev/null
echo "      installed: noah-miner (activate .venv-validator to use it)"

# Recheck SDK absence after installing the validator environment to detect aliasing.
if "$VENV/bin/python" -c 'import bittensor' >/dev/null 2>&1; then
  echo "ERROR: bittensor is importable in $VENV after building $VENV_VALIDATOR," >&2
  echo "       though it was not right after $VENV alone was built. The two venvs" >&2
  echo "       are not as separate as this script assumes (a symlink between them," >&2
  echo "       for example). Fix the aliasing and rerun." >&2
  exit 1
fi

# ------------------------------------------------------------------ Node ----

echo
echo "[5/6] Node dependencies for the TypeScript implementation"
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node was not found. Install Node $MIN_NODE_MAJOR or newer and run again." >&2
  exit 1
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt "$MIN_NODE_MAJOR" ]; then
  echo "ERROR: Node $(node --version) is too old. This repo needs Node $MIN_NODE_MAJOR or newer," >&2
  echo "       which runs TypeScript sources directly with no build step." >&2
  exit 1
fi
( cd "$TS_DIR" && npm ci --silent )
echo "      installed Node deps in verification/noah-ts (node $(node --version))"

# ------------------------------------------------------------ Git hooks -----

echo
echo "[6/6] Commit hooks"
if [ -d "$REPO_ROOT/.git" ] && [ -f "$REPO_ROOT/.pre-commit-config.yaml" ] && [ -x "$VENV/bin/pre-commit" ]; then
  "$VENV/bin/pre-commit" install >/dev/null
  echo "      pre-commit hooks installed"
else
  echo "      skipped (no Git checkout, hook configuration, or pre-commit executable)"
fi

echo
echo "Done."
echo "Miner commands: source .venv-validator/bin/activate; noah-miner --help"
echo "Next: run ./scripts/run-tests.sh to check both implementations and the twin proof."
