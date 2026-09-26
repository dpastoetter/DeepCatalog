#!/usr/bin/env bash
# DeepCatalog one-shot installer.
#
#   curl -fsSL https://github.com/dpastoetter/DeepCatalog/releases/latest/download/install.sh | bash
#
# By default this installs the latest *GitHub Release* tarball (same verified
# artifact the in-app updater uses) — not a floating git checkout — so another
# machine matches the published release.
#
# Optional env vars:
#   DEEPCATALOG_DIR              install location (default: ~/deepcatalog)
#   DEEPCATALOG_PORT             port printed in the run hint (default: 8080)
#   DEEPCATALOG_INSTALL_SOURCE   release (default) | git
#   DEEPCATALOG_UPDATE_REPO        owner/repo (default: dpastoetter/DeepCatalog)

set -euo pipefail

REPO="${DEEPCATALOG_UPDATE_REPO:-dpastoetter/DeepCatalog}"
REPO_SSH="git@github.com:${REPO}.git"
REPO_HTTPS="https://github.com/${REPO}.git"
INSTALL_DIR="${DEEPCATALOG_DIR:-$HOME/deepcatalog}"
PORT="${DEEPCATALOG_PORT:-8080}"
SOURCE="${DEEPCATALOG_INSTALL_SOURCE:-release}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die() { printf '  ✗ %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null || die "$1 is required"
}

sha256_file() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "sha256sum or shasum is required to verify the release"
  fi
}

bold "DeepCatalog installer"
echo "  → $INSTALL_DIR"
echo "  source: $SOURCE"
echo

# --- prerequisites -----------------------------------------------------------
need_cmd python3
need_cmd curl
need_cmd tar
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10+ required (found $PY_VER)"
ok "Python $PY_VER"

if command -v pdftoppm >/dev/null; then
  ok "poppler (pdftoppm) — PDF OCR ready"
else
  warn "poppler not found — AI OCR for PDFs needs it"
  warn "  Fedora/RHEL:  sudo dnf install poppler-utils"
  warn "  Debian/Ubuntu: sudo apt install poppler-utils"
  warn "  macOS:         brew install poppler"
fi

if command -v tesseract >/dev/null; then
  ok "tesseract — classical OCR fast path ready"
else
  warn "tesseract not found — scanned pages fall back to slower AI vision OCR"
  warn "  Fedora/RHEL:  sudo dnf install tesseract tesseract-langpack-eng"
  warn "  Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-eng"
  warn "  macOS:         brew install tesseract"
fi

install_from_release() {
  need_cmd python3
  local api="https://api.github.com/repos/${REPO}/releases/latest"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/deepcatalog-install.XXXXXX")"

  bold "Fetching latest GitHub release"
  if ! curl -fsSL -H "Accept: application/vnd.github+json" -H "User-Agent: DeepCatalog-installer" \
      "$api" >"$tmp/release.json"; then
    rm -rf "$tmp"
    die "Could not fetch $api — check network or set DEEPCATALOG_INSTALL_SOURCE=git"
  fi

  python3 - "$tmp/release.json" "$tmp" <<'PY'
import json, pathlib, sys
release = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
out = pathlib.Path(sys.argv[2])
assets = release.get("assets") or []
archive = None
sums = None
for asset in assets:
    name = asset.get("name") or ""
    url = asset.get("browser_download_url") or ""
    if not url:
        continue
    lower = name.lower()
    if name in {"SHA256SUMS", "SHA256SUMS.txt", "checksums.txt"}:
        sums = (name, url, (asset.get("digest") or ""))
    elif lower.startswith("deepcatalog-") and lower.endswith((".tar.gz", ".tgz")):
        archive = (name, url, (asset.get("digest") or ""))
if archive is None:
    raise SystemExit("latest release has no deepcatalog-*.tar.gz asset")
meta = {
    "TAG": release.get("tag_name") or "",
    "ARCHIVE_NAME": archive[0],
    "ARCHIVE_URL": archive[1],
    "ARCHIVE_DIGEST": archive[2],
    "SUMS_NAME": sums[0] if sums else "",
    "SUMS_URL": sums[1] if sums else "",
}
(out / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
print(meta["TAG"])
PY

  TAG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["TAG"])' "$tmp/meta.json")"
  ARCHIVE_NAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["ARCHIVE_NAME"])' "$tmp/meta.json")"
  ARCHIVE_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["ARCHIVE_URL"])' "$tmp/meta.json")"
  ARCHIVE_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["ARCHIVE_DIGEST"])' "$tmp/meta.json")"
  SUMS_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["SUMS_URL"])' "$tmp/meta.json")"
  ok "release $TAG ($ARCHIVE_NAME)"

  curl -fsSL -H "User-Agent: DeepCatalog-installer" -o "$tmp/$ARCHIVE_NAME" "$ARCHIVE_URL"
  if [ -n "${SUMS_URL:-}" ]; then
    curl -fsSL -H "User-Agent: DeepCatalog-installer" -o "$tmp/SHA256SUMS" "$SUMS_URL"
  fi

  local actual expected=""
  actual="$(sha256_file "$tmp/$ARCHIVE_NAME")"
  if [ -f "$tmp/SHA256SUMS" ]; then
    expected="$(awk -v name="$ARCHIVE_NAME" '$2 == name || $2 == "*"name {print $1; exit}' "$tmp/SHA256SUMS")"
  fi
  if [ -z "$expected" ] && [[ "${ARCHIVE_DIGEST:-}" == sha256:* ]]; then
    expected="${ARCHIVE_DIGEST#sha256:}"
  fi
  if [ -z "$expected" ]; then
    die "No SHA-256 available for $ARCHIVE_NAME — refusing unverified install"
  fi
  actual_l="$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')"
  expected_l="$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')"
  if [ "$actual_l" != "$expected_l" ]; then
    die "SHA-256 mismatch for $ARCHIVE_NAME (expected $expected, got $actual)"
  fi
  ok "SHA-256 verified"

  mkdir -p "$tmp/extract"
  tar -xzf "$tmp/$ARCHIVE_NAME" -C "$tmp/extract"
  local src
  src="$(find "$tmp/extract" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "$src" ] || die "unexpected tarball layout"
  [ -f "$src/pyproject.toml" ] || die "release archive missing pyproject.toml"
  [ -f "$src/app/main.py" ] || die "release archive missing app/main.py"

  mkdir -p "$INSTALL_DIR"
  bold "Installing into $INSTALL_DIR"
  # Preserve user state; replace code from the verified archive (delete stale code).
  python3 - "$src" "$INSTALL_DIR" <<'PY'
import os
import shutil
import sys
from pathlib import Path

src = Path(sys.argv[1]).resolve()
dest = Path(sys.argv[2]).resolve()
protected = {"data", ".env", ".venv", "venv", ".git", "node_modules"}
dest.mkdir(parents=True, exist_ok=True)

new_files: set[str] = set()
for path in src.rglob("*"):
    if not path.is_file():
        continue
    rel = path.relative_to(src)
    if rel.parts and rel.parts[0] in protected:
        continue
    if rel.name == ".env":
        continue
    new_files.add(rel.as_posix())
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)

manifest = src / ".release-files"
old_manifest = dest / ".release-files"
previous: set[str] = set()
if old_manifest.is_file():
    previous = {
        line.strip()
        for line in old_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
elif manifest.is_file():
    # First release-style install over a legacy tree: only prune paths we know
    # about after writing the new manifest below.
    previous = set()

obsolete = previous - new_files
for rel in sorted(obsolete):
    parts = Path(rel).parts
    if not parts or parts[0] in protected or Path(rel).name == ".env":
        continue
    target = dest / rel
    if target.is_file() and not target.is_symlink():
        target.unlink()
        parent = target.parent
        while parent != dest and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

if manifest.is_file():
    shutil.copy2(manifest, dest / ".release-files")
print(f"synced {len(new_files)} files; removed {len(obsolete)} stale paths")
PY
  ok "code synced from release archive"

  if [ -f "$INSTALL_DIR/.release-commit" ]; then
    ok "installed $(tr '\n' ' ' <"$INSTALL_DIR/.release-commit" | sed 's/ $//')"
  else
    ok "installed $TAG"
  fi
  rm -rf "$tmp"
}

install_from_git() {
  need_cmd git
  ok "git"
  if [ -d "$INSTALL_DIR/.git" ]; then
    bold "Updating existing git install"
    git -C "$INSTALL_DIR" fetch --tags --force origin
    local branch
    branch="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)"
    if [ "$branch" = "HEAD" ]; then
      git -C "$INSTALL_DIR" checkout -B main origin/main
    fi
    git -C "$INSTALL_DIR" reset --hard origin/main
    ok "reset to $(git -C "$INSTALL_DIR" rev-parse --short HEAD) on main"
  else
    if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR/.git" ]; then
      die "$INSTALL_DIR exists but is not a git checkout — move it aside or set DEEPCATALOG_DIR"
    fi
    bold "Cloning repository (main)"
    if git clone --depth 1 "$REPO_HTTPS" "$INSTALL_DIR" 2>/dev/null; then
      ok "cloned via HTTPS"
    else
      git clone --depth 1 "$REPO_SSH" "$INSTALL_DIR" || die "git clone failed"
      ok "cloned via SSH"
    fi
  fi
}

case "$SOURCE" in
  release|RELEASE)
    install_from_release
    ;;
  git|GIT|main|MAIN)
    install_from_git
    ;;
  *)
    die "DEEPCATALOG_INSTALL_SOURCE must be 'release' or 'git' (got $SOURCE)"
    ;;
esac

cd "$INSTALL_DIR"

# --- virtualenv + deps -------------------------------------------------------
venv_usable() {
  [ -x .venv/bin/python ] && [ -f .venv/bin/activate ]
}

create_venv() {
  if ! python3 -m venv .venv; then
    rm -rf .venv
    die "python3 -m venv failed — install the venv package, then re-run:
    Debian/Ubuntu:  sudo apt install python3-venv
    Fedora/RHEL:    sudo dnf install python3
    macOS:          brew install python"
  fi
  venv_usable || {
    rm -rf .venv
    die "venv was created but .venv/bin/activate is missing — re-run after:
    Debian/Ubuntu:  sudo apt install python3-venv"
  }
}

bold "Creating virtualenv"
if venv_usable; then
  ok "reusing existing .venv"
else
  if [ -e .venv ]; then
    warn "existing .venv is incomplete — recreating"
    rm -rf .venv
  fi
  create_venv
  ok "created .venv"
fi

VENV_PY="$INSTALL_DIR/.venv/bin/python"
ok "venv at $INSTALL_DIR/.venv"

bold "Installing Python packages"
"$VENV_PY" -m pip install -U pip >/dev/null
# requirements.txt → editable install constrained by constraints.txt
"$VENV_PY" -m pip install -r requirements.txt
ok "dependencies installed"

# --- config ------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  ok "created .env from .env.example (mode 600)"
else
  chmod 600 .env 2>/dev/null || true
  ok ".env already present — left untouched (permissions tightened to 600)"
fi
mkdir -p data/inbox data/archive data/chroma
ok "data directories ready"

if command -v ollama >/dev/null; then
  ok "ollama CLI found — use Settings → Local Ollama for fully local models"
else
  warn "ollama not found — optional for fully local AI (https://ollama.com/download)"
fi

# --- done --------------------------------------------------------------------
echo
bold "Install complete"
if [ -f .release-commit ]; then
  echo
  echo "  Release manifest:"
  sed 's/^/    /' .release-commit
fi
cat <<EOF

  Start the app (activate .venv first — do not use a system uvicorn):

    cd $INSTALL_DIR
    source .venv/bin/activate
    uvicorn app.main:app --port $PORT

  Then open http://localhost:$PORT

  If activate fails or you see ModuleNotFoundError: fastapi, re-run this installer.

  First-run tips:
    • Settings → AI provider — Sign in with ChatGPT, or click Local Ollama
      (Ollama: install from https://ollama.com/download, then Pull required models)
    • Settings → Filing & scanning — point the inbox at your scan folder
    • Drop a PDF in Inbox and click Process inbox
    • Boot autostart (systemd) is Linux-only — unavailable on macOS

  Re-run this installer anytime to install the latest verified release:
    curl -fsSL https://github.com/${REPO}/releases/latest/download/install.sh | bash

  On Windows, use install.ps1 instead:
    irm https://github.com/${REPO}/releases/latest/download/install.ps1 | iex

  Tip: set DEEPCATALOG_INSTALL_SOURCE=git to track the main branch instead of the release.

  Uninstall:
    rm -rf "$INSTALL_DIR"
  That does not delete ~/.codex/auth.json or archive folders outside the install dir.

EOF
