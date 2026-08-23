import asyncio
from types import SimpleNamespace
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from basswiesn.app.db.database import Base
from basswiesn.app.models import Device, DeviceActionJournal, SetupRebuildDeviceState
from basswiesn.app.services.setup_rebuild.candidates import (
    candidate_from_device,
    setup_candidates,
)
from basswiesn.app.services.setup_rebuild.coordinator import SetupCoordinator
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
from basswiesn.app.services.setup_rebuild.states import SetupState
from basswiesn.app.services.setup_rebuild.audio_safety import (
    clear_audio_safety,
    load_audio_safety,
    lock_audio_safety,
)
from basswiesn.app.services.setup_rebuild.backup import read_json_artifact, write_json_artifact
from basswiesn.app.services.setup_rebuild.radio_adapter import _volume_state


pytestmark = pytest.mark.integration


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _device(
    device_id: str = "SETUP160A",
    *,
    ip_address: str = "192.0.2.61",
    firmware: str = "27.0.6.46330.5043500 epdbuild.test",
    model: str = "SoundTouch 20",
) -> Device:
    return Device(
        device_id=device_id,
        name="Wohnzimmer",
        ip_address=ip_address,
        model=model,
        firmware=firmware,
        info_xml=(
            f'<info deviceID="{device_id}"><name>Wohnzimmer</name><type>{model}</type>'
            f"<components><component><softwareVersion>{firmware}</softwareVersion>"
            "</component></components><moduleType>sm2</moduleType>"
            "<variant>spotty</variant></info>"
        ),
        identity_verified=True,
    )


def test_setup_candidates_come_from_database_and_exclude_protected_device(monkeypatch):
    from basswiesn.app import config

    monkeypatch.setenv("PROTECTED_DEVICE_IPS", "192.0.2.25")
    monkeypatch.setenv("PROTECTED_DEVICE_IDS", "001122334455")
    config.get_settings.cache_clear()
    Session = _session()
    db = Session()
    db.add_all(
        [
            _device(),
            _device("001122334455", ip_address="192.0.2.25"),
        ]
    )
    db.commit()

    candidates = setup_candidates(db)

    assert [item.device_id for item in candidates] == ["SETUP160A"]
    assert candidates[0].eligible is True
    assert candidates[0].public_dict()["ssh_required"] is False
    assert candidates[0].public_dict()["product_id_provenance"] == "PROFILE_DERIVED"
    db.close()
    config.get_settings.cache_clear()


def test_historical_audio_lock_is_inherited_until_explicit_verification():
    Session = _session()
    db = Session()
    db.add(_device())
    db.add(
        SetupRebuildDeviceState(
            job_id="old-volume-excursion",
            device_id="SETUP160A",
            evidence_json='{"audio_test_locked": true, "audio_lock_reason": "volume exceeded 1"}',
        )
    )
    db.commit()

    inherited = load_audio_safety(db, "SETUP160A")
    candidate = setup_candidates(db)[0]
    assert inherited.locked is True
    assert inherited.source == "legacy_setup_evidence"
    assert candidate.audio_safety_locked is True
    assert "volume exceeded" in candidate.audio_safety_reason

    cleared = clear_audio_safety(db, "SETUP160A", "identity and volume-1 readback confirmed")
    assert cleared.locked is False
    assert load_audio_safety(db, "SETUP160A").locked is False
    assert setup_candidates(db)[0].audio_safety_locked is False
    db.close()


def test_explicit_audio_lock_is_persistent_and_fail_closed():
    Session = _session()
    db = Session()
    db.add(_device())
    db.commit()

    lock_audio_safety(db, "SETUP160A", "post-select volume excursion")

    state = load_audio_safety(db, "SETUP160A")
    assert state.locked is True
    assert state.volume_limit == 1
    assert state.source == "setup_playback_guard"
    db.close()


def test_setup_json_artifact_is_valid_and_legacy_literal_newline_is_readable(tmp_path):
    artifact = write_json_artifact(tmp_path, "route-before.json", {"margeServerUrl": "http://192.0.2.1:1516"})
    path = __import__("pathlib").Path(artifact.path)
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert not path.read_bytes().endswith(b"\\n")
    assert read_json_artifact(path)["margeServerUrl"].endswith(":1516")

    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"value": 1}\\n', encoding="utf-8")
    assert read_json_artifact(legacy) == {"value": 1}


def test_setup_audio_safety_parses_real_volume_and_mute_readback():
    assert _volume_state(
        '<volume deviceID="X"><targetvolume>53</targetvolume><actualvolume>53</actualvolume><muteenabled>false</muteenabled></volume>'
    ) == (53, False)
    assert _volume_state(
        '<volume><targetvolume>1</targetvolume><actualvolume>1</actualvolume><muteenabled>true</muteenabled></volume>'
    ) == (1, True)


def test_unknown_firmware_profile_is_visible_but_fail_closed():
    row = _device(firmware="99.1.0")

    candidate = candidate_from_device(row)

    assert candidate is not None
    assert candidate.eligible is False
    assert "firmware" in candidate.blocking_reason.lower()


def test_setup_profile_rejects_conflicting_radio_product_id_and_reports_provenance():
    row = _device()
    row.reachable = True
    row.info_xml = row.info_xml.replace(
        "<moduleType>",
        "<productID>0xDEAD</productID><moduleType>",
    )
    conflict = candidate_from_device(row)
    assert conflict is not None
    assert conflict.eligible is False
    assert "product ID" in conflict.blocking_reason

    row.info_xml = row.info_xml.replace("0xDEAD", "0x093B")
    confirmed = candidate_from_device(row)
    assert confirmed is not None
    assert confirmed.eligible is True
    assert confirmed.product_id == "0X093B"
    assert confirmed.product_id_provenance == "RADIO_INFO"


def test_server_targets_keep_multiple_lan_addresses_and_drop_virtual_interfaces(monkeypatch):
    from basswiesn.app.services.setup_rebuild import server_target as target_module

    monkeypatch.setattr(
        target_module,
        "get_settings",
        lambda: SimpleNamespace(
            lan_host="172.17.0.1",
            lan_host_configured=True,
            lan_host_candidates=(),
            test_mode=False,
        ),
    )
    monkeypatch.setattr(
        target_module,
        "_interface_addresses",
        lambda: [
            ("lo", "127.0.0.1"),
            ("docker0", "172.17.0.1"),
            ("veth1234", "10.23.4.5"),
            ("wlan0", "192.168.40.12"),
            ("enp3s0", "10.20.30.40"),
            ("eth9", "169.254.10.20"),
        ],
    )

    candidates = target_module.server_target_candidates()

    assert [item.host for item in candidates] == ["10.20.30.40", "192.168.40.12"]
    assert all(item.interface not in {"lo", "docker0", "veth1234"} for item in candidates)


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    async def _result(self, name, value):
        self.calls.append(name)
        return value

    async def identify(self, row):
        return await self._result("identify", {"verified": True})

    async def backup(self, row):
        return await self._result("backup", {"verified": True, "backup_path": "/tmp/test", "sha256": {}})

    async def ssh_status(self, row):
        return await self._result("ssh_status", {"already_active": False})

    async def activate_ssh(self, row):
        return await self._result("activate_ssh", {})

    async def persist_ssh(self, row):
        return await self._result("persist_ssh", {})

    async def reboot_verify_ssh(self, row):
        return await self._result("reboot_verify_ssh", {"verified": True})

    async def backup_routing(self, row):
        return await self._result("backup_routing", {"routing_backup": True})

    async def route(self, row, target):
        return await self._result("route", {"routing_status": "active"})

    async def reboot(self, row):
        return await self._result("reboot", {"reboot_requested": True})

    async def reconnect(self, row):
        return await self._result("reconnect", {"reachable": True})

    async def pair_account(self, row, target):
        return await self._result("pair_account", {"account_paired": True})

    async def read_presets(self, row):
        return await self._result("read_presets", {"presets_readable": True})

    async def playback_test(self, row, target):
        return await self._result("playback_test", {"playback_ready": True, "volume": 1})

    async def rollback(self, row):
        return await self._result(
            "rollback",
            {"rolled_back": True, "fully_restored": True},
        )


class BlockingRouteAdapter(RecordingAdapter):
    def __init__(self, route_started: asyncio.Event, route_release: asyncio.Event):
        super().__init__()
        self.route_started = route_started
        self.route_release = route_release

    async def route(self, row, target):
        del row, target
        self.calls.append("route")
        self.route_started.set()
        await self.route_release.wait()
        return {"routing_status": "active"}


class LimitedRollbackAdapter(RecordingAdapter):
    async def rollback(self, row):
        return await self._result(
            "rollback",
            {
                "rolled_back": True,
                "fully_restored": False,
                "rollback_scope": "ROUTING_RUNTIME_READBACK",
                "persistence_validation": "OPEN_AFTER_LATER_REBOOT",
            },
        )


def test_normal_coordinator_path_does_not_call_ssh():
    Session = _session()
    db = Session()
    db.add(_device())
    db.commit()
    adapter = RecordingAdapter()
    coordinator = SetupCoordinator(adapter=adapter, session_factory=Session)
    started = coordinator.start(
        db,
        device_ids=["SETUP160A"],
        target=ServerTarget("192.0.2.10", 1328, 1516, 1860),
        options={"ssh_required": False, "pair_account": True, "playback_test": True},
    )
    db.close()

    result = asyncio.run(coordinator.execute(started["job_id"]))

    assert result["status"] == "completed"
    assert result["devices"][0]["ssh_status"] == "NOT_REQUIRED"
    assert not ({"ssh_status", "activate_ssh", "persist_ssh", "reboot_verify_ssh"} & set(adapter.calls))
    assert adapter.calls == [
        "identify",
        "backup",
        "backup_routing",
        "route",
        "reboot",
        "reconnect",
        "pair_account",
        "read_presets",
        "playback_test",
    ]
    audit_db = Session()
    ledger = audit_db.query(DeviceActionJournal).filter(
        DeviceActionJournal.job_id == started["job_id"],
        DeviceActionJournal.action == "setup_rebuild",
    ).one()
    assert ledger.verified is True
    assert ledger.backup_ref == "/tmp/test"
    assert ledger.rollback_ref.endswith(":routing-rollback")
    assert '"playback_test":"passed"' in ledger.readback
    audit_db.close()


class FailSecondDeviceAdapter(RecordingAdapter):
    async def route(self, row, target):
        if row.device_id == "SETUP160B":
            self.calls.append("route:SETUP160B")
            raise RuntimeError("simulated per-radio routing failure")
        self.calls.append(f"route:{row.device_id}")
        return {"routing_status": "active"}


def test_multi_device_setup_keeps_verified_radios_after_one_failure():
    Session = _session()
    db = Session()
    db.add_all(
        [
            _device("SETUP160A", ip_address="192.0.2.61"),
            _device("SETUP160B", ip_address="192.0.2.62"),
            _device("SETUP160C", ip_address="192.0.2.63"),
        ]
    )
    db.commit()
    coordinator = SetupCoordinator(adapter=FailSecondDeviceAdapter(), session_factory=Session)
    started = coordinator.start(
        db,
        device_ids=["SETUP160A", "SETUP160B", "SETUP160C"],
        target=ServerTarget("192.0.2.10", 1328, 1516, 1860),
        options={"ssh_required": False, "pair_account": False, "playback_test": False},
    )
    db.close()

    result = asyncio.run(coordinator.execute(started["job_id"]))

    assert result["status"] == "partial_failure"
    assert result["summary"] == {
        "total": 3,
        "verified": 2,
        "failed": 1,
        "pending": 0,
        "verified_device_ids": ["SETUP160A", "SETUP160C"],
        "failed_device_ids": ["SETUP160B"],
    }
    assert [item["state"] for item in result["devices"]] == [
        SetupState.VERIFIED.value,
        SetupState.FAILED.value,
        SetupState.VERIFIED.value,
    ]


def test_routing_only_rollback_is_reported_as_limited_not_full_success():
    Session = _session()
    db = Session()
    db.add(_device())
    db.commit()
    adapter = LimitedRollbackAdapter()
    coordinator = SetupCoordinator(adapter=adapter, session_factory=Session)
    started = coordinator.start(
        db,
        device_ids=["SETUP160A"],
        target=ServerTarget("192.0.2.10", 1328, 1516, 1860),
        options={"ssh_required": False, "pair_account": True, "playback_test": False},
    )
    db.close()

    completed = asyncio.run(coordinator.execute(started["job_id"]))
    result = asyncio.run(coordinator.rollback(started["job_id"]))

    assert completed["status"] == "completed"
    assert result["status"] == "rollback_limited"
    assert result["current_state"] == "ROLLED_BACK"
    assert result["devices"][0]["recovery_status"] == (
        "ROUTING_ROLLBACK_VERIFIED_PERSISTENCE_OPEN"
    )
    assert result["devices"][0]["evidence"]["rollback"][
        "persistence_validation"
    ] == "OPEN_AFTER_LATER_REBOOT"


def test_cancel_during_atomic_route_stops_before_every_following_transport(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'setup-cancel.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(_device())
    db.commit()

    async def run_cancel_scenario():
        route_started = asyncio.Event()
        route_release = asyncio.Event()
        adapter = BlockingRouteAdapter(route_started, route_release)
        coordinator = SetupCoordinator(adapter=adapter, session_factory=Session)
        started = coordinator.start(
            db,
            device_ids=["SETUP160A"],
            target=ServerTarget("192.0.2.10", 1328, 1516, 1860),
            options={"ssh_required": False, "pair_account": True, "playback_test": True},
        )
        db.close()

        execution = asyncio.create_task(coordinator.execute(started["job_id"]))
        await asyncio.wait_for(route_started.wait(), timeout=2)
        cancel_db = Session()
        cancel_job = coordinator.repository.job(cancel_db, started["job_id"])
        assert cancel_job is not None
        cancel_job.cancel_requested = True
        cancel_db.commit()
        cancel_db.close()
        route_release.set()
        return await asyncio.wait_for(execution, timeout=2), adapter.calls

    result, calls = asyncio.run(run_cancel_scenario())

    assert result["status"] == "cancelled"
    assert 45 <= result["progress"] < 100
    assert result["current_state"] == SetupState.FAILED.value
    assert result["devices"][0]["state"] == SetupState.FAILED.value
    assert result["devices"][0]["recovery_status"] == "CANCELLED_AT_CHECKPOINT"
    assert "kein weiterer Gerätebefehl" in result["error"]
    assert calls == ["identify", "backup", "backup_routing", "route"]


@pytest.mark.parametrize(
    ("cancel_requested", "expected_status", "expected_recovery"),
    [
        (False, "failed", "INTERRUPTED_REVIEW_REQUIRED"),
        (True, "cancelled", "INTERRUPTED_CANCELLED"),
    ],
)
def test_restart_marks_interrupted_setup_for_review_without_replaying_transport(
    cancel_requested, expected_status, expected_recovery
):
    Session = _session()
    db = Session()
    db.add(_device())
    db.commit()
    original = SetupCoordinator(adapter=RecordingAdapter(), session_factory=Session)
    started = original.start(
        db,
        device_ids=["SETUP160A"],
        target=ServerTarget("192.0.2.10", 1328, 1516, 1860),
        options={"ssh_required": False, "pair_account": True},
    )
    row = original.repository.state(db, started["job_id"], "SETUP160A")
    job = original.repository.job(db, started["job_id"])
    row.state = SetupState.BASSWIESN_ROUTE_ACTIVE.value
    row.backup_path = "/tmp/preserved-setup-backup"
    job.current_state = row.state
    job.cancel_requested = cancel_requested
    db.commit()
    db.close()

    adapter = RecordingAdapter()
    restarted = SetupCoordinator(adapter=adapter, session_factory=Session)
    asyncio.run(restarted.resume_pending_jobs())

    check = Session()
    recovered_job = restarted.repository.job(check, started["job_id"])
    recovered = restarted.repository.state(check, started["job_id"], "SETUP160A")
    assert adapter.calls == []
    assert recovered_job.status == expected_status
    assert recovered_job.current_state == SetupState.FAILED.value
    assert "kein Schritt automatisch" in recovered_job.error
    assert recovered.state == SetupState.FAILED.value
    assert recovered.recovery_status == expected_recovery
    assert recovered.backup_path == "/tmp/preserved-setup-backup"
    check.close()


def test_simulation_device_is_impossible_outside_test_mode(monkeypatch):
    row = _device("BASSWIESN-SIM-160", ip_address="192.0.2.160")
    monkeypatch.setattr(
        "basswiesn.app.services.setup_rebuild.candidates.get_settings",
        lambda: SimpleNamespace(test_mode=False),
    )
    assert candidate_from_device(row) is None


def test_test_mode_simulation_can_complete_and_rollback_without_transport(monkeypatch):
    Session = _session()
    db = Session()
    db.add(_device("BASSWIESN-SIM-160", ip_address="192.0.2.160"))
    db.commit()
    monkeypatch.setattr(
        "basswiesn.app.services.setup_rebuild.candidates.get_settings",
        lambda: SimpleNamespace(test_mode=True),
    )
    coordinator = SetupCoordinator(session_factory=Session)
    started = coordinator.start(
        db,
        device_ids=["BASSWIESN-SIM-160"],
        target=ServerTarget("192.0.2.10", 1328, 1516, 1860),
        dry_run=True,
        options={"simulation": True, "ssh_required": False},
    )
    db.close()

    completed = asyncio.run(coordinator.execute(started["job_id"], dry_run=True))
    rolled_back = asyncio.run(coordinator.rollback(started["job_id"]))

    assert completed["status"] == "completed"
    assert completed["devices"][0]["backup_path"].startswith("simulation://")
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["current_state"] == "ROLLED_BACK"
    assert rolled_back["devices"][0]["state"] == "ROLLED_BACK"


def test_test_mode_http_flow_uses_real_router_coordinator_and_database(monkeypatch):
    from basswiesn.app import config
    from basswiesn.app.main import create_web_app
    from basswiesn.app.services.setup_rebuild import coordinator as coordinator_module

    monkeypatch.setenv("BASSWIESN_TEST_MODE", "true")
    config.get_settings.cache_clear()
    coordinator_module._COORDINATOR = None
    try:
        with TestClient(create_web_app(background_tasks=False)) as client:
            latest = client.get("/api/setup/rebuild/jobs/latest")
            assert latest.status_code == 200
            assert {"job_id", "status"} <= latest.json().keys()
            devices = client.get("/api/setup/rebuild/devices").json()
            simulated = next(item for item in devices if item["simulated"])
            targets = client.get("/api/setup/rebuild/server-targets").json()["candidates"]
            simulation_target = next(item for item in targets if item["source"] == "Testmodus")
            payload = {
                "device_ids": [simulated["device_id"]],
                "server_host": simulation_target["host"],
                "playback_test": True,
            }

            preview = client.post("/api/setup/rebuild/preview", json=payload)
            assert preview.status_code == 200
            assert preview.json()["ready_for_start"] is True
            assert preview.json()["ssh_required"] is False

            started = client.post("/api/setup/rebuild/start", json=payload)
            assert started.status_code == 200
            job_id = started.json()["job_id"]
            deadline = time.monotonic() + 5
            current = started.json()
            while current["status"] in {"pending", "running"} and time.monotonic() < deadline:
                time.sleep(0.1)
                current = client.get(f"/api/setup/rebuild/jobs/{job_id}").json()
            assert current["status"] == "completed"
            assert current["devices"][0]["evidence"]["playback_test"] == "passed"

            rollback = client.post(f"/api/setup/rebuild/jobs/{job_id}/rollback", json={})
            assert rollback.status_code == 200
            assert rollback.json()["devices"][0]["state"] == "ROLLED_BACK"
    finally:
        coordinator_module._COORDINATOR = None
        config.get_settings.cache_clear()


def test_explicit_setup_discovery_verifies_only_devices_from_current_ssdp_result(monkeypatch):
    from basswiesn.app import db as app_db
    from basswiesn.app.main import create_web_app
    from basswiesn.app.routers import setup_rebuild as setup_rebuild_router

    calls: list[tuple[str, str]] = []

    async def discovery(db, *, timeout_seconds):
        assert timeout_seconds == 3
        fresh = _device("FRESH-DISCOVERY", ip_address="192.0.2.71")
        fresh.identity_verified = False
        fresh.info_xml = ""
        fresh.firmware = ""
        stale = _device("STALE-STORED", ip_address="192.0.2.72")
        db.add_all([fresh, stale])
        db.flush()
        return {
            "devices": [{"device_id": "FRESH-DISCOVERY", "ip_address": "192.0.2.71"}],
            "errors": [],
        }

    class Client:
        def __init__(self, ip_address: str, device_id: str):
            self.ip_address = ip_address
            self.device_id = device_id

        async def get_xml(self, path: str) -> str:
            calls.append((self.device_id, path))
            return (
                '<info deviceID="FRESH-DISCOVERY"><name>Neu verbunden</name>'
                '<type>SoundTouch 20</type><components><component>'
                '<softwareVersion>27.0.6.46330.5043500 epdbuild.test</softwareVersion>'
                '</component></components><moduleType>sm2</moduleType><variant>spotty</variant></info>'
            )

    monkeypatch.setattr(setup_rebuild_router, "manual_discovery_test", discovery)
    monkeypatch.setattr(
        setup_rebuild_router,
        "_explicit_identity_client",
        lambda ip_address, device_id: Client(ip_address, device_id),
    )

    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post("/api/setup/rebuild/discover", json={"timeout_seconds": 3})

    assert response.status_code == 200
    payload = response.json()
    assert payload["network_configuration_changed"] is False
    assert payload["found"] == 1
    assert payload["verified"] == 1
    assert payload["failed"] == 0
    assert calls == [("FRESH-DISCOVERY", "/info")]
    db = app_db.SessionLocal()
    try:
        fresh = db.query(Device).filter(Device.device_id == "FRESH-DISCOVERY").one()
        stale = db.query(Device).filter(Device.device_id == "STALE-STORED").one()
        assert fresh.identity_verified is True
        assert fresh.discovery_method == "setup_ssdp_info"
        assert stale.discovery_method != "setup_ssdp_info"
    finally:
        db.close()


def test_explicit_setup_discovery_rejects_identity_mismatch_without_success(monkeypatch):
    from basswiesn.app import db as app_db
    from basswiesn.app.main import create_web_app
    from basswiesn.app.routers import setup_rebuild as setup_rebuild_router

    async def discovery(db, *, timeout_seconds):
        del timeout_seconds
        row = _device("EXPECTED-ID", ip_address="192.0.2.73")
        row.identity_verified = False
        db.add(row)
        db.flush()
        return {
            "devices": [{"device_id": "EXPECTED-ID", "ip_address": "192.0.2.73"}],
            "errors": [],
        }

    class Client:
        async def get_xml(self, _path: str) -> str:
            return '<info deviceID="DIFFERENT-ID"><name>Wrong</name><type>SoundTouch 20</type></info>'

    monkeypatch.setattr(setup_rebuild_router, "manual_discovery_test", discovery)
    monkeypatch.setattr(setup_rebuild_router, "_explicit_identity_client", lambda *_args: Client())

    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post("/api/setup/rebuild/discover", json={})

    assert response.status_code == 200
    assert response.json()["verified"] == 0
    assert response.json()["failed"] == 1
    assert "stimmt nicht" in response.json()["failures"][0]["reason"]
    db = app_db.SessionLocal()
    try:
        row = db.query(Device).filter(Device.device_id == "EXPECTED-ID").one()
        assert row.identity_verified is False
        assert row.reachable is False
        assert candidate_from_device(row, db).eligible is False
    finally:
        db.close()
