"""Real auth + real guarded service + simulated radio; no hardware exemption."""
import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.core.api_tokens import ApiTokenStore
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, DeviceActionJournal, Setting
from basswiesn.app.routers import api
from test_admin_boundary import secured, authenticate  # noqa: F401
from test_settings_write_safety import Radio, DEVICE_ID, BUILD, info
from test_preset_apply_safety import rig as preset_rig, ID as PRESET_ID  # noqa: F401
from test_zone_creation_safety import rig as zone_rig, MASTER, MEMBER  # noqa: F401
from test_backup_integrity import backup_dir  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def workflow(secured, monkeypatch):
    radio = Radio()
    with app_db.SessionLocal() as db:
        db.add(Device(device_id=DEVICE_ID, ip_address="192.0.2.42", name="Before",
                      model="SoundTouch 30", firmware=BUILD, info_xml=info()))
        db.commit()
    monkeypatch.setattr(api, "SoundTouchClient", radio.factory)
    with TestClient(create_web_app(background_tasks=False), base_url="https://testserver") as client:
        yield radio, client, secured[1]


def credentials(client, store, kind):
    if kind == "session":
        csrf = authenticate(client)
        return {"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf}
    issued = ApiTokenStore(store).issue("offline workflow", ["playback"], 3600)
    return {"Authorization": f"Bearer {issued.token}"}


@pytest.mark.parametrize("kind", ["session", "token"])
@pytest.mark.parametrize("outcome", ["verified", "lost_reply", "fail_readback", "switch_identity", "ignore_write"])
def test_authenticated_volume_keeps_the_complete_write_readback_contract(workflow, kind, outcome):
    radio, client, store = workflow
    headers = credentials(client, store, kind)
    if outcome != "verified":
        setattr(radio, outcome, True)
    response = client.post(f"/api/devices/{DEVICE_ID}/settings/volume",
                           json={"value": 1, "dry_run": False}, headers=headers)
    assert radio.calls.count(("POST", "/volume")) == 1
    assert radio.calls[0] == ("GET", "/info")
    success = outcome in {"verified", "lost_reply"}
    assert response.status_code == (200 if success else 409), response.text
    if success:
        # The compatible API returns the actual verified volume, not a boolean.
        assert response.json()["verified"] == 1
        assert type(response.json()["verified"]) is int
    else:
        assert response.json()["detail"]["status"] == "DIVERGED"
    with app_db.SessionLocal() as db:
        expected = "settings_verified" if success else "settings_diverged"
        assert db.query(DeviceActionJournal).filter_by(phase=expected, verified=success).count() == 1


@pytest.mark.parametrize("kind", ["session", "token"])
@pytest.mark.parametrize("failure", ["protected", "changed_previous", "unknown_firmware"])
def test_authentication_never_overrides_device_preconditions(workflow, kind, failure):
    radio, client, store = workflow
    headers = credentials(client, store, kind)
    payload = {"value": 1, "dry_run": False}
    if failure == "protected":
        with app_db.SessionLocal() as db:
            db.add(Setting(key="protected_device_ids", value=DEVICE_ID))
            db.commit()
    elif failure == "changed_previous":
        payload["expected_previous"] = 99
    else:
        radio.build = "27.0.6.UNRESEARCHED"
    response = client.post(f"/api/devices/{DEVICE_ID}/settings/volume", json=payload, headers=headers)
    assert response.status_code in {400, 403, 409}, response.text
    assert not any(method == "POST" for method, _ in radio.calls)
    if failure == "protected":
        assert radio.constructors == []


@pytest.mark.parametrize("path", [
    "/api/devices/EXAMPLE/key", "/api/presets/EXAMPLE/1", "/api/multiroom/set",
    "/api/update/admin/install", "/api/devices/EXAMPLE/rename",
    "/api/devices/EXAMPLE/settings/volume", "/api/stations", "/api/settings",
    "/api/devices/EXAMPLE/power/standby", "/api/devices/EXAMPLE/wireless-survey",
    "/api/devices/EXAMPLE/station/add-native", "/api/devices/EXAMPLE/wireless-profiles",
])
def test_missing_or_expired_session_stops_every_feature_before_handler(workflow, path):
    radio, client, _ = workflow
    assert client.post(path, json={}, headers={"Origin": "https://testserver"}).status_code == 401
    headers = credentials(client, None, "session")
    assert client.post(path, json={}, headers={"Origin": "https://testserver"}).status_code == 403
    assert client.post("/auth/logout", json={}, headers=headers).status_code == 200
    assert client.post(path, json={}, headers=headers).status_code == 401
    assert radio.constructors == []


@pytest.mark.parametrize("path", ["/api/devices/EXAMPLE/power/standby", "/api/devices/EXAMPLE/wireless-survey",
    "/api/devices/EXAMPLE/station/add-native", "/api/devices/EXAMPLE/wireless-profiles"])
def test_unreviewed_lab_commands_cannot_be_granted_by_admin_token(workflow, path):
    radio, client, store = workflow
    issued = ApiTokenStore(store).issue("offline boundary", ["admin", "read", "playback"], 3600)
    response = client.post(path, json={"dry_run": False, "confirmation": "YES"},
                           headers={"Authorization": f"Bearer {issued.token}"})
    assert response.status_code == 403
    assert radio.constructors == []


@pytest.mark.parametrize("outcome", ["verified", "lost_reply", "ignore_write"])
def test_authenticated_preset_transaction_commits_only_actual_readback(secured, preset_rig, outcome):
    from basswiesn.app.models import Preset, PresetMutation

    radio = preset_rig
    before = radio.xml
    if outcome == "lost_reply":
        radio.lost = True
    elif outcome == "ignore_write":
        radio.on_write = lambda: setattr(radio, "xml", before)
    with TestClient(create_web_app(background_tasks=False), base_url="https://testserver") as client:
        headers = credentials(client, None, "session")
        response = client.post(f"/api/presets/{PRESET_ID}/1", headers=headers,
            json={"station_id": radio.station.id, "dry_run": False, "memory_checked": True})
    success = outcome != "ignore_write"
    assert len(radio.posts) == 1
    assert response.status_code == (200 if success else 502), response.text
    if success:
        assert response.json()["verified"] is True
    with app_db.SessionLocal() as db:
        row = db.query(Preset).filter_by(device_id=PRESET_ID, button=1).one()
        assert row.location.endswith("/new" if success else "/live")
        mutation = db.query(PresetMutation).filter_by(device_id=PRESET_ID).one()
        assert mutation.state == ("LOCAL_COMMIT" if success else "RECONCILE")


@pytest.mark.parametrize("outcome", ["verified", "lost_reply", "ignore_write"])
def test_authenticated_zone_transaction_keeps_distributed_verification(secured, zone_rig, outcome):
    radio = zone_rig
    if outcome == "lost_reply":
        radio.lost = "/setZone"
    elif outcome == "ignore_write":
        radio.ignore = "/setZone"
    with TestClient(create_web_app(background_tasks=False), base_url="https://testserver") as client:
        response = client.post("/api/multiroom/set", headers=credentials(client, None, "session"),
            json={"master_device_id": MASTER, "member_device_ids": [MEMBER],
                  "memory_checked": True, "dry_run": False, "preserve_volumes": True})
    success = outcome != "ignore_write"
    assert response.status_code == (200 if success else 502), response.text
    assert sum(path == "/setZone" for _, path, _ in radio.posts) == 1
    assert radio.backups_at_write and all(radio.backups_at_write)
    assert set(radio.volumes.values()) == {1}
    if success:
        assert all(item["ok"] for item in response.json()["verification"])
    else:
        assert response.json()["detail"]["status"] == "DIVERGED"
        assert response.json()["detail"]["automatic_retry"] is False


@pytest.mark.parametrize("domain", ["presets", "zones"])
def test_full_admin_token_still_cannot_inherit_unreviewed_writes(secured, preset_rig, zone_rig, domain):
    issued = ApiTokenStore(secured[1]).issue("offline scope boundary", ["admin", "read", "playback"], 3600)
    with TestClient(create_web_app(background_tasks=False), base_url="https://testserver") as client:
        path = f"/api/presets/{PRESET_ID}/1" if domain == "presets" else "/api/multiroom/set"
        response = client.post(path, json={"dry_run": False},
                               headers={"Authorization": f"Bearer {issued.token}"})
    assert response.status_code == 403
    assert not preset_rig.posts and not zone_rig.posts
    assert preset_rig.constructed == 0 and not zone_rig.constructors


def test_authenticated_restore_requires_csrf_confirmation_and_verified_backup(secured, backup_dir):
    from basswiesn.app.services import backup_restore

    with TestClient(create_web_app(background_tasks=False), base_url="https://testserver") as client:
        headers = credentials(client, None, "session")
        created = client.post("/api/backup/create", json={}, headers=headers)
        assert created.status_code == 200, created.text
        path = created.json()["path"]
        assert backup_restore.preview_system_backup(path)["ok"]
        payload = {"path": path, "confirmation": "YES"}
        assert client.post("/api/backup/restore", json=payload).status_code == 403
        assert client.post("/api/backup/restore", json={"path": path}, headers=headers).status_code == 409
        assert not (backup_dir / "restore-pending.zip").exists()
        response = client.post("/api/backup/restore", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["prepared"] is True
        again = client.post("/api/backup/restore", json=payload, headers=headers)
        assert again.json()["prepared"] is False
        assert again.json()["code"] == "RESTORE_ALREADY_PENDING"
