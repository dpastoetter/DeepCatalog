"""Tests for the desktop shell helpers (no GUI)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import pytest

from deepcatalog.desktop import (
    WM_CLASS,
    DesktopJsApi,
    _launch_ui_window,
    _pick_port,
    _project_root,
    _try_native_window,
    chromium_app_argv,
    chromium_profile_dir,
    configure_webview_runtime_env,
    desktop_exec_command,
    desktop_prefers_dark,
    desktop_ui_url,
    drop_payload_to_paths,
    find_chromium_app_browser,
    health_url,
    ingest_dropped_scan_paths,
    install_linux_desktop_entry,
    is_external_http_url,
    is_server_healthy,
    main,
    open_chromium_app_window,
    render_desktop_entry,
    should_reuse_running_server,
    splash_html,
    splash_html_path,
    wait_for_health,
    webview_storage_path,
    window_icon_path,
)


def test_pick_port_returns_preferred():
    assert _pick_port("127.0.0.1", 9090) == 9090


def test_pick_port_returns_positive():
    port = _pick_port("127.0.0.1", None)
    assert isinstance(port, int)
    assert port > 0


def test_wait_for_health_succeeds():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wait_for_health("127.0.0.1", port, timeout=5.0)
        assert is_server_healthy("127.0.0.1", port)
        assert health_url("127.0.0.1", port).endswith(f":{port}/api/health")
    finally:
        server.shutdown()


def test_wait_for_health_times_out():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    # Port is not listening after the socket closes — health must time out.
    with pytest.raises(TimeoutError, match="did not become ready"):
        wait_for_health("127.0.0.1", port, timeout=0.4)


def test_appimage_does_not_reuse_a_running_server(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_APPIMAGE", "1")
    assert should_reuse_running_server() is False
    monkeypatch.delenv("DEEPCATALOG_APPIMAGE", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert should_reuse_running_server() is True
    monkeypatch.setenv("APPIMAGE", "/tmp/DeepCatalog.AppImage")
    assert should_reuse_running_server() is False


def test_project_root_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_PROJECT_ROOT", str(tmp_path))
    assert _project_root() == tmp_path.resolve()


def test_main_passes_headless(monkeypatch):
    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("deepcatalog.desktop.run_desktop", fake_run)
    assert main(["--headless", "--port", "9999"]) == 0
    assert captured["headless"] is True
    assert captured["port"] == 9999


def test_render_desktop_entry_sets_wm_class():
    text = render_desktop_entry(
        exec_line="/opt/DeepCatalog.AppImage",
        icon="/tmp/deepcatalog.png",
    )
    assert f"StartupWMClass={WM_CLASS}" in text
    assert "Exec=/opt/DeepCatalog.AppImage" in text
    assert "Icon=/tmp/deepcatalog.png" in text
    assert "Terminal=false" in text


def test_desktop_exec_command_uses_appimage(tmp_path, monkeypatch):
    image = tmp_path / "DeepCatalog-x86_64.AppImage"
    image.write_bytes(b"fake")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert desktop_exec_command() == str(image.resolve())


def test_desktop_exec_command_quotes_spaces(tmp_path, monkeypatch):
    image = tmp_path / "DeepCatalog App.AppImage"
    image.write_bytes(b"fake")
    monkeypatch.setenv("APPIMAGE", str(image))
    quoted = desktop_exec_command()
    assert quoted.startswith('"')
    assert "DeepCatalog App.AppImage" in quoted


def test_chromium_app_argv_uses_isolated_profile_and_class(tmp_path):
    profile = chromium_profile_dir(tmp_path)
    argv = chromium_app_argv(
        "/usr/bin/brave-browser",
        "http://127.0.0.1:8080/",
        profile,
        width=1280,
        height=840,
    )
    assert argv[0] == "/usr/bin/brave-browser"
    assert "--app=http://127.0.0.1:8080/" in argv
    assert f"--user-data-dir={profile}" in argv
    assert "--profile-directory=Default" in argv
    assert f"--class={WM_CLASS}" in argv
    assert "--ozone-platform-hint=x11" in argv
    assert profile.is_dir()


def test_webview_storage_path_creates_dir(tmp_path):
    path = webview_storage_path(tmp_path)
    assert path == tmp_path / "webview"
    assert path.is_dir()


def test_find_chromium_app_browser_prefers_chromium(monkeypatch):
    def fake_which(name: str) -> str | None:
        mapping = {"brave-browser": "/usr/bin/brave-browser", "chromium": "/usr/bin/chromium"}
        return mapping.get(name)

    monkeypatch.setattr("deepcatalog.desktop.shutil.which", fake_which)
    assert find_chromium_app_browser() == "/usr/bin/chromium"


def test_open_chromium_app_window_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.desktop.find_chromium_app_browser",
        lambda: "/usr/bin/brave-browser",
    )

    class Proc:
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="brave", timeout=timeout)
            return 0

    monkeypatch.setattr("deepcatalog.desktop.subprocess.Popen", lambda *a, **k: Proc())
    result = open_chromium_app_window(
        "http://127.0.0.1:8080/",
        data_dir=tmp_path,
        width=800,
        height=600,
    )
    assert result == "closed"


def test_open_chromium_app_window_detached(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "deepcatalog.desktop.find_chromium_app_browser",
        lambda: "/usr/bin/brave-browser",
    )

    class Proc:
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("deepcatalog.desktop.subprocess.Popen", lambda *a, **k: Proc())
    result = open_chromium_app_window(
        "http://127.0.0.1:8080/",
        data_dir=tmp_path,
        width=800,
        height=600,
    )
    assert result == "detached"


def test_open_chromium_app_window_missing_browser(tmp_path, monkeypatch):
    monkeypatch.setattr("deepcatalog.desktop.find_chromium_app_browser", lambda: None)
    assert (
        open_chromium_app_window(
            "http://127.0.0.1:8080/",
            data_dir=tmp_path,
            width=800,
            height=600,
        )
        is None
    )


def test_install_linux_desktop_entry(tmp_path, monkeypatch):
    image = tmp_path / "DeepCatalog.AppImage"
    image.write_bytes(b"fake")
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("APPIMAGE", str(image))
    monkeypatch.setattr("deepcatalog.desktop.sys.platform", "linux")
    monkeypatch.setattr("deepcatalog.desktop.shutil.which", lambda _name: None)

    dest = install_linux_desktop_entry()
    assert dest == xdg / "applications" / "deepcatalog.desktop"
    text = dest.read_text(encoding="utf-8")
    assert f"StartupWMClass={WM_CLASS}" in text
    assert str(image.resolve()) in text
    icons = list((xdg / "icons").rglob("deepcatalog.*"))
    assert icons, "expected themed icon files"


def test_window_icon_path_prefers_appdir_png(tmp_path, monkeypatch):
    png = tmp_path / "deepcatalog.png"
    png.write_bytes(b"png")
    monkeypatch.setenv("APPDIR", str(tmp_path))
    assert window_icon_path() == png


def test_try_native_window_logs_import_error(monkeypatch, caplog):
    monkeypatch.delitem(sys.modules, "webview", raising=False)

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("no webview")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with caplog.at_level(logging.WARNING, logger="deepcatalog.desktop"):
        assert _try_native_window("http://127.0.0.1:8080/", 800, 600) is False
    assert "pywebview is not available" in caplog.text


def test_try_native_window_logs_webkit_failure(monkeypatch, caplog):
    webview = MagicMock()
    webview.create_window.side_effect = RuntimeError("WebKit2 typelib missing")
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr("deepcatalog.desktop._apply_gtk_wm_class", lambda: None)
    with caplog.at_level(logging.ERROR, logger="deepcatalog.desktop"):
        assert _try_native_window("http://127.0.0.1:8080/", 800, 600) is False
    assert "Native WebKitGTK window failed" in caplog.text


def test_try_native_window_starts_gtk_backend(monkeypatch, tmp_path):
    webview = MagicMock()
    webview.settings = {}
    window = MagicMock()
    webview.create_window.return_value = window
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr("deepcatalog.desktop._apply_gtk_wm_class", lambda: None)
    monkeypatch.setattr("deepcatalog.desktop.sys.platform", "linux")
    assert (
        _try_native_window("http://127.0.0.1:8080/?desktop=1", 800, 600, data_dir=tmp_path) is True
    )
    kwargs = webview.start.call_args.kwargs
    assert kwargs["gui"] == "gtk"
    assert kwargs["debug"] is False
    assert kwargs["private_mode"] is False
    assert kwargs["storage_path"] == str(tmp_path / "webview")
    created = webview.create_window.call_args.kwargs
    assert created["url"] == "http://127.0.0.1:8080/?desktop=1"
    assert created["text_select"] is False
    assert not created.get("html")
    assert created["js_api"].__class__.__name__ == "DesktopJsApi"


def test_desktop_prefers_dark_reads_env(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_GTK_DARK", "1")
    monkeypatch.delenv("GTK_THEME", raising=False)
    assert desktop_prefers_dark() is True
    monkeypatch.setenv("DEEPCATALOG_GTK_DARK", "0")
    assert desktop_prefers_dark() is False
    monkeypatch.delenv("DEEPCATALOG_GTK_DARK", raising=False)
    monkeypatch.setenv("GTK_THEME", "Adwaita:dark")
    assert desktop_prefers_dark() is True


def test_apply_gtk_wm_class_sets_dark_theme(monkeypatch):
    props: dict[str, object] = {}

    class FakeSettings:
        def set_property(self, name: str, value: object) -> None:
            props[name] = value

    class FakeGtk:
        class Settings:
            @staticmethod
            def get_default():
                return FakeSettings()

    class FakeGLib:
        @staticmethod
        def set_prgname(_name: str) -> None:
            return None

        @staticmethod
        def set_application_name(_name: str) -> None:
            return None

    import sys
    from types import ModuleType

    gi = ModuleType("gi")
    repository = ModuleType("gi.repository")
    repository.GLib = FakeGLib
    repository.Gtk = FakeGtk
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setenv("DEEPCATALOG_GTK_DARK", "1")
    from deepcatalog.desktop import _apply_gtk_wm_class

    _apply_gtk_wm_class()
    assert props["gtk-theme-name"] == "Adwaita:dark"
    assert props["gtk-application-prefer-dark-theme"] is True
    assert props["gtk-decoration-layout"] == ":minimize,maximize,close"


def test_configure_webview_runtime_env_sets_webkit_defaults(monkeypatch):
    monkeypatch.setattr("deepcatalog.desktop.sys.platform", "linux")
    monkeypatch.delenv("WEBKIT_FORCE_SANDBOX", raising=False)
    monkeypatch.delenv("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", raising=False)
    monkeypatch.delenv("WEBKIT_DISABLE_COMPOSITING_MODE", raising=False)
    monkeypatch.delenv("WEBKIT_DISABLE_DMABUF_RENDERER", raising=False)
    configure_webview_runtime_env()
    assert os.environ["WEBKIT_FORCE_SANDBOX"] == "0"
    assert os.environ["WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS"] == "1"
    assert os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] == "1"
    assert os.environ["WEBKIT_DISABLE_DMABUF_RENDERER"] == "1"


def test_configure_webview_runtime_env_does_not_override(monkeypatch):
    monkeypatch.setattr("deepcatalog.desktop.sys.platform", "linux")
    monkeypatch.setenv("WEBKIT_DISABLE_COMPOSITING_MODE", "0")
    configure_webview_runtime_env()
    assert os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] == "0"


def test_launch_falls_back_to_chromium_when_webview_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("deepcatalog.desktop._try_native_window", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "deepcatalog.desktop.open_chromium_app_window",
        lambda *_a, **_k: "closed",
    )
    assert (
        _launch_ui_window(
            "http://127.0.0.1:8080/?desktop=1",
            data_dir=tmp_path,
            width=800,
            height=600,
        )
        is True
    )


def test_desktop_ui_url_marks_app_shell():
    assert desktop_ui_url("127.0.0.1", 8080) == "http://127.0.0.1:8080/?desktop=1"
    assert (
        desktop_ui_url("127.0.0.1", 8080, bootstrap_nonce="abc")
        == "http://127.0.0.1:8080/api/auth/desktop-bootstrap/abc?desktop=1"
    )


def test_is_external_http_url():
    assert is_external_http_url("https://auth.openai.com/authorize") is True
    assert is_external_http_url("https://ollama.com/download") is True
    assert is_external_http_url("http://127.0.0.1:8080/?desktop=1") is False
    assert is_external_http_url("http://localhost:1455/auth/callback") is False
    assert is_external_http_url("about:blank") is False
    assert is_external_http_url("not a url") is False


def test_desktop_js_api_opens_external_only(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("deepcatalog.desktop._open_in_browser", opened.append)
    api = DesktopJsApi()
    assert api.open_url("https://github.com/dpastoetter/DeepCatalog") is True
    assert api.open_url("http://127.0.0.1:8080/") is False
    assert api.open_url("about:blank") is False
    assert opened == ["https://github.com/dpastoetter/DeepCatalog"]


def test_desktop_js_api_ingest_drop_copies_local_pdf(isolated_data, tmp_path, monkeypatch):
    from tests.media_fixtures import write_minimal_pdf

    pdf = write_minimal_pdf(tmp_path / "nautilus.pdf")
    api = DesktopJsApi()
    result = api.ingest_drop(f"copy\n{pdf.as_uri()}\n")
    assert result["ok"] == ["nautilus.pdf"]
    assert result["errors"] == []


def test_splash_html_reads_packaging_file():
    path = splash_html_path()
    assert path is not None
    assert path.name == "splash.html"
    html = splash_html()
    assert "DeepCatalog" in html
    assert "Studio" in html


def test_launch_uses_webview_before_chromium(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_APPIMAGE", "1")
    order: list[str] = []

    def webview_ok(*_a, **_k):
        order.append("webview")
        return True

    def chromium_should_not_run(*_a, **_k):
        raise AssertionError("Chromium --app must not run when pywebview succeeded")

    monkeypatch.setattr("deepcatalog.desktop._try_native_window", webview_ok)
    monkeypatch.setattr("deepcatalog.desktop.open_chromium_app_window", chromium_should_not_run)
    assert (
        _launch_ui_window(
            "http://127.0.0.1:8080/?desktop=1",
            data_dir=tmp_path,
            width=800,
            height=600,
        )
        is True
    )
    assert order == ["webview"]


def test_launch_falls_back_to_chromium_then_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_APPIMAGE", "1")
    opened: list[str] = []
    monkeypatch.setattr("deepcatalog.desktop._try_native_window", lambda *_a, **_k: False)
    monkeypatch.setattr("deepcatalog.desktop.open_chromium_app_window", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "deepcatalog.desktop._open_in_browser",
        lambda url: opened.append(url),
    )
    assert (
        _launch_ui_window(
            "http://127.0.0.1:8080/?desktop=1",
            data_dir=tmp_path,
            width=800,
            height=600,
        )
        is False
    )
    assert opened == ["http://127.0.0.1:8080/?desktop=1"]


def test_drop_payload_to_paths_parses_uri_list(tmp_path):
    from tests.media_fixtures import write_minimal_pdf

    spaced = write_minimal_pdf(tmp_path / "my scan.pdf")
    nested = write_minimal_pdf(tmp_path / "ok.pdf")
    missing = tmp_path / "gone.pdf"
    folder = tmp_path / "dir"
    folder.mkdir()
    payload = "\r\n".join(
        [
            "# comment",
            spaced.as_uri(),
            f"file://localhost{nested.as_posix()}",
            str(nested),
            str(missing),
            str(folder),
            "file://example.com/remote.pdf",
            "https://example.com/scan.pdf",
            "",
        ]
    )
    paths = drop_payload_to_paths(payload)
    assert paths == [spaced.resolve(), nested.resolve()]
    gnome = drop_payload_to_paths(f"copy\n{spaced.as_uri()}\n")
    assert gnome == [spaced.resolve()]


def test_ingest_dropped_scan_paths_copies_and_reports(isolated_data, tmp_path):
    from tests.media_fixtures import write_minimal_pdf

    pdf = write_minimal_pdf(tmp_path / "drop.pdf")
    junk = tmp_path / "notes.txt"
    junk.write_text("nope")
    result = ingest_dropped_scan_paths([pdf, junk])
    assert result["ok"] == ["drop.pdf"]
    assert len(result["errors"]) == 1
    assert "notes.txt" in result["errors"][0]
