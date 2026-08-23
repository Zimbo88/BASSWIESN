from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, Setting, Station
from basswiesn.app.routers import api, stations_presets


pytestmark = pytest.mark.integration


class SelectClient:
    calls: list[tuple[str, str]] = []

    def __init__(self, _ip_address: str):
        pass

    async def get_xml(self, path: str) -> str:
        self.calls.append(("get", path))
        if path == "/sources":
            return '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY" /></sources>'
        if path == "/now_playing":
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>BUFFERING_STATE</playStatus></nowPlaying>'
        raise AssertionError(path)

    async def post_xml(self, path: str, body: str, headers=None) -> str:
        self.calls.append(("post", path))
        assert path == "/select"
        assert ET.fromstring(body).tag == "ContentItem"
        return "<status>OK</status>"


class StopClient:
    calls: list[tuple[str, str]] = []

    def __init__(self, _ip_address: str):
        pass

    async def get_xml(self, path: str) -> str:
        self.calls.append(("get", path))
        if path == "/now_playing":
            return '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>STOP_STATE</playStatus></nowPlaying>'
        raise AssertionError(path)

    async def post_xml(self, path: str, body: str, headers=None) -> str:
        self.calls.append(("post", path))
        assert path == "/key"
        return "<status>OK</status>"


def _create_station_device() -> tuple[str, int]:
    db = app_db.SessionLocal()
    device = Device(device_id="SOUNDBIGMOCK", name="SoundBig Mock", ip_address="192.0.2.47")
    station = Station(name="Bayern 1", stream_url="http://example.test/bayern1.mp3")
    db.add_all([device, station, Setting(key="lan_host", value="192.168.50.77")])
    db.commit()
    result = device.device_id, station.id
    db.close()
    return result


def test_preset_select_without_volume_argument_has_no_write_side_effect(monkeypatch):
    SelectClient.calls = []
    monkeypatch.setattr(stations_presets, "SoundTouchClient", SelectClient)
    device_id, station_id = _create_station_device()

    with TestClient(create_web_app()) as client:
        response = client.post(f"/api/devices/{device_id}/stations/{station_id}/play", json={"dry_run": False})

    assert response.status_code == 200
    assert ("post", "/select") in SelectClient.calls
    assert not any(method == "post" and endpoint == "/volume" for method, endpoint in SelectClient.calls)
    assert not any(method == "post" and endpoint in {"/storePreset", "/notification", "/key"} for method, endpoint in SelectClient.calls)
    assert response.json()["confirmed_volume"] is None


def test_stop_uses_key_only_and_confirms_stopped_status_without_volume_write(monkeypatch):
    StopClient.calls = []
    monkeypatch.setattr(api, "SoundTouchClient", StopClient)

    with TestClient(create_web_app()) as client:
        create = client.post("/api/devices", json={"device_id": "SOUNDBIGSTOP", "name": "SoundBig Stop", "ip_address": "192.0.2.48", "model": "SoundTouch Test"})
        assert create.status_code == 200
        response = client.post(f"/api/devices/SOUNDBIGSTOP/key", json={"key": "STOP"})

    assert response.status_code == 200
    assert [endpoint for method, endpoint in StopClient.calls if method == "post"] == ["/key", "/key"]
    assert not any(method == "post" and endpoint == "/volume" for method, endpoint in StopClient.calls)
    assert response.json()["readback"]["playback_state"] == "STOP_STATE"


def test_buffering_status_is_not_confirmed_as_audio():
    buffering = '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>BUFFERING_STATE</playStatus><ContentItem location="http://local/station" /></nowPlaying>'
    playing = '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>PLAY_STATE</playStatus><ContentItem location="http://local/station" /></nowPlaying>'

    assert api._now_playing_has_audio(buffering) is False
    assert api._now_playing_has_audio(playing) is True
