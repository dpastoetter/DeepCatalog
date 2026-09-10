"""Access-log query redaction so ?token= never hits disk."""

from __future__ import annotations

import logging

from deepcatalog.access_log import (
    AccessLogQueryFilter,
    strip_query_for_log,
)


def test_strip_query_for_log_removes_token_from_request_line():
    assert (
        strip_query_for_log("GET /api/inbox?token=super-secret HTTP/1.1")
        == "GET /api/inbox HTTP/1.1"
    )
    assert strip_query_for_log("/api/auth/session?token=abc&next=/") == "/api/auth/session"
    assert strip_query_for_log("GET /api/health HTTP/1.1") == "GET /api/health HTTP/1.1"
    assert strip_query_for_log("") == ""
    assert (
        strip_query_for_log("GET /api/auth/desktop-bootstrap/super-secret-nonce HTTP/1.1")
        == "GET /api/auth/desktop-bootstrap/<redacted> HTTP/1.1"
    )
    assert (
        strip_query_for_log("GET /api/auth/desktop-bootstrap/super-secret-nonce?desktop=1 HTTP/1.1")
        == "GET /api/auth/desktop-bootstrap/<redacted> HTTP/1.1"
    )


def test_access_log_filter_redacts_args_and_message():
    filt = AccessLogQueryFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s"',
        args=("127.0.0.1", "GET /x?access_token=leak HTTP/1.1"),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "leak" not in record.getMessage()
    assert "access_token" not in record.getMessage()
    assert "?token=" not in record.getMessage()
