from datetime import UTC, datetime

from fastapi.testclient import TestClient
from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, PlayHistory, Preset, Station
from basswiesn.app.routers.multiroom import split_csv
from basswiesn.app.routers import api
from basswiesn.app.services.orion import ORION_STATION_PATH, StationDescriptor, encode_orion_data


def test_play_history_and_stats_handle_minimal_old_rows():
    db = app_db.SessionLocal()
    db.add(Device(device_id="OLDHIST", ip_address="192.0.2.96", name="Old Radio"))
    db.add(PlayHistory(device_id="OLDHIST"))
    db.add(PlayHistory(device_id="", device_name="", station_name="", stream_url="", source=""))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        history = client.get("/api/play-history")
        raw_history = client.get("/api/play-history?include_internal=true")
        stats = client.get("/api/stats/playback")

    assert history.status_code == 200
    assert raw_history.status_code == 200
    assert stats.status_code == 200
    assert history.json() == []
    assert len(raw_history.json()) >= 2
    assert "today" in stats.json()
    assert "lifetime" in stats.json()
    assert "server" in stats.json()
    assert api._split_csv(None) == []


def test_play_history_start_accepts_string_zone_member_ids_and_bad_station_id():
    db = app_db.SessionLocal()
    db.add(Device(device_id="NEWHIST", ip_address="192.0.2.97", name="New Radio"))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        observed = datetime.now(UTC).isoformat()
        started = client.post("/api/play-history/start", json={"device_id": "NEWHIST", "station_id": "bad", "zone_member_ids": "A,B", "source": "PRESET", "source_type": "PRESET", "trigger": "preset_3", "preset_button": 3, "preset_name": "Preset 3", "volume": 25, "play_status": "PLAY_STATE", "state_observed_at": observed})
        history = client.get("/api/play-history")
        stats = client.get("/api/stats/playback")

    assert started.status_code == 200
    assert history.status_code == 200
    assert started.json()["opened"] is True
    assert history.json()[0]["trigger_type"] == "preset"
    assert history.json()[0]["source_type"] == "PRESET"
    assert "aggregate" in stats.json()
    assert "top_triggers" in stats.json()
    assert "server" in stats.json()


def test_stats_keep_device_id_when_radio_is_renamed():
    db = app_db.SessionLocal()
    db.add(Device(device_id="RENAMED", ip_address="192.0.2.98", name="Current Name"))
    db.add(PlayHistory(device_id="RENAMED", device_name="Old Snapshot", station_name="Station", volume=25))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        stats = client.get("/api/stats/playback")

    assert stats.status_code == 200
    row = next(item for item in stats.json()["by_device"] if item["device_id"] == "RENAMED")
    assert row["device_name"] == "Current Name"
    assert row["current_device_name"] == "Current Name"
    assert row["device_name_snapshot"] == "Old Snapshot"
    assert row["volume_last"] == 25


def test_stats_include_20_year_history_and_removed_devices():
    db = app_db.SessionLocal()
    db.add(PlayHistory(device_id="REMOVEDRADIO", device_name="Removed Snapshot", device_ip="192.0.2.150", station_name="Old Station", started_at=datetime(2010, 5, 1, tzinfo=UTC)))
    db.add(PlayHistory(device_id="REMOVEDRADIO", device_name="Renamed Snapshot", device_ip="192.0.2.151", station_name="Old Station", started_at=datetime(2026, 5, 1, tzinfo=UTC)))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        stats = client.get("/api/stats/playback")

    assert stats.status_code == 200
    body = stats.json()
    years = {item["year"] for item in body["yearly"]}
    assert 2010 in years
    assert 2026 in years
    removed = next(item for item in body["device_history"]["removed"] if item["device_id"] == "REMOVEDRADIO")
    assert removed["linked"] is False
    assert removed["known_names"] == ["Removed Snapshot", "Renamed Snapshot"]
    assert removed["known_ips"] == ["192.0.2.150", "192.0.2.151"]


def test_stats_resolve_unknown_sender_from_local_station_url():
    db = app_db.SessionLocal()
    station = Station(
        name="Bayern 3",
        stream_url="http://stream.example.test/bayern3.mp3",
        stream_url_resolved="http://edge.example.test/live/bayern3.mp3",
        provider_station_id="br-bayern3",
    )
    db.add(station)
    db.flush()
    station_id = station.id
    db.add(
        PlayHistory(
            device_id="URLSTATS",
            device_name="Kitchen",
            station_display_name="Unbekannter Sender",
            station_name="",
            station_id=None,
            stream_url="http://edge.example.test/live/bayern3.mp3",
            source_account="br-bayern3",
            started_at=datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
            ended_at=datetime(2026, 7, 27, 8, 30, tzinfo=UTC),
        )
    )
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        stats = client.get("/api/stats/playback")
        history = client.get("/api/play-history")

    assert stats.status_code == 200
    station_row = next(item for item in stats.json()["by_station"] if item["station_display_name"] == "Bayern 3")
    assert station_row["station_id"] == station_id
    assert station_row["identity_source"] in {"station_stream_url", "station_source_account"}
    history_row = next(item for item in history.json() if item["device_id"] == "URLSTATS")
    assert history_row["station_display_name"] == "Bayern 3"
    assert history_row["station_id"] == station_id


def test_stats_do_not_let_unknown_display_name_hide_snapshot_station_name():
    db = app_db.SessionLocal()
    db.add(
        PlayHistory(
            device_id="SNAPSHOTSTATS",
            device_name="Wohnzimmer",
            station_display_name="Unbekannter Sender",
            station_name="Radio Eins",
            stream_url="",
            started_at=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 7, 27, 10, 20, tzinfo=UTC),
        )
    )
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        stats = client.get("/api/stats/playback")
        history = client.get("/api/play-history")

    assert stats.status_code == 200
    assert next(item for item in stats.json()["by_station"] if item["station_display_name"] == "Radio Eins")
    assert next(item for item in stats.json()["by_device"] if item["device_id"] == "SNAPSHOTSTATS")["last_source"] == "Radio Eins"
    assert history.json()[0]["station_display_name"] == "Radio Eins"
    assert history.json()[0]["identity_source"] == "snapshot"


def test_stats_resolve_unknown_sender_from_orion_location():
    db = app_db.SessionLocal()
    station = Station(name="Orion Radio", stream_url="http://radio.example.test/live.aac")
    db.add(station)
    db.flush()
    station_id = station.id
    descriptor = StationDescriptor(name=station.name, stream_url=station.stream_url)
    orion_location = f"http://192.0.2.10:1516{ORION_STATION_PATH}?data={encode_orion_data(descriptor)}"
    db.add(
        PlayHistory(
            device_id="ORIONSTATS",
            device_name="Office",
            station_display_name="Unbekannter Sender",
            stream_url=orion_location,
            started_at=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 7, 27, 9, 5, tzinfo=UTC),
        )
    )
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        stats = client.get("/api/stats/playback")

    assert stats.status_code == 200
    station_row = next(item for item in stats.json()["by_station"] if item["station_display_name"] == "Orion Radio")
    assert station_row["station_id"] == station_id
    assert station_row["identity_source"] == "station_stream_url"


def test_multiroom_recent_stations_tolerates_old_history_and_presets():
    db = app_db.SessionLocal()
    station = Station(name="Recent Radio", stream_url="http://example.test/recent.mp3")
    db.add_all([
        station,
        PlayHistory(device_id="OLDMULTI"),
        PlayHistory(device_id="OLDMULTI", station_id=99999),
    ])
    db.flush()
    db.add(Preset(device_id="OLDMULTI", button=1, station_id=station.id))
    db.commit()
    db.close()

    with TestClient(create_web_app()) as client:
        methods = client.get("/api/multiroom/methods")
        recent = client.get("/api/multiroom/recent-stations")
        schedules = client.get("/api/schedules")

    assert methods.status_code == 200
    assert recent.status_code == 200
    assert schedules.status_code == 200
    assert recent.json()[0]["name"] == "Recent Radio"
    assert split_csv(None) == []
    assert split_csv(["A", "", "B"]) == ["A", "B"]


def test_stop_key_closes_active_play_history(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/key"
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            assert path == "/now_playing"
            return '<nowPlaying source="STANDBY"><playStatus>STOP_STATE</playStatus></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "STOPHIST", "name": "Stop History", "ip_address": "192.0.2.52", "model": "SoundTouch Test"})
        started = client.post("/api/play-history/start", json={"device_id": "STOPHIST", "trigger": "manual", "source": "LOCAL_INTERNET_RADIO", "play_status": "PLAY_STATE", "state_observed_at": datetime.now(UTC).isoformat()})
        assert started.status_code == 200
        stopped = client.post("/api/devices/STOPHIST/key", json={"key": "STOP"})
        assert stopped.status_code == 200
        assert stopped.json()["readback_active"] is False
        history = client.get("/api/play-history?include_internal=true").json()

    row = next(item for item in history if item["device_id"] == "STOPHIST")
    assert row["ended_at"] is not None


def test_stop_key_does_not_close_history_when_readback_still_playing(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/key"
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            assert path == "/now_playing"
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><stationName>Bayern 3</stationName></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "STOPFAIL", "name": "Stop Fail", "ip_address": "192.0.2.53", "model": "SoundTouch Test"})
        started = client.post("/api/play-history/start", json={"device_id": "STOPFAIL", "station_name": "Bayern 3", "trigger": "manual", "source": "LOCAL_INTERNET_RADIO", "play_status": "PLAY_STATE", "state_observed_at": datetime.now(UTC).isoformat()})
        assert started.status_code == 200
        stopped = client.post("/api/devices/STOPFAIL/key", json={"key": "STOP"})
        assert stopped.status_code == 502
        history = client.get("/api/play-history").json()

    row = next(item for item in history if item["device_id"] == "STOPFAIL")
    assert row["ended_at"] is None


def test_stop_key_does_not_accept_buffering_as_stopped(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/key"
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            assert path == "/now_playing"
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>BUFFERING_STATE</playStatus><stationName>Bayern 3</stationName></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "STOPBUFFER", "name": "Stop Buffer", "ip_address": "192.0.2.54", "model": "SoundTouch Test"})
        started = client.post("/api/play-history/start", json={"device_id": "STOPBUFFER", "station_name": "Bayern 3", "trigger": "manual", "source": "LOCAL_INTERNET_RADIO", "play_status": "PLAY_STATE", "state_observed_at": datetime.now(UTC).isoformat()})
        assert started.status_code == 200
        stopped = client.post("/api/devices/STOPBUFFER/key", json={"key": "STOP"})
        assert stopped.status_code == 502
        assert "did not confirm" in stopped.text
        history = client.get("/api/play-history").json()

    row = next(item for item in history if item["device_id"] == "STOPBUFFER")
    assert row["ended_at"] is None


def test_pause_key_is_fail_closed_when_radio_keeps_playing(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/key"
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            assert path == "/now_playing"
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><stationName>Radio</stationName></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "PAUSEFAIL", "name": "Pause Fail", "ip_address": "192.0.2.154", "model": "SoundTouch Test"})
        started = client.post("/api/play-history/start", json={"device_id": "PAUSEFAIL", "station_name": "Radio", "trigger": "manual", "source": "LOCAL_INTERNET_RADIO", "play_status": "PLAY_STATE", "state_observed_at": datetime.now(UTC).isoformat()})
        assert started.status_code == 200
        paused = client.post("/api/devices/PAUSEFAIL/key", json={"key": "PAUSE"})
        assert paused.status_code == 502
        assert "spielt laut Readback aber weiter" in paused.text
        history = client.get("/api/play-history").json()

    row = next(item for item in history if item["device_id"] == "PAUSEFAIL")
    assert row["ended_at"] is None


def test_pause_key_closes_history_only_after_confirmed_pause(monkeypatch):
    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            assert path == "/key"
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            assert path == "/now_playing"
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PAUSE_STATE</playStatus></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "PAUSEOK", "name": "Pause OK", "ip_address": "192.0.2.155", "model": "SoundTouch Test"})
        started = client.post("/api/play-history/start", json={"device_id": "PAUSEOK", "station_name": "Radio", "trigger": "manual", "source": "LOCAL_INTERNET_RADIO", "play_status": "PLAY_STATE", "state_observed_at": datetime.now(UTC).isoformat()})
        assert started.status_code == 200
        paused = client.post("/api/devices/PAUSEOK/key", json={"key": "PAUSE"})
        assert paused.status_code == 200
        assert paused.json()["readback"]["playback_state"] == "PAUSE_STATE"
        history = client.get("/api/play-history?include_internal=true").json()

    row = next(item for item in history if item["device_id"] == "PAUSEOK")
    assert row["ended_at"] is not None


def test_preset_key_confirms_safe_volume_before_audio_command(monkeypatch):
    calls = []

    class Client:
        volume = 12

        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append(("post", path, body))
            if path == "/volume":
                Client.volume = int(body.replace("<volume>", "").replace("</volume>", ""))
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            calls.append(("get", path, ""))
            if path == "/volume":
                return f"<volume><actualvolume>{Client.volume}</actualvolume></volume>"
            assert path == "/now_playing"
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>STOP_STATE</playStatus></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "PRESETSAFE", "name": "Preset Safe", "ip_address": "192.0.2.55", "model": "SoundTouch Test"})
        response = client.post("/api/devices/PRESETSAFE/key", json={"key": "PRESET_1", "safe_volume": 1})

    assert response.status_code == 200
    assert response.json()["confirmed_volume"] == 1
    first_key = next(index for index, call in enumerate(calls) if call[0] == "post" and call[1] == "/key")
    first_volume_post = next(index for index, call in enumerate(calls) if call[0] == "post" and call[1] == "/volume")
    assert first_volume_post < first_key


def test_play_key_uses_module_playback_readback_without_local_import_shadow(monkeypatch):
    class Client:
        volume = 1

        def __init__(self, ip_address: str, **_kwargs):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            if path == "/volume":
                Client.volume = int(body.replace("<volume>", "").replace("</volume>", ""))
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            if path == "/volume":
                return f"<volume><actualvolume>{Client.volume}</actualvolume></volume>"
            assert path == "/now_playing"
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post(
            "/api/devices",
            json={
                "device_id": "PLAYREADBACK",
                "name": "Play Readback",
                "ip_address": "192.0.2.157",
                "model": "SoundTouch Test",
            },
        )
        response = client.post(
            "/api/devices/PLAYREADBACK/key",
            json={"key": "PLAY", "safe_volume": 1},
        )

    assert response.status_code == 200
    assert response.json()["readback_active"] is True
    assert response.json()["readback"]["playback_state"] == "PLAY_STATE"


def test_play_key_waits_through_buffering_for_authoritative_play(monkeypatch):
    class Client:
        volume = 1
        now_reads = 0

        def __init__(self, ip_address: str, **_kwargs):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            if path == "/volume":
                Client.volume = int(body.replace("<volume>", "").replace("</volume>", ""))
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            if path == "/volume":
                return f"<volume><actualvolume>{Client.volume}</actualvolume></volume>"
            assert path == "/now_playing"
            Client.now_reads += 1
            state = "BUFFERING_STATE" if Client.now_reads < 3 else "PLAY_STATE"
            return f'<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>{state}</playStatus></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post(
            "/api/devices",
            json={
                "device_id": "PLAYBUFFER",
                "name": "Play Buffer",
                "ip_address": "192.0.2.158",
                "model": "SoundTouch Test",
            },
        )
        response = client.post(
            "/api/devices/PLAYBUFFER/key",
            json={"key": "PLAY", "safe_volume": 1},
        )

    assert response.status_code == 200
    assert response.json()["readback_active"] is True
    assert response.json()["readback"]["playback_state"] == "PLAY_STATE"
    assert Client.now_reads == 3


def test_preset_key_blocks_when_safe_volume_readback_fails(monkeypatch):
    calls = []

    class Client:
        def __init__(self, ip_address: str):
            self.ip_address = ip_address

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            calls.append(("post", path, body))
            return "<status>OK</status>"

        async def get_xml(self, path: str) -> str:
            calls.append(("get", path, ""))
            if path == "/volume":
                return "<volume><actualvolume>9</actualvolume></volume>"
            assert path == "/now_playing"
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>STOP_STATE</playStatus></nowPlaying>'

    monkeypatch.setattr("basswiesn.app.routers.api.SoundTouchClient", Client)
    with TestClient(create_web_app()) as client:
        client.post("/api/devices", json={"device_id": "PRESETUNSAFE", "name": "Preset Unsafe", "ip_address": "192.0.2.56", "model": "SoundTouch Test"})
        response = client.post("/api/devices/PRESETUNSAFE/key", json={"key": "PRESET_1", "safe_volume": 1})

    assert response.status_code == 409
    assert not any(call[0] == "post" and call[1] == "/key" for call in calls)


def test_preset_readback_does_not_treat_buffering_as_confirmed_audio():
    preset = Preset(location="http://local/station")
    buffering = '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>BUFFERING_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="http://local/station"/></nowPlaying>'
    playing = '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem source="LOCAL_INTERNET_RADIO" location="http://local/station"/></nowPlaying>'
    legacy_no_status = '<nowPlaying source="LOCAL_INTERNET_RADIO"><ContentItem source="LOCAL_INTERNET_RADIO" location="http://local/station"/></nowPlaying>'

    assert api._now_playing_has_audio(buffering, preset) is False
    assert api._now_playing_has_audio(playing, preset) is True
    assert api._now_playing_has_audio(legacy_no_status, preset) is True
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
