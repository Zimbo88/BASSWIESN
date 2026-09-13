"""Opt-in, single-shot recovery of confirmed local live-radio end-of-stream.

No startup scan, six-hour timer, volume command, preset write or retry of an
ambiguous POST. Only the cloud process schedules work, after an incoming native
report. Durable generation and compare-and-swap prevent duplicate writes across
workers. A restarted process never replays old pending jobs.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime
from hashlib import sha256
import ipaddress
import json
import time
from uuid import uuid4
from xml.etree import ElementTree as ET

from sqlalchemy import update

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import ConfigBackup, Device, RuntimeState, Setting, Station
from basswiesn.app.services.device_policy import device_lock, policy_for_device
from basswiesn.app.services.network_security import validate_outbound_http_url
from basswiesn.app.services.orion import StationDescriptor, station_contract_key, station_location
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.services.stream_compat import probe_stream_reachability
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.services.logo_validation import validate_logo_reference

PREFIX = "live_radio_reconnect:"
DELAY_SECONDS = 5
FRESH_SECONDS = 90
COOLDOWN_SECONDS = 300
MAX_ATTEMPTS_PER_HOUR = 3


def _key(device_id):
    return PREFIX + device_id.upper()


def enabled(db, device_id):
    row = db.query(Setting).filter_by(key=_key(device_id)).one_or_none()
    return row is not None and row.value == "true"


def state(db, device_id):
    row = db.query(RuntimeState).filter_by(key=_key(device_id)).one_or_none()
    value = json.loads(row.value) if row else {}
    if not isinstance(value, dict):
        raise ValueError("invalid reconnect state")
    return row, value


def _store(db, device_id, value):
    row = db.query(RuntimeState).filter_by(key=_key(device_id)).one_or_none()
    if row is None:
        row = RuntimeState(key=_key(device_id))
        db.add(row)
    row.value = json.dumps(value, sort_keys=True)
    row.updated_at = datetime.now(UTC)
    db.flush()


def _compare_store(db, row, value):
    """Do not let a delayed report overwrite another worker's claim."""
    return db.execute(
        update(RuntimeState).where(RuntimeState.id == row.id, RuntimeState.value == row.value)
        .values(value=json.dumps(value, sort_keys=True), updated_at=datetime.now(UTC)),
        execution_options={"synchronize_session": False},
    ).rowcount == 1


def cancel(db, device_id):
    row, value = state(db, device_id)
    if row:
        value.update(phase="CANCELLED", generation=str(uuid4()))
        _store(db, device_id, value)


def preference(db, device_id):
    _row, value = state(db, device_id)
    phase = value.get("phase", "IDLE")
    if phase in {"PENDING", "ATTEMPTED"} and time.time() - value.get("finish_at", 0) > FRESH_SECONDS:
        phase = "EXPIRED"
    return {"enabled": enabled(db, device_id), "state": phase,
            "last_result": value.get("last_result"), "volume_action": "NONE",
            "requires_new_playback": True, "hardware_validation": "OPEN"}


def save_preference(db, device_id, value):
    if type(value) is not bool:
        raise ValueError("enabled must be boolean")
    row = db.query(Setting).filter_by(key=_key(device_id)).one_or_none()
    if row is None:
        row = Setting(key=_key(device_id))
        db.add(row)
    row.value = "true" if value else "false"
    # Changing preference cannot arm an already-ended session.
    cancel(db, device_id)
    db.commit()
    return preference(db, device_id)


def _native_peer(request, device):
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host) == ipaddress.ip_address(device.ip_address)
    except ValueError:
        return False


def observe_contract(db, request, device, descriptor):
    """Bind only a new native selection while opt-in is already enabled."""
    if not enabled(db, device.device_id) or not _native_peer(request, device):
        return
    require_unprotected_device(device, action="live_reconnect_registration")
    _row, old = state(db, device.device_id)
    now = time.time()
    # Never retain/replay a previously resolved signed CDN URL.
    descriptor = replace(descriptor, stream_url_resolved="")
    _store(db, device.device_id, {
        "generation": str(uuid4()), "phase": "ARMED", "created": now,
        "station_id": station_contract_key(descriptor), "descriptor": asdict(descriptor),
        "provider_host": request.headers.get("host", ""),
        "ip_address": device.ip_address,
        "attempts": [t for t in old.get("attempts", []) if now - t < 3600],
        "last_result": old.get("last_result"),
    })


def observe_report(db, request, device, station_id, fields):
    """Return a generation to schedule; never open a transport here."""
    if not enabled(db, device.device_id) or not _native_peer(request, device):
        return None
    row, value = state(db, device.device_id)
    if not row or value.get("station_id") != station_id or value.get("phase") != "ARMED":
        return None
    now = time.time()
    event, reason, position = fields.get("eventType"), fields.get("reason"), fields.get("timeIntoTrack")
    if event == "STOP" and reason == "FINISH":
        previous = value.get("position")
        if (type(position) is not int or type(previous) is not int or position < 60
                or not 0 <= position - previous <= FRESH_SECONDS
                or not 0 <= now - value.get("progress_at", 0) <= 30
                or position > now - value["created"] + 60):
            return None
        attempts = [t for t in value.get("attempts", []) if now - t < 3600]
        if len(attempts) >= MAX_ATTEMPTS_PER_HOUR or (attempts and now - attempts[-1] < COOLDOWN_SECONDS):
            value.update(phase="CANCELLED", last_result="COOLDOWN")
            _compare_store(db, row, value)
            return None
        value.update(phase="PENDING", finish_at=now, attempts=attempts)
        return value["generation"] if _compare_store(db, row, value) else None
    if event in {"STOP", "PAUSE"}:
        cancel(db, device.device_id)
    elif event in {"START", "TIMED"} and type(position) is int and position >= 0:
        # A frozen post-FINISH timed report cannot arm another attempt; phase
        # PENDING/ATTEMPTED is excluded above. Only actual advancing counters
        # refresh progress evidence.
        previous = value.get("position")
        if previous is not None and position < previous:
            cancel(db, device.device_id)
        elif previous is None or position > previous:
            value.update(position=position, progress_at=now)
            _compare_store(db, row, value)
    return None


class ReconnectStopped(Exception):
    pass


async def public_stream_probe(url):
    return await probe_stream_reachability(url, public_only=True)


class LiveRadioReconnect:
    def __init__(self, session_factory, *, client_factory=SoundTouchClient, probe=public_stream_probe):
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.probe = probe
        self.tasks = {}

    def schedule(self, device_id, generation):
        key = (device_id, generation)
        if key in self.tasks:
            return
        task = asyncio.create_task(self.run(device_id, generation), name="live-radio-reconnect")
        self.tasks[key] = task
        task.add_done_callback(lambda done: self.tasks.pop(key, None))

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _current(self, device_id, generation, phases):
        with self.session_factory() as db:
            device = db.query(Device).filter_by(device_id=device_id).one_or_none()
            if device is None or not enabled(db, device_id):
                raise ReconnectStopped("DISABLED_OR_REMOVED")
            require_unprotected_device(device, action="live_radio_reconnect")
            if not policy_for_device(device, db).allow_invalid_source_recovery:
                raise ReconnectStopped("DEVICE_POLICY_BLOCKED")
            _row, value = state(db, device_id)
            if (value.get("generation") != generation or value.get("phase") not in phases
                    or value.get("ip_address") != device.ip_address
                    or not 0 <= time.time() - value.get("finish_at", 0) <= FRESH_SECONDS):
                raise ReconnectStopped("SESSION_CHANGED_OR_EXPIRED")
            return device, value

    def _result(self, device_id, generation, result):
        with self.session_factory() as db:
            row, value = state(db, device_id)
            # A reconnect serves a new provider contract/generation. Keep its
            # history but do not change its ARMED/PENDING phase.
            if row:
                value["last_result"] = result
                if value.get("generation") == generation:
                    value["phase"] = "FINISHED"
                _compare_store(db, row, value)
                db.commit()
        write_masterlog("live_radio_reconnect_result", device_id=device_id, result=result, volume_action="NONE")

    async def run(self, device_id, generation):
        try:
            await asyncio.sleep(DELAY_SECONDS)
            async with device_lock(device_id):
                await self._run_locked(device_id, generation)
        except asyncio.CancelledError:
            # Never replay this job after shutdown/restart.
            raise
        except ReconnectStopped as exc:
            self._safe_result(device_id, generation, str(exc))
        except Exception as exc:
            self._safe_result(device_id, generation, "FAILED_" + type(exc).__name__)

    def _safe_result(self, device_id, generation, result):
        try:
            self._result(device_id, generation, result)
        except Exception as exc:
            write_masterlog("live_radio_reconnect_storage_failed", error_type=type(exc).__name__)

    async def _run_locked(self, device_id, generation):
        device, value = self._current(device_id, generation, {"PENDING"})
        client = self.client_factory(device.ip_address, device_id=device_id, request_purpose="live_radio_reconnect", trigger="confirmed_live_eos")

        async def identity():
            info = ET.fromstring(await client.get_xml("/info"))
            if info.tag != "info" or info.get("deviceID", "").upper() != device_id.upper():
                raise ReconnectStopped("IDENTITY_MISMATCH")
            return ET.tostring(info, encoding="unicode")

        async def read(path):
            self._current(device_id, generation, {"PENDING", "ATTEMPTED"})
            await identity()
            return await client.get_xml(path)

        def inactive(xml):
            root = ET.fromstring(xml)
            if root.tag != "nowPlaying" or root.get("source") != "INVALID_SOURCE":
                raise ReconnectStopped("USER_STATE_OR_SOURCE_CHANGED")

        before = {"/info": await identity()}
        before["/now_playing"] = await read("/now_playing")
        inactive(before["/now_playing"])
        before["/getZone"] = await read("/getZone")
        zone = ET.fromstring(before["/getZone"])
        if zone.tag != "zone" or zone.get("master") or zone.findall(".//member"):
            raise ReconnectStopped("ZONE_ACTIVE_OR_UNKNOWN")
        for path, root_name in (("/volume", "volume"), ("/presets", "presets"), ("/sources", "sources")):
            before[path] = await read(path)
            if ET.fromstring(before[path]).tag != root_name:
                raise ReconnectStopped("BACKUP_INVALID")
        volume = int(ET.fromstring(before["/volume"]).findtext("actualvolume", "-1"))
        if not 0 <= volume <= 100:
            raise ReconnectStopped("VOLUME_UNKNOWN")
        descriptor = StationDescriptor(**value["descriptor"])
        if not validate_outbound_http_url(descriptor.stream_url, public_only=True).ok:
            raise ReconnectStopped("STREAM_TARGET_BLOCKED")
        # Bounded, guarded redirect/codec probe. Do not use an endless GET or
        # reuse the old signed resolved URL. Overall deadline caps slow peers.
        probe = await asyncio.wait_for(self.probe(descriptor.stream_url), timeout=15)
        if probe.get("status") != "VALID" or not probe.get("reachable"):
            raise ReconnectStopped("STREAM_NOT_VERIFIED")
        resolved = probe.get("resolved_url", "")
        if not validate_outbound_http_url(resolved, public_only=True).ok:
            raise ReconnectStopped("RESOLVED_TARGET_BLOCKED")
        with self.session_factory() as db:
            location = station_location(replace(descriptor, stream_url_resolved=resolved), db=db, request_host=value["provider_host"])
            art_setting = db.query(Setting).filter_by(key=f"station_art_mode:{device_id}").one_or_none()
            art_mode = art_setting.value if art_setting else "radio_symbol"
        if not validate_outbound_http_url(location).ok:
            raise ReconnectStopped("PROVIDER_TARGET_BLOCKED")
        selection = content_item_xml(
            Station(name=descriptor.name, stream_url=descriptor.stream_url, image_url=descriptor.image_url), location,
            include_container_art=art_mode == "station_logo" and validate_logo_reference(descriptor.image_url)["valid"],
            empty_container_art=art_mode == "no_station_logo")
        backup = {"kind": "live_radio_reconnect", "http": before,
                  "sha256": {k: sha256(v.encode()).hexdigest() for k, v in before.items()}}
        with self.session_factory() as db:
            db.add(ConfigBackup(device_id=device_id, path="live_radio_reconnect/" + generation,
                                content=json.dumps(backup)))
            db.commit()  # A missing durable backup forbids the POST.
        inactive(await read("/now_playing"))
        final_zone = ET.fromstring(await read("/getZone"))
        if final_zone.tag != "zone" or final_zone.get("master") or final_zone.findall(".//member"):
            raise ReconnectStopped("ZONE_CHANGED")
        # Atomic single-use claim before POST; never reissue if response is lost.
        with self.session_factory() as db:
            row, current = state(db, device_id)
            if not row or current.get("generation") != generation or current.get("phase") != "PENDING":
                raise ReconnectStopped("SESSION_CHANGED")
            claimed = dict(current, phase="ATTEMPTED", attempts=[*current["attempts"], time.time()])
            changed = db.execute(update(RuntimeState).where(RuntimeState.id == row.id, RuntimeState.value == row.value)
                                 .values(value=json.dumps(claimed, sort_keys=True))).rowcount
            db.commit()
            if changed != 1:
                raise ReconnectStopped("ALREADY_CLAIMED")
        self._current(device_id, generation, {"ATTEMPTED"})
        await identity()
        # In-process safety: no event-loop yield between the last intent check
        # and invoking the transport other than the transport's own I/O.
        self._current(device_id, generation, {"ATTEMPTED"})
        await client.post_xml("/select", selection)
        # The new provider contract replaces generation; readback still checks
        # identity and protection but must not reject that expected change.
        result = "SENT_NOT_CONFIRMED"
        after = {}
        for _ in range(3):
            await asyncio.sleep(2)
            await identity()
            after["/now_playing"] = await client.get_xml("/now_playing")
            now = ET.fromstring(after["/now_playing"])
            content = now.find("ContentItem")
            if (now.get("source") == "LOCAL_INTERNET_RADIO" and now.findtext("playStatus") == "PLAY_STATE"
                    and content is not None and content.get("location") == location):
                result = "PLAYBACK_VERIFIED"
                break
            if now.get("source") == "STANDBY":
                result = "USER_STANDBY"; break
        await identity()
        after["/volume"] = await client.get_xml("/volume")
        after_volume = int(ET.fromstring(after["/volume"]).findtext("actualvolume", "-1"))
        if after_volume != volume:
            result += "_VOLUME_CHANGED"
        with self.session_factory() as db:
            row = db.query(ConfigBackup).filter_by(
                device_id=device_id, path="live_radio_reconnect/" + generation).one()
            saved = json.loads(row.content)
            saved.update(after_http=after, after_sha256={k: sha256(v.encode()).hexdigest() for k, v in after.items()},
                         readback_result=result)
            row.content = json.dumps(saved)
            db.commit()
        self._result(device_id, generation, result)
