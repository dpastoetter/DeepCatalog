"""Classical Tesseract OCR — fast local tier before AI vision.

Soft-fails when the ``tesseract`` binary or language data is missing so ingest
still falls through to multimodal vision.

Does **not** import Pillow/pypdf — page images arrive as PNG bytes from the
media worker boundary.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from functools import lru_cache
from typing import Any

from deepcatalog import config

logger = logging.getLogger(__name__)

_availability_lock = threading.Lock()
_availability_cached: bool | None = None


def reset_tesseract_availability_cache() -> None:
    """Clear the cached binary probe (tests)."""
    global _availability_cached
    with _availability_lock:
        _availability_cached = None
    resolve_tesseract_cmd.cache_clear()


@lru_cache(maxsize=1)
def resolve_tesseract_cmd() -> str | None:
    """Return the tesseract executable path, or None if not found."""
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in ("/usr/bin/tesseract", "/bin/tesseract"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ensure_tessdata_prefix() -> None:
    """Point TESSDATA_PREFIX at AppImage-bundled eng data when present."""
    if os.getenv("TESSDATA_PREFIX", "").strip():
        return
    appdir = os.getenv("APPDIR", "").strip()
    if not appdir:
        return
    bundled = os.path.join(appdir, "usr", "share", "tessdata")
    if os.path.isdir(bundled) and os.path.isfile(os.path.join(bundled, "eng.traineddata")):
        os.environ["TESSDATA_PREFIX"] = bundled


def tesseract_available() -> bool:
    """True when a tesseract binary is on PATH (cached)."""
    global _availability_cached
    with _availability_lock:
        if _availability_cached is not None:
            return _availability_cached
    cmd = resolve_tesseract_cmd()
    ok = cmd is not None
    if ok:
        ensure_tessdata_prefix()
    with _availability_lock:
        _availability_cached = ok
    return ok


def tesseract_enabled_by_env() -> bool:
    """Master env switch (settings.json can still disable)."""
    return bool(config.TESSERACT_ENABLED)


def tesseract_enabled() -> bool:
    """True when env + settings allow Tesseract and the binary is present."""
    if not tesseract_enabled_by_env():
        return False
    try:
        from deepcatalog.settings import load_settings

        settings_flag = (load_settings().get("ocr") or {}).get("tesseract", True)
        if settings_flag is False:
            return False
    except Exception:  # noqa: BLE001 — settings must not block OCR path
        pass
    return tesseract_available()


def ocr_png_bytes(
    png_bytes: bytes,
    *,
    lang: str | None = None,
    timeout: float | None = None,
    psm: int | None = None,
) -> str:
    """
    Run Tesseract on RGB PNG bytes. Returns empty string on any failure.

    Never raises for missing binary, bad tessdata, or timeouts — callers fall
    through to AI vision. Writes a temp PNG so we never import Pillow here.
    """
    if not png_bytes:
        return ""

    cmd = resolve_tesseract_cmd()
    if not cmd:
        return ""

    ensure_tessdata_prefix()
    use_lang = (lang or config.TESSERACT_LANG or "eng").strip() or "eng"
    use_timeout = float(timeout if timeout is not None else config.TESSERACT_TIMEOUT)
    use_psm = int(psm if psm is not None else config.TESSERACT_PSM)
    if use_timeout <= 0:
        use_timeout = 60.0
    if use_psm < 0 or use_psm > 13:
        use_psm = 3

    try:
        import pytesseract
    except ImportError:
        logger.warning("pytesseract not installed; skipping classical OCR")
        return ""

    tmp_path: str | None = None
    try:
        pytesseract.pytesseract.tesseract_cmd = cmd
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png_bytes)
            tmp_path = tmp.name
        config_args = f"--psm {use_psm}"
        text = pytesseract.image_to_string(
            tmp_path,
            lang=use_lang,
            config=config_args,
            timeout=use_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail to vision
        logger.info("Tesseract OCR failed (falling back to vision): %s", exc)
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return (text or "").strip()


def tesseract_status() -> dict[str, Any]:
    """Small status dict for diagnostics / Settings."""
    return {
        "enabled": tesseract_enabled_by_env(),
        "available": tesseract_available(),
        "cmd": resolve_tesseract_cmd(),
        "lang": config.TESSERACT_LANG,
        "active": tesseract_enabled(),
    }
