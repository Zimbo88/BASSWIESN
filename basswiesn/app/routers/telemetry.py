import asyncio
import json
import re
import socket
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.core.setup_mode import is_yes_confirmation
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import ConfigBackup, Device, TelemetryEvent, utc_now
from basswiesn.app.routers.shared import device_or_404, summarize_payload
from basswiesn.app.services.catalogs import RADIO_LOG_CLI17000_COMMANDS, RADIO_LOG_GUARDED_HTTP_ENDPOINTS, RADIO_LOG_HTTP_ENDPOINTS, RADIO_LOG_SSH_PLAN
from basswiesn.app.services.network_security import validate_outbound_host
from basswiesn.app.services.protected_devices import is_protected_ip, reject_protected_write_ip
from basswiesn.app.services.action_journal import record_transport_attempt
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.adapters.ssh import build_legacy_ssh_command

router = APIRouter(prefix="/api", tags=["telemetry"])


async def send_cli17000(ip_address: str, commands: list[str], timeout: float = 8.0) -> str:
    reject_protected_write_ip(
        ip_address,
        action="telemetry_cli17000",
        requester="telemetry",
        method="TELNET",
        endpoint="cli17000",
    )
    validation = validate_outbound_host(ip_address, port=17000)
    if not validation.ok:
        raise PermissionError(validation.reason)
    target = validation.addresses[0]

    def run() -> str:
        chunks: list[bytes] = []
        with socket.create_connection((target, 17000), timeout=timeout) as sock:
            sock.settimeout(0.8)
            time.sleep(0.25)
            for command in commands:
                sock.sendall((command + "\n").encode("utf-8"))
                time.sleep(0.75)
                while True:
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not data:
                        break
                    chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def is_write(command: str) -> bool:
        normalized = " ".join(str(command or "").strip().lower().split())
        return not (
            normalized.startswith("getpdo ")
            or normalized.endswith(" get")
            or normalized == "sys configuration"
        )

    writes = [command for command in commands if is_write(command)]
    started = time.monotonic()
    try:
        output = await asyncio.to_thread(run)
        if writes:
            record_transport_attempt(
                ip_address=ip_address,
                action="CLI17000 telemetry batch",
                trigger="telemetry",
                requested_state={"commands": writes},
                result="command_sent",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return output
    except Exception as exc:
        if writes:
            record_transport_attempt(
                ip_address=ip_address,
                action="CLI17000 telemetry batch",
                trigger="telemetry",
                requested_state={"commands": writes},
                result="failed",
                duration_ms=int((time.monotonic() - started) * 1000),
                error_category=exc.__class__.__name__,
            )
        raise


async def run_ssh_readonly_command(ip_address: str, username: str, command: str, timeout: int = 12) -> dict:
    if is_protected_ip(ip_address):
        write_masterlog(
            "protected_device_write_blocked",
            radio_ip=ip_address,
            action="telemetry_ssh_readonly",
            requester="telemetry",
            method="SSH",
            endpoint="ssh",
        )
        return {
            "returncode": 126,
            "stdout": "",
            "stderr": "protected device: SSH is blocked by PROTECTED_DEVICE_IPS",
        }
    validation = validate_outbound_host(ip_address, port=22)
    if not validation.ok:
        return {
            "returncode": 126,
            "stdout": "",
            "stderr": validation.reason,
        }
    target = validation.addresses[0]

    def run() -> dict:
        proc = subprocess.run(
            build_legacy_ssh_command(target, username, command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    return await asyncio.to_thread(run)


async def capture_ssh_readonly_logs(device: Device, db: Session, reason: str = "manual", username: str = "root") -> dict:
    captured: list[dict] = []
    failed: list[dict] = []
    for command in RADIO_LOG_SSH_PLAN:
        try:
            result = await run_ssh_readonly_command(device.ip_address, username, command)
        except Exception as exc:
            failed.append({"command": command, "error": str(exc)})
            continue
        payload = json.dumps({"command": command, **result}, ensure_ascii=False)
        event = TelemetryEvent(
            device_id=device.device_id,
            event_type=f"radio_log_ssh:{reason}",
            endpoint="ssh-readonly",
            payload=payload,
            parsed_summary=summarize_payload(result.get("stdout") or result.get("stderr") or payload),
        )
        db.add(event)
        db.add(ConfigBackup(device_id=device.device_id, path=f"radio-log/{reason}/ssh/{len(captured)+1}.json", content=payload))
        item = {"command": command, "returncode": result["returncode"], "stdout_bytes": len(result.get("stdout", "")), "stderr_bytes": len(result.get("stderr", ""))}
        if result["returncode"] == 0:
            captured.append(item)
        else:
            failed.append({**item, "stderr": result.get("stderr", "")[:500]})
    device.last_seen = utc_now()
    return {"reason": reason, "username": username, "captured": captured, "failed": failed}


async def capture_radio_logs_for_device(device: Device, db: Session, reason: str = "manual", include_cli: bool = True) -> dict:
    client = SoundTouchClient(device.ip_address)
    captured: list[dict] = []
    failed: list[dict] = []
    for endpoint in RADIO_LOG_HTTP_ENDPOINTS:
        try:
            payload = await client.get_xml(endpoint)
        except Exception as exc:
            failed.append({"source": "http", "endpoint": endpoint, "error": str(exc)})
            continue
        event = TelemetryEvent(
            device_id=device.device_id,
            event_type=f"radio_log_http:{reason}",
            endpoint=endpoint,
            payload=payload,
            parsed_summary=summarize_payload(payload),
        )
        db.add(event)
        db.add(ConfigBackup(device_id=device.device_id, path=f"radio-log/{reason}/http{endpoint}", content=payload))
        captured.append({"source": "http", "endpoint": endpoint, "bytes": len(payload)})
        if endpoint == "/info":
            device.info_xml = payload
        elif endpoint in {"/supportedURLs", "/capabilities"} and not device.capabilities_xml:
            device.capabilities_xml = payload
    if include_cli:
        try:
            cli_output = await send_cli17000(device.ip_address, RADIO_LOG_CLI17000_COMMANDS, timeout=5.0)
        except Exception as exc:
            failed.append({"source": "cli17000", "endpoint": "commands", "error": str(exc), "commands": RADIO_LOG_CLI17000_COMMANDS})
        else:
            event = TelemetryEvent(
                device_id=device.device_id,
                event_type=f"radio_log_cli17000:{reason}",
                endpoint="cli17000",
                payload=cli_output,
                parsed_summary=summarize_payload(cli_output),
            )
            db.add(event)
            db.add(ConfigBackup(device_id=device.device_id, path=f"radio-log/{reason}/cli17000.txt", content=cli_output))
            captured.append({"source": "cli17000", "endpoint": "commands", "bytes": len(cli_output), "commands": RADIO_LOG_CLI17000_COMMANDS})
    device.last_seen = utc_now()
    return {"reason": reason, "captured": captured, "failed": failed, "ssh_readonly_plan": RADIO_LOG_SSH_PLAN}


@router.get("/telemetry")
async def telemetry(limit: int = 200, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(TelemetryEvent).order_by(TelemetryEvent.ts.desc()).limit(min(limit, 1000)).all()
    return [
        {
            "id": row.id,
            "ts": row.ts.isoformat(),
            "device_id": row.device_id,
            "event_type": row.event_type,
            "endpoint": row.endpoint,
            "parsed_summary": row.parsed_summary,
            "payload": row.payload,
        }
        for row in rows
    ]


@router.get("/telemetry/summary")
async def telemetry_summary(db: Session = Depends(get_db)) -> dict:
    rows = db.query(TelemetryEvent).order_by(TelemetryEvent.ts.desc()).limit(1000).all()
    by_type: dict[str, int] = {}
    by_device: dict[str, int] = {}
    for row in rows:
        by_type[row.event_type or "unknown"] = by_type.get(row.event_type or "unknown", 0) + 1
        by_device[row.device_id or "unknown"] = by_device.get(row.device_id or "unknown", 0) + 1
    return {"total": len(rows), "by_type": by_type, "by_device": by_device}


@router.post("/telemetry")
async def ingest_telemetry(payload: dict, db: Session = Depends(get_db)) -> dict:
    raw = payload.get("payload", "")
    if not isinstance(raw, str):
        raw = str(raw)
    row = TelemetryEvent(
        device_id=payload.get("device_id", ""),
        event_type=payload.get("event_type", "manual"),
        endpoint=payload.get("endpoint", ""),
        payload=raw,
        parsed_summary=payload.get("parsed_summary") or summarize_payload(raw),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.post("/devices/{device_id}/telemetry/probe")
async def probe_device_telemetry(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    endpoint = payload.get("endpoint", "/netStats")
    if endpoint not in {"/netStats", "/info", "/networkInfo", "/now_playing", "/nowPlaying", "/volume", "/getZone"}:
        raise HTTPException(status_code=400, detail="unsupported telemetry probe endpoint")
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "target": device.ip_address, "endpoint": endpoint}
    xml = await SoundTouchClient(device.ip_address).get_xml(endpoint)
    row = TelemetryEvent(device_id=device.device_id, event_type="probe", endpoint=endpoint, payload=xml, parsed_summary=summarize_payload(xml))
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"dry_run": False, "id": row.id, "endpoint": endpoint, "summary": row.parsed_summary}


@router.get("/devices/{device_id}/radio-log/sources")
async def radio_log_sources(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    return {
        "device_id": device.device_id,
        "radio_ip": device.ip_address,
        "http_endpoints": RADIO_LOG_HTTP_ENDPOINTS,
        "guarded_http_endpoints": RADIO_LOG_GUARDED_HTTP_ENDPOINTS,
        "cli17000_commands": RADIO_LOG_CLI17000_COMMANDS,
        "ssh_readonly_plan": RADIO_LOG_SSH_PLAN,
        "note": "HTTP/XML and CLI17000 sources can be captured by basswiesn now. Full syslog/logread requires SSH read-only access on the radio.",
    }


@router.post("/devices/{device_id}/radio-log/capture")
async def capture_radio_logs(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    reason = str(payload.get("reason") or "manual").strip()[:64] or "manual"
    if payload.get("dry_run", True):
        return {
            "dry_run": True,
            "device_id": device.device_id,
            "radio_ip": device.ip_address,
            "http_endpoints": RADIO_LOG_HTTP_ENDPOINTS,
            "guarded_http_endpoints": RADIO_LOG_GUARDED_HTTP_ENDPOINTS,
            "cli17000_commands": RADIO_LOG_CLI17000_COMMANDS if payload.get("include_cli", True) else [],
            "ssh_readonly_plan": RADIO_LOG_SSH_PLAN,
            "storage": ["telemetry_events", "config_backups"],
        }
    result = await capture_radio_logs_for_device(device, db, reason=reason, include_cli=bool(payload.get("include_cli", True)))
    db.commit()
    return {"dry_run": False, "device_id": device.device_id, **result}


@router.post("/devices/radio-log/capture-batch")
async def capture_radio_logs_batch(payload: dict, db: Session = Depends(get_db)) -> dict:
    device_ids = [str(item).strip() for item in (payload.get("device_ids") or []) if str(item).strip()]
    if not device_ids:
        raise HTTPException(status_code=400, detail="device_ids is required")
    reason = str(payload.get("reason") or "batch").strip()[:64] or "batch"
    include_cli = bool(payload.get("include_cli", True))
    include_ssh = bool(payload.get("include_ssh", False))
    username = str(payload.get("username") or "root").strip() or "root"
    devices = [device_or_404(db, device_id) for device_id in device_ids]
    if payload.get("dry_run", True):
        return {
            "dry_run": True,
            "devices": [{"device_id": device.device_id, "radio_ip": device.ip_address} for device in devices],
            "http_endpoints": RADIO_LOG_HTTP_ENDPOINTS,
            "cli17000_commands": RADIO_LOG_CLI17000_COMMANDS if include_cli else [],
            "ssh_readonly_plan": RADIO_LOG_SSH_PLAN if include_ssh else [],
            "note": "Batch capture stores results per device in telemetry_events/config_backups. SSH still needs per-device confirmation when enabled.",
        }
    if include_ssh:
        provided = [str(item).strip() for item in (payload.get("ssh_confirmations") or [])]
        if len(provided) != len(devices) or not all(is_yes_confirmation(item) for item in provided):
            raise HTTPException(status_code=409, detail={"error": "ssh read confirmations required", "expected": ["YES"] * len(devices)})
    results = []
    for device in devices:
        http_result = await capture_radio_logs_for_device(device, db, reason=reason, include_cli=include_cli)
        item = {"device_id": device.device_id, "radio_ip": device.ip_address, "radio_log": http_result}
        if include_ssh:
            item["ssh_log"] = await capture_ssh_readonly_logs(device, db, reason=reason, username=username)
        results.append(item)
    db.commit()
    return {"dry_run": False, "reason": reason, "results": results}


@router.post("/devices/{device_id}/ssh-log/capture")
async def capture_ssh_logs(device_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    device = device_or_404(db, device_id)
    username = str(payload.get("username") or "root").strip() or "root"
    reason = str(payload.get("reason") or "manual-ssh").strip()[:64] or "manual-ssh"
    if not re.match(r"^[A-Za-z0-9_.-]{1,32}$", username):
        raise HTTPException(status_code=400, detail="invalid ssh username")
    if payload.get("dry_run", True):
        return {"dry_run": True, "device_id": device.device_id, "radio_ip": device.ip_address, "username": username, "commands": RADIO_LOG_SSH_PLAN, "confirmation_required_for_write": "YES", "note": "Read-only SSH capture uses fixed commands only and stores outputs in telemetry_events/config_backups."}
    expected = "YES"
    if not is_yes_confirmation(payload.get("confirmation")):
        raise HTTPException(status_code=409, detail={"error": "ssh read confirmation required", "expected": expected})
    result = await capture_ssh_readonly_logs(device, db, reason=reason, username=username)
    db.commit()
    return {"dry_run": False, "device_id": device.device_id, "radio_ip": device.ip_address, **result}
