from __future__ import annotations

import socket

from basswiesn.app.config import get_settings
from basswiesn.app.services.network_security import validate_outbound_host
from basswiesn.app.services.protected_devices import is_protected_ip, reject_protected_device_access


def lab_status() -> dict:
    return {
        "enabled": get_settings().lab_mode,
        "read_only": True,
        "factory_reset_executable": False,
        "ssh_activation": False,
        "telnet_activation": False,
        "warnings": [
            "LAB ist standardmäßig deaktiviert.",
            "Diese lokale Testversion führt keinen True Factory Reset aus.",
            "SSH-/Telnet-Prüfungen sind read-only und ändern keine Geräte.",
        ],
    }


def probe_port(host: str, port: int, *, timeout: float = 1.0) -> dict:
    if not get_settings().lab_mode:
        return {"enabled": False, "ok": False, "skipped": True, "reason": "BASSWIESN_LAB_MODE=false"}
    if is_protected_ip(host):
        return {"enabled": True, "ok": False, "skipped": True, "host": host, "port": port, "reason": "protected device: all network probing is blocked by PROTECTED_DEVICE_IPS"}
    validation = validate_outbound_host(host, port=port)
    if not validation.ok:
        return {"enabled": True, "ok": False, "skipped": True, "host": host, "port": port, "reason": validation.reason}
    target = validation.addresses[0]
    try:
        reject_protected_device_access(target, action="LAB port probe", requester="lab_tools", method="TCP", endpoint=str(port))
    except Exception:
        return {"enabled": True, "ok": False, "skipped": True, "host": host, "port": port, "reason": "protected device: transport blocked"}
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return {"enabled": True, "ok": True, "host": host, "port": port}
    except OSError as exc:
        return {"enabled": True, "ok": False, "host": host, "port": port, "error": str(exc)}
