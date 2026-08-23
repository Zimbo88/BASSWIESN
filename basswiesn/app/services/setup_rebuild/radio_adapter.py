"""Real, allowlisted radio adapter for the setup-rebuild coordinator.

The adapter is intentionally narrow: it accepts only database-backed,
profile-verified devices, a fixed set of SoundTouch HTTP/XML paths, fixed
CLI-17000 operations, and fixed expert-only SSH operation IDs. It never
accepts a shell command from the browser as an execution primitive.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from html import escape as html_escape
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any
from xml.etree import ElementTree as ET

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.models import Station
from basswiesn.app.services.network_safety import assert_transport_allowed
from basswiesn.app.services.network_security import validate_outbound_host
from basswiesn.app.services.orion import (
    ORION_STATION_PATH,
    StationDescriptor,
    encode_orion_data,
)
from basswiesn.app.services.setup_rebuild.backup import (
    BaselineArtifact,
    baseline_root,
    write_artifact,
    write_json_artifact,
    read_json_artifact,
    write_baseline_metadata,
)
from basswiesn.app.services.setup_rebuild.cli17000 import (
    apply_route,
    apply_route_values,
    extract_route_values,
    read_current_config,
    reboot as cli_reboot,
    route_diff,
)
from basswiesn.app.services.setup_rebuild.profiles import DeviceFacts, detect_profile
from basswiesn.app.services.setup_rebuild.profiles.verification import parse_ssh_state
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
from basswiesn.app.services.setup_rebuild.ssh_runner import (
    SshConfig,
    probe_ssh_port,
    run_internal_operation,
)
from basswiesn.app.services.setup_rebuild.states import SshStatus
from basswiesn.app.services.setup_rebuild.audio_safety import (
    clear_audio_safety,
    load_audio_safety,
    lock_audio_safety,
)
from basswiesn.app.services.xml import content_item_xml
from basswiesn.app.services.action_journal import record_transport_attempt


_REQUIRED_HTTP_BACKUP_ENDPOINTS = {
    "/info": "info.xml",
    "/networkInfo": "networkInfo.xml",
    "/sources": "sources.xml",
    "/presets": "presets.xml",
    "/now_playing": "now_playing.xml",
    "/volume": "volume.xml",
}
_OPTIONAL_HTTP_BACKUP_ENDPOINTS = {
    "/capabilities": "capabilities.xml",
    "/getZone": "getZone.xml",
    "/supportedURLs": "supportedURLs.xml",
    "/bass": "bass.xml",
    "/marge": "marge.xml",
    "/serviceAvailability": "serviceAvailability.xml",
}
_REDACTION_RE = re.compile(
    r"(?is)(password|passwd|secret|token|authorization|private[_ -]?key)"
    r"(\s*[=:]\s*|\s*>\s*)([^<\s,;\"']+)"
)

_SETUP_PLAYBACK_STATION = {
    "name": "BASSWIESN Setup-Wiedergabeprüfung",
    "stream_url": "https://dispatcher.rndfnk.com/br/br1/obb/mp3/mid",
    "stream_format": "mp3",
    "stream_mime": "audio/mpeg",
}


def _local_account_id(device_id: str) -> str:
    normalized = str(device_id or "").strip().upper()
    digest = hashlib.sha256(normalized.encode("ascii", errors="ignore")).digest()
    return str(1_000_000 + int.from_bytes(digest[:4], "big") % 9_000_000)


def _read_or_create_marge_auth_token() -> str:
    """Load an explicit secret or create a private per-install token.

    A normal user must not need to discover and configure a hidden environment
    variable.  The generated token is an implementation detail stored with
    restrictive permissions below the configured data directory.
    """

    path = str(getattr(get_settings(), "marge_auth_token_file", "") or "").strip()
    if not path:
        path = str(get_settings().data_dir / "setup-rebuild" / "marge-auth-token")
    token_path = Path(path)
    try:
        token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not token_path.exists():
            try:
                with token_path.open("x", encoding="utf-8") as handle:
                    handle.write(secrets.token_urlsafe(32))
                os.chmod(token_path, 0o600)
            except FileExistsError:
                pass
        value = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Das lokale Setup-Geheimnis konnte nicht sicher angelegt werden.") from exc
    if not value or "\n" in value or "\r" in value or len(value) > 4096:
        raise RuntimeError("Das lokale Setup-Geheimnis ist ungültig.")
    return value


def _redact(value: str) -> str:
    return _REDACTION_RE.sub(r"\1\2***REDACTED***", str(value or ""))


def _json(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _xml_root(value: str) -> ET.Element:
    return ET.fromstring(value or "")


def _volume(value: str) -> int:
    root = _xml_root(value)
    text = root.findtext("actualvolume") or root.findtext("targetvolume") or "-1"
    return int(float(text))


def _volume_state(value: str) -> tuple[int, bool]:
    root = _xml_root(value)
    text = root.findtext("actualvolume") or root.findtext("targetvolume") or "-1"
    muted_text = (
        root.findtext("muteenabled")
        or root.findtext("mute")
        or root.attrib.get("muted")
        or "false"
    )
    return int(float(text)), str(muted_text).strip().lower() == "true"


def _firmware(root: ET.Element) -> str:
    return root.findtext(".//softwareVersion", "").strip()


def _device_expected(row: Any) -> tuple[str, dict[str, str]]:
    ip_address = str(getattr(row, "ip_address", "")).strip()
    device_id = str(getattr(row, "device_id", "")).strip().upper()
    expected = {
        "device_id": device_id,
        "name": str(getattr(row, "name", "") or device_id),
        "model": str(getattr(row, "expected_model", "") or ""),
    }
    if not ip_address or not device_id or not expected["model"]:
        raise RuntimeError("Der gespeicherte Radio-Datensatz ist unvollständig.")
    target = assert_transport_allowed(
        ip_address,
        device_id=device_id,
        transport="setup-rebuild",
        approved_only=False,
    )
    return target, expected


def _baseline_for(row: Any, timestamp: str) -> Path:
    ip_address, expected = _device_expected(row)
    del ip_address
    return baseline_root(timestamp, expected["device_id"])


class RadioSetupAdapter:
    """Perform one sequential setup run against validated local radios."""

    def __init__(self, *, ssh_config: SshConfig | None = None, reboot_timeout: int = 180):
        self.ssh_config = ssh_config or SshConfig.from_settings()
        self.timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.reboot_timeout = max(30, int(reboot_timeout))

    def _client(self, row: Any) -> SoundTouchClient:
        ip_address, expected = _device_expected(row)
        return SoundTouchClient(
            ip_address,
            device_id=expected["device_id"],
            request_purpose="setup_rebuild",
            trigger="setup_rebuild_coordinator",
        )

    async def identify(self, row: Any) -> dict[str, Any]:
        client = self._client(row)
        _, expected = _device_expected(row)
        raw = await client.get_xml("/info")
        root = _xml_root(raw)
        actual_id = root.attrib.get("deviceID", "").strip().upper()
        model = root.findtext("type", "").strip()
        firmware = _firmware(root)
        variant = root.findtext("variant", "").strip()
        platform = root.findtext("moduleType", "").strip()
        observed_product_id = next(
            (
                str(root.findtext(tag, "") or "").strip()
                for tag in ("productID", "productId", "product_id", "productCode")
                if str(root.findtext(tag, "") or "").strip()
            ),
            "",
        )
        network_ip = ""
        for node in root.findall("networkInfo"):
            if node.findtext("ipAddress", "").strip():
                network_ip = node.findtext("ipAddress", "").strip()
                break
        if actual_id != expected["device_id"]:
            raise RuntimeError("radio identity changed during setup")
        if expected["model"].lower() not in model.lower():
            raise RuntimeError("radio model does not match the approved profile")
        match = detect_profile(
            DeviceFacts(
                expected["device_id"],
                str(getattr(row, "ip_address", "")),
                model,
                firmware,
                product_id=observed_product_id,
                variant=variant,
                platform=platform,
            )
        )
        if match.profile is None:
            raise RuntimeError(f"Critical writes are blocked: {match.reason}")
        return {
            "verified": True,
            "device_id": actual_id,
            "model": model,
            "firmware": firmware,
            "firmware_build": firmware.split()[0],
            "product_id": match.product_id,
            "product_id_provenance": "RADIO_INFO" if observed_product_id else "PROFILE_DERIVED",
            "variant": variant,
            "platform": platform,
            "write_profile": match.profile.key,
            "network_ip": network_ip,
            "name": root.findtext("name", "").strip(),
        }

    async def backup(self, row: Any) -> dict[str, Any]:
        client = self._client(row)
        ip_address, expected = _device_expected(row)
        root = _baseline_for(row, self.timestamp)
        artifacts: dict[str, str] = {}
        for endpoint, filename in _REQUIRED_HTTP_BACKUP_ENDPOINTS.items():
            raw = await client.get_xml(endpoint)
            artifact = write_artifact(root, filename, _redact(raw))
            artifacts[filename] = artifact.sha256
        optional: dict[str, dict[str, Any]] = {}
        for endpoint, filename in _OPTIONAL_HTTP_BACKUP_ENDPOINTS.items():
            try:
                raw = await client.get_xml(endpoint)
            except Exception as exc:
                optional[endpoint] = {
                    "captured": False,
                    "error_type": exc.__class__.__name__,
                }
                continue
            artifact = write_artifact(root, filename, _redact(raw))
            artifacts[filename] = artifact.sha256
            optional[endpoint] = {"captured": True, "sha256": artifact.sha256}

        # The normal setup path needs only the regular SoundTouch HTTP API and
        # CLI 17000.  SSH backup is intentionally not attempted here.
        ssh_status = "NOT_REQUIRED"

        metadata = {
            "device_id": expected["device_id"],
            "ip_address": ip_address,
            "model": expected["model"],
            "ssh_backup_status": ssh_status,
            "firmware": str(_json(getattr(row, "evidence_json", "{}")).get("firmware") or ""),
            "product_id": str(_json(getattr(row, "evidence_json", "{}")).get("product_id") or ""),
            "variant": str(_json(getattr(row, "evidence_json", "{}")).get("variant") or ""),
            "platform": str(_json(getattr(row, "evidence_json", "{}")).get("platform") or ""),
            "http_endpoints": sorted(_REQUIRED_HTTP_BACKUP_ENDPOINTS),
            "optional_http_endpoints": optional,
            "rollback_scope": "ROUTING_ONLY",
            "not_restorable": [
                "original account authentication token",
                "provider session/lease state",
                "runtime source/playback position",
            ],
            "secret_values": "not stored",
        }
        write_baseline_metadata(root, metadata)
        manifest = write_json_artifact(
            root,
            "backup-manifest.json",
            {
                "artifacts": artifacts,
                "ssh_backup_status": ssh_status,
                "optional_endpoints": optional,
                "rollback_scope": "ROUTING_ONLY",
            },
        )
        artifacts["backup-manifest.json"] = manifest.sha256
        return {
            "backup_path": str(root),
            "sha256": artifacts,
            "verified": bool(artifacts),
            "ssh_backup_status": ssh_status,
            "optional_endpoints": optional,
            "rollback_scope": "ROUTING_ONLY",
        }

    def _facts(self, row: Any) -> DeviceFacts:
        evidence = _json(getattr(row, "evidence_json", "{}"))
        return DeviceFacts(
            device_id=str(row.device_id),
            ip_address=str(row.ip_address),
            model=str(evidence.get("model") or row.expected_model),
            firmware=str(evidence.get("firmware") or ""),
            product_id=str(evidence.get("product_id") or ""),
            variant=str(evidence.get("variant") or ""),
            platform=str(evidence.get("platform") or ""),
        )

    async def ssh_status(self, row: Any) -> dict[str, Any]:
        ip_address, expected = _device_expected(row)
        facts = self._facts(row)
        match = detect_profile(facts)
        if match.profile is None:
            return {
                "ssh_status": SshStatus.SSH_SERVICE_FAILED.value,
                "already_active": False,
                "persistent": False,
                "profile_key": "",
                "reason": match.reason,
            }
        port_open = await probe_ssh_port(
            ip_address,
            device_id=expected["device_id"],
            port=match.profile.port,
            timeout=self.ssh_config.timeout_seconds,
            approved_only=False,
        )
        if not port_open:
            return {
                "ssh_status": SshStatus.SSH_DISABLED.value,
                "already_active": False,
                "persistent": False,
                "profile_key": match.profile.key,
                "reason": "SSH port is closed",
            }
        result = await run_internal_operation(
            ip_address,
            expected["device_id"],
            match.profile.status_operation,
            config=self.ssh_config,
            approved_only=False,
        )
        verified = parse_ssh_state(
            facts,
            port_reachable=True,
            operation_ok=result.status == SshStatus.SSH_REACHABLE.value,
            output=result.output,
        )
        return {
            "ssh_status": verified.status.value,
            "already_active": result.status == SshStatus.SSH_REACHABLE.value,
            "persistent": verified.persistence_detected,
            "profile_key": match.profile.key,
            "daemon_detected": verified.daemon_detected,
            "read_only_command_ok": verified.read_only_command_ok,
            "reason": verified.reason,
        }

    def _profile_for_row(self, row: Any):
        match = detect_profile(self._facts(row))
        if match.profile is None:
            raise RuntimeError(match.reason)
        return match.profile

    async def activate_ssh(self, row: Any) -> dict[str, Any]:
        ip_address, expected = _device_expected(row)
        profile = self._profile_for_row(row)
        result = await run_internal_operation(
            ip_address,
            expected["device_id"],
            profile.temporary_operation,
            config=self.ssh_config,
            approved_only=False,
        )
        if result.status != SshStatus.SSH_REACHABLE.value:
            raise RuntimeError("profile-fixed temporary SSH activation failed")
        return {"ssh_status": SshStatus.SSH_TEMPORARILY_ENABLED.value, "profile_key": profile.key}

    async def persist_ssh(self, row: Any) -> dict[str, Any]:
        ip_address, expected = _device_expected(row)
        profile = self._profile_for_row(row)
        evidence = _json(getattr(row, "evidence_json", "{}"))
        if evidence.get("persistent") or evidence.get("ssh_status") == SshStatus.SSH_PERSISTENTLY_ENABLED.value:
            return {
                "ssh_status": SshStatus.SSH_PERSISTENTLY_ENABLED.value,
                "profile_key": profile.key,
                "changed": False,
            }
        result = await run_internal_operation(
            ip_address,
            expected["device_id"],
            profile.persistent_operation,
            config=self.ssh_config,
            approved_only=False,
        )
        if result.status != SshStatus.SSH_REACHABLE.value:
            raise RuntimeError("profile-fixed persistent SSH activation failed")
        return {
            "ssh_status": SshStatus.SSH_PERSISTENTLY_ENABLED.value,
            "profile_key": profile.key,
            "changed": True,
        }

    async def _wait_for_identity(self, row: Any) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.reboot_timeout
        last_error = "radio did not return after reboot"
        while asyncio.get_running_loop().time() < deadline:
            try:
                identity = await self.identify(row)
                if identity.get("verified"):
                    return identity
            except Exception as exc:  # the device is expected to be offline briefly
                last_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
            await asyncio.sleep(2)
        raise RuntimeError(last_error)

    async def _wait_for_reboot_identity(self, row: Any) -> dict[str, Any]:
        """Verify an actual offline-to-online reboot transition.

        Some firmware builds acknowledge the CLI reboot several seconds before
        their HTTP service goes down. Treating that still-running pre-reboot
        service as the reconnect readback races every following CLI operation.
        """

        deadline = asyncio.get_running_loop().time() + self.reboot_timeout
        offline_seen = False
        last_error = "radio reboot transition was not observed"
        while asyncio.get_running_loop().time() < deadline:
            try:
                identity = await self.identify(row)
                if offline_seen and identity.get("verified"):
                    return identity
            except Exception as exc:
                offline_seen = True
                last_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
            await asyncio.sleep(2)
        if not offline_seen:
            raise RuntimeError("radio never entered the offline phase after reboot")
        raise RuntimeError(last_error)

    async def reboot_verify_ssh(self, row: Any) -> dict[str, Any]:
        ip_address, expected = _device_expected(row)
        profile = self._profile_for_row(row)
        result = await run_internal_operation(
            ip_address,
            expected["device_id"],
            profile.reboot_operation,
            config=self.ssh_config,
            approved_only=False,
        )
        if result.status not in {SshStatus.SSH_REACHABLE.value, SshStatus.SSH_SERVICE_FAILED.value}:
            raise RuntimeError("controlled reboot operation was not dispatched")
        await self._wait_for_reboot_identity(row)
        port_open = await probe_ssh_port(
            ip_address,
            device_id=expected["device_id"],
            port=profile.port,
            timeout=self.ssh_config.timeout_seconds,
            approved_only=False,
        )
        ssh = await run_internal_operation(
            ip_address,
            expected["device_id"],
            profile.status_operation,
            config=self.ssh_config,
            approved_only=False,
        )
        if not port_open or ssh.status != SshStatus.SSH_REACHABLE.value:
            raise RuntimeError("SSH did not verify after reboot")
        parsed = parse_ssh_state(
            self._facts(row),
            port_reachable=True,
            operation_ok=True,
            output=ssh.output,
            after_reboot=True,
        )
        if parsed.status != SshStatus.SSH_VERIFIED_AFTER_REBOOT:
            raise RuntimeError("SSH daemon did not verify after reboot")
        return {
            "ssh_status": parsed.status.value,
            "verified": True,
            "persistent": parsed.persistence_detected,
            "profile_key": profile.key,
        }

    async def _read_current_route_with_retry(self, row: Any) -> Any:
        """Wait for CLI-17000 separately; HTTP and SSH return at different times."""

        ip_address, expected = _device_expected(row)
        deadline = asyncio.get_running_loop().time() + min(90, self.reboot_timeout)
        last_error: BaseException | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await read_current_config(ip_address, expected["device_id"])
            except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                await asyncio.sleep(2)
        if last_error is not None:
            raise last_error
        raise RuntimeError("CLI-17000 did not return after reboot")

    async def backup_routing(self, row: Any) -> dict[str, Any]:
        ip_address, expected = _device_expected(row)
        root = _baseline_for(row, self.timestamp)
        result = await self._read_current_route_with_retry(row)
        values = extract_route_values(result.output)
        if len(values) != 4:
            raise RuntimeError("routing readback is incomplete; no route write is permitted")
        raw_artifact = write_artifact(root, "route-before.txt", _redact(result.output))
        values_artifact = write_json_artifact(root, "route-before.json", values)
        return {
            "routing_backup": True,
            "routing_values": values,
            "sha256": {
                "route-before.txt": raw_artifact.sha256,
                "route-before.json": values_artifact.sha256,
            },
        }

    async def _server_ports(self, target: ServerTarget) -> None:
        for port in (target.web_port, target.cloud_port, target.debug_port):
            validation = validate_outbound_host(target.host, port=port)
            if not validation.ok:
                raise RuntimeError(f"BASSWIESN server target blocked: {validation.reason}")
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(validation.addresses[0], port), timeout=3
                )
            except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
                raise RuntimeError(f"BASSWIESN server port {port} is unreachable") from exc
            writer.close()
            await writer.wait_closed()

    async def _apply_route_with_retry(self, row: Any, target: ServerTarget) -> Any:
        ip_address, expected = _device_expected(row)
        deadline = asyncio.get_running_loop().time() + min(60, self.reboot_timeout)
        last_error: BaseException | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await apply_route(ip_address, expected["device_id"], target)
            except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                await asyncio.sleep(2)
        if last_error is not None:
            raise last_error
        raise RuntimeError("CLI-17000 route write did not become available")

    async def route(self, row: Any, target: ServerTarget) -> dict[str, Any]:
        await self._server_ports(target)
        ip_address, expected = _device_expected(row)
        result = await self._apply_route_with_retry(row, target)
        readback = await self._read_current_route_with_retry(row)
        values = extract_route_values(readback.output)
        differences = route_diff(values, target)
        if any(bool(item["changed"]) for item in differences):
            raise RuntimeError("BASSWIESN route readback does not match the target")
        root = _baseline_for(row, self.timestamp)
        artifact = write_artifact(root, "route-after.txt", _redact(readback.output))
        return {
            "routing_status": "active",
            "target": target.to_public_dict(),
            "route_verified": True,
            "route_sha256": artifact.sha256,
            "commands_accepted": len(result.responses),
        }

    async def reboot(self, row: Any) -> dict[str, Any]:
        ip_address, expected = _device_expected(row)
        result = await cli_reboot(ip_address, expected["device_id"])
        del result
        return {"reboot_requested": True}

    async def reconnect(self, row: Any) -> dict[str, Any]:
        await self._wait_for_reboot_identity(row)
        return {"reachable": True, "ssh_status": "NOT_REQUIRED"}

    async def pair_account(self, row: Any, target: ServerTarget) -> dict[str, Any]:
        """Pair the approved radio with its deterministic local BASSWIESN account.

        SoundTouch firmware keeps the Marge source catalogue empty until the
        local account callback has completed.  Routing alone is therefore not
        sufficient for an existing radio.  The account ID is device-scoped,
        contains no credential, and is never accepted from the browser.
        """

        ip_address, expected = _device_expected(row)
        client = self._client(row)
        before = await client.get_xml("/info")
        root = _xml_root(before)
        current = root.findtext("margeAccountUUID", "").strip()
        # Preserve an existing valid association. A factory-fresh radio with
        # no association receives a deterministic, installation-independent
        # local ID; no personal radio mapping is compiled into the product.
        account_id = current if current.isdecimal() else _local_account_id(expected["device_id"])
        artifact_root = _baseline_for(row, self.timestamp)
        before_artifact = write_artifact(artifact_root, "marge-account-before-info.xml", _redact(before))
        if current == account_id:
            # Account identity and server routing are separate contracts. In
            # particular, SM2 can retain the local account while restoring a
            # Bose Marge URL during reboot. Always reapply and read back the
            # validated route before accepting this idempotent account case.
            await self._apply_route_with_retry(row, target)
            return {
                "account_paired": True,
                "account_changed": False,
                "account_id": account_id,
                "account_backup_sha256": before_artifact.sha256,
                "routing_restored_after_pairing": True,
            }
        cloud_url = target.urls["cloud"]
        auth_token = _read_or_create_marge_auth_token()
        body = (
            f"<PairDeviceWithAccount><accountId>{account_id}</accountId>"
            f"<userAuthToken>{html_escape(auth_token)}</userAuthToken>"
            f"<boseServer>{cloud_url}</boseServer><updateServer>{cloud_url}</updateServer>"
            "<accountEmail>local@basswiesn.invalid</accountEmail></PairDeviceWithAccount>"
        )
        await client.post_xml("/setMargeAccount", body)
        # Account pairing can restart network-facing services (and on SM2 can
        # make both HTTP and CLI unavailable for more than a minute). Require
        # a fresh identity readback before restoring the validated route; the
        # generic CLI retry alone is not an authoritative reconnect signal.
        await self._wait_for_identity(row)
        # FW 27 copies updateServer into the runtime Marge URL. Restore every
        # validated route field after pairing before accepting the result.
        await self._apply_route_with_retry(row, target)
        last_info = ""
        for _ in range(20):
            await asyncio.sleep(0.5)
            last_info = await client.get_xml("/info")
            if _xml_root(last_info).findtext("margeAccountUUID", "").strip() == account_id:
                after_artifact = write_artifact(artifact_root, "marge-account-after-info.xml", _redact(last_info))
                return {
                    "account_paired": True,
                    "account_changed": True,
                    "account_id": account_id,
                    "account_backup_sha256": before_artifact.sha256,
                    "account_after_sha256": after_artifact.sha256,
                    "routing_restored_after_pairing": True,
                }
        raise RuntimeError("local Marge account did not persist after /setMargeAccount")

    async def read_presets(self, row: Any) -> dict[str, Any]:
        client = self._client(row)
        raw = await client.get_xml("/presets")
        root = _xml_root(raw)
        count = len(root.findall(".//preset"))
        artifact = write_artifact(
            _baseline_for(row, self.timestamp),
            "presets-after.xml",
            _redact(raw),
        )
        return {"presets_readable": True, "count": count, "sha256": artifact.sha256}

    async def _stop_and_standby(self, client: SoundTouchClient) -> None:
        for state in ("press", "release"):
            # SoundTouch firmware accepts the established local-control
            # sender here.  The setup engine keeps this as a fixed safety
            # operation; it is not user-provided command input.
            await client.post_xml("/key", f'<key state="{state}" sender="Gabbo">STOP</key>')
        try:
            await client.get_xml("/standby")
        except Exception as exc:
            record_transport_attempt(
                ip_address=client.ip_address,
                device_id=client.device_id,
                action="HTTP_GET /standby",
                trigger=client.trigger or "setup_rebuild",
                phase="mutating_get",
                requested_state={"standby": True},
                result="failed",
                error_category=exc.__class__.__name__,
            )
            raise
        record_transport_attempt(
            ip_address=client.ip_address,
            device_id=client.device_id,
            action="HTTP_GET /standby",
            trigger=client.trigger or "setup_rebuild",
            phase="mutating_get",
            requested_state={"standby": True},
            result="success",
        )

    def _test_station(self, target: ServerTarget) -> tuple[str, str]:
        session = app_db.SessionLocal()
        try:
            station = (
                session.query(Station)
                .filter(
                    Station.lab_only.is_(False),
                    Station.stream_url != "",
                    Station.name != "Protected Test",
                )
                .order_by(Station.id)
                .first()
            )
            if station is None:
                station = Station(
                    **_SETUP_PLAYBACK_STATION,
                    provider="LOCAL_INTERNET_RADIO",
                    stream_codec="mp3",
                    compatibility_score=100,
                    is_direct_audio=1,
                    internal=True,
                    purpose="setup_playback",
                    lab_only=True,
                )
                session.add(station)
                session.commit()
            url = str(station.stream_url_resolved or station.stream_url or "").strip()
            if not re.fullmatch(r"https?://[^\s\"']+", url):
                raise RuntimeError("setup playback station URL is not safe")
            descriptor = StationDescriptor(
                name=str(station.name),
                stream_url=str(station.stream_url or url),
                stream_url_resolved=str(station.stream_url_resolved or ""),
                image_url=str(station.image_url or ""),
                tunein_id=str(station.provider_station_id or ""),
                stream_format=str(station.stream_format or station.stream_codec or ""),
                stream_mime=str(station.stream_mime or ""),
                compatibility_warning=str(station.compatibility_warning or ""),
            )
            # Radios must receive the BASSWIESN Orion adapter URL.  Sending
            # the external stream URL directly bypasses the local adapter and
            # is rejected by some SoundTouch firmware versions.
            location = (
                f"{target.urls['cloud']}{ORION_STATION_PATH}"
                f"?data={encode_orion_data(descriptor)}"
            )
            return str(station.name), location
        finally:
            session.close()

    async def _restore_safe_volume(self, client: SoundTouchClient) -> int:
        current = _volume(await client.get_xml("/volume"))
        if current != 1:
            await client.post_xml("/volume", "<volume>1</volume>")
            current = _volume(await client.get_xml("/volume"))
        if current != 1:
            raise RuntimeError("VOLUME_SAFETY_LOCK: volume 1 could not be restored")
        return current

    async def _set_mute(self, client: SoundTouchClient, enabled: bool) -> tuple[int, bool]:
        current = _volume_state(await client.get_xml("/volume"))
        if current[1] != enabled:
            for state in ("press", "release"):
                await client.post_xml("/key", f'<key state="{state}" sender="Gabbo">MUTE</key>')
            current = _volume_state(await client.get_xml("/volume"))
        if current[1] != enabled:
            expected = "aktiv" if enabled else "deaktiviert"
            raise RuntimeError(f"VOLUME_SAFETY_LOCK: Stummschaltung konnte nicht {expected} bestätigt werden")
        return current

    def _mark_audio_test_locked(self, row: Any, reason: str) -> None:
        """Persist a hardware safety lock before propagating an audio error."""

        evidence = _json(getattr(row, "evidence_json", "{}"))
        evidence.update(
            {
                "audio_test_locked": True,
                "audio_lock_reason": str(reason)[:500],
                "audio_lock_volume_limit": 1,
                "audio_lock_recovery": "manual review after STOP/STANDBY and volume-1 readback",
            }
        )
        row.evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        session = app_db.SessionLocal()
        try:
            lock_audio_safety(session, str(getattr(row, "device_id", "")), reason)
        finally:
            session.close()
        backup_path = Path(str(getattr(row, "backup_path", "") or ""))
        if backup_path.is_dir():
            try:
                write_json_artifact(
                    backup_path,
                    "audio-safety-lock.json",
                    {
                        "locked": True,
                        "reason": str(reason)[:500],
                        "volume_limit": 1,
                        "recovery": "manual review after STOP/STANDBY and volume-1 readback",
                    },
                )
            except OSError as exc:
                evidence["audio_lock_artifact_error"] = f"{exc.__class__.__name__}: {str(exc)[:180]}"
                row.evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)

    async def playback_test(self, row: Any, target: ServerTarget) -> dict[str, Any]:
        evidence = _json(getattr(row, "evidence_json", "{}"))
        safety_session = app_db.SessionLocal()
        try:
            persistent_safety = load_audio_safety(
                safety_session, str(getattr(row, "device_id", ""))
            )
        finally:
            safety_session.close()
        if evidence.get("audio_test_locked") or persistent_safety.locked:
            raise RuntimeError(
                "VOLUME_SAFETY_LOCK: Audiotest ist nach einer früheren Lautstärkeabweichung gesperrt; "
                "bitte zuerst die sichtbare Audio-Sicherheitsprüfung ausführen"
            )
        client = self._client(row)
        volume_before, muted_before = _volume_state(await client.get_xml("/volume"))
        if volume_before != 1:
            await client.post_xml("/volume", "<volume>1</volume>")
        volume_pre, _muted_pre = _volume_state(await client.get_xml("/volume"))
        if volume_pre != 1:
            raise RuntimeError("VOLUME_SAFETY_LOCK: volume 1 could not be verified before playback")
        # Select can restore a source-specific volume on some Portable units.
        # Keep the output muted across select and only unmute after a second
        # volume-1 readback.  A mute readback failure aborts before /select.
        muted_volume, muted_confirmed = await self._set_mute(client, True)
        if muted_volume != 1:
            await client.post_xml("/volume", "<volume>1</volume>")
            muted_volume, muted_confirmed = _volume_state(await client.get_xml("/volume"))
        if muted_volume != 1 or not muted_confirmed:
            raise RuntimeError("VOLUME_SAFETY_LOCK: volume 1 and mute were not both confirmed before select")
        station_name, stream_url = self._test_station(target)
        result: dict[str, Any] | None = None
        primary_error: Exception | None = None
        try:
            # Keep the XML shape identical to the normal station playback
            # path, including omission of empty container art.
            session = app_db.SessionLocal()
            try:
                station = (
                    session.query(Station)
                    .filter(
                        Station.stream_url != "",
                        Station.name == station_name,
                    )
                    .order_by(Station.id)
                    .first()
                )
                if station is None:
                    raise RuntimeError("playback verification station disappeared")
                xml = content_item_xml(
                    station,
                    stream_url,
                    include_container_art=bool(station.image_url),
                    source="LOCAL_INTERNET_RADIO",
                )
            finally:
                session.close()
            try:
                await client.post_xml("/select", xml)
            except Exception as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", "")
                detail = _redact(getattr(response, "text", "")) if response is not None else ""
                suffix = f" response={detail[:180]}" if detail else ""
                raise RuntimeError(
                    f"radio rejected BASSWIESN playback select ({status or 'transport error'}){suffix}"
                ) from exc
            volume_after, muted_after_select = _volume_state(await client.get_xml("/volume"))
            if not muted_after_select:
                reason = f"radio left mute state during source select; observed volume {volume_after}"
                self._mark_audio_test_locked(row, reason)
                raise RuntimeError(f"VOLUME_SAFETY_LOCK: {reason}")
            source_volume_changed = volume_after != 1
            if volume_after != 1:
                await client.post_xml("/volume", "<volume>1</volume>")
                volume_after, muted_after_select = _volume_state(await client.get_xml("/volume"))
            if volume_after != 1:
                raise RuntimeError("VOLUME_SAFETY_LOCK: post-select volume 1 readback failed")
            if not muted_after_select:
                raise RuntimeError("VOLUME_SAFETY_LOCK: mute readback was lost before playback verification")
            await asyncio.sleep(1)
            now = await client.get_xml("/now_playing")
            if 'source="STANDBY"' in now or "INVALID_SOURCE" in now.upper():
                raise RuntimeError("playback source was not accepted by the radio")
            if not muted_before:
                volume_after, muted_for_audio = await self._set_mute(client, False)
                if volume_after != 1 or muted_for_audio:
                    raise RuntimeError("VOLUME_SAFETY_LOCK: audible playback was not released at volume 1")
                await asyncio.sleep(1)
                audible_now = await client.get_xml("/now_playing")
                if "INVALID_SOURCE" in audible_now.upper():
                    raise RuntimeError("playback source became invalid after safe unmute")
            result = {
                "playback_ready": True,
                "station": station_name,
                "volume_before": volume_before,
                "volume_verified": 1,
                "muted_during_select": True,
                "muted_before": muted_before,
                "audible_at_volume_one": not muted_before,
                "source_volume_changed_while_muted": source_volume_changed,
                "stopped": True,
                "standby": True,
            }
        except Exception as exc:
            primary_error = exc
        finally:
            cleanup_errors: list[Exception] = []
            try:
                await self._stop_and_standby(client)
            except Exception as exc:
                cleanup_errors.append(exc)
            try:
                # The safety readback is deliberately performed after the
                # stop/standby action. A volume jump therefore locks the
                # device at volume 1 before this setup job can be retried.
                await self._restore_safe_volume(client)
            except Exception as exc:
                cleanup_errors.append(exc)
            if primary_error is not None:
                if cleanup_errors:
                    detail = "; ".join(str(item)[:180] for item in cleanup_errors)
                    raise RuntimeError(f"{primary_error}; safety cleanup failed: {detail}") from primary_error
                raise primary_error
            if cleanup_errors:
                raise cleanup_errors[0]
        if result is None:
            raise RuntimeError("playback verification did not produce a result")
        return result

    async def verify_audio_safety(self, row: Any) -> dict[str, Any]:
        """Human-triggered non-audio recovery for a persistent volume lock."""

        identity = await self.identify(row)
        client = self._client(row)
        volume_before = _volume(await client.get_xml("/volume"))
        if volume_before != 1:
            await client.post_xml("/volume", "<volume>1</volume>")
        volume_readback = _volume(await client.get_xml("/volume"))
        if volume_readback != 1:
            raise RuntimeError("Audio-Sicherheitsprüfung fehlgeschlagen: Lautstärke 1 wurde nicht bestätigt.")
        await self._stop_and_standby(client)
        final_volume, final_muted = await self._set_mute(client, False)
        if final_volume != 1:
            await client.post_xml("/volume", "<volume>1</volume>")
            final_volume, final_muted = _volume_state(await client.get_xml("/volume"))
        if final_volume != 1:
            raise RuntimeError("Audio-Sicherheitsprüfung fehlgeschlagen: Radio blieb nach STOP/STANDBY nicht auf Lautstärke 1.")
        if final_muted:
            raise RuntimeError("Audio-Sicherheitsprüfung fehlgeschlagen: Radio blieb nach STOP/STANDBY stummgeschaltet.")
        session = app_db.SessionLocal()
        try:
            state = clear_audio_safety(
                session,
                str(getattr(row, "device_id", "")),
                "Identität bestätigt; Lautstärke 1 vor und nach STOP/STANDBY gelesen",
            )
        finally:
            session.close()
        record_transport_attempt(
            ip_address=client.ip_address,
            device_id=client.device_id,
            action="audio_safety_verification",
            trigger="human_setup_ui",
            phase="readback",
            requested_state={"volume": 1, "stopped": True, "standby": True},
            result="success",
            readback={"volume": final_volume, "muted": final_muted, "identity": identity["device_id"]},
            verified=True,
        )
        return {
            "device_id": identity["device_id"],
            "identity_verified": True,
            "volume_before": volume_before,
            "volume_readback": volume_readback,
            "final_volume": final_volume,
            "final_muted": final_muted,
            "stopped": True,
            "standby": True,
            "audio_safety": state.public_dict(),
        }

    async def rollback(self, row: Any) -> dict[str, Any]:
        ip_address, expected = _device_expected(row)
        backup_path = Path(str(getattr(row, "backup_path", "") or ""))
        route_file = backup_path / "route-before.json"
        if not route_file.is_file():
            return {
                "rolled_back": True,
                "fully_restored": False,
                "route_changed": False,
                "rollback_scope": "NO_ROUTING_WRITE",
                "persistence_validation": "NOT_APPLICABLE",
                "reason": "no route write was recorded; account/environment state was not restored",
            }
        values = read_json_artifact(route_file)
        if not isinstance(values, dict):
            raise RuntimeError("routing rollback backup is invalid")
        await apply_route_values(ip_address, expected["device_id"], values)
        await cli_reboot(ip_address, expected["device_id"])
        await self._wait_for_reboot_identity(row)
        readback = await self._read_current_route_with_retry(row)
        if extract_route_values(readback.output) != {str(k): str(v) for k, v in values.items()}:
            raise RuntimeError("routing rollback readback did not match the backup")
        return {
            "rolled_back": True,
            "fully_restored": False,
            "route_changed": True,
            "routing_readback": True,
            "rollback_scope": "ROUTING_RUNTIME_READBACK",
            "persistence_validation": "OPEN_AFTER_LATER_REBOOT",
            "limitation": (
                "Account-, Environment- und spätere Boot-Persistenz wurden nicht "
                "restauriert; ein späterer Reboot kann einzelne URLs erneut setzen."
            ),
        }
