"""Production hardening: CSRF, path confinement, upload limits, atomic filing."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from tests.conftest import apply_test_client_auth

from app.main import CSRF_HEADER_NAME, app
from deepcatalog.pipeline.agents import file_and_persist
from deepcatalog.review import create_review
from deepcatalog.settings import get_source_dir
from deepcatalog.tools import filesystem
from deepcatalog.tools.metadata_db import get_document, list_recent


def test_mutating_routes_require_csrf_header(isolated_data):
    bare = TestClient(app)
    apply_test_client_auth(bare)
    # Drop CSRF so we isolate the header check from Bearer auth.
    bare.headers.pop(CSRF_HEADER_NAME, None)
    # Body-less POSTs are the classic CSRF vector for local unauthenticated APIs.
    for path in (
        "/api/process-inbox",
        "/api/update/apply",
        "/api/update/restart",
        "/api/auth/logout",
        "/api/ollama/enable",
        "/api/ollama/start",
        "/api/autostart",
        "/api/llm/provider",
        "/api/privacy/cloud-disclaimer",
    ):
        resp = bare.post(path)
        assert resp.status_code == 403, path
        assert "cross-site" in resp.json()["detail"]

    # GET liveness remains open and must stay privacy-minimal.
    health = bare.get("/api/health")
    assert health.status_code == 200
    assert set(health.json().keys()) <= {"status", "version"}


def test_csrf_header_allows_mutation(client):
    resp = client.post("/api/process-inbox")
    assert resp.status_code == 200
    assert resp.json()["status"] in {"empty", "success", "partial"}


def test_csrf_same_origin_browser_post_allows_upload(isolated_data):
    from tests.media_fixtures import minimal_pdf_bytes

    bare = TestClient(app)
    apply_test_client_auth(bare)
    bare.headers.pop(CSRF_HEADER_NAME, None)
    resp = bare.post(
        "/api/upload",
        files={"file": ("scan.pdf", minimal_pdf_bytes(), "application/pdf")},
        headers={
            "Origin": "http://testserver",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "scan.pdf"


def test_csrf_matching_origin_without_fetch_metadata_allows_mutation(isolated_data):
    bare = TestClient(app)
    apply_test_client_auth(bare)
    bare.headers.pop(CSRF_HEADER_NAME, None)
    resp = bare.post("/api/process-inbox", headers={"Origin": "http://testserver"})
    assert resp.status_code == 200


def test_csrf_cross_site_origin_is_blocked(isolated_data):
    bare = TestClient(app)
    apply_test_client_auth(bare)
    bare.headers.pop(CSRF_HEADER_NAME, None)
    resp = bare.post(
        "/api/process-inbox",
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403
    assert "cross-site" in resp.json()["detail"]


def test_origin_matches_host_header():
    from app.deps import origin_matches_host_header

    assert origin_matches_host_header("http://127.0.0.1:8080", "127.0.0.1:8080")
    assert origin_matches_host_header("http://testserver", "testserver")
    assert not origin_matches_host_header("https://evil.example", "127.0.0.1:8080")
    assert not origin_matches_host_header("http://user@127.0.0.1:8080", "127.0.0.1:8080")
    assert not origin_matches_host_header("null", "127.0.0.1:8080")
    assert not origin_matches_host_header("", "127.0.0.1:8080")


def test_process_rejects_paths_outside_inbox(client, isolated_data):
    secret = isolated_data.parent / "secret.txt"
    secret.write_text("credentials")
    resp = client.post("/api/process", json={"path": str(secret)})
    assert resp.status_code == 400
    assert "inbox" in resp.json()["detail"].lower()


def test_review_file_rejects_outside_inbox(client, isolated_data):
    secret = isolated_data.parent / "outside.pdf"
    secret.write_bytes(b"%PDF outside")
    queued = create_review(
        source_path=str(secret),
        original_name="outside.pdf",
        proposal={"filename": "x.pdf", "doc_type": "other"},
    )
    resp = client.get(f"/api/reviews/{queued['review_id']}/file")
    assert resp.status_code == 403


def test_upload_rejects_unsupported_type(client):
    resp = client.post(
        "/api/upload",
        files={"file": ("malware.exe", b"MZ fake", "application/octet-stream")},
    )
    assert resp.status_code == 415


def test_upload_accepts_pdf(client):
    from tests.media_fixtures import minimal_pdf_bytes

    resp = client.post(
        "/api/upload",
        files={"file": ("scan.pdf", minimal_pdf_bytes(), "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "scan.pdf"
    assert (get_source_dir() / "scan.pdf").exists()


def test_upload_accepts_raw_body_with_filename_header(client):
    from tests.media_fixtures import minimal_pdf_bytes

    payload = minimal_pdf_bytes()
    resp = client.post(
        "/api/upload",
        content=payload,
        headers={
            "Content-Type": "application/pdf",
            "X-File-Name": "raw.pdf",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "raw.pdf"
    assert (get_source_dir() / "raw.pdf").exists()


def test_upload_rejects_oversize_without_leaving_partial(client, monkeypatch):
    monkeypatch.setattr("app.deps.MAX_UPLOAD_BYTES", 64)
    monkeypatch.setattr(
        "deepcatalog.tools.filesystem.UPLOAD_CHUNK_BYTES",
        16,
    )
    # processing router reads MAX_UPLOAD_BYTES at call time via deps import binding
    monkeypatch.setattr("app.routers.processing.MAX_UPLOAD_BYTES", 64)
    payload = b"%PDF-" + (b"x" * 200)
    resp = client.post(
        "/api/upload",
        files={"file": ("huge.pdf", payload, "application/pdf")},
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()
    inbox = get_source_dir()
    assert not (inbox / "huge.pdf").exists()
    leftovers = [p for p in inbox.iterdir() if p.name.endswith(".part") or "huge" in p.name]
    assert leftovers == []


def test_stream_upload_aborts_as_soon_as_limit_exceeded(isolated_data):
    class FakeUpload:
        def __init__(self, data: bytes, chunk: int = 8):
            self._data = data
            self._offset = 0
            self._chunk = chunk
            self.reads = 0

        async def read(self, size: int = -1) -> bytes:
            self.reads += 1
            if self._offset >= len(self._data):
                return b""
            take = self._chunk if size < 0 else min(size, self._chunk)
            piece = self._data[self._offset : self._offset + take]
            self._offset += len(piece)
            return piece

    upload = FakeUpload(b"abcdefghijklmnop", chunk=4)  # 16 bytes
    result = asyncio.run(
        filesystem.stream_upload_to_inbox(
            "cap.pdf",
            upload,
            max_bytes=10,
            chunk_size=4,
        )
    )
    assert result["status"] == "error"
    assert result["code"] == "too_large"
    # Stopped after the chunk that crossed the limit (3×4 = 12 > 10).
    assert result["bytes_received"] == 12
    assert upload.reads == 3
    assert list(get_source_dir().glob("*.part")) == []
    assert not (get_source_dir() / "cap.pdf").exists()


def test_file_and_persist_is_atomic_on_metadata_failure(isolated_data, stub_rag_index, monkeypatch):
    inbox = get_source_dir()
    scan = inbox / "keep_me.pdf"
    scan.write_bytes(b"%PDF keep")

    def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("deepcatalog.pipeline.agents.upsert_metadata", boom)
    result = file_and_persist(
        source_path=str(scan),
        filename="2024-01-01_Invoice_Test.pdf",
        doc_type="invoice",
        doc_date="2024-01-01",
        summary="test",
    )
    assert result["status"] == "error"
    # Source must still be in the inbox; no orphan archive copy.
    assert scan.exists()
    assert list_recent(limit=10)["count"] == 0


def test_approve_confines_source_to_inbox(client, isolated_data, stub_rag_index):
    outside = isolated_data.parent / "escape.pdf"
    outside.write_bytes(b"%PDF escape")
    queued = create_review(
        source_path=str(outside),
        original_name="escape.pdf",
        proposal={
            "filename": "2024-01-01_Other_Escape.pdf",
            "doc_type": "other",
            "summary": "nope",
        },
    )
    resp = client.post(f"/api/reviews/{queued['review_id']}/approve", json={})
    assert resp.status_code == 409
    assert outside.exists()
    assert get_document(queued["review_id"]).get("status") != "success"
