import asyncio
import logging
from xml.etree import ElementTree as ET

import httpx

from basswiesn.app.adapters.discovery import (
    DiscoveryScanResult,
    ProbeFailure,
    probe_device_detailed,
    scan_subnet_detailed,
)
from basswiesn.app.services.device_discovery_service import DeviceDiscoveryService


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error

    async def info(self):
        if self.error:
            raise self.error
        return self.result


def test_discovery_reports_unreachable_host():
    device, failure = asyncio.run(
        probe_device_detailed(
            "192.0.2.81",
            client_factory=lambda _ip: FakeClient(error=httpx.ConnectError("refused")),
        )
    )
    assert device is None
    assert failure.code == "unreachable"


def test_discovery_reports_timeout():
    _device, failure = asyncio.run(
        probe_device_detailed(
            "192.0.2.82",
            client_factory=lambda _ip: FakeClient(error=httpx.ReadTimeout("slow")),
        )
    )
    assert failure.code == "timeout"


def test_discovery_reports_invalid_response():
    _device, failure = asyncio.run(
        probe_device_detailed(
            "192.0.2.83",
            client_factory=lambda _ip: FakeClient(error=ET.ParseError("bad XML")),
        )
    )
    assert failure.code == "invalid_response"


def test_discovery_successful_scan_without_network(caplog):
    async def scanner(cidr, *, limit, timeout, concurrency):
        assert (cidr, limit, timeout, concurrency) == (
            "192.0.2.0/30",
            2,
            0.2,
            64,
        )
        return await scan_subnet_detailed(
            cidr,
            limit=limit,
            timeout=timeout,
            concurrency=concurrency,
            client_factory=lambda ip: FakeClient(
                {"device_id": ip, "ip_address": ip, "raw": '<info deviceID="x" />'}
            ),
        )

    caplog.set_level(logging.WARNING)
    result = asyncio.run(
        DeviceDiscoveryService(scanner=scanner).discover(
            "192.0.2.0/30", timeout=0.2, limit=2
        )
    )
    assert result.scanned == 2
    assert len(result.devices) == 2
    assert result.failures == []
    assert caplog.records == []


def test_discovery_service_logs_failures(caplog):
    async def scanner(_cidr, *, limit, timeout, concurrency):
        return DiscoveryScanResult(
            scanned=1,
            devices=[],
            failures=[ProbeFailure("192.0.2.84", "timeout", "slow")],
        )

    caplog.set_level(logging.INFO)
    asyncio.run(DeviceDiscoveryService(scanner=scanner).discover("192.0.2.84/32"))

    assert "SoundTouch discovery summary" in caplog.text
    assert "timeouts=1" in caplog.text
    assert "ip=192.0.2.84 code=timeout message=slow" not in caplog.text
def test_discovery_log_records_escape_untrusted_line_breaks(caplog):
    # A custom scanner or IPv6 scope identifier must not inject log records.
    async def scanner(_cidr, **_kwargs):
        return DiscoveryScanResult(
            scanned=1, devices=[],
            failures=[ProbeFailure("192.0.2.84\nFORGED", "timeout\rFORGED", "error\nFORGED")],
        )

    caplog.set_level(logging.DEBUG, logger="basswiesn.app.services.device_discovery_service")
    asyncio.run(DeviceDiscoveryService(scanner=scanner).discover("fe80::%scope\nFORGED/64"))
    records = [r.getMessage() for r in caplog.records
               if r.name == "basswiesn.app.services.device_discovery_service"]
    assert len(records) == 2
    assert all("\n" not in text and "\r" not in text for text in records)
    assert all("\\nFORGED" in text for text in records)
    assert "\\rFORGED" in records[1]


import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
