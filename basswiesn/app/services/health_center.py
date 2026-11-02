from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import socket
import time
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, HealthcheckResult, HealthcheckRun, RuntimeState
from basswiesn.app.services.device_interactions import InteractionPriority, coordinator
from basswiesn.app.services.device_policy import policy_for_device
from basswiesn.app.services.model_library import resolve_device_model
from basswiesn.app.services.network_security import validate_outbound_host
from basswiesn.app.services.offline_mode import offline_status
from basswiesn.app.services.standby_clock_recovery import standby_clock_status
from basswiesn.app.services.telnet_device_control import telnet_capabilities
from basswiesn.app.services.protected_devices import is_protected_ip, reject_protected_device_access


STATUSES = {"healthy", "warning", "failed", "skipped", "unsupported"}


def _result(
    *,
    run_id: str,
    check_id: str,
    status: str,
    description: str,
    category: str = "system",
    device_id: str = "",
    details: dict | None = None,
    cause: str = "",
    recommendation: str = "",
    started_at: datetime | None = None,
    duration_ms: int = 0,
) -> dict:
    status = status if status in STATUSES else "warning"
    return {
        "run_id": run_id,
        "check_id": check_id,
        "category": category,
        "device_id": device_id,
        "status": status,
        "description": description,
        "details": details or {},
        "cause": cause,
        "recommendation": recommendation,
        "started_at": (started_at or datetime.now(UTC)).isoformat(),
        "duration_ms": duration_ms,
    }


def _persist_result(db: Session, item: dict) -> None:
    db.add(HealthcheckResult(
        run_id=item["run_id"],
        device_id=item.get("device_id", ""),
        category=item.get("category", "system"),
        check_id=item["check_id"],
        status=item["status"],
        description=item["description"],
        details_json=json.dumps(item.get("details", {}), ensure_ascii=False),
        cause=item.get("cause", ""),
        recommendation=item.get("recommendation", ""),
        started_at=datetime.fromisoformat(item["started_at"]),
        duration_ms=int(item.get("duration_ms", 0)),
    ))


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".basswiesn-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    if is_protected_ip(host):
        return False
    validation = validate_outbound_host(host, port=port)
    if not validation.ok:
        return False
    target = validation.addresses[0]
    try:
        reject_protected_device_access(target, action="health port check", requester="health_center", method="TCP", endpoint=str(port))
    except Exception:
        return False
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def _runtime_state(db: Session, key: str) -> str:
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    return row.value if row else ""


async def run_healthcheck(db: Session, *, include_device_http: bool = False) -> dict:
    run_id = str(uuid4())
    started_at = datetime.now(UTC)
    started = time.monotonic()
    run = HealthcheckRun(run_id=run_id, started_at=started_at, status="running")
    db.add(run)
    db.flush()
    results: list[dict] = []
    settings = get_settings()
    data_dir = settings.data_dir.resolve()
    backup_dir = data_dir / "backups"
    log_dir = data_dir / "logs"

    try:
        quick_check = str(db.execute(text("PRAGMA quick_check")).scalar() or "unknown")
        results.append(_result(run_id=run_id, check_id="database_quick_check", status="healthy" if quick_check == "ok" else "failed", description="SQLite quick_check", details={"quick_check": quick_check}))
    except Exception as exc:
        results.append(_result(run_id=run_id, check_id="database_quick_check", status="failed", description="SQLite quick_check fehlgeschlagen", cause=str(exc), recommendation="Datenbankbackup prüfen und Restore vorbereiten."))

    for check_id, path in (("data_dir_writable", data_dir), ("backup_dir_writable", backup_dir), ("log_dir_writable", log_dir)):
        ok, error = _writable(path)
        results.append(_result(run_id=run_id, check_id=check_id, status="healthy" if ok else "failed", description=f"{path} schreibbar", details={"path": str(path)}, cause=error))

    usage = shutil.disk_usage(data_dir)
    free_mb = usage.free // (1024 * 1024)
    results.append(_result(run_id=run_id, check_id="free_disk_space", status="healthy" if free_mb >= 512 else "warning", description="Freier Speicherplatz", details={"free_mb": free_mb, "total_mb": usage.total // (1024 * 1024)}, recommendation="Mindestens 512 MB frei halten."))

    results.append(_result(run_id=run_id, check_id="version", status="healthy", description="BASSWIESN-Version", details={"version": settings.version}))
    results.append(_result(run_id=run_id, check_id="offline_mode", status="healthy", description="Offline Mode", details=offline_status(db)))
    results.append(_result(run_id=run_id, check_id="last_migration", status="healthy", description="Letzte Migration", details={"last_migration": _runtime_state(db, "schema:last_migration")}))
    results.append(_result(run_id=run_id, check_id="last_backup", status="healthy", description="Letztes Backup", details={"last_backup": _runtime_state(db, "backup:last_success")}))

    devices = db.query(Device).order_by(Device.name, Device.device_id).all()
    for device in devices:
        policy = policy_for_device(device, db)
        model = resolve_device_model(device, db)
        results.append(_result(
            run_id=run_id,
            category="device",
            device_id=device.device_id,
            check_id="device_record",
            status="healthy",
            description="Gespeichertes Gerät vorhanden",
            details={"name": device.name, "ip": device.ip_address, "model": device.model, "model_resolution": model.to_dict(), "policy": policy.to_dict()},
        ))
        results.append(_result(
            run_id=run_id,
            category="device",
            device_id=device.device_id,
            check_id="circuit_breaker",
            status="warning" if policy.circuit_state.value == "open" else "healthy",
            description="Circuit-Breaker-Zustand",
            details=policy.to_dict(),
            recommendation="Offene Circuit Breaker nicht durch aggressive Parallelabfragen schließen.",
        ))
        telnet = telnet_capabilities(db, device)
        results.append(_result(
            run_id=run_id,
            category="device",
            device_id=device.device_id,
            check_id="telnet_reboot_capability",
            status="healthy" if telnet.get("supported") else "skipped",
            description="Telnet-Reboot-Fähigkeit",
            details={key: value for key, value in telnet.items() if key not in {"warnings"}},
            recommendation="Telnet ist standardmäßig deaktiviert und wird nie automatisch ausgeführt.",
        ))
        clock = standby_clock_status(db, device)
        results.append(_result(
            run_id=run_id,
            category="device",
            device_id=device.device_id,
            check_id="standby_clock_recovery_capability",
            status="healthy" if clock.get("supported") else "skipped",
            description="Standby-Uhr-Recovery-Fähigkeit",
            details=clock,
            recommendation="Standby-Uhr-Recovery ist manuell und benötigt Read-back oder Sichtprüfung.",
        ))
        if not device.ip_address:
            results.append(_result(run_id=run_id, category="device", device_id=device.device_id, check_id="device_ip_known", status="failed", description="Keine IP-Adresse gespeichert", recommendation="Discovery oder manuelle IP-Aktualisierung ausführen."))
            continue
        port_ok = _port_open(device.ip_address, settings.radio_port)
        results.append(_result(run_id=run_id, category="device", device_id=device.device_id, check_id="port_8090", status="healthy" if port_ok else "warning", description="Port 8090 erreichbar", details={"ip": device.ip_address, "port": settings.radio_port}, recommendation="Backoff und Circuit Breaker respektieren."))
        if include_device_http and port_ok:
            for endpoint in ("/info", "/now_playing", "/volume", "/sources", "/presets", "/serviceAvailability"):
                check_started = time.monotonic()
                interaction = await coordinator.request_xml(
                    db,
                    device,
                    endpoint,
                    request_purpose="healthcheck",
                    requester="health_center",
                    priority=InteractionPriority.HEALTHCHECK,
                    timeout_seconds=3,
                    retry_budget=0,
                    cache_ttl_seconds=5,
                    allow_safe_mode_skip=True,
                )
                results.append(_result(
                    run_id=run_id,
                    category="device",
                    device_id=device.device_id,
                    check_id=f"http_{endpoint.strip('/') or 'root'}",
                    status="healthy" if interaction.ok else "warning",
                    description=f"{endpoint} lesbar",
                    details={key: value for key, value in interaction.to_dict().items() if key != "payload"},
                    duration_ms=int((time.monotonic() - check_started) * 1000),
                    cause=interaction.error,
                ))

    for item in results:
        _persist_result(db, item)
    counts = {status: sum(1 for item in results if item["status"] == status) for status in STATUSES}
    overall = "failed" if counts.get("failed") else "warning" if counts.get("warning") else "healthy"
    duration_ms = int((time.monotonic() - started) * 1000)
    run.ended_at = datetime.now(UTC)
    run.status = overall
    run.duration_ms = duration_ms
    run.summary_json = json.dumps({"counts": counts, "include_device_http": include_device_http}, ensure_ascii=False)
    return {"run_id": run_id, "status": overall, "duration_ms": duration_ms, "counts": counts, "results": results}


def latest_healthchecks(db: Session, *, limit: int = 10) -> list[dict]:
    rows = db.query(HealthcheckRun).order_by(HealthcheckRun.started_at.desc()).limit(min(max(limit, 1), 50)).all()
    result = []
    for row in rows:
        try:
            summary = json.loads(row.summary_json or "{}")
        except ValueError:
            summary = {}
        result.append({
            "run_id": row.run_id,
            "started_at": row.started_at.isoformat() if row.started_at else "",
            "ended_at": row.ended_at.isoformat() if row.ended_at else "",
            "status": row.status,
            "duration_ms": row.duration_ms,
            "summary": summary,
        })
    return result
