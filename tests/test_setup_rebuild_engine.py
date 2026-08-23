import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from basswiesn.app.db.database import Base
from basswiesn.app.models import Device
from basswiesn.app.services.network_safety import NetworkSafetyError, assert_transport_allowed
from basswiesn.app.services.setup_rebuild.coordinator import SetupCoordinator
from basswiesn.app.services.setup_rebuild.profiles import DeviceFacts, detect_profile
from basswiesn.app.services.setup_rebuild.profiles.activation import get_operation
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
from basswiesn.app.services.setup_rebuild.states import SetupState, transition


def test_identity_required_for_strict_setup_transport():
    with pytest.raises(NetworkSafetyError):
        assert_transport_allowed(
            "192.0.2.25",
            transport="HTTP",
            approved_only=True,
        )
    assert_transport_allowed("192.0.2.25", transport="HTTP", approved_only=False)


def test_state_machine_rejects_skipping_backup_or_routing_backup():
    assert transition(SetupState.UNKNOWN, SetupState.DISCOVERED) is SetupState.DISCOVERED
    with pytest.raises(ValueError):
        transition(SetupState.IDENTIFIED, SetupState.BASSWIESN_ROUTE_ACTIVE)


def test_profiles_are_model_specific():
    stationary = detect_profile(
        DeviceFacts(
            "A", "192.0.2.10", "SoundTouch 20",
            "27.0.6.46330.5043500 epdbuild.test", variant="spotty", platform="sm2",
        )
    )
    portable = detect_profile(
        DeviceFacts(
            "B", "192.0.2.11", "SoundTouch Portable",
            "27.0.6.46330.5043500 epdbuild.test", variant="taigan", platform="scm",
        )
    )
    assert stationary.profile is not None
    assert stationary.profile.model_family == "stationary"
    assert portable.profile is not None
    assert portable.profile.model_family == "portable"
    with pytest.raises(ValueError):
        get_operation("arbitrary.shell.command")


def test_dry_run_coordinator_persists_all_checkpoints_without_transport():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    coordinator = SetupCoordinator(session_factory=Session)
    db = Session()
    db.add(Device(
        device_id="334455667788",
        name="BadRadio",
        ip_address="192.168.50.176",
        model="SoundTouch Portable",
        firmware="27.0.6.46330.5043500 epdbuild.test",
        info_xml='<info deviceID="334455667788"><type>SoundTouch Portable</type><moduleType>scm</moduleType><variant>taigan</variant></info>',
        identity_verified=True,
    ))
    db.commit()
    target = ServerTarget("192.0.2.77", 1328, 1516, 1860)
    started = coordinator.start(
        db,
        device_ids=["334455667788"],
        target=target,
        dry_run=True,
    )
    db.close()

    result = asyncio.run(coordinator.execute(started["job_id"], dry_run=True))
    assert result["status"] == "completed"
    assert result["progress"] == 100
    assert result["devices"][0]["state"] == SetupState.VERIFIED.value
    assert result["devices"][0]["backup_sha256"] == {}


def test_volume_safety_failure_is_persisted_as_audio_lock():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    coordinator = SetupCoordinator(session_factory=Session)
    db = Session()
    db.add(Device(
        device_id="334455667788",
        name="BadRadio",
        ip_address="192.168.50.176",
        model="SoundTouch Portable",
        firmware="27.0.6.46330.5043500 epdbuild.test",
        info_xml='<info deviceID="334455667788"><type>SoundTouch Portable</type><moduleType>scm</moduleType><variant>taigan</variant></info>',
        identity_verified=True,
    ))
    db.commit()
    target = ServerTarget("192.0.2.77", 1328, 1516, 1860)
    started = coordinator.start(
        db,
        device_ids=["334455667788"],
        target=target,
        dry_run=True,
    )
    row = coordinator.repository.state(db, started["job_id"], "334455667788")
    row.state = SetupState.PRESETS_READABLE.value
    row.evidence_json = "{}"
    db.commit()
    coordinator.repository.transition_state(db, row, SetupState.PLAYBACK_READY)
    row.last_error = "VOLUME_SAFETY_LOCK: radio exceeded volume 1 after select"
    evidence = coordinator._load_json(row.evidence_json)
    evidence.update(
        {
            "audio_test_locked": True,
            "audio_lock_volume_limit": 1,
        }
    )
    row.evidence_json = __import__("json").dumps(evidence)
    coordinator.repository.transition_state(db, row, SetupState.FAILED, error=row.last_error)
    db.commit()
    assert coordinator.repository.public_job(db, coordinator.repository.job(db, started["job_id"]))["devices"][0]["evidence"]["audio_test_locked"] is True
    db.close()
