"""Shared short preflight for radio-changing actions."""

import asyncio
import ipaddress

from basswiesn.app.models import Device, RuntimeState
from basswiesn.app.services.network_security import validate_outbound_host
from basswiesn.app.services.protected_devices import is_protected_device, is_protected_ip, protected_device_detail, reject_protected_device_access


async def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    if is_protected_ip(host):
        return False
    validation = validate_outbound_host(host, port=port)
    if not validation.ok:
        return False
    target = validation.addresses[0]
    reject_protected_device_access(target, action="port preflight", requester="action_preflight", method="TCP", endpoint=str(port))
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(target, port), timeout)
        del reader
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError, asyncio.TimeoutError):
        return False


async def action_preflight(db, device: Device | None, *, required_port: int, action: str) -> dict:
    if device is None:
        return {"ok": False, "code": "DEVICE_NOT_FOUND", "message": "Das Radio wurde nicht gefunden."}
    try:
        ipaddress.ip_address(device.ip_address)
    except ValueError:
        return {"ok": False, "code": "INVALID_DEVICE_IP", "message": "Das Radio hat keine gültige IP-Adresse."}
    if device.reachable is False:
        return {"ok": False, "code": "DEVICE_UNREACHABLE", "message": "Das Radio ist nicht erreichbar."}
    if is_protected_device(device):
        return {
            "ok": False,
            "code": "PROTECTED_DEVICE",
            "message": "Dieses Radio ist fest geschuetzt und darf nicht Ziel einer schreibenden Aktion sein.",
            "detail": protected_device_detail(device, action=action, requester="action_preflight"),
        }
    locks = {row.key for row in db.query(RuntimeState).filter(RuntimeState.key.like(f"device:{device.device_id}:%lock%")).all() if str(row.value).lower() not in {"", "0", "false", "idle"}}
    if locks:
        return {"ok": False, "code": "DEVICE_BUSY", "message": "Für das Radio läuft bereits eine Aktion.", "locks": sorted(locks)}
    if not await port_open(device.ip_address, required_port):
        return {"ok": False, "code": "DEVICE_UNREACHABLE", "message": "Das Radio ist nicht erreichbar.", "required_port": required_port, "action": action}
    return {"ok": True, "code": "READY", "required_port": required_port, "action": action}
