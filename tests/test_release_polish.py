import asyncio
from datetime import UTC, datetime, timedelta
import json
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_cloud_app, create_web_app
from basswiesn.app.models import ConfigBackup, Device, DiagnosticEvent, PlayHistory, RequestLog, RuntimeState, TelemetryEvent
from basswiesn.app.config import get_settings
from basswiesn.app.services.playback_keepalive import run_playback_keepalive_once
from basswiesn.app.services.device_state import merge_provider_maps, parse_service_availability_xml, parse_sources_xml, update_runtime_state


def test_cloud_device_routes_and_catchall_log_safely():
    with TestClient(create_cloud_app()) as client:
        deleted = client.delete("/streaming/account/ACC/device/DEV", headers={"Authorization": "Bearer secret-token"})
        put = client.put("/streaming/account/ACC/device/DEV", content='{"token":"secret-token"}')
        got = client.get("/streaming/account/ACC/device/DEV")
        heartbeat = client.post("/streaming/account/ACC/device/DEV/heartbeat")
        keepalive = client.post("/streaming/account/ACC/device/DEV/keepalive")
        reporting = client.post("/bmx/orion/reporting/station/abc")
        settings = client.get("/serviceSettings")
        station_info = client.get("/stationInfo")
        credentials = client.post("/setMusicServiceAccount", content="<Credentials><username>u</username><password>p</password><displayName>P</displayName><source>PANDORA</source></Credentials>")
        oauth = client.post("/setMusicServiceOAuthAccount", content="<OAuthCredentials><user>u</user><source>SPOTIFY</source><accessToken>a</accessToken><refreshToken>r</refreshToken><displayName>S</displayName><expiresIn>3600</expiresIn></OAuthCredentials>")
        group_empty = client.get("/group")
        group_create = client.post("/group/create", json={"group_id": "g1", "leader": "A", "members": ["B"], "source": "LOCAL_INTERNET_RADIO"})
        group_update = client.post("/group/update", json={"group_id": "g1", "members": ["B", "C"]})
        group_delete = client.post("/group/delete", json={"group_id": "g1"})
        post = client.post("/streaming/telemetry/new", content="password=topsecret")
        xml = client.get("/unknown/serviceAvailability.xml", headers={"Accept": "application/xml"})

    assert deleted.status_code == 200
    assert deleted.json()["status"] == "ok"
    assert deleted.json()["action"] == "noop"
    assert put.status_code == 200
    assert put.json()["action"] == "accepted"
    assert got.status_code == 200
    assert heartbeat.status_code == 200
    assert keepalive.status_code == 200
    assert reporting.status_code == 200
    assert set(reporting.json()) == {"nextReportIn", "_links", "_embedded"}
    assert reporting.json()["nextReportIn"] == 6
    assert reporting.json()["_links"]["bmx_reporting"]["href"].endswith(
        "/bmx/orion/reporting/station/abc"
    )
    assert settings.status_code == 200
    assert settings.json()["quality"] == "high"
    assert station_info.status_code == 200
    assert {"stationId", "name", "description", "logo", "genre"} <= set(station_info.json())
    assert credentials.status_code == 200
    assert credentials.json()["credentialType"] == "Credentials"
    assert oauth.status_code == 200
    assert oauth.json()["credentialType"] == "OAuthCredentials"
    assert group_empty.status_code == 200
    assert group_create.json()["group"]["group_id"] == "g1"
    assert group_update.json()["group"]["members"] == ["B", "C"]
    assert group_delete.json()["deleted"] is True
    assert post.status_code == 501
    assert post.json()["title"] == "Unsupported cloud contract"
    assert xml.status_code == 404
    assert xml.json()["title"] == "Unsupported cloud contract"

    db = app_db.SessionLocal()
    rows = db.query(RequestLog).all()
    db.close()
    assert len(rows) >= 7
    logged = "\n".join(row.body for row in rows)
    assert "unknown_cloud_request" in logged
    assert "secret-token" not in logged
    assert "topsecret" not in logged
    assert any(row.status_code == 501 for row in rows)

    db = app_db.SessionLocal()
    timeline = db.query(DiagnosticEvent).filter(
        DiagnosticEvent.code == "UNSUPPORTED_CLOUD_CONTRACT"
    ).all()
    db.close()
    assert len(timeline) >= 2


def test_known_cloud_route_still_behaves_like_before():
    with TestClient(create_cloud_app()) as client:
        response = client.get("/streaming/sourceproviders")
        full = client.get("/streaming/account/abc/full")
        provider_settings = client.get("/streaming/account/abc/provider_settings")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.bose.streaming-v1.2+xml")
    assert ET.fromstring(response.text).tag == "sourceProviders"
    assert full.status_code == 200
    assert provider_settings.status_code == 200


def test_play_history_handles_naive_and_aware_datetimes():
    db = app_db.SessionLocal()
    db.add(Device(device_id="HISTORY1", name="History", ip_address="192.0.2.60"))
    db.add(PlayHistory(device_id="HISTORY1", device_name="History", started_at=datetime.now(UTC).replace(tzinfo=None)))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.get("/api/play-history")

    assert response.status_code == 200
    assert response.json()[0]["duration_seconds"] >= 0


def test_runtime_state_parses_sources_availability_and_groups_spotify():
    sources = '<sources><source source="LOCAL_INTERNET_RADIO" status="READY"/><source source="SPOTIFY" status="READY"/><source source="SPOTIFY_CONNECT" status="READY"/></sources>'
    availability = '<serviceAvailability><service service="TUNEIN" status="READY"/><service service="UNKNOWN_NEW" status="UNAVAILABLE"/></serviceAvailability>'
    source_rows, source_map = parse_sources_xml(sources, "now")
    availability_rows, availability_map = parse_service_availability_xml(availability, "now")
    providers = merge_provider_maps(source_map, availability_map)

    assert any(row["source"] == "LOCAL_INTERNET_RADIO" for row in source_rows)
    assert providers["LOCAL_INTERNET_RADIO"]["ready"] is True
    assert providers["TUNEIN"]["provider_id"] == 25
    assert providers["TUNEIN"]["ready"] is True
    assert providers["SPOTIFY"]["visible_in_sources"] is True
    assert len([row for row in source_rows if row["source"] == "SPOTIFY"]) == 2
    assert any(row["service"] == "UNKNOWN_NEW" for row in availability_rows)


def test_service_unavailable_overrides_a_visible_ready_source():
    sources = '<sources><source source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'
    availability = '<serviceAvailability><service service="LOCAL_INTERNET_RADIO" status="UNAVAILABLE"/></serviceAvailability>'
    _, source_map = parse_sources_xml(sources, "now")
    _, availability_map = parse_service_availability_xml(availability, "now")

    provider = merge_provider_maps(source_map, availability_map)[
        "LOCAL_INTERNET_RADIO"
    ]

    assert provider["source_observed"] is True
    assert provider["source_available"] is True
    assert provider["service_observed"] is True
    assert provider["service_available"] is False
    assert provider["available"] is False
    assert provider["ready"] is False


def test_empty_runtime_xml_is_crash_free():
    assert parse_sources_xml("") == ([], {})
    assert parse_service_availability_xml("") == ([], {})


def test_runtime_state_repeated_writes_are_idempotent():
    db = app_db.SessionLocal()
    update_runtime_state(db, "STATE1", current_source="LOCAL_INTERNET_RADIO")
    update_runtime_state(db, "STATE1", playback_state="PLAY_STATE")
    rows = db.query(RuntimeState).filter(RuntimeState.key == "device:STATE1:runtime_state").all()
    payload = json.loads(rows[0].value)
    db.close()

    assert len(rows) == 1
    assert payload["current_source"] == "LOCAL_INTERNET_RADIO"
    assert payload["playback_state"] == "PLAY_STATE"


def test_telemetry_analysis_exports_and_recommendations_are_available():
    db = app_db.SessionLocal()
    db.add(RequestLog(direction="in", service="cloud-catchall", method="PUT", path="/streaming/account/A/device/D", host="streaming.bose.com", status_code=200, body='{"event":"unknown_cloud_request"}'))
    db.add(RequestLog(direction="in", service="cloud", method="GET", path="/missing", host="streaming.bose.com", status_code=404, body=""))
    db.add(TelemetryEvent(device_id="RADIO1", event_type="volume_safety_failed", endpoint="/volume", payload="token=secret", parsed_summary="failed"))
    db.add(RuntimeState(key="legacy:non_object_payload", value="1"))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        summary = client.get("/api/diagnostics/telemetry/summary?range=all")
        json_export = client.get("/api/diagnostics/telemetry/export?format=json&range=all")
        csv_export = client.get("/api/diagnostics/telemetry/export?format=csv&range=all")
        report = client.get("/api/diagnostics/telemetry/report?range=all")
        gaps = client.get("/api/diagnostics/emulation-gaps")

    assert summary.status_code == 200
    assert summary.json()["cloud_requests"]["unknown_requests"] == 1
    assert summary.json()["error_groups"]["volume_safety_failed"] >= 1
    assert json_export.status_code == 200
    assert "secret" not in json_export.text
    assert csv_export.status_code == 200
    assert "top_path" in csv_export.text
    assert report.status_code == 200
    assert "Telemetry Report" in report.text
    assert gaps.status_code == 200
    assert gaps.json()["status"] == "problem"


def test_retention_cleanup_dry_run_and_run_keep_current_data():
    db = app_db.SessionLocal()
    old = datetime.now(UTC) - timedelta(days=90)
    now = datetime.now(UTC)
    db.add(RequestLog(ts=old, direction="in", service="cloud", method="GET", path="/old", status_code=200))
    db.add(RequestLog(ts=now, direction="in", service="cloud", method="GET", path="/new", status_code=200))
    db.add(TelemetryEvent(ts=old, event_type="old"))
    db.add(TelemetryEvent(ts=now, event_type="new"))
    for index in range(105):
        db.add(ConfigBackup(device_id="RADIO1", path=f"backup/{index}", content="x", created_at=now - timedelta(minutes=index)))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        dry = client.post("/api/maintenance/cleanup/dry-run")
        run = client.post("/api/maintenance/cleanup/run")
        storage = client.get("/api/maintenance/storage")

    assert dry.status_code == 200
    assert dry.json()["request_logs"] == 1
    assert dry.json()["telemetry_events"] == 1
    assert dry.json()["config_backups"] == 5
    assert run.status_code == 200
    assert run.json()["request_logs"] == 1
    assert run.json()["telemetry_events"] == 1
    assert run.json()["config_backups"] == 5
    assert storage.json()["request_log_count"] == 1
    assert storage.json()["telemetry_count"] == 1
    assert storage.json()["config_backup_count"] == 100


def test_six_hour_gap_analysis_detects_candidate():
    db = app_db.SessionLocal()
    now = datetime.now(UTC)
    db.add(RequestLog(ts=now - timedelta(hours=8), direction="in", service="streaming", method="POST", path="/streaming/support/power_on", host="streaming.bose.com", status_code=200))
    db.add(RequestLog(ts=now, direction="in", service="streaming", method="GET", path="/streaming/account/abc/full", host="streaming.bose.com", status_code=200))
    db.add(RequestLog(ts=now, direction="in", service="streaming", method="GET", path="/streaming/account/abc/provider_settings", host="streaming.bose.com", status_code=200))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        summary = client.get("/api/diagnostics/telemetry/summary?range=all").json()

    heartbeat = summary["heartbeat_analysis"]
    assert heartbeat["six_hour_gap_candidate"] is True
    assert heartbeat["longest_gap_seconds"] >= 6 * 60 * 60
    assert heartbeat["power_on_events"] == 1
    assert heartbeat["account_sync_events"] == 1
    assert heartbeat["provider_settings_requests"] == 1


def test_playback_keepalive_reads_only_and_keeps_true_24_hour_duration():
    db = app_db.SessionLocal()
    db.add(Device(device_id="KEEP1", name="Keepalive", ip_address="192.0.2.50"))
    started = datetime.now(UTC) - timedelta(hours=24)
    db.add(RuntimeState(key="device:KEEP1:runtime_state", value=json.dumps({"playback_keepalive": {"playing": True, "playback_started_at": started.isoformat()}})))
    db.commit()
    calls = []

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            calls.append(("GET", endpoint))
            if endpoint == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="x"/></nowPlaying>'
            if endpoint == "/volume":
                return "<volume><actualvolume>5</actualvolume></volume>"
            return f"<{endpoint.strip('/')}/>"

        async def post_xml(self, endpoint, xml):
            raise AssertionError("keepalive must not write")

    result = asyncio.run(run_playback_keepalive_once(db, client_factory=Client))
    events = db.query(TelemetryEvent).filter(TelemetryEvent.device_id == "KEEP1").all()
    db.close()

    assert result[0]["ok"] is True
    assert result[0]["playback_observation_status"] == "playing_observed"
    assert result[0]["duration_seconds"] >= 24 * 60 * 60
    assert calls == [
        ("GET", "/now_playing"),
        ("GET", "/volume"),
    ]
    assert not {event.event_type for event in events} & {"six_hour_protection_refresh", "six_hour_protection_ok"}


def test_playback_keepalive_records_stop_without_assuming_six_hour_cause():
    db = app_db.SessionLocal()
    db.add(Device(device_id="KEEPSTOP", name="Keep Stop", ip_address="192.0.2.51"))
    started = datetime.now(UTC) - timedelta(hours=6)
    db.add(RuntimeState(key="device:KEEPSTOP:runtime_state", value=json.dumps({"playback_keepalive": {"playing": True, "playback_started_at": started.isoformat()}})))
    db.commit()

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            if endpoint == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            return "<volume><actualvolume>5</actualvolume></volume>"

        async def post_xml(self, endpoint, xml):
            raise AssertionError("keepalive must not write")

    asyncio.run(run_playback_keepalive_once(db, client_factory=Client))
    event = db.query(TelemetryEvent).filter(TelemetryEvent.device_id == "KEEPSTOP", TelemetryEvent.event_type == "playback_stop_observed").one()
    runtime = json.loads(db.query(RuntimeState).filter(RuntimeState.key == "device:KEEPSTOP:runtime_state").one().value)
    db.close()

    assert "previous_playback_seconds=" in event.parsed_summary
    assert "restriction_state=UNKNOWN" in event.parsed_summary
    assert runtime["playback_keepalive"]["playback_observation_status"] == "stop_observed"


def test_playback_keepalive_invalid_source_records_evidence_without_write():
    db = app_db.SessionLocal()
    db.add(Device(device_id="KEEPRECOVER", name="Recover", ip_address="192.0.2.53"))
    db.add(RuntimeState(key="device:KEEPRECOVER:runtime_state", value=json.dumps({"playback_keepalive": {"playing": True, "last_source": "LOCAL_INTERNET_RADIO", "last_preset_slot": 3, "playback_started_at": datetime.now(UTC).isoformat()}})))
    db.commit()
    posts = []

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            if endpoint == "/now_playing":
                return '<nowPlaying source="INVALID_SOURCE"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            return "<volume><actualvolume>5</actualvolume></volume>"

        async def post_xml(self, endpoint, xml):
            posts.append((endpoint, xml))
            return "<ok/>"

    result = asyncio.run(run_playback_keepalive_once(db, client_factory=Client))
    runtime = json.loads(db.query(RuntimeState).filter(RuntimeState.key == "device:KEEPRECOVER:runtime_state").one().value)
    diagnostics = db.query(DiagnosticEvent).filter(DiagnosticEvent.device_id == "KEEPRECOVER").all()
    db.close()

    assert result[0]["ok"] is True
    assert posts == []
    assert result[0]["invalid_source_action"] == "NONE"
    assert runtime["playback_keepalive"]["invalid_source_automatic_action"] == "NONE"
    assert runtime["playback_keepalive"]["playback_observation_status"] == "invalid_source_diagnosis_required"
    assert runtime["playback_keepalive"]["playback_session"]["retry_count"] == 0
    assert [event.code for event in diagnostics] == [
        "INVALID_SOURCE_OBSERVED",
        "PROVIDER_SOURCE_INVALID",
        "PLAYBACK_FAILED",
    ]
    assert runtime["playback_keepalive"]["invalid_source_cause"] == "UNKNOWN"
    assert runtime["playback_keepalive"]["invalid_source_confidence"] == 0


def test_playback_keepalive_manual_stop_still_never_writes_for_invalid_source():
    db = app_db.SessionLocal()
    db.add(Device(device_id="KEEPMANUAL", name="Manual", ip_address="192.0.2.54"))
    db.add(RuntimeState(key="device:KEEPMANUAL:runtime_state", value=json.dumps({"playback_keepalive": {"playing": True, "manual_stop": True, "last_source": "LOCAL_INTERNET_RADIO", "playback_started_at": datetime.now(UTC).isoformat()}})))
    db.commit()
    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            if endpoint == "/now_playing":
                return '<nowPlaying source="INVALID_SOURCE"><playStatus>STOP_STATE</playStatus></nowPlaying>'
            return "<volume><actualvolume>5</actualvolume></volume>"

        async def post_xml(self, endpoint, xml):
            raise AssertionError("manual stop must block recovery")

    result = asyncio.run(run_playback_keepalive_once(db, client_factory=Client))
    db.close()

    assert result[0]["ok"] is True
    assert result[0]["invalid_source_action"] == "NONE"


def test_playback_keepalive_creates_runtime_state_once_when_repeated():
    db = app_db.SessionLocal()
    db.add(Device(device_id="KEEPNEW", name="Keep New", ip_address="192.0.2.52"))
    db.commit()

    class Client:
        def __init__(self, ip):
            self.ip = ip

        async def get_xml(self, endpoint):
            if endpoint == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="x"/></nowPlaying>'
            return "<volume><actualvolume>5</actualvolume></volume>"

        async def post_xml(self, endpoint, xml):
            raise AssertionError("keepalive must not write")

    first = asyncio.run(run_playback_keepalive_once(db, client_factory=Client))
    second = asyncio.run(run_playback_keepalive_once(db, client_factory=Client))
    rows = db.query(RuntimeState).filter(RuntimeState.key == "device:KEEPNEW:runtime_state").all()
    payload = json.loads(rows[0].value)
    db.close()

    assert first[0]["ok"] is True
    assert second[0]["ok"] is True
    assert len(rows) == 1
    assert payload["current_source"] == "LOCAL_INTERNET_RADIO"
    assert payload["playback_keepalive"]["consecutive_failures"] == 0


def test_playback_keepalive_settings_defaults_exist():
    settings = get_settings()
    assert settings.playback_keepalive_enabled is True
    assert settings.playback_keepalive_interval_seconds == 300
    assert settings.playback_keepalive_log_every_seconds == 1800
    assert settings.enable_https is False
    assert settings.https_port == 1329


def test_system_health_reports_https_disabled_by_default():
    with TestClient(create_web_app()) as client:
        version = client.get("/api/version")
        readiness = client.get("/api/readiness")
        health = client.get("/api/system/healthcheck").json()

    assert version.status_code == 200
    assert version.json()["version"] == "2.5.0"
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True
    assert readiness.json()["version"] == "2.5.0"
    check = next(item for item in health["checks"] if item["name"] == "https_status")
    assert check["status"] == "green"
    assert "optional HTTPS disabled" in check["message"]


def test_release_polish_ui_and_translation_keys_exist():
    with TestClient(create_web_app()) as client:
        html = client.get("/").text
    js = open("basswiesn/app/static/app.js", encoding="utf-8").read()
    translations = open("basswiesn/app/static/js/translations.js", encoding="utf-8").read()

    assert "Telemetry Analyse" in js
    assert "6h Protection" in js
    assert "Emulation Gap Report" in js
    assert "Speicher & Cleanup" in js
    assert "state.presetFilter = \"\";" in js
    assert "select.value = String(selectedStationId);" in js
    assert "Sender konnte nicht hinzugefügt werden" in js
    assert "data-multiroom-remove-device" in js
    assert "dry_run: false" in js
    for key in ["telemetry_analysis", "download_json", "emulation_gaps", "storage_cleanup", "run_cleanup"]:
        assert key in translations
    assert "SETUP AUSGEWÄHLTE RADIOS STARTEN" in html
    assert 'setup-wizard-shell setup-locked lab-only' in html
    assert 'id="setup-rebuild-assistant"' in html
    assert "SSH im normalen Setup nicht erforderlich" in html
    assert "SSH-Zugriff ohne USB-Stick" not in html
    assert "Source label plan <span" in html


def test_translation_catalogs_cover_all_languages_and_about_has_clean_names():
    import re
    import subprocess

    script = """
global.window = {};
global.console = { warn() {}, log: (...args) => process.stdout.write(args.join(' ') + '\\n'), error() {} };
require('./basswiesn/app/static/js/translations.js');
const i18n = window.BasswiesnI18n;
const languages = i18n.languages;
const keys = i18n.keys;
const missing = {};
const phraseProbe = [
  'Device Settings',
  'WebGUI defaults',
  'SETUP AUSGEWÄHLTE RADIOS STARTEN',
  'Add radio',
  'Current Slots',
  'Gerät per Telnet neu starten',
  'BatteryMonitor Patch / Portable Battery'
];
const firstRunProbe = ['first_run_title', 'first_run_p1', 'first_run_read', 'first_run_ack'];
const phraseTranslations = {};
const firstRunTranslations = {};
for (const lang of languages) {
  missing[lang] = keys.filter((key) => !i18n.catalogs[lang][key]);
  i18n.setLanguage(lang);
  phraseTranslations[lang] = phraseProbe.map((text) => i18n.phrase(text));
  firstRunTranslations[lang] = firstRunProbe.map((key) => i18n.catalogs[lang][key]);
}
console.log(JSON.stringify({languages, keys, missing, phraseProbe, phraseTranslations, firstRunTranslations}));
"""
    result = subprocess.run(["node", "-e", script], cwd=".", text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)
    assert len(data["languages"]) == 25
    assert all(not values for values in data["missing"].values())
    for lang in data["languages"]:
        if lang == "en":
            continue
        assert data["phraseTranslations"][lang] != data["phraseTranslations"]["en"]
        for index, value in enumerate(data["phraseTranslations"][lang]):
            if lang == "de" and data["phraseProbe"][index] in {
                "SETUP AUSGEWÄHLTE RADIOS STARTEN",
                "Gerät per Telnet neu starten",
            }:
                continue
            assert value != data["phraseProbe"][index]
        assert data["firstRunTranslations"][lang] != data["firstRunTranslations"]["en"]

    js = open("basswiesn/app/static/app.js", encoding="utf-8").read()
    translations = open("basswiesn/app/static/js/translations.js", encoding="utf-8").read()
    assert "const aboutKeys" not in translations
    assert "const aboutRows" not in translations
    about_segment = js[js.index("const ABOUT_COPY ="):js.index("const FIRST_RUN_COPY =")]
    for lang in data["languages"]:
        assert f"\n  {lang}: {{" in about_segment
        lang_start = about_segment.index(f"\n  {lang}: {{")
        next_lang = re.search(r"\n  [a-z]{2}: \{", about_segment[lang_start + 1 :])
        lang_end = lang_start + 1 + next_lang.start() if next_lang else len(about_segment)
        lang_section = about_segment[lang_start:lang_end]
        assert "paragraphs: [" in lang_section
        assert "400" in lang_section
        assert "Raspberry Pi 5" in lang_section
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.integration, _pytest_marker.mark.release]
