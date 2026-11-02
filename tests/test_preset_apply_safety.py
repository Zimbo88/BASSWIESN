"""Shared single/clone/artwork transactions; no real device or network access."""
import asyncio
from xml.etree import ElementTree as ET

from fastapi import HTTPException
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, Preset, PresetMutation, Setting, Station
from basswiesn.app.routers import stations_presets as api
from basswiesn.app.services.device_policy import device_lock
from basswiesn.app.services.preset_contract import preset_summaries_from_xml
from test_maintenance_reboot_safety import ID, BUILD, info
from test_preset_recovery_safety import ITEM, SLOTS, REQUEST
from basswiesn.app.services import preset_writer
from basswiesn.app.services.preset_observation import PresetObservation
from basswiesn.app.services.preset_transactions import prepare_preset_mutation

pytestmark = pytest.mark.integration
SOURCE = "BB22CC33DD44"


@pytest.fixture
def rig(monkeypatch):
    class Radio:
        def __init__(self):
            self.xml, self.identity = SLOTS, info()
            self.posts, self.reads = [], []
            self.on_write = lambda: None
            self.on_read = lambda _: None
            self.constructed = 0
            self.cancel = False
            self.lost = False
            self.partial = False

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            self.reads.append(path)
            self.on_read(path)
            return self.identity if path == "/info" else self.xml

        async def post_xml(self, path, body):
            assert path == "/storePreset"
            assert device_lock(ID).owned_by_current_task()
            self.posts.append((path, body))
            root, new = ET.fromstring(self.xml), ET.fromstring(body)
            if not self.partial or len(self.posts) == 1:
                for row in list(root):
                    if row.get("id") == new.get("id"):
                        root.remove(row)
                root.append(new)
                self.xml = ET.tostring(root, encoding="unicode")
            self.on_write()
            if self.cancel:
                raise asyncio.CancelledError()
            if self.lost:
                raise TimeoutError("synthetic lost reply")
            return "<ok/>"

    radio = Radio()
    monkeypatch.setattr(api, "SoundTouchClient", radio.factory)
    monkeypatch.setattr(api, "_station_location_or_409", lambda *_: "http://example.invalid/new")
    async def no_sleep(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with app_db.SessionLocal() as db:
        radio.db = db
        radio.station = Station(name="New station", stream_url="http://example.invalid/new.mp3", image_url="http://example.invalid/new.png")
        db.add_all([Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30", firmware=BUILD, info_xml=info()),
                    Device(device_id=SOURCE, ip_address="192.0.2.43", model="SoundTouch 30", firmware=BUILD, info_xml=info(SOURCE)),
                    radio.station, Setting(key=f"station_art_mode:{ID}", value="station_logo")])
        db.flush()
        for identity in (ID, SOURCE):
            for slot in (1, 2):
                xml = ITEM if identity == ID else ITEM.replace("/live", f"/clone{slot}")
                row = preset_summaries_from_xml(f'<presets><preset id="{slot}">{xml}</preset></presets>')[0]
                db.add(Preset(device_id=identity, button=slot, station_id=radio.station.id,
                    source=row["source"], source_account=row["source_account"], location=row["location"], content_item_xml=xml))
        db.commit()
        yield radio


def invoke(rig, action, **changes):
    payload = {"dry_run": False, "memory_checked": True, **changes}
    if action == "set":
        operation = api.set_preset(ID, 1, {"station_id": rig.station.id, **payload}, REQUEST, rig.db)
    elif action == "clone":
        operation = api.clone_presets({"source_device_id": SOURCE, "target_device_id": ID, **payload}, rig.db)
    else:
        operation = api.sync_local_presets(ID, payload, rig.db)
    return asyncio.run(operation)


def prepared_call(rig, prepared, **options):
    device = rig.db.query(Device).filter_by(device_id=ID).one()
    return asyncio.run(preset_writer.apply_prepared_presets(device, prepared, rig.db,
        ports=api.preset_write_ports(), backup_label="fixture", **options))


@pytest.mark.parametrize("prepared", [{}, {0: {}}, {True: {}}, {7: {}}])
def test_invalid_prepared_slots_fail_before_transport(rig, prepared):
    with pytest.raises(preset_writer.PresetWriteFailure):
        prepared_call(rig, prepared)
    assert not rig.posts and rig.constructed == 0


@pytest.mark.parametrize("option", [{"expected_local": "stale"}, {"operation": "UNSUPPORTED"}])
def test_invalid_plan_preconditions_never_prepare_or_write(rig, option):
    intent = preset_summaries_from_xml(SLOTS)[0]
    with pytest.raises(preset_writer.PresetWriteFailure):
        prepared_call(rig, {1: intent}, **option)
    assert not rig.posts and rig.constructed == 0
    assert rig.db.query(PresetMutation).count() == 0


def test_unresolved_previous_intent_blocks_new_apply(rig):
    intent = preset_summaries_from_xml(SLOTS)[0]
    previous = prepare_preset_mutation(rig.db, device_id=ID, button=1, operation="WRITE", requested_state=intent)
    with pytest.raises(preset_writer.PresetWriteFailure):
        prepared_call(rig, {1: intent})
    assert not rig.posts
    assert rig.db.query(PresetMutation).one().mutation_id == previous.mutation_id


@pytest.mark.parametrize("fault", ["intent", "backup", "radio"])
def test_changed_durable_preset_preconditions_never_dispatch(rig, fault):
    from basswiesn.app.models import ConfigBackup
    def interfere(path):
        reads = rig.reads.count("/presets")
        if path != "/presets":
            return
        if fault == "intent" and reads == 1:
            rig.db.query(PresetMutation).filter_by(device_id=ID).one().revision += 1
            rig.db.commit()
        elif fault == "backup" and reads == 2:
            row = rig.db.query(ConfigBackup).filter(ConfigBackup.path.like("%/before.xml")).one()
            row.content = "<presets/>"
            rig.db.commit()
        elif fault == "radio" and reads == 2:
            rig.xml = rig.xml.replace("/live", "/other")
    rig.on_read = interfere
    with pytest.raises(HTTPException):
        invoke(rig, "set")
    assert not rig.posts
    assert rig.db.query(Preset).filter_by(device_id=ID, button=1).one().content_item_xml == ITEM


@pytest.mark.parametrize("changed", [False, True])
def test_noop_requires_stable_observation_and_rechecks_external_precondition(rig, changed):
    observed = PresetObservation.parse(SLOTS, ID)
    checked = []
    def check():
        checked.append(True)
    if changed:
        rig.xml = SLOTS.replace("/live", "/other")
        with pytest.raises(preset_writer.PresetWriteFailure):
            prepared_call(rig, {}, expected_before=observed, precondition=check)
    else:
        result = prepared_call(rig, {}, expected_before=observed, precondition=check)
        assert result.integrity["radio_write_count"] == 0
        assert len(checked) >= 2
    assert not rig.posts


def test_local_write_during_intent_persistence_blocks_radio(rig, monkeypatch):
    original = preset_writer.prepare_preset_mutation
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        rig.db.query(Preset).filter_by(device_id=ID, button=1).one().location = "http://example.invalid/concurrent"
        rig.db.commit()
        return result
    monkeypatch.setattr(preset_writer, "prepare_preset_mutation", prepare)
    with pytest.raises(HTTPException):
        invoke(rig, "set")
    assert not rig.posts
    assert rig.db.query(Preset).filter_by(device_id=ID, button=1).one().location.endswith("/concurrent")


@pytest.mark.parametrize("action", ["set", "clone", "artwork"])
def test_cancelled_write_keeps_local_state_and_durable_reconciliation(rig, action):
    rig.cancel = True
    with pytest.raises(asyncio.CancelledError):
        invoke(rig, action)
    rig.db.expire_all()
    assert rig.db.query(Preset).filter_by(device_id=ID, button=1).one().location.endswith("/live")
    rows = rig.db.query(PresetMutation).filter_by(device_id=ID).all()
    assert rows and all(row.state == "RECONCILE" and row.diverged for row in rows)
    assert len(rig.posts) == 1


@pytest.mark.parametrize("action", ["set", "clone", "artwork"])
def test_final_commit_failure_never_loses_recovery_state(rig, monkeypatch, action):
    commit, failed = rig.db.commit, False
    def fail_final():
        nonlocal failed
        if not failed and rig.db.query(PresetMutation).filter_by(device_id=ID, state="LOCAL_COMMIT").count():
            failed = True
            raise RuntimeError("synthetic commit failure")
        commit()
    monkeypatch.setattr(rig.db, "commit", fail_final)
    with pytest.raises(HTTPException):
        invoke(rig, action)
    assert failed
    assert rig.db.query(Preset).filter_by(device_id=ID, button=1).one().content_item_xml == ITEM
    assert all(row.state == "RECONCILE" for row in rig.db.query(PresetMutation).filter_by(device_id=ID).all())


@pytest.mark.parametrize("action", ["set", "clone", "artwork"])
def test_success_commits_actual_content_and_ledger(rig, action):
    result = invoke(rig, action)
    assert result["verified"] is True
    actual = {row["button"]: row for row in preset_summaries_from_xml(rig.xml)}
    for slot in ({1} if action == "set" else {1, 2}):
        local = rig.db.query(Preset).filter_by(device_id=ID, button=slot).one()
        assert local.content_item_xml == actual[slot]["content_item_xml"]
        assert local.source_account == actual[slot]["source_account"]
    assert all(row.state == "LOCAL_COMMIT" for row in rig.db.query(PresetMutation).filter_by(device_id=ID).all())


@pytest.mark.parametrize("bad", ["<presets>", "<error/>", "wrong-identity"])
def test_artwork_noop_cannot_certify_invalid_or_wrong_device_xml(rig, bad):
    if bad == "wrong-identity":
        rig.identity = info(SOURCE)
        rig.xml = "<presets/>"
    else:
        rig.xml = bad
    with pytest.raises(HTTPException):
        invoke(rig, "artwork")
    assert not rig.posts


def test_artwork_changes_only_art_not_other_content_fields(rig):
    result = invoke(rig, "artwork")
    assert result["verified"] is True
    original = ET.fromstring(ITEM)
    for raw in rig.posts:
        content = ET.fromstring(raw[1]).find("ContentItem")
        assert content.attrib == original.attrib
        assert content.findtext("extra") == "keep"
        assert content.findtext("itemName") == "Example"
        assert content.findtext("containerArt") == "http://example.invalid/new.png"


def test_artwork_snapshot_change_between_plan_and_write_is_blocked(rig):
    def interfere(path):
        if path == "/presets" and rig.reads.count(path) == 2:
            rig.xml = rig.xml.replace("/live", "/new-external")
    rig.on_read = interfere
    with pytest.raises(HTTPException):
        invoke(rig, "artwork")
    assert not rig.posts


def test_clone_source_change_during_write_does_not_commit_newer_unwritten_source(rig):
    def change():
        rig.db.query(Preset).filter_by(device_id=SOURCE, button=1).one().location = "http://example.invalid/newer"
        rig.db.commit()
    rig.on_write = change
    with pytest.raises(HTTPException):
        invoke(rig, "clone")
    assert rig.db.query(Preset).filter_by(device_id=ID, button=1).one().location.endswith("/live")


@pytest.mark.parametrize("action", ["set", "clone", "artwork"])
def test_preview_is_transport_free(rig, action):
    result = invoke(rig, action, dry_run=True)
    assert result["dry_run"] is True
    assert rig.constructed == 0


def test_partial_clone_never_commits_subset_as_complete(rig):
    rig.partial = True
    with pytest.raises(HTTPException):
        invoke(rig, "clone")
    assert all(row.location.endswith("/live") for row in rig.db.query(Preset).filter_by(device_id=ID).all())
    assert all(row.state == "RECONCILE" for row in rig.db.query(PresetMutation).filter_by(device_id=ID).all())


def test_lost_single_reply_is_resolved_by_actual_state_without_replay(rig):
    rig.lost = True
    result = invoke(rig, "set")
    assert result["verified"] is True
    assert len(rig.posts) == 1
    assert result["integrity"]["transport_uncertainties"][0]["button"] == 1
    assert rig.db.query(PresetMutation).one().state == "LOCAL_COMMIT"


def test_lost_clone_reply_stops_other_slots_and_requires_reconciliation(rig):
    rig.lost = True
    with pytest.raises(HTTPException):
        invoke(rig, "clone")
    assert len(rig.posts) == 1
    assert all(row.location.endswith("/live") for row in rig.db.query(Preset).filter_by(device_id=ID).all())
    assert all(row.state == "RECONCILE" for row in rig.db.query(PresetMutation).all())


def test_artwork_unchanged_is_observed_without_cache_rebase(rig):
    rig.xml = SLOTS.replace("http://example.invalid/art", "http://example.invalid/new.png")
    result = invoke(rig, "artwork")
    assert result["verified"] is True
    assert result["integrity"]["radio_write_count"] == 0
    assert result["integrity"]["local_projection_changed"] is False
    assert not rig.posts
    assert rig.reads.count("/presets") == 3
    assert rig.reads.count("/info") >= 3
    assert all(row.content_item_xml == ITEM for row in rig.db.query(Preset).filter_by(device_id=ID).all())


def test_artwork_skipped_slots_are_not_reported_as_complete(rig):
    rig.xml = "<presets/>"
    result = invoke(rig, "artwork")
    assert result["verified"] is False
    assert result["status"] == "partial"
    assert len(result["skipped_slots"]) == 2
    assert not rig.posts


@pytest.mark.parametrize("action", ["set", "clone", "artwork"])
def test_protected_target_is_rejected_before_client_construction(rig, action):
    rig.db.query(Device).filter_by(device_id=ID).one().ip_address = "192.168.50.25"
    rig.db.commit()
    with pytest.raises(HTTPException):
        invoke(rig, action)
    assert rig.constructed == 0


@pytest.mark.parametrize("action", ["set", "clone", "artwork"])
def test_unapproved_live_profile_is_never_written(rig, action):
    rig.identity = info(firmware="27.99.0")
    with pytest.raises(HTTPException):
        invoke(rig, action)
    assert not rig.posts


def test_clone_rejects_inconsistent_cached_content_before_transport(rig):
    rig.db.query(Preset).filter_by(device_id=SOURCE, button=1).one().source_account = "different-account"
    rig.db.commit()
    with pytest.raises(HTTPException):
        invoke(rig, "clone")
    assert rig.constructed == 0


@pytest.mark.parametrize("partial", [False, True])
def test_secondary_artwork_status_failure_cannot_change_transaction_outcome(rig, monkeypatch, partial):
    def fail_status(*args):
        raise RuntimeError("synthetic UI status storage failure")
    monkeypatch.setattr(api, "_record_preset_sync_state", fail_status)
    rig.partial = partial
    if partial:
        with pytest.raises(HTTPException) as error:
            invoke(rig, "artwork")
        assert error.value.detail["reconciliation_required"] is True
        assert all(row.state == "RECONCILE" for row in rig.db.query(PresetMutation).all())
    else:
        result = invoke(rig, "artwork")
        assert result["verified"] is True
        assert result["status_saved"] is False
        assert all(row.state == "LOCAL_COMMIT" for row in rig.db.query(PresetMutation).all())
