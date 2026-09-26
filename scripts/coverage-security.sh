#!/usr/bin/env bash
# Focused branch/line coverage floors for security-sensitive modules.
#
#   ./scripts/coverage-security.sh
#
# Run after pytest has written .coverage (scripts/ci.sh does this). Does not
# raise the global --cov-fail-under; it gates modules that handle auth, bind
# policy, updates, Ollama URL SSRF checks, media isolation, and inbox confinement.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi

fail() {
  echo "✗ $1" >&2
  exit 1
}

[ -f .coverage ] || fail "No .coverage data — run pytest with coverage first"

# file:minimum combined Cover% (lines+branches) from `coverage report`
# Floors track current suite with a small cushion; raise as tests tighten.
FLOORS=(
  "deepcatalog/local_security.py:85"
  "deepcatalog/sessions.py:80"
  "deepcatalog/updater.py:70"
  "deepcatalog/ollama_url.py:80"
  "deepcatalog/media_validate.py:75"
  "deepcatalog/media_worker.py:70"
  "deepcatalog/tools/filesystem.py:60"
)

echo "[security-coverage] focused floors"
overall_fail=0
for entry in "${FLOORS[@]}"; do
  file="${entry%%:*}"
  min="${entry##*:}"
  if [ ! -f "$file" ]; then
    echo "  miss  $file (not found)" >&2
    overall_fail=1
    continue
  fi
  # Cover is column 6 when branch coverage is on (Name Stmts Miss Branch BrPart Cover Missing).
  # Do not use $NF — that is the Missing line list.
  if "$PY" -m coverage report --include="$file" --fail-under="$min" >/dev/null; then
    pct="$("$PY" -m coverage report --include="$file" | awk -v f="$file" '
      $1 == f { print $6; found=1 }
      END { if (!found) print "?" }
    ')"
    echo "  ok    $file ≥ ${min}% (Cover ${pct})"
  else
    echo "  FAIL  $file below ${min}%:" >&2
    "$PY" -m coverage report --include="$file" --show-missing >&2 || true
    overall_fail=1
  fi
done

[ "$overall_fail" -eq 0 ] || fail "Security-sensitive coverage floors not met"
echo "✓ Security-sensitive coverage floors passed"
