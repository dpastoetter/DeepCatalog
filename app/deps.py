"""Shared path/security helpers for API routers."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from deepcatalog.config import ARCHIVE_DIR
from deepcatalog.local_security import (
    AUTH_HEADER,
    COOKIE_NAME,
    extract_bearer_token,
    forwarded_client_host,
    get_api_token,
    host_header_allowed,
    request_appears_https,
    token_matches,
)
from deepcatalog.privacy import require_cloud_disclaimer
from deepcatalog.sessions import session_is_valid
from deepcatalog.settings import load_settings

# CSRF: browsers cannot attach custom headers to cross-site form posts without
# CORS preflight (which this app never grants).
CSRF_HEADER_NAME = "X-Requested-With"
CSRF_HEADER_VALUE = "DeepCatalog"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Health + session bootstrap must work before a cookie exists.
AUTH_EXEMPT_PATHS = frozenset(
    {
        "/api/health",
        "/api/auth/session",
        "/api/auth/session/status",
        "/api/auth/session/logout",
    }
)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # generous cap for large scans


def origin_matches_host_header(origin: str, host_header: str | None) -> bool:
    """True when Origin is http(s) and its host[:port] equals the Host header."""
    origin = (origin or "").strip()
    host_header = (host_header or "").strip()
    if not origin or not host_header:
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False
    authority = (parsed.netloc or "").strip().lower()
    if not authority or authority != host_header.lower():
        return False
    return host_header_allowed(host_header)


def request_passes_csrf(request: Request) -> bool:
    """Custom header, or a same-origin browser POST (WebKitGTK drops FormData headers)."""
    if request.headers.get(CSRF_HEADER_NAME) == CSRF_HEADER_VALUE:
        return True
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site in {"same-origin", "same-site"}:
        return True
    return origin_matches_host_header(
        request.headers.get("origin") or "",
        request.headers.get("host"),
    )


def peer_host(request: Request) -> str | None:
    """Immediate TCP peer (never taken from X-Forwarded-*)."""
    if request.client is None:
        return None
    return request.client.host


def client_host(request: Request) -> str | None:
    """
    End-user client host for logging / forwarded HTTPS detection / rate limits.

    Honors X-Forwarded-For only when the TCP peer is listed in
    DEEPCATALOG_TRUSTED_PROXIES. Never use this to grant authentication.
    """
    return forwarded_client_host(
        peer_host=peer_host(request),
        x_forwarded_for=request.headers.get("x-forwarded-for"),
    )


def rate_limit_ip(request: Request) -> str:
    """Client IP used for auth rate limits (trusted-proxy XFF when applicable)."""
    return (client_host(request) or peer_host(request) or "unknown").strip() or "unknown"


def request_is_https(request: Request) -> bool:
    return request_appears_https(
        peer_host=peer_host(request),
        url_scheme=request.url.scheme,
        x_forwarded_proto=request.headers.get("x-forwarded-proto"),
    )


def request_presents_credentials(request: Request) -> bool:
    """True when the request carries a Bearer header or a deepcatalog_session cookie."""
    if (request.headers.get(AUTH_HEADER) or "").strip():
        return True
    cookie = request.cookies.get(COOKIE_NAME)
    return bool(cookie and str(cookie).strip())


def request_has_valid_token(request: Request) -> bool:
    """
    Authenticate via Bearer DEEPCATALOG_API_TOKEN (machine clients) or deepcatalog_session cookie.

    The cookie must be a random session id — never the long-lived API secret.
    Query parameters (``token``, ``access_token``, …) are never authentication;
    they leak via history, Referer, proxies, and access logs.
    """
    expected = get_api_token()
    if not expected:
        return False
    bearer = extract_bearer_token(request.headers.get(AUTH_HEADER))
    if token_matches(bearer, expected):
        return True
    return session_is_valid(request.cookies.get(COOKIE_NAME))


def is_within(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def archive_roots() -> list[Path]:
    """Directories archived documents are allowed to be served from."""
    roots = [Path(ARCHIVE_DIR).expanduser().resolve()]
    for category in load_settings().get("categories", []):
        folder = category.get("folder")
        if folder:
            roots.append(Path(folder).expanduser().resolve())
    return roots


def require_cloud_disclaimer_or_403() -> None:
    try:
        require_cloud_disclaimer()
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=(
                "Approve the cloud processing disclaimer in Settings → AI provider "
                "before signing in or saving a cloud API key."
            ),
        ) from None
