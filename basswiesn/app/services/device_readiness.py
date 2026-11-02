"""Explicit read-only marker observation; a marker is not a reboot test."""
from datetime import UTC, datetime, timedelta
import json
from xml.etree import ElementTree as ET

from fastapi import HTTPException

from basswiesn.app.models import RuntimeState
from basswiesn.app.services.protected_devices import require_unprotected_device

# Fixed command: no user-supplied paths, writes, daemon starts or secret reads.
# Only boolean observations leave the radio; never return the hosts file.
READINESS_COMMAND = """test -d /mnt/nv || exit 4
if test -e /mnt/nv/remote_services; then echo PERSISTENT=1; else echo PERSISTENT=0; fi
if test -e /mnt/nv/remote_services || test -e /tmp/remote_services || test -e /etc/remote_services; then echo SERVICES=1; else echo SERVICES=0; fi
if test -r /etc/hosts; then
 if grep -v '^[[:space:]]*#' /etc/hosts | grep -Eq '[[:space:]](content\\.api\\.bose\\.io|streaming\\.bose\\.com)([[:space:]]|$)'; then echo REDIRECT=1; else echo REDIRECT=0; fi
fi
echo PROBE_COMPLETE
"""


def empty_readiness(device_id):
    return {"device_id": device_id, "ssh": "unknown", "persistent_ssh": None,
            "remote_services": None, "host_redirect": None, "factory_fix": None,
            "observed_at": None, "provenance": "NOT_PROBED", "stale": False,
            "persistent_ssh_boot_verified": False, "redirect_destination_verified": False}


def cached_readiness(db, device_id, *, now=None):
    value = empty_readiness(device_id)
    row = db.query(RuntimeState).filter(RuntimeState.key == f"device:{device_id}:readiness").one_or_none()
    if row is None:
        return value
    try:
        cached = json.loads(row.value)
        stamp = datetime.fromisoformat(cached["observed_at"])
        if stamp.tzinfo is None or cached.get("device_id") != device_id:
            return value
        current = now or datetime.now(UTC)
        stale = not timedelta(0) <= current - stamp <= timedelta(minutes=15)
        value.update({key: cached[key] for key in value if key in cached})
        value["ssh"] = value["ssh"] if value["ssh"] in {"unknown", "available", "not_verified"} else "unknown"
        for field in ("persistent_ssh", "remote_services", "host_redirect"):
            if type(value[field]) is not bool:
                value[field] = None
        value["persistent_ssh_boot_verified"] = False
        value["redirect_destination_verified"] = False
        value["stale"] = stale
        # Keep a date, not a stale green status.
        if stale:
            value.update(ssh="unknown", persistent_ssh=None, remote_services=None, host_redirect=None)
        return value
    except (TypeError, ValueError, KeyError):
        return empty_readiness(device_id)


async def probe_readiness(device, db, *, client_factory, ssh_runner):
    require_unprotected_device(device, action="readiness_probe", requester="explicit_ui", method="SSH", endpoint="readiness")
    # A failed fresh identity check must not leave an earlier green badge.
    key = f"device:{device.device_id}:readiness"
    db.query(RuntimeState).filter(RuntimeState.key == key).delete(synchronize_session=False)
    db.commit()
    client = client_factory(device.ip_address, device_id=device.device_id, request_purpose="readiness_identity")
    try:
        info = ET.fromstring(await client.get_xml("/info"))
    except Exception:
        raise HTTPException(status_code=502, detail={"error": "identity_read_failed"}) from None
    if info.tag != "info" or info.get("deviceID", "").upper() != device.device_id.upper():
        raise HTTPException(status_code=409, detail={"error": "identity_mismatch"})
    require_unprotected_device(device, action="readiness_probe", requester="explicit_ui", method="SSH", endpoint="readiness")
    result = empty_readiness(device.device_id)
    result.update(observed_at=datetime.now(UTC).isoformat(), provenance="EXPLICIT_READ_ONLY")
    try:
        probe = await ssh_runner(device.ip_address, "root", READINESS_COMMAND, timeout=10)
        lines = str(probe.get("stdout", ""))[:1024].splitlines()
        if probe.get("returncode") == 0 and "PROBE_COMPLETE" in lines:
            result["ssh"] = "available"
            for marker, field in (("PERSISTENT", "persistent_ssh"), ("SERVICES", "remote_services"), ("REDIRECT", "host_redirect")):
                observations = [line for line in lines if line.startswith(marker + "=")]
                if observations in ([marker + "=0"], [marker + "=1"]):
                    result[field] = observations == [marker + "=1"]
        else:
            result["ssh"] = "not_verified"
    except Exception:
        result["ssh"] = "not_verified"
    # No command, stderr, host-file content, authentication data or transport
    # exception is saved. Inability to authenticate does not prove no sshd.
    key = f"device:{device.device_id}:readiness"
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        row = RuntimeState(key=key)
        db.add(row)
    row.value = json.dumps(result)
    row.updated_at = datetime.now(UTC)
    db.commit()
    return result
