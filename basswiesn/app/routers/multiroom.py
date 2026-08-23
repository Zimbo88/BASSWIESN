import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.models import Device, MultiroomScenario, PlayHistory, Preset, ScheduledAction, Station, utc_now
from basswiesn.app.routers import api as api_core
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.protected_devices import is_protected_device, require_unprotected_device
from basswiesn.app.services.action_journal import record_action
import xml.etree.ElementTree as ET

router = APIRouter(prefix="/api", tags=["multiroom"])


def _client_for(device: Device, *, purpose: str) -> SoundTouchClient:
    try:
        return SoundTouchClient(
            device.ip_address,
            device_id=device.device_id,
            request_purpose=purpose,
            trigger="webui",
        )
    except TypeError:
        # Preserve small test doubles while production always carries the
        # device identity into the central policy and write ledger.
        return SoundTouchClient(device.ip_address)


def split_csv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _stop_action(value) -> str:
    action = str(value or "stop_standby").strip().lower()
    if action not in {"stop", "standby", "stop_standby"}:
        raise HTTPException(status_code=400, detail="stop_action must be stop, standby or stop_standby")
    return action


def zone_payload(master: Device, members: list[Device]) -> str:
    root = ET.Element("zone", {"master": str(master.device_id)})
    for member in members:
        if member.device_id == master.device_id:
            continue
        node = ET.SubElement(root, "member", {"ipaddress": str(member.ip_address)})
        node.text = str(member.device_id)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


MULTIROOM_METHODS = [
    {
        "id": "zone",
        "label": "SoundTouch Zone",
        "recommended": True,
        "endpoint": "/setZone + /getZone",
        "purpose": "Mehrere Radios spielen dieselbe Quelle synchron.",
        "detail": "Der auf allen vorhandenen Geräten live bestätigte, lokale Hauptweg. Funktioniert ohne Bose-Cloud und ist für normale Nutzer die beste Wahl.",
    },
    {
        "id": "room_sync",
        "label": "Raum-Synchronisation",
        "recommended": False,
        "endpoint": "/rebroadcastlatencymode",
        "purpose": "Legt fest, wie ein Radio seine Audioverzögerung ausrichtet.",
        "detail": "SYNC_TO_ZONE ist für eine SoundTouch-Zone optimiert. SYNC_TO_ROOM richtet die Ausgabe auf den einzelnen Raum aus. Dieser Schalter bildet selbst keine Gruppe.",
    },
    {
        "id": "group",
        "label": "Bose Group Engine",
        "recommended": False,
        "endpoint": "/getGroup + /addGroup + /updateGroup + /removeGroup",
        "purpose": "Modernere interne Gruppen-Zustandsmaschine der Bose-App.",
        "detail": "Real in der Firmware vorhanden, aber komplexer und von Account-/Versionszustand abhängig. BASSWIESN nutzt für Endnutzer die stabilere Zone und hält Group als Kompatibilitäts-/Diagnoseweg bereit.",
    },
    {
        "id": "capabilities",
        "label": "Fähigkeiten erkennen",
        "recommended": False,
        "endpoint": "/capabilities + /sources",
        "purpose": "Liest nur, was Modell und aktuelle Quelle erlauben.",
        "detail": "Das ist keine Multiroom-Methode. Es verhindert lediglich, dass eine ungeeignete Quelle wie AirPlay mit multiroomallowed=false angeboten wird.",
    },
]


def _zone_summary(xml: str) -> dict:
    root = ET.fromstring(xml)
    return {
        "active": bool(root.attrib.get("master")),
        "master_device_id": root.attrib.get("master", ""),
        "sender_ip": root.attrib.get("senderIPAddress", ""),
        "sender_is_master": root.attrib.get("senderIsMaster", "").lower() == "true",
        "members": [{"device_id": (node.text or "").strip(), "ip_address": node.attrib.get("ipaddress", "")} for node in root.findall("member")],
    }


def _zone_contains_device(summary: dict, device: Device) -> bool:
    for member in summary.get("members") or []:
        if member.get("device_id") == device.device_id:
            return True
        if member.get("ip_address") == device.ip_address:
            return True
    return False


def _require_unprotected_devices(devices: list[Device], *, action: str) -> None:
    for device in devices:
        require_unprotected_device(device, action=action, requester="multiroom", method="POST", endpoint="/setZone")


def _devices_for_ids(db: Session, device_ids: list[str]) -> list[Device]:
    ids = list(dict.fromkeys(split_csv(device_ids)))
    if not ids:
        return []
    by_id = {
        device.device_id: device
        for device in db.query(Device).filter(Device.device_id.in_(ids)).all()
    }
    return [by_id[device_id] for device_id in ids if device_id in by_id]


def _known_devices_or_404(db: Session, device_ids, *, role: str) -> list[Device]:
    ids = list(dict.fromkeys(split_csv(device_ids)))
    devices = _devices_for_ids(db, ids)
    found = {device.device_id for device in devices}
    missing = [device_id for device_id in ids if device_id not in found]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown multiroom devices",
                "role": role,
                "missing_device_ids": missing,
            },
        )
    return devices


def _members_or_404(db: Session, member_ids, master: Device) -> list[Device]:
    ids = list(dict.fromkeys(split_csv(member_ids)))
    if master.device_id in ids:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "master cannot also be requested as a member",
                "master_device_id": master.device_id,
            },
        )
    return _known_devices_or_404(db, ids, role="member")


def _reject_protected_batch_ids(db: Session, device_ids: list[str], *, action: str) -> None:
    for device in _devices_for_ids(db, device_ids):
        require_unprotected_device(device, action=action, requester="multiroom_batch", method="POST", endpoint="batch")


async def _read_volume(device: Device) -> int | None:
    try:
        root = ET.fromstring(await _client_for(device, purpose="multiroom_volume_readback").get_xml("/volume"))
        return int(float(root.findtext("actualvolume") or root.findtext("targetvolume") or "-1"))
    except Exception:
        return None


def _optional_xml(value) -> tuple[ET.Element | None, str | None]:
    if isinstance(value, BaseException):
        return None, value.__class__.__name__
    try:
        return ET.fromstring(value), None
    except (ET.ParseError, TypeError, ValueError) as exc:
        return None, exc.__class__.__name__


def _int_value(*values) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            continue
    return None


def _volume_contract(root: ET.Element | None, error: str | None) -> dict:
    if root is None:
        return {"known": False, "actual": None, "target": None, "muted": None, "evidence": "/volume", "error": error}
    mute_text = root.findtext("mute") or root.attrib.get("mute")
    return {
        "known": True,
        "actual": _int_value(root.findtext("actualvolume"), root.attrib.get("actualvolume")),
        "target": _int_value(root.findtext("targetvolume"), root.attrib.get("targetvolume")),
        "muted": str(mute_text).lower() == "true" if mute_text not in (None, "") else None,
        "evidence": "/volume",
        "error": None,
    }


def _source_contract(now_playing: ET.Element | None, sources: ET.Element | None, error: str | None) -> dict:
    content = now_playing.find("ContentItem") if now_playing is not None else None
    source_id = ""
    if now_playing is not None:
        source_id = now_playing.attrib.get("source", "") or (content.attrib.get("source", "") if content is not None else "")
    capability = None
    if sources is not None and source_id:
        capability = next((node for node in sources.findall("sourceItem") if node.attrib.get("source", "") == source_id), None)
    return {
        "known": now_playing is not None,
        "source": source_id or None,
        "source_account_present": bool(content is not None and content.attrib.get("sourceAccount")),
        "play_status": now_playing.findtext("playStatus") if now_playing is not None else None,
        "station_name": now_playing.findtext("stationName") if now_playing is not None else None,
        "multiroom_allowed": (
            capability.attrib.get("multiroomallowed", "").lower() == "true"
            if capability is not None and "multiroomallowed" in capability.attrib
            else None
        ),
        "evidence": "/now_playing",
        "error": error,
    }


def _output_latency_contract(root: ET.Element | None, error: str | None) -> dict:
    value = None
    if root is not None:
        value = _int_value(
            root.attrib.get("outputLatency"),
            root.attrib.get("latency"),
            root.attrib.get("value"),
            root.findtext("outputLatency"),
            root.findtext("latency"),
            root.text,
        )
    return {
        "known": value is not None,
        "milliseconds": value,
        "evidence": "/outputLatency",
        "error": error,
    }


@router.get("/multiroom/methods")
async def multiroom_methods() -> list[dict]:
    return MULTIROOM_METHODS


@router.get("/multiroom/status/{device_id}")
async def multiroom_status(device_id: str, db: Session = Depends(get_db)) -> dict:
    device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    require_unprotected_device(device, action="multiroom_status", requester="multiroom", method="GET", endpoint="/getZone")
    client = SoundTouchClient(device.ip_address)
    results = await asyncio.gather(
        client.get_xml("/getZone"),
        client.get_xml("/rebroadcastlatencymode"),
        client.get_xml("/sources"),
        client.get_xml("/now_playing"),
        client.get_xml("/volume"),
        client.get_xml("/outputLatency"),
        return_exceptions=True,
    )
    if isinstance(results[0], BaseException):
        raise results[0]
    zone = _zone_summary(results[0])
    latency, latency_error = _optional_xml(results[1])
    sources, sources_error = _optional_xml(results[2])
    now_playing, now_playing_error = _optional_xml(results[3])
    volume_root, volume_error = _optional_xml(results[4])
    output_latency_root, output_latency_error = _optional_xml(results[5])
    source = _source_contract(now_playing, sources, now_playing_error or sources_error)
    volume = _volume_contract(volume_root, volume_error)
    output_latency = _output_latency_contract(output_latency_root, output_latency_error)
    master_id = zone["master_device_id"] or None
    configured_ids = [member["device_id"] for member in zone["members"] if member["device_id"]]
    if master_id:
        configured_ids.append(master_id)
    configured = {item.device_id: item for item in _devices_for_ids(db, configured_ids)}
    master_device = configured.get(master_id or "")
    master_contract = {
        "device_id": master_id,
        "configured": master_device is not None,
        "name": master_device.name if master_device is not None else None,
        "queried_device": master_id == device.device_id if master_id else False,
    }
    member_contracts = []
    for member in zone["members"]:
        configured_member = configured.get(member["device_id"])
        member_contracts.append(
            {
                **member,
                "configured": configured_member is not None,
                "name": configured_member.name if configured_member is not None else None,
                "volume": volume if member["device_id"] == device.device_id else {"known": False},
            }
        )
    clock = {
        "known": bool(master_id),
        "master_device_id": master_id,
        "sender_ip": zone["sender_ip"] or None,
        "sender_is_master": zone["sender_is_master"],
        "confidence": "INFERRED_FROM_TOPOLOGY" if master_id else "UNKNOWN",
        "evidence": "/getZone",
    }
    topology = {**zone, "evidence": "/getZone"}
    return {
        "device_id": device.device_id,
        "name": device.name,
        # Existing projections remain stable; the separated contracts below
        # are additive and do not imply one combined multiroom state.
        "zone": zone,
        "latency_mode": latency.attrib.get("mode", "") if latency is not None else "",
        "latency_controllable": latency.attrib.get("controllable", "").lower() == "true" if latency is not None else False,
        "sources": [
            {"source": node.attrib.get("source", ""), "status": node.attrib.get("status", ""), "multiroom_allowed": node.attrib.get("multiroomallowed", "").lower() == "true"}
            for node in (sources.findall("sourceItem") if sources is not None else [])
        ],
        "topology": topology,
        "master": master_contract,
        "members": member_contracts,
        "source": source,
        "clock": clock,
        "output_latency": output_latency,
        "volume": volume,
        "contracts": {
            "topology": topology,
            "master": master_contract,
            "members": member_contracts,
            "source": source,
            "clock": clock,
            "output_latency": output_latency,
            "volume": volume,
            "rebroadcast_latency_mode": {
                "known": latency is not None,
                "mode": latency.attrib.get("mode") if latency is not None else None,
                "controllable": latency.attrib.get("controllable", "").lower() == "true" if latency is not None else None,
                "evidence": "/rebroadcastlatencymode",
                "error": latency_error,
            },
        },
    }


@router.post("/multiroom/latency")
async def multiroom_latency(payload: dict, db: Session = Depends(get_db)) -> dict:
    mode = str(payload.get("mode") or "SYNC_TO_ZONE")
    if mode not in {"SYNC_TO_ZONE", "SYNC_TO_ROOM"}:
        raise HTTPException(status_code=400, detail="mode must be SYNC_TO_ZONE or SYNC_TO_ROOM")
    ids = list(dict.fromkeys(split_csv(payload.get("device_ids") or [])))
    devices = _known_devices_or_404(db, ids, role="latency target")
    _require_unprotected_devices(devices, action="multiroom_latency")
    results = []
    for device in devices:
        operation_id = uuid4().hex
        client = _client_for(device, purpose="multiroom_latency")
        before = await client.get_xml("/rebroadcastlatencymode")
        response = await client.post_xml("/rebroadcastlatencymode", f'<rebroadcastlatencymode mode="{mode}" />')
        current = await client.get_xml("/rebroadcastlatencymode")
        verified = f'mode="{mode}"' in current
        record_action(
            db,
            job_id=operation_id,
            device_id=device.device_id,
            ip_address=device.ip_address,
            action="multiroom_latency",
            trigger="webui",
            phase="VERIFIED" if verified else "READBACK_MISMATCH",
            requested_state={"mode": mode},
            before_state={"readback": before[:2048]},
            result="latency_readback_verified" if verified else "latency_readback_mismatch",
            readback={"readback": current[:2048]},
            verified=verified,
        )
        db.commit()
        results.append({"device_id": device.device_id, "name": device.name, "ok": verified, "response": response})
    return {"mode": mode, "results": results, "explanation": "ZONE synchronisiert eine SoundTouch-Gruppe; ROOM optimiert die Ausgabe eines einzelnen Raums und bildet keine Gruppe."}


@router.post("/multiroom/preview")
async def multiroom_preview(payload: dict, db: Session = Depends(get_db)) -> dict:
    master = db.query(Device).filter(Device.device_id == payload.get("master_device_id")).one_or_none()
    if master is None:
        raise HTTPException(status_code=404, detail="master device not found")
    member_ids = payload.get("member_device_ids", [])
    members = _members_or_404(db, member_ids, master)
    devices = [master, *members]
    protected = [device.device_id for device in devices if is_protected_device(device)]
    current = []
    if payload.get("read_volumes"):
        for device in devices:
            protected_device = is_protected_device(device)
            current.append({"device_id": device.device_id, "name": device.name, "ip_address": device.ip_address, "protected": protected_device, "volume": None if protected_device else await _read_volume(device), "read_skipped": "protected_device" if protected_device else None})
    return {"dry_run": True, "master": master.device_id, "members": [m.device_id for m in members], "xml": zone_payload(master, members), "preserve_volumes": bool(payload.get("preserve_volumes") or payload.get("no_volume_change")), "latency_mode": payload.get("latency_mode") or "SYNC_TO_ZONE", "current_volumes": current, "protected_devices": protected, "blocked": bool(protected)}


@router.post("/multiroom/set")
async def multiroom_set(payload: dict, db: Session = Depends(get_db)) -> dict:
    master = db.query(Device).filter(Device.device_id == payload.get("master_device_id")).one_or_none()
    if master is None:
        raise HTTPException(status_code=404, detail="master device not found")
    member_ids = payload.get("member_device_ids", [])
    members = _members_or_404(db, member_ids, master)
    if not members:
        raise HTTPException(status_code=400, detail="choose at least one additional radio")
    volume = int(payload.get("volume", 5))
    if volume < 0 or volume > 100:
        raise HTTPException(status_code=400, detail="volume must be 0..100")
    preserve_volumes = bool(payload.get("preserve_volumes") or payload.get("no_volume_change"))
    api_core._require_memory_checked(master, payload)
    latency_mode = str(payload.get("latency_mode") or "SYNC_TO_ZONE")
    if latency_mode not in {"SYNC_TO_ZONE", "SYNC_TO_ROOM"}:
        raise HTTPException(status_code=400, detail="latency_mode must be SYNC_TO_ZONE or SYNC_TO_ROOM")
    xml = zone_payload(master, members)
    if payload.get("dry_run", True):
        protected = [device.device_id for device in [master, *members] if is_protected_device(device)]
        return {
            "dry_run": True,
            "target": master.ip_address,
            "xml": xml,
            "memory_check": api_core._memory_check_plan(master),
            "preserve_volumes": preserve_volumes,
            "protected_devices": protected,
            "blocked": bool(protected),
        }
    _require_unprotected_devices([master, *members], action="multiroom_set")
    operation_id = uuid4().hex
    write_masterlog("multiroom_action", action="set", master_device_id=master.device_id, member_count=len(members))
    before_volumes = {device.device_id: await _read_volume(device) for device in [master, *members]} if preserve_volumes else {}
    before_zones: dict[str, object] = {}
    for device in [master, *members]:
        try:
            before_zones[device.device_id] = _zone_summary(
                await _client_for(device, purpose="multiroom_set_backup").get_xml("/getZone")
            )
        except Exception as exc:
            before_zones[device.device_id] = {"known": False, "error": exc.__class__.__name__}
    for device in [master, *members]:
        await _client_for(device, purpose="multiroom_set_latency").post_xml("/rebroadcastlatencymode", f'<rebroadcastlatencymode mode="{latency_mode}" />')
    response = await _client_for(master, purpose="multiroom_set_topology").post_xml("/setZone", xml)
    playback = None
    if not preserve_volumes:
        for device in [master, *members]:
            await _client_for(device, purpose="multiroom_set_volume").post_xml("/volume", f"<volume>{volume}</volume>")
    if payload.get("station_id"):
        from basswiesn.app.routers.stations_presets import play_station_on_device
        playback_payload = {"dry_run": False}
        playback = await play_station_on_device(master.device_id, int(payload["station_id"]), playback_payload, db)
        await asyncio.sleep(1.0)
    elif payload.get("preset_button"):
        key_payload = {"key": f"PRESET_{int(payload['preset_button'])}"}
        if not preserve_volumes:
            key_payload["safe_volume"] = volume
        playback = await api_core.send_key_command(master.device_id, key_payload, db)
        await asyncio.sleep(1.0)
    # A source start can restore each radio's stored startup volume. Re-apply
    # the requested group value immediately after playback begins.
    if not preserve_volumes:
        for device in [master, *members]:
            await _client_for(device, purpose="multiroom_set_volume_readjust").post_xml("/volume", f"<volume>{volume}</volume>")
    else:
        await asyncio.sleep(1.5)
    verification = []
    volume_warnings = []
    volume_warnings_seen: list[dict] = []
    volume_observations: list[dict] = []

    async def verify_zone_and_volumes() -> bool:
        nonlocal verification, volume_warnings, volume_observations
        verification = []
        volume_warnings = []
        volume_observations = []
        for device in [master, *members]:
            client = _client_for(device, purpose="multiroom_set_readback")
            current = await client.get_xml("/getZone")
            current_volume = await client.get_xml("/volume")
            summary = _zone_summary(current)
            actual_volume = int(ET.fromstring(current_volume).findtext("actualvolume", "-1"))
            before_volume = before_volumes.get(device.device_id)
            volume_changed = preserve_volumes and before_volume is not None and actual_volume != before_volume
            if volume_changed:
                volume_warnings.append({"device_id": device.device_id, "before": before_volume, "after": actual_volume})
            if preserve_volumes:
                volume_observations.append(
                    {
                        "device_id": device.device_id,
                        "before": before_volume,
                        "after": actual_volume,
                        "changed": volume_changed if before_volume is not None else None,
                    }
                )
            zone_ok = summary["master_device_id"] == master.device_id
            requested_volume_ok = True if preserve_volumes else actual_volume == volume
            verification.append({"device_id": device.device_id, "name": device.name, "ok": zone_ok and requested_volume_ok, "zone_ok": zone_ok, "zone": summary, "volume": actual_volume, "volume_before": before_volume, "volume_preserved": (not volume_changed) if preserve_volumes and before_volume is not None else None})
        return all(item["ok"] for item in verification)

    for verify_attempt in range(1, 5):
        verified = await verify_zone_and_volumes()
        for warning in volume_warnings:
            if warning not in volume_warnings_seen:
                volume_warnings_seen.append(warning)
        for item in verification:
            item["verify_attempt"] = verify_attempt
        if verified:
            break
        await asyncio.sleep(0.5)
    if preserve_volumes:
        write_masterlog(
            "multiroom_volume_preserve_observation",
            master_device_id=master.device_id,
            volume_observations=volume_observations,
            volume_warnings=volume_warnings_seen,
            automatic_volume_action="NONE",
        )
    if not all(item["ok"] for item in verification):
        record_action(
            db,
            job_id=operation_id,
            device_id=master.device_id,
            ip_address=master.ip_address,
            action="multiroom_set",
            trigger="webui",
            phase="READBACK_MISMATCH",
            requested_state={"master": master.device_id, "members": [item.device_id for item in members], "preserve_volumes": preserve_volumes, "volume": None if preserve_volumes else volume, "latency_mode": latency_mode},
            backup_ref=f"inline:multiroom:{operation_id}:before_state",
            before_state={"zones": before_zones, "volumes": before_volumes},
            result="zone_readback_not_verified",
            readback={"verification": verification, "volume_observations": volume_observations},
            verified=False,
        )
        db.commit()
        raise HTTPException(status_code=502, detail={"error": "zone was not confirmed by every radio", "verification": verification, "volume_warnings": volume_warnings_seen, "volume_observations": volume_observations, "automatic_volume_action": "NONE" if preserve_volumes else "SET_REQUESTED_VOLUME"})
    record_action(
        db,
        job_id=operation_id,
        device_id=master.device_id,
        ip_address=master.ip_address,
        action="multiroom_set",
        trigger="webui",
        phase="VERIFIED",
        requested_state={"master": master.device_id, "members": [item.device_id for item in members], "preserve_volumes": preserve_volumes, "volume": None if preserve_volumes else volume, "latency_mode": latency_mode},
        backup_ref=f"inline:multiroom:{operation_id}:before_state",
        before_state={"zones": before_zones, "volumes": before_volumes},
        result="zone_readback_verified",
        readback={"verification": verification, "volume_observations": volume_observations},
        verified=True,
    )
    db.commit()
    write_masterlog("multiroom_action_complete", action="set", master_device_id=master.device_id, verified=True)
    return {"dry_run": False, "target": master.ip_address, "master": master.name, "members": [m.name for m in members], "volume": None if preserve_volumes else volume, "preserve_volumes": preserve_volumes, "volume_warnings": volume_warnings_seen, "volume_observations": volume_observations, "volume_rollbacks": [], "automatic_volume_action": "NONE" if preserve_volumes else "SET_REQUESTED_VOLUME", "latency_mode": latency_mode, "playback": playback, "xml": xml, "response": response, "verification": verification}


@router.post("/multiroom/clear")
async def multiroom_clear(payload: dict, db: Session = Depends(get_db)) -> dict:
    master = db.query(Device).filter(Device.device_id == payload.get("master_device_id")).one_or_none()
    if master is None:
        raise HTTPException(status_code=404, detail="master device not found")
    api_core._require_memory_checked(master, payload)
    xml = zone_payload(master, [])
    if payload.get("dry_run", True):
        return {"dry_run": True, "target": master.ip_address, "xml": xml, "memory_check": api_core._memory_check_plan(master)}
    require_unprotected_device(master, action="multiroom_clear", requester="multiroom", method="POST", endpoint="/setZone")
    operation_id = uuid4().hex
    write_masterlog("multiroom_action", action="clear", master_device_id=master.device_id)
    client = _client_for(master, purpose="multiroom_clear")
    before = _zone_summary(await client.get_xml("/getZone"))
    response = await client.post_xml("/setZone", xml)
    current = await client.get_xml("/getZone")
    after = _zone_summary(current)
    cleared = not after["active"]
    record_action(
        db,
        job_id=operation_id,
        device_id=master.device_id,
        ip_address=master.ip_address,
        action="multiroom_clear",
        trigger="webui",
        phase="VERIFIED" if cleared else "READBACK_MISMATCH",
        requested_state={"master": master.device_id, "members": []},
        backup_ref=f"inline:multiroom:{operation_id}:before_state",
        before_state={"zone": before},
        result="zone_clear_verified" if cleared else "zone_clear_not_verified",
        readback={"zone": after},
        verified=cleared,
    )
    db.commit()
    return {"dry_run": False, "target": master.ip_address, "xml": xml, "response": response, "cleared": cleared}


async def _read_device_zone(device: Device) -> tuple[Device, dict | None, str]:
    if is_protected_device(device):
        return device, None, "protected device: read skipped"
    try:
        xml = await SoundTouchClient(device.ip_address).get_xml("/getZone")
        return device, _zone_summary(xml), ""
    except Exception as exc:
        return device, None, str(exc)


@router.post("/multiroom/clear-all")
async def multiroom_clear_all(payload: dict, db: Session = Depends(get_db)) -> dict:
    """Dissolve every discoverable zone; the user does not need to know its master."""
    write_masterlog("multiroom_action", action="clear_all")
    devices = db.query(Device).all()
    states = await asyncio.gather(*[_read_device_zone(device) for device in devices])
    by_id = {device.device_id: device for device in devices}
    master_ids = sorted({summary["master_device_id"] for _, summary, _ in states if summary and summary["active"]})
    results = []
    for master_id in master_ids:
        master = by_id.get(master_id)
        if master is None:
            results.append({"master_device_id": master_id, "ok": False, "error": "master is not configured in BASSWIESN"})
            continue
        if is_protected_device(master):
            results.append({"master_device_id": master_id, "name": master.name, "ok": False, "error": "protected device: clear-all skipped"})
            continue
        try:
            response = await SoundTouchClient(master.ip_address).post_xml("/setZone", zone_payload(master, []))
            verify = _zone_summary(await SoundTouchClient(master.ip_address).get_xml("/getZone"))
            results.append({"master_device_id": master_id, "name": master.name, "ok": not verify["active"], "response": response})
        except Exception as exc:
            results.append({"master_device_id": master_id, "name": master.name, "ok": False, "error": str(exc)})
    return {"cleared": bool(master_ids) and all(item["ok"] for item in results), "already_clear": not master_ids, "zones_found": len(master_ids), "results": results, "unreachable": [{"device_id": device.device_id, "name": device.name, "error": error} for device, summary, error in states if summary is None]}


@router.post("/multiroom/remove-device")
async def multiroom_remove_device(payload: dict, db: Session = Depends(get_db)) -> dict:
    """Remove one radio from the live zone only; never delete local device records."""
    from basswiesn.app.models import Setting
    lab = db.query(Setting).filter(Setting.key == "lab_mode").one_or_none()
    if lab is None or str(lab.value).lower() != "true":
        raise HTTPException(status_code=403, detail={"error": "experimental_lab_only", "experimental": True})
    if str(payload.get("confirmation") or "") != "REMOVE MEMBER":
        raise HTTPException(status_code=409, detail={"error": "confirmation_required", "confirmation": "REMOVE MEMBER", "experimental": True})
    target = api_core._device_or_404(db, str(payload.get("device_id") or ""))
    require_unprotected_device(target, action="multiroom_remove_device", requester="multiroom", method="POST", endpoint="/setZone")
    write_masterlog("multiroom_action", action="remove_device", device_id=target.device_id, radio_ip=target.ip_address)
    try:
        target_zone = _zone_summary(await SoundTouchClient(target.ip_address).get_xml("/getZone"))
    except Exception as exc:
        target_zone = {"active": False, "master_device_id": "", "members": [], "error": str(exc) or exc.__class__.__name__}
    master_id = target_zone["master_device_id"] if target_zone.get("active") else ""
    if target_zone["active"] and master_id == target.device_id:
        raise HTTPException(status_code=409, detail="Das Hauptradio kann nicht einzeln entfernt werden. Nutze 'Alle Gruppen auflösen' oder wähle zuerst ein anderes Hauptradio.")

    if not master_id:
        devices = db.query(Device).all()
        for candidate in devices:
            if candidate.device_id == target.device_id:
                continue
            if is_protected_device(candidate):
                continue
            try:
                candidate_zone = _zone_summary(await SoundTouchClient(candidate.ip_address).get_xml("/getZone"))
            except Exception:
                continue
            if candidate_zone["active"] and _zone_contains_device(candidate_zone, target):
                master_id = candidate_zone["master_device_id"] or candidate.device_id
                break
    if not master_id:
        if target_zone.get("error"):
            raise HTTPException(status_code=503, detail={"error": "Radio ist offline oder /getZone nicht erreichbar. Es wurde nicht aus BASSWIESN entfernt.", "device_id": target.device_id, "message": target_zone["error"]})
        return {"removed": False, "already_standalone": True, "device_id": target.device_id, "device_still_configured": True}

    master = api_core._device_or_404(db, master_id)
    require_unprotected_device(master, action="multiroom_remove_device_master", requester="multiroom", method="POST", endpoint="/setZone")
    master_zone = _zone_summary(await SoundTouchClient(master.ip_address).get_xml("/getZone"))
    if master_zone["master_device_id"] == target.device_id:
        raise HTTPException(status_code=409, detail="Das Hauptradio kann nicht einzeln entfernt werden. Nutze 'Alle Gruppen auflösen' oder wähle zuerst ein anderes Hauptradio.")
    remaining_members = [
        item for item in master_zone["members"]
        if item.get("device_id") != target.device_id and item.get("ip_address") != target.ip_address
    ]
    remaining_ids = [item["device_id"] for item in remaining_members if item.get("device_id")]
    remaining_by_id = {item.device_id: item for item in db.query(Device).filter(Device.device_id.in_(remaining_ids)).all()} if remaining_ids else {}
    remaining = []
    for item in remaining_members:
        device = remaining_by_id.get(item.get("device_id"))
        if device is None and item.get("ip_address"):
            device = db.query(Device).filter(Device.ip_address == item["ip_address"]).one_or_none()
        if device is not None and device.device_id != master.device_id:
            remaining.append(device)
    xml = zone_payload(master, remaining)
    response = await SoundTouchClient(master.ip_address).post_xml("/setZone", xml)
    await asyncio.sleep(0.7)
    try:
        verify_target = _zone_summary(await SoundTouchClient(target.ip_address).get_xml("/getZone"))
    except Exception as exc:
        verify_target = {"active": False, "master_device_id": "", "members": [], "error": str(exc) or exc.__class__.__name__}
    verify_master = _zone_summary(await SoundTouchClient(master.ip_address).get_xml("/getZone"))
    still_configured = db.query(Device).filter(Device.device_id == target.device_id).one_or_none() is not None
    removed = not _zone_contains_device(verify_master, target) and verify_master.get("master_device_id") != target.device_id
    write_masterlog("multiroom_action_complete", action="remove_device", device_id=target.device_id, master_device_id=master.device_id, removed=removed, device_still_configured=still_configured)
    return {"removed": removed, "device_id": target.device_id, "name": target.name, "master": master.name, "remaining": [item.name for item in remaining], "device_still_configured": still_configured, "response": response, "verify_target": verify_target, "verify_master": verify_master}


@router.get("/multiroom/recent-stations")
async def multiroom_recent_stations(db: Session = Depends(get_db)) -> list[dict]:
    ids: list[int] = []
    for row in db.query(PlayHistory).order_by(PlayHistory.started_at.desc()).limit(300).all():
        if row.station_id and row.station_id not in ids:
            ids.append(row.station_id)
        if len(ids) >= 30:
            break
    if len(ids) < 30:
        for row in db.query(Preset).order_by(Preset.updated_at.desc()).all():
            if row.station_id and row.station_id not in ids:
                ids.append(row.station_id)
            if len(ids) >= 30:
                break
    stations = {row.id: row for row in db.query(Station).filter(Station.id.in_(ids)).all()} if ids else {}
    return [{"id": station_id, "name": stations[station_id].name, "stream_url": stations[station_id].stream_url} for station_id in ids if station_id in stations]


@router.get("/schedules")
async def schedules(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(ScheduledAction).order_by(ScheduledAction.start_time, ScheduledAction.name).all()
    return [
        {
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
            "dry_run": bool(row.dry_run),
        }
        for row in rows
    ]


@router.post("/schedules")
async def create_schedule(payload: dict, db: Session = Depends(get_db)) -> dict:
    device_ids = split_csv(payload.get("device_ids") or [])
    member_ids = split_csv(payload.get("multiroom_member_ids") or [])
    batch_ids = list(dict.fromkeys([*device_ids, str(payload.get("multiroom_master_id") or ""), *member_ids]))
    _reject_protected_batch_ids(db, batch_ids, action="schedule_create")
    row = ScheduledAction(
        name=payload.get("name", ""),
        enabled=1 if payload.get("enabled", True) else 0,
        start_time=payload.get("start_time", ""),
        end_time=payload.get("end_time", ""),
        days=payload.get("days", "daily"),
        device_ids=",".join(device_ids),
        station_id=int(payload["station_id"]) if payload.get("station_id") else None,
        preset_button=int(payload["preset_button"]) if payload.get("preset_button") else None,
        volume=int(payload["volume"]) if payload.get("volume") not in (None, "") else None,
        multiroom_master_id=payload.get("multiroom_master_id", ""),
        multiroom_member_ids=",".join(member_ids),
        stop_action=_stop_action(payload.get("stop_action")),
        dry_run=1 if payload.get("dry_run", True) else 0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    write_masterlog("alarm_timer_create", schedule_id=row.id, name=row.name, enabled=bool(row.enabled), start_time=row.start_time, end_time=row.end_time, station_id=row.station_id, preset_button=row.preset_button)
    return {"id": row.id}


def _schedule_or_404(db: Session, schedule_id: int) -> ScheduledAction:
    row = db.query(ScheduledAction).filter(ScheduledAction.id == schedule_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return row


def _update_schedule_row(row: ScheduledAction, payload: dict) -> None:
    if "name" in payload:
        row.name = payload.get("name", "")
    if "enabled" in payload:
        row.enabled = 1 if payload.get("enabled") else 0
    if "start_time" in payload:
        row.start_time = payload.get("start_time", "")
    if "end_time" in payload:
        row.end_time = payload.get("end_time", "")
    if "days" in payload:
        row.days = payload.get("days", "daily")
    if "device_ids" in payload:
        row.device_ids = ",".join(split_csv(payload.get("device_ids") or []))
    if "station_id" in payload:
        row.station_id = int(payload["station_id"]) if payload.get("station_id") else None
    if "preset_button" in payload:
        row.preset_button = int(payload["preset_button"]) if payload.get("preset_button") else None
    if "volume" in payload:
        row.volume = int(payload["volume"]) if payload.get("volume") not in (None, "") else None
    if "multiroom_master_id" in payload:
        row.multiroom_master_id = payload.get("multiroom_master_id", "")
    if "multiroom_member_ids" in payload:
        row.multiroom_member_ids = ",".join(split_csv(payload.get("multiroom_member_ids") or []))
    if "stop_action" in payload:
        row.stop_action = _stop_action(payload.get("stop_action"))
    if "dry_run" in payload:
        row.dry_run = 1 if payload.get("dry_run") else 0


@router.post("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, payload: dict, db: Session = Depends(get_db)) -> dict:
    row = _schedule_or_404(db, schedule_id)
    candidate_device_ids = split_csv(payload.get("device_ids") if "device_ids" in payload else row.device_ids)
    candidate_member_ids = split_csv(payload.get("multiroom_member_ids") if "multiroom_member_ids" in payload else row.multiroom_member_ids)
    candidate_master_id = str(payload.get("multiroom_master_id") if "multiroom_master_id" in payload else row.multiroom_master_id or "")
    _reject_protected_batch_ids(db, list(dict.fromkeys([*candidate_device_ids, candidate_master_id, *candidate_member_ids])), action="schedule_update")
    _update_schedule_row(row, payload)
    db.commit()
    write_masterlog("alarm_timer_update", schedule_id=row.id, name=row.name, enabled=bool(row.enabled), start_time=row.start_time, end_time=row.end_time, station_id=row.station_id, preset_button=row.preset_button)
    return {"id": row.id, "updated": True}


@router.post("/schedules/{schedule_id}/enable")
async def enable_schedule(schedule_id: int, payload: dict, db: Session = Depends(get_db)) -> dict:
    row = _schedule_or_404(db, schedule_id)
    row.enabled = 1 if payload.get("enabled", True) else 0
    db.commit()
    write_masterlog("alarm_timer_enabled" if row.enabled else "alarm_timer_disabled", schedule_id=row.id, name=row.name)
    return {"id": row.id, "enabled": bool(row.enabled)}


@router.post("/schedules/{schedule_id}/trigger")
async def trigger_schedule_now(schedule_id: int, payload: dict, db: Session = Depends(get_db)) -> dict:
    from basswiesn.app.services.alarm_engine import trigger_schedule

    row = _schedule_or_404(db, schedule_id)
    _reject_protected_batch_ids(db, list(dict.fromkeys([*split_csv(row.device_ids), row.multiroom_master_id, *split_csv(row.multiroom_member_ids)])), action="schedule_trigger")
    return await trigger_schedule(row, db, trigger="manual", force_dry_run=payload.get("dry_run"))


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    row = _schedule_or_404(db, schedule_id)
    write_masterlog("alarm_timer_disabled", schedule_id=row.id, name=row.name, deleted=True)
    db.delete(row)
    db.commit()
    return {"id": schedule_id, "deleted": True}


@router.get("/multiroom/scenarios")
async def multiroom_scenarios(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(MultiroomScenario).order_by(MultiroomScenario.name).all()
    return [{"id": row.id, "name": row.name, "master_device_id": row.master_device_id, "member_device_ids": split_csv(row.member_device_ids), "station_id": row.station_id, "volume": row.volume, "preserve_volumes": bool(getattr(row, "preserve_volumes", False)), "trigger_device_id": row.trigger_device_id, "trigger_button": row.trigger_button, "description": row.description, "preset_type": "BASSWIESN_MULTIROOM_PRESET", "stored_on_radio": False, "requires_basswiesn": True, "activation_contract": "MANUAL_WEBUI", "hardware_button_activation": "NOT_IMPLEMENTED"} for row in rows]


@router.post("/multiroom/scenarios")
async def save_multiroom_scenario(payload: dict, db: Session = Depends(get_db)) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="scenario name is required")
    row = db.query(MultiroomScenario).filter(MultiroomScenario.name == name).one_or_none()
    if row is None:
        row = MultiroomScenario(name=name)
        db.add(row)
    member_ids = split_csv(payload.get("member_device_ids") or [])
    master_id = str(payload.get("master_device_id") or "")
    master = db.query(Device).filter(Device.device_id == master_id).one_or_none() if master_id else None
    if master_id and master is None:
        raise HTTPException(status_code=404, detail={"error": "unknown multiroom master", "missing_device_ids": [master_id]})
    if master is not None:
        _members_or_404(db, member_ids, master)
    else:
        _known_devices_or_404(db, member_ids, role="member")
    _reject_protected_batch_ids(db, list(dict.fromkeys([str(payload.get("master_device_id") or ""), *member_ids, str(payload.get("trigger_device_id") or "")])), action="multiroom_scenario_save")
    row.master_device_id = payload.get("master_device_id", "")
    row.member_device_ids = ",".join(member_ids)
    row.station_id = int(payload["station_id"]) if payload.get("station_id") else None
    row.volume = int(payload["volume"]) if payload.get("volume") not in (None, "") else None
    row.preserve_volumes = bool(payload.get("preserve_volumes") or payload.get("no_volume_change"))
    row.trigger_device_id = payload.get("trigger_device_id", "")
    row.trigger_button = int(payload["trigger_button"]) if payload.get("trigger_button") else None
    row.description = payload.get("description", "")
    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "preset_type": "BASSWIESN_MULTIROOM_PRESET", "stored_on_radio": False, "requires_basswiesn": True, "activation_contract": "MANUAL_WEBUI", "hardware_button_activation": "NOT_IMPLEMENTED"}


@router.post("/multiroom/scenarios/{scenario_id}/preview")
async def preview_multiroom_scenario(scenario_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.query(MultiroomScenario).filter(MultiroomScenario.id == scenario_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    master = api_core._device_or_404(db, row.master_device_id)
    members = _members_or_404(db, split_csv(row.member_device_ids), master)
    station_name, stream_url = api_core._station_summary(db, row.station_id)
    protected = [device.device_id for device in [master, *members] if is_protected_device(device)]
    preserve_volumes = bool(getattr(row, "preserve_volumes", False))
    current_volumes = []
    if preserve_volumes:
        for device in [master, *members]:
            current_volumes.append({"device_id": device.device_id, "name": device.name, "volume": None if is_protected_device(device) else await _read_volume(device), "read_skipped": "protected_device" if is_protected_device(device) else None})
    return {"dry_run": True, "scenario": row.name, "preset_type": "BASSWIESN_MULTIROOM_PRESET", "stored_on_radio": False, "requires_basswiesn": True, "activation_contract": "MANUAL_WEBUI", "hardware_button_activation": "NOT_IMPLEMENTED", "zone_xml": zone_payload(master, members), "station": station_name, "stream_url": stream_url, "volume": row.volume, "preserve_volumes": preserve_volumes, "current_volumes": current_volumes, "protected_devices": protected, "blocked": bool(protected), "trigger": {"device_id": row.trigger_device_id, "button": row.trigger_button, "active": False, "note": "Zuordnung wird nur gespeichert; kein automatischer Hardwaretasten-Consumer ist aktiv."}, "memory_check": api_core._memory_check_plan(master)}


@router.post("/multiroom/scenarios/{scenario_id}/activate")
async def activate_multiroom_scenario(scenario_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.query(MultiroomScenario).filter(MultiroomScenario.id == scenario_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    result = await multiroom_set({
        "master_device_id": row.master_device_id,
        "member_device_ids": split_csv(row.member_device_ids),
        "station_id": row.station_id,
        "volume": row.volume if row.volume is not None else 5,
        "preserve_volumes": bool(getattr(row, "preserve_volumes", False)),
        "latency_mode": "SYNC_TO_ZONE",
        "dry_run": False,
        "memory_checked": True,
    }, db)
    return {"scenario": row.name, "preset_type": "BASSWIESN_MULTIROOM_PRESET", "activation_contract": "MANUAL_WEBUI", "activated": True, "result": result}


@router.delete("/multiroom/scenarios/{scenario_id}")
async def delete_multiroom_scenario(scenario_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.query(MultiroomScenario).filter(MultiroomScenario.id == scenario_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    name = row.name
    db.delete(row)
    db.commit()
    write_masterlog("multiroom_scenario_deleted", scenario_id=scenario_id, name=name)
    return {"id": scenario_id, "name": name, "deleted": True, "preset_type": "BASSWIESN_MULTIROOM_PRESET"}


@router.delete("/schedules")
async def clear_schedules(db: Session = Depends(get_db)) -> dict:
    deleted = db.query(ScheduledAction).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted}
