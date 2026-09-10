"""Local API security: bind policy, sessions token, Host allowlist, trusted proxies."""

from __future__ import annotations

import gc
import hmac
import ipaddress
import os
import secrets
import sys
from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import Path
from types import FrameType

from uvicorn.lifespan.on import LifespanOn
from uvicorn.server import Server as UvicornServer

TOKEN_ENV = "DEEPCATALOG_API_TOKEN"
SINGLE_USER_ENV = "DEEPCATALOG_SINGLE_USER"
ALLOW_REMOTE_ENV = "DEEPCATALOG_ALLOW_REMOTE"
ALLOWED_HOSTS_ENV = "DEEPCATALOG_ALLOWED_HOSTS"
TRUSTED_PROXIES_ENV = "DEEPCATALOG_TRUSTED_PROXIES"
SSL_CERTFILE_ENV = "DEEPCATALOG_SSL_CERTFILE"
SSL_KEYFILE_ENV = "DEEPCATALOG_SSL_KEYFILE"
COOKIE_NAME = "deepcatalog_session"
AUTH_HEADER = "Authorization"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8080
_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*"})

_Network = IPv4Network | IPv6Network
_IP = IPv4Address | IPv6Address
_ALLOWED_FORWARDED_PROTO = frozenset({"http", "https"})

# Hosts treated as loopback for bind checks and client trust.
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "::1", "localhost", "testclient", "testserver"})


def env_flag(name: str) -> bool:
    """True when an env var is a common truthy string (1/true/yes/on)."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_loopback_hostname(host: str | None) -> bool:
    """True for loopback hostnames / IPs (ignoring optional port)."""
    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("[") and "]" in name:
        name = name[1 : name.index("]")]
    elif name.count(":") == 1 and not name.startswith(":"):
        # hostname:port
        name = name.rsplit(":", 1)[0]
    if name in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def port_probe_host(host: str | None) -> str:
    """Host used when checking whether a port is free — never a wildcard bind."""
    name = (host or "").strip()
    if not name or name.lower() in _WILDCARD_BIND_HOSTS:
        return "127.0.0.1"
    return name


def is_wildcard_or_non_loopback_bind(host: str | None) -> bool:
    """True when binding would accept connections from the network."""
    if not host:
        return False
    name = host.strip().lower()
    if name in _WILDCARD_BIND_HOSTS:
        return True
    if is_loopback_hostname(name):
        return False
    try:
        # Specific interface IP (LAN/WAN) — also non-loopback.
        return not ipaddress.ip_address(name).is_loopback
    except ValueError:
        # Unknown hostname — treat as non-loopback to be safe.
        return name not in _LOOPBACK_HOSTNAMES


def get_api_token() -> str | None:
    """Return the configured API token, or None if unset."""
    raw = os.getenv(TOKEN_ENV, "").strip()
    return raw or None


def generate_api_token() -> str:
    """Cryptographically strong token for DEEPCATALOG_API_TOKEN."""
    return secrets.token_urlsafe(32)


def single_user_desktop_enabled() -> bool:
    """Explicit opt-in: treat direct loopback as trusted (dedicated single-user machine)."""
    return env_flag(SINGLE_USER_ENV)


def allow_remote_enabled() -> bool:
    """Explicit opt-in for non-loopback binds (network mode)."""
    return env_flag(ALLOW_REMOTE_ENV)


def ssl_cert_paths() -> tuple[Path, Path] | None:
    """Return (certfile, keyfile) when both DEEPCATALOG_SSL_* paths are set and exist."""
    cert = os.getenv(SSL_CERTFILE_ENV, "").strip()
    key = os.getenv(SSL_KEYFILE_ENV, "").strip()
    if not cert or not key:
        return None
    cert_path = Path(cert).expanduser()
    key_path = Path(key).expanduser()
    if not cert_path.is_file() or not key_path.is_file():
        return None
    return cert_path.resolve(), key_path.resolve()


def ssl_configured() -> bool:
    return ssl_cert_paths() is not None


def _parse_forwarded_ip(value: str | None) -> _IP | None:
    """Parse a single X-Forwarded-For hop as an IP literal (no hostnames, no ports)."""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("[") and "]" in raw:
        close = raw.index("]")
        if close != len(raw) - 1:
            return None
        raw = raw[1:close]
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def _network_is_unrestricted(net: _Network) -> bool:
    """True for catch-alls like 0.0.0.0/0 and ::/0 (never usable as trusted proxies)."""
    return net.prefixlen == 0 or net.is_unspecified


def _parse_networks(raw: str) -> list[_Network]:
    nets: list[_Network] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            if "/" in item:
                net = ipaddress.ip_network(item, strict=False)
            else:
                addr = ipaddress.ip_address(item)
                net = ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
            if not isinstance(net, (IPv4Network, IPv6Network)):
                continue
            if _network_is_unrestricted(net):
                continue
            nets.append(net)
        except ValueError:
            continue
    return nets


def trusted_proxy_networks() -> list[_Network]:
    """CIDRs/IPs allowed to set X-Forwarded-* (empty = never trust proxy headers)."""
    return _parse_networks(os.getenv(TRUSTED_PROXIES_ENV, ""))


def is_trusted_proxy(host: str | None) -> bool:
    """True only for an explicit DEEPCATALOG_TRUSTED_PROXIES IP (never hostnames)."""
    addr = _parse_forwarded_ip(host)
    if addr is None or addr.is_unspecified:
        return False
    return any(addr in net for net in trusted_proxy_networks())


def configured_bind_host() -> str:
    """Bind host from DEEPCATALOG_HOST, defaulting to loopback."""
    raw = (os.getenv("DEEPCATALOG_HOST") or "").strip()
    return raw or DEFAULT_BIND_HOST


def configured_bind_port() -> int:
    """Bind port from DEEPCATALOG_PORT, defaulting to 8080."""
    raw = (os.getenv("DEEPCATALOG_PORT") or "").strip()
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_BIND_PORT
    return port if port > 0 else DEFAULT_BIND_PORT


def sync_configured_bind(host: str, port: int | None = None) -> None:
    """Keep DEEPCATALOG_HOST / DEEPCATALOG_PORT aligned with the process bind."""
    os.environ["DEEPCATALOG_HOST"] = (host or "").strip() or DEFAULT_BIND_HOST
    if port is not None:
        os.environ["DEEPCATALOG_PORT"] = str(port)


def bind_host_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Uvicorn/CLI ``--host`` from argv (manual ``uvicorn … --host 0.0.0.0``)."""
    args = list(sys.argv if argv is None else argv)
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == "--host" and i + 1 < len(args) and not args[i + 1].startswith("-"):
            host = args[i + 1].strip()
            return host or None
        if arg.startswith("--host="):
            host = arg.split("=", 1)[1].strip()
            return host or None
        i += 1
    return None


def _server_bind_host(server: object) -> str | None:
    try:
        host = str(getattr(getattr(server, "config", None), "host", "") or "").strip()
    except Exception:  # noqa: BLE001 — skip half-initialized Server objects
        return None
    return host or None


def _bind_host_from_startup_stack() -> str | None:
    """Host of the uvicorn Server/Lifespan currently starting this app.

    Lifespan runs in a task created by ``LifespanOn.startup()``, so ``Server``
    is not on this stack. ``LifespanOn.config.host`` is the address uvicorn will
    bind. ``gc.get_objects()`` can still see leftover Server instances.
    """
    frame: FrameType | None = sys._getframe(0)
    try:
        while frame is not None:
            owner = frame.f_locals.get("self")
            if isinstance(owner, (UvicornServer, LifespanOn)):
                host = _server_bind_host(owner)
                if host:
                    return host
            frame = frame.f_back
        return None
    finally:
        del frame


def discover_uvicorn_bind_host() -> str | None:
    """Host uvicorn will bind, taken from live Server.config (set before listen)."""
    stacked = _bind_host_from_startup_stack()
    if stacked:
        return stacked
    try:
        objects = gc.get_objects()
    except Exception:  # noqa: BLE001 — discovery must never break startup
        return None
    last: str | None = None
    exposed: str | None = None
    for obj in objects:
        try:
            if not isinstance(obj, UvicornServer):
                continue
        except Exception:  # noqa: BLE001 — skip broken gc entries
            continue
        host = _server_bind_host(obj)
        if not host:
            continue
        last = host
        if is_wildcard_or_non_loopback_bind(host):
            exposed = host
    return exposed or last


def effective_bind_host() -> str:
    """Address the security policy must check: uvicorn's bind, else DEEPCATALOG_HOST.

    ``uvicorn app.main:app --host 0.0.0.0`` can diverge from DEEPCATALOG_HOST.
    Lifespan runs before the socket exists, but Server.config.host is already set.
    """
    discovered = discover_uvicorn_bind_host()
    if discovered:
        return discovered
    from_argv = bind_host_from_argv()
    if from_argv:
        return from_argv
    return configured_bind_host()


def assert_bind_allowed(host: str | None) -> None:
    """
    Enforce local vs network bind policy.

    - Local mode (loopback): plain HTTP is fine.
    - Network mode (0.0.0.0 / LAN IP / …): requires DEEPCATALOG_ALLOW_REMOTE=1,
      DEEPCATALOG_API_TOKEN, and TLS cert/key files on the process itself.

    Preferred LAN exposure: keep DEEPCATALOG_HOST=127.0.0.1 and terminate TLS on a
    reverse proxy (Caddy/nginx/Traefik). That stays in local mode for the app.
    """
    if not is_wildcard_or_non_loopback_bind(host):
        return

    problems: list[str] = []
    if not allow_remote_enabled():
        problems.append(
            f"set {ALLOW_REMOTE_ENV}=1 to acknowledge network exposure "
            "(plain HTTP on a LAN is not enough — see README network mode)"
        )
    if not get_api_token():
        problems.append(
            f"set {TOKEN_ENV}=… "
            f'(python -c "from deepcatalog.local_security import generate_api_token; '
            f'print(generate_api_token())")'
        )
    if not ssl_configured():
        problems.append(
            f"set {SSL_CERTFILE_ENV} and {SSL_KEYFILE_ENV} to existing PEM files "
            "(network mode requires HTTPS on the uvicorn process), "
            "or keep DEEPCATALOG_HOST=127.0.0.1 and put Caddy/nginx/Traefik in front"
        )
    if not problems:
        return
    detail = "; ".join(problems)
    raise RuntimeError(
        f"Refusing to bind to {host!r} in network mode — {detail}. "
        "Local mode (127.0.0.1 / ::1) allows HTTP without these flags."
    )


def is_direct_loopback_request(*, peer_host: str | None, host_header: str | None) -> bool:
    """True only for a browser hitting the app directly on loopback.

    Requires a loopback TCP peer *and* a loopback Host header. A peer listed in
    DEEPCATALOG_TRUSTED_PROXIES is the reverse-proxy hop, not the user — even when
    that hop is 127.0.0.1. X-Forwarded-* is ignored (never an auth signal).
    """
    if not peer_host or not host_header:
        return False
    if is_trusted_proxy(peer_host):
        return False
    return is_loopback_hostname(peer_host) and is_loopback_hostname(host_header)


def auth_required_for_request(*, peer_host: str | None, host_header: str | None = None) -> bool:
    """Bearer/cookie auth is required unless single-user desktop mode is opted in.

    Loopback is not an authentication boundary: any local process or OS account
    can connect to 127.0.0.1. Set DEEPCATALOG_SINGLE_USER=1 only on a dedicated
    machine that accepts that risk. X-Forwarded-* is never used here.
    """
    if single_user_desktop_enabled() and is_direct_loopback_request(
        peer_host=peer_host, host_header=host_header
    ):
        return False
    return True


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def token_matches(candidate: str | None, expected: str | None) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def allowed_hosts() -> set[str]:
    """Hostnames allowed in the Host header (without ports)."""
    raw = os.getenv(ALLOWED_HOSTS_ENV, "").strip()
    if raw:
        hosts = {h.strip().lower() for h in raw.split(",") if h.strip()}
    else:
        hosts = {"localhost", "127.0.0.1", "::1", "testclient"}
    # Always allow loopback names even if the env list is customized.
    hosts.update(_LOOPBACK_HOSTNAMES)
    # Include the actual / configured bind host when it is a concrete hostname/IP.
    for bind in (effective_bind_host(), configured_bind_host()):
        name = bind.strip().lower()
        if name and name not in _WILDCARD_BIND_HOSTS:
            hosts.add(name.strip("[]"))
    return hosts


def host_header_allowed(host_header: str | None) -> bool:
    """Validate Host / :authority against the allowlist (DNS-rebinding hardening)."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("[") and "]" in host:
        name = host[1 : host.index("]")]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if host.count(":") > 1:
            # bare IPv6 without brackets
            name = host
    if name in allowed_hosts():
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def forwarded_client_host(
    *,
    peer_host: str | None,
    x_forwarded_for: str | None,
) -> str | None:
    """
    Resolve the browser/client host for logging / HTTPS detection.

    X-Forwarded-For is only honored when the immediate TCP peer is in
    DEEPCATALOG_TRUSTED_PROXIES. The chain is walked right-to-left (nearest
    proxy toward the original client). Only explicitly trusted proxy IPs are
    skipped; the first untrusted valid IP is the client. A malformed hop
    invalidates the header and the TCP peer is used instead. This value must
    never be used to grant authentication.
    """
    if not is_trusted_proxy(peer_host):
        return peer_host
    if not x_forwarded_for:
        return peer_host
    hops = [part.strip() for part in x_forwarded_for.split(",") if part.strip()]
    trusted = trusted_proxy_networks()
    for candidate in reversed(hops):
        addr = _parse_forwarded_ip(candidate)
        if addr is None or addr.is_unspecified:
            return peer_host
        if any(addr in net for net in trusted):
            continue
        return str(addr)
    return peer_host


def _nearest_forwarded_proto(header: str | None) -> str | None:
    """Rightmost X-Forwarded-Proto hop, or None if missing/malformed."""
    if not header:
        return None
    hops = [part.strip().lower() for part in header.split(",") if part.strip()]
    if not hops:
        return None
    nearest = hops[-1]
    if nearest not in _ALLOWED_FORWARDED_PROTO:
        return None
    return nearest


def request_appears_https(
    *,
    peer_host: str | None,
    url_scheme: str | None,
    x_forwarded_proto: str | None,
) -> bool:
    """True for direct HTTPS or HTTPS reported by a trusted proxy only."""
    if (url_scheme or "").lower() == "https":
        return True
    if not is_trusted_proxy(peer_host):
        return False
    return _nearest_forwarded_proto(x_forwarded_proto) == "https"


def remote_auth_must_be_https(
    *,
    peer_host: str | None,
    host_header: str | None,
    url_scheme: str | None,
    x_forwarded_proto: str | None,
) -> bool:
    """True when a non-loopback client must use HTTPS before credentials are processed.

    Direct loopback HTTP stays allowed. A loopback TCP peer listed in
    DEEPCATALOG_TRUSTED_PROXIES is the reverse-proxy hop, not the user — those
    requests need trusted X-Forwarded-Proto=https (or a real HTTPS scheme).
    """
    if is_direct_loopback_request(peer_host=peer_host, host_header=host_header):
        return False
    return not request_appears_https(
        peer_host=peer_host,
        url_scheme=url_scheme,
        x_forwarded_proto=x_forwarded_proto,
    )
