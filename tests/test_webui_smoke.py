from html.parser import HTMLParser
from uuid import uuid4

from fastapi.testclient import TestClient

from basswiesn.app.main import create_web_app
from basswiesn.app.routers import api


class ControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.buttons = []
        self.inputs = []
        self.selects = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "form":
            self.forms.append(data)
        elif tag == "button":
            self.buttons.append(data)
        elif tag == "input":
            self.inputs.append(data)
        elif tag == "select":
            self.selects.append(data)


def _controls():
    with TestClient(create_web_app()) as client:
        html = client.get("/").text
    parser = ControlParser()
    parser.feed(html)
    return html, parser


def _remote_controls():
    with TestClient(create_web_app()) as client:
        html = client.get("/remote/REMOTE-UI-SMOKE").text
    parser = ControlParser()
    parser.feed(html)
    return html, parser


def test_webui_controls_are_present_and_wired_to_javascript():
    html, parser = _controls()
    js = open("basswiesn/app/static/app.js", encoding="utf-8").read()

    assert len(parser.forms) >= 40
    assert len(parser.buttons) >= 110
    # Preview-only switches were deliberately removed from the end-user UI.
    assert len(parser.inputs) >= 100
    assert len(parser.selects) >= 60

    missing_forms = [item["id"] for item in parser.forms if item.get("id") and item["id"] not in js]
    assert missing_forms == []

    allowed_data_buttons = {"data-view", "data-open", "data-view-jump", "data-lab", "data-wizard-action", "data-route-action", "data-setting", "data-power-action", "data-recovery-action", "data-display-action"}
    missing_buttons = []
    for item in parser.buttons:
        button_id = item.get("id")
        data_keys = {key for key in item if key.startswith("data-")}
        if button_id and button_id not in js and not data_keys.intersection(allowed_data_buttons):
            missing_buttons.append(button_id)
    assert missing_buttons == []

    for expected in ["download-radio-presets", "station-play-form", "device-info-form", "native-station-search-form", "zone-status-form", "device-live-comparison"]:
        assert expected in html

    assert '/static/js/api.js' in html
    assert '/static/js/ui-errors.js' in html
    assert "setFormBusy(formElement" in js
    assert "showApiError(error" in js
    assert "http://127.0.0.1:${cloudPort}" not in js
    assert "http://127.0.0.1:${debugPort}" not in js
    assert "server.cloud_base_url || shell.dataset.cloudBaseUrl" in js
    assert "server.debug_base_url || shell.dataset.debugBaseUrl" in js
    assert "`${urls.cloud}/about`" in js
    assert "`${urls.debug}/`" in js
    assert '"rollback_failed"' in js
    assert 'state.devices = await getJson("/api/devices");' in js
    assert 'getJson("/api/setup/jobs/latest")' not in js
    assert 'reportServiceStatus("cloud"' in js
    assert 'reportServiceStatus("debug"' in js
    assert 'const noStore = { cache: "no-store" };' in js
    assert "refreshServiceStatus();" in js
    assert "30_000" in js
    # SSH remains available for explicitly marked expert/LAB tools, but it is
    # no longer a prerequisite of the normal Setup Rebuild assistant.
    assert html.count('class="requires-ssh"') >= 3
    setup_assistant = html.split('id="setup-rebuild-assistant"', 1)[1].split('id="setup-oneclick"', 1)[0]
    assert 'class="requires-ssh"' not in setup_assistant
    assert ".requires-ssh" in open("basswiesn/app/static/app.css", encoding="utf-8").read()
    assert 'id="setup-apply-dry-run"' in html
    assert '<button class="nav-button" data-view="schedules" data-easy>Wecker Timer</button>' in html
    assert '<section class="view" id="view-schedules">' in html
    assert 'name="stop_action"' in html
    assert 'id="schedule-days-select"' in html
    assert 'id="schedule-weekday-picker"' in html
    assert html.count('name="weekday"') == 7
    assert html.count('name="weekday" value=') == 7
    assert html.count('name="weekday" value="mon" disabled') == 1
    assert "function updateScheduleWeekdayControls()" in js
    assert 'input.disabled = !custom' in js
    assert "Diese Funktion ist noch nicht releasefähig" not in html
    # The normal UI must not advertise USB/reset-era setup workarounds.
    assert "SSH-Zugriff ohne USB-Stick klappt" not in html
    assert 'id="debug-summary"' in html
    assert "Browser-Link nicht erreichbar" in js
    assert 'debugSummary.textContent = debugOnline ? "Online" : "Eingeschränkt"' in js
    assert '"Diagnose-Port prüfen"' in js
    assert 'setStatus("debug-state", Boolean(health.debug?.online), health.debug?.online ? "online" : "offline")' not in js
    assert 'dry_run: dryRun' in js
    assert 'state.devices.find((device) => device.ip_address === button.dataset.scanIp)' in js
    assert "maybeAutoScanRadios" not in js
    assert 'postJson("/api/devices/scan"' in js
    assert 'document.getElementById("scan-radios-now")?.addEventListener("click"' in js
    assert 'data-service-link="cloud"' in html
    assert 'data-service-link="debug"' in html
    assert 'data-view="multiroom" data-capability' not in html
    assert 'if (element.closest(".topnav"))' in js
    assert 'nav-button[data-view="devices"]\')?.click()' not in js
    assert 'data-schedule-trigger' in js
    assert 'data-schedule-toggle' in js
    assert 'id="station-play-safe-volume"' in html
    assert 'max="5" value="1"' in html
    assert 'if (!dryRun && safeVolume !== null) body.safe_volume = safeVolume' in js
    assert 'if (deviceId) await playStation(created.id, true)' in js
    assert 'Die aktuelle Radio-Lautstärke bleibt unverändert.' in js
    assert 'id="ui-mode-switch"' in html
    assert 'id="key-safe-volume-enabled"' in html
    assert 'if (safeVolume !== null) payload.safe_volume = safeVolume' in js


def test_standalone_remote_controls_are_present_and_wired_to_remote_javascript():
    html, parser = _remote_controls()
    js = open("basswiesn/app/static/remote.js", encoding="utf-8").read()

    assert 'id="remote-play-station"' in html
    assert 'id="remote-station"' in html
    assert 'id="remote-volume"' in html
    assert "remote-play-station" in js
    assert "/stations/${encodeURIComponent(stationId)}/play" in js
    assert 'id="remote-safe-start-enabled"' in html
    assert 'if (safeStartEnabled.checked) payload.safe_volume' in js
    assert 'await setVolume(volume.value || 5)' not in js
    assert "data-volume-step" in html
    assert "data-key" in html

    allowed_data_buttons = {"data-key", "data-volume-step"}
    missing_buttons = []
    for item in parser.buttons:
        button_id = item.get("id")
        data_keys = {key for key in item if key.startswith("data-")}
        if button_id and button_id not in js and not data_keys.intersection(allowed_data_buttons):
            missing_buttons.append(button_id)
    assert missing_buttons == []


def test_easy_mode_is_persisted_and_only_changes_ui_complexity():
    with TestClient(create_web_app()) as client:
        saved = client.post("/api/system/settings", json={"ui_mode": "easy"})
        current = client.get("/api/system/settings")

    assert saved.status_code == 200
    assert saved.json()["ui_mode"] == "easy"
    assert saved.json()["lab_mode"] == "false"
    assert current.json()["ui_mode"] == "easy"
    html, _parser = _controls()
    css = open("basswiesn/app/static/app.css", encoding="utf-8").read()
    js = open("basswiesn/app/static/app.js", encoding="utf-8").read()
    assert 'data-view="setup" data-normal data-easy' in html
    assert '.easy-mode .topnav > :not([data-easy])' in css
    assert 'new Set(["setup", "devices", "controls", "presets", "multiroom", "schedules", "device-settings"])' in js


def test_diagnostics_service_health_reports_internal_online(monkeypatch):
    async def fake_status(name, url):
        return {"service": name, "online": True, "status_code": 200, "url": url}

    monkeypatch.setattr(api, "_http_service_status", fake_status)
    with TestClient(create_web_app()) as client:
        response = client.get("/api/system/service-health")

    assert response.status_code == 200
    body = response.json()
    assert body["debug"]["online"] is True
    assert body["debug"]["internal_url"].endswith("/health")
    assert body["debug"]["browser_url"].endswith("/")
    assert body["status"] == "green"


def test_diagnostics_ui_uses_degraded_instead_of_offline_for_debug():
    html, _parser = _controls()
    js = open("basswiesn/app/static/app.js", encoding="utf-8").read()
    assert 'id="debug-summary"' in html
    assert "Nicht erreichbar" not in js
    assert "Diagnose-Port prüfen" in js
    assert "Browser-Link eingeschränkt" in js


def test_lan_webgui_origin_is_allowed_for_cross_port_service_checks():
    with TestClient(create_web_app()) as client:
        response = client.options(
            "/api/health",
            headers={
                "Origin": "http://192.168.50.20:1328",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://192.168.50.20:1328"


def test_webui_task_smoke_simulates_all_major_forms_without_hardware(monkeypatch):
    async def fake_sync_presets_to_radio(device, expected, db, backup_label="preset-sync", **_kwargs):
        from basswiesn.app.services.preset_transactions import transition_preset_mutation

        for mutation in (_kwargs.get("mutations") or {}).values():
            transition_preset_mutation(db, mutation, "RADIO_WRITE")
            transition_preset_mutation(db, mutation, "RADIO_READBACK")
            transition_preset_mutation(db, mutation, "VERIFIED")
        return [
            {"button": button, "location": location, "source": "LOCAL_INTERNET_RADIO", "type": "stationurl"}
            for button, location in expected.items()
        ]

    monkeypatch.setattr(
        "basswiesn.app.routers.stations_presets.sync_presets_to_radio",
        fake_sync_presets_to_radio,
    )
    suffix = uuid4().hex[:8]
    device_a = f"UITESTA{suffix}"
    device_b = f"UITESTB{suffix}"
    station_name = f"UI Test Station {suffix}"

    with TestClient(create_web_app()) as client:
        def post(path, payload, expected=200):
            response = client.post(path, json=payload)
            assert response.status_code == expected, f"{path}: {response.status_code} {response.text}"
            return response.json()

        def get(path, expected=200):
            response = client.get(path)
            assert response.status_code == expected, f"{path}: {response.status_code} {response.text}"
            return response.json()

        # Dashboard/catalog/navigation data loads.
        for path in [
            "/api/health", "/api/devices", "/api/stations", "/api/languages", "/api/keys",
            "/api/display/metadata-modes", "/api/telnet/commands", "/api/media-library/capabilities",
            "/api/services/catalog", "/api/stereo-pairing/research", "/api/media-types",
            "/api/system/settings", "/api/media-playlists", "/api/reference-setups",
            "/api/play-history", "/api/stats/playback", "/api/schedules", "/api/telemetry",
            "/api/telemetry/summary", "/api/settings/catalog", "/api/devices/live-comparison",
        ]:
            get(path)

        # Devices and setup plans.
        post("/api/devices", {"device_id": device_a, "name": "UI Wohnzimmer", "ip_address": "127.0.0.1", "model": "SoundTouch Test"})
        post("/api/devices", {"device_id": device_b, "name": "UI Kueche", "ip_address": "127.0.0.2", "model": "SoundTouch Test"})
        post("/api/system/settings", {"lan_host": "192.168.50.77"})
        post(f"/api/devices/{device_a}/rename", {"name": "UI Wohnzimmer Neu", "dry_run": True})
        get(f"/api/setup/plans/{device_a}")
        post(f"/api/setup/plans/{device_a}", {"name": "UI Setup", "steps": [{"key": "verify"}]})
        post("/api/devices/scan", {"cidr": "127.0.0.1/32", "timeout": 0.05, "limit": 1, "save": False})
        get("/api/setup/wizard/server-info")

        # Device settings, diagnostics and controls.
        get(f"/api/devices/{device_a}/settings")
        for setting, value in [
            ("volume", 30), ("bass", -3), ("clockDisplay", "true"), ("language", "en"),
            ("systemtimeout", 20), ("powersaving", "true"), ("rebroadcastlatencymode", "SYNC_TO_ZONE"),
        ]:
            post(f"/api/devices/{device_a}/settings/{setting}", {"value": value, "dry_run": True})
        post(f"/api/devices/{device_a}/settings/clockConfig", {"value": {"timezoneInfo": "Europe/Berlin", "timeFormat": "TIME_FORMAT_24HOUR_ID", "userOffsetMinute": 0, "brightnessLevel": 7}, "dry_run": True})
        post(f"/api/devices/{device_a}/bass-capabilities", {"dry_run": True})
        post(f"/api/devices/{device_a}/sources/name-plan", {"source": "AUX", "name": "Plattenspieler"})
        post(f"/api/devices/{device_a}/probe-info", {"dry_run": True})
        get(f"/api/devices/{device_a}/host-config")
        key_plan = post(f"/api/devices/{device_a}/key", {"key": "PLAY", "dry_run": True})
        assert key_plan["sequence"] == [
            '<key state="press" sender="Gabbo">PLAY</key>',
            '<key state="release" sender="Gabbo">PLAY</key>',
        ]
        post(f"/api/devices/{device_a}/telnet/plan", {"command_key": "get_current_config"})

        # Stations, native station API and playback dry-run.
        station = post("/api/stations", {"name": station_name, "stream_url": f"http://example.test/{suffix}.mp3", "image_url": ""})
        station_id = station["id"]
        duplicate = post("/api/stations", {"name": station_name, "stream_url": f"http://example.test/{suffix}.mp3", "image_url": ""})
        assert duplicate["id"] == station_id
        assert duplicate["created"] is False
        post(f"/api/devices/{device_a}/stations/{station_id}/play", {"dry_run": True})
        post(f"/api/devices/{device_a}/station/search-native", {"source": "TUNEIN", "query": "Jazz", "dry_run": True})
        post(f"/api/devices/{device_a}/station/add-native", {"source": "TUNEIN", "token": "station123", "name": "Jazz FM", "dry_run": True})

        # Display.
        post(f"/api/devices/{device_a}/display/settings", {"mode": "station_clock_wifi"})
        post(f"/api/devices/{device_a}/display/metadata-preview", {"mode": "station_clock_wifi", "station_id": station_id, "probe": False})
        post(f"/api/devices/{device_a}/display/direct-select", {"mode": "station_clock_wifi", "station_id": station_id, "probe": False, "dry_run": True})
        post(f"/api/devices/{device_a}/display-recovery/plan", {"mode": "pixel_wash", "minutes": 1})

        # Presets and profiles.
        get(f"/api/presets/{device_a}")
        get(f"/api/presets/{device_a}/status")
        post(f"/api/presets/{device_a}/1", {"station_id": station_id, "dry_run": True})
        post(f"/api/presets/{device_a}/1", {"station_id": station_id, "dry_run": False, "memory_checked": True})
        post(f"/api/devices/{device_a}/presets/download", {"dry_run": True})
        post("/api/presets/clone", {"source_device_id": device_a, "target_device_id": device_b})
        profile = post("/api/preset-profiles", {"name": f"UI Profile {suffix}", "description": "smoke", "slots": [{"button": 1, "station_id": station_id}]})
        get("/api/preset-profiles")
        post(f"/api/preset-profiles/{profile['id']}/apply/{device_a}", {"dry_run": True})

        # Multiroom and schedules.
        post("/api/multiroom/preview", {"master_device_id": device_a, "member_device_ids": [device_b]})
        post("/api/multiroom/set", {"master_device_id": device_a, "member_device_ids": [device_b], "dry_run": True})
        post("/api/multiroom/clear", {"master_device_id": device_a, "dry_run": True})
        post(f"/api/devices/{device_a}/zone/status", {"dry_run": True})
        scenario = post("/api/multiroom/scenarios", {"name": f"UI Scenario {suffix}", "master_device_id": device_a, "member_device_ids": [device_b], "station_id": station_id, "volume": 25})
        get("/api/multiroom/scenarios")
        post(f"/api/multiroom/scenarios/{scenario['id']}/preview", {})
        post("/api/schedules", {"name": f"UI Schedule {suffix}", "start_time": "07:00", "end_time": "08:00", "days": "daily", "device_ids": [device_a], "station_id": station_id, "volume": 20, "dry_run": True})

        # Media, backup/reference and telemetry/lab plans.
        post("/api/media-playlists", {"name": f"UI Media {suffix}", "source_type": "HTTP_PLAYLIST", "uri": "http://example.test/list.m3u", "notes": "smoke"})
        post(f"/api/devices/{device_a}/media/list-servers", {"dry_run": True})
        get(f"/api/devices/{device_a}/battery/patch-plan")
        post(f"/api/devices/{device_a}/backup/plan", {"include_usb": False})
        reference = post(f"/api/reference-setups/from-device/{device_a}", {"name": f"UI Reference {suffix}", "notes": "smoke"})
        post(f"/api/reference-setups/{reference['id']}/apply/{device_b}", {"dry_run": True})
        post("/api/system/settings", {"web_language": "en", "default_timezone": "Europe/Berlin", "device_language_default": "en", "display_metadata_mode": "station_clock_wifi"})
        post("/api/telemetry", {"device_id": device_a, "event_type": "ui-smoke", "endpoint": "software", "payload": "ok"})
        post(f"/api/devices/{device_a}/telemetry/probe", {"endpoint": "/info", "dry_run": True})
        get(f"/api/devices/{device_a}/radio-log/sources")
        post(f"/api/devices/{device_a}/radio-log/capture", {"dry_run": True, "include_cli": True, "reason": "ui-smoke"})
        post(f"/api/devices/{device_a}/ssh-log/capture", {"dry_run": True, "reason": "ui-smoke", "username": "root"})
        post(f"/api/devices/{device_a}/setup/live-test", {"dry_run": True, "host": "192.0.2.1", "station_id": station_id})
        post(f"/api/devices/{device_a}/power/standby", {"dry_run": True})
        post(f"/api/devices/{device_a}/power/low_power_standby", {"dry_run": True})
        post(f"/api/devices/{device_a}/recovery/factory_default", {"dry_run": True, "confirmation": "YES"}, expected=410)
        post(f"/api/devices/{device_a}/recovery/factory_reset_fix_plan", {"dry_run": True}, expected=410)
        post(f"/api/devices/{device_a}/recovery/persistent_ssh", {"dry_run": True})
        post(f"/api/devices/{device_a}/recovery/nuclear_reset_plan", {"dry_run": True, "confirmation": "YES"}, expected=410)
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
