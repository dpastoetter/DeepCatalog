"""Tests for AppImage-related config helpers."""

from __future__ import annotations

from pathlib import Path

from deepcatalog.config import resolve_project_root, running_as_appimage

_REPO = Path(__file__).resolve().parent.parent


def test_resolve_project_root_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_PROJECT_ROOT", str(tmp_path))
    assert resolve_project_root() == tmp_path.resolve()


def test_running_as_appimage_flag(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("DEEPCATALOG_APPIMAGE", raising=False)
    assert running_as_appimage() is False
    monkeypatch.setenv("DEEPCATALOG_APPIMAGE", "1")
    assert running_as_appimage() is True


def test_running_as_appimage_runtime_path(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPCATALOG_APPIMAGE", raising=False)
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "DeepCatalog.AppImage"))
    assert running_as_appimage() is True


def test_apprun_sets_webkit_and_adwaita_env():
    text = (_REPO / "packaging" / "linux" / "AppRun").read_text(encoding="utf-8")
    assert "WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS" in text
    assert "WEBKIT_DISABLE_COMPOSITING_MODE" in text
    assert "WEBKIT_DISABLE_DMABUF_RENDERER" in text
    assert "Adwaita:dark" in text
    assert "DEEPCATALOG_GTK_DARK" in text
    assert "GTK_USE_PORTAL" in text
    assert "GSETTINGS_BACKEND" in text
    assert "usr/bin/gdk-pixbuf-query-loaders" in text
    assert "command -v gdk-pixbuf-query-loaders" not in text
    assert "XDG_CONFIG_DIRS" in text
    assert "export XDG_CONFIG_HOME=" not in text
    assert "/usr/bin/gsettings" in text
    assert "env -u LD_LIBRARY_PATH" in text
    # Color-scheme probe must run before WebKit LD_LIBRARY_PATH is exported.
    probe_at = text.index("org.gnome.desktop.interface color-scheme")
    ld_at = text.index('export LD_LIBRARY_PATH="${WEBKIT_DIR}')
    assert probe_at < ld_at
    assert "/tmp/.dc/x86_64-linux-gnu/webkit2gtk-4.1" in text
    assert "ln -sfn" in text


def test_apprun_gsettings_probe_uses_clean_ld_path(tmp_path):
    """Host gsettings under WebKit LD_LIBRARY_PATH must not decide the theme."""
    import os
    import subprocess

    script = tmp_path / "fake-gsettings"
    script.write_text(
        "#!/bin/sh\n"
        'if [ -n "${LD_LIBRARY_PATH:-}" ]; then echo "\'default\'"; else echo "\'prefer-dark\'"; fi\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    polluted_env = os.environ.copy()
    polluted_env["LD_LIBRARY_PATH"] = "/tmp/webkit"
    polluted = subprocess.check_output([str(script)], env=polluted_env, text=True).strip()
    clean = subprocess.check_output(
        ["env", "-u", "LD_LIBRARY_PATH", str(script)],
        text=True,
    ).strip()
    assert polluted == "'default'"
    assert clean == "'prefer-dark'"


def test_relocate_webkit_rewrites_libexec_prefix(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "relocate_webkit",
        _REPO / "scripts" / "relocate-webkit.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    lib = tmp_path / "libwebkit2gtk-4.1.so.0"
    payload = b"hdr" + mod.OLD_LIBEXEC + b"/injected-bundle/\0" + mod.OLD_LIBEXEC + b"\0tail"
    lib.write_bytes(payload)
    assert mod.relocate_webkit_library(lib) == 2
    patched = lib.read_bytes()
    assert mod.OLD_LIBEXEC not in patched
    assert mod.NEW_LIBEXEC in patched
    assert mod.NEW_LIBEXEC + b"/injected-bundle/" in patched
