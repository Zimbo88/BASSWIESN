"""No LAN access: process failure and all-service readiness regressions."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tools import run_dev

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failed_port", [None, 1328, 1516, 1860])
def test_healthcheck_cannot_hide_dead_cloud(monkeypatch, failed_port):
    calls = []
    def open_url(url, timeout):
        calls.append(url)
        assert url.startswith("http://127.0.0.1:")
        assert timeout == 2
        if failed_port and f":{failed_port}/" in url:
            raise ConnectionRefusedError()
        payload = {"ready": True, "status": "ok", "bmx_services": []}
        stream = io.BytesIO(json.dumps(payload).encode())
        stream.status = 200
        return stream
    monkeypatch.setattr(run_dev.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_url))
    assert run_dev.healthcheck() is (failed_port is None)
    if failed_port is None:
        assert len(calls) == 3


def test_spawned_worker_uses_prepared_schema(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(run_dev.importlib, "import_module", lambda name: SimpleNamespace(app=app))
    run = Mock()
    monkeypatch.setattr(run_dev.uvicorn, "run", run)
    run_dev.run("example:app", 1516)
    assert app.state.database_schema_prepared is True
    assert run.call_args.args[0] is app


@pytest.mark.parametrize("exitcode", [0, 1, -11])
def test_child_exit_stops_siblings_and_fails_parent(exitcode):
    failed = Mock(pid=100, exitcode=exitcode)
    failed.is_alive.return_value = False
    alive = Mock(pid=101, exitcode=None)
    alive.is_alive.side_effect = [True, False]
    with pytest.raises(SystemExit) as error:
        run_dev.supervise([alive, failed])
    assert error.value.code != 0
    alive.terminate.assert_called_once()
    failed.terminate.assert_not_called()
    alive.start.assert_called_once()
    failed.start.assert_called_once()


def test_malformed_health_response_fails_closed(monkeypatch):
    stream = io.BytesIO(b'[]')
    stream.status = 200
    monkeypatch.setattr(run_dev.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **k: stream))
    assert run_dev.healthcheck() is False
