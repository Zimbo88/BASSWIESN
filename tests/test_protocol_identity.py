"""Actual first-dispatched device handlers, with private test inventory only."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from basswiesn.app import db as app_db
from basswiesn.app.core.device_ingress import DeviceIngress
from basswiesn.app.core.protocol_identity import account_from_info, cached_account_binding, registration_device_id
from basswiesn.app.main import create_cloud_app, create_web_app
from basswiesn.app.models import Device
from basswiesn.app.routers import cloud
from test_admin_boundary import secured  # noqa: F401
from test_device_ingress import PEER, invoke

pytestmark = pytest.mark.integration


def inventory():
    with app_db.SessionLocal() as db:
        db.add(Device(device_id="EXAMPLE", ip_address=PEER, name="Before",
                      last_seen=datetime.now(UTC) - timedelta(hours=1),
                      info_xml='<info><margeAccountUUID>ACCOUNT-A</margeAccountUUID></info>'))
        db.add(Device(device_id="OTHER", ip_address="192.0.2.99", name="Other",
                      last_seen=datetime.now(UTC),
                      info_xml='<info><margeAccountUUID>ACCOUNT-B</margeAccountUUID></info>'))
        db.commit()


@pytest.mark.parametrize("xml,expected", [
    ('<info><margeAccountUUID> A </margeAccountUUID></info>', "A"),
    ('<info/>', ""), ('<info>', ""), ('<info><margeAccountUUID/></info>', ""),
    ('<device><margeAccountUUID>A</margeAccountUUID></device>', ""),
    ('<info><margeAccountUUID>A</margeAccountUUID><margeAccountUUID>B</margeAccountUUID></info>', ""),
    ('<info><margeAccountUUID>' + 'x' * 129 + '</margeAccountUUID></info>', ""),
    ('<!DOCTYPE info [<!ENTITY x "A">]><info><margeAccountUUID>&x;</margeAccountUUID></info>', ""),
])
def test_cached_account_schema_is_unambiguous(xml, expected):
    assert account_from_info(xml) == expected


@pytest.mark.parametrize("body", [
    b'<device>', b'<device/>', b'<device deviceid=" "/>', b'<info deviceid="EXAMPLE"/>',
    b'<device deviceid="' + b'x' * 129 + b'"/>',
    b'<!DOCTYPE device [<!ENTITY x "EXAMPLE">]><device deviceid="&x;"/>',
    '<!DOCTYPE device><device deviceid="EXAMPLE"/>'.encode('utf-16'),
    b'<device deviceid="EXAMPLE" deviceid="OTHER"/>',
])
def test_unreviewed_registration_shape_never_enters_handler(body):
    with pytest.raises(ValueError):
        registration_device_id(body)
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,)), chunks=(body,), registration=True))
    assert sent[0]["status"] == 400 and not called


def test_registration_conflicting_path_id_rejected_before_inventory():
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,)), chunks=(b'<device deviceid="EXAMPLE"/>',),
                                     registration=True, device_id="OTHER"))
    assert sent[0]["status"] == 403 and not called


@pytest.mark.parametrize("account", ["", "x" * 129])
def test_account_parameter_is_bounded(account):
    sent, called = asyncio.run(invoke(DeviceIngress((PEER,)), account_id=account))
    assert sent[0]["status"] == 403 and not called


def test_unavailable_account_inventory_is_not_authority_or_information_leak():
    def broken(*_):
        raise RuntimeError("private inventory canary")
    policy = DeviceIngress((PEER,), account_binding=broken)
    sent, called = asyncio.run(invoke(policy, account_id="ACCOUNT-A"))
    assert sent[0]["status"] == 503 and not called
    assert b"canary" not in sent[1]["body"] and policy.budgets[PEER].active == 0


@pytest.mark.parametrize("factory", [create_web_app, create_cloud_app])
def test_account_binding_on_effective_handlers_and_body_registration(secured, factory):
    inventory()
    client = TestClient(factory(), base_url="http://device-service.invalid", client=(PEER, 41000))
    for suffix in ('full', 'provider_settings', 'sources', 'presets/all', 'device/EXAMPLE/presets'):
        assert client.get(f'/streaming/account/ACCOUNT-A/{suffix}').status_code == 200
        for account in ('ACCOUNT-B', 'UNKNOWN'):
            response = client.get(f'/streaming/account/{account}/{suffix}')
            assert response.status_code == 403, response.text
    for account, identifier in [('ACCOUNT-B', 'EXAMPLE'), ('ACCOUNT-A', 'OTHER'), ('ACCOUNT-A', 'MISSING')]:
        response = client.post(f'/streaming/account/{account}/device/',
                               content=f'<device deviceid="{identifier}"><name>Intruder</name></device>')
        assert response.status_code == 403
    with app_db.SessionLocal() as db:
        device = db.query(Device).filter_by(device_id='EXAMPLE').one()
        assert device.name == 'Before' and account_from_info(device.info_xml) == 'ACCOUNT-A'
    # A previously bound callback remains compatible; it cannot assign itself
    # a new account. Authorization neither discovers nor probes the radio.
    response = client.post('/streaming/account/ACCOUNT-A/device/',
                           content='<device deviceid="EXAMPLE"><name>Confirmed</name></device>')
    assert response.status_code == 201, response.text
    with app_db.SessionLocal() as db:
        assert db.query(Device).filter_by(device_id='EXAMPLE').one().name == 'Confirmed'


def test_account_collection_never_returns_another_accounts_recent_radio(monkeypatch):
    inventory()
    calls = []
    def presets(db, device_id):
        calls.append(device_id)
        return '<presets/>'
    monkeypatch.setattr(cloud, 'marge_presets_xml', presets)
    client = TestClient(create_cloud_app())  # also fixes isolation in legacy mode
    assert client.get('/streaming/account/ACCOUNT-A/presets/all').status_code == 200
    assert calls == ['EXAMPLE']
    assert client.get('/streaming/account/UNKNOWN/presets/all').status_code == 200
    assert calls == ['EXAMPLE']


def test_missing_ambiguous_or_changed_inventory_binding_fails_closed():
    assert not cached_account_binding(PEER, 'ACCOUNT-A', None)
    inventory()
    assert cached_account_binding(PEER, 'ACCOUNT-A', 'example')
    assert not cached_account_binding(PEER, 'ACCOUNT-A', 'OTHER')
    with app_db.SessionLocal() as db:
        duplicate = db.query(Device).filter_by(device_id='OTHER').one()
        duplicate.ip_address = '::ffff:192.0.2.42'
        db.commit()
    assert not cached_account_binding(PEER, 'ACCOUNT-A', None)
    assert not cached_account_binding(PEER, 'ACCOUNT-A', 'EXAMPLE')
