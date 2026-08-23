"""Validated, configurable BASSWIESN server target."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import subprocess
from urllib.parse import urlparse

from basswiesn.app.config import get_settings, is_safe_radio_host
from basswiesn.app.services.protected_devices import is_protected_ip


_VIRTUAL_INTERFACE_PREFIXES = (
    "br-",
    "cni",
    "docker",
    "podman",
    "veth",
    "virbr",
)


@dataclass(frozen=True)
class ServerTargetCandidate:
    host: str
    interface: str
    source: str
    configured: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "interface": self.interface,
            "source": self.source,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class ServerTarget:
    host: str
    web_port: int
    cloud_port: int
    debug_port: int

    def __post_init__(self) -> None:
        if is_protected_ip(self.host):
            raise ValueError("server target is a protected device address")

    @property
    def urls(self) -> dict[str, str]:
        return {
            "web": f"http://{self.host}:{self.web_port}",
            "cloud": f"http://{self.host}:{self.cloud_port}",
            "debug": f"http://{self.host}:{self.debug_port}",
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "ports": {
                "web": self.web_port,
                "cloud": self.cloud_port,
                "debug": self.debug_port,
            },
            "urls": self.urls,
        }


def _host(value: str) -> str:
    parsed = urlparse(value.strip())
    candidate = parsed.hostname or value.strip()
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("server target must be a literal LAN IPv4 address") from exc
    if ip.version != 4 or not is_safe_radio_host(candidate) or is_protected_ip(str(ip)):
        raise ValueError("server target is not a safe radio-reachable LAN address")
    return str(ip)


def _is_virtual_interface(name: str) -> bool:
    normalized = str(name or "").strip().lower()
    return normalized == "lo" or normalized.startswith(_VIRTUAL_INTERFACE_PREFIXES)


def _interface_addresses() -> list[tuple[str, str]]:
    """Read local IPv4/interface pairs without contacting a radio."""

    try:
        output = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
    except Exception:
        return []
    result: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        interface = parts[1].split("@", 1)[0]
        host = parts[3].split("/", 1)[0]
        result.append((interface, host))
    return result


def server_target_candidates() -> list[ServerTargetCandidate]:
    """Return every usable host address, preferring explicit configuration.

    Docker/Podman/virtual bridge interfaces are filtered by interface name,
    rather than by private address range: a real LAN may legitimately use a
    172.16/12 address.
    """

    settings = get_settings()
    rows = _interface_addresses()
    by_host = {host: interface for interface, host in rows}
    candidates: dict[str, ServerTargetCandidate] = {}

    def add(host: str, *, interface: str, source: str, configured: bool = False) -> None:
        normalized = str(host or "").strip()
        if (
            not is_safe_radio_host(normalized)
            or is_protected_ip(normalized)
            or _is_virtual_interface(interface)
        ):
            return
        existing = candidates.get(normalized)
        if existing is None or configured:
            candidates[normalized] = ServerTargetCandidate(
                normalized,
                interface or "unbekannt",
                source,
                configured,
            )

    for interface, host in rows:
        add(host, interface=interface, source="Netzwerkschnittstelle")

    configured = str(settings.lan_host or "").strip()
    if configured:
        add(
            configured,
            interface=by_host.get(configured, "konfiguriert"),
            source="BASSWIESN-Konfiguration",
            configured=bool(settings.lan_host_configured),
        )

    for host in getattr(settings, "lan_host_candidates", ()):
        add(
            host,
            interface=by_host.get(host, "Host-LAN"),
            source="Installer-Erkennung",
            configured=True,
        )

    if settings.test_mode:
        add(
            "192.0.2.10",
            interface="simulation",
            source="Testmodus",
        )

    return sorted(
        candidates.values(),
        key=lambda item: (
            0 if item.configured else 1,
            0 if item.interface.lower().startswith(("wl", "en", "eth")) else 1,
            item.host,
        ),
    )


def _port(value: object, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = default
    if not 1 <= port <= 65535:
        raise ValueError("server port must be between 1 and 65535")
    return port


def resolve_server_target(payload: dict | None = None) -> ServerTarget:
    payload = payload or {}
    settings = get_settings()
    candidate = str(
        payload.get("server_host")
        or payload.get("host")
        or settings.lan_host
        or ""
    ).strip()
    if not candidate:
        raise ValueError(
            "Keine geeignete LAN-Adresse dieses BASSWIESN-Servers wurde erkannt. "
            "Verbinde den Server mit dem Radio-LAN und wähle die dort angebotene Adresse."
        )
    return ServerTarget(
        host=_host(candidate),
        web_port=_port(payload.get("web_port"), settings.web_port),
        cloud_port=_port(payload.get("cloud_port"), settings.cloud_port),
        debug_port=_port(payload.get("debug_port"), settings.debug_port),
    )
