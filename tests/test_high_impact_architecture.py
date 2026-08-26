from io import BytesIO
import zipfile

from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_cloud_app, create_web_app
from basswiesn.app.models import Device, Preset, ProviderHealthState, RequestLog
from basswiesn.app.models import RuntimeState, Setting, Station
from basswiesn.app.services import device_state, diagnostics
from basswiesn.app.services.capabilities import capability_flags
from basswiesn.app.services.orion import ORION_STATION_PATH
from basswiesn.app.routers.shared import enforce_ip_write_guard
from basswiesn.app.routers import api
from basswiesn.app.routers import stations_presets


class FakeRadioClient:
    def __init__(self, _ip_address: str):
        pass

    async def get_xml(self, path: str) -> str:
        rows = {
            "/info": '<info deviceID="TEST"><name>Radio</name><ip>192.168.1.44</ip></info>',
            "/capabilities": '<capabilities><supportedURL>/bass</supportedURL><supportedURL>/getZone</supportedURL></capabilities>',
            "/sources": '<sources><source source="AUX" status="READY"/><source source="LOCAL_INTERNET_RADIO" status="READY"/></sources>',
            "/presets": '<presets><preset id="1"><ContentItem source="LOCAL_INTERNET_RADIO" location="station:1"/></preset></presets>',
            "/now_playing": '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="station:1"/></nowPlaying>',
            "/volume": '<volume><actualvolume>30</actualvolume></volume>',
            "/bass": '<bass><actualbass>-3</actualbass><targetbass>-3</targetbass></bass>',
            "/language": '<sysLanguage>2</sysLanguage>',
            "/clockDisplay": '<clockDisplay><clockConfig userEnable="true"/></clockDisplay>',
            "/systemtimeout": '<systemtimeout>20</systemtimeout>',
            "/serviceAvailability": '<serviceAvailability><service service="LOCAL_INTERNET_RADIO" status="READY"/></serviceAvailability>',
            "/marge": '<marge><credential>secret-value</credential></marge>',
        }
        return rows[path]


def test_capability_ui_and_state_are_device_driven(monkeypatch):
    monkeypatch.setattr(device_state, "SoundTouchClient", FakeRadioClient)
    db = app_db.SessionLocal()
    db.add(Device(device_id="TEST", ip_address="192.168.1.44", name="Radio", capabilities_xml='<capabilities><url>/bass</url><url>/getZone</url></capabilities>'))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        capabilities = client.get("/api/devices/ui-capabilities").json()[0]
        state = client.get("/api/devices/TEST/state").json()

    assert capabilities["features"]["dsp"] is True
    assert capabilities["features"]["zone"] is True
    assert capabilities["features"]["battery"] is False
    assert state["settings_state"]["bass"] == -3
    assert state["runtime_state"]["current_preset"] == 1
    assert state["runtime_state"]["provider_initialized"] is True
    db = app_db.SessionLocal()
    provider_health = (
        db.query(ProviderHealthState)
        .filter(
            ProviderHealthState.device_id == "TEST",
            ProviderHealthState.provider_id == "LOCAL_INTERNET_RADIO",
        )
        .one()
    )
    db.close()
    assert provider_health.state == "HEALTHY"
    assert provider_health.availability == "AVAILABLE"


def test_support_bundle_contains_required_sanitized_files(monkeypatch):
    monkeypatch.setattr(device_state, "SoundTouchClient", FakeRadioClient)
    monkeypatch.setattr(diagnostics, "SoundTouchClient", FakeRadioClient)
    db = app_db.SessionLocal()
    db.add(Device(device_id="TEST", ip_address="192.168.1.44", name="Radio"))
    db.add(RequestLog(direction="in", service="cloud", method="GET", path="/token/secret-value", host="192.168.1.44", status_code=200))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.get("/api/devices/TEST/support-bundle")

    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"info.xml", "capabilities.xml", "sources.xml", "presets.xml", "now_playing.xml", "volume.xml", "bass.xml", "marge.xml", "serviceAvailability.xml", "runtime_state.json", "provider_state.json", "sanitized_config.json", "request_log.txt", "masterlog_tail.txt"} <= names
        combined = b"\n".join(archive.read(name) for name in names).decode("utf-8")
    assert "192.168.1.44" not in combined
    assert "secret-value" not in combined


def test_provider_manifest_drives_discovery():
    with TestClient(create_cloud_app()) as client:
        discovery = client.get("/bmx/registry/v1/introspect").json()["services"]
        availability = client.get("/bmx/registry/v1/servicesAvailability").json()["services"]
    local = next(item for item in discovery if item["name"] == "LOCAL_INTERNET_RADIO")
    assert local["provider_id"] == 11
    assert local["adapter"] == "orion"
    assert {item["service"] for item in availability} == {item["name"] for item in discovery if item["visible"]}


def test_ip_write_guard_off_block_and_allow():
    db = app_db.SessionLocal()
    device = Device(device_id="GUARD", ip_address="192.0.2.40")
    db.add(device)
    db.commit()
    enforce_ip_write_guard(db, device)
    db.add(Setting(key="ip_write_guard", value="true"))
    db.commit()
    try:
        enforce_ip_write_guard(db, device)
        assert False, "guard should block an unknown IP"
    except Exception as exc:
        assert exc.status_code == 403
        assert "IP Write Guard blockiert" in exc.detail
    db.add(Setting(key="ip_write_allowed_ips", value="192.0.2.40"))
    db.commit()
    enforce_ip_write_guard(db, device)
    db.close()


def test_capability_parser_handles_model_variants_and_bad_xml():
    st20, known = capability_flags('<capabilities><capability name="bass"/><supportedURL>/getZone</supportedURL></capabilities>')
    portable, _ = capability_flags('<capabilities><supportedURL>/powerManagement</supportedURL><supportedURL>/clockDisplay</supportedURL></capabilities>')
    empty, empty_known = capability_flags("")
    recovered, recovered_known = capability_flags("<capabilities><supportedURL>/powerManagement")
    assert known and st20["dsp"] and st20["zone"] and not st20["hdmi"]
    assert portable["battery"] is False and portable["clockDisplay"]
    assert not empty_known and empty["battery"] is None
    assert recovered_known and recovered["battery"] is False


def test_select_preset_updates_runtime_only_after_success(monkeypatch):
    class Client(FakeRadioClient):
        async def post_xml(self, _path, _xml, headers=None):
            return "<status>OK</status>"

    monkeypatch.setattr(api, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    db.add(Device(device_id="PLAY", ip_address="192.0.2.50", name="Radio"))
    db.commit()
    db.close()
    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices/PLAY/key", json={"key": "PRESET_1", "dry_run": False})
    assert response.status_code == 200
    db = app_db.SessionLocal()
    row = db.query(RuntimeState).filter(RuntimeState.key == "device:PLAY:runtime_state").one()
    assert '"current_preset": 1' in row.value
    db.close()


def test_preset_key_falls_back_to_select_when_key_does_not_start_audio(monkeypatch):
    calls = []

    class Client(FakeRadioClient):
        def __init__(self, _ip_address: str):
            self.now_reads = 0

        async def get_xml(self, path: str) -> str:
            if path == "/sources":
                return '<sources><source source="TUNEIN" status="READY"/></sources>'
            if path == "/now_playing":
                self.now_reads += 1
                if self.now_reads < 6:
                    return '<nowPlaying source="STANDBY"><playStatus>STOP_STATE</playStatus></nowPlaying>'
                return '<nowPlaying source="TUNEIN"><playStatus>PLAY_STATE</playStatus><ContentItem source="TUNEIN" location="http://cloud.test/station/1"/></nowPlaying>'
            return await super().get_xml(path)

        async def post_xml(self, path, xml, headers=None):
            calls.append((path, xml))
            return "<status>OK</status>"

    monkeypatch.setattr(api, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    db.add(Device(device_id="PLAYSELECT", ip_address="192.0.2.51", name="Radio"))
    db.add(Preset(
        device_id="PLAYSELECT",
        button=1,
        source="TUNEIN",
        location="http://cloud.test/station/1",
        content_item_xml='<ContentItem source="TUNEIN" type="stationurl" location="http://cloud.test/station/1"><itemName>Radio</itemName></ContentItem>',
    ))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices/PLAYSELECT/key", json={"key": "PRESET_1", "dry_run": False})

    assert response.status_code == 200
    assert response.json()["select_fallback"]["used"] is True
    assert any(path == "/select" for path, _xml in calls)


def test_preset_key_does_not_rewrite_or_fail_when_sources_unavailable(monkeypatch):
    calls = []

    class Client(FakeRadioClient):
        async def get_xml(self, path: str) -> str:
            if path == "/sources":
                raise OSError("sources unavailable")
            if path == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="http://cloud.test/station/1"/></nowPlaying>'
            return await super().get_xml(path)

        async def post_xml(self, path, xml, headers=None):
            calls.append((path, xml))
            return "<status>OK</status>"

    monkeypatch.setattr(api, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    db.add(Device(device_id="PLAYNOSOURCES", ip_address="192.0.2.52", name="Radio"))
    db.add(Preset(
        device_id="PLAYNOSOURCES",
        button=1,
        source="LOCAL_INTERNET_RADIO",
        location="http://cloud.test/station/1",
        content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/station/1"><itemName>Radio</itemName></ContentItem>',
    ))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        response = client.post("/api/devices/PLAYNOSOURCES/key", json={"key": "PRESET_1", "dry_run": False})

    assert response.status_code == 200
    payload = response.json()
    assert payload["preset_rewrite"]["rewritten"] is False
    assert not any(path == "/storePreset" for path, _xml in calls)


def test_radio_browser_stream_rank_prefers_direct_mp3_over_playlists():
    urls = [
        "https://example.test/live.m3u8",
        "https://example.test/live.aac",
        "https://example.test/live.ogg",
        "http://example.test/live.mp3",
        "http://example.test/live",
    ]

    assert sorted(urls, key=api._radio_browser_stream_rank) == [
        "http://example.test/live.mp3",
        "https://example.test/live.aac",
        "https://example.test/live.ogg",
        "http://example.test/live",
        "https://example.test/live.m3u8",
    ]


def test_provider_status_aggregates_registry_sources_and_availability(monkeypatch):
    monkeypatch.setattr(device_state, "SoundTouchClient", FakeRadioClient)
    db = app_db.SessionLocal()
    db.add(Device(device_id="PROVIDERS", ip_address="192.0.2.60", name="Radio"))
    db.commit()
    db.close()
    with TestClient(create_web_app()) as client:
        response = client.get("/api/devices/PROVIDERS/provider-status")
    assert response.status_code == 200
    local = next(item for item in response.json()["providers"] if item["name"] == "LOCAL_INTERNET_RADIO")
    assert local["registered"] is True
    assert local["available"] is True
    assert local["ready"] is True


def test_preset_checker_returns_both_sides_and_changed_fields(monkeypatch):
    class Client:
        def __init__(self, _ip): pass
        async def get_xml(self, path):
            assert path == "/presets"
            return '<presets><preset id="1"><ContentItem source="TUNEIN" location="radio:1"><itemName>Radio title</itemName></ContentItem></preset></presets>'
    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Local title", stream_url="http://example.test/live", provider="LOCAL_INTERNET_RADIO")
    db.add_all([
        Device(device_id="CHECK", ip_address="192.0.2.70", name="Radio"),
        station,
        Setting(key="lan_host", value="192.168.50.77"),
    ])
    db.flush()
    from basswiesn.app.models import Preset
    db.add(Preset(device_id="CHECK", button=1, station_id=station.id, source="LOCAL_INTERNET_RADIO", location="local:1", content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" location="local:1"><itemName>Local title</itemName></ContentItem>'))
    db.commit()
    db.close()
    with TestClient(create_web_app()) as client:
        # Radio access is explicit; passive status rendering is DB-only.
        response = client.get("/api/presets/CHECK/status?probe=true")
        preview = client.post("/api/presets/CHECK/sync", json={"dry_run": True})
    slot = response.json()["slots"][0]
    assert slot["verdict"] == "BROKEN"
    assert slot["state"] == "broken"
    assert slot["radio"]["title"] == "Radio title"
    assert slot["basswiesn"]["title"] == "Local title"
    assert {"source", "location", "xml"} <= set(slot["changed_fields"])
    assert preview.json()["expected_slots"]["1"].startswith(f"http://192.168.50.77:1516{ORION_STATION_PATH}?data=")


def test_system_settings_expose_all_25_web_languages():
    with TestClient(create_web_app()) as client:
        response = client.get("/api/system/settings")
    assert response.status_code == 200
    assert len(response.json()["web_languages"]) == 25


def test_source_select_updates_runtime_state(monkeypatch):
    class Client:
        def __init__(self, _ip): pass
        async def post_xml(self, _path, _xml, headers=None): return "<status>OK</status>"
        async def get_xml(self, path):
            if path == "/now_playing":
                return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'
            if path == "/sources":
                return '<sources><source source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'
            return "<ok/>"
    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Runtime Radio", stream_url="http://example.test/runtime.mp3", provider="LOCAL_INTERNET_RADIO")
    db.add_all([Device(device_id="SOURCE", ip_address="192.0.2.80", name="Radio"), station, Setting(key="lan_host", value="192.168.50.77")])
    db.commit(); station_id = station.id; db.close()
    with TestClient(create_web_app()) as client:
        response = client.post(f"/api/devices/SOURCE/stations/{station_id}/play", json={"dry_run": False})
    assert response.status_code == 200
    db = app_db.SessionLocal()
    row = db.query(RuntimeState).filter(RuntimeState.key == "device:SOURCE:runtime_state").one()
    assert '"current_source": "LOCAL_INTERNET_RADIO"' in row.value
    db.close()


def test_failed_preset_write_does_not_create_playing_runtime_state(monkeypatch):
    class Client:
        def __init__(self, _ip): pass
        async def get_xml(self, _path): return "<presets/>"
        async def post_xml(self, _path, _xml, headers=None): raise OSError("radio timeout")
    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    station = Station(name="Fail Radio", stream_url="http://example.test/fail.mp3", provider="LOCAL_INTERNET_RADIO")
    db.add_all([Device(device_id="FAIL", ip_address="192.0.2.81", name="Radio"), station, Setting(key="lan_host", value="192.168.50.77")])
    db.commit(); station_id = station.id; db.close()
    with TestClient(create_web_app()) as client:
        response = client.post("/api/presets/FAIL/1", json={"station_id": station_id, "dry_run": False, "memory_checked": True})
    assert response.status_code == 502
    db = app_db.SessionLocal()
    runtime = db.query(RuntimeState).filter(RuntimeState.key == "device:FAIL:runtime_state").one_or_none()
    assert runtime is None or '"playback_state": "selected"' not in runtime.value
    assert db.query(Preset).filter(Preset.device_id == "FAIL", Preset.button == 1).count() == 0
    mutation = db.query(stations_presets.PresetMutation).filter(
        stations_presets.PresetMutation.device_id == "FAIL",
        stations_presets.PresetMutation.button == 1,
    ).one()
    # The atomic preset contract retains an uncertain radio write for explicit
    # reconciliation instead of pretending the pre-write state is known.
    assert mutation.state == "RECONCILE"
    assert mutation.diverged is True
    db.close()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
