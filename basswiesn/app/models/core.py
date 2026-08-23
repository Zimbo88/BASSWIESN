from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from basswiesn.app.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class UInt64Text(TypeDecorator[int]):
    """Persist an unsigned 64-bit value losslessly on SQLite.

    SQLite INTEGER is signed, so the upper half of a protobuf ``uint64`` must
    not be stored in an INTEGER column. The decimal text representation keeps
    the provider value exact while presenting ``int`` values to Python.
    """

    impl = Text
    cache_ok = True
    MAX_VALUE = (1 << 64) - 1

    def process_bind_param(self, value: int | str | None, dialect: object) -> str | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("uint64 values cannot be boolean")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid uint64 value") from exc
        if parsed < 0 or parsed > self.MAX_VALUE:
            raise ValueError("uint64 value out of range")
        return str(parsed)

    def process_result_value(self, value: str | int | None, dialect: object) -> int | None:
        del dialect
        return None if value is None else int(value)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str] = mapped_column(Text, default="")
    firmware: Mapped[str] = mapped_column(Text, default="")
    capabilities_xml: Mapped[str] = mapped_column(Text, default="")
    info_xml: Mapped[str] = mapped_column(Text, default="")
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    reachable: Mapped[bool] = mapped_column(Boolean, default=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    offline_reason: Mapped[str] = mapped_column(Text, default="")
    maintenance_reboot_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    maintenance_reboot_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    maintenance_last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maintenance_next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maintenance_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maintenance_last_result: Mapped[str] = mapped_column(Text, default="")
    maintenance_phase: Mapped[str] = mapped_column(Text, default="idle")
    maintenance_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    device_class_override: Mapped[str] = mapped_column(Text, default="auto")
    safe_mode: Mapped[str] = mapped_column(Text, default="auto")
    polling_profile_override: Mapped[str] = mapped_column(Text, default="auto")
    auto_restore_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    battery_poll_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    maintenance_actions_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    discovery_method: Mapped[str] = mapped_column(Text, default="unknown")
    discovery_confidence: Mapped[int] = mapped_column(Integer, default=0)
    discovery_last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovery_location: Mapped[str] = mapped_column(Text, default="")
    discovered_interface: Mapped[str] = mapped_column(Text, default="")
    descriptor_url: Mapped[str] = mapped_column(Text, default="")
    descriptor_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class DeviceModelDefinition(Base):
    __tablename__ = "device_model_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    product_name: Mapped[str] = mapped_column(Text, default="")
    model_family: Mapped[str] = mapped_column(Text, default="")
    generation: Mapped[str] = mapped_column(Text, default="")
    device_class: Mapped[str] = mapped_column(Text, default="unknown")
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}")
    firmware_notes: Mapped[str] = mapped_column(Text, default="")
    known_limitations: Mapped[str] = mapped_column(Text, default="")
    recommended_polling_profile: Mapped[str] = mapped_column(Text, default="idle_online")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeviceCapabilityOverride(Base):
    __tablename__ = "device_capability_overrides"
    __table_args__ = (UniqueConstraint("device_id", "capability_key", name="uq_device_capability_override"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    capability_key: Mapped[str] = mapped_column(Text)
    override_value: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DiscoveryEvent(Base):
    __tablename__ = "discovery_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    ip_address: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(Text, default="unknown")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    location: Mapped[str] = mapped_column(Text, default="")
    interface: Mapped[str] = mapped_column(Text, default="")
    descriptor_url: Mapped[str] = mapped_column(Text, default="")
    descriptor_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")


class HealthcheckRun(Base):
    __tablename__ = "healthcheck_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="running")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class HealthcheckResult(Base):
    __tablename__ = "healthcheck_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    category: Mapped[str] = mapped_column(Text, default="system")
    check_id: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(Text, default="skipped")
    description: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    cause: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class QuickFixRun(Base):
    __tablename__ = "quick_fix_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    quick_fix_id: Mapped[str] = mapped_column(Text, index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(Text, default="planned")
    confirmation: Mapped[str] = mapped_column(Text, default="")
    preview_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    rollback_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceInteraction(Base):
    __tablename__ = "device_interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    correlation_id: Mapped[str] = mapped_column(Text, default="", index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    device_name: Mapped[str] = mapped_column(Text, default="")
    device_class: Mapped[str] = mapped_column(Text, default="unknown")
    ip_address: Mapped[str] = mapped_column(Text, default="")
    request_purpose: Mapped[str] = mapped_column(Text, default="")
    requester: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    method: Mapped[str] = mapped_column(Text, default="GET")
    endpoint: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[str] = mapped_column(Text, default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    error_class: Mapped[str] = mapped_column(Text, default="")
    polling_profile: Mapped[str] = mapped_column(Text, default="")
    safe_mode_state: Mapped[str] = mapped_column(Text, default="")
    circuit_breaker_state: Mapped[str] = mapped_column(Text, default="")
    lock_wait_ms: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    skip_reason: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(Text, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    source: Mapped[str] = mapped_column(Text, default="basswiesn")
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    correlation_id: Mapped[str] = mapped_column(Text, default="", index=True)
    severity: Mapped[str] = mapped_column(Text, default="info")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    redaction: Mapped[str] = mapped_column(Text, default="redacted")
    delivery_status: Mapped[str] = mapped_column(Text, default="pending")


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    event_types_json: Mapped[str] = mapped_column(Text, default="[]")
    secret_ref: Mapped[str] = mapped_column(Text, default="")
    allowlist_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_error: Mapped[str] = mapped_column(Text, default="")


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(Integer, index=True)
    event_id: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    error_class: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemBackup(Base):
    __tablename__ = "system_backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    backup_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    path: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    version: Mapped[str] = mapped_column(Text, default="")
    schema_version: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(Text, default="")
    quick_check: Mapped[str] = mapped_column(Text, default="")
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")


class RestoreJob(Base):
    __tablename__ = "restore_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    status: Mapped[str] = mapped_column(Text, default="prepared")
    archive_path: Mapped[str] = mapped_column(Text, default="")
    safety_backup_path: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UpdateJob(Base):
    __tablename__ = "update_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    source_type: Mapped[str] = mapped_column(Text, default="local_archive")
    source: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="planned")
    current_version: Mapped[str] = mapped_column(Text, default="")
    target_version: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SetupJob(Base):
    __tablename__ = "setup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    job_type: Mapped[str] = mapped_column(Text, default="setup")
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    user_request_json: Mapped[str] = mapped_column(Text, default="{}")
    current_step: Mapped[str] = mapped_column(Text, default="")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    previous_state_json: Mapped[str] = mapped_column(Text, default="{}")
    after_state_json: Mapped[str] = mapped_column(Text, default="{}")
    readback_json: Mapped[str] = mapped_column(Text, default="{}")
    rollback_status: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SetupJobStep(Base):
    __tablename__ = "setup_job_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, index=True)
    step_id: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="pending")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")


class SetupRebuildJob(Base):
    __tablename__ = "setup_rebuild_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    current_device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    current_state: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    selected_device_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    target_server_json: Mapped[str] = mapped_column(Text, default="{}")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_id: Mapped[str] = mapped_column(Text, default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SetupRebuildDeviceState(Base):
    __tablename__ = "setup_rebuild_device_states"
    __table_args__ = (
        UniqueConstraint("job_id", "device_id", name="uq_setup_rebuild_device_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, index=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    ip_address: Mapped[str] = mapped_column(Text, default="")
    expected_model: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(Text, default="UNKNOWN", index=True)
    ssh_status: Mapped[str] = mapped_column(Text, default="SSH_UNKNOWN")
    ssh_profile_key: Mapped[str] = mapped_column(Text, default="")
    routing_status: Mapped[str] = mapped_column(Text, default="unknown")
    backup_path: Mapped[str] = mapped_column(Text, default="")
    backup_sha256_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    last_error: Mapped[str] = mapped_column(Text, default="")
    recovery_status: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SetupRebuildCoordinatorLease(Base):
    __tablename__ = "setup_rebuild_coordinator_lease"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_key: Mapped[str] = mapped_column(Text, unique=True, index=True, default="global")
    owner_id: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MediaRoot(Base):
    __tablename__ = "media_roots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    root_id: Mapped[int] = mapped_column(Integer, index=True)
    relative_path: Mapped[str] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    artist: Mapped[str] = mapped_column(Text, default="")
    album: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    format: Mapped[str] = mapped_column(Text, default="")
    codec: Mapped[str] = mapped_column(Text, default="")
    bitrate: Mapped[int] = mapped_column(Integer, default=0)
    samplerate: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(Text, default="")
    cover_path: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(Text, default="")
    compatibility_json: Mapped[str] = mapped_column(Text, default="{}")
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MediaPlaylistItem(Base):
    __tablename__ = "media_playlist_items"
    __table_args__ = (UniqueConstraint("playlist_id", "item_id", name="uq_media_playlist_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int] = mapped_column(Integer, index=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AnnouncementJob(Base):
    __tablename__ = "announcement_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    group_id: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="planned")
    text_preview: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(Text, default="de")
    volume: Mapped[int] = mapped_column(Integer, default=20)
    max_volume: Mapped[int] = mapped_column(Integer, default=30)
    previous_state_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelnetDeviceProfile(Base):
    __tablename__ = "telnet_device_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    model_family: Mapped[str] = mapped_column(Text, default="")
    firmware_family: Mapped[str] = mapped_column(Text, default="")
    command_port: Mapped[int] = mapped_column(Integer, default=17000)
    telnet_reachable: Mapped[bool] = mapped_column(Boolean, default=False)
    telnet_reboot_supported: Mapped[bool] = mapped_column(Boolean, default=False)
    standby_clock_recovery_supported: Mapped[bool] = mapped_column(Boolean, default=False)
    reboot_command_key: Mapped[str] = mapped_column(Text, default="")
    standby_clock_command_key: Mapped[str] = mapped_column(Text, default="")
    commands_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence: Mapped[str] = mapped_column(Text, default="")
    limitations: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TelnetJob(Base):
    __tablename__ = "telnet_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    action: Mapped[str] = mapped_column(Text, default="reboot", index=True)
    status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    profile_key: Mapped[str] = mapped_column(Text, default="")
    command_key: Mapped[str] = mapped_column(Text, default="")
    command_port: Mapped[int] = mapped_column(Integer, default=17000)
    correlation_id: Mapped[str] = mapped_column(Text, default="", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=8)
    wait_seconds: Mapped[int] = mapped_column(Integer, default=180)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")


class StandbyClockJob(Base):
    __tablename__ = "standby_clock_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    profile_key: Mapped[str] = mapped_column(Text, default="")
    command_key: Mapped[str] = mapped_column(Text, default="")
    correlation_id: Mapped[str] = mapped_column(Text, default="", index=True)
    before_state_json: Mapped[str] = mapped_column(Text, default="{}")
    readback_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceFirmwareProfile(Base):
    __tablename__ = "device_firmware_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    model_family: Mapped[str] = mapped_column(Text, default="")
    firmware_family: Mapped[str] = mapped_column(Text, default="")
    platform: Mapped[str] = mapped_column(Text, default="")
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}")
    command_profile_key: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    limitations: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    build: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    airplay_capability: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_hardware_expected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metadata_capability: Mapped[str | None] = mapped_column(Text, nullable=True)
    artwork_capability: Mapped[str | None] = mapped_column(Text, nullable=True)
    multiroom_capability: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeviceCapabilityEvidence(Base):
    __tablename__ = "device_capability_evidence"
    __table_args__ = (UniqueConstraint("device_id", "capability_key", "source", name="uq_device_capability_evidence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    capability_key: Mapped[str] = mapped_column(Text, index=True)
    source: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeviceCapabilitiesState(Base):
    """Latest observed capabilities; nullable booleans mean not observed."""

    __tablename__ = "device_capability_snapshots"
    __table_args__ = (UniqueConstraint("device_id", name="uq_device_capability_snapshot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    firmware_profile_key: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    has_display: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_clock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_battery: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_multiroom: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_bluetooth: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_airplay: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metadata_fields_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProviderHealthState(Base):
    """Provider state and health, intentionally separate from playback."""

    __tablename__ = "provider_health_state"
    __table_args__ = (
        UniqueConstraint("device_id", "provider_id", name="uq_provider_health_state"),
        Index("idx_provider_health_state_changed", "state", "changed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    provider_id: Mapped[str] = mapped_column(Text, index=True)
    source: Mapped[str] = mapped_column(Text, default="")
    availability: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    association: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    state: Mapped[str] = mapped_column(Text, default="DEGRADED", index=True)
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    last_error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    recovery_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_visible_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProviderLeaseState(Base):
    __tablename__ = "provider_lease_state"
    __table_args__ = (
        UniqueConstraint("device_id", "provider_id", name="uq_provider_lease_state"),
        CheckConstraint("retry_count >= 0", name="ck_provider_lease_retry_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    provider_id: Mapped[str] = mapped_column(Text, index=True)
    inactivity_timeout_s: Mapped[int | None] = mapped_column(UInt64Text(), nullable=True)
    report_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    metadata_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    session_health: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReportingState(Base):
    __tablename__ = "reporting_state"
    __table_args__ = (
        UniqueConstraint("device_id", "provider_id", name="uq_reporting_state"),
        CheckConstraint("queue_depth >= 0 AND queue_depth <= 20", name="ck_reporting_queue_depth"),
        CheckConstraint("retry_count >= 0 AND retry_count <= 5", name="ck_reporting_retry_count"),
        Index("idx_reporting_state_due", "state", "next_due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    provider_id: Mapped[str] = mapped_column(Text, index=True)
    state: Mapped[str] = mapped_column(Text, default="IDLE", index=True)
    report_url: Mapped[str | None] = mapped_column("report_url_redacted", Text, nullable=True)
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReportingQueueEntry(Base):
    __tablename__ = "reporting_queue"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_reporting_queue_item"),
        UniqueConstraint(
            "device_id", "provider_id", "generation", "queue_slot",
            name="uq_reporting_queue_slot",
        ),
        CheckConstraint("queue_slot >= 0 AND queue_slot < 20", name="ck_reporting_queue_slot"),
        CheckConstraint("retry_count >= 0 AND retry_count <= 5", name="ck_reporting_queue_retry_count"),
        Index("idx_reporting_queue_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, index=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    provider_id: Mapped[str] = mapped_column(Text, index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    queue_slot: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default="QUEUED", index=True)
    event_type: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    redacted: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MetadataState(Base):
    __tablename__ = "metadata_state"
    __table_args__ = (UniqueConstraint("device_id", name="uq_metadata_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    station_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    station_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    track: Mapped[str | None] = mapped_column(Text, nullable=True)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    album: Mapped[str | None] = mapped_column(Text, nullable=True)
    genre: Mapped[str | None] = mapped_column(Text, nullable=True)
    artwork_url: Mapped[str | None] = mapped_column("artwork_url_redacted", Text, nullable=True)
    artwork_provenance: Mapped[str] = mapped_column(Text, default="NONE")
    duration_ms: Mapped[int | None] = mapped_column(UInt64Text(), nullable=True)
    position_ms: Mapped[int | None] = mapped_column(UInt64Text(), nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    display_projection: Mapped[str | None] = mapped_column(Text, nullable=True)


class PlaybackState(Base):
    __tablename__ = "playback_state"
    __table_args__ = (UniqueConstraint("device_id", name="uq_playback_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_account: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="STOPPED", index=True)
    content_item_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mute: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    readback_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PlaybackHealthState(Base):
    __tablename__ = "playback_health_state"
    __table_args__ = (
        UniqueConstraint("device_id", name="uq_playback_health_state"),
        CheckConstraint("recovery_stage >= 0 AND recovery_stage <= 7", name="ck_playback_recovery_stage"),
        Index("idx_playback_health_state_since", "state", "since"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    state: Mapped[str] = mapped_column(Text, default="STOPPED", index=True)
    source_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stream_alive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    position_advancing: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_health: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    recovery_stage: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RestrictionState(Base):
    __tablename__ = "restriction_state"
    __table_args__ = (
        UniqueConstraint("device_id", "source_key", name="uq_restriction_state"),
        Index("idx_restriction_state_expiry", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    source_key: Mapped[str] = mapped_column(Text, index=True)
    inactivity_timeout_s: Mapped[int | None] = mapped_column(UInt64Text(), nullable=True)
    timer_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origin: Mapped[str] = mapped_column(Text, default="ABSENT")
    timer_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_until: Mapped[datetime | None] = mapped_column("expires_at", DateTime(timezone=True), nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def expires_at(self) -> datetime | None:
        return self.effective_until

    @expires_at.setter
    def expires_at(self, value: datetime | None) -> None:
        self.effective_until = value


class AirPlayReadinessState(Base):
    __tablename__ = "airplay_readiness_state"
    __table_args__ = (
        UniqueConstraint("device_id", name="uq_airplay_readiness_state"),
        CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_airplay_readiness_confidence"),
        Index("idx_airplay_readiness_blocking", "blocking_stage", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    firmware_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    firmware_build: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    auth_hardware_expected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    auth_hardware_detected: Mapped[bool | None] = mapped_column("auth_hardware_seen", Boolean, nullable=True)
    sts_registered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source_visible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mdns_visible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pairing_ready: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ptp_ready: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    audio_ready: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    blocking_stage: Mapped[str] = mapped_column(Text, default="UNKNOWN", index=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    provenance: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def auth_hardware_seen(self) -> bool | None:
        return self.auth_hardware_detected

    @auth_hardware_seen.setter
    def auth_hardware_seen(self, value: bool | None) -> None:
        self.auth_hardware_detected = value


class DiagnosticEvent(Base):
    __tablename__ = "diagnostic_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_diagnostic_event"),
        Index("idx_diagnostic_events_device_time", "device_id", "occurred_at"),
        Index("idx_diagnostic_events_domain_severity", "domain", "severity"),
        Index("idx_diagnostic_events_correlation", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    domain: Mapped[str] = mapped_column(Text, default="UNKNOWN", index=True)
    severity: Mapped[str] = mapped_column(Text, default="INFO", index=True)
    device_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    code: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column("message_redacted", Text, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    redacted: Mapped[bool] = mapped_column(Boolean, default=True)


class RecoveryOperation(Base):
    __tablename__ = "recovery_operations"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_recovery_operation"),
        CheckConstraint("stage >= 0 AND stage <= 7", name="ck_recovery_operation_stage"),
        Index("idx_recovery_operations_device_status", "device_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[str] = mapped_column(Text, index=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    status: Mapped[str] = mapped_column(Text, default="PENDING", index=True)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    trigger_domain: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    readback_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    manual_required: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ArtworkCacheEntry(Base):
    __tablename__ = "artwork_cache"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_artwork_cache_key"),
        Index("idx_artwork_cache_expiry", "expires_at"),
        Index("idx_artwork_cache_source_status", "source", "failure_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(Text, index=True)
    device_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    station_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="UNKNOWN")
    source_url_hash: Mapped[str] = mapped_column(Text, default="", index=True)
    source_url_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    cached_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    failure_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# Compatibility/domain names reuse the existing persistent implementations.
FirmwareProfile = DeviceFirmwareProfile
DeviceCapabilitySnapshot = DeviceCapabilitiesState
ProviderState = ProviderHealthState
ReportingQueueItem = ReportingQueueEntry
PlaybackHealth = PlaybackHealthState
AirPlayReadiness = AirPlayReadinessState
ArtworkCache = ArtworkCacheEntry


class WebhookDeliveryQueue(Base):
    __tablename__ = "webhook_delivery_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    endpoint_id: Mapped[int] = mapped_column(Integer, index=True)
    event_id: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RestoreExecutionJob(Base):
    __tablename__ = "restore_execution_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    restore_job_id: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(Text, default="planned", index=True)
    safety_backup_path: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UpdateExecutionJob(Base):
    __tablename__ = "update_execution_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    update_job_id: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(Text, default="planned", index=True)
    current_version: Mapped[str] = mapped_column(Text, default="")
    target_version: Mapped[str] = mapped_column(Text, default="")
    archive_sha256: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MediaScanJob(Base):
    __tablename__ = "media_scan_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    root_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    scanned_files: Mapped[int] = mapped_column(Integer, default=0)
    added_files: Mapped[int] = mapped_column(Integer, default=0)
    updated_files: Mapped[int] = mapped_column(Integer, default=0)
    skipped_files: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DlnaRenderer(Base):
    __tablename__ = "dlna_renderers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    renderer_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(Text, default="")
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HealthStatusHistory(Base):
    __tablename__ = "health_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, default="", index=True)
    category: Mapped[str] = mapped_column(Text, default="", index=True)
    check_id: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(Text, default="unknown", index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    stream_url: Mapped[str] = mapped_column(Text, nullable=False)
    stream_url_original: Mapped[str] = mapped_column(Text, default="")
    stream_url_resolved: Mapped[str] = mapped_column(Text, default="")
    stream_format: Mapped[str] = mapped_column(Text, default="")
    stream_mime: Mapped[str] = mapped_column(Text, default="")
    stream_codec: Mapped[str] = mapped_column(Text, default="")
    compatibility_score: Mapped[int] = mapped_column(Integer, default=0)
    compatibility_warning: Mapped[str] = mapped_column(Text, default="")
    is_hls: Mapped[int] = mapped_column(Integer, default=0)
    is_direct_audio: Mapped[int] = mapped_column(Integer, default=0)
    image_url: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(Text, default="LOCAL_INTERNET_RADIO")
    provider_station_id: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    internal: Mapped[bool] = mapped_column(Boolean, default=False)
    purpose: Mapped[str] = mapped_column(Text, default="")
    lab_only: Mapped[bool] = mapped_column(Boolean, default=False)


class Preset(Base):
    __tablename__ = "presets"
    __table_args__ = (UniqueConstraint("device_id", "button", name="uq_device_button"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    button: Mapped[int] = mapped_column(Integer)
    station_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="LOCAL_INTERNET_RADIO")
    source_account: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(Text, default="")
    content_item_xml: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PresetMutation(Base):
    """Durable fail-closed state machine for one hardware preset mutation."""

    __tablename__ = "preset_mutations"
    __table_args__ = (
        UniqueConstraint("mutation_id", name="uq_preset_mutation_id"),
        Index("idx_preset_mutation_device_state", "device_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mutation_id: Mapped[str] = mapped_column(Text, index=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    button: Mapped[int] = mapped_column(Integer, index=True)
    operation: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="PREPARED", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    expected_previous_sha256: Mapped[str] = mapped_column(Text, default="")
    requested_sha256: Mapped[str] = mapped_column(Text, default="")
    before_radio_sha256: Mapped[str] = mapped_column(Text, default="")
    after_radio_sha256: Mapped[str] = mapped_column(Text, default="")
    backup_ref: Mapped[str] = mapped_column(Text, default="")
    diverged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PresetProfile(Base):
    __tablename__ = "preset_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    slots_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReferenceSetup(Base):
    __tablename__ = "reference_setups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_device_id: Mapped[str] = mapped_column(Text, default="")
    model_family: Mapped[str] = mapped_column(Text, default="")
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    presets_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MediaPlaylist(Base):
    __tablename__ = "media_playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(Text, default="DLNA")
    uri: Mapped[str] = mapped_column(Text, default="")
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SetupPlan(Base):
    __tablename__ = "setup_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="draft")
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DescriptorCache(Base):
    __tablename__ = "descriptor_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RequestLog(Base):
    __tablename__ = "request_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    direction: Mapped[str] = mapped_column(Text)
    service: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text, default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[str] = mapped_column(Text, default="")


class PlayHistory(Base):
    __tablename__ = "play_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    device_name: Mapped[str] = mapped_column(Text, default="")
    device_ip: Mapped[str] = mapped_column(Text, default="")
    station_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    station_name: Mapped[str] = mapped_column(Text, default="")
    stream_url: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="LOCAL_INTERNET_RADIO")
    source_type: Mapped[str] = mapped_column(Text, default="LOCAL_INTERNET_RADIO")
    zone_master_id: Mapped[str] = mapped_column(Text, default="")
    zone_member_ids: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[str] = mapped_column(Text, default="manual")
    trigger_type: Mapped[str] = mapped_column(Text, default="manual")
    preset_button: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preset_name: Mapped[str] = mapped_column(Text, default="")
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[int] = mapped_column(Integer, default=1)
    error_message: Mapped[str] = mapped_column(Text, default="")
    internal_event: Mapped[bool] = mapped_column(Boolean, default=False)
    last_confirmed_playing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    end_reason: Mapped[str] = mapped_column(Text, default="")
    station_display_name: Mapped[str] = mapped_column(Text, default="")
    station_name_normalized: Mapped[str] = mapped_column(Text, default="")
    source_account: Mapped[str] = mapped_column(Text, default="")
    content_item_name: Mapped[str] = mapped_column(Text, default="")
    canonical_stream_id: Mapped[str] = mapped_column(Text, default="")
    identity_source: Mapped[str] = mapped_column(Text, default="")
    identity_confidence: Mapped[int] = mapped_column(Integer, default=0)
    source_display_name: Mapped[str] = mapped_column(Text, default="")
    stream_host: Mapped[str] = mapped_column(Text, default="")
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed_duration_seconds: Mapped[int] = mapped_column(Integer, default=0)


class DeviceActionJournal(Base):
    __tablename__ = "device_action_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    job_id: Mapped[str] = mapped_column(Text, default="", index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    ip_address: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[str] = mapped_column(Text, default="manual")
    phase: Mapped[str] = mapped_column(Text, default="")
    requested_state: Mapped[str] = mapped_column(Text, default="{}")
    backup_ref: Mapped[str] = mapped_column(Text, default="")
    before_state: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[str] = mapped_column(Text, default="")
    readback: Mapped[str] = mapped_column(Text, default="{}")
    rollback_ref: Mapped[str] = mapped_column(Text, default="")
    after_state: Mapped[str] = mapped_column(Text, default="{}")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_category: Mapped[str] = mapped_column(Text, default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class MultiroomScenario(Base):
    __tablename__ = "multiroom_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    master_device_id: Mapped[str] = mapped_column(Text, default="")
    member_device_ids: Mapped[str] = mapped_column(Text, default="")
    station_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preserve_volumes: Mapped[bool] = mapped_column(Boolean, default=False)
    trigger_device_id: Mapped[str] = mapped_column(Text, default="")
    trigger_button: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ScheduledAction(Base):
    __tablename__ = "scheduled_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    start_time: Mapped[str] = mapped_column(Text, default="")
    end_time: Mapped[str] = mapped_column(Text, default="")
    days: Mapped[str] = mapped_column(Text, default="daily")
    device_ids: Mapped[str] = mapped_column(Text, default="")
    station_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preset_button: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    multiroom_master_id: Mapped[str] = mapped_column(Text, default="")
    multiroom_member_ids: Mapped[str] = mapped_column(Text, default="")
    stop_action: Mapped[str] = mapped_column(Text, default="stop_standby")
    dry_run: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    device_id: Mapped[str] = mapped_column(Text, default="", index=True)
    event_type: Mapped[str] = mapped_column(Text, default="")
    endpoint: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="")
    parsed_summary: Mapped[str] = mapped_column(Text, default="")


class RuntimeState(Base):
    __tablename__ = "runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(Text, unique=True, index=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    description: Mapped[str] = mapped_column(Text, default="")


class ConfigBackup(Base):
    __tablename__ = "config_backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    path: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Diagnostic(Base):
    __tablename__ = "diagnostics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    payload: Mapped[str] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
