"""Test infrastructure failures must not spill into the next fixture database."""
import socket
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

import test_mobile_release as mobile

pytestmark = pytest.mark.integration


def release_reservation(server):
    reservation = getattr(server, "_socket", None)
    if reservation is not None:
        reservation.close()


def test_browser_server_keeps_its_port_reserved_until_start():
    server = mobile._LiveServer()
    try:
        with socket.socket() as contender:
            with pytest.raises(OSError):
                contender.bind(("127.0.0.1", server.port))
    finally:
        release_reservation(server)


def test_browser_server_never_silently_accepts_a_surviving_thread():
    server = mobile._LiveServer()
    server.thread = Mock()
    server.thread.is_alive.return_value = True
    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            server.__exit__()
        assert server.server.should_exit is True
        assert server.server.force_exit is True
        assert server.thread.join.call_count == 2
    finally:
        release_reservation(server)


def test_browser_server_escalates_only_when_graceful_join_did_not_stop_it():
    server = mobile._LiveServer()
    server.thread = Mock()
    server.thread.is_alive.side_effect = [True, False]
    try:
        server.__exit__()
        assert server.server.should_exit is True
        assert server.server.force_exit is True
        assert server.thread.join.call_count == 2
        assert server._socket.fileno() == -1
    finally:
        release_reservation(server)


def test_browser_server_preserves_startup_exception_and_releases_resources(monkeypatch):
    error = RuntimeError("synthetic startup failure")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(mobile.uvicorn.Server, "run", fail)
    server = mobile._LiveServer()
    try:
        with pytest.raises(RuntimeError, match="server crashed") as caught:
            server.__enter__()
        assert caught.value.__cause__ is error
        assert not server.thread.is_alive()
        assert server._socket.fileno() == -1
    finally:
        release_reservation(server)


def test_browser_server_stops_before_database_fixture_is_reused():
    for _ in range(3):
        server = mobile._LiveServer()
        with server:
            assert server.thread.is_alive()
            assert httpx.get(server.url + "/api/health", trust_env=False).status_code == 200
        assert not server.thread.is_alive()
        assert server._socket.fileno() == -1


@pytest.mark.parametrize("name", ["test_release.sh", "package_release.sh"])
def test_release_runners_stop_on_first_failure_instead_of_losing_cascaded_errors(name):
    text = (Path(__file__).resolve().parents[1] / "tools" / name).read_text()
    assert "--maxfail=1" in text
    assert "--tb=short" in text
    assert "not hardware" in text
