"""Network discovery adapter for SoundTouch devices."""

import asyncio
import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice
from typing import Protocol
from xml.etree import ElementTree as ET

import httpx

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import BASSWIESN_TABOO_HOSTS, get_settings
from basswiesn.app.services.protected_devices import is_protected_ip, protected_device_ips


class SoundTouchInfoClient(Protocol):
    async def info(self) -> dict[str, str]: ...


ClientFactory = Callable[[str], SoundTouchInfoClient]


@dataclass(frozen=True)
class ProbeFailure:
    ip_address: str
    code: str
    message: str


@dataclass(frozen=True)
class DiscoveryScanResult:
    scanned: int
    devices: list[dict[str, str]]
    failures: list[ProbeFailure]


async def probe_device_detailed(
    ip_address: str,
    *,
    client_factory: ClientFactory,
) -> tuple[dict[str, str] | None, ProbeFailure | None]:
    """Probe one host while retaining an actionable failure category."""

    # Keep the guard in the lowest-level discovery entry point as well as in
    # the subnet builder. Callers may inject a custom client factory, so
    # filtering only in ``scan_subnet_detailed`` would not be sufficient to
    # guarantee that the immutable protected target is never contacted.
    if is_protected_ip(ip_address):
        return None, ProbeFailure(
            ip_address,
            "protected_device",
            "protected device access blocked before discovery transport",
        )

    try:
        return await client_factory(ip_address).info(), None
    except httpx.TimeoutException as exc:
        return None, ProbeFailure(ip_address, "timeout", str(exc) or "probe timed out")
    except ET.ParseError as exc:
        return None, ProbeFailure(ip_address, "invalid_response", str(exc))
    except (httpx.HTTPError, OSError) as exc:
        return None, ProbeFailure(ip_address, "unreachable", str(exc))


async def probe_device(
    ip_address: str,
    *,
    client_factory: ClientFactory = SoundTouchClient,
) -> dict[str, str] | None:
    """Return device info or ``None`` for expected reachability failures."""

    device, _failure = await probe_device_detailed(
        ip_address, client_factory=client_factory
    )
    return device


async def scan_subnet_detailed(
    cidr: str,
    limit: int = 512,
    *,
    timeout: float = 0.7,
    concurrency: int = 64,
    client_factory: ClientFactory | None = None,
) -> DiscoveryScanResult:
    """Scan a subnet and retain failures without exposing network operations."""

    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    network = ipaddress.ip_network(cidr, strict=False)
    excluded = set(BASSWIESN_TABOO_HOSTS) | protected_device_ips()
    try:
        excluded.add(get_settings().lan_host)
    except Exception:
        pass
    hosts = [str(host) for host in islice(network.hosts(), max(limit, 0)) if str(host) not in excluded]
    factory = client_factory or (
        lambda ip: SoundTouchClient(ip, get_timeout=timeout)
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def limited_probe(
        host: str,
    ) -> tuple[dict[str, str] | None, ProbeFailure | None]:
        async with semaphore:
            return await probe_device_detailed(host, client_factory=factory)

    results = await asyncio.gather(*(limited_probe(host) for host in hosts))
    return DiscoveryScanResult(
        scanned=len(hosts),
        devices=[device for device, _failure in results if device is not None],
        failures=[failure for _device, failure in results if failure is not None],
    )


async def scan_subnet(
    cidr: str,
    limit: int = 512,
    *,
    client_factory: ClientFactory = SoundTouchClient,
    concurrency: int = 64,
) -> list[dict[str, str]]:
    """Probe at most ``limit`` hosts without persisting discovery results."""

    result = await scan_subnet_detailed(
        cidr,
        limit,
        concurrency=concurrency,
        client_factory=client_factory,
    )
    return result.devices
