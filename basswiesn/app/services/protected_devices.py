from __future__ import annotations

import ipaddress
import re
from typing import Any, Iterable

from fastapi import HTTPException

from basswiesn.app.config import (
    IMMUTABLE_PROTECTED_DEVICE_IDS,
    IMMUTABLE_PROTECTED_IPS,
    get_settings,
)
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import Device


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE", "TELNET", "SSH"}


def _normalize_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return str(value or "").strip()


def _normalize_device_id(value: str | None) -> str:
    return str(value or "").strip().upper()


def _parse_ip_list(value: str) -> set[str]:
    ips: set[str] = set()
    for item in re.split(r"[\s,;]+", str(value or "").strip()):
        if not item:
            continue
        try:
            ips.add(str(ipaddress.ip_address(item)))
        except ValueError:
            continue
    return ips


def _settings_table_protected_ips() -> set[str]:
    try:
        from basswiesn.app.db import SessionLocal
        from basswiesn.app.models.core import Setting

        db = SessionLocal()
        try:
            row = db.query(Setting).filter(Setting.key == "protected_device_ips").one_or_none()
            return _parse_ip_list(row.value if row is not None else "")
        finally:
            db.close()
    except Exception:
        return set()


def _settings_table_protected_ids() -> set[str]:
    try:
        from basswiesn.app.db import SessionLocal
        from basswiesn.app.models.core import Setting

        db = SessionLocal()
        try:
            row = db.query(Setting).filter(Setting.key == "protected_device_ids").one_or_none()
            return {
                _normalize_device_id(item)
                for item in re.split(r"[\s,;]+", row.value if row is not None else "")
                if _normalize_device_id(item)
            }
        finally:
            db.close()
    except Exception:
        return set()


def protected_device_ips() -> set[str]:
    env_ips = {_normalize_ip(item) for item in get_settings().protected_device_ips if str(item or "").strip()}
    return {
        item
        for item in (set(IMMUTABLE_PROTECTED_IPS) | env_ips | _settings_table_protected_ips())
        if item
    }


def protected_device_ids() -> set[str]:
    env_ids = {
        _normalize_device_id(item)
        for item in get_settings().protected_device_ids
        if _normalize_device_id(item)
    }
    return set(IMMUTABLE_PROTECTED_DEVICE_IDS) | env_ids | _settings_table_protected_ids()


def _device_row_by_id(device_id: str | None) -> tuple[Device | None, bool]:
    """Return the current device mapping and whether lookup failed."""
    normalized_id = _normalize_device_id(device_id)
    if not normalized_id:
        return None, False
    try:
        from basswiesn.app.db import SessionLocal

        db = SessionLocal()
        try:
            return db.query(Device).filter(Device.device_id == normalized_id).first(), False
        finally:
            db.close()
    except Exception:
        # A failed mapping lookup must not permit a potentially protected
        # device to be contacted.
        return None, True


def is_device_access_protected(ip_address: str | None = None, device_id: str | None = None) -> bool:
    """Return whether all network access to a device must be blocked.

    Protection is evaluated on every call so changes to the runtime setting
    apply to already-running background workers. If both identifiers are
    available, an inconsistent database mapping is fail-closed.
    """
    normalized_ip = _normalize_ip(ip_address)
    normalized_id = _normalize_device_id(device_id)
    configured_ips = protected_device_ips()
    # These checks intentionally precede any database lookup. They are the
    # last line of defence for callers that do not have a Device row yet.
    if normalized_ip in IMMUTABLE_PROTECTED_IPS:
        return True
    if normalized_id in protected_device_ids():
        return True
    if normalized_ip and normalized_ip in configured_ips:
        return True
    if not normalized_id:
        return False

    row, lookup_failed = _device_row_by_id(normalized_id)
    if lookup_failed:
        return True
    # A previously unseen identity is normal during an explicit discovery
    # request. It is safe only when neither the identity nor the literal IP is
    # protected; both checks already happened above. A database lookup error,
    # by contrast, remains fail-closed.
    if row is None:
        return False
    row_ip = _normalize_ip(getattr(row, "ip_address", ""))
    if row_ip in configured_ips:
        return True
    if normalized_ip and row_ip and row_ip != normalized_ip:
        return True
    return False


def is_protected_ip(value: str) -> bool:
    return _normalize_ip(value) in protected_device_ips()


def is_explicit_discovery_target_protected(
    ip_address: str | None,
    advertised_device_id: str | None,
) -> bool:
    """Apply configured protection before an explicit discovery follow-up.

    A legitimate radio may receive a new DHCP address after a factory reset,
    so discovery cannot treat an old database IP mapping as permanent. The
    multicast-advertised identity and literal reply IP are checked directly
    against the current protection policy before any unicast descriptor read.
    Normal transports continue to use :func:`is_device_access_protected` and
    therefore retain the stricter stored-mapping consistency check.
    """

    return (
        _normalize_ip(ip_address) in protected_device_ips()
        or _normalize_device_id(advertised_device_id) in protected_device_ids()
    )


def first_protected_ip(values: Iterable[str]) -> str | None:
    """Return the first protected address from an already resolved target.

    Generic URL transports use this after DNS resolution.  Keeping the
    configured and immutable policy lookup here prevents each HTTP caller
    from growing a subtly different protected-device check.
    """

    protected = protected_device_ips()
    for value in values:
        normalized = _normalize_ip(value)
        if normalized and normalized in protected:
            return normalized
    return None


def is_protected_device(device: Device | None) -> bool:
    if device is None:
        return False
    ip_address = getattr(device, "ip_address", "")
    if _normalize_ip(ip_address) in IMMUTABLE_PROTECTED_IPS:
        return True
    if is_protected_ip(ip_address):
        return True
    device_id = _normalize_device_id(getattr(device, "device_id", ""))
    if not device_id:
        return False
    if device_id in protected_device_ids():
        return True
    row, lookup_failed = _device_row_by_id(device_id)
    if lookup_failed:
        return True
    # The ORM object is itself the authoritative mapping for this request;
    # this also supports newly created rows before their transaction commits.
    if row is None:
        return False
    row_ip = _normalize_ip(getattr(row, "ip_address", ""))
    return row_ip in protected_device_ips() or bool(row_ip and _normalize_ip(ip_address) and row_ip != _normalize_ip(ip_address))


def filter_unprotected_devices(devices):
    """Filter device objects before creating clients or opening sockets."""
    return [device for device in devices if not is_protected_device(device)]


def _protected_access_detail(
    ip_address: str | None,
    *,
    action: str,
    requester: str = "",
    device_id: str = "",
    method: str = "",
    endpoint: str = "",
) -> dict[str, Any]:
    target = " ".join(part for part in (method, endpoint) if part)
    return {
        "error": "protected_device",
        "message": (
            "Dieses Radio ist vollstaendig geschuetzt. Weder automatische "
            "noch manuelle Netzwerkzugriffe werden serverseitig zugelassen."
        ),
        "action": action,
        "requester": requester,
        "method": method,
        "endpoint": endpoint,
        "target": target,
        "device_id": _normalize_device_id(device_id),
        "ip_address": _normalize_ip(ip_address),
        "protected_device_ips": sorted(protected_device_ips()),
        "protected_device_ids": sorted(protected_device_ids()),
    }


def reject_protected_device_access(
    ip_address: str | None,
    *,
    device_id: str = "",
    action: str,
    requester: str = "",
    method: str = "GET",
    endpoint: str = "",
) -> None:
    """Raise before any HTTP, socket, WebSocket, Telnet, or SSH access."""
    if not is_device_access_protected(ip_address, device_id):
        return
    detail = _protected_access_detail(
        ip_address,
        action=action,
        requester=requester,
        device_id=device_id,
        method=method,
        endpoint=endpoint,
    )
    write_masterlog(
        "protected_device_access_blocked",
        **detail,
    )
    raise HTTPException(status_code=403, detail=detail)


def protected_device_detail(device: Device | None, *, action: str, requester: str = "") -> dict[str, Any]:
    detail = _protected_access_detail(
        getattr(device, "ip_address", "") if device is not None else "",
        action=action,
        requester=requester,
        device_id=getattr(device, "device_id", "") if device is not None else "",
    )
    detail["device_name"] = getattr(device, "name", "") if device is not None else ""
    return detail


def log_protected_block(device: Device | None, *, action: str, requester: str = "", method: str = "", endpoint: str = "") -> None:
    write_masterlog(
        "protected_device_access_blocked",
        device_id=getattr(device, "device_id", "") if device is not None else "",
        device_name=getattr(device, "name", "") if device is not None else "",
        radio_ip=getattr(device, "ip_address", "") if device is not None else "",
        action=action,
        requester=requester,
        method=method,
        endpoint=endpoint,
        protected_device_ips=sorted(protected_device_ips()),
    )


def require_unprotected_device(device: Device | None, *, action: str, requester: str = "", method: str = "", endpoint: str = "") -> None:
    if device is None:
        return
    if not is_protected_device(device):
        return
    log_protected_block(device, action=action, requester=requester, method=method, endpoint=endpoint)
    raise HTTPException(status_code=403, detail=protected_device_detail(device, action=action, requester=requester))


def reject_protected_write_ip(ip_address: str, *, device_id: str = "", action: str, requester: str = "", method: str, endpoint: str = "") -> None:
    reject_protected_device_access(
        ip_address,
        device_id=device_id,
        action=action,
        requester=requester,
        method=method,
        endpoint=endpoint,
    )
