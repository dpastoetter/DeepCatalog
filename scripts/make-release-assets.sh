#!/usr/bin/env bash
# Build verified release assets for GitHub Releases / the in-app updater:
#   deepcatalog-<version>.tar.gz
#   SHA256SUMS
#
# Always packs the *exact git commit* for the given tag (not a dirty working tree).
# Usage (from a clean checkout that contains the tag):
#   ./scripts/make-release-assets.sh v0.2.0
#
# Optional: pack an untagged commit while naming the archive for a future tag:
#   ./scripts/make-release-assets.sh v0.2.0 HEAD
#   ./scripts/make-release-assets.sh v0.2.0 abcdef012345

set -euo pipefail

TAG="${1:-}"
REF_ARG="${2:-}"
if [ -z "$TAG" ]; then
  echo "usage: $0 <tag> [git-ref]   e.g. $0 v0.2.0" >&2
  exit 1
fi

VERSION="${TAG#v}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
ARCHIVE_NAME="deepcatalog-${VERSION}.tar.gz"
STAGE="$DIST/stage"

if ! command -v git >/dev/null; then
  echo "git is required to build a clean release archive" >&2
  exit 1
fi
if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "git checkout required to build a clean release archive" >&2
  exit 1
fi

# Prefer the annotated/lightweight tag commit; fall back to an explicit ref.
if [ -n "$REF_ARG" ]; then
  REF="$REF_ARG"
elif git -C "$ROOT" rev-parse -q --verify "refs/tags/${TAG}^{commit}" >/dev/null; then
  REF="refs/tags/${TAG}"
elif git -C "$ROOT" rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  REF="refs/tags/${TAG}"
else
  echo "tag ${TAG} not found locally — pass an explicit ref, e.g. $0 ${TAG} HEAD" >&2
  echo "Create and push the tag from the commit that should ship before building CI assets." >&2
  exit 1
fi

COMMIT="$(git -C "$ROOT" rev-parse "${REF}^{commit}")"
COMMIT_SHORT="$(git -C "$ROOT" rev-parse --short=12 "$COMMIT")"
SUBJECT="$(git -C "$ROOT" log -1 --format='%s' "$COMMIT")"

rm -rf "$DIST"
mkdir -p "$STAGE"

# Pack tracked files for that commit only (no .git, .venv, data, local .env).
PREFIX="deepcatalog-${VERSION}"
mkdir -p "$STAGE/$PREFIX"
git -C "$ROOT" archive --format=tar "$COMMIT" | tar -x -C "$STAGE/$PREFIX"

# Never ship local secrets or runtime data even if somehow tracked.
rm -rf "$STAGE/$PREFIX/data" "$STAGE/$PREFIX/.venv" "$STAGE/$PREFIX/venv"
rm -f "$STAGE/$PREFIX/.env"

EXPECTED_LIST="$DIST/expected-files.txt"
ARCHIVED_LIST="$DIST/archived-files.txt"

git -C "$ROOT" ls-tree -r --name-only "$COMMIT" \
  | awk '$0 != ".env" && $0 !~ /^data(\/|$)/ && $0 !~ /^\.venv(\/|$)/ && $0 !~ /^venv(\/|$)/ { print }' \
  | sort > "$EXPECTED_LIST"

(
  cd "$STAGE/$PREFIX"
  find . -type f | sed 's|^\./||' | sort
) > "$ARCHIVED_LIST"

if ! cmp -s "$EXPECTED_LIST" "$ARCHIVED_LIST"; then
  echo "release archive file list does not match commit $COMMIT_SHORT" >&2
  echo "--- only in git commit ---" >&2
  comm -23 "$EXPECTED_LIST" "$ARCHIVED_LIST" >&2 || true
  echo "--- only in archive ---" >&2
  comm -13 "$EXPECTED_LIST" "$ARCHIVED_LIST" >&2 || true
  exit 1
fi

EXPECTED_COUNT="$(wc -l < "$EXPECTED_LIST" | tr -d ' ')"

# Full file manifest so installers/updaters can prune stale paths on upgrade.
cp "$EXPECTED_LIST" "$STAGE/$PREFIX/.release-files"

# Align archived pyproject version with the release tag.
if [ -f "$STAGE/$PREFIX/pyproject.toml" ]; then
  python3 - "$STAGE/$PREFIX/pyproject.toml" "$VERSION" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
version = sys.argv[2]
text = path.read_text(encoding="utf-8")
text, n = re.subn(
    r'^version\s*=\s*"[^"]*"',
    f'version = "{version}"',
    text,
    count=1,
    flags=re.MULTILINE,
)
if n != 1:
    raise SystemExit("could not rewrite version in pyproject.toml")
path.write_text(text, encoding="utf-8")
PY
fi

# Record exactly what was packed so installs/debugging can confirm completeness.
{
  echo "tag=${TAG}"
  echo "version=${VERSION}"
  echo "commit=${COMMIT}"
  echo "commit_short=${COMMIT_SHORT}"
  echo "subject=${SUBJECT}"
  echo "file_count=${EXPECTED_COUNT}"
} > "$STAGE/$PREFIX/.release-commit"

# Required paths that must always ship (guards against accidental export-ignore / sparse packs).
REQUIRED_PATHS=(
  "pyproject.toml"
  "constraints.txt"
  "requirements.txt"
  "README.md"
  ".env.example"
  "app/main.py"
  "app/static/app.js"
  "app/static/api.js"
  "app/static/state.js"
  "app/static/router.js"
  "app/static/inbox.js"
  "app/static/review.js"
  "app/static/documents.js"
  "app/static/ask.js"
  "app/static/events.js"
  "app/static/settings.js"
  "app/static/index.html"
  "deepcatalog/ingest.py"
  "deepcatalog/llm.py"
  "deepcatalog/ocr.py"
  "deepcatalog/system_service.py"
  "deepcatalog/updater.py"
  "deepcatalog/release_trust.py"
  "scripts/sign-release-manifest.py"
  "packaging/linux/AppRun"
  "packaging/linux/deepcatalog.desktop"
  "packaging/linux/deepcatalog.svg"
  "packaging/linux/deepcatalog.png"
  "packaging/linux/splash.html"
  "scripts/build-appimage.sh"
  "scripts/install.sh"
  "scripts/install.ps1"
  "scripts/lock-deps.sh"
  "scripts/make-release-assets.sh"
)
for path in "${REQUIRED_PATHS[@]}"; do
  if [ ! -f "$STAGE/$PREFIX/$path" ]; then
    echo "required path missing from release archive: $path" >&2
    exit 1
  fi
done

tar -czf "$DIST/$ARCHIVE_NAME" -C "$STAGE" "$PREFIX"

# Standalone bootstrap scripts for:
#   curl -fsSL …/releases/latest/download/install.sh | bash
#   irm …/releases/latest/download/install.ps1 | iex
# (avoids stale raw.githubusercontent.com/main CDN caches)
cp "$STAGE/$PREFIX/scripts/install.sh" "$DIST/install.sh"
chmod +x "$DIST/install.sh"
cp "$STAGE/$PREFIX/scripts/install.ps1" "$DIST/install.ps1"

(
  cd "$DIST"
  sha256sum "$ARCHIVE_NAME" install.sh install.ps1 > SHA256SUMS
)

echo "Release assets ready in $DIST:"
echo "  $ARCHIVE_NAME"
echo "  install.sh"
echo "  install.ps1"
echo "  SHA256SUMS"
echo "  commit ${COMMIT_SHORT} (${COMMIT})"
echo "  tracked files packed: ${EXPECTED_COUNT} (+ .release-commit + .release-files)"
cat "$DIST/SHA256SUMS"
echo
echo "Upload these files to the GitHub release for $TAG."
echo "CI signs dist/SHA256SUMS into release-manifest.json after this script (and AppImage checksums)."
