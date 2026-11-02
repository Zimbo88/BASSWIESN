import asyncio
from datetime import UTC, datetime
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.db import SessionLocal
from basswiesn.app.models import Device, PlayHistory, RuntimeState, ScheduledAction, Station, Setting, utc_now
from basswiesn.app.services.device_transactions import device_transaction


def split_csv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _normalize_volume(value, default: int = 25) -> int:
    try:
        volume = int(value if value is not None else default)
    except (TypeError, ValueError):
        volume = default
    return max(0, min(100, volume))


def _day_matches(days: str, now: datetime) -> bool:
    key = (days or "daily").strip().lower()
    weekday = now.weekday()
    if key == "daily":
        return True
    if key == "weekdays":
        return weekday < 5
    if key == "weekend":
        return weekday >= 5
    if key == "once":
        return True
    if "," in key:
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return names[weekday] in {item.strip()[:3] for item in key.split(",")}
    return True


def _runtime_row(db: Session, key: str) -> RuntimeState:
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        row = RuntimeState(key=key, value="{}")
        db.add(row)
    return row


def _claim_schedule_marker(db: Session, marker: RuntimeState, minute: str) -> bool:
    """Durably claim a scheduled occurrence before a worker can dispatch it.

    Device ownership alone would serialize duplicate workers, not prevent their
    sequential replay. A lost action reply never clears this single-use claim.
    """
    raw = marker.value or "{}"
    try:
        previous = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(previous, dict) or previous.get("minute") == minute:
        return False
    encoded = json.dumps({"minute": minute, "ts": utc_now().isoformat()})
    if marker.id is None:
        marker.value = encoded
        try:
            db.commit()  # The unique marker key arbitrates first-use races.
        except IntegrityError:
            db.rollback()
            return False
        return True
    changed = db.execute(update(RuntimeState).where(RuntimeState.id == marker.id,
        RuntimeState.value == marker.value).values(value=encoded, updated_at=utc_now()),
        execution_options={"synchronize_session": False}).rowcount
    db.commit()
    db.expire(marker)
    return changed == 1


def _schedule_timezone(db: Session) -> ZoneInfo:
    row = db.query(Setting).filter(Setting.key == "default_timezone").one_or_none()
    name = (row.value if row and row.value else "") or getattr(get_settings(), "default_timezone", "") or "Europe/Berlin"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        write_masterlog("alarm_timer_timezone_invalid", timezone=name, fallback="UTC")
        return ZoneInfo("UTC")


def _schedule_payload(row: ScheduledAction, *, trigger: str, dry_run: bool | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "start_time": row.start_time,
        "end_time": row.end_time,
        "days": row.days,
        "device_ids": split_csv(row.device_ids),
        "station_id": row.station_id,
        "preset_button": row.preset_button,
        "volume": row.volume,
        "multiroom_master_id": row.multiroom_master_id,
        "multiroom_member_ids": split_csv(row.multiroom_member_ids),
        "stop_action": row.stop_action or "stop_standby",
        "dry_run": bool(row.dry_run) if dry_run is None else dry_run,
        "trigger": trigger,
    }


async def trigger_schedule(row: ScheduledAction, db: Session, *, trigger: str = "scheduler", force_dry_run: bool | None = None) -> dict:
    dry_run = bool(row.dry_run) if force_dry_run is None else force_dry_run
    payload = _schedule_payload(row, trigger=trigger, dry_run=dry_run)
    if trigger == "manual":
        write_masterlog("timer_manual_trigger", schedule_id=row.id, name=row.name, dry_run=dry_run)
    if dry_run:
        write_masterlog("alarm_timer_due", schedule_id=row.id, trigger=trigger, name=row.name, dry_run=True)
        return {"ok": True, "dry_run": True, "schedule": payload, "actions": _planned_actions(payload)}

    write_masterlog("alarm_timer_due", schedule_id=row.id, trigger=trigger, name=row.name, dry_run=False)
    write_masterlog("timer_trigger_start", schedule_id=row.id, trigger=trigger, name=row.name)
    write_masterlog("alarm_timer_play_start", schedule_id=row.id, trigger=trigger, name=row.name)
    try:
        if row.multiroom_master_id and row.multiroom_member_ids:
            from basswiesn.app.routers.multiroom import multiroom_set

            result = await multiroom_set(
                {
                    "master_device_id": row.multiroom_master_id,
                    "member_device_ids": split_csv(row.multiroom_member_ids),
                    "station_id": row.station_id,
                    "preset_button": row.preset_button,
                    "volume": row.volume if row.volume is not None else 5,
                    "latency_mode": "SYNC_TO_ZONE",
                    "dry_run": False,
                    "memory_checked": True,
                },
                db,
            )
        else:
            result = await _play_schedule_devices(row, db)
    except Exception as exc:
        write_masterlog("alarm_timer_play_failed", schedule_id=row.id, trigger=trigger, name=row.name, error=str(exc))
        _record_schedule_history(row, db, trigger=trigger, success=False, error_message=str(exc))
        write_masterlog("timer_trigger_failed", schedule_id=row.id, trigger=trigger, name=row.name, error=str(exc))
        raise
    if row.multiroom_master_id and row.multiroom_member_ids:
        # A verified topology is not evidence that every member plays audio.
        verified_devices = {row.multiroom_master_id} if (result.get("playback") or {}).get("verified") is True else set()
        expected = {row.multiroom_master_id, *split_csv(row.multiroom_member_ids)}
    else:
        expected = set(split_csv(row.device_ids))
        verified_devices = {identity for identity in expected if isinstance(result.get(identity), dict)
                            and result[identity].get("verified") is True}
    verified = bool(expected) and verified_devices == expected
    _record_schedule_history(row, db, trigger=trigger, success=verified,
                             verified_devices=verified_devices,
                             error_message="" if verified else "PLAYBACK_PENDING_READBACK")
    write_masterlog("alarm_timer_play_complete" if verified else "alarm_timer_play_pending",
                    schedule_id=row.id, trigger=trigger, verified=verified)
    return {"ok": verified, "verified": verified, "dry_run": False,
            "status": "VERIFIED" if verified else "PENDING_READBACK", "schedule": payload, "result": result}


async def _play_schedule_devices(row: ScheduledAction, db: Session) -> dict:
    from basswiesn.app.routers.api import send_key_command
    from basswiesn.app.routers.stations_presets import play_station_on_device

    result = {}
    device_ids = split_csv(row.device_ids)
    target_volume = _normalize_volume(row.volume)
    if row.preset_button:
        key = f"PRESET_{int(row.preset_button)}"
        for device_id in device_ids:
            result[device_id] = await send_key_command(device_id, {"key": key, "safe_volume": target_volume, "trigger": "scheduler"}, db)
        return result
    if not row.station_id:
        return {"skipped": True, "reason": "no station_id or preset_button configured"}
    for device_id in device_ids:
        result[device_id] = await play_station_on_device(
            device_id,
            int(row.station_id),
            {"dry_run": False, "memory_checked": True, "target_volume": target_volume,
             "trigger": "scheduler", "trigger_type": "timer"},
            db,
        )
    return result


def _record_schedule_history(row: ScheduledAction, db: Session, *, trigger: str, success: bool,
                             error_message: str = "", verified_devices: set[str] | None = None) -> None:
    device_ids = set(split_csv(row.device_ids))
    if row.multiroom_master_id:
        device_ids.add(row.multiroom_master_id)
        device_ids.update(split_csv(row.multiroom_member_ids))
    station = db.query(Station).filter(Station.id == row.station_id).one_or_none() if row.station_id else None
    trigger_type = "timer"
    source = "PRESET" if row.preset_button else "LOCAL_INTERNET_RADIO"
    for device_id in sorted(device_ids):
        device_success = success if verified_devices is None else device_id in verified_devices
        device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
        db.add(PlayHistory(
            device_id=device_id,
            device_name=device.name if device else "",
            device_ip=device.ip_address if device else "",
            station_id=station.id if station else None,
            station_name=station.name if station else (f"Preset {row.preset_button}" if row.preset_button else row.name),
            stream_url=station.stream_url if station else "",
            source=source,
            source_type=source,
            zone_master_id=row.multiroom_master_id or "",
            zone_member_ids=",".join(split_csv(row.multiroom_member_ids)),
            trigger=trigger,
            trigger_type=trigger_type,
            preset_button=row.preset_button,
            preset_name=f"Preset {row.preset_button}" if row.preset_button else "",
            volume=_normalize_volume(row.volume) if row.volume is not None else None,
            success=1 if device_success else 0,
            error_message="" if device_success else error_message[:1024],
            ended_at=utc_now() if not device_success else None,
        ))
    db.commit()
    write_masterlog("playback_event_complete" if success else "playback_event_failed", schedule_id=row.id, trigger=trigger, trigger_type=trigger_type, success=success, error_message=error_message[:300])


def _planned_actions(payload: dict) -> list[dict]:
    if payload.get("multiroom_master_id"):
        return [
            {
                "action": "multiroom_play",
                "master_device_id": payload["multiroom_master_id"],
                "member_device_ids": payload["multiroom_member_ids"],
                "station_id": payload["station_id"],
                "volume": payload["volume"],
            }
        ]
    return [
        {"action": "play_preset" if payload.get("preset_button") else "play_station", "device_id": device_id, "station_id": payload["station_id"], "preset_button": payload.get("preset_button"), "volume": payload["volume"]}
        for device_id in payload.get("device_ids", [])
    ]


async def stop_schedule(row: ScheduledAction, db: Session, *, trigger: str = "scheduler") -> dict:
    from basswiesn.app.routers.api import _execute_key_command, device_power_action
    from basswiesn.app.routers.shared import enforce_ip_write_guard

    write_masterlog("alarm_timer_stop_start", schedule_id=row.id, trigger=trigger, name=row.name)
    device_ids = set(split_csv(row.device_ids))
    if row.multiroom_master_id:
        device_ids.add(row.multiroom_master_id)
        device_ids.update(split_csv(row.multiroom_member_ids))
    stop_action = (row.stop_action or "stop_standby").strip().lower()
    if stop_action not in {"stop", "standby", "stop_standby"}:
        stop_action = "stop_standby"
    result = {}
    try:
        devices = [db.query(Device).filter_by(device_id=identity).one_or_none() for identity in sorted(device_ids)]
        if not devices or any(device is None for device in devices):
            raise ValueError("schedule targets missing; no stop attempted")
        for device in devices:
            enforce_ip_write_guard(db, device)
        async with device_transaction(device_ids):
            for device in devices:
                actions = {}
                if stop_action in {"stop", "stop_standby"}:
                    actions["stop"] = await _execute_key_command(device.device_id, {"key": "STOP", "trigger": trigger}, db, participants=devices)
                    if actions["stop"].get("verified") is not True:
                        result[device.device_id] = {"ok": False, "actions": actions}
                        continue  # Unknown STOP cannot authorize a follow-up.
                if stop_action in {"standby", "stop_standby"}:
                    # Uses the shared identified, backed-up power transaction.
                    # Existing groups fail closed; never silently dissolve them.
                    actions["standby"] = await device_power_action(device.device_id, "standby", {"dry_run": False, "confirmation": "YES"}, db)
                verified = all(isinstance(item, dict) and item.get("verified") is True for item in actions.values())
                result[device.device_id] = {"ok": verified, "stop_action": stop_action, "actions": actions}
    except Exception as exc:
        write_masterlog("alarm_timer_stop_failed", schedule_id=row.id, trigger=trigger, name=row.name, error=str(exc))
        raise
    verified = bool(result) and all(item["ok"] for item in result.values())
    if verified:
        _close_schedule_history(row, db)
    write_masterlog("alarm_timer_stop_complete" if verified else "alarm_timer_stop_pending", schedule_id=row.id, trigger=trigger)
    return {"ok": verified, "verified": verified, "schedule_id": row.id, "result": result}


def _close_schedule_history(row: ScheduledAction, db: Session) -> None:
    device_ids = set(split_csv(row.device_ids))
    if row.multiroom_master_id:
        device_ids.add(row.multiroom_master_id)
        device_ids.update(split_csv(row.multiroom_member_ids))
    if not device_ids:
        return
    now = utc_now()
    active = (
        db.query(PlayHistory)
        .filter(PlayHistory.device_id.in_(sorted(device_ids)))
        .filter(PlayHistory.trigger_type == "timer")
        .filter(PlayHistory.ended_at.is_(None))
        .all()
    )
    for item in active:
        item.ended_at = now
    db.commit()


async def run_alarm_engine_once(db: Session, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(_schedule_timezone(db))
    current_minute = local_now.strftime("%Y-%m-%dT%H:%M")
    current_time = local_now.strftime("%H:%M")
    results = []
    rows = db.query(ScheduledAction).filter(ScheduledAction.enabled == 1).all()
    for row in rows:
        if not _day_matches(row.days, local_now):
            continue
        if row.start_time == current_time:
            marker_key = f"alarm:last_run:{row.id}"
            marker = _runtime_row(db, marker_key)
            if _claim_schedule_marker(db, marker, current_minute):
                try:
                    results.append(await trigger_schedule(row, db, trigger="scheduler"))
                    if (row.days or "").strip().lower() == "once" and not row.end_time:
                        row.enabled = 0
                        db.commit()
                except Exception as exc:
                    write_masterlog("alarm_timer_play_failed", schedule_id=row.id, trigger="scheduler", error=str(exc))
                    results.append({"ok": False, "schedule_id": row.id, "error": str(exc)})
        if row.end_time and row.end_time == current_time:
            marker_key = f"alarm:last_stop:{row.id}"
            marker = _runtime_row(db, marker_key)
            if _claim_schedule_marker(db, marker, current_minute):
                try:
                    results.append(await stop_schedule(row, db, trigger="scheduler"))
                    if (row.days or "").strip().lower() == "once":
                        row.enabled = 0
                        db.commit()
                except Exception as exc:
                    write_masterlog("alarm_timer_stop_failed", schedule_id=row.id, trigger="scheduler", error=str(exc))
                    results.append({"ok": False, "schedule_id": row.id, "error": str(exc)})
    return results


async def alarm_engine_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            await run_alarm_engine_once(db)
        finally:
            db.close()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
