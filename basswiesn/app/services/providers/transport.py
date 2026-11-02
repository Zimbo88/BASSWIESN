"""Bounded outbound reads with an exact origin, address policy and DNS pinning."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias
from urllib.parse import urlsplit

import httpx

from basswiesn.app.services.network_security import UrlValidation, pinned_http_target
from basswiesn.app.services.protected_devices import first_protected_ip

from .contracts import ProviderError, ProviderErrorCode

IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver: TypeAlias = Callable[[str, int], Awaitable[tuple[str, ...]]]
BlockedAddresses: TypeAlias = Callable[[], frozenset[str]]
JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


async def resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        return (str(ipaddress.ip_address(hostname)),)
    except ValueError:
        results = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return tuple(sorted({str(item[4][0]) for item in results}))


def canonical_ip(value: str) -> IPAddress:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _safe_ip(address: IPAddress, *, public_only: bool) -> bool:
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    # Transition mechanisms can tunnel an otherwise rejected IPv4 peer.
    if isinstance(address, ipaddress.IPv6Address):
        if address.sixtofour or address.teredo or address.scope_id:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
    if public_only:
        return address.is_global
    return address.is_global or any(address in network for network in (
        ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7"),
    ) if address.version == network.version)


def validate_origin(origin: str) -> str:
    """Configuration syntax only. Never resolve or contact a peer at startup."""
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        valid = (
            parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None
            and parsed.password is None and parsed.path in {"", "/"} and not parsed.query
            and not parsed.fragment and (port is None or 1 <= port <= 65535)
            and not any(char.isspace() or ord(char) < 32 for char in origin)
            and "\\" not in origin and "%" not in parsed.netloc
        )
        if not valid:
            raise ValueError
        return origin.rstrip("/")
    except ValueError:
        raise ProviderError(ProviderErrorCode.UNSAFE_TARGET) from None


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    headers: dict[str, str]
    extensions: dict[str, str]
    addresses: tuple[str, ...]


class TargetPolicy:
    def __init__(
        self, *, blocked_addresses: BlockedAddresses,
        resolver: Resolver = resolve_addresses, allowed_ips: frozenset[str] = frozenset(),
        origin: str | None = None,
    ) -> None:
        self.origin = validate_origin(origin) if origin is not None else None
        self._resolver = resolver
        self._blocked_addresses = blocked_addresses
        try:
            self._allowed_ips = frozenset(str(canonical_ip(value)) for value in allowed_ips)
        except ValueError:
            raise ProviderError(ProviderErrorCode.UNSAFE_TARGET) from None
        if self.origin is not None and not self._allowed_ips:
            raise ProviderError(ProviderErrorCode.UNSAFE_TARGET)

    async def prepare(self, url: str) -> PinnedTarget:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
            if (
                parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None
                or parsed.fragment or "\\" in url or "%" in parsed.netloc
                or any(char.isspace() or ord(char) < 32 for char in url)
                or not 1 <= port <= 65535 or len(url) > 4096
            ):
                raise ProviderError(ProviderErrorCode.UNSAFE_TARGET)
            if self.origin is not None:
                origin = urlsplit(self.origin)
                if (parsed.scheme, host.lower(), port) != (origin.scheme, origin.hostname, origin.port or 443):
                    raise ProviderError(ProviderErrorCode.UNSAFE_TARGET)
            raw_addresses = await self._resolver(host, port)
            addresses = tuple(str(canonical_ip(value)) for value in raw_addresses)
            blocked = frozenset(str(canonical_ip(value)) for value in self._blocked_addresses())
            if not addresses or len(addresses) > 32 or first_protected_ip(addresses):
                raise ProviderError(ProviderErrorCode.UNSAFE_TARGET)
            if any(
                address in blocked
                or not _safe_ip(canonical_ip(address), public_only=self.origin is None)
                or (self.origin is not None and address not in self._allowed_ips)
                for address in addresses
            ):
                raise ProviderError(ProviderErrorCode.UNSAFE_TARGET)
            validation = UrlValidation(True, "ok", hostname=host, addresses=addresses,
                                       scheme=parsed.scheme, port=port)
            pinned_url, headers, extensions = pinned_http_target(url, validation)
            return PinnedTarget(pinned_url, headers, extensions, addresses)
        except ProviderError:
            raise
        except Exception:
            # DNS and unavailable/malformed radio inventory must fail closed.
            raise ProviderError(ProviderErrorCode.UNSAFE_TARGET) from None


class BoundedJSONClient:
    """Only fixed paths, no redirects/proxies/cookies and no raw error payloads."""

    def __init__(
        self, policy: TargetPolicy, *, token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        max_bytes: int = 524288,
    ) -> None:
        if not policy.origin or not 16 <= len(token) <= 8192 or not token.isascii() or any(char.isspace() or ord(char) < 33 for char in token):
            raise ProviderError(ProviderErrorCode.AUTH_REQUIRED)
        if not 1024 <= max_bytes <= 1048576:
            raise ValueError("invalid provider response bound")
        self._policy = policy
        self._token = token
        self._transport = transport
        self._max_bytes = max_bytes

    async def request(self, path: str, payload: Mapping[str, JSONValue] | None = None) -> JSONValue:
        if path not in {"/info", "/api"} or (path == "/info") != (payload is None):
            raise ProviderError(ProviderErrorCode.INVALID_REQUEST)
        try:
            async with asyncio.timeout(8.0):
                target = await self._policy.prepare(f"{self._policy.origin}{path}")
                headers = {**target.headers, "Accept": "application/json", "Accept-Encoding": "identity"}
                # /info is public; it does not need a credential.
                if path == "/api":
                    headers["Authorization"] = f"Bearer {self._token}"
                async with httpx.AsyncClient(
                    timeout=5.0, follow_redirects=False, trust_env=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        "GET" if payload is None else "POST", target.url,
                        headers=headers, extensions=target.extensions,
                        json=dict(payload) if payload is not None else None,
                    ) as response:
                        if response.status_code in {401, 403}:
                            raise ProviderError(ProviderErrorCode.AUTH_REQUIRED)
                        if response.status_code != 200:
                            raise ProviderError(ProviderErrorCode.UNAVAILABLE)
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
                        if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
                        length = response.headers.get("Content-Length")
                        if length and (not length.isdecimal() or int(length) > self._max_bytes):
                            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
                        data = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            data.extend(chunk)
                            if len(data) > self._max_bytes:
                                raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
                        value: JSONValue = json.loads(data)
                        # CPython versions differ in JSON recursion limits.
                        # Enforce our own shape budget, including otherwise
                        # ignored extra upstream fields, before projection.
                        pending: list[tuple[JSONValue, int]] = [(value, 0)]
                        visited = 0
                        while pending:
                            item, depth = pending.pop()
                            visited += 1
                            if depth > 16 or visited > 20000:
                                raise ProviderError(ProviderErrorCode.INVALID_RESPONSE)
                            if isinstance(item, dict):
                                pending.extend((child, depth + 1) for child in item.values())
                            elif isinstance(item, list):
                                pending.extend((child, depth + 1) for child in item)
                        return value
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError(ProviderErrorCode.TIMEOUT) from None
        except (ValueError, RecursionError):
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE) from None
        except Exception:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE) from None
