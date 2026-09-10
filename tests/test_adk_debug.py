"""ADK debug factory is isolated from production ingest/Ask and uses trusted Ollama URLs."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from deepcatalog.adk_debug import build_pipeline_agent, build_query_agent
from deepcatalog.llm import get_adk_debug_model, get_model

_REPO = Path(__file__).resolve().parent.parent
_PRODUCTION_PY = (
    _REPO / "deepcatalog" / "ingest.py",
    _REPO / "deepcatalog" / "ask.py",
    _REPO / "deepcatalog" / "ocr.py",
    _REPO / "deepcatalog" / "pipeline" / "agents.py",
    _REPO / "deepcatalog" / "tools" / "rag_index.py",
    _REPO / "deepcatalog" / "providers" / "runtime.py",
    _REPO / "deepcatalog" / "providers" / "base.py",
)


def test_production_modules_do_not_import_adk_factory():
    hits: list[str] = []
    for path in _PRODUCTION_PY:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_REPO).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "google.adk" or alias.name.startswith("google.adk."):
                        hits.append(f"{rel}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = {alias.name for alias in node.names}
                if mod == "google.adk" or mod.startswith("google.adk."):
                    hits.append(f"{rel}: from {mod}")
                if (
                    mod == "deepcatalog.adk_debug"
                    or mod == "deepcatalog.llm"
                    and names
                    & {
                        "get_model",
                        "get_adk_debug_model",
                        "OpenAILlm",
                    }
                ):
                    hits.append(f"{rel}: from {mod} import {sorted(names)}")
    assert hits == []


def test_openai_base_url_is_only_set_via_trusted_helper():
    hits: list[str] = []
    for path in (_REPO / "deepcatalog").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(_REPO).as_posix()
        if 'os.environ["OPENAI_BASE_URL"] =' not in text:
            continue
        if rel == "deepcatalog/ollama_url.py" and "ollama_openai_compatible_base_url" in text:
            continue
        hits.append(rel)
    assert hits == []


def test_get_adk_debug_model_ollama_uses_trusted_origin(monkeypatch):
    monkeypatch.setattr("deepcatalog.llm.config.LLM_PROVIDER", "ollama")
    monkeypatch.setattr("deepcatalog.llm.config.OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setattr("deepcatalog.llm.config.MODEL_NAME", "gemma3")
    monkeypatch.setattr("deepcatalog.llm.resolve_runtime_model", lambda name: name)
    monkeypatch.setattr("deepcatalog.llm.allow_remote_ollama_enabled", lambda: False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    model = get_adk_debug_model()
    assert os.environ["OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert type(model).__name__ == "_AdkOllamaOpenAILlm"
    assert str(model._openai_client.base_url).rstrip("/") == "http://127.0.0.1:11434/v1"
    assert get_model is not get_adk_debug_model
    alias = get_model()
    assert type(alias).__name__ == "_AdkOllamaOpenAILlm"


def test_get_adk_debug_model_rejects_metadata_url(monkeypatch):
    monkeypatch.setattr("deepcatalog.llm.config.LLM_PROVIDER", "ollama")
    monkeypatch.setattr("deepcatalog.llm.config.OLLAMA_BASE_URL", "http://169.254.169.254")
    monkeypatch.setattr("deepcatalog.llm.allow_remote_ollama_enabled", lambda: True)
    monkeypatch.setattr("deepcatalog.llm.resolve_runtime_model", lambda name: name)
    with pytest.raises(ValueError, match="link-local|blocked|not allowed"):
        get_adk_debug_model()


def test_adk_agents_are_debug_only_and_mark_content_untrusted():
    ingest = build_pipeline_agent()
    query = build_query_agent()
    assert ingest.name == "deepcatalog_ingest"
    assert query.name == "deepcatalog_query"
    assert "untrusted" in ingest.instruction.lower()
    assert "untrusted" in query.instruction.lower()
