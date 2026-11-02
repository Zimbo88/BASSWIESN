"""Frequent key evidence must not evict persistent or unresolved recovery data."""
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.models import ConfigBackup, DeviceActionJournal
from basswiesn.app.services.maintenance import cleanup_plan, run_cleanup

pytestmark = pytest.mark.unit


@pytest.fixture
def retention(monkeypatch):
    monkeypatch.setenv("BASSWIESN_CONFIG_BACKUP_RETENTION_COUNT", "2")
    get_settings.cache_clear()
    with app_db.SessionLocal() as db:
        yield db


def backup(db, age, *, key=False, complete=False):
    row = ConfigBackup(device_id="AA11BB22CC33", path="http8090:key-start" if key else "http8090:/name",
                       content=f'{{"fixture":{age}}}', created_at=datetime(2026, 1, 1, tzinfo=UTC) - timedelta(days=age))
    db.add(row)
    db.flush()
    if complete:
        journal(db, row, phase="KEY_COMPLETED", verified=True, result="VERIFIED")
    db.commit()
    return row


def journal(db, row, **changes):
    fields = dict(job_id=f"key-{row.id}", device_id=row.device_id, action="key_command", phase="KEY_WRITE_INTENT",
                  backup_ref=f"config-backup:{row.id}:sha256:{sha256(row.content.encode()).hexdigest()}",
                  verified=False, result="PENDING_READBACK")
    fields.update(changes)
    entry = DeviceActionJournal(**fields)
    db.add(entry)
    db.flush()
    return entry


def test_completed_keys_do_not_consume_configuration_backup_quota(retention):
    db = retention
    ordinary = [backup(db, age) for age in (20, 21, 22)]
    keys = [backup(db, age, key=True, complete=True) for age in (1, 2, 3)]
    plan = cleanup_plan(db)
    assert set(plan["config_backup_ids"]) == {ordinary[-1].id, keys[-1].id}
    assert db.query(ConfigBackup).count() == 6  # Preview is read-only.
    result = run_cleanup(db)
    assert result["config_backups"] == 2
    assert {row.id for row in db.query(ConfigBackup).all()} == {row.id for row in ordinary[:2] + keys[:2]}


@pytest.mark.parametrize("state", ["missing", "intent", "diverged", "late_failure", "wrong_hash", "wrong_device", "wrong_phase"])
def test_unresolved_or_inconsistent_key_evidence_is_never_pruned(retention, state):
    db = retention
    protected = backup(db, 30, key=True, complete=state in {"late_failure", "wrong_hash"})
    if state in {"intent", "diverged", "late_failure"}:
        journal(db, protected, phase="KEY_WRITE_INTENT" if state == "intent" else "DIVERGED")
    elif state == "wrong_hash":
        protected.content += " changed"
    elif state == "wrong_device":
        journal(db, protected, device_id="BB22CC33DD44", phase="KEY_COMPLETED", verified=True, result="VERIFIED")
    elif state == "wrong_phase":
        journal(db, protected, phase="KEY_WRITE_INTENT", verified=True, result="VERIFIED")
    db.commit()
    completed = [backup(db, age, key=True, complete=True) for age in (1, 2, 3)]
    plan = cleanup_plan(db)
    assert plan["config_backup_ids"] == [completed[-1].id]
    assert plan["key_backups_retained_for_reconciliation"] == 1
    result = run_cleanup(db)
    assert result["config_backups"] == 1
    assert db.get(ConfigBackup, protected.id, populate_existing=True) is not None


def test_retention_limits_are_independent_for_each_device(retention):
    db = retention
    first = [backup(db, age, key=True, complete=True) for age in (1, 2)]
    other = backup(db, 3, key=True)
    other.device_id = "BB22CC33DD44"
    journal(db, other, phase="KEY_COMPLETED", verified=True, result="VERIFIED")
    db.commit()
    assert cleanup_plan(db)["config_backup_ids"] == []
    run_cleanup(db)
    assert db.query(ConfigBackup).count() == len(first) + 1


def test_reconnect_start_is_not_pruned_as_ordinary_configuration(retention):
    db = retention
    old = backup(db, 40)
    old.path = "live_radio_reconnect/example-generation"
    db.commit()
    for age in (1, 2, 3):
        backup(db, age)
    assert old.id not in cleanup_plan(db)["config_backup_ids"]
    run_cleanup(db)
    assert db.get(ConfigBackup, old.id, populate_existing=True) is not None


@pytest.mark.parametrize("kind,phase", [
    ("power", "POWER_PENDING_READBACK"), ("power", "DIVERGED"),
    ("wireless-survey", "WIRELESS_SURVEY_PENDING"), ("wireless-survey", "WIRELESS_COMPLETED"),
    ("native_station", "MANUAL_COMMAND_PENDING"), ("native_station", "MANUAL_COMMAND_COMPLETED"),
    ("wireless_profile", "MANUAL_COMMAND_PENDING"), ("wireless_profile", "MANUAL_COMMAND_COMPLETED"),
    ("station", "STATION_BUFFERING"), ("station", "DIVERGED"),
])
def test_other_unresolved_radio_actions_retain_their_backups(retention, kind, phase):
    db = retention
    old = backup(db, 30)
    old.path = f"http8090:{kind}-start"
    journal(db, old, action={"power": "power_action", "wireless-survey": "wireless_survey", "station": "station_playback"}.get(kind, kind),
            phase=phase, verified=True, result="VERIFIED")  # Invalid success flags must not free it.
    db.commit()
    for age in (1, 2, 3):
        backup(db, age)
    plan = cleanup_plan(db)
    assert old.id not in plan["config_backup_ids"]
    assert plan["transaction_backups_retained_for_reconciliation"] == 1
    run_cleanup(db)
    assert db.get(ConfigBackup, old.id, populate_existing=True) is not None


def test_completed_power_actions_have_their_own_quota(retention):
    db = retention
    power = []
    for age in (1, 2, 3):
        old = backup(db, age)
        old.path = "http8090:power-start"
        journal(db, old, action="power_action", phase="POWER_COMPLETED", verified=True, result="VERIFIED")
        db.commit()
        power.append(old)
    configuration = backup(db, 30)
    assert cleanup_plan(db)["config_backup_ids"] == [power[-1].id]
    run_cleanup(db)
    assert db.get(ConfigBackup, configuration.id, populate_existing=True) is not None


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
def test_cleanup_does_not_rollback_or_delete_pending_user_changes(retention, pending):
    db = retention
    row = backup(db, 1)
    if pending == "new":
        db.add(ConfigBackup(device_id=row.device_id, path="pending", content="not committed"))
    elif pending == "dirty":
        row.content = "pending user change"
    else:
        db.delete(row)
    before = (set(db.new), set(db.dirty), set(db.deleted))
    with pytest.raises(ValueError, match="clean database session"):
        run_cleanup(db)
    assert (set(db.new), set(db.dirty), set(db.deleted)) == before
    db.rollback()
    assert db.query(ConfigBackup).count() == 1
    assert db.get(ConfigBackup, row.id).content != "pending user change"


def test_cleanup_unknown_dialect_cannot_prune_evidence(retention, monkeypatch):
    db = retention
    for age in (1, 2, 3):
        backup(db, age)
    with monkeypatch.context() as patch:
        patch.setattr(db, "get_bind", lambda: SimpleNamespace(dialect=SimpleNamespace(name="unreviewed")))
        with pytest.raises(ValueError, match="isolation not established"):
            run_cleanup(db)
    assert db.query(ConfigBackup).count() == 3


def test_missing_storage_files_do_not_hide_database_counts(retention, monkeypatch, tmp_path):
    from basswiesn.app.services import maintenance

    db = retention
    backup(db, 1)
    settings = get_settings().model_copy(update={
        "data_dir": tmp_path / "does-not-exist", "database_url": "sqlite:///basswiesn.db",
    })
    monkeypatch.setattr(maintenance, "get_settings", lambda: settings)
    summary = maintenance.storage_summary(db)
    assert summary["db_size_mb"] == summary["masterlog_size_mb"] == 0
    assert summary["config_backup_count"] == 1
    assert db.query(ConfigBackup).count() == 1
