"""Single-radio /select transaction, not an OLED overlay or audible-audio test.

Only the existing LOCAL_INTERNET_RADIO contract is accepted. A source selection
can affect other speakers, so an existing multiroom topology blocks this route.
External controllers cannot be locked: rechecks narrow, not eliminate, races.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any, cast
from uuid import uuid4
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from basswiesn.app.models import ConfigBackup, Device
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_settings_writer import ClientFactory, IdentityMismatch, verify_settings_identity
from basswiesn.app.services.device_state import load_runtime_state, runtime_from_now_playing, save_runtime_state
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.network_security import validate_outbound_http_url
from basswiesn.app.services.zone_changes import Zone


class SelectionError(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(str(result["code"]))


def _root(xml: str, tag: str, identity: str = "") -> ET.Element:
    if not xml or len(xml.encode()) > 131072:
        raise ValueError("missing or excessive XML")
    root = cast(ET.Element, SafeXML.fromstring(xml, forbid_dtd=True))
    if root.tag != tag:
        raise ValueError("unexpected XML root")
    claimed = root.get("deviceID")
    if identity and claimed is not None and claimed.strip().upper() != identity.upper():
        raise IdentityMismatch("readback belongs to another radio")
    return root


@dataclass(frozen=True)
class Selection:
    source: str
    source_account: str
    location: str
    play_status: str = ""

    @classmethod
    def content(cls, node: ET.Element) -> Selection:
        return cls(node.get("source", ""), node.get("sourceAccount", ""), node.get("location", ""))

    @classmethod
    def read(cls, xml: str, identity: str, *, allow_pending: bool = False) -> Selection:
        root = _root(xml, "nowPlaying", identity)
        items = root.findall("ContentItem")
        statuses = root.findall("playStatus")
        if len(items) > 1 or len(statuses) > 1:
            raise ValueError("ambiguous playback observation")
        item = cls.content(items[0]) if items else cls("", "", "")
        source = root.get("source", "")
        if not source or (item.source and item.source != source):
            raise ValueError("missing or contradictory source")
        if allow_pending and source == "LOCAL_INTERNET_RADIO" and (not items or not item.source):
            # A factory-reset SCM can publish the new source before its
            # ContentItem exists. Preserve the missing identity, not the
            # requested station. This internal post-command observation only
            # permits the caller's guarded volume/mute correction while its
            # provider withholds audio; it cannot match a requested selection
            # or prove PLAY_STATE. Ordinary/pre-write reads remain strict.
            if statuses and not (statuses[0].text or ""):
                raise ValueError("empty playback observation")
            return cls(source, "", "", "UNCONFIRMED_STATE")
        # 27.0.6 SCM and SM2 radios omit playStatus in an explicit STANDBY
        # response. This is an observed stopped source, not a failed read or
        # permission to guess the state of an active/unknown source.
        # The source-level INVALID_SOURCE error has the same sparse shape;
        # preserve it as a failure, never as a playable content identity.
        if not statuses:
            if len(items) != 1 or item.source != source:
                raise ValueError("missing playback observation")
            if source in {"STANDBY", "INVALID_SOURCE"}:
                status = "STOP_STATE" if source == "STANDBY" else "INVALID_SOURCE"
            elif allow_pending:
                # Internal observation only, after our accepted playback action.
                # Never a vendor state or evidence of successful playback.
                status = "UNCONFIRMED_STATE"
            else:
                raise ValueError("missing playback observation")
        else:
            status = statuses[0].text or ""
            if not status or (source == "STANDBY" and status != "STOP_STATE"):
                raise ValueError("empty or contradictory playback observation")
        return cls(source, item.source_account, item.location, status)

    def same_content(self, requested: Selection) -> bool:
        return (self.source, self.source_account, self.location) == (
            requested.source, requested.source_account, requested.location)


def _volume(xml: str, identity: str) -> tuple[int, bool]:
    root = _root(xml, "volume", identity)
    values = [root.findall(tag) for tag in ("actualvolume", "muteenabled")]
    if any(len(nodes) != 1 for nodes in values):
        raise ValueError("actual volume/mute missing or ambiguous")
    actual, mute = values[0][0].text or "", values[1][0].text or ""
    if not actual.isascii() or not actual.isdecimal() or not 0 <= int(actual) <= 100 or mute not in {"true", "false"}:
        raise ValueError("unknown volume or mute")
    return int(actual), mute == "true"


def _source_ready(xml: str, identity: str, desired: Selection) -> None:
    root = _root(xml, "sources", identity)
    matches = [node for node in root if node.tag in {"source", "sourceItem"}
               and (node.get("source"), node.get("sourceAccount", "")) == (desired.source, desired.source_account)]
    if len(matches) != 1 or matches[0].get("status") != "READY":
        raise ValueError("requested source/account not uniquely READY")


async def select_direct(
    db: Session, device: Device, xml: str, *, client_factory: ClientFactory,
    guard: Callable[[Session, Device], None], expected_previous: object = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "job_id": f"direct-select-{uuid4().hex}", "device_id": device.device_id,
        "target": device.ip_address, "dry_run": False, "path": "/select",
        "audible_audio_verified": False, "automatic_retry": False, "automatic_rollback": False,
    }
    attempted, backup_ref, code = False, "", "SELECTION_PREFLIGHT_FAILED"
    async with device_transaction([device.device_id]):
        guard(db, device)  # protection before constructing the transport
        frozen = device_fingerprint(device)

        def unchanged_target() -> None:
            db.refresh(device)
            if device_fingerprint(device) != frozen:
                raise IdentityMismatch("stored device target changed")
            guard(db, device)

        try:
            item = _root(xml, "ContentItem")
            desired = Selection.content(item)
            if desired.source != "LOCAL_INTERNET_RADIO" or desired.source_account or item.get("type") != "stationurl":
                raise ValueError("unsupported selection contract")
            # Validate the instruction's URI without fetching any audio/artwork.
            # The radio's later DNS/redirect behavior is not controlled by us.
            urls = [desired.location, *(node.text or "" for node in item.findall("containerArt"))]
            if not desired.location or any(url and not validate_outbound_http_url(url).ok for url in urls):
                raise ValueError("unsafe or unresolved media/artwork URL")
            client = client_factory(frozen[1], device_id=frozen[0], request_purpose="direct_select",
                                    trigger="explicit_display_selection")

            async def snapshot() -> tuple[dict[str, str], Selection, Zone, tuple[int, bool]]:
                unchanged_target()
                raw = {"/info": await client.get_xml("/info")}
                _root(raw["/info"], "info", frozen[0])
                verify_settings_identity(device, raw["/info"])
                for path in ("/sources", "/getZone", "/now_playing", "/volume"):
                    unchanged_target()
                    raw[path] = await client.get_xml(path)
                unchanged_target()
                _source_ready(raw["/sources"], frozen[0], desired)
                zone = Zone.parse(raw["/getZone"], device_id=frozen[0])
                if not zone.standalone(frozen[0]):
                    raise ValueError("single-radio selection cannot affect a zone")
                return raw, Selection.read(raw["/now_playing"], frozen[0]), zone, _volume(raw["/volume"], frozen[0])

            raw, before, zone, volume = await snapshot()
            if expected_previous is not None and (
                not isinstance(expected_previous, dict) or expected_previous != asdict(before)
            ):
                code = "WRITE_PRECONDITION_FAILED"
                raise ValueError("selection differs from caller snapshot")
            code = "SELECTION_BACKUP_FAILED"
            content = json.dumps({"schema": 1, "device_id": frozen[0], "http": raw}, sort_keys=True)
            digest = sha256(content.encode()).hexdigest()
            backup = ConfigBackup(device_id=frozen[0], path="http8090:direct-select-start", content=content)
            db.add(backup)
            db.flush()
            identifier = backup.id
            backup_ref = f"config-backup:{identifier}:sha256:{digest}"
            result["backup_ref"] = backup_ref
            result["rollback_scope"] = "MANUAL_SELECTION_RECONCILIATION"
            record_action(db, job_id=result["job_id"], device_id=frozen[0], ip_address=frozen[1],
                          action="direct_select", trigger="explicit_display_selection", phase="direct_select_intent",
                          before_state=asdict(before), requested_state=asdict(desired), backup_ref=backup_ref,
                          rollback_ref=backup_ref, result="PREPARED", verified=False)
            db.commit()

            code = "WRITE_PRECONDITION_FAILED"
            _, current, current_zone, current_volume = await snapshot()
            if (current, current_zone, current_volume) != (before, zone, volume):
                raise ValueError("radio state changed after START")
            db.expire_all()
            stored = db.get(ConfigBackup, identifier)
            if stored is None or stored.device_id != frozen[0] or sha256(stored.content.encode()).hexdigest() != digest:
                raise ValueError("missing or changed backup")
            unchanged_target()
            code = "SELECTION_READBACK_FAILED"
            attempted = True
            uncertain = False
            try:
                await client.post_xml("/select", xml)
            except Exception:
                uncertain = True  # a lost reply is not permission to resend
            raw, actual, after_zone, after_volume = await snapshot()
            result["readback"] = asdict(actual)
            if not actual.same_content(desired) or after_zone != zone or after_volume != volume:
                code = "WRITE_READBACK_MISMATCH"
                raise ValueError("selected content, topology or volume differs")
            verified = actual.play_status == "PLAY_STATE"
            pending = actual.play_status in {"BUFFERING", "BUFFERING_STATE"}
            if not verified and not pending:
                code = "PLAYBACK_NOT_CONFIRMED"
                raise ValueError("selection is not playing or buffering")
            status = "VERIFIED" if verified else "PENDING_READBACK"
            code = "SELECTION_RESULT_COMMIT_FAILED"
            record_action(db, job_id=result["job_id"], device_id=frozen[0], ip_address=frozen[1],
                          action="direct_select", trigger="explicit_display_selection", phase="direct_select_readback",
                          before_state=asdict(before), readback=asdict(actual), backup_ref=backup_ref,
                          rollback_ref=backup_ref, result=status, verified=verified)
            # Cache only observed fields. Keep the ledger and cache in one commit.
            _, state = load_runtime_state(db, frozen[0])
            state.update(runtime_from_now_playing(raw["/now_playing"]))
            state["current_preset"] = None
            save_runtime_state(db, frozen[0], state, commit=False)
            db.commit()
            return {**result, "ok": verified, "status": status, "verified": verified,
                    "transport_uncertain": uncertain, "reconciliation_required": pending,
                    "note": "Radio source/content and playback state read back; audible output not verified."}
        except (Exception, asyncio.CancelledError) as exc:
            db.rollback()
            code = "IDENTITY_MISMATCH" if isinstance(exc, IdentityMismatch) else code
            status = "DIVERGED" if attempted else "BLOCKED"
            record_transport_attempt(
                device_id=frozen[0], ip_address=frozen[1], action="direct_select", trigger="explicit_display_selection",
                phase="direct_select_diverged" if attempted else "direct_select_blocked", result=status,
                job_id=result["job_id"], backup_ref=backup_ref, rollback_ref=backup_ref,
                error_category=code, readback=result.get("readback"), verified=False)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SelectionError({**result, "ok": False, "verified": False, "status": status, "code": code,
                "reconciliation_required": attempted,
                "message": "No automatic retry or rollback. Re-read the radio and reconcile the saved START state."}) from None
