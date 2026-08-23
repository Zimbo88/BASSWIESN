from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urlparse, urlsplit, urlunsplit

from basswiesn.app.services.protected_devices import first_protected_ip


METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
}

TEST_NETS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)


@dataclass(frozen=True)
class UrlValidation:
    ok: bool
    reason: str
    hostname: str = ""
    addresses: tuple[str, ...] = ()
    scheme: str = ""
    port: int | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "hostname": self.hostname,
            "addresses": list(self.addresses),
            "scheme": self.scheme,
            "port": self.port,
        }


def _ip(value: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _resolve(hostname: str) -> tuple[ipaddress._BaseAddress, ...]:
    direct = _ip(hostname)
    if direct is not None:
        return (direct,)
    addresses: set[ipaddress._BaseAddress] = set()
    for info in socket.getaddrinfo(hostname, None):
        address = info[4][0]
        parsed = _ip(address)
        if parsed is not None:
            addresses.add(parsed)
    return tuple(sorted(addresses, key=str))


def _is_test_net(address: ipaddress._BaseAddress) -> bool:
    return any(address in network for network in TEST_NETS)


def _is_local_lan(address: ipaddress._BaseAddress) -> bool:
    return bool(address.version == 4 and (address.is_private or _is_test_net(address)) and not address.is_loopback and not address.is_link_local)


def _is_blocked_outbound(address: ipaddress._BaseAddress) -> bool:
    return bool(
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address in METADATA_ADDRESSES
        or address.is_private
    )


def validate_outbound_http_url(
    url: str,
    *,
    allowed_hosts: set[str] | None = None,
    public_only: bool = False,
) -> UrlValidation:
    """Resolve and validate one HTTP target before a transport is created.

    Private LAN destinations remain valid for stream and setup-adjacent
    workflows.  Immutable and runtime-configured protected device addresses
    are rejected for every caller, including when reached through DNS.
    """

    allowed = {str(item).strip().lower() for item in (allowed_hosts or set()) if str(item).strip()}
    try:
        parsed = urlparse(url or "")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return UrlValidation(False, "invalid URL port")
    if parsed.scheme not in {"http", "https"}:
        return UrlValidation(False, "only http and https are allowed", scheme=parsed.scheme)
    if parsed.username or parsed.password:
        return UrlValidation(False, "credentials in URL are not allowed", scheme=parsed.scheme)
    host = (parsed.hostname or "").lower()
    if not host:
        return UrlValidation(False, "missing host", scheme=parsed.scheme, port=port)
    if allowed and host not in allowed:
        return UrlValidation(False, "host is not in allowlist", hostname=host, scheme=parsed.scheme, port=port)
    try:
        addresses = _resolve(host)
    except OSError as exc:
        return UrlValidation(False, f"DNS resolution failed: {exc}", hostname=host, scheme=parsed.scheme, port=port)
    address_text = tuple(map(str, addresses))
    if not addresses:
        return UrlValidation(False, "host did not resolve", hostname=host, scheme=parsed.scheme, port=port)
    protected = first_protected_ip(address_text)
    if protected:
        return UrlValidation(
            False,
            "URL resolves to a protected device address",
            hostname=host,
            addresses=address_text,
            scheme=parsed.scheme,
            port=port,
        )
    if public_only and any(_is_blocked_outbound(address) for address in addresses):
        return UrlValidation(
            False,
            "URL resolves to a blocked local, private, link-local or metadata address",
            hostname=host,
            addresses=address_text,
            scheme=parsed.scheme,
            port=port,
        )
    return UrlValidation(True, "ok", hostname=host, addresses=address_text, scheme=parsed.scheme, port=port)


def validate_outbound_host(host: str, *, port: int | None = None) -> UrlValidation:
    """Resolve a raw TCP host and reject every protected address."""

    hostname = str(host or "").strip().strip("[]").lower()
    if not hostname:
        return UrlValidation(False, "missing host", port=port)
    try:
        addresses = _resolve(hostname)
    except OSError as exc:
        return UrlValidation(
            False,
            f"DNS resolution failed: {exc}",
            hostname=hostname,
            port=port,
        )
    address_text = tuple(map(str, addresses))
    if not addresses:
        return UrlValidation(False, "host did not resolve", hostname=hostname, port=port)
    if first_protected_ip(address_text):
        return UrlValidation(
            False,
            "host resolves to a protected device address",
            hostname=hostname,
            addresses=address_text,
            port=port,
        )
    return UrlValidation(
        True,
        "ok",
        hostname=hostname,
        addresses=address_text,
        port=port,
    )


def pinned_http_target(
    url: str,
    validation: UrlValidation,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Pin a validated URL to the exact address that passed the policy gate.

    This closes the validate-then-resolve DNS-rebinding window.  Callers merge
    the returned Host header with their own headers and pass the extensions to
    httpx; redirects must be validated and pinned again.
    """

    if not validation.ok or not validation.addresses:
        raise ValueError("an allowed, resolved URL validation is required")
    parsed = urlsplit(url or "")
    hostname = parsed.hostname or ""
    if not hostname or hostname.lower() != validation.hostname.lower():
        raise ValueError("validated hostname does not match request URL")
    try:
        address = ipaddress.ip_address(validation.addresses[0])
    except ValueError as exc:
        raise ValueError("validated target address is malformed") from exc
    address_text = f"[{address.compressed}]" if address.version == 6 else address.compressed
    pinned_netloc = (
        f"{address_text}:{parsed.port}"
        if parsed.port is not None
        else address_text
    )
    default_port = 443 if parsed.scheme == "https" else 80
    display_host = f"[{hostname}]" if ":" in hostname else hostname.encode("idna").decode("ascii")
    host_header = (
        display_host
        if parsed.port in {None, default_port}
        else f"{display_host}:{parsed.port}"
    )
    pinned_url = urlunsplit(
        (parsed.scheme, pinned_netloc, parsed.path or "/", parsed.query, "")
    )
    extensions = (
        {"sni_hostname": hostname.encode("idna").decode("ascii")}
        if parsed.scheme == "https" and ":" not in hostname
        else {}
    )
    return pinned_url, {"Host": host_header}, extensions


def validate_local_soundtouch_url(url: str, *, allowed_ports: set[int] | None = None) -> UrlValidation:
    allowed_ports = allowed_ports or {80, 8090}
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http"}:
        return UrlValidation(False, "only local http descriptor URLs are allowed", scheme=parsed.scheme)
    if parsed.username or parsed.password:
        return UrlValidation(False, "credentials in descriptor URL are not allowed", scheme=parsed.scheme)
    host = parsed.hostname or ""
    if not host:
        return UrlValidation(False, "missing host", scheme=parsed.scheme)
    port = parsed.port or (80 if parsed.scheme == "http" else 443)
    if port not in allowed_ports:
        return UrlValidation(False, "unexpected SoundTouch descriptor port", hostname=host, scheme=parsed.scheme, port=port)
    try:
        addresses = _resolve(host)
    except OSError as exc:
        return UrlValidation(False, f"DNS resolution failed: {exc}", hostname=host, scheme=parsed.scheme, port=port)
    if not addresses:
        return UrlValidation(False, "host did not resolve", hostname=host, scheme=parsed.scheme, port=port)
    protected = first_protected_ip(tuple(map(str, addresses)))
    if protected:
        return UrlValidation(False, "descriptor URL resolves to a protected device address", hostname=host, addresses=tuple(map(str, addresses)), scheme=parsed.scheme, port=port)
    if not all(_is_local_lan(address) for address in addresses):
        return UrlValidation(False, "descriptor URL must resolve to a local LAN address", hostname=host, addresses=tuple(map(str, addresses)), scheme=parsed.scheme, port=port)
    return UrlValidation(True, "ok", hostname=host, addresses=tuple(map(str, addresses)), scheme=parsed.scheme, port=port)


def validate_public_callback_url(url: str, *, allowed_hosts: set[str] | None = None) -> UrlValidation:
    return validate_outbound_http_url(
        url,
        allowed_hosts=allowed_hosts,
        public_only=True,
    )


def host_in_optional_allowlist(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return bool(host and (not allowed_hosts or host in {item.lower() for item in allowed_hosts}))
