from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx


def _public_addresses(hostname: str, port: int) -> set[str]:
    try:
        raw_addresses = {item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("URL host could not be resolved") from exc
    addresses = {str(address) for address in raw_addresses}
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast, ip.is_unspecified)):
            raise ValueError("private or local network URLs are not allowed")
    return addresses


def _validated_url(url: str) -> tuple[str, int, set[str]]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("absolute public http(s) URL is required")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain", "metadata.google.internal"} or hostname.endswith(".local"):
        raise ValueError("private or local network URLs are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return hostname, port, _public_addresses(hostname, port)


def validate_external_url(url: str) -> None:
    _validated_url(url)


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to the address validated for this request, never re-resolving DNS."""

    def __init__(self, hostname: str, addresses: set[str], backend: Any | None = None):
        self.hostname = hostname
        self.addresses = addresses
        self.backend: Any = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if host.rstrip(".").casefold() != self.hostname:
            raise OSError("destination changed after SSRF validation")
        address = sorted(self.addresses)[0]
        # Preserve the original port, but connect to the validated numeric IP;
        # TLS still receives the original hostname from httpcore's connection.
        return await self.backend.connect_tcp(address, port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise OSError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


def safe_http_transport(url: str) -> httpx.AsyncHTTPTransport:
    hostname, _port, addresses = _validated_url(url)
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    # httpx 0.28 does not expose httpcore's network backend publicly. Its
    # transport owns a pool, so replace only that backend while retaining TLS,
    # HTTP/1.1, proxy, and connection-limit behavior from httpx.
    pool: Any = transport._pool
    pool._network_backend = _PinnedNetworkBackend(hostname, addresses)
    return transport
