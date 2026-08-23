"""Transport-level safety gates for setup and hardware tooling.

Every identity-bearing operation is checked against the runtime database and
the centrally configured protected-device policy. Public builds contain no
installation-specific allowlist.
"""

from __future__ import annotations

import ipaddress

from basswiesn.app.services.protected_devices import is_device_access_protected
from basswiesn.app.services.network_security import validate_outbound_host


class NetworkSafetyError(PermissionError):
    """Raised before an unsafe or out-of-scope transport can be opened."""


def normalize_ip(value: str | None) -> str:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return str(value or "").strip()


def assert_transport_allowed(
    ip_address: str,
    *,
    device_id: str = "",
    transport: str = "unknown",
    approved_only: bool = False,
) -> str:
    """Validate a target without performing DNS or network I/O."""

    ip = normalize_ip(ip_address)
    normalized_id = str(device_id or "").strip().upper()
    if approved_only and not normalized_id:
        raise NetworkSafetyError(
            f"target {ip or '<empty>'} requires an exact database-backed device identity"
        )
    if is_device_access_protected(ip, normalized_id):
        raise NetworkSafetyError(
            f"protected device access blocked before {transport} transport"
        )
    validation = validate_outbound_host(ip)
    if not validation.ok:
        raise NetworkSafetyError(
            f"target blocked before {transport} transport: {validation.reason}"
        )
    return validation.addresses[0]
