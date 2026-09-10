"""Redact query strings and desktop-bootstrap nonces from HTTP access logs."""

from __future__ import annotations

import logging
import re

# Uvicorn access lines look like: GET /api/inbox?token=secret HTTP/1.1
_QUERY_IN_REQUEST_LINE = re.compile(r"\?([^ \t]*)")
_BOOTSTRAP_IN_REQUEST_LINE = re.compile(
    r"(/api/auth/desktop-bootstrap/)([^ \t?]+)",
    flags=re.IGNORECASE,
)

_installed = False


def redact_access_log_value(value: str) -> str:
    """Remove query strings and desktop-bootstrap nonces from a URL or request line."""
    if not value:
        return value
    redacted = _BOOTSTRAP_IN_REQUEST_LINE.sub(r"\1<redacted>", value)
    if "?" not in redacted:
        return redacted
    return _QUERY_IN_REQUEST_LINE.sub("", redacted)


def strip_query_for_log(value: str) -> str:
    """Remove query strings from a URL or HTTP request line."""
    return redact_access_log_value(value)


class AccessLogQueryFilter(logging.Filter):
    """Drop query strings from uvicorn access-log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = strip_query_for_log(record.msg)
        args = record.args
        if isinstance(args, dict):
            record.args = {
                key: strip_query_for_log(val) if isinstance(val, str) else val
                for key, val in args.items()
            }
        elif isinstance(args, tuple):
            record.args = tuple(
                strip_query_for_log(arg) if isinstance(arg, str) else arg for arg in args
            )
        return True


def install_access_log_redaction() -> None:
    """Attach the filter to uvicorn.access once per process."""
    global _installed
    if _installed:
        return
    logging.getLogger("uvicorn.access").addFilter(AccessLogQueryFilter())
    _installed = True
