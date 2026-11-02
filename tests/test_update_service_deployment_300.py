"""Offline synthetic installation + real local Unix listener, no Docker or LAN."""
import hashlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import zipfile

import pytest

from basswiesn.update_helper import deployment as deploy, daemon, onboarding
from basswiesn.update_helper.journal import Journal
from basswiesn.update_helper.protocol import Authorization
from basswiesn.update_helper.service import UpdateService
from basswiesn.update_helper.transaction import TransactionRunner
from test_update_host_preflight_300 import enrolled, observed

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def bundle():
    return deploy.build_bundle(Path(__file__).resolve().parents[1])


@pytest.fixture
def prepared(enrolled, tmp_path):
    policy = enrolled[0]
    root, units = tmp_path / "host", tmp_path / "units"
    units.mkdir(mode=0o700)
    plan = onboarding.preview(root=policy.root, installation_owner_uid=os.getuid(),
        compose_project=policy.compose_project, container_id=policy.container_id,
        image_id=policy.image_id, version=policy.version, target=str(root), owner_uid=os.getuid())
    onboarding.prepare(plan, approve=True, approval_sha256=plan.approval_sha256, observe=observed)
    return {"root": str(root), "unit_root": str(units), "owner_uid": os.getuid()}


def install(bundle, prepared):
    return deploy.provision(bundle, approve=True, approve_sha256=deploy.sha(bundle), **prepared)


def test_bundle_is_reproducible_and_self_contained(bundle):
    assert bundle == deploy.build_bundle(Path(__file__).resolve().parents[1])
    assert deploy.verify_bundle(bundle) == deploy.sha(bundle)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == deploy.EXPECTED | {"manifest.json"}
        assert not any(".env" in name or "tests" in name or "__pycache__" in name for name in archive.namelist())


def test_unit_writes_are_limited_to_enrolled_root_and_specifiers_are_escaped(enrolled):
    policy = replace(enrolled[0], root="/srv/example radio%name")
    text = deploy.units_for(policy)[deploy.SERVICE_NAME].decode()
    assert 'ReadWritePaths=/var/lib/basswiesn-update "/srv/example radio%%name"\n' in text
    assert "ProtectSystem=strict\n" in text and "ProtectHome=read-only\n" in text
    assert "RestrictSUIDSGID=no\n" in text  # preserve valid installer directory setgid
    assert "NoNewPrivileges=yes\n" in text
    assert "ReadWritePaths=/srv\n" not in text


@pytest.mark.parametrize("path", ['/srv/bad"scope', '/srv/bad\nscope', '/srv/bad,scope'])
def test_unit_never_expands_untrusted_path_syntax(enrolled, path):
    with pytest.raises(ValueError):
        deploy.units_for(replace(enrolled[0], root=path))


@pytest.mark.parametrize("kind", ["bad-header", "truncated", "manifest", "extra", "duplicate", "syntax", "entrypoint", "compressed", "link"])
def test_bundle_refuses_corruption_and_ambiguous_code(bundle, kind):
    if kind == "bad-header":
        bad = b"not zip"
    elif kind == "truncated":
        bad = bundle[:-20]
    else:
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(bundle)) as source, zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if kind in {"manifest", "entrypoint", "syntax"} and item.filename == {"manifest": "manifest.json", "entrypoint": "__main__.py", "syntax": "basswiesn/update_helper/daemon.py"}[kind]:
                    data = b"invalid!"
                if kind == "compressed":
                    item.compress_type = zipfile.ZIP_DEFLATED
                if kind == "link":
                    item.external_attr = (stat.S_IFLNK | 0o600) << 16
                target.writestr(item, data)
            if kind == "extra":
                target.writestr("extra.py", b"pass")
            if kind == "duplicate":
                target.writestr("__main__.py", deploy.ENTRY)
        bad = output.getvalue()
    with pytest.raises(deploy.DeploymentRejected, match="^BUNDLE_INVALID$"):
        deploy.verify_bundle(bad)


def test_install_seals_code_units_and_separate_capability_without_activation(bundle, prepared, capsys):
    result = install(bundle, prepared)
    auth = deploy.verify_deployment(**prepared)
    root = Path(prepared["root"]) / "service"
    token = (root / "administrator.capability").read_text().strip()
    assert len(token) == 43 and auth.credential_sha256 == hashlib.sha256(token.encode()).hexdigest()
    assert result["capability_provisioned"] and not result["capability_delivered_to_web"]
    assert not result["service_enabled"] and not result["installation_available"]
    assert token not in json.dumps(result) + repr(auth) + capsys.readouterr().out
    for path in (root, *root.iterdir(), *Path(prepared["unit_root"]).iterdir()):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert json.loads((Path(prepared["root"]) / "journal/state.json").read_text())["jobs"] == []


@pytest.mark.parametrize("approve,digest", [(False, "match"), (1, "match"), (True, "0" * 64)])
def test_install_needs_explicit_matching_approval(bundle, prepared, approve, digest):
    with pytest.raises(deploy.DeploymentRejected, match="^APPROVAL_REQUIRED$"):
        deploy.provision(bundle, approve=approve, approve_sha256=deploy.sha(bundle) if digest == "match" else digest, **prepared)
    assert not (Path(prepared["root"]) / "service").exists()


@pytest.mark.parametrize("target", ["helper.pyz", "administrator.capability", "authorization.json", "installed.json", "reservation.json", "extra"])
def test_any_changed_service_state_refuses_start(bundle, prepared, target):
    install(bundle, prepared)
    path = Path(prepared["root"]) / "service" / target
    path.write_bytes(b"synthetic changed data")
    with pytest.raises(deploy.DeploymentRejected):
        deploy.verify_deployment(**prepared)


@pytest.mark.parametrize("kind", ["writable", "link", "hardlink", "unit", "peer-schema"])
def test_permissions_links_units_and_peer_are_reverified(bundle, prepared, kind):
    install(bundle, prepared)
    path = Path(prepared["root"]) / "service/helper.pyz"
    if kind == "writable":
        path.chmod(0o666)
    elif kind == "link":
        saved = path.with_suffix(".original")
        path.rename(saved)
        path.symlink_to(saved)
    elif kind == "hardlink":
        os.link(path, path.with_suffix(".linked"))
    elif kind == "unit":
        (Path(prepared["unit_root"]) / deploy.SERVICE_NAME).write_bytes(b"changed")
    else:
        path = Path(prepared["root"]) / "peer.json"
        value = json.loads(path.read_text())
        value["schema"] = True
        path.write_text(json.dumps(value))
    with pytest.raises(deploy.DeploymentRejected):
        deploy.verify_deployment(**prepared)


def test_existing_unit_is_not_overwritten(bundle, prepared):
    path = Path(prepared["unit_root"]) / deploy.SERVICE_NAME
    path.write_text("operator existing unit")
    with pytest.raises(deploy.DeploymentRejected, match="^DEPLOYMENT_EXISTS$"):
        install(bundle, prepared)
    assert path.read_text() == "operator existing unit"
    assert not (Path(prepared["root"]) / "service").exists()


def test_partial_install_is_never_reset_or_enabled(bundle, prepared, monkeypatch):
    original = deploy._write_new
    def interrupt(parent, name, data, owner):
        if name == "authorization.json":
            raise OSError("synthetic credential must not escape")
        return original(parent, name, data, owner)
    monkeypatch.setattr(deploy, "_write_new", interrupt)
    with pytest.raises(deploy.DeploymentRejected, match="^DEPLOYMENT_IO$"):
        install(bundle, prepared)
    assert (Path(prepared["root"]) / "service/reservation.json").exists()
    assert not list(Path(prepared["unit_root"]).iterdir())
    with pytest.raises(deploy.DeploymentRejected):
        install(bundle, prepared)


def frame(connection, body):
    data = json.dumps(body).encode()
    connection.sendall(struct.pack("!I", len(data)) + data)
    def receive(size):
        result = b""
        while len(result) < size:
            part = connection.recv(size - len(result))
            assert part
            result += part
        return result
    return json.loads(receive(struct.unpack("!I", receive(4))[0]))


def test_real_unix_listener_status_and_authenticated_install_fail_closed(tmp_path):
    path = tmp_path / "journal"
    path.mkdir(mode=0o700)
    journal = Journal(path, owner_uid=os.getuid())
    journal.initialize()
    credential = "A" * 43  # synthetic, never deployed
    service = UpdateService(Authorization(os.getuid(), hashlib.sha256(credential.encode()).hexdigest()), TransactionRunner(journal))
    stop = threading.Event()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(tmp_path / "control.sock"))
        listener.listen(4)
        thread = threading.Thread(target=daemon.run_listener, args=(listener, service, stop))
        thread.start()
        try:
            for body, expected in [({"protocol": 1, "action": "status"}, "IDLE"),
                ({"protocol": 1, "action": "install", "approve": True, "credential": credential,
                  "request_id": "12345678-1234-4234-8234-123456789abc", "target_version": "3.0.0",
                  "expected_current_version": "2.6.5"}, "EXECUTOR_UNAVAILABLE")]:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(tmp_path / "control.sock"))
                    reply = frame(client, body)
                assert reply.get("result", {}).get("state", reply.get("code")) == expected
                assert credential not in json.dumps(reply)
            assert journal.read()["jobs"] == []
        finally:
            stop.set()
            thread.join(timeout=6)
            assert not thread.is_alive()
            journal.close()


@pytest.mark.parametrize("values", [{}, {"LISTEN_PID": "0", "LISTEN_FDS": "1"}, {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "2"}])
def test_inherited_listener_rejects_unrelated_activation(values, monkeypatch):
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="SOCKET_ACTIVATION_INVALID"):
        daemon.inherited_listener()
