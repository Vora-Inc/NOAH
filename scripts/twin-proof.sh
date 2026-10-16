#!/usr/bin/env bash
# Score each checksum-verified shared fixture with Python and TypeScript,
# then require byte-identical outputs. Refusal fixtures require both engines
# to fail with the same stable error code; free-text messages may differ.
# PYTHON selects the interpreter (default .venv/bin/python, then python3).
# OUT_DIR selects the output parent (default a fresh directory under TMPDIR).
# Its resolved path must be under the checkout or system scratch directory
# before existing python/ and typescript/ output directories are replaced.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FIXTURE_DIR="$REPO_ROOT/fixtures/shared"
REFUSED_DIR="$FIXTURE_DIR/refused"
TS_DIR="$REPO_ROOT/verification/noah-ts"
# The checked runner supplies writable TMPDIR; the checkout stays read-only.
# A fresh default also preserves earlier proof instead of deleting its output.
if [ -z "${OUT_DIR:-}" ]; then
  OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/noah-twin-proof.XXXXXX")"
fi
PY_OUT="$OUT_DIR/python"
TS_OUT="$OUT_DIR/typescript"

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="python3"
fi

# Require the installed TypeScript dependency before running tests or type checks.
if [ ! -d "$TS_DIR/node_modules/typescript" ]; then
  echo "FAIL: $TS_DIR/node_modules/typescript is not present." >&2
  echo "      node_modules is missing or was never fully installed. The TypeScript" >&2
  echo "      implementation cannot be trusted to have run without it" >&2
  echo "      (its own test files may execute without npm packages, which is exactly" >&2
  echo "      how this went unnoticed before). Run ./scripts/install.sh, or" >&2
  echo "      (cd verification/noah-ts && npm ci), then try again." >&2
  exit 1
fi

# ------------------------------------------------- step 1: the fixture set ---

if command -v sha256sum >/dev/null 2>&1; then
  CHECK_CMD=(sha256sum -c --quiet MANIFEST.sha256)
elif command -v shasum >/dev/null 2>&1; then
  CHECK_CMD=(shasum -a 256 -c MANIFEST.sha256)
else
  echo "ERROR: neither sha256sum nor shasum is available; cannot verify fixtures." >&2
  exit 1
fi

echo "== fixture checksums =="
if ! CHECK_OUTPUT="$( cd "$FIXTURE_DIR" && "${CHECK_CMD[@]}" 2>&1 )"; then
  echo "FAIL: fixtures/shared does not match MANIFEST.sha256." >&2
  echo "      A fixture was edited or replaced. Nothing below this line is trustworthy." >&2
  echo "$CHECK_OUTPUT" >&2
  exit 1
fi

FIXTURES=()
while IFS= read -r line; do FIXTURES+=("$line"); done < <(find "$FIXTURE_DIR" -maxdepth 1 -name '*.json' | sort)
TOTAL="${#FIXTURES[@]}"

REFUSED_FIXTURES=()
while IFS= read -r line; do REFUSED_FIXTURES+=("$line"); done < <(find "$REFUSED_DIR" -maxdepth 1 -name '*.json' 2>/dev/null | sort)
REFUSED_TOTAL="${#REFUSED_FIXTURES[@]}"

MANIFEST_LINES="$(grep -c . "$FIXTURE_DIR/MANIFEST.sha256")"
EXPECTED_LINES=$((TOTAL + REFUSED_TOTAL))

if [ "$EXPECTED_LINES" -ne "$MANIFEST_LINES" ]; then
  echo "FAIL: $TOTAL scored fixture(s) + $REFUSED_TOTAL refusal fixture(s) =" \
    "$EXPECTED_LINES file(s) on disk, but $MANIFEST_LINES lines in MANIFEST.sha256." >&2
  echo "      A fixture was added or removed without updating the manifest." >&2
  exit 1
fi
if [ "$TOTAL" -eq 0 ]; then
  echo "FAIL: no scored fixtures found in $FIXTURE_DIR." >&2
  exit 1
fi
echo "ok: $EXPECTED_LINES fixtures ($TOTAL scored, $REFUSED_TOTAL refusal) match their recorded checksums"
echo

# ------------------------------------------------ step 2: score both ways ----

# Resolve symlinks and require output containment before replacing output trees.
mkdir -p "$OUT_DIR"
RESOLVED_OUT_DIR="$(cd -P "$OUT_DIR" && pwd)"
CONTAINED=0
for SAFE_ROOT in "$REPO_ROOT" "${TMPDIR:-/tmp}"; do
  RESOLVED_SAFE_ROOT="$(cd -P "$SAFE_ROOT" 2>/dev/null && pwd || true)"
  [ -n "$RESOLVED_SAFE_ROOT" ] || continue
  case "$RESOLVED_OUT_DIR/" in
    "$RESOLVED_SAFE_ROOT"/*) CONTAINED=1 ;;
  esac
done
if [ "$CONTAINED" -ne 1 ]; then
  echo "FAIL: OUT_DIR ($RESOLVED_OUT_DIR) is not under this repo ($REPO_ROOT) or the" >&2
  echo "      system scratch directory (${TMPDIR:-/tmp}). Refusing to recursively delete" >&2
  echo "      python/ and typescript/ under an uncontained, arbitrary path." >&2
  exit 1
fi

rm -rf "$PY_OUT" "$TS_OUT"
mkdir -p "$PY_OUT" "$TS_OUT"
printf 'Proof output: %s\n' "$OUT_DIR"

echo "== scoring $TOTAL fixtures with the Python implementation =="
for fixture in "${FIXTURES[@]}"; do
  if ! "$PY" -m noah.scoring.run_fixture "$fixture" "$PY_OUT" >/dev/null; then
    echo "FAIL: the Python implementation could not score $(basename "$fixture")." >&2
    exit 1
  fi
done
echo "ok"
echo

echo "== scoring $TOTAL fixtures with the TypeScript implementation =="
for fixture in "${FIXTURES[@]}"; do
  if ! ( cd "$TS_DIR" && node src/cli.ts run-fixture "$fixture" "$TS_OUT" >/dev/null ); then
    echo "FAIL: the TypeScript implementation could not score $(basename "$fixture")." >&2
    exit 1
  fi
done
echo "ok"
echo

# ----------------------------------------------------- step 3: compare -------

PY_COUNT="$(find "$PY_OUT" -name '*.score.json' | wc -l | tr -d ' ')"
TS_COUNT="$(find "$TS_OUT" -name '*.score.json' | wc -l | tr -d ' ')"

echo "== byte-for-byte comparison =="
if ! diff -ru "$PY_OUT" "$TS_OUT"; then
  echo >&2
  echo "FAIL: the two implementations disagree. The differing bytes are shown above." >&2
  echo "      Left side is Python ($PY_OUT), right side is TypeScript ($TS_OUT)." >&2
  echo "      Weight setting must not proceed while this is red." >&2
  exit 1
fi

if [ "$PY_COUNT" -ne "$TOTAL" ] || [ "$TS_COUNT" -ne "$TOTAL" ]; then
  echo "FAIL: expected $TOTAL outputs per side, got $PY_COUNT (Python) and $TS_COUNT (TypeScript)." >&2
  exit 1
fi

echo "$TOTAL of $TOTAL fixtures byte-identical"
echo

# --------------------------------------- step 4: refusal fixtures agree ------
#
# Each fixture under fixtures/shared/refused/ must be refused by BOTH
# implementations, with the SAME stable error code. Extracted from each
# engine's own "refused[CODE]: ..." stderr line (see run_fixture.py's main()
# and cli.ts's own catch block) -- never from comparing the free-text
# message that follows it, which the two independently-written engines are
# not expected to phrase alike.

if [ "$REFUSED_TOTAL" -gt 0 ]; then
  echo "== refusal fixtures: both implementations must refuse identically =="
  CODE_RE='refused\[([A-Za-z0-9_]+)\]'
  for fixture in "${REFUSED_FIXTURES[@]}"; do
    name="$(basename "$fixture" .json)"

    PY_ERR="$("$PY" -m noah.scoring.run_fixture "$fixture" "$PY_OUT" 2>&1 >/dev/null)" \
      && PY_STATUS=0 || PY_STATUS=$?
    TS_ERR="$( (cd "$TS_DIR" && node src/cli.ts run-fixture "$fixture" "$TS_OUT" 2>&1 >/dev/null) )" \
      && TS_STATUS=0 || TS_STATUS=$?

    if [ "$PY_STATUS" -eq 0 ] || [ "$TS_STATUS" -eq 0 ]; then
      echo "FAIL: $name was expected to be refused by both implementations." >&2
      [ "$PY_STATUS" -eq 0 ] && echo "      Python SCORED it instead of refusing." >&2
      [ "$TS_STATUS" -eq 0 ] && echo "      TypeScript SCORED it instead of refusing." >&2
      exit 1
    fi

    PY_CODE=""
    TS_CODE=""
    [[ "$PY_ERR" =~ $CODE_RE ]] && PY_CODE="${BASH_REMATCH[1]}"
    [[ "$TS_ERR" =~ $CODE_RE ]] && TS_CODE="${BASH_REMATCH[1]}"

    if [ -z "$PY_CODE" ] || [ -z "$TS_CODE" ] || [ "$PY_CODE" != "$TS_CODE" ]; then
      echo "FAIL: $name: refusal codes do not agree." >&2
      echo "      python:     ${PY_CODE:-<no code>}  ($PY_ERR)" >&2
      echo "      typescript: ${TS_CODE:-<no code>}  ($TS_ERR)" >&2
      exit 1
    fi
    echo "ok: $name refused by both implementations, code $PY_CODE"
  done
  echo
fi
