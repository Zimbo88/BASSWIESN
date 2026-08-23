from pathlib import Path

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.models import Setting
from basswiesn.app.services import backup_restore


def test_system_backup_manifest_preview_and_restore_prepare(tmp_path, monkeypatch):
    db = app_db.SessionLocal()
    db.add(Setting(key="web_language", value="de"))
    db.commit()
    database_path = Path(app_db.engine.url.database).resolve()
    settings = get_settings().model_copy(
        update={
            "data_dir": tmp_path,
            "database_url": f"sqlite:///{database_path}",
            "version": "1.5.0",
        }
    )
    monkeypatch.setattr(backup_restore, "get_settings", lambda: settings)

    created = backup_restore.create_system_backup(db)
    preview = backup_restore.preview_system_backup(created["path"])
    prepared = backup_restore.prepare_system_restore(db, created["filename"])
    db.close()

    assert Path(created["path"]).exists()
    assert created["manifest"]["version"] == "1.5.0"
    assert preview["ok"] is True
    assert prepared["prepared"] is True
    assert Path(prepared["pending_archive"]).exists()
    assert Path(prepared["safety_backup"]["path"]).exists()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
