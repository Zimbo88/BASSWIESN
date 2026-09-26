"""Offline contract/safety tests. Global socket guard forbids household I/O."""
from datetime import UTC, datetime, timedelta
import csv
import hashlib
import io
import json
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import (
    Device, DiagnosticEvent, MetadataState, PlaybackHealthState, PlaybackState,
    PlayHistory, ProviderHealthState, ReportingState, RestrictionState,
    RuntimeState, Setting, Station,
)
from basswiesn.app.routers import stations_presets
from basswiesn.app.services import lab_workbench as svc

pytestmark = pytest.mark.unit
BASE = "/api/lab/workbench"
D = "LAB-FIXTURE"
R = BASE + "/devices/" + D


@pytest.fixture
def lab():
    now = datetime.now(UTC)
    with app_db.SessionLocal() as db:
        station = Station(name="Example Station", stream_url="https://example.invalid/current.mp3", stream_format="mp3")
        db.add_all([station, Device(device_id=D, name="Example Radio", model="SoundTouch", ip_address="192.0.2.42"),
                    Device(device_id="OTHER", name="Other", ip_address="192.0.2.43"),
                    Device(device_id="PROTECTED", name="Private Radio", ip_address="192.0.2.25"),
                    Setting(key="protected_device_ips", value="192.0.2.25"),
                    Setting(key="ui_mode", value="lab"), Setting(key="lan_host", value="192.0.2.100")])
        db.flush()
        db.add(PlayHistory(device_id=D, station_id=station.id, station_name="Old Station Name",
                           stream_url="https://example.invalid/expired.mp3?secret=never-output",
                           started_at=now-timedelta(minutes=20), ended_at=now-timedelta(minutes=5),
                           last_confirmed_playing_at=now-timedelta(minutes=5), end_reason="user_stop"))
        db.add_all([
            PlaybackState(device_id=D, source="LOCAL_INTERNET_RADIO", status="PLAYING", volume=1),
            PlaybackHealthState(device_id=D, state="PLAYING", stream_alive=True, source_valid=True),
            MetadataState(device_id=D, source="LOCAL_INTERNET_RADIO", provenance="STREAM", track="Example Song",
                          artist="Example Artist", updated_at=now, stale=False),
        ])
        db.commit()
    with TestClient(create_web_app(background_tasks=False)) as client:
        yield client


def test_cache_overview_is_read_only_and_no_remote_contact(lab, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("No radio transport allowed")
    monkeypatch.setattr(stations_presets, "SoundTouchClient", denied)
    with app_db.SessionLocal() as db:
        before = db.query(RuntimeState).count()
    result = lab.get(R + "/overview").json()
    assert result["snapshot"]["radio_contacted"] is False
    assert result["snapshot"]["metadata"]["fields"]["track"] == "Example Song"
    assert result["snapshot"]["metadata"]["state"] == "RECENT"
    assert result["snapshot"]["metadata"]["selection_match"] == "UNKNOWN"
    assert result["diagnosis"]["root_cause"] == "NOT_ESTABLISHED"
    assert result["recent"]["items"][0]["name"] == "Example Station"
    assert "expired.mp3" not in json.dumps(result)
    with app_db.SessionLocal() as db:
        assert db.query(RuntimeState).count() == before


@pytest.mark.parametrize("mode", ["easy", "standard", "", "LAB"])
def test_every_api_requires_exact_lab_before_target_or_transport(lab, mode):
    with app_db.SessionLocal() as db:
        db.query(Setting).filter_by(key="ui_mode").one().value = mode
        db.commit()
    paths = [("get", BASE+"/devices", None), ("get", BASE+"/formats", None),
             ("get", R+"/overview", None), ("get", R+"/listening.csv", None),
             ("post", R+"/qr", {"origin":"http://example.invalid"}),
             ("get", R+"/snapshots", None), ("post", R+"/snapshots", {}),
             ("get", R+"/snapshots/"+"0"*32+"/compare", None),
             ("get", R+"/snapshots/"+"0"*32+"/download", None),
             ("delete", R+"/snapshots/"+"0"*32, None),
             ("post", R+"/recent/1/play", {"approve":True,"revision":"0"*64})]
    for method, path, payload in paths:
        response = lab.request(method, path, **({"json":payload} if payload is not None else {}))
        assert response.status_code == 409, (method, path, response.text)
        assert response.json()["detail"]["code"] == "LAB_MODE_REQUIRED"


def test_protection_applies_to_listing_and_direct_workbench_routes(lab):
    assert "PROTECTED" not in str(lab.get(BASE+"/devices").json())
    for suffix in ["/overview", "/snapshots", "/listening.csv"]:
        assert lab.get(BASE+"/devices/PROTECTED"+suffix).status_code == 403
    assert lab.post(BASE+"/devices/PROTECTED/qr", json={"origin":"http://example.invalid"}).status_code == 403


def test_diagnosis_separates_reporting_warning_from_playback(lab):
    with app_db.SessionLocal() as db:
        db.add(ReportingState(device_id=D, provider_id="private-account", state="DEGRADED", retry_count=2,
                              report_url="https://example.invalid?secret=not-public", last_failure_json='{"secret":"not-public"}'))
        db.add(RestrictionState(device_id=D, source_key="private-key", timer_enabled=True,
                                inactivity_timeout_s=21600, effective_until=datetime.now(UTC)+timedelta(hours=6)))
        db.commit()
    result = lab.get(R+"/overview").json()
    assert {o["code"] for o in result["diagnosis"]["observations"]} == {"REPORTING_ONLY", "TIMER_CONFIGURED"}
    assert result["snapshot"]["health"]["state"] == "PLAYING"
    assert result["diagnosis"]["automatic_action"] is False
    assert "not-public" not in json.dumps(result) and "private-account" not in json.dumps(result)


@pytest.mark.parametrize("condition,expected", [("old","STALE"),("future","UNKNOWN"),("different","SELECTION_MISMATCH"),("missing","NOT_OBSERVED")])
def test_metadata_freshness_and_missing_evidence(lab, condition, expected):
    with app_db.SessionLocal() as db:
        row = db.query(MetadataState).one()
        if condition == "old": row.updated_at = datetime.now(UTC)-timedelta(minutes=10)
        if condition == "future": row.updated_at = datetime.now(UTC)+timedelta(hours=1)
        if condition == "different": row.source = "AIRPLAY"
        if condition == "missing": db.delete(row)
        db.commit()
    assert lab.get(R+"/overview").json()["snapshot"]["metadata"]["state"] == expected


def test_latest_legacy_runtime_cache_used_without_fabricated_volume(lab):
    with app_db.SessionLocal() as db:
        db.add(RuntimeState(key=f"device:{D}:runtime_state", value=json.dumps({"current_source":"STANDBY","playback_state":"STOP_STATE","secret":"dont-show"})))
        db.commit()
    state = lab.get(R+"/overview").json()["snapshot"]["playback"]
    assert state["source"] == "STANDBY" and state["volume"] is None
    assert "secret" not in str(state)


def test_timeline_bounded_chronological_scoped_and_no_raw_payload(lab):
    now = datetime.now(UTC)
    with app_db.SessionLocal() as db:
        for i in range(110):
            db.add(DiagnosticEvent(event_id=str(i), device_id=D, occurred_at=now-timedelta(seconds=120-i),
                                   code=f"EVENT_{i}", message="secret: never-display", evidence_json='{"password":"never-display"}'))
        db.add(DiagnosticEvent(event_id="other", device_id="OTHER", code="OTHER"))
        db.add(DiagnosticEvent(event_id="future", device_id=D, occurred_at=now+timedelta(hours=1), code="FUTURE"))
        db.commit()
    result = lab.get(R+"/overview?hours=1").json()["timeline"]
    assert result["truncated"] and len(result["items"]) == 100
    assert result["items"][0]["code"] == "EVENT_10"
    assert result["items"][-1]["code"] == "EVENT_109"
    assert "never-display" not in str(result)
    assert lab.get(R+"/overview?hours=999999").status_code == 422


def test_replay_delegates_current_mapping_safe_volume_and_requires_approval(lab, monkeypatch):
    recent = lab.get(R+"/overview").json()["recent"]["items"][0]
    fake = AsyncMock(return_value={"verified":True})
    monkeypatch.setattr(stations_presets, "play_station_on_device", fake)
    route = R+f'/recent/{recent["history_id"]}/play'
    assert lab.post(route, json={"approve":False,"revision":recent["revision"]}).status_code == 409
    for extra in [{"safe_volume":35}, {"target_volume":35}, {"approve":"yes"}]:
        assert lab.post(route, json={"approve":True,"revision":recent["revision"],**extra}).status_code == 422
    assert fake.await_count == 0
    result = lab.post(route,json={"approve":True,"revision":recent["revision"]})
    assert result.status_code == 200
    assert fake.await_args.args[0:3] == (D,recent["station_id"],{"safe_volume":1,"trigger":"lab_recent"})
    with app_db.SessionLocal() as db:
        db.query(Station).one().stream_url = "https://example.invalid/changed.mp3"
        db.commit()
    assert lab.post(route,json={"approve":True,"revision":recent["revision"]}).json()["detail"]["code"] == "STATION_CHANGED"
    assert fake.await_count == 1


def test_replay_preserves_existing_audio_lock_before_hardware(lab, monkeypatch):
    recent = lab.get(R+"/overview").json()["recent"]["items"][0]
    with app_db.SessionLocal() as db:
        db.add(RuntimeState(key=f"device:{D}:audio_safety",value='{"locked":true}'))
        db.commit()
    def denied(*args, **kwargs): raise AssertionError("Locked replay constructed transport")
    monkeypatch.setattr(stations_presets,"SoundTouchClient",denied)
    result = lab.post(R+f'/recent/{recent["history_id"]}/play',json={"approve":True,"revision":recent["revision"]})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "audio_safety_locked"


def test_missing_station_and_cross_device_history_cannot_replay(lab):
    item = lab.get(R+"/overview").json()["recent"]["items"][0]
    payload={"approve":True,"revision":item["revision"]}
    assert lab.post(BASE+f'/devices/OTHER/recent/{item["history_id"]}/play',json=payload).status_code == 409
    with app_db.SessionLocal() as db:
        db.delete(db.query(Station).one());db.commit()
    assert not lab.get(R+"/overview").json()["recent"]["items"][0]["replayable"]
    assert lab.post(R+f'/recent/{item["history_id"]}/play',json=payload).status_code == 409


def test_snapshot_save_hash_diff_export_delete_and_device_scope(lab):
    created = lab.post(R+"/snapshots",json={"label":"Personal label"})
    assert created.status_code == 200
    record = created.json(); path=R+"/snapshots/"+record["id"]
    content = json.dumps(record["snapshot"],ensure_ascii=False,sort_keys=True,separators=(",",":"))
    assert hashlib.sha256(content.encode()).hexdigest() == record["sha256"]
    assert lab.get(path+"/compare").json()["changes"] == []
    assert lab.get(BASE+"/devices/OTHER/snapshots/"+record["id"]+"/download").status_code == 404
    with app_db.SessionLocal() as db:
        db.query(PlaybackState).one().volume=2;db.commit()
    diff = lab.get(path+"/compare").json()
    assert diff["changes"] == [{"field":"playback.volume","before":1,"after":2,"state_class":"VOLATILE"}]
    assert diff["restore_verified"] is False
    export = lab.get(path+"/download")
    assert "Personal label" not in export.text and D not in export.text and "192.0.2.42" not in export.text
    assert lab.delete(path).json()["deleted"]
    assert lab.get(path+"/download").status_code == 404


def test_snapshot_bounds_and_integrity(lab):
    ids=[]
    for _ in range(10):
        response=lab.post(R+"/snapshots",json={});assert response.status_code==200
        ids.append(response.json()["id"])
    assert lab.post(R+"/snapshots",json={}).json()["detail"]["code"]=="SNAPSHOT_LIMIT"
    with app_db.SessionLocal() as db:
        row=db.query(RuntimeState).filter(RuntimeState.key.endswith(ids[0])).one()
        value=json.loads(row.value);value["snapshot"]["device"]["model"]="Changed"
        row.value=json.dumps(value);db.commit()
    assert lab.get(R+"/snapshots/"+ids[0]+"/download").status_code==409
    assert lab.delete(R+"/snapshots/"+ids[0]).status_code==200
    with app_db.SessionLocal() as db:
        db.query(RuntimeState).filter(RuntimeState.key.endswith(ids[1])).one().value="invalid-json"
        db.commit()
    assert len(lab.get(R+"/snapshots").json()["items"])==9
    assert lab.get(R+"/snapshots/"+ids[1]+"/compare").status_code==409
    assert lab.delete(R+"/snapshots/"+ids[1]).status_code==200


def test_snapshot_nested_observation_ages_do_not_create_content_changes():
    a={"providers":[{"state":"READY","age_seconds":1,"observed_at":"a","freshness":"RECENT"}]}
    b={"providers":[{"state":"READY","age_seconds":2000,"observed_at":"b","freshness":"STALE"}]}
    assert svc.compare_snapshots(a,b)["changes"]==[]


def test_csv_filters_and_formula_injection(lab):
    now=datetime.now(UTC)
    with app_db.SessionLocal() as db:
        row = db.query(PlayHistory).one()
        row.station_name = row.station_display_name = "=HYPERLINK(example)"
        for changes in [{"is_confirmed":False},{"internal_event":True},{"is_internal":True},{"success":0},
                        {"source":"STANDBY"},{"trigger":"six_hour_refresh"},{"trigger_type":"background_probe"}]:
            db.add(PlayHistory(device_id=D,station_name="MUST-NOT-EXPORT",started_at=now-timedelta(hours=1),ended_at=now,**changes))
        db.commit()
    response=lab.get(R+"/listening.csv?days=7")
    rows=list(csv.reader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert len(rows)==2 and rows[1][0].startswith("'=HYPERLINK")
    assert rows[1][4]=="900" and rows[1][6]=="false"
    assert "expired.mp3" not in response.text
    assert lab.get(R+"/listening.csv?days=0").status_code==422


@pytest.mark.parametrize("origin",["javascript:alert(1)","http://user:password@example.invalid","http://example.invalid?token=secret","http://example.invalid/prefix","http://example.invalid#secret","http://example.invalid:bad","http://example.invalid\\evil"])
def test_qr_rejects_credentials_paths_and_non_web_origins(lab,origin):
    assert lab.post(R+"/qr",json={"origin":origin}).status_code==422


def test_qr_is_local_real_svg_with_quiet_zone_and_stable_identity(lab):
    result=lab.post(R+"/qr",json={"origin":"http://192.0.2.100:1328"}).json()
    assert result["url"]==f"http://192.0.2.100:1328/remote/{D}"
    root=ET.fromstring(result["svg"])
    assert root.tag.endswith("svg")
    paths=[e for e in root if e.tag.endswith("path")]
    assert len(paths)==1 and len(paths[0].get("d"))>1000
    assert result["radio_contacted"] is False and result["contains_credentials"] is False


def test_format_audit_no_native_hls_or_transcoding_claim(lab):
    with app_db.SessionLocal() as db:
        db.add_all([Station(name="HLS",stream_url="https://example.invalid/live.m3u8",is_hls=1),
                    Station(name="Internal",stream_url="https://example.invalid/internal",internal=True)])
        db.commit()
    result=lab.get(BASE+"/formats").json()
    assert len(result["items"])==2
    assert result["transcoding_available"] is False and result["network_probed"] is False
    assert {r["assessment"] for r in result["items"]}=={"DIRECT_CANDIDATE","HLS_REQUIRES_ADAPTATION"}


def test_snapshot_limit_holds_across_concurrent_tabs(lab):
    from concurrent.futures import ThreadPoolExecutor
    for _ in range(9):
        assert lab.post(R+"/snapshots",json={}).status_code==200
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:lab.post(R+"/snapshots",json={}).status_code,range(2)))
    assert sorted(outcomes)==[200,409]
    assert len(lab.get(R+"/snapshots").json()["items"])==10


def test_snapshot_export_omits_free_logs_urls_and_assignment_secrets(lab):
    with app_db.SessionLocal() as db:
        meta=db.query(MetadataState).one()
        meta.track="secret: must-not-escape"
        meta.artwork_url="https://example.invalid/art?token=must-not-escape"
        db.add(ProviderHealthState(device_id=D,provider_id="private-account",source="LOCAL_INTERNET_RADIO",
                                   evidence_json='{"password":"must-not-escape"}'))
        db.commit()
    identity=lab.post(R+"/snapshots",json={"label":"private-user"}).json()["id"]
    export=lab.get(R+"/snapshots/"+identity+"/download")
    assert export.status_code==200
    for forbidden in ("must-not-escape","private-account","private-user","https://example.invalid"):
        assert forbidden not in export.text


def test_recents_deduplicate_and_do_not_offer_future_or_internal_history(lab):
    now=datetime.now(UTC)
    with app_db.SessionLocal() as db:
        station=db.query(Station).one()
        db.add(PlayHistory(device_id=D,station_id=station.id,station_name="duplicate",started_at=now-timedelta(days=1)))
        db.add(PlayHistory(device_id=D,station_name="future",started_at=now+timedelta(days=1)))
        db.add(PlayHistory(device_id=D,station_name="internal",trigger="background_probe",started_at=now))
        db.commit()
    assert len(lab.get(R+"/overview").json()["recent"]["items"])==1


def test_strict_bounded_inputs(lab):
    assert lab.post(R+"/snapshots",json={"label":"x"*61}).status_code==422
    assert lab.post(R+"/snapshots",json={"raw_xml":"<anything/>"}).status_code==422
    assert lab.get(R+"/overview?hours=0").status_code==422
    assert lab.get(R+"/listening.csv?days=366").status_code==422
    assert lab.post(R+"/qr",json={"origin":"https://"+"x"*241}).status_code==422
