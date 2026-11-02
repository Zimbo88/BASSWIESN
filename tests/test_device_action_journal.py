from basswiesn.app import db as app_db
from basswiesn.app.models import DeviceActionJournal
from basswiesn.app.services.action_journal import record_action


def test_action_journal_redacts_secrets():
    db = app_db.SessionLocal()
    record_action(db, job_id="job", device_id="radio", ip_address="192.0.2.1", action="reboot", trigger="manual", phase="test", before_state={"token": "secret-value", "stream_url": "http://example.test/live"})
    db.commit()
    row = db.query(DeviceActionJournal).one()
    assert "secret-value" not in row.before_state
    assert "REDACTED" in row.before_state
    db.close()


def test_action_journal_redacts_sensitive_xml_inside_generic_field():
    db = app_db.SessionLocal()
    record_action(
        db,
        job_id="xml-job",
        device_id="xml-radio",
        ip_address="192.0.2.2",
        action="pair",
        trigger="manual",
        phase="write",
        requested_state={"xml": "<Pair><userAuthToken>do-not-store</userAuthToken></Pair>"},
    )
    db.commit()
    row = db.query(DeviceActionJournal).filter(DeviceActionJournal.job_id == "xml-job").one()
    assert "do-not-store" not in row.requested_state
    assert "REDACTED" in row.requested_state
    db.close()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
