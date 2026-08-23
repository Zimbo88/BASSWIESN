from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from basswiesn.app.models import DescriptorCache, Device, QuickFixRun, RuntimeState
from basswiesn.app.services.device_state import load_runtime_state, save_runtime_state
from basswiesn.app.services.events import create_event


CONFIRMATION_PHRASE = "BASSWIESN QUICK FIX"


@dataclass(frozen=True)
class QuickFixDefinition:
    quick_fix_id: str
    name: str
    description: str
    risk: str
    device_required: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.quick_fix_id,
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "device_required": self.device_required,
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "automatic": False,
        }


QUICK_FIXES = {
    item.quick_fix_id: item
    for item in (
        QuickFixDefinition("device_recheck", "Gerät erneut prüfen", "Plant eine erneute sichere Prüfung über Healthcheck/Discovery.", "low", True),
        QuickFixDefinition("rediscover_device", "Gerät erneut entdecken", "SSDP/IP-Discovery erneut ausführen, ohne alte Geräte zu löschen.", "low", True),
        QuickFixDefinition("update_saved_ip", "Gespeicherte IP aktualisieren", "Aktualisiert nur die gespeicherte IP aus einem validierten Discovery-Ergebnis.", "medium", True),
        QuickFixDefinition("clear_descriptor_cache", "Descriptor-Cache löschen", "Löscht lokale Descriptor-Cacheeinträge.", "low", False),
        QuickFixDefinition("reset_circuit_breaker", "Circuit Breaker zurücksetzen", "Setzt lokale Fehlerzähler für ein Gerät zurück; weckt das Gerät nicht.", "medium", True),
        QuickFixDefinition("reset_polling_state", "Pollingzustand zurücksetzen", "Entfernt nur lokale Keepalive-/Polling-Fehlerzustände.", "low", True),
        QuickFixDefinition("reread_presets", "Presets neu lesen", "Plant einen read-only Preset-Read-back über die zentrale Koordination.", "low", True),
        QuickFixDefinition("reread_sources", "Quellen neu lesen", "Plant einen read-only Sources-Read-back über die zentrale Koordination.", "low", True),
        QuickFixDefinition("standby_clock_recovery", "Standby-Uhr neu aktivieren", "Verweist auf den eigenen bestätigten Standby-Clock-Recovery-Ablauf; kein automatischer Fix.", "medium", True),
        QuickFixDefinition("telnet_reboot", "Gerät per Telnet neu starten", "Verweist auf den eigenen bestätigten Telnet-Reboot-Job; kein Direktaufruf.", "high", True),
        QuickFixDefinition("stream_compatibility_recheck", "Stream-Kompatibilität neu prüfen", "Markiert lokale Stream-Kompatibilitätsdaten zur erneuten Prüfung.", "low", False),
        QuickFixDefinition("clear_local_cache", "Lokale Cacheeinträge löschen", "Löscht nur basswiesn Runtime-/Descriptor-Caches.", "low", False),
        QuickFixDefinition("database_quick_check", "Datenbank quick_check ausführen", "Führt PRAGMA quick_check aus; keine Änderung an Daten.", "low", False),
        QuickFixDefinition("reinitialize_background_services", "Hintergrunddienste neu initialisieren", "Setzt lokale Runtime-Marker; der Prozess wird nicht neu gestartet.", "medium", False),
        QuickFixDefinition("prepare_container_restart", "Container-Neustart vorbereiten", "Erstellt nur einen lokalen Hinweis/Plan; kein Neustart wird ausgeführt.", "medium", False),
    )
}


def list_quick_fixes() -> list[dict]:
    return [item.to_dict() for item in QUICK_FIXES.values()]


def preview_quick_fix(db: Session, quick_fix_id: str, *, device_id: str = "", parameters: dict | None = None) -> dict:
    definition = QUICK_FIXES.get(quick_fix_id)
    if definition is None:
        raise ValueError("unknown quick fix")
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none() if device_id else None
    if definition.device_required and device is None:
        raise ValueError("quick fix requires a known device")
    preview = {
        "quick_fix": definition.to_dict(),
        "device": {"device_id": device.device_id, "name": device.name, "ip": device.ip_address} if device else None,
        "parameters": parameters or {},
        "will_write_device": False,
        "will_use_ssh": False,
        "will_power_device": False,
        "readback_required": quick_fix_id in {"reread_presets", "reread_sources", "database_quick_check"},
        "rollback_possible": quick_fix_id in {"reset_circuit_breaker", "reset_polling_state"},
        "requires_separate_confirmed_flow": quick_fix_id in {"standby_clock_recovery", "telnet_reboot"},
    }
    if quick_fix_id == "standby_clock_recovery":
        preview["will_write_device"] = True
        preview["readback_required"] = True
        preview["note"] = "Ausfuehrung nur ueber /api/devices/{device_id}/standby-clock/restore mit eigener Bestaetigung."
    if quick_fix_id == "telnet_reboot":
        preview["will_write_device"] = True
        preview["note"] = "Ausfuehrung nur ueber /api/devices/{device_id}/telnet/reboot mit eigener Bestaetigung; kein allgemeines Telnet."
    return preview


def _clear_runtime_like(db: Session, prefix: str = "") -> int:
    query = db.query(RuntimeState)
    if prefix:
        query = query.filter(RuntimeState.key.like(f"{prefix}%"))
    deleted = query.filter(RuntimeState.key.like("%cache%")).delete(synchronize_session=False)
    return int(deleted or 0)


def _set_runtime(db: Session, key: str, value: str) -> None:
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        row = RuntimeState(key=key)
        db.add(row)
    row.value = value
    row.updated_at = datetime.now(UTC)


def execute_quick_fix(db: Session, quick_fix_id: str, *, confirmation: str, device_id: str = "", parameters: dict | None = None) -> dict:
    if confirmation != CONFIRMATION_PHRASE:
        raise PermissionError("confirmation phrase required")
    preview = preview_quick_fix(db, quick_fix_id, device_id=device_id, parameters=parameters)
    run_id = str(uuid4())
    run = QuickFixRun(
        run_id=run_id,
        quick_fix_id=quick_fix_id,
        device_id=device_id,
        status="running",
        confirmation="accepted",
        preview_json=json.dumps(preview, ensure_ascii=False),
    )
    db.add(run)
    result: dict = {"ok": True, "changed": False, "quick_fix_id": quick_fix_id}
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none() if device_id else None
    try:
        if quick_fix_id == "reset_circuit_breaker" and device is not None:
            before = {"failure_count": device.failure_count, "reachable": device.reachable, "offline_reason": device.offline_reason}
            device.failure_count = 0
            device.reachable = True
            device.offline_reason = ""
            _row, state = load_runtime_state(db, device.device_id)
            state.pop("playback_keepalive", None)
            save_runtime_state(db, device.device_id, state, commit=False)
            result.update({"changed": True, "before": before, "after": {"failure_count": 0, "reachable": True}})
            create_event(db, "circuit_breaker_closed", device_id=device.device_id, payload={"trigger": "quick_fix"})
        elif quick_fix_id == "reset_polling_state" and device is not None:
            _row, state = load_runtime_state(db, device.device_id)
            before = dict(state)
            for key in ("playback_keepalive", "polling", "last_probe_at"):
                state.pop(key, None)
            save_runtime_state(db, device.device_id, state, commit=False)
            result.update({"changed": before != state, "before_keys": sorted(before), "after_keys": sorted(state)})
        elif quick_fix_id == "clear_descriptor_cache":
            deleted = db.query(DescriptorCache).delete(synchronize_session=False)
            result.update({"changed": bool(deleted), "deleted": int(deleted or 0)})
        elif quick_fix_id == "clear_local_cache":
            descriptors = db.query(DescriptorCache).delete(synchronize_session=False)
            runtime = _clear_runtime_like(db)
            result.update({"changed": bool(descriptors or runtime), "deleted_descriptor_cache": int(descriptors or 0), "deleted_runtime_cache": runtime})
        elif quick_fix_id == "database_quick_check":
            result.update({"changed": False, "quick_check": str(db.execute(text("PRAGMA quick_check")).scalar() or "unknown")})
        elif quick_fix_id in {"reread_presets", "reread_sources", "device_recheck", "rediscover_device", "stream_compatibility_recheck", "standby_clock_recovery", "telnet_reboot"}:
            result.update({"changed": False, "planned": True, "next_step": "Manueller Read-back/Healthcheck über den passenden Endpoint starten."})
        elif quick_fix_id == "reinitialize_background_services":
            _set_runtime(db, "services:reinitialize_requested", datetime.now(UTC).isoformat())
            result.update({"changed": True, "note": "Lokaler Reinitialisierungsmarker gesetzt; kein Prozessneustart."})
        elif quick_fix_id == "prepare_container_restart":
            _set_runtime(db, "container:restart_plan", json.dumps({"created_at": datetime.now(UTC).isoformat(), "manual_only": True}))
            result.update({"changed": True, "manual_only": True})
        elif quick_fix_id == "update_saved_ip" and device is not None:
            new_ip = str((parameters or {}).get("ip_address") or "").strip()
            if not new_ip:
                raise ValueError("ip_address parameter required")
            before = device.ip_address
            device.ip_address = new_ip
            result.update({"changed": before != new_ip, "before": before, "after": new_ip})
        else:
            result.update({"changed": False, "unsupported": True})
        run.status = "succeeded"
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "quick_fix_id": quick_fix_id}
        run.status = "failed"
    run.result_json = json.dumps(result, ensure_ascii=False)
    run.finished_at = datetime.now(UTC)
    return {"run_id": run_id, "preview": preview, "result": result}
