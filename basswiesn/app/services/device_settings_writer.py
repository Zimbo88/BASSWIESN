"""Explicit, identity-bound HTTP settings transactions, never SSH fallbacks.

This is an orchestration service, not a new transport or protection policy.
An uncertain POST is reconciled by one readback, never replayed. A partial batch
stops and retains durable intent/backup evidence instead of guessing rollback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.core.xml_safety import parse_xml
from basswiesn.app.models import ConfigBackup, Device, Setting
from basswiesn.app.services.action_journal import record_action, record_transport_attempt
from basswiesn.app.services.device_transactions import device_transaction
from basswiesn.app.services.protected_devices import require_unprotected_device
from basswiesn.app.services.settings_contract import SettingPlan, SettingValue, read_setting
from basswiesn.app.services.setup_rebuild.profiles import DeviceFacts, detect_profile


class XmlClient(Protocol):
    async def get_xml(self, path: str) -> str: ...
    async def post_xml(self, path: str, xml: str) -> str: ...


class ClientFactory(Protocol):
    def __call__(self, ip_address: str, *, device_id: str, request_purpose: str, trigger: str) -> XmlClient: ...


class SettingsOperationError(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(str(result["code"]))


class IdentityMismatch(ValueError):
    pass


def verify_settings_identity(device: Device, xml: str) -> str:
    """Reuse the exact researched family registry; never adopt a changed ID.

    These are conservative hardware prerequisites, not an assertion that every
    setting has hardware approval. Each field additionally needs readable actual
    state and exact post-write readback. No new profile is authorized here.
    """
    root = parse_xml(xml)
    cached = parse_xml(device.info_xml)
    expected_id = device.device_id.strip().upper()
    for node in (root, cached):
        if node.tag != "info" or node.get("deviceID", "").strip().upper() != expected_id:
            raise IdentityMismatch("device identity changed or missing")
    require_unprotected_device(device, action="settings_identity", requester="settings_writer")

    def facts(node: ET.Element) -> DeviceFacts:
        return DeviceFacts(
            expected_id, device.ip_address, node.findtext("type", "").strip(),
            node.findtext(".//softwareVersion", "").strip(),
            product_id=next((node.findtext(tag, "").strip() for tag in
                             ("productID", "productId", "product_id", "productCode") if node.findtext(tag)), ""),
            variant=node.findtext("variant", "").strip(), platform=node.findtext("moduleType", "").strip(),
        )

    live, old = facts(root), facts(cached)
    if not device.model or not device.firmware:
        raise ValueError("stored hardware identity is incomplete")
    if live.model.casefold() != device.model.strip().casefold() or live.firmware != device.firmware.strip():
        raise IdentityMismatch("model or firmware changed")
    if (live.model.casefold(), live.firmware, live.variant.casefold(), live.platform.casefold()) != (
        old.model.casefold(), old.firmware, old.variant.casefold(), old.platform.casefold()
    ):
        raise IdentityMismatch("hardware identity differs from stored evidence")
    current_match, old_match = detect_profile(live), detect_profile(old)
    if current_match.profile is None or old_match.profile is None:
        raise ValueError("exact hardware profile is not approved")
    if current_match.profile.key != old_match.profile.key:
        raise IdentityMismatch("hardware profile changed")
    return current_match.profile.key


def _read(plan: SettingPlan, xml: str, device: Device) -> SettingValue:
    root = parse_xml(xml)
    # Some settings omit deviceID. A contradictory ID must never be ignored.
    observed = root.get("deviceID")
    if observed is not None and observed.strip().upper() != device.device_id.strip().upper():
        raise IdentityMismatch("setting response belongs to a different device")
    return read_setting(plan.setting, xml)


def _fail(result: dict[str, Any], code: str, setting: str = "", *, attempted: bool = False) -> SettingsOperationError:
    return SettingsOperationError({
        **result, "ok": False, "status": "DIVERGED" if attempted or result["changed"] else "BLOCKED",
        "code": code, "failed_setting": setting, "applied": len(result["changed"]),
        "reconciliation_required": attempted or bool(result["changed"]),
        "message": "No automatic write retry or rollback. Read the radio state before another action.",
    })


async def apply_settings_batch(
    db: Session, devices: Sequence[Device], plan: SettingPlan, *,
    client_factory: ClientFactory, guard: Callable[[Session, Device], None],
) -> list[dict[str, Any]]:
    """Apply one setting to explicit targets, retaining any verified prefix.

    All participants are protected and identity/profile/state-checked before
    the first write. Each existing single-device transaction then verifies its
    durable backup and the captured precondition again. The batch is not an
    atomic radio operation: a later failure stops it without replay/rollback.
    """
    if not devices or len({device.device_id for device in devices}) != len(devices):
        raise ValueError("A nonempty unique device selection is required")
    results: list[dict[str, Any]] = []
    previous: dict[str, SettingValue] = {}
    async with device_transaction(device.device_id for device in devices):
        # A protected later member must block before even the first transport.
        for device in devices:
            guard(db, device)
        for device in devices:
            try:
                client = client_factory(device.ip_address, device_id=device.device_id,
                                        request_purpose="settings_batch_preflight", trigger="explicit_settings_action")
                identity = await client.get_xml("/info")
                verify_settings_identity(device, identity)
                raw = identity if plan.read_path == "/info" else await client.get_xml(plan.read_path)
                previous[device.device_id] = _read(plan, raw, device)
            except Exception as exc:
                raise SettingsOperationError({
                    "ok": False, "status": "BLOCKED", "code": "IDENTITY_MISMATCH"
                    if isinstance(exc, IdentityMismatch) else "SETTINGS_PREFLIGHT_FAILED",
                    "failed_device_id": device.device_id, "results": [],
                    "reconciliation_required": False,
                }) from None
        for device in devices:
            try:
                result = await apply_settings(
                    db, device, [plan], client_factory=client_factory, guard=guard,
                    expected_previous={plan.setting: previous[device.device_id]},
                )
            except Exception as exc:
                changed = any(row["transaction"]["changed"] for row in results)
                failure = exc.result if isinstance(exc, SettingsOperationError) else {
                    "ok": False, "code": "SETTINGS_BATCH_INTERRUPTED", "reconciliation_required": True,
                    "message": "Read current state before another action; no automatic retry or rollback.",
                }
                uncertain = changed or failure["reconciliation_required"]
                raise SettingsOperationError({
                    **failure, "status": "DIVERGED" if uncertain else "BLOCKED",
                    "results": results, "failed_device_id": device.device_id,
                    "reconciliation_required": uncertain,
                }) from None
            first_change: dict[str, Any] = next(iter(result["changed"]), {})
            results.append({
                "device_id": device.device_id, "name": device.name, "ok": True,
                "status": result["status"], "transaction": result,
                "response": first_change.get("response", ""),
                "transport_uncertain": any(change.get("transport_uncertain", False) for change in result["changed"]),
            })
    return results


async def apply_settings(
    db: Session, device: Device, plans: Sequence[SettingPlan], *,
    client_factory: ClientFactory, guard: Callable[[Session, Device], None],
    expected_previous: Mapping[str, object] | None = None, art_mode: str | None = None,
) -> dict[str, Any]:
    """Serialize with other cooperative device jobs; return only verified changes.

    There is no atomic radio-side compare-and-swap. Front-panel/external commands
    can race even with this lock; rechecking state narrows but cannot remove that
    physical limitation. A batch is deliberately not described as atomic.
    """
    result: dict[str, Any] = {
        "device_id": device.device_id, "job_id": f"settings-{uuid4().hex}",
        "changed": [], "unchanged": [], "preset_sync_required": False,
    }
    async with device_transaction([device.device_id]):
        if art_mode is not None and expected_previous is not None and "station_art_mode" in expected_previous:
            row = db.query(Setting).filter(Setting.key == f"station_art_mode:{device.device_id}").one_or_none()
            current_mode = row.value if row else "radio_symbol"
            if current_mode != expected_previous["station_art_mode"]:
                raise _fail(result, "WRITE_PRECONDITION_FAILED", "station_art_mode")
        if plans:
            # Guard before even constructing a transport, including batch writes.
            guard(db, device)
            client = client_factory(device.ip_address, device_id=device.device_id,
                                    request_purpose="settings_transaction", trigger="explicit_settings_action")
            before: dict[str, SettingValue] = {}
            raw: dict[str, str] = {}
            backups: dict[str, tuple[int, str]] = {}
            try:
                initial_info = await client.get_xml("/info")
                verify_settings_identity(device, initial_info)
                raw["/info"] = initial_info
                for plan in plans:
                    if plan.read_path not in raw:
                        raw[plan.read_path] = await client.get_xml(plan.read_path)
                    before[plan.setting] = _read(plan, raw[plan.read_path], device)
                    if expected_previous is not None and plan.setting in expected_previous:
                        expected = expected_previous[plan.setting]
                        if not plan.matches_previous(before[plan.setting], expected):
                            raise _fail(result, "WRITE_PRECONDITION_FAILED", plan.setting)
                for path, content in raw.items():
                    digest = sha256(content.encode()).hexdigest()
                    backup = ConfigBackup(device_id=device.device_id, path=f"http8090:{path}", content=content)
                    db.add(backup)
                    db.flush()
                    backups[path] = (backup.id, digest)
                # Commit and re-read before any radio write: a rolled-back DB
                # transaction must not erase the only copy of the START state.
                db.commit()
                for identifier, digest in backups.values():
                    stored = db.get(ConfigBackup, identifier, populate_existing=True)
                    if stored is None or sha256(stored.content.encode()).hexdigest() != digest:
                        raise ValueError("backup integrity verification failed")
            except SettingsOperationError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise _fail(result, "IDENTITY_MISMATCH" if isinstance(exc, IdentityMismatch)
                            else "SETTINGS_PREFLIGHT_FAILED") from None

            for plan in plans:
                attempted = False
                response = ""
                transport_error = ""
                observed: SettingValue | None = None
                backup_id, digest = backups[plan.read_path]
                backup_ref = f"config-backup:{backup_id}"
                try:
                    guard(db, device)
                    live_info = await client.get_xml("/info")
                    verify_settings_identity(device, live_info)
                    latest = live_info if plan.read_path == "/info" else await client.get_xml(plan.read_path)
                    current = _read(plan, latest, device)
                    previous = before[plan.setting]
                    if plan.setting == "clockConfig" and isinstance(current, dict) and isinstance(previous, dict) and isinstance(plan.expected, dict):
                        # Clock responses can include advancing UTC fields. Only
                        # compare the fields this explicit update would overwrite.
                        # clockDisplay may already have applied userEnable in
                        # this batch. A field already at the requested value is
                        # also safe; a third value still blocks the overwrite.
                        precondition_matches = all(
                            current.get(k) in (previous.get(k), desired)
                            for k, desired in plan.expected.items()
                        )
                    else:
                        precondition_matches = type(current) is type(previous) and current == previous
                    if not precondition_matches:
                        raise _fail(result, "WRITE_PRECONDITION_FAILED", plan.setting)
                    if plan.matches(current):
                        result["unchanged"].append(plan.setting)
                        continue
                    record_action(db, job_id=result["job_id"], device_id=device.device_id,
                                  ip_address=device.ip_address, action=f"setting:{plan.setting}",
                                  trigger="explicit_settings_action", phase="settings_intent",
                                  requested_state=plan.expected, before_state={"value": current, "sha256": digest},
                                  backup_ref=backup_ref, result="PENDING_READBACK", verified=False)
                    db.commit()
                    attempted = True
                    try:
                        response = await client.post_xml(plan.path, plan.xml)
                    except (httpx.HTTPError, OSError) as exc:
                        # Timeout/HTTP error is not evidence of an unchanged radio.
                        transport_error = type(exc).__name__
                    final_info = await client.get_xml("/info")
                    verify_settings_identity(device, final_info)
                    final_xml = final_info if plan.read_path == "/info" else await client.get_xml(plan.read_path)
                    observed = _read(plan, final_xml, device)
                    if not plan.matches(observed):
                        raise _fail(result, "WRITE_READBACK_MISMATCH", plan.setting, attempted=True)
                    record_action(db, job_id=result["job_id"], device_id=device.device_id,
                                  ip_address=device.ip_address, action=f"setting:{plan.setting}",
                                  trigger="explicit_settings_action", phase="settings_verified",
                                  requested_state=plan.expected, backup_ref=backup_ref,
                                  before_state={"value": current}, readback={"value": observed}, after_state={"value": observed},
                                  result="VERIFIED", error_category=transport_error, verified=True)
                    if plan.setting == "name":
                        device.name = str(observed)
                        device.info_xml = final_info
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    error = exc if isinstance(exc, SettingsOperationError) else _fail(
                        result, "IDENTITY_MISMATCH" if isinstance(exc, IdentityMismatch)
                        else "SETTINGS_VERIFICATION_FAILED", plan.setting, attempted=attempted)
                    record_transport_attempt(
                        ip_address=device.ip_address, device_id=device.device_id,
                        action=f"setting:{plan.setting}", trigger="explicit_settings_action",
                        phase="settings_diverged" if attempted else "settings_blocked",
                        result=error.result["status"], job_id=result["job_id"], backup_ref=backup_ref,
                        rollback_ref=backup_ref, readback={"value": observed}, error_category=error.result["code"], verified=False,
                    )
                    raise error from None
                change: dict[str, Any] = {
                    "setting": plan.setting, "before": current, "after": observed,
                    "path": plan.path, "response": response, "transport_uncertain": bool(transport_error),
                    "backup_ref": backup_ref, "verified": True,
                }
                if plan.setting == "name":
                    change["persistence"] = {"updated": False, "reason": "HTTP name verified; reboot persistence not tested; no SSH fallback"}
                result["changed"].append(change)

        if art_mode is not None:
            # Local presentation preference needs no radio probe or transport.
            key = f"station_art_mode:{device.device_id}"
            row = db.query(Setting).filter(Setting.key == key).one_or_none()
            current_mode = row.value if row else "radio_symbol"
            if current_mode == art_mode:
                result["unchanged"].append("station_art_mode")
            else:
                try:
                    if row is None:
                        row = Setting(key=key)
                        db.add(row)
                    row.value = art_mode
                    db.commit()
                except Exception:
                    db.rollback()
                    raise _fail(result, "SETTINGS_LOCAL_COMMIT_FAILED", "station_art_mode") from None
                result["preset_sync_required"] = True
                result["changed"].append({"setting": "station_art_mode", "before": current_mode,
                                          "after": art_mode, "scope": "BASSWIESN playback metadata"})
    return {**result, "applied": len(result["changed"]), "ok": True, "status": "VERIFIED",
            "message": "Nur zurückgelesene Radioänderungen und gespeicherte lokale Einstellungen wurden bestätigt."}
