"""Tests for Ollama unload/restart helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from deepcatalog import ollama_setup


def test_list_running_models_parses_ps_response():
    mock_resp = MagicMock()
    mock_resp.content = b'{"models":[{"name":"gemma3:4b"}]}'
    mock_resp.json.return_value = {"models": [{"name": "gemma3:4b"}]}
    mock_resp.raise_for_status = MagicMock()

    with patch("deepcatalog.ollama_setup._ollama_client") as client_fn:
        client = client_fn.return_value.__enter__.return_value
        client.get.return_value = mock_resp
        result = ollama_setup.list_running_models()

    assert result["status"] == "success"
    assert result["models"][0]["name"] == "gemma3:4b"


def test_unload_model_posts_keep_alive_zero():
    chat_resp = MagicMock()
    chat_resp.raise_for_status = MagicMock()
    embed_resp = MagicMock()
    embed_resp.raise_for_status = MagicMock()

    with patch("deepcatalog.ollama_setup._ollama_client") as client_fn:
        client = client_fn.return_value.__enter__.return_value
        client.post.side_effect = [chat_resp, embed_resp]
        with (
            patch(
                "deepcatalog.ollama_setup.resolve_runtime_model",
                side_effect=lambda name, **_: name,
            ),
            patch(
                "deepcatalog.ollama_setup.list_running_models",
                return_value={"status": "success", "models": []},
            ),
        ):
            result = ollama_setup.unload_model()

    assert result["status"] == "success"
    payloads = [
        call.args[1] if len(call.args) > 1 else call.kwargs.get("json")
        for call in client.post.call_args_list
    ]
    assert payloads[0]["keep_alive"] == 0
    assert payloads[1]["keep_alive"] == 0
