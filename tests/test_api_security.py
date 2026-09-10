"""API security boundary: bind policy, bearer token, session cookies."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import CSRF_HEADER_NAME, CSRF_HEADER_VALUE, app
from deepcatalog.local_security import (
    COOKIE_NAME,
    assert_bind_allowed,
    auth_required_for_request,
    forwarded_client_host,
    generate_api_token,
    is_direct_loopback_request,
    is_loopback_hostname,
    is_trusted_proxy,
    is_wildcard_or_non_loopback_bind,
    remote_auth_must_be_https,
    request_appears_https,
    trusted_proxy_networks,
)
from deepcatalog.sessions import clear_all_sessions, session_is_valid


def test_loopback_bind_helpers():
    assert is_loopback_hostname("127.0.0.1")
    assert is_loopback_hostname("localhost:8080")
    assert is_loopback_hostname("::1")
    assert is_loopback_hostname("testclient")
    assert not is_loopback_hostname("192.168.1.10")
    assert is_wildcard_or_non_loopback_bind("0.0.0.0")
    assert is_wildcard_or_non_loopback_bind("::")
    assert is_wildcard_or_non_loopback_bind("192.168.0.5")
    assert is_wildcard_or_non_loopback_bind("10.0.0.1")
    assert not is_wildcard_or_non_loopback_bind("127.0.0.1")
    assert not is_wildcard_or_non_loopback_bind("localhost")


def test_loopback_is_not_an_auth_boundary(monkeypatch):
    monkeypatch.delenv("DEEPCATALOG_SINGLE_USER", raising=False)
    assert auth_required_for_request(peer_host="127.0.0.1", host_header="localhost") is True
    assert auth_required_for_request(peer_host="testclient", host_header="testserver") is True


def test_single_user_mode_exempts_direct_loopback_only(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_SINGLE_USER", "1")
    assert auth_required_for_request(peer_host="127.0.0.1", host_header="localhost") is False
    assert auth_required_for_request(peer_host="203.0.113.9", host_header="localhost") is True
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "127.0.0.1")
    assert auth_required_for_request(peer_host="127.0.0.1", host_header="localhost") is True


def test_assert_bind_requires_remote_opt_in_token_and_tls(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPCATALOG_API_TOKEN", raising=False)
    monkeypatch.delenv("DEEPCATALOG_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("DEEPCATALOG_SSL_CERTFILE", raising=False)
    monkeypatch.delenv("DEEPCATALOG_SSL_KEYFILE", raising=False)

    with pytest.raises(RuntimeError, match="DEEPCATALOG_ALLOW_REMOTE"):
        assert_bind_allowed("0.0.0.0")

    monkeypatch.setenv("DEEPCATALOG_ALLOW_REMOTE", "1")
    with pytest.raises(RuntimeError, match="DEEPCATALOG_API_TOKEN"):
        assert_bind_allowed("0.0.0.0")

    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", generate_api_token())
    with pytest.raises(RuntimeError, match="DEEPCATALOG_SSL_CERTFILE"):
        assert_bind_allowed("0.0.0.0")

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("DEEPCATALOG_SSL_CERTFILE", str(cert))
    monkeypatch.setenv("DEEPCATALOG_SSL_KEYFILE", str(key))
    assert_bind_allowed("0.0.0.0")  # network mode fully configured
    assert_bind_allowed("192.168.1.20")

    # Loopback stays local mode (HTTP OK, no remote flags needed).
    monkeypatch.delenv("DEEPCATALOG_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("DEEPCATALOG_API_TOKEN", raising=False)
    monkeypatch.delenv("DEEPCATALOG_SSL_CERTFILE", raising=False)
    monkeypatch.delenv("DEEPCATALOG_SSL_KEYFILE", raising=False)
    assert_bind_allowed("127.0.0.1")
    assert_bind_allowed("localhost")


def test_invalid_host_header_rejected(isolated_data):
    client = TestClient(app)
    resp = client.get("/api/health", headers={"Host": "evil.example"})
    assert resp.status_code == 400
    assert "Host" in resp.json()["detail"]


def test_token_required_when_configured(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    clear_all_sessions()
    bare = TestClient(app)
    denied = bare.get("/api/inbox")
    assert denied.status_code == 401

    ok = bare.get(
        "/api/inbox",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200

    # API secret must not work as a cookie value anymore.
    bare.cookies.set(COOKIE_NAME, token)
    assert bare.get("/api/inbox").status_code == 401


def test_session_exchange_sets_independent_cookie(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    clear_all_sessions()
    client = TestClient(app)
    client.headers.update({CSRF_HEADER_NAME: CSRF_HEADER_VALUE})

    bad = client.post("/api/auth/session", json={"token": "wrong-token-value"})
    assert bad.status_code == 401

    exchanged = client.post("/api/auth/session", json={"token": token})
    assert exchanged.status_code == 200
    session = exchanged.cookies.get(COOKIE_NAME)
    assert session
    assert session != token
    assert session_is_valid(session)

    # Cookie authenticates subsequent API calls without Bearer.
    authed = TestClient(app)
    authed.cookies.set(COOKIE_NAME, session)
    assert authed.get("/api/inbox").status_code == 200

    # Logout revokes the hashed session.
    authed.headers.update({CSRF_HEADER_NAME: CSRF_HEADER_VALUE})
    logged_out = authed.post("/api/auth/session/logout")
    assert logged_out.status_code == 200
    assert not session_is_valid(session)


def test_mutations_still_need_csrf_with_token(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    client = TestClient(app)
    resp = client.post(
        "/api/process-inbox",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "cross-site" in resp.json()["detail"]

    resp = client.post(
        "/api/process-inbox",
        headers={
            "Authorization": f"Bearer {token}",
            CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
        },
    )
    assert resp.status_code == 200


def test_health_exempt_from_bearer(isolated_data, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", generate_api_token())
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "version": body["version"]}
    assert "auth" not in body
    assert "usage" not in body
    assert "llm_provider" not in body


def test_diagnostics_requires_bearer_when_token_set(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    client = TestClient(app)
    assert client.get("/api/diagnostics").status_code == 401
    ok = client.get("/api/diagnostics", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] in {"ok", "degraded"}
    assert "usage" in body
    assert "auth" in body
    assert "llm_provider" in body


def test_index_does_not_auto_login_loopback(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    clear_all_sessions()
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert COOKIE_NAME not in resp.cookies
    assert "PA_API_TOKEN" not in resp.text
    assert client.get("/api/inbox").status_code == 401


def test_single_user_mode_sets_session_cookie_on_loopback(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    monkeypatch.setenv("DEEPCATALOG_SINGLE_USER", "1")
    clear_all_sessions()
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert COOKIE_NAME in resp.cookies
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie
    assert cookie != token
    assert session_is_valid(cookie)


def test_query_token_is_ignored(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "deepcatalog.example.com")
    clear_all_sessions()
    # Non-loopback peer so localhost auto-login cannot mask a query exchange.
    client = TestClient(app, client=("203.0.113.9", 50000))
    headers = {"Host": "deepcatalog.example.com"}
    resp = client.get(f"/?token={token}", headers=headers, follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers.get("location") is None
    assert not resp.cookies.get(COOKIE_NAME)
    assert client.get("/api/inbox", headers=headers).status_code == 401
    assert client.get(f"/api/inbox?token={token}", headers=headers).status_code == 401
    assert client.get(f"/api/inbox?access_token={token}", headers=headers).status_code == 401
    assert client.get(f"/api/diagnostics?token={token}", headers=headers).status_code == 401
    session = client.get(f"/api/auth/session?token={token}", headers=headers)
    assert session.status_code in {401, 403, 405, 422}
    assert not session.cookies.get(COOKIE_NAME)


def test_loopback_query_token_does_not_authenticate(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    clear_all_sessions()
    client = TestClient(app)
    assert client.get(f"/api/inbox?token={token}").status_code == 401
    resp = client.get(f"/?token={token}")
    assert resp.status_code == 200
    assert not resp.cookies.get(COOKIE_NAME)
    assert client.get("/api/inbox").status_code == 401


def test_forwarded_headers_only_from_trusted_proxies(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "10.0.0.1,192.168.0.0/24")
    # Untrusted peer — ignore spoofed XFF.
    assert (
        forwarded_client_host(
            peer_host="8.8.8.8",
            x_forwarded_for="1.2.3.4",
        )
        == "8.8.8.8"
    )
    # Trusted peer — walk right-to-left, skip trusted hops, take the client.
    assert (
        forwarded_client_host(
            peer_host="10.0.0.1",
            x_forwarded_for="203.0.113.9, 10.0.0.1",
        )
        == "203.0.113.9"
    )
    assert not request_appears_https(
        peer_host="8.8.8.8",
        url_scheme="http",
        x_forwarded_proto="https",
    )
    assert request_appears_https(
        peer_host="10.0.0.1",
        url_scheme="http",
        x_forwarded_proto="https",
    )
    assert not remote_auth_must_be_https(
        peer_host="testclient",
        host_header="testserver",
        url_scheme="http",
        x_forwarded_proto=None,
    )
    assert remote_auth_must_be_https(
        peer_host="203.0.113.9",
        host_header="deepcatalog.example.com",
        url_scheme="http",
        x_forwarded_proto=None,
    )
    assert not remote_auth_must_be_https(
        peer_host="203.0.113.9",
        host_header="deepcatalog.example.com",
        url_scheme="https",
        x_forwarded_proto=None,
    )
    assert not remote_auth_must_be_https(
        peer_host="10.0.0.1",
        host_header="deepcatalog.example.com",
        url_scheme="http",
        x_forwarded_proto="https",
    )
    assert remote_auth_must_be_https(
        peer_host="8.8.8.8",
        host_header="deepcatalog.example.com",
        url_scheme="http",
        x_forwarded_proto="https",
    )
    # Spoofed loopback on the left must not win over the real client on the right.
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "127.0.0.1")
    # nginx/Caddy on loopback is a trusted hop, not a direct browser — HTTP is not enough.
    assert remote_auth_must_be_https(
        peer_host="127.0.0.1",
        host_header="localhost",
        url_scheme="http",
        x_forwarded_proto=None,
    )
    assert not remote_auth_must_be_https(
        peer_host="127.0.0.1",
        host_header="localhost",
        url_scheme="http",
        x_forwarded_proto="https",
    )
    assert (
        forwarded_client_host(
            peer_host="127.0.0.1",
            x_forwarded_for="127.0.0.2, 203.0.113.9",
        )
        == "203.0.113.9"
    )


def test_forwarded_for_skips_only_explicit_trusted_hops(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "10.0.0.1")
    # 10.0.0.2 is on the same LAN but not listed — it is the client, not a hop to skip.
    assert (
        forwarded_client_host(
            peer_host="10.0.0.1",
            x_forwarded_for="198.51.100.20, 10.0.0.2, 10.0.0.1",
        )
        == "10.0.0.2"
    )


def test_malformed_forwarded_for_is_rejected(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "10.0.0.1")
    # Hostname / garbage hops invalidate the header — do not treat them as the client.
    assert (
        forwarded_client_host(
            peer_host="10.0.0.1",
            x_forwarded_for="203.0.113.9, not-an-ip, 10.0.0.1",
        )
        == "10.0.0.1"
    )
    assert (
        forwarded_client_host(
            peer_host="10.0.0.1",
            x_forwarded_for="localhost, 10.0.0.1",
        )
        == "10.0.0.1"
    )
    assert (
        forwarded_client_host(
            peer_host="10.0.0.1",
            x_forwarded_for="203.0.113.9:8080, 10.0.0.1",
        )
        == "10.0.0.1"
    )
    assert (
        forwarded_client_host(
            peer_host="10.0.0.1",
            x_forwarded_for="unknown",
        )
        == "10.0.0.1"
    )


def test_catch_all_trusted_proxy_cidrs_are_ignored(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "0.0.0.0/0,::/0,not-a-cidr")
    assert trusted_proxy_networks() == []
    assert not is_trusted_proxy("8.8.8.8")
    assert not is_trusted_proxy("2001:db8::1")
    assert (
        forwarded_client_host(
            peer_host="8.8.8.8",
            x_forwarded_for="1.2.3.4",
        )
        == "8.8.8.8"
    )
    assert not request_appears_https(
        peer_host="8.8.8.8",
        url_scheme="http",
        x_forwarded_proto="https",
    )
    # A real proxy IP still works when listed beside a discarded catch-all.
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "0.0.0.0/0,10.0.0.1")
    assert is_trusted_proxy("10.0.0.1")
    assert not is_trusted_proxy("8.8.8.8")


def test_spoofed_forwarded_proto_from_untrusted_peer(monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "10.0.0.1")
    assert not request_appears_https(
        peer_host="203.0.113.9",
        url_scheme="http",
        x_forwarded_proto="https",
    )
    assert not request_appears_https(
        peer_host="10.0.0.1",
        url_scheme="http",
        x_forwarded_proto="https://evil.example",
    )
    assert not request_appears_https(
        peer_host="10.0.0.1",
        url_scheme="http",
        x_forwarded_proto="https extra",
    )
    # Client-spoofed https on the left; nearest hop from the proxy is http.
    assert not request_appears_https(
        peer_host="10.0.0.1",
        url_scheme="http",
        x_forwarded_proto="https, http",
    )
    assert request_appears_https(
        peer_host="10.0.0.1",
        url_scheme="http",
        x_forwarded_proto="http, https",
    )


def test_direct_loopback_request_ignores_forwarded_headers(monkeypatch):
    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)
    assert is_direct_loopback_request(peer_host="127.0.0.1", host_header="localhost")
    assert is_direct_loopback_request(peer_host="testclient", host_header="testserver")
    assert not is_direct_loopback_request(
        peer_host="127.0.0.1", host_header="deepcatalog.example.com"
    )
    assert not is_direct_loopback_request(peer_host="203.0.113.9", host_header="localhost")

    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "127.0.0.1,::1")
    # nginx on loopback is a trusted hop — never treat it as the user.
    assert not is_direct_loopback_request(peer_host="127.0.0.1", host_header="localhost")
    assert not is_direct_loopback_request(
        peer_host="127.0.0.1", host_header="deepcatalog.example.com"
    )


def test_spoofed_xff_does_not_issue_session_behind_proxy(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "deepcatalog.example.com")
    clear_all_sessions()
    client = TestClient(app, client=("127.0.0.1", 50000))
    spoof = {
        "Host": "deepcatalog.example.com",
        "X-Forwarded-For": "127.0.0.2",
    }
    resp = client.get("/", headers=spoof)
    assert resp.status_code == 200
    assert not resp.cookies.get(COOKIE_NAME)
    assert client.get("/api/inbox", headers=spoof).status_code == 401

    # Host: localhost is allowlisted but still must not auto-login via the proxy.
    local_host = {"Host": "localhost", "X-Forwarded-For": "127.0.0.2"}
    resp = client.get("/", headers=local_host)
    assert not resp.cookies.get(COOKIE_NAME)
    assert client.get("/api/inbox", headers=local_host).status_code == 401


def test_spoofed_forwarded_proto_does_not_issue_session(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "deepcatalog.example.com")
    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)
    clear_all_sessions()
    client = TestClient(app, client=("203.0.113.9", 50000))
    spoof = {
        "Host": "deepcatalog.example.com",
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }
    resp = client.get("/", headers=spoof)
    assert resp.status_code == 200
    assert not resp.cookies.get(COOKIE_NAME)
    assert client.get("/api/inbox", headers=spoof).status_code == 401


def test_spoofed_xff_does_not_skip_auth_without_token(isolated_data, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "deepcatalog.example.com")
    client = TestClient(app, client=("127.0.0.1", 50000))
    # After lifespan (which would generate a token) drop it so the request is unauthenticated.
    monkeypatch.delenv("DEEPCATALOG_API_TOKEN", raising=False)
    resp = client.get(
        "/api/inbox",
        headers={"Host": "deepcatalog.example.com", "X-Forwarded-For": "127.0.0.2"},
    )
    assert resp.status_code == 403
    assert "DEEPCATALOG_API_TOKEN" in resp.json()["detail"]


def test_remote_credentials_require_https(isolated_data, monkeypatch):
    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "deepcatalog.example.com")
    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)
    clear_all_sessions()
    auth = {"Authorization": f"Bearer {token}"}

    loopback = TestClient(app)
    assert loopback.get("/api/inbox", headers=auth).status_code == 200

    remote_http = TestClient(
        app, client=("203.0.113.9", 50000), base_url="http://deepcatalog.example.com"
    )
    denied = remote_http.get("/api/inbox", headers=auth)
    assert denied.status_code == 403
    assert "HTTPS" in denied.json()["detail"]

    remote_https = TestClient(
        app, client=("203.0.113.9", 50000), base_url="https://deepcatalog.example.com"
    )
    assert remote_https.get("/api/inbox", headers=auth).status_code == 200

    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "10.0.0.1")
    via_proxy = TestClient(
        app, client=("10.0.0.1", 50000), base_url="http://deepcatalog.example.com"
    )
    assert (
        via_proxy.get(
            "/api/inbox",
            headers={**auth, "X-Forwarded-Proto": "https"},
        ).status_code
        == 200
    )

    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)
    spoofed = TestClient(app, client=("8.8.8.8", 50000), base_url="http://deepcatalog.example.com")
    spoofed_denied = spoofed.get(
        "/api/inbox",
        headers={**auth, "X-Forwarded-Proto": "https"},
    )
    assert spoofed_denied.status_code == 403
    assert "HTTPS" in spoofed_denied.json()["detail"]

    session_http = TestClient(
        app, client=("203.0.113.9", 50000), base_url="http://deepcatalog.example.com"
    )
    session_denied = session_http.post(
        "/api/auth/session",
        json={"token": token},
        headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
    )
    assert session_denied.status_code == 403
    assert "HTTPS" in session_denied.json()["detail"]

    no_cred = TestClient(
        app, client=("203.0.113.9", 50000), base_url="http://deepcatalog.example.com"
    )
    missing = no_cred.get("/api/inbox")
    assert missing.status_code == 401
    assert "HTTPS" not in missing.json()["detail"]


def test_desktop_bootstrap_sets_session_on_loopback(isolated_data, monkeypatch):
    from deepcatalog.desktop_bootstrap import clear_desktop_bootstrap, mint_desktop_bootstrap

    token = generate_api_token()
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", token)
    clear_all_sessions()
    clear_desktop_bootstrap()
    nonce = mint_desktop_bootstrap()
    client = TestClient(app)
    resp = client.get(f"/api/auth/desktop-bootstrap/{nonce}?desktop=1", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?desktop=1"
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie
    assert cookie != token
    assert session_is_valid(cookie)
    authed = TestClient(app)
    authed.cookies.set(COOKIE_NAME, cookie)
    assert authed.get("/api/inbox").status_code == 200
    # Single-use.
    replay = client.get(f"/api/auth/desktop-bootstrap/{nonce}", follow_redirects=False)
    assert replay.status_code == 401


def test_desktop_bootstrap_rejects_non_loopback(isolated_data, monkeypatch):
    from deepcatalog.desktop_bootstrap import clear_desktop_bootstrap, mint_desktop_bootstrap

    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "deepcatalog.example.com")
    clear_desktop_bootstrap()
    nonce = mint_desktop_bootstrap()
    client = TestClient(
        app, client=("203.0.113.9", 50000), base_url="https://deepcatalog.example.com"
    )
    resp = client.get(
        f"/api/auth/desktop-bootstrap/{nonce}",
        headers={"Host": "deepcatalog.example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert "loopback" in resp.json()["detail"]
    # Nonce is still valid for a later loopback consume.
    local = TestClient(app)
    ok = local.get(f"/api/auth/desktop-bootstrap/{nonce}", follow_redirects=False)
    assert ok.status_code == 303
