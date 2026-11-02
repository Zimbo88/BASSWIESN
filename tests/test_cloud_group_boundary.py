"""Bose compatibility group state, never a real SoundTouch Zone."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.main import create_cloud_app, create_web_app
from basswiesn.app.models import Device, Setting
from basswiesn.app.routers import cloud
from basswiesn.app.services import cloud_groups

pytestmark = pytest.mark.integration


@pytest.fixture
def clients(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_auth_mode", "required")
    monkeypatch.setattr(settings, "device_allowed_peers", ("192.0.2.42", "192.0.2.43", "192.0.2.44"))
    cloud.GROUPS.clear()
    with app_db.SessionLocal() as db:
        for identity, address, account in [
            ("RADIOA", "192.0.2.42", "account-a"),
            ("RADIOB", "192.0.2.43", "account-b"),
            ("RADIOC", "192.0.2.44", "account-a"),
        ]:
            db.add(Device(device_id=identity, ip_address=address,
                          info_xml=f'<info deviceID="{identity}"><margeAccountUUID>{account}</margeAccountUUID></info>'))
        db.commit()
    app = create_cloud_app()
    return tuple(TestClient(app, base_url="http://device-service.invalid", client=(ip, 41000))
                 for ip in settings.device_allowed_peers)


def test_allowed_radio_cannot_read_other_accounts_group_state(clients):
    first, second, _ = clients
    assert first.post("/group/create", json={"group_id": "g1", "leader": "RADIOA", "members": ["RADIOC"]}).status_code == 200
    assert second.get("/group").json()["groups"] == []


def test_allowed_radio_cannot_delete_another_accounts_group(clients):
    first, second, _ = clients
    assert first.post("/group/create", json={"group_id": "g1", "leader": "RADIOA", "members": []}).status_code == 200
    assert second.post("/group/delete", json={"group_id": "g1"}).status_code == 404
    assert len(first.get("/group").json()["groups"]) == 1


def test_allowed_radio_cannot_impersonate_leader(clients):
    first, _, _ = clients
    response = first.post("/group/create", json={"group_id": "g1", "leader": "RADIOB", "members": []})
    assert response.status_code == 403


def test_other_account_member_is_rejected(clients):
    first, _, _ = clients
    response = first.post("/group/create", json={"group_id": "g1", "leader": "RADIOA", "members": ["RADIOB"]})
    assert response.status_code == 403


def create(client, **changes):
    return client.post("/group/create", json={"group_id": "g1", "leader": "RADIOA", "members": ["RADIOC"], **changes})


def test_current_owner_can_update_read_and_delete_across_factories(clients):
    first, _, same_account = clients
    assert create(first).json()["hardware_verified"] is False
    assert cloud.GROUPS == {}  # never mix authenticated records with legacy state
    other_worker = TestClient(create_web_app(background_tasks=False), base_url="http://device-service.invalid",
                              client=("192.0.2.42", 41000))
    group = other_worker.get("/group").json()["groups"][0]
    assert group["revision"] == 1
    assert same_account.get("/group").json()["groups"][0]["group_id"] == "g1"
    response = other_worker.post("/group/update", json={"id": "g1", "members": [], "source": "AUX",
                                                       "status": "paused", "expected_revision": 1})
    assert response.status_code == 200 and response.json()["group"]["revision"] == 2
    assert response.json()["state_only"] is True
    assert first.get("/group").json()["groups"][0]["members"] == []
    assert first.post("/group/delete", json={"id": "g1", "expected_revision": 2}).json()["deleted"]
    assert other_worker.get("/group").json()["groups"] == []


@pytest.mark.parametrize("action", ["update", "delete"])
def test_same_account_member_is_not_group_owner(clients, action):
    first, _, member = clients
    assert create(first).status_code == 200
    assert member.post("/group/" + action, json={"id": "g1"}).status_code == 403
    assert first.get("/group").json()["groups"][0]["leader"] == "RADIOA"


def test_account_namespaces_can_use_same_group_id(clients):
    first, other, _ = clients
    assert create(first).status_code == 200
    assert create(other, leader="RADIOB", members=[]).status_code == 200
    assert other.get("/group").json()["groups"][0]["leader"] == "RADIOB"
    assert first.get("/group").json()["groups"][0]["leader"] == "RADIOA"


@pytest.mark.parametrize("payload", [
    [], None, True, "text", {"id": "../invalid"}, {"id": 42},
    {"group_id": "g1", "id": "other"}, {"leader": "RADIOA", "master": "RADIOC"},
    {"members": "RADIOC"}, {"members": [42]}, {"members": None},
    {"members": ["RADIOC", "radioc"]}, {"members": ["RADIOA"]},
    {"members": ["x"] * 33}, {"source": []}, {"source": "x" * 257}, {"source": "bad\nlabel"},
    {"status": {}}, {"status": "x" * 33}, {"account_id": "account-b"}, {"token": "private-canary"},
])
def test_unreviewed_group_schema_is_bounded_and_has_no_state_change(clients, payload):
    first, _, _ = clients
    if isinstance(payload, dict):
        payload = {"group_id": "g1", "leader": "RADIOA", "members": [], **payload}
    response = first.post("/group/create", json=payload)
    assert response.status_code == 400, response.text
    assert "private-canary" not in response.text
    assert first.get("/group").json()["groups"] == []


@pytest.mark.parametrize("content,content_type", [("{", "application/json"), ('<group/>', "application/xml"),
                                                 (b"\xff", "application/json")])
def test_invalid_group_body_is_a_bounded_client_error(clients, content, content_type):
    response = clients[0].post("/group/create", content=content, headers={"Content-Type": content_type})
    assert response.status_code == 400


def test_no_implicit_create_or_leader_takeover(clients):
    first, _, _ = clients
    assert first.post("/group/update", json={"group_id": "missing"}).status_code == 404
    assert create(first).status_code == 200
    assert create(first).status_code == 409
    assert first.post("/group/update", json={"group_id": "g1", "leader": "RADIOC"}).status_code == 403
    assert first.post("/group/delete", json={"group_id": "g1", "members": []}).status_code == 400


@pytest.mark.parametrize("revision", [0, 2, True, "1"])
def test_stale_revision_does_not_overwrite_or_delete(clients, revision):
    first, _, _ = clients
    assert create(first).status_code == 200
    for action in ("update", "delete"):
        assert first.post("/group/" + action, json={"id": "g1", "expected_revision": revision}).status_code == 409
    assert first.get("/group").json()["groups"][0]["revision"] == 1


@pytest.mark.parametrize("kind", ["missing", "ambiguous", "no_account", "duplicate_id"])
def test_inventory_ambiguity_is_not_a_group_identity(clients, kind):
    with app_db.SessionLocal() as db:
        if kind == "missing":
            db.query(Device).filter_by(device_id="RADIOA").delete()
        elif kind == "ambiguous":
            db.add(Device(device_id="AMBIGUOUS", ip_address="192.0.2.42"))
        elif kind == "duplicate_id":
            db.add(Device(device_id="radioa", ip_address="192.0.2.88"))
        else:
            db.query(Device).filter_by(device_id="RADIOA").one().info_xml = "<info/>"
        db.commit()
    assert create(clients[0]).status_code == 403


@pytest.mark.parametrize("identity", ["RADIOA", "RADIOC"])
def test_protected_inventory_identity_is_rejected_without_radio_contact(clients, identity, monkeypatch):
    monkeypatch.setattr(cloud_groups, "is_device_access_protected",
                        lambda ip, identifier: identifier == identity)
    assert create(clients[0]).status_code == 403


@pytest.mark.parametrize("raw", ['{', '[]', '{"schema":2}', '{"schema":1,"account":"account-a","group":{}}'])
def test_corrupt_group_is_not_silently_repaired(clients, raw):
    first, _, _ = clients
    assert create(first).status_code == 200
    with app_db.SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key.startswith(cloud_groups.PREFIX)).one()
        row.value = raw
        db.commit()
    assert first.get("/group").status_code == 503
    assert first.post("/group/update", json={"id": "g1"}).status_code == 503
    with app_db.SessionLocal() as db:
        assert db.query(Setting).filter(Setting.key.startswith(cloud_groups.PREFIX)).one().value == raw


def test_group_capacity_is_atomic_and_bounded(clients, monkeypatch):
    monkeypatch.setattr(cloud_groups, "MAX_GROUPS", 1)
    barrier = Barrier(2)
    def contender(index):
        # Two authorized senders in the same account exercise the database's
        # shared capacity. Concurrent requests from one peer can correctly be
        # rejected earlier by the independent ingress concurrency boundary.
        peer, leader = [("192.0.2.42", "RADIOA"), ("192.0.2.44", "RADIOC")][index]
        client = TestClient(create_cloud_app(), base_url="http://device-service.invalid",
                            client=(peer, 41000))
        barrier.wait(timeout=5)
        return create(client, group_id=f"race-{index}", leader=leader, members=[]).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(contender, [0, 1])) == [200, 409]
    assert len(clients[0].get("/group").json()["groups"]) == 1


def test_storage_failure_does_not_report_success(clients):
    first, _, _ = clients
    assert create(first).status_code == 200
    with app_db.SessionLocal() as db:
        with patch.object(db, "commit", side_effect=OperationalError("query", {}, Exception("private-canary"))):
            with pytest.raises(cloud_groups.GroupError) as caught:
                cloud_groups.group_operation(db, peer="192.0.2.42", action="update", payload={"id": "g1", "source": "AUX"})
        assert caught.value.status_code == 503 and "private-canary" not in str(caught.value)
    assert first.get("/group").json()["groups"][0]["source"] == ""


@pytest.mark.parametrize("action", ["update", "delete"])
def test_concurrent_change_is_never_overwritten(clients, action):
    first, _, _ = clients
    assert create(first).status_code == 200
    with app_db.SessionLocal() as db:
        original_execute = db.execute
        changed = False
        def injected(statement, *args, **kwargs):
            nonlocal changed
            if (getattr(statement, "is_update", False) or getattr(statement, "is_delete", False)) and not changed:
                changed = True
                with app_db.SessionLocal() as rival:
                    row = rival.query(Setting).filter(Setting.key.startswith(cloud_groups.PREFIX)).one()
                    value = json.loads(row.value)
                    value["group"]["source"] = "RIVAL"
                    value["group"]["revision"] = 2
                    row.value = json.dumps(value)
                    rival.commit()
            return original_execute(statement, *args, **kwargs)
        with patch.object(db, "execute", injected), pytest.raises(cloud_groups.GroupError) as caught:
            cloud_groups.group_operation(db, peer="192.0.2.42", action=action, payload={"id": "g1", **({"source": "STALE"} if action == "update" else {})})
        assert caught.value.status_code == 409
    assert first.get("/group").json()["groups"][0]["source"] == "RIVAL"


@pytest.mark.parametrize("change", [
    {"group_id": "wrong-key"}, {"revision": True}, {"revision": 0},
    {"members": None}, {"members": ["RADIOC", "radioc"]}, {"members": [False]},
    {"source": []}, {"leader": "bad/identity"},
])
def test_invalid_stored_fields_fail_closed(clients, change):
    first, _, _ = clients
    assert create(first).status_code == 200
    with app_db.SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key.startswith(cloud_groups.PREFIX)).one()
        value = json.loads(row.value)
        value["group"].update(change)
        row.value = json.dumps(value)
        db.commit()
    assert first.post("/group/update", json={"id": "g1"}).status_code == 503


def test_group_response_is_not_shared_cacheable(clients):
    first, _, _ = clients
    assert create(first).headers["cache-control"] == "no-store"
    assert first.get("/group").headers["cache-control"] == "no-store"


def test_expected_ingress_marker_cannot_come_from_request_headers(clients):
    app = create_cloud_app()
    outsider = TestClient(app, base_url="http://device-service.invalid", client=("192.0.2.88", 41000))
    response = outsider.post("/group/create", json={"id": "g1", "leader": "RADIOA", "members": []},
                             headers={"X-Device-Ingress-Peer": "192.0.2.42", "X-Forwarded-For": "192.0.2.42"})
    assert response.status_code == 403
