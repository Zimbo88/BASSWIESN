"""Fault injection after the radio write, using only an in-memory transport."""
import asyncio
from xml.etree import ElementTree as ET

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.services.preset_contract import compare_preset_snapshots, preset_summaries_from_xml
from basswiesn.app.services.preset_writer import PresetWriteFailure, PresetWritePorts, write_presets
from test_maintenance_reboot_safety import BUILD, info

pytestmark = pytest.mark.integration
LOCATION = "http://example.invalid/station"
CONTENT = f'<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="{LOCATION}" sourceAccount=""><itemName>Example</itemName></ContentItem>'


@pytest.fixture
def rig(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr("basswiesn.app.services.preset_writer.asyncio.sleep", no_wait)
    device = Device(device_id="PRESET-FAULT", ip_address="192.0.2.42", model="SoundTouch 30", firmware=BUILD, info_xml=info("PRESET-FAULT"))
    db = app_db.SessionLocal()
    db.add(device)
    db.commit()
    yield device, db
    db.close()


class Radio:
    def __init__(self, *, saved=True, readback=True, error=None):
        self.saved, self.readback, self.error = saved, readback, error
        self.posts = []
        self.gets = []
        self.xml = "<presets/>"

    async def get_xml(self, path):
        self.gets.append(path)
        if path == "/info":
            return info("PRESET-FAULT")
        if self.posts and not self.readback:
            raise httpx.ReadTimeout("readback unavailable")
        return self.xml

    async def post_xml(self, path, xml):
        with app_db.SessionLocal() as db:
            assert db.query(ConfigBackup).count() == 1, "START not durable before POST"
        self.posts.append((path, xml))
        if self.saved:
            self.xml = f"<presets>{xml}</presets>"
        if self.error is not None:
            raise self.error
        raise httpx.ReadTimeout("reply lost after dispatch")


def run(rig, radio, *, two_slots=False):
    device, db = rig
    summary = preset_summaries_from_xml(f'<presets><preset id="1">{CONTENT}</preset></presets>')[0]
    ports = PresetWritePorts(
        guard=lambda *_: None, client=lambda _: radio,
        expected_summary=lambda *_: summary, effective_item=lambda *_: CONTENT,
    )
    expected = {1: LOCATION, 2: LOCATION} if two_slots else {1: LOCATION}
    prepared = {button: {"content_item_xml": CONTENT} for button in expected}
    return asyncio.run(write_presets(device, expected, db, ports=ports, prepared_presets=prepared))


@pytest.mark.parametrize("error", [httpx.ReadTimeout("lost"), httpx.WriteError("connection lost"), ConnectionError("lost"), TimeoutError("lost")])
def test_lost_reply_is_confirmed_by_stable_readback_never_repeated(rig, error):
    radio = Radio(error=error)
    result = run(rig, radio)
    assert result.integrity["overall_status"] == "verified"
    assert len(result.integrity["stability_readbacks"]) == 2
    assert result.integrity["transport_uncertainties"][0]["button"] == 1
    assert len(radio.posts) == 1
    assert len(radio.gets) >= 4


@pytest.mark.parametrize("saved,readback", [(False, True), (True, False)])
def test_unknown_outcome_never_becomes_success(rig, saved, readback):
    radio = Radio(saved=saved, readback=readback)
    with pytest.raises(PresetWriteFailure) as failure:
        run(rig, radio)
    assert failure.value.detail["local_saved"] is False
    assert len(radio.posts) == 1
    assert len(radio.gets) > 1


def test_uncertain_first_slot_stops_remaining_writes(rig):
    radio = Radio()
    with pytest.raises(PresetWriteFailure):
        run(rig, radio, two_slots=True)
    assert len(radio.posts) == 1
    assert ET.fromstring(radio.posts[0][1]).get("id") == "1"


def test_explicit_empty_account_is_a_required_readback_value():
    before = f'<presets><preset id="1">{CONTENT}</preset></presets>'
    after = before.replace('sourceAccount=""', 'sourceAccount="stale-account"')
    summary = preset_summaries_from_xml(before)[0]
    result = compare_preset_snapshots(before, after, {1: LOCATION}, expected_items={1: summary})
    assert result["overall_status"] == "integrity_failure"
    assert any(item["field"] == "source_account" for item in result["unexpected_changes"])


def test_http_rejection_is_not_an_ambiguous_transport_reply(rig):
    response = httpx.Response(400, request=httpx.Request("POST", "http://example.invalid/storePreset"))
    radio = Radio(saved=False, error=httpx.HTTPStatusError("rejected", request=response.request, response=response))
    with pytest.raises(PresetWriteFailure):
        run(rig, radio)
    assert len(radio.posts) == 1
    assert radio.gets == ["/info", "/presets", "/info", "/presets"]


def test_stability_window_timeout_does_not_certify_initial_readback(rig):
    radio = Radio()
    original = radio.get_xml
    async def fail_later(path):
        if path == "/presets" and radio.gets.count(path) == 3:
            raise TimeoutError("synthetic late readback failure")
        return await original(path)
    radio.get_xml = fail_later
    with pytest.raises(PresetWriteFailure) as failure:
        run(rig, radio)
    assert failure.value.detail["error"] == "radio preset stability readback failed"
    assert len(radio.posts) == 1


def test_already_equal_preset_is_observed_without_post(rig):
    radio = Radio()
    radio.xml = f'<presets><preset id="1">{CONTENT}</preset></presets>'
    result = run(rig, radio)
    assert result.integrity["overall_status"] == "verified"
    assert result[0]["content_item_xml"] == ET.tostring(ET.fromstring(CONTENT), encoding="unicode")
    assert not radio.posts


@pytest.mark.parametrize("expected", [{}, {7: LOCATION}])
def test_raw_writer_invalid_slot_never_dispatches(rig, expected):
    device, db = rig
    radio = Radio()
    ports = PresetWritePorts(guard=lambda *_: None, client=lambda _: radio,
                             expected_summary=lambda *_: {}, effective_item=lambda *_: CONTENT)
    with pytest.raises(PresetWriteFailure):
        asyncio.run(write_presets(device, expected, db, ports=ports))
    assert not radio.posts


def test_raw_writer_missing_content_cannot_be_reported_verified(rig):
    device, db = rig
    radio = Radio()
    summary = preset_summaries_from_xml(f'<presets><preset id="1">{CONTENT}</preset></presets>')[0]
    ports = PresetWritePorts(guard=lambda *_: None, client=lambda _: radio,
                             expected_summary=lambda *_: summary, effective_item=lambda *_: "")
    with pytest.raises(PresetWriteFailure):
        asyncio.run(write_presets(device, {1: LOCATION}, db, ports=ports))
    assert not radio.posts
