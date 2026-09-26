"""Browser hardening headers (CSP, framing, referrer, permissions, optional HSTS)."""

from __future__ import annotations

import os
from typing import Any

from deepcatalog.local_security import is_loopback_hostname

# Strict CSP for the local SPA. No third-party origins; no unsafe-inline.
# connect-src covers same-origin fetch + SSE. img blob:/data: for local previews.
# worker-src 'self' for PDF.js preview worker (same-origin vendor bundle).
_CSP_CORE = (
    "default-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' blob: data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "worker-src 'self'; "
    "manifest-src 'self'"
)
CONTENT_SECURITY_POLICY = f"script-src 'self'; {_CSP_CORE}"
# pywebview injects its JS bridge with evaluate_javascript (needs unsafe-eval).
DESKTOP_CONTENT_SECURITY_POLICY = f"script-src 'self' 'unsafe-eval'; {_CSP_CORE}"

PERMISSIONS_POLICY = (
    "accelerometer=(), "
    "autoplay=(), "
    "camera=(), "
    "display-capture=(), "
    "geolocation=(), "
    "gyroscope=(), "
    "magnetometer=(), "
    "microphone=(), "
    "payment=(), "
    "publickey-credentials-get=(), "
    "screen-wake-lock=(), "
    "usb=(), "
    "interest-cohort=()"
)

BROWSER_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# Opt-in only — never force HSTS on localhost or self-signed hobby TLS.
HSTS_ENV = "DEEPCATALOG_HSTS"
HSTS_MAX_AGE_ENV = "DEEPCATALOG_HSTS_MAX_AGE"
DEFAULT_HSTS_MAX_AGE = 31_536_000  # 365 days


def hsts_enabled() -> bool:
    """True when the operator opted into Strict-Transport-Security."""
    raw = os.getenv(HSTS_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def hsts_max_age() -> int:
    raw = os.getenv(HSTS_MAX_AGE_ENV, "").strip()
    if not raw:
        return DEFAULT_HSTS_MAX_AGE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_HSTS_MAX_AGE
    return max(0, value)


def hsts_header_value(*, https: bool, host_header: str | None) -> str | None:
    """
    Return an HSTS header value, or None when it must not be sent.

    Requires ``DEEPCATALOG_HSTS=1``, a confirmed HTTPS request, and a non-loopback
    Host. Ordinary localhost / self-signed loopback deployments never get HSTS.
    """
    if not hsts_enabled() or not https:
        return None
    if is_loopback_hostname(host_header):
        return None
    return f"max-age={hsts_max_age()}; includeSubDomains"


def apply_browser_security_headers(
    response: Any,
    *,
    https: bool = False,
    host_header: str | None = None,
) -> Any:
    """Attach hardening headers without overwriting an explicit caller value."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return response
    for name, value in BROWSER_SECURITY_HEADERS.items():
        if name not in headers:
            headers[name] = value
    hsts = hsts_header_value(https=https, host_header=host_header)
    if hsts and "Strict-Transport-Security" not in headers:
        headers["Strict-Transport-Security"] = hsts
    return response
