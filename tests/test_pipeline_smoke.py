"""Smoke tests for ADK agent wiring and ingest helpers (no live API calls)."""

from deepcatalog.adk_debug import build_pipeline_agent
from deepcatalog.agent import root_agent as pipeline_agent
from deepcatalog.llm import resolve_model_name
from deepcatalog.pipeline.agents import parse_json_blob
from query_agent.agent import root_agent as query_agent


def test_pipeline_agent_structure():
    agent = build_pipeline_agent()
    assert agent.name == "deepcatalog_ingest"
    assert len(agent.tools) == 3
    assert "untrusted" in agent.instruction.lower()
    assert pipeline_agent.name == "deepcatalog_ingest"


def test_query_agent_tools():
    assert query_agent.name == "deepcatalog_query"
    assert len(query_agent.tools) == 3
    assert "untrusted" in query_agent.instruction.lower()
    assert "retrieve_chunks" in query_agent.instruction


def test_parse_json_blob():
    assert parse_json_blob('{"doc_type":"invoice"}')["doc_type"] == "invoice"
    assert parse_json_blob('```json\n{"a":1}\n```')["a"] == 1
    assert parse_json_blob("no json here") == {}


def test_codex_oauth_falls_back_from_gpt41(monkeypatch, tmp_path):
    import base64
    import json

    monkeypatch.setenv("DEEPCATALOG_LLM_PROVIDER", "openai")
    monkeypatch.setenv("DEEPCATALOG_MODEL", "gpt-4.1")
    monkeypatch.delenv("DEEPCATALOG_CODEX_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"}}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    access = f"eyJhbGciOiJub25lIn0.{payload}.sig"
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": access,
                    "refresh_token": "r",
                    "account_id": "acct_1",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("deepcatalog.config.LLM_PROVIDER", "openai")
    monkeypatch.setattr("deepcatalog.config.MODEL_NAME", "gpt-4.1")
    assert resolve_model_name() == "gpt-5.6-luna"
