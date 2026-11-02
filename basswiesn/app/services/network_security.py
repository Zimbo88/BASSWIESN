from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit, urlunsplit

from basswiesn.app.services.protected_devices import first_protected_ip

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
}

TEST_NETS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)

PRIVATE_LANS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


@dataclass(frozen=True)
class UrlValidation:
    ok: bool
    reason: str
    hostname: str = ""
    addresses: tuple[str, ...] = ()
    scheme: str = ""
    port: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "hostname": self.hostname,
            "addresses": list(self.addresses),
            "scheme": self.scheme,
            "port": self.port,
        }


def _ip(value: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _resolve(hostname: str) -> tuple[IPAddress, ...]:
    direct = _ip(hostname)
    if direct is not None:
        return (direct,)
    addresses: set[IPAddress] = set()
    for info in socket.getaddrinfo(hostname, None):
        address = info[4][0]
        parsed = _ip(address) if isinstance(address, str) else None
        if parsed is not None:
            addresses.add(parsed)
    return tuple(sorted(addresses, key=str))


def _is_test_net(address: IPAddress) -> bool:
    return any(address in network for network in TEST_NETS)


def _is_local_lan(address: IPAddress) -> bool:
    # is_private also includes unspecified/reserved ranges, not just LANs.
    # Documentation ranges remain supported by the offline fixture contract.
    return isinstance(address, ipaddress.IPv4Address) and (
        any(address in network for network in PRIVATE_LANS) or _is_test_net(address)
    )


def _is_blocked_outbound(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # Translation/tunnelling would require checking the eventual IPv4 peer,
    # which this public HTTP policy cannot attest. Do not infer approval from
    # an apparently global outer IPv6 address.
    if isinstance(address, ipaddress.IPv6Address):
        if address.sixtofour or address.teredo or address.scope_id:
            return True
        if address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48"):
            return True
    return bool(
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address in METADATA_ADDRESSES
        or not address.is_global
    )


def _http_parts(url: str) -> SplitResult:
    """Reject ambiguous authorities before DNS or transport construction."""
    if not isinstance(url, str) or not url or "\\" in url or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url
    ):
        raise ValueError("invalid HTTP URL")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        # Empty explicit ports and encoded host characters are not equivalent
        # to a default-port approval under every downstream URL parser.
        if parsed.netloc.endswith(":") or "%" in parsed.netloc:
            raise ValueError
    except ValueError:
        raise ValueError("invalid HTTP URL authority") from None
    return parsed


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
        parsed = _http_parts(url)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return UrlValidation(False, "invalid HTTP URL authority")
    host = (parsed.hostname or "").lower()
    if allowed and host not in allowed:
        return UrlValidation(False, "host is not in allowlist", hostname=host, scheme=parsed.scheme, port=port)
    try:
        addresses = _resolve(host)
    except (OSError, ValueError):
        return UrlValidation(False, "DNS resolution failed", hostname=host, scheme=parsed.scheme, port=port)
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
    except (OSError, ValueError):
        return UrlValidation(
            False,
            "DNS resolution failed",
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
    parsed = _http_parts(url)
    hostname = parsed.hostname or ""
    if not hostname or hostname.lower() != validation.hostname.lower():
        raise ValueError("validated hostname does not match request URL")
    effective_port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if type(validation.port) is not int or (parsed.scheme, effective_port) != (validation.scheme, validation.port):
        raise ValueError("validated origin does not match request URL")
    try:
        # Prefer IPv4 when a hostname resolves to both IPv4 and IPv6.
        #
        # Some BASSWIESN hosts (especially Raspberry Pi installations)
        # have IPv6 addresses available through DNS but no working IPv6
        # route to the Internet. Pinning blindly to addresses[0] can
        # therefore make a perfectly reachable HTTPS service fail.
        #
        # All addresses in validation.addresses have already passed the
        # outbound security policy, so selecting a validated IPv4 address
        # here preserves the DNS-rebinding/SSRF protection.
        parsed_addresses = [
            ipaddress.ip_address(item)
            for item in validation.addresses
        ]
        address = next(
            (item for item in parsed_addresses if item.version == 4),
            parsed_addresses[0],
        )
    except (ValueError, IndexError) as exc:
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
    allowed_ports = {80, 8090} if allowed_ports is None else allowed_ports
    try:
        parsed = _http_parts(url)
    except ValueError:
        return UrlValidation(False, "invalid descriptor URL authority")
    if parsed.scheme not in {"http"}:
        return UrlValidation(False, "only local http descriptor URLs are allowed", scheme=parsed.scheme)
    host = parsed.hostname or ""
    port = parsed.port if parsed.port is not None else 80
    if port not in allowed_ports:
        return UrlValidation(False, "unexpected SoundTouch descriptor port", hostname=host, scheme=parsed.scheme, port=port)
    try:
        addresses = _resolve(host)
    except (OSError, ValueError):
        return UrlValidation(False, "DNS resolution failed", hostname=host, scheme=parsed.scheme, port=port)
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
