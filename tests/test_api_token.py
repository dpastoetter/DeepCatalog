"""First-launch API token generation and DATA_DIR persistence."""

from __future__ import annotations

import os

from deepcatalog.api_token import ensure_api_token, token_env_path
from deepcatalog.desktop_bootstrap import (
    consume_desktop_bootstrap,
    mint_desktop_bootstrap,
)
from deepcatalog.local_security import TOKEN_ENV, generate_api_token


def test_ensure_api_token_keeps_existing(monkeypatch, isolated_data):
    token = generate_api_token()
    monkeypatch.setenv(TOKEN_ENV, token)
    assert ensure_api_token() == token
    assert os.environ[TOKEN_ENV] == token


def test_ensure_api_token_generates_and_persists(monkeypatch, isolated_data):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    token = ensure_api_token(persist=True)
    assert token
    assert os.environ[TOKEN_ENV] == token
    env_file = token_env_path()
    assert env_file.is_file()
    text = env_file.read_text(encoding="utf-8")
    assert f"{TOKEN_ENV}={token}" in text
    mode = env_file.stat().st_mode & 0o777
    if os.name != "nt":
        assert mode == 0o600
    # Second call is stable.
    assert ensure_api_token() == token


def test_desktop_bootstrap_nonce_is_single_use(isolated_data):
    nonce = mint_desktop_bootstrap()
    assert consume_desktop_bootstrap(nonce) is True
    assert consume_desktop_bootstrap(nonce) is False
    assert consume_desktop_bootstrap("wrong-nonce-value-here") is False


def test_desktop_bootstrap_wrong_nonce_does_not_burn(isolated_data):
    nonce = mint_desktop_bootstrap()
    assert consume_desktop_bootstrap("not-the-nonce-xxxxxxxx") is False
    assert consume_desktop_bootstrap(nonce) is True
