"""Explicit remote-key transactions; transport acceptance is not radio success.

Use the existing profile, ownership and complete HTTP-snapshot authorities.
The legacy-named RebootSnapshot is also a pure parser of the state needed here;
this service never calls reboot or maintenance. Physical controllers cannot be
locked, so precondition checks narrow but cannot eliminate external races.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device, utc_now
from basswiesn.app.repositories.research_state_repository import ResearchStateRepository
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_policy import policy_for_device
from basswiesn.app.services.device_settings_writer import IdentityMismatch, XmlClient, verify_settings_identity
from basswiesn.app.services.device_state import RUNTIME_FIELDS, load_runtime_state, runtime_from_now_playing, save_runtime_state
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.direct_selection import Selection, _source_ready
from basswiesn.app.services.maintenance_reboot import PATHS, RebootSnapshot, _root
from basswiesn.app.services.network_security import validate_outbound_http_url
from basswiesn.app.services.playback_state import close_open_sessions
from basswiesn.app.services.playback_safety_gate import (
    arm_playback_safety_gate, clear_playback_safety_gate,
    fail_playback_safety_gate, verify_playback_safety_gate,
)
from basswiesn.app.core.redaction import redact_text
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.zone_changes import require_selected_scope


class KeyActionError(Exception):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail["code"]))


def stopped(value: Selection) -> bool:
    return value.source == "STANDBY" or value.play_status in {"STOP_STATE", "PAUSE_STATE"}


def preset_item(snapshot: RebootSnapshot, key: str) -> ET.Element | None:
    if not key.startswith("PRESET_"):
        return None
    slot = key.removeprefix("PRESET_")
    root = _root(snapshot.raw["/presets"], "presets")
    nodes = [node for node in root.findall("preset") if node.get("id", node.get("buttonNumber", "")) == slot]
    if len(nodes) != 1 or len(nodes[0].findall("ContentItem")) != 1:
        raise ValueError("preset is missing or ambiguous")
    item = nodes[0].findall("ContentItem")[0]
    if not item.get("source") or not item.get("location"):
        raise ValueError("preset content identity is incomplete")
    return item


def matched(key: str, before: RebootSnapshot, after: RebootSnapshot, desired: Selection | None) -> bool:
    old, actual = before.selection, after.selection
    playing = actual.source != "STANDBY" and actual.play_status == "PLAY_STATE"
    if desired is not None:
        return playing and actual.same_content(desired)
    if key in {"STOP", "PAUSE"} or (key == "PLAY_PAUSE" and old.play_status == "PLAY_STATE"):
        return stopped(actual) and (actual.source == "STANDBY" or actual.same_content(old))
    if key in {"PLAY", "PLAY_PAUSE"}:
        return playing and (old.source == "STANDBY" or actual.same_content(old))
    if key == "POWER":
        return (actual.source != "STANDBY") if old.source == "STANDBY" else actual.source == "STANDBY"
    if key == "MUTE":
        return after.volume[1] != before.volume[1] and actual == old
    if key == "VOLUME_UP":
        return after.volume[0] > before.volume[0] or before.volume[0] == after.volume[0] == 100
    if key == "VOLUME_DOWN":
        return after.volume[0] < before.volume[0] or before.volume[0] == after.volume[0] == 0
    if key == "AUX_INPUT":
        return actual.source == "AUX"
    # A next-track/repeat/shuffle acknowledgement has no established expected
    # state contract here. Return an honest unverified outcome, never infer it.
    return False


async def execute_key(
    db: Session, device: Device, key: str, *, safe_volume: int | None,
    client_factory: Callable[[Device], XmlClient], guard: Callable[[Session, Device], None],
    participants: Sequence[Device] | None = None, trigger: str = "api",
) -> dict[str, Any]:
    """Participants are server-selected zone transaction scope, never JSON input."""
    devices = list(participants) if participants is not None else [device]
    if device not in devices or len({item.device_id for item in devices}) != len(devices):
        raise ValueError("invalid internal key transaction scope")
    for item in devices:
        guard(db, item)  # protected targets fail before transport construction
    result: dict[str, Any] = {
        "device_id": device.device_id, "job_id": f"key-{uuid4().hex}", "key": key,
        "dry_run": False, "path": "/key", "safe_volume": safe_volume,
        "confirmed_volume": None, "verified": False, "automatic_retry": False,
        "automatic_rollback": False, "audible_audio_verified": False,
        "responses": [], "wake_sequence": [], "preset_rewrite": {"rewritten": False},
        "select_fallback": {"used": False}, "reconciliation_required": False,
    }
    frozen = {item.device_id: device_fingerprint(item) for item in devices}
    targets = {item.device_id: item.ip_address for item in devices}
    identity = device.device_id
    desired: Selection | None = None
    attempted, backup_ref, armed, audio_sent = False, "", False, False
    code = "KEY_PREFLIGHT_FAILED"
    async with device_transaction(targets):
        def unchanged() -> None:
            for item in devices:
                db.refresh(item)
                if device_fingerprint(item) != frozen[item.device_id]:
                    raise IdentityMismatch("stored target changed")
                guard(db, item)

        try:
            unchanged()
            for item in devices:
                _root(item.info_xml, "info", item.device_id)
                verify_settings_identity(item, item.info_xml)
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
                        raw[path] = await client.get_xml(path)
                        unchanged()
                    # After an accepted source command the radio may briefly
                    # omit playStatus. Observe only: never replay the command
                    # or use this incomplete state to authorize another write.
                    if wait_for_state and not audio_sent:
                        for _ in range(20):
                            playing = _root(raw["/now_playing"], "nowPlaying", item.device_id)
                            if playing.findall("playStatus") or playing.get("source") in {"STANDBY", "INVALID_SOURCE"}:
                                break
                            await asyncio.sleep(0.15)
                            unchanged()
                            raw["/info"] = await client.get_xml("/info")
                            verify_settings_identity(item, raw["/info"])
                            for path in PATHS:
                                raw[path] = await client.get_xml(path)
                                unchanged()
                    if key.startswith("PRESET_") and item.device_id == identity:
                        raw["/sources"] = await client.get_xml("/sources")
                        if desired is not None:
                            _source_ready(raw["/sources"], identity, desired)
                    observed = RebootSnapshot.parse(raw, item.device_id, allow_pending=audio_sent)
                    require_selected_scope(observed.zone, targets)
                    if len(devices) == 1 and not observed.zone.standalone(identity):
                        raise ValueError("zone scope required")
                    observations[item.device_id] = observed
                return observations

            before = await snapshot()
            start = before[identity]
            if key == "PLAY_PAUSE" and start.selection.play_status not in {"PLAY_STATE", "STOP_STATE", "PAUSE_STATE"}:
                raise ValueError("toggle previous state unknown")
            content_item = preset_item(start, key)
            desired = Selection.content(content_item) if content_item is not None else None
            if desired:
                _source_ready(start.raw["/sources"], identity, desired)
            serialized = json.dumps({"schema": 1, "devices": {key: snap.raw for key, snap in before.items()}}, sort_keys=True)
            digest = sha256(serialized.encode()).hexdigest()
            backup = ConfigBackup(device_id=identity, path="http8090:key-start", content=serialized)
            db.add(backup)
            db.flush()
            backup_id = backup.id
            backup_ref = f"config-backup:{backup_id}:sha256:{digest}"
            result["backup_ref"] = backup_ref

            def ledger(phase: str, requested: object = None, verified: bool = False) -> None:
                record_action(db, job_id=result["job_id"], device_id=identity, ip_address=frozen[identity][1],
                    action="key_command", trigger=trigger, phase=phase, requested_state=requested,
                    backup_ref=backup_ref, rollback_ref=backup_ref, verified=verified,
                    result="VERIFIED" if verified else "PENDING_READBACK")
                db.commit()

            def integrity() -> None:
                unchanged()
                row = db.get(ConfigBackup, backup_id, populate_existing=True)
                if row is None or row.device_id != identity or sha256(row.content.encode()).hexdigest() != digest:
                    raise ValueError("START missing or changed")

            def stable(observations: dict[str, RebootSnapshot]) -> None:
                for target, observed in observations.items():
                    if (observed.zone, observed.presets) != (before[target].zone, before[target].presets):
                        raise ValueError("zone or stored presets changed")

            def equal(left: dict[str, RebootSnapshot], right: dict[str, RebootSnapshot]) -> bool:
                for target in targets:
                    old, new = left[target], right[target]
                    if old.same_state(new):
                        continue
                    # Buffer completion for the already requested content is
                    # forward progress, not permission to ignore STOP, a new
                    # source, volume changes or an unowned topology change.
                    forward = (attempted and (desired is not None or key in {"PLAY", "PLAY_PAUSE"}
                               or (key == "POWER" and start.selection.source == "STANDBY"))
                               and old.selection.play_status in {"BUFFERING", "BUFFERING_STATE", "UNCONFIRMED_STATE"}
                               and new.selection.play_status in {"BUFFERING", "BUFFERING_STATE", "PLAY_STATE"}
                               and old.selection.same_content(new.selection)
                               and (desired is None or new.selection.same_content(desired)))
                    if not forward or not old.same_state(replace(new, selection=old.selection)):
                        return False
                return True

            ledger("KEY_START", {"key": key, "safe_volume": safe_volume})
            integrity()
            code = "WRITE_PRECONDITION_FAILED"
            current = await snapshot()
            if not equal(before, current):
                raise ValueError("START changed while persisted")

            def correction_equal(left: dict[str, RebootSnapshot], right: dict[str, RebootSnapshot]) -> bool:
                # While our local provider withholds audio, player state cannot
                # be a prerequisite for volume reduction or muting. Identity,
                # topology, presets and observed volume/mute remain mandatory.
                return all((left[t].volume, left[t].zone, left[t].presets) ==
                           (right[t].volume, right[t].zone, right[t].presets) for t in targets)

            async def write(path: str, body: str, *, safety: bool = False) -> str:
                nonlocal attempted, current, code
                if safety and (not audio_sent or safe_volume is None or path != "/volume"
                               or body != f"<volume>{safe_volume}</volume>"):
                    raise ValueError("invalid safety correction")
                compare = correction_equal if safety else equal
                latest = await snapshot()
                stable(latest)
                if not compare(current, latest):
                    raise ValueError("state changed before next write")
                integrity()
                ledger("KEY_WRITE_INTENT", {"path": path, "key": key})
                latest = await snapshot()
                if not compare(current, latest):
                    raise ValueError("state changed while intent persisted")
                integrity()
                attempted, code = True, "KEY_OUTCOME_UNKNOWN"
                try:
                    response = (await clients[identity].get_xml(path) if path == "/standby"
                                else await clients[identity].post_xml(path, body))
                except Exception:
                    # One observation, no release/fallback/replay after a lost
                    # press or any other uncertain step in the composite.
                    try:
                        result["last_observation"] = (await snapshot())[identity].summary(identity)
                    except Exception:
                        result["readback_unavailable"] = True
                    raise
                current = await snapshot(wait_for_state=True)
                stable(current)
                return redact_text(response)[:2048]

            async def press(name: str, *, safety: bool = False) -> list[str]:
                nonlocal attempted, current, code, audio_sent
                if safety and (not audio_sent or name != "MUTE" or current[identity].volume[1]):
                    raise ValueError("invalid mute-on correction")
                compare = correction_equal if safety else equal
                async def await_unmute_readiness(observed: dict[str, RebootSnapshot]) -> dict[str, RebootSnapshot]:
                    # A live station can briefly rebuffer between final checks.
                    # Wait read-only, never toggle mute while readiness is unknown
                    # and never treat a different source as our own progress.
                    if not (audio_sent and name == "MUTE" and current[identity].volume[1] and not safety):
                        return observed
                    for _ in range(20):
                        stable(observed)
                        actual = observed[identity]
                        if actual.volume != current[identity].volume:
                            raise ValueError("volume/mute changed before unmute")
                        if not actual.selection.same_content(current[identity].selection):
                            raise ValueError("source changed before unmute")
                        if actual.selection.play_status == "PLAY_STATE":
                            return observed
                        if actual.selection.play_status not in {"BUFFERING", "BUFFERING_STATE", "UNCONFIRMED_STATE"}:
                            raise ValueError("playback no longer ready for unmute")
                        await asyncio.sleep(0.2)
                        observed = await snapshot(wait_for_state=True)
                    raise ValueError("playback did not settle before unmute")

                latest = await snapshot()
                latest = await await_unmute_readiness(latest)
                stable(latest)
                if not compare(current, latest):
                    raise ValueError("state changed before key pair")
                integrity()
                ledger("KEY_WRITE_INTENT", {"path": "/key", "key": name, "sequence": ["press", "release"]})
                latest = await snapshot()
                latest = await await_unmute_readiness(latest)
                if not compare(current, latest):
                    raise ValueError("state changed while intent persisted")
                integrity()
                attempted, code = True, "KEY_OUTCOME_UNKNOWN"
                try:
                    response = await clients[identity].post_xml("/key", f'<key state="press" sender="Gabbo">{name}</key>')
                    if starts_audio and (name == key or (
                            key == "PLAY" and name == "PLAY_PAUSE" and start.selection.source == "STANDBY")):
                        audio_sent = True
                    # Release an acknowledged key promptly. Full state snapshots
                    # between press/release artificially hold a physical button
                    # for seconds. Release is continuation, never a fresh action.
                    await asyncio.sleep(0.12)
                    integrity()
                    live_info = await clients[identity].get_xml("/info")
                    verify_settings_identity(device, live_info)
                    unchanged()
                    release = await clients[identity].post_xml("/key", f'<key state="release" sender="Gabbo">{name}</key>')
                except (Exception, asyncio.CancelledError):
                    try:
                        result["last_observation"] = (await snapshot())[identity].summary(identity)
                    except Exception:
                        result["readback_unavailable"] = True
                    raise
                current = await snapshot(wait_for_state=True)
                stable(current)
                return [redact_text(item)[:2048] for item in (response, release)]

            async def volume_once() -> None:
                nonlocal code
                if current[identity].volume[0] != safe_volume:
                    await write("/volume", f"<volume>{safe_volume}</volume>", safety=audio_sent)
                if current[identity].volume[0] != safe_volume:
                    code = "KEY_SAFE_VOLUME_MISMATCH"
                    raise ValueError("safe volume not confirmed before audio command")
                result["confirmed_volume"] = safe_volume

            async def mute_once(enabled: bool) -> None:
                if current[identity].volume[1] != enabled:
                    await press("MUTE", safety=audio_sent and enabled)
                if current[identity].volume[1] != enabled:
                    raise ValueError("safe playback mute not confirmed")

            starts_audio = (desired is not None or key == "PLAY"
                            or (key == "PLAY_PAUSE" and stopped(start.selection))
                            or (key == "POWER" and start.selection.source == "STANDBY"))
            if safe_volume is not None and starts_audio:
                await volume_once()
                # Muting before selection protects remembered source-volume
                # normalization. The local provider additionally withholds its
                # audio URL until post-command volume/mute readback is verified.
                if safe_volume <= 5 and (desired is None or desired.source == "LOCAL_INTERNET_RADIO"):
                    # Resume/POWER can restore the last local station without a
                    # preset ContentItem in standby. Apply the same device-bound
                    # provider hold; absence of a preset must not skip this gate.
                    arm_playback_safety_gate(db, identity, safe_volume=safe_volume, station_id=0)
                    armed = True
                await mute_once(True)
            policy = policy_for_device(device, db)
            if desired and safe_volume is not None and start.selection.source == "STANDBY":
                if not policy.allow_auto_wakeup:
                    raise ValueError("physical wake required by device policy")
                # A preset key wakes natively. An extra POWER would first start
                # the unrelated previous source and restore its remembered volume.
            if key == "POWER" and start.selection.source != "STANDBY":
                # The explicit standby route is not a toggle. A slow identity
                # response between POWER press/release can otherwise straddle
                # the transition and wake an SCM device again.
                result["responses"] = [await write("/standby", "")]
                result["path"] = "/standby"
            else:
                # SCM accepts PLAY without waking from standby; PLAY_PAUSE is
                # the observed native wake/resume control. Translate only after
                # strict standby readback, never toggle a playing/unknown target.
                command = "PLAY_PAUSE" if key == "PLAY" and start.selection.source == "STANDBY" else key
                result["command_sent"] = command
                result["responses"] = await press(command)
            if armed and start.selection.source == "STANDBY":
                # An acknowledged preset may still leave an old standby sample
                # visible. Its volume is not a post-selection observation: the
                # firmware can restore a louder source volume on activation.
                # Await departure only, NOT successful resolution/PLAY_STATE;
                # the latter depends on the provider response we withhold.
                for _ in range(20):
                    if current[identity].selection.source != "STANDBY":
                        break
                    await asyncio.sleep(0.15)
                    current = await snapshot(wait_for_state=True)
                    stable(current)
                if current[identity].selection.source == "STANDBY":
                    raise ValueError("source activation not observed; provider remains blocked")
            # Do not wait for PLAY_STATE before releasing the provider: the
            # receiver needs that response to enter playback. Only safe volume
            # and mute correction may use an incomplete post-command selection.
            # Firmware can normalize volume during source selection. One
            # deliberate correction is distinct from blindly retrying a POST.
            if safe_volume is not None and starts_audio:
                await volume_once()
                await mute_once(True)
                if armed:
                    verify_playback_safety_gate(db, identity, volume=current[identity].volume[0], muted=current[identity].volume[1])
            elif safe_volume is not None and key == "POWER" and start.selection.source == "STANDBY":
                await volume_once()
            for attempt in range(20):
                actual = current[identity]
                if matched(key, start, actual, desired):
                    break
                if attempt < 19:
                    await asyncio.sleep(0.3)
                    current = await snapshot(wait_for_state=True)
                    stable(current)
            # A fallback requires a definite failure for this exact requested
            # content, not unknown state, another station, or a manual STOP.
            if (desired and content_item is not None and not matched(key, start, current[identity], desired)
                    and current[identity].selection.play_status == "INVALID_SOURCE"
                    and current[identity].selection.same_content(desired) and policy.allow_preset_restore):
                if (desired.source != "LOCAL_INTERNET_RADIO" or desired.source_account
                        or content_item.get("type") != "stationurl"
                        or any(not validate_outbound_http_url(url).ok for url in
                               [desired.location, *(node.text or "" for node in content_item.findall("containerArt"))] if url)):
                    raise ValueError("fallback contract not supported")
                response = await write("/select", ET.tostring(content_item, encoding="unicode"))
                result["select_fallback"] = {"used": True, "path": "/select", "response": response}
                if safe_volume is not None:
                    await volume_once()
            actual = current[identity]
            result["readback"] = runtime_from_now_playing(actual.raw["/now_playing"])
            result["readback_active"] = actual.selection.source != "STANDBY" and actual.selection.play_status == "PLAY_STATE"
            code = "KEY_READBACK_MISMATCH"
            if not matched(key, start, actual, desired):
                raise ValueError("expected key state not established")
            if safe_volume is not None and starts_audio and actual.volume[0] != safe_volume:
                raise ValueError("final safe volume diverged")
            if safe_volume is not None and starts_audio:
                await mute_once(start.volume[1])
                actual = current[identity]
                if not matched(key, start, actual, desired) or actual.volume[0] != safe_volume:
                    raise ValueError("playback or safe volume changed during unmute")
            if not key.startswith("VOLUME_") and not (safe_volume is not None and starts_audio):
                if actual.volume[0] != start.volume[0]:
                    raise ValueError("unrequested local volume change")
            if key != "MUTE" and actual.volume[1] != start.volume[1]:
                raise ValueError("unrequested mute change")
            # Members' volume/mute must not silently change from a master key.
            if any(current[target].volume != before[target].volume for target in targets if target != identity):
                raise ValueError("member volume changed")
            integrity()
            code = "KEY_RESULT_COMMIT_FAILED"
            if armed:
                clear_playback_safety_gate(db, identity)
                armed = False
            if stopped(actual.selection) and key in {"STOP", "PAUSE", "PLAY_PAUSE", "POWER"}:
                from basswiesn.app.services.live_radio_reconnect import cancel
                cancel(db, identity)
                close_open_sessions(db, identity, reason="manual_stop" if key in {"STOP", "POWER"} else "manual_pause", device_last_seen=device.last_seen)
            if key in {"PLAY", "PLAY_PAUSE"}:
                ResearchStateRepository(db).set_current_restriction_timer(identity, actual.selection.source,
                    play_started_at=utc_now() if result["readback_active"] else None,
                    reason="explicit_play_readback" if result["readback_active"] else "explicit_pause_readback")
            changes = dict(result["readback"])
            if desired:
                changes["current_preset"] = int(key.removeprefix("PRESET_"))
                result["select_fallback"]["verified"] = True
            _row, runtime = load_runtime_state(db, identity)
            runtime.update({name: value for name, value in changes.items() if name in RUNTIME_FIELDS})
            save_runtime_state(db, identity, runtime, commit=False)
            ledger("KEY_COMPLETED", verified=True)
            return {**result, "ok": True, "verified": True, "status": "VERIFIED"}
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            write_masterlog("key_transaction_failed", device_id=identity,
                            job_id=result["job_id"], code=code,
                            exception_type=type(exc).__name__,
                            reason=redact_text(str(exc))[:300] if isinstance(exc, ValueError) else "details withheld")
            if armed:
                fail_playback_safety_gate(db, identity, code)
            if isinstance(exc, IdentityMismatch):
                code = "IDENTITY_MISMATCH"
            detail = {**result, "ok": False, "verified": False, "code": code,
                      "status": "DIVERGED" if attempted else "BLOCKED", "reconciliation_required": attempted,
                      "message": "Zustand nicht bestätigt. Keine automatische Wiederholung; Radiozustand neu einlesen."}
            record_transport_attempt(device_id=identity, ip_address=frozen[identity][1], action="key_command",
                trigger=trigger, phase=detail["status"], result=detail["status"], job_id=result["job_id"],
                backup_ref=backup_ref, rollback_ref=backup_ref, error_category=code, verified=False)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise KeyActionError(detail) from None
