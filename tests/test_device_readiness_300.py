import asyncio
from datetime import UTC, datetime, timedelta
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, RuntimeState
from basswiesn.app.services.device_readiness import READINESS_COMMAND, cached_readiness, probe_readiness

pytestmark = pytest.mark.integration


@pytest.fixture
def fixture():
    calls = []
    options = {"identity": "READINESS-A", "rc": 0,
               "stdout": "PERSISTENT=1\nSERVICES=1\nREDIRECT=0\nPROBE_COMPLETE\n"}
    with app_db.SessionLocal() as db:
        radio = Device(device_id="READINESS-A", ip_address="192.0.2.42", name="Fixture")
        db.add(radio)
        db.commit()
        class Client:
            def __init__(self, ip, **kwargs):
                calls.append(("client", ip))
            async def get_xml(self, path):
                assert path == "/info"
                calls.append(("GET", path))
                return f'<info deviceID="{options["identity"]}" />'
        async def ssh(ip, user, command, timeout):
            assert calls[-1] == ("GET", "/info")
            assert user == "root" and command == READINESS_COMMAND and timeout == 10
            calls.append(("ssh", ip))
            return {"returncode": options["rc"], "stdout": options["stdout"], "stderr": "private failure detail"}
        yield db, radio, calls, options, Client, ssh


def run(fixture):
    db, radio, calls, options, client, ssh = fixture
    return asyncio.run(probe_readiness(radio, db, client_factory=client, ssh_runner=ssh))


def test_explicit_probe_reads_only_fixed_markers_and_caches_boolean_observations(fixture):
    value = run(fixture)
    db, radio, calls, *_ = fixture
    assert calls == [("client", "192.0.2.42"), ("GET", "/info"), ("ssh", "192.0.2.42")]
    assert value["ssh"] == "available"
    assert value["persistent_ssh"] is True and value["remote_services"] is True
    assert value["host_redirect"] is False
    assert value["persistent_ssh_boot_verified"] is False
    assert value["redirect_destination_verified"] is False
    stored = db.query(RuntimeState).one().value
    assert "private failure" not in stored and "stdout" not in stored
    assert cached_readiness(db, radio.device_id)["persistent_ssh"] is True
    later = datetime.fromisoformat(value["observed_at"]) + timedelta(minutes=16)
    stale = cached_readiness(db, radio.device_id, now=later)
    assert stale["stale"] and stale["ssh"] == "unknown" and stale["persistent_ssh"] is None


def test_identity_mismatch_stops_before_ssh(fixture):
    run(fixture)
    fixture[2].clear()
    fixture[3]["identity"] = "OTHER"
    with pytest.raises(HTTPException) as exc:
        run(fixture)
    assert exc.value.status_code == 409
    assert not any(call[0] == "ssh" for call in fixture[2])
    assert cached_readiness(fixture[0], "READINESS-A")["ssh"] == "unknown"


def test_protected_identity_stops_before_any_transport(fixture, monkeypatch):
    from basswiesn.app.config import get_settings
    monkeypatch.setenv("PROTECTED_DEVICE_IDS", "READINESS-A")
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as exc:
            run(fixture)
        assert exc.value.status_code == 403 and fixture[2] == []
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("stdout,rc", [("", 0), ("PERSISTENT=1\nPROBE_COMPLETE", 255)])
def test_unusable_ssh_is_not_proof_of_missing_markers(fixture, stdout, rc):
    fixture[3].update(stdout=stdout, rc=rc)
    value = run(fixture)
    assert value["ssh"] == "not_verified"
    assert all(value[key] is None for key in ("persistent_ssh", "remote_services", "host_redirect"))


def test_unexpected_values_and_missing_hosts_result_remain_unknown(fixture):
    fixture[3]["stdout"] = "PERSISTENT=1\nPERSISTENT=0\nSERVICES=secret-value\nPROBE_COMPLETE"
    value = run(fixture)
    assert value["ssh"] == "available"
    assert value["persistent_ssh"] is None and value["remote_services"] is None and value["host_redirect"] is None
    assert "secret-value" not in fixture[0].query(RuntimeState).one().value


def test_passive_badges_never_probe_and_explicit_confirmation_is_required(fixture, monkeypatch):
    from basswiesn.app.routers import api
    async def forbidden(*args, **kwargs):
        pytest.fail("Passive status must not probe")
    monkeypatch.setattr(api, "probe_readiness", forbidden)
    with TestClient(create_web_app(background_tasks=False)) as client:
        value = client.get("/api/devices/status-badges").json()[0]
        assert value["provenance"] == "NOT_PROBED"
        for payload in ({}, {"confirm_read_only": "true"}, {"confirm_read_only": 1}):
            assert client.post("/api/devices/READINESS-A/diagnostics/readiness", json=payload).status_code == 400
    assert fixture[2] == []


def test_probe_command_has_no_activation_or_secret_read():
    for forbidden in ("touch ", "sshd start", "reboot", "cat ", "private_key", "authorized_keys", "chmod", "rm "):
        assert forbidden not in READINESS_COMMAND


@pytest.mark.parametrize("content", ["[]", "null", "{}", "not json"])
def test_bad_cached_data_is_unknown(content):
    with app_db.SessionLocal() as db:
        db.add(RuntimeState(key="device:READINESS-A:readiness", value=content))
        db.commit()
        assert cached_readiness(db, "READINESS-A")["provenance"] == "NOT_PROBED"
