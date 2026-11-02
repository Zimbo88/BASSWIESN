"""Bounded LAB commands with no invented persistence or network-switch proof.

Native addStation can start audio; adding Wi-Fi can remove the current endpoint.
Both share one explicit, single-device transaction boundary, but have different
preconditions and observations. A PLAY_STATE or active SSID is not proof that
the new provider entry/profile survived reboot. No automatic rollback can safely
reconstruct provider state or a previous Wi-Fi credential from these snapshots.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from typing import Any, Literal
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from basswiesn.app.core.redaction import redact_text
from basswiesn.app.models import ConfigBackup, Device, DeviceActionJournal
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_settings_writer import ClientFactory, IdentityMismatch, verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.direct_selection import Selection, _source_ready
from basswiesn.app.services.live_radio_reconnect import cancel
from basswiesn.app.services.maintenance_reboot import PATHS, RebootSnapshot, _root
from basswiesn.app.services.wireless_diagnostics import network_observation


def _text(value: object, *, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not value and not empty):
        raise ValueError("invalid or missing text field")
    # XML 1.0 permits TAB/LF/CR, but not other C0 controls or surrogate scalars.
    if any(ord(ch) < 32 and ch not in "\t\n\r" or 0xD800 <= ord(ch) <= 0xDFFF
           or ord(ch) in {0xFFFE, 0xFFFF} for ch in value):
        raise ValueError("invalid XML text")
    return value


@dataclass(frozen=True)
class ManualCommand:
    kind: Literal["native_station", "wireless_profile"]
    body: str = field(repr=False)
    source: str = ""
    account: str = field(default="", repr=False)
    ssid: str = field(default="", repr=False)

    @property
    def path(self) -> str:
        return "/addStation" if self.kind == "native_station" else "/addWirelessProfile"

    @classmethod
    def station(cls, xml: str) -> ManualCommand:
        root = _root(xml, "addStation")
        _text(root.get("source", ""), maximum=64)
        _text(root.get("sourceAccount", ""), maximum=128, empty=True)
        _text(root.get("token", ""), maximum=256)
        if len(root.findall("name")) != 1:
            raise ValueError("station name must be unambiguous")
        _text(root.findtext("name", ""), maximum=128)
        return cls("native_station", xml, root.get("source", ""), root.get("sourceAccount", ""))

    @classmethod
    def wireless(cls, payload: Mapping[str, object]) -> ManualCommand:
        ssid = _text(payload.get("ssid"), maximum=32).strip()
        password = _text(payload.get("password", ""), maximum=63, empty=True)
        security = _text(payload.get("security_type", "wpa_or_wpa2"), maximum=32).lower()
        if not ssid or len(ssid.encode()) > 32 or any(ch in ssid + password for ch in '<>"&'):
            raise ValueError("SSID/password must fit the established XML contract")
        if security not in {"none", "wep", "wpa_or_wpa2"} or (security != "none" and not password):
            raise ValueError("invalid wireless security or missing password")
        root = ET.Element("AddWirelessProfile", {"timeout": "90"})
        attributes = {"ssid": ssid}
        if security != "none":
            attributes.update(password=password, securityType=security)
        ET.SubElement(root, "profile", attributes)
        return cls("wireless_profile", ET.tostring(root, encoding="unicode"), ssid=ssid)

    def preview(self) -> dict[str, Any]:
        # Never return the Wi-Fi credential or the opaque native station token.
        return {"dry_run": True, "path": self.path, "http_method": "POST",
                "xml": redact_text(self.body), "confirmation_required": "YES", "mode_required": "lab",
                "manual_recovery_acknowledgement_required": True, "persistence_verified": False,
                "warning": "May start playback or disconnect this radio. Manual recovery may be required."}


class ManualCommandError(Exception):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail["code"]))


async def execute_manual_command(
    db: Session, device: Device, command: ManualCommand, *, client_factory: ClientFactory,
    guard: Callable[[Session, Device], None], operation_id: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise ValueError("operation_id must be 32 lowercase hexadecimal characters")
    frozen = device_fingerprint(device)
    identity, address = frozen[:2]
    kind = command.kind
    result: dict[str, Any] = {"device_id": identity, "dry_run": False, "path": command.path,
        "job_id": f"{kind}-{identity}-{operation_id}", "verified": False, "persistence_verified": False,
        "audible_audio_verified": False, "automatic_retry": False, "automatic_rollback": False,
        "reconciliation_required": False, "backup_scope": "observed_state_not_full_provider_or_wifi_credentials"}
    attempted, backup_ref, code = False, "", "MANUAL_COMMAND_PREFLIGHT_FAILED"
    try:
        guard(db, device)
        async with device_transaction([identity]):
            previous = db.query(DeviceActionJournal).filter_by(job_id=result["job_id"]).order_by(DeviceActionJournal.id.desc()).first()
            if previous is not None:
                # Reusing a key never authorizes another write, even with a
                # changed payload. Do not persist a password hash to compare it.
                return {**result, "ok": False, "status": "ALREADY_RECORDED", "code": "OPERATION_ALREADY_RECORDED",
                        "backup_ref": previous.backup_ref, "reconciliation_required": True,
                        "message": "Operation already recorded. No transport opened or command repeated; inspect the previous result."}

            def unchanged() -> None:
                db.refresh(device)
                if device_fingerprint(device) != frozen:
                    raise IdentityMismatch("stored target changed")
                guard(db, device)

            unchanged()
            _root(frozen[4], "info", identity)
            verify_settings_identity(device, frozen[4])
            client = client_factory(address, device_id=identity, request_purpose=kind, trigger="manual_lab")

            async def snapshot() -> tuple[RebootSnapshot, dict[str, Any]]:
                unchanged()
                raw = {"/info": await client.get_xml("/info")}
                _root(raw["/info"], "info", identity)
                verify_settings_identity(device, raw["/info"])
                for path in PATHS:
                    unchanged()
                    raw[path] = await client.get_xml(path)
                state = RebootSnapshot.parse(raw, identity)
                if not state.zone.standalone(identity):
                    raise ValueError("manual command requires a standalone radio")
                extra: dict[str, Any] = {}
                if kind == "native_station":
                    unchanged()
                    sources = await client.get_xml("/sources")
                    _source_ready(sources, identity, Selection(command.source, command.account, ""))
                else:
                    unchanged()
                    network = await client.get_xml("/networkInfo")
                    unchanged()
                    active = await client.get_xml("/getActiveWirelessProfile")
                    extra = network_observation(network, active, identity)
                unchanged()
                return state, extra

            before, network_before = await snapshot()
            # Provider snapshots are local private recovery evidence. Wi-Fi
            # responses are allowlisted before persistence; never store request
            # bodies/passwords or an opaque station token in the intent ledger.
            serialized = json.dumps({"schema": 1, "kind": kind,
                "raw": {path: redact_text(value, anonymize_ips=False) for path, value in before.raw.items()},
                "network": network_before}, sort_keys=True)
            digest = sha256(serialized.encode()).hexdigest()
            backup = ConfigBackup(device_id=identity, path=f"http8090:{kind}-start", content=serialized)
            db.add(backup)
            db.flush()
            backup_id = backup.id
            backup_ref = f"config-backup:{backup_id}:sha256:{digest}"
            result["backup_ref"] = backup_ref

            def ledger(phase: str) -> None:
                record_action(db, job_id=result["job_id"], device_id=identity, ip_address=address,
                    action=kind, trigger="manual_lab", phase=phase, backup_ref=backup_ref,
                    requested_state={"path": command.path}, verified=False, result="PENDING_READBACK")
                db.commit()

            def integrity() -> None:
                unchanged()
                row = db.get(ConfigBackup, backup_id, populate_existing=True)
                if (row is None or row.device_id != identity or row.path != f"http8090:{kind}-start"
                        or sha256(row.content.encode()).hexdigest() != digest):
                    raise ValueError("START missing or changed")

            def stable(state: RebootSnapshot, network: dict[str, Any]) -> bool:
                return before.same_state(state) and all(network_before.get(key) == network.get(key)
                    for key in ("wifi_profile_count", "active_ssid"))

            ledger("MANUAL_COMMAND_START")
            integrity()
            code = "WRITE_PRECONDITION_FAILED"
            if not stable(*(await snapshot())):
                raise ValueError("state changed while START persisted")
            # A queued opt-in reconnect must not undo an explicit source/network
            # transition. Persist cancellation under the same device ownership.
            cancel(db, identity)
            ledger("MANUAL_COMMAND_INTENT")
            integrity()
            if not stable(*(await snapshot())):
                raise ValueError("state changed while intent persisted")
            integrity()
            attempted = True
            code = "MANUAL_COMMAND_OUTCOME_UNKNOWN"
            try:
                await client.post_xml(command.path, command.body)
                result["response_received"] = True
            except Exception:
                result["response_received"] = False
            try:
                after, network_after = await snapshot()
                result["last_observation"] = {"source": after.selection.source,
                    "play_status": after.selection.play_status, "volume": after.volume[0], "muted": after.volume[1]}
                result["unrequested_state_preserved"] = (
                    before.volume, before.zone, before.presets) == (after.volume, after.zone, after.presets)
                if kind == "native_station":
                    result["selected_source_matches"] = (after.selection.source, after.selection.source_account) == (
                        command.source, command.account)
                    result["station_identity_verified"] = False  # token != established ContentItem.location mapping
                else:
                    result["requested_ssid_observed"] = network_after["active_ssid"] == command.ssid
                    result["network_state_changed"] = any(network_before[key] != network_after[key]
                        for key in ("active_ssid", "wifi_profile_count"))
            except IdentityMismatch:
                raise
            except Exception:
                result["readback_unavailable"] = True
            integrity()
            ledger("MANUAL_COMMAND_PENDING")
            return {**result, "ok": False, "status": "PENDING_READBACK", "code": code,
                    "reconciliation_required": True, "manual_recovery_may_be_required": True,
                    "message": "Command attempted once; persistence not verified. Read actual state before any further action."}
    except (Exception, asyncio.CancelledError) as exc:
        db.rollback()
        if isinstance(exc, IdentityMismatch):
            code = "IDENTITY_MISMATCH"
        detail = {**result, "ok": False, "status": "DIVERGED" if attempted else "BLOCKED", "code": code,
            "reconciliation_required": attempted,
            "message": "State not confirmed. No automatic retry or rollback; check the radio before another action."}
        record_transport_attempt(device_id=identity, ip_address=address, action=kind, trigger="manual_lab",
            job_id=result["job_id"], phase=detail["status"], result=detail["status"], backup_ref=backup_ref,
            error_category=code, verified=False)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ManualCommandError(detail) from None
