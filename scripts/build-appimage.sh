#!/usr/bin/env bash
# Build a Linux x86_64 AppImage for DeepCatalog.
#
# Usage (from a git checkout):
#   ./scripts/build-appimage.sh
#   ./scripts/build-appimage.sh v0.2.9
#   ./scripts/build-appimage.sh v0.2.9 HEAD
#
#   DEEPCATALOG_APPIMAGE_WORKDIR=1 ./scripts/build-appimage.sh
#     Pack HEAD plus uncommitted tracked files (local test images).
#
# Requires: linux x86_64, git, curl, tar, python3, pdftoppm, pdfinfo, patchelf, ldd,
# gcc, pkg-config, meson, ninja, WebKitGTK 4.1 or 4.0, gobject-introspection, cairo headers
# (see CI apt list in .github/workflows/release.yml / appimage.yml).
# Downloads a pinned CPython and appimagetool (SHA-256 verified).
#
# Output:
#   dist/DeepCatalog-<version>-x86_64.AppImage
#   dist/DeepCatalog-<version>-x86_64.AppImage.sha256

set -euo pipefail

TAG="${1:-}"
REF_ARG="${2:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APPDIR="$DIST/AppDir"
CACHE="$DIST/cache"
ARCH="x86_64"

# python-build-standalone (astral-sh) — install_only_stripped CPython 3.12.
PYTHON_RELEASE="20260814"
PYTHON_VERSION="3.12.14"
PYTHON_TARBALL="cpython-${PYTHON_VERSION}+${PYTHON_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE}/${PYTHON_TARBALL}"
PYTHON_SHA256="5acfa3e9ba26b51ae161c83aff278da915b590d22373a424b2ba55b8afe91fcc"

# AppImage/appimagetool continuous build (pinned by digest).
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="a6d71e2b6cd66f8e8d16c37ad164658985e0cf5fcaa950c90a482890cb9d13e0"

die() {
  echo "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null || die "$1 is required to build the AppImage"
}

sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}

verify_sha256() {
  local path="$1"
  local expected="$2"
  local actual
  actual="$(sha256_file "$path")"
  if [ "$actual" != "$expected" ]; then
    die "SHA-256 mismatch for $(basename "$path") (expected ${expected}, got ${actual})"
  fi
}

download_verified() {
  local url="$1"
  local dest="$2"
  local sha="$3"
  if [ -f "$dest" ]; then
    if [ "$(sha256_file "$dest")" = "$sha" ]; then
      return 0
    fi
    rm -f "$dest"
  fi
  curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url"
  verify_sha256 "$dest" "$sha"
}

pyproject_version() {
  python3 - "$ROOT/pyproject.toml" <<'PY'
from pathlib import Path
import re
import sys
text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
if not match:
    raise SystemExit("could not read version from pyproject.toml")
print(match.group(1))
PY
}

rewrite_pyproject_version() {
  python3 - "$1" "$2" <<'PY'
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
}

vendor_binary() {
  local src="$1"
  local dest_dir="$2"
  local native_dir="$3"
  local name
  name="$(basename "$src")"
  install -m 0755 "$src" "$dest_dir/$name"
  vendor_deps "$dest_dir/$name" "$native_dir"
  if command -v patchelf >/dev/null; then
    patchelf --set-rpath "\$ORIGIN/../lib/deepcatalog-native" "$dest_dir/$name"
  fi
}

# Host graphics / libc — never vendor (NVIDIA/Mesa/X11 must come from the OS).
host_lib_excluded() {
  local base="$1"
  case "$base" in
    libc.so*|libm.so*|libpthread.so*|libdl.so*|librt.so*|libutil.so*|libresolv.so*|libnsl.so*|libcrypt.so*|ld-linux*|libgcc_s.so*|libstdc++.so*|libGL.so*|libOpenGL.so*|libGLdispatch.so*|libGLX.so*|libEGL.so*|libGLESv2.so*|libdrm.so*|libgbm.so*|libnvidia-*|libcuda.so*|libwayland-*|libX11.so*|libXext.so*|libXrender.so*|libXcursor.so*|libXrandr.so*|libXi.so*|libXfixes.so*|libXcomposite.so*|libXdamage.so*|libXinerama.so*|libXss.so*|libXtst.so*|libXxf86vm.so*|libXau.so*|libXdmcp.so*|libxcb.so*|libxcb-*)
      return 0
      ;;
  esac
  return 1
}

lib_excluded() {
  local base="$1"
  if host_lib_excluded "$base"; then
    return 0
  fi
  # Poppler tree stays GTK-free; WebKit/GTK go in deepcatalog-webkit instead.
  if [ "${VENDOR_MODE:-poppler}" = poppler ]; then
    case "$base" in
      libgtk-*|libwebkit*|libjavascriptcoregtk*|libgdk-*|libgdk_pixbuf*|libpango-*|libpangocairo*|libpangoft*|libsoup-*|libgio-2*|libgobject-2*|libglib-2*|libgmodule-2*|libgthread-2*|libgirepository*)
        return 0
        ;;
    esac
  fi
  return 1
}

vendor_deps() {
  local target="$1"
  local dest="$2"
  mkdir -p "$dest"
  local pending=("$target")
  local seen=""
  local item lib base
  while [ "${#pending[@]}" -gt 0 ]; do
    item="${pending[0]}"
    pending=("${pending[@]:1}")
    [ -f "$item" ] || continue
    while read -r lib; do
      [ -n "$lib" ] || continue
      [ -f "$lib" ] || continue
      base="$(basename "$lib")"
      if lib_excluded "$base"; then
        continue
      fi
      case " $seen " in
        *" $base "*) continue ;;
      esac
      seen+=" $base"
      # Do not clobber a previously vendored (and possibly relocated) copy.
      if [ -e "$dest/$base" ]; then
        continue
      fi
      cp -aL "$lib" "$dest/$base"
      pending+=("$dest/$base")
    done < <(ldd "$item" 2>/dev/null | awk '/=> \/|=> \.\// {print $3} /^\// {print $1}')
  done
  if command -v patchelf >/dev/null; then
    for lib in "$dest"/*; do
      [ -f "$lib" ] || continue
      patchelf --set-rpath "\$ORIGIN" "$lib" 2>/dev/null || true
    done
  fi
}

find_webkit_so() {
  local so candidate
  for candidate in \
    /usr/lib/x86_64-linux-gnu/libwebkit2gtk-4.1.so.0 \
    /usr/lib64/libwebkit2gtk-4.1.so.0 \
    /usr/lib/libwebkit2gtk-4.1.so.0 \
    /usr/lib/x86_64-linux-gnu/libwebkit2gtk-4.0.so.37 \
    /usr/lib/x86_64-linux-gnu/libwebkit2gtk-4.0.so.0 \
    /usr/lib64/libwebkit2gtk-4.0.so.0 \
    /usr/lib/libwebkit2gtk-4.0.so.0
  do
    if [ -e "$candidate" ]; then
      readlink -f "$candidate"
      return 0
    fi
  done
  so="$(ldconfig -p 2>/dev/null | awk '/libwebkit2gtk-4\.[01]\.so/{print $NF; exit}')"
  if [ -n "$so" ] && [ -e "$so" ]; then
    readlink -f "$so"
    return 0
  fi
  return 1
}

copy_dir_contents() {
  local src="$1"
  local dest="$2"
  [ -d "$src" ] || return 0
  mkdir -p "$dest"
  cp -a "$src"/. "$dest/"
}

vendor_webkit_stack() {
  local webkit_dest="$1"
  local webkit_so helper_src helper_name libdir loader_src gi_so so
  webkit_so="$(find_webkit_so)" || die \
    "libwebkit2gtk-4.1 (or 4.0) not found. Install libwebkit2gtk-4.1-0 / libwebkit2gtk-4.0-37, gir1.2-webkit2-4.1 or gir1.2-webkit2-4.0, libgirepository1.0-dev, libcairo2-dev, gobject-introspection, gcc."

  echo "Vendoring WebKitGTK from ${webkit_so}"
  mkdir -p "$webkit_dest"
  VENDOR_MODE=webkit
  # vendor_deps only copies NEEDED dependencies, not the seed library itself.
  # Store under the ABI soname loaders expect (…so.0), not the fully versioned
  # realpath (…so.0.19.7), so relocate-webkit patches the file that is loaded.
  webkit_base="$(basename "$webkit_so")"
  case "$webkit_base" in
    libwebkit2gtk-4.1.so.0.*) webkit_name="libwebkit2gtk-4.1.so.0" ;;
    libwebkit2gtk-4.0.so.0.* | libwebkit2gtk-4.0.so.37.*) webkit_name="libwebkit2gtk-4.0.so.0" ;;
    *) webkit_name="$webkit_base" ;;
  esac
  cp -aL "$webkit_so" "$webkit_dest/$webkit_name"
  vendor_deps "$webkit_dest/$webkit_name" "$webkit_dest"

  bundled_webkit=""
  for candidate in "$webkit_dest"/libwebkit2gtk-4.1.so* "$webkit_dest"/libwebkit2gtk-4.0.so*; do
    [ -f "$candidate" ] || continue
    bundled_webkit="$candidate"
    break
  done
  [ -n "$bundled_webkit" ] || die "vendored libwebkit2gtk missing from ${webkit_dest}"

  libdir="$(dirname "$webkit_so")"
  helper_src=""
  helper_name=""
  if [ -d "$libdir/webkit2gtk-4.1" ]; then
    helper_src="$libdir/webkit2gtk-4.1"
    helper_name="webkit2gtk-4.1"
  elif [ -d "$libdir/webkit2gtk-4.0" ]; then
    helper_src="$libdir/webkit2gtk-4.0"
    helper_name="webkit2gtk-4.0"
  fi
  if [ -n "$helper_src" ]; then
    mkdir -p "$APPDIR/usr/lib/$helper_name"
    cp -a "$helper_src"/. "$APPDIR/usr/lib/$helper_name/"
    for so in "$APPDIR/usr/lib/$helper_name"/WebKitWebProcess \
              "$APPDIR/usr/lib/$helper_name"/WebKitNetworkProcess \
              "$APPDIR/usr/lib/$helper_name"/WebKitGPUProcess \
              "$APPDIR/usr/lib/$helper_name"/injected-bundle/*.so
    do
      [ -f "$so" ] || continue
      vendor_deps "$so" "$webkit_dest"
      if command -v patchelf >/dev/null; then
        patchelf --set-rpath "\$ORIGIN:\$ORIGIN/../deepcatalog-webkit" "$so" 2>/dev/null || true
      fi
    done
  fi

  mkdir -p "$APPDIR/usr/lib/girepository-1.0"
  copy_dir_contents /usr/lib/x86_64-linux-gnu/girepository-1.0 "$APPDIR/usr/lib/girepository-1.0"
  copy_dir_contents /usr/lib64/girepository-1.0 "$APPDIR/usr/lib/girepository-1.0"
  copy_dir_contents /usr/lib/girepository-1.0 "$APPDIR/usr/lib/girepository-1.0"
  [ -n "$(ls -A "$APPDIR/usr/lib/girepository-1.0" 2>/dev/null)" ] \
    || die "girepository typelibs missing (install gir1.2-webkit2-4.1 or gir1.2-webkit2-4.0 and gir1.2-gtk-3.0)"

  mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
  copy_dir_contents /usr/share/glib-2.0/schemas "$APPDIR/usr/share/glib-2.0/schemas"
  rm -f "$APPDIR/usr/share/glib-2.0/schemas/gschemas.compiled"
  if command -v glib-compile-schemas >/dev/null; then
    glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas" 2>/dev/null || true
  fi

  loader_src="$(find /usr/lib /usr/lib64 -type d -path '*/gdk-pixbuf-2.0/*/loaders' 2>/dev/null | head -1 || true)"
  if [ -n "$loader_src" ] && [ -d "$loader_src" ]; then
    mkdir -p "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders"
    cp -a "$loader_src"/. "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders/"
    for so in "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders"/*.so; do
      [ -f "$so" ] || continue
      vendor_deps "$so" "$webkit_dest"
      if command -v patchelf >/dev/null; then
        patchelf --set-rpath "\$ORIGIN/../../deepcatalog-webkit" "$so" 2>/dev/null || true
      fi
    done
  fi

  query_loaders=""
  for candidate in \
    "$(command -v gdk-pixbuf-query-loaders 2>/dev/null || true)" \
    "$(command -v gdk-pixbuf-query-loaders-64 2>/dev/null || true)" \
    /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders \
    /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/*/gdk-pixbuf-query-loaders \
    /usr/lib64/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders \
    /usr/bin/gdk-pixbuf-query-loaders
  do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      query_loaders="$candidate"
      break
    fi
  done
  if [ -z "$query_loaders" ]; then
    query_loaders="$(find /usr/lib /usr/lib64 /usr/bin -name 'gdk-pixbuf-query-loaders' -type f 2>/dev/null | head -1 || true)"
  fi
  if [ -n "$query_loaders" ] && [ -x "$query_loaders" ]; then
    echo "Vendoring gdk-pixbuf-query-loaders from ${query_loaders}"
    install -m 0755 "$query_loaders" "$APPDIR/usr/bin/gdk-pixbuf-query-loaders"
    vendor_deps "$APPDIR/usr/bin/gdk-pixbuf-query-loaders" "$webkit_dest"
    if command -v patchelf >/dev/null; then
      patchelf --set-rpath "\$ORIGIN/../lib/deepcatalog-webkit" \
        "$APPDIR/usr/bin/gdk-pixbuf-query-loaders" 2>/dev/null || true
    fi
  fi

  mkdir -p "$APPDIR/usr/share/themes" "$APPDIR/usr/share/icons" "$APPDIR/etc/gtk-3.0"
  for theme in Adwaita Adwaita-dark HighContrast Default; do
    if [ -d "/usr/share/themes/$theme" ]; then
      cp -a "/usr/share/themes/$theme" "$APPDIR/usr/share/themes/"
    fi
  done
  if [ -d /usr/share/icons/Adwaita ]; then
    cp -a /usr/share/icons/Adwaita "$APPDIR/usr/share/icons/"
  fi
  if [ -f /usr/share/icons/hicolor/index.theme ]; then
    mkdir -p "$APPDIR/usr/share/icons/hicolor"
    cp -a /usr/share/icons/hicolor/index.theme "$APPDIR/usr/share/icons/hicolor/"
  fi
  if [ -d /usr/share/mime ]; then
    mkdir -p "$APPDIR/usr/share/mime"
    cp -a /usr/share/mime/. "$APPDIR/usr/share/mime/"
  fi
  cat > "$APPDIR/etc/gtk-3.0/settings.ini" <<'EOF'
[Settings]
gtk-theme-name=Adwaita
gtk-icon-theme-name=Adwaita
gtk-cursor-theme-name=Adwaita
gtk-font-name=Sans 11
gtk-decoration-layout=:minimize,maximize,close
gtk-application-prefer-dark-theme=true
EOF
  [ -d "$APPDIR/usr/share/icons/Adwaita" ] \
    || die "Adwaita icons missing (install adwaita-icon-theme)"
  [ -x "$APPDIR/usr/bin/gdk-pixbuf-query-loaders" ] \
    || die "gdk-pixbuf-query-loaders missing (install libgdk-pixbuf2.0-bin / libgdk-pixbuf2.0-dev)"

  if [ -d /usr/lib/x86_64-linux-gnu/gio/modules ]; then
    mkdir -p "$APPDIR/usr/lib/gio/modules"
    cp -a /usr/lib/x86_64-linux-gnu/gio/modules/. "$APPDIR/usr/lib/gio/modules/" 2>/dev/null || true
  elif [ -d /usr/lib64/gio/modules ]; then
    mkdir -p "$APPDIR/usr/lib/gio/modules"
    cp -a /usr/lib64/gio/modules/. "$APPDIR/usr/lib/gio/modules/" 2>/dev/null || true
  fi
  for so in "$APPDIR/usr/lib/gio/modules"/*.so; do
    [ -f "$so" ] || continue
    vendor_deps "$so" "$webkit_dest"
    if command -v patchelf >/dev/null; then
      patchelf --set-rpath "\$ORIGIN/../../deepcatalog-webkit" "$so" 2>/dev/null || true
    fi
  done

  for gi_so in "$APPDIR"/usr/lib/python3.*/site-packages/gi/_gi*.so \
               "$APPDIR"/usr/lib/python3.*/site-packages/cairo/_cairo*.so; do
    [ -f "$gi_so" ] || continue
    vendor_deps "$gi_so" "$webkit_dest"
    if command -v patchelf >/dev/null; then
      patchelf --set-rpath "\$ORIGIN/../../../deepcatalog-webkit" "$gi_so" 2>/dev/null || true
    fi
  done

  # Relocate after every vendor_deps pass so helpers cannot overwrite with the
  # host libwebkit (compile-time /usr/lib/.../webkit2gtk-* paths).
  python3 "$ROOT/scripts/relocate-webkit.py" "$webkit_dest/$webkit_name"
  VENDOR_MODE=poppler
}

[ "$(uname -s)" = "Linux" ] || die "AppImage builds require Linux"
[ "$(uname -m)" = "x86_64" ] || die "AppImage builds currently support x86_64 only"

need_cmd git
need_cmd curl
need_cmd tar
need_cmd python3
need_cmd sha256sum
need_cmd ldd
need_cmd patchelf
need_cmd pdftoppm
need_cmd pdfinfo
need_cmd file

if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die "git checkout required to build a clean AppImage"
fi

if [ -z "$TAG" ]; then
  TAG="v$(pyproject_version)"
fi
VERSION="${TAG#v}"

if [ -n "$REF_ARG" ]; then
  REF="$REF_ARG"
elif [ "${DEEPCATALOG_APPIMAGE_WORKDIR:-0}" = "1" ]; then
  REF="HEAD"
elif git -C "$ROOT" rev-parse -q --verify "refs/tags/${TAG}^{commit}" >/dev/null; then
  REF="refs/tags/${TAG}"
else
  REF="HEAD"
fi

COMMIT="$(git -C "$ROOT" rev-parse "${REF}^{commit}")"
COMMIT_SHORT="$(git -C "$ROOT" rev-parse --short=12 "$COMMIT")"

for leftover in "$APPDIR" "$DIST/squashfs-root"; do
  [ -e "$leftover" ] || continue
  chmod -R u+w "$leftover" 2>/dev/null || true
  rm -rf "$leftover" || true
  if [ -e "$leftover" ]; then
    find "$leftover" -mindepth 1 -delete 2>/dev/null || true
    rm -rf "$leftover" || die "could not remove ${leftover}"
  fi
done
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/deepcatalog-native" "$APPDIR/usr/lib/deepcatalog-webkit" "$APPDIR/opt/deepcatalog" "$CACHE"

echo "Packing commit ${COMMIT_SHORT} as DeepCatalog ${VERSION}"

git -C "$ROOT" archive --format=tar "$COMMIT" | tar -x -C "$APPDIR/opt/deepcatalog"

SRC="$APPDIR/opt/deepcatalog"
LOCAL_SUFFIX=""
if [ "${DEEPCATALOG_APPIMAGE_WORKDIR:-0}" = "1" ]; then
  echo "Overlaying git working tree (uncommitted tracked files)"
  git -C "$ROOT" ls-files -z \
    | tar -C "$ROOT" --null -T - -cf - \
    | tar -x -C "$SRC"
  LOCAL_SUFFIX="-local"
fi
rm -rf \
  "$SRC/tests" \
  "$SRC/.github" \
  "$SRC/docs" \
  "$SRC/node_modules" \
  "$SRC/.venv" \
  "$SRC/data" \
  "$SRC/.env"

if [ ! -f "$SRC/pyproject.toml" ] || [ ! -f "$SRC/app/static/index.html" ]; then
  die "git archive is missing required project files"
fi
if [ ! -f "$SRC/packaging/linux/AppRun" ]; then
  die "packaging/linux/AppRun missing from commit ${COMMIT_SHORT} — commit packaging assets first"
fi
if [ ! -f "$SRC/packaging/linux/deepcatalog.png" ]; then
  die "packaging/linux/deepcatalog.png missing from commit ${COMMIT_SHORT}"
fi
if [ ! -f "$SRC/packaging/linux/splash.html" ]; then
  die "packaging/linux/splash.html missing from commit ${COMMIT_SHORT}"
fi

rewrite_pyproject_version "$SRC/pyproject.toml" "$VERSION"

install -m 0755 "$SRC/packaging/linux/AppRun" "$APPDIR/AppRun"
install -m 0644 "$SRC/packaging/linux/deepcatalog.desktop" "$APPDIR/deepcatalog.desktop"
install -m 0644 "$SRC/packaging/linux/deepcatalog.svg" "$APPDIR/deepcatalog.svg"
install -m 0644 "$SRC/packaging/linux/deepcatalog.png" "$APPDIR/deepcatalog.png"
install -m 0644 "$SRC/packaging/linux/splash.html" "$APPDIR/splash.html"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
install -m 0644 "$SRC/packaging/linux/deepcatalog.svg" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps/deepcatalog.svg"
install -m 0644 "$SRC/packaging/linux/deepcatalog.png" \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps/deepcatalog.png"

cat > "$APPDIR/usr/bin/deepcatalog" <<'WRAP'
#!/usr/bin/env bash
set -euo pipefail
SELF="$(readlink -f "$0")"
BIN="$(dirname "$SELF")"
export APPDIR="$(cd "$BIN/../.." && pwd)"
exec "$APPDIR/AppRun" "$@"
WRAP
chmod +x "$APPDIR/usr/bin/deepcatalog"

download_verified "$PYTHON_URL" "$CACHE/$PYTHON_TARBALL" "$PYTHON_SHA256"
rm -rf "$CACHE/python"
mkdir -p "$CACHE/python-extract"
tar -xzf "$CACHE/$PYTHON_TARBALL" -C "$CACHE/python-extract"
if [ -d "$CACHE/python-extract/python" ]; then
  cp -a "$CACHE/python-extract/python/." "$APPDIR/usr/"
else
  die "python-build-standalone tarball did not contain a python/ directory"
fi
rm -rf "$CACHE/python-extract"

PYTHON="$APPDIR/usr/bin/python3"
[ -x "$PYTHON" ] || die "bundled python3 missing"
"$PYTHON" -m ensurepip --upgrade >/dev/null
"$PYTHON" -m pip install -U pip
"$PYTHON" -m pip install --no-warn-script-location \
  -c "$SRC/constraints.txt" \
  "$SRC[desktop]"
"$PYTHON" -m pip install --no-warn-script-location \
  'pycairo>=1.26' \
  'pygobject>=3.46,<3.52' \
  || die "pycairo/PyGObject build failed — install gcc, pkg-config, meson, ninja, libgirepository1.0-dev, libcairo2-dev, gobject-introspection (PyGObject 3.52+ needs girepository-2.0, not available on Ubuntu 22.04)"

VENDOR_MODE=poppler
vendor_binary "$(command -v pdftoppm)" "$APPDIR/usr/bin" "$APPDIR/usr/lib/deepcatalog-native"
vendor_binary "$(command -v pdfinfo)" "$APPDIR/usr/bin" "$APPDIR/usr/lib/deepcatalog-native"
vendor_webkit_stack "$APPDIR/usr/lib/deepcatalog-webkit"

download_verified "$APPIMAGETOOL_URL" "$CACHE/appimagetool-x86_64.AppImage" "$APPIMAGETOOL_SHA256"
chmod +x "$CACHE/appimagetool-x86_64.AppImage"

APPIMAGE_NAME="DeepCatalog-${VERSION}${LOCAL_SUFFIX}-x86_64.AppImage"
rm -f "$DIST/$APPIMAGE_NAME"

# appimagetool is itself an AppImage; extract-and-run so CI (no FUSE) can pack.
ARCH="$ARCH" VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 \
  "$CACHE/appimagetool-x86_64.AppImage" "$APPDIR" "$DIST/$APPIMAGE_NAME"
chmod +x "$DIST/$APPIMAGE_NAME"

# Smoke: extract without FUSE and import the FastAPI app + run --help.
rm -rf "$DIST/squashfs-root"
(
  cd "$DIST"
  "./$APPIMAGE_NAME" --appimage-extract >/dev/null
)
SMOKE="$DIST/squashfs-root"
[ -x "$SMOKE/usr/bin/python3" ] || die "extracted AppImage is missing python3"
[ -x "$SMOKE/usr/bin/pdftoppm" ] || die "extracted AppImage is missing pdftoppm"
DEEPCATALOG_PROJECT_ROOT="$SMOKE/opt/deepcatalog" \
  PYTHONPATH="$SMOKE/opt/deepcatalog" \
  "$SMOKE/usr/bin/python3" -c "from app.main import app; print(app.version)"
"$SMOKE/usr/bin/pdftoppm" -v >/dev/null
[ -d "$SMOKE/usr/share/icons/Adwaita" ] || die "extracted AppImage is missing Adwaita icons"
[ -x "$SMOKE/usr/bin/gdk-pixbuf-query-loaders" ] || die "extracted AppImage is missing gdk-pixbuf-query-loaders"
[ -f "$SMOKE/etc/gtk-3.0/settings.ini" ] || die "extracted AppImage is missing gtk-3.0/settings.ini"
ls "$SMOKE"/usr/lib/gdk-pixbuf-2.0/loaders/*svg* >/dev/null 2>&1 \
  || die "extracted AppImage is missing the gdk-pixbuf SVG loader (install librsvg2-common)"
GI_TYPELIB_PATH="$SMOKE/usr/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}" \
  LD_LIBRARY_PATH="$SMOKE/usr/lib/deepcatalog-webkit${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$SMOKE/usr/bin/python3" -c "
import gi
gi.require_version('Gtk', '3.0')
ok = False
for ver in ('4.1', '4.0'):
    try:
        gi.require_version('WebKit2', ver)
        ok = True
        print('webkit2', ver)
        break
    except ValueError:
        pass
if not ok:
    raise SystemExit('WebKit2 typelib missing from AppImage')
from gi.repository import Gtk, WebKit2
print('gi-ok', Gtk, WebKit2)
"
python3 - "$SMOKE" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
libs = list((root / "usr/lib/deepcatalog-webkit").glob("libwebkit2gtk-4.*.so*"))
if not libs:
    raise SystemExit("extracted AppImage is missing libwebkit2gtk")
relocated = False
for lib in libs:
    blob = lib.read_bytes()
    if b"/usr/lib/x86_64-linux-gnu/webkit2gtk-4." in blob:
        raise SystemExit(f"{lib.name} still contains the Ubuntu libexec path")
    if b"/tmp/.dc/x86_64-linux-gnu/webkit2gtk-4." in blob:
        relocated = True
if not relocated:
    raise SystemExit("libwebkit2gtk was not relocated to /tmp/.dc libexec")
print("webkit-libexec-ok")
PY
"$SMOKE/AppRun" --help >/dev/null
rm -rf "$DIST/squashfs-root"

(
  cd "$DIST"
  sha256sum "$APPIMAGE_NAME" | tee "${APPIMAGE_NAME}.sha256"
)

echo "AppImage ready:"
echo "  $DIST/$APPIMAGE_NAME"
echo "  commit ${COMMIT_SHORT} (${COMMIT})"
echo "  version ${VERSION}"
