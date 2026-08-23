"""Application lifecycle integration for the independent 1.6 contracts.

The runtime deliberately owns one-shot/per-session asyncio tasks.  It has no
global reporting or metadata polling loop and never probes a radio while
rehydrating persisted state.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import ipaddress
import json
import socket
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.models import (
    AirPlayReadinessState,
    Device,
    DeviceCapabilitiesState,
    DeviceFirmwareProfile,
    MetadataState,
    RecoveryOperation,
    utc_now,
)
from basswiesn.app.repositories.research_state_repository import (
    ResearchStateRepository,
    aware_utc,
    dump_evidence,
)
from basswiesn.app.services.airplay_readiness import assess_airplay_readiness
from basswiesn.app.services.metadata_engine import (
    BOSEAPP_COALESCE_SECONDS,
    DEFAULT_METADATA_STALE_AFTER_SECONDS,
    MetadataCoalescer,
    MetadataProvenance,
    MetadataScheduler,
    MetadataSnapshot,
    mark_metadata_stale,
    metadata_changes,
    normalize_metadata,
)
from basswiesn.app.services.protected_devices import is_protected_device, is_protected_ip
from basswiesn.app.services.recovery import (
    RecoveredCheck,
    RecoveryAction,
    RecoveryCoordinator,
    RecoveryPlan,
    RecoveryRun,
    RecoveryStage,
    RecoveryStatus,
    plan_recovery,
)
from basswiesn.app.services.reporting_scheduler import (
    DEFAULT_RETRY_BACKOFF_SECONDS,
    ReportPayload,
    ReportingResult,
    ReportingScheduler,
    ReportingSession,
    embedded_now_playing,
    reporting_link,
)
from basswiesn.app.services.reporting_store import (
    SqlAlchemyReportingStore,
    reporting_session_key,
    split_reporting_session_key,
)
from basswiesn.app.services.research_state_retention import apply_research_retention


SessionFactory = Callable[[], Session]
Clock = Callable[[], datetime]
ReportPost = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
ReportResolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
MetadataFetch = Callable[[], Awaitable[Mapping[str, Any]]]
AIRPLAY_TRANSIENT_EVIDENCE_TTL_SECONDS = 300


class ProviderContractMode(StrEnum):
    """Why a freshly observed external provider contract is being attached."""

    SELECTION_START = "SELECTION_START"
    RESTART_REACQUIRE = "RESTART_REACQUIRE"


async def _resolve_report_host(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve once so the validated address can be pinned for the request."""

    infos = await asyncio.to_thread(
        socket.getaddrinfo,
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    addresses: list[str] = []
    for info in infos:
        try:
            address = str(ipaddress.ip_address(info[4][0]))
        except (IndexError, TypeError, ValueError):
            continue
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _clock_utc(clock: Clock) -> datetime:
    return aware_utc(clock())


def _safe_json(value: str | None, fallback: Any) -> Any:
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return decoded


def _url_origin(value: str) -> tuple[str, str, int] | None:
    """Return a credential-free normalized HTTP origin."""

    try:
        parsed = urlsplit(str(value or "").strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        return None
    return parsed.scheme, parsed.hostname.casefold(), port


def _is_configured_local_origin(origin: tuple[str, str, int]) -> bool:
    """Block accidental outbound scheduling against BASSWIESN itself."""

    settings = get_settings()
    local_origins = {
        candidate
        for candidate in (
            _url_origin(settings.local_base_url),
            _url_origin(settings.web_base_url),
            _url_origin(settings.debug_base_url),
            ("http", "localhost", settings.cloud_port),
            ("http", "127.0.0.1", settings.cloud_port),
            ("http", "::1", settings.cloud_port),
        )
        if candidate is not None
    }
    if settings.lan_host:
        local_origins.add(("http", settings.lan_host.casefold(), settings.cloud_port))
        local_origins.add(("https", settings.lan_host.casefold(), settings.cloud_port))
    return origin in local_origins


def _is_local_provider_reporting_route(value: str) -> bool:
    """Recognize the facade's own reporting routes on its service ports."""

    try:
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        return False
    settings = get_settings()
    return port in {settings.cloud_port, settings.web_port} and parsed.path.startswith(
        (
            "/bmx/orion/reporting",
            "/bmx/tunein/v1/reporting",
            "/bmx/radiobrowser/v1/reporting",
        )
    )


def _metadata_snapshot(row: MetadataState) -> MetadataSnapshot:
    try:
        provenance = MetadataProvenance(str(row.provenance or "UNKNOWN").upper())
    except ValueError:
        provenance = MetadataProvenance.UNKNOWN
    return MetadataSnapshot(
        station_name=row.station_name,
        station_id=row.station_id,
        track=row.track,
        artist=row.artist,
        album=row.album,
        image_url=row.artwork_url,
        provider=row.provider,
        source=row.source,
        updated_at=aware_utc(row.updated_at) if row.updated_at else None,
        provenance=provenance,
        confidence=int(row.confidence or 0),
        stale=bool(row.stale),
        display_projection=row.display_projection,
    )


def _xml_fields(xml_text: str) -> dict[str, str]:
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return {}
    result = {str(key).lower(): str(value).strip() for key, value in root.attrib.items()}
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1].lower()
        text = str(node.text or "").strip()
        if text and name not in result:
            result[name] = text
        for key, value in node.attrib.items():
            result.setdefault(str(key).lower(), str(value).strip())
    return result


def _first_text(values: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = str(values.get(name.lower()) or "").strip()
        if value:
            return value
    return None


def _profile_for_device(db: Session, device: Device) -> DeviceFirmwareProfile | None:
    capabilities = (
        db.query(DeviceCapabilitiesState)
        .filter(DeviceCapabilitiesState.device_id == device.device_id)
        .one_or_none()
    )
    if capabilities is not None and capabilities.firmware_profile_key:
        return (
            db.query(DeviceFirmwareProfile)
            .filter(DeviceFirmwareProfile.profile_key == capabilities.firmware_profile_key)
            .one_or_none()
        )
    # Without an explicit device/profile binding, accept only one exact
    # firmware match.  Model-family guesses are not readiness evidence.
    firmware = str(device.firmware or "").strip()
    if not firmware:
        return None
    matches: list[DeviceFirmwareProfile] = []
    for candidate in db.query(DeviceFirmwareProfile).all():
        version = str(candidate.version or "").strip()
        build = str(candidate.build or "").strip()
        if firmware == version or (
            version
            and build
            and firmware.startswith(f"{version}.")
            and build in firmware
        ):
            matches.append(candidate)
            if len(matches) > 1:
                break
    return matches[0] if len(matches) == 1 else None


def _source_visibility(
    db: Session,
    device: Device,
    runtime_state: Mapping[str, Any],
) -> tuple[bool | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    current = str(runtime_state.get("current_source") or "").strip().upper()
    if current == "AIRPLAY":
        return True, [{"source": "persisted_radio_now_playing", "value": "AIRPLAY"}]

    provider_rows = runtime_state.get("provider_state")
    if isinstance(provider_rows, list) and provider_rows:
        visible = any(
            str(row.get("source") or "").strip().upper() == "AIRPLAY"
            for row in provider_rows
            if isinstance(row, Mapping)
        )
        evidence.append({"source": "persisted_sources_readback", "airplay": visible})
        return visible, evidence

    providers = runtime_state.get("providers")
    if isinstance(providers, Mapping) and providers:
        item = next(
            (
                value
                for name, value in providers.items()
                if str(name).strip().upper() == "AIRPLAY" and isinstance(value, Mapping)
            ),
            None,
        )
        if item is not None:
            visible = bool(item.get("visible_in_sources"))
            evidence.append({"source": "persisted_provider_map", "airplay": visible})
            return visible, evidence

    capabilities = (
        db.query(DeviceCapabilitiesState)
        .filter(DeviceCapabilitiesState.device_id == device.device_id)
        .one_or_none()
    )
    if capabilities is not None:
        sources = _safe_json(capabilities.sources_json, [])
        if isinstance(sources, list) and sources:
            visible = any(str(value).strip().upper() == "AIRPLAY" for value in sources)
            evidence.append({"source": "persisted_capability_sources", "airplay": visible})
            return visible, evidence
        if capabilities.supports_airplay is False:
            evidence.append({"source": "persisted_capability", "supports_airplay": False})
            return False, evidence
    return None, evidence


def project_airplay_readiness_from_persisted(
    db: Session,
    device: Device,
    *,
    runtime_state: Mapping[str, Any] | None = None,
) -> AirPlayReadinessState | None:
    """Derive readiness only from already persisted/read-back evidence.

    No discovery, mDNS lookup, radio request or socket operation is performed.
    """

    if is_protected_device(device):
        return None
    runtime = dict(runtime_state or {})
    info = _xml_fields(device.info_xml)
    profile = _profile_for_device(db, device)
    previous = (
        db.query(AirPlayReadinessState)
        .filter(AirPlayReadinessState.device_id == device.device_id)
        .one_or_none()
    )
    firmware = (
        str(device.firmware or "").strip()
        or (str(profile.version or "").strip() if profile is not None else "")
        or (_first_text(info, "softwareversion", "firmwareversion") or "")
        or None
    )
    product_id = (
        (str(profile.product_id or "").strip() if profile is not None else "")
        or (_first_text(info, "productid", "product_id", "productcode") or "")
        or None
    )
    variant = (
        (str(profile.variant or "").strip() if profile is not None else "")
        or (_first_text(info, "variant", "variantmode") or "")
        or None
    )
    platform = (str(profile.platform or "").strip() if profile is not None else "") or None
    observed = utc_now()
    previous_expiry = (
        aware_utc(previous.expires_at)
        if previous is not None and previous.expires_at is not None
        else None
    )
    previous_transient_fresh = bool(
        previous_expiry is not None and previous_expiry > observed
    )
    source_visible, evidence = _source_visibility(db, device, runtime)
    source_observed_now = source_visible is not None
    if (
        source_visible is None
        and previous is not None
        and previous_transient_fresh
    ):
        source_visible = previous.source_visible

    carried = {
        "auth_hardware_detected": previous.auth_hardware_detected if previous and previous_transient_fresh else None,
        "sts_registered": previous.sts_registered if previous and previous_transient_fresh else None,
        "mdns_visible": previous.mdns_visible if previous and previous_transient_fresh else None,
        "pairing_ready": previous.pairing_ready if previous and previous_transient_fresh else None,
        "ptp_ready": previous.ptp_ready if previous and previous_transient_fresh else None,
        "audio_ready": previous.audio_ready if previous and previous_transient_fresh else None,
    }
    if previous is not None and previous_expiry is not None and not previous_transient_fresh:
        evidence.append(
            {
                "source": "transient_airplay_evidence_expired",
                "expired_at": previous_expiry.isoformat(),
            }
        )
    if profile is not None:
        evidence.append(
            {
                "source": "firmware_profile",
                "profile_key": profile.profile_key,
                "evidence_class": "persisted",
            }
        )
    if firmware or product_id or variant or platform:
        evidence.append(
            {
                "source": "persisted_identity",
                "firmware_present": bool(firmware),
                "product_id_present": bool(product_id),
                "variant_present": bool(variant),
                "platform_present": bool(platform),
            }
        )
    if not evidence and previous is None:
        # Creating rows for completely evidence-free placeholders would make
        # an UNKNOWN assessment look newly observed.
        return None

    readiness = assess_airplay_readiness(
        firmware_version=firmware,
        product_id=product_id,
        variant=variant,
        platform=platform,
        source_visible=source_visible,
        evidence=tuple(evidence),
        **carried,
    )
    if profile is not None and profile.auth_hardware_expected is not None:
        readiness = replace(
            readiness,
            auth_hardware_expected=bool(profile.auth_hardware_expected),
        )
    carries_transient_gate = any(value is not None for value in carried.values())
    expires_at = (
        previous_expiry
        if carries_transient_gate and previous_expiry is not None
        else observed + timedelta(seconds=AIRPLAY_TRANSIENT_EVIDENCE_TTL_SECONDS)
        if source_observed_now
        else None
    )
    return ResearchStateRepository(db).upsert_airplay_readiness(
        device.device_id,
        readiness,
        observed_at=observed,
        expires_at=expires_at,
        provenance="PERSISTED_RADIO_READBACK",
    )


class ResearchRuntime:
    """Own lifecycle state for reporting, metadata, retention and recovery."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        reporting_post: ReportPost | None = None,
        report_resolver: ReportResolver | None = None,
        reporting_transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        metadata_stale_after_seconds: int = DEFAULT_METADATA_STALE_AFTER_SECONDS,
        metadata_coalesce_seconds: float = BOSEAPP_COALESCE_SECONDS,
        retention_interval_seconds: int = 24 * 60 * 60,
        reporting_backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
        reporting_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._metadata_stale_after = max(5, int(metadata_stale_after_seconds))
        self._retention_interval = max(60, int(retention_interval_seconds))
        self._report_resolver = report_resolver or _resolve_report_host
        self._reporting_transport = reporting_transport
        self.reporting_store = SqlAlchemyReportingStore(session_factory)
        self.reporting = ReportingScheduler(
            reporting_post or self._post_report,
            store=self.reporting_store,
            backoff_seconds=reporting_backoff_seconds,
            sleep=reporting_sleep,
            result_handler=self._handle_reporting_result,
        )
        self.metadata = MetadataScheduler()
        self.metadata_coalescer = MetadataCoalescer(
            delay_seconds=metadata_coalesce_seconds
        )
        self.recovery = RecoveryCoordinator()
        self._retention_task: asyncio.Task[None] | None = None
        self._restored_reporting_keys: set[str] = set()
        self._provider_selections: dict[str, str] = {}
        self._active_provider_by_device: dict[str, str] = {}
        self._metadata_fetchers: dict[str, MetadataFetch] = {}
        self._metadata_snapshots: dict[str, MetadataSnapshot] = {}
        self._metadata_generations: dict[str, int] = {}
        self._metadata_ingest_locks: dict[str, asyncio.Lock] = {}
        self.started = False
        self._event_tasks_enabled = False
        self.last_retention_result: dict[str, int | bool] | None = None

    async def _post_report(self, url: str, payload: Mapping[str, Any]) -> httpx.Response:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise PermissionError("reporting target URL is not allowed")
        host = (parsed.hostname or "").strip()
        if not host:
            raise PermissionError("reporting target has no hostname")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = await self._report_resolver(host, port)
        except Exception as exc:
            raise PermissionError("reporting target could not be safely resolved") from exc
        if not addresses:
            raise PermissionError("reporting target could not be safely resolved")

        normalized: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        try:
            normalized = [ipaddress.ip_address(value) for value in addresses]
        except (TypeError, ValueError) as exc:
            raise PermissionError("reporting target resolution was invalid") from exc
        db = self._session_factory()
        try:
            radio_ips = {
                str(ipaddress.ip_address(value))
                for (value,) in db.query(Device.ip_address).all()
                if value
            }
        except (TypeError, ValueError) as exc:
            # An invalid/failed device inventory cannot be used to prove that
            # an outbound reporting destination is not a radio.
            raise PermissionError("radio target inventory could not be validated") from exc
        finally:
            db.close()
        if any(is_protected_ip(str(address)) or str(address) in radio_ips for address in normalized):
            raise PermissionError("reporting target is a radio or protected device")

        address = normalized[0]
        address_text = f"[{address.compressed}]" if address.version == 6 else address.compressed
        explicit_port = parsed.port
        pinned_netloc = f"{address_text}:{explicit_port}" if explicit_port is not None else address_text
        hostname = host.encode("idna").decode("ascii")
        default_port = 443 if parsed.scheme == "https" else 80
        host_header = hostname if explicit_port in {None, default_port} else f"{hostname}:{explicit_port}"
        pinned_url = urlunsplit(
            (parsed.scheme, pinned_netloc, parsed.path or "/", parsed.query, "")
        )
        extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else {}
        async with httpx.AsyncClient(
            timeout=8.0,
            follow_redirects=False,
            transport=self._reporting_transport,
            trust_env=False,
        ) as client:
            return await client.post(
                pinned_url,
                headers={"Host": host_header},
                extensions=extensions,
                json=dict(payload),
            )

    def enable_event_tasks(self) -> None:
        """Allow request-driven one-shot tasks without starting background work."""

        self._event_tasks_enabled = True

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        self._event_tasks_enabled = True
        restored = self.reporting_store.load_sessions()
        for session in restored:
            await self.reporting.restore(session)
            self._restored_reporting_keys.add(session.key)
            # Startup never arms a restored outbound report, even for a
            # custom store that retained an operational URL. A fresh dynamic
            # link must flow through ``refresh_reporting_url`` first; this
            # prevents catch-up bursts and guarantees zero provider traffic
            # during application startup.

        db = self._session_factory()
        try:
            metadata_rows = db.query(MetadataState).all()
            device_ids = [row.device_id for row in metadata_rows]
            self._metadata_snapshots.update(
                {row.device_id: _metadata_snapshot(row) for row in metadata_rows}
            )
            for device in db.query(Device).order_by(Device.id).all():
                project_airplay_readiness_from_persisted(db, device)
            db.commit()
        except Exception as exc:
            db.rollback()
            write_masterlog(
                "research_runtime_start_projection_failed",
                error_type=type(exc).__name__,
            )
            device_ids = []
        finally:
            db.close()
        for device_id in device_ids:
            self.schedule_metadata_staleness(device_id)

        self.run_retention_once()
        self._schedule_retention()
        write_masterlog(
            "research_runtime_started",
            reporting_sessions=len(restored),
            metadata_sessions=len(device_ids),
            network_probe=False,
        )

    async def shutdown(self) -> None:
        if self._retention_task is not None:
            self._retention_task.cancel()
            await asyncio.gather(self._retention_task, return_exceptions=True)
            self._retention_task = None
        await self.reporting.shutdown()
        await self.metadata.shutdown()
        await self.metadata_coalescer.shutdown()
        self._metadata_fetchers.clear()
        self._provider_selections.clear()
        self._active_provider_by_device.clear()
        self.started = False
        self._event_tasks_enabled = False

    async def enqueue_report(
        self,
        key: str,
        payload: ReportPayload,
        *,
        report_url: str | None = None,
        due_at: datetime | None = None,
        item_id: str | None = None,
    ) -> ReportingSession:
        session = await self.reporting.enqueue(
            key,
            payload,
            report_url=report_url,
            due_at=due_at,
            item_id=item_id,
        )
        self.reporting.schedule_due(key, now=self._clock)
        return session

    async def refresh_reporting_url(self, key: str, report_url: str) -> ReportingSession:
        session = await self.reporting.update_report_url(key, report_url)
        self.reporting.schedule_due(key, now=self._clock)
        return session

    async def observe_external_provider_selection_response(
        self,
        device_id: str,
        provider_id: str,
        selection_id: str,
        response: Mapping[str, Any],
        *,
        provider_origin: str,
        mode: ProviderContractMode,
        metadata_fetch: MetadataFetch | None = None,
        source: str | None = None,
    ) -> ReportingSession:
        """Attach a genuinely external BMX contract to one device session.

        The local Orion/TuneIn/RadioBrowser facade is a provider *server*; its
        reporting link belongs to the radio and must never be posted to by
        BASSWIESN itself.  Consequently this entry point requires the origin
        of an independently received external response, checks same-origin,
        rejects configured local service origins, and distinguishes a fresh
        selection from restart reacquisition explicitly.
        """

        if not self._event_tasks_enabled:
            raise RuntimeError("research runtime event tasks are not enabled")
        normalized_device = str(device_id or "").strip()
        normalized_provider = str(provider_id or "").strip().upper()
        normalized_selection = str(selection_id or "").strip()
        if not normalized_device or not normalized_provider or not normalized_selection:
            raise ValueError("device, provider and selection identity are required")
        try:
            contract_mode = ProviderContractMode(mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid provider contract observation mode") from exc

        db = self._session_factory()
        try:
            device = (
                db.query(Device)
                .filter(Device.device_id == normalized_device)
                .one_or_none()
            )
            if device is None:
                raise ValueError("external provider contract device is unknown")
            if is_protected_device(device):
                raise PermissionError("external provider contract device is protected")
        finally:
            db.close()

        report_url = reporting_link(response)
        response_origin = _url_origin(provider_origin)
        report_origin = _url_origin(report_url or "")
        if response_origin is None or report_origin is None:
            raise ValueError("external provider response has no valid reporting origin")
        if response_origin != report_origin:
            raise ValueError("dynamic reporting link changed provider origin")
        if _is_configured_local_origin(response_origin) or _is_local_provider_reporting_route(
            report_url
        ):
            raise ValueError("local BASSWIESN provider links are inbound-only")

        key = reporting_session_key(normalized_device, normalized_provider)
        previous_key = self._active_provider_by_device.get(normalized_device)
        if previous_key and previous_key != key:
            await self.reporting.cancel(previous_key, clear_queue=False)
            self.metadata.cancel(f"metadata-refresh::{previous_key}")
            self._metadata_fetchers.pop(previous_key, None)

        previous_selection = self._provider_selections.get(key)
        restored = key in self._restored_reporting_keys
        if contract_mode == ProviderContractMode.RESTART_REACQUIRE and not restored:
            raise ValueError("reporting session has no persisted contract to reacquire")

        is_new_selection = (
            contract_mode == ProviderContractMode.SELECTION_START
            and previous_selection != normalized_selection
        )
        if is_new_selection and (previous_selection is not None or restored):
            # A queued report belongs to the previous source binding.  Cancel
            # it generation-safely rather than sending it under the new link.
            await self.reporting.cancel(key, clear_queue=True)
            self.metadata.cancel(f"metadata-refresh::{key}")
            self._metadata_fetchers.pop(key, None)

        self._provider_selections[key] = normalized_selection
        self._active_provider_by_device[normalized_device] = key
        session = await self.refresh_reporting_url(key, report_url)

        if is_new_selection:
            selection_digest = sha256(
                f"{normalized_device}\0{normalized_provider}\0{normalized_selection}".encode(
                    "utf-8"
                )
            ).hexdigest()
            session = await self.enqueue_report(
                key,
                ReportPayload(
                    timeStamp=_clock_utc(self._clock).isoformat(),
                    eventType="start",
                ),
                due_at=_clock_utc(self._clock),
                item_id=f"selection-start:{selection_digest}",
            )
        else:
            # Reacquisition only re-arms the already persisted absolute due
            # time/queue. It never fabricates or replays a start event.
            self.reporting.schedule_due(key, now=self._clock)

        embedded = embedded_now_playing(response)
        if embedded is not None:
            await self.observe_provider_now_playing_response(
                normalized_device,
                normalized_provider,
                normalized_selection,
                embedded,
                metadata_fetch=metadata_fetch,
                source=source,
            )
        elif metadata_fetch is not None:
            self._metadata_fetchers[key] = metadata_fetch
            self._schedule_metadata_refresh(
                key,
                normalized_selection,
                due_at=_clock_utc(self._clock),
                source=source,
            )
        return session

    async def observe_provider_now_playing_response(
        self,
        device_id: str,
        provider_id: str,
        selection_id: str,
        payload: Mapping[str, Any],
        *,
        metadata_fetch: MetadataFetch | None = None,
        source: str | None = None,
        observed_at: datetime | None = None,
    ) -> MetadataSnapshot | None:
        """Ingest one external BMX.NowPlaying response for the active source.

        Late responses from a cancelled provider/source generation are
        discarded.  Runtime fields are coalesced and persisted without any
        source selection, SetURL, Stop, rebuffer or radio request.
        """

        normalized_provider = str(provider_id or "").strip().upper()
        normalized_selection = str(selection_id or "").strip()
        normalized_device = str(device_id or "").strip()
        if not normalized_device or not normalized_provider or not normalized_selection:
            raise ValueError("device, provider and selection identity are required")
        key = reporting_session_key(normalized_device, normalized_provider)
        active_selection = self._provider_selections.get(key)
        if active_selection is not None and active_selection != normalized_selection:
            write_masterlog(
                "metadata_response_discarded",
                device_id=device_id,
                provider_id=normalized_provider,
                reason="stale_selection_generation",
            )
            return None
        if active_selection is None:
            self._provider_selections[key] = normalized_selection
        if metadata_fetch is not None:
            self._metadata_fetchers[key] = metadata_fetch

        snapshot = await self.ingest_metadata(
            normalized_device,
            payload,
            provenance=MetadataProvenance.PROVIDER,
            confidence=90,
            observed_at=observed_at,
            station_id=normalized_selection,
            provider=normalized_provider,
            source=(str(source or normalized_provider).strip().upper()),
        )
        fetch = self._metadata_fetchers.get(key)
        if fetch is not None and snapshot.next_due_at is not None:
            self._schedule_metadata_refresh(
                key,
                normalized_selection,
                due_at=snapshot.next_due_at,
                source=source,
            )
        return snapshot

    def _schedule_metadata_refresh(
        self,
        key: str,
        selection_id: str,
        *,
        due_at: datetime,
        source: str | None,
    ) -> None:
        if not self._event_tasks_enabled:
            return
        fetch = self._metadata_fetchers.get(key)
        if fetch is None:
            return
        device_id, provider_id = split_reporting_session_key(key)

        async def fetch_once() -> None:
            try:
                payload = await fetch()
                if not isinstance(payload, Mapping):
                    raise TypeError("metadata response must be a mapping")
                await self.observe_provider_now_playing_response(
                    device_id,
                    provider_id,
                    selection_id,
                    payload,
                    source=source,
                )
            except Exception as exc:
                self._record_metadata_ingest_failure(
                    device_id,
                    provider_id,
                    error_type=type(exc).__name__,
                    origin="scheduled_now_playing",
                )

        self.metadata.schedule(
            f"metadata-refresh::{key}",
            due_at=due_at,
            callback=fetch_once,
            now=self._clock,
        )

    async def _handle_reporting_result(
        self, key: str, result: ReportingResult
    ) -> None:
        if result.embedded_now_playing is None:
            return
        try:
            device_id, provider_id = split_reporting_session_key(key)
            selection_id = self._provider_selections.get(key)
            if not selection_id:
                raise ValueError("report metadata has no active selection identity")
            await self.observe_provider_now_playing_response(
                device_id,
                provider_id,
                selection_id,
                result.embedded_now_playing,
            )
        except Exception as exc:
            try:
                device_id, provider_id = split_reporting_session_key(key)
            except ValueError:
                device_id, provider_id = "", "UNKNOWN"
            self._record_metadata_ingest_failure(
                device_id or None,
                provider_id,
                error_type=type(exc).__name__,
                origin="report_response",
            )

    def _record_metadata_ingest_failure(
        self,
        device_id: str | None,
        provider_id: str,
        *,
        error_type: str,
        origin: str,
    ) -> None:
        db = self._session_factory()
        try:
            ResearchStateRepository(db).record_event(
                device_id=device_id,
                domain="METADATA",
                code="METADATA_INGEST_FAILED",
                message="A provider metadata response could not be ingested.",
                severity="WARNING",
                evidence={
                    "provider_id": provider_id,
                    "origin": origin,
                    "error_type": error_type,
                    "playback_action": "NONE",
                },
                occurred_at=_clock_utc(self._clock),
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def schedule_metadata_staleness(self, device_id: str) -> bool:
        schedule_key = f"metadata-stale::{device_id}"
        if not self._event_tasks_enabled:
            return False
        db = self._session_factory()
        try:
            row = (
                db.query(MetadataState)
                .filter(MetadataState.device_id == device_id)
                .one_or_none()
            )
            if row is None or row.updated_at is None or row.stale:
                self.metadata.cancel(schedule_key)
                return False
            expected_updated_at = aware_utc(row.updated_at)
            due_at = expected_updated_at + timedelta(seconds=self._metadata_stale_after)
        finally:
            db.close()

        async def mark_if_current() -> None:
            current_db = self._session_factory()
            reschedule = False
            try:
                current = (
                    current_db.query(MetadataState)
                    .filter(MetadataState.device_id == device_id)
                    .one_or_none()
                )
                if current is None or current.updated_at is None or current.stale:
                    return
                if aware_utc(current.updated_at) != expected_updated_at:
                    reschedule = True
                    return
                snapshot = mark_metadata_stale(
                    _metadata_snapshot(current),
                    now=_clock_utc(self._clock),
                    stale_after_s=self._metadata_stale_after,
                )
                if snapshot.stale:
                    ResearchStateRepository(current_db).upsert_metadata(device_id, snapshot)
                    current_db.commit()
            except Exception:
                current_db.rollback()
                raise
            finally:
                current_db.close()
                if reschedule:
                    self.schedule_metadata_staleness(device_id)

        self.metadata.schedule(
            schedule_key,
            due_at=due_at,
            callback=mark_if_current,
            now=self._clock,
        )
        return True

    async def ingest_metadata(
        self,
        device_id: str,
        payload: Mapping[str, Any],
        *,
        provenance: MetadataProvenance = MetadataProvenance.PROVIDER,
        confidence: int = 90,
        observed_at: datetime | None = None,
        station_name: str | None = None,
        station_id: str | None = None,
        provider: str | None = None,
        source: str | None = None,
    ) -> MetadataSnapshot:
        normalized_device = str(device_id or "").strip()
        if not normalized_device:
            raise ValueError("metadata device identity is required")
        if not isinstance(payload, Mapping):
            raise TypeError("metadata payload must be a mapping")
        lock = self._metadata_ingest_locks.setdefault(
            normalized_device, asyncio.Lock()
        )
        async with lock:
            previous = self._metadata_snapshots.get(normalized_device)
            if previous is None:
                db = self._session_factory()
                try:
                    row = (
                        db.query(MetadataState)
                        .filter(MetadataState.device_id == normalized_device)
                        .one_or_none()
                    )
                    previous = (
                        _metadata_snapshot(row)
                        if row is not None
                        else MetadataSnapshot()
                    )
                finally:
                    db.close()

            current = normalize_metadata(
                payload,
                previous=previous,
                provenance=provenance,
                confidence=confidence,
                observed_at=observed_at or _clock_utc(self._clock),
                station_name=station_name,
                station_id=station_id,
                provider=provider,
                source=source,
            )
            previous_identity = (
                previous.station_name,
                previous.station_id,
                previous.provider,
                previous.source,
            )
            current_identity = (
                current.station_name,
                current.station_id,
                current.provider,
                current.source,
            )
            identity_changed = previous_identity != current_identity
            if identity_changed:
                self._metadata_generations[normalized_device] = (
                    self._metadata_generations.get(normalized_device, 0) + 1
                )
                self.metadata_coalescer.cancel(normalized_device)
            generation = self._metadata_generations.get(normalized_device, 0)
            self._metadata_snapshots[normalized_device] = current
            changed = metadata_changes(previous, current)
            publish_fields = changed or (
                "selection" if identity_changed else "freshness",
            )

            async def publish(
                snapshot: MetadataSnapshot, _changed: tuple[str, ...]
            ) -> None:
                if self._metadata_generations.get(normalized_device, 0) != generation:
                    return
                publish_db = self._session_factory()
                try:
                    ResearchStateRepository(publish_db).upsert_metadata(
                        normalized_device, snapshot
                    )
                    publish_db.commit()
                except Exception:
                    publish_db.rollback()
                    raise
                finally:
                    publish_db.close()
                self.schedule_metadata_staleness(normalized_device)

            self.metadata_coalescer.submit(
                normalized_device, current, publish_fields, publish
            )
            return current

    def run_retention_once(self) -> dict[str, int | bool]:
        db = self._session_factory()
        try:
            settings = get_settings()
            result = apply_research_retention(
                db,
                now=_clock_utc(self._clock),
                diagnostic_days=settings.retention_days,
                recovery_days=settings.retention_days,
            )
            self.last_retention_result = result
            write_masterlog("research_retention_completed", **result)
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _schedule_retention(self) -> None:
        if self._retention_task is not None and not self._retention_task.done():
            return

        async def run_once_then_rearm() -> None:
            current_task = asyncio.current_task()
            try:
                await asyncio.sleep(self._retention_interval)
                self.run_retention_once()
            finally:
                if self._retention_task is current_task:
                    self._retention_task = None
            if self.started:
                self._schedule_retention()

        self._retention_task = asyncio.create_task(
            run_once_then_rearm(), name="research-retention"
        )

    async def execute_recovery(
        self,
        device_id: str,
        plan: RecoveryPlan,
        *,
        actions: dict[RecoveryStage, RecoveryAction],
        recovered: RecoveredCheck,
        provider_id: str | None = None,
        source: str | None = None,
        correlation_id: str | None = None,
    ) -> RecoveryRun:
        db = self._session_factory()
        try:
            device = db.query(Device).filter(Device.device_id == device_id).one_or_none()
            if device is not None and is_protected_device(device):
                plan = plan_recovery(
                    reason=plan.reason,
                    requested_max_stage=plan.requested_max_stage,
                    automatic=plan.automatic,
                    lab_mode=plan.lab_mode,
                    protected_device=True,
                )
        finally:
            db.close()
        if plan.automatic and any(stage >= RecoveryStage.SAME_SOURCE_RESELECT for stage in plan.stages):
            plan = replace(
                plan,
                effective_max_stage=RecoveryStage.STREAM_RERESOLVE,
                stages=tuple(RecoveryStage(value) for value in range(4)),
            )
        run = await self.recovery.execute(
            device_id,
            plan,
            actions=actions,
            recovered=recovered,
        )
        self._persist_recovery_run(
            device_id,
            run,
            provider_id=provider_id,
            source=source,
            correlation_id=correlation_id,
        )
        return run

    def _persist_recovery_run(
        self,
        device_id: str,
        run: RecoveryRun,
        *,
        provider_id: str | None,
        source: str | None,
        correlation_id: str | None,
    ) -> None:
        db = self._session_factory()
        try:
            row = RecoveryOperation(
                operation_id=run.operation_id,
                device_id=device_id,
                provider_id=provider_id,
                source=source,
                correlation_id=correlation_id,
                status=run.status.value,
                stage=int(run.current_stage),
                trigger_domain=run.plan.reason.value,
                reason=run.error,
                evidence_json=dump_evidence(
                    {"plan": run.plan.as_dict(), "event_count": len(run.events)}
                ),
                result_json=dump_evidence({"events": run.events}),
                manual_required=(
                    run.plan.requested_max_stage == RecoveryStage.MANUAL_LAB_RADIO_REBOOT
                ),
                started_at=run.started_at,
                completed_at=run.finished_at,
                updated_at=utc_now(),
            )
            db.add(row)
            ResearchStateRepository(db).record_event(
                device_id=device_id,
                domain="RECOVERY",
                code=f"RECOVERY_{run.status.value}",
                message=run.error or f"Recovery ended with {run.status.value}.",
                evidence={
                    "operation_id": run.operation_id,
                    "stage": int(run.current_stage),
                    "automatic": run.plan.automatic,
                },
                correlation_id=correlation_id,
                occurred_at=run.finished_at or run.started_at,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
