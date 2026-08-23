"""Fixed CLI-17000 operations for the setup rebuild.

The public API selects operation IDs. Commands are generated only from a
validated ServerTarget and are never accepted from a request body.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import time

from basswiesn.app.services.action_journal import record_transport_attempt
from basswiesn.app.services.network_safety import assert_transport_allowed
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget


READ_CONFIG_COMMAND = "getpdo CurrentSystemConfiguration"
REBOOT_COMMAND = "sys reboot"


@dataclass(frozen=True)
class CliResult:
    operation: str
    responses: tuple[str, ...]
    output: str

    def public_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "responses": list(self.responses),
            "output": " ".join(self.output.split())[:8000],
        }


def route_commands(target: ServerTarget) -> tuple[str, ...]:
    base = f"http://{target.host}:{target.cloud_port}"
    update = f"{base}/updates/soundtouch"
    bmx = f"{base}/bmx/registry/v1/services"
    return (
        f'envswitch boseurls set "{base}" "{update}"',
        f"sys configuration bmxRegistryUrl {bmx}",
        f"sys configuration margeServerUrl {base}",
        f"sys configuration swUpdateUrl {update}",
        f"sys configuration statsServerUrl {base}",
    )


_ROUTE_TAGS = ("bmxRegistryUrl", "margeServerUrl", "swUpdateUrl", "statsServerUrl")


def route_commands_from_values(values: dict[str, str]) -> tuple[str, ...]:
    """Build a rollback command set from a previously read, validated state."""

    missing = [tag for tag in _ROUTE_TAGS if not str(values.get(tag) or "").strip()]
    if missing:
        raise ValueError(f"routing backup is incomplete: {', '.join(missing)}")
    checked: dict[str, str] = {}
    for tag in _ROUTE_TAGS:
        value = str(values[tag]).strip()
        # Values came from the radio readback, but they are still treated as
        # untrusted input before being interpolated into a CLI command.
        if (
            len(value) > 512
            or not re.fullmatch(r"https?://[^\s\"'`;$|&<>]+", value)
        ):
            raise ValueError(f"routing backup contains an unsafe URL for {tag}")
        checked[tag] = value
    return (
        f'envswitch boseurls set "{checked["margeServerUrl"]}" "{checked["swUpdateUrl"]}"',
        f"sys configuration bmxRegistryUrl {checked['bmxRegistryUrl']}",
        f"sys configuration margeServerUrl {checked['margeServerUrl']}",
        f"sys configuration swUpdateUrl {checked['swUpdateUrl']}",
        f"sys configuration statsServerUrl {checked['statsServerUrl']}",
    )


def extract_route_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for tag in ("bmxRegistryUrl", "margeServerUrl", "swUpdateUrl", "statsServerUrl"):
        match = re.search(
            rf"\b{tag}\b\s*\{{\s*text:\s*\"([^\"]+)",
            output or "",
            re.IGNORECASE,
        )
        if match:
            values[tag] = match.group(1).strip()
            continue
        match = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", output or "", re.IGNORECASE)
        if match:
            values[tag] = match.group(1).strip()
    return values


def route_diff(current: dict[str, str], target: ServerTarget) -> list[dict[str, object]]:
    commands = route_commands(target)
    target_base = f"http://{target.host}:{target.cloud_port}"
    expected = {
        "bmxRegistryUrl": f"{target_base}/bmx/registry/v1/services",
        "margeServerUrl": target_base,
        "swUpdateUrl": f"{target_base}/updates/soundtouch",
        "statsServerUrl": target_base,
    }
    return [
        {
            "tag": tag,
            "current": current.get(tag, ""),
            "target": value,
            "changed": current.get(tag, "") != value,
        }
        for tag, value in expected.items()
    ]


async def _send_fixed(
    ip_address: str,
    device_id: str,
    command: str,
    *,
    timeout: float = 8.0,
) -> str:
    allowed = {READ_CONFIG_COMMAND, REBOOT_COMMAND}
    if command not in allowed and not any(command == item for item in _active_route_commands):
        raise ValueError("CLI command is not an internal allowlisted operation")
    target = assert_transport_allowed(
        ip_address,
        device_id=device_id,
        transport="CLI17000",
        approved_only=False,
    )
    reader, writer = await asyncio.wait_for(asyncio.open_connection(target, 17000), timeout=timeout)
    chunks: list[bytes] = []
    try:
        try:
            greeting = await asyncio.wait_for(reader.read(1024), timeout=0.25)
        except asyncio.TimeoutError:
            greeting = b""
        if greeting:
            chunks.append(greeting)
        writer.write((command + "\r\n").encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        for _ in range(8):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=0.75)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return b"".join(chunks).decode("utf-8", errors="replace")


_active_route_commands: set[str] = set()


def _validate_internal_command(command: str) -> None:
    allowed = {READ_CONFIG_COMMAND, REBOOT_COMMAND}
    if command not in allowed and command not in _active_route_commands:
        raise ValueError("CLI command is not an internal allowlisted operation")


async def _send_fixed_batch(
    ip_address: str,
    device_id: str,
    commands: tuple[str, ...],
    *,
    timeout: float = 8.0,
    response_pause: float = 0.75,
) -> tuple[str, ...]:
    """Send one fixed command batch over one CLI-17000 session.

    Firmware 27.x may accept the first persistent URL update and refuse a
    second freshly opened CLI session while its configuration service is
    refreshing.  The radio's established control flow is one connection with
    a short response interval per command.  Commands still come only from the
    internal allowlist; this function never receives browser input.
    """

    if not commands:
        raise ValueError("CLI command batch must not be empty")
    for command in commands:
        _validate_internal_command(command)
    target = assert_transport_allowed(
        ip_address,
        device_id=device_id,
        transport="CLI17000",
        approved_only=False,
    )
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(target, 17000), timeout=timeout
    )
    responses: list[str] = []
    try:
        try:
            greeting = await asyncio.wait_for(reader.read(1024), timeout=0.25)
        except asyncio.TimeoutError:
            greeting = b""
        if greeting:
            responses.append(greeting.decode("utf-8", errors="replace"))
        for command in commands:
            writer.write((command + "\r\n").encode("ascii"))
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            await asyncio.sleep(response_pause)
            chunks: list[bytes] = []
            response_deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < response_deadline:
                try:
                    remaining = max(0.05, response_deadline - asyncio.get_running_loop().time())
                    data = await asyncio.wait_for(reader.read(4096), timeout=min(0.75, remaining))
                except asyncio.TimeoutError:
                    break
                if not data:
                    break
                chunks.append(data)
            responses.append(b"".join(chunks).decode("utf-8", errors="replace"))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return tuple(responses)


async def read_current_config(ip_address: str, device_id: str) -> CliResult:
    output = await _send_fixed(ip_address, device_id, READ_CONFIG_COMMAND)
    return CliResult("read_current_config", (READ_CONFIG_COMMAND,), output)


async def apply_route(
    ip_address: str,
    device_id: str,
    target: ServerTarget,
) -> CliResult:
    commands = route_commands(target)
    started = time.monotonic()
    _active_route_commands.update(commands)
    try:
        responses = await _send_fixed_batch(ip_address, device_id, commands)
        if any("error" in response.lower() or "unknown" in response.lower() for response in responses):
            raise RuntimeError("CLI route command was rejected")
        record_transport_attempt(
            ip_address=ip_address,
            device_id=device_id,
            action="CLI17000 apply_route",
            trigger="setup_rebuild",
            requested_state={"target": target.to_public_dict()},
            result="success",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        record_transport_attempt(
            ip_address=ip_address,
            device_id=device_id,
            action="CLI17000 apply_route",
            trigger="setup_rebuild",
            requested_state={"target": target.to_public_dict()},
            result="failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category=exc.__class__.__name__,
        )
        raise
    finally:
        _active_route_commands.difference_update(commands)
    return CliResult("apply_route", responses, "".join(responses))


async def apply_route_values(
    ip_address: str,
    device_id: str,
    values: dict[str, str],
) -> CliResult:
    commands = route_commands_from_values(values)
    started = time.monotonic()
    _active_route_commands.update(commands)
    try:
        responses = await _send_fixed_batch(ip_address, device_id, commands)
        if any("error" in response.lower() or "unknown" in response.lower() for response in responses):
            raise RuntimeError("CLI routing rollback command was rejected")
        record_transport_attempt(
            ip_address=ip_address,
            device_id=device_id,
            action="CLI17000 restore_route",
            trigger="setup_rebuild",
            requested_state={"routing": values},
            result="success",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        record_transport_attempt(
            ip_address=ip_address,
            device_id=device_id,
            action="CLI17000 restore_route",
            trigger="setup_rebuild",
            requested_state={"routing": values},
            result="failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category=exc.__class__.__name__,
        )
        raise
    finally:
        _active_route_commands.difference_update(commands)
    return CliResult("restore_route", responses, "".join(responses))


async def reboot(ip_address: str, device_id: str) -> CliResult:
    started = time.monotonic()
    try:
        output = await _send_fixed(ip_address, device_id, REBOOT_COMMAND)
        record_transport_attempt(
            ip_address=ip_address,
            device_id=device_id,
            action="CLI17000 reboot",
            trigger="setup_rebuild",
            requested_state={"operation": "controlled_reboot"},
            result="command_sent",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return CliResult("reboot", (REBOOT_COMMAND,), output)
    except Exception as exc:
        record_transport_attempt(
            ip_address=ip_address,
            device_id=device_id,
            action="CLI17000 reboot",
            trigger="setup_rebuild",
            requested_state={"operation": "controlled_reboot"},
            result="failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category=exc.__class__.__name__,
        )
        raise
