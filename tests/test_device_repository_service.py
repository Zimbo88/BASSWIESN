import asyncio

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device
from basswiesn.app.repositories.device_repository import DeviceRepository
from basswiesn.app.services.device_service import DeviceService, device_summary


def _discovered(device_id: str = "DEVICE-1", ip_address: str = "192.0.2.41") -> dict[str, str]:
    return {
        "device_id": device_id,
        "ip_address": ip_address,
        "name": "Wohnzimmer",
        "model": "SoundTouch 30",
        "raw": (
            f'<info deviceID="{device_id}"><name>Wohnzimmer</name>'
            "<type>SoundTouch 30</type><components><component>"
            "<softwareVersion>27.0.13</softwareVersion>"
            "</component></components></info>"
        ),
    }


def test_device_repository_add_lookup_and_commit():
    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    try:
        device = repository.add(Device(device_id="REPO-1", ip_address="192.0.2.40"))
        repository.commit()

        assert repository.get_by_device_id("REPO-1") is device
        assert repository.get_latest_by_ip("192.0.2.40") is device
        assert repository.list_by_name() == [device]
    finally:
        session.close()


def test_device_service_persists_new_discovery_record():
    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    try:
        result = DeviceService(repository).record_discovery([_discovered()], persist=True)
        saved = repository.get_by_device_id("DEVICE-1")

        assert result == [
            {
                "device_id": "DEVICE-1",
                "ip_address": "192.0.2.41",
                "name": "Wohnzimmer",
                "model": "SoundTouch 30",
                "firmware": "27.0.13",
            }
        ]
        assert saved is not None
        assert saved.info_xml == _discovered()["raw"]
    finally:
        session.close()


def test_device_service_updates_existing_ip_identity_without_duplicate():
    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    try:
        repository.add(Device(device_id="192.0.2.42", ip_address="192.0.2.42"))
        repository.commit()

        DeviceService(repository).record_discovery(
            [_discovered(device_id="CANONICAL-42", ip_address="192.0.2.42")],
            persist=True,
        )

        assert repository.get_by_device_id("CANONICAL-42") is not None
        assert session.query(Device).count() == 1
    finally:
        session.close()


def test_device_service_preview_does_not_persist_and_filters_invalid_xml():
    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    try:
        invalid = {**_discovered(), "raw": "<info"}
        result = DeviceService(repository).record_discovery(
            [_discovered(), invalid],
            persist=False,
        )

        assert len(result) == 1
        assert session.query(Device).count() == 0
    finally:
        session.close()


def test_device_service_lists_devices_by_name():
    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    try:
        repository.add(Device(device_id="B", name="Zimmer"))
        repository.add(Device(device_id="A", name="Arbeitszimmer"))
        repository.commit()

        rows = DeviceService(repository).list_devices()

        assert [row.device_id for row in rows] == ["A", "B"]
    finally:
        session.close()


def test_device_summary_preserves_existing_api_shape():
    summary = device_summary(
        Device(
            device_id="SUMMARY-1",
            ip_address="192.0.2.62",
            info_xml=(
                '<info deviceID="RADIO-SUMMARY-1"><name>Summary Radio</name>'
                "<type>SoundTouch 20</type><margeURL>http://basswiesn:1516</margeURL>"
                "<components><component><componentCategory>PackagedProduct</componentCategory>"
                "<serialNumber>SERIAL-1</serialNumber><softwareVersion>27.0.13.123</softwareVersion>"
                "</component></components></info>"
            ),
        )
    )

    assert summary["radio_device_id"] == "RADIO-SUMMARY-1"
    assert summary["serial_number"] == "SERIAL-1"
    assert summary["firmware"] == "27.0.13"
    assert summary["configured_for"] == "basswiesn"
    assert summary["ready"] is True


def test_device_service_upserts_local_device_without_overwriting_missing_fields():
    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    service = DeviceService(repository)
    try:
        created = service.upsert_local_device(
            device_id="LOCAL-1",
            ip_address="192.0.2.43",
            name="Küche",
            model="SoundTouch 10",
        )
        repository.commit()
        updated = service.upsert_local_device(
            device_id="LOCAL-1",
            ip_address="192.0.2.44",
        )
        repository.commit()

        assert updated is created
        assert updated.name == "Küche"
        assert updated.model == "SoundTouch 10"
        assert updated.ip_address == "192.0.2.44"
    finally:
        session.close()


def test_device_service_uses_existing_ip_record_for_local_upsert():
    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    try:
        existing = repository.add(Device(device_id="TEMP-IP", ip_address="192.0.2.45"))
        repository.commit()

        result = DeviceService(repository).upsert_local_device(
            device_id="REQUESTED-ID",
            ip_address="192.0.2.45",
            name="Portable",
        )

        assert result is existing
        assert result.device_id == "TEMP-IP"
        assert result.name == "Portable"
        assert session.query(Device).count() == 1
    finally:
        session.close()


def test_device_service_refreshes_device_with_injected_client():
    xml = (
        '<info deviceID="LIVE-1"><name>Live Radio</name><type>SoundTouch 20</type>'
        "<components><component><softwareVersion>27.0.13</softwareVersion>"
        "</component></components></info>"
    )

    class Client:
        async def get_xml(self, path: str) -> str:
            assert path == "/info"
            return xml

    session = app_db.SessionLocal()
    repository = DeviceRepository(session)
    device = Device(device_id="LIVE-1", ip_address="192.0.2.46")
    try:
        result = asyncio.run(
            DeviceService(repository, client_factory=lambda _ip: Client()).refresh_device(device)
        )

        assert result["ok"] is True
        assert device.name == "Live Radio"
        assert device.model == "SoundTouch 20"
        assert device.firmware == "27.0.13"
        assert device.info_xml == xml
    finally:
        session.close()


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("offline"), OSError("network down")],
)
def test_device_service_maps_expected_probe_failures(failure):
    class Client:
        async def get_xml(self, _path: str) -> str:
            raise failure

    session = app_db.SessionLocal()
    device = Device(device_id="LIVE-2", ip_address="192.0.2.47", name="Unchanged")
    try:
        result = asyncio.run(
            DeviceService(
                DeviceRepository(session), client_factory=lambda _ip: Client()
            ).refresh_device(device)
        )

        assert result["ok"] is False
        assert device.name == "Unchanged"
    finally:
        session.close()


def test_device_service_maps_invalid_info_xml_to_probe_failure():
    class Client:
        async def get_xml(self, _path: str) -> str:
            return "<info"

    session = app_db.SessionLocal()
    try:
        result = asyncio.run(
            DeviceService(
                DeviceRepository(session), client_factory=lambda _ip: Client()
            ).refresh_device(Device(device_id="LIVE-3", ip_address="192.0.2.48"))
        )

        assert result["ok"] is False
    finally:
        session.close()


def test_device_service_does_not_hide_unexpected_probe_errors():
    class Client:
        async def get_xml(self, _path: str) -> str:
            raise RuntimeError("programming defect")

    session = app_db.SessionLocal()
    try:
        with pytest.raises(RuntimeError, match="programming defect"):
            asyncio.run(
                DeviceService(
                    DeviceRepository(session), client_factory=lambda _ip: Client()
                ).refresh_device(Device(device_id="LIVE-4", ip_address="192.0.2.49"))
            )
    finally:
        session.close()


def test_device_service_refreshes_multiple_devices_in_input_order():
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, _path: str) -> str:
            return f'<info deviceID="{self.ip_address}"><name>{self.ip_address}</name></info>'

    session = app_db.SessionLocal()
    devices = [
        Device(device_id="A", ip_address="192.0.2.60"),
        Device(device_id="B", ip_address="192.0.2.61"),
    ]
    try:
        results = asyncio.run(
            DeviceService(DeviceRepository(session), client_factory=Client).refresh_devices(devices)
        )

        assert [result["device"] for result in results] == devices
        assert [device.name for device in devices] == ["192.0.2.60", "192.0.2.61"]
    finally:
        session.close()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
