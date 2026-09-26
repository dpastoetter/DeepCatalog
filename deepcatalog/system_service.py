"""Install and manage a systemd user service for boot-time autostart."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from deepcatalog import config
from deepcatalog.config import running_as_appimage
from deepcatalog.local_security import (
    assert_bind_allowed,
    configured_bind_host,
    configured_bind_port,
    port_probe_host,
)

UNIT_NAME = "deepcatalog.service"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _run(
    args: list[str],
    *,
    timeout: float = 30.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _systemctl(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", "--user", *args], timeout=timeout)


def _loginctl(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return _run(["loginctl", *args], timeout=timeout)


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def venv_python() -> Path:
    return config.PROJECT_ROOT / ".venv" / "bin" / "python"


def uvicorn_binary() -> Path:
    return config.PROJECT_ROOT / ".venv" / "bin" / "uvicorn"


def appimage_exec_path() -> str | None:
    """Absolute path of the running AppImage, if this process was launched from one."""
    path = os.getenv("APPIMAGE", "").strip()
    if path and Path(path).is_file():
        return path
    return None


def service_host() -> str:
    return configured_bind_host()


def service_port() -> int:
    return configured_bind_port()


def service_url() -> str:
    return f"http://{service_host()}:{service_port()}"


def _systemd_available() -> bool:
    if not _is_linux():
        return False
    if not shutil.which("systemctl"):
        return False
    try:
        completed = _systemctl("status", timeout=5.0)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    # status exits non-zero when the queried unit is missing; that still means systemd works.
    return completed.returncode in {0, 3, 4}


def _unit_enabled() -> bool:
    try:
        completed = _systemctl("is-enabled", UNIT_NAME, timeout=5.0)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "enabled"


def _unit_active() -> bool:
    try:
        completed = _systemctl("is-active", UNIT_NAME, timeout=5.0)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "active"


def _linger_enabled() -> bool:
    if not shutil.which("loginctl"):
        return False
    try:
        completed = _loginctl(
            "show-user", os.getenv("USER") or "", "--property=Linger", timeout=5.0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    if completed.returncode != 0:
        return False
    for line in completed.stdout.splitlines():
        if line.startswith("Linger="):
            return line.split("=", 1)[1].strip().lower() == "yes"
    return False


def _managed_by_systemd() -> bool:
    return os.getenv("DEEPCATALOG_SYSTEMD", "").strip() == "1"


def _extra_environment_lines() -> list[str]:
    lines = [
        f"Environment=DATA_DIR={config.DATA_DIR}",
        "Environment=DEEPCATALOG_SYSTEMD=1",
    ]
    if running_as_appimage():
        lines.append("Environment=DEEPCATALOG_APPIMAGE=1")
        env_file = Path(config.DATA_DIR) / ".env"
    else:
        env_file = config.PROJECT_ROOT / ".env"
    if env_file.is_file():
        lines.append(f"EnvironmentFile=-{env_file}")
    ollama_lib = Path.home() / ".local" / "lib" / "ollama"
    if ollama_lib.is_dir():
        lines.append(f"Environment=LD_LIBRARY_PATH={ollama_lib}")
    return lines


def _strict_systemd_enabled() -> bool:
    """Opt-in tighter sandbox; may need DEEPCATALOG_SYSTEMD_READWRITE_PATHS."""
    return os.getenv("DEEPCATALOG_SYSTEMD_STRICT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _extra_readwrite_paths() -> list[str]:
    """Colon-separated absolute paths the strict profile must remain able to write."""
    raw = os.getenv("DEEPCATALOG_SYSTEMD_READWRITE_PATHS", "").strip()
    if not raw:
        return []
    paths: list[str] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        paths.append(str(Path(part).expanduser()))
    return paths


def _hardening_directives(*, appimage: bool, workdir: Path) -> list[str]:
    """Return systemd sandbox directives safe for default DeepCatalog installs.

    Enabled by default (why safe):
    - UMask=0077 — new files owner-only; matches DATA_DIR 0700/0600 policy
    - NoNewPrivileges=true — blocks privilege escalation; media_worker also sets
      PR_SET_NO_NEW_PRIVS in-process
    - PrivateTmp=true — private /tmp; media_worker already uses its own TMPDIR
    - ProtectControlGroups/KernelTunables/KernelModules/Hostname/Clock — service
      never manages cgroups, sysctls, modules, hostname, or the system clock
    - RestrictSUIDSGID=true — no SUID/SGID binaries needed for serve/Poppler
    - LockPersonality=true — no personality(2) changes
    - RestrictRealtime=true — no soft-RT scheduling
    - ProtectSystem=full — /usr and /boot read-only; home and DATA_DIR stay writable
      so user-configured inbox/archive paths under $HOME keep working
    - RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 — loopback UI, Ollama,
      HTTPS updates/integrations; no raw/netlink sockets

    Intentionally NOT enabled by default:
    - ProtectHome — would hide or freeze $HOME and break archive/inbox under home
    - PrivateDevices — AppImage needs /dev/fuse; Poppler does not need host devices
      beyond what PrivateDevices provides, but FUSE breaks headless AppImage start
    - RestrictNamespaces — would block media_worker's best-effort CLONE_NEWNET
    - SystemCallFilter / MemoryDenyWriteExecute — can break chromadb native code
      or Poppler; available only via DEEPCATALOG_SYSTEMD_STRICT=1
    - ProtectSystem=strict — needs explicit ReadWritePaths for every archive root
    """
    # ProtectSystem: default `full` keeps $HOME writable for user archive/inbox
    # roots. Strict mode switches to `strict` + explicit ReadWritePaths.
    protect_system = "strict" if _strict_systemd_enabled() else "full"
    lines = [
        "UMask=0077",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        f"ProtectSystem={protect_system}",
        "ProtectControlGroups=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectHostname=true",
        "ProtectClock=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "RestrictRealtime=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    ]
    if _strict_systemd_enabled():
        # Opt-in: tighter FS + optional device/W^X filters. Callers must list
        # every writable archive/inbox path in DEEPCATALOG_SYSTEMD_READWRITE_PATHS.
        rw: list[str] = [str(Path(config.DATA_DIR).expanduser())]
        root = Path(config.PROJECT_ROOT).expanduser()
        if str(root) not in rw:
            rw.append(str(root))
        wd = Path(workdir).expanduser()
        if str(wd) not in rw:
            rw.append(str(wd))
        for extra in _extra_readwrite_paths():
            if extra not in rw:
                rw.append(extra)
        lines.append(f"ReadWritePaths={' '.join(rw)}")
        # RestrictNamespaces is still omitted: media_worker netns is best-effort
        # and useful when the user has unprivileged user namespaces.
        lines.append("MemoryDenyWriteExecute=true")
        if not appimage:
            # AppImage runtime needs /dev/fuse; venv installs do not.
            lines.append("PrivateDevices=true")
        lines.append("SystemCallFilter=@system-service @network-io")
        lines.append("SystemCallErrorNumber=EPERM")
    return lines


def render_unit_file() -> str:
    """Render the systemd user unit for the current install."""
    appimage = appimage_exec_path()
    host = service_host()
    port = service_port()
    env_lines = "\n".join(_extra_environment_lines())
    if appimage:
        workdir = Path(config.DATA_DIR)
        exec_start = f"{appimage} --headless --host {host} --port {port}"
    else:
        workdir = Path(config.PROJECT_ROOT)
        python = venv_python()
        exec_start = f"{python} -m deepcatalog.serve --host {host} --port {port}"
    harden = "\n".join(_hardening_directives(appimage=bool(appimage), workdir=workdir))
    return f"""[Unit]
Description=DeepCatalog — deep document intelligence, local-first.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
{harden}
{env_lines}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _install_unit() -> None:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit_file(), encoding="utf-8")


def _daemon_reload() -> None:
    completed = _systemctl("daemon-reload")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or "systemctl --user daemon-reload failed")


def _enable_linger() -> None:
    if not shutil.which("loginctl"):
        return
    user = os.getenv("USER") or ""
    if not user:
        return
    completed = _loginctl("enable-linger", user)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or "loginctl enable-linger failed")


def _port_is_free(host: str, port: int) -> bool:
    probe = port_probe_host(host)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe, port))
        except OSError:
            return False
    return True


def autostart_status() -> dict[str, Any]:
    """Return autostart / systemd status for the Settings UI."""
    supported = _systemd_available()
    python = venv_python()
    status: dict[str, Any] = {
        "supported": supported,
        "platform": sys.platform,
        "unit_name": UNIT_NAME,
        "unit_path": str(unit_path()),
        "enabled": False,
        "active": False,
        "installed": unit_path().is_file(),
        "linger": _linger_enabled() if supported else False,
        "managed_by_service": _managed_by_systemd(),
        "host": service_host(),
        "port": service_port(),
        "url": service_url(),
        "project_root": str(config.PROJECT_ROOT),
        "uvicorn_path": str(uvicorn_binary()),
        "error": None,
        "install_hint": None,
    }
    if not supported:
        status["install_hint"] = "Autostart requires Linux with systemd user services."
        return status
    if running_as_appimage():
        if not appimage_exec_path():
            status["error"] = (
                "APPIMAGE path is missing; cannot install autostart from an extracted image."
            )
            status["install_hint"] = (
                "Launch DeepCatalog from the .AppImage file, then enable autostart."
            )
            return status
        status["enabled"] = _unit_enabled()
        status["active"] = _unit_active()
        return status
    if not python.is_file():
        status["error"] = (
            f"Virtualenv not found at {python}. "
            "Run the installer or create .venv before enabling autostart."
        )
        status["install_hint"] = (
            f"From {config.PROJECT_ROOT}: python3 -m venv .venv && "
            ".venv/bin/pip install -e . -c constraints.txt"
        )
        return status
    status["enabled"] = _unit_enabled()
    status["active"] = _unit_active()
    return status


def set_autostart(enabled: bool) -> dict[str, Any]:
    """Enable or disable the systemd user service for DeepCatalog."""
    status = autostart_status()
    if not status["supported"]:
        raise RuntimeError(
            status.get("install_hint") or "Autostart is not supported on this system."
        )
    if status.get("error"):
        raise RuntimeError(str(status["error"]))

    if enabled:
        assert_bind_allowed(service_host())
        _install_unit()
        _daemon_reload()
        completed = _systemctl("enable", UNIT_NAME)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or f"systemctl --user enable {UNIT_NAME} failed")
        _enable_linger()
        host = service_host()
        port = service_port()
        if not _unit_active():
            if _port_is_free(host, port):
                start_completed = _systemctl("start", UNIT_NAME)
                if start_completed.returncode != 0:
                    detail = (start_completed.stderr or start_completed.stdout or "").strip()
                    raise RuntimeError(detail or f"systemctl --user start {UNIT_NAME} failed")
            # If the port is already in use, leave the current instance running.
    else:
        completed = _systemctl("disable", UNIT_NAME)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or f"systemctl --user disable {UNIT_NAME} failed")

    return {"status": "success", "autostart": autostart_status()}
