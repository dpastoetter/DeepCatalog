#!/usr/bin/env bash
# Reproducible quality gate used by GitHub Actions and locally.
#
#   ./scripts/ci.sh
#
# Steps: ruff format → ruff lint → media-parser boundary → pip check → mypy →
# JS syntax → Vitest → pytest → security-module coverage floors

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

fail() {
  echo "✗ $1" >&2
  exit 1
}

if command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi

echo "[1/7] Ruff format"
"$PY" -m ruff format --check deepcatalog app query_agent tests \
  || fail "Formatting drift — run: $PY -m ruff format deepcatalog app query_agent tests"

echo "[2/7] Ruff lint"
"$PY" -m ruff check deepcatalog app query_agent tests \
  || fail "Ruff lint failed"

echo "[2b] Media parser boundary"
# Only deepcatalog/media_worker.py may import pypdf / pdf2image / PIL for scans.
if command -v rg >/dev/null 2>&1; then
  if rg -n --glob '!deepcatalog/media_worker.py' --glob '!tests/**' \
      -e '^\s*(from\s+pypdf|import\s+pypdf|from\s+pdf2image|import\s+pdf2image|from\s+PIL|import\s+PIL)\b' \
      deepcatalog app query_agent; then
    fail "untrusted PDF/image parser import outside deepcatalog/media_worker.py"
  fi
else
  hits="$("$PY" - <<'PY'
from pathlib import Path
import re
pat = re.compile(r"^\s*(from\s+pypdf|import\s+pypdf|from\s+pdf2image|import\s+pdf2image|from\s+PIL|import\s+PIL)\b")
roots = [Path("deepcatalog"), Path("app"), Path("query_agent")]
bad = []
for root in roots:
    if not root.is_dir():
        continue
    for path in root.rglob("*.py"):
        if path.name == "media_worker.py" and path.parent.name == "deepcatalog":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{path}:{i}:{line}")
print("\n".join(bad))
PY
)"
  if [ -n "$hits" ]; then
    echo "$hits" >&2
    fail "untrusted PDF/image parser import outside deepcatalog/media_worker.py"
  fi
fi

echo "[3/7] pip check"
"$PY" -m pip check || fail "pip check reported broken dependencies"

echo "[4/7] mypy"
"$PY" -m mypy || fail "mypy failed"

echo "[5/7] JavaScript syntax"
if command -v node >/dev/null 2>&1; then
  for js in app/static/*.js; do
    node --check "$js" || fail "JS syntax error in $js"
  done
else
  echo "  node not found — skipping JS check"
fi

echo "[6/7] Frontend unit tests (Vitest)"
if command -v npm >/dev/null 2>&1; then
  if [ ! -d node_modules/vitest ]; then
    if [ -f package-lock.json ]; then
      npm ci --no-fund --no-audit || fail "npm ci failed"
    else
      npm install --no-fund --no-audit || fail "npm install failed"
    fi
  fi
  npm test || fail "Frontend unit tests failed"
else
  echo "  npm not found — skipping Vitest"
fi

echo "[7/8] pytest + coverage"
"$PY" -m pytest tests/ -q || fail "Tests failed (or coverage below floor)"

echo "[8/8] Security-sensitive coverage floors"
chmod +x scripts/coverage-security.sh
./scripts/coverage-security.sh || fail "Security module coverage floors failed"

echo "✓ CI quality gate passed"
