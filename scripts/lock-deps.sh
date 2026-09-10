#!/usr/bin/env bash
# Regenerate pinned constraints from pyproject.toml (source of truth).
#
#   ./scripts/lock-deps.sh
#
# Writes constraints.txt covering runtime + desktop + dev extras so installers
# and CI resolve the same graph. Do not hand-edit constraints.txt.

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

echo "Ensuring pip-tools is available…"
"$PY" -m pip install -q 'pip-tools>=7.4'

echo "Compiling constraints.txt from pyproject.toml…"
tmp="$(mktemp)"
"$PY" -m piptools compile \
  --quiet \
  --resolver=backtracking \
  --extra=desktop \
  --extra=dev \
  --strip-extras \
  --allow-unsafe \
  --no-emit-options \
  --output-file="$tmp" \
  pyproject.toml

# Drop the editable self-reference if pip-tools emits the local project name.
# Constraints files should only pin third-party distributions.
if grep -qE '^deepcatalog(==| @)' "$tmp" 2>/dev/null; then
  grep -vE '^deepcatalog(==| @)' "$tmp" > "${tmp}.stripped"
  mv "${tmp}.stripped" "$tmp"
fi
mv "$tmp" constraints.txt

echo "✓ Wrote constraints.txt"
echo "  Install runtime:  pip install -e . -c constraints.txt"
echo "  Install desktop:  pip install -e '.[desktop]' -c constraints.txt"
echo "  Install dev:      pip install -e '.[dev]' -c constraints.txt"
