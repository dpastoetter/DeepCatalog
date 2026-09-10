#!/usr/bin/env bash
# Dependency vulnerability scan (pip-audit → OSV, plus npm audit).
#
#   ./scripts/dependency-audit.sh
#
# Used by CI/release workflows. Keep ignores documented and minimal.

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

echo "[1/2] pip-audit (OSV)"
"$PY" -m pip install -q pip-audit
# chromadb HTTP/FastAPI server advisories (CVE-2026-45829 / 45830 / 45832 / 45833
# and related PYSEC IDs). DeepCatalog only constructs embedded PersistentClient
# (see deepcatalog/chroma_local.py) and never starts Chroma's HTTP listener.
# These ignores are for PR/release CI only — the weekly unsuppressed scan is
# scripts/chroma-advisory-watch.py (.github/workflows/advisory-watch.yml).
# Drop them when a release newer than 1.5.9 lands on PyPI.
if command -v pip-audit >/dev/null 2>&1; then
  AUDIT=(pip-audit)
else
  AUDIT=("$PY" -m pip_audit)
fi
"${AUDIT[@]}" --progress-spinner off \
  --ignore-vuln PYSEC-2026-311 \
  --ignore-vuln GHSA-f4j7-r4q5-qw2c \
  --ignore-vuln PYSEC-2026-3813 \
  --ignore-vuln PYSEC-2026-3814 \
  --ignore-vuln PYSEC-2026-3815 \
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
