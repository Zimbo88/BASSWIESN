"""Fail-closed preset revisions, deletion tombstones and reconciliation."""

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_cloud_app, create_web_app
from basswiesn.app.models import (
    ConfigBackup,
    Device,
    DeviceActionJournal,
    Preset,
    PresetMutation,
    Station,
)
from basswiesn.app.routers import cloud, stations_presets
from basswiesn.app.services.preset_transactions import (
    prepare_preset_mutation,
    transition_preset_mutation,
)


pytestmark = pytest.mark.integration

CONTENT = '<ContentItem source="LOCAL_INTERNET_RADIO" type="stationurl" location="http://example.test/live"><itemName>Test</itemName></ContentItem>'


def _seed(device_id: str, button: int = 1):
    db = app_db.SessionLocal()
    db.add(Device(device_id=device_id, ip_address="192.0.2.220"))
    db.add(Preset(device_id=device_id, button=button, location="http://example.test/live", content_item_xml=CONTENT))
    db.commit()
    db.close()


def test_delete_tombstone_is_visible_to_marge_but_preserves_local_row():
    _seed("TOMBSTONE")
    db = app_db.SessionLocal()
    mutation = prepare_preset_mutation(
        db,
        device_id="TOMBSTONE",
        button=1,
        operation="DELETE",
        requested_state={"deleted": True},
    )
    transition_preset_mutation(db, mutation, "RADIO_WRITE")

    assert db.query(Preset).filter(Preset.device_id == "TOMBSTONE").count() == 1
    assert 'preset id="1"' not in cloud._content_presets_xml(db, "TOMBSTONE")

    transition_preset_mutation(db, mutation, "RECONCILE", diverged=True)
    assert 'preset id="1"' in cloud._content_presets_xml(db, "TOMBSTONE")
    db.close()


def test_verified_delete_commits_local_only_after_radio_readback(monkeypatch):
    _seed("DELETE-OK", 2)

    class Client:
        removed = False

        def __init__(self, _ip):
            pass

        async def get_xml(self, _path):
            return "<presets/>" if self.removed else f'<presets><preset id="2">{CONTENT}</preset></presets>'

        async def post_xml(self, path, _body, headers=None):
            assert path == "/removePreset"
            assert _body == '<preset id="2"></preset>'
            type(self).removed = True
            return "<ok/>"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    monkeypatch.setattr(stations_presets.asyncio, "sleep", no_sleep)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.delete("/api/presets/DELETE-OK/2")

    assert response.status_code == 200
    db = app_db.SessionLocal()
    assert db.query(Preset).filter(Preset.device_id == "DELETE-OK", Preset.button == 2).count() == 0
    mutation = db.query(PresetMutation).filter(PresetMutation.device_id == "DELETE-OK").one()
    assert mutation.state == "LOCAL_COMMIT"
    assert mutation.before_radio_sha256
    assert mutation.after_radio_sha256
    assert mutation.backup_ref.endswith("before.xml")
    ledger = db.query(DeviceActionJournal).filter(
        DeviceActionJournal.job_id == mutation.mutation_id,
        DeviceActionJournal.action == "preset_delete",
    ).one()
    assert ledger.verified is True
    assert ledger.backup_ref == mutation.backup_ref
    assert ledger.rollback_ref == mutation.backup_ref
    assert '"slot_absent":true' in ledger.readback
    db.close()


def test_marge_delete_callback_is_staged_during_active_transaction():
    _seed("DELETE-CALLBACK", 4)
    db = app_db.SessionLocal()
    mutation = prepare_preset_mutation(
        db,
        device_id="DELETE-CALLBACK",
        button=4,
        operation="DELETE",
        requested_state={"deleted": True},
    )
    transition_preset_mutation(db, mutation, "RADIO_WRITE")
    mutation_id = mutation.mutation_id
    db.close()

    with TestClient(create_cloud_app()) as client:
        response = client.delete(
            "/streaming/account/account/device/DELETE-CALLBACK/preset/4"
        )

    assert response.status_code == 200
    assert 'preset id="4"' not in response.text
    db = app_db.SessionLocal()
    assert db.query(Preset).filter(
        Preset.device_id == "DELETE-CALLBACK", Preset.button == 4
    ).count() == 1
    staged = db.query(ConfigBackup).filter(
        ConfigBackup.path == f"preset-inbound-staged/{mutation_id}.delete"
    ).one()
    assert '"button": 4' in staged.content
    db.close()


def test_failed_delete_restores_cloud_view_and_keeps_local_slot(monkeypatch):
    _seed("DELETE-FAIL", 3)

    class Client:
        def __init__(self, _ip):
            pass

        async def get_xml(self, _path):
            return f'<presets><preset id="3">{CONTENT}</preset></presets>'

        async def post_xml(self, _path, _body, headers=None):
            return "<ok/>"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    monkeypatch.setattr(stations_presets.asyncio, "sleep", no_sleep)
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.delete("/api/presets/DELETE-FAIL/3")

    assert response.status_code == 502
    db = app_db.SessionLocal()
    assert db.query(Preset).filter(Preset.device_id == "DELETE-FAIL", Preset.button == 3).count() == 1
    mutation = db.query(PresetMutation).filter(PresetMutation.device_id == "DELETE-FAIL").one()
    assert mutation.state == "RECONCILE"
    assert mutation.diverged is True
    assert 'preset id="3"' in cloud._content_presets_xml(db, "DELETE-FAIL")
    ledger = db.query(DeviceActionJournal).filter(
        DeviceActionJournal.job_id == mutation.mutation_id,
        DeviceActionJournal.action == "preset_delete",
    ).one()
    assert ledger.verified is False
    assert ledger.backup_ref == mutation.backup_ref
    assert ledger.rollback_ref == mutation.backup_ref
    assert ledger.error_category == "RuntimeError"
    db.close()


def test_slot_revisions_are_monotonic():
    _seed("REVISIONS", 4)
    db = app_db.SessionLocal()
    first = prepare_preset_mutation(db, device_id="REVISIONS", button=4, operation="WRITE", requested_state={"v": 1})
    transition_preset_mutation(db, first, "FAILED")
    second = prepare_preset_mutation(db, device_id="REVISIONS", button=4, operation="WRITE", requested_state={"v": 2})
    assert first.revision == 1
    assert second.revision == 2
    assert first.requested_sha256 != second.requested_sha256
    db.close()


def test_marge_callback_is_staged_while_radio_write_is_unverified():
    _seed("CALLBACK-STAGED", 5)
    db = app_db.SessionLocal()
    mutation = prepare_preset_mutation(
        db,
        device_id="CALLBACK-STAGED",
        button=5,
        operation="WRITE",
        requested_state={"location": "http://new.example/live"},
    )
    transition_preset_mutation(db, mutation, "RADIO_WRITE")
    mutation_id = mutation.mutation_id
    db.close()
    payload = (
        "<preset><sourceid>LOCAL_INTERNET_RADIO</sourceid>"
        "<location>http://new.example/live</location><name>New</name></preset>"
    )

    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.put(
            "/streaming/account/123/device/CALLBACK-STAGED/preset/5",
            content=payload,
        )

    assert response.status_code == 200
    db = app_db.SessionLocal()
    preset = db.query(Preset).filter(Preset.device_id == "CALLBACK-STAGED", Preset.button == 5).one()
    assert preset.location == "http://example.test/live"
    staged = db.query(ConfigBackup).filter(
        ConfigBackup.device_id == "CALLBACK-STAGED",
        ConfigBackup.path == f"preset-inbound-staged/{mutation_id}.xml",
    ).one()
    assert "new.example" in staged.content
    db.close()


def test_explicit_radio_download_reconciles_matching_divergent_write(monkeypatch):
    device_id = "RECONCILE-DOWNLOAD"
    db = app_db.SessionLocal()
    station = Station(name="Radio", stream_url="http://stream.example/live.mp3")
    db.add(Device(device_id=device_id, ip_address="192.0.2.221"))
    db.add(station)
    db.commit()
    db.add(Preset(
        device_id=device_id,
        button=1,
        station_id=station.id,
        source="LOCAL_INTERNET_RADIO",
        location="http://old.example/orion?data=x",
        content_item_xml=CONTENT,
    ))
    db.commit()
    mutation = prepare_preset_mutation(
        db,
        device_id=device_id,
        button=1,
        operation="WRITE",
        requested_state={"location": "http://new.example/orion?data=x"},
    )
    transition_preset_mutation(db, mutation, "FAILED", diverged=True)
    mutation_id = mutation.mutation_id
    db.close()

    radio_xml = (
        '<presets><preset id="1"><ContentItem source="LOCAL_INTERNET_RADIO" '
        'type="stationurl" location="http://new.example/orion?data=x">'
        '<itemName>Radio</itemName></ContentItem></preset></presets>'
    )

    class Client:
        def __init__(self, _ip):
            pass

        async def get_xml(self, path):
            assert path == "/presets"
            return radio_xml

    monkeypatch.setattr(stations_presets, "SoundTouchClient", Client)
    monkeypatch.setattr(
        stations_presets,
        "_effective_preset_location",
        lambda *_args, **_kwargs: "http://new.example/orion?data=x",
    )
    with TestClient(create_web_app(background_tasks=False)) as client:
        response = client.post(f"/api/devices/{device_id}/presets/download", json={"dry_run": False})

    assert response.status_code == 200
    assert response.json()["reconciled_mutations"][0]["mutation_id"] == mutation_id
    db = app_db.SessionLocal()
    mutation = db.query(PresetMutation).filter(PresetMutation.mutation_id == mutation_id).one()
    assert mutation.state == "LOCAL_COMMIT"
    assert mutation.diverged is False
    assert db.query(Preset).filter(Preset.device_id == device_id, Preset.button == 1).one().location == "http://new.example/orion?data=x"
    db.close()
