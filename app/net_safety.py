"""Outbound URL safety checks shared by the proxy routes and source providers.

These stop authenticated endpoints from being used as an SSRF proxy into the
server's own network (loopback, private ranges, cloud metadata addresses).
"""

import asyncio
import ipaddress
import socket
import threading
import time
import typing
from urllib.parse import urlsplit

import httpcore
import httpx


def _ip_is_blocked(ip: "ipaddress._BaseAddress") -> bool:
    # ``is_global`` also covers CGNAT (100.64/10, e.g. Tailscale), NAT64 and the
    # other special-purpose ranges ``is_private`` misses. IPv4-mapped IPv6
    # addresses are judged by the IPv4 address they carry.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


# Short-TTL cache of host → SSRF-safety verdict. ``getaddrinfo`` is a blocking DNS
# lookup; memoizing it keeps a slow/flaky resolver from re-paying that cost (and,
# for async callers offloading via run_in_executor, from tying up a thread) on
# every playback start and source probe for the same host.
_HOST_SAFETY_TTL = 300
_HOST_SAFETY_MAX = 512
_host_safety_cache: dict[str, tuple[float, bool]] = {}
_host_safety_lock = threading.Lock()


def _host_is_safe(host: str) -> bool:
    """Resolve ``host`` and return True only if every address is public.

    Memoized with a short TTL. A host that does not resolve is refused (and not
    cached, so a transient failure isn't sticky).
    """
    now = time.time()
    with _host_safety_lock:
        hit = _host_safety_cache.get(host)
        if hit and now - hit[0] < _HOST_SAFETY_TTL:
            return hit[1]

    try:
        # If host is already an IP literal this just returns it; otherwise resolve.
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False

    safe = True
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]))
        except ValueError:
            continue
        if _ip_is_blocked(ip):
            safe = False
            break

    with _host_safety_lock:
        _host_safety_cache[host] = (now, safe)
        if len(_host_safety_cache) > _HOST_SAFETY_MAX:
            oldest = min(_host_safety_cache, key=lambda k: _host_safety_cache[k][0])
            del _host_safety_cache[oldest]
    return safe


def is_safe_source_url(url: str) -> bool:
    """Reject anything that isn't a plain remote http(s) URL.

    Prevents authenticated endpoints from being abused as an SSRF proxy into
    the server's own network (loopback / private ranges / metadata). The DNS
    resolution is memoized via ``_host_is_safe`` — async callers should still
    offload this call off the event loop.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host == "localhost":
        return False
    return _host_is_safe(host)


def is_browser_fetchable_url(url: str) -> bool:
    """True only if a browser on the https dashboard can fetch this URL directly.

    Requires https (http is mixed-content blocked on an https page and also fails
    the dashboard CSP) and a public host — ``is_safe_source_url`` already rejects
    internal/private hosts such as other Docker services. Used to gate
    "browser_direct" stream resolution: when false, the server resolves instead.
    """
    return url.lower().startswith("https://") and is_safe_source_url(url)


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Network backend that re-checks every address at connect time.

    ``is_safe_source_url`` resolves a host once; the HTTP client would resolve
    it again when connecting, so a DNS-rebinding host could pass the check and
    then connect to 127.0.0.1. This backend resolves once more, refuses any
    non-public address and connects to the vetted IP itself. TLS still uses the
    original host name for SNI and certificate checks (httpcore passes the
    origin host to ``start_tls``).
    """

    def __init__(self, allowed_hosts: typing.Iterable[str] = ()) -> None:
        from httpcore._backends.auto import AutoBackend

        self._inner = AutoBackend()
        self._allowed = {h.lower() for h in allowed_hosts if h}

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: typing.Optional[float] = None,
        local_address: typing.Optional[str] = None,
        socket_options: typing.Optional[typing.Iterable[typing.Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.lower() in self._allowed:
            return await self._inner.connect_tcp(
                host, port, timeout, local_address, socket_options
            )
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except (socket.gaierror, UnicodeError) as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}") from exc
        addresses: list[str] = []
        for info in infos:
            address = str(info[4][0])
            if _ip_is_blocked(ipaddress.ip_address(address)):
                raise httpcore.ConnectError(
                    "refused: host resolves to a non-public address"
                )
            if address not in addresses:
                addresses.append(address)
        last_error: typing.Optional[Exception] = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError(f"cannot connect to {host}")

    async def connect_unix_socket(self, *args: typing.Any, **kwargs: typing.Any):
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_only_transport(
    *, allowed_hosts: typing.Iterable[str] = (), **kwargs: typing.Any
) -> httpx.AsyncHTTPTransport:
    """An httpx transport that can only connect to public addresses.

    ``allowed_hosts`` names hosts exempt from the check (Curatio's own origin,
    which serves signed ``/media/`` links and may be a LAN or loopback address).
    """
    transport = httpx.AsyncHTTPTransport(**kwargs)
    # httpx exposes no backend option; the pool reads this attribute per connect.
    transport._pool._network_backend = _PublicOnlyBackend(allowed_hosts)
    return transport
