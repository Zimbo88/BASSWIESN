"""Live-worker attestation with synthetic stores and loopback/mocked transports."""
import json
import os
import runpy
import secrets
import sys
import urllib.request

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from basswiesn.app.config import get_settings
from basswiesn.app.core import runtime_probe as probe
from basswiesn.app.core.admin_store import AdminStore
from basswiesn.app.main import create_web_app, create_cloud_app, create_debug_app
from basswiesn.app.services.filesystem_contract import ensure_runtime_directories
from basswiesn.update_helper.candidate_runtime import probe_source

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "admin_auth_mode", "required")
    monkeypatch.setattr(settings, "admin_origins", ("https://testserver",))
    ensure_runtime_directories(settings.data_dir)
    store = AdminStore(settings.data_dir / "secrets/admin/auth.sqlite3")
    store.bootstrap("operator", secrets.token_urlsafe(24))
    monkeypatch.setattr(probe, "TOKEN_FILE", tmp_path / "runtime" / "probe-token")
    probe.initialize()
    monkeypatch.setattr(probe, "owned_task_status", lambda: [])
    return settings, store


def test_capability_is_private_rotates_and_never_recreates_on_read(runtime):
    previous = probe._token()
    assert probe.TOKEN_FILE.stat().st_mode & 0o777 == 0o600
    assert probe.TOKEN_FILE.parent.stat().st_mode & 0o777 == 0o700
    probe.initialize()
    assert probe._token() != previous
    probe.TOKEN_FILE.unlink()
    with pytest.raises(OSError):
        probe._token()
    assert not probe.TOKEN_FILE.exists()


@pytest.mark.parametrize("tamper", ["mode", "parent-mode", "link", "hardlink", "empty", "format", "directory"])
def test_unsafe_capability_file_fails_closed(runtime, tmp_path, tamper):
    if tamper == "mode":
        probe.TOKEN_FILE.chmod(0o644)
    elif tamper == "parent-mode":
        probe.TOKEN_FILE.parent.chmod(0o755)
    elif tamper == "link":
        copy = tmp_path / "retained"
        probe.TOKEN_FILE.rename(copy)
        probe.TOKEN_FILE.symlink_to(copy)
    elif tamper == "hardlink":
        os.link(probe.TOKEN_FILE, tmp_path / "linked")
    elif tamper == "directory":
        probe.TOKEN_FILE.unlink()
        probe.TOKEN_FILE.mkdir()
    else:
        probe.TOKEN_FILE.write_text("" if tamper == "empty" else "z" * 64)
    with pytest.raises((OSError, probe.ProbeRejected)):
        probe._token()


@pytest.mark.parametrize("factory,listener", [(create_web_app, "web"), (create_cloud_app, "cloud"), (create_debug_app, "debug")])
def test_exact_capability_gives_read_only_worker_attestation_not_admin(runtime, factory, listener):
    client = TestClient(factory(), base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
    assert client.get("/ready").json() == {"ready": True}
    response = client.get("/ready", headers={probe.HEADER: probe._token()})
    body = response.json()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert body == {"ready": True, **probe.attestation(listener)}
    assert body["auth_mode"] == "required" and body["auth_store_ready"] is True
    assert not client.cookies
    assert client.get("/api/devices", headers={probe.HEADER: probe._token()}).status_code in {401, 403}
    assert client.post("/ready", headers={probe.HEADER: probe._token()}).status_code == 403


@pytest.mark.parametrize("peer,headers", [
    ("192.0.2.42", {}), ("testclient", {}), ("127.0.0.1", {"Origin": "http://127.0.0.1"}),
    ("127.0.0.1", {"Sec-Fetch-Site": "same-origin"}),
])
def test_browsers_and_external_peers_cannot_use_capability(runtime, peer, headers):
    client = TestClient(create_web_app(), client=(peer, 12345))
    response = client.get("/ready", headers={probe.HEADER: probe._token(), **headers})
    assert response.status_code == 403 and response.json() == {"ready": False}


def test_bad_token_and_forwarded_loopback_never_get_details(runtime):
    client = TestClient(create_web_app(), client=("127.0.0.1", 12345))
    assert client.get("/ready", headers={probe.HEADER: "bad"}).status_code == 403
    external = TestClient(create_web_app(), client=("192.0.2.42", 12345))
    assert external.get("/ready", headers={probe.HEADER: probe._token(), "X-Forwarded-For": "127.0.0.1"}).status_code == 403


@pytest.fixture
def listeners(runtime, monkeypatch):
    settings, _ = runtime
    clients = {port: TestClient(factory(), client=("127.0.0.1", 12345)) for port, factory in (
        (settings.web_port, create_web_app), (settings.cloud_port, create_cloud_app), (settings.debug_port, create_debug_app))}
    def read(port, token):
        response = clients[port].get("/ready", headers={probe.HEADER: token})
        assert response.status_code == 200
        return response.json()
    monkeypatch.setattr(probe, "_read_listener", read)
    return read


@pytest.mark.parametrize("validation", [True, False])
def test_probe_verifies_each_real_factory_and_local_sqlite(runtime, listeners, validation, monkeypatch):
    settings, _ = runtime
    monkeypatch.setattr(settings, "update_validation_mode", validation)
    result = probe.verify_runtime(validation=validation)
    assert result["ready"] is True and result["version"] == settings.version
    if validation:
        assert result["table_count"] > 1 and len(result["schema_sha256"]) == 64
        assert result["sqlite_integrity"] is True
    else:
        assert set(result) == {"ready", "version"}


@pytest.mark.parametrize("key,value", [("ready", False), ("version", "0.0.0"), ("listener", "other"),
    ("validation_mode", False), ("auth_mode", "legacy"), ("background_tasks", 1),
    ("background_tasks", True), ("contract", True), ("storage_ok", False), ("auth_store_ready", False)])
def test_one_bad_worker_prevents_success(runtime, listeners, monkeypatch, key, value):
    settings, _ = runtime
    monkeypatch.setattr(settings, "update_validation_mode", True)
    def read(port, token):
        result = listeners(port, token)
        if port == settings.cloud_port:
            result[key] = value
        return result
    monkeypatch.setattr(probe, "_read_listener", read)
    with pytest.raises(probe.ProbeRejected):
        probe.verify_runtime(validation=True)


def test_missing_store_storage_and_schema_are_not_ready(runtime, listeners):
    settings, store = runtime
    (settings.data_dir / "tmp").rmdir()
    with pytest.raises(probe.ProbeRejected):
        probe.verify_runtime(validation=False)
    (settings.data_dir / "tmp").mkdir()
    store.path.rename(store.path.with_suffix(".retained"))
    with pytest.raises(probe.ProbeRejected):
        probe.verify_runtime(validation=False)
    store.path.with_suffix(".retained").rename(store.path)
    from basswiesn.app.db import engine
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE schema_migrations"))
    with pytest.raises((probe.ProbeRejected, AssertionError)):
        probe.verify_runtime(validation=False)


def test_mode_mismatch_and_bad_schema_even_if_listener_claims_ready(runtime, listeners, monkeypatch):
    with pytest.raises(probe.ProbeRejected):
        probe.verify_runtime(validation=True)
    from basswiesn.app.db import engine
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE devices"))
    with pytest.raises(probe.ProbeRejected):
        probe.verify_runtime(validation=False)


def test_network_reader_disables_redirects_proxies_and_bounds_response(runtime, monkeypatch):
    calls = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit):
            assert limit == 4097
            return b"x" * 4097
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == "http://127.0.0.1:1328/ready" and timeout == 2
            return Response()
    def build(*handlers):
        assert handlers[0].proxies == {} and isinstance(handlers[1], probe._NoRedirect)
        return Opener()
    monkeypatch.setattr(probe.urllib.request, "build_opener", build)
    with pytest.raises(probe.ProbeRejected): probe._read_listener(1328, probe._token())
    assert len(calls) == 1
    with pytest.raises(probe.ProbeRejected):
        probe._NoRedirect().redirect_request(urllib.request.Request("http://127.0.0.1"), None, 302, "redirect", {}, "http://192.0.2.42")


def test_present_but_failed_new_probe_never_falls_back(runtime, monkeypatch, capsys):
    def fail(**_): raise probe.ProbeRejected()
    monkeypatch.setattr(probe, "verify_runtime", fail)
    with pytest.raises(SystemExit) as error:
        exec(probe_source('print("UNSAFE FALLBACK")', validation=False), {})
    assert error.value.code == 2 and "UNSAFE" not in capsys.readouterr().out


@pytest.mark.parametrize("optimization", [0, 1, 2])
def test_absent_new_probe_only_uses_explicit_legacy_mode(runtime, monkeypatch, capsys, optimization):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    with pytest.raises(SystemExit):
        exec(compile(probe_source('print("legacy")', validation=False), "<probe>", "exec", optimize=optimization), {})
    assert not capsys.readouterr().out
    monkeypatch.setattr(runtime[0], "admin_auth_mode", "legacy")
    exec(compile(probe_source('print("legacy")', validation=False), "<probe>", "exec", optimize=optimization), {})
    assert capsys.readouterr().out.strip() == "legacy"


@pytest.mark.parametrize("optimization", [0, 1, 2])
@pytest.mark.parametrize("validation", [False, True])
@pytest.mark.parametrize("tamper", ["none", "http", "not-object", "not-ready", "registry", "debug", "wrong-validation"])
def test_legacy_worker_checks_survive_optimized_compilation(runtime, monkeypatch, capsys, optimization, validation, tamper):
    import importlib.util
    from types import SimpleNamespace
    from basswiesn.update_helper.candidate_runtime import PROBE
    from basswiesn.update_helper.host_executor import HEALTH_PROBE

    settings, _store = runtime
    monkeypatch.setattr(settings, "admin_auth_mode", "legacy")
    monkeypatch.setattr(settings, "update_validation_mode", validation)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    bodies = {
        1328: {"ready": True, "version": settings.version, "update_validation_mode": validation,
               "checks": {"background_tasks": []}},
        1516: {"bmx_services": []}, 1860: {"status": "ok"},
    }
    if tamper == "not-object":
        bodies[1328] = []
    elif tamper == "not-ready":
        bodies[1328]["ready"] = False
    elif tamper == "registry":
        bodies[1516]["bmx_services"] = "not a service list"
    elif tamper == "debug":
        bodies[1860]["status"] = "failed"
    elif tamper == "wrong-validation":
        bodies[1328]["update_validation_mode"] = not validation

    class Response:
        status = 503 if tamper == "http" else 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            assert limit == 131073
            return json.dumps(self.body).encode()

    def open_response(url, *, timeout):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        assert parsed.hostname == "127.0.0.1" and timeout == 2
        return Response(bodies[parsed.port])

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_response))
    source = compile(PROBE if validation else HEALTH_PROBE, "<legacy-probe>", "exec", optimize=optimization)
    # Dynamic exec compiles its input using the interpreter's optimization, not
    # the caller code object's level. Exercise both layers at the selected level.
    def execute_legacy(legacy_source):
        exec(compile(legacy_source, "<legacy-body>", "exec", optimize=optimization), {})

    namespace = {"exec": execute_legacy}
    if tamper == "none":
        exec(source, namespace)
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["ready"] is True and receipt["version"] == settings.version
    else:
        with pytest.raises(SystemExit) as rejected:
            exec(source, namespace)
        assert rejected.value.code == 2
        assert not capsys.readouterr().out


def test_real_launcher_revokes_sessions_before_any_worker_starts(runtime, monkeypatch):
    settings, store = runtime
    password = secrets.token_urlsafe(24)
    store.reset_password("operator", password)
    cookie = store.login("operator", password, peer="fixture").cookie
    previous = probe._token()
    monkeypatch.setattr(settings, "enable_https", False)
    monkeypatch.setattr(sys, "argv", ["tools/run_dev.py"])
    monkeypatch.setattr("signal.signal", lambda *_: None)
    started = []
    class Worker:
        pid = None
        exitcode = 0
        def __init__(self, **_): pass
        def start(self):
            assert store.session(cookie) is None
            assert probe._token() != previous
            started.append(True)
    monkeypatch.setattr("multiprocessing.Process", Worker)
    with pytest.raises(SystemExit):
        runpy.run_path("tools/run_dev.py", run_name="__main__")
    assert len(started) == 3
