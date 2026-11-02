"""Recover space only from inactive, backed-up presentation packages.

No wildcard removal, recursive deletion, rootfs remount or running-app change.
The complete private tar is fsynced off-device before individual files can be
unlinked. Unknown files/links, changed state and uncertain replies fail closed.
"""
from __future__ import annotations

import io
from pathlib import PurePosixPath
import re
import shlex
import tarfile
from typing import Any

from .backup import write_artifact, write_json_artifact
from .presentation import read_private
from .presentation_model import BASE, OLD_BASES, HASH, LIMIT, PresentationError, digest, parse_snapshot
from .presentation_restore import restored

# Historical research packages are never swept automatically. Their exact names
# are accepted only for an explicitly requested maintenance operation after a
# verified restore. Normal restore cleans just its own archived installation.
TRIALS = frozenset({
    "basswiesn-display-trial", "basswiesn-display-trial-acceptance1-preserved",
    *(f"basswiesn-display-trial-attempt{i}-preserved" for i in range(2, 6)),
    "basswiesn-bootlogo-trial", "basswiesn-bootlogo-trial-prior-preserved",
    "basswiesn-bootlogo-trial-attempt1-preserved", "basswiesn-bootlogo-trial-attempt2-preserved",
    "basswiesn-wifi-trial",
})
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def allowed_directory(directory: str, plan: dict[str, str]) -> str:
    if plan.get("base") not in (BASE, *OLD_BASES) or not UUID.fullmatch(plan.get("boot", "")):
        raise PresentationError("CLEANUP_RESTORE_PLAN_INVALID")
    expected = plan["base"] + ".restored-" + plan["boot"]
    if directory != expected and directory not in {"/mnt/nv/" + name for name in TRIALS}:
        raise PresentationError("CLEANUP_DIRECTORY_NOT_OWNED")
    return PurePosixPath(directory).name


def manifest(raw: bytes, name: str) -> dict[str, str]:
    """Validate without extraction; directories map to 'dir', files to SHA256."""
    if len(raw) > LIMIT or not SEGMENT.fullmatch(name):
        raise PresentationError("CLEANUP_ARCHIVE_LIMIT")
    result: dict[str, str] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for entry in archive:
                path = entry.name.rstrip("/")
                parts = path.split("/")
                if (len(result) >= 512 or path in result or parts[0] != name or len(parts) > 8
                        or any(p in (".", "..") or not SEGMENT.fullmatch(p) for p in parts)
                        or not (entry.isdir() or entry.isfile()) or entry.linkname
                        or entry.size < 0 or entry.size > LIMIT):
                    raise PresentationError("CLEANUP_ARCHIVE_UNSAFE")
                total += entry.size
                if total > LIMIT:
                    raise PresentationError("CLEANUP_ARCHIVE_LIMIT")
                if entry.isdir():
                    result[path] = "dir"
                else:
                    stream = archive.extractfile(entry)
                    if stream is None:
                        raise PresentationError("CLEANUP_ARCHIVE_UNSAFE")
                    data = stream.read(LIMIT + 1)
                    if len(data) != entry.size:
                        raise PresentationError("CLEANUP_ARCHIVE_TRUNCATED")
                    result[path] = digest(data)
    except (tarfile.TarError, EOFError) as exc:
        raise PresentationError("CLEANUP_ARCHIVE_INVALID") from exc
    if result.get(name) != "dir" or len(result) < 2:
        raise PresentationError("CLEANUP_ARCHIVE_EMPTY")
    for path in result:
        parent = str(PurePosixPath(path).parent)
        if path != name and result.get(parent) != "dir":
            raise PresentationError("CLEANUP_ARCHIVE_PARENT_INVALID")
    return result


def cleanup_command(operation: str, before: dict[str, str], plan: dict[str, str],
                    directory: str, entries: dict[str, str] | None = None) -> str:
    name = allowed_directory(directory, plan)
    if not restored(plan, before) or before.get("base") != "none":
        raise PresentationError("CLEANUP_ORIGINAL_READBACK_REQUIRED")
    if not UUID.fullmatch(before["boot"]):
        raise PresentationError("CLEANUP_BOOT_INVALID")
    cmd = f'''[ ! -L /mnt ] && [ ! -L /mnt/nv ] || exit 73
[ "$(cat /proc/sys/kernel/random/boot_id)" = {before['boot']} ]
awk '$2=="/" && $4 ~ /^ro(,|$)/ {{ok=1}} END {{exit !ok}}' /proc/mounts
awk '$2=="/opt/Bose/BoseApp" {{bad=1}} END {{exit bad}}' /proc/mounts
'''
    for field, path in (("app", "/opt/Bose/BoseApp"), ("battery", "/opt/Bose/BatteryMonitor"),
                        ("splash_a", "/usr/bin/splash-write"), ("splash_b", "/opt/Bose/splash-write")):
        if not HASH.fullmatch(before[field]):
            raise PresentationError("CLEANUP_BASELINE_INVALID")
        cmd += f'regular {path} && [ "$(hash {path})" = {before[field]} ] || exit 73\n'
    if before["rc"] == "absent":
        cmd += '[ ! -e /mnt/nv/rc.local ] && [ ! -L /mnt/nv/rc.local ] || exit 73\n'
    elif HASH.fullmatch(before["rc"]):
        cmd += f'regular /mnt/nv/rc.local && [ "$(hash /mnt/nv/rc.local)" = {before["rc"]} ] || exit 73\n'
        cmd += f'if grep -F {shlex.quote(directory)} /mnt/nv/rc.local >/dev/null; then exit 71; fi\n'
    else:
        raise PresentationError("CLEANUP_STARTUP_INVALID")
    for base in (BASE, *OLD_BASES):
        cmd += f'[ ! -e {base} ] && [ ! -L {base} ] || exit 73\n'
    if operation == "cleanup_absent":
        return cmd + f'[ ! -e {directory} ] && [ ! -L {directory} ] || exit 73\nprintf "ABSENT\\n"\n'
    cmd += f'''directory={directory}
[ -d "$directory" ] && [ ! -L "$directory" ] || exit 73
awk -v p="$directory" '$2==p || index($2,p"/")==1 {{bad=1}} END {{exit bad}}' /proc/mounts
# Refuse live processes, open files and mapped executables. Ignore our own
# SSH shell command, which necessarily contains the inspected path.
for proc in /proc/[0-9]*; do
 [ "$proc" != /proc/$$ ] || continue
 if [ -r "$proc/cmdline" ]; then
  line=$(tr '\\000' ' ' < "$proc/cmdline" 2>/dev/null) || line=
  case "$line" in *"$directory"*) exit 72;; esac
 fi
 if [ -r "$proc/maps" ] && grep -F "$directory/" "$proc/maps" >/dev/null 2>&1; then exit 72; fi
 for link in "$proc/cwd" "$proc/exe" "$proc"/fd/*; do
  value=$(readlink "$link" 2>/dev/null) || continue
  case "$value" in "$directory"|"$directory"/*) exit 72;; esac
 done
done
cd /mnt/nv
'''
    if operation == "cleanup_backup":
        return cmd + f'tar -cf - {name}\n'
    if operation != "cleanup_remove" or not entries or len(entries) > 512 or entries.get(name) != "dir":
        raise PresentationError("CLEANUP_MANIFEST_REQUIRED")
    for path, value in entries.items():
        parts = path.split("/")
        if (parts[0] != name or any(p in (".", "..") or not SEGMENT.fullmatch(p) for p in parts)
                or (value != "dir" and not HASH.fullmatch(value))):
            raise PresentationError("CLEANUP_MANIFEST_INVALID")
        if path != name and entries.get(str(PurePosixPath(path).parent)) != "dir":
            raise PresentationError("CLEANUP_MANIFEST_INVALID")
    expected_tree = digest(("\n".join(sorted(entries)) + "\n").encode())
    cmd += '''mkdir /mnt/nv/.basswiesn-presentation-cleanup-lock
trap 'rmdir /mnt/nv/.basswiesn-presentation-cleanup-lock' 0
'''
    cmd += f'[ "$(find {name} -print | LC_ALL=C sort | sha256sum | cut -d\' \' -f1)" = {expected_tree} ]\n'
    # Check every file before the first unlink, then recheck at each unlink.
    for path, value in sorted(entries.items(), key=lambda p: (p[0].count("/"), p[0])):
        if value == "dir":
            cmd += f'[ -d {path} ] && [ ! -L {path} ] || exit 73\n'
        else:
            cmd += f'regular {path} && [ "$(stat -c %h {path})" = 1 ] && [ "$(hash {path})" = {value} ] || exit 73\n'
    for path, value in sorted(entries.items()):
        if value != "dir":
            cmd += f'regular {path} && [ "$(stat -c %h {path})" = 1 ] && [ "$(hash {path})" = {value} ] || exit 73\nrm {path}\n'
    for path in sorted((p for p, v in entries.items() if v == "dir"), key=lambda p: (p.count("/"), p), reverse=True):
        cmd += f'rmdir {path}\n'
    return cmd + f'sync\n[ ! -e {directory} ] && [ ! -L {directory} ] || exit 73\nprintf "REMOVED\\n"\n'


async def cleanup(service: Any, row: Any, plan: dict[str, str], directory: str) -> dict[str, Any]:
    """Called inside an explicit setup transaction, never from page load/startup."""
    name = allowed_directory(directory, plan)
    root = service.root(row) / "cleanup" / name
    receipt = root / "receipt.json"
    if receipt.exists():
        raise PresentationError("CLEANUP_EXISTING_INTENT_REVIEW_REQUIRED")
    transport = await service.transport(row)
    before = parse_snapshot(await transport.exchange("inspect"))
    # Validate before any archive access, not only before the deletion.
    cleanup_command("cleanup_backup", before, plan, directory)
    raw = await transport.exchange("cleanup_backup", before=before, hashes=plan, directory=directory)
    entries = manifest(raw, name)
    artifact = write_artifact(root, "package.private.tar", raw, max_bytes=LIMIT)
    if digest(read_private(root / "package.private.tar")) != artifact.sha256:
        raise PresentationError("CLEANUP_OFFDEVICE_BACKUP_CORRUPT")
    current = parse_snapshot(await transport.exchange("inspect"))
    if any(current[k] != before[k] for k in before if k not in ("space", "root_space")):
        raise PresentationError("CLEANUP_PRECONDITION_CHANGED")
    value = {"identity": str(row.device_id).upper(), "directory": directory, "before": before,
             "archive_sha256": artifact.sha256, "entries": entries, "phase": "DELETE_INTENT"}
    write_json_artifact(root, "receipt.json", value)
    reply = await transport.exchange("cleanup_remove", before=before, hashes=plan, directory=directory, entries=entries)
    if reply != b"REMOVED\n":
        raise PresentationError("CLEANUP_REPLY_UNCERTAIN")
    if await transport.exchange("cleanup_absent", before=before, hashes=plan, directory=directory) != b"ABSENT\n":
        raise PresentationError("CLEANUP_ABSENCE_UNCONFIRMED")
    after = parse_snapshot(await transport.exchange("inspect"))
    if any(after[k] != before[k] for k in before if k not in ("space", "root_space")):
        raise PresentationError("CLEANUP_FINAL_READBACK_MISMATCH")
    value.update(phase="VERIFIED", after=after)
    write_json_artifact(root, "receipt.json", value)
    return {"cleanup_verified": True, "removed_files": sum(v != "dir" for v in entries.values()),
            "free_kib": int(after["space"]), "backup_sha256": artifact.sha256}
