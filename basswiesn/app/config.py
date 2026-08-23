from functools import lru_cache
from pathlib import Path
import ipaddress
import os
import socket
import subprocess
from pydantic import BaseModel

from basswiesn import __version__


# Public builds must not contain installation-specific radio identities. The
# constants remain as an extension point for downstream appliance builds, but
# upstream protection is configured at runtime.
IMMUTABLE_PROTECTED_IPS: frozenset[str] = frozenset()
IMMUTABLE_PROTECTED_DEVICE_IDS: frozenset[str] = frozenset()
BASSWIESN_TABOO_HOSTS: set[str] = set(IMMUTABLE_PROTECTED_IPS)


def _is_private_lan_ipv4(ip: ipaddress._BaseAddress) -> bool:
    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        # RFC 5737 test ranges are accepted so unit tests never need real LANs.
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
    )
    return any(ip in network for network in private_networks)


_VIRTUAL_INTERFACE_PREFIXES = ("br-", "cni", "docker", "podman", "veth", "virbr")


def _is_virtual_interface(value: str) -> bool:
    name = str(value or "").strip().lower()
    return name == "lo" or name.startswith(_VIRTUAL_INTERFACE_PREFIXES)


def _is_radio_reachable_ipv4(value: str) -> bool:
    """Return whether an address is suitable as a LAN target for a radio."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        ip.version == 4
        and not ip.is_loopback
        and not ip.is_link_local
        and _is_private_lan_ipv4(ip)
    )


def is_safe_radio_host(value: str) -> bool:
    """Return whether a browser/server IPv4 is safe to suggest to radios."""
    return (
        _is_radio_reachable_ipv4(value)
        and value not in IMMUTABLE_PROTECTED_IPS
        and value not in BASSWIESN_TABOO_HOSTS
    )


def _default_lan_host() -> str:
    """Return an auto-detected host without opening a network transport.

    LAN identity is local operating-system state.  A passive WebUI load must
    never need a UDP ``connect`` (even one that normally sends no packet) to
    infer it, because transport attempts make protected-device auditing
    ambiguous and are unnecessary on supported Linux hosts.
    """
    route_host = ""
    try:
        route_output = subprocess.run(
            ["ip", "-o", "-4", "route", "get", "192.0.2.1"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        route_parts = route_output.split()
        if "src" in route_parts:
            route_index = route_parts.index("src") + 1
            if route_index < len(route_parts):
                route_host = route_parts[route_index]
    except Exception:
        pass
    physical: list[tuple[str, str]] = []
    try:
        output = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[2] != "inet":
                continue
            interface = parts[1].split("@", 1)[0]
            host = parts[3].split("/", 1)[0]
            if not _is_virtual_interface(interface) and _is_radio_reachable_ipv4(host):
                physical.append((interface, host))
    except Exception:
        pass

    # Interface identity is authoritative: private 172.16/12 is a perfectly
    # valid home LAN and must not be rejected merely because Docker commonly
    # uses the same range.
    physical_hosts = {host for _interface, host in physical}
    if route_host in physical_hosts:
        return route_host
    if physical:
        physical.sort(
            key=lambda item: (
                0 if item[0].lower().startswith(("wl", "en", "eth")) else 1,
                item[1],
            )
        )
        return physical[0][1]

    # Portable/test fallback for systems where `ip` is unavailable. Prefer a
    # conventional LAN candidate over ambiguous 172/12 addresses when both
    # are present, but still allow 172/12 when it is the only usable network.
    candidates: list[str] = [route_host] if route_host else []
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    candidates = sorted(set(candidates), key=lambda value: (0 if value.startswith("192.168.") else 1 if value.startswith("10.") else 2, value))
    for candidate in candidates:
        if _is_radio_reachable_ipv4(candidate):
            return candidate
    return ""


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def scan_cidr_for_host(host: str) -> str:
    """Return the /24 scan range for a configured IPv4 LAN host."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if ip.version != 4 or not _is_radio_reachable_ipv4(host):
        return ""
    return str(ipaddress.ip_network(f"{host}/24", strict=False))


class Settings(BaseModel):
    project_name: str = "basswiesn"
    version: str = __version__
    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/basswiesn.db"
    web_port: int = 1328
    cloud_port: int = 1516
    debug_port: int = 1860
    radio_port: int = 8090
    cloud_host: str = "content.api.bose.io"
    lan_host: str = ""
    lan_host_configured: bool = False
    lan_host_candidates: tuple[str, ...] = ()
    local_base_url: str = ""
    local_base_url_configured: bool = False
    web_base_url: str = "http://127.0.0.1:1328"
    debug_base_url: str = "http://127.0.0.1:1860"
    test_mode: bool = False
    release_manifest_required: bool = False
    disable_setup_confirmations: bool = False
    masterlog_enabled: bool = True
    setup_write_radio_ips: tuple[str, ...] = ()
    update_check_enabled: bool = False
    update_channel: str = "manual"
    update_manifest_url: str = ""
    update_repo_url: str = ""
    retention_days: int = 30
    request_log_retention_days: int = 14
    telemetry_retention_days: int = 30
    config_backup_retention_count: int = 100
    masterlog_max_mb: int = 50
    masterlog_backup_count: int = 5
    station_upload_max_mb: int = 50
    station_upload_quota_mb: int = 500
    support_bundle_max_mb: int = 50
    playback_keepalive_enabled: bool = True
    playback_keepalive_interval_seconds: int = 300
    playback_keepalive_log_every_seconds: int = 1800
    playback_state_stale_after_seconds: int = 360
    portable_safe_low_risk_interval_seconds: int = 3600
    stationary_low_risk_interval_seconds: int = 300
    ssdp_enabled: bool = True
    ssdp_timeout_seconds: int = 4
    ssdp_interval_seconds: int = 300
    ip_scan_fallback: bool = True
    device_interaction_max_concurrency: int = 4
    telnet_enabled: bool = False
    telnet_port: int = 23
    telnet_timeout_seconds: int = 8
    telnet_reboot_wait_seconds: int = 180
    telnet_username: str = ""
    telnet_password_file: str = ""
    telnet_allowed_device_ids: tuple[str, ...] = ()
    ssh_port: int = 22
    ssh_timeout_seconds: int = 8
    ssh_retry_count: int = 2
    ssh_username: str = ""
    ssh_password_file: str = ""
    ssh_private_key_file: str = ""
    ssh_known_hosts_file: str = ""
    ssh_host_key_policy: str = "strict"
    ssh_allowed_device_ids: tuple[str, ...] = ()
    marge_auth_token_file: str = ""
    standby_clock_recovery_enabled: bool = False
    webhooks_enabled: bool = False
    webhook_allowed_hosts: tuple[str, ...] = ()
    webhook_timeout_seconds: int = 5
    webhook_max_retries: int = 5
    media_enabled: bool = False
    media_roots: tuple[str, ...] = ()
    media_max_file_size_mb: int = 500
    experimental_dlna: bool = False
    experimental_announcements: bool = False
    lab_mode: bool = False
    event_retention_days: int = 30
    interaction_retention_days: int = 14
    backup_retention_count: int = 10
    diagnostic_max_size_mb: int = 50
    update_allow_local_archive: bool = True
    protected_device_ips: tuple[str, ...] = ()
    protected_device_ids: tuple[str, ...] = ()
    offline_mode: str = "auto"
    offline_allowed_stream_hosts: tuple[str, ...] = ()
    maintenance_reboot_enabled_by_default: bool = False
    maintenance_reboot_scheduler_enabled: bool = False
    maintenance_reboot_default_interval_hours: int = 24
    maintenance_reboot_min_interval_hours: int = 6
    maintenance_reboot_max_interval_hours: int = 168
    maintenance_reboot_return_timeout_seconds: int = 600
    maintenance_reboot_scheduler_interval_seconds: int = 300
    enable_https: bool = False
    https_port: int = 1329
    cert_mode: str = "selfsigned"
    cert_days: int = 3650
    tls_cert_file: str = ""
    tls_key_file: str = ""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int((os.getenv(name) or "").strip())
    except ValueError:
        return default
    return max(minimum, value)


@lru_cache
def get_settings() -> Settings:
    configured_lan_host = (os.getenv("BASSWIESN_LAN_HOST") or "").strip()
    configured_local_base_url = (os.getenv("BASSWIESN_LOCAL_BASE_URL") or "").strip()
    if configured_lan_host and not is_safe_radio_host(configured_lan_host):
        configured_lan_host = ""
    configured_lan_candidates = tuple(
        dict.fromkeys(
            item.strip()
            for item in (os.getenv("BASSWIESN_LAN_HOST_CANDIDATES") or "").split(",")
            if item.strip() and is_safe_radio_host(item.strip())
        )
    )
    lan_host = configured_lan_host or _default_lan_host()
    update_channel = _env_text("BASSWIESN_UPDATE_CHANNEL", "manual").lower()
    if update_channel not in {"manual", "stable", "beta"}:
        update_channel = "manual"
    settings = Settings(
        version=_env_text("BASSWIESN_VERSION", __version__),
        lan_host=lan_host,
        lan_host_configured=bool(configured_lan_host),
        lan_host_candidates=configured_lan_candidates,
        local_base_url=configured_local_base_url or (f"http://{lan_host}:1516" if lan_host else ""),
        local_base_url_configured=bool(configured_local_base_url),
        web_base_url=_env_text(
            "BASSWIESN_WEB_BASE_URL", f"http://{lan_host}:1328" if lan_host else ""
        ),
        debug_base_url=_env_text(
            "BASSWIESN_DEBUG_BASE_URL", f"http://{lan_host}:1860" if lan_host else ""
        ),
        test_mode=_env_bool("BASSWIESN_TEST_MODE", False),
        release_manifest_required=_env_bool("BASSWIESN_RELEASE_MANIFEST_REQUIRED", False),
        disable_setup_confirmations=_env_bool(
            "BASSWIESN_DISABLE_SETUP_CONFIRMATIONS", False
        ),
        masterlog_enabled=_env_bool("BASSWIESN_MASTERLOG_ENABLED", True),
        setup_write_radio_ips=tuple(
            item.strip()
            for item in (os.getenv("BASSWIESN_SETUP_WRITE_RADIO_IPS") or "").split(",")
            if item.strip()
        ),
        update_check_enabled=_env_bool("BASSWIESN_UPDATE_CHECK_ENABLED", False),
        update_channel=update_channel,
        update_manifest_url=(os.getenv("BASSWIESN_UPDATE_MANIFEST_URL") or "").strip(),
        update_repo_url=(os.getenv("BASSWIESN_UPDATE_REPO_URL") or "").strip(),
        retention_days=_env_int("BASSWIESN_RETENTION_DAYS", 30),
        request_log_retention_days=_env_int("BASSWIESN_REQUEST_LOG_RETENTION_DAYS", 14),
        telemetry_retention_days=_env_int("BASSWIESN_TELEMETRY_RETENTION_DAYS", 30),
        config_backup_retention_count=_env_int("BASSWIESN_CONFIG_BACKUP_RETENTION_COUNT", 100),
        masterlog_max_mb=_env_int("BASSWIESN_MASTERLOG_MAX_MB", 50),
        masterlog_backup_count=_env_int("BASSWIESN_MASTERLOG_BACKUP_COUNT", 5),
        station_upload_max_mb=_env_int("BASSWIESN_STATION_UPLOAD_MAX_MB", 50),
        station_upload_quota_mb=_env_int("BASSWIESN_STATION_UPLOAD_QUOTA_MB", 500),
        support_bundle_max_mb=_env_int("BASSWIESN_SUPPORT_BUNDLE_MAX_MB", 50),
        playback_keepalive_enabled=_env_bool("BASSWIESN_PLAYBACK_KEEPALIVE_ENABLED", True),
        playback_keepalive_interval_seconds=_env_int("BASSWIESN_PLAYBACK_KEEPALIVE_INTERVAL_SECONDS", 300),
        playback_keepalive_log_every_seconds=_env_int("BASSWIESN_PLAYBACK_KEEPALIVE_LOG_EVERY_SECONDS", 1800),
        playback_state_stale_after_seconds=_env_int("BASSWIESN_PLAYBACK_STATE_STALE_AFTER_SECONDS", 360),
        portable_safe_low_risk_interval_seconds=_env_int("BASSWIESN_PORTABLE_SAFE_LOW_RISK_INTERVAL_SECONDS", 3600),
        stationary_low_risk_interval_seconds=_env_int("BASSWIESN_STATIONARY_LOW_RISK_INTERVAL_SECONDS", 300),
        ssdp_enabled=_env_bool("BASSWIESN_SSDP_ENABLED", True),
        ssdp_timeout_seconds=_env_int("BASSWIESN_SSDP_TIMEOUT_SECONDS", 4),
        ssdp_interval_seconds=_env_int("BASSWIESN_SSDP_INTERVAL_SECONDS", 300),
        ip_scan_fallback=_env_bool("BASSWIESN_IP_SCAN_FALLBACK", True),
        device_interaction_max_concurrency=_env_int("BASSWIESN_DEVICE_INTERACTION_MAX_CONCURRENCY", 4),
        telnet_enabled=_env_bool("BASSWIESN_TELNET_ENABLED", False),
        telnet_port=_env_int("BASSWIESN_TELNET_PORT", 23),
        telnet_timeout_seconds=_env_int("BASSWIESN_TELNET_TIMEOUT_SECONDS", 8),
        telnet_reboot_wait_seconds=_env_int("BASSWIESN_TELNET_REBOOT_WAIT_SECONDS", 180),
        telnet_username=(os.getenv("BASSWIESN_TELNET_USERNAME") or "").strip(),
        telnet_password_file=(os.getenv("BASSWIESN_TELNET_PASSWORD_FILE") or "").strip(),
        telnet_allowed_device_ids=tuple(
            item.strip().upper()
            for item in (os.getenv("BASSWIESN_TELNET_ALLOWED_DEVICE_IDS") or "").split(",")
            if item.strip()
        ),
        ssh_port=_env_int("BASSWIESN_SSH_PORT", 22),
        ssh_timeout_seconds=_env_int("BASSWIESN_SSH_TIMEOUT_SECONDS", 8),
        ssh_retry_count=_env_int("BASSWIESN_SSH_RETRY_COUNT", 2, minimum=0),
        ssh_username=(os.getenv("BASSWIESN_SSH_USERNAME") or "").strip(),
        ssh_password_file=(os.getenv("BASSWIESN_SSH_PASSWORD_FILE") or "").strip(),
        ssh_private_key_file=(os.getenv("BASSWIESN_SSH_PRIVATE_KEY_FILE") or "").strip(),
        ssh_known_hosts_file=(os.getenv("BASSWIESN_SSH_KNOWN_HOSTS_FILE") or "").strip(),
        ssh_host_key_policy=_env_text("BASSWIESN_SSH_HOST_KEY_POLICY", "strict").lower()
        if _env_text("BASSWIESN_SSH_HOST_KEY_POLICY", "strict").lower() in {"strict", "accept-new", "off"}
        else "strict",
        ssh_allowed_device_ids=tuple(
            item.strip().upper()
            for item in (os.getenv("BASSWIESN_SSH_ALLOWED_DEVICE_IDS") or "").split(",")
            if item.strip()
        ),
        marge_auth_token_file=(os.getenv("BASSWIESN_MARGE_AUTH_TOKEN_FILE") or "").strip(),
        standby_clock_recovery_enabled=_env_bool("BASSWIESN_STANDBY_CLOCK_RECOVERY_ENABLED", False),
        webhooks_enabled=_env_bool("BASSWIESN_WEBHOOKS_ENABLED", False),
        webhook_allowed_hosts=tuple(
            item.strip().lower()
            for item in (os.getenv("BASSWIESN_WEBHOOK_ALLOWED_HOSTS") or "").split(",")
            if item.strip()
        ),
        webhook_timeout_seconds=_env_int("BASSWIESN_WEBHOOK_TIMEOUT_SECONDS", 5),
        webhook_max_retries=_env_int("BASSWIESN_WEBHOOK_MAX_RETRIES", 5),
        media_enabled=_env_bool("BASSWIESN_MEDIA_ENABLED", False),
        media_roots=tuple(
            item.strip()
            for item in (os.getenv("BASSWIESN_MEDIA_ROOTS") or "").split(",")
            if item.strip()
        ),
        media_max_file_size_mb=_env_int("BASSWIESN_MEDIA_MAX_FILE_SIZE_MB", 500),
        experimental_dlna=_env_bool("BASSWIESN_EXPERIMENTAL_DLNA", False),
        experimental_announcements=_env_bool("BASSWIESN_EXPERIMENTAL_ANNOUNCEMENTS", False),
        lab_mode=_env_bool("BASSWIESN_LAB_MODE", False),
        event_retention_days=_env_int("BASSWIESN_EVENT_RETENTION_DAYS", 30),
        interaction_retention_days=_env_int("BASSWIESN_INTERACTION_RETENTION_DAYS", 14),
        backup_retention_count=_env_int("BASSWIESN_BACKUP_RETENTION_COUNT", 10),
        diagnostic_max_size_mb=_env_int("BASSWIESN_DIAGNOSTIC_MAX_SIZE_MB", 50),
        update_allow_local_archive=_env_bool("BASSWIESN_UPDATE_ALLOW_LOCAL_ARCHIVE", True),
        protected_device_ips=tuple(
            item.strip()
            for item in (
                os.getenv("PROTECTED_DEVICE_IPS")
                or os.getenv("BASSWIESN_PROTECTED_DEVICE_IPS")
                or ""
            ).split(",")
            if item.strip()
        ),
        protected_device_ids=tuple(
            item.strip().upper()
            for item in (
                os.getenv("PROTECTED_DEVICE_IDS")
                or os.getenv("BASSWIESN_PROTECTED_DEVICE_IDS")
                or ""
            ).split(",")
            if item.strip()
        ),
        offline_mode=_env_text("BASSWIESN_OFFLINE_MODE", "auto").lower()
        if _env_text("BASSWIESN_OFFLINE_MODE", "auto").lower() in {"off", "auto", "strict"}
        else "auto",
        offline_allowed_stream_hosts=tuple(
            item.strip().lower()
            for item in (os.getenv("BASSWIESN_OFFLINE_ALLOWED_STREAM_HOSTS") or "").split(",")
            if item.strip()
        ),
        maintenance_reboot_enabled_by_default=_env_bool("BASSWIESN_MAINTENANCE_REBOOT_ENABLED_BY_DEFAULT", False),
        maintenance_reboot_scheduler_enabled=_env_bool("BASSWIESN_MAINTENANCE_REBOOT_SCHEDULER_ENABLED", False),
        maintenance_reboot_default_interval_hours=_env_int("BASSWIESN_MAINTENANCE_REBOOT_DEFAULT_INTERVAL_HOURS", 24),
        maintenance_reboot_min_interval_hours=_env_int("BASSWIESN_MAINTENANCE_REBOOT_MIN_INTERVAL_HOURS", 6),
        maintenance_reboot_max_interval_hours=_env_int("BASSWIESN_MAINTENANCE_REBOOT_MAX_INTERVAL_HOURS", 168),
        maintenance_reboot_return_timeout_seconds=_env_int("BASSWIESN_MAINTENANCE_REBOOT_RETURN_TIMEOUT_SECONDS", 600),
        maintenance_reboot_scheduler_interval_seconds=_env_int("BASSWIESN_MAINTENANCE_REBOOT_SCHEDULER_INTERVAL_SECONDS", 300),
        enable_https=_env_bool("BASSWIESN_ENABLE_HTTPS", False),
        https_port=_env_int("BASSWIESN_HTTPS_PORT", 1329),
        cert_mode=_env_text("BASSWIESN_CERT_MODE", "selfsigned").lower(),
        cert_days=_env_int("BASSWIESN_CERT_DAYS", 3650),
        tls_cert_file=(os.getenv("BASSWIESN_TLS_CERT_FILE") or "").strip(),
        tls_key_file=(os.getenv("BASSWIESN_TLS_KEY_FILE") or "").strip(),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
