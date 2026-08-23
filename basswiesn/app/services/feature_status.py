from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from basswiesn.app.config import get_settings
from basswiesn.app.models import Device, MediaRoot, MultiroomScenario, Preset, Setting, Station, WebhookEndpoint
from basswiesn.app.services.offline_mode import allowed_stream_hosts, offline_mode
from basswiesn.app.services.protected_devices import protected_device_ips


STATUS_ACTIVE = "Aktiv und verfügbar"
STATUS_CONFIG = "Aktiv, Konfiguration unvollständig"
STATUS_DISABLED = "Deaktiviert"
STATUS_EXPERIMENTAL = "Experimentell"
STATUS_LAB = "LAB"
STATUS_HARDWARE = "Hardwaretest offen"
STATUS_UNSUPPORTED = "Nicht unterstützt"
STATUS_UI_INCOMPLETE = "Backend vorhanden, UI unvollständig"


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _target(view: str, anchor: str = "") -> dict[str, str]:
    return {"view": view, "anchor": anchor}


def _doc(document_id: str, title: str = "Dokumentation") -> dict[str, str]:
    return {"id": document_id, "title": title, "href": f"/api/features/docs/{document_id}"}


def _status(
    *,
    enabled: bool,
    available: bool,
    configured: bool,
    experimental: bool,
    lab_only: bool,
    ui_complete: bool,
    hardware_status: str,
) -> str:
    if not enabled:
        return STATUS_DISABLED
    if experimental:
        return STATUS_EXPERIMENTAL
    if lab_only:
        return STATUS_LAB
    if not ui_complete:
        return STATUS_UI_INCOMPLETE
    if not available:
        return STATUS_UNSUPPORTED
    if not configured:
        return STATUS_CONFIG
    if hardware_status == "offen":
        return STATUS_HARDWARE
    return STATUS_ACTIVE


def _feature(
    *,
    feature_id: str,
    title: str,
    category: str,
    description: str,
    maturity: str,
    enabled: bool,
    available: bool,
    configured: bool,
    restart_required: bool,
    experimental: bool = False,
    lab_only: bool = False,
    hardware_status: str = "nicht erforderlich",
    blockers: list[str] | None = None,
    requirements: list[str] | None = None,
    settings_target: dict[str, str] | None = None,
    navigation_target: dict[str, str] | None = None,
    documentation: dict[str, str] | None = None,
    safe_test_available: bool = False,
    security_status: str = "Keine sensiblen Werte erforderlich",
    backend_present: bool = True,
    ui_complete: bool = True,
    feature_flags: list[str] | None = None,
    activation_method: str = "Laufzeitstatus",
) -> dict:
    return {
        "id": feature_id,
        "title": title,
        "category": category,
        "description": description,
        "maturity": maturity,
        "status": _status(
            enabled=enabled,
            available=available,
            configured=configured,
            experimental=experimental,
            lab_only=lab_only,
            ui_complete=ui_complete,
            hardware_status=hardware_status,
        ),
        "enabled": enabled,
        "available": available,
        "configured": configured,
        "restart_required": restart_required,
        "experimental": experimental,
        "lab_only": lab_only,
        "hardware_status": hardware_status,
        "security_status": security_status,
        "blockers": blockers or [],
        "requirements": requirements or [],
        "settings_target": settings_target,
        "navigation_target": navigation_target,
        "documentation": documentation,
        "safe_test_available": safe_test_available,
        "backend_present": backend_present,
        "ui_complete": ui_complete,
        "feature_flags": feature_flags or [],
        "activation_method": activation_method,
    }


def _effective_lab_mode(rows: dict[str, str]) -> bool:
    return _bool(rows.get("lab_mode"), get_settings().lab_mode)


def build_feature_status(db: Session) -> list[dict]:
    """Build a local, read-only activation view without contacting radios."""
    settings = get_settings()
    rows = {row.key: row.value for row in db.query(Setting).all()}
    devices = db.query(Device).order_by(Device.name, Device.device_id).all()
    device_count = len(devices)
    station_count = db.query(Station).count()
    preset_count = db.query(Preset).count()
    scenario_count = db.query(MultiroomScenario).count()
    protected_ips = protected_device_ips()
    lab_mode = _effective_lab_mode(rows)
    configured_lan_host = bool((rows.get("lan_host") or settings.lan_host).strip())
    write_guard = _bool(rows.get("ip_write_guard"))
    write_allowlist = bool((rows.get("ip_write_allowed_ips") or "").strip() or settings.setup_write_radio_ips)
    offline = offline_mode(db)
    stream_hosts = allowed_stream_hosts(db)

    media_rows = db.query(MediaRoot).filter(MediaRoot.enabled.is_(True)).all()
    media_paths = [row.path for row in media_rows] or list(settings.media_roots)
    media_roots_valid = bool(media_paths) and all(Path(path).expanduser().is_dir() for path in media_paths)
    webhook_db_ready = False
    for row in db.query(WebhookEndpoint).filter(WebhookEndpoint.enabled.is_(True)).all():
        try:
            webhook_db_ready = webhook_db_ready or bool(json.loads(row.allowlist_json or "[]"))
        except (TypeError, ValueError):
            continue
    webhook_ready = bool(settings.webhook_allowed_hosts) or webhook_db_ready
    update_enabled = _bool(rows.get("update_check_enabled"), settings.update_check_enabled)
    update_url = (rows.get("update_manifest_url") or settings.update_manifest_url).strip()
    logo_mode_count = db.query(Setting).filter(Setting.key.like("station_art_mode:%")).count()
    logo_count = db.query(Station).filter(Station.image_url.is_not(None), Station.image_url != "").count()
    multiroom_configured = device_count >= 2

    features = [
        _feature(
            feature_id="webgui_api", title="WebGUI und API", category="Kern",
            description="Lokale Benutzeroberfläche und HTTP-API für BASSWIESN.", maturity="Kern",
            enabled=True, available=True, configured=True, restart_required=False,
            navigation_target=_target("dashboard"), documentation=_doc("project-status", "Projektstatus"), safe_test_available=True,
        ),
        _feature(
            feature_id="local_database", title="Lokale Datenbank", category="Kern",
            description="Persistiert Radios, Sender, Presets, Einstellungen, Jobs und Backups.", maturity="Kern",
            enabled=True, available=True, configured=True, restart_required=False,
            navigation_target=_target("system-settings"), documentation=_doc("project-status", "Projektstatus"), safe_test_available=True,
        ),
        _feature(
            feature_id="discovery", title="Discovery", category="Setup",
            description="SSDP und begrenzter IP-Fallback finden Radios im lokalen Netz.", maturity="Kern",
            enabled=settings.ssdp_enabled or settings.ip_scan_fallback,
            available=settings.ssdp_enabled or settings.ip_scan_fallback,
            configured=settings.ssdp_enabled or settings.ip_scan_fallback,
            restart_required=True, blockers=[] if settings.ssdp_enabled or settings.ip_scan_fallback else ["SSDP und IP-Fallback sind deaktiviert"],
            requirements=["Privates LAN", "Schutz-IP-Liste beachten"], navigation_target=_target("devices"),
            documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=False,
        ),
        _feature(
            feature_id="setup_redirect", title="Setup und Redirect", category="Setup",
            description="Preflight, Backup, Cloud-Route, Apply, Verify und Rollback für ein Radio.", maturity="Kern",
            enabled=True, available=configured_lan_host, configured=configured_lan_host, restart_required=True,
            blockers=[] if configured_lan_host else ["BASSWIESN LAN-IP fehlt"],
            requirements=["LAN-IP des BASSWIESN-Servers", "Backup und Write-Guard vor Live-Write"],
            settings_target=_target("system-settings", "system-lan-host"), navigation_target=_target("setup"),
            documentation=_doc("activation-gaps", "Aktivierungslücken"), safe_test_available=True,
            security_status="Backup, Confirmation und serverseitige Write-Gates",
        ),
        _feature(
            feature_id="local_stations", title="Lokale Sender", category="Sender und Presets",
            description="Lokaler Senderkatalog mit Streamanalyse und explizitem Playback.", maturity="Kern",
            enabled=True, available=True, configured=station_count > 0, restart_required=False,
            blockers=[] if station_count else ["Noch kein lokaler Sender angelegt"],
            navigation_target=_target("stations"), documentation=_doc("project-status", "Projektstatus"), safe_test_available=False,
        ),
        _feature(
            feature_id="preset_readback", title="Preset Readback", category="Sender und Presets",
            description="Liest Radio-Presets zurück und vergleicht sie mit der lokalen Definition.", maturity="Kern",
            enabled=True, available=device_count > 0, configured=device_count > 0, restart_required=False,
            hardware_status="offen", blockers=[] if device_count else ["Mindestens ein Radio lokal konfigurieren"],
            requirements=["Radio erreichbar", "Read-only /presets-Readback"], navigation_target=_target("presets"),
            documentation=_doc("testing", "Testkonzept"), safe_test_available=True,
            security_status="Read-only; kein Radio-Write im Statuszentrum",
        ),
        _feature(
            feature_id="preset_write", title="Preset Write", category="Sender und Presets",
            description="Schreibt ein einzelnes lokales Preset mit Backup, Guards und Readback.", maturity="Kern",
            enabled=True, available=device_count > 0, configured=device_count > 0, restart_required=False,
            hardware_status="offen", blockers=[] if device_count else ["Mindestens ein Radio lokal konfigurieren"],
            requirements=["Memory-Check", "IP Write Guard und Schutz-IP beachten", "Radio-Readback"],
            settings_target=_target("system-settings", "ip-write-guard"), navigation_target=_target("presets"),
            documentation=_doc("activation-gaps", "Aktivierungslücken"), safe_test_available=False,
            security_status="Schreibaktion nur mit Bestätigung und serverseitigen Guards",
        ),
        _feature(
            feature_id="preset_sync", title="Preset-Synchronisierung", category="Sender und Presets",
            description="Synchronisiert lokale Presets und bestätigt die Slots durch Radio-Readback.", maturity="Kern",
            enabled=True, available=device_count > 0, configured=preset_count > 0, restart_required=False,
            hardware_status="offen", blockers=[] if preset_count else ["Keine lokalen Presets zum Synchronisieren"],
            requirements=["Memory-Check", "Backup vor Write", "Slotweiser Readback"],
            settings_target=_target("device-settings", "sync-device-presets"), navigation_target=_target("presets"),
            documentation=_doc("activation-gaps", "Aktivierungslücken"), safe_test_available=True,
            security_status="Write-Guard, Schutz-IP und explizite Nutzerbestätigung",
        ),
        _feature(
            feature_id="station_logo", title="Senderlogo", category="Sender und Presets",
            description="Verwendet gespeicherte Senderlogos als containerArt in neuen oder synchronisierten Presets.", maturity="Kern",
            enabled=True, available=device_count > 0, configured=logo_mode_count > 0 and logo_count > 0, restart_required=False,
            hardware_status="offen", blockers=[] if logo_count else ["Mindestens ein Sender benötigt ein validiertes Logo"],
            requirements=["Logo-URL oder lokaler BASSWIESN-Pfad", "Preset-Synchronisierung nach Änderung"],
            settings_target=_target("device-settings", "station_art_mode"), navigation_target=_target("device-settings"),
            documentation=_doc("activation-gaps", "Aktivierungslücken"), safe_test_available=True,
            security_status="Logoabruf nur nach URL- und Größenprüfung; Readback erforderlich",
        ),
        _feature(
            feature_id="offline_mode", title="Offline Mode", category="Abhängigkeiten",
            description="Steuert BASSWIESN-eigene optionale externe Requests; nicht die Internetverbindung des Radios.", maturity="Kern",
            enabled=True, available=True, configured=offline in {"off", "auto", "strict"}, restart_required=False,
            blockers=[] if offline != "strict" or stream_hosts else ["Strict erlaubt keine konfigurierten Stream-Hosts"],
            requirements=["off, auto oder strict", "Strict kontrolliert BASSWIESN-Requests, nicht das Radio"],
            settings_target=_target("system-settings", "offline-mode"), navigation_target=_target("system-settings"),
            documentation=_doc("offline", "Offline-Betrieb"), safe_test_available=True,
        ),
        _feature(
            feature_id="multiroom", title="Multiroom", category="Multiroom",
            description="Erstellt und liest Multiroom-Zonen über die vorhandenen Router und Safety-Gates.", maturity="Kern",
            enabled=True, available=device_count >= 1, configured=multiroom_configured, restart_required=False,
            hardware_status="offen", blockers=[] if multiroom_configured else ["Mindestens zwei Radios konfigurieren"],
            requirements=["Erreichbare Radios", "Schutz-IP und Volume-Readback"], navigation_target=_target("multiroom"),
            documentation=_doc("project-status", "Projektstatus"), safe_test_available=True,
            security_status="Preview read-only; Live-Write nur mit bestehenden Guards",
        ),
        _feature(
            feature_id="multiroom_preserve_volumes", title="Multiroom ohne Lautstärkeänderung", category="Multiroom",
            description="Baut eine Zone mit preserve_volumes auf und prüft Lautstärkeabweichungen.", maturity="Kern",
            enabled=True, available=device_count >= 2, configured=multiroom_configured, restart_required=False,
            hardware_status="offen", blockers=[] if multiroom_configured else ["Mindestens zwei Radios konfigurieren"],
            requirements=["Aktuelle Lautstärken vorab lesen", "Lautstärken nachher zurücklesen"], navigation_target=_target("multiroom"),
            documentation=_doc("testing", "Testkonzept"), safe_test_available=True,
            security_status="Preview und Schutz-IP vor Live-Write",
        ),
        _feature(
            feature_id="multiroom_scenes", title="BASSWIESN-Multiroom-Szenen", category="Multiroom",
            description="Serverseitige Szenen mit Master, Mitgliedern, Sender und Lautstärkeoptionen; kein natives Bose-Preset.", maturity="Kern",
            enabled=True, available=device_count >= 1, configured=scenario_count > 0, restart_required=False,
            hardware_status="offen", blockers=[] if scenario_count else ["Noch keine BASSWIESN-Szene gespeichert"],
            requirements=["BASSWIESN-Server erforderlich", "Master und Teilnehmer", "Readback nach Aktivierung"], navigation_target=_target("multiroom", "multiroom-scenario-form"),
            documentation=_doc("activation-gaps", "Aktivierungslücken"), safe_test_available=True,
            security_status="Serverseitige Szene; Teilnehmer- und Schutzprüfung vor Aktivierung",
        ),
        _feature(
            feature_id="protected_ips", title="Schutz-IP-Liste", category="Sicherheit",
            description="Blockiert serverseitig automatische und manuelle Netzwerkzugriffe auf geschützten Radio-IPs.", maturity="Sicherheit",
            enabled=True, available=True, configured=bool(protected_ips), restart_required=False,
            blockers=[] if protected_ips else ["Keine Schutz-IP konfiguriert"], requirements=["PROTECTED_DEVICE_IPS oder geschützte IP in Einstellungen"],
            settings_target=_target("system-settings", "protected-device-ips"), navigation_target=_target("system-settings"),
            documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
            security_status="Serverseitig wirksam; Liste wird nicht als Geheimnis behandelt",
        ),
        _feature(
            feature_id="backup", title="Backup", category="Sicherung und Diagnose",
            description="Erstellt redigierte Systembackups mit Manifest, SHA256 und Journal.", maturity="Kern",
            enabled=True, available=True, configured=settings.data_dir.is_dir(), restart_required=False,
            blockers=[] if settings.data_dir.is_dir() else ["Datenverzeichnis ist nicht verfügbar"], requirements=["Schreibbares Datenverzeichnis"],
            navigation_target=_target("backup"), documentation=_doc("project-status", "Projektstatus"), safe_test_available=True,
            security_status="Settings werden im Backup redigiert",
        ),
        _feature(
            feature_id="restore", title="Restore", category="Sicherung und Diagnose",
            description="Backup-Preview und Restore-Vorbereitung sind vorhanden; vollständiger sicherer UI-Restore fehlt.", maturity="Infrastruktur",
            enabled=True, available=False, configured=True, restart_required=True, ui_complete=False,
            blockers=["Vollständiger staged UI-Restore mit Rollback fehlt"], requirements=["Backup", "Prozess-/Containerneustart", "Rollbackplan"],
            navigation_target=_target("backup"), documentation=_doc("activation-gaps", "Aktivierungslücken"), safe_test_available=True,
            security_status="Restore bleibt Preview/Prepare; kein stiller Web-Restore",
        ),
        _feature(
            feature_id="diagnostics", title="Diagnose", category="Sicherung und Diagnose",
            description="Telemetry, Request-Logs, Emulation Gaps und technische Exporte ohne Radioaktion.", maturity="Kern",
            enabled=True, available=True, configured=True, restart_required=False,
            navigation_target=_target("telemetry"), documentation=_doc("testing", "Testkonzept"), safe_test_available=True,
        ),
        _feature(
            feature_id="health_center", title="Health Center", category="Sicherung und Diagnose",
            description="Lesender System-, Service- und Healthcheck-Überblick.", maturity="Kern",
            enabled=True, available=True, configured=True, restart_required=False,
            navigation_target=_target("dashboard", "system-health"), documentation=_doc("project-status", "Projektstatus"), safe_test_available=True,
        ),
        _feature(
            feature_id="support_bundle", title="Support Bundle", category="Sicherung und Diagnose",
            description="Erzeugt ein redigiertes Diagnosepaket ohne bekannte Secrets.", maturity="Kern",
            enabled=True, available=True, configured=True, restart_required=False,
            navigation_target=_target("telemetry"), documentation=_doc("project-status", "Projektstatus"), safe_test_available=True,
            security_status="Redaction vor Export",
        ),
        _feature(
            feature_id="update_check", title="Update Check", category="Updates",
            description="Prüft manuell ein konfiguriertes externes Release-Manifest.", maturity="Kern",
            enabled=update_enabled, available=update_enabled and bool(update_url), configured=bool(update_url), restart_required=False,
            blockers=[] if update_enabled and update_url else (["Updateprüfung deaktiviert"] if not update_enabled else ["Manifest-URL fehlt"]),
            requirements=["Update-Flag oder UI-Einstellung", "Manifest-URL", "Strict Offline kann externe Prüfung blockieren"],
            settings_target=_target("system-settings", "update-manifest-url"), navigation_target=_target("system-settings"),
            documentation=_doc("release-pipeline", "Releasepipeline"), safe_test_available=True,
        ),
        _feature(
            feature_id="local_update", title="Local Update", category="Updates",
            description="Prüft lokale Archive und bereitet ein Update vor; vollständige UI-Ausführung fehlt.", maturity="Infrastruktur",
            enabled=settings.update_allow_local_archive, available=False, configured=settings.update_allow_local_archive,
            restart_required=True, ui_complete=False, blockers=["Vollständiger UI-Ablauf mit Neustart und Rollback fehlt"],
            requirements=["Lokales Archiv", "SHA256", "Administrativer Neustart"], navigation_target=_target("system-settings"),
            documentation=_doc("activation-gaps", "Aktivierungslücken"), safe_test_available=True,
            security_status="Archivprüfung vor Prepare; kein automatischer Neustart",
        ),
        _feature(
            feature_id="telnet_reboot", title="Telnet-Reboot", category="Experimentell",
            description="Profilbasierter Reboot; kein freies Telnet-Kommando.", maturity="Experimentell",
            enabled=settings.telnet_enabled, available=settings.telnet_enabled and bool(settings.telnet_allowed_device_ids) and bool(settings.telnet_username or settings.telnet_password_file),
            configured=bool(settings.telnet_allowed_device_ids) and bool(settings.telnet_username or settings.telnet_password_file), restart_required=True,
            experimental=True, hardware_status="offen", blockers=([] if settings.telnet_enabled else ["BASSWIESN_TELNET_ENABLED=false"]) + ([] if settings.telnet_allowed_device_ids else ["Geräte-Allowlist fehlt"]) + ([] if settings.telnet_username or settings.telnet_password_file else ["Secretquelle fehlt"]),
            requirements=["Device-Allowlist", "Secretquelle per Environment/Datei", "Kompatibles Profil", "Hardwaretest"],
            settings_target=_target("system-settings"), navigation_target=_target("telnet"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
            security_status="Unverschlüsseltes Protokoll; Secretwerte werden nie angezeigt",
        ),
        _feature(
            feature_id="standby_clock_recovery", title="Standby Clock Recovery", category="Experimentell",
            description="Manuelle Clock-Recovery mit Profil, Confirmation und Readback.", maturity="Experimentell",
            enabled=settings.standby_clock_recovery_enabled, available=False, configured=settings.standby_clock_recovery_enabled,
            restart_required=True, experimental=True, hardware_status="offen", blockers=[] if settings.standby_clock_recovery_enabled else ["BASSWIESN_STANDBY_CLOCK_RECOVERY_ENABLED=false", "Hardwaretest offen"],
            requirements=["Kompatibles Modellprofil", "Write-Guard", "Sicht-/Readbackprüfung"], navigation_target=_target("telnet"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
            security_status="Manuelle Confirmation und Write-Guard",
        ),
        _feature(
            feature_id="local_media", title="Lokale Medien", category="Experimentell",
            description="Lokale Medienroots, Bibliothek und Playlist-Infrastruktur.", maturity="Experimentell",
            enabled=settings.media_enabled, available=settings.media_enabled and media_roots_valid, configured=bool(media_paths) and media_roots_valid,
            restart_required=True, experimental=True, blockers=[] if settings.media_enabled and media_roots_valid else (["BASSWIESN_MEDIA_ENABLED=false"] if not settings.media_enabled else ["Keine gültige Medien-Root konfiguriert"]),
            requirements=["Explizite existierende Medien-Root", "Unterstütztes Audioformat"], settings_target=_target("system-settings"), navigation_target=_target("media"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
        ),
        _feature(
            feature_id="dlna", title="DLNA", category="Experimentell",
            description="Manuelle, experimentelle Renderer-Erkennung ohne Hintergrundscan.", maturity="Experimentell",
            enabled=settings.experimental_dlna, available=False, configured=settings.experimental_dlna, restart_required=True,
            experimental=True, hardware_status="offen", blockers=[] if settings.experimental_dlna else ["BASSWIESN_EXPERIMENTAL_DLNA=false", "Hardwaretest offen"],
            requirements=["Expliziter manueller Start", "Kompatibles Renderer-/Radioverhalten"], settings_target=_target("system-settings"), navigation_target=_target("media"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
        ),
        _feature(
            feature_id="announcements", title="Announcements / TTS", category="Experimentell",
            description="Preview- und Jobinfrastruktur für begrenzte Ansagen; keine bestätigte Wiedergabe.", maturity="Experimentell",
            enabled=settings.experimental_announcements, available=False, configured=settings.experimental_announcements, restart_required=True,
            experimental=True, hardware_status="offen", blockers=[] if settings.experimental_announcements else ["BASSWIESN_EXPERIMENTAL_ANNOUNCEMENTS=false", "Hardwaretest offen"],
            requirements=["Explizite Confirmation", "Audio-/Radio-Readback"], settings_target=_target("system-settings"), navigation_target=_target("lab"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
        ),
        _feature(
            feature_id="lab_mode", title="LAB Mode", category="Experimentell",
            description="Zeigt vorbereitete, nicht produktionsreife Diagnose- und Schreibpläne.", maturity="LAB",
            enabled=lab_mode, available=lab_mode, configured=lab_mode, restart_required=False,
            lab_only=True, blockers=[] if lab_mode else ["LAB Mode ist deaktiviert"], requirements=["Expertenworkflow", "Je Funktion zusätzliche Guards"],
            settings_target=_target("system-settings", "lab-mode"), navigation_target=_target("lab"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
            security_status="LAB; Preview und Confirmation vor Live-Action",
        ),
        _feature(
            feature_id="webhooks", title="Webhooks", category="Experimentell",
            description="Konfigurierbare Ereigniszustellung an explizit erlaubte Zielhosts.", maturity="Experimentell",
            enabled=settings.webhooks_enabled, available=settings.webhooks_enabled and webhook_ready, configured=webhook_ready, restart_required=True,
            experimental=True, blockers=[] if settings.webhooks_enabled and webhook_ready else (["BASSWIESN_WEBHOOKS_ENABLED=false"] if not settings.webhooks_enabled else ["Ziel-Allowlist fehlt"]),
            requirements=["Zielhost-Allowlist", "Secret bleibt außerhalb der Anzeige"], settings_target=_target("system-settings"), navigation_target=_target("config"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
            security_status="Nur erlaubte Hosts; Secretwerte redigiert",
        ),
        _feature(
            feature_id="https", title="HTTPS", category="Sicherheit",
            description="Optionale TLS-WebGUI mit Self-Signed- oder eigener Zertifikatskonfiguration.", maturity="Kern",
            enabled=settings.enable_https, available=settings.enable_https and (settings.cert_mode == "selfsigned" or bool(settings.tls_cert_file and settings.tls_key_file)),
            configured=settings.enable_https and (settings.cert_mode == "selfsigned" or bool(settings.tls_cert_file and settings.tls_key_file)), restart_required=True,
            blockers=[] if settings.enable_https else ["BASSWIESN_ENABLE_HTTPS=false"], requirements=["Zertifikatsmodus", "Neustart des Webdienstes"], settings_target=_target("system-settings"), navigation_target=_target("system-settings"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=True,
            security_status="Zertifikatspfade und private Schlüssel werden nicht ausgegeben",
        ),
        _feature(
            feature_id="maintenance_reboot", title="Manueller Radio-Reboot", category="Wartung",
            description="Recovery-Stufe 7 als ausschliesslich manuelle LAB-Aktion; kein Scheduler und keine automatische Ausfuehrung.", maturity="Experimentell",
            enabled=lab_mode, available=lab_mode, configured=lab_mode, restart_required=False,
            experimental=True, lab_only=True, blockers=[] if lab_mode else ["LAB Mode ist deaktiviert"], requirements=["Manueller LAB-Start", "Explizite REBOOT RADIO-Bestaetigung", "Backup und Reboot-Readback"], settings_target=_target("system-settings", "lab-mode"), navigation_target=_target("devices"), documentation=_doc("activation-matrix", "Aktivierungsmatrix"), safe_test_available=False,
            security_status="Nie automatisch; geschuetzte Radios bleiben serverseitig blockiert",
        ),
    ]
    feature_flags = {
        "discovery": ["BASSWIESN_SSDP_ENABLED", "BASSWIESN_IP_SCAN_FALLBACK"],
        "setup_redirect": ["BASSWIESN_LAN_HOST", "BASSWIESN_SETUP_WRITE_RADIO_IPS"],
        "offline_mode": ["BASSWIESN_OFFLINE_MODE", "BASSWIESN_OFFLINE_ALLOWED_STREAM_HOSTS"],
        "update_check": ["BASSWIESN_UPDATE_CHECK_ENABLED", "BASSWIESN_UPDATE_MANIFEST_URL"],
        "local_update": ["BASSWIESN_UPDATE_ALLOW_LOCAL_ARCHIVE"],
        "telnet_reboot": ["BASSWIESN_TELNET_ENABLED", "BASSWIESN_TELNET_ALLOWED_DEVICE_IDS", "BASSWIESN_TELNET_USERNAME oder BASSWIESN_TELNET_PASSWORD_FILE"],
        "standby_clock_recovery": ["BASSWIESN_STANDBY_CLOCK_RECOVERY_ENABLED"],
        "local_media": ["BASSWIESN_MEDIA_ENABLED", "BASSWIESN_MEDIA_ROOTS"],
        "dlna": ["BASSWIESN_EXPERIMENTAL_DLNA"],
        "announcements": ["BASSWIESN_EXPERIMENTAL_ANNOUNCEMENTS"],
        "lab_mode": ["BASSWIESN_LAB_MODE"],
        "webhooks": ["BASSWIESN_WEBHOOKS_ENABLED", "BASSWIESN_WEBHOOK_ALLOWED_HOSTS"],
        "https": ["BASSWIESN_ENABLE_HTTPS", "BASSWIESN_CERT_MODE"],
        "maintenance_reboot": ["BASSWIESN_LAB_MODE"],
        "protected_ips": ["PROTECTED_DEVICE_IPS oder UI-Einstellung"],
    }
    for feature in features:
        feature["feature_flags"] = feature_flags.get(feature["id"], [])
        if feature["feature_flags"]:
            feature["activation_method"] = "Environment/Runtime-Konfiguration"
        elif feature.get("settings_target"):
            feature["activation_method"] = "UI-Einstellung oder lokale Datenbank"
        elif feature.get("navigation_target"):
            feature["activation_method"] = "Vorhandener Bedienbereich"
    return features
