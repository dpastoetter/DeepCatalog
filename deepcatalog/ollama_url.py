"""Validate Ollama base URLs to prevent SSRF and accidental remote processing."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

from deepcatalog.local_security import env_flag, is_loopback_hostname

ALLOW_REMOTE_OLLAMA_ENV = "DEEPCATALOG_ALLOW_REMOTE_OLLAMA"
ALLOWED_HOSTS_ENV = "DEEPCATALOG_OLLAMA_ALLOWED_HOSTS"
DEFAULT_LOCAL_OLLAMA_URL = "http://localhost:11434"

# Hostnames commonly used for cloud instance metadata (resolve to link-local).
_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)


def allow_remote_ollama_enabled() -> bool:
    """Explicit env opt-in for non-loopback Ollama (LAN / remote host)."""
    return env_flag(ALLOW_REMOTE_OLLAMA_ENV)


def normalize_ollama_base_url(url: str | None) -> str:
    raw = (url or "").strip() or DEFAULT_LOCAL_OLLAMA_URL
    return raw.rstrip("/")


def is_loopback_ollama_url(url: str) -> bool:
    """True when the URL host is loopback by name (before DNS)."""
    parsed = urlparse(normalize_ollama_base_url(url))
    host = parsed.hostname
    return bool(host) and is_loopback_hostname(host)


def _strip_zone(addr: str) -> str:
    return addr.split("%", 1)[0]


def canonicalize_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap IPv4-mapped/6to4 forms so link-local checks see the real address."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return mapped
        sixtofour = ip.sixtofour
        if sixtofour is not None:
            return sixtofour
    return ip


def parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    token = _strip_zone(host.strip().strip("[]"))
    try:
        return canonicalize_ip(ipaddress.ip_address(token))
    except ValueError:
        return None


def _normalize_allowlist_token(raw: str) -> str:
    token = raw.strip().strip("[]").strip(".").lower()
    if not token:
        return ""
    literal = parse_ip_literal(token)
    if literal is not None:
        return str(literal)
    try:
        return token.encode("idna").decode("ascii")
    except UnicodeError:
        return token


def ollama_allowed_hosts() -> frozenset[str]:
    """Configured hostname/IP allowlist (empty means 'no extra names')."""
    raw = os.getenv(ALLOWED_HOSTS_ENV, "")
    tokens = {_normalize_allowlist_token(part) for part in raw.split(",")}
    return frozenset(token for token in tokens if token)


def host_in_allowlist(host: str, *, allowed: frozenset[str] | None = None) -> bool:
    names = ollama_allowed_hosts() if allowed is None else allowed
    token = _normalize_allowlist_token(host)
    return bool(token) and token in names


def _reject_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_remote: bool
) -> str | None:
    ip = canonicalize_ip(ip)
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_link_local:
        return "link-local / metadata address"
    if ip.is_reserved and not ip.is_loopback:
        return "reserved address"
    if ip.is_loopback:
        return None
    if not allow_remote:
        return "non-loopback address (local Ollama allows localhost / 127.0.0.1 / ::1 only)"
    return None


def _resolve_host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve Ollama host {host!r}: {exc}") from exc
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addr = _strip_zone(str(sockaddr[0]))
        try:
            parsed = canonicalize_ip(ipaddress.ip_address(addr))
        except ValueError:
            continue
        key = str(parsed)
        if key in seen:
            continue
        seen.add(key)
        ips.append(parsed)
    if not ips:
        raise ValueError(f"Cannot resolve Ollama host {host!r} to an IP address")
    return ips


def _require_resolved_ips_safe(
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address], *, allow_remote: bool
) -> None:
    for ip in ips:
        reason = _reject_ip(ip, allow_remote=allow_remote)
        if reason:
            raise ValueError(f"Ollama base URL resolves to a blocked address ({ip}): {reason}")


def _require_remote_host_allowed(host: str, *, allow_remote: bool) -> None:
    """Remote DNS names must be on an explicit allowlist (literal IPs cannot rebind)."""
    if not allow_remote:
        return
    if is_loopback_hostname(host):
        return
    if parse_ip_literal(host) is not None:
        allowed = ollama_allowed_hosts()
        if allowed and not host_in_allowlist(host, allowed=allowed):
            raise ValueError(
                f"Ollama IP {host!r} is not in {ALLOWED_HOSTS_ENV}. "
                "Add the address to that allowlist, or leave the allowlist empty "
                "to permit opted-in literal IPs only."
            )
        return
    allowed = ollama_allowed_hosts()
    if not allowed or not host_in_allowlist(host, allowed=allowed):
        raise ValueError(
            f"Remote Ollama hostname {host!r} is not in {ALLOWED_HOSTS_ENV}. "
            "Set a comma-separated hostname/IP allowlist so DNS cannot retarget "
            "the connection after validation."
        )


def validate_ollama_base_url(url: str | None, *, allow_remote: bool = False) -> str:
    """
    Normalize and validate an Ollama base URL.

    Local mode (``allow_remote=False``): host must be loopback after DNS resolution.
    Remote mode: http(s) only; rejects link-local/metadata and other unsafe targets.
    Remote *hostnames* also require ``DEEPCATALOG_OLLAMA_ALLOWED_HOSTS``.
    """
    normalized = normalize_ollama_base_url(url)
    parsed = urlparse(normalized)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Ollama base URL must use http:// or https://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama base URL must not include credentials")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Ollama base URL must be an origin only (no path, query, or fragment)")
    host = parsed.hostname
    if not host:
        raise ValueError("Ollama base URL must include a host")
    if host.strip(".").lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Ollama host {host!r} is not allowed")

    _require_remote_host_allowed(host, allow_remote=allow_remote)
    ips = _resolve_host_ips(host)
    _require_resolved_ips_safe(ips, allow_remote=allow_remote)

    # Rebuild without trailing junk; preserve brackets for IPv6 literals.
    port = parsed.port
    if ":" in host and not host.startswith("["):
        host_part = f"[{host}]"
    else:
        host_part = host
    netloc = f"{host_part}:{port}" if port else host_part
    return f"{parsed.scheme}://{netloc}"


def pin_tcp_host(host: str) -> str:
    """
    Resolve ``host``, refuse mixed/blocked answers, return a literal IP to connect to.

    Used at TCP connect time so request-time DNS cannot differ from the check.
    """
    if not host:
        raise ValueError("Ollama base URL must include a host")
    allow_remote = not is_loopback_hostname(host)
    _require_remote_host_allowed(host, allow_remote=allow_remote)
    ips = _resolve_host_ips(host)
    _require_resolved_ips_safe(ips, allow_remote=allow_remote)
    chosen = ips[0]
    return str(chosen)


def verify_connected_peer(stream: Any) -> None:
    """Fail closed if the connected socket peer is a blocked address."""
    extra = stream.get_extra_info("peername") if hasattr(stream, "get_extra_info") else None
    if extra is None:
        raise ValueError("Ollama connection did not expose a peer address")
    addr = extra[0] if isinstance(extra, (tuple, list)) else extra
    ip = parse_ip_literal(str(addr))
    if ip is None:
        raise ValueError("Ollama connection peer is not an IP address")
    reason = _reject_ip(ip, allow_remote=not ip.is_loopback)
    if reason:
        raise ValueError(f"Ollama connected to a blocked address ({ip}): {reason}")


class _ValidatingSyncBackend(httpcore.SyncBackend):
    """httpcore backend that pin-connects to a re-checked IP on every TCP open."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        try:
            pinned = pin_tcp_host(host)
            stream = super().connect_tcp(
                pinned,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
            verify_connected_peer(stream)
        except ValueError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        return stream


class _ValidatingAnyIOBackend(httpcore.AnyIOBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            pinned = pin_tcp_host(host)
            stream = await super().connect_tcp(
                pinned,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
            verify_connected_peer(stream)
        except ValueError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        return stream


def _install_network_backend(transport: Any, backend: Any) -> None:
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError(
            "httpx transport no longer exposes a network backend; refusing to "
            "open an Ollama client without DNS pinning"
        )
    pool._network_backend = backend


_ORIGIN_RE = re.compile(
    r"\Ahttps?://(?:localhost|127\.0\.0\.1|\[::1\]|[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])"
    r"(?::[0-9]{1,5})?\Z"
)


def ollama_openai_compatible_base_url(url: str | None = None, *, allow_remote: bool = False) -> str:
    """OpenAI-compatible Ollama endpoint (`…/v1`) rebuilt from a trusted origin.

    ADK ``OpenAILlm`` and any other OpenAI SDK client must use this — never
    concatenate ``config.OLLAMA_BASE_URL + '/v1'`` (that skips SSRF / rebind checks).
    """
    return f"{trusted_ollama_origin(url, allow_remote=allow_remote)}/v1"


def apply_openai_compat_env_for_ollama(
    url: str | None = None, *, allow_remote: bool = False
) -> str:
    """Point ``OPENAI_BASE_URL`` at a trusted Ollama ``/v1`` origin (ADK OpenAILlm)."""
    base = ollama_openai_compatible_base_url(url, allow_remote=allow_remote)
    os.environ["OPENAI_BASE_URL"] = base
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = "ollama"
    return base


def trusted_ollama_origin(url: str | None, *, allow_remote: bool = False) -> str:
    """
    Validate ``url`` then rebuild a scheme+host+port origin.

    Outbound Ollama clients must use this origin with literal paths
    (``/api/tags``, ``/api/chat``, …) so a caller cannot supply a request path.
    Loopback names are pinned to a literal address so request-time DNS cannot
    rebind them. Remote *hostnames* are kept (TLS SNI / Host) and the HTTP
    client pins TCP to a re-validated IP on every connect.
    """
    safe = require_ollama_base_url(url, allow_remote=allow_remote)
    parsed = urlparse(safe)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if scheme != "http" and scheme != "https":
        raise ValueError("Ollama base URL must use http:// or https://")
    if not host:
        raise ValueError("Ollama base URL must include a host")

    if host == "localhost" or host == "127.0.0.1":
        origin = "https://127.0.0.1" if scheme == "https" else "http://127.0.0.1"
        if port is not None:
            origin = f"{origin}:{port}"
    elif host == "::1":
        origin = "https://[::1]" if scheme == "https" else "http://[::1]"
        if port is not None:
            origin = f"{origin}:{port}"
    elif scheme == "https":
        host_part = f"[{host}]" if ":" in host else host
        origin = f"https://{host_part}:{port}" if port is not None else f"https://{host_part}"
    else:
        host_part = f"[{host}]" if ":" in host else host
        origin = f"http://{host_part}:{port}" if port is not None else f"http://{host_part}"

    if _ORIGIN_RE.fullmatch(origin) is None:
        raise ValueError("Ollama base URL must be an origin only (no path, query, or fragment)")
    return origin


def ollama_client(*, origin: str, timeout: float) -> httpx.Client:
    """Sync Ollama HTTP client: no redirects, no env proxies, DNS pinned per connect."""
    transport = httpx.HTTPTransport()
    _install_network_backend(transport, _ValidatingSyncBackend())
    return httpx.Client(
        base_url=origin,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def ollama_pinned_async_transport() -> httpx.AsyncHTTPTransport:
    """httpx async transport with DNS pinning and peer re-validation."""
    transport = httpx.AsyncHTTPTransport()
    _install_network_backend(transport, _ValidatingAnyIOBackend())
    return transport


def ollama_pinned_async_http_client(*, timeout: float) -> httpx.AsyncClient:
    """DNS-pinned async httpx client without a base URL (for the OpenAI SDK)."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=ollama_pinned_async_transport(),
    )


def ollama_async_client(*, origin: str, timeout: float) -> httpx.AsyncClient:
    """Async Ollama HTTP client: no redirects, no env proxies, DNS pinned per connect."""
    return httpx.AsyncClient(
        base_url=origin,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=ollama_pinned_async_transport(),
    )


def public_ollama_config_error(exc: BaseException) -> str:
    """Stable API message for Ollama URL / config validation failures."""
    raw = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
    if any(token in raw for token in ("link-local", "metadata", "blocked", "not allowed")):
        return "Ollama URL is not allowed (blocked address)"
    if ALLOWED_HOSTS_ENV in raw or "allowlist" in raw.lower():
        return (
            "Remote Ollama hostnames must be listed in "
            f"{ALLOWED_HOSTS_ENV} (comma-separated hostnames or IPs)."
        )
    if "Remote Ollama" in raw or "non-loopback" in raw:
        return (
            "Remote Ollama is disabled. Use localhost / 127.0.0.1 / ::1, or enable "
            "Remote Ollama (allow_remote=true and approve the privacy disclaimer)."
        )
    return "Invalid Ollama URL"


def public_llm_config_error(exc: BaseException) -> str:
    """Stable API message for LLM provider / Ollama URL validation failures."""
    raw = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
    if "provider must be" in raw:
        return "provider must be one of: openai, gemini, ollama"
    return public_ollama_config_error(exc)


def require_ollama_base_url(url: str | None, *, allow_remote: bool = False) -> str:
    """
    Validate a URL for outbound Ollama HTTP.

    Remote destinations also require ``DEEPCATALOG_ALLOW_REMOTE_OLLAMA`` or an
    explicit ``allow_remote=True`` from an authenticated API that already checked
    the privacy disclaimer.
    """
    normalized = normalize_ollama_base_url(url)
    remote = not is_loopback_ollama_url(normalized)
    if remote and not (allow_remote or allow_remote_ollama_enabled()):
        raise ValueError(
            "Remote Ollama is disabled. Use localhost / 127.0.0.1 / ::1, or enable "
            "Remote Ollama (allow_remote=true and approve the privacy disclaimer), "
            f"or set {ALLOW_REMOTE_OLLAMA_ENV}=1 for a configured remote server."
        )
    return validate_ollama_base_url(normalized, allow_remote=remote)


def remote_ollama_allowed_for_request(*, allow_remote: bool) -> bool:
    """Whether this API call may target a non-loopback Ollama URL."""
    return bool(allow_remote) or allow_remote_ollama_enabled()
