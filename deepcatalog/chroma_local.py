"""Embedded Chroma only — never Chroma's HTTP/FastAPI server (CVE-2026-45829 / 45833)."""

from __future__ import annotations

from pathlib import Path

import chromadb
from chromadb.api import ClientAPI
from chromadb.config import Settings

# CVE-2026-45829 (pre-auth RCE) and CVE-2026-45833 (authenticated injection) in
# Chroma 1.0.0–1.5.9 target the HTTP server, not PersistentClient. DeepCatalog
# must only ever construct an on-disk embedded client.
_HTTP_API_MARKERS = ("fastapi", "chromadb.api.fastapi", "httpclient")


def reject_http_chroma_settings(settings: Settings) -> None:
    """Fail closed if settings would use or bind Chroma's HTTP/gRPC server."""
    impl = (settings.chroma_api_impl or "").strip().lower()
    if any(marker in impl for marker in _HTTP_API_MARKERS):
        raise RuntimeError(
            f"Refusing Chroma HTTP API implementation {settings.chroma_api_impl!r}. "
            "DeepCatalog only uses embedded PersistentClient."
        )
    if settings.chroma_server_host or settings.chroma_server_http_port:
        raise RuntimeError(
            "Refusing Chroma server host/port settings. "
            "DeepCatalog must not start or connect to Chroma's HTTP API."
        )
    if settings.chroma_server_grpc_port:
        raise RuntimeError("Refusing Chroma gRPC server port.")


def embedded_chroma_settings(path: str | Path) -> Settings:
    """Settings for a local persistent client, ignoring CHROMA_SERVER_* env."""
    persist = str(Path(path))
    settings = Settings(
        persist_directory=persist,
        is_persistent=True,
        chroma_api_impl="chromadb.api.rust.RustBindingsAPI",
        chroma_server_host=None,
        chroma_server_http_port=None,
        chroma_server_grpc_port=None,
        chroma_server_ssl_enabled=False,
        anonymized_telemetry=False,
    )
    reject_http_chroma_settings(settings)
    return settings


def embedded_chroma_client(path: str | Path) -> ClientAPI:
    """On-disk PersistentClient that cannot expose Chroma's vulnerable HTTP server."""
    persist = str(Path(path))
    settings = embedded_chroma_settings(persist)
    client = chromadb.PersistentClient(path=persist, settings=settings)
    live = client.get_settings()
    reject_http_chroma_settings(live)
    if not live.is_persistent:
        raise RuntimeError("Chroma client is not persistent")
    return client
