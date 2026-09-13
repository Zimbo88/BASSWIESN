import os
import ipaddress
import socket

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Public tests use synthetic protected targets and must not depend on a
# developer's private .env file. Existing protections are retained so a test
# run can never weaken the operator's hardware safety policy.
os.environ["PROTECTED_DEVICE_IPS"] = ",".join(
    value
    for value in (
        os.environ.get("PROTECTED_DEVICE_IPS", ""),
        os.environ.get("BASSWIESN_PROTECTED_DEVICE_IPS", ""),
        "192.168.50.25",
    )
    if value
)
os.environ["PROTECTED_DEVICE_IDS"] = ",".join(
    value
    for value in (
        os.environ.get("PROTECTED_DEVICE_IDS", ""),
        os.environ.get("BASSWIESN_PROTECTED_DEVICE_IDS", ""),
        "CCDDEEFF0011",
    )
    if value
)

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.core import masterlog
from basswiesn.app.db import database as database_module


@pytest.fixture(autouse=True)
def no_real_network_in_software_tests(monkeypatch, request):
    """Software suites may use loopback servers, never a household radio."""
    if request.node.get_closest_marker("hardware") is not None:
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def require_local(address):
        if not isinstance(address, tuple):  # AF_UNIX socket path
            return
        host = address[0]
        if host == "localhost":
            return
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise AssertionError("Software test attempted non-loopback network transport; use a mock")

    def connect(sock, address):
        require_local(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        require_local(address)
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        require_local(args[-1])
        return original_sendto(sock, data, *args)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch, request):
    """Keep API/UI tests out of the user's production database."""
    if request.node.get_closest_marker("hardware") is not None:
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
