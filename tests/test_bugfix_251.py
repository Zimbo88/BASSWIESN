import asyncio
import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, Preset, Setting
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.models import Station


PROFILED_DEVICE_ID = "A1B2C3D4E5F6"


def _profiled_device(device_id: str = PROFILED_DEVICE_ID, ip_address: str = "192.0.2.112") -> Device:
    info = (
        f'<info deviceID="{device_id}"><name>Kitchen</name>'
        '<type>SoundTouch 20 Series III</type>'
        '<components><component><softwareVersion>'
        '27.0.6.46330.5043500 epdbuild.release'
        '</softwareVersion></component></components>'
        '<productID>093B</productID><variant>spotty</variant><moduleType>sm2</moduleType></info>'
    )
    return Device(
        device_id=device_id,
        ip_address=ip_address,
        name="Kitchen",
        model="SoundTouch 20 Series III",
        firmware="27.0.6.46330.5043500 epdbuild.release",
        info_xml=info,
        identity_verified=True,
        reachable=True,
    )


def test_easy_discovery_uses_same_explicit_scan_contract_and_has_visible_results():
    html = TestClient(create_web_app(background_tasks=False)).get("/").text
    source = Path("basswiesn/app/static/app.js").read_text(encoding="utf-8")
    assert 'id="setup-rebuild-discover"' in html
    assert 'id="scan-radios-now"' in html
    assert 'id="device-scan-results"' in html
    assert 'postJson("/api/setup/rebuild/discover"' not in source
    assert 'postJson("/api/devices/scan"' in source
    assert "const result = await performRadioScan(button)" in source


def test_about_is_easy_and_member_remove_is_lab_only():
    html = TestClient(create_web_app(background_tasks=False)).get("/").text
    assert 'data-view="about" data-normal data-easy' in html
    assert 'class="single-room-remove lab-only"' in html


def test_devices_use_responsive_cards_without_desktop_table_overflow():
    css = Path("basswiesn/app/static/app.css").read_text(encoding="utf-8")
    assert ".device-card-grid { display: grid;" in css
    assert "#view-devices .table-scroll { display: none; }" in css


def test_no_station_logo_is_explicit_empty_container_art():
    station = Station(name="Bayern 3", stream_url="https://example.test/live.mp3", image_url="https://example.test/logo.png")
    radio_symbol = content_item_xml(station, "https://example.test/live.mp3", include_container_art=False)
    station_logo = content_item_xml(station, "https://example.test/live.mp3", include_container_art=True)
    no_logo = content_item_xml(station, "https://example.test/live.mp3", include_container_art=False, empty_container_art=True)
    assert "containerArt" not in radio_symbol
    assert "<containerArt>https://example.test/logo.png</containerArt>" in station_logo
    assert "<containerArt></containerArt>" in no_logo


def test_factory_reset_preview_is_lab_only_and_profile_bound():
    with app_db.SessionLocal() as db:
        db.add(_profiled_device())
        db.add(Setting(key="ui_mode", value="standard"))
        db.commit()
    with TestClient(create_web_app(background_tasks=False)) as client:
        denied = client.post(f"/api/lab/devices/{PROFILED_DEVICE_ID}/factory-reset", json={"dry_run": True})
        assert denied.status_code == 403
        client.post("/api/system/settings", json={"ui_mode": "lab"}).raise_for_status()
        preview = client.post(f"/api/lab/devices/{PROFILED_DEVICE_ID}/factory-reset", json={"dry_run": True})
        assert preview.status_code == 200
        body = preview.json()
        assert body["eligible"] is True
        assert body["profile"]
        assert body["operation"] == "sys factorydefault"
        assert body["confirmation_required"] == "FACTORY RESET RADIO"


def test_factory_reset_execute_requires_backup_then_sends_once(monkeypatch):
    calls = []

    class Adapter:
        async def identify(self, row):
            calls.append("identity")
            return {
                "device_id": row.device_id,
                "model": row.expected_model,
                "firmware": "27.0.6.46330.5043500 epdbuild.release",
                "product_id": "0X093B",
                "variant": "spotty",
                "platform": "sm2",
                "write_profile": "test-profile",
            }

        async def backup(self, row):
            calls.append("backup")
            return {"verified": True, "backup_path": "/tmp/test-backup", "sha256": {"info.xml": "abc"}}

    class Result:
        def public_dict(self):
            return {"operation": "factory_reset", "output": ""}

    async def send(ip_address, device_id):
        calls.append(("send", ip_address, device_id))
        return Result()

    from basswiesn.app.routers import api

    monkeypatch.setattr(api, "RadioSetupAdapter", Adapter)
    monkeypatch.setattr(api, "cli_factory_reset", send)
    with app_db.SessionLocal() as db:
        db.add(_profiled_device())
        db.add(Setting(key="ui_mode", value="lab"))
        db.commit()
    with TestClient(create_web_app(background_tasks=False)) as client:
        missing = client.post(
            f"/api/lab/devices/{PROFILED_DEVICE_ID}/factory-reset",
            json={"dry_run": False, "acknowledged": False, "confirmation": "FACTORY RESET RADIO"},
        )
        assert missing.status_code == 400
        response = client.post(
            f"/api/lab/devices/{PROFILED_DEVICE_ID}/factory-reset",
            json={"dry_run": False, "acknowledged": True, "confirmation": "FACTORY RESET RADIO"},
        )
        assert response.status_code == 200
        assert calls == ["identity", "backup", ("send", "192.0.2.112", PROFILED_DEVICE_ID)]
        assert response.json()["automatic_follow_up_probe"] is False
    with app_db.SessionLocal() as db:
        row = db.query(Device).filter(Device.device_id == PROFILED_DEVICE_ID).one()
        assert row.reachable is False
        assert "factory reset" in row.offline_reason


def test_factory_reset_ui_is_lab_only_and_requires_explicit_acknowledgement():
    html = TestClient(create_web_app(background_tasks=False)).get("/").text
    assert 'class="panel danger-panel lab-only lab-factory-reset-card"' in html
    assert 'id="factory-reset-device-select"' in html
    assert "I understand that this resets the radio" in html
    assert "FACTORY RESET RADIO" in html


def test_factory_reset_rejects_protected_device_before_profile_or_transport(monkeypatch):
    from basswiesn.app.routers import api

    with app_db.SessionLocal() as db:
        db.add(
            Device(
                device_id="CCDDEEFF0011",
                ip_address="192.168.50.25",
                name="Protected fixture",
                model="SoundTouch 20 Series III",
                firmware="27.0.6",
                identity_verified=True,
                reachable=True,
            )
        )
        db.add(Setting(key="ui_mode", value="lab"))
        db.commit()

    monkeypatch.setattr(
        api,
        "candidate_from_device",
        lambda *_args, **_kwargs: pytest.fail("profile lookup must not run for protected device"),
    )
    monkeypatch.setattr(
        api,
        "cli_factory_reset",
        lambda *_args, **_kwargs: pytest.fail("transport must not run for protected device"),
    )
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post("/api/lab/devices/CCDDEEFF0011/factory-reset", json={"dry_run": True})
    assert response.status_code == 403
    assert "Protected devices" in response.json()["detail"]


def test_cli_factory_reset_sends_only_confirmed_fixed_command_once(monkeypatch):
    from basswiesn.app.services.setup_rebuild import cli17000

    calls = []
    journal = []

    async def send(ip_address, device_id, command, *, timeout):
        calls.append((ip_address, device_id, command, timeout))
        return ""

    monkeypatch.setattr(cli17000, "_send_fixed", send)
    monkeypatch.setattr(cli17000, "record_transport_attempt", lambda **fields: journal.append(fields))
    result = asyncio.run(cli17000.factory_reset("192.0.2.44", "AABBCCDDEEFF"))

    assert calls == [("192.0.2.44", "AABBCCDDEEFF", "sys factorydefault", 8.0)]
    assert result.operation == "factory_reset"
    assert result.responses == ("sys factorydefault",)
    assert journal[-1]["phase"] == "factory_reset_sent"
    assert journal[-1]["verified"] is True


def test_cli_factory_reset_rejects_explicit_firmware_error(monkeypatch):
    from basswiesn.app.services.setup_rebuild import cli17000

    async def rejected(*_args, **_kwargs):
        return "Error: unknown command"

    monkeypatch.setattr(cli17000, "_send_fixed", rejected)
    monkeypatch.setattr(cli17000, "record_transport_attempt", lambda **_fields: None)
    with pytest.raises(RuntimeError, match="rejected"):
        asyncio.run(cli17000.factory_reset("192.0.2.44", "AABBCCDDEEFF"))


def test_station_art_mode_change_is_local_and_requires_verified_preset_sync(monkeypatch):
    from basswiesn.app.routers import api

    async def current_settings(*_args, **_kwargs):
        return {"current": {"station_art_mode": "radio_symbol"}}

    class NoTransport:
        def __init__(self, _ip_address):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"unexpected radio transport: {name}")

    monkeypatch.setattr(api, "device_settings", current_settings)
    monkeypatch.setattr(api, "SoundTouchClient", NoTransport)
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="LOGO-MODE", ip_address="192.0.2.61", name="Logo radio"))
        db.commit()
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post(
            "/api/devices/LOGO-MODE/settings-apply",
            json={"values": {"station_art_mode": "no_station_logo"}},
        )
    assert response.status_code == 200
    assert response.json()["preset_sync_required"] is True
    assert response.json()["changed"] == [{
        "setting": "station_art_mode",
        "before": "radio_symbol",
        "after": "no_station_logo",
        "scope": "BASSWIESN playback metadata",
    }]
    with app_db.SessionLocal() as db:
        assert db.query(Setting).filter(Setting.key == "station_art_mode:LOGO-MODE").one().value == "no_station_logo"


def test_no_logo_sync_preview_reads_radio_and_skips_already_current_slot(monkeypatch):
    from basswiesn.app.routers import stations_presets

    with app_db.SessionLocal() as db:
        device = Device(device_id="LOGO-SYNC", ip_address="192.0.2.62", name="Logo sync radio")
        station = Station(name="Bayern 3", stream_url="https://example.test/live.mp3", image_url="https://example.test/logo.png")
        db.add_all([device, station, Setting(key="lan_host", value="192.0.2.10"), Setting(key="station_art_mode:LOGO-SYNC", value="no_station_logo")])
        db.flush()
        db.add(Preset(device_id="LOGO-SYNC", button=1, station_id=station.id, source="LOCAL_INTERNET_RADIO", location="placeholder", content_item_xml=""))
        db.commit()
        preset = db.query(Preset).filter(Preset.device_id == "LOGO-SYNC").one()
        preset.location = stations_presets._effective_preset_location(db, "LOGO-SYNC", preset)
        desired_xml = stations_presets._effective_preset_content_item_xml(db, "LOGO-SYNC", preset, location_override=preset.location)
        db.commit()

    radio_xml = [f'<presets><preset id="1">{desired_xml}</preset></presets>']

    class Radio:
        async def get_xml(self, path):
            assert path == "/presets"
            return radio_xml[0]

    monkeypatch.setattr(stations_presets, "_soundtouch_client_for", lambda *_args, **_kwargs: Radio())
    with TestClient(create_web_app(background_tasks=False)) as client:
        current = client.post("/api/presets/LOGO-SYNC/sync", json={"dry_run": True, "probe": True})
        assert current.status_code == 200
        assert current.json()["already_current_slots"] == [1]
        assert current.json()["expected_changes"] == []
        assert current.json()["radio_action"] == "none"

        # Omitted containerArt is a different contract from an explicit empty
        # containerArt and therefore must be shown in the preview. The radio
        # may legitimately point at a different BASSWIESN host; artwork sync
        # must preserve that location and every non-artwork identity field.
        radio_xml[0] = (
            radio_xml[0]
            .replace("192.0.2.10", "192.0.2.99")
            .replace('sourceAccount=""', 'sourceAccount="legacy-radio-account"')
            .replace("<containerArt></containerArt>", "")
        )
        changed = client.post("/api/presets/LOGO-SYNC/sync", json={"dry_run": True, "probe": True})
        assert changed.status_code == 200
        assert changed.json()["already_current_slots"] == []
        assert [item["button"] for item in changed.json()["expected_changes"]] == [1]
        assert "192.0.2.99:1516" in changed.json()["expected_changes"][0]["location"]
        assert changed.json()["expected_changes"][0]["preserves"] == [
            "source", "sourceAccount", "location", "itemName"
        ]

    with app_db.SessionLocal() as db:
        local_rows = db.query(Preset).filter(Preset.device_id == "LOGO-SYNC").all()
        parsed_radio = stations_presets.preset_summaries_from_xml(radio_xml[0])
        prepared, _already, _skipped = stations_presets._prepare_artwork_only_sync(
            db, "LOGO-SYNC", local_rows, parsed_radio
        )
        desired = prepared[1]["content_item_xml"]
        assert 'location="http://192.0.2.99:1516/' in desired
        assert 'sourceAccount="legacy-radio-account"' in desired
        assert "<containerArt />" in desired or "<containerArt></containerArt>" in desired


def test_checker_canonicalizes_local_radio_account_and_distinguishes_origin_warning():
    from basswiesn.app.routers import stations_presets

    assert stations_presets._canonical_preset_source_account(
        "LOCAL_INTERNET_RADIO", "stale station display name"
    ) == ""
    assert stations_presets._canonical_preset_source_account("TUNEIN", "account") == "account"
    encode = lambda payload: base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    left = f"http://192.0.2.10:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data={encode({'streamUrl': 'https://example.test/live.mp3'})}"
    right = f"http://192.0.2.99:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data={encode({'streamUrl': 'https://example.test/live.mp3', 'streamFormat': 'mp3'})}"
    assert not stations_presets._locations_match(left, right)
    assert stations_presets._locations_select_same_station(left, right)


def test_online_station_artwork_rejects_private_target_before_fetch(monkeypatch):
    from basswiesn.app.routers import api

    async def forbidden_fetch(*_args, **_kwargs):
        pytest.fail("private/protected artwork target must be rejected before fetch")

    monkeypatch.setattr(api, "cache_artwork", forbidden_fetch)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.get(
            "/api/stations/online-artwork",
            params={"url": "http://192.168.50.25/logo.png"},
        )
    assert response.status_code == 404
