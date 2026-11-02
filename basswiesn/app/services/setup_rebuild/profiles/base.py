"""SSH profile contracts used by the setup coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActivationMethod(StrEnum):
    EXISTING_DAEMON = "existing_daemon"
    CLI17000 = "cli17000"
    LOCAL_CONFIG = "local_config"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DeviceFacts:
    device_id: str
    ip_address: str
    model: str
    firmware: str
    product_id: str = ""
    variant: str = ""
    platform: str = ""
    ssh_port_open: bool = False
    ssh_login_ok: bool = False
    cli17000_open: bool = False
    telnet_port_open: bool = False


@dataclass(frozen=True)
class SshProfile:
    key: str
    model_family: str
    supported_model_patterns: tuple[str, ...]
    firmware_patterns: tuple[str, ...]
    product_id: str
    variants: tuple[str, ...]
    platforms: tuple[str, ...]
    ssh_daemon: str
    port: int
    activation_method: ActivationMethod
    persistent_operation: str
    temporary_operation: str
    reboot_operation: str
    status_operation: str
    recovery_operation: str
    risk: str
    limitations: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "model_family": self.model_family,
            "supported_model_patterns": list(self.supported_model_patterns),
            "firmware_patterns": list(self.firmware_patterns),
            "product_id": self.product_id,
            "variants": list(self.variants),
            "platforms": list(self.platforms),
            "ssh_daemon": self.ssh_daemon,
            "port": self.port,
            "activation_method": self.activation_method.value,
            "persistent_operation": self.persistent_operation,
            "temporary_operation": self.temporary_operation,
            "reboot_operation": self.reboot_operation,
            "status_operation": self.status_operation,
            "recovery_operation": self.recovery_operation,
            "risk": self.risk,
            "limitations": list(self.limitations),
        }
