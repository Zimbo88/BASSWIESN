"""Grouping defaults never silently request a common volume. No hardware I/O."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, MultiroomScenario
from basswiesn.app.routers.multiroom import _preserve_volumes


@pytest.mark.parametrize("payload,expected", [
    ({}, True), ({"volume": None}, True), ({"volume": ""}, True),
    ({"preserve_volumes": True, "volume": 5}, True),
    ({"no_volume_change": True, "volume": 5}, True),
    ({"volume": 0}, False), ({"volume": 15}, False),
    ({"set_start_volumes": True}, False),
    ({"set_start_volumes": False}, True),
    ({"preserve_volumes": False, "volume": 5}, False),
])
def test_explicit_volume_choice_or_safe_default(payload, expected):
    assert _preserve_volumes(payload) is expected


@pytest.mark.parametrize("field", ["preserve_volumes", "no_volume_change", "set_start_volumes"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_non_boolean_policy_is_not_an_implicit_write_authorization(field, value):
    with pytest.raises(HTTPException) as exc:
        _preserve_volumes({field: value})
    assert exc.value.status_code == 400


def test_new_scene_defaults_to_preserve_without_rewriting_existing_scene():
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="VOLUME-DEFAULT", ip_address="192.0.2.44"))
        db.add(MultiroomScenario(name="explicit-old", master_device_id="VOLUME-DEFAULT",
                                 preserve_volumes=False, volume=12))
        db.commit()
    with TestClient(create_web_app()) as client:
        reply = client.post("/api/multiroom/scenarios", json={
            "name": "default-new", "master_device_id": "VOLUME-DEFAULT",
            "member_device_ids": [],
        })
        assert reply.status_code == 200
    with app_db.SessionLocal() as db:
        new = db.query(MultiroomScenario).filter_by(name="default-new").one()
        old = db.query(MultiroomScenario).filter_by(name="explicit-old").one()
        assert new.preserve_volumes is True and new.volume is None
        assert old.preserve_volumes is False and old.volume == 12
