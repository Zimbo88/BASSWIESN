"""Passive local readiness and release metadata. No radio probes or certificate generation."""
import json
import os
from pathlib import Path
import re
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, RequestLog
from basswiesn.app.services.filesystem_contract import ensure_runtime_directories
from basswiesn.app.services.task_registry import owned_task_status

def filesystem_status(data_dir: Path) -> dict[str, Any]:
    """Inspect configured directories without creating, overwriting or deleting."""
    try:
        paths = ensure_runtime_directories(data_dir, create=False)
        checks = {}
        for name, value in paths.items():
            path = Path(value)
            exists = path.is_dir()
            checks[name] = {"path": value, "exists": exists, "writable": exists and os.access(path, os.W_OK),
                            "mode": oct(path.stat().st_mode & 0o777) if exists else ""}
        required = ("data", "logs", "tmp")
        ok = all(checks[name]["exists"] and checks[name]["writable"] for name in required)
        return {"ok": ok, "degraded": any(not row["exists"] for row in checks.values()),
                "directories": checks, "contract_directories": paths, "write_probe": "NOT_RUN"}
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "directories": {}, "error": type(exc).__name__, "write_probe": "NOT_RUN"}

def manifest_candidates() -> list[Path]:
    """Return source, package and container locations without relying on cwd."""
    project_root = Path(__file__).resolve().parents[3]
    candidates = [
        Path.cwd() / "manifest.json",
        project_root / "manifest.json",
        Path("/app/manifest.json"),
        Path.cwd() / "dist" / "manifest.json",
        project_root / "dist" / "manifest.json",
    ]
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique

def validate_manifest_data(data: object, *, expected_version: str) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(data, dict):
        return False, "manifest root must be an object", {}
    if data.get("format") != 1:
        return False, "manifest format must be 1", {"format": data.get("format")}
    version = str(data.get("version") or "").strip()
    if not version:
        return False, "manifest version is missing", {"format": data.get("format")}
    if expected_version and version != expected_version:
        return False, f"manifest version {version} does not match runtime {expected_version}", {"format": 1, "version": version}
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return False, "manifest files must be a non-empty list", {"format": 1, "version": version, "files": 0}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            return False, "manifest contains a file entry without path", {"format": 1, "version": version, "files": len(files)}
        manifest_path = Path(item["path"])
        if manifest_path.is_absolute() or ".." in manifest_path.parts:
            return False, f"manifest has unsafe path {item['path']}", {"format": 1, "version": version, "files": len(files)}
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("sha256") or "")):
            return False, f"manifest has invalid sha256 for {item['path']}", {"format": 1, "version": version, "files": len(files)}
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            return False, f"manifest has invalid size for {item['path']}", {"format": 1, "version": version, "files": len(files)}
    return True, "manifest schema and release metadata are valid", {"format": 1, "version": version, "files": len(files)}

def manifest_status(*, required: bool | None = None, candidates: list[Path] | None = None) -> dict[str, Any]:
    required = get_settings().release_manifest_required if required is None else required
    path = next((candidate for candidate in (manifest_candidates() if candidates is None else candidates) if candidate.is_file()), None)
    if path is None:
        return {
            "valid": not required,
            "present": False,
            "required": required,
            "runtime_optional": not required,
            "message": "release manifest missing; optional during source development" if not required else "release manifest is required but missing",
        }
    try:
        with path.open("rb") as handle:
            payload = handle.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("manifest exceeds diagnostic size limit")
        data = json.loads(payload)
    except (OSError, ValueError, RecursionError) as exc:
        return {"valid": False, "present": True, "required": required, "path": str(path), "message": f"manifest parse failed: {type(exc).__name__}"}
    valid, message, metadata = validate_manifest_data(data, expected_version=get_settings().version)
    return {"valid": valid, "present": True, "required": required, "path": str(path), "message": message, "verification_scope": "schema_only", **metadata}

def readiness(db: Session) -> dict[str, Any]:
    settings = get_settings()
    database_ok = False
    database_error = ""
    try:
        db.execute(sql_text("SELECT 1")).scalar_one()
        database_ok = True
    except Exception as exc:
        database_error = exc.__class__.__name__
    storage = filesystem_status(settings.data_dir)
    ready = database_ok and storage["ok"]
    readiness_status = "ready" if ready and not storage.get("degraded") else "degraded" if ready else "not_ready"
    return {
        "ok": ready,
        "ready": ready,
        "status": readiness_status,
        "version": settings.version,
        "update_validation_mode": settings.update_validation_mode,
        "checks": {
            "database": "ok" if database_ok else "failed",
            "storage": storage,
            "background_tasks": owned_task_status(),
        },
        "error": database_error,
    }

def release_healthcheck(db: Session) -> dict[str, Any]:
    settings = get_settings()
    checks = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    add("api_alive", "green", "Web API antwortet")
    try:
        db.execute(sql_text("select 1")).scalar()
        add("database_alive", "green", "SQLite erreichbar")
    except Exception as exc:
        add("database_alive", "red", type(exc).__name__)
    storage = filesystem_status(settings.data_dir)
    add("writable_storage", "green" if storage["ok"] else "yellow",
        "Directory permissions inspected; no write probe performed")
    manifest = manifest_status()
    manifest_state = "green" if manifest.get("valid") else "red" if manifest.get("required") or manifest.get("present") else "yellow"
    add("manifest_valid", manifest_state, json.dumps(manifest, ensure_ascii=False, default=str))
    device_count = db.query(Device).count()
    cached = db.query(Device).filter(Device.reachable.is_(True)).count()
    add("devices_reachable", "yellow" if device_count else "green",
        f"NOT_PROBED: {cached}/{device_count} cached reachable flags; no fresh device verification")
    recent_errors = db.query(RequestLog).filter(RequestLog.status_code >= 500).count()
    add("emulator_healthy", "green" if recent_errors == 0 else "yellow", f"{recent_errors} cloud/debug 5xx request(s) recorded")
    add("websocket_polling", "green", "UI uses HTTP polling; setup jobs persist latest status")
    if settings.enable_https:
        cert = Path(settings.tls_cert_file) if settings.cert_mode == "external" else settings.data_dir / "tls/basswiesn.crt"
        key = Path(settings.tls_key_file) if settings.cert_mode == "external" else settings.data_dir / "tls/basswiesn.key"
        present = cert.is_file() and key.is_file()
        add("https_status", "yellow", "Certificate/key files present; TLS not probed" if present else "Configured certificate/key missing")
    else:
        add("https_status", "green", "optional HTTPS disabled")
    for name, port in {"web": settings.web_port, "cloud": settings.cloud_port, "debug": settings.debug_port}.items():
        add(f"port_{name}", "green" if int(port) > 0 else "red", f"configured port {port}")
    docker_hint = Path("/.dockerenv").exists()
    add("docker_running", "green" if docker_hint else "yellow", "container marker present" if docker_hint else "not running inside Docker or marker unavailable")
    if any(check["status"] == "red" for check in checks):
        status = "red"
    elif any(check["status"] == "yellow" for check in checks):
        status = "yellow"
    else:
        status = "green"
    return {"status": status, "summary": f"{sum(1 for item in checks if item['status'] == 'green')}/{len(checks)} checks green", "checks": checks}
