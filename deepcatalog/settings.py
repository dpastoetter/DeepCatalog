"""User-editable setup: source folder, categories, and batch parameters."""

from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from deepcatalog import config

SETTINGS_FILENAME = "settings.json"

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


class SettingsError(Exception):
    """Raised when settings.json exists but cannot be loaded or validated."""

    def __init__(self, message: str, *, path: Path | None = None):
        super().__init__(message)
        self.path = path


def settings_path() -> Path:
    return config.DATA_DIR / SETTINGS_FILENAME


def default_settings() -> dict[str, Any]:
    """Seed settings from current DATA_DIR layout and DOC_TYPES."""
    return {
        "source_dir": str(config.INBOX_DIR.resolve()),
        "categories": [
            {"name": name, "folder": str((config.ARCHIVE_DIR / name).resolve())}
            for name in config.DOC_TYPES
        ],
        "batch": {
            # How often (seconds) to scan the inbox for new files. 0 = manual only.
            "poll_interval_seconds": 30.0,
        },
        "review": {
            # Hold every proposed filing for human approval before writing.
            "require_approval": True,
        },
        "ocr": {
            # fast | balanced | maximum — see deepcatalog.ocr.resolve_ocr_mode
            "mode": "balanced",
            # Classical Tesseract before AI vision when the binary is available.
            "tesseract": True,
        },
    }


def _normalize_path(value: str) -> Path:
    raw = (value or "").strip()
    if not raw or "\x00" in raw:
        raise ValueError("invalid path")
    # Owner-configured storage root (authenticated Settings). Dangerous
    # prefixes are refused in refuse_dangerous_storage_path / validate_settings.
    return Path(raw).expanduser().resolve()  # codeql[py/path-injection]


# Exact home children that must never be used as inbox or category roots.
_FORBIDDEN_HOME_CHILDREN = frozenset(
    {
        "Documents",
        "Downloads",
        "Desktop",
        "Pictures",
        "Music",
        "Videos",
        "Library",
    }
)

# System prefixes that must never be configured as storage roots.
_FORBIDDEN_SYSTEM_PREFIXES = (
    Path("/etc"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/boot"),
    Path("/dev"),
    Path("/proc"),
    Path("/sys"),
    Path("/lib"),
    Path("/lib64"),
    Path("/run"),
)


def refuse_dangerous_storage_path(path: Path, *, label: str) -> None:
    """
    Raise ValueError when a settings path is a dangerous filesystem root.

    Category and inbox paths may live under home, but never *be* home, common
    user libraries, the project root, or system directories.
    """
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"{label}: cannot resolve path ({exc})") from exc

    if resolved == Path("/"):
        raise ValueError(f"{label}: refusing filesystem root /")

    home = Path.home().resolve()
    if resolved == home:
        raise ValueError(f"{label}: refusing home directory ({home})")
    if resolved.parent == home and resolved.name in _FORBIDDEN_HOME_CHILDREN:
        raise ValueError(f"{label}: refusing {resolved.name} under home")

    project = config.PROJECT_ROOT.resolve()
    if resolved == project:
        raise ValueError(f"{label}: refusing project root ({project})")

    data_dir = Path(config.DATA_DIR).expanduser().resolve()
    if resolved == data_dir:
        raise ValueError(
            f"{label}: refuse DATA_DIR itself — use DATA_DIR/inbox or a category subfolder"
        )

    for prefix in _FORBIDDEN_SYSTEM_PREFIXES:
        try:
            under = resolved == prefix or resolved.is_relative_to(prefix)
        except (OSError, ValueError):
            continue
        if under:
            raise ValueError(f"{label}: refusing system path under {prefix}")


def _normalize_category_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in cleaned)
    cleaned = cleaned.strip("_-")
    return cleaned or "other"


def validate_path(path: str) -> dict[str, Any]:
    """Return existence / directory info for a path string (for UI feedback)."""
    try:
        resolved = _normalize_path(path)
    except (OSError, ValueError):
        return {
            "status": "error",
            "path": path,
            "error": "cannot resolve path",
            "exists": False,
            "is_dir": False,
        }
    return {
        "status": "success",
        "path": str(resolved),
        "exists": resolved.exists(),
        "is_dir": resolved.is_dir(),
    }


def validate_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize and validate a settings payload.

    Raises ValueError with a human-readable message on invalid input.
    """
    if not isinstance(payload, dict):
        raise ValueError("settings must be an object")

    source_raw = payload.get("source_dir")
    if not isinstance(source_raw, str) or not source_raw.strip():
        raise ValueError("source_dir is required")
    source_dir = _normalize_path(source_raw)
    refuse_dangerous_storage_path(source_dir, label="source_dir")

    categories_raw = payload.get("categories")
    if not isinstance(categories_raw, list) or not categories_raw:
        raise ValueError("categories must be a non-empty list")

    categories: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in categories_raw:
        if not isinstance(item, dict):
            raise ValueError("each category must be an object with name and folder")
        name = _normalize_category_name(str(item.get("name") or ""))
        folder_raw = item.get("folder")
        if not isinstance(folder_raw, str) or not folder_raw.strip():
            raise ValueError(f"category '{name}' needs a folder path")
        folder = _normalize_path(folder_raw)
        refuse_dangerous_storage_path(folder, label=f"category '{name}' folder")
        if name in seen:
            raise ValueError(f"duplicate category name: {name}")
        seen.add(name)
        categories.append({"name": name, "folder": str(folder)})

    if "other" not in seen:
        raise ValueError("categories must include an 'other' entry")

    batch_raw = payload.get("batch") or {}
    if not isinstance(batch_raw, dict):
        raise ValueError("batch must be an object")

    # Prefer poll_interval_seconds; migrate older delay_seconds if present.
    raw_interval = batch_raw.get("poll_interval_seconds", batch_raw.get("delay_seconds", 30))
    if raw_interval is None:
        raise ValueError("batch.poll_interval_seconds must be a number")
    try:
        poll_interval_seconds = float(raw_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("batch.poll_interval_seconds must be a number") from exc
    if poll_interval_seconds < 0:
        raise ValueError("batch.poll_interval_seconds must be >= 0")

    review_raw = payload.get("review") or {}
    if not isinstance(review_raw, dict):
        raise ValueError("review must be an object")
    require_approval = bool(review_raw.get("require_approval", True))

    ocr_raw = payload.get("ocr") or {}
    if not isinstance(ocr_raw, dict):
        raise ValueError("ocr must be an object")
    mode = str(ocr_raw.get("mode") or "balanced").strip().lower()
    if mode not in {"fast", "balanced", "maximum"}:
        raise ValueError("ocr.mode must be one of: fast, balanced, maximum")
    tesseract = bool(ocr_raw.get("tesseract", True))

    return {
        "source_dir": str(source_dir),
        "categories": categories,
        "batch": {
            "poll_interval_seconds": poll_interval_seconds,
        },
        "review": {
            "require_approval": require_approval,
        },
        "ocr": {
            "mode": mode,
            "tesseract": tesseract,
        },
    }


def ensure_settings_dirs(settings: dict[str, Any]) -> None:
    """Create source and category folders from validated settings."""
    Path(settings["source_dir"]).mkdir(parents=True, exist_ok=True)
    for cat in settings["categories"]:
        Path(cat["folder"]).mkdir(parents=True, exist_ok=True)


def _read_existing_file(path: Path) -> dict[str, Any]:
    """Parse an existing settings.json; raise SettingsError on corruption."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(
            f"could not read settings file: {exc}",
            path=path,
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(
            f"settings.json is not valid JSON ({exc.msg} at line {exc.lineno})",
            path=path,
        ) from exc
    if not isinstance(data, dict):
        raise SettingsError(
            "settings.json must contain a JSON object",
            path=path,
        )
    return data


def _write_file(settings: dict[str, Any]) -> None:
    config.ensure_data_dirs()
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_settings(*, reload: bool = False) -> dict[str, Any]:
    """
    Load settings from disk (cached).

    Missing ``settings.json`` (first run) seeds defaults. A present but
    unreadable or invalid file raises ``SettingsError`` — it is never replaced
    with defaults, because that would silently retarget archive locations.
    """
    global _cache
    with _lock:
        if _cache is not None and not reload:
            return deepcopy(_cache)

        path = settings_path()
        if not path.exists():
            settings = default_settings()
            ensure_settings_dirs(settings)
            _write_file(settings)
            _cache = settings
            logger.info("Created default settings at %s", path)
            return deepcopy(_cache)

        raw = _read_existing_file(path)
        try:
            settings = validate_settings(raw)
        except ValueError as exc:
            raise SettingsError(
                f"settings.json is invalid: {exc}",
                path=path,
            ) from exc

        ensure_settings_dirs(settings)
        if "review" not in raw or "ocr" not in raw:
            _write_file(settings)
            logger.info("Migrated settings.json with missing review/ocr keys at %s", path)
        _cache = settings
        return deepcopy(_cache)


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate, persist, and cache settings. Returns the saved dict."""
    global _cache
    settings = validate_settings(payload)
    ensure_settings_dirs(settings)
    with _lock:
        _write_file(settings)
        _cache = settings
        return deepcopy(_cache)


def clear_settings_cache() -> None:
    """Drop in-memory cache (tests / after external edits)."""
    global _cache
    with _lock:
        _cache = None


def get_source_dir() -> Path:
    return Path(load_settings()["source_dir"])


def get_category_names() -> tuple[str, ...]:
    return tuple(c["name"] for c in load_settings()["categories"])


def get_folder_for_category(name: str) -> Path:
    """Resolve archive folder for a category; fall back to 'other' then ARCHIVE_DIR/other."""
    settings = load_settings()
    key = _normalize_category_name(name)
    by_name = {c["name"]: Path(c["folder"]) for c in settings["categories"]}
    if key in by_name:
        return by_name[key]
    if "other" in by_name:
        return by_name["other"]
    return config.ARCHIVE_DIR / "other"


def get_batch_settings() -> dict[str, Any]:
    return deepcopy(load_settings()["batch"])


def review_approval_required() -> bool:
    """Whether proposed filings must be approved by a human before writing."""
    return bool(load_settings().get("review", {}).get("require_approval", True))
