"""Browser hardening headers (CSP, framing, referrer, permissions)."""

from __future__ import annotations

from typing import Any

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


def apply_browser_security_headers(response: Any) -> Any:
    """Attach hardening headers without overwriting an explicit caller value."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return response
    for name, value in BROWSER_SECURITY_HEADERS.items():
        if name not in headers:
            headers[name] = value
    return response
