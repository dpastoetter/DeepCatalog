"""Browser security headers and privacy-safe static assets."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.security_headers import BROWSER_SECURITY_HEADERS, CONTENT_SECURITY_POLICY


def test_root_and_api_send_browser_hardening_headers(client):
    for path in ("/", "/api/health", "/static/styles.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        for name, value in BROWSER_SECURITY_HEADERS.items():
            assert resp.headers.get(name) == value, f"{path} missing {name}"


def test_csp_is_strict_and_blocks_third_party():
    csp = CONTENT_SECURITY_POLICY
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-src 'self' blob:" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "fonts.googleapis" not in csp
    assert "worker-src 'self'" in csp
    assert "https:" not in csp


def test_desktop_csp_allows_pywebview_eval(client):
    from app.security_headers import DESKTOP_CONTENT_SECURITY_POLICY

    resp = client.get("/?desktop=1")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy")
    assert csp == DESKTOP_CONTENT_SECURITY_POLICY
    assert "unsafe-eval" in csp
    assert "script-src 'self' 'unsafe-eval'" in csp
    assert "unsafe-inline" not in csp


def test_spa_has_no_external_fonts_or_inline_theme_script():
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert 'src="/static/theme-boot.js' in html
    assert 'localStorage.getItem("dc-theme")' not in html
    assert "<script>\n" not in html

    css = Path("app/static/styles.css").read_text(encoding="utf-8")
    assert "Space Grotesk" not in css
    assert "JetBrains Mono" not in css
    assert "ui-sans-serif" in css or "system-ui" in css


def test_theme_boot_script_is_served(client):
    resp = client.get("/static/theme-boot.js")
    assert resp.status_code == 200
    assert "dc-theme" in resp.text
    assert resp.headers.get("Content-Security-Policy")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


def test_error_responses_also_carry_headers(isolated_data, monkeypatch):
    from deepcatalog.local_security import generate_api_token
    from deepcatalog.sessions import clear_all_sessions

    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", generate_api_token())
    clear_all_sessions()
    bare = TestClient(app)
    resp = bare.get("/api/inbox")
    assert resp.status_code == 401
    assert resp.headers.get("Content-Security-Policy") == CONTENT_SECURITY_POLICY
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_header_value_opt_in_https_non_loopback(monkeypatch):
    from app.security_headers import DEFAULT_HSTS_MAX_AGE, hsts_header_value

    monkeypatch.delenv("DEEPCATALOG_HSTS", raising=False)
    assert hsts_header_value(https=True, host_header="docs.example.com") is None

    monkeypatch.setenv("DEEPCATALOG_HSTS", "1")
    assert hsts_header_value(https=False, host_header="docs.example.com") is None
    assert hsts_header_value(https=True, host_header="localhost:8080") is None
    assert hsts_header_value(https=True, host_header="127.0.0.1") is None
    assert hsts_header_value(https=True, host_header="[::1]:8443") is None

    value = hsts_header_value(https=True, host_header="archive.example.com")
    assert value == f"max-age={DEFAULT_HSTS_MAX_AGE}; includeSubDomains"

    monkeypatch.setenv("DEEPCATALOG_HSTS_MAX_AGE", "3600")
    assert (
        hsts_header_value(https=True, host_header="archive.example.com")
        == "max-age=3600; includeSubDomains"
    )


def test_hsts_on_direct_https_request(isolated_data, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_HSTS", "1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "archive.example.com")
    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)

    client = TestClient(
        app,
        client=("203.0.113.10", 50000),
        base_url="https://archive.example.com",
    )
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.headers.get("Strict-Transport-Security", "").startswith("max-age=")
    assert resp.headers.get("Content-Security-Policy") == CONTENT_SECURITY_POLICY


def test_hsts_not_on_plain_http(isolated_data, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_HSTS", "1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "archive.example.com")
    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)

    client = TestClient(
        app,
        client=("203.0.113.10", 50000),
        base_url="http://archive.example.com",
    )
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_not_on_loopback_even_over_https(isolated_data, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_HSTS", "1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "localhost,127.0.0.1,::1")

    for base in (
        "https://127.0.0.1:8080",
        "https://localhost:8080",
        "https://[::1]:8080",
    ):
        client = TestClient(app, base_url=base)
        resp = client.get("/api/health")
        assert resp.status_code == 200, base
        assert "Strict-Transport-Security" not in resp.headers, base


def test_hsts_via_trusted_reverse_proxy(isolated_data, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_HSTS", "1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "archive.example.com")
    monkeypatch.setenv("DEEPCATALOG_TRUSTED_PROXIES", "10.0.0.1")

    # TLS terminated at the proxy: app sees plain HTTP + trusted X-Forwarded-Proto.
    client = TestClient(
        app,
        client=("10.0.0.1", 50000),
        base_url="http://archive.example.com",
    )
    resp = client.get(
        "/api/health",
        headers={"Host": "archive.example.com", "X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("Strict-Transport-Security", "").startswith("max-age=")


def test_hsts_ignores_spoofed_x_forwarded_proto(isolated_data, monkeypatch):
    monkeypatch.setenv("DEEPCATALOG_HSTS", "1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "archive.example.com")
    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)

    client = TestClient(
        app,
        client=("203.0.113.9", 50000),
        base_url="http://archive.example.com",
    )
    resp = client.get(
        "/api/health",
        headers={"Host": "archive.example.com", "X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 200
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_on_early_error_when_https(isolated_data, monkeypatch):
    """Early middleware 401s must carry the same HSTS decision as success paths."""
    from deepcatalog.local_security import generate_api_token
    from deepcatalog.sessions import clear_all_sessions

    monkeypatch.setenv("DEEPCATALOG_HSTS", "1")
    monkeypatch.setenv("DEEPCATALOG_ALLOWED_HOSTS", "archive.example.com")
    monkeypatch.setenv("DEEPCATALOG_API_TOKEN", generate_api_token())
    monkeypatch.delenv("DEEPCATALOG_TRUSTED_PROXIES", raising=False)
    clear_all_sessions()

    bare = TestClient(
        app,
        client=("203.0.113.10", 50000),
        base_url="https://archive.example.com",
    )
    resp = bare.get("/api/inbox")
    assert resp.status_code == 401
    assert resp.headers.get("Content-Security-Policy") == CONTENT_SECURITY_POLICY
    assert resp.headers.get("Strict-Transport-Security", "").startswith("max-age=")

    http_bare = TestClient(
        app,
        client=("203.0.113.10", 50000),
        base_url="http://archive.example.com",
    )
    http_resp = http_bare.get("/api/inbox")
    # Remote credentials over HTTP are rejected before auth (403 HTTPS required)
    # or 401 — either way must not advertise HSTS on cleartext.
    assert http_resp.status_code in {401, 403}
    assert "Strict-Transport-Security" not in http_resp.headers
