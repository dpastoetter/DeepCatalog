"""Media worker isolation, Pipe drain semantics, and OCR render integration."""

from __future__ import annotations

import asyncio
import io
import logging
import multiprocessing as mp
import shutil
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from tests.media_fixtures import write_minimal_pdf, write_minimal_png

from deepcatalog.media_worker import (
    MediaSandboxReport,
    MediaWorkerError,
    MediaWorkerLimits,
    _apply_resource_limits,
    _default_limits,
    _harden_worker_process,
    _worker_main,
    extract_pdf_page_texts_isolated,
    load_image_rgb_png_isolated,
    media_sandbox_capabilities,
    media_worker_enabled,
    refuse_inprocess_media_in_network_mode,
    render_pdf_page_png_isolated,
    run_media_job,
    sanitize_worker_environ,
    warn_if_media_worker_disabled,
)
from deepcatalog.ocr import render_document_page


def _noisy_png(path: Path, size: tuple[int, int] = (1600, 1600)) -> Path:
    """Write an uncompressed noisy PNG large enough to overflow a Pipe buffer."""
    Image.effect_noise(size, 80).convert("RGB").save(path, format="PNG", compress_level=0)
    assert path.stat().st_size > 64 * 1024
    return path


def _require_poppler() -> None:
    if shutil.which("pdftoppm") is None or shutil.which("pdfinfo") is None:
        pytest.skip("poppler-utils not installed (pdftoppm/pdfinfo)")


def _legacy_join_before_recv(
    job: str,
    payload: dict[str, Any],
    *,
    limits: MediaWorkerLimits,
) -> Any:
    """
    Recreate the pre-fix parent loop that deadlocks on large Pipe payloads.

    Kept only in tests so a regression reintroducing join-before-recv is obvious.
    """
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_worker_main,
        args=(job, payload, child_conn, limits),
        daemon=True,
    )
    proc.start()
    child_conn.close()
    proc.join(limits.timeout_s)
    if proc.is_alive():
        proc.terminate()
        proc.join(2.0)
        if proc.is_alive():
            proc.kill()
            proc.join(1.0)
        raise MediaWorkerError(
            f"media worker timed out after {limits.timeout_s:.0f}s ({job})",
            code="timeout",
        )
    if parent_conn.poll(0.1):
        status, body = parent_conn.recv()
    else:
        raise MediaWorkerError(
            f"media worker exited without result (job={job}, exit={proc.exitcode})",
            code="worker_failed",
        )
    parent_conn.close()
    if status == "ok":
        return body
    raise MediaWorkerError(str(body), code="worker_failed")


class _CaptureConn:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.closed = False

    def send(self, item: Any) -> None:
        self.sent.append(item)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def no_parent_resource_limits(monkeypatch):
    """Never apply RLIMIT_* / env wipe in the pytest process when exercising `_worker_main`."""

    def stub_harden(_limits: MediaWorkerLimits):
        return MediaSandboxReport(isolated_process=False, notes=["test inline harden"]), None

    monkeypatch.setattr(
        "deepcatalog.media_worker._harden_worker_process",
        stub_harden,
    )


# --- enabled flag / limits -------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("OFF", False),
    ],
)
def test_media_worker_enabled_env(monkeypatch, value: str, expected: bool):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", value)
    assert media_worker_enabled() is expected


def test_default_limits_respect_config(monkeypatch):
    monkeypatch.setattr("deepcatalog.media_worker.config.MEDIA_WORKER_TIMEOUT_S", 12.5)
    monkeypatch.setattr("deepcatalog.media_worker.config.MEDIA_WORKER_MEMORY_MB", 512)
    monkeypatch.setattr("deepcatalog.media_worker.config.MEDIA_WORKER_CPU_S", 30)
    limits = _default_limits()
    assert limits.timeout_s == 12.5
    assert limits.memory_bytes == 512 * 1024 * 1024
    assert limits.cpu_seconds == 30


def test_apply_resource_limits_calls_setrlimit(monkeypatch):
    import resource as resource_mod

    calls: list[tuple[int, tuple[int, int]]] = []

    def fake_setrlimit(res: int, lim: tuple[int, int]) -> None:
        calls.append((res, lim))

    monkeypatch.setattr(resource_mod, "setrlimit", fake_setrlimit)
    limits = MediaWorkerLimits(timeout_s=5, memory_bytes=256 * 1024 * 1024, cpu_seconds=5)
    _apply_resource_limits(limits)
    kinds = {c[0] for c in calls}
    assert resource_mod.RLIMIT_CPU in kinds
    assert resource_mod.RLIMIT_AS in kinds
    assert resource_mod.RLIMIT_NOFILE in kinds
    assert resource_mod.RLIMIT_NPROC not in kinds
    cpu = next(c for c in calls if c[0] == resource_mod.RLIMIT_CPU)
    assert cpu[1] == (5, 6)
    mem = next(c for c in calls if c[0] == resource_mod.RLIMIT_AS)
    assert mem[1] == (256 * 1024 * 1024, 256 * 1024 * 1024)


# --- environment sanitization / sandbox report -----------------------------


def test_sanitize_worker_environ_strips_secrets_and_sets_tmpdir():
    source = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/user",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "OPENAI_API_KEY": "sk-secret",
        "ANTHROPIC_API_KEY": "sk-ant",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "DEEPCATALOG_API_TOKEN": "tok",
        "CODEX_API_KEY": "codex",
        "MY_BEARER_TOKEN": "bearer",
        "RANDOM_CLOUD_CREDENTIAL": "cred",
        "LD_PRELOAD": "/evil.so",
        "UNRELATED_CONFIG": "drop-me",
        "TMPDIR": "/tmp/parent",
    }
    cleaned = sanitize_worker_environ(source, tmpdir="/tmp/private-media")
    assert cleaned["PATH"] == "/usr/bin:/bin"
    assert cleaned["HOME"] == "/home/user"
    assert cleaned["LANG"] == "en_US.UTF-8"
    assert cleaned["LC_ALL"] == "en_US.UTF-8"
    assert cleaned["TMPDIR"] == "/tmp/private-media"
    assert cleaned["TMP"] == "/tmp/private-media"
    assert cleaned["TEMP"] == "/tmp/private-media"
    assert "OPENAI_API_KEY" not in cleaned
    assert "ANTHROPIC_API_KEY" not in cleaned
    assert "AWS_SECRET_ACCESS_KEY" not in cleaned
    assert "DEEPCATALOG_API_TOKEN" not in cleaned
    assert "CODEX_API_KEY" not in cleaned
    assert "MY_BEARER_TOKEN" not in cleaned
    assert "RANDOM_CLOUD_CREDENTIAL" not in cleaned
    assert "LD_PRELOAD" not in cleaned
    assert "UNRELATED_CONFIG" not in cleaned


def test_sanitize_worker_environ_denies_secret_named_keys():
    dirty = sanitize_worker_environ(
        {
            "PATH": "/bin",
            "OPENAI_API_KEY": "x",
            "SESSION_COOKIE": "y",
            "FONTCONFIG_PATH": "/etc/fonts",
        }
    )
    assert dirty["PATH"] == "/bin"
    assert dirty["FONTCONFIG_PATH"] == "/etc/fonts"
    assert "OPENAI_API_KEY" not in dirty
    assert "SESSION_COOKIE" not in dirty


def test_media_sandbox_capabilities_are_honest():
    caps = media_sandbox_capabilities()
    assert caps["intended"]["landlock"] is False
    assert caps["intended"]["bubblewrap"] is False
    assert caps["intended"]["env_sanitized"] is True
    assert caps["intended"]["private_tmpdir"] is True
    assert any("network" in n.lower() for n in caps["notes"])


def test_harden_worker_process_private_tmpdir_and_report(monkeypatch, tmp_path):
    replaced: dict[str, str] = {}

    def fake_replace(env: dict[str, str]) -> None:
        replaced.clear()
        replaced.update(env)

    monkeypatch.setattr("deepcatalog.media_worker._replace_process_environ", fake_replace)
    monkeypatch.setattr("deepcatalog.media_worker._apply_resource_limits", lambda _l: None)
    monkeypatch.setattr("deepcatalog.media_worker._try_no_new_privileges", lambda: True)
    monkeypatch.setattr("deepcatalog.media_worker._try_unshare_net", lambda: False)
    monkeypatch.setattr(
        "deepcatalog.media_worker.tempfile.mkdtemp",
        lambda prefix="": str(tmp_path / "deepcatalog-media-test"),
    )
    (tmp_path / "deepcatalog-media-test").mkdir()

    limits = MediaWorkerLimits(timeout_s=5, memory_bytes=64 * 1024 * 1024, cpu_seconds=5)
    report, tmpdir = _harden_worker_process(limits)
    assert tmpdir is not None
    assert report.private_tmpdir is True
    assert report.env_sanitized is True
    assert report.resource_limits is True
    assert report.no_new_privileges is True
    assert report.network_namespace is False
    assert report.landlock is False
    assert report.bubblewrap is False
    assert replaced["TMPDIR"] == tmpdir
    assert "OPENAI_API_KEY" not in replaced
    assert any("network namespace unavailable" in n for n in report.notes)
    assert any("Landlock/bubblewrap not enabled" in n for n in report.notes)


def test_refuse_inprocess_media_when_allow_remote(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "0")
    monkeypatch.setattr("deepcatalog.media_worker.allow_remote_enabled", lambda: True)
    monkeypatch.setattr("deepcatalog.media_worker.effective_bind_host", lambda: "127.0.0.1")
    monkeypatch.setattr(
        "deepcatalog.media_worker.is_wildcard_or_non_loopback_bind",
        lambda _h: False,
    )
    with pytest.raises(RuntimeError, match="refused for network"):
        refuse_inprocess_media_in_network_mode()


def test_refuse_inprocess_media_when_non_loopback_bind(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "0")
    monkeypatch.setattr("deepcatalog.media_worker.allow_remote_enabled", lambda: False)
    monkeypatch.setattr("deepcatalog.media_worker.effective_bind_host", lambda: "0.0.0.0")
    monkeypatch.setattr(
        "deepcatalog.media_worker.is_wildcard_or_non_loopback_bind",
        lambda h: h == "0.0.0.0",
    )
    with pytest.raises(RuntimeError, match="refused for network"):
        refuse_inprocess_media_in_network_mode()


def test_warn_inprocess_media_on_loopback_only(monkeypatch, caplog):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "0")
    monkeypatch.setattr("deepcatalog.media_worker.allow_remote_enabled", lambda: False)
    monkeypatch.setattr("deepcatalog.media_worker.effective_bind_host", lambda: "127.0.0.1")
    monkeypatch.setattr(
        "deepcatalog.media_worker.is_wildcard_or_non_loopback_bind",
        lambda _h: False,
    )
    monkeypatch.setattr("deepcatalog.media_worker._inprocess_warned", False)
    with caplog.at_level(logging.WARNING, logger="deepcatalog.media_worker"):
        refuse_inprocess_media_in_network_mode()
    assert any(
        "SECURITY: DEEPCATALOG_MEDIA_WORKER is disabled" in r.message for r in caplog.records
    )


def test_refuse_noop_when_worker_enabled(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")
    refuse_inprocess_media_in_network_mode()  # must not raise


def test_warn_if_media_worker_disabled_once(monkeypatch, caplog):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "0")
    monkeypatch.setattr("deepcatalog.media_worker._inprocess_warned", False)
    with caplog.at_level(logging.WARNING, logger="deepcatalog.media_worker"):
        warn_if_media_worker_disabled()
        warn_if_media_worker_disabled()
    warnings = [r for r in caplog.records if "SECURITY: DEEPCATALOG_MEDIA_WORKER" in r.message]
    assert len(warnings) == 1


# --- in-process worker body ------------------------------------------------


def test_worker_main_extract_pdf_text(tmp_path: Path, no_parent_resource_limits):
    pdf = write_minimal_pdf(tmp_path / "doc.pdf", line="Invoice FA-99")
    conn = _CaptureConn()
    limits = MediaWorkerLimits(timeout_s=10, memory_bytes=512 * 1024 * 1024, cpu_seconds=30)
    _worker_main("extract_pdf_page_texts", {"path": str(pdf)}, conn, limits)
    assert conn.closed is True
    assert conn.sent and conn.sent[0][0] == "ok"
    texts = conn.sent[0][1]
    assert isinstance(texts, list)
    assert len(texts) == 1


def test_worker_main_load_image(tmp_path: Path, no_parent_resource_limits):
    png = write_minimal_png(tmp_path / "img.png", size=(64, 48))
    conn = _CaptureConn()
    limits = MediaWorkerLimits(timeout_s=10, memory_bytes=512 * 1024 * 1024, cpu_seconds=30)
    _worker_main("load_image_rgb_png", {"path": str(png)}, conn, limits)
    assert conn.sent[0][0] == "ok"
    assert isinstance(conn.sent[0][1], (bytes, bytearray))
    assert conn.sent[0][1][:8] == b"\x89PNG\r\n\x1a\n"


def test_worker_main_render_pdf_page(tmp_path: Path, no_parent_resource_limits):
    _require_poppler()
    pdf = write_minimal_pdf(tmp_path / "page.pdf", line="Render me")
    conn = _CaptureConn()
    limits = MediaWorkerLimits(timeout_s=30, memory_bytes=1024 * 1024 * 1024, cpu_seconds=60)
    _worker_main(
        "render_pdf_page",
        {"path": str(pdf), "page_index": 1, "dpi": 72},
        conn,
        limits,
    )
    assert conn.sent[0][0] == "ok"
    assert isinstance(conn.sent[0][1], (bytes, bytearray))
    assert len(conn.sent[0][1]) > 100


def test_worker_main_unknown_job_reports_err(no_parent_resource_limits):
    conn = _CaptureConn()
    limits = MediaWorkerLimits(timeout_s=5, memory_bytes=64 * 1024 * 1024, cpu_seconds=5)
    _worker_main("not_a_real_job", {}, conn, limits)
    assert conn.sent[0][0] == "err"
    body = conn.sent[0][1]
    message = body["message"] if isinstance(body, dict) else str(body)
    assert "unknown media worker job" in message


def test_worker_main_missing_file_reports_err(tmp_path: Path, no_parent_resource_limits):
    conn = _CaptureConn()
    limits = MediaWorkerLimits(timeout_s=5, memory_bytes=64 * 1024 * 1024, cpu_seconds=5)
    _worker_main(
        "load_image_rgb_png",
        {"path": str(tmp_path / "missing.png")},
        conn,
        limits,
    )
    assert conn.sent[0][0] == "err"


# --- spawn / Pipe semantics ------------------------------------------------


def test_legacy_join_before_recv_deadlocks_on_large_png(tmp_path: Path):
    """Prove the old parent loop times out once PNG bytes exceed the Pipe buffer."""
    path = _noisy_png(tmp_path / "noisy.png")
    limits = MediaWorkerLimits(
        timeout_s=3,
        memory_bytes=1024 * 1024 * 1024,
        cpu_seconds=60,
    )
    t0 = time.time()
    with pytest.raises(MediaWorkerError, match="timed out") as exc:
        _legacy_join_before_recv(
            "load_image_rgb_png",
            {"path": str(path.resolve())},
            limits=limits,
        )
    elapsed = time.time() - t0
    assert exc.value.code == "timeout"
    assert elapsed >= 2.5
    assert elapsed < 8


def test_run_media_job_drains_large_png_without_deadlock(tmp_path: Path):
    """Current loop must recv while the child writes — no timeout on multi-MB PNG."""
    path = _noisy_png(tmp_path / "noisy.png")
    limits = MediaWorkerLimits(
        timeout_s=20,
        memory_bytes=1024 * 1024 * 1024,
        cpu_seconds=60,
    )
    t0 = time.time()
    png = run_media_job(
        "load_image_rgb_png",
        {"path": str(path.resolve())},
        limits=limits,
    )
    elapsed = time.time() - t0
    assert isinstance(png, (bytes, bytearray))
    assert len(png) > 64 * 1024
    assert elapsed < 15, f"worker looked stuck ({elapsed:.1f}s) — Pipe deadlock?"


def test_extract_pdf_page_texts_isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")
    pdf = write_minimal_pdf(tmp_path / "invoice.pdf", line="Invoice FA-1")
    pages = extract_pdf_page_texts_isolated(pdf)
    assert len(pages) == 1
    assert isinstance(pages[0], str)


def test_load_image_rgb_png_isolated_small(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")
    png = write_minimal_png(tmp_path / "small.png")
    raw = load_image_rgb_png_isolated(png)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_pdf_page_png_isolated(tmp_path: Path, monkeypatch):
    _require_poppler()
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")
    pdf = write_minimal_pdf(tmp_path / "page.pdf", line="Hello")
    raw = render_pdf_page_png_isolated(pdf, 1, dpi=72)
    assert isinstance(raw, (bytes, bytearray))
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_run_media_job_propagates_worker_error(tmp_path: Path):
    limits = MediaWorkerLimits(
        timeout_s=10,
        memory_bytes=256 * 1024 * 1024,
        cpu_seconds=20,
    )
    with pytest.raises(MediaWorkerError) as exc:
        run_media_job(
            "load_image_rgb_png",
            {"path": str((tmp_path / "nope.png").resolve())},
            limits=limits,
        )
    assert exc.value.code == "worker_failed"


def test_run_media_job_unknown_job_errors():
    limits = MediaWorkerLimits(
        timeout_s=10,
        memory_bytes=256 * 1024 * 1024,
        cpu_seconds=20,
    )
    with pytest.raises(MediaWorkerError) as exc:
        run_media_job("not_a_job", {}, limits=limits)
    assert exc.value.code == "worker_failed"


def test_run_media_job_timeout_terminates_hung_child(monkeypatch):
    """Hung child with no Pipe traffic must be terminated as timeout."""
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")

    real_pipe = mp.get_context("spawn").Pipe

    class _HungProc:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self._alive = True
            self.exitcode: int | None = None

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return self._alive

        def join(self, timeout: float | None = None) -> None:
            if timeout is not None:
                return
            self._alive = False
            self.exitcode = -15

        def terminate(self) -> None:
            self._alive = False
            self.exitcode = -15

        def kill(self) -> None:
            self._alive = False
            self.exitcode = -9

    class _Ctx:
        def Pipe(self, duplex: bool = True):  # noqa: N802 — mirrors mp API
            return real_pipe(duplex=duplex)

        def Process(self, *_a: Any, **kwargs: Any) -> _HungProc:  # noqa: N802
            proc = _HungProc()
            # Retain the child's write end so closing the parent's copy does not EOF.
            args = kwargs.get("args") or ()
            if len(args) >= 3:
                proc._child_conn = args[2]
            return proc

    monkeypatch.setattr(
        "deepcatalog.media_worker.mp.get_context",
        lambda _name="spawn": _Ctx(),
    )
    limits = MediaWorkerLimits(
        timeout_s=0.4,
        memory_bytes=64 * 1024 * 1024,
        cpu_seconds=5,
    )
    t0 = time.time()
    with pytest.raises(MediaWorkerError, match="timed out") as exc:
        run_media_job("load_image_rgb_png", {"path": "/tmp/x"}, limits=limits)
    assert exc.value.code == "timeout"
    assert time.time() - t0 < 5


# --- OCR integration -------------------------------------------------------


def test_render_document_page_pdf_via_worker(tmp_path: Path, monkeypatch):
    _require_poppler()
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")
    pdf = write_minimal_pdf(tmp_path / "scan.pdf", line="OCR page")
    png = render_document_page(pdf, 1, dpi=72)
    assert isinstance(png, (bytes, bytearray))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_document_page_image_via_worker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")
    path = write_minimal_png(tmp_path / "scan.png", size=(80, 40))
    png = render_document_page(path, 1)
    assert isinstance(png, (bytes, bytearray))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(io.BytesIO(png)) as img:
        assert img.size == (80, 40)


def test_render_document_page_pdf_without_worker(tmp_path: Path, monkeypatch):
    _require_poppler()
    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "0")
    pdf = write_minimal_pdf(tmp_path / "scan.pdf", line="Direct poppler")
    png = render_document_page(pdf, 1, dpi=72)
    assert isinstance(png, (bytes, bytearray))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_ai_vision_render_runs_off_event_loop(tmp_path: Path, monkeypatch):
    """Sync render sleep must not freeze the event loop (asyncio.to_thread)."""
    from deepcatalog.ocr import _ai_vision_one_page

    path = write_minimal_png(tmp_path / "scan.png")

    def slow_render(*_a, **_k):
        time.sleep(0.35)
        buf = io.BytesIO()
        Image.new("RGB", (32, 24), "white").save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr("deepcatalog.ocr.render_document_page", slow_render)
    monkeypatch.setattr(
        "deepcatalog.llm.complete_with_images",
        AsyncMock(return_value="transcribed text"),
    )

    async def exercise() -> None:
        t0 = time.monotonic()
        page_task = asyncio.create_task(_ai_vision_one_page(path, 1))
        await asyncio.sleep(0.05)
        mid = time.monotonic() - t0
        # Event loop stayed responsive while render slept in a worker thread.
        assert mid < 0.25, f"event loop blocked for {mid:.2f}s during render"
        text = await page_task
        assert text == "transcribed text"
        assert time.monotonic() - t0 >= 0.3

    asyncio.run(exercise())


def test_recover_uses_worker_extract_in_thread(tmp_path: Path, monkeypatch):
    """PDF text-layer recovery should succeed with the media worker enabled."""
    from deepcatalog.ocr import recover_document_text

    monkeypatch.setenv("DEEPCATALOG_MEDIA_WORKER", "1")
    monkeypatch.setenv("DEEPCATALOG_OCR_MODE", "fast")
    pdf = write_minimal_pdf(
        tmp_path / "invoice.pdf",
        line=(
            "Invoice FA2022-0001 from BV CRE8 dated 2022-09-05. "
            "Total including VAT is EUR 181.50 for the comanage business package."
        ),
    )
    result = asyncio.run(recover_document_text(pdf))
    assert result["status"] in {"success", "partial"}
    assert result.get("method") in {"pdf_text_layer", "ai_vision", "mixed"}
