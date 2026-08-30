from sqlalchemy.orm import Session
from pathlib import Path

from basswiesn.app import db as compatibility_db
from basswiesn.app.db import database
from basswiesn.app.db import models as database_models
from basswiesn.app import models as compatibility_models
from basswiesn.app.db.repositories import DeviceIdentityRepository, DeviceRepository
from basswiesn.app.repositories.device_identity_repository import (
    DeviceIdentityRepository as CompatibilityIdentityRepository,
)
from basswiesn.app.repositories.device_repository import (
    DeviceRepository as CompatibilityDeviceRepository,
)


def test_database_package_preserves_legacy_imports():
    assert compatibility_db.Base is database.Base
    assert compatibility_db.get_db is database.get_db
    assert compatibility_db.init_db is database.init_db


def test_get_db_uses_isolated_session_factory_and_closes_session():
    dependency = database.get_db()
    session = next(dependency)

    assert isinstance(session, Session)

    dependency.close()
    assert not session.is_active or session.get_transaction() is None


def test_repository_paths_preserve_class_identity():
    assert CompatibilityDeviceRepository is DeviceRepository
    assert CompatibilityIdentityRepository is DeviceIdentityRepository


def test_database_model_exports_preserve_identity_and_metadata():
    model_names = [
        "AirPlayReadinessState",
        "AnnouncementJob",
        "ArtworkCacheEntry",
        "ConfigBackup",
        "DescriptorCache",
        "Device",
        "DeviceActionJournal",
        "DeviceCapabilitiesState",
        "DeviceCapabilityOverride",
        "DeviceInteraction",
        "DeviceModelDefinition",
        "Diagnostic",
        "DiagnosticEvent",
        "DiscoveryEvent",
        "Event",
        "FirmwareProfile",
        "HealthcheckResult",
        "HealthcheckRun",
        "MediaItem",
        "MediaPlaylist",
        "MediaPlaylistItem",
        "MediaRoot",
        "MetadataState",
        "MultiroomScenario",
        "PlaybackHealthState",
        "PlaybackState",
        "PlayHistory",
            "Preset",
            "PresetMutation",
        "PresetProfile",
        "ProviderLeaseState",
        "ProviderState",
        "QuickFixRun",
        "ReferenceSetup",
        "RecoveryOperation",
        "ReportingQueueEntry",
        "ReportingState",
        "RequestLog",
        "RestrictionState",
        "RestoreJob",
        "RuntimeState",
        "SchemaMigration",
        "ScheduledAction",
        "Setting",
        "SetupJob",
        "SetupJobStep",
        "SetupPlan",
        "Station",
        "SystemBackup",
        "TelemetryEvent",
        "UpdateJob",
        "WebhookDelivery",
        "WebhookEndpoint",
        "TelnetDeviceProfile",
        "TelnetJob",
        "StandbyClockJob",
        "DeviceFirmwareProfile",
        "DeviceCapabilityEvidence",
        "WebhookDeliveryQueue",
        "RestoreExecutionJob",
        "UpdateExecutionJob",
        "MediaScanJob",
        "DlnaRenderer",
        "HealthStatusHistory",
    ]

    for name in model_names:
        assert getattr(database_models, name) is getattr(compatibility_models, name)

    expected_tables = {
        "announcement_jobs",
        "config_backups",
        "descriptor_cache",
        "devices",
        "device_action_journal",
        "device_capability_overrides",
        "device_interactions",
        "device_model_definitions",
        "diagnostics",
        "discovery_events",
        "events",
        "healthcheck_results",
        "healthcheck_runs",
        "media_items",
        "media_playlists",
        "media_playlist_items",
        "media_roots",
        "multiroom_scenarios",
        "play_history",
        "preset_profiles",
            "presets",
            "preset_mutations",
        "quick_fix_runs",
        "reference_setups",
        "request_log",
        "restore_jobs",
        "runtime_state",
        "schema_migrations",
        "scheduled_actions",
        "settings",
        "setup_jobs",
        "setup_job_steps",
        "setup_plans",
        "stations",
        "system_backups",
        "telemetry_events",
        "update_jobs",
        "webhook_deliveries",
        "webhook_endpoints",
        "telnet_device_profiles",
        "telnet_jobs",
        "standby_clock_jobs",
        "device_firmware_profiles",
        "device_capability_evidence",
        "webhook_delivery_queue",
        "restore_execution_jobs",
        "update_execution_jobs",
        "media_scan_jobs",
        "dlna_renderers",
        "health_status_history",
        "setup_rebuild_jobs",
        "setup_rebuild_device_states",
        "setup_rebuild_coordinator_lease",
        "device_capability_snapshots",
        "provider_health_state",
        "provider_lease_state",
        "reporting_state",
        "reporting_queue",
        "metadata_state",
        "playback_state",
        "playback_health_state",
        "restriction_state",
        "airplay_readiness_state",
        "diagnostic_events",
        "recovery_operations",
        "artwork_cache",
    }
    assert set(database.Base.metadata.tables) == expected_tables


def test_release_packaging_includes_public_runtime_docs_and_excludes_private_files():
    script = Path("tools/package_release.sh").read_text(encoding="utf-8")

    assert "BASE_ITEMS=(basswiesn Dockerfile docker-compose.yml requirements.txt README.md FEATURES.md SETUP_READ_HERE.md RELEASE_CHECKLIST.md LICENSE .env.example install.sh" in script
    assert "PUBLIC_TOOLS=(tools/run_dev.py)" in script
    assert "PUBLIC_DOCS=(docs/releases/2.5.1/RELEASE_NOTES_2.5.1.md)" in script
    assert "installation-specific hardware or filesystem data" in script
    assert "__pycache__" in script
    assert "package_private_rpi.sh" in script
    assert "*.tar.gz" in script
    assert "manifest.json" in script
    assert "release-test-summary.json" in script
    assert "SOURCE_DATE_EPOCH" in script
    assert "sha256sum" in script
    assert "RELEASE_REPORTS" not in script


def test_installer_grants_only_one_shot_filesystem_repair_capabilities():
    script = Path("install.sh").read_text(encoding="utf-8")

    repair = script.split("if ! docker compose run", 1)[1].split("; then", 1)[0]
    assert "--user 0" in repair
    assert "--cap-add CHOWN" in repair
    assert "--cap-add FOWNER" in repair
    assert "--cap-add DAC_OVERRIDE" in repair
    assert "docker compose up -d --force-recreate" in script
    assert "cap_add" not in Path("docker-compose.yml").read_text(encoding="utf-8")


def test_fresh_installer_persists_host_lan_candidates_for_bridge_mode():
    script = Path("install.sh").read_text(encoding="utf-8")

    assert "detect_lan_candidates" in script
    assert "is_physical_lan_interface" in script
    assert "BASSWIESN_LAN_HOST=%s" in script
    assert "BASSWIESN_LAN_HOST_CANDIDATES=%s" in script
    assert "CREATED_ENV == 1" in script
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.unit, _pytest_marker.mark.release]
