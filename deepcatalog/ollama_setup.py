"""Ollama detection, model checks, .env persistence, and runtime provider switch."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from deepcatalog import config
from deepcatalog.env_permissions import write_secret_text
from deepcatalog.ollama_url import (
    ALLOW_REMOTE_OLLAMA_ENV,
    DEFAULT_LOCAL_OLLAMA_URL,
    allow_remote_ollama_enabled,
    apply_openai_compat_env_for_ollama,
    is_loopback_ollama_url,
    ollama_client,
    public_ollama_config_error,
    trusted_ollama_origin,
)

logger = logging.getLogger(__name__)


def _ollama_client(*, origin: str, timeout: float) -> httpx.Client:
    """HTTP client for Ollama — never follow redirects (SSRF); DNS pinned per connect."""
    return ollama_client(origin=origin, timeout=timeout)


def resolved_ollama_base_url(
    base_url: str | None = None,
    *,
    allow_remote: bool = False,
) -> str:
    """Validate caller or configured Ollama URL before any outbound request."""
    return trusted_ollama_origin(
        base_url or config.OLLAMA_BASE_URL or DEFAULT_LOCAL_OLLAMA_URL,
        allow_remote=allow_remote or allow_remote_ollama_enabled(),
    )


DEFAULT_CHAT_MODEL = "gemma3"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
PROBE_TIMEOUT = 0.6
READY_TIMEOUT = 2.0
READY_CACHE_TTL = 5.0
PULL_TIMEOUT = 600.0
START_WAIT_TIMEOUT = 25.0
START_POLL_INTERVAL = 0.4
OLLAMA_CHAT_TIMEOUT = float(os.getenv("DEEPCATALOG_OLLAMA_TIMEOUT", "300"))
_TAGS_CACHE_TTL = 30.0

_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# (expires_at, base_url, models)
_tags_cache: tuple[float, str, list[str]] | None = None
# (expires_at, base_url, error_message_or_None, status_snapshot)
_ready_cache: tuple[float, str, str | None, dict[str, Any]] | None = None


def env_path() -> Path:
    """Persist provider settings next to user data when the install is read-only."""
    data_env = Path(config.DATA_DIR).expanduser() / ".env"
    if config.running_as_appimage():
        return data_env
    project_env = Path(config.PROJECT_ROOT) / ".env"
    try:
        writable = os.access(config.PROJECT_ROOT, os.W_OK)
    except OSError:
        writable = False
    if not writable:
        return data_env
    return project_env


def model_name_matches(installed: str, wanted: str) -> bool:
    """True if an Ollama tag satisfies the configured model name."""
    inst = (installed or "").strip().lower()
    want = (wanted or "").strip().lower()
    if not inst or not want:
        return False
    if inst == want:
        return True
    if inst.startswith(f"{want}:"):
        return True
    return inst.split(":", 1)[0] == want.split(":", 1)[0]


def tags_include(models: list[str], wanted: str) -> bool:
    return any(model_name_matches(name, wanted) for name in models)


def resolve_installed_model(wanted: str, installed: list[str]) -> str | None:
    """
    Map a configured name (e.g. gemma3) to an installed Ollama tag (e.g. gemma3:4b).

    Ollama does not always alias bare names to size-tagged variants, so API calls
    must use a concrete local tag.
    """
    want = (wanted or "").strip()
    if not want:
        return None
    matches = [name for name in installed if model_name_matches(name, want)]
    if not matches:
        return None

    want_l = want.lower()

    def rank(name: str) -> tuple[int, int, str]:
        lower = name.lower()
        if lower == want_l:
            return (0, 0, lower)
        if lower == f"{want_l}:latest":
            return (1, 0, lower)
        # Prefer shorter concrete tags (gemma3:4b before gemma3:27b).
        return (2, len(lower), lower)

    return sorted(matches, key=rank)[0]


def clear_ollama_tags_cache() -> None:
    global _tags_cache, _ready_cache
    _tags_cache = None
    _ready_cache = None


def list_installed_models(base_url: str | None = None) -> list[str]:
    """Cached list of local Ollama model tags."""
    global _tags_cache
    try:
        url = resolved_ollama_base_url(base_url)
    except ValueError:
        return []
    now = time.monotonic()
    if _tags_cache is not None:
        expires_at, cached_url, models = _tags_cache
        if cached_url == url and now < expires_at:
            return list(models)
    probe = probe_ollama(url)
    models = list(probe.get("models") or []) if probe.get("reachable") else []
    _tags_cache = (now + _TAGS_CACHE_TTL, url, models)
    return list(models)


def resolve_runtime_model(wanted: str, *, base_url: str | None = None) -> str:
    """Resolve configured model to an installed tag; fall back to the configured name."""
    installed = list_installed_models(base_url)
    return resolve_installed_model(wanted, installed) or (wanted or "").strip()


def _connect_error_validation_message(exc: BaseException) -> str | None:
    """
    Map DNS-pin / peer-validation failures that were remapped to ConnectError.

    httpcore often does ``raise ConnectError(...) from None``, so ``__cause__``
    may be missing; walk the chain and inspect the message text.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValueError):
            return public_ollama_config_error(current)
        raw = str(current)
        if any(
            token in raw
            for token in (
                "blocked address",
                "link-local",
                "non-loopback",
                "not allowed",
                "allowlist",
                "ALLOWED_HOSTS",
                "did not expose a peer",
                "peer is not an IP",
            )
        ):
            return public_ollama_config_error(ValueError(raw))
        current = current.__cause__ or current.__context__
    return None


def probe_ollama(base_url: str | None = None, *, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    """
    Probe an Ollama server.

    Returns a status dict; never raises for connectivity failures.
    """
    result: dict[str, Any] = {
        "reachable": False,
        "listening": False,
        "base_url": (base_url or config.OLLAMA_BASE_URL or DEFAULT_LOCAL_OLLAMA_URL).rstrip("/"),
        "models": [],
        "version": None,
        "error": None,
        "is_local": True,
    }
    try:
        url = resolved_ollama_base_url(base_url)
    except ValueError:
        result["error"] = "Invalid Ollama URL"
        result["is_local"] = is_loopback_ollama_url(result["base_url"])
        return result

    result["base_url"] = url
    result["is_local"] = is_loopback_ollama_url(url)
    try:
        with _ollama_client(origin=url, timeout=timeout) as client:
            tags_resp = client.get("/api/tags")
            tags_resp.raise_for_status()
            payload = tags_resp.json()
            models = [
                str(item.get("name") or "")
                for item in (payload.get("models") or [])
                if isinstance(item, dict) and item.get("name")
            ]
            result["models"] = models
            result["reachable"] = True
            # /api/tags succeeding means the daemon accepted an HTTP request.
            result["listening"] = True
            try:
                ver_resp = client.get("/api/version")
                if ver_resp.is_success:
                    result["version"] = (ver_resp.json() or {}).get("version")
            except (httpx.HTTPError, ValueError):
                pass
    except httpx.ConnectError as exc:
        validation = _connect_error_validation_message(exc)
        result["error"] = validation or "Cannot reach Ollama — is `ollama serve` running?"
    except httpx.HTTPError:
        result["error"] = "Ollama returned an HTTP error"
    except ValueError:
        result["error"] = "Unexpected Ollama response"
    return result


def ollama_reachable(base_url: str | None = None, *, timeout: float = PROBE_TIMEOUT) -> bool:
    return bool(probe_ollama(base_url, timeout=timeout).get("reachable"))


def infer_model_processor(entry: dict[str, Any]) -> str:
    """Return ``gpu`` or ``cpu`` for one ``/api/ps`` model entry."""
    vram = entry.get("size_vram")
    try:
        if vram is not None and int(vram) > 0:
            return "gpu"
    except (TypeError, ValueError):
        pass
    return "cpu"


def summarize_compute(running: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize CPU/GPU use from Ollama ``/api/ps`` model entries."""
    if not running:
        return {
            "compute": "idle",
            "compute_label": "idle",
            "size_vram": 0,
            "running": [],
        }

    processors = [infer_model_processor(model) for model in running]
    total_vram = 0
    for model in running:
        try:
            total_vram += int(model.get("size_vram") or 0)
        except (TypeError, ValueError):
            continue

    if all(processor == "gpu" for processor in processors):
        compute = "gpu"
        label = "GPU"
    elif all(processor == "cpu" for processor in processors):
        compute = "cpu"
        label = "CPU"
    else:
        compute = "mixed"
        label = "CPU + GPU"

    return {
        "compute": compute,
        "compute_label": label,
        "size_vram": total_vram,
        "running": [
            {
                "name": model.get("name"),
                "processor": infer_model_processor(model),
                "size_vram": int(model.get("size_vram") or 0),
            }
            for model in running
        ],
    }


def _fetch_ps_models(
    base_url: str | None = None,
    *,
    timeout: float = READY_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return raw model entries from Ollama ``GET /api/ps``."""
    url = resolved_ollama_base_url(base_url)
    with _ollama_client(origin=url, timeout=timeout) as client:
        resp = client.get("/api/ps")
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, dict)]


def fetch_running_ps_models(
    base_url: str | None = None,
    *,
    timeout: float = READY_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return ``/api/ps`` models, or an empty list when Ollama is unreachable."""
    try:
        return _fetch_ps_models(base_url, timeout=timeout)
    except httpx.HTTPError:
        return []


def current_compute_label(*, base_url: str | None = None) -> str | None:
    """Human label for the active Ollama processor (``CPU``/``GPU``), or ``None`` when idle."""
    summary = summarize_compute(fetch_running_ps_models(base_url))
    label = summary.get("compute_label")
    if label in {None, "", "idle"}:
        return None
    return str(label)


def verify_model_accepts_requests(
    model: str,
    *,
    base_url: str | None = None,
    timeout: float = READY_TIMEOUT,
) -> str | None:
    """
    Confirm Ollama will accept work for ``model`` via ``/api/show``.

    Returns an error string on failure, or None when the model is available.
    Does not load weights into VRAM (unlike a generate/chat round-trip).
    """
    name = (model or "").strip()
    if not name:
        return "No Ollama model configured"
    try:
        url = resolved_ollama_base_url(base_url)
    except ValueError:
        return "Invalid Ollama URL"
    try:
        with _ollama_client(origin=url, timeout=timeout) as client:
            resp = client.post("/api/show", json={"name": name})
            if resp.status_code == 404:
                return f"Ollama model '{name}' is not available locally. Run: ollama pull {name}"
            resp.raise_for_status()
    except httpx.ConnectError:
        return "Cannot reach Ollama — is `ollama serve` running?"
    except httpx.HTTPError as exc:
        return format_http_error(exc, model=name, kind="model")
    except ValueError:
        return f"Unexpected Ollama response for model '{name}'"
    return None


def ensure_ollama_ready(
    *,
    base_url: str | None = None,
    require_models: bool = True,
    verify_chat: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """
    Fail fast when the active provider is Ollama but the local daemon is not usable.

    Checks that Ollama is reachable/listening, required models are installed, and
    (by default) that the chat model is accepted by ``/api/show``. Results are
    cached briefly so ingest/RAG bursts do not re-probe on every call.

    Raises RuntimeError with an actionable message when not ready.
    """
    global _ready_cache
    if config.LLM_PROVIDER != "ollama":
        return {"skipped": True, "provider": config.LLM_PROVIDER}

    try:
        url = resolved_ollama_base_url(base_url)
    except ValueError as exc:
        raise RuntimeError("Invalid Ollama URL") from exc
    now = time.monotonic()
    if not force and _ready_cache is not None:
        expires_at, cached_url, cached_error, snapshot = _ready_cache
        if cached_url == url and now < expires_at:
            if cached_error:
                raise RuntimeError(cached_error)
            return dict(snapshot)

    status = ollama_status(base_url=url)
    error: str | None = None
    if not status.get("reachable") or not status.get("listening"):
        error = status.get("error") or "Cannot reach Ollama — is `ollama serve` running?"
    elif require_models and status.get("missing_models"):
        missing = status["missing_models"]
        hint = status.get("pull_command") or pull_hint(missing)
        error = (
            f"Ollama is running but required models are missing: {', '.join(missing)}. {hint}"
        ).strip()
    elif verify_chat:
        chat = status.get("resolved_chat_model") or status.get("chat_model") or ""
        error = verify_model_accepts_requests(str(chat), base_url=url)

    snapshot = {
        "ready": error is None,
        "reachable": bool(status.get("reachable")),
        "listening": bool(status.get("listening")),
        "base_url": url,
        "chat_model": status.get("resolved_chat_model") or status.get("chat_model"),
        "embedding_model": status.get("resolved_embedding_model") or status.get("embedding_model"),
        "version": status.get("version"),
        "error": error,
    }
    _ready_cache = (now + READY_CACHE_TTL, url, error, snapshot)
    if error:
        raise RuntimeError(error)
    return snapshot


def required_models(
    *,
    chat_model: str | None = None,
    embed_model: str | None = None,
) -> tuple[str, str]:
    chat = (chat_model or config.MODEL_NAME or DEFAULT_CHAT_MODEL).strip() or DEFAULT_CHAT_MODEL
    embed = (
        embed_model or config.EMBEDDING_MODEL or DEFAULT_EMBED_MODEL
    ).strip() or DEFAULT_EMBED_MODEL
    # If leftover cloud model names are still in config, fall back to Ollama defaults.
    if chat.startswith(("gpt-", "gemini", "text-embedding")):
        chat = DEFAULT_CHAT_MODEL
    if embed.startswith(("text-embedding", "gemini")):
        embed = DEFAULT_EMBED_MODEL
    return chat, embed


def missing_models(installed: list[str], *, chat: str, embed: str) -> list[str]:
    missing: list[str] = []
    if not tags_include(installed, chat):
        missing.append(chat)
    if embed != chat and not tags_include(installed, embed):
        missing.append(embed)
    return missing


def pull_hint(models: list[str]) -> str:
    if not models:
        return ""
    return " && ".join(f"ollama pull {name}" for name in models)


def format_http_error(exc: Exception, *, model: str | None = None, kind: str = "model") -> str:
    """Turn Ollama HTTP failures into actionable pull / serve hints."""
    text = str(exc)
    lower = text.lower()
    label = model or "ollama"
    if "not found" in lower or "404" in lower or "pull" in lower:
        return f"Ollama {kind} '{label}' is not available locally. Run: ollama pull {label}"
    return f"Ollama request failed for {kind} '{label}'"


def upsert_env_values(updates: dict[str, str], path: Path | None = None) -> Path:
    """Create or update keys in `.env` without dropping unrelated lines/comments."""
    target = path or env_path()
    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        match = (
            _ENV_KEY_RE.match(line.strip())
            if line.strip() and not line.lstrip().startswith("#")
            else None
        )
        if not match:
            out.append(line)
            continue
        key = match.group(1)
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    target.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    # Same 0600 posture as ~/.codex/auth.json — .env may hold DEEPCATALOG_API_TOKEN.
    write_secret_text(target, text)
    return target


def apply_llm_provider(
    provider: str,
    *,
    model: str | None = None,
    embedding_model: str | None = None,
    base_url: str | None = None,
    allow_remote: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Switch the active LLM provider at runtime and optionally persist to `.env`.

    Updates `deepcatalog.config` globals used throughout the process.
    Local Ollama is loopback-only; remote Ollama requires ``allow_remote`` (or
    ``DEEPCATALOG_ALLOW_REMOTE_OLLAMA``) and is validated against SSRF rules.
    """
    normalized = (provider or "").strip().lower()
    aliases = {
        "google": "gemini",
        "codex": "openai",
        "local": "ollama",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"openai", "gemini", "ollama"}:
        raise ValueError("provider must be one of: openai, gemini, ollama")

    remote_enabled = False
    if normalized == "ollama":
        chat, embed = required_models(chat_model=model, embed_model=embedding_model)
        candidate = (
            base_url
            if base_url is not None
            else (config.OLLAMA_BASE_URL or DEFAULT_LOCAL_OLLAMA_URL)
        )
        remote_requested = allow_remote or (
            base_url is None
            and allow_remote_ollama_enabled()
            and not is_loopback_ollama_url(candidate)
        )
        url = trusted_ollama_origin(candidate, allow_remote=remote_requested)
        remote_enabled = not is_loopback_ollama_url(url)
        if remote_enabled and not (allow_remote or allow_remote_ollama_enabled()):
            raise ValueError(
                "Remote Ollama requires allow_remote=true (and the privacy disclaimer) "
                f"or {ALLOW_REMOTE_OLLAMA_ENV}=1."
            )
    elif normalized == "openai":
        chat = (model or "gpt-5.6-luna").strip() or "gpt-5.6-luna"
        embed = (embedding_model or "text-embedding-3-small").strip() or "text-embedding-3-small"
        url = config.OLLAMA_BASE_URL
    else:
        chat = (model or "gemini-flash-latest").strip() or "gemini-flash-latest"
        embed = (embedding_model or "text-embedding-004").strip() or "text-embedding-004"
        url = config.OLLAMA_BASE_URL

    config.LLM_PROVIDER = normalized
    config.MODEL_NAME = chat
    config.EMBEDDING_MODEL = embed
    if normalized == "ollama":
        config.OLLAMA_BASE_URL = url

    os.environ["DEEPCATALOG_LLM_PROVIDER"] = normalized
    os.environ["DEEPCATALOG_MODEL"] = chat
    os.environ["DEEPCATALOG_EMBEDDING_MODEL"] = embed
    if normalized == "ollama":
        os.environ["OLLAMA_BASE_URL"] = url
        if remote_enabled:
            os.environ[ALLOW_REMOTE_OLLAMA_ENV] = "1"
        else:
            os.environ.pop(ALLOW_REMOTE_OLLAMA_ENV, None)
        apply_openai_compat_env_for_ollama(url, allow_remote=remote_enabled)
    else:
        # Clear leftovers from Ollama's OpenAI-compatible endpoint.
        base = os.environ.get("OPENAI_BASE_URL", "")
        if "11434" in base or "ollama" in base.lower():
            os.environ.pop("OPENAI_BASE_URL", None)
        if os.environ.get("OPENAI_API_KEY") == "ollama":
            os.environ.pop("OPENAI_API_KEY", None)

    if persist:
        updates = {
            "DEEPCATALOG_LLM_PROVIDER": normalized,
            "DEEPCATALOG_MODEL": chat,
            "DEEPCATALOG_EMBEDDING_MODEL": embed,
        }
        if normalized == "ollama":
            updates["OLLAMA_BASE_URL"] = url
            updates[ALLOW_REMOTE_OLLAMA_ENV] = "1" if remote_enabled else "0"
        upsert_env_values(updates)

    try:
        from deepcatalog.tools.rag_index import mark_index_stale

        mark_index_stale(f"LLM provider switched to {normalized} (embed model {embed})")
    except Exception:  # noqa: BLE001 — never block provider switch on index meta
        logger.exception("Could not mark RAG index stale after provider switch")

    return {
        "provider": normalized,
        "model": chat,
        "embedding_model": embed,
        "ollama_base_url": config.OLLAMA_BASE_URL,
        "ollama_is_local": (
            is_loopback_ollama_url(config.OLLAMA_BASE_URL) if normalized == "ollama" else True
        ),
        "persisted": persist,
    }


def ollama_status(*, base_url: str | None = None) -> dict[str, Any]:
    """Full status payload for Settings UI / diagnostics enrichment."""
    try:
        url = resolved_ollama_base_url(base_url)
    except ValueError:
        chat, embed = required_models()
        raw = (base_url or config.OLLAMA_BASE_URL or DEFAULT_LOCAL_OLLAMA_URL).rstrip("/")
        return {
            "active": config.LLM_PROVIDER == "ollama",
            "ready": False,
            "reachable": False,
            "listening": False,
            "can_start": False,
            "binary": find_ollama_binary(),
            "base_url": raw,
            "is_local": is_loopback_ollama_url(raw),
            "version": None,
            "installed_models": [],
            "chat_model": chat,
            "embedding_model": embed,
            "resolved_chat_model": None,
            "resolved_embedding_model": None,
            "missing_models": [chat, embed],
            "pull_command": None,
            "compute": "idle",
            "compute_label": "idle",
            "size_vram": 0,
            "running_models": [],
            "error": "Invalid Ollama URL",
            "install_hint": None,
        }

    chat, embed = required_models()
    probe = probe_ollama(url)
    installed = list(probe.get("models") or [])
    # Keep the tags cache warm for subsequent chat/embed calls.
    global _tags_cache
    if probe.get("reachable"):
        _tags_cache = (time.monotonic() + _TAGS_CACHE_TTL, url, installed)
    missing = (
        missing_models(installed, chat=chat, embed=embed) if probe["reachable"] else [chat, embed]
    )
    resolved_chat = resolve_installed_model(chat, installed)
    resolved_embed = resolve_installed_model(embed, installed)
    active = config.LLM_PROVIDER == "ollama"
    listening = bool(probe.get("listening") or probe.get("reachable"))
    ready = bool(listening and not missing and active)
    binary = find_ollama_binary()
    can_start = binary is not None and not listening and bool(probe.get("is_local", True))
    compute = (
        summarize_compute(fetch_running_ps_models(url))
        if probe["reachable"]
        else summarize_compute([])
    )
    return {
        "active": active,
        "ready": ready,
        "reachable": probe["reachable"],
        "listening": listening,
        "can_start": can_start,
        "binary": binary,
        "base_url": url,
        "is_local": bool(probe.get("is_local", is_loopback_ollama_url(url))),
        "version": probe.get("version"),
        "installed_models": installed,
        "chat_model": chat,
        "embedding_model": embed,
        "resolved_chat_model": resolved_chat,
        "resolved_embedding_model": resolved_embed,
        "missing_models": missing,
        "pull_command": pull_hint(missing),
        "compute": compute["compute"],
        "compute_label": compute["compute_label"],
        "size_vram": compute["size_vram"],
        "running_models": compute["running"],
        "error": probe.get("error"),
        "install_hint": (
            (
                "Ollama is installed — click Start Ollama, or run `ollama serve` in a terminal."
                if binary
                else "Install Ollama from https://ollama.com/download then run `ollama serve`."
            )
            if not probe["reachable"] and probe.get("is_local", True)
            else None
        ),
    }


def _host_path_candidates() -> list[Path]:
    """Well-known install locations for the official Ollama Linux binary."""
    home = Path.home()
    return [
        home / ".local" / "bin" / "ollama",
        Path("/usr/local/bin/ollama"),
        Path("/usr/bin/ollama"),
    ]


def find_ollama_binary() -> str | None:
    """Return the absolute path to the `ollama` CLI if installed.

    Desktop AppImage launches often omit ``~/.local/bin`` from PATH (where the
    official installer places ``ollama``), so we also check well-known paths.
    """
    found = shutil.which("ollama")
    if found:
        return found
    for candidate in _host_path_candidates():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def host_subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """
    Environment for host tools (ollama, systemctl) launched from an AppImage.

    AppRun puts Ubuntu WebKit libs on ``LD_LIBRARY_PATH``. Spawning the system
    ``ollama`` binary with that path often fails on Debian/Fedora (wrong
    libssl/libstdc++). Strip AppImage mount paths and ensure ``~/.local/bin``
    is searchable.
    """
    env = dict(os.environ if base is None else base)
    appdir = (env.get("APPDIR") or "").rstrip("/")
    strip_keys = (
        "LD_PRELOAD",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "GI_TYPELIB_PATH",
        "GIO_MODULE_DIR",
        "GDK_PIXBUF_MODULEDIR",
        "GDK_PIXBUF_MODULE_FILE",
        "GSETTINGS_SCHEMA_DIR",
        "GSETTINGS_BACKEND",
        "GTK_DATA_PREFIX",
        "GTK_THEME",
        "WEBKIT_EXEC_PATH",
        "WEBKIT_INJECTED_BUNDLE_PATH",
        "WEBKIT_FORCE_SANDBOX",
        "WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS",
        "WEBKIT_DISABLE_COMPOSITING_MODE",
        "WEBKIT_DISABLE_DMABUF_RENDERER",
    )
    for key in strip_keys:
        env.pop(key, None)

    def _keep_path_entry(entry: str) -> bool:
        if not entry:
            return False
        if appdir and (entry == appdir or entry.startswith(appdir + "/")):
            return False
        if "deepcatalog-webkit" in entry:
            return False
        if "/tmp/.mount_" in entry or entry.startswith("/tmp/.mount"):
            return False
        return True

    for path_key in ("LD_LIBRARY_PATH", "PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS"):
        raw = env.get(path_key, "")
        if not raw:
            if path_key == "LD_LIBRARY_PATH":
                env.pop(path_key, None)
            continue
        kept = [part for part in raw.split(":") if _keep_path_entry(part)]
        if path_key == "LD_LIBRARY_PATH":
            if kept:
                env[path_key] = ":".join(kept)
            else:
                env.pop(path_key, None)
        else:
            env[path_key] = ":".join(kept)

    # Official installer; desktop .desktop launches often omit this directory.
    home = Path(env.get("HOME") or Path.home()).expanduser()
    local_bin = str(home / ".local" / "bin")
    path = env.get("PATH", "")
    path_parts = [part for part in path.split(":") if part]
    if local_bin not in path_parts:
        env["PATH"] = f"{local_bin}:{path}" if path else local_bin

    # Prefer Ollama's own shared libs over any leftover AppImage entries.
    ollama_lib = home / ".local" / "lib" / "ollama"
    if ollama_lib.is_dir():
        existing = env.get("LD_LIBRARY_PATH", "")
        prefix = str(ollama_lib)
        env["LD_LIBRARY_PATH"] = f"{prefix}:{existing}" if existing else prefix

    return env


def _try_systemctl_start() -> str | None:
    """Start ollama.service via systemd when available. Returns the method label."""
    candidates = (
        ["systemctl", "--user", "start", "ollama.service"],
        ["systemctl", "start", "ollama.service"],
    )
    env = host_subprocess_env()
    for args in candidates:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=env,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if completed.returncode == 0:
            return " ".join(args)
    return None


def _spawn_ollama_serve(binary: str) -> None:
    """Detach `ollama serve` so it keeps running after the HTTP request ends."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
        "env": host_subprocess_env(),
    }
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    subprocess.Popen([binary, "serve"], **kwargs)  # noqa: S603


def start_ollama(
    *,
    base_url: str | None = None,
    wait_timeout: float = START_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """
    Start the local Ollama daemon from the GUI when it is not reachable.

    Prefers systemd (`ollama.service`), then falls back to a detached
    `ollama serve`. Only invokes the known `ollama` binary / unit — no
    caller-supplied commands.
    """
    url = resolved_ollama_base_url(base_url)
    if not is_loopback_ollama_url(url):
        raise RuntimeError(
            "Start/restart only works for local Ollama on this machine. "
            "Remote Ollama must be started on the remote host."
        )
    clear_ollama_tags_cache()
    status = ollama_status(base_url=url)
    if status.get("reachable"):
        return {
            "status": "success",
            "started": False,
            "already_running": True,
            "method": None,
            "ollama": status,
        }

    binary = find_ollama_binary()
    if not binary:
        raise RuntimeError(
            "Ollama is not installed (or not on PATH). "
            "Install from https://ollama.com/download then try again."
        )

    method = _try_systemctl_start()
    if not method:
        try:
            _spawn_ollama_serve(binary)
        except OSError as exc:
            raise RuntimeError(f"Failed to start `{binary} serve`: {exc}") from exc
        method = f"{binary} serve"

    deadline = time.monotonic() + max(1.0, float(wait_timeout))
    while time.monotonic() < deadline:
        clear_ollama_tags_cache()
        if probe_ollama(url).get("reachable"):
            return {
                "status": "success",
                "started": True,
                "already_running": False,
                "method": method,
                "ollama": ollama_status(base_url=url),
            }
        time.sleep(START_POLL_INTERVAL)

    raise RuntimeError(
        f"Started Ollama via `{method}` but it did not become reachable at {url} "
        f"within {wait_timeout:.0f}s. Check the Ollama service logs or run "
        "`ollama serve` in a terminal."
    )


def enable_ollama(
    *,
    base_url: str | None = None,
    chat_model: str | None = None,
    embedding_model: str | None = None,
    allow_remote: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    """Switch to Ollama (local loopback by default) and return fresh status."""
    applied = apply_llm_provider(
        "ollama",
        model=chat_model or DEFAULT_CHAT_MODEL,
        embedding_model=embedding_model or DEFAULT_EMBED_MODEL,
        base_url=base_url
        if base_url is not None
        else (None if allow_remote else DEFAULT_LOCAL_OLLAMA_URL),
        allow_remote=allow_remote,
        persist=persist,
    )
    status = ollama_status(base_url=applied["ollama_base_url"])
    return {"status": "success", "applied": applied, "ollama": status}


def pull_model(model: str, *, base_url: str | None = None) -> dict[str, Any]:
    """Pull a model via the Ollama HTTP API (blocking until finished)."""
    name = (model or "").strip()
    if not name:
        raise ValueError("model name is required")
    url = resolved_ollama_base_url(base_url)
    try:
        with _ollama_client(origin=url, timeout=PULL_TIMEOUT) as client:
            resp = client.post(
                "/api/pull",
                json={"name": name, "stream": False},
            )
            resp.raise_for_status()
            payload = resp.json() if resp.content else {}
    except httpx.ConnectError as exc:
        raise RuntimeError("Cannot reach Ollama — is `ollama serve` running?") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(format_http_error(exc, model=name, kind="pull")) from exc

    clear_ollama_tags_cache()
    status = ollama_status(base_url=url)
    return {
        "status": "success",
        "model": name,
        "pull": payload if isinstance(payload, dict) else {},
        "ollama": status,
    }


def list_running_models(base_url: str | None = None) -> dict[str, Any]:
    """Return models currently loaded in Ollama (GET /api/ps)."""
    url = resolved_ollama_base_url(base_url)
    try:
        models = _fetch_ps_models(url)
    except httpx.ConnectError as exc:
        raise RuntimeError("Cannot reach Ollama — is `ollama serve` running?") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(format_http_error(exc, kind="ps")) from exc
    compute = summarize_compute(models)
    return {
        "status": "success",
        "models": models,
        "compute": compute["compute"],
        "compute_label": compute["compute_label"],
        "size_vram": compute["size_vram"],
        "raw": {"models": models},
    }


def unload_model(
    model: str | None = None,
    *,
    base_url: str | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Unload chat and embedding models from VRAM (keep_alive=0)."""
    url = resolved_ollama_base_url(base_url)
    chat = resolve_runtime_model(model or config.MODEL_NAME, base_url=url)
    embed = resolve_runtime_model(
        embedding_model or config.EMBEDDING_MODEL,
        base_url=url,
    )
    unloaded: list[str] = []
    with _ollama_client(origin=url, timeout=OLLAMA_CHAT_TIMEOUT) as client:
        chat_payload = {
            "model": chat,
            "messages": [{"role": "user", "content": " "}],
            "stream": False,
            "keep_alive": 0,
        }
        try:
            resp = client.post("/api/chat", json=chat_payload)
            resp.raise_for_status()
            unloaded.append(chat)
        except httpx.HTTPError:
            pass
        embed_payload = {
            "model": embed,
            "input": " ",
            "keep_alive": 0,
        }
        try:
            resp = client.post("/api/embed", json=embed_payload)
            resp.raise_for_status()
            if embed not in unloaded:
                unloaded.append(embed)
        except httpx.HTTPError:
            pass
    clear_ollama_tags_cache()
    ps = list_running_models(base_url=url)
    return {
        "status": "success",
        "unloaded": unloaded,
        "running_models": ps.get("models") or [],
    }


def _try_systemctl_stop() -> str | None:
    candidates = (
        ["systemctl", "--user", "stop", "ollama.service"],
        ["systemctl", "stop", "ollama.service"],
    )
    env = host_subprocess_env()
    for args in candidates:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=env,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if completed.returncode == 0:
            return " ".join(args)
    return None


def _try_systemctl_restart() -> str | None:
    candidates = (
        ["systemctl", "--user", "restart", "ollama.service"],
        ["systemctl", "restart", "ollama.service"],
    )
    env = host_subprocess_env()
    for args in candidates:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=env,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if completed.returncode == 0:
            return " ".join(args)
    return None


def restart_ollama(
    *,
    base_url: str | None = None,
    wait_timeout: float = START_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """
    Restart the local Ollama daemon (systemd restart, or stop + start).
    """
    url = resolved_ollama_base_url(base_url)
    if not is_loopback_ollama_url(url):
        raise RuntimeError(
            "Restart only works for local Ollama on this machine. "
            "Remote Ollama must be restarted on the remote host."
        )
    clear_ollama_tags_cache()
    method = _try_systemctl_restart()
    if not method:
        stop_method = _try_systemctl_stop()
        time.sleep(0.8)
        if probe_ollama(url).get("reachable"):
            raise RuntimeError(
                "Ollama is still running and could not be stopped automatically. "
                "Run `systemctl restart ollama` or restart the Ollama app manually."
            )
        started = start_ollama(base_url=url, wait_timeout=wait_timeout)
        return {
            "status": "success",
            "restarted": True,
            "method": stop_method or "stop",
            "start": started,
            "ollama": started.get("ollama") or ollama_status(base_url=url),
        }

    deadline = time.monotonic() + max(1.0, float(wait_timeout))
    while time.monotonic() < deadline:
        clear_ollama_tags_cache()
        if probe_ollama(url).get("reachable"):
            return {
                "status": "success",
                "restarted": True,
                "method": method,
                "ollama": ollama_status(base_url=url),
            }
        time.sleep(START_POLL_INTERVAL)

    raise RuntimeError(
        f"Restarted Ollama via `{method}` but it did not become reachable at {url} "
        f"within {wait_timeout:.0f}s."
    )
