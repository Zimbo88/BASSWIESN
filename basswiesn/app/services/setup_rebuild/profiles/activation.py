"""Internal SSH operation allowlist.

The UI and API exchange operation IDs only. No caller can provide a shell
command. The command strings below are implementation details and are not
returned by public status endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InternalOperation:
    operation_id: str
    purpose: str
    read_only: bool
    command: str


OPERATIONS: dict[str, InternalOperation] = {
    "common.backup_runtime_files": InternalOperation(
        "common.backup_runtime_files",
        "read fixed SSH/runtime configuration files for off-device backup",
        True,
        "for p in /mnt/nv/remote_services /mnt/nv/Settings/SystemConfigurationDB.xml /mnt/nv/Settings/CurrentSystemConfiguration.xml /mnt/nv/SoundTouchSdkPrivateCfg.xml /mnt/nv/OverrideSdkPrivateCfg.xml /mnt/nv/rc.local /etc/hosts /etc/init.d/sshd /etc/init.d/dropbear; do if [ -f $p ]; then printf 'BASSWIESN_FILE_BEGIN:%s\\n' $p; base64 $p 2>/dev/null || true; printf 'BASSWIESN_FILE_END\\n'; fi; done; true",
    ),
    "common.read_ssh_state": InternalOperation(
        "common.read_ssh_state",
        "read daemon/persistence markers",
        True,
        "printf BASSWIESN_SSH_STATE; command -v sshd 2>/dev/null || true; command -v dropbear 2>/dev/null || true; ps 2>/dev/null | grep sshd || true; for p in /mnt/nv/remote_services /etc/init.d/sshd /etc/init.d/dropbear /mnt/nv/rc.local; do [ -e $p ] && printf PATH:%s $p; done; true",
    ),
    "common.reboot": InternalOperation(
        "common.reboot",
        "controlled device reboot",
        False,
        "sync; reboot",
    ),
    "common.rollback_ssh": InternalOperation(
        "common.rollback_ssh",
        "restore the setup-created SSH marker only",
        False,
        "if [ -f /mnt/nv/remote_services.basswiesn-backup ]; then cp -p /mnt/nv/remote_services.basswiesn-backup /mnt/nv/remote_services; rm -f /mnt/nv/remote_services.basswiesn-backup; elif [ -f /mnt/nv/remote_services.basswiesn-created ]; then rm -f /mnt/nv/remote_services /mnt/nv/remote_services.basswiesn-created; fi; sync",
    ),
    "stationary.start_ssh": InternalOperation(
        "stationary.start_ssh",
        "profile-fixed temporary SSH start",
        False,
        "if [ -x /etc/init.d/sshd ]; then /etc/init.d/sshd start; elif [ -x /etc/init.d/dropbear ]; then /etc/init.d/dropbear start; else exit 42; fi",
    ),
    "stationary.persist_ssh": InternalOperation(
        "stationary.persist_ssh",
        "profile-fixed persistent SSH marker",
        False,
        "if [ -e /mnt/nv/remote_services ]; then cp -p /mnt/nv/remote_services /mnt/nv/remote_services.basswiesn-backup; else : > /mnt/nv/remote_services; : > /mnt/nv/remote_services.basswiesn-created; fi; : > /tmp/remote_services; sync",
    ),
    "portable.start_ssh": InternalOperation(
        "portable.start_ssh",
        "profile-fixed temporary SSH start for portable firmware",
        False,
        "if [ -x /etc/init.d/sshd ]; then /etc/init.d/sshd start; elif [ -x /etc/init.d/dropbear ]; then /etc/init.d/dropbear start; else exit 42; fi",
    ),
    "portable.persist_ssh": InternalOperation(
        "portable.persist_ssh",
        "profile-fixed persistent SSH marker for portable firmware",
        False,
        "if [ -e /mnt/nv/remote_services ]; then cp -p /mnt/nv/remote_services /mnt/nv/remote_services.basswiesn-backup; else : > /mnt/nv/remote_services; : > /mnt/nv/remote_services.basswiesn-created; fi; : > /tmp/remote_services; sync",
    ),
}


def get_operation(operation_id: str) -> InternalOperation:
    try:
        return OPERATIONS[operation_id]
    except KeyError as exc:
        raise ValueError(f"operation is not allowlisted: {operation_id}") from exc


def public_operation(operation_id: str) -> dict[str, object]:
    operation = get_operation(operation_id)
    return {
        "operation_id": operation.operation_id,
        "purpose": operation.purpose,
        "read_only": operation.read_only,
    }
