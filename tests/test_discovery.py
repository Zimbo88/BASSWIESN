import asyncio
from xml.etree import ElementTree as ET

import httpx
import pytest

from basswiesn.app.adapters.discovery import probe_device, scan_subnet
from basswiesn.app.services.discovery import scan_subnet as compatibility_scan_subnet


class FakeInfoClient:
    def __init__(self, result: dict[str, str] | None = None, error: Exception | None = None):
        self.result = result or {}
        self.error = error

    async def info(self) -> dict[str, str]:
        if self.error is not None:
            raise self.error
        return self.result


def test_probe_device_returns_info_from_injected_client_factory():
    expected = {"device_id": "DEVICE-1", "name": "Wohnzimmer"}

    result = asyncio.run(
        probe_device("192.0.2.20", client_factory=lambda _ip: FakeInfoClient(expected))
    )

    assert result == expected


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        ET.ParseError("invalid XML"),
        OSError("network unavailable"),
    ],
)
def test_probe_device_maps_expected_discovery_failures_to_none(error):
    result = asyncio.run(
        probe_device("192.0.2.21", client_factory=lambda _ip: FakeInfoClient(error=error))
    )

    assert result is None


def test_probe_device_does_not_hide_unexpected_programming_errors():
    with pytest.raises(RuntimeError, match="broken parser"):
        asyncio.run(
            probe_device(
                "192.0.2.22",
                client_factory=lambda _ip: FakeInfoClient(error=RuntimeError("broken parser")),
            )
        )


def test_scan_subnet_honors_limit_and_filters_unreachable_devices():
    probed: list[str] = []

    def factory(ip_address: str) -> FakeInfoClient:
        probed.append(ip_address)
        if ip_address.endswith(".2"):
            return FakeInfoClient(error=httpx.ConnectError("offline"))
        return FakeInfoClient({"device_id": ip_address, "name": f"Radio {ip_address}"})

    result = asyncio.run(scan_subnet("192.0.2.0/29", limit=3, client_factory=factory))

    assert probed == ["192.0.2.1", "192.0.2.2", "192.0.2.3"]
    assert [item["device_id"] for item in result] == ["192.0.2.1", "192.0.2.3"]
    assert compatibility_scan_subnet is scan_subnet


def test_scan_subnet_limits_probe_concurrency():
    active = 0
    maximum_active = 0

    class ConcurrentClient:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def info(self) -> dict[str, str]:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"device_id": self.ip_address}

    result = asyncio.run(
        scan_subnet(
            "192.0.2.0/29",
            limit=5,
            concurrency=2,
            client_factory=ConcurrentClient,
        )
    )

    assert len(result) == 5
    assert maximum_active == 2
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
