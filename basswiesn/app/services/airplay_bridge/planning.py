"""Read-only AP2 endpoint planning. Never advertise or contact devices."""
from __future__ import annotations

from collections import Counter

from basswiesn.app.models import Device
from basswiesn.app.services.protected_devices import is_device_access_protected


def receiver_name(name: str) -> str:
    clean = "".join(c for c in str(name or "") if c.isprintable()).strip()
    # DNS-SD instance label has a 63-byte limit. Keep the AP2 prefix intact.
    return ("AP2 " + (clean or "Radio")).encode("utf-8")[:63].decode("utf-8", "ignore")


def preview_receivers(devices: list[Device]) -> list[dict]:
    rows = []
    for device in devices:
        if not device.device_id or not device.ip_address:
            continue
        if is_device_access_protected(device.ip_address, device.device_id):
            continue
        rows.append({"device_id": device.device_id, "name": receiver_name(device.name),
                     "advertised": False, "state": "NOT_PROVISIONED"})
    counts = Counter(row["name"].casefold() for row in rows)
    for row in rows:
        if counts[row["name"].casefold()] > 1:
            row["state"] = "NAME_CONFLICT"
    return rows


def bridge_status(devices: list[Device]) -> dict:
    return {
        "enabled": False,
        "stage": "OFFLINE_PROTOTYPE",
        "receiver_backend_running": False,
        "receivers": preview_receivers(devices),
        "capabilities": {
            "apple_single_audio": "NOT_TESTED",
            "apple_multi_select": "NOT_TESTED",
            "bose_group_sync": "NOT_TESTED",
            "radio_metadata_display": "NOT_TESTED",
            "native_airplay_sync": "NOT_ESTABLISHED",
        },
        "blockers": [
            "ISOLATED_RECEIVER_NETWORKS_NOT_PROVISIONED",
            "RECEIVER_BACKEND_NOT_INTEGRATED",
            "APPLE_GROUP_MEMBERSHIP_CONTRACT_NOT_VERIFIED",
            "RADIO_RELAY_AND_RESTORE_NOT_VERIFIED",
        ],
    }
