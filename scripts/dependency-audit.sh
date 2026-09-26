#!/usr/bin/env bash
# Dependency vulnerability scan (pip-audit → OSV, plus npm audit).
#
#   ./scripts/dependency-audit.sh
#
# Used by CI/release workflows. Keep ignores documented and minimal.
# Chroma HTTP-server advisories are suppressed via
# scripts/chroma_vuln_suppressions.json (rationale + review expiry). That file
# is the source of truth; this script fails if a suppression has expired.

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

echo "[0/2] chroma vuln suppression expiry"
"$PY" scripts/check_chroma_suppressions.py \
  || fail "chroma vuln suppressions expired or out of sync (see scripts/chroma_vuln_suppressions.json)"

mapfile -t CHROMA_IGNORE_ARGS < <("$PY" scripts/check_chroma_suppressions.py --pip-audit-args)
if [ "${#CHROMA_IGNORE_ARGS[@]}" -eq 0 ]; then
  fail "no Chroma --ignore-vuln flags from check_chroma_suppressions.py"
fi

echo "[1/2] pip-audit (OSV)"
"$PY" -m pip install -q pip-audit
# Ignore IDs come from scripts/chroma_vuln_suppressions.json (HTTP-server CVEs;
# DeepCatalog uses embedded PersistentClient only — see deepcatalog/chroma_local.py).
# Weekly unsuppressed scan: scripts/chroma-advisory-watch.py.
if command -v pip-audit >/dev/null 2>&1; then
  AUDIT=(pip-audit)
else
  AUDIT=("$PY" -m pip_audit)
fi
"${AUDIT[@]}" --progress-spinner off \
  "${CHROMA_IGNORE_ARGS[@]}" \
  || fail "pip-audit found vulnerabilities"

echo "[2/2] npm audit"
if command -v npm >/dev/null 2>&1; then
  if [ ! -f package-lock.json ]; then
    fail "package-lock.json missing"
  fi
  npm audit --audit-level=high || fail "npm audit found high/critical vulnerabilities"
else
  echo "  npm not found — skipping"
fi

echo "✓ Dependency audit passed"
