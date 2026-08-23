from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, Preset, Station
from basswiesn.app.routers import stations_presets


pytestmark = pytest.mark.integration


def _xml(*slots: str) -> str:
    return f"<presets>{''.join(slots)}</presets>"


def _slot(button: int, location: str, *, source: str = "LOCAL_INTERNET_RADIO", source_account: str = "", name: str = "Station") -> str:
    return f'<preset id="{button}"><ContentItem source="{source}" sourceAccount="{source_account}" type="stationurl" location="{location}"><itemName>{name}</itemName></ContentItem></preset>'


def test_unexplained_orion_payload_change_is_integrity_failure():
    before = _xml(
        _slot(1, "http://192.0.2.77:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=target"),
        _slot(2, "http://192.0.2.200:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=untouched-before"),
    )
    after = _xml(
        _slot(1, "http://192.0.2.77:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=target"),
        _slot(2, "http://192.0.2.200:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=untouched-after"),
    )

    result = stations_presets.compare_preset_snapshots(
        before,
        after,
        {1: "http://192.0.2.77:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=target"},
    )
    assert result["target_verified"] is True
    assert result["untouched_slots_verified"] is False
    assert result["normalization_detected"] == []
    assert result["overall_status"] == "integrity_failure"
    assert result["rollback_recommended"] is True
    assert any(item["button"] == 2 for item in result["unexpected_changes"])


def test_approved_orion_origin_migration_keeps_untouched_contract_verified():
    before = _xml(
        _slot(1, "http://192.0.2.10:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=target", name="One"),
        _slot(2, "http://192.0.2.10:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=other", name="Two"),
    )
    after = before.replace("192.0.2.10", "192.0.2.20")
    target = "http://192.0.2.20:1516/core02/svc-bmx-adapter-orion/prod/orion/station?data=target"

    accepted = stations_presets.compare_preset_snapshots(
        before,
        after,
        {1: target},
        allowed_orion_origins={"http://192.0.2.20:1516"},
    )
    rejected = stations_presets.compare_preset_snapshots(
        before,
        after,
        {1: target},
        allowed_orion_origins={"http://192.0.2.30:1516"},
    )

    assert accepted["overall_status"] == "verified"
    assert accepted["normalization_detected"][0]["button"] == 2
    assert accepted["slot_results"][1]["status"] == "normalized"
    assert rejected["overall_status"] == "integrity_failure"


def test_store_and_stable_readbacks_do_not_inject_notification(monkeypatch):
    events: list[tuple[str, str]] = []
    before = _xml()
    after = _xml(_slot(1, "http://cloud.test/live", name="Live"))
    state = {"after_store": False}

    class Client:
        def __init__(self, _ip: str):
            pass

        async def get_xml(self, path: str) -> str:
            events.append(("get", path))
            return after if state["after_store"] else before

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            events.append(("post", path))
            if path == "/storePreset":
                state["after_store"] = True
            return "<ok />"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    device = Device(device_id="ORDER1", ip_address="192.0.2.90")
    station = Station(name="Live", stream_url="http://example.test/live.mp3")
    db.add_all([device, station])
    db.flush()
    db.add(Preset(device_id=device.device_id, button=1, station_id=station.id, source="LOCAL_INTERNET_RADIO", location="http://cloud.test/live", content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/live"><itemName>Live</itemName></ContentItem>'))
    db.commit()

    result = asyncio.run(stations_presets.sync_presets_to_radio(device, {1: "http://cloud.test/live"}, db))
    db.close()

    assert result.integrity["overall_status"] == "verified"
    assert result.integrity["notification_sequence"]["variant"] == "C"
    assert result.integrity["radio_owned_notification"] is True
    assert len(result.integrity["stability_readbacks"]) == 2
    assert events == [
        ("get", "/presets"),
        ("post", "/storePreset"),
        ("get", "/presets"),
        ("get", "/presets"),
        ("get", "/presets"),
    ]


def test_unexpected_untouched_slot_prevents_notification_and_false_success(monkeypatch):
    events: list[str] = []
    before = _xml(
        _slot(2, "http://cloud.test/untouched", name="Untouched"),
    )
    after = _xml(
        _slot(1, "http://cloud.test/live", name="Live"),
        _slot(2, "http://cloud.test/changed", name="Untouched"),
    )
    state = {"after_store": False}

    class Client:
        def __init__(self, _ip: str):
            pass

        async def get_xml(self, path: str) -> str:
            events.append(f"get:{path}")
            return after if state["after_store"] else before

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            events.append(f"post:{path}")
            if path == "/storePreset":
                state["after_store"] = True
            return "<ok />"

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    db = app_db.SessionLocal()
    device = Device(device_id="INTEGRITY1", ip_address="192.0.2.91")
    station = Station(name="Live", stream_url="http://example.test/live.mp3")
    db.add_all([device, station])
    db.flush()
    db.add(Preset(device_id=device.device_id, button=1, station_id=station.id, source="LOCAL_INTERNET_RADIO", location="http://cloud.test/live", content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/live"><itemName>Live</itemName></ContentItem>'))
    db.commit()

    with pytest.raises(HTTPException) as error:
        asyncio.run(stations_presets.sync_presets_to_radio(device, {1: "http://cloud.test/live"}, db))
    db.close()

    assert error.value.status_code == 502
    detail = error.value.detail
    assert detail["integrity"]["overall_status"] == "integrity_failure"
    assert detail["integrity"]["untouched_slots_verified"] is False
    assert "post:/notification" not in events


def test_delayed_radio_rollback_fails_closed_after_initial_good_readback(monkeypatch):
    events: list[str] = []
    before = _xml()
    after = _xml(_slot(1, "http://cloud.test/live", name="Live"))
    reads = {"count": 0}

    class Client:
        def __init__(self, _ip: str):
            pass

        async def get_xml(self, path: str) -> str:
            events.append(f"get:{path}")
            reads["count"] += 1
            if reads["count"] == 1:
                return before
            if reads["count"] == 2:
                return after
            return before

        async def post_xml(self, path: str, body: str, headers=None) -> str:
            events.append(f"post:{path}")
            return "<ok />"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    monkeypatch.setattr(stations_presets.asyncio, "sleep", no_sleep)
    db = app_db.SessionLocal()
    device = Device(device_id="DELAYED-ROLLBACK", ip_address="192.0.2.92")
    station = Station(name="Live", stream_url="http://example.test/live.mp3")
    db.add_all([device, station])
    db.flush()
    db.add(Preset(
        device_id=device.device_id,
        button=1,
        station_id=station.id,
        source="LOCAL_INTERNET_RADIO",
        location="http://cloud.test/live",
        content_item_xml='<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://cloud.test/live"><itemName>Live</itemName></ContentItem>',
    ))
    db.commit()

    with pytest.raises(HTTPException) as error:
        asyncio.run(stations_presets.sync_presets_to_radio(device, {1: "http://cloud.test/live"}, db))
    db.close()

    assert error.value.status_code == 502
    assert error.value.detail["error"] == "preset integrity check failed during stability window"
    assert "post:/notification" not in events
