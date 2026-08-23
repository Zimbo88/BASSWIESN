import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Public tests use synthetic protected targets and must not depend on a
# developer's private .env file.
os.environ.setdefault("PROTECTED_DEVICE_IPS", "192.168.50.25")
os.environ.setdefault("PROTECTED_DEVICE_IDS", "CCDDEEFF0011")

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.core import masterlog
from basswiesn.app.db import database as database_module


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch, request):
    """Keep API/UI tests out of the user's production database."""
    if request.node.get_closest_marker("unit") is not None:
        yield
        return
    from basswiesn.app import models  # noqa: F401

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setattr(app_db, "SessionLocal", session_factory)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", session_factory)
    from basswiesn.app.services import alarm_engine
    from basswiesn.app.routers import setup as setup_router
    from basswiesn.app import main as main_module

    monkeypatch.setattr(alarm_engine, "SessionLocal", session_factory)
    monkeypatch.setattr(setup_router, "SessionLocal", session_factory)
    monkeypatch.setattr(main_module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        masterlog,
        "get_settings",
        lambda: get_settings().model_copy(
            update={"data_dir": tmp_path, "masterlog_enabled": True}
        ),
    )
    from basswiesn.app.db.migrations import ensure_schema_baseline

    ensure_schema_baseline(engine)
    yield
    engine.dispose()
