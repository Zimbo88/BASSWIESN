"""Offline CLI contracts and adversarial local artifacts; no live installations."""

from __future__ import annotations

from contextlib import closing
import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from zipfile import ZipFile, ZipInfo

import pytest

from basswiesn import __version__
from basswiesn import cli, cli_checks, cli_storage
from basswiesn.app.db.database import Base
from basswiesn.cli_checks import OfflineConfig, load_config
from basswiesn.cli_contract import OfflineError
from basswiesn.cli_schema import declared_baseline, declared_schema
from basswiesn.cli_storage import create_backup, offline_database, private_artifact, verify_backup

pytestmark = pytest.mark.integration


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    for key in cli_checks.CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    data = tmp_path / "installation"
    data.mkdir(mode=0o700)
    database = data / "basswiesn.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables["devices"].insert().values(device_id="AABBCCDDEEFF", ip_address="192.0.2.11", name="private device name", reachable=True, last_seen=datetime(2026, 9, 30, 12), failure_count=0))
        connection.execute(Base.metadata.tables["settings"].insert(), [{"key": "protected_device_ips", "value": "192.0.2.11"}, {"key": "ordinary_note", "value": "opaque-sensitive-test-value"}])
        connection.execute(Base.metadata.tables["runtime_state"].insert(), [{"key": "server:last_boot", "value": "2026-09-30T10:00:00+00:00"}, {"key": "server:restart_count", "value": "3"}])
    engine.dispose()
    database.chmod(0o600)
    return data, database


def insert_row(path, table, **values):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(Base.metadata.tables[table].insert().values(**values))
    engine.dispose()


def invoke(capsys, installation, command, *arguments):
    data, database = installation
    code = cli.main(["--json", "--data-dir", str(data), "--database", str(database), command, *map(str, arguments)])
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["offline"] is True
    assert payload["version"] == __version__
    assert payload["command"] == command
    assert code == {"PASS": 0, "WARN": 1, "FAIL": 2}[payload["status"]]
    assert "opaque-sensitive-test-value" not in captured.out
    assert "private device name" not in captured.out
    return code, payload


def tree_state(path):
    return {str(item.relative_to(path)): (item.stat().st_mode, item.stat().st_mtime_ns, hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None) for item in path.rglob("*")}


def test_schema_projection_matches_real_models():
    from basswiesn.app.db.migrations import SCHEMA_BASELINE

    assert declared_schema() == {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}
    assert declared_baseline() == SCHEMA_BASELINE


@pytest.mark.parametrize("command", ["status", "doctor", "devices", "health", "diagnostics", "providers", "version"])
def test_every_read_command_is_bounded_and_does_not_mutate(capsys, installation, monkeypatch, command):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("offline CLI attempted a transport/subprocess")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "gethostbyname_ex", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    before = tree_state(installation[0])
    code, payload = invoke(capsys, installation, command)
    assert code in {0, 1}
    assert tree_state(installation[0]) == before
    assert "192.0.2.11" not in json.dumps(payload)
    assert "AABBCCDDEEFF" not in json.dumps(payload)


def test_status_distinguishes_cached_from_live(capsys, installation):
    code, payload = invoke(capsys, installation, "status")
    assert code == 0
    assert payload["data"]["devices"] == 1
    assert payload["data"]["recorded_reachable"] == 1
    assert payload["data"]["live_runtime"] == "NOT_PROBED"


def test_doctor_full_schema_runtime_and_policy(capsys, installation):
    code, payload = invoke(capsys, installation, "doctor")
    assert code == 1  # Deliberate legacy-auth and no-live-readiness warnings.
    checks = {item["code"]: item for item in payload["checks"]}
    assert checks["database.schema"]["status"] == "PASS"
    assert checks["database.integrity"]["status"] == "PASS"
    assert checks["policy.protected"]["status"] == "PASS"
    assert checks["auth.mode"]["status"] == "WARN"
    assert payload["data"]["schema"]["missing_tables"] == []
    assert payload["data"]["runtime"]["recorded_restart_count"] == 3
    assert payload["data"]["runtime"]["last_boot"] == "2026-09-30T10:00:00+00:00"
    assert payload["data"]["configured_ports"]["cloud"] == 1516


def test_devices_explicit_identifiers_are_not_names_or_credentials(capsys, installation):
    code, payload = invoke(capsys, installation, "devices", "--include-identifiers")
    row = payload["data"]["devices"][0]
    assert code == 0
    assert row["device_id"] == "AABBCCDDEEFF"
    assert row["ip_address"] == "192.0.2.11"
    assert row["protected"] is True
    assert row["live_reachability"] == "NOT_PROBED"
    assert "name" not in row


def test_device_rows_never_echo_untrusted_identifiers(capsys, installation):
    with closing(sqlite3.connect(installation[1])) as db:
        db.execute("UPDATE devices SET device_id=?, ip_address=?, last_seen=?, failure_count=?", ("opaque-sensitive-test-value", "https://user:password@example.invalid", "Bearer private-value", "bad-value"))
        db.commit()
    _, payload = invoke(capsys, installation, "devices", "--include-identifiers")
    row = payload["data"]["devices"][0]
    assert row["device_id"] == row["ip_address"] == "UNAVAILABLE"
    assert row["last_seen"] is None
    assert row["recorded_failure_count"] is None


def test_devices_limit_and_truncation(capsys, installation):
    insert_row(installation[1], "devices", device_id="001122334455", ip_address="192.0.2.12", reachable=False)
    _, payload = invoke(capsys, installation, "devices", "--limit", 1)
    assert len(payload["data"]["devices"]) == 1
    assert payload["data"]["truncated"] is True
    assert payload["data"]["total"] == 2


@pytest.mark.parametrize("limit", [0, -1, 501, 1000000000000])
def test_limits_fail_closed(capsys, installation, limit):
    assert invoke(capsys, installation, "devices", "--limit", limit)[0] == 2


def test_health_history_is_allowlisted(capsys, installation):
    insert_row(installation[1], "healthcheck_runs", run_id="synthetic-run", status="opaque-sensitive-test-value", started_at=datetime(2026, 9, 30, 12), duration_ms=7, summary_json="opaque-sensitive-test-value")
    insert_row(installation[1], "provider_health_state", device_id="AABBCCDDEEFF", provider_id="private account", state="FAILED")
    code, payload = invoke(capsys, installation, "health")
    assert code == 0
    assert payload["data"]["runs"][0]["status"] == "unknown"
    assert payload["data"]["provider_state_counts"] == {"FAILED": 1}


def test_missing_database_does_not_create_directories(tmp_path, capsys):
    target = tmp_path / "absent" / "data"
    assert cli.main(["doctor", "--json", "--data-dir", str(target)]) == 2
    assert not target.parent.exists()
    assert "Traceback" not in capsys.readouterr().out


def test_doctor_detects_historical_schema_without_migrating(capsys, installation):
    with closing(sqlite3.connect(installation[1])) as db:
        db.execute("DROP TABLE provider_health_state")
        db.execute("ALTER TABLE devices DROP COLUMN capabilities_xml")
        db.commit()
    before = installation[1].read_bytes()
    code, payload = invoke(capsys, installation, "doctor")
    assert code == 2
    assert "provider_health_state" in payload["data"]["schema"]["missing_tables"]
    assert "devices.capabilities_xml" in payload["data"]["schema"]["missing_columns"]
    assert installation[1].read_bytes() == before


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_journal_refused_without_creation_or_deletion(capsys, installation, suffix):
    Path(str(installation[1]) + suffix).write_bytes(b"uncheckpointed sentinel")
    before = tree_state(installation[0])
    code, payload = invoke(capsys, installation, "status")
    assert code == 2
    assert payload["checks"][0]["code"] == "database.active_journal"
    assert tree_state(installation[0]) == before


def test_real_wal_database_refused(capsys, installation):
    with closing(sqlite3.connect(installation[1])) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE devices SET reachable=0")
        writer.commit()
        assert Path(str(installation[1]) + "-wal").stat().st_size > 0
        assert invoke(capsys, installation, "status")[0] == 2


def test_database_change_detected_before_return(installation):
    with pytest.raises(OfflineError, match="changed"):
        with offline_database(installation[1]) as db:
            assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
            with closing(sqlite3.connect(installation[1])) as writer:
                writer.execute("UPDATE devices SET reachable=0")
                writer.commit()


@pytest.mark.parametrize("kind", ["file_symlink", "parent_symlink", "hardlink", "directory"])
def test_unsafe_database_paths_fail_closed(tmp_path, capsys, installation, kind):
    path = tmp_path / "unsafe"
    if kind == "file_symlink":
        path.symlink_to(installation[1])
    elif kind == "parent_symlink":
        path.symlink_to(installation[0], target_is_directory=True)
        path = path / "basswiesn.db"
    elif kind == "hardlink":
        os.link(installation[1], path)
    else:
        path.mkdir()
    assert cli.main(["status", "--json", "--database", str(path)]) == 2
    assert "path.unsafe" in capsys.readouterr().out


def test_corrupt_database_no_raw_error(capsys, installation):
    installation[1].write_bytes(b"not a database opaque-sensitive-test-value")
    code, payload = invoke(capsys, installation, "status")
    assert code == 2
    assert payload["checks"][0]["code"] == "database.unreadable"


def test_oversized_database_without_reading(capsys, installation, monkeypatch):
    monkeypatch.setattr(cli_storage, "MAX_DATABASE_BYTES", 1)
    code, payload = invoke(capsys, installation, "status")
    assert code == 2
    assert payload["checks"][0]["code"] == "database.too_large"


def test_database_query_deadline(capsys, installation, monkeypatch):
    monkeypatch.setattr(cli_storage, "SQL_SECONDS", -1)
    assert invoke(capsys, installation, "status")[0] == 2


def test_config_literals_environment_precedence_and_no_execution(tmp_path):
    config = tmp_path / "settings.env"
    config.write_text("# sample\nexport BASSWIESN_ADMIN_AUTH_MODE='required'\nBASSWIESN_ENABLE_HTTPS=true # enabled\nUNRELATED=$(touch /tmp/never-run-cli-test)\n")
    loaded = load_config(tmp_path, None, config, {"BASSWIESN_ADMIN_AUTH_MODE": "legacy"})
    assert loaded.text("BASSWIESN_ADMIN_AUTH_MODE") == "legacy"
    assert loaded.enabled("BASSWIESN_ENABLE_HTTPS") is True
    assert "UNRELATED" not in loaded.values
    assert "values" not in repr(loaded)


@pytest.mark.parametrize("content", ["no assignment", "BASSWIESN_ADMIN_AUTH_MODE=$(secret)", 'BASSWIESN_ADMIN_AUTH_MODE="unterminated', "BASSWIESN_ADMIN_AUTH_MODE=`secret`", "BASSWIESN_ADMIN_AUTH_MODE=required\\n"])
def test_config_unsupported_syntax_is_never_executed(tmp_path, content):
    config = tmp_path / "settings.env"
    config.write_text(content)
    with pytest.raises(OfflineError):
        load_config(tmp_path, None, config, {})


@pytest.mark.parametrize("value", ["not-an-ip", "https://opaque-sensitive-test-value.invalid", "192.0.2.999"])
def test_invalid_protection_fails_without_raw_values(capsys, installation, value):
    with closing(sqlite3.connect(installation[1])) as db:
        db.execute("UPDATE settings SET value=? WHERE key='protected_device_ips'", (value,))
        db.commit()
    assert invoke(capsys, installation, "devices")[0] == 2


def test_mapped_ipv6_protection(installation):
    config = OfflineConfig(*installation, values={"PROTECTED_DEVICE_IPS": "::ffff:192.0.2.12", "PROTECTED_DEVICE_IDS": "001122334455"})
    with offline_database(installation[1]) as db:
        ips, ids = cli_checks.protected_policy(config, db)
    assert ips == {"192.0.2.11", "192.0.2.12"}
    assert ids == {"001122334455"}


@pytest.mark.parametrize("key,value,code", [
    ("BASSWIESN_HTTPS_PORT", "0", "runtime.ports"),
    ("BASSWIESN_HTTPS_PORT", "65536", "runtime.ports"),
    ("BASSWIESN_HTTPS_PORT", "opaque-sensitive-test-value", "runtime.ports"),
    ("BASSWIESN_OFFLINE_MODE", "invalid", "config.offline_mode"),
    ("BASSWIESN_ENABLE_HTTPS", "sometimes", "config.boolean"),
    ("BASSWIESN_ADMIN_AUTH_MODE", "invalid", "auth.mode"),
])
def test_invalid_runtime_metadata(capsys, installation, monkeypatch, key, value, code):
    monkeypatch.setenv(key, value)
    exit_code, payload = invoke(capsys, installation, "doctor")
    assert exit_code == 2
    assert any(check["code"] == code and check["status"] == "FAIL" for check in payload["checks"])


def test_port_collision_and_required_auth_failures(capsys, installation, monkeypatch):
    monkeypatch.setenv("BASSWIESN_ENABLE_HTTPS", "true")
    monkeypatch.setenv("BASSWIESN_HTTPS_PORT", "1328")
    monkeypatch.setenv("BASSWIESN_ADMIN_AUTH_MODE", "required")
    code, payload = invoke(capsys, installation, "doctor")
    checks = {item["code"]: item["status"] for item in payload["checks"]}
    assert code == 2
    assert checks["runtime.ports"] == checks["auth.origins"] == checks["auth.file"] == "FAIL"
    assert not (installation[0] / "secrets").exists()


def test_required_auth_schema_inspection_has_no_credential_side_effect(capsys, installation, monkeypatch):
    from basswiesn.app.core.admin_store import AdminStore

    auth = installation[0] / "secrets" / "admin" / "auth.sqlite3"
    auth.parent.parent.mkdir(mode=0o700)
    store = AdminStore(auth)
    store.bootstrap("operator", "synthetic test-only password for CLI")
    monkeypatch.setenv("BASSWIESN_ADMIN_AUTH_MODE", "required")
    monkeypatch.setenv("BASSWIESN_ENABLE_HTTPS", "true")
    monkeypatch.setenv("BASSWIESN_ADMIN_ORIGINS", "https://admin.example.invalid:1329")
    before = tree_state(installation[0])
    code, payload = invoke(capsys, installation, "doctor")
    assert code == 1  # Offline runtime remains unverified.
    assert any(check["code"] == "auth.schema" and check["status"] == "PASS" for check in payload["checks"])
    assert tree_state(installation[0]) == before
    assert "password_hash" not in json.dumps(payload)


@pytest.mark.parametrize("mode", [0o644, 0o666, 0o777])
def test_secret_file_permissions_fail(capsys, installation, monkeypatch, mode):
    secret = installation[0] / "secret.key"
    secret.write_text("opaque-sensitive-test-value")
    secret.chmod(mode)
    monkeypatch.setenv("BASSWIESN_TLS_KEY_FILE", str(secret))
    code, payload = invoke(capsys, installation, "doctor")
    assert code == 2
    assert any(check["code"].startswith("secrets.configured.") and check["status"] == "FAIL" for check in payload["checks"])


def test_low_disk_warning(capsys, installation, monkeypatch):
    from collections import namedtuple

    monkeypatch.setattr(cli_checks.shutil, "disk_usage", lambda path: namedtuple("Usage", "total used free")(100, 99, 1))
    _, payload = invoke(capsys, installation, "doctor")
    assert any(check["code"] == "storage.capacity" and check["status"] == "WARN" for check in payload["checks"])


def test_backup_round_trip_preserves_source_and_is_private(capsys, installation):
    output = installation[0] / "snapshot.zip"
    before = installation[1].read_bytes()
    code, payload = invoke(capsys, installation, "backup", "--output", output, "--offline-confirmed")
    assert code == 0
    assert payload["data"]["created"] is True
    assert payload["data"]["contains_sensitive_database"] is True
    assert payload["data"]["authentication_store_included"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert installation[1].read_bytes() == before
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {"database.sqlite", "settings.json", "manifest.json"}
        assert json.loads(archive.read("settings.json")) == {}
        assert b"opaque-sensitive-test-value" in archive.read("database.sqlite")
    assert invoke(capsys, installation, "verify-backup", output)[0] == 0
    assert not list(installation[0].glob(".basswiesnctl-*"))


def test_existing_backup_service_can_preview_cli_artifact(installation, monkeypatch):
    from basswiesn.app.config import Settings
    from basswiesn.app.services import backup_restore

    root = installation[0] / "backups" / "system"
    root.mkdir(parents=True, mode=0o700)
    output = root / "cli.zip"
    create_backup(installation[1], output)
    monkeypatch.setattr(backup_restore, "get_settings", lambda: Settings(data_dir=installation[0]))
    assert backup_restore.preview_system_backup(str(output))["ok"] is True


def test_backup_requires_explicit_offline_acknowledgement(capsys, installation):
    output = installation[0] / "snapshot.zip"
    assert invoke(capsys, installation, "backup", "--output", output)[0] == 2
    assert not output.exists()


def test_backup_refuses_incomplete_current_schema(capsys, installation):
    with closing(sqlite3.connect(installation[1])) as db:
        db.execute("DROP TABLE provider_health_state")
        db.commit()
    output = installation[0] / "snapshot.zip"
    assert invoke(capsys, installation, "backup", "--output", output, "--offline-confirmed")[0] == 2
    assert not output.exists()
    assert not list(installation[0].glob(".basswiesnctl-*"))


def test_backup_never_overwrites(capsys, installation):
    output = installation[0] / "existing.zip"
    output.write_bytes(b"existing sentinel")
    assert invoke(capsys, installation, "backup", "--output", output, "--offline-confirmed")[0] == 2
    assert output.read_bytes() == b"existing sentinel"


def test_atomic_publication_race_does_not_replace(installation):
    output = installation[0] / "raced.zip"
    with pytest.raises(FileExistsError):
        with private_artifact(output) as staged:
            staged.write(b"ours")
            output.write_bytes(b"another writer")
    assert output.read_bytes() == b"another writer"
    assert not list(installation[0].glob(".basswiesnctl-*"))


def test_failed_artifact_write_never_publishes(installation):
    output = installation[0] / "interrupted.zip"
    with pytest.raises(RuntimeError):
        with private_artifact(output) as staged:
            staged.write(b"partial")
            raise RuntimeError("injected")
    assert not output.exists()
    assert not list(installation[0].glob(".basswiesnctl-*"))


def test_output_requires_private_existing_directory(capsys, installation):
    public = installation[0] / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)  # Keep the unsafe fixture explicit under a private CI umask.
    assert invoke(capsys, installation, "backup", "--output", public / "snapshot.zip", "--offline-confirmed")[0] == 2
    assert list(public.iterdir()) == []


def archive_bytes(database, *, version=__version__, extra=False, settings=b"{}", corrupt_hash=False):
    manifest = {"format": "basswiesn-backup-v1", "created_at": "2026-09-30T12:00:00+00:00", "version": version, "schema_baseline": 2, "quick_check": "ok", "restore_mode": "prepare_and_restart", "files": [{"path": "database.sqlite", "sha256": hashlib.sha256(database).hexdigest()}, {"path": "settings.json", "sha256": "0" * 64 if corrupt_hash else hashlib.sha256(settings).hexdigest()}]}
    result = {"manifest.json": json.dumps(manifest).encode(), "database.sqlite": database, "settings.json": settings}
    if extra:
        result["secrets.txt"] = b"do not allow"
    return result


@pytest.mark.parametrize("case", ["path_traversal", "extra", "duplicate", "symlink", "hash", "sqlite", "settings", "unsupported", "empty", "invalid_json", "metadata_limit"])
def test_adversarial_archives_never_restore(capsys, installation, case):
    output = installation[0] / "bad.zip"
    payload = archive_bytes(installation[1].read_bytes(), extra=case == "extra", settings=b"[]" if case == "settings" else b"{}", corrupt_hash=case == "hash")
    if case == "sqlite":
        payload = archive_bytes(b"opaque-sensitive-test-value")
    if case == "unsupported":
        manifest = json.loads(payload["manifest.json"])
        manifest["schema_baseline"] = 999
        payload["manifest.json"] = json.dumps(manifest).encode()
    if case == "empty":
        payload = {}
    if case == "invalid_json":
        payload["manifest.json"] = b"[opaque-sensitive-test-value"
    if case == "metadata_limit":
        payload["manifest.json"] = b" " * (cli_storage.MAX_METADATA_BYTES + 1)
    with ZipFile(output, "w") as archive:
        for name, content in payload.items():
            if case == "path_traversal" and name == "settings.json":
                name = "../settings.json"
            if case == "symlink" and name == "settings.json":
                info = ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, content)
            else:
                archive.writestr(name, content)
        if case == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr("settings.json", b"{}")
    before = tree_state(installation[0])
    assert invoke(capsys, installation, "verify-backup", output)[0] == 2
    assert tree_state(installation[0]) == before
    assert not (installation[0] / "restore-pending.zip").exists()


@pytest.mark.parametrize("version", ["0.1.0", "999.0.0", "3.0.2-unknown"])
def test_other_archive_versions_are_not_claimed_compatible(capsys, installation, version):
    output = installation[0] / "other-version.zip"
    with ZipFile(output, "w") as archive:
        for name, content in archive_bytes(installation[1].read_bytes(), version=version).items():
            archive.writestr(name, content)
    code, payload = invoke(capsys, installation, "verify-backup", output)
    assert code == 1
    assert payload["data"]["integrity_verified"] is True
    assert payload["data"]["version_compatible"] is False
    assert payload["data"]["compatibility"] == "NOT_CONFIRMED"


def test_doctor_diagnostics_file_is_allowlisted_and_private(capsys, installation):
    output = installation[0] / "diagnostics.json"
    before = installation[1].read_bytes()
    assert invoke(capsys, installation, "diagnostics", "--output", output)[0] == 1
    payload = json.loads(output.read_text())
    assert payload["command"] == "diagnostics"
    assert payload["data"]["devices"]["identifiers_included"] is False
    assert "opaque-sensitive-test-value" not in output.read_text()
    assert "192.0.2.11" not in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert installation[1].read_bytes() == before


def test_cli_fresh_process_never_imports_startup_or_creates_data(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    before = tree_state(tmp_path)
    code = """
import sys, socket, subprocess
sys.path.insert(0, sys.argv[1])
def blocked(*args, **kwargs):
    raise AssertionError('unexpected transport or process')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.socket.sendto = blocked
socket.getaddrinfo = blocked
socket.gethostbyname_ex = blocked
subprocess.run = blocked
from basswiesn.cli import main
assert main(['version', '--json']) == 0
assert main(['status', '--json']) == 2
assert 'basswiesn.app.config' not in sys.modules
assert 'basswiesn.app.db' not in sys.modules
assert 'basswiesn.app.main' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code, str(repository)], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert tree_state(tmp_path) == before


def test_source_launcher_works_outside_repository(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "tools" / "basswiesnctl"
    before = tree_state(tmp_path)
    result = subprocess.run([sys.executable, str(launcher), "version", "--json"], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert json.loads(result.stdout)["data"]["version"] == __version__
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("command", ["restore", "reboot", "migrate", "discover", "set-volume", "start", "update", "--url", "--token"])
def test_no_critical_or_network_command(command, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([command])
    assert exc.value.code == 2
    assert capsys.readouterr().err


def test_global_options_before_and_after_subcommand(capsys):
    assert cli.main(["--json", "version"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.main(["version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == first


def test_text_output_is_readable(capsys, installation):
    assert cli.main(["status", "--database", str(installation[1])]) == 0
    output = capsys.readouterr().out
    assert "PASS | OFFLINE" in output
    assert "status.snapshot" in output


@pytest.mark.parametrize("argv", [["secret-unrecognized-command"], ["version", "--token", "secret-token-value"], ["devices", "--limit", "secret-not-an-integer"]])
def test_usage_errors_never_echo_submitted_values(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "secret-" not in captured.err + captured.out


def test_protection_alias_shadow_is_reported(capsys, installation, monkeypatch):
    monkeypatch.setenv("PROTECTED_DEVICE_IPS", "192.0.2.12")
    monkeypatch.setenv("BASSWIESN_PROTECTED_DEVICE_IPS", "192.0.2.13")
    _, payload = invoke(capsys, installation, "doctor")
    assert any(check["code"] == "policy.alias_shadowed" for check in payload["checks"])
    assert payload["data"]["protected_policy"]["ip_entries"] == 2  # authoritative + persisted


@pytest.mark.parametrize("key,value", [("PROTECTED_DEVICE_IPS", "192.0.2.12 192.0.2.13"), ("PROTECTED_DEVICE_IDS", "INVALID SPACE")])
def test_nonruntime_protection_syntax_fails(capsys, installation, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert invoke(capsys, installation, "status")[0] == 2


@pytest.mark.parametrize("source", ["", "class Broken:\n __tablename__ = 'devices'\n", "class Broken:\n __tablename__ = '../bad'\n id: int = mapped_column(Integer)\n", "class Broken:\n __tablename__ = 'devices'\n id: int = mapped_column(Integer)\nclass Duplicate:\n __tablename__ = 'devices'\n id: int = mapped_column(Integer)\n", "not valid python !!!!"])
def test_schema_source_missing_or_unsupported_fails(monkeypatch, source):
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: source)
    with pytest.raises(OfflineError):
        declared_schema()


@pytest.mark.parametrize("source", ["", "SCHEMA_BASELINE = 99", "SCHEMA_BASELINE = True", "not valid python !!!!"])
def test_schema_baseline_never_inferred(monkeypatch, source):
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: source)
    with pytest.raises(OfflineError):
        declared_baseline()


@pytest.mark.parametrize("source", ["", "IMMUTABLE_PROTECTED_IPS: set = arbitrary_code()", "IMMUTABLE_PROTECTED_IPS: set = frozenset([123])", "IMMUTABLE_PROTECTED_IPS: set = frozenset({'a': 'b'})", "not valid python !!!!"])
def test_immutable_policy_is_not_executed(monkeypatch, source):
    from basswiesn.cli_schema import immutable_policy

    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: source)
    with pytest.raises(OfflineError):
        immutable_policy()


def test_immutable_literal_policy_is_honored(monkeypatch, installation):
    monkeypatch.setattr(cli_checks, "immutable_policy", lambda: ({"192.0.2.77"}, {"112233445566"}))
    with offline_database(installation[1]) as db:
        ips, ids = cli_checks.protected_policy(OfflineConfig(*installation), db)
    assert ips == {"192.0.2.11", "192.0.2.77"}
    assert ids == {"112233445566"}


def test_legacy_existing_auth_permissions_are_still_checked(capsys, installation):
    auth = installation[0] / "secrets" / "admin" / "auth.sqlite3"
    auth.parent.mkdir(parents=True, mode=0o700)
    auth.write_bytes(b"private account fixture")
    auth.chmod(0o644)
    code, payload = invoke(capsys, installation, "doctor")
    assert code == 2
    assert any(check["code"] == "auth.file" and check["status"] == "FAIL" for check in payload["checks"])


def test_incomplete_archive_schema_is_not_claimed_compatible(capsys, installation):
    with closing(sqlite3.connect(installation[1])) as db:
        db.execute("DROP TABLE provider_health_state")
        db.commit()
    output = installation[0] / "old-schema.zip"
    with ZipFile(output, "w") as archive:
        for name, content in archive_bytes(installation[1].read_bytes()).items():
            archive.writestr(name, content)
    code, payload = invoke(capsys, installation, "verify-backup", output)
    assert code == 1
    assert payload["data"]["integrity_verified"] is True
    assert payload["data"]["schema_compatible"] is False


def test_local_error_is_constant_and_json(capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("opaque-sensitive-test-value")

    monkeypatch.setattr(cli, "_execute", fail)
    assert cli.main(["version", "--json"]) == 2
    output = capsys.readouterr().out
    assert "opaque-sensitive-test-value" not in output
    assert json.loads(output)["checks"][0]["code"] == "local.operation_failed"


def test_missing_diagnostics_source_exports_safe_failure(tmp_path, capsys):
    private = tmp_path / "reports"
    private.mkdir(mode=0o700)
    output = private / "failure.json"
    assert cli.main(["diagnostics", "--data-dir", str(tmp_path / "missing"), "--output", str(output), "--json"]) == 2
    assert json.loads(output.read_text())["status"] == "FAIL"
    assert "providers" in json.loads(output.read_text())["data"]
    assert str(tmp_path) not in output.read_text()
    capsys.readouterr()


@pytest.mark.parametrize("origin", ["https://admin.example.invalid:bad", "https://bad host", "https://*.example.invalid", "https://user:secret@example.invalid", "http://admin.example.invalid", "https://admin.example.invalid/path"])
def test_doctor_rejects_unsafe_origins(capsys, installation, monkeypatch, origin):
    monkeypatch.setenv("BASSWIESN_ADMIN_AUTH_MODE", "required")
    monkeypatch.setenv("BASSWIESN_ADMIN_ORIGINS", origin)
    _, payload = invoke(capsys, installation, "doctor")
    assert any(check["code"] == "auth.origins" and check["status"] == "FAIL" for check in payload["checks"])


@pytest.mark.parametrize("kind", ["oversize", "encoding"])
def test_config_file_bounds(tmp_path, kind):
    path = tmp_path / "config.env"
    path.write_bytes(b"x" * (cli_checks.CONFIG_LIMIT + 1) if kind == "oversize" else b"\xff\xfe")
    with pytest.raises(OfflineError):
        load_config(tmp_path, None, path, {})


def test_archive_size_limit(installation, monkeypatch):
    path = installation[0] / "huge.zip"
    path.write_bytes(b"123")
    monkeypatch.setattr(cli_storage, "MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(OfflineError, match="exceeds"):
        verify_backup(path)


def test_backup_output_cannot_be_source(installation):
    before = installation[1].read_bytes()
    with pytest.raises(OfflineError, match="replace"):
        create_backup(installation[1], installation[1])
    assert installation[1].read_bytes() == before


def test_quick_check_bad_result_is_constant():
    class FakeDatabase:
        def execute(self, sql):
            return self

        def fetchall(self):
            return [("opaque-sensitive-test-value",)]

    with pytest.raises(OfflineError) as error:
        cli_storage.quick_check(FakeDatabase())
    assert error.value.code == "database.integrity"
    assert "opaque-sensitive-test-value" not in str(error.value)


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_artifact_parent_substitution_cannot_redirect_write_or_cleanup(tmp_path, replacement):
    container = tmp_path / "writable-ancestor"
    container.mkdir(mode=0o777)
    private = container / "private"
    private.mkdir(mode=0o700)
    relocated = container / "moved-private"
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o700)
    victim = attacker / "untouched.txt"
    victim.write_bytes(b"victim sentinel")
    output = private / "artifact.json"
    substituted = None
    with pytest.raises(OfflineError, match="directory changed"):
        with private_artifact(output) as staged:
            stage_name = next(private.glob(".basswiesnctl-*")).name
            private.rename(relocated)
            if replacement == "symlink":
                private.symlink_to(attacker, target_is_directory=True)
                substituted = attacker / stage_name
            else:
                private.mkdir(mode=0o700)
                substituted = private / stage_name
            substituted.symlink_to(victim)
            staged.write(b"private artifact bytes")
    assert victim.read_bytes() == b"victim sentinel"
    assert substituted.is_symlink()  # Cleanup did not touch the replacement tree.
    assert not (attacker / "artifact.json").exists()
    assert not (relocated / "artifact.json").exists()
    assert list(relocated.iterdir()) == []


def test_artifact_ancestor_substitution_is_detected(tmp_path):
    ancestor = tmp_path / "container"
    private = ancestor / "private"
    private.mkdir(parents=True, mode=0o700)
    moved = tmp_path / "moved"
    with pytest.raises(OfflineError, match="directory changed"):
        with private_artifact(private / "report.json") as staged:
            ancestor.rename(moved)
            private.mkdir(parents=True, mode=0o700)
            staged.write(b"retained inode only")
    assert list(private.iterdir()) == []
    assert list((moved / "private").iterdir()) == []


@pytest.mark.parametrize("replacement", ["symlink", "regular", "hardlink"])
def test_artifact_staging_substitution_never_truncates_or_unlinks_other_file(tmp_path, replacement):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim sentinel")
    output = private / "new.json"
    with pytest.raises(OfflineError, match="staging file"):
        with private_artifact(output) as staged:
            stage_path = next(private.glob(".basswiesnctl-*"))
            stage_path.unlink()
            if replacement == "symlink":
                stage_path.symlink_to(victim)
            elif replacement == "hardlink":
                os.link(victim, stage_path)
            else:
                stage_path.write_bytes(b"replacement sentinel")
            staged.write(b"must stay on the original unlinked inode")
    assert victim.read_bytes() == b"victim sentinel"
    assert stage_path.exists()  # Replaced staging entry is not ours to delete.
    if replacement == "regular":
        assert stage_path.read_bytes() == b"replacement sentinel"
    assert not output.exists()


def test_artifact_link_uses_retained_inode_even_if_stage_name_changes_at_publication(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "new.json"
    real_link = os.link
    swapped = None

    def substitute_then_link(source, target, **kwargs):
        nonlocal swapped
        stage_path = next(private.glob(".basswiesnctl-*"))
        retained_name = private / "renamed-original"
        stage_path.rename(retained_name)
        stage_path.write_bytes(b"attacker replacement")
        swapped = stage_path
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", substitute_then_link)
    with private_artifact(output) as staged:
        staged.write(b"verified original bytes")
    assert output.read_bytes() == b"verified original bytes"
    assert swapped.read_bytes() == b"attacker replacement"


def test_parent_replacement_during_publication_removes_only_own_output(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    moved = tmp_path / "moved"
    output = private / "new.json"
    real_link = os.link

    def substitute_then_link(source, target, **kwargs):
        private.rename(moved)
        private.mkdir(mode=0o700)
        output.write_bytes(b"replacement directory sentinel")
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", substitute_then_link)
    with pytest.raises(OfflineError, match="directory changed"):
        with private_artifact(output) as staged:
            staged.write(b"must not replace sentinel")
    assert output.read_bytes() == b"replacement directory sentinel"
    assert list(moved.iterdir()) == []


def test_artifact_parent_permissions_are_rechecked_on_descriptor(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "new.json"
    with pytest.raises(OfflineError, match="not private"):
        with private_artifact(output) as staged:
            staged.write(b"sensitive bytes")
            private.chmod(0o755)
    assert list(private.iterdir()) == []


def test_artifact_descriptor_link_failure_has_no_unsafe_fallback(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)

    def deny_link(*args, **kwargs):
        raise OSError("descriptor publication unsupported")

    monkeypatch.setattr(os, "link", deny_link)
    with pytest.raises(OSError):
        with private_artifact(private / "report.json") as staged:
            staged.write(b"private bytes")
    assert list(private.iterdir()) == []


@pytest.mark.parametrize("kind", ["parent_symlink", "traversal"])
def test_artifact_output_directory_walk_is_nofollow(tmp_path, kind):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    if kind == "parent_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(private, target_is_directory=True)
        output = alias / "report.json"
    else:
        output = private / ".." / "private" / "report.json"
    with pytest.raises((OSError, OfflineError)):
        with private_artifact(output):
            pytest.fail("unsafe directory was opened")
    assert list(private.iterdir()) == []


def test_archive_regular_file_swapped_to_fifo_never_blocks(tmp_path, monkeypatch):
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"initial regular file")
    real_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if path == archive.name and not flags & os.O_DIRECTORY:
            assert flags & os.O_NONBLOCK  # Fail promptly if protection regresses.
            archive.unlink()
            os.mkfifo(archive)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(OfflineError, match="changed before verification"):
        verify_backup(archive)
    assert stat.S_ISFIFO(archive.lstat().st_mode)


def test_directory_fsync_failure_cleans_only_own_artifact(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    other = private / "other.json"
    other.write_bytes(b"unrelated sentinel")
    output = private / "report.json"
    real_fsync = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError):
        with private_artifact(output) as staged:
            staged.write(b"new artifact")
    assert other.read_bytes() == b"unrelated sentinel"
    assert not output.exists()
    assert list(private.iterdir()) == [other]
