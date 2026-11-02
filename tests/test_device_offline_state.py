import asyncio

from basswiesn.app import db as app_db
from basswiesn.app.models import Device
from basswiesn.app.db.repositories import DeviceRepository
from basswiesn.app.services.device_service import DeviceService, device_summary


def test_refresh_failure_marks_offline_after_threshold_and_keeps_device():
    db = app_db.SessionLocal()
    device = Device(device_id="OFFLINE1", name="Offline", model="SoundTouch 20", ip_address="192.0.2.10")
    db.add(device)
    db.commit()

    class FailingClient:
        def __init__(self, _ip):
            pass

        async def get_xml(self, _path):
            raise OSError("network unreachable")

    service = DeviceService(DeviceRepository(db), client_factory=FailingClient)
    for _ in range(3):
        result = asyncio.run(service.refresh_device(device))
    db.commit()
    db.refresh(device)

    assert result["ok"] is False
    assert db.query(Device).filter(Device.device_id == "OFFLINE1").one() is not None
    assert device.failure_count == 3
    assert device.reachable is False
    assert device_summary(device)["ready"] is False
    assert "network unreachable" in device.offline_reason
    db.close()


def test_refresh_success_resets_offline_state():
    db = app_db.SessionLocal()
    device = Device(device_id="RECOVER1", name="Recover", model="SoundTouch 20", ip_address="192.0.2.11", reachable=False, failure_count=7, offline_reason="old")
    db.add(device)
    db.commit()

    class OkClient:
        def __init__(self, _ip):
            pass

        async def get_xml(self, _path):
            return '<info deviceID="RECOVER1"><name>Recover</name><type>SoundTouch 20</type><components><component><softwareVersion>27.0.6</softwareVersion></component></components></info>'

    result = asyncio.run(DeviceService(DeviceRepository(db), client_factory=OkClient).refresh_device(device))
    db.commit()
    db.refresh(device)

    assert result["ok"] is True
    assert device.reachable is True
    assert device.failure_count == 0
    assert device.offline_reason == ""
    assert device.last_failed_at is None
    assert device_summary(device)["ready"] is True
    db.close()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
