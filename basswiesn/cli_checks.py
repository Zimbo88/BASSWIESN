"""Practical offline preflight diagnostics, with no fix/start/probe operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import ipaddress
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
from urllib.parse import urlsplit

from basswiesn.cli_contract import Check, JsonValue, OfflineError, Report
from basswiesn.cli_schema import declared_baseline, declared_schema, immutable_policy
from basswiesn.cli_storage import checked_path, offline_database, quick_check, require_core, table_columns

CONFIG_LIMIT = 64 * 1024
SECRET_FILES = (
    "BASSWIESN_TELNET_PASSWORD_FILE", "BASSWIESN_SSH_PASSWORD_FILE",
    "BASSWIESN_SSH_PRIVATE_KEY_FILE", "BASSWIESN_MARGE_AUTH_TOKEN_FILE", "BASSWIESN_TLS_KEY_FILE",
)
BOOL_KEYS = (
    "BASSWIESN_ENABLE_HTTPS", "BASSWIESN_ADMIN_ALLOW_HTTP", "BASSWIESN_SSDP_ENABLED",
    "BASSWIESN_IP_SCAN_FALLBACK", "BASSWIESN_PLAYBACK_KEEPALIVE_ENABLED",
    "BASSWIESN_MAINTENANCE_REBOOT_SCHEDULER_ENABLED", "BASSWIESN_TELNET_ENABLED",
)
CONFIG_KEYS = frozenset((*SECRET_FILES, *BOOL_KEYS,
    "BASSWIESN_ADMIN_AUTH_MODE", "BASSWIESN_ADMIN_ORIGINS", "BASSWIESN_HTTPS_PORT",
    "BASSWIESN_OFFLINE_MODE", "BASSWIESN_SSH_HOST_KEY_POLICY", "BASSWIESN_TLS_CERT_FILE",
    "BASSWIESN_PROTECTED_DEVICE_IPS", "BASSWIESN_PROTECTED_DEVICE_IDS",
    "PROTECTED_DEVICE_IPS", "PROTECTED_DEVICE_IDS",
))


@dataclass(frozen=True)
class OfflineConfig:
    data_dir: Path
    database: Path
    values: dict[str, str] = field(default_factory=dict, repr=False)
    config_file: Path | None = None

    def text(self, key: str, default: str = "") -> str:
        return self.values.get(key, default).strip()

    def enabled(self, key: str, default: bool = False) -> bool:
        return self.text(key, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def load_config(data_dir: Path, database: Path | None, config_file: Path | None, environ: Mapping[str, str]) -> OfflineConfig:
    """Parse an explicit simple dotenv file as data; never shell source/interpolate."""
    values: dict[str, str] = {}
    if config_file is not None:
        info = checked_path(config_file)
        if info.st_size > CONFIG_LIMIT:
            raise OfflineError("config.too_large", "The configuration file exceeds 64 KiB.", "Select the intended small dotenv file.")
        try:
            source = config_file.read_text(encoding="utf-8")
        except UnicodeError:
            raise OfflineError("config.invalid", "The configuration is not valid UTF-8.", "Use a UTF-8 dotenv file; no shell scripts are accepted.") from None
        for line in source.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            if match is None:
                raise OfflineError("config.invalid", "The configuration contains unsupported dotenv syntax.", "Use one literal KEY=value per line; no multiline values or shell expressions.")
            key, value = match.groups()
            if key not in CONFIG_KEYS:
                continue
            if value.startswith(("'", '"')):
                quote = value[0]
                if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                    raise OfflineError("config.invalid", "The configuration contains unsupported quoting.", "Use simple literal quoted or unquoted values.")
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            if "$" in value or "`" in value or "\\" in value:
                raise OfflineError("config.interpolation", "A relevant configuration value requires interpolation or escapes.", "Pass resolved literal values through the environment; the CLI never evaluates shell expressions.")
            values[key] = value
    values.update({key: value for key, value in environ.items() if key in CONFIG_KEYS})
    return OfflineConfig(data_dir.absolute(), (database or data_dir / "basswiesn.db").absolute(), values, config_file)


def _normal_ip(value: str) -> str:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def protected_policy(config: OfflineConfig, db: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """Union relevant env/config and persisted policy, without returning raw values."""
    immutable_ips, immutable_ids = immutable_policy()
    ips: set[str] = set()
    identifiers: set[str] = set()
    stored = {str(row[0]): str(row[1] or "") for row in db.execute("SELECT key, value FROM settings WHERE key IN ('protected_device_ips', 'protected_device_ids')")}
    # Runtime environment aliases are comma-separated; persisted policy also
    # supports whitespace/semicolons. Do not claim protections runtime ignores.
    ip_values = (config.text("PROTECTED_DEVICE_IPS") or config.text("BASSWIESN_PROTECTED_DEVICE_IPS")).split(",") + re.split(r"[\s,;]+", stored.get("protected_device_ips", "")) + list(immutable_ips)
    id_values = (config.text("PROTECTED_DEVICE_IDS") or config.text("BASSWIESN_PROTECTED_DEVICE_IDS")).split(",") + re.split(r"[\s,;]+", stored.get("protected_device_ids", "")) + list(immutable_ids)
    try:
        for text in ip_values:
            if item := text.strip():
                ips.add(_normal_ip(item))
        for text in id_values:
            if item := text.strip():
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item):
                    raise ValueError("invalid identifier")
                identifiers.add(item.upper())
    except ValueError:
        raise OfflineError("policy.invalid", "A protected-device policy entry is malformed.", "Correct the policy locally; no target is considered authorized and no device is contacted.") from None
    return ips, identifiers


def timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat()


def _database_checks(config: OfflineConfig, report: Report) -> None:
    try:
        with offline_database(config.database) as db:
            quick_check(db)
            existing = table_columns(db)
            expected = declared_schema()
            missing_tables = sorted(set(expected) - set(existing))
            missing_columns = [f"{table}.{column}" for table in expected if table in existing for column in sorted(expected[table] - existing[table])]
            # Only trusted source-defined names enter this diagnostic.
            schema_data: dict[str, JsonValue] = {
                "baseline": declared_baseline(),
                "expected_tables": len(expected),
                "existing_tables": len(existing),
                "missing_tables": list(missing_tables),
                "missing_columns": list(missing_columns),
                "scope": "declared_table_and_column_presence",
                "indexes_and_migration_semantics": "NOT_VERIFIED",
            }
            ips, identifiers = protected_policy(config, db) if {"key", "value"}.issubset(existing.get("settings", set())) else (set(), set())
            runtime: dict[str, JsonValue] = {"live_health": "NOT_PROBED", "port_availability": "NOT_PROBED"}
            if {"key", "value"}.issubset(existing.get("runtime_state", set())):
                for row in db.execute("SELECT key, value FROM runtime_state WHERE key IN ('server:last_boot', 'server:last_shutdown', 'server:restart_count')"):
                    key, value = str(row[0]), row[1]
                    if key == "server:restart_count":
                        runtime["recorded_restart_count"] = int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value) else None
                    else:
                        runtime[key.removeprefix("server:")] = timestamp(value)
        report.data["schema"] = schema_data
        report.data["runtime"] = runtime
        report.data["protected_policy"] = {"ip_entries": len(ips), "identifier_entries": len(identifiers), "source": "immutable_constants_local_configuration_and_snapshot", "configuration_precedence": "PROTECTED_* overrides BASSWIESN_PROTECTED_*; persisted entries are unioned", "authorization": "NO_GRANT"}
        report.checks.append(Check("database.integrity", "PASS", "The immutable SQLite snapshot passed quick_check."))
        report.checks.append(Check("database.schema", "FAIL" if missing_tables or missing_columns else "PASS", "Declared application table/column presence was checked.", "Use the documented backup/upgrade workflow for missing schema; the CLI never migrates." if missing_tables or missing_columns else ""))
        report.checks.append(Check("policy.protected", "PASS" if ips or identifiers else "WARN", "Protected-device policy was parsed without contacting targets.", "Configure protection for any excluded devices before authorizing runtime operations." if not ips and not identifiers else ""))
    except (OfflineError, OSError, SyntaxError, UnicodeError) as exc:
        if isinstance(exc, OfflineError):
            report.failure(exc)
        else:
            report.failure(OfflineError("schema.contract", "The installed schema source is unavailable or invalid.", "Use the complete matching source distribution."))


def _private_check(path: Path, code: str, *, directory: bool = False) -> Check:
    try:
        info = checked_path(path, directory=directory)
        safe = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
        return Check(code, "PASS" if safe else "FAIL", "Private path ownership and mode checked without reading contents.", "Use the service account and 0700 directories/0600 secret files; the CLI never changes permissions." if not safe else "")
    except OfflineError:
        return Check(code, "FAIL", "A private path is missing, linked or inaccessible.", "Review the corresponding local secret/configuration path and service-account access.")


def _secrets_checks(config: OfflineConfig, report: Report) -> None:
    root = config.data_dir / "secrets"
    if root.exists() or root.is_symlink():
        report.checks.append(_private_check(root, "secrets.directory", directory=True))
        # No recursive directory walk: known authentication paths and explicit
        # configured files are sufficient without collecting secret inventories.
    for index, key in enumerate(SECRET_FILES):
        if config.text(key):
            report.checks.append(_private_check(Path(config.text(key)), f"secrets.configured.{index}"))
    if config.config_file is not None:
        report.checks.append(_private_check(config.config_file, "config.permissions"))
    mode = config.text("BASSWIESN_ADMIN_AUTH_MODE", "legacy")
    existing_auth = root / "admin" / "auth.sqlite3"
    if mode != "required" and (existing_auth.exists() or existing_auth.is_symlink()):
        report.checks.extend((_private_check(existing_auth.parent, "auth.directory", directory=True), _private_check(existing_auth, "auth.file")))
    if mode == "legacy":
        report.checks.append(Check("auth.mode", "WARN", "Legacy mode does not authenticate administrator requests.", "Review docs/ADMIN_AUTH.md before explicitly enabling the required-mode preview."))
    elif mode != "required":
        report.checks.append(Check("auth.mode", "FAIL", "The authentication mode is unsupported.", "Set BASSWIESN_ADMIN_AUTH_MODE to legacy or required."))
    else:
        report.checks.append(Check("auth.mode", "PASS", "Required-mode authentication is configured (preview, not deployment certification)."))
        tls = config.enabled("BASSWIESN_ENABLE_HTTPS") and not config.enabled("BASSWIESN_ADMIN_ALLOW_HTTP")
        report.checks.append(Check("auth.transport", "PASS" if tls else "FAIL", "Required-mode HTTPS/no-HTTP policy checked.", "Enable HTTPS and disable ADMIN_ALLOW_HTTP before required-mode deployment." if not tls else ""))
        origins = [item.strip() for item in config.text("BASSWIESN_ADMIN_ORIGINS").split(",") if item.strip()]
        valid = bool(origins)
        try:
            for origin in origins:
                parsed = urlsplit(origin)
                valid = valid and parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                valid = valid and not any(char.isspace() for char in parsed.hostname or "") and not any(char in (parsed.hostname or "") for char in ("\\", "*"))
                _ = parsed.port
        except ValueError:
            valid = False
        report.checks.append(Check("auth.origins", "PASS" if valid else "FAIL", "Explicit HTTPS administrator origin metadata checked.", "Configure exact HTTPS origins without paths, userinfo, query or fragments." if not valid else ""))
        auth = root / "admin" / "auth.sqlite3"
        parent_check = _private_check(auth.parent, "auth.directory", directory=True)
        file_check = _private_check(auth, "auth.file")
        report.checks.extend((parent_check, file_check))
        if parent_check.status == file_check.status == "PASS":
            try:
                with offline_database(auth) as db:
                    quick_check(db)
                    columns = table_columns(db)
                    required = {"administrator": {"id", "username", "password_hash"}, "sessions": {"digest", "created", "seen", "expires"}, "attempts": {"bucket", "start", "count"}, "audit": {"id", "created", "code"}}
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version == 1:
                        required["api_tokens"] = {"id", "digest", "name", "scopes", "created", "expires", "last_used", "revoked"}
                    valid_auth = version in {0, 1} and columns == required
                    populated = valid_auth and db.execute("SELECT COUNT(*) FROM administrator WHERE id=1").fetchone()[0] == 1
                report.checks.append(Check("auth.schema", "PASS" if populated else "FAIL", "Authentication schema and bootstrap presence checked; no credential values read.", "Use documented local administrator bootstrap/recovery; this command never creates or migrates accounts." if not populated else ""))
            except OfflineError as exc:
                report.failure(exc)


def doctor(config: OfflineConfig, *, command: str = "doctor") -> Report:
    report = Report(command)
    report.data["evidence"] = "offline_snapshot_only"
    report.data["no_actions"] = ["network", "discovery", "startup", "migration", "repair", "restore", "device_write"]
    for suffix in ("IPS", "IDS"):
        primary = config.text(f"PROTECTED_DEVICE_{suffix}")
        alias = config.text(f"BASSWIESN_PROTECTED_DEVICE_{suffix}")
        if primary and alias and primary != alias:
            report.checks.append(Check("policy.alias_shadowed", "WARN", "A prefixed protected-device alias is shadowed by the authoritative PROTECTED_DEVICE setting.", "Consolidate intended entries in the authoritative setting; runtime precedence is preserved, not silently unioned by the CLI."))
    report.checks.append(Check("runtime.python", "PASS" if sys.version_info >= (3, 11) else "FAIL", "Python 3.11 or later is required.", "Use the documented locked Python environment." if sys.version_info < (3, 11) else ""))
    for key in BOOL_KEYS:
        if key in config.values and config.text(key).lower() not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
            report.checks.append(Check("config.boolean", "FAIL", "A relevant boolean configuration value is invalid.", "Use true/false, yes/no, on/off or 1/0; raw values are not printed."))
    if config.text("BASSWIESN_OFFLINE_MODE", "auto") not in {"off", "auto", "strict"}:
        report.checks.append(Check("config.offline_mode", "FAIL", "The runtime offline mode is invalid.", "Use off, auto or strict."))
    if config.text("BASSWIESN_SSH_HOST_KEY_POLICY", "strict") != "strict":
        report.checks.append(Check("config.host_keys", "WARN", "SSH host-key policy is not strict.", "Use strict host-key verification for any separately authorized setup operation."))
    try:
        info = checked_path(config.data_dir, directory=True)
        private = not stat.S_IMODE(info.st_mode) & 0o077
        report.checks.append(Check("storage.directory", "PASS" if private else "WARN", "Data directory metadata checked.", "Restrict sensitive data to its service account before deployment." if not private else ""))
        usage = shutil.disk_usage(config.data_dir)
        reserve = max(256 * 1024 * 1024, checked_path(config.database).st_size * 2)
        report.data["disk"] = {"free_bytes": usage.free, "recommended_free_bytes": reserve}
        report.checks.append(Check("storage.capacity", "PASS" if usage.free >= reserve else "WARN", "Available disk space was compared with a conservative backup reserve.", "Free space before backup/upgrade; this is a preflight estimate, not a write test." if usage.free < reserve else ""))
    except OfflineError as exc:
        report.failure(exc)
    except OSError:
        report.failure(OfflineError("storage.unavailable", "Disk capacity could not be inspected.", "Check local data-directory access and mount health."))
    try:
        info = checked_path(config.database)
        safe = not stat.S_IMODE(info.st_mode) & 0o077
        report.checks.append(Check("database.permissions", "PASS" if safe else "WARN", "Application database permissions checked.", "Restrict access: the application database can contain sensitive settings and device configuration." if not safe else ""))
    except OfflineError as exc:
        report.failure(exc)
    _database_checks(config, report)
    _secrets_checks(config, report)
    ports: dict[str, JsonValue] = {"web": 1328, "cloud": 1516, "debug": 1860, "radio_target": 8090}
    try:
        https_port = int(config.text("BASSWIESN_HTTPS_PORT", "1329"))
        if not 1 <= https_port <= 65535:
            raise ValueError("port")
        ports["https"] = https_port
        collision = config.enabled("BASSWIESN_ENABLE_HTTPS") and https_port in {1328, 1516, 1860}
        report.checks.append(Check("runtime.ports", "FAIL" if collision else "PASS", "Configured listener numbers checked; sockets were not opened.", "Choose a distinct HTTPS listener port." if collision else ""))
    except ValueError:
        report.checks.append(Check("runtime.ports", "FAIL", "The HTTPS port is not in the range 1..65535.", "Configure a valid distinct HTTPS listener port."))
    report.data["configured_ports"] = ports
    report.data["runtime_verification"] = "NOT_PROBED; no bind, DNS, socket, subprocess or HTTP checks"
    report.checks.append(Check("runtime.offline_evidence", "WARN", "Offline preflight cannot certify running service readiness or device reachability.", "For live acceptance, use separately authorized deployment checks; this CLI has no network mode."))
    return report
