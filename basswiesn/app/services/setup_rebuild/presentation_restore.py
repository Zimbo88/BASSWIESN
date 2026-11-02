"""Explicit display/logo removal; never replace a running application image.

The original application is underneath the read-only cold-boot bind mount.
Disable only the owned startup hook, restore the verified splash writers, then
require a new boot and original hashes before archiving the inactive package.
Other application modifications and BatteryMonitor remain byte-identical.
"""
from __future__ import annotations

import json
import re
from typing import Any

from basswiesn.app.services import radio_patch_policy as policy
from .backup import write_artifact, write_json_artifact
from .presentation import SetupPresentation, read_private
from .presentation_model import BASE, OLD_BASES, FILES, HASH, LIMIT, PresentationError, classify, digest, parse_snapshot, unpack_backup


def restoration_plan(before: dict[str, str], files: dict[str, bytes]) -> dict[str, str]:
    base = before["base"]
    if base not in (BASE, *OLD_BASES):
        raise PresentationError("RESTORE_OWNED_INSTALLATION_REQUIRED")
    prefix = base.lstrip("/") + "/"
    control = files.get(prefix + "control.sh", b"")
    if digest(control) != before["control"] or digest(files.get("mnt/nv/rc.local", b"")) != before["rc"]:
        raise PresentationError("RESTORE_CONTROLLER_BACKUP_DIVERGED")
    # Parse data from a previously hash-approved controller, never source/eval it.
    values = dict(re.findall(rb"^(APP|RC_BEFORE)=([a-z0-9]+)$", control, re.M))
    app = values.get(b"APP", b"").decode("ascii")
    rc = values.get(b"RC_BEFORE", b"absent").decode("ascii")
    if app not in (policy.APP_STOCK, policy.APP_LEGACY):
        raise PresentationError("RESTORE_ORIGINAL_APP_UNKNOWN")
    original = files.get(prefix + "backup/splash.original", b"")
    if digest(original) != policy.SPLASH_STOCK:
        raise PresentationError("RESTORE_ORIGINAL_SPLASH_CORRUPT")
    previous_rc = files.get(prefix + "backup/rc.original")
    if rc == "absent":
        if previous_rc is not None:
            raise PresentationError("RESTORE_STARTUP_BACKUP_CONFLICT")
    elif not HASH.fullmatch(rc) or previous_rc is None or digest(previous_rc) != rc:
        raise PresentationError("RESTORE_ORIGINAL_STARTUP_CORRUPT")
    return {"app": app, "rc": rc, "battery": before["battery"], "base": base,
            "control": before["control"], "boot": before["boot"]}


def restored(plan: dict[str, str], snap: dict[str, str]) -> bool:
    return (snap["boot"] != plan["boot"] and snap["app"] == plan["app"]
            and snap["battery"] == plan["battery"] and snap["rc"] == plan["rc"]
            and snap["splash_a"] == snap["splash_b"] == policy.SPLASH_STOCK
            and snap["bind_ro"] == "no" and snap["root_ro"] == "yes"
            and snap.get("pending") == "no")


def restore_command(operation: str, before: dict[str, str], plan: dict[str, str]) -> str:
    if plan.get("base") not in (BASE, *OLD_BASES):
        raise PresentationError("RESTORE_BASE_INVALID")
    for field in ("app", "battery", "control"):
        if not HASH.fullmatch(plan.get(field, "")):
            raise PresentationError("RESTORE_HASH_INVALID")
    if plan.get("rc") != "absent" and not HASH.fullmatch(plan.get("rc", "")):
        raise PresentationError("RESTORE_HASH_INVALID")
    if not re.fullmatch(r"[a-f0-9-]{36}", plan.get("boot", "")):
        raise PresentationError("RESTORE_BOOT_INVALID")
    base = plan["base"]
    cmd = f'''base={base}
[ -d "$base" ] && [ ! -L "$base" ]
regular "$base/control.sh"
[ "$(hash "$base/control.sh")" = {plan['control']} ]
[ "$(hash /opt/Bose/BatteryMonitor)" = {plan['battery']} ]
'''
    if operation == "restore_archive":
        cmd += f'''[ "$(cat /proc/sys/kernel/random/boot_id)" != {plan['boot']} ]
[ ! -e "$base/enabled" ] && [ ! -L "$base/enabled" ]
[ ! -e "$base/pending" ]
[ "$(hash /opt/Bose/BoseApp)" = {plan['app']} ]
awk '$2=="/opt/Bose/BoseApp" {{bad=1}} END {{exit bad}}' /proc/mounts
[ "$(hash /usr/bin/splash-write)" = {policy.SPLASH_STOCK} ]
[ "$(hash /opt/Bose/splash-write)" = {policy.SPLASH_STOCK} ]
'''
        cmd += ('[ ! -e /mnt/nv/rc.local ] && [ ! -L /mnt/nv/rc.local ]\n' if plan['rc'] == 'absent'
                else f'regular /mnt/nv/rc.local && [ "$(hash /mnt/nv/rc.local)" = {plan["rc"]} ]\n')
        return cmd + f'''archive="$base.restored-{plan['boot']}"
[ ! -e "$archive" ] && [ ! -L "$archive" ]
mv "$base" "$archive"
sync
printf 'ARCHIVED\\n'
'''
    if operation != "restore_original":
        raise PresentationError("RESTORE_OPERATION_INVALID")
    for field, path in zip(("app", "battery", "splash_a", "splash_b", "rc"), (*FILES, "mnt/nv/rc.local")):
        if not HASH.fullmatch(before.get(field, "")):
            raise PresentationError("RESTORE_BASELINE_INVALID")
        cmd += f'regular /{path} && [ "$(hash /{path})" = {before[field]} ]\n'
    cmd += f'''[ "$(cat /proc/sys/kernel/random/boot_id)" = {plan['boot']} ]
[ ! -e "$base/pending" ]
regular "$base/backup/splash.original"
[ "$(hash "$base/backup/splash.original")" = {policy.SPLASH_STOCK} ]
'''
    if plan['rc'] != 'absent':
        cmd += f'regular "$base/backup/rc.original" && [ "$(hash "$base/backup/rc.original")" = {plan["rc"]} ]\n'
    cmd += '''curl -fsS --max-time 5 http://127.0.0.1:8090/now_playing | grep -q 'source="STANDBY"'
awk '$1=="ubi0:rootfs" && $2=="/" && $3=="ubifs" && $4 ~ /^ro(,|$)/ {ok=1} END {exit !ok}' /proc/mounts
awk '$2=="/usr/bin/splash-write" || $2=="/opt/Bose/splash-write" {bad=1} END {exit bad}' /proc/mounts
[ "$(df -Pk / | awk 'NR==2 {print $4}')" -ge 1024 ]
for p in /usr/bin/splash-write /opt/Bose/splash-write; do
 [ ! -e "$p.basswiesn-restore" ] && [ ! -L "$p.basswiesn-restore" ]
 [ "$(stat -c %u:%g:%a:%h "$p")" = 0:0:755:1 ]
done
[ ! -e /mnt/nv/rc.local.basswiesn-restore ] && [ ! -L /mnt/nv/rc.local.basswiesn-restore ]
[ ! -e "$base/restore-disabled" ] && [ ! -L "$base/restore-disabled" ]
[ ! -e "$base/restore-rc" ] && [ ! -L "$base/restore-rc" ]
regular "$base/enabled"
mkdir "$base/lock"
dirty=no
finish() { code=$?; if [ "$dirty" = yes ]; then sync; mount -o remount,ro / || code=81; fi; rmdir "$base/lock" || code=82; exit "$code"; }
trap finish 0
printf 'RESTORE_ORIGINAL_INTENT\n' >> "$base/journal"; sync
mv "$base/enabled" "$base/restore-disabled"
'''
    if plan['rc'] == 'absent':
        cmd += 'mv /mnt/nv/rc.local "$base/restore-rc"\n'
    else:
        cmd += '''cp -p "$base/backup/rc.original" /mnt/nv/rc.local.basswiesn-restore
cmp /mnt/nv/rc.local.basswiesn-restore "$base/backup/rc.original"
mv /mnt/nv/rc.local.basswiesn-restore /mnt/nv/rc.local
'''
    cmd += f'''sync
dirty=yes; mount -o remount,rw /
for p in /usr/bin/splash-write /opt/Bose/splash-write; do
 cp "$base/backup/splash.original" "$p.basswiesn-restore"
 chmod 755 "$p.basswiesn-restore"
 [ "$(hash "$p.basswiesn-restore")" = {policy.SPLASH_STOCK} ]
 sync; mv "$p.basswiesn-restore" "$p"; sync
 [ "$(hash "$p")" = {policy.SPLASH_STOCK} ]
done
mount -o remount,ro /; dirty=no
[ "$(hash /opt/Bose/BatteryMonitor)" = {plan['battery']} ]
printf 'RESTORE_CONFIGURED_REBOOT_REQUIRED\\n'
'''
    return cmd


class PresentationRestore:
    def __init__(self, service: SetupPresentation):
        self.service = service

    def state(self, row: Any) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(read_private(self.service.root(row) / "restore.json", 65536))
        if value["identity"] != row.device_id:
            raise PresentationError("RESTORE_IDENTITY_MISMATCH")
        return value

    def save(self, row: Any, value: dict[str, Any], phase: str) -> None:
        value["phase"] = phase
        write_json_artifact(self.service.root(row), "restore.json", value)

    async def prepare(self, row: Any) -> dict[str, Any]:
        if (self.service.root(row) / "restore.json").exists():
            raise PresentationError("RESTORE_EXISTING_INTENT_REVIEW_REQUIRED")
        transport = await self.service.transport(row)
        before = parse_snapshot(await transport.exchange("inspect"))
        if classify(transport.target, before, self.service.managed(transport.target)) != "existing":
            raise PresentationError("RESTORE_OWNED_INSTALLATION_REQUIRED")
        raw = await transport.exchange("backup")
        files = unpack_backup(raw)
        for path, field in zip(FILES, ("app", "battery", "splash_a", "splash_b")):
            if digest(files[path]) != before[field]:
                raise PresentationError("RESTORE_BACKUP_DIVERGED")
        plan = restoration_plan(before, files)
        after = parse_snapshot(await transport.exchange("inspect"))
        if any(before[k] != after[k] for k in before if k not in ("space", "root_space")):
            raise PresentationError("RESTORE_BASELINE_CHANGED")
        artifact = write_artifact(self.service.root(row), "restore.private.tar", raw, max_bytes=LIMIT)
        if digest(read_private(self.service.root(row) / "restore.private.tar")) != artifact.sha256:
            raise PresentationError("RESTORE_OFFDEVICE_BACKUP_FAILED")
        self.save(row, {"identity": row.device_id, "before": before, "plan": plan, "backup_sha256": artifact.sha256}, "PREPARED")
        return {"restore_prepared": True, "restore_backup_sha256": artifact.sha256}

    async def activate(self, row: Any) -> dict[str, Any]:
        value = self.state(row)
        if value["phase"] != "PREPARED":
            raise PresentationError("RESTORE_NO_AUTOMATIC_WRITE_REPLAY")
        if digest(read_private(self.service.root(row) / "restore.private.tar")) != value["backup_sha256"]:
            raise PresentationError("RESTORE_BACKUP_CORRUPT")
        transport = await self.service.transport(row)
        current = parse_snapshot(await transport.exchange("inspect"))
        if any(current[k] != value["before"][k] for k in value["before"] if k not in ("space", "root_space")):
            raise PresentationError("RESTORE_PRECONDITION_CHANGED")
        self.save(row, value, "WRITE_INTENT")
        result = await transport.exchange("restore_original", before=value["before"], hashes=value["plan"])
        if result != b"RESTORE_CONFIGURED_REBOOT_REQUIRED\n":
            raise PresentationError("RESTORE_REPLY_UNCERTAIN")
        self.save(row, value, "REBOOT_REQUIRED")
        return {"restore_configured": True, "original_verified": False}

    async def verify(self, row: Any) -> dict[str, Any]:
        value = self.state(row)
        if value["phase"] != "REBOOT_REQUIRED":
            raise PresentationError("RESTORE_PHASE_REVIEW_REQUIRED")
        transport = await self.service.transport(row)
        snap = parse_snapshot(await transport.exchange("inspect"))
        if not restored(value["plan"], snap):
            raise PresentationError("RESTORE_READBACK_MISMATCH")
        self.save(row, value, "ARCHIVE_INTENT")
        if await transport.exchange("restore_archive", hashes=value["plan"]) != b"ARCHIVED\n":
            raise PresentationError("RESTORE_ARCHIVE_UNCERTAIN")
        final = parse_snapshot(await transport.exchange("inspect"))
        if final["base"] != "none" or not restored(value["plan"], final):
            raise PresentationError("RESTORE_FINAL_READBACK_MISMATCH")
        value["after"] = final
        self.save(row, value, "VERIFIED")
        write_json_artifact(self.service.registry(row.device_id), "receipt.json", {
            "identity": row.device_id, "verified": False, "state": "ORIGINAL_PRESENTATION_RESTORED"})
        # Cleanup failure must not disguise a verified firmware restore as a
        # rollback failure. Keep its independent evidence and private backup.
        from .presentation_cleanup import cleanup
        try:
            result = await cleanup(self.service, row, value["plan"],
                                   value["plan"]["base"] + ".restored-" + value["plan"]["boot"])
        except (PresentationError, OSError, ValueError, TimeoutError):
            value["cleanup"] = {"cleanup_verified": False, "status": "MANUAL_REVIEW_REQUIRED"}
        else:
            value["cleanup"] = result
        self.save(row, value, "VERIFIED")
        return {"original_verified": True, "battery_preserved": True,
                "presentation_mode": "original", "new_patches_installed": False,
                "presentation_cleanup": value["cleanup"]}
