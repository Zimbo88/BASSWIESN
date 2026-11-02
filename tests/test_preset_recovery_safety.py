"""Identity, whole-slot readback and reconciliation; fictional radios only."""
import asyncio
from hashlib import sha256
import json
from xml.etree import ElementTree as ET

from fastapi import HTTPException
from starlette.requests import Request
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, Preset, PresetMutation, PresetProfile, Station
from basswiesn.app.routers import stations_presets as api
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.preset_transactions import prepare_preset_mutation, transition_preset_mutation
from test_maintenance_reboot_safety import ID, BUILD, info

pytestmark = pytest.mark.integration
ITEM = '<ContentItem source="TUNEIN" sourceAccount="example-account" type="stationurl" location="http://example.invalid/live" custom="preserve"><itemName>Example</itemName><containerArt>http://example.invalid/art</containerArt><extra>keep</extra></ContentItem>'
SLOTS = f'<presets><preset id="1">{ITEM}</preset><preset id="2">{ITEM}</preset></presets>'
REMAINING = f'<presets><preset id="2">{ITEM}</preset></presets>'
REQUEST = Request({"type": "http", "headers": [(b"host", b"example.invalid")]})


@pytest.fixture
def rig(monkeypatch):
    class Radio:
        def __init__(self):
            self.xml, self.identity, self.after = SLOTS, info(), REMAINING
            self.writes, self.reads = [], []
            self.constructed = 0
            self.error = None
            self.on_read = lambda path: None
            self.on_write = lambda: None

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            self.reads.append(path)
            value = self.identity if path == "/info" else self.xml
            self.on_read(path)
            return value

        async def post_xml(self, path, body):
            assert device_lock(ID).owned_by_current_task()
            with app_db.SessionLocal() as db:
                assert db.query(ConfigBackup).count() > 0
                assert db.query(PresetMutation).filter_by(state="RADIO_WRITE").count() == 1
            self.writes.append((path, body))
            self.xml = self.after
            self.on_write()
            if self.error:
                raise self.error
            return "<ok/>"

    radio = Radio()
    monkeypatch.setattr(api, "SoundTouchClient", radio.factory)
    async def no_wait(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    with app_db.SessionLocal() as db:
        radio.db = db
        radio.device = Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30", firmware=BUILD, info_xml=info())
        db.add(radio.device)
        for slot in (1, 2):
            db.add(Preset(device_id=ID, button=slot, source="TUNEIN", source_account="example-account",
                          location="http://example.invalid/live", content_item_xml=ITEM))
        db.commit()
        yield radio


def run(rig, action="delete", **payload):
    try:
        return asyncio.run(api.delete_preset(ID, 1, db=rig.db) if action == "delete"
                           else api.download_device_presets(ID, payload, REQUEST, rig.db))
    except HTTPException as exc:
        return {"rejected": True, "status": exc.status_code, "detail": exc.detail}


@pytest.mark.parametrize("action", ["delete", "download"])
@pytest.mark.parametrize("failure", ["identity", "protected", "malformed", "duplicate", "nested", "root"])
def test_untrusted_readback_never_changes_local_state(rig, action, failure):
    if failure == "identity":
        rig.identity = info("BB22CC33DD44")
    elif failure == "protected":
        rig.device.ip_address = "192.168.50.25"  # Isolated conftest policy.
        rig.db.commit()
    elif failure == "malformed":
        rig.xml = "<presets>"
    elif failure == "duplicate":
        rig.xml = SLOTS.replace('id="2"', 'id="1"')
    elif failure == "nested":
        rig.xml = f"<presets><wrapper>{SLOTS}</wrapper></presets>"
    else:
        rig.xml = "<error/>"
    assert run(rig, action).get("rejected") is True
    assert not rig.writes
    assert rig.db.query(Preset).count() == 2
    if failure == "protected":
        assert rig.constructed == 0


def test_delete_checks_live_exact_profile(rig):
    rig.identity = info(firmware="27.99.0")
    assert run(rig).get("rejected") is True
    assert not rig.writes


@pytest.mark.parametrize("change", ["other-slot", "identity", "malformed", "unchanged"])
def test_delete_does_not_certify_partial_or_untrusted_result(rig, change):
    rig.after = {"other-slot": REMAINING.replace("Example", "Other"), "identity": REMAINING,
                 "malformed": "<presets>", "unchanged": SLOTS}[change]
    if change == "identity":
        rig.on_write = lambda: setattr(rig, "identity", info("BB22CC33DD44"))
    assert run(rig).get("rejected") is True
    assert len(rig.writes) == 1
    assert rig.db.query(Preset).count() == 2
    assert rig.db.query(PresetMutation).one().state == "RECONCILE"


def test_lost_delete_reply_is_read_back_once_not_replayed(rig):
    rig.error = TimeoutError("synthetic lost response")
    result = run(rig)
    assert result.get("verified") is True
    assert len(rig.writes) == 1
    assert rig.db.query(Preset).count() == 1
    assert rig.reads.count("/info") >= 3


def test_download_preserves_exact_content_and_account(rig):
    rig.db.query(Preset).filter_by(button=1).one().source_account = "stale"
    rig.db.commit()
    result = run(rig, "download")
    assert not result.get("rejected")
    row = rig.db.query(Preset).filter_by(button=1).one()
    assert row.source_account == "example-account"
    assert 'custom="preserve"' in row.content_item_xml
    assert "<extra>keep</extra>" in row.content_item_xml
    assert "<containerArt>" in row.content_item_xml
    assert not rig.writes


def divergent(rig, *, complete=True):
    requested = {"source": "TUNEIN", "source_account": "example-account", "location": "http://example.invalid/live",
                 "content_item_xml": ITEM} if complete else {"location": "http://example.invalid/live"}
    row = prepare_preset_mutation(rig.db, device_id=ID, button=1, operation="WRITE", requested_state=requested)
    backup = ConfigBackup(device_id=ID, path=f"preset-write/{row.mutation_id}/before.xml", content=SLOTS)
    rig.db.add(backup)
    rig.db.commit()
    transition_preset_mutation(rig.db, row, "RADIO_WRITE", before_radio=SLOTS, backup_ref=backup.path)
    transition_preset_mutation(rig.db, row, "RECONCILE", diverged=True)
    return row


@pytest.mark.parametrize("change", ["source", "account", "other-slot", "missing-intent"])
def test_reconciliation_never_uses_location_alone(rig, change):
    row = divergent(rig, complete=change != "missing-intent")
    rig.xml = {"source": SLOTS.replace('source="TUNEIN"', 'source="SPOTIFY"', 1),
               "account": SLOTS.replace("example-account", "other-account", 1),
               "other-slot": SLOTS.replace('<preset id="2">', '<preset id="3">'),
               "missing-intent": SLOTS}[change]
    result = run(rig, "download")
    rig.db.refresh(row)
    assert row.state == "RECONCILE" and row.diverged
    assert result.get("reconciled_mutations") == []
    assert not rig.writes


def test_complete_immutable_intent_reconciles_without_radio_write(rig):
    row = divergent(rig)
    result = run(rig, "download")
    rig.db.refresh(row)
    assert row.state == "LOCAL_COMMIT" and not row.diverged
    assert result["reconciled_mutations"][0]["mutation_id"] == row.mutation_id
    assert row.after_radio_sha256 == sha256(SLOTS.encode()).hexdigest()
    assert not rig.writes


@pytest.mark.parametrize("change", ["backup", "intent", "local", "revision"])
def test_changes_during_delete_do_not_commit(rig, change):
    def interfere():
        if change in {"backup", "intent"}:
            pattern = "%/before.xml" if change == "backup" else "preset-intent/%"
            rig.db.query(ConfigBackup).filter(ConfigBackup.path.like(pattern)).one().content = "corrupt"
        elif change == "local":
            rig.db.query(Preset).filter_by(button=2).one().location = "http://example.invalid/new"
        else:
            rig.db.query(PresetMutation).one().revision += 1
        rig.db.commit()
    rig.on_write = interfere
    assert run(rig).get("rejected") is True
    assert rig.db.query(Preset).count() == 2
    assert rig.db.query(PresetMutation).one().state == "RECONCILE"
    assert len(rig.writes) == 1
    # An uncertain deletion is not safe to replay, even after another click.
    assert run(rig).get("rejected") is True
    assert len(rig.writes) == 1


def test_delete_cancellation_retains_intent_and_previous_rows(rig):
    rig.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(rig)
    assert rig.db.query(Preset).count() == 2
    assert rig.db.query(PresetMutation).one().state == "RECONCILE"
    assert rig.db.query(ConfigBackup).filter(ConfigBackup.path.like("preset-intent/%")).count() == 1


def test_delete_rejects_stale_requested_revision_before_dispatch(rig):
    with pytest.raises(HTTPException):
        asyncio.run(api.delete_preset(ID, 1, expected_revision=99, db=rig.db))
    assert not rig.writes


def test_download_does_not_overwrite_local_change_during_read(rig):
    def interfere(path):
        if path == "/presets":
            rig.db.query(Preset).filter_by(button=2).one().source_account = "newer-account"
            rig.db.commit()
    rig.on_read = interfere
    assert run(rig, "download").get("rejected") is True
    assert rig.db.query(Preset).filter_by(button=2).one().source_account == "newer-account"


def test_pending_preset_backups_survive_retention(rig, monkeypatch):
    from basswiesn.app.services import maintenance
    row = divergent(rig)
    retained = {backup.id for backup in rig.db.query(ConfigBackup).all()}
    settings = maintenance.get_settings()
    monkeypatch.setattr(settings, "config_backup_retention_count", 0)
    assert retained.isdisjoint(maintenance.cleanup_plan(rig.db)["config_backup_ids"])
    transition_preset_mutation(rig.db, row, "VERIFIED", diverged=False)
    transition_preset_mutation(rig.db, row, "LOCAL_COMMIT")
    assert retained.issubset(maintenance.cleanup_plan(rig.db)["config_backup_ids"])


def test_cleanup_reserves_writer_before_plan_and_preserves_pending(rig, monkeypatch):
    from sqlalchemy import event
    from basswiesn.app.services import maintenance
    divergent(rig)
    statements = []
    engine = rig.db.get_bind()
    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        monkeypatch.setattr(maintenance.get_settings(), "config_backup_retention_count", 0)
        result = maintenance.run_cleanup(rig.db)
        assert result["config_backups"] == 0
        assert statements[0] == "BEGIN IMMEDIATE"
        assert rig.db.query(ConfigBackup).count() == 2
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_profile_cannot_overwrite_an_unresolved_operation(rig):
    from basswiesn.app.services.preset_writer import apply_prepared_presets, PresetWriteFailure
    divergent(rig)
    with pytest.raises(PresetWriteFailure):
        asyncio.run(apply_prepared_presets(rig.device,
            {1: {"location": "http://example.invalid/new", "content_item_xml": ITEM}}, rig.db,
            ports=api.preset_write_ports(), backup_label="example"))
    assert not rig.constructed
    assert rig.db.query(PresetMutation).count() == 1


@pytest.mark.parametrize("action", ["delete", "download"])
def test_failed_final_database_commit_never_reports_success(rig, monkeypatch, action):
    commit = rig.db.commit
    failed = False
    def fail_final():
        nonlocal failed
        final = (rig.db.query(PresetMutation).filter_by(state="LOCAL_COMMIT").count() > 0
                 if action == "delete" else any(isinstance(row, Preset) for row in rig.db.dirty))
        if final and not failed:
            failed = True
            raise RuntimeError("synthetic local commit loss")
        commit()
    if action == "download":
        rig.xml = SLOTS.replace("example-account", "updated-account")
    monkeypatch.setattr(rig.db, "commit", fail_final)
    assert run(rig, action).get("rejected") is True
    assert failed
    assert rig.db.query(Preset).count() == 2
    assert rig.db.query(Preset).filter_by(button=1).one().source_account == "example-account"


@pytest.mark.parametrize("outcome", ["success", "lost-reply", "rejected", "other-slot", "readback-corrupt", "local-change", "intent-change", "preview"])
def test_profile_local_commit_follows_verified_radio_state(rig, monkeypatch, outcome):
    from basswiesn.app.routers import api as profiles
    station = Station(name="New station", stream_url="http://example.invalid/new.mp3")
    rig.db.add(station)
    rig.db.commit()
    profile = PresetProfile(name="Example", slots_json=json.dumps([{"button": 1, "station_id": station.id}]))
    rig.db.add(profile)
    rig.db.commit()
    monkeypatch.setattr(profiles, "_station_location_or_409", lambda *_: "http://example.invalid/new")
    async def write(path, body):
        assert path == "/storePreset"
        assert rig.db.query(Preset).filter_by(button=1).one().source == "TUNEIN"
        assert device_lock(ID).owned_by_current_task()
        rig.writes.append((path, body))
        rig.xml = f'<presets>{body}<preset id="2">{ITEM}</preset></presets>'
        if outcome == "other-slot":
            rig.xml = rig.xml.replace('<preset id="2">', '<preset id="3">')
        elif outcome == "readback-corrupt":
            rig.xml = "<presets>"
        elif outcome == "local-change":
            rig.db.query(Preset).filter_by(button=2).one().source_account = "newer"
            rig.db.commit()
        elif outcome == "intent-change":
            rig.db.query(ConfigBackup).filter(ConfigBackup.path.like("preset-intent/%")).one().content = "{}"
            rig.db.commit()
        elif outcome == "lost-reply":
            raise TimeoutError("response lost after store")
        elif outcome == "rejected":
            raise ValueError("synthetic refusal")
        return "<ok/>"
    rig.post_xml = write
    try:
        result = asyncio.run(profiles.apply_preset_profile(profile.id, ID,
            {"dry_run": outcome == "preview", "memory_checked": True}, REQUEST, rig.db))
    except HTTPException as exc:
        result = {"rejected": True, "detail": exc.detail}
    row = rig.db.query(Preset).filter_by(button=1).one()
    if outcome in {"success", "lost-reply"}:
        assert result.get("verified") is True
        assert row.station_id == station.id
        assert row.source == "LOCAL_INTERNET_RADIO" and row.source_account == ""
        assert ET.fromstring(row.content_item_xml).get("source") == row.source
        assert rig.db.query(PresetMutation).one().state == "LOCAL_COMMIT"
    else:
        assert row.source == "TUNEIN" and row.location == "http://example.invalid/live"
        if outcome == "preview":
            assert result["dry_run"] is True
            assert not rig.constructed
        else:
            assert result.get("rejected") is True
            assert rig.db.query(PresetMutation).one().state == "RECONCILE"
    assert len(rig.writes) == (0 if outcome == "preview" else 1)
