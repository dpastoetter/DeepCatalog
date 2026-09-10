"""FastAPI application entry: lifespan, security middleware, static UI, routers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.deps import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    MAX_UPLOAD_BYTES,
    MUTATING_METHODS,
    path_is_auth_exempt,
    peer_host,
    rate_limit_ip,
    request_has_valid_token,
    request_is_https,
    request_passes_csrf,
    request_presents_credentials,
)
from app.routers import build_api_router
from app.security_headers import DESKTOP_CONTENT_SECURITY_POLICY, apply_browser_security_headers
from deepcatalog.access_log import install_access_log_redaction
from deepcatalog.api_token import ensure_api_token
from deepcatalog.auth_rate_limit import (
    RATE_LIMIT_DETAIL,
    get_auth_rate_limiter,
    log_rate_limited,
    rate_limit_response_headers,
)
from deepcatalog.config import ensure_data_dirs
from deepcatalog.env_permissions import ensure_dotenv_permissions
from deepcatalog.inbox_worker import inbox_poll_loop
from deepcatalog.local_security import (
    COOKIE_NAME,
    assert_bind_allowed,
    auth_required_for_request,
    effective_bind_host,
    get_api_token,
    host_header_allowed,
    is_direct_loopback_request,
    remote_auth_must_be_https,
    single_user_desktop_enabled,
)
from deepcatalog.review import recover_stale_processing
from deepcatalog.sessions import (
    attach_session_cookie,
    create_session,
    session_is_valid,
)
from deepcatalog.settings import load_settings
from deepcatalog.version import get_current_version

__all__ = [
    "CSRF_HEADER_NAME",
    "CSRF_HEADER_VALUE",
    "MAX_UPLOAD_BYTES",
    "app",
]

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    ensure_dotenv_permissions(fix=True)
    ensure_api_token()
    install_access_log_redaction()
    assert_bind_allowed(effective_bind_host())
    load_settings()
    recover_stale_processing()
    stop_event = asyncio.Event()
    poller = asyncio.create_task(inbox_poll_loop(stop_event), name="inbox-poller")
    logger.info("Started inbox poller task")
    try:
        yield
    finally:
        stop_event.set()
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="DeepCatalog Studio",
    description="Deep document intelligence, local-first.",
    version=get_current_version(),
    lifespan=lifespan,
)


@app.middleware("http")
async def security_boundary(request: Request, call_next):
    """Host allowlist, request-time HTTPS for credentials, auth, CSRF, headers."""
    path = request.url.path

    if path.startswith("/api/") or path == "/" or path.startswith("/static/"):
        if not host_header_allowed(request.headers.get("host")):
            return apply_browser_security_headers(
                JSONResponse(
                    status_code=400,
                    content={"detail": "invalid Host header"},
                )
            )

    tcp_peer = peer_host(request)
    # Reject credentials (and session exchange) from non-loopback clients unless
    # the request is confirmed HTTPS. Do this before token checks so the secret
    # is not processed over plain HTTP.
    if (
        path.startswith("/api/")
        and path != "/api/health"
        and (request_presents_credentials(request) or path == "/api/auth/session")
        and remote_auth_must_be_https(
            peer_host=tcp_peer,
            host_header=request.headers.get("host"),
            url_scheme=request.url.scheme,
            x_forwarded_proto=request.headers.get("x-forwarded-proto"),
        )
    ):
        return apply_browser_security_headers(
            JSONResponse(
                status_code=403,
                content={"detail": "HTTPS required"},
            )
        )

    needs_auth = path.startswith("/api/") and not path_is_auth_exempt(path)
    if needs_auth and auth_required_for_request(
        peer_host=tcp_peer,
        host_header=request.headers.get("host"),
    ):
        if not get_api_token():
            return apply_browser_security_headers(
                JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "API access requires DEEPCATALOG_API_TOKEN "
                            "(loopback is not an authentication boundary; "
                            "set DEEPCATALOG_SINGLE_USER=1 only on a dedicated machine)"
                        )
                    },
                )
            )
        limiter = get_auth_rate_limiter()
        client_ip = rate_limit_ip(request)
        allowed, retry_after = limiter.check_api_auth(client_ip)
        if not allowed:
            log_rate_limited(client_ip, path, retry_after)
            return apply_browser_security_headers(
                JSONResponse(
                    status_code=429,
                    content={"detail": RATE_LIMIT_DETAIL},
                    headers=rate_limit_response_headers(retry_after),
                )
            )
        if not request_has_valid_token(request):
            limiter.record_api_auth_failure(client_ip, path)
            return apply_browser_security_headers(
                JSONResponse(
                    status_code=401,
                    content={
                        "detail": (
                            "authentication required — send Authorization: Bearer "
                            f"<DEEPCATALOG_API_TOKEN> or create a browser session via "
                            f"POST /api/auth/session (cookie {COOKIE_NAME})"
                        )
                    },
                )
            )

    if (
        request.method in MUTATING_METHODS
        and path.startswith("/api/")
        and not request_passes_csrf(request)
    ):
        return apply_browser_security_headers(
            JSONResponse(
                status_code=403,
                content={
                    "detail": (f"missing {CSRF_HEADER_NAME} header — cross-site request blocked")
                },
            )
        )

    response = await call_next(request)
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    response = apply_browser_security_headers(response)
    if request.query_params.get("desktop") == "1":
        response.headers["Content-Security-Policy"] = DESKTOP_CONTENT_SECURITY_POLICY
    return response


app.include_router(build_api_router())


@app.get("/", response_model=None)
def index(request: Request) -> HTMLResponse:
    """
    Serve the SPA.

    Never injects DEEPCATALOG_API_TOKEN into HTML/JS. Does not treat loopback as
    logged-in: other local accounts can reach 127.0.0.1. A session cookie is
    issued here only when DEEPCATALOG_SINGLE_USER=1 (dedicated machine). The
    desktop window uses a one-time bootstrap nonce instead. Other browsers use
    POST /api/auth/session. Query-string tokens are not accepted.
    """
    expected = get_api_token()
    existing = request.cookies.get(COOKIE_NAME)

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    response = HTMLResponse(html)

    if (
        expected
        and single_user_desktop_enabled()
        and is_direct_loopback_request(
            peer_host=peer_host(request),
            host_header=request.headers.get("host"),
        )
    ):
        if not session_is_valid(existing):
            attach_session_cookie(response, create_session(), secure=request_is_https(request))
        return response

    return response


@app.get("/settings")
def settings_page() -> RedirectResponse:
    """Settings now lives inside the app shell."""
    return RedirectResponse(url="/#/settings")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
