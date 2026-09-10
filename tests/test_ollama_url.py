"""SSRF / privacy guards for Ollama base URLs."""

from __future__ import annotations

import ipaddress
import os

import pytest

from deepcatalog.ollama_url import (
    ALLOW_REMOTE_OLLAMA_ENV,
    ALLOWED_HOSTS_ENV,
    apply_openai_compat_env_for_ollama,
    is_loopback_ollama_url,
    ollama_client,
    ollama_openai_compatible_base_url,
    pin_tcp_host,
    public_ollama_config_error,
    require_ollama_base_url,
    trusted_ollama_origin,
    validate_ollama_base_url,
    verify_connected_peer,
)


@pytest.fixture(autouse=True)
def _clear_remote_ollama_allowlist(monkeypatch):
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_local_ollama_urls_allowed(url):
    assert is_loopback_ollama_url(url)
    assert validate_ollama_base_url(url, allow_remote=False) == url.rstrip("/")


def test_rejects_non_http_schemes():
    with pytest.raises(ValueError, match="http:// or https://"):
        validate_ollama_base_url("file:///etc/passwd", allow_remote=False)


def test_rejects_credentials_and_paths():
    with pytest.raises(ValueError, match="credentials"):
        validate_ollama_base_url("http://user:pass@127.0.0.1:11434", allow_remote=False)
    with pytest.raises(ValueError, match="origin only"):
        validate_ollama_base_url("http://127.0.0.1:11434/api/tags", allow_remote=False)


def test_rejects_lan_without_remote_opt_in():
    with pytest.raises(ValueError, match="non-loopback|Remote Ollama"):
        require_ollama_base_url("http://192.168.1.50:11434", allow_remote=False)


def test_allows_lan_with_remote_opt_in(monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_OLLAMA_ENV, raising=False)
    url = validate_ollama_base_url("http://192.168.1.50:11434", allow_remote=True)
    assert url == "http://192.168.1.50:11434"
    assert require_ollama_base_url("http://10.0.0.8:11434", allow_remote=True)


def test_rejects_link_local_metadata_even_when_remote_allowed():
    with pytest.raises(ValueError, match="link-local|metadata|blocked"):
        validate_ollama_base_url("http://169.254.169.254/", allow_remote=True)


def test_rejects_blocked_metadata_hostname(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "metadata.google.internal"
        return [
            (0, 0, 0, "", ("169.254.169.254", 0)),
        ]

    monkeypatch.setattr("deepcatalog.ollama_url.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="not allowed|link-local|blocked"):
        validate_ollama_base_url("http://metadata.google.internal/", allow_remote=True)


def test_rejects_dns_rebinding_to_metadata(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "evil.example")

    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [(0, 0, 0, "", ("169.254.169.254", 0))]

    monkeypatch.setattr("deepcatalog.ollama_url.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="link-local|blocked"):
        validate_ollama_base_url("http://evil.example:11434", allow_remote=True)


def test_remote_hostname_requires_allowlist(monkeypatch):
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)

    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [(0, 0, 0, "", ("192.168.1.20", 0))]

    monkeypatch.setattr("deepcatalog.ollama_url.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        validate_ollama_base_url("http://ollama.lan:11434", allow_remote=True)


def test_remote_hostname_allowed_when_listed(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "ollama.lan")

    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "ollama.lan"
        return [(0, 0, 0, "", ("192.168.1.20", 0))]

    monkeypatch.setattr("deepcatalog.ollama_url.socket.getaddrinfo", fake_getaddrinfo)
    url = validate_ollama_base_url("http://ollama.lan:11434", allow_remote=True)
    assert url == "http://ollama.lan:11434"
    assert trusted_ollama_origin("http://ollama.lan:11434", allow_remote=True) == (
        "http://ollama.lan:11434"
    )


def test_pin_tcp_host_rejects_rebinding_after_validation(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "gpu.lan")
    calls = {"n": 0}

    def fake_getaddrinfo(host, *_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [(0, 0, 0, "", ("192.168.1.20", 0))]
        return [(0, 0, 0, "", ("169.254.169.254", 0))]

    monkeypatch.setattr("deepcatalog.ollama_url.socket.getaddrinfo", fake_getaddrinfo)
    assert validate_ollama_base_url("http://gpu.lan:11434", allow_remote=True)
    with pytest.raises(ValueError, match="link-local|blocked"):
        pin_tcp_host("gpu.lan")


def test_pin_tcp_host_returns_literal_ip(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "gpu.lan")

    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [(0, 0, 0, "", ("10.0.0.8", 0))]

    monkeypatch.setattr("deepcatalog.ollama_url.socket.getaddrinfo", fake_getaddrinfo)
    assert pin_tcp_host("gpu.lan") == "10.0.0.8"


def test_verify_connected_peer_rejects_link_local():
    class FakeStream:
        @staticmethod
        def get_extra_info(name: str):
            assert name == "server_addr"
            return ("169.254.169.254", 80)

    with pytest.raises(ValueError, match="blocked"):
        verify_connected_peer(FakeStream())


def test_verify_connected_peer_uses_httpcore_server_addr():
    class FakeStream:
        @staticmethod
        def get_extra_info(name: str):
            if name == "peername":
                return None
            if name == "server_addr":
                return ("127.0.0.1", 11434)
            return None

    verify_connected_peer(FakeStream())


def test_verify_connected_peer_falls_back_to_socket():
    class FakeSock:
        @staticmethod
        def getpeername():
            return ("127.0.0.1", 11434)

    class FakeStream:
        @staticmethod
        def get_extra_info(name: str):
            if name == "socket":
                return FakeSock()
            return None

    verify_connected_peer(FakeStream())


def test_verify_connected_peer_unwraps_ipv4_mapped():
    class FakeStream:
        @staticmethod
        def get_extra_info(name: str):
            if name == "server_addr":
                return ("::ffff:169.254.169.254", 80)
            return None

    with pytest.raises(ValueError, match="blocked"):
        verify_connected_peer(FakeStream())


def test_allowlist_restricts_literal_ip_when_set(monkeypatch):
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "10.0.0.8")
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        validate_ollama_base_url("http://192.168.1.50:11434", allow_remote=True)
    assert validate_ollama_base_url("http://10.0.0.8:11434", allow_remote=True)


def test_ollama_client_installs_validating_backend():
    client = ollama_client(origin="http://127.0.0.1:11434", timeout=1)
    try:
        backend = client._transport._pool._network_backend
        assert type(backend).__name__ == "_ValidatingSyncBackend"
        assert client._transport._pool._network_backend is backend
    finally:
        client.close()


def test_api_rejects_remote_url_without_allow_remote(client):
    resp = client.post(
        "/api/ollama/enable",
        json={"base_url": "http://192.168.1.50:11434", "allow_remote": False},
    )
    assert resp.status_code == 400
    assert "allow_remote" in resp.json()["detail"].lower()


def test_api_rejects_remote_url_without_disclaimer(client, isolated_data):
    from deepcatalog.privacy import clear_privacy_cache, revoke_cloud_disclaimer

    revoke_cloud_disclaimer()
    clear_privacy_cache()
    resp = client.post(
        "/api/ollama/enable",
        json={"base_url": "http://192.168.1.50:11434", "allow_remote": True},
    )
    assert resp.status_code == 403


def test_api_rejects_ssrf_metadata_target(client):
    # Even with allow_remote + disclaimer, metadata IPs stay blocked.
    assert client.post("/api/privacy/cloud-disclaimer", json={"accepted": True}).status_code == 200
    resp = client.post(
        "/api/ollama/enable",
        json={"base_url": "http://169.254.169.254", "allow_remote": True},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "link-local" in detail or "blocked" in detail or "metadata" in detail


def test_trusted_origin_pins_loopback_and_rebuilds_remote():
    assert trusted_ollama_origin("http://localhost:11434") == "http://127.0.0.1:11434"
    assert trusted_ollama_origin("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert trusted_ollama_origin("http://[::1]:11434") == "http://[::1]:11434"
    assert (
        trusted_ollama_origin("http://192.168.1.50:11434", allow_remote=True)
        == "http://192.168.1.50:11434"
    )


def test_public_ollama_config_error_is_stable():
    assert (
        "blocked"
        in public_ollama_config_error(
            ValueError(
                "Ollama base URL resolves to a blocked address (169.254.169.254): link-local"
            )
        ).lower()
    )
    assert "Remote Ollama is disabled" in public_ollama_config_error(
        ValueError("Remote Ollama is disabled. Use localhost")
    )
    assert public_ollama_config_error(ValueError("traceback-looking junk")) == "Invalid Ollama URL"
    assert ALLOWED_HOSTS_ENV in public_ollama_config_error(
        ValueError(f"Remote Ollama hostname 'x' is not in {ALLOWED_HOSTS_ENV}.")
    )


def test_literal_ip_classification():
    assert ipaddress.ip_address("169.254.169.254").is_link_local
    assert ipaddress.ip_address("127.0.0.1").is_loopback


def test_openai_compat_base_url_rebuilds_via_trusted_origin():
    assert ollama_openai_compatible_base_url("http://localhost:11434") == (
        "http://127.0.0.1:11434/v1"
    )
    assert (
        ollama_openai_compatible_base_url("http://192.168.1.50:11434", allow_remote=True)
        == "http://192.168.1.50:11434/v1"
    )


def test_openai_compat_env_pins_localhost_and_rejects_remote(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    applied = apply_openai_compat_env_for_ollama("http://localhost:11434")
    assert applied == "http://127.0.0.1:11434/v1"
    assert os.environ["OPENAI_BASE_URL"] == applied
    assert os.environ["OPENAI_API_KEY"] == "ollama"
    with pytest.raises(ValueError, match="non-loopback|Remote Ollama"):
        apply_openai_compat_env_for_ollama("http://192.168.1.50:11434", allow_remote=False)
