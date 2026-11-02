"""Synthetic-only learning tools, app-only profiles and calendar boundaries."""
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import RuntimeState, Setting
from basswiesn.app.routers import lab_learning as api
from basswiesn.app.services.lab_learning import SCENARIOS
from basswiesn.app.services.listening_stats import listening_summary
from test_lab_workbench import lab
from test_listening_stats_300 import row

pytestmark = pytest.mark.unit
BASE = "/api/lab/learning"
LAYOUT = {"fields": ["station", "artist", "title", "clock"],
          "field_order": ["station", "artist", "title", "clock", "other"]}


def test_every_simulator_step_is_synthetic_and_never_persists_radio_state(lab):
    with app_db.SessionLocal() as db:
        before = [(r.key, r.value) for r in db.query(RuntimeState).order_by(RuntimeState.key)]
    assert len(lab.get(BASE+"/simulator").json()["scenarios"]) == 7
    for scenario in SCENARIOS:
        for step in range(4):
            result = lab.post(BASE+"/simulator", json={"scenario":scenario,"step":step})
            assert result.status_code == 200
            value = result.json()
            assert value["synthetic"] and not value["radio_contacted"] and not value["state_saved"]
            assert value["step"] == step and value["steps"] == 4
            assert not value["recovery_executed"] and not value["audible_output_verified"]
            assert value["diagnosis"]["root_cause"] == "NOT_ESTABLISHED"
    with app_db.SessionLocal() as db:
        assert before == [(r.key, r.value) for r in db.query(RuntimeState).order_by(RuntimeState.key)]


def test_report_failure_never_claims_audio_failure_and_partial_zone_never_passes(lab):
    report = lab.post(BASE+"/simulator", json={"scenario":"reporting_failure","step":2}).json()
    assert report["snapshot"]["playback"]["status"] == "PLAYING"
    assert report["diagnosis"]["observations"][0]["not_proven"] == "audio_failure"
    partial = lab.post(BASE+"/simulator", json={"scenario":"partial_zone","step":1}).json()
    assert partial["zone"]["result"] == "PARTIAL_NOT_VERIFIED"


@pytest.mark.parametrize("body", [{"scenario":"unknown"},{"scenario":"stale_ip","step":4},
    {"scenario":"stale_ip","step":True},{"scenario":"stale_ip","device_id":"REAL"}])
def test_simulator_rejects_arbitrary_input(lab, body):
    assert lab.post(BASE+"/simulator",json=body).status_code == 422


@pytest.mark.parametrize("mode", ["easy","standard"])
def test_all_learning_routes_require_lab(lab, mode):
    with app_db.SessionLocal() as db:
        db.query(Setting).filter_by(key="ui_mode").one().value = mode; db.commit()
    for method, path, body in [
        ("GET","/simulator",None),("POST","/simulator",{"scenario":"stale_ip"}),
        ("GET","/display-profiles",None),("POST","/display-profiles",dict(LAYOUT,name="Example")),
        ("POST","/display-preview",LAYOUT),("DELETE","/display-profiles/"+"0"*32,{"revision":"0"*64})]:
        result = lab.request(method,BASE+path,**({"json":body} if body else {}))
        assert result.status_code == 409


def test_profile_save_list_preview_delete_is_app_only(lab):
    result = lab.post(BASE+"/display-profiles",json=dict(LAYOUT,name="Example layout"))
    assert result.status_code == 200
    value=result.json(); profile=value["profile"]
    assert not value["radio_contacted"] and not value["applied_to_radio"]
    assert lab.get(BASE+"/display-profiles").json()["items"] == [profile]
    for scenario in ["normal","missing","long"]:
        preview=lab.post(BASE+"/display-preview",json=dict(LAYOUT,scenario=scenario)).json()
        assert preview["synthetic"] and not preview["native_layout_verified"]
        assert len(preview["title"]) <= 256 and "20:15" in preview["title"]
        if scenario == "missing": assert set(preview["missing_fields"]) == {"artist","title"}
    path=BASE+"/display-profiles/"+profile["id"]
    assert lab.request("DELETE",path,json={"revision":"0"*64}).status_code == 409
    assert lab.request("DELETE",path,json={"revision":profile["revision"]}).json()["deleted"]
    assert lab.get(BASE+"/display-profiles").json()["items"] == []
    with app_db.SessionLocal() as db:
        assert db.query(Setting).filter(Setting.key.like("station_display:%")).count() == 0


@pytest.mark.parametrize("extra", [{"name":"  "},{"name":"x\ny"},{"fields":["unknown"]},
    {"fields":["clock","clock"]},{"field_order":["clock"]},{"device_id":"REAL"}])
def test_profile_validation(lab, extra):
    assert lab.post(BASE+"/display-profiles",json=dict(dict(LAYOUT,name="Example"),**extra)).status_code == 422


def test_profile_limit_is_atomic(lab, monkeypatch):
    monkeypatch.setattr(api,"LIMIT",2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses=list(pool.map(lambda n:lab.post(BASE+"/display-profiles",json=dict(LAYOUT,name=f"Example {n}")).status_code,range(4)))
    assert sorted(statuses) == [200,200,409,409]
    assert len(lab.get(BASE+"/display-profiles").json()["items"]) == 2


@pytest.mark.parametrize("day,hours", [("2026-03-29",23),("2026-10-25",25)])
def test_local_day_handles_dst_length(day,hours):
    zone=ZoneInfo("Europe/Berlin")
    start=datetime.fromisoformat(day).replace(tzinfo=zone).astimezone(UTC)
    end=(datetime.fromisoformat(day)+timedelta(days=1)).replace(tzinfo=zone).astimezone(UTC)
    now=end-timedelta(seconds=1)
    value=row(started_at=start,ended_at=now,end_reason="user_stop")
    result=listening_summary([value],now=now,tolerance=360,device_names={},station_names={1:"Example"},timezone_name=zone.key)
    assert result["periods"]["today"]["seconds"] == hours*3600-1
    assert result["periods"]["today"]["observed_end_reasons"] == {"user_stop":1}
    assert not result["end_reason_is_cause_proof"]


def test_stats_uses_saved_timezone_and_rejects_invalid_override(lab,monkeypatch):
    from basswiesn.app.routers import api as stats_api
    monkeypatch.setattr(stats_api,"utc_now",lambda:datetime(2026,9,23,22,30,tzinfo=UTC))
    with app_db.SessionLocal() as db:
        db.add(Setting(key="default_timezone",value="Europe/Berlin"));db.commit()
    result=lab.get("/api/stats/playback").json()
    assert result["timezone"] == result["listening"]["timezone"] == "Europe/Berlin"
    assert result["listening"]["periods"]["today"]["window_start_utc"] == "2026-09-23T22:00:00+00:00"
    assert lab.get("/api/stats/playback?timezone=Not/AZone").status_code == 422
    assert lab.get("/api/stats/playback?timezone=UTC").json()["timezone"] == "UTC"
