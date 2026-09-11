"""Native desktop window shell around the local FastAPI UI (pywebview)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
import uvicorn
from dotenv import load_dotenv

from deepcatalog.desktop_bootstrap import mint_desktop_bootstrap
from deepcatalog.env_permissions import harden_secret_file, write_secret_text
from deepcatalog.local_security import (
    assert_bind_allowed,
    port_probe_host,
    ssl_cert_paths,
    sync_configured_bind,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 840
HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.15
WM_CLASS = "DeepCatalog"
DESKTOP_FILE_NAME = "deepcatalog.desktop"
ICON_THEME_NAME = "deepcatalog"
CHROMIUM_HANDOFF_S = 2.5
DROP_MAX_BYTES = 200 * 1024 * 1024
CHROMIUM_BINARIES = (
    "chromium-browser",
    "chromium",
    "google-chrome-stable",
    "google-chrome",
    "microsoft-edge-stable",
    "microsoft-edge",
    "vivaldi-stable",
    "vivaldi",
    "brave-browser",
    "brave-browser-stable",
    "brave",
)
CHROMIUM_FALLBACK_PATHS = (
    "/usr/lib64/chromium-browser/chromium-browser",
    "/usr/lib/chromium-browser/chromium-browser",
    "/opt/google/chrome/chrome",
    "/opt/brave.com/brave/brave",
)

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    override = os.getenv("DEEPCATALOG_PROJECT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "deepcatalog"


def _prepare_environment() -> Path:
    """Ensure DATA_DIR / optional per-user .env are ready before importing the app."""
    data_dir = Path(os.environ.get("DATA_DIR", _default_data_dir())).expanduser()
    os.environ["DATA_DIR"] = str(data_dir.resolve())
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "inbox").mkdir(parents=True, exist_ok=True)
    (data_dir / "archive").mkdir(parents=True, exist_ok=True)
    (data_dir / "chroma").mkdir(parents=True, exist_ok=True)

    env_file = data_dir / ".env"
    example = _project_root() / ".env.example"
    if not env_file.exists() and example.exists():
        write_secret_text(env_file, example.read_text(encoding="utf-8"))
    elif env_file.exists():
        harden_secret_file(env_file, fix=True)
    if env_file.exists():
        load_dotenv(env_file, override=False)
    return data_dir


def _port_is_free(host: str, port: int) -> bool:
    probe = port_probe_host(host)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, preferred: int | None) -> int:
    if preferred and preferred > 0 and _port_is_free(host, preferred):
        return preferred
    if _port_is_free(host, 8080):
        return 8080
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((port_probe_host(host), 0))
        return int(sock.getsockname()[1])


def health_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/api/health"


def is_server_healthy(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        resp = httpx.get(health_url(host, port), timeout=timeout)
        return resp.is_success
    except httpx.HTTPError:
        return False


def should_reuse_running_server() -> bool:
    """False in the AppImage so a leftover :8080 process cannot serve stale code."""
    if os.getenv("APPIMAGE", "").strip():
        return False
    return os.getenv("DEEPCATALOG_APPIMAGE", "").strip().lower() not in {"1", "true", "yes"}


def wait_for_health(host: str, port: int, *, timeout: float = HEALTH_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(health_url(host, port), timeout=0.5)
            if resp.is_success:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(HEALTH_POLL_S)
    detail = f" ({last_error})" if last_error else ""
    raise TimeoutError(
        f"DeepCatalog server did not become ready at {health_url(host, port)}{detail}"
    )


def _start_uvicorn(host: str, port: int) -> uvicorn.Server:
    # Import app only after DATA_DIR / env are prepared so config picks them up.
    from app.main import app

    sync_configured_bind(host, port)
    ssl_paths = ssl_cert_paths()
    uv_kwargs: dict = {
        "app": app,
        "host": host,
        "port": port,
        "log_level": os.getenv("DEEPCATALOG_LOG_LEVEL", "warning"),
        "access_log": False,
    }
    if ssl_paths:
        cert, key = ssl_paths
        uv_kwargs["ssl_certfile"] = str(cert)
        uv_kwargs["ssl_keyfile"] = str(key)
    uv_config = uvicorn.Config(**uv_kwargs)
    server = uvicorn.Server(uv_config)

    def _run() -> None:
        # uvicorn.Server.run() installs its own signal handlers; disable in thread.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        server.run()

    thread = threading.Thread(target=_run, name="deepcatalog-uvicorn", daemon=True)
    thread.start()
    return server


def _stop_uvicorn(server: uvicorn.Server) -> None:
    server.should_exit = True


def _wait_for_server(server: uvicorn.Server) -> None:
    """Block until SIGINT/SIGTERM or the embedded uvicorn loop exits."""

    def _stop(_signum: int | None = None, _frame: object = None) -> None:
        server.should_exit = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while not server.should_exit:
        time.sleep(0.25)


def xdg_data_home() -> Path:
    raw = os.getenv("XDG_DATA_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".local" / "share"


def window_icon_path() -> Path | None:
    """PNG preferred (GTK window icon), then SVG. AppImage overlay first."""
    names = ("deepcatalog.png", "deepcatalog.svg")
    candidates: list[Path] = []
    appdir = os.getenv("APPDIR", "").strip()
    if appdir:
        root = Path(appdir)
        for name in names:
            candidates.append(root / name)
            candidates.append(root / "usr/share/icons/hicolor/256x256/apps" / name)
            candidates.append(root / "usr/share/icons/hicolor/scalable/apps" / name)
    project = _project_root()
    for name in names:
        candidates.append(project / "packaging" / "linux" / name)
    for path in candidates:
        if path.is_file():
            return path
    return None


_SPLASH_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>DeepCatalog Studio</title>
<style>html,body{margin:0;height:100%;background:#0e1317;color:#e8eef2;font-family:ui-sans-serif,system-ui,sans-serif}
body{display:grid;place-items:center}h1{margin:0;font-size:1.6rem}p{margin:.45rem 0 0;opacity:.72}</style>
</head><body><div><h1>DeepCatalog</h1><p>Studio</p></div></body></html>
"""


def splash_html_path() -> Path | None:
    """Bundled splash page shown while WebKit starts (AppImage overlay first)."""
    candidates: list[Path] = []
    appdir = os.getenv("APPDIR", "").strip()
    if appdir:
        root = Path(appdir)
        candidates.append(root / "opt/deepcatalog/packaging/linux/splash.html")
        candidates.append(root / "splash.html")
    candidates.append(_project_root() / "packaging" / "linux" / "splash.html")
    for path in candidates:
        if path.is_file():
            return path
    return None


def splash_html() -> str:
    path = splash_html_path()
    if path is None:
        return _SPLASH_HTML
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return _SPLASH_HTML


_WEBKIT_ENV_DEFAULTS = (
    ("WEBKIT_FORCE_SANDBOX", "0"),
    ("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1"),
    # Bundled (and many host) WebKitGTK builds paint a black view on
    # NVIDIA/Wayland unless compositing and the DMA-BUF renderer are off.
    ("WEBKIT_DISABLE_COMPOSITING_MODE", "1"),
    ("WEBKIT_DISABLE_DMABUF_RENDERER", "1"),
)


def configure_webview_runtime_env() -> None:
    """Fill WebKit defaults before importing webview / GI.

    Existing process env wins so a user can override from the shell.
    """
    if not sys.platform.startswith("linux"):
        return
    for key, value in _WEBKIT_ENV_DEFAULTS:
        os.environ.setdefault(key, value)


def is_external_http_url(url: str) -> bool:
    """True for http(s) URLs that are not the local Studio server."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host not in {"127.0.0.1", "localhost", "::1"}


class DesktopJsApi:
    """pywebview JS bridge: system browser links and file-manager drops."""

    def open_url(self, url: str) -> bool:
        if not isinstance(url, str) or not is_external_http_url(url):
            return False
        _open_in_browser(url)
        return True

    def ingest_drop(self, payload: object) -> dict[str, list[str]]:
        """Copy Nautilus/GTK uri-list drops into the inbox (WebKit FileList is empty)."""
        if isinstance(payload, list):
            text = "\n".join(str(item) for item in payload if item)
        elif isinstance(payload, str):
            text = payload
        else:
            return {"ok": [], "errors": ["invalid drop"]}
        paths = drop_payload_to_paths(text)
        if not paths:
            return {"ok": [], "errors": ["Drop did not contain a supported local file"]}
        return ingest_dropped_scan_paths(paths)


def drop_payload_to_paths(payload: str) -> list[Path]:
    """Parse a GTK/Nautilus text/uri-list drop into existing local files."""
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_line in payload.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        candidate: Path | None = None
        if line.startswith("file:"):
            parsed = urlparse(line)
            if parsed.scheme != "file":
                continue
            host = (parsed.netloc or "").lower()
            if host not in {"", "localhost", "localhost.localdomain"}:
                continue
            candidate = Path(unquote(parsed.path))
        elif line.startswith("/"):
            candidate = Path(unquote(line))
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if not resolved.is_file() or resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)
    return paths


def ingest_dropped_scan_paths(paths: list[Path]) -> dict[str, list[str]]:
    """Copy dropped local scans into the inbox. Returns ok / error names."""
    # Imported here so `deepcatalog.desktop` can load without tools/__init__
    # (that package import pulls chromadb and the rest of the pipeline).
    from deepcatalog.tools.filesystem import copy_local_scan_to_inbox

    ok: list[str] = []
    errors: list[str] = []
    for path in paths:
        result = copy_local_scan_to_inbox(path, max_bytes=DROP_MAX_BYTES)
        name = path.name
        if result.get("status") == "success":
            ok.append(str(result.get("filename") or name))
        else:
            errors.append(f"{name}: {result.get('error') or 'failed'}")
    return {"ok": ok, "errors": errors}


def _notify_desktop_drop(window: object, result: dict[str, list[str]]) -> None:
    evaluate = getattr(window, "evaluate_js", None)
    if not callable(evaluate):
        return
    detail = json.dumps({"ok": result.get("ok") or [], "errors": result.get("errors") or []})
    evaluate(f"window.dcDispatchDesktopDrop && window.dcDispatchDesktopDrop({detail});")


def _gtk_selection_payload(data: object) -> str:
    """Best-effort text from a GTK SelectionData (uri-list, text, or raw bytes)."""
    chunks: list[str] = []
    get_uris = getattr(data, "get_uris", None)
    if callable(get_uris):
        try:
            chunks.extend([str(item) for item in (get_uris() or []) if item])
        except Exception:  # noqa: BLE001 — SelectionData backends vary
            pass
    if not chunks:
        get_text = getattr(data, "get_text", None)
        if callable(get_text):
            try:
                text = get_text()
            except Exception:  # noqa: BLE001
                text = None
            if text:
                chunks.append(str(text))
    if not chunks:
        get_data = getattr(data, "get_data", None)
        if callable(get_data):
            try:
                raw = get_data()
            except Exception:  # noqa: BLE001
                raw = None
            if isinstance(raw, bytes) and raw:
                chunks.append(raw.decode("utf-8", "replace"))
    return "\n".join(chunks)


def _gtk_widget_descendants(widget: object) -> list[object]:
    found: list[object] = [widget]
    get_children = getattr(widget, "get_children", None)
    if callable(get_children):
        try:
            children = list(get_children() or [])
        except Exception:  # noqa: BLE001 — widget types vary
            children = []
        for child in children:
            found.extend(_gtk_widget_descendants(child))
    get_child = getattr(widget, "get_child", None)
    if callable(get_child) and not callable(get_children):
        try:
            child = get_child()
        except Exception:  # noqa: BLE001
            child = None
        if child is not None:
            found.extend(_gtk_widget_descendants(child))
    return found


def _enable_gtk_file_drops(window: object) -> None:
    """Accept file-manager drops. WebKitGTK often leaves JS dataTransfer.files empty."""
    native = getattr(window, "native", None)
    if native is None:
        return
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk
    except (ImportError, ValueError):
        return

    try:
        targets = [
            Gtk.TargetEntry.new("text/uri-list", 0, 0),
            Gtk.TargetEntry.new("text/plain", 0, 1),
            Gtk.TargetEntry.new("x-special/gnome-copied-files", 0, 2),
            Gtk.TargetEntry.new("application/vnd.portal.filetransfer", 0, 3),
        ]
        action = Gdk.DragAction.COPY
        defaults = Gtk.DestDefaults.ALL
        last_key: tuple[str, ...] | None = None
        last_at = 0.0

        def _on_drag_motion(
            _widget: object,
            context: object,
            _x: int,
            _y: int,
            drag_time: int,
        ) -> bool:
            status = getattr(Gdk, "drag_status", None)
            if callable(status):
                try:
                    status(context, action, drag_time)
                except Exception:  # noqa: BLE001
                    pass
            return True

        def _on_drag_data(
            _widget: object,
            _ctx: object,
            _x: int,
            _y: int,
            data: object,
            _info: int,
            _time: int,
        ) -> None:
            nonlocal last_key, last_at
            paths = drop_payload_to_paths(_gtk_selection_payload(data))
            if not paths:
                return
            key = tuple(str(path) for path in paths)
            now = time.monotonic()
            if key == last_key and now - last_at < 0.75:
                return
            last_key = key
            last_at = now
            result = ingest_dropped_scan_paths(paths)
            _notify_desktop_drop(window, result)

        for widget in _gtk_widget_descendants(native):
            type_name = type(widget).__name__
            # Do not replace WebView's HTML5 dest — JS then never sees uri-list.
            if type_name not in {"ApplicationWindow", "Window"}:
                continue
            setter = getattr(widget, "drag_dest_set", None)
            if not callable(setter):
                continue
            try:
                setter(defaults, targets, action)
            except Exception:  # noqa: BLE001 — not every descendant is a drag dest
                continue
            add_uri = getattr(widget, "drag_dest_add_uri_targets", None)
            if callable(add_uri):
                try:
                    add_uri()
                except Exception:  # noqa: BLE001
                    pass
            connect = getattr(widget, "connect", None)
            if callable(connect):
                connect("drag-motion", _on_drag_motion)
                connect("drag-data-received", _on_drag_data)
    except Exception as exc:  # noqa: BLE001 — drop support is best-effort
        logger.debug("Could not enable GTK file drops: %s", exc)


def _quote_desktop_exec_arg(value: str) -> str:
    if value and all(ch.isalnum() or ch in "/._-+:@" for ch in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def desktop_exec_command() -> str:
    """Exec= line for the user .desktop file (AppImage path, or this interpreter)."""
    image = os.getenv("APPIMAGE", "").strip()
    if image and Path(image).is_file():
        return _quote_desktop_exec_arg(str(Path(image).resolve()))
    return " ".join(
        [
            _quote_desktop_exec_arg(sys.executable),
            "-m",
            "deepcatalog.desktop",
        ]
    )


def render_desktop_entry(*, exec_line: str, icon: str = ICON_THEME_NAME) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.1\n"
        "Name=DeepCatalog Studio\n"
        "Comment=Your local workspace for archive discovery and automation.\n"
        f"Exec={exec_line}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Office;Scanning;Utility;\n"
        "StartupNotify=true\n"
        f"StartupWMClass={WM_CLASS}\n"
        "MimeType=application/pdf;image/png;image/jpeg;image/tiff;image/webp;\n"
        "Keywords=OCR;PDF;documents;archive;RAG;\n"
    )


def _copy_icon_into_hicolor(src: Path, data_home: Path) -> Path | None:
    name = src.name.lower()
    if name.endswith(".png"):
        dest = data_home / "icons" / "hicolor" / "256x256" / "apps" / "deepcatalog.png"
    elif name.endswith(".svg"):
        dest = data_home / "icons" / "hicolor" / "scalable" / "apps" / "deepcatalog.svg"
    else:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def install_linux_desktop_entry() -> Path | None:
    """Install ~/.local/share/applications/deepcatalog.desktop and themed icons."""
    if not sys.platform.startswith("linux"):
        return None
    data_home = xdg_data_home()
    applications = data_home / "applications"
    applications.mkdir(parents=True, exist_ok=True)

    icon_field = ICON_THEME_NAME
    png = None
    svg = None
    icon_src = window_icon_path()
    appdir = os.getenv("APPDIR", "").strip()
    search: list[Path] = []
    if icon_src is not None:
        search.append(icon_src)
    if appdir:
        search.extend(
            [
                Path(appdir) / "deepcatalog.png",
                Path(appdir) / "deepcatalog.svg",
                Path(appdir) / "usr/share/icons/hicolor/256x256/apps/deepcatalog.png",
                Path(appdir) / "usr/share/icons/hicolor/scalable/apps/deepcatalog.svg",
            ]
        )
    project_packaging = _project_root() / "packaging" / "linux"
    search.extend(
        [
            project_packaging / "deepcatalog.png",
            project_packaging / "deepcatalog.svg",
        ]
    )
    seen: set[Path] = set()
    for src in search:
        resolved = src.resolve() if src.exists() else src
        if resolved in seen or not src.is_file():
            continue
        seen.add(resolved)
        copied = _copy_icon_into_hicolor(src, data_home)
        if copied is None:
            continue
        if copied.suffix == ".png" and png is None:
            png = copied
        if copied.suffix == ".svg" and svg is None:
            svg = copied
    if png is not None:
        icon_field = str(png)
    elif svg is not None:
        icon_field = str(svg)

    dest = applications / DESKTOP_FILE_NAME
    text = render_desktop_entry(exec_line=desktop_exec_command(), icon=icon_field)
    if dest.is_file() and dest.read_text(encoding="utf-8") == text:
        return dest
    dest.write_text(text, encoding="utf-8")
    try:
        dest.chmod(0o644)
    except OSError:
        pass
    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run(  # noqa: S603
            [updater, str(applications)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    logger.info("Installed desktop entry %s", dest)
    return dest


def desktop_ui_url(host: str, port: int, *, bootstrap_nonce: str | None = None) -> str:
    """Local UI URL with desktop=1 so the SPA uses app chrome, not website chrome."""
    if bootstrap_nonce:
        return f"http://{host}:{port}/api/auth/desktop-bootstrap/{bootstrap_nonce}?desktop=1"
    return f"http://{host}:{port}/?desktop=1"


def find_chromium_app_browser() -> str | None:
    for name in CHROMIUM_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    for raw in CHROMIUM_FALLBACK_PATHS:
        candidate = Path(raw)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def chromium_profile_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "chromium-profile"


def webview_storage_path(data_dir: Path) -> Path:
    """Cookies and localStorage for the native window (theme, Ask examples, sessions)."""
    path = Path(data_dir) / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chromium_app_argv(
    browser: str,
    url: str,
    profile: Path,
    *,
    width: int,
    height: int,
) -> list[str]:
    profile.mkdir(parents=True, exist_ok=True)
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--profile-directory=Default",
        f"--class={WM_CLASS}",
        f"--name={WM_CLASS}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=TranslateUI,MediaRouter,InfiniteSessionRestore",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--password-store=basic",
        "--noerrdialogs",
        # XWayland so --class/StartupWMClass apply; native Wayland groups with Chromium.
        "--ozone-platform-hint=x11",
    ]


def open_chromium_app_window(
    url: str,
    *,
    data_dir: Path,
    width: int,
    height: int,
) -> str | None:
    """Open a chromeless Chromium window. Returns closed / detached, or None."""
    browser = find_chromium_app_browser()
    if not browser:
        logger.info("No Chromium-based browser found for an --app window")
        return None
    profile = chromium_profile_dir(data_dir)
    argv = chromium_app_argv(browser, url, profile, width=width, height=height)
    env = os.environ.copy()
    env["CHROME_DESKTOP"] = DESKTOP_FILE_NAME
    logger.info("Opening app window with %s", browser)
    try:
        proc = subprocess.Popen(  # noqa: S603
            argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        logger.warning("Could not launch %s: %s", browser, exc)
        return None
    try:
        proc.wait(timeout=CHROMIUM_HANDOFF_S)
    except subprocess.TimeoutExpired:
        proc.wait()
        return "closed"
    if proc.returncode == 0:
        logger.info("Chromium --app handed off; keeping the local server running")
        return "detached"
    logger.warning("%s exited immediately with code %s", browser, proc.returncode)
    return None


def _open_in_browser(url: str) -> None:
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if opener:
        subprocess.Popen(  # noqa: S603
            [opener, url] if opener.endswith("xdg-open") else [opener, "open", url],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    webbrowser.open(url)


def desktop_prefers_dark() -> bool:
    """Whether the GTK chrome should use Adwaita dark (from AppRun / env)."""
    raw = os.getenv("DEEPCATALOG_GTK_DARK", "").strip().lower()
    if raw in {"1", "true", "yes", "dark"}:
        return True
    if raw in {"0", "false", "no", "light"}:
        return False
    theme = os.getenv("GTK_THEME", "").strip().lower()
    return ":dark" in theme or theme.endswith("-dark")


def _apply_gtk_wm_class() -> None:
    """Set WM_CLASS and reinforce Adwaita dark CSD after Gtk 3 is pinned.

    Must call ``gi.require_version('Gtk', '3.0')`` before importing Gtk —
    a bare import can bind host Gtk 4 and abort AppImage startup on Debian.
    """
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk
    except Exception:  # noqa: BLE001 — GI missing or typelib mismatch
        return
    try:
        GLib.set_prgname(WM_CLASS)
        GLib.set_application_name("DeepCatalog Studio")
    except Exception as exc:  # noqa: BLE001 — GI bindings vary by distro
        logger.debug("Could not set GTK application id: %s", exc)
    try:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return
        dark = desktop_prefers_dark()
        # Property value is the theme family name; variant comes from prefer-dark
        # and/or GTK_THEME=Adwaita:dark set by AppRun.
        settings.set_property("gtk-theme-name", "Adwaita")
        settings.set_property("gtk-application-prefer-dark-theme", dark)
        settings.set_property("gtk-decoration-layout", ":minimize,maximize,close")
    except Exception as exc:  # noqa: BLE001 — GI bindings vary by distro
        logger.debug("Could not apply GTK dark preference: %s", exc)


def _apply_window_icon(window: object, icon: Path | None) -> None:
    if icon is None or window is None:
        return
    native = getattr(window, "native", None)
    setter = getattr(native, "set_icon_from_file", None)
    if callable(setter):
        try:
            setter(str(icon))
        except Exception as exc:  # noqa: BLE001 — icon is best-effort
            logger.debug("Could not set native window icon: %s", exc)


def _try_native_window(
    url: str,
    width: int,
    height: int,
    *,
    data_dir: Path | None = None,
) -> bool:
    """Open pywebview; return False when WebKitGTK / pywebview is unavailable."""
    configure_webview_runtime_env()
    try:
        import webview
    except ImportError as exc:
        logger.warning("pywebview is not available (%s)", exc)
        return False
    icon = window_icon_path()
    try:
        # After webview import (which pins Gtk 3); never import Gtk ourselves first.
        _apply_gtk_wm_class()
        try:
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:  # noqa: BLE001 — settings may be immutable
            pass
        window = webview.create_window(
            "DeepCatalog Studio",
            url=url,
            js_api=DesktopJsApi(),
            width=width,
            height=height,
            min_size=(900, 600),
            text_select=False,
            background_color="#0e1317",
        )
        if window is not None:
            events = getattr(window, "events", None)
            shown = getattr(events, "shown", None)

            def _on_shown() -> None:
                _apply_window_icon(window, icon)
                _enable_gtk_file_drops(window)

            if shown is not None:
                shown += _on_shown
        storage = webview_storage_path(
            data_dir
            if data_dir is not None
            else Path(os.environ.get("DATA_DIR", _default_data_dir()))
        )
        start_kwargs: dict = {
            "debug": False,
            "private_mode": False,
            "storage_path": str(storage),
        }
        if sys.platform.startswith("linux"):
            start_kwargs["gui"] = "gtk"
        if icon is not None:
            start_kwargs["icon"] = str(icon)
        webview.start(**start_kwargs)
    except Exception:
        logger.exception(
            "Native WebKitGTK window failed; falling back to a Chromium --app window. "
            "Install WebKitGTK (webkit2gtk4.1) plus PyGObject for the GTK window."
        )
        return False
    return True


def _chromium_result_closes_ui(result: str | None) -> bool | None:
    """True = window closed; False = keep server; None = try the next backend."""
    if result == "closed":
        return True
    if result == "detached":
        return False
    return None


def _launch_ui_window(url: str, *, data_dir: Path, width: int, height: int) -> bool:
    """Open the UI. Return True when the window closed and the process should exit."""
    backends = ("webview", "chromium-app", "browser")
    for backend in backends:
        if backend == "webview":
            if _try_native_window(url, width, height, data_dir=data_dir):
                return True
            continue
        if backend == "chromium-app":
            closed = _chromium_result_closes_ui(
                open_chromium_app_window(url, data_dir=data_dir, width=width, height=height)
            )
            if closed is None:
                continue
            return closed
        _open_in_browser(url)
        return False
    return False


def run_desktop(
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    headless: bool = False,
) -> int:
    """
    Open the DeepCatalog Studio UI in a native window.

    Starts a local uvicorn server when nothing healthy is already listening.
    ``headless`` keeps the server in the foreground (systemd / AppImage autostart).
    Window order: native pywebview/WebKitGTK first (bundled in the AppImage),
    then a Chromium ``--app`` window, then the default browser.
    """
    if not logging.getLogger().handlers:
        level_name = os.getenv("DEEPCATALOG_LOG_LEVEL", "warning").strip().upper() or "WARNING"
        level = getattr(logging, level_name, logging.WARNING)
        logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    data_dir = _prepare_environment()
    root = _project_root()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        install_linux_desktop_entry()
    except OSError:
        logger.exception("Could not install the DeepCatalog .desktop entry")

    preferred = port
    if preferred is None:
        env_port = os.getenv("DEEPCATALOG_PORT", "").strip()
        preferred = int(env_port) if env_port.isdigit() else None

    assert_bind_allowed(host)

    # Prefer an already-running local instance (e.g. systemd --user service).
    # AppImage always starts its own server so a leftover :8080 process cannot
    # keep serving an older build's CSRF / upload code.
    reuse_port = preferred or 8080
    owned_server: uvicorn.Server | None = None
    if should_reuse_running_server() and is_server_healthy(host, reuse_port):
        active_port = reuse_port
    else:
        active_port = _pick_port(host, preferred)
        owned_server = _start_uvicorn(host, active_port)
        try:
            wait_for_health(host, active_port)
        except TimeoutError:
            if owned_server is not None:
                _stop_uvicorn(owned_server)
            raise

    if not headless:
        url = desktop_ui_url(host, active_port, bootstrap_nonce=mint_desktop_bootstrap())
        ui_closed = _launch_ui_window(url, data_dir=data_dir, width=width, height=height)
        if ui_closed:
            if owned_server is not None:
                _stop_uvicorn(owned_server)
            return 0

    if owned_server is None:
        return 0
    try:
        _wait_for_server(owned_server)
    finally:
        _stop_uvicorn(owned_server)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepCatalog Studio desktop window")
    parser.add_argument(
        "--host",
        default=os.getenv("DEEPCATALOG_HOST", DEFAULT_HOST),
        help="Loopback host for the embedded server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to use (default: DEEPCATALOG_PORT, or 8080 if free / ephemeral)",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the local server without a native window (systemd / AppImage autostart)",
    )
    args = parser.parse_args(argv)
    return run_desktop(
        host=args.host,
        port=args.port,
        width=args.width,
        height=args.height,
        headless=args.headless,
    )


if __name__ == "__main__":
    raise SystemExit(main())
