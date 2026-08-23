from basswiesn.app import db as app_db
from basswiesn.app.models import Device, MultiroomScenario, Preset, SetupPlan
from basswiesn.app.repositories.device_identity_repository import DeviceIdentityRepository
from basswiesn.app.services.device_identity_service import DeviceIdentityService


def test_identity_service_keeps_matching_or_invalid_identity_unchanged():
    session = app_db.SessionLocal()
    device = Device(device_id="SAME-1")
    service = DeviceIdentityService(DeviceIdentityRepository(session))
    try:
        same, same_result = service.reconcile(device, '<info deviceID="SAME-1" />')
        invalid, invalid_result = service.reconcile(device, "<info")

        assert same is device
        assert invalid is device
        assert same_result == {"merged": False, "canonical_id": "SAME-1"}
        assert invalid_result == same_result
    finally:
        session.close()


def test_identity_service_migrates_device_when_canonical_row_does_not_exist():
    session = app_db.SessionLocal()
    device = Device(device_id="192.0.2.70")
    session.add(device)
    session.commit()
    try:
        migrated, result = DeviceIdentityService(
            DeviceIdentityRepository(session)
        ).reconcile(device, '<info deviceID="CANONICAL-70" />')
        session.commit()

        assert migrated.device_id == "CANONICAL-70"
        assert result == {
            "merged": False,
            "migrated": True,
            "old_device_id": "192.0.2.70",
            "canonical_id": "CANONICAL-70",
        }
    finally:
        session.close()


def test_identity_service_merges_presets_and_text_and_csv_references():
    session = app_db.SessionLocal()
    source = Device(
        device_id="TEMP-71",
        name="Source Name",
        ip_address="192.0.2.71",
    )
    target = Device(device_id="CANONICAL-71")
    session.add_all([source, target])
    session.flush()
    session.add_all(
        [
            Preset(device_id="TEMP-71", button=1, station_id=123, location="source"),
            Preset(device_id="CANONICAL-71", button=1, location="target"),
            SetupPlan(device_id="TEMP-71", name="Plan"),
            MultiroomScenario(
                name="Merge Scenario",
                master_device_id="TEMP-71",
                member_device_ids="TEMP-71,CANONICAL-71,OTHER",
            ),
        ]
    )
    session.commit()
    try:
        merged, result = DeviceIdentityService(
            DeviceIdentityRepository(session)
        ).reconcile(source, '<info deviceID="CANONICAL-71" />')
        session.commit()

        preset = session.query(Preset).filter(Preset.device_id == "CANONICAL-71").one()
        plan = session.query(SetupPlan).one()
        scenario = session.query(MultiroomScenario).one()
        assert merged is target
        assert result["merged"] is True
        assert preset.station_id == 123
        assert plan.device_id == "CANONICAL-71"
        assert scenario.master_device_id == "CANONICAL-71"
        assert scenario.member_device_ids == "CANONICAL-71,OTHER"
        assert target.name == "Source Name"
        assert target.ip_address == "192.0.2.71"
        assert session.query(Device).count() == 1
    finally:
        session.close()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
