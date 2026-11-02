"""Identity-bound station selection, shared by manual, timer and zone callers.

The local provider gate prevents audio URL release until post-select safe volume
and mute have actually been observed. No command is replayed on a lost reply.
Snapshots reuse the established parsers/policies, not a new hardware allowlist.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device, Station, utc_now
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.core.redaction import redact_text
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_policy import policy_for_device
from basswiesn.app.services.device_settings_writer import IdentityMismatch, XmlClient, verify_settings_identity
from basswiesn.app.services.device_state import load_runtime_state, runtime_from_now_playing, save_runtime_state
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.direct_selection import Selection, _source_ready
from basswiesn.app.services.maintenance_reboot import PATHS, RebootSnapshot, _root
from basswiesn.app.services.network_security import validate_outbound_http_url
from basswiesn.app.services.playback_safety_gate import (
    arm_playback_safety_gate, clear_playback_safety_gate, fail_playback_safety_gate,
    verify_playback_safety_gate,
)
from basswiesn.app.services.playback_state import close_open_sessions
from basswiesn.app.services.setup_rebuild.audio_safety import load_audio_safety, lock_audio_safety
from basswiesn.app.services.zone_changes import require_selected_scope


class StationPlaybackError(Exception):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail["code"]))


async def play_station(
    db: Session, device: Device, station: Station, xml: str, *,
    client_factory: Callable[[Device], XmlClient], guard: Callable[[Session, Device], None],
    safe_volume: int | None = None, target_volume: int | None = None,
    participants: Sequence[Device] | None = None, trigger: str = "stream",
    trigger_type: str = "station", internal_event: bool = False,
    attempts: int = 20, interval: float = 0.5,
) -> dict[str, Any]:
    devices = list(participants) if participants is not None else [device]
    if device not in devices or len({item.device_id for item in devices}) != len(devices):
        raise ValueError("invalid server-selected participant scope")
    for item in devices:
        guard(db, item)  # All affected radios, before any transport construction.
    identity = device.device_id
    targets = {item.device_id: item.ip_address for item in devices}
    frozen = {item.device_id: device_fingerprint(item) for item in devices}
    catalog = (station.id, station.name, station.stream_url, station.internal)
    result: dict[str, Any] = {
        "dry_run": False, "device_id": identity, "station_id": station.id, "path": "/select",
        "job_id": f"station-{uuid4().hex}", "verified": False, "confirmed_volume": None,
        "response": "", "audible_audio_verified": False, "automatic_retry": False,
        "automatic_rollback": False, "transport_uncertain": False,
        "playback_history": "pending_live_confirmation",
    }
    attempted, armed, backup_ref, selected = False, False, "", False
    code = "STATION_PREFLIGHT_FAILED"
    volume_target = safe_volume if safe_volume is not None else target_volume
    async with device_transaction(targets):
        def unchanged() -> None:
            db.refresh(station)
            if (station.id, station.name, station.stream_url, station.internal) != catalog:
                raise ValueError("station changed during playback transaction")
            for item in devices:
                db.refresh(item)
                if device_fingerprint(item) != frozen[item.device_id]:
                    raise IdentityMismatch("stored playback target changed")
                guard(db, item)

        try:
            for value, upper in ((safe_volume, 5), (target_volume, 100)):
                if value is not None and (type(value) is not int or not 0 <= value <= upper):
                    raise ValueError("invalid volume")
            if safe_volume is not None and target_volume is not None:
                raise ValueError("ambiguous volume policy")
            unchanged()
            for item in devices:
                if load_audio_safety(db, item.device_id).locked:
                    code = "audio_safety_locked"
                    raise ValueError("explicit audio safety check required")
                _root(item.info_xml, "info", item.device_id)
                verify_settings_identity(item, item.info_xml)
            content = _root(xml, "ContentItem")
            desired = Selection.content(content)
            if desired.source != "LOCAL_INTERNET_RADIO" or desired.source_account or content.get("type") != "stationurl":
                raise ValueError("unsupported station contract")
            if not desired.location or any(url and not validate_outbound_http_url(url).ok for url in
                [desired.location, *(child.text or "" for child in content.findall("containerArt"))]):
                raise ValueError("invalid media or artwork URI")
            clients = {item.device_id: client_factory(item) for item in devices}

            async def snapshot(*, wait_for_state: bool = False) -> dict[str, RebootSnapshot]:
                observations = {}
                for item in devices:
                    unchanged()
                    client = clients[item.device_id]
                    raw = {"/info": await client.get_xml("/info")}
                    _root(raw["/info"], "info", item.device_id)
                    verify_settings_identity(item, raw["/info"])
                    for path in PATHS:
                        unchanged()
                        raw[path] = await client.get_xml(path)
                    # Some receivers briefly omit playStatus after selection.
                    # Only observe; missing state can never authorize a command.
                    if item.device_id == identity and wait_for_state and not (armed and selected):
                        for _ in range(max(0, attempts - 1)):
                            playing = _root(raw["/now_playing"], "nowPlaying", identity)
                            if playing.findall("playStatus"):
                                break
                            await asyncio.sleep(interval)
                            unchanged()
                            raw["/info"] = await client.get_xml("/info")
                            _root(raw["/info"], "info", identity)
                            verify_settings_identity(item, raw["/info"])
                            for path in PATHS:
                                unchanged()
                                raw[path] = await client.get_xml(path)
                    if item.device_id == identity:
                        raw["/sources"] = await client.get_xml("/sources")
                        _source_ready(raw["/sources"], identity, desired)
                    unchanged()
                    observed = RebootSnapshot.parse(raw, item.device_id,
                        allow_pending=armed and selected and item.device_id == identity)
                    require_selected_scope(observed.zone, targets)
                    if len(devices) == 1 and not observed.zone.standalone(identity):
                        raise ValueError("server-selected zone participants required")
                    observations[item.device_id] = observed
                return observations

            before = await snapshot()
            start = before[identity]
            code = "STATION_BACKUP_FAILED"
            serialized = json.dumps({"schema": 1, "devices": {key: item.raw for key, item in before.items()},
                                     "intent": {"xml": xml, "safe_volume": safe_volume, "target_volume": target_volume}}, sort_keys=True)
            digest = sha256(serialized.encode()).hexdigest()
            backup = ConfigBackup(device_id=identity, path="http8090:station-start", content=serialized)
            db.add(backup)
            db.flush()
            backup_id = backup.id
            backup_ref = f"config-backup:{backup_id}:sha256:{digest}"
            result["backup_ref"] = backup_ref

            def ledger(phase: str, requested: object = None, verified: bool = False) -> None:
                record_action(db, job_id=result["job_id"], device_id=identity, ip_address=frozen[identity][1],
                    action="station_playback", trigger=trigger, phase=phase, requested_state=requested,
                    backup_ref=backup_ref, rollback_ref=backup_ref, verified=verified,
                    result="VERIFIED" if verified else "PENDING_READBACK")
                db.commit()

            def integrity() -> None:
                unchanged()
                saved = db.get(ConfigBackup, backup_id, populate_existing=True)
                if saved is None or saved.device_id != identity or sha256(saved.content.encode()).hexdigest() != digest:
                    raise ValueError("station START missing or changed")

            def stable(observations: dict[str, RebootSnapshot]) -> None:
                for target, item in observations.items():
                    if (item.zone, item.presets) != (before[target].zone, before[target].presets):
                        raise ValueError("topology or presets changed")
                    if target != identity and item.volume != before[target].volume:
                        raise ValueError("unrequested member volume change")

            def equal(left: dict[str, RebootSnapshot], right: dict[str, RebootSnapshot]) -> bool:
                for target, old in left.items():
                    new = right[target]
                    if old.same_state(new):
                        continue
                    forward = (old.selection.play_status in {"BUFFERING", "BUFFERING_STATE"}
                               and new.selection.play_status == "PLAY_STATE" and new.selection.same_content(old.selection))
                    if selected and target == identity:
                        active = {"BUFFERING", "BUFFERING_STATE", "PLAY_STATE", "UNCONFIRMED_STATE"}
                        forward = (old.selection.play_status in active and new.selection.play_status in active
                                   and old.selection.same_content(desired) and new.selection.same_content(desired))
                    if not forward or not old.same_state(replace(new, selection=old.selection)):
                        return False
                return True

            from basswiesn.app.services.live_radio_reconnect import cancel
            cancel(db, identity)
            ledger("STATION_START", {"source": desired.source, "location": desired.location})
            integrity()
            code = "WRITE_PRECONDITION_FAILED"
            current = await snapshot()
            if not equal(before, current):
                raise ValueError("START changed while persisted")
            if safe_volume is not None:
                arm_playback_safety_gate(db, identity, safe_volume=safe_volume, station_id=station.id)
                armed = True

            async def write(path: str, body: str = "", *, selection: bool = False) -> None:
                nonlocal current, attempted, code, selected
                correcting = selected and armed and (path == "/volume" or (
                    path == "/key" and ">MUTE</key>" in body and not current[identity].volume[1]))
                def compare(left: dict[str, RebootSnapshot], right: dict[str, RebootSnapshot]) -> bool:
                    if correcting:
                        return all((left[t].volume, left[t].zone, left[t].presets) ==
                                   (right[t].volume, right[t].zone, right[t].presets) for t in targets)
                    return equal(left, right)
                latest = await snapshot()
                stable(latest)
                if not compare(current, latest):
                    raise ValueError("state changed before command")
                integrity()
                ledger("STATION_WRITE_INTENT", {"path": path})
                latest = await snapshot()
                if not compare(current, latest):
                    raise ValueError("state changed while intent persisted")
                integrity()
                attempted, code = True, "STATION_OUTCOME_UNKNOWN"
                try:
                    if path == "/standby":
                        await clients[identity].get_xml(path)
                    else:
                        await clients[identity].post_xml(path, body)
                    if selection:
                        selected = True
                except httpx.HTTPStatusError:
                    raise  # Explicit rejection is not proof of success.
                except Exception:
                    result["transport_uncertain"] = True
                    if not selection or safe_volume is not None:
                        raise  # Never continue a composite after an uncertain step.
                prior = current[identity].selection
                current = await snapshot(wait_for_state=selection)
                stable(current)
                preserves_source = path == "/volume" or (path == "/key" and ">MUTE</key>" in body)
                observed = current[identity].selection
                own_progress = (selected and prior.same_content(desired) and observed.same_content(desired)
                                and prior.play_status in {"BUFFERING", "BUFFERING_STATE", "PLAY_STATE", "UNCONFIRMED_STATE"}
                                and observed.play_status in {"BUFFERING", "BUFFERING_STATE", "PLAY_STATE", "UNCONFIRMED_STATE"})
                if preserves_source and observed != prior and not correcting and not own_progress and not (
                    prior.play_status in {"BUFFERING", "BUFFERING_STATE"}
                    and observed.play_status == "PLAY_STATE" and observed.same_content(prior)):
                    raise ValueError("unrequested selection change")

            async def press(key: str) -> None:
                for state in ("press", "release"):
                    await write("/key", f'<key state="{state}" sender="Gabbo">{key}</key>')

            async def volume_once() -> None:
                nonlocal code
                if current[identity].volume[0] != volume_target:
                    await write("/volume", f"<volume>{volume_target}</volume>")
                if current[identity].volume[0] != volume_target:
                    code = "STATION_VOLUME_MISMATCH"
                    raise ValueError("actual volume not confirmed")
                result["confirmed_volume"] = volume_target

            async def mute_once(enabled: bool) -> None:
                if current[identity].volume[1] != enabled:
                    await press("MUTE")
                if current[identity].volume[1] != enabled:
                    raise ValueError("actual mute not confirmed")

            if target_volume is not None and start.selection.source == "STANDBY":
                if not policy_for_device(device, db).allow_auto_wakeup:
                    raise ValueError("automatic wake denied by device policy")
                await press("POWER")
                if current[identity].selection.source == "STANDBY":
                    raise ValueError("wake not confirmed")
            if volume_target is not None:
                await volume_once()
            if safe_volume is not None:
                await mute_once(True)
            try:
                await write("/select", xml, selection=True)
            except httpx.HTTPStatusError:
                # Preserve the established safety shutdown, but only after
                # fresh identity/topology/START checks, never after a lost reply.
                if safe_volume is not None:
                    current = await snapshot(wait_for_state=True)
                    stable(current)
                    observed = current[identity].volume[0]
                    await press("STOP")
                    await write("/standby")
                    await volume_once()
                    stopped = current[identity].selection.source == "STANDBY"
                    result["audio_safety"] = {"required": True, "observed_before_cleanup": observed,
                        "confirmed_volume": current[identity].volume[0], "stopped_and_standby": stopped, "locked": True}
                raise
            if volume_target is not None and not result["transport_uncertain"]:
                if armed and start.selection.source == "STANDBY":
                    for _ in range(attempts):
                        if current[identity].selection.source != "STANDBY":
                            break
                        await asyncio.sleep(interval)
                        current = await snapshot()
                        stable(current)
                    if current[identity].selection.source == "STANDBY":
                        raise ValueError("source activation not observed")
                await volume_once()  # One observed firmware normalization correction, not a retry.
            if safe_volume is not None:
                await mute_once(True)
                verify_playback_safety_gate(db, identity, volume=current[identity].volume[0], muted=current[identity].volume[1])
            elif current[identity].volume[1] != start.volume[1]:
                raise ValueError("unrequested mute change")
            if volume_target is None and current[identity].volume != start.volume:
                raise ValueError("unrequested volume change")

            accepted = False
            for index in range(attempts):
                observed_selection = current[identity].selection
                accepted = observed_selection.same_content(desired) and observed_selection.play_status in {"PLAY_STATE", "BUFFERING_STATE", "BUFFERING"}
                if accepted:
                    break
                if index + 1 < attempts:
                    await asyncio.sleep(interval)
                    current = await snapshot(wait_for_state=True)
                    stable(current)
            code = "STATION_READBACK_MISMATCH"
            if not accepted:
                raise ValueError("requested station not observed")
            if safe_volume is not None:
                await mute_once(start.volume[1])
            if volume_target is not None and current[identity].volume[0] != volume_target:
                raise ValueError("final volume differs")
            if current[identity].volume[1] != start.volume[1]:
                raise ValueError("final mute differs")
            actual = current[identity]
            if not actual.selection.same_content(desired) or actual.selection.play_status not in {"PLAY_STATE", "BUFFERING_STATE", "BUFFERING"}:
                raise ValueError("selection changed during final steps")
            integrity()
            code = "STATION_RESULT_COMMIT_FAILED"
            if armed:
                clear_playback_safety_gate(db, identity)
                armed = False
            _, runtime = load_runtime_state(db, identity)
            runtime.update(runtime_from_now_playing(actual.raw["/now_playing"]))
            runtime["current_preset"] = None
            close_open_sessions(db, identity, reason="source_command_pending", device_last_seen=device.last_seen)
            runtime["playback_pending"] = {"station_id": station.id, "station_name": station.name,
                "stream_url": station.stream_url, "source": desired.source, "trigger": trigger,
                "trigger_type": trigger_type, "internal_event": internal_event or station.internal,
                "created_at": utc_now().isoformat()}
            save_runtime_state(db, identity, runtime, commit=False)
            verified = actual.selection.play_status == "PLAY_STATE"
            ledger("STATION_COMPLETED" if verified else "STATION_BUFFERING", verified=verified)
            return {**result, "ok": verified, "verified": verified,
                    "status": "VERIFIED" if verified else "PENDING_READBACK",
                    "selection_verified": True, "readback": actual.summary(identity),
                    "reconciliation_required": not verified}
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            write_masterlog("station_transaction_failed", device_id=identity, code=code,
                exception_type=type(exc).__name__,
                reason=redact_text(str(exc))[:300] if isinstance(exc, ValueError) else "details withheld")
            if isinstance(exc, IdentityMismatch):
                code = "IDENTITY_MISMATCH"
            if safe_volume is not None and (armed or attempted):
                try:
                    fail_playback_safety_gate(db, identity, code)
                    lock_audio_safety(db, identity, code)
                except Exception:
                    db.rollback()
            detail = {**result, "ok": False, "verified": False, "code": code,
                "status": "DIVERGED" if attempted else "BLOCKED", "reconciliation_required": attempted,
                "message": "Station state not confirmed. No automatic retry; inspect actual state and saved START."}
            record_transport_attempt(device_id=identity, ip_address=frozen[identity][1], action="station_playback",
                trigger=trigger, phase=detail["status"], result=detail["status"], job_id=result["job_id"],
                backup_ref=backup_ref, rollback_ref=backup_ref, error_category=code, verified=False)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StationPlaybackError(detail) from None
