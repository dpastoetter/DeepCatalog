"""Chroma is embedded-only: no HTTP server, no HttpClient, no FastAPI API impl."""

from __future__ import annotations

import ast
import io
import json
import socket
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from chromadb.config import Settings

from deepcatalog.chroma_local import (
    APPROVED_EMBEDDED_API_IMPLS,
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
    "CHROMA_SERVER_HOST",
    "CHROMA_SERVER_HTTP_PORT",
    "CHROMA_SERVER_GRPC_PORT",
    "chroma_server_host=",
    "chroma_server_http_port=",
)
_SUPPRESSIONS = _REPO / "scripts" / "chroma_vuln_suppressions.json"


def test_source_never_starts_chroma_http_server():
    hits: list[str] = []
    for root in _APP_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_REPO).as_posix()
            if rel == "deepcatalog/chroma_local.py":
                # Guard module may name forbidden impls in order to reject them.
                assert "HttpClient(" not in text
                assert "chromadb.server" not in text
                assert "chromadb.HttpClient" not in text
                continue
            for needle in _FORBIDDEN_SUBSTRINGS:
                if needle in text:
                    hits.append(f"{rel}: {needle}")
    assert hits == []


def test_chroma_helper_never_constructs_http_client():
    source_text = (_REPO / "deepcatalog" / "chroma_local.py").read_text(encoding="utf-8")
    source = ast.parse(source_text)
    attrs: list[str] = []
    calls: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.value, ast.Name) and node.value.id == "chromadb":
                attrs.append(node.attr)
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "chromadb":
                    calls.append(node.func.attr)
            self.generic_visit(node)

    Visitor().visit(source)
    assert "PersistentClient" in attrs
    assert "HttpClient" not in attrs
    assert calls == ["PersistentClient"]
    rag_text = (_REPO / "deepcatalog" / "tools" / "rag_index.py").read_text(encoding="utf-8")
    assert "embedded_chroma_client(CHROMA_DIR)" in rag_text
    assert "PersistentClient" not in rag_text
    assert "HttpClient" not in rag_text
    storage_text = (_REPO / "deepcatalog" / "tools" / "storage.py").read_text(encoding="utf-8")
    assert "embedded_chroma_client(chroma_dir)" in storage_text
    assert "PersistentClient" not in storage_text
    assert "HttpClient" not in storage_text


def test_production_chromadb_imports_are_embedded_only():
    """Semantic import scan: no Chroma HttpClient / FastAPI / server modules."""
    forbidden_modules = {
        "chromadb.api.fastapi",
        "chromadb.server",
        "chromadb.server.fastapi",
    }
    hits: list[str] = []
    for root in _APP_ROOTS:
        for path in root.rglob("*.py"):
            rel = path.relative_to(_REPO).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                    if any(mod == bad or mod.startswith(bad + ".") for bad in forbidden_modules):
                        hits.append(f"{rel}: from {mod}")
                    if mod.startswith("chromadb"):
                        for alias in node.names:
                            if alias.name in {"HttpClient", "FastAPI"}:
                                hits.append(f"{rel}: from {mod} import {alias.name}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(
                            alias.name == bad or alias.name.startswith(bad + ".")
                            for bad in forbidden_modules
                        ):
                            hits.append(f"{rel}: import {alias.name}")
    assert hits == []


def test_reject_http_chroma_settings_raises():
    with pytest.raises(RuntimeError, match="HTTP API implementation"):
        reject_http_chroma_settings(
            Settings(
                chroma_api_impl="chromadb.api.fastapi.FastAPI",
                persist_directory="/tmp/chroma-reject",
                is_persistent=True,
            )
        )
    with pytest.raises(RuntimeError, match="unapproved Chroma API"):
        reject_http_chroma_settings(
            Settings(
                chroma_api_impl="chromadb.api.segment.SegmentAPI",
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
    assert settings.chroma_api_impl == "chromadb.api.rust.RustBindingsAPI"
    assert settings.chroma_api_impl in APPROVED_EMBEDDED_API_IMPLS
    assert settings.chroma_server_host is None
    assert settings.chroma_server_http_port is None


def test_live_client_uses_approved_rust_bindings(tmp_path):
    client = embedded_chroma_client(tmp_path / "chroma")
    live = client.get_settings()
    reject_http_chroma_settings(live)
    assert live.chroma_api_impl in APPROVED_EMBEDDED_API_IMPLS
    assert live.chroma_api_impl == "chromadb.api.rust.RustBindingsAPI"
    assert live.is_persistent is True
    assert not live.chroma_server_host
    assert not live.chroma_server_http_port


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
    assert client.get_settings().chroma_api_impl in APPROVED_EMBEDDED_API_IMPLS
    assert "fastapi" not in client.get_settings().chroma_api_impl.lower()


def test_chroma_vuln_suppressions_have_rationale_and_future_expiry():
    payload = json.loads(_SUPPRESSIONS.read_text(encoding="utf-8"))
    rows = payload["suppressions"]
    assert rows
    today = date.today()
    ids: list[str] = []
    for row in rows:
        vuln_id = row["id"]
        ids.append(vuln_id)
        assert len(row["rationale"]) >= 40
        assert row.get("compensating_control")
        reviewed = datetime.strptime(row["reviewed"], "%Y-%m-%d").date()
        expires = datetime.strptime(row["expires"], "%Y-%m-%d").date()
        assert expires >= reviewed
        assert expires >= today, f"{vuln_id} expired on {expires}; re-evaluate suppressions"
        assert expires <= today + timedelta(days=400), f"{vuln_id} expiry too far out"
    assert len(ids) == len(set(ids))


def test_check_chroma_suppressions_script_fails_when_expired():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_chroma_suppressions",
        _REPO / "scripts" / "check_chroma_suppressions.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.load_suppressions()
    # Force "today" past every expiry → must fail.
    future = date(2099, 1, 1)
    reasons = mod.check_suppressions(rows, today=future)
    assert reasons
    assert any("expired" in r for r in reasons)
    # Fresh today with real dates must pass the expiry portion.
    ok = mod.check_suppressions(rows, today=date.today())
    assert not any("expired" in r for r in ok)


def test_dependency_audit_uses_suppression_json_and_watch_script():
    import importlib.util

    audit = (_REPO / "scripts" / "dependency-audit.sh").read_text(encoding="utf-8")
    assert "check_chroma_suppressions.py" in audit
    assert "chroma_vuln_suppressions.json" in audit
    payload = json.loads(_SUPPRESSIONS.read_text(encoding="utf-8"))
    for row in payload["suppressions"]:
        assert row["id"]
    # Script must obtain ignore flags from the JSON helper (not hard-code only).
    assert "--pip-audit-args" in audit
    # Emit separate argv tokens (flag, then id) so bash mapfile does not glue
    # "--ignore-vuln ID" into one unrecognized pip-audit argument.
    spec = importlib.util.spec_from_file_location(
        "check_chroma_suppressions",
        _REPO / "scripts" / "check_chroma_suppressions.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert mod.main(["--pip-audit-args"]) == 0
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines and lines[0] == "--ignore-vuln"
    assert lines[1] == payload["suppressions"][0]["id"]
    assert all(lines[i] == "--ignore-vuln" for i in range(0, len(lines), 2))
    assert {lines[i] for i in range(1, len(lines), 2)} == {
        row["id"] for row in payload["suppressions"]
    }
    watch = (_REPO / "scripts" / "chroma-advisory-watch.py").read_text(encoding="utf-8")
    assert "chroma_vuln_suppressions.json" in watch
    assert '"--ignore-vuln"' not in watch
    workflow = (_REPO / ".github" / "workflows" / "advisory-watch.yml").read_text(encoding="utf-8")
    assert "chroma-advisory-watch.py" in workflow
    assert "schedule:" in workflow
