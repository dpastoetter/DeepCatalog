"""Run untrusted PDF/image parse/render work in a resource-limited subprocess.

Architectural rule: **only this module** may import ``pypdf``, ``pdf2image``, or
``PIL`` for untrusted scan media. Other packages call the public helpers below.
When ``DEEPCATALOG_MEDIA_WORKER=0``, jobs still execute via ``_execute_job`` in
this module (same parser imports) so tests stay fast without weakening the
boundary.
"""

from __future__ import annotations

import io
import logging
import multiprocessing as mp
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepcatalog import config

logger = logging.getLogger(__name__)


class MediaWorkerError(RuntimeError):
    """Native media worker failed, timed out, or was killed by resource limits."""

    def __init__(self, message: str, *, code: str = "worker_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MediaWorkerLimits:
    timeout_s: float
    memory_bytes: int
    cpu_seconds: int


def _default_limits() -> MediaWorkerLimits:
    return MediaWorkerLimits(
        timeout_s=float(config.MEDIA_WORKER_TIMEOUT_S),
        memory_bytes=max(64 * 1024 * 1024, int(config.MEDIA_WORKER_MEMORY_MB) * 1024 * 1024),
        cpu_seconds=max(1, int(config.MEDIA_WORKER_CPU_S)),
    )


def media_worker_enabled() -> bool:
    """Allow tests / constrained environments to disable subprocess isolation."""
    return os.getenv("DEEPCATALOG_MEDIA_WORKER", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _apply_resource_limits(limits: MediaWorkerLimits) -> None:
    """Best-effort CPU/address-space caps (Linux/macOS; no-op when unsupported)."""
    try:
        import resource
    except ImportError:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 1))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = max(1, int(config.MEDIA_MAX_IMAGE_PIXELS))
    except Exception:  # noqa: BLE001
        pass


def _page_box_points(page: Any) -> tuple[float, float] | None:
    box = getattr(page, "mediabox", None) or getattr(page, "mediaBox", None)
    if box is None:
        return None
    try:
        width = float(box.width)
        height = float(box.height)
    except Exception:  # noqa: BLE001 — malformed box
        return None
    return width, height


def _validate_pdf_structure(path: str) -> dict[str, Any]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path, strict=False)
    except (PdfReadError, OSError, ValueError) as exc:
        raise MediaWorkerError(f"unparseable PDF: {exc}", code="unparseable") from exc

    if getattr(reader, "is_encrypted", False):
        unlocked = False
        try:
            unlocked = bool(reader.decrypt(""))
        except Exception:  # noqa: BLE001
            unlocked = False
        if not unlocked:
            raise MediaWorkerError("encrypted PDFs are not supported", code="encrypted")

    try:
        page_count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise MediaWorkerError(f"unparseable PDF pages: {exc}", code="unparseable") from exc

    max_pages = max(1, int(config.MEDIA_MAX_PDF_PAGES))
    if page_count <= 0:
        raise MediaWorkerError("PDF has no pages", code="empty")
    if page_count > max_pages:
        raise MediaWorkerError(
            f"PDF has too many pages ({page_count}; max {max_pages})",
            code="too_many_pages",
        )

    max_pts = float(config.MEDIA_MAX_PDF_PAGE_POINTS)
    for index, page in enumerate(reader.pages):
        size = _page_box_points(page)
        if size is None:
            continue
        width, height = size
        if width <= 0 or height <= 0:
            raise MediaWorkerError(
                f"PDF page {index + 1} has invalid MediaBox",
                code="bad_page_size",
            )
        if width > max_pts or height > max_pts:
            raise MediaWorkerError(
                f"PDF page {index + 1} MediaBox is too large "
                f"({width:.0f}×{height:.0f} pts; max {max_pts:.0f})",
                code="bad_page_size",
            )

    return {"kind": "pdf", "page_count": page_count}


def _validate_image_structure(path: str, expected_kind: str | None) -> dict[str, Any]:
    from PIL import Image, UnidentifiedImageError

    max_pixels = max(1, int(config.MEDIA_MAX_IMAGE_PIXELS))
    Image.MAX_IMAGE_PIXELS = max_pixels

    try:
        with Image.open(path) as img:
            width, height = img.size
            fmt = (img.format or "").lower() or None
            if expected_kind and fmt and fmt.lower() != expected_kind.lower():
                raise MediaWorkerError(
                    f"image format {fmt!r} does not match content type {expected_kind!r}",
                    code="type_mismatch",
                )
            if width <= 0 or height <= 0:
                raise MediaWorkerError("image has invalid dimensions", code="bad_image_size")
            pixels = width * height
            if pixels > max_pixels:
                raise MediaWorkerError(
                    f"image too large ({width}×{height} = {pixels} pixels; max {max_pixels})",
                    code="too_many_pixels",
                )
            img.load()
            kind = (fmt or expected_kind or "image").lower()
            return {"kind": kind, "width": width, "height": height, "pixels": pixels}
    except MediaWorkerError:
        raise
    except UnidentifiedImageError as exc:
        raise MediaWorkerError("unrecognized or corrupt image", code="unparseable") from exc
    except Image.DecompressionBombError as exc:
        raise MediaWorkerError(
            f"image exceeds pixel limit ({max_pixels})",
            code="too_many_pixels",
        ) from exc
    except OSError as exc:
        raise MediaWorkerError(f"unreadable image: {exc}", code="unparseable") from exc


def _extract_pdf_page_texts(path: str) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    return [(page.extract_text() or "").strip() for page in reader.pages]


def _render_pdf_page_png(path: str, page_index: int, dpi: int) -> bytes:
    from pdf2image import convert_from_path

    images = convert_from_path(
        path,
        dpi=dpi,
        first_page=page_index,
        last_page=page_index,
        fmt="png",
    )
    if not images:
        raise RuntimeError(f"failed to render page {page_index}")
    img = images[0]
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _load_image_rgb_png(path: str) -> bytes:
    from PIL import Image

    with Image.open(path) as img:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
        return buf.getvalue()


def prepare_png_for_vision(
    png_bytes: bytes,
    *,
    max_px: int,
    as_jpeg: bool,
) -> tuple[bytes, str]:
    """
    Downscale trusted RGB PNG bytes for multimodal OCR.

    Operates only on bytes already produced by this worker (not an untrusted path).
    """
    from PIL import Image

    max_edge = max(256, int(max_px))
    with Image.open(io.BytesIO(png_bytes)) as opened:
        img = opened.convert("RGB")
    width, height = img.size
    long_edge = max(width, height)
    if long_edge > max_edge:
        scale = max_edge / long_edge
        img = img.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    if as_jpeg:
        img.save(buf, format="JPEG", quality=80, optimize=True)
        return buf.getvalue(), "image/jpeg"
    img.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def _execute_job(job: str, payload: dict[str, Any]) -> Any:
    """Run a media job in-process (parsers stay confined to this module)."""
    if job == "extract_pdf_page_texts":
        return _extract_pdf_page_texts(str(payload["path"]))
    if job == "render_pdf_page":
        return _render_pdf_page_png(
            str(payload["path"]),
            int(payload["page_index"]),
            int(payload["dpi"]),
        )
    if job == "load_image_rgb_png":
        return _load_image_rgb_png(str(payload["path"]))
    if job == "validate_pdf_structure":
        return _validate_pdf_structure(str(payload["path"]))
    if job == "validate_image_structure":
        return _validate_image_structure(
            str(payload["path"]),
            payload.get("expected_kind"),
        )
    if job == "prepare_png_for_vision":
        data, mime = prepare_png_for_vision(
            bytes(payload["png"]),
            max_px=int(payload["max_px"]),
            as_jpeg=bool(payload["as_jpeg"]),
        )
        return {"bytes": data, "mime_type": mime}
    raise ValueError(f"unknown media worker job: {job}")


def _worker_main(job: str, payload: dict[str, Any], conn: Any, limits: MediaWorkerLimits) -> None:
    _apply_resource_limits(limits)
    try:
        result = _execute_job(job, payload)
        conn.send(("ok", result))
    except MediaWorkerError as exc:
        conn.send(("err", {"message": str(exc), "code": exc.code}))
    except Exception as exc:  # noqa: BLE001 — surface to parent
        conn.send(("err", {"message": f"{type(exc).__name__}: {exc}", "code": "worker_failed"}))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def run_media_job(
    job: str,
    payload: dict[str, Any],
    *,
    limits: MediaWorkerLimits | None = None,
) -> Any:
    """
    Execute ``job`` with CPU/memory/time limits when the worker is enabled.

    When the worker is disabled, runs ``_execute_job`` in-process so parser
    imports never leak to callers.
    """
    if not media_worker_enabled():
        try:
            return _execute_job(job, payload)
        except MediaWorkerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MediaWorkerError(f"{type(exc).__name__}: {exc}", code="worker_failed") from exc

    effective = limits or _default_limits()
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_worker_main,
        args=(job, payload, child_conn, effective),
        daemon=True,
    )
    proc.start()
    child_conn.close()

    deadline = time.monotonic() + effective.timeout_s
    result: tuple[str, Any] | None = None
    timed_out = False
    try:
        while result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if parent_conn.poll(min(0.25, remaining)):
                try:
                    result = parent_conn.recv()
                except EOFError:
                    break
                break
            if not proc.is_alive():
                if parent_conn.poll(0.5):
                    try:
                        result = parent_conn.recv()
                    except EOFError:
                        break
                break

        if result is None:
            if proc.is_alive() or timed_out:
                proc.terminate()
                proc.join(2.0)
                if proc.is_alive():
                    proc.kill()
                    proc.join(1.0)
                raise MediaWorkerError(
                    f"media worker timed out after {effective.timeout_s:.0f}s ({job})",
                    code="timeout",
                )
            exit_code = proc.exitcode
            raise MediaWorkerError(
                f"media worker exited without result (job={job}, exit={exit_code})",
                code="worker_failed",
            )
    finally:
        try:
            parent_conn.close()
        except OSError:
            pass
        if proc.is_alive():
            proc.join(5.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(2.0)

    status, body = result
    if status == "ok":
        return body
    if isinstance(body, dict):
        raise MediaWorkerError(
            str(body.get("message") or "media worker failed"),
            code=str(body.get("code") or "worker_failed"),
        )
    raise MediaWorkerError(str(body), code="worker_failed")


def extract_pdf_page_texts_isolated(path: Path | str) -> list[str]:
    """Extract text layers via the media worker boundary."""
    return list(
        run_media_job(
            "extract_pdf_page_texts",
            {"path": str(Path(path).resolve())},
        )
    )


def render_pdf_page_png_isolated(path: Path | str, page_index: int, *, dpi: int) -> bytes:
    """Rasterize one PDF page with Poppler inside the media worker boundary."""
    return bytes(
        run_media_job(
            "render_pdf_page",
            {
                "path": str(Path(path).resolve()),
                "page_index": int(page_index),
                "dpi": int(dpi),
            },
        )
    )


def load_image_rgb_png_isolated(path: Path | str) -> bytes:
    """Decode an image to RGB PNG bytes inside the media worker boundary."""
    return bytes(
        run_media_job(
            "load_image_rgb_png",
            {"path": str(Path(path).resolve())},
        )
    )


def validate_pdf_structure_isolated(path: Path | str) -> dict[str, Any]:
    """Structural PDF checks (pages, encryption, MediaBox) via the worker boundary."""
    return dict(
        run_media_job(
            "validate_pdf_structure",
            {"path": str(Path(path).resolve())},
        )
    )


def validate_image_structure_isolated(
    path: Path | str,
    *,
    expected_kind: str | None = None,
) -> dict[str, Any]:
    """Structural image checks (pixels, decode) via the worker boundary."""
    return dict(
        run_media_job(
            "validate_image_structure",
            {
                "path": str(Path(path).resolve()),
                "expected_kind": expected_kind,
            },
        )
    )


def prepare_png_for_vision_isolated(
    png_bytes: bytes,
    *,
    max_px: int,
    as_jpeg: bool,
) -> tuple[bytes, str]:
    """Prepare already-rendered PNG bytes for vision (in-process; trusted bytes)."""
    # Skip subprocess for our own PNG bytes — still uses PIL only in this module.
    return prepare_png_for_vision(png_bytes, max_px=max_px, as_jpeg=as_jpeg)
