"""Bounded, pinned SSH for fixed presentation operations; never retries a write."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from basswiesn.app.services.action_journal import record_transport_attempt
from basswiesn.app.services.network_safety import assert_transport_allowed
from .presentation_model import BASE, FILES, HASH, LIMIT, OLD_BASES, PresentationError, Target
from .ssh_runner import SshConfig, _credential_args, _ssh_options


def envelope(target: Target) -> str:
    target.validate()
    return f'''set -eu
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
[ "$(id -u)" = 0 ]
match=no
for p in /sys/class/net/*/address; do
 [ -f "$p" ] || continue
 value=$(tr -d ':\\n' < "$p" | tr a-f A-F)
 case " {' '.join(target.protected)} " in *" $value "*) exit 41;; esac
 [ "$value" != {target.identity} ] || match=yes
done
[ "$match" = yes ]
[ "$(cat /proc/variant)" = {target.variant} ]
grep -q ' 27.0.6.46330.5043500$' /etc/FS_VERSION
[ "$(cat /sys/class/graphics/fb0/virtual_size)" = 128,100 ]
[ "$(cat /sys/class/graphics/fb0/bits_per_pixel)" = 8 ]
[ "$(cat /sys/class/graphics/fb0/stride)" = 128 ]
hash() {{ sha256sum "$1" | cut -d' ' -f1; }}
regular() {{ [ -f "$1" ] && [ ! -L "$1" ]; }}
[ "$(hash /etc/init.d/shelby_local)" = 40c83d45cdc678442922bddc4a3553ab2292607d8e1854aa1fa4f08676baf169 ]
[ "$(hash /etc/init.d/SoundTouch)" = 81ad531b237629c40e9129257b024a50898ada6c56a5cb17ae70b4019c840f39 ]
[ "$(hash /etc/init.d/rc)" = 3989bca468123b737c853329e47766bb33e826e0641e71bc18a9a180107a13a8 ]
[ "$(readlink /etc/rc5.d/S97shelby_local)" = ../init.d/shelby_local ]
[ "$(readlink /etc/rc5.d/S99SoundTouch)" = ../init.d/SoundTouch ]
[ ! -e /mnt/nv/shepherd ]
'''


def inspect_command() -> str:
    script = '''value_hash() { if regular "$1"; then printf '%s' "$(hash "$1")"; else printf absent; fi; }
printf 'boot=%s\\n' "$(cat /proc/sys/kernel/random/boot_id)"
'''
    for name, path in zip(("app", "battery", "splash_a", "splash_b", "rc"), (*FILES, "mnt/nv/rc.local")):
        script += f'printf "{name}=%s\\n" "$(value_hash /{path})"\n'
    script += '''root_ro=no
if awk '$1=="ubi0:rootfs" && $2=="/" && $3=="ubifs" && $4 ~ /^ro(,|$)/ {ok=1} END {exit !ok}' /proc/mounts; then root_ro=yes; fi
printf 'root_ro=%s\\n' "$root_ro"
base=none
'''
    script += f'for p in {" ".join((BASE, *OLD_BASES))}; do\n'
    script += ''' if [ -e "$p" ] || [ -L "$p" ]; then
  [ -d "$p" ] && [ ! -L "$p" ] && [ "$base" = none ]
  base=$p
 fi
done
printf 'base=%s\\ncontrol=%s\\nrc_installed=%s\\n' "$base" "$(value_hash "$base/control.sh")" "$(value_hash "$base/rc.installed")"
healthy=none; pending=no; bind_ro=no
if regular "$base/enabled" && regular "$base/last-healthy-boot"; then healthy=$(head -c 37 "$base/last-healthy-boot"); fi
[ ! -e "$base/pending" ] || pending=yes
if regular "$base/BoseApp.candidate" && [ "$(stat -c %d:%i /opt/Bose/BoseApp)" = "$(stat -c %d:%i "$base/BoseApp.candidate")" ]; then
 if awk '$2=="/opt/Bose/BoseApp" && $4 ~ /^ro(,|$)/ {ok=1} END {exit !ok}' /proc/mounts; then bind_ro=yes; fi
fi
printf 'healthy=%s\\npending=%s\\nbind_ro=%s\\n' "$healthy" "$pending" "$bind_ro"
printf 'space=%s\\nroot_space=%s\\n' "$(df -Pk /mnt/nv | awk 'NR==2 {print $4}')" "$(df -Pk / | awk 'NR==2 {print $4}')"
'''
    return script


def backup_command() -> str:
    paths = list(FILES) + ["mnt/nv/rc.local"]
    for base in (BASE, *OLD_BASES):
        paths.extend(base.lstrip("/") + "/" + part for part in (
            "control.sh", "rc.installed", "SHA256SUMS", "backup/splash.original",
            "backup/rc.original", "backup/SHA256SUMS", "backup.receipt"))
    return 'cd /\nset --\ntotal=0\nfor p in ' + " ".join(paths) + f'''; do
 if [ -e "$p" ] || [ -L "$p" ]; then
  regular "$p"
  total=$((total+$(stat -c %s "$p"))); [ "$total" -le {LIMIT} ]
  set -- "$@" "$p"
 fi
done
tar -cf - "$@"
'''


def mutation_command(action: str, before: dict[str, str], hashes: dict[str, str], archive_sha: str) -> str:
    allowed = {"control.sh", "rc.installed", "BoseApp.candidate", "splash.candidate"}
    if set(hashes) != allowed or any(not HASH.fullmatch(v) for v in hashes.values()):
        raise PresentationError("PRESENTATION_PACKAGE_RECEIPT_INVALID")
    if any(not HASH.fullmatch(before.get(k, "")) for k in ("app", "battery", "splash_a", "splash_b")):
        raise PresentationError("PRESENTATION_BASELINE_INVALID")
    if before.get("rc") != "absent" and not HASH.fullmatch(before.get("rc", "")):
        raise PresentationError("PRESENTATION_STARTUP_HASH_INVALID")
    command = f'base={BASE}\n[ "$(hash /opt/Bose/BatteryMonitor)" = {before["battery"]} ]\n'
    if action == "stage":
        if not HASH.fullmatch(archive_sha):
            raise PresentationError("PRESENTATION_ARCHIVE_HASH_INVALID")
        for field, path in zip(("app", "splash_a", "splash_b"), (FILES[0], FILES[2], FILES[3])):
            command += f'[ "$(hash /{path})" = {before[field]} ]\n'
        if before["rc"] == "absent":
            command += '[ ! -e /mnt/nv/rc.local ] && [ ! -L /mnt/nv/rc.local ]\n'
        else:
            command += f'regular /mnt/nv/rc.local && [ "$(hash /mnt/nv/rc.local)" = {before["rc"]} ]\n'
        command += f'''[ ! -e "$base" ] && [ ! -L "$base" ]
[ ! -e "$base-upload" ] && [ ! -L "$base-upload" ]
[ "$(df -Pk /mnt/nv | awk 'NR==2 {{print $4}}')" -ge 24000 ]
mkdir -m700 "$base-upload"
cd "$base-upload"
head -c {LIMIT+1} > incoming.tar
[ "$(stat -c %s incoming.tar)" -le {LIMIT} ]
[ "$(hash incoming.tar)" = {archive_sha} ]
mkdir -m700 package
tar -xf incoming.tar -C package
cd package
sha256sum -c SHA256SUMS >/dev/null
'''
        for name, value in sorted(hashes.items()):
            command += f'regular {name} && [ "$(hash {name})" = {value} ]\n'
        command += '''mkdir -m700 backup
cp -p /usr/bin/splash-write backup/splash.original
if [ -f /mnt/nv/rc.local ]; then cp -p /mnt/nv/rc.local backup/rc.original; fi
(cd backup; sha256sum splash.original > SHA256SUMS; if [ -f rc.original ]; then sha256sum rc.original >> SHA256SUMS; fi)
cd ..
mv package "$base"
sync
# Off-device candidate is already durable; do not retain a duplicate upload.
'''
        command += f'''[ "$(hash incoming.tar)" = {archive_sha} ]
rm {BASE}-upload/incoming.tar
cd /mnt/nv
rmdir {BASE}-upload
cat "$base/backup/SHA256SUMS"
'''
    elif action == "receipt":
        if not HASH.fullmatch(archive_sha):
            raise PresentationError("PRESENTATION_BACKUP_RECEIPT_INVALID")
        command += f'''[ "$(hash "$base/backup/SHA256SUMS")" = {archive_sha} ]
[ ! -e "$base/backup.receipt" ] && [ ! -L "$base/backup.receipt" ]
printf '%s\\n' {archive_sha} > "$base/backup.receipt"
sync
'''
    elif action in {"activate", "undo"}:
        command += f'''regular "$base/control.sh"
[ "$(hash "$base/control.sh")" = {hashes['control.sh']} ]
/bin/sh "$base/control.sh" configure {"1 1" if action == "activate" else "0 0"} --ack-private-radio-patches
'''
    else:
        raise PresentationError("PRESENTATION_OPERATION_INVALID")
    return command


class PresentationTransport:
    def __init__(self, ip: str, target: Target, config: SshConfig):
        # Presentation operations already require UID 0 and exact board/image
        # checks. These radios support passwordless root SSH; an unset generic
        # SSH username must not disable that established setup path. Explicit
        # non-root configuration and unpinned host keys still fail closed.
        self.ip, self.target = ip, target
        self.config = replace(config, username="root") if not config.username else config

    async def exchange(self, operation: str, *, before: dict[str, str] | None = None,
                       hashes: dict[str, str] | None = None, data: bytes = b"", archive_sha: str = "",
                       directory: str = "", entries: dict[str, str] | None = None) -> bytes:
        ip = assert_transport_allowed(self.ip, device_id=self.target.identity, transport="SSH", approved_only=False)
        self.target.validate()
        if self.config.host_key_policy != "strict" or self.config.username != "root":
            raise PresentationError("PRESENTATION_PINNED_ROOT_SSH_REQUIRED")
        if len(data) > LIMIT or (data and operation != "stage"):
            raise PresentationError("PRESENTATION_INPUT_INVALID")
        if operation in {"cleanup_backup", "cleanup_remove", "cleanup_absent"}:
            from .presentation_cleanup import cleanup_command
            command = cleanup_command(operation, before or {}, hashes or {}, directory, entries)
        elif operation in {"restore_original", "restore_archive"}:
            from .presentation_restore import restore_command
            command = restore_command(operation, before or {}, hashes or {})
        else:
            command = inspect_command() if operation == "inspect" else backup_command() if operation == "backup" else mutation_command(operation, before or {}, hashes or {}, archive_sha)
        credential = _credential_args(self.config)
        args = (credential + ["ssh"]) if self.config.password_file and not self.config.private_key_file else ["ssh", *credential]
        args += ["-F", "/dev/null", "-T", *_ssh_options(self.config), "-o", "ConnectionAttempts=1",
                 "-o", "GlobalKnownHostsFile=/dev/null", "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes",
                 f"root@{ip}", envelope(self.target) + command]
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def read(stream: asyncio.StreamReader, limit: int) -> bytes:
            output = bytearray()
            while chunk := await stream.read(65536):
                output.extend(chunk)
                if len(output) > limit:
                    raise PresentationError("PRESENTATION_OUTPUT_LIMIT")
            return bytes(output)
        async def send() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
        assert proc.stdout is not None and proc.stderr is not None
        tasks = [asyncio.create_task(read(proc.stdout, LIMIT)), asyncio.create_task(read(proc.stderr, 8192)), asyncio.create_task(send())]
        outcome = "uncertain"
        try:
            async with asyncio.timeout(90):
                results = await asyncio.gather(*tasks)
                if await proc.wait() != 0:
                    raise PresentationError("PRESENTATION_SSH_REFUSED_READBACK_REQUIRED")
                outcome = "transport_completed_not_verified"
                assert isinstance(results[0], bytes)
                return results[0]
        except TimeoutError as exc:
            raise PresentationError("PRESENTATION_TIMEOUT_READBACK_REQUIRED") from exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            if operation not in {"inspect", "backup", "cleanup_backup", "cleanup_absent"}:
                record_transport_attempt(ip_address=ip, device_id=self.target.identity,
                    action="setup_presentation_" + operation, trigger="setup", result=outcome, verified=False)
