#!/usr/bin/env bash
# Run declared Python suites in two separately checked environments.
# VALIDATOR_DIRS use an interpreter with Bittensor; other declared suites use
# one without it. This checks the SDK import boundary, not general signing ability.
# Missing interpreters, wrong SDK availability and empty collected suites fail.
# VALIDATOR_PYTHON and MINER_TOOL_PYTHON override interpreter discovery.
# Otherwise, use .venv-validator/.venv in this checkout, then the relative
# ../noah-validator/.venv-validator and ../noah-miner-tool/.venv fallbacks.
# The machine-readable RESULT lines report each half and the combined result;
# RESOLVED lines expose only interpreters that passed their SDK availability check.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# PYTEST_ADDOPTS is pytest's own documented way to silently inject extra CLI
# args from the environment (e.g. `--ignore=noah/audit/tests`). Left alone, a
# stray value here could make one of the directories this script explicitly
# asked to run never actually collect, while the aggregate passed-count check
# below stays nonzero and still reports PASS. Cleared so this script's own
# split is what actually runs, not whatever the environment overrides it to.
unset PYTEST_ADDOPTS

# Directories that need the real chain client (bittensor) and therefore run
# under the validator interpreter, not the miner-tool one. See the header
# comment above for why each one is here. Adding a directory here is a
# deliberate classification decision, not automatic -- unlike the rest of
# the split (derived from pyproject.toml's testpaths below), this list is
# not re-derived from anything and must be edited by hand.
VALIDATOR_DIRS=("noah/core/tests" "scripts/tests" "verification/hand/test_reconcile_incentive_live.py")

# True (0) if "$1" is one of VALIDATOR_DIRS, false (1) otherwise.
is_validator_dir() {
  local candidate="$1" d
  for d in "${VALIDATOR_DIRS[@]}"; do
    [ "$candidate" = "$d" ] && return 0
  done
  return 1
}

LOG_DIR="${RUN_TESTS_PYTHON_LOG_DIR:-${TMPDIR:-/tmp}/noah-run-tests-python-$$}"
mkdir -p "$LOG_DIR"
if [ -z "${RUN_TESTS_PYTHON_LOG_DIR:-}" ]; then
  trap 'rm -rf "$LOG_DIR"' EXIT
fi

banner() {
  echo
  echo "---- $1 ----"
}

# --------------------------------------------------------------- resolve ----

# Prints "path|source" on stdout and returns 0 on success. On failure prints
# nothing to stdout, prints the reason to stderr, and returns 1. Never falls
# back to a bare `python3` guess: every candidate here is either an explicit
# override or a specific, documented, checkable path.
resolve_interpreter() {
  local role="$1" override_var="$2"
  shift 2
  local -a candidates=("$@")
  local override_value="${!override_var:-}"

  if [ -n "$override_value" ]; then
    if [ -x "$override_value" ]; then
      echo "$override_value|\$$override_var"
      return 0
    fi
    echo "$role: \$$override_var=$override_value is set but is not an executable file." \
      "Fix the path, or unset $override_var to fall back to auto-detection." >&2
    return 1
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    if [ -x "$candidate" ]; then
      echo "$candidate|$candidate"
      return 0
    fi
  done

  echo "$role: no interpreter found. \$$override_var is not set, and none of these exist:" >&2
  for candidate in "${candidates[@]}"; do
    echo "    $candidate" >&2
  done
  echo "  Set \$$override_var to the interpreter's path." >&2
  return 1
}

VALIDATOR_OK=1
MINER_TOOL_OK=1
VALIDATOR_FAIL_REASON=""
MINER_TOOL_FAIL_REASON=""

banner "Resolving interpreters"

if OUT="$(resolve_interpreter "validator" VALIDATOR_PYTHON \
  "$REPO_ROOT/.venv-validator/bin/python" \
  "$REPO_ROOT/../noah-validator/.venv-validator/bin/python" 2>"$LOG_DIR/validator-resolve.err")"; then
  VALIDATOR_PYTHON_PATH="${OUT%%|*}"
  VALIDATOR_SOURCE="${OUT##*|}"
  echo "validator interpreter:   $VALIDATOR_PYTHON_PATH  (via $VALIDATOR_SOURCE)"
else
  VALIDATOR_OK=0
  VALIDATOR_FAIL_REASON="$(cat "$LOG_DIR/validator-resolve.err")"
  echo "validator interpreter:   UNRESOLVED" >&2
  sed 's/^/    /' "$LOG_DIR/validator-resolve.err" >&2
fi

if OUT="$(resolve_interpreter "miner-tool" MINER_TOOL_PYTHON \
  "$REPO_ROOT/.venv/bin/python" \
  "$REPO_ROOT/../noah-miner-tool/.venv/bin/python" 2>"$LOG_DIR/miner-tool-resolve.err")"; then
  MINER_TOOL_PYTHON_PATH="${OUT%%|*}"
  MINER_TOOL_SOURCE="${OUT##*|}"
  echo "miner-tool interpreter:  $MINER_TOOL_PYTHON_PATH  (via $MINER_TOOL_SOURCE)"
else
  MINER_TOOL_OK=0
  MINER_TOOL_FAIL_REASON="$(cat "$LOG_DIR/miner-tool-resolve.err")"
  echo "miner-tool interpreter:  UNRESOLVED" >&2
  sed 's/^/    /' "$LOG_DIR/miner-tool-resolve.err" >&2
fi

# ---------------------------------------------------------- verify property --

# A resolved path is not trusted by name. Whatever it is -- an override, a
# local convention, a sibling checkout -- it is checked for the one property
# that actually matters before any real test runs on it.

if [ "$VALIDATOR_OK" = 1 ]; then
  if ! "$VALIDATOR_PYTHON_PATH" -c "import bittensor" >/dev/null 2>&1; then
    VALIDATOR_OK=0
    VALIDATOR_FAIL_REASON="$VALIDATOR_PYTHON_PATH cannot import bittensor. ${VALIDATOR_DIRS[*]} need it (chain reads, weight submission, and other real chain-client operations) -- this is not the validator environment."
    echo "validator property check: FAILED -- $VALIDATOR_FAIL_REASON" >&2
  else
    echo "validator property check: bittensor importable, as required"
  fi
fi

if [ "$MINER_TOOL_OK" = 1 ]; then
  if "$MINER_TOOL_PYTHON_PATH" -c "import bittensor" >/dev/null 2>&1; then
    MINER_TOOL_OK=0
    MINER_TOOL_FAIL_REASON="$MINER_TOOL_PYTHON_PATH CAN import bittensor. The miner-tool environment must not be able to -- that absence is the custody boundary. Running the non-core suite here would not prove the boundary holds."
    echo "miner-tool property check: FAILED -- $MINER_TOOL_FAIL_REASON" >&2
  else
    echo "miner-tool property check: bittensor NOT importable, as required"
  fi
fi

# ---------------------------------------------------- derive the split ------

# The split is read from pyproject.toml's own [tool.pytest.ini_options]
# testpaths -- the same list pytest itself already treats as authoritative --
# not re-typed here. If a directory is added to testpaths later, it
# automatically lands on the miner-tool side unless it is also added to
# VALIDATOR_DIRS above (that part is a deliberate, hand-maintained
# classification, not automatic). This needs no interpreter: it's a
# plain-text extraction.
banner "Deriving the split from pyproject.toml testpaths"

TESTPATHS=()
while IFS= read -r line; do
  TESTPATHS+=("$line")
done < <(
  awk '/^testpaths = \[/{flag=1; next} /^\]/{flag=0} flag' pyproject.toml \
    | sed 's/#.*$//' \
    | grep -oE "\"[^\"]*\"|'[^']*'" \
    | sed -e 's/^.//' -e 's/.$//'
)
# Strips trailing comments before extracting quoted entries, and accepts
# either TOML quote style: an in-block comment like `"tests",  # was "old"`
# or a single-quoted entry (both legal TOML) previously either produced a
# phantom extra path or silently dropped a real one.

if [ "${#TESTPATHS[@]}" -eq 0 ]; then
  echo "Could not parse testpaths out of pyproject.toml. Refusing to guess a split." >&2
  echo "RESULT validator STATE=FAIL NOTE=\"split could not be derived\""
  echo "RESULT miner-tool STATE=FAIL NOTE=\"split could not be derived\""
  echo "RESULT combined STATE=FAIL NOTE=\"could not parse testpaths from pyproject.toml\""
  exit 1
fi

# The standalone snapshot declares its deliberate omissions explicitly. The
# source checkout has no omissions; a missing or empty shipped suite is red.
CHECK_PYTHON="${MINER_TOOL_PYTHON_PATH:-${VALIDATOR_PYTHON_PATH:-python3}}"
if ! INCLUDED_TESTPATHS="$("$CHECK_PYTHON" "$REPO_ROOT/scripts/check_test_suites.py" "${TESTPATHS[@]}")"; then
  echo 'RESULT validator STATE=FAIL NOTE="test suite inventory failed"'
  echo 'RESULT miner-tool STATE=FAIL NOTE="test suite inventory failed"'
  echo 'RESULT combined STATE=FAIL NOTE="test suite inventory failed"'
  exit 1
fi
TESTPATHS=()
while IFS= read -r line; do TESTPATHS+=("$line"); done <<< "$INCLUDED_TESTPATHS"

FOUND_VALIDATOR_DIRS=()
REST_DIRS=()
for p in "${TESTPATHS[@]}"; do
  if is_validator_dir "$p"; then
    FOUND_VALIDATOR_DIRS+=("$p")
  else
    REST_DIRS+=("$p")
  fi
done

# If no validator directories are declared, that half is an explicit no-op.
if [ "${#FOUND_VALIDATOR_DIRS[@]}" -eq 0 ]; then
  echo "validator:   (none -- ${VALIDATOR_DIRS[*]} are not in this candidate's testpaths)"
else
  echo "validator:   ${FOUND_VALIDATOR_DIRS[*]}"
fi
# Bash 3.2 with set -u cannot safely expand an empty array; check it first.
if [ "${#REST_DIRS[@]}" -eq 0 ]; then
  echo "miner-tool:  (none -- pyproject.toml testpaths has nothing besides ${VALIDATOR_DIRS[*]})" >&2
  echo "RESULT validator STATE=FAIL NOTE=\"not attempted -- miner-tool split is empty\""
  echo "RESULT miner-tool STATE=FAIL NOTE=\"testpaths has no directories besides ${VALIDATOR_DIRS[*]} -- nothing to run\""
  echo "RESULT combined STATE=FAIL NOTE=\"miner-tool split is empty\""
  echo "RESOLVED validator="
  echo "RESOLVED miner-tool="
  exit 1
fi
echo "miner-tool:  ${REST_DIRS[*]}"

# ------------------------------------------------------------- run a half ---

VALIDATOR_STATE="FAIL"
VALIDATOR_NOTE=""
MINER_TOOL_STATE="FAIL"
MINER_TOOL_NOTE=""
VALIDATOR_PASSED=0
MINER_TOOL_PASSED=0
VALIDATOR_FAILED_COUNT=0
MINER_TOOL_FAILED_COUNT=0

run_half() {
  local label="$1" prefix="$2" py="$3"
  shift 3
  local -a dirs=("$@")
  local log="$LOG_DIR/${label}.log"

  banner "Python tests: $label ($*)"
  if "$py" -m pytest -q "${dirs[@]}" 2>&1 | tee "$log"; then
    local exit_code=0
  else
    local exit_code=$?
  fi

  # Read log content even when captured output contains NUL bytes. Otherwise
  # grep may print a binary-file diagnostic whose path is mistaken for counts.
  local passed failed skipped subtests
  passed="$(grep -aEo '[0-9]+ passed' "$log" | tail -1 | grep -Eo '[0-9]+' || true)"
  failed="$(grep -aEo '[0-9]+ failed' "$log" | tail -1 | grep -Eo '[0-9]+' || true)"
  skipped="$(grep -aEo '[0-9]+ skipped' "$log" | tail -1 | grep -Eo '[0-9]+' || true)"
  subtests="$(grep -aEo '[0-9]+ subtests passed' "$log" | tail -1 | grep -Eo '[0-9]+' || true)"
  passed="${passed:-0}"
  failed="${failed:-0}"

  if [ "$exit_code" -eq 5 ] || { [ "$passed" -eq 0 ] && [ "$failed" -eq 0 ]; }; then
    echo "  $label: RED -- 0 tests actually ran (exit $exit_code). The directory may be" >&2
    echo "  missing, renamed, empty, or the interpreter lacks pytest/this project installed." >&2
    printf -v "${prefix}_STATE" 'FAIL'
    printf -v "${prefix}_NOTE" 'collected/ran 0 tests -- half did not run'
    return 1
  fi

  local note="$passed passed"
  [ "$failed" != 0 ] && note="$note, $failed failed"
  [ -n "$skipped" ] && note="$note, $skipped skipped"
  [ -n "$subtests" ] && note="$note, $subtests subtests passed"

  printf -v "${prefix}_PASSED" '%s' "$passed"
  printf -v "${prefix}_FAILED_COUNT" '%s' "$failed"

  if [ "$exit_code" -eq 0 ]; then
    printf -v "${prefix}_STATE" 'PASS'
    printf -v "${prefix}_NOTE" '%s' "$note"
    echo "  $label: PASS -- $note"
    return 0
  else
    printf -v "${prefix}_STATE" 'FAIL'
    printf -v "${prefix}_NOTE" '%s' "$note (exit $exit_code)"
    echo "  $label: FAIL -- $note (exit $exit_code)" >&2
    return 1
  fi
}

# Report interpreter failures before considering whether its suite is empty.
if [ "$VALIDATOR_OK" != 1 ]; then
  VALIDATOR_STATE="FAIL"
  VALIDATOR_NOTE="did not run -- $VALIDATOR_FAIL_REASON"
  banner "Python tests: validator (${VALIDATOR_DIRS[*]})"
  echo "  SKIPPED -- $VALIDATOR_FAIL_REASON" >&2
elif [ "${#FOUND_VALIDATOR_DIRS[@]}" -eq 0 ]; then
  # Nothing to run on the validator side yet for this candidate -- an
  # explicit, named no-op PASS, not a silently-skipped check and not a red
  # result for directories this candidate never claimed to adopt. Reached
  # only once the interpreter itself is already known to be genuinely fine.
  VALIDATOR_STATE="PASS"
  VALIDATOR_NOTE="not attempted -- none of ${VALIDATOR_DIRS[*]} are part of this candidate"
  banner "Python tests: validator (${VALIDATOR_DIRS[*]})"
  echo "  SKIPPED (by design) -- none of ${VALIDATOR_DIRS[*]} are part of this candidate's testpaths"
else
  run_half "validator" "VALIDATOR" "$VALIDATOR_PYTHON_PATH" "${FOUND_VALIDATOR_DIRS[@]}" || true
fi

if [ "$MINER_TOOL_OK" = 1 ]; then
  run_half "miner-tool" "MINER_TOOL" "$MINER_TOOL_PYTHON_PATH" "${REST_DIRS[@]}" || true
else
  MINER_TOOL_STATE="FAIL"
  MINER_TOOL_NOTE="did not run -- $MINER_TOOL_FAIL_REASON"
  banner "Python tests: miner-tool (${REST_DIRS[*]})"
  echo "  SKIPPED -- $MINER_TOOL_FAIL_REASON" >&2
fi

# ------------------------------------------------------------------ total ---

banner "Combined"

if [ "$VALIDATOR_STATE" = "PASS" ] && [ "$MINER_TOOL_STATE" = "PASS" ]; then
  TOTAL_PASSED=$((VALIDATOR_PASSED + MINER_TOOL_PASSED))
  TOTAL_FAILED=$((VALIDATOR_FAILED_COUNT + MINER_TOOL_FAILED_COUNT))
  if [ "$TOTAL_FAILED" -eq 0 ]; then
    COMBINED_STATE="PASS"
    COMBINED_NOTE="$TOTAL_PASSED passed, $TOTAL_FAILED failed"
    echo "$COMBINED_NOTE"
  else
    COMBINED_STATE="FAIL"
    COMBINED_NOTE="$TOTAL_PASSED passed, $TOTAL_FAILED failed"
    echo "$COMBINED_NOTE" >&2
  fi
else
  COMBINED_STATE="FAIL"
  MISSING=""
  [ "$VALIDATOR_STATE" != "PASS" ] && MISSING="validator half"
  [ "$MINER_TOOL_STATE" != "PASS" ] && MISSING="${MISSING:+$MISSING, }miner-tool half"
  COMBINED_NOTE="cannot total -- $MISSING did not run cleanly"
  echo "$COMBINED_NOTE" >&2
fi

echo
echo "RESULT validator STATE=$VALIDATOR_STATE NOTE=\"$VALIDATOR_NOTE\""
echo "RESULT miner-tool STATE=$MINER_TOOL_STATE NOTE=\"$MINER_TOOL_NOTE\""
echo "RESULT combined STATE=$COMBINED_STATE NOTE=\"$COMBINED_NOTE\""
# For callers (run-tests.sh) that need a bittensor-free interpreter for other
# steps -- the twin proof scores noah/scoring/, which has zero runtime deps,
# and using the miner-tool interpreter for that keeps the same property this
# whole script exists to enforce. Empty when unresolved OR when the property
# check rejected it: a candidate that failed verification (e.g. a miner-tool
# path that turned out to have bittensor importable) must never be handed to
# a caller as if it were safe to use, even though its path was resolved.
if [ "$VALIDATOR_OK" = 1 ]; then
  echo "RESOLVED validator=$VALIDATOR_PYTHON_PATH"
else
  echo "RESOLVED validator="
fi
if [ "$MINER_TOOL_OK" = 1 ]; then
  echo "RESOLVED miner-tool=$MINER_TOOL_PYTHON_PATH"
else
  echo "RESOLVED miner-tool="
fi

[ "$COMBINED_STATE" = "PASS" ]
