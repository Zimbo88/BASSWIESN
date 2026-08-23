"""Conservative per-device problem-radio maintenance reboot workflow."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import uuid
import xml.etree.ElementTree as ET

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import Device, Setting, Station, utc_now
from basswiesn.app.services.action_journal import record_action
from basswiesn.app.services.action_preflight import action_preflight, port_open
from basswiesn.app.services.playback_state import close_open_sessions, confirm_playback_session, is_confirmed_playing
from basswiesn.app.services.protected_devices import require_unprotected_device


_locks: dict[str, asyncio.Lock] = {}


def _lock(device_id: str) -> asyncio.Lock:
    return _locks.setdefault(device_id, asyncio.Lock())


def maintenance_reboot_lab_enabled(db) -> bool:
    row = db.query(Setting).filter(Setting.key == "lab_mode").one_or_none()
    return get_settings().lab_mode or (row is not None and str(row.value).strip().lower() == "true")


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml or "")


def _volume(xml: str) -> tuple[int | None, bool]:
    root = _root(xml)
    raw = root.findtext("actualvolume", root.findtext("targetvolume", root.text or ""))
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = None
    return value, root.attrib.get("muted", "false").lower() == "true"


async def capture_reboot_snapshot(device: Device, db, *, client_factory=SoundTouchClient) -> dict:
    require_unprotected_device(device, action="maintenance_reboot_snapshot", requester="maintenance_reboot", method="GET", endpoint="/now_playing")
    client = client_factory(device.ip_address)
    now_xml, volume_xml, zone_xml, presets_xml = await asyncio.gather(
        client.get_xml("/now_playing"), client.get_xml("/volume"), client.get_xml("/getZone"), client.get_xml("/presets")
    )
    root = _root(now_xml)
    content = root.find(".//ContentItem")
    source = root.attrib.get("source", "") or (content.attrib.get("source", "") if content is not None else "")
    play_status = root.findtext("playStatus", root.attrib.get("playStatus", ""))
    captured_at = utc_now()
    volume, muted = _volume(volume_xml)
    zone = _root(zone_xml)
    zone_active = bool(zone.attrib.get("master") or zone.findall(".//member"))
    location = content.attrib.get("location", "") if content is not None else ""
    preset_id = None
    for preset in _root(presets_xml).findall(".//preset"):
        item = preset.find("ContentItem")
        if item is not None and location and item.attrib.get("location") == location:
            preset_id = int(preset.attrib.get("id", preset.attrib.get("buttonNumber", "0")) or 0) or None
            break
    station = db.query(Station).filter(Station.stream_url == location, Station.internal.is_(False)).one_or_none() if location else None
    confirmed = is_confirmed_playing(reachable=True, current_source=source, playback_state=play_status, play_status=play_status, state_observed_at=captured_at, now=captured_at, stale_after_seconds=30)
    return {
        "device_id": device.device_id, "captured_at": captured_at.isoformat(), "reachable": True,
        "confirmed_playing": confirmed, "source": source, "play_status": play_status,
        "now_playing": {child.tag: (child.text or "").strip() for child in root if child.text},
        "content_item": dict(content.attrib) if content is not None else {}, "station_name": station.name if station else "",
        "station_id": station.id if station else None, "preset_id": preset_id,
        "stream_url": "" if station and station.internal else location, "volume": volume, "muted": muted,
        "standby": source.upper() == "STANDBY", "zone_active": zone_active,
        "zone_master_id": zone.attrib.get("master", ""), "zone_xml": zone_xml[:2048],
    }


async def _set_phase(db, device: Device, job_id: str, phase: str, snapshot: dict, *, trigger: str, result: str = "") -> None:
    device.maintenance_phase = phase
    record_action(db, job_id=job_id, device_id=device.device_id, ip_address=device.ip_address, action="maintenance_reboot", trigger=trigger, phase=phase, before_state=snapshot, result=result)
    db.commit()


async def run_maintenance_reboot(device: Device, db, *, trigger: str = "automatic", send_cli=None, client_factory=SoundTouchClient, wait_for_return: bool = True) -> dict:
    job_id = f"maintenance-{uuid.uuid4().hex[:12]}"
    if trigger != "manual_lab":
        write_masterlog(
            "maintenance_reboot_denied",
            job_id=job_id,
            device_id=device.device_id,
            trigger=trigger,
            reason="automatic radio reboot is disabled",
        )
        return {
            "ok": False,
            "code": "AUTOMATIC_RADIO_REBOOT_DISABLED",
            "job_id": job_id,
            "message": "Radio-Reboot ist ausschliesslich als manuelle LAB-Aktion erlaubt.",
        }
    if not maintenance_reboot_lab_enabled(db):
        return {
            "ok": False,
            "code": "LAB_MODE_REQUIRED",
            "job_id": job_id,
            "message": "Radio-Reboot ist nur bei bewusst aktiviertem LAB-Modus erlaubt.",
        }
    lock = _lock(device.device_id)
    if lock.locked():
        return {"ok": False, "code": "DEVICE_BUSY", "job_id": job_id}
    async with lock:
        started = datetime.now(UTC)
        device.maintenance_last_attempt_at = started
        snapshot: dict = {}
        try:
            preflight = await action_preflight(db, device, required_port=17000, action="maintenance_reboot")
            if not preflight["ok"]:
                device.maintenance_phase = "skipped"
                device.maintenance_last_result = "skipped_device_offline" if preflight["code"] == "DEVICE_UNREACHABLE" else preflight["code"].lower()
                device.maintenance_next_run_at = started + timedelta(hours=device.maintenance_reboot_interval_hours)
                record_action(db, job_id=job_id, device_id=device.device_id, ip_address=device.ip_address, action="maintenance_reboot", trigger=trigger, phase="skipped", result=device.maintenance_last_result, error_category=preflight["code"])
                db.commit()
                return {"ok": False, "code": preflight["code"], "job_id": job_id, "message": preflight["message"]}
            snapshot = await capture_reboot_snapshot(device, db, client_factory=client_factory)
            await _set_phase(db, device, job_id, "snapshot_captured", snapshot, trigger=trigger)
            if snapshot["zone_active"]:
                device.maintenance_last_result = "skipped_active_multiroom"
                device.maintenance_next_run_at = started + timedelta(hours=device.maintenance_reboot_interval_hours)
                await _set_phase(db, device, job_id, "skipped", snapshot, trigger=trigger, result="skipped_active_multiroom")
                return {"ok": False, "code": "ACTIVE_MULTIROOM", "job_id": job_id, "snapshot": snapshot}
            close_open_sessions(db, device.device_id, reason="maintenance_reboot", device_last_seen=device.last_seen)
            await _set_phase(db, device, job_id, "reboot_requested", snapshot, trigger=trigger)
            if send_cli is None:
                from basswiesn.app.routers.api import _send_cli17000
                send_cli = _send_cli17000
            await send_cli(device.ip_address, ["sys reboot"], timeout=10.0)
            if wait_for_return:
                await _set_phase(db, device, job_id, "waiting_for_online", snapshot, trigger=trigger)
                deadline = datetime.now(UTC) + timedelta(seconds=get_settings().maintenance_reboot_return_timeout_seconds)
                online = False
                await asyncio.sleep(5)
                while datetime.now(UTC) < deadline:
                    if await port_open(device.ip_address, get_settings().radio_port, 2.0):
                        try:
                            await client_factory(device.ip_address).get_xml("/info")
                            online = True
                            break
                        except Exception:
                            pass
                    await asyncio.sleep(10)
                if not online:
                    raise TimeoutError("radio did not return before maintenance timeout")
            await _set_phase(db, device, job_id, "restoring_state", snapshot, trigger=trigger)
            client = client_factory(device.ip_address)
            restore = "inactive_preserved"
            if snapshot.get("confirmed_playing"):
                preset_id = snapshot.get("preset_id")
                station_id = snapshot.get("station_id")
                if preset_id:
                    for state in ("press", "release"):
                        await client.post_xml("/key", f'<key state="{state}" sender="Gabbo">PRESET_{preset_id}</key>')
                    restore = "preset_restored"
                elif station_id:
                    station = db.query(Station).filter(Station.id == station_id, Station.internal.is_(False)).one_or_none()
                    if station is None:
                        restore = "restore_unavailable"
                    else:
                        from basswiesn.app.routers.stations_presets import _device_content_item_xml
                        location = station.stream_url_resolved or station.stream_url
                        xml = _device_content_item_xml(db, device.device_id, station, location, snapshot.get("source") or "LOCAL_INTERNET_RADIO")
                        await client.post_xml("/select", xml)
                        restore = "station_restored"
                else:
                    restore = "restore_unavailable"
            elif snapshot.get("standby"):
                current = await client.get_xml("/now_playing")
                if _root(current).attrib.get("source", "").upper() != "STANDBY":
                    for state in ("press", "release"):
                        await client.post_xml("/key", f'<key state="{state}" sender="Gabbo">POWER</key>')
                restore = "standby_restored"
            if snapshot.get("volume") is not None:
                await client.post_xml("/volume", f'<volume>{int(snapshot["volume"])}</volume>')
            current_volume_xml = await client.get_xml("/volume")
            _current_volume, current_muted = _volume(current_volume_xml)
            if current_muted != bool(snapshot.get("muted")):
                for state in ("press", "release"):
                    await client.post_xml("/key", f'<key state="{state}" sender="Gabbo">MUTE</key>')
            await _set_phase(db, device, job_id, "verifying_state", snapshot, trigger=trigger, result=restore)
            verify_xml = await client.get_xml("/now_playing")
            verify_root = _root(verify_xml)
            verified = (not snapshot.get("confirmed_playing")) or verify_root.findtext("playStatus", "").upper() == "PLAY_STATE"
            if verified and snapshot.get("confirmed_playing"):
                confirm_playback_session(db, device, observed_at=utc_now(), source=snapshot.get("source") or "LOCAL_INTERNET_RADIO", station_id=snapshot.get("station_id"), station_name=snapshot.get("station_name", ""), stream_url=snapshot.get("stream_url", ""), trigger="maintenance_restore", trigger_type="station", internal_event=False)
            device.maintenance_last_success_at = utc_now()
            device.maintenance_next_run_at = device.maintenance_last_success_at + timedelta(hours=device.maintenance_reboot_interval_hours)
            device.maintenance_last_result = restore if verified else "restore_unverified"
            device.maintenance_phase = "completed" if verified else "failed"
            device.maintenance_failure_count = 0 if verified else device.maintenance_failure_count + 1
            record_action(db, job_id=job_id, device_id=device.device_id, ip_address=device.ip_address, action="maintenance_reboot", trigger=trigger, phase=device.maintenance_phase, before_state=snapshot, result=device.maintenance_last_result, after_state={"now_playing": verify_xml[:1024]}, duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000), verified=verified)
            db.commit()
            return {"ok": verified, "job_id": job_id, "phase": device.maintenance_phase, "result": device.maintenance_last_result, "snapshot": snapshot}
        except (OSError, TimeoutError, asyncio.TimeoutError, ConnectionError) as exc:
            device.maintenance_phase = "failed"
            device.maintenance_last_result = str(exc)[:255]
            device.maintenance_failure_count += 1
            device.maintenance_next_run_at = started + timedelta(hours=device.maintenance_reboot_interval_hours)
            record_action(db, job_id=job_id, device_id=device.device_id, ip_address=device.ip_address, action="maintenance_reboot", trigger=trigger, phase="failed", before_state=snapshot, result=str(exc), duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000), error_category=type(exc).__name__)
            db.commit()
            write_masterlog("maintenance_reboot_failed", job_id=job_id, device_id=device.device_id, radio_ip=device.ip_address, error_category=type(exc).__name__, duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000), automatic=trigger == "automatic")
            return {"ok": False, "code": "DEVICE_UNREACHABLE" if isinstance(exc, OSError) else "MAINTENANCE_FAILED", "job_id": job_id, "message": str(exc)}


async def run_due_maintenance(db) -> list[dict]:
    """Disable legacy scheduled radio reboots without contacting a device."""

    changed = False
    for device in db.query(Device).filter(Device.maintenance_reboot_enabled.is_(True)).all():
        device.maintenance_reboot_enabled = False
        device.maintenance_next_run_at = None
        device.maintenance_phase = "idle"
        device.maintenance_last_result = "automatic_reboot_disabled_in_1_6"
        changed = True
        write_masterlog(
            "maintenance_reboot_schedule_disabled",
            device_id=device.device_id,
            reason="radio reboot is manual LAB only",
        )
    if changed:
        db.commit()
    return []


async def maintenance_reboot_loop(stop_event: asyncio.Event) -> None:
    """Compatibility no-op; BASSWIESN 2.0 never schedules a radio reboot."""

    del stop_event
    write_masterlog(
        "maintenance_reboot_scheduler_disabled",
        reason="radio reboot is manual LAB only",
    )
