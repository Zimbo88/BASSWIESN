"""Offline wireless read/command boundary and failure-injection checks."""
import asyncio
import json

from fastapi import HTTPException
import pytest
from starlette.responses import JSONResponse
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal, Setting
from basswiesn.app.routers import api
from basswiesn.app.services.device_policy import device_lock
from test_maintenance_reboot_safety import BUILD, ID, info
from test_admin_boundary import secured, authenticate  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def wireless(monkeypatch):
    class Radio:
        def __init__(self):
            self.constructed = 0
            self.calls, self.writes = [], []
            self.fail = False
            self.raw = {"/info": info(), "/networkInfo": '<networkInfo wifiProfileCount="2"/>',
                        "/getActiveWirelessProfile": '<activeWirelessProfile><ssid>Example</ssid><password>DO-NOT-EXPOSE</password></activeWirelessProfile>',
                        "/getZone": '<zone/>'}

        def factory(self, *args, **kwargs):
            self.constructed += 1
            return self

        async def get_xml(self, path):
            self.calls.append(path)
            return self.raw[path]

        async def post_xml(self, path, body):
            self.writes.append((path, body))
            assert device_lock(ID).owned_by_current_task()
            with app_db.SessionLocal() as db:
                assert db.query(ConfigBackup).count() == 1
                assert db.query(DeviceActionJournal).filter_by(phase="WIRELESS_SURVEY_INTENT").count() == 1
            if self.fail:
                raise TimeoutError("password=DO-NOT-EXPOSE")
            return '<survey password="DO-NOT-EXPOSE"><status>OK</status></survey>'

    radio = Radio()
    monkeypatch.setattr(api, "SoundTouchClient", radio.factory)
    with app_db.SessionLocal() as db:
        radio.db = db
        radio.device = Device(device_id=ID, ip_address="192.0.2.42", model="SoundTouch 30", firmware=BUILD, info_xml=info())
        db.add(radio.device)
        db.add(Setting(key="ui_mode", value="lab"))
        db.commit()
        yield radio


def run(wireless, *, method="read", **payload):
    try:
        call = (api.wireless_profiles(ID, db=wireless.db, **payload) if method == "read"
                else api.wireless_survey(ID, {"dry_run": False, "confirmation": "YES", **payload}, wireless.db))
        result = asyncio.run(call)
        return json.loads(result.body) if isinstance(result, JSONResponse) else result
    except HTTPException as exc:
        return {"rejected": True, "detail": exc.detail, "status_code": exc.status_code}


def test_wireless_get_is_read_only_and_does_not_expose_credentials(wireless):
    wireless.raw["/networkInfo"] = '<networkInfo wifiProfileCount="2" psk="DO-NOT-EXPOSE"><interface type="WIFI_INTERFACE" state="NETWORK_WIFI_CONNECTED" ssid="Example" signal="GOOD_SIGNAL" key="DO-NOT-EXPOSE"/><unknown>DO-NOT-EXPOSE</unknown></networkInfo>'
    result = run(wireless)
    assert result["active_ssid"] == "Example" and result["wifi_profile_count"] == 2
    assert result["supports_multiple_profiles"] is None
    assert "DO-NOT-EXPOSE" not in json.dumps(result)
    assert wireless.calls == ["/info", "/networkInfo", "/getActiveWirelessProfile"]
    assert not wireless.writes and not wireless.db.query(ConfigBackup).count()
    assert 'signal="GOOD_SIGNAL"' in result["network_xml"]
    assert result["unknown_fields_omitted"] is True


def test_legacy_survey_get_rejected_without_contact(wireless):
    assert run(wireless, survey=True)["detail"]["code"] == "EXPLICIT_SURVEY_REQUIRED"
    assert not wireless.constructed


@pytest.mark.parametrize("method", ["read", "survey"])
def test_wireless_protection_precedes_transport(wireless, method):
    wireless.device.ip_address = "192.168.50.25"  # Isolated conftest policy.
    wireless.db.commit()
    assert run(wireless, method=method)["rejected"] is True
    assert not wireless.constructed


@pytest.mark.parametrize("method", ["read", "survey"])
def test_wireless_wrong_live_identity_stops_before_further_queries(wireless, method):
    wireless.raw["/info"] = info("BB22CC33DD44")
    assert run(wireless, method=method)["detail"]["code"] == "IDENTITY_MISMATCH"
    assert wireless.calls == ["/info"] and not wireless.writes


def test_unknown_profile_allows_safe_read_but_not_scan(wireless):
    wireless.device.firmware = "27.99.0"
    wireless.device.info_xml = wireless.raw["/info"] = info(firmware="27.99.0")
    wireless.db.commit()
    assert run(wireless)["status"] == "OBSERVED"
    assert run(wireless, method="survey")["rejected"] is True
    assert not wireless.writes


@pytest.mark.parametrize("payload", [{"dry_run": True}, {"confirmation": "NO"}, {"dry_run": "false"}])
def test_wireless_preview_and_confirmation_construct_no_transport(wireless, payload):
    result = run(wireless, method="survey", **payload)
    assert result.get("dry_run") is True or result.get("rejected") is True
    assert not wireless.constructed


@pytest.mark.parametrize("fail", [False, True])
def test_survey_is_once_owned_backed_up_and_never_claims_completion(wireless, fail):
    wireless.fail = fail
    result = run(wireless, method="survey")
    assert len(wireless.writes) == 1
    assert result["status"] == "PENDING_READBACK" and result["verified"] is False
    assert result["completion_verified"] is False and result["response_received"] is not fail
    assert result["network_state_preserved"] is True
    assert "DO-NOT-EXPOSE" not in json.dumps(result)
    assert "DO-NOT-EXPOSE" not in wireless.db.query(ConfigBackup).one().content
    assert not wireless.db.query(DeviceActionJournal).filter_by(verified=True).count()


@pytest.mark.parametrize("field,xml", [
    ("/networkInfo", '<networkInfo wifiProfileCount="-1"/>'),
    ("/networkInfo", '<networkInfo deviceID="OTHER"/>'),
    ("/getActiveWirelessProfile", '<active><ssid>A</ssid><ssid>B</ssid></active>'),
    ("/getActiveWirelessProfile", '<active deviceID="OTHER"><ssid>A</ssid></active>'),
    ("/getActiveWirelessProfile", '<!DOCTYPE active [<!ENTITY x "secret">]><active><ssid>&x;</ssid></active>'),
    ("/getActiveWirelessProfile", 'x' * 131073),
], ids=["bad-count", "network-identity", "ambiguous-ssid", "profile-identity", "dtd", "oversize"])
def test_ambiguous_or_unsafe_profile_responses_block_survey(wireless, field, xml):
    wireless.raw[field] = xml
    assert run(wireless, method="survey")["rejected"] is True
    assert not wireless.writes


def test_survey_refuses_unowned_topology(wireless):
    wireless.raw["/getZone"] = '<zone master="OTHER"><member>OTHER</member></zone>'
    assert run(wireless, method="survey")["rejected"] is True
    assert not wireless.writes


def test_wireless_survey_is_lab_only_and_preview_does_not_enable_it(wireless):
    wireless.db.query(Setting).filter_by(key="ui_mode").one().value = "standard"
    wireless.db.commit()
    assert run(wireless, method="survey", dry_run=True)["dry_run"] is True
    assert run(wireless, method="survey")["status_code"] == 403
    assert not wireless.constructed


def test_missing_count_remains_unknown(wireless):
    wireless.raw["/networkInfo"] = '<networkInfo/>'
    assert run(wireless)["wifi_profile_count"] is None


@pytest.mark.parametrize("kind", ["commit", "backup", "ssid", "mapping"])
def test_wireless_failed_durability_or_changed_precondition_prevents_dispatch(wireless, monkeypatch, kind):
    original = wireless.db.commit
    calls = 0
    def changed():
        nonlocal calls
        calls += 1
        if kind == "commit":
            raise RuntimeError("database unavailable")
        original()
        if calls == 2:
            if kind == "backup":
                wireless.db.query(ConfigBackup).one().content += " changed"
                original()
            elif kind == "ssid":
                wireless.raw["/getActiveWirelessProfile"] = '<active><ssid>Different</ssid></active>'
            elif kind == "mapping":
                wireless.device.ip_address = "192.0.2.43"
                original()
    monkeypatch.setattr(wireless.db, "commit", changed)
    assert run(wireless, method="survey")["rejected"] is True
    assert not wireless.writes


@pytest.mark.parametrize("failure", ["network-loss", "identity", "network-change", "commit"])
def test_post_survey_failures_cannot_claim_success_or_retry(wireless, monkeypatch, failure):
    original = wireless.get_xml
    async def changed(path):
        if wireless.writes:
            if failure == "network-loss":
                raise TimeoutError("password=DO-NOT-EXPOSE")
            if failure == "identity":
                wireless.raw["/info"] = info("BB22CC33DD44")
            if failure == "network-change":
                wireless.raw["/getActiveWirelessProfile"] = '<active><ssid>Different</ssid></active>'
        return await original(path)
    monkeypatch.setattr(wireless, "get_xml", changed)
    original_commit = wireless.db.commit
    def failed_commit():
        if wireless.writes and failure == "commit":
            raise RuntimeError("database unavailable")
        original_commit()
    monkeypatch.setattr(wireless.db, "commit", failed_commit)
    result = run(wireless, method="survey")
    detail = result.get("detail", result)
    assert detail["verified"] is False and detail["reconciliation_required"] is True
    if failure == "network-change":
        assert detail["network_state_preserved"] is False
    assert len(wireless.writes) == 1 and "DO-NOT-EXPOSE" not in json.dumps(result)
    assert wireless.db.query(ConfigBackup).count() == 1


def test_real_session_csrf_and_get_boundary_with_mock_radio(wireless, secured):
    from basswiesn.app.main import create_web_app
    path = f"/api/devices/{ID}/wireless-survey"
    payload = {"dry_run": False, "confirmation": "YES"}
    with TestClient(create_web_app(background_tasks=False), base_url="https://testserver") as client:
        assert client.post(path, json=payload, headers={"Origin": "https://testserver"}).status_code == 401
        csrf = authenticate(client)
        assert client.post(path, json=payload, headers={"Origin": "https://testserver"}).status_code == 403
        assert client.get(f"/api/devices/{ID}/wireless-profiles?survey=true").status_code == 409
        assert wireless.constructed == 0
        response = client.post(path, json=payload, headers={"Origin": "https://testserver", "X-BASSWIESN-CSRF": csrf})
    assert response.status_code == 202 and response.json()["verified"] is False
    assert len(wireless.writes) == 1
