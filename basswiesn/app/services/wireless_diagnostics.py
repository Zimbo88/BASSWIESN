"""Identity-bound wireless reads and an explicitly requested, one-shot survey.

A survey is a radio command, not a harmless GET option. Its firmware-specific
completion schema is not established here: an acknowledgement never certifies
completion. This module does not add profiles, reconnect or change credentials.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
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
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.maintenance_reboot import _root
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.services.zone_changes import Zone


class WirelessDiagnosticError(Exception):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail["code"]))


def _profile(xml: str, identity: str) -> ET.Element:
    # Do not invent a root-name allowlist from undocumented firmware variants.
    # We only expose the already observed direct child `ssid`, never secrets or
    # unknown attributes. Bounded DTD-free XML and contradictory IDs still fail.
    if not xml or len(xml.encode()) > 131072:
        raise ValueError("missing or excessive profile response")
    root = cast(ET.Element, SafeXML.fromstring(xml, forbid_dtd=True))
    if root.get("deviceID", identity).upper() != identity.upper():
        raise IdentityMismatch("wireless profile belongs to another radio")
    if len(root.findall("ssid")) != 1:
        raise ValueError("active SSID missing or ambiguous")
    return root


def network_observation(network: str, active: str, identity: str) -> dict[str, Any]:
    """One credential-free projection shared by diagnostics and Wi-Fi writes."""
    parsed = _root(network, "networkInfo", identity)
    count = parsed.get("wifiProfileCount", "")
    if count and (not count.isascii() or not count.isdecimal() or len(count) > 5):
        raise ValueError("invalid profile count")
    public_network = ET.Element("networkInfo", {"wifiProfileCount": count} if count else {})
    for interface in parsed.findall("interface"):
        ET.SubElement(public_network, "interface", {
            key: value for key, value in interface.attrib.items()
            if key in {"type", "state", "ssid", "signal"}
        })
    profile = _profile(active, identity)
    return {"wifi_profile_count": int(count) if count else None,
            "active_ssid": profile.findtext("ssid", ""),
            "network_xml": ET.tostring(public_network, encoding="unicode"),
            "unknown_fields_omitted": True}


async def wireless_diagnostics(
    db: Session, device: Device, *, survey: bool, client_factory: ClientFactory,
    guard: Callable[[Session, Device], None],
) -> dict[str, Any]:
    frozen = device_fingerprint(device)
    identity, address = frozen[:2]
    result: dict[str, Any] = {"device_id": identity, "survey_xml": "", "survey_requested": survey,
        "verified": False, "automatic_retry": False, "reconciliation_required": False,
        "supports_multiple_profiles": None}
    backup_ref, attempted = "", False
    code = "WIRELESS_PREFLIGHT_FAILED"
    job = f"wireless-{uuid4().hex}"
    try:
        async with device_transaction([identity]):
            def unchanged() -> None:
                db.refresh(device)
                if device_fingerprint(device) != frozen:
                    raise IdentityMismatch("stored target changed")
                require_unprotected_device(device, action="wireless_diagnostics", requester="manual")
                if survey:
                    guard(db, device)

            unchanged()
            client = client_factory(address, device_id=identity, request_purpose="wireless_diagnostics", trigger="manual")

            async def observe() -> dict[str, Any]:
                unchanged()
                info = await client.get_xml("/info")
                root = _root(info, "info", identity)
                if root.get("deviceID", "").upper() != identity.upper():
                    raise IdentityMismatch("missing live identity")
                if survey:
                    _root(frozen[4], "info", identity)
                    verify_settings_identity(device, info)
                unchanged()
                network = await client.get_xml("/networkInfo")
                unchanged()
                active = await client.get_xml("/getActiveWirelessProfile")
                observation = network_observation(network, active, identity)
                unchanged()
                # A diagnostic read is allowed for safe, unknown hardware; a
                # radio command additionally needs an exact known profile and
                # may not interfere with unowned group participants.
                if survey:
                    zone = await client.get_xml("/getZone")
                    _root(zone, "zone", identity)
                    if not Zone.parse(zone, device_id=identity).standalone(identity):
                        raise ValueError("survey requires a standalone radio")
                unchanged()
                return observation

            before = await observe()
            if not survey:
                return {**result, **before, "status": "OBSERVED"}
            result["job_id"] = job
            serialized = json.dumps({"schema": 1, "before": before}, sort_keys=True)
            digest = sha256(serialized.encode()).hexdigest()
            backup = ConfigBackup(device_id=identity, path="http8090:wireless-survey-start", content=serialized)
            db.add(backup)
            db.flush()
            backup_id = backup.id
            backup_ref = f"config-backup:{backup_id}:sha256:{digest}"
            result["backup_ref"] = backup_ref

            def ledger(phase: str) -> None:
                record_action(db, job_id=job, device_id=identity, ip_address=address, action="wireless_survey",
                    phase=phase, trigger="manual", backup_ref=backup_ref, verified=False, result="PENDING_READBACK")
                db.commit()

            def integrity() -> None:
                unchanged()
                stored = db.get(ConfigBackup, backup_id, populate_existing=True)
                if stored is None or stored.device_id != identity or sha256(stored.content.encode()).hexdigest() != digest:
                    raise ValueError("wireless START missing or changed")

            def same_profile(actual: dict[str, Any]) -> bool:
                # Signal strength is volatile. SSID/profile count are the
                # established stable fields, not the raw XML byte sequence.
                return all(before[key] == actual[key] for key in ("wifi_profile_count", "active_ssid"))

            ledger("WIRELESS_START")
            integrity()
            code = "WRITE_PRECONDITION_FAILED"
            if not same_profile(await observe()):
                raise ValueError("network changed while START persisted")
            ledger("WIRELESS_SURVEY_INTENT")
            integrity()
            if not same_profile(await observe()):
                raise ValueError("network changed while intent persisted")
            integrity()
            attempted = True
            code = "WIRELESS_SURVEY_OUTCOME_UNKNOWN"
            try:
                await client.post_xml("/performWirelessSiteSurvey", '<PerformWirelessSiteSurvey timeout="8" />')
                # No established schema to safely project. Withhold raw data:
                # regex redaction cannot identify arbitrary credential fields.
                result["response_received"] = True
            except Exception:
                result["response_received"] = False
            try:
                after = await observe()
                result.update(after)
                result["network_state_preserved"] = same_profile(after)
            except IdentityMismatch:
                raise
            except Exception:
                result["readback_unavailable"] = True
            integrity()
            ledger("WIRELESS_SURVEY_PENDING")
            return {**result, "ok": False, "status": "PENDING_READBACK", "code": code,
                    "reconciliation_required": True, "completion_verified": False,
                    "message": "Survey sent once. Completion is not established; no retry or network change was requested."}
    except (Exception, asyncio.CancelledError) as exc:
        db.rollback()
        if isinstance(exc, IdentityMismatch):
            code = "IDENTITY_MISMATCH"
        detail = {**result, "ok": False, "status": "DIVERGED" if attempted else "BLOCKED", "code": code,
                  "reconciliation_required": attempted}
        if survey:
            record_transport_attempt(device_id=identity, ip_address=address, action="wireless_survey", job_id=job, trigger="manual",
                phase=detail["status"], result=detail["status"], backup_ref=backup_ref, error_category=code, verified=False)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise WirelessDiagnosticError(detail) from None
