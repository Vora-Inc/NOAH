#!/usr/bin/env bash
# Run both Python environments, static-error checks, TypeScript tests/types,
# shared scoring comparisons and saved-answer checks. Run install.sh first.
# VALIDATOR_PYTHON and MINER_TOOL_PYTHON override interpreter discovery.
# Missing dependencies or checks that did not run produce a failing summary.
# General style and formatting checks are separate.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Use the distributed test profile unless the caller selected another profile.
if [ -z "${NOAH_PARAMETERS_JSON:-}" ]; then
  for PROFILE in "$REPO_ROOT/docs/parameters/profiles/m1-revised-v1.json" "$REPO_ROOT/../../docs/parameters/profiles/m1-revised-v1.json"; do
    if [ -f "$PROFILE" ]; then
      export NOAH_PARAMETERS_JSON="$PROFILE"
      break
    fi
  done
fi

TS_DIR="$REPO_ROOT/verification/noah-ts"
LOG_DIR="${TMPDIR:-/tmp}/noah-run-tests-$$"
mkdir -p "$LOG_DIR"
trap 'rm -rf "$LOG_DIR"' EXIT

RESULT_NAMES=()
RESULT_STATES=()
RESULT_NOTES=()
FAILED=0

record() {
  RESULT_NAMES+=("$1")
  RESULT_STATES+=("$2")
  RESULT_NOTES+=("$3")
  [ "$2" = "PASS" ] || FAILED=1
}

banner() {
  echo
  echo "------------------------------------------------------------"
  echo "  $1"
  echo "------------------------------------------------------------"
}

# --------------------------------------------------------------- Python -----

# Resolve and verify both interpreters before running their declared suites.
banner "Python tests (dual interpreter: validator + miner-tool)"
if "$REPO_ROOT/scripts/run-tests-python.sh" 2>&1 | tee "$LOG_DIR/pytest-python.log"; then
  PY_EXIT=0
else
  PY_EXIT=$?
fi
VALIDATOR_LINE="$(grep '^RESULT validator ' "$LOG_DIR/pytest-python.log" | tail -1)"
MINER_TOOL_LINE="$(grep '^RESULT miner-tool ' "$LOG_DIR/pytest-python.log" | tail -1)"
COMBINED_LINE="$(grep '^RESULT combined ' "$LOG_DIR/pytest-python.log" | tail -1)"
MINER_TOOL_PYTHON_RESOLVED="$(grep '^RESOLVED miner-tool=' "$LOG_DIR/pytest-python.log" | tail -1 | sed 's/^RESOLVED miner-tool=//')"

parse_result_state() { echo "$1" | grep -Eo 'STATE=[A-Z]+' | cut -d= -f2; }
parse_result_note() { echo "$1" | sed -E 's/^RESULT [a-z-]+ STATE=[A-Z]+ NOTE="(.*)"$/\1/'; }

if [ -n "$VALIDATOR_LINE" ]; then
  record "Python (validator)" "$(parse_result_state "$VALIDATOR_LINE")" "$(parse_result_note "$VALIDATOR_LINE")"
else
  record "Python (validator)" "FAIL" "run-tests-python.sh did not report a result (exit $PY_EXIT) -- see output above"
fi
if [ -n "$MINER_TOOL_LINE" ]; then
  record "Python (miner-tool)" "$(parse_result_state "$MINER_TOOL_LINE")" "$(parse_result_note "$MINER_TOOL_LINE")"
else
  record "Python (miner-tool)" "FAIL" "run-tests-python.sh did not report a result (exit $PY_EXIT) -- see output above"
fi
if [ -n "$COMBINED_LINE" ]; then
  record "Python (combined)" "$(parse_result_state "$COMBINED_LINE")" "$(parse_result_note "$COMBINED_LINE")"
else
  record "Python (combined)" "FAIL" "run-tests-python.sh did not report a result (exit $PY_EXIT) -- see output above"
fi

# Catch static errors in paths the behavioral tests may not exercise. This is
# non-mutating and deliberately does not enforce formatting or import sorting.
banner "Python static errors"
if [ -n "$MINER_TOOL_PYTHON_RESOLVED" ] && "$MINER_TOOL_PYTHON_RESOLVED" -m ruff check --select E9,F63,F7,F82 "$REPO_ROOT"; then
  record "Python static errors" "PASS" "syntax, names and invalid constructs"
else
  record "Python static errors" "FAIL" "static errors or checker unavailable"
fi

# ----------------------------------------------------------- TypeScript -----

# Require the installed TypeScript dependency before running tests or type checks.
if [ -d "$TS_DIR/node_modules/typescript" ]; then
  banner "TypeScript tests"
  if ( cd "$TS_DIR" && npm test ) 2>&1 | tee "$LOG_DIR/npm-test.log"; then
    COUNT="$(grep -Eo 'pass [0-9]+' "$LOG_DIR/npm-test.log" | tail -1)"
    record "TypeScript tests" "PASS" "${COUNT:-passed}"
  else
    record "TypeScript tests" "FAIL" "see output above"
  fi

  banner "TypeScript type check"
  if ( cd "$TS_DIR" && npx tsc --noEmit ); then
    record "TypeScript type check" "PASS" "no type errors"
  else
    record "TypeScript type check" "FAIL" "see output above"
  fi
else
  banner "TypeScript tests"
  echo "RED: $TS_DIR/node_modules/typescript is not present -- node_modules is missing or was" >&2
  echo "     never fully installed. Run ./scripts/install.sh, or" >&2
  echo "     (cd verification/noah-ts && npm ci), then try again." >&2
  record "TypeScript tests" "FAIL" "node_modules not properly installed -- suite did not run"
  record "TypeScript type check" "FAIL" "node_modules not properly installed -- suite did not run"
fi

# ------------------------------------------------------------ twin proof ----

# twin-proof.sh scores noah/scoring/ (zero runtime deps) against its TS twin.
# Use the miner-tool interpreter resolved above so the twin proof keeps the
# same bittensor-free property the dual-interpreter split enforces elsewhere;
# a run where that interpreter never resolved has nothing safe to use here.
banner "Twin proof (Python output vs TypeScript output)"
if [ -n "$MINER_TOOL_PYTHON_RESOLVED" ]; then
  if PYTHON="$MINER_TOOL_PYTHON_RESOLVED" "$REPO_ROOT/scripts/twin-proof.sh" 2>&1 | tee "$LOG_DIR/twin-proof.log"; then
    SUMMARY="$(grep -E 'byte-identical' "$LOG_DIR/twin-proof.log" | tail -1)"
    record "Twin proof" "PASS" "${SUMMARY:-byte-identical}"
  else
    record "Twin proof" "FAIL" "see output above"
  fi
else
  echo "RED: no miner-tool interpreter was resolved above; twin proof needs one" >&2
  echo "     to score the Python side (see the Python (miner-tool) result)." >&2
  record "Twin proof" "FAIL" "no miner-tool interpreter resolved -- did not run"
fi

# Check committed answers independently of the fresh Python/TypeScript comparison.
banner "Saved scoring examples"
if [ -n "$MINER_TOOL_PYTHON_RESOLVED" ] && "$MINER_TOOL_PYTHON_RESOLVED" "$REPO_ROOT/scripts/check_examples.py"; then
  record "Saved scoring examples" "PASS" "all committed answers agree"
else
  record "Saved scoring examples" "FAIL" "missing or stale saved answers"
fi

# --------------------------------------------------------------- summary ----

echo
echo "============================================================"
echo "  Summary"
echo "============================================================"
i=0
while [ "$i" -lt "${#RESULT_NAMES[@]}" ]; do
  printf '  %-4s  %-24s  %s\n' "${RESULT_STATES[$i]}" "${RESULT_NAMES[$i]}" "${RESULT_NOTES[$i]}"
  i=$((i + 1))
done
echo "============================================================"
echo "  Not checked here: general style and formatting"
echo "  Run the documented style checks separately."

if [ "$FAILED" -ne 0 ]; then
  echo "  Something failed. Do not treat this build as verified."
  exit 1
fi
echo "  All green."
