"""Chroma is embedded-only: no HTTP server, no HttpClient, no FastAPI API impl."""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest
from chromadb.config import Settings

from deepcatalog.chroma_local import (
    embedded_chroma_client,
    embedded_chroma_settings,
    reject_http_chroma_settings,
)
from deepcatalog.tools import rag_index

_REPO = Path(__file__).resolve().parent.parent
_APP_ROOTS = (_REPO / "deepcatalog", _REPO / "app", _REPO / "query_agent")
_FORBIDDEN_SUBSTRINGS = (
    "HttpClient(",
    "chromadb.HttpClient",
    "chromadb.server",
    "chromadb.api.fastapi",
)


def test_source_never_starts_chroma_http_server():
    hits: list[str] = []
    for root in _APP_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_REPO).as_posix()
            if rel == "deepcatalog/chroma_local.py":
                # Guard module may name the forbidden impl in order to reject it.
                assert "HttpClient(" not in text
                assert "chromadb.server" not in text
                continue
            for needle in _FORBIDDEN_SUBSTRINGS:
                if needle in text:
                    hits.append(f"{rel}: {needle}")
    assert hits == []


def test_chroma_helper_never_constructs_http_client():
    source_text = (_REPO / "deepcatalog" / "chroma_local.py").read_text(encoding="utf-8")
    source = ast.parse(source_text)
    attrs: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.value, ast.Name) and node.value.id == "chromadb":
                attrs.append(node.attr)
            self.generic_visit(node)

    Visitor().visit(source)
    assert "PersistentClient" in attrs
    assert "HttpClient" not in attrs
    rag_text = (_REPO / "deepcatalog" / "tools" / "rag_index.py").read_text(encoding="utf-8")
    assert "embedded_chroma_client(CHROMA_DIR)" in rag_text
    assert "PersistentClient" not in rag_text
    assert "HttpClient" not in rag_text
    storage_text = (_REPO / "deepcatalog" / "tools" / "storage.py").read_text(encoding="utf-8")
    assert "embedded_chroma_client(chroma_dir)" in storage_text
    assert "PersistentClient" not in storage_text
    assert "HttpClient" not in storage_text


def test_reject_http_chroma_settings_raises():
    with pytest.raises(RuntimeError, match="HTTP API implementation"):
        reject_http_chroma_settings(
            Settings(
                chroma_api_impl="chromadb.api.fastapi.FastAPI",
                persist_directory="/tmp/chroma-reject",
                is_persistent=True,
            )
        )
    with pytest.raises(RuntimeError, match="server host/port"):
        reject_http_chroma_settings(
            Settings(
                chroma_api_impl="chromadb.api.rust.RustBindingsAPI",
                chroma_server_host="0.0.0.0",
                persist_directory="/tmp/chroma-reject",
                is_persistent=True,
            )
        )
    with pytest.raises(RuntimeError, match="gRPC"):
        reject_http_chroma_settings(
            Settings(
                chroma_api_impl="chromadb.api.rust.RustBindingsAPI",
                chroma_server_grpc_port=50051,
                persist_directory="/tmp/chroma-reject",
                is_persistent=True,
            )
        )


def test_env_cannot_switch_embedded_client_to_http(monkeypatch, tmp_path):
    monkeypatch.setenv("CHROMA_API_IMPL", "chromadb.api.fastapi.FastAPI")
    monkeypatch.setenv("CHROMA_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("CHROMA_SERVER_HTTP_PORT", "8000")
    settings = embedded_chroma_settings(tmp_path / "chroma")
    reject_http_chroma_settings(settings)
    assert "fastapi" not in settings.chroma_api_impl.lower()
    assert settings.chroma_server_host is None
    assert settings.chroma_server_http_port is None


def test_embedded_client_does_not_listen_on_tcp(tmp_path, monkeypatch):
    listens: list[str] = []
    orig = socket.socket.listen

    def hooked(self: socket.socket, *args: object, **kwargs: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            try:
                listens.append(str(self.getsockname()))
            except OSError:
                listens.append(f"family={self.family}")
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "listen", hooked)
    client = embedded_chroma_client(tmp_path / "chroma")
    collection = client.get_or_create_collection("deepcatalog_chunks")
    collection.upsert(ids=["a"], documents=["hello"], embeddings=[[0.1, 0.2, 0.3]])
    live = client.get_settings()
    reject_http_chroma_settings(live)
    assert live.is_persistent is True
    assert listens == []


def test_rag_index_client_uses_embedded_helper(isolated_data):
    client = rag_index._chroma_client()
    reject_http_chroma_settings(client.get_settings())
    assert client.get_settings().is_persistent is True
    assert "fastapi" not in client.get_settings().chroma_api_impl.lower()


def test_dependency_audit_documents_chroma_ignore_and_watch_script():
    audit = (_REPO / "scripts" / "dependency-audit.sh").read_text(encoding="utf-8")
    assert "--ignore-vuln PYSEC-2026-311" in audit
    assert "--ignore-vuln GHSA-f4j7-r4q5-qw2c" in audit
    assert "PersistentClient" in audit or "CVE-2026-45829" in audit
    watch = (_REPO / "scripts" / "chroma-advisory-watch.py").read_text(encoding="utf-8")
    assert '"--ignore-vuln"' not in watch
    assert "'--ignore-vuln'" not in watch
    workflow = (_REPO / ".github" / "workflows" / "advisory-watch.yml").read_text(encoding="utf-8")
    assert "chroma-advisory-watch.py" in workflow
    assert "schedule:" in workflow
    ci = (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "advisory-watch.yml" not in ci
    release = (_REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "advisory-watch.yml" not in release
