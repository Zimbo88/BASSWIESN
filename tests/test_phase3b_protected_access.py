from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.adapters.discovery import scan_subnet_detailed
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.models import Device, Setting
from basswiesn.app.services.action_preflight import port_open
from basswiesn.app.services.device_interactions import InteractionPriority, coordinator
from basswiesn.app.services.device_service import DeviceService
from basswiesn.app.routers import api as api_router
from basswiesn.app.services import health_center, lab_tools, telnet_device_control
from basswiesn.app.services.playback_keepalive import run_playback_keepalive_for_device
from basswiesn.app.services.ssdp_discovery import discover_ssdp


pytestmark = pytest.mark.integration
PROTECTED_IP = "192.168.50.25"
PROTECTED_ID = "CCDDEEFF0011"


def _protect(value: str = PROTECTED_IP) -> None:
    db = app_db.SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "protected_device_ips").one_or_none()
        if row is None:
            row = Setting(key="protected_device_ips")
            db.add(row)
        row.value = value
        db.commit()
    finally:
        db.close()


def _device(*, ip: str = PROTECTED_IP, device_id: str = PROTECTED_ID) -> Device:
    db = app_db.SessionLocal()
    row = Device(device_id=device_id, name="Protected test device", ip_address=ip, model="SoundTouch Test")
    db.add(row)
    db.commit()
    db.refresh(row)
    db.close()
    return row


def test_protected_http_reads_are_rejected_before_transport():
    _protect()
    called: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return httpx.Response(200, text="<unexpected />")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            radio = SoundTouchClient(PROTECTED_IP, http_client=client, device_id=PROTECTED_ID)
            for endpoint in ("/now_playing", "/volume", "/presets", "/info"):
                with pytest.raises(Exception) as error:
                    await radio.get_xml(endpoint)
                assert getattr(error.value, "status_code", None) == 403

    asyncio.run(scenario())
    assert called == []


def test_protected_device_id_blocks_even_when_request_ip_differs():
    _protect()
    _device()
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="<unexpected />")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SoundTouchClient("192.0.2.99", http_client=client, device_id=PROTECTED_ID).get_xml("/info")

    with pytest.raises(Exception) as error:
        asyncio.run(scenario())
    assert getattr(error.value, "status_code", None) == 403
    assert called is False


def test_unseen_unprotected_identity_is_allowed_for_explicit_discovery():
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="<unexpected />")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SoundTouchClient("192.0.2.99", http_client=client, device_id="UNKNOWN-ID").get_xml("/info")

    asyncio.run(scenario())
    assert called is True


def test_keepalive_skips_protected_device_without_creating_client():
    _protect()
    device = _device()
    calls: list[str] = []

    class Client:
        def __init__(self, _ip: str):
            calls.append("constructed")

    db = app_db.SessionLocal()
    try:
        result = asyncio.run(run_playback_keepalive_for_device(device, db, client_factory=Client))
    finally:
        db.close()
    assert result["skipped"] is True
    assert result["protected"] is True
    assert calls == []


def test_device_refresh_skips_protected_device_before_injected_client():
    _protect()
    device = _device()
    calls: list[str] = []

    class Client:
        def __init__(self, _ip: str):
            calls.append("constructed")

        async def get_xml(self, _path: str) -> str:
            calls.append("request")
            return "<info />"

    db = app_db.SessionLocal()
    try:
        device = db.query(Device).filter(Device.device_id == PROTECTED_ID).one()
        service = DeviceService.__new__(DeviceService)
        service.client_factory = Client
        result = asyncio.run(service.refresh_device(device))
    finally:
        db.close()
    assert result["protected"] is True
    assert result["skipped"] is True
    assert calls == []


def test_coordinator_skips_protected_get_without_transport():
    _protect()
    _device()
    db = app_db.SessionLocal()
    try:
        device = db.query(Device).filter(Device.device_id == PROTECTED_ID).one()
        result = asyncio.run(
            coordinator.request_xml(
                db,
                device,
                "/now_playing",
                method="GET",
                request_purpose="test",
                requester="test",
                priority=InteractionPriority.HEALTHCHECK,
            )
        )
        db.commit()
    finally:
        db.close()
    assert result.skipped is True
    assert result.status_code == 403
    assert result.error_class == "ProtectedDevice"


def test_protected_port_and_lab_probes_do_not_open_sockets(monkeypatch):
    _protect()

    def fail(*_args, **_kwargs):
        raise AssertionError("protected device was probed")

    monkeypatch.setattr(health_center.socket, "create_connection", fail)
    monkeypatch.setattr(api_router.socket, "create_connection", fail)
    monkeypatch.setattr(telnet_device_control.socket, "create_connection", fail)
    assert health_center._port_open(PROTECTED_IP, 8090) is False
    assert api_router._tcp_port_open(PROTECTED_IP, 8090)[0] is False
    assert telnet_device_control._port_open(PROTECTED_IP, 23) is False
    assert asyncio.run(port_open(PROTECTED_IP, 8090)) is False
    monkeypatch.setattr(lab_tools, "get_settings", lambda: SimpleNamespace(lab_mode=True))
    result = lab_tools.probe_port(PROTECTED_IP, 8090)
    assert result["skipped"] is True
    assert result["ok"] is False


def test_discovery_excludes_protected_ip_before_probe():
    _protect("192.0.2.25")
    probed: list[str] = []

    class Client:
        def __init__(self, ip: str):
            probed.append(ip)

        async def info(self) -> dict[str, str]:
            return {}

    result = asyncio.run(
        scan_subnet_detailed(
            "192.0.2.0/27",
            limit=31,
            concurrency=4,
            client_factory=Client,
        )
    )
    assert PROTECTED_IP not in probed
    assert result.scanned == 29


def test_direct_discovery_probe_blocks_protected_ip_before_custom_client():
    _protect("192.0.2.25")
    from basswiesn.app.adapters.discovery import probe_device_detailed

    called = False

    def factory(_ip):
        nonlocal called
        called = True
        raise AssertionError("protected device was probed")

    device, failure = asyncio.run(
        probe_device_detailed("192.0.2.25", client_factory=factory)
    )
    assert device is None
    assert failure is not None
    assert failure.code == "protected_device"
    assert called is False


def test_device_api_exposes_full_protection_status_without_live_access():
    _protect()
    _device()

    # Exercise the route projection directly. Starlette's deprecated
    # TestClient/httpx compatibility layer can deadlock on Python 3.14; the
    # separate browser safety test still covers the mounted HTTP contract.
    from basswiesn.app.api.routes_devices import list_devices

    db = app_db.SessionLocal()
    try:
        response = asyncio.run(list_devices(live=False, db=db))
    finally:
        db.close()

    protected = next(item for item in response if item["device_id"] == PROTECTED_ID)
    assert protected["protected"] is True
    assert protected["access_protected"] is True
    assert protected["protection_label"] == "GESCHUETZT - VOLLSTAENDIG GESPERRT"
    assert protected["protection_scope"] == "kein automatischer oder manueller Netzwerkzugriff"


def test_ssdp_multicast_filters_protected_reply_before_unicast(monkeypatch):
    _protect()

    from basswiesn.app.services.ssdp_discovery import SSDPCandidate

    replies = [SSDPCandidate(
        location=f"http://{PROTECTED_IP}:8090/description.xml",
        usn=f"uuid:{PROTECTED_ID}",
        server="Bose SoundTouch",
        st="ssdp:all",
        remote_ip=PROTECTED_IP,
    )]

    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("protected descriptor must never be fetched")

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", fail_fetch)
    db = app_db.SessionLocal()
    try:
        # Feed the captured multicast reply directly. This keeps the safety
        # contract deterministic without depending on Python's process-wide
        # default thread executor during loop teardown.
        result = asyncio.run(discover_ssdp(db, timeout_seconds=1, candidates=replies))
    finally:
        db.close()
    assert result["devices"] == []
    assert result["candidates"] == []


def test_ssdp_filters_protected_identity_after_ip_change_before_unicast(monkeypatch):
    from basswiesn.app import config
    from basswiesn.app.services.ssdp_discovery import SSDPCandidate

    monkeypatch.setenv("PROTECTED_DEVICE_IDS", PROTECTED_ID)
    config.get_settings.cache_clear()

    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("protected identity descriptor must never be fetched")

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", fail_fetch)
    candidate = SSDPCandidate(
        location="http://192.0.2.77:8090/description.xml",
        usn=f"uuid:{PROTECTED_ID}::urn:schemas-upnp-org:device:MediaRenderer:1",
        server="Bose SoundTouch",
        st="ssdp:all",
        remote_ip="192.0.2.77",
    )
    db = app_db.SessionLocal()
    try:
        result = asyncio.run(discover_ssdp(db, candidates=[candidate]))
    finally:
        db.close()
        config.get_settings.cache_clear()
    assert result["devices"] == []
    assert result["candidates"] == []


def test_ssdp_filters_real_wrapped_protected_identity_after_ip_change_before_unicast(monkeypatch):
    from basswiesn.app import config
    from basswiesn.app.services.ssdp_discovery import SSDPCandidate

    monkeypatch.setenv("PROTECTED_DEVICE_IDS", PROTECTED_ID)
    config.get_settings.cache_clear()

    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("wrapped protected identity descriptor must never be fetched")

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", fail_fetch)
    candidate = SSDPCandidate(
        location=f"http://192.0.2.87:8091/XD/BO5EBO5E-F00D-F00D-FEED-{PROTECTED_ID}.xml",
        usn=f"uuid:BO5EBO5E-F00D-F00D-FEED-{PROTECTED_ID}::upnp:rootdevice",
        server="Bose SoundTouch",
        st="ssdp:all",
        remote_ip="192.0.2.87",
    )
    db = app_db.SessionLocal()
    try:
        result = asyncio.run(discover_ssdp(db, candidates=[candidate]))
    finally:
        db.close()
        config.get_settings.cache_clear()
    assert result["devices"] == []
    assert result["candidates"] == []


def test_ssdp_refuses_unicast_when_multicast_reply_has_no_identity(monkeypatch):
    from basswiesn.app.services.ssdp_discovery import SSDPCandidate

    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("identity-free SSDP reply must never trigger unicast")

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", fail_fetch)
    candidate = SSDPCandidate(
        location="http://192.0.2.78:8090/description.xml",
        usn="",
        server="Bose SoundTouch",
        st="ssdp:all",
        remote_ip="192.0.2.78",
    )
    db = app_db.SessionLocal()
    try:
        result = asyncio.run(discover_ssdp(db, candidates=[candidate]))
    finally:
        db.close()
    assert result["devices"] == []
    assert result["candidates"] == []


def test_explicit_ssdp_allows_unprotected_known_identity_to_rebind_dhcp_ip(monkeypatch):
    from basswiesn.app.services.ssdp_discovery import SSDPCandidate

    device_id = "KNOWN-UNPROTECTED"
    old_ip = "192.0.2.79"
    new_ip = "192.0.2.80"
    db = app_db.SessionLocal()
    db.add(Device(device_id=device_id, name="Known", ip_address=old_ip))
    db.commit()

    async def descriptor(_candidate, *, timeout_seconds):
        assert timeout_seconds >= 1
        return True, {
            "friendlyName": "Known moved radio",
            "manufacturer": "Bose",
            "modelName": "SoundTouch 20",
            "modelDescription": "",
            "UDN": f"uuid:{device_id}",
        }, "ok"

    monkeypatch.setattr("basswiesn.app.services.ssdp_discovery._fetch_descriptor", descriptor)
    candidate = SSDPCandidate(
        location=f"http://{new_ip}:8090/description.xml",
        usn=f"uuid:{device_id}::upnp:rootdevice",
        server="Bose SoundTouch",
        st="ssdp:all",
        remote_ip=new_ip,
    )
    try:
        result = asyncio.run(discover_ssdp(db, candidates=[candidate]))
        db.commit()
        moved = db.query(Device).filter(Device.device_id == device_id).one()
        assert result["devices"][0]["ip_address"] == new_ip
        assert moved.ip_address == new_ip
    finally:
        db.close()


def test_unprotected_device_refresh_still_uses_injected_client():
    calls: list[str] = []

    class Client:
        async def get_xml(self, path: str) -> str:
            calls.append(path)
            return '<info><name>Allowed</name><type>SoundTouch Test</type><softwareVersion>1</softwareVersion></info>'

    db = app_db.SessionLocal()
    try:
        device = Device(device_id="ALLOWED", name="Allowed", ip_address="192.0.2.26")
        db.add(device)
        db.commit()
        service = DeviceService.__new__(DeviceService)
        service.client_factory = lambda _ip: Client()
        result = asyncio.run(service.refresh_device(device))
    finally:
        db.close()
    assert result["ok"] is True
    assert calls == ["/info"]
