import json
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient

from basswiesn.app.config import get_settings
from basswiesn.app.core import masterlog, setup_mode
from basswiesn.app.main import create_web_app


def _settings(tmp_path, *, test=False, disable=False, enabled=True):
    return SimpleNamespace(
        data_dir=tmp_path,
        test_mode=test,
        disable_setup_confirmations=disable,
        masterlog_enabled=enabled,
    )


def test_test_mode_flags_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("BASSWIESN_TEST_MODE", "1")
    monkeypatch.setenv("BASSWIESN_DISABLE_SETUP_CONFIRMATIONS", "1")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.test_mode is True
        assert settings.disable_setup_confirmations is True
    finally:
        get_settings.cache_clear()


def test_setup_confirmation_remains_required_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_mode, "get_settings", lambda: _settings(tmp_path))
    assert not setup_mode.setup_confirmation_allowed(
        "", "yes", endpoint="/setup", action="apply"
    )


def test_setup_confirmation_remains_required_without_disable_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        setup_mode, "get_settings", lambda: _settings(tmp_path, test=True)
    )
    assert not setup_mode.setup_confirmation_allowed(
        "", "yes", endpoint="/setup", action="apply"
    )


def test_setup_confirmation_is_skipped_and_logged_only_with_both_flags(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(
        setup_mode,
        "get_settings",
        lambda: _settings(tmp_path, test=True, disable=True),
    )
    monkeypatch.setattr(setup_mode, "write_masterlog", lambda event, **fields: events.append((event, fields)))

    assert setup_mode.setup_confirmation_allowed(
        "", "yes", endpoint="/setup/apply", action="apply cloud route"
    )
    assert events == [
        (
            "setup_confirmation_skipped",
            {
                "endpoint": "/setup/apply",
                "skipped_confirmation": "yes",
                "action": "apply cloud route",
            },
        )
    ]


def test_masterlog_creates_parseable_jsonl_and_masks_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(masterlog, "get_settings", lambda: _settings(tmp_path))
    masterlog.write_masterlog(
        "test_event",
        device_id="RADIO-1",
        password="do-not-log",
        nested={"access_token": "also-secret"},
    )

    lines = (Path(tmp_path) / "logs" / "master.log").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    record = next(item for item in reversed(records) if item.get("event") == "test_event")
    assert record["event"] == "test_event"
    assert record["device_id"] == "RADIO-1"
    assert record["password"] == "***REDACTED***"
    assert record["nested"]["access_token"] == "***REDACTED***"


def test_masterlog_write_failure_does_not_crash(tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked", encoding="utf-8")
    monkeypatch.setattr(masterlog, "get_settings", lambda: _settings(blocked))
    masterlog.write_masterlog("must_not_raise")


def test_app_start_is_written_to_masterlog(tmp_path, monkeypatch):
    monkeypatch.setattr(masterlog, "get_settings", lambda: _settings(tmp_path))
    with TestClient(create_web_app()) as client:
        assert client.get("/api/health").status_code == 200

    records = [
        json.loads(line)
        for line in (Path(tmp_path) / "logs" / "master.log").read_text(encoding="utf-8").splitlines()
    ]
    assert any(record["event"] == "app_start" for record in records)
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
