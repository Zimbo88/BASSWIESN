from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import PlayHistory, RuntimeState, ScheduledAction, Setting, Station
from basswiesn.app.services import alarm_engine
from basswiesn.app.services.alarm_engine import run_alarm_engine_once
from basswiesn.app.models import Device, DeviceActionJournal
from basswiesn.app.routers import api, stations_presets
from basswiesn.app.routers.multiroom import split_csv, zone_payload


def test_zone_payload_excludes_master_from_members():
    master = Device(device_id="MASTER01", ip_address="192.0.2.10")
    members = [
        Device(device_id="MASTER01", ip_address="192.0.2.10"),
        Device(device_id="MEMBER01", ip_address="192.0.2.11"),
    ]

    xml = zone_payload(master, members)

    assert xml == '<zone master="MASTER01"><member ipaddress="192.0.2.11">MEMBER01</member></zone>'


def test_split_csv_trims_empty_items():
    assert split_csv(" A, ,B ,, C ") == ["A", "B", "C"]


def test_multiroom_human_ui_targets_one_group_and_explains_firmware_volume_changes():
    with TestClient(create_web_app()) as client:
        html = client.get("/").text
    script = Path("basswiesn/app/static/app.js").read_text(encoding="utf-8")

    assert "Bestehende Lautstärken beibehalten" in html
    assert "Ausgewählte Gruppe auflösen" in html
    assert 'postJson("/api/multiroom/clear",' in script
    assert "Bose-Firmware änderte trotz ausbleibendem SetVolume" in script
    assert "BASSWIESN hat nicht automatisch zurückkorrigiert" in script
    assert 'confirmation, trigger: "webui"' in script


def _set_test_timezone(db, value: str = "UTC") -> None:
    row = db.query(Setting).filter(Setting.key == "default_timezone").one_or_none()
    if row is None:
        row = Setting(key="default_timezone")
        db.add(row)
    row.value = value


def test_multiroom_routes_are_dry_run_guarded_and_preview_scenarios():
    suffix = uuid4().hex[:8]
    master = f"MRMASTER{suffix}"
    member = f"MRMEMBER{suffix}"
    station_name = f"Multiroom Station {suffix}"

    with TestClient(create_web_app()) as client:
        for device_id, name, ip in [
            (master, "Master", "192.0.2.21"),
            (member, "Member", "192.0.2.22"),
        ]:
            response = client.post("/api/devices", json={"device_id": device_id, "name": name, "ip_address": ip, "model": "SoundTouch Test"})
            assert response.status_code == 200

        station = client.post("/api/stations", json={"name": station_name, "stream_url": "http://example.test/live.mp3"}).json()

        preview = client.post("/api/multiroom/preview", json={"master_device_id": master, "member_device_ids": [member]}).json()
        assert preview["master"] == master
        assert preview["members"] == [member]
        assert f'<zone master="{master}">' in preview["xml"]
        assert f'<member ipaddress="192.0.2.22">{member}</member>' in preview["xml"]

        dry_run = client.post("/api/multiroom/set", json={"master_device_id": master, "member_device_ids": [member], "dry_run": True}).json()
        assert dry_run["dry_run"] is True
        assert dry_run["memory_check"]["required_before_write"] is True

        blocked_write = client.post("/api/multiroom/set", json={"master_device_id": master, "member_device_ids": [member], "dry_run": False})
        assert blocked_write.status_code == 409
        assert blocked_write.json()["detail"]["error"] == "memory check required before radio write"

        cleared = client.post("/api/multiroom/clear", json={"master_device_id": master, "dry_run": True}).json()
        assert cleared["xml"] == f'<zone master="{master}" />'

        scenario = client.post(
            "/api/multiroom/scenarios",
            json={
                "name": f"Scenario {suffix}",
                "master_device_id": master,
                "member_device_ids": [member],
                "station_id": station["id"],
                "volume": 24,
                "trigger_device_id": master,
                "trigger_button": 1,
            },
        ).json()
        scenario_preview = client.post(f"/api/multiroom/scenarios/{scenario['id']}/preview", json={}).json()
        assert scenario_preview["dry_run"] is True
        assert scenario["preset_type"] == "BASSWIESN_MULTIROOM_PRESET"
        assert scenario["stored_on_radio"] is False
        assert scenario["hardware_button_activation"] == "NOT_IMPLEMENTED"
        assert scenario_preview["preset_type"] == "BASSWIESN_MULTIROOM_PRESET"
        assert scenario_preview["activation_contract"] == "MANUAL_WEBUI"
        assert scenario_preview["trigger"]["active"] is False
        assert scenario_preview["station"] == station_name
        assert scenario_preview["stream_url"] == "http://example.test/live.mp3"
        assert scenario_preview["volume"] == 24
        assert scenario_preview["trigger"]["device_id"] == master
        assert scenario_preview["trigger"]["button"] == 1
        assert scenario_preview["trigger"]["active"] is False

        deleted = client.delete(f"/api/multiroom/scenarios/{scenario['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["preset_type"] == "BASSWIESN_MULTIROOM_PRESET"
        assert deleted.json()["deleted"] is True
        assert all(item["id"] != scenario["id"] for item in client.get("/api/multiroom/scenarios").json())


def test_multiroom_set_with_station_uses_internal_play_session(monkeypatch):
    calls = []

    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, path: str) -> str:
            calls.append(("get", self.ip_address, path))
            if path == "/getZone":
                if self.ip_address == "192.0.2.41":
                    return '<zone master="MRSETMASTER"><member ipaddress="192.0.2.42">MRSETMEMBER</member></zone>'
                return '<zone master="MRSETMASTER"><member ipaddress="192.0.2.42">MRSETMEMBER</member></zone>'
            if path == "/volume":
                return "<volume><actualvolume>5</actualvolume></volume>"
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append(("post", self.ip_address, path, body))
            return "<status>OK</status>"

    async def fake_play_station_on_device(device_id, station_id, payload, request, db=None):
        calls.append(("play", device_id, station_id, payload, request.__class__.__name__, db is None))
        return {"ok": True, "device_id": device_id, "station_id": station_id}

    monkeypatch.setattr("basswiesn.app.routers.multiroom.SoundTouchClient", Client)
    monkeypatch.setattr(stations_presets, "play_station_on_device", fake_play_station_on_device)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "MRSETMASTER", "name": "Master", "ip_address": "192.0.2.41", "model": "SoundTouch Test"})
        client.post("/api/devices", json={"device_id": "MRSETMEMBER", "name": "Member", "ip_address": "192.0.2.42", "model": "SoundTouch Test"})
        response = client.post(
            "/api/multiroom/set",
            json={
                "master_device_id": "MRSETMASTER",
                "member_device_ids": ["MRSETMEMBER"],
                "station_id": 123,
                "volume": 5,
                "dry_run": False,
                "memory_checked": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["playback"]["ok"] is True
    assert ("play", "MRSETMASTER", 123, {"dry_run": False}, "Session", True) in calls
    assert any(call[0] == "post" and call[2] == "/setZone" for call in calls)


def test_multiroom_preserve_volumes_documents_firmware_jump_without_set_volume(monkeypatch):
    zone_by_ip = {"192.0.2.43": '<zone />', "192.0.2.44": '<zone />'}
    volume_by_ip = {"192.0.2.43": 1, "192.0.2.44": 1}
    posts = []

    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, path: str) -> str:
            if path == "/getZone":
                return zone_by_ip[self.ip_address]
            if path == "/volume":
                return f"<volume><actualvolume>{volume_by_ip[self.ip_address]}</actualvolume></volume>"
            if path == "/rebroadcastlatencymode":
                return '<rebroadcastlatencymode mode="SYNC_TO_ZONE" />'
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            posts.append((self.ip_address, path, body))
            if path == "/setZone":
                zone_by_ip["192.0.2.43"] = '<zone master="MRPRESMASTER"><member ipaddress="192.0.2.44">MRPRESMEMBER</member></zone>'
                zone_by_ip["192.0.2.44"] = '<zone master="MRPRESMASTER" senderIPAddress="192.0.2.43" senderIsMaster="true"><member ipaddress="192.0.2.44">MRPRESMEMBER</member></zone>'
                volume_by_ip["192.0.2.44"] = 10
                return "<status>OK</status>"
            if path == "/volume":
                volume_by_ip[self.ip_address] = int(body.replace("<volume>", "").replace("</volume>", ""))
                return "<status>OK</status>"
            if path == "/rebroadcastlatencymode":
                return "<status>OK</status>"
            raise AssertionError(path)

    monkeypatch.setattr("basswiesn.app.routers.multiroom.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "MRPRESMASTER", "name": "Master", "ip_address": "192.0.2.43", "model": "SoundTouch Test"})
        client.post("/api/devices", json={"device_id": "MRPRESMEMBER", "name": "Member", "ip_address": "192.0.2.44", "model": "SoundTouch Test"})
        response = client.post(
            "/api/multiroom/set",
            json={
                "master_device_id": "MRPRESMASTER",
                "member_device_ids": ["MRPRESMEMBER"],
                "preserve_volumes": True,
                "dry_run": False,
                "memory_checked": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["preserve_volumes"] is True
    assert body["volume_warnings"] == [{"device_id": "MRPRESMEMBER", "before": 1, "after": 10}]
    assert body["volume_observations"] == [
        {"device_id": "MRPRESMASTER", "before": 1, "after": 1, "changed": False},
        {"device_id": "MRPRESMEMBER", "before": 1, "after": 10, "changed": True},
    ]
    assert body["volume_rollbacks"] == []
    assert body["automatic_volume_action"] == "NONE"
    assert all(item["ok"] for item in body["verification"])
    assert all(path != "/volume" for _ip, path, _body in posts)
    assert volume_by_ip["192.0.2.44"] == 10
    db = app_db.SessionLocal()
    ledger = (
        db.query(DeviceActionJournal)
        .filter(
            DeviceActionJournal.device_id == "MRPRESMASTER",
            DeviceActionJournal.action == "multiroom_set",
        )
        .order_by(DeviceActionJournal.id.desc())
        .first()
    )
    assert ledger is not None and ledger.verified is True
    assert ledger.backup_ref.startswith("inline:multiroom:")
    assert '"preserve_volumes":true' in ledger.requested_state
    assert '"after":10' in ledger.readback
    assert ledger.rollback_ref == ""
    db.close()


def test_schedule_roundtrip_keeps_multiroom_fields_as_lists():
    suffix = uuid4().hex[:8]
    master = f"SCHMASTER{suffix}"
    member = f"SCHMEMBER{suffix}"

    with TestClient(create_web_app()) as client:
        for device_id, ip in [(master, "192.0.2.31"), (member, "192.0.2.32")]:
            response = client.post("/api/devices", json={"device_id": device_id, "name": device_id, "ip_address": ip, "model": "SoundTouch Test"})
            assert response.status_code == 200

        created = client.post(
            "/api/schedules",
            json={
                "name": f"Schedule {suffix}",
                "start_time": "07:00",
                "end_time": "07:30",
                "days": "weekdays",
                "device_ids": [master],
                "multiroom_master_id": master,
                "multiroom_member_ids": [member],
                "volume": 18,
                "dry_run": True,
            },
        ).json()

        schedules = client.get("/api/schedules").json()
        row = next(item for item in schedules if item["id"] == created["id"])
        assert row["device_ids"] == [master]
        assert row["multiroom_master_id"] == master
        assert row["multiroom_member_ids"] == [member]


def test_schedule_roundtrip_keeps_flexible_weekdays_and_standby_default():
    suffix = uuid4().hex[:8]
    device_id = f"SCHDAYS{suffix}"
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": device_id, "name": "Weekday Radio", "ip_address": "192.0.2.33", "model": "SoundTouch Test"})
        created = client.post(
            "/api/schedules",
            json={"name": f"Weekday Timer {suffix}", "start_time": "09:00", "days": "tue,fri,sun", "device_ids": [device_id], "preset_button": 1, "volume": 25, "dry_run": True},
        ).json()
        schedules = client.get("/api/schedules").json()

    row = next(item for item in schedules if item["id"] == created["id"])
    assert row["days"] == "tue,fri,sun"
    assert row["volume"] == 25
    assert row["stop_action"] == "stop_standby"


def test_schedule_alarm_engine_trigger_now_and_persistent_marker():
    import asyncio
    from datetime import UTC, datetime

    suffix = uuid4().hex[:8]
    device_id = f"ALARM{suffix}"
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": device_id, "name": "Alarm Test", "ip_address": "127.0.0.1", "model": "SoundTouch Test"})
        station = client.post("/api/stations", json={"name": f"Alarm Station {suffix}", "stream_url": f"http://example.test/{suffix}.mp3"}).json()
        created = client.post(
            "/api/schedules",
            json={"name": f"Dry Alarm API {suffix}", "start_time": "07:31", "days": "daily", "device_ids": [device_id], "station_id": station["id"], "volume": 5, "dry_run": True},
        ).json()
        trigger = client.post(f"/api/schedules/{created['id']}/trigger", json={"dry_run": True})
        assert trigger.status_code == 200
        assert trigger.json()["dry_run"] is True

    db = app_db.SessionLocal()
    try:
        _set_test_timezone(db)
        row = ScheduledAction(name=f"Dry Alarm Engine {suffix}", enabled=1, start_time="07:30", days="daily", device_ids=device_id, station_id=station["id"], volume=5, dry_run=1)
        db.add(row)
        db.commit()
        db.refresh(row)
        results = asyncio.run(run_alarm_engine_once(db, datetime(2026, 6, 26, 7, 30, tzinfo=UTC)))
        assert any(item.get("schedule", {}).get("id") == row.id or item.get("schedule_id") == row.id for item in results)
        marker = db.query(RuntimeState).filter(RuntimeState.key == f"alarm:last_run:{row.id}").one_or_none()
        assert marker is not None
        assert "2026-06-26T07:30" in marker.value
        assert asyncio.run(run_alarm_engine_once(db, datetime(2026, 6, 26, 7, 30, 20, tzinfo=UTC))) == []
    finally:
        db.close()


def test_alarm_engine_starts_preset_button_and_stops_on_end_time(monkeypatch):
    import asyncio
    from datetime import UTC, datetime

    calls = []

    class Client:
        stopped = False

        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            if path == "/now_playing":
                if Client.stopped:
                    return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>STOP_STATE</playStatus></nowPlaying>'
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
            if path == "/volume":
                return "<volume><actualvolume>5</actualvolume></volume>"
            if path == "/presets":
                return "<presets/>"
            raise AssertionError(path)

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append((path, body))
            if path == "/key" and ">STOP<" in body:
                Client.stopped = True
            return "<status>OK</status>"

    monkeypatch.setattr(api, "SoundTouchClient", Client)
    suffix = uuid4().hex[:8]
    device_id = f"ALARMPRESET{suffix}"
    db = app_db.SessionLocal()
    try:
        _set_test_timezone(db)
        db.add(Device(device_id=device_id, ip_address="192.0.2.77", name="Alarm Preset", model="SoundTouch Test"))
        guard = db.query(Setting).filter(Setting.key == "ip_write_allowed_ips").one_or_none()
        if guard is None:
            guard = Setting(key="ip_write_allowed_ips")
            db.add(guard)
        guard.value = "192.0.2.77"
        row = ScheduledAction(name=f"Preset Alarm {suffix}", enabled=1, start_time="06:10", end_time="06:11", days="daily", device_ids=device_id, preset_button=2, volume=5, stop_action="stop", dry_run=0)
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id

        started = asyncio.run(run_alarm_engine_once(db, datetime(2026, 6, 26, 6, 10, tzinfo=UTC)))
        stopped = asyncio.run(run_alarm_engine_once(db, datetime(2026, 6, 26, 6, 11, tzinfo=UTC)))
    finally:
        db.close()

    started_row = next(item for item in started if item.get("schedule", {}).get("id") == row_id or item.get("schedule_id") == row_id)
    stopped_row = next(item for item in stopped if item.get("schedule_id") == row_id)
    assert started_row["ok"] is True
    assert stopped_row["ok"] is True
    key_bodies = [body for path, body in calls if path == "/key"]
    assert any("PRESET_2" in body for body in key_bodies)
    assert any(">STOP<" in body for body in key_bodies)


def test_alarm_engine_stop_standby_closes_timer_history(monkeypatch):
    import asyncio
    from datetime import UTC, datetime, timedelta

    calls = []

    class Client:
        stopped = False

        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            calls.append(("GET", path))
            if path == "/standby":
                return "<status>OK</status>"
            if path == "/now_playing":
                if Client.stopped:
                    return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>STOP_STATE</playStatus></nowPlaying>'
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
            if path == "/volume":
                return "<volume><actualvolume>5</actualvolume></volume>"
            return "<ok/>"

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append((path, body))
            if path == "/key" and ">STOP<" in body:
                Client.stopped = True
            return "<status>OK</status>"

    monkeypatch.setattr(api, "SoundTouchClient", Client)
    monkeypatch.setattr(alarm_engine, "SoundTouchClient", Client)
    suffix = uuid4().hex[:8]
    device_id = f"ALARMSTOP{suffix}"
    db = app_db.SessionLocal()
    try:
        _set_test_timezone(db)
        db.add(Device(device_id=device_id, ip_address="192.0.2.79", name="Alarm Stop", model="SoundTouch Test"))
        db.add(PlayHistory(device_id=device_id, trigger_type="timer", trigger="scheduler", started_at=datetime.now(UTC) - timedelta(minutes=10)))
        row = ScheduledAction(name=f"Stop Alarm {suffix}", enabled=1, start_time="06:10", end_time="06:11", days="once", device_ids=device_id, preset_button=2, volume=5, stop_action="stop_standby", dry_run=0)
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id

        stopped = asyncio.run(run_alarm_engine_once(db, datetime(2026, 6, 26, 6, 11, tzinfo=UTC)))
        history = db.query(PlayHistory).filter(PlayHistory.device_id == device_id).one()
        db.refresh(row)
    finally:
        db.close()

    stopped_row = next(item for item in stopped if item.get("schedule_id") == row_id)
    assert stopped_row["ok"] is True
    assert any(path == "/key" and ">STOP<" in body for path, body in calls)
    assert ("GET", "/standby") in calls
    assert history.ended_at is not None
    assert row.enabled == 0


def test_schedule_manual_trigger_executes_not_dry_run(monkeypatch):
    calls = []

    class Client:
        def __init__(self, _ip_address: str):
            pass

        async def get_xml(self, path: str) -> str:
            if path == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
            if path == "/volume":
                return "<volume><actualvolume>12</actualvolume></volume>"
            if path == "/presets":
                return "<presets/>"
            return "<ok/>"

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append((path, body))
            return "<status>OK</status>"

    monkeypatch.setattr(api, "SoundTouchClient", Client)
    suffix = uuid4().hex[:8]
    device_id = f"NOWTRIGGER{suffix}"
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": device_id, "name": "Now Trigger", "ip_address": "192.0.2.80", "model": "SoundTouch Test"})
        db = app_db.SessionLocal()
        try:
            guard = db.query(Setting).filter(Setting.key == "ip_write_allowed_ips").one_or_none()
            if guard is None:
                guard = Setting(key="ip_write_allowed_ips")
                db.add(guard)
            guard.value = "192.0.2.80"
            db.commit()
        finally:
            db.close()
        created = client.post("/api/schedules", json={"name": f"Now Timer {suffix}", "start_time": "08:15", "days": "daily", "device_ids": [device_id], "preset_button": 1, "volume": 12, "dry_run": False}).json()
        triggered = client.post(f"/api/schedules/{created['id']}/trigger", json={"dry_run": False})
        stats = client.get("/api/stats/playback").json()

    assert triggered.status_code == 200
    assert triggered.json()["dry_run"] is False
    assert any(path == "/key" and "PRESET_1" in body for path, body in calls)
    assert stats["timer"]["plays"] >= 1
    assert any(item["preset_button"] == 1 for item in stats["top_presets"])


def test_alarm_engine_station_timer_uses_requested_volume_25(monkeypatch):
    import asyncio

    calls = []

    async def fake_play_station_on_device(device_id, station_id, payload, request, db=None):
        calls.append((device_id, station_id, payload))
        return {"ok": True, "confirmed_volume": payload.get("target_volume")}

    monkeypatch.setattr(stations_presets, "play_station_on_device", fake_play_station_on_device)
    suffix = uuid4().hex[:8]
    device_id = f"VOL25{suffix}"
    db = app_db.SessionLocal()
    try:
        db.add(Device(device_id=device_id, ip_address="192.0.2.85", name="Volume Timer", model="SoundTouch Test"))
        station = Station(name="Volume Station", stream_url="http://example.test/volume.mp3")
        db.add(station)
        db.commit()
        db.refresh(station)
        row = ScheduledAction(name=f"Volume Timer {suffix}", enabled=1, start_time="10:00", days="daily", device_ids=device_id, station_id=station.id, volume=25, dry_run=0)
        db.add(row)
        db.commit()
        result = asyncio.run(alarm_engine.trigger_schedule(row, db, trigger="manual"))
        history = db.query(PlayHistory).filter(PlayHistory.device_id == device_id).one()
    finally:
        db.close()

    assert result["ok"] is True
    assert calls[0][2]["target_volume"] == 25
    assert "safe_volume" not in calls[0][2]
    assert history.volume == 25


def test_multiroom_remove_member_keeps_device(monkeypatch):
    zone_by_ip = {
        "192.0.2.81": '<zone master="MRKEEPMASTER"><member ipaddress="192.0.2.82">MRKEEPMEMBER</member></zone>',
        "192.0.2.82": '<zone master="MRKEEPMASTER"><member ipaddress="192.0.2.82">MRKEEPMEMBER</member></zone>',
    }

    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, path: str) -> str:
            assert path == "/getZone"
            return zone_by_ip[self.ip_address]

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/setZone"
            zone_by_ip["192.0.2.81"] = body
            zone_by_ip["192.0.2.82"] = '<zone />'
            return "<status>OK</status>"

    monkeypatch.setattr("basswiesn.app.routers.multiroom.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        db = app_db.SessionLocal(); db.add(Setting(key="lab_mode", value="true")); db.commit(); db.close()
        client.post("/api/devices", json={"device_id": "MRKEEPMASTER", "name": "Master", "ip_address": "192.0.2.81", "model": "SoundTouch Test"})
        client.post("/api/devices", json={"device_id": "MRKEEPMEMBER", "name": "Member", "ip_address": "192.0.2.82", "model": "SoundTouch Test"})
        removed = client.post("/api/multiroom/remove-device", json={"device_id": "MRKEEPMEMBER", "confirmation": "REMOVE MEMBER"})
        devices = client.get("/api/devices")

    assert removed.status_code == 200
    assert removed.json()["device_still_configured"] is True
    assert any(item["device_id"] == "MRKEEPMEMBER" for item in devices.json())
    assert all(item["device_id"] != "MRKEEPMEMBER" for item in removed.json()["remaining"])


def test_multiroom_remove_member_finds_master_when_slave_reports_standalone(monkeypatch):
    zone_by_ip = {
        "192.0.2.83": '<zone master="MRFINDMASTER"><member ipaddress="192.0.2.84">MRFINDSLAVE</member></zone>',
        "192.0.2.84": '<zone />',
    }

    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, path: str) -> str:
            assert path == "/getZone"
            return zone_by_ip[self.ip_address]

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/setZone"
            zone_by_ip["192.0.2.83"] = body
            return "<status>OK</status>"

    monkeypatch.setattr("basswiesn.app.routers.multiroom.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        db = app_db.SessionLocal(); db.add(Setting(key="lab_mode", value="true")); db.commit(); db.close()
        client.post("/api/devices", json={"device_id": "MRFINDMASTER", "name": "Master", "ip_address": "192.0.2.83", "model": "SoundTouch Test"})
        client.post("/api/devices", json={"device_id": "MRFINDSLAVE", "name": "Slave", "ip_address": "192.0.2.84", "model": "SoundTouch Test"})
        removed = client.post("/api/multiroom/remove-device", json={"device_id": "MRFINDSLAVE", "confirmation": "REMOVE MEMBER"})
        devices = client.get("/api/devices")

    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    assert removed.json()["device_still_configured"] is True
    assert any(item["device_id"] == "MRFINDSLAVE" for item in devices.json())
    assert zone_by_ip["192.0.2.83"] == '<zone master="MRFINDMASTER" />'


def test_multiroom_remove_offline_member_returns_clear_error_and_keeps_device(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def get_xml(self, path: str) -> str:
            assert path == "/getZone"
            raise OSError("radio offline")

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            raise AssertionError("offline detach must not write")

    monkeypatch.setattr("basswiesn.app.routers.multiroom.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        db = app_db.SessionLocal(); db.add(Setting(key="lab_mode", value="true")); db.commit(); db.close()
        client.post("/api/devices", json={"device_id": "MROFFLINE", "name": "Offline", "ip_address": "192.0.2.86", "model": "SoundTouch Test"})
        removed = client.post("/api/multiroom/remove-device", json={"device_id": "MROFFLINE", "confirmation": "REMOVE MEMBER"})
        devices = client.get("/api/devices")

    assert removed.status_code == 503
    assert "nicht aus BASSWIESN entfernt" in removed.json()["detail"]["error"]
    assert any(item["device_id"] == "MROFFLINE" for item in devices.json())


def test_schedule_api_create_update_delete_and_enable_disable_with_preset_button():
    suffix = uuid4().hex[:8]
    device_id = f"SCHPRESET{suffix}"

    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": device_id, "name": "Schedule Preset", "ip_address": "192.0.2.78", "model": "SoundTouch Test"})
        created = client.post(
            "/api/schedules",
            json={"name": f"Preset Timer {suffix}", "start_time": "08:00", "end_time": "08:30", "days": "daily", "device_ids": [device_id], "preset_button": 3, "volume": 12, "stop_action": "stop_standby", "dry_run": False},
        )
        assert created.status_code == 200
        schedule_id = created.json()["id"]

        disabled = client.post(f"/api/schedules/{schedule_id}/enable", json={"enabled": False})
        enabled = client.post(f"/api/schedules/{schedule_id}/enable", json={"enabled": True})
        updated = client.post(f"/api/schedules/{schedule_id}", json={"preset_button": 4, "device_ids": device_id})
        schedules = client.get("/api/schedules")
        deleted = client.delete(f"/api/schedules/{schedule_id}")

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert enabled.json()["enabled"] is True
    assert updated.status_code == 200
    row = next(item for item in schedules.json() if item["id"] == schedule_id)
    assert row["preset_button"] == 4
    assert row["device_ids"] == [device_id]
    assert row["stop_action"] == "stop_standby"
    assert deleted.status_code == 200
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.integration, _pytest_marker.mark.slow]
