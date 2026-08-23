from contextlib import asynccontextmanager
import asyncio
from datetime import UTC, datetime
import json
import logging
import platform

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text as sql_text

from basswiesn.app.config import get_settings, is_safe_radio_host
from basswiesn.app.core.errors import install_exception_handlers
from basswiesn.app.core.masterlog import write_masterlog
from basswiesn.app.services.config_rewrite import HOSTS_DOMAINS
from basswiesn.app.services.alarm_engine import alarm_engine_loop
from basswiesn.app.services.playback_keepalive import playback_keepalive_loop
from basswiesn.app.services.research_runtime import ResearchRuntime
from basswiesn.app.db import init_db, SessionLocal
from basswiesn.app.models import RuntimeState, utc_now
from basswiesn.app.services.playback_state import reconcile_open_play_history
from basswiesn.app.services.filesystem_contract import ensure_runtime_directories
from basswiesn.app.services.task_registry import start_owned_task, stop_owned_task
from basswiesn.app.api import routes_devices
from basswiesn.app.routers import api, catalogs, cloud, debug, devices, fulltest, media, multiroom, research_state, setup, setup_rebuild, stations_presets, telemetry

logger = logging.getLogger(__name__)


def _duplicate_contract_routes(app: FastAPI) -> list[dict]:
    target = "/api/devices/{device_id}/telnet/reboot"
    matches = []
    for route in app.routes:
        candidates = [route]
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            prefix = getattr(context, "prefix", "")
            candidates = [(item, prefix) for item in getattr(included, "routes", [])]
        for candidate in candidates:
            item, prefix = candidate if isinstance(candidate, tuple) else (candidate, "")
            item_path = getattr(item, "path", "")
            full_path = item_path if item_path.startswith(prefix or "\0") else f"{prefix}{item_path}"
            if full_path != target:
                continue
            matches.append({"path": target, "methods": sorted(getattr(item, "methods", set())), "handler": getattr(getattr(item, "endpoint", None), "__name__", "unknown")})
    return matches if len(matches) > 1 else []


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_runtime_directories(get_settings().data_dir)
    init_db()
    duplicate_routes = _duplicate_contract_routes(app)
    if duplicate_routes:
        logger.warning("Duplicate legacy route contract remains mounted: %s", duplicate_routes)
        write_masterlog("duplicate_route_contract", routes=duplicate_routes)
    if app.title == "basswiesn WebGUI":
        reconciliation_db = SessionLocal()
        try:
            repaired = reconcile_open_play_history(reconciliation_db)
            write_masterlog("play_history_startup_reconciliation", repaired=repaired)
        finally:
            reconciliation_db.close()
    boot_ts = datetime.now(UTC)
    _record_server_boot(app.title, boot_ts)
    logger.info("BASSWIESN application version: %s", get_settings().version)
    write_masterlog("application_version", message=f"BASSWIESN application version: {get_settings().version}", version=get_settings().version)
    write_masterlog("app_start", service=app.title, version=get_settings().version)
    write_masterlog("server_boot", service=app.title, version=get_settings().version)
    stop_keepalive = asyncio.Event()
    stop_alarm_engine = asyncio.Event()
    keepalive_task = None
    alarm_task = None
    setup_resume_task = None
    research_runtime = ResearchRuntime(lambda: SessionLocal())
    app.state.research_runtime = research_runtime
    starts_background_tasks = bool(getattr(app.state, "starts_background_tasks", True))
    if app.title == "basswiesn WebGUI" and starts_background_tasks:
        # Rehydrate only persisted/local research state. Startup performs no
        # device discovery, radio request or provider catch-up burst.
        await research_runtime.start()
    elif app.title == "basswiesn Cloud Emulator":
        # The cloud service may schedule one-shot work in response to an
        # incoming provider request, but startup itself creates no task and
        # performs no external contact.
        research_runtime.enable_event_tasks()
    if app.title == "basswiesn WebGUI" and starts_background_tasks and get_settings().playback_keepalive_enabled:
        keepalive_task = start_owned_task(
            "playback_keepalive",
            lambda: playback_keepalive_loop(
                stop_keepalive, research_runtime=research_runtime
            ),
            stop_event=stop_keepalive,
        )
    if app.title == "basswiesn WebGUI" and starts_background_tasks:
        alarm_task = start_owned_task("alarm_engine", lambda: alarm_engine_loop(stop_alarm_engine), stop_event=stop_alarm_engine)
    if app.title == "basswiesn WebGUI" and starts_background_tasks:
        from basswiesn.app.services.setup_rebuild.coordinator import get_coordinator

        # The rebuild job/state is durable. After a container restart an
        # interrupted job is marked for explicit review without replaying a
        # possibly already-applied radio write; the database lease still
        # prevents multiple Uvicorn processes from executing a live job.
        setup_resume_task = asyncio.create_task(get_coordinator().resume_pending_jobs())
    yield
    stop_keepalive.set()
    stop_alarm_engine.set()
    if keepalive_task:
        await stop_owned_task("playback_keepalive")
    if alarm_task:
        await stop_owned_task("alarm_engine")
    if setup_resume_task:
        setup_resume_task.cancel()
        try:
            await setup_resume_task
        except asyncio.CancelledError:
            pass
    await research_runtime.shutdown()
    _record_server_shutdown(app.title, boot_ts)
    write_masterlog("server_shutdown", service=app.title, runtime_seconds=int((datetime.now(UTC) - boot_ts).total_seconds()))


def _runtime_state(db, key: str) -> RuntimeState:
    db.execute(
        sql_text("INSERT OR IGNORE INTO runtime_state (key, value, updated_at) VALUES (:key, '', :updated_at)"),
        {"key": key, "updated_at": utc_now()},
    )
    row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
    if row is None:
        db.flush()
        row = db.query(RuntimeState).filter(RuntimeState.key == key).one()
    return row


def _record_server_boot(service: str, boot_ts: datetime) -> None:
    db = SessionLocal()
    try:
        first = _runtime_state(db, "server:first_boot")
        if not first.value:
            first.value = boot_ts.isoformat()
        count = _runtime_state(db, "server:restart_count")
        count.value = str(int(count.value or "0") + 1)
        _runtime_state(db, "server:last_boot").value = boot_ts.isoformat()
        _runtime_state(db, "server:last_service").value = service
        db.commit()
    finally:
        db.close()


def _record_server_shutdown(service: str, boot_ts: datetime) -> None:
    db = SessionLocal()
    try:
        runtime = max(0, int((datetime.now(UTC) - boot_ts).total_seconds()))
        total = _runtime_state(db, "server:total_runtime_seconds")
        total.value = str(int(total.value or "0") + runtime)
        _runtime_state(db, "server:last_shutdown").value = utc_now().isoformat()
        _runtime_state(db, "server:last_checkpoint").value = json.dumps({"service": service, "runtime_seconds": runtime, "ts": utc_now().isoformat()})
        db.commit()
        write_masterlog("server_runtime_checkpoint", service=service, runtime_seconds=runtime, total_runtime_seconds=int(total.value or "0"))
    finally:
        db.close()


def _allow_local_browser_clients(app: FastAPI) -> None:
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:1328",
            "http://localhost:1328",
            settings.web_base_url.rstrip("/"),
        ],
        allow_origin_regex=rf"^https?://(?:127\.0\.0\.1|localhost|10(?:\.\d{{1,3}}){{3}}|192\.168(?:\.\d{{1,3}}){{2}}):{settings.web_port}$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _mount_web_cloud_compat(app: FastAPI) -> None:
    """Serve Bose cloud compatibility paths on the WebGUI port without catchall."""

    app.add_api_route("/streaming/sourceproviders", cloud.streaming_sourceproviders, methods=["GET"])
    app.add_api_route("/streaming/provider-discovery", cloud.provider_discovery, methods=["GET"])
    app.add_api_route("/bmx/registry/v1/introspect", cloud.provider_discovery, methods=["GET"])
    app.add_api_route("/streaming/support/power_on", cloud.streaming_power_on, methods=["POST"])
    app.add_api_route("/streaming/device/{device_id}/streaming_token", cloud.streaming_token, methods=["GET"])
    app.add_api_route("/v1/blacklist/{device_id}", cloud.device_blacklist, methods=["GET", "POST"])
    app.add_api_route("/streaming/account/{account_id}/full", cloud.streaming_account_full, methods=["GET"])
    app.add_api_route("/streaming/account/{account_id}/provider_settings", cloud.streaming_provider_settings, methods=["GET"])
    app.add_api_route("/serviceSettings", cloud.service_settings, methods=["GET", "POST"])
    app.add_api_route("/getServiceSettings", cloud.service_settings, methods=["GET", "POST"])
    app.add_api_route("/stationInfo", cloud.station_info, methods=["GET"])
    app.add_api_route("/setMusicServiceAccount", cloud.set_music_service_account, methods=["POST"])
    app.add_api_route("/setMusicServiceOAuthAccount", cloud.set_music_service_oauth_account, methods=["POST"])
    app.add_api_route("/group", cloud.group_state, methods=["GET"])
    app.add_api_route("/group/create", cloud.group_create, methods=["POST"])
    app.add_api_route("/group/update", cloud.group_update, methods=["POST"])
    app.add_api_route("/group/delete", cloud.group_delete, methods=["POST"])
    app.add_api_route("/streaming/account/{account_id}/device/", cloud.streaming_add_device, methods=["POST"], status_code=201)
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}", cloud.streaming_get_device, methods=["GET"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}", cloud.streaming_put_device, methods=["PUT"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}", cloud.streaming_delete_device, methods=["DELETE"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}/heartbeat", cloud.streaming_device_keepalive, methods=["POST"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}/keepalive", cloud.streaming_device_keepalive, methods=["POST"])
    app.add_api_route("/streaming/account/{account_id}/sources", cloud.streaming_account_sources, methods=["GET"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}/presets", cloud.streaming_device_presets, methods=["GET"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}/recents", cloud.streaming_device_recents, methods=["GET"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}/recent", cloud.streaming_device_recent, methods=["POST"])
    app.add_api_route("/streaming/account/{account_id}/presets/all", cloud.streaming_account_presets, methods=["GET"])
    app.add_api_route("/core02/svc-bmx-adapter-orion/prod/orion/token", cloud.orion_token, methods=["POST"])
    app.add_api_route("/core02/svc-bmx-adapter-orion/prod/orion/station", cloud.orion_station, methods=["GET"])
    app.add_api_route("/bmx/registry/v1/services", cloud.bmx_registry, methods=["GET"])
    app.add_api_route("/bmx/registry/v1/servicesAvailability", cloud.bmx_services_availability, methods=["GET"])
    app.add_api_route("/bmx/registry/servicesAvailability", cloud.bmx_services_availability, methods=["GET"])
    app.add_api_route("/bmx/orion/now-playing", cloud.now_playing, methods=["GET"])
    app.add_api_route("/bmx/orion/now-playing/station/{station_id}", cloud.now_playing, methods=["GET"])
    app.add_api_route("/bmx/orion/reporting", cloud.reporting, methods=["POST"])
    app.add_api_route("/bmx/orion/reporting/station/{station_id}", cloud.reporting, methods=["POST"])
    app.add_api_route("/bmx/tunein/v1/playback/station/{station_id}", cloud.bmx_tunein_station, methods=["GET"])
    app.add_api_route("/bmx/tunein/v1/now-playing/station/{station_id}", cloud.bmx_tunein_station, methods=["GET"])
    app.add_api_route("/bmx/tunein/v1/reporting/station/{station_id}", cloud.bmx_tunein_station, methods=["POST"])
    app.add_api_route("/bmx/tunein/v1/favorite/{station_id}", cloud.bmx_tunein_station, methods=["GET", "POST"])
    app.add_api_route("/bmx/radiobrowser/v1/playback/station/{uuid}", cloud.bmx_radiobrowser_station, methods=["GET"])
    app.add_api_route("/bmx/radiobrowser/v1/now-playing/station/{uuid}", cloud.bmx_radiobrowser_station, methods=["GET"])
    app.add_api_route("/bmx/radiobrowser/v1/reporting/station/{uuid}", cloud.bmx_radiobrowser_station, methods=["POST"])
    app.add_api_route("/bmx/resolve", cloud.bmx_resolve, methods=["GET", "POST"])
    app.add_api_route("/v1/systems/devices/{device_id}/presets", cloud.marge_presets, methods=["GET"])
    app.add_api_route("/v1/systems/devices/{device_id}/sources", cloud.marge_sources, methods=["GET"])
    app.add_api_route("/v1/systems/devices/{device_id}", cloud.marge_full, methods=["GET"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}/preset/{button}", cloud.put_preset, methods=["PUT"])
    app.add_api_route("/streaming/account/{account_id}/device/{device_id}/presets/{button}", cloud.put_preset, methods=["PUT", "POST"])


def create_web_app(*, title: str = "basswiesn WebGUI", background_tasks: bool = True) -> FastAPI:
    app = FastAPI(title=title, lifespan=lifespan)
    app.state.starts_background_tasks = background_tasks
    install_exception_handlers(app)
    _allow_local_browser_clients(app)

    @app.middleware("http")
    async def cache_policy(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        content_type = response.headers.get("content-type", "")
        if path == "/api/health":
            response.headers["Cache-Control"] = "no-store"
        elif "text/html" in content_type:
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("/static/") and request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    app.mount("/static", StaticFiles(directory="basswiesn/app/static"), name="static")
    app.include_router(routes_devices.router)
    app.include_router(api.router)
    app.include_router(media.router)
    app.include_router(fulltest.router)
    app.include_router(stations_presets.router)
    app.include_router(multiroom.router)
    app.include_router(setup.router)
    app.include_router(setup_rebuild.router)
    app.include_router(catalogs.router)
    app.include_router(telemetry.router)
    app.include_router(devices.router)
    app.include_router(research_state.router)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        settings = get_settings()
        python_version = platform.python_version()
        docker_version = platform.platform()
        return f"""
        <!doctype html>
        <html lang="de"><head><title>basswiesn</title>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
        <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%231f6f5c'/%3E%3Ctext x='10' y='44' font-size='36' fill='white'%3Ebw%3C/text%3E%3C/svg%3E">
        <link rel="stylesheet" href="/static/app.css?v={settings.version}"></head>
        <body class="normal-mode guided-hints">
        <div class="app-shell" data-cloud-port="{settings.cloud_port}" data-debug-port="{settings.debug_port}" data-server-url="{settings.local_base_url}" data-cloud-base-url="{settings.local_base_url}" data-debug-base-url="{settings.debug_base_url}">
          <header class="topbar">
            <div class="brand"><div class="brand-mark">bw</div><div><h1>basswiesn</h1><p>SoundTouch Local Cloud</p></div></div>
            <div class="top-clock" aria-label="Aktuelle Zeit und Server"><span id="clock-date">--.--.----</span><strong id="clock-time">--:--</strong><small id="server-identity">Version wird geladen · Host nicht gesetzt</small></div>
            <nav class="topnav" aria-label="Hauptnavigation">
              <button class="nav-button is-active" data-view="dashboard">Start</button>
              <button class="nav-button" data-view="features" data-normal>Funktionen &amp; Aktivierung</button>
              <button class="nav-button" data-view="setup" data-normal>Setup</button>
              <button class="nav-button" data-view="devices" data-normal>Radios</button>
              <button class="nav-button" data-view="health" data-normal>Status &amp; Diagnose</button>
              <button class="nav-button" data-view="controls">Fernbedienung</button>
              <button class="nav-button" data-view="stations">Sender</button>
              <button class="nav-button" data-view="presets" data-normal>Presets</button>
              <button class="nav-button" data-view="multiroom">Multiroom</button>
              <button class="nav-button" data-view="schedules">Wecker Timer</button>
              <button class="nav-button" data-view="device-settings" data-normal>Device Settings</button>
              <details class="advanced-nav"><summary>Mehr</summary><div>
                <button class="nav-button" data-view="display" data-capability="display clockDisplay">Display</button>
                <button class="nav-button" data-view="media">Musikbibliothek</button>
                <button class="nav-button" data-view="backup">Sicherung</button>
                <button class="nav-button" data-view="config">Technik</button>
                <button class="nav-button" data-view="telnet">Telnet</button>
                <button class="nav-button" data-view="debug">Protokoll</button>
                <button class="nav-button" data-view="telemetry">Diagnose</button>
                <button class="nav-button" data-view="lab">Labor</button>
              </div></details>
              <button class="nav-button" data-view="about" data-normal>Über BASSWIESN</button>
              <button class="nav-button" data-view="system-settings" data-normal>Einstellungen</button>
            </nav>
          </header>
          <div class="app-toast" id="app-toast" role="status" aria-live="polite" hidden></div>
          <main class="workspace">
            <section class="view is-active" id="view-dashboard">
              <div class="page-head"><div><span class="section-kicker">Übersicht</span><h2>Deine SoundTouch Anlage</h2></div><button class="command" id="refresh-all">Aktualisieren</button></div>
              <div class="metric-grid">
                <article class="metric"><span>Bedienoberfläche</span><strong>Bereit</strong><small id="web-state">wird geprüft</small></article>
                <article class="metric"><span>Lokale Cloud</span><strong>Verbunden</strong><small id="cloud-state">wird geprüft</small></article>
                <article class="metric"><span>Diagnose</span><strong id="debug-summary">wird geprüft</strong><small id="debug-state">wird geprüft</small></article>
                <article class="metric"><span>Aktivitäten</span><strong id="request-count">0</strong><small>zuletzt erfasst</small></article>
              </div>
              <div class="service-link-grid"><a class="service-link" data-service-link="cloud" href="{settings.local_base_url.rstrip('/')}/about" target="_blank"><strong>Cloud-Dienst · :{settings.cloud_port}</strong><span>Registry, Quellen und Radio-Anfragen ansehen</span></a><a class="service-link" data-service-link="debug" href="{settings.debug_base_url.rstrip('/')}/" target="_blank"><strong>Diagnose · :{settings.debug_port}</strong><span>Status, Requests und Diagnose-Endpunkte öffnen</span></a></div>
              <section class="panel"><div class="panel-title-row"><div><h3>System Health</h3><p class="muted-copy">Release-Check für API, Datenbank, Storage, Emulator und Ports.</p></div><button class="command" id="reload-health" type="button">Health prüfen</button></div><div id="system-health" class="event-list"></div></section>
              <div class="split"><section class="panel"><h3>Devices</h3><div id="dashboard-devices" class="list"></div></section><section class="panel"><h3>Recent Requests</h3><div id="dashboard-requests" class="event-list"></div></section></div>
              <div class="split"><section class="panel"><h3>Playback Log</h3><div id="dashboard-play-history" class="event-list"></div></section><section class="panel"><h3>Playback Statistics</h3><div id="dashboard-play-stats" class="event-list"></div><div id="dashboard-play-stats-detail" class="stats-detail"></div></section></div>
            </section>
            <section class="view" id="view-features">
              <div class="page-head"><div><span class="section-kicker">Transparenz</span><h2>Funktionen &amp; Aktivierung</h2><p class="muted-copy">Ein lesender Überblick aus Laufzeitkonfiguration, lokaler Datenbank und dokumentiertem Hardwarestatus.</p></div><button class="command" id="reload-features" type="button">Status aktualisieren</button></div>
              <div class="feature-status-toolbar" role="toolbar" aria-label="Funktionsfilter">
                <button class="command is-selected" data-feature-filter="all" type="button">Alle</button>
                <button class="command" data-feature-filter="active" type="button">Aktiv</button>
                <button class="command" data-feature-filter="action_required" type="button">Aktion erforderlich</button>
                <button class="command" data-feature-filter="disabled" type="button">Deaktiviert</button>
                <button class="command" data-feature-filter="experimental" type="button">Experimentell</button>
                <button class="command" data-feature-filter="hardware_open" type="button">Hardwaretest offen</button>
              </div>
              <div id="feature-status-summary" class="feature-status-summary"><div class="empty">Funktionsstatus wird geladen.</div></div>
              <div id="feature-status-groups" class="feature-status-groups"></div>
            </section>
            <section class="view" id="view-setup">
              <div class="page-head setup-page-head"><div><span class="section-kicker">Geführte Einrichtung</span><h2>Setup</h2></div><button class="command" id="setup-refresh">Radios aktualisieren</button></div>
              <section class="panel setup-rebuild-assistant" id="setup-rebuild-assistant">
                <div class="panel-title-row"><div><span class="section-kicker">Setup 2.0</span><h3>Bereits mit dem Heimnetz verbundene Radios einrichten</h3><p class="muted-copy">Verbinde jedes zurückgesetzte Radio zuerst selbst mit deinem Heim-WLAN. BASSWIESN verändert weder das WLAN dieses Computers noch WLAN-Zugangsdaten eines Radios.</p></div><span class="status-pill status-ready">HTTP + CLI 17000</span></div>
                <div class="setup-rebuild-safety"><strong>Geräteschutz ist serverseitig aktiv</strong><span>Vollständig geschützte Radios werden bereits vor der Auswahl entfernt und vor jedem Netzwerktransport erneut blockiert.</span></div>
                <div class="setup-rebuild-discovery"><div><strong>1. Radios manuell mit dem Heimnetz verbinden</strong><span>Nutze dafür den vorgesehenen Bose-/Geräteablauf. Sobald alle Radios im selben LAN wie BASSWIESN sind, starte hier einmal die Suche.</span></div><button class="command primary" id="setup-rebuild-discover" type="button">Jetzt verbundene Radios suchen</button></div>
                <div id="setup-rebuild-discovery-status" class="setup-rebuild-discovery-status" aria-live="polite"><div class="empty">Noch keine ausdrückliche LAN-Suche in dieser Sitzung ausgeführt.</div></div>
                <div class="setup-rebuild-config"><label>Erreichbare BASSWIESN-Adresse<select id="setup-rebuild-host" required><option value="">LAN-Adressen werden ermittelt …</option></select><small id="setup-rebuild-host-help">Loopback-, Link-Local- und Container-Adressen werden nicht angeboten. Ports: Web {settings.web_port} · Cloud {settings.cloud_port} · Debug {settings.debug_port}</small></label><div class="setup-rebuild-policy"><strong>Sicherer Ablauf</strong><span>Sequenziell · Backup vor Write · optionale Audio-Prüfung ausschließlich bei Lautstärke 1 · SSH im normalen Setup nicht erforderlich</span><label class="toggle-line"><input id="setup-rebuild-playback" type="checkbox">Optionale Wiedergabeprüfung ausführen</label></div></div>
                <div class="setup-rebuild-steps" aria-label="Setup-Schritte"><span data-setup-rebuild-step="identity">1 Identität</span><span data-setup-rebuild-step="backup">2 Backup</span><span data-setup-rebuild-step="route">3 Serverziel</span><span data-setup-rebuild-step="verify">4 Readback</span><span data-setup-rebuild-step="done">5 Abschluss</span></div>
                <div id="setup-rebuild-devices" class="setup-rebuild-device-list" aria-live="polite"></div>
                <div class="button-row setup-rebuild-actions"><button class="command" id="setup-rebuild-refresh" type="button">Gespeicherte Liste neu laden</button><button class="command" id="setup-rebuild-preview" type="button">Änderungen prüfen</button><button class="command primary" id="setup-rebuild-start" type="button">Setup starten</button><button class="command" id="setup-rebuild-cancel" type="button" disabled>Abbrechen</button><button class="command danger" id="setup-rebuild-rollback" type="button" disabled>Gesicherte Serverziele wiederherstellen</button></div>
                <div id="setup-rebuild-status" class="event-list"><div class="empty">Noch kein Setup-Rebuild gestartet.</div></div>
                <details id="setup-rebuild-details"><summary>Vorschau und Fehlerdetails</summary><pre id="setup-rebuild-output">Wähle zuerst ein Radio und eine Serveradresse.</pre></details>
              </section>
              <section class="panel setup-oneclick legacy-setup-retired" id="setup-oneclick">
                <div class="guided-box setup-prep-copy"><h3>Stillgelegter alter Setupfluss</h3><p>Dieser Ablauf ist nur noch zur lokalen Datenmigration im Quellstand vorhanden. Verwende den Setup-Assistenten oben.</p></div>
                <label class="setup-host-field">BASSWIESN Host IP<input id="setup-batch-host" value="{settings.lan_host}" placeholder="LAN-IP des BASSWIESN Hosts"><small>Diese IP wird in die Radios geschrieben. Cloud-Ziel: <code id="setup-batch-cloud-target">{f'http://{settings.lan_host}:{settings.cloud_port}' if settings.lan_host else 'wird erkannt'}</code></small></label>
                <div class="table-scroll"><table><thead><tr><th></th><th>Name</th><th>Device ID</th><th>IP</th><th>Model</th><th>SSH</th><th>17000</th><th>Ready</th><th>Config</th></tr></thead><tbody id="setup-batch-devices"></tbody></table></div>
                <div class="button-row"><button class="command primary" id="setup-batch-start" type="button">SETUP AUSGEWÄHLTE RADIOS STARTEN</button><button class="command" id="setup-batch-cancel" type="button">Setup abbrechen</button></div>
                <div id="setup-batch-status" class="event-list"></div>
              </section>
              <div class="setup-risk-overlay legacy-setup-retired" id="setup-risk-box"><div class="setup-risk-card"><strong>Stillgelegter alter Setupfluss</strong><p>Dieser Dialog gehört zum nicht mehr verwendeten Setup-Wizard. Verwende den Setup-Assistenten oben.</p><label class="toggle-line"><input id="setup-risk-ack" type="checkbox" disabled>Alter Ablauf deaktiviert</label></div></div>
              <div class="setup-wizard-shell setup-locked lab-only legacy-setup-retired" id="setup-layout-main">
                <div class="setup-progress" id="setup-progress" aria-label="Setup Fortschritt"></div>
                <section class="setup-stage setup-linear-stage">
                  <article class="setup-flow-step is-current" data-setup-step="0">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 1</div><h3>Server erkennen</h3><p>basswiesn muss mit einer LAN-IP erreichbar sein. 127.0.0.1 ist nur fuer diesen Browser gueltig, nicht fuer das Radio.</p></div>
                    <div class="setup-card"><label>basswiesn LAN host<input id="setup-wizard-host" name="host" list="setup-wizard-host-candidates" placeholder="LAN-IP des BASSWIESN Hosts"><datalist id="setup-wizard-host-candidates"></datalist></label><div class="hint-row"><strong>Ziel</strong><span>Cloud :{settings.cloud_port}, WebGUI :{settings.web_port}, Debug :{settings.debug_port}</span></div><button class="command primary" id="setup-wizard-detect" data-setup-flow-action="detect" type="button">Server erkennen</button></div>
                  </article>
                  <article class="setup-flow-step" data-setup-step="1">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 2</div><h3>Radio auswaehlen</h3><p>Waehle ein vorhandenes Radio oder lege es lokal an. Dieser Schritt schreibt nichts ins Geraet.</p></div>
                    <div class="setup-guided-grid"><section class="setup-card"><h4>Vorhandenes Radio</h4><label>Radio<select id="setup-wizard-device" name="device_id" required></select></label><button class="command primary" data-setup-flow-action="radio" type="button">Radio verwenden</button></section><section class="setup-card"><h4>Radio hinzufuegen</h4><form class="setup-form" id="setup-device-form"><label>Radio IP<input name="ip_address" placeholder="Radio-LAN-IP" required></label><label>Name<input name="name" placeholder="Wohnzimmer"></label><input name="model" type="hidden" value="SoundTouch"><button class="command primary" type="submit">Lokal speichern</button></form><pre id="setup-device-output">Speichert nur den lokalen Datensatz.</pre></section></div>
                    <section class="setup-card"><h4>Optional scannen</h4><form class="scan-form" id="network-scan-form"><label>Scan CIDR<input name="cidr" value="" placeholder="automatisch aus LAN-IP"></label><label>Timeout seconds<input name="timeout" type="number" step="0.1" min="0.2" max="5" value="0.7"></label><button class="command" type="submit">Scan for radios</button></form><div id="scan-results" class="event-list"></div></section>
                  </article>
                  <article class="setup-flow-step" data-setup-step="2">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 3</div><h3>Preflight</h3><p>Prueft Host, lokale Cloud, Radio HTTP 8090 und CLI 17000. Backup-Capture bleibt hier aus, weil Backup im naechsten Schritt optional gefuehrt wird.</p></div>
                    <form id="setup-wizard-form" class="settings-form wizard-form"><input name="device_id" type="hidden"><label>basswiesn LAN host<input name="host" list="setup-wizard-host-candidates" placeholder="LAN-IP des BASSWIESN Hosts" required></label><label class="toggle-line"><input name="reboot" type="checkbox" checked>Reboot nach Write vorbereiten</label><label class="toggle-line"><input name="force" type="checkbox">Force nur nach manueller Pruefung</label><button class="command primary" data-wizard-action="preflight" data-setup-flow-action="preflight" type="submit">Preflight ausfuehren</button></form><div id="setup-wizard-checks" class="wizard-check-grid"></div>
                  </article>
                  <article class="setup-flow-step" data-setup-step="3">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 4</div><h3>Backup optional</h3><p>OCT fuehrt Backup als eigenen Schritt. In basswiesn ist es optional, aber empfohlen, bevor echte Writes ausgefuehrt werden.</p></div>
                    <div class="setup-card"><div class="button-row"><button class="command primary" id="setup-backup-plan" data-setup-flow-action="backup" type="button">Backup-Plan erstellen</button><button class="command" data-setup-flow-action="skip-backup" type="button">Backup ueberspringen</button></div><small>Der aktuelle Button erstellt einen lokalen Plan und schreibt nichts ins Radio.</small></div>
                  </article>
                  <article class="setup-flow-step" data-setup-step="4">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 5</div><h3>Cloud-Route Dry-Run</h3><p>Zeigt den Diff zwischen aktuellen Radio-Werten und basswiesn-Zielwerten. Noch kein Write.</p></div>
                    <form id="cloud-route-form" class="settings-form"><label>Radio<select id="cloud-route-device" name="device_id" required></select></label><label>basswiesn LAN host<input id="cloud-route-host" name="host" placeholder="LAN-IP des BASSWIESN Hosts"></label><input name="reboot" type="hidden" value="true"><button class="command primary" data-route-action="preview" data-setup-flow-action="route-preview" type="submit">Route-Diff anzeigen</button></form><div class="route-plan"><div><span>Bestätigter Weg</span><strong>Persistente Cloud-Route</strong><small>Der Zwei-URL-Befehl schreibt den SystemConfiguration-PB-Store und startet das Radio neu.</small></div><div><span>Sicherheit</span><strong>Vorschau zuerst</strong><small>Ausführen kommt erst im nächsten Schritt mit Bestätigung.</small></div></div>
                  </article>
                  <article class="setup-flow-step" data-setup-step="5">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 6</div><h3>Apply bewusst ausfuehren</h3><p>Dieser Schritt kann echte CLI-17000-Writes ausloesen. Zum Fortfahren <b>yes</b> eingeben.</p></div>
                    <div class="setup-card"><label class="toggle-line"><input id="setup-apply-dry-run" type="checkbox" checked>Apply als Dry-run testen (kein Radio-Write)</label><label>Confirmation<input id="cloud-route-confirmation" name="confirmation" placeholder="yes" autocomplete="off"></label><div class="button-row"><button class="command danger" data-setup-flow-action="apply" type="button">Apply testen / ausführen</button><button class="command danger" data-setup-flow-action="rollback" type="button">Rollback route</button></div></div>
                  </article>
                  <article class="setup-flow-step" data-setup-step="6">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 7</div><h3>Verify und Testplan</h3><p>Nach Apply oder Reboot wird zuerst verifiziert. Ein optionaler Sender startet nur bewusst über den Testknopf.</p></div>
                    <div class="setup-warning-box"><strong>Stillgelegter Ablauf</strong><p>Die aktive Einrichtung und ihre Fehlerhilfe befinden sich im Setup-Assistenten oben.</p></div>
                    <div class="verify-grid"><button class="command" data-open="{settings.local_base_url}/bmx/registry/v1/services" type="button">BMX registry</button><button class="command" data-open="{settings.debug_base_url}/requests" type="button">Request log</button><button class="command" data-open="/api/devices" type="button">Devices API</button></div><form id="setup-live-test-form" class="settings-form"><label>Radio<select id="setup-live-test-device" name="device_id" required></select></label><label>basswiesn LAN host<input name="host" placeholder="LAN-IP des BASSWIESN Hosts"></label><label>Optional test station<select id="setup-live-test-station" name="station_id"></select></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><div class="button-row"><button class="command primary" data-setup-flow-action="verify" type="button">Verify route</button><button class="command" type="submit">Run guided test plan</button></div></form>
                  </article>
                  <article class="setup-flow-step" data-setup-step="7">
                    <div class="wizard-step-header"><div class="wizard-step-number">Schritt 8</div><h3>Abschluss</h3><p>Speichere den Setup-Plan lokal und pruefe danach erst mit echtem Geraet, wenn Backup, Verify und Rollback geklaert sind.</p></div>
                    <div class="setup-card"><label>Setup-Plan Name<input id="guided-setup-name" name="setup_name" placeholder="Wohnzimmer Setup"></label><div class="button-row"><button class="command primary" id="setup-finish" type="button">Abschliessen</button><button class="command" id="save-guided-setup" type="button">Setup-Plan speichern</button><button class="command" id="load-guided-setup" type="button">Setup-Plan laden</button></div><label class="setup-hidden-select">Plan radio<select id="guided-setup-device" name="device_id"></select></label><div class="verify-grid"><button class="command" data-open="/api/health" type="button">Web health</button><button class="command" data-open="{settings.local_base_url}/bmx/registry/v1/services" type="button">Cloud registry</button><button class="command" data-open="{settings.debug_base_url}/health" type="button">Debug health</button></div><div id="guided-setup-steps" class="event-list"></div></div>
                  </article>
                  <div class="setup-wizard-actions"><button class="command" data-setup-flow-prev type="button">Zurueck</button><span id="setup-flow-hint">Fuehre den aktuellen Schritt aus, dann geht es weiter.</span><button class="command primary" data-setup-flow-next type="button" disabled>Weiter</button></div>
                  <div class="setup-console">
                    <div id="setup-finish-status" role="status" aria-live="polite"></div>
                    <details><summary>Aktueller Schritt</summary><pre id="setup-wizard-output">Starte mit Server erkennen.</pre></details>
                    <details><summary>Cloud-Route</summary><pre id="cloud-route-output" class="setup-secondary-output">Route-Diff erscheint hier.</pre></details>
                    <details><summary>Setup-Plan und Test</summary><pre id="setup-output" class="setup-secondary-output">Setup-Plan, Backup und Live-Test erscheinen hier.</pre></details>
                  </div>
                </section>
              </div>
            </section>
            <section class="view" id="view-devices">
              <div class="page-head"><h2>Radios</h2><div class="button-row"><button class="command primary" id="scan-radios-now" type="button">Radios suchen</button><button class="command" id="reload-devices">Aktualisieren</button></div></div>
              <form class="toolbar-form" id="device-form"><input name="name" placeholder="Name or room"><input name="ip_address" placeholder="IP address" required><input name="model" type="hidden" value="SoundTouch"><button class="command primary" type="submit">Add radio</button></form><pre id="device-form-output">Add saves a radio locally, then /info can fill model, firmware and device id.</pre>
              <div class="device-layout"><section class="panel"><h3>Configured radios</h3><div id="devices-cards" class="device-card-grid"></div><div class="table-scroll"><table><thead><tr><th>Name</th><th>IP</th><th>Model</th><th>Firmware</th><th>Config</th><th>Ready</th><th>Identifier</th><th>Aktion</th></tr></thead><tbody id="devices-table"></tbody></table></div></section></div>
              <details class="panel lab-only danger-panel"><summary>Manueller LAB-Radio-Reboot</summary><p class="muted-copy">Recovery-Stufe 7 ist ausschliesslich eine bewusst gestartete LAB-Aktion. BASSWIESN plant niemals automatische Radio-Reboots.</p><div class="settings-form"><label>Radio<select id="maintenance-reboot-device" name="device_id" required></select></label><div class="button-row"><button class="command danger" id="maintenance-reboot-now" type="button">Radio manuell neu starten</button></div></div><pre id="maintenance-reboot-output">Keine Reboot-Aktion gestartet.</pre></details>
              <div class="split">
                <section class="panel"><h3>Radio Info ohne SSH</h3><form id="device-info-form" class="settings-form"><label>Radio<select id="device-info-select" name="device_id" required></select></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><div class="button-row"><button class="command" id="probe-device-info" type="button">Probe planen</button><button class="command" id="load-host-config" type="button">Config-Dateien lesen <span class="requires-ssh">SSH</span></button></div></form><div id="device-info-cleartext" class="event-list"></div><details><summary>Debug XML / raw plan</summary><pre id="device-info-output">Hier erscheinen /info, /capabilities, erkannter Cloud-Zielhost und der SSH-Leseplan.</pre></details></section>
                <section class="panel"><h3>Einrichtungsstatus</h3><div class="status-legend"><span class="status-pill config-basswiesn">basswiesn</span><span class="status-pill config-bose">Bose Cloud</span><span class="status-pill config-other">anderer Dienst</span><span class="status-pill config-unknown">unbekannt</span></div><div id="device-readiness" class="event-list"></div></section>
              </div>
              <section class="panel"><h3>Live-Capture Vergleich</h3><div id="device-live-comparison" class="event-list"></div><details><summary>Endpoint-Matrix</summary><pre id="device-live-comparison-output">Noch kein Vergleich geladen.</pre></details></section>
            </section>
            <section class="view" id="view-health">
              <div class="page-head"><div><span class="section-kicker">Getrennte Laufzeitverträge</span><h2>Status &amp; Diagnose</h2><p class="muted-copy">Persistierte Beobachtungen anzeigen, ohne beim Öffnen das Radio zu kontaktieren.</p></div><button class="command" id="research-health-refresh" type="button">Status aktualisieren</button></div>
              <section class="panel research-health-picker"><label>Radio<select id="health-device-select" required><option value="">Noch kein Radio vorhanden</option></select></label><div class="health-readback-note"><strong>Playback kommt vom Radio-Readback</strong><span>Provider, Stream, Metadaten und Reporting bleiben davon unabhängige Signale.</span></div></section>
              <div class="research-health-overview" id="research-health-overview" aria-live="polite"><div class="empty">Radio wählen, um die gespeicherten Zustände zu laden.</div></div>
              <div class="split research-health-primary">
                <section class="panel"><div class="panel-title-row"><h3>Playback Health</h3><span id="playback-health-badge" class="status-pill">UNKNOWN</span></div><div id="playback-health-details" class="event-list"></div></section>
                <section class="panel"><div class="panel-title-row"><h3>Provider Health</h3><span id="provider-health-badge" class="status-pill">UNKNOWN</span></div><div id="provider-health-details" class="event-list"></div></section>
              </div>
              <section class="panel metadata-health-panel"><div class="metadata-artwork-frame"><img id="metadata-artwork" src="/static/bmx-icons/orion/monochrome.svg" alt="Artwork oder Quellen-Symbol"></div><div class="metadata-health-copy"><div class="panel-title-row"><h3>Live-Metadaten</h3><span id="metadata-health-badge" class="status-pill">UNKNOWN</span></div><div id="metadata-health-details" class="event-list"></div><details><summary>Live-Metadaten aktualisieren</summary><p class="muted-copy">Ändert nur Track, Interpret, Album und imageUrl des laufenden BASSWIESN-Senders. Kein Sourcewechsel, SetURL, Stop oder Rebuffer.</p><form id="live-metadata-form" class="settings-form"><label>Titel<input name="track" maxlength="512"></label><label>Interpret<input name="artist" maxlength="512"></label><label>Album<input name="album" maxlength="512"></label><label>imageUrl<input name="imageUrl" type="url" maxlength="2048" placeholder="https://…"></label><button class="command primary" type="submit">Live-Metadaten übernehmen</button></form><p class="form-message" id="live-metadata-status">Zuerst einen BASSWIESN-Sender starten.</p></details></div></section>
              <div class="split research-contract-grid">
                <section class="panel"><div class="panel-title-row"><h3>Restrictions</h3><span id="restrictions-health-badge" class="status-pill">ABSENT</span></div><p class="muted-copy">Kein lokaler 21600-Sekunden-Default. Fehlend oder 0 bedeutet deaktiviert.</p><div id="restrictions-health-details" class="event-list"></div></section>
                <section class="panel"><div class="panel-title-row"><h3>Reporting</h3><span id="reporting-health-badge" class="status-pill">UNKNOWN</span></div><p class="muted-copy">Separater POST-Vertrag; ein Fehler stoppt weder Wiedergabe noch Quelle.</p><div id="reporting-health-details" class="event-list"></div></section>
              </div>
              <section class="panel airplay-readiness-panel"><div class="panel-title-row"><div><h3>AirPlay 2 Readiness</h3><p class="muted-copy">Nur Diagnose – keine Firmware-, MFi- oder Authentifizierungsumgehung.</p></div><span id="airplay-health-badge" class="status-pill">Unbekannt</span></div><div id="airplay-health-summary" class="event-list"></div><div class="inline-actions"><button class="command" id="airplay-readonly-probe" type="button" disabled>Ausgewähltes Radio read-only prüfen</button><span class="muted-copy">Liest Info, Sources, Capabilities und Unicast-mDNS nur am ausgewählten Radio. Kein Subnetz-/Multicast-Scan, SSH oder CLI.</span></div><details class="lab-only"><summary>LAB: Gate-Details</summary><div id="airplay-health-gates" class="airplay-gate-grid"></div></details></section>
              <section class="panel lab-only clock-metadata-lab"><div class="panel-title-row"><div><h3>Uhr als Live-Metadaten</h3><p class="muted-copy">Experimentell, pro Radio und standardmäßig aus. Keine Display-Binary-Patches.</p></div><span class="status-pill status-warning">LAB</span></div><form id="clock-metadata-form" class="settings-form"><label class="toggle-line"><input id="clock-metadata-enabled" type="checkbox">Uhrzeit in Live-Metadaten anzeigen</label><label>Darstellung<select id="clock-metadata-mode"><option value="MISSING_TITLE">Nur bei fehlendem Titel</option><option value="APPEND">An Titel anhängen</option></select></label><label>Update-Intervall in Sekunden<input id="clock-metadata-interval" type="number" min="60" step="60" value="60"></label><button class="command primary" type="submit">LAB-Einstellung speichern</button></form><p class="form-message" id="clock-metadata-status">Hardwarevalidierung offen.</p></section>
              <section class="panel diagnostics-timeline-panel"><div class="panel-title-row"><div><h3>Diagnose-Zeitlinie</h3><p class="muted-copy">Chronologisch korrelierte Zustandswechsel pro Radio.</p></div><span id="timeline-count" class="status-pill">0 Ereignisse</span></div><div id="diagnostics-timeline" class="diagnostics-timeline event-list"></div></section>
            </section>
            <section class="view" id="view-device-settings">
              <div class="page-head"><h2>Device Settings</h2><button class="command" id="reload-device-settings">Reload</button></div>
              <div class="settings-grid">
                <section class="panel">
                  <h3>Target radio</h3>
                  <form id="device-settings-form" class="settings-form">
                    <label>Radio<select id="settings-device-select" name="device_id" required></select></label>
                    <label>Radio-Name<input name="name" maxlength="63" placeholder="Wohnzimmer"></label>
                    <label>Volume<input name="volume" type="range" min="0" max="100" value="5"><span id="volume-value">5</span></label>
                    <label>Bass<input name="bass" type="range" min="-9" max="0" value="0"><span id="bass-value">0</span></label>
                    <label>Standby-Uhr<select name="clockDisplay"><option value="true">Ein</option><option value="false">Aus</option></select></label>
                    <label>Sprache<select name="language" id="device-language-select"></select></label>
                    <label>Zeitzone<select name="timezoneInfo" id="device-timezone-select"></select></label>
                    <label>Zeitformat<select name="timeFormat"><option value="TIME_FORMAT_24HOUR_ID">24 Stunden</option><option value="TIME_FORMAT_12HOUR_ID">12 Stunden</option></select></label>
                    <label>DST / offset minutes<input name="userOffsetMinute" type="number" min="-720" max="840" value="0"></label>
                    <label>Uhrhelligkeit<input name="brightnessLevel" type="number" min="0" max="100" value="70"></label>
                    <label>Energiesparen<select name="powersaving"><option value="true">Ein</option><option value="false">Aus</option></select></label>
                    <label>Multiroom-Synchronisation<select name="rebroadcastlatencymode"><option value="SYNC_TO_ZONE">Zur Zone synchronisieren</option><option value="SYNC_TO_ROOM">Auf den Raum ausrichten</option></select></label>
                    <label>Anzeige am Radio<select name="station_art_mode"><option value="radio_symbol">Bose-Standardsymbol über Sendernamen anzeigen</option><option value="station_logo">Senderlogo über Sendernamen anzeigen</option></select><small>Die Auswahl wird lokal gespeichert. Bestehende Presets brauchen danach eine Synchronisierung mit Readback.</small></label>
                    <div class="settings-followup"><label class="toggle-line"><input id="device-settings-memory-check" type="checkbox">Ich habe den Backup-/Memory-Hinweis gelesen</label><button class="command" id="sync-device-presets" type="button">Vorschau für Preset-Sync</button><button class="command primary" id="device-settings-preset-sync-confirm" type="button" hidden>Vorschau bestätigen und synchronisieren</button><div id="device-settings-preset-sync-preview" class="sync-preview" hidden></div><p class="form-message" id="device-settings-preset-sync-status">Noch keine Synchronisierung gestartet.</p></div>
                    <button class="command primary pending-apply" id="device-settings-apply" type="submit">Änderungen anwenden</button>
                    <p class="form-message" id="device-settings-dirty">Live-Werte werden beim Auswählen geladen. Geschrieben werden nur Änderungen.</p>
                  </form>
                </section>
                <section class="panel">
                  <h3>Live-Informationen</h3>
                  <div id="device-settings-live-info" class="event-list"><div class="empty">Radio wählen; danach erscheinen aktuelle Werte.</div></div>
                  <details><summary>Technischer Nachweis / Antwort</summary><pre id="device-settings-output">Radio wählen; danach erscheinen gelesene Werte und nur die tatsächlich geschriebenen Änderungen.</pre></details>
                </section>
                <section class="panel lab-only"><h3>Bass capabilities</h3><form id="bass-capabilities-form" class="settings-form"><label>Radio<select id="bass-capabilities-device" name="device_id" required></select></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><button class="command" type="submit">Probe bass capabilities</button></form><pre id="bass-capabilities-output">Reads /bassCapabilities and shows the model range before changing bass.</pre></section>
                <section class="panel lab-only feature-limited"><h3>Source label plan <span class="status-pill status-warning">eingeschränkt / lab</span></h3><form id="source-name-form" class="settings-form"><label>Radio<select id="source-name-device" name="device_id" required></select></label><label>Source<input name="source" value="AUX" placeholder="AUX, BLUETOOTH, LOCAL_INTERNET_RADIO"></label><label>Source account<input name="sourceAccount" placeholder="optional"></label><label>New source label<input name="name" placeholder="Plattenspieler"></label><button class="command" type="submit">Create /nameSource plan</button></form><pre id="source-name-output">/nameSource is plan-only until a real capture confirms model-safe behavior.</pre></section>
                <section class="panel lab-only feature-limited"><h3>Zusätzliches WLAN / Handy-Hotspot <span class="status-pill status-warning">eingeschränkt / lab</span></h3><p class="muted-copy">SoundTouch kann mehrere persistente WLAN-Profile speichern. Beim Hinzufügen kann das Radio sofort das Netz wechseln.</p><form id="wireless-profile-form" class="settings-form"><label>Radio<select id="wireless-profile-device" name="device_id" required></select></label><label>SSID<input name="ssid" maxlength="32" placeholder="Mein Handy-Hotspot" required></label><label>Sicherheit<select name="security_type"><option value="wpa_or_wpa2">WPA/WPA2</option><option value="none">Offen</option><option value="wep">WEP (alt)</option></select></label><label>Passwort<input name="password" type="password" maxlength="63" autocomplete="new-password"></label><label class="toggle-line"><input name="understood" type="checkbox" required>Ich verstehe, dass das Radio danach vorübergehend aus diesem Netz verschwinden kann.</label><div class="button-row"><button class="command" id="wireless-profile-read" type="button">Vorhandenen Status lesen</button><button class="command primary" type="submit">WLAN-Profil hinzufügen</button></div></form><pre id="wireless-profile-output">BASSWIESN zeigt aktive SSID und Anzahl gespeicherter Profile. Passwörter werden nicht lokal gespeichert.</pre></section>
              </div>
            </section>
            <section class="view" id="view-controls">
              <div class="page-head"><h2>Fernbedienung</h2><button class="command" id="reload-controls">Aktualisieren</button></div>
              <div class="split">
                <section class="panel remote-control-panel"><h3>Radio auswählen</h3><form id="key-command-form" class="settings-form"><label>Radio<select id="key-device-select" name="device_id" required></select></label><label>Sichere Startlautstärke<input id="key-safe-volume" name="safe_volume" type="number" min="0" max="100" value="5"><small>Wird nach Power und Preset nochmals gesetzt, weil das Radio sonst seine alte Einschaltlautstärke laden kann.</small></label></form><div id="key-command-grid" class="remote-command-grid"></div></section>
                <section class="panel"><h3>Status</h3><div id="key-command-status" class="friendly-status">Wähle ein Radio und eine Taste.</div><details><summary>Technische Antwort</summary><pre id="key-command-output">Noch kein Befehl gesendet.</pre></details></section>
              </div>
            </section>
            <section class="view" id="view-display">
              <div class="page-head"><h2>Display</h2><button class="command" id="reload-display">Reload</button></div>
              <div class="split">
                <section class="panel"><h3>Display-Vorschau</h3><form id="display-settings-form" class="settings-form"><label>Radio<select id="display-device-select" name="device_id" required></select></label><label>Anzeige<select id="display-mode-select" name="mode"></select></label><label>Sender normal starten<select id="display-direct-station" name="station_id"><option value="">Nur aktuellen Zustand lesen</option></select></label><label class="toggle-line"><input name="include_date" type="checkbox" checked>Datum bei der Uhr berücksichtigen</label><label class="toggle-line"><input name="probe" type="checkbox" checked>Radiowerte zuerst lesen</label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Nur Vorschau</label><div class="button-row"><button class="command" data-display-action="save" type="submit">Anzeigeplan speichern</button><button class="command" data-display-action="preview" type="submit">Vorschau aktualisieren</button><button class="command primary" data-display-action="direct_select" type="submit">Sender starten</button></div></form><p class="muted-copy">Batterie-Metadaten werden im regulären Lauf nicht aktiv abgefragt. Anzeigepläne nutzen Sender, Uhr und WLAN nur über explizite Read-back-Pfade.</p><div id="display-mode-list" class="event-list"></div></section>
                <section class="panel lab-only feature-limited"><h3>Display recovery <span class="status-pill status-warning">eingeschränkt / lab</span></h3><form id="display-recovery-form" class="settings-form"><label>Radio<select id="display-recovery-device-select" name="device_id" required></select></label><label>Mode<select name="mode"><option value="pixel_wash">Pixel wash</option><option value="inverse_scroll">Inverse scroll</option><option value="black_white_cycle">Black/white cycle</option></select></label><label>Minutes<input name="minutes" type="number" min="1" max="60" value="10"></label><label class="toggle-line"><input name="cleanup_required" type="checkbox" checked>Cleanup temporary files after stop</label><button class="command" type="submit">Create recovery plan</button></form></section>
              </div>
              <div class="panel"><h3>Display result</h3><pre id="display-output">No display action planned.</pre></div>
            </section>
            <section class="view" id="view-stations">
              <div class="page-head"><h2>Stations</h2><button class="command" id="reload-stations">Reload</button></div>
              <div class="station-tools">
                <form class="toolbar-form" id="station-form"><input name="name" placeholder="Station Name" required><input name="stream_url" placeholder="Stream URL" required><input name="image_url" placeholder="Bild URL"><button class="command" type="submit">Play Stream</button></form>
                <form class="toolbar-form" id="station-play-form"><select id="station-play-device" name="device_id" required></select><select id="station-play-select" name="station_id" required></select><label>Sichere Testlautstärke<input id="station-play-safe-volume" name="safe_volume" type="number" min="0" max="5" value="1"></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Nur Vorschau</label><button class="command primary" type="submit">Am Radio abspielen</button></form>
                <form class="toolbar-form upload-form" id="station-upload-form"><input name="name" placeholder="Station name" required><input name="file" type="file" required><button class="command" type="submit">Upload file</button></form>
                <input class="search-input" id="station-search" placeholder="Search stations">
              </div>
              <div class="panel"><table><thead><tr><th>Name</th><th>Stream</th><th>Image</th><th>Action</th></tr></thead><tbody id="stations-table"></tbody></table><pre id="station-play-output">Choose a radio and station to test playback.</pre></div>
              <div class="split lab-only"><section class="panel feature-limited"><h3>Native radio station search</h3><p class="muted-copy">Manual only</p><form id="native-station-search-form" class="settings-form"><label>Radio<select id="native-station-device" name="device_id" required></select></label><label>Source<input name="source" value="TUNEIN"></label><label>Source account<input name="sourceAccount" placeholder="optional account"></label><label>Search<input name="query" placeholder="Jazz" required></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><button class="command" type="submit">Search via radio</button></form></section><section class="panel feature-limited"><h3>Native add station</h3><p class="muted-copy">Manual only</p><form id="native-station-add-form" class="settings-form"><label>Radio<select id="native-station-add-device" name="device_id" required></select></label><label>Source<input name="source" value="TUNEIN"></label><label>Source account<input name="sourceAccount" placeholder="optional account"></label><label>Token<input name="token" placeholder="station token" required></label><label>Name<input name="name" placeholder="Station name" required></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><button class="command" type="submit">Add native station</button></form></section></div><div class="panel lab-only feature-limited"><h3>Native station result</h3><p class="muted-copy">Manual only</p><pre id="native-station-output">Verwendet /searchStation und /addStation. Der ausgewählte Dienst muss auf dem Radio als READY registriert sein.</pre></div>
            </section>

            <section class="view" id="view-media">
              <div class="page-head"><h2>Media/NAS</h2><button class="command" id="reload-media">Reload</button></div>
              <div class="split">
                <section class="panel"><h3>DLNA / NAS Probe</h3><form id="media-server-form" class="settings-form"><label>Radio<select id="media-device-select" name="device_id" required></select></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><button class="command primary" type="submit">List media servers</button></form><pre id="media-server-output">Liest /listMediaServers. Diese Abfrage verändert das Radio nicht.</pre></section>
                <section class="panel"><h3>Media capabilities</h3><pre id="media-capabilities-output">Loading...</pre></section>
              </div>
              <div class="split">
                <section class="panel"><h3>NAS playlist / collection</h3><form id="media-playlist-form" class="settings-form"><label>Name<input name="name" placeholder="80er Sammlung" required></label><label>Source type<select name="source_type"><option value="DLNA">DLNA/NAS</option><option value="LOCAL_FOLDER">Local folder served by basswiesn</option><option value="HTTP_PLAYLIST">HTTP playlist URL</option></select></label><label>URI<input name="uri" placeholder="dlna://server/container or http://nas/playlist.m3u"></label><label>Browse local file/folder<input id="media-local-browser" type="file" webkitdirectory multiple></label><label>Notes<input name="notes" placeholder="Kinderzimmer Hörspiele, 80er Musik, Gutenachtgeschichten"></label><button class="command" type="submit">Save collection</button></form></section>
                <section class="panel"><div class="panel-title-row"><h3>Saved media collections</h3><button class="command" id="clear-media-playlists" type="button">Sammlungen leeren</button></div><div id="media-playlists" class="event-list"></div></section>
              </div>
              <div class="panel"><h3>Services</h3><div id="service-catalog" class="event-list"></div></div>
            </section>
            <section class="view" id="view-presets">
              <div class="page-head"><h2>Presets</h2><button class="command" id="reload-preset-data">Reload</button></div>
              <div class="preset-builder">
                <section class="panel preset-workbench">
                  <h3>Preset Builder</h3>
                  <form id="preset-form">
                    <label>Radio<select name="device_id" id="preset-device-select" required></select></label>
                    <fieldset class="slot-picker" aria-label="Preset slot">
                      <legend>Slot</legend>
                      <label><input type="radio" name="button" value="1" checked><span>Preset 1</span></label>
                      <label><input type="radio" name="button" value="2"><span>Preset 2</span></label>
                      <label><input type="radio" name="button" value="3"><span>Preset 3</span></label>
                      <label><input type="radio" name="button" value="4"><span>Preset 4</span></label>
                      <label><input type="radio" name="button" value="5"><span>Preset 5</span></label>
                      <label><input type="radio" name="button" value="6"><span>Preset 6</span></label>
                    </fieldset>
                    <label>Station<select name="station_id" id="preset-station-select" required></select></label>
                    <details class="quick-station"><summary>Create stream station here</summary><div class="quick-grid"><input id="quick-station-name" placeholder="Station Name"><input id="quick-station-url" placeholder="Stream URL"><input id="quick-station-image" placeholder="Bild URL"><button class="command" id="quick-station-add" type="button">Create and select</button></div><p class="form-message" id="quick-station-message" role="status"></p></details>
                    <button class="command primary" type="submit">Preset auf Radio speichern</button>
                  </form>
                </section>
                <section class="panel preset-slots">
                  <h3>Current Slots</h3>
                  <div class="button-row"><button class="command" id="download-radio-presets" type="button">Vom Radio aktualisieren</button></div>
                  <div class="slot-grid" id="preset-slot-grid"></div>
                </section>
              </div>
              <div class="split"><section class="panel"><h3>Online-Sendersuche</h3><p class="muted-copy">Sender finden und direkt für den Preset Builder auswählen.</p><form id="online-search-form" class="toolbar-form"><input name="q" placeholder="Sender suchen" required><button class="command" type="submit">Suchen</button></form><p class="form-message" id="online-station-message" role="status"></p><div id="online-station-results" class="station-search-results"></div></section><section class="panel"><h3>Presets kopieren</h3><form id="preset-clone-form" class="settings-form"><label>Von Radio<select id="clone-source-device" name="source_device_id" required></select></label><label>Auf Radio<select id="clone-target-device" name="target_device_id" required></select></label><button class="command" type="submit">Alle Slots kopieren</button></form><pre id="preset-clone-output">Die Presets werden kopiert und anschließend auf dem Zielradio geprüft.</pre></section></div>
              <section class="panel offline-preflight-panel"><div class="panel-title-row"><div><h3>Offline-Preflight</h3><p class="muted-copy">Nur Klassifizierung und optionale Serverprüfung. Keine Radioaktion und kein Hardwarebeweis.</p></div><button class="command" id="offline-preflight-run" type="button">Abhängigkeiten prüfen</button></div><label class="toggle-line"><input id="offline-preflight-probe" type="checkbox">Serverseitig den Stream testen (manueller Netzwerkrequest)</label><div id="offline-preflight-output" class="event-list"><div class="empty">Radio und Preset wählen, danach den Preflight starten.</div></div></section>
              <div class="split"><section class="panel"><h3>Preset-Set speichern</h3><form id="preset-profile-form" class="settings-form"><label>Name<input name="name" placeholder="Standard Küche" required></label><label>Beschreibung<input name="description" placeholder="z.B. Alltag / Nachrichten / Musik"></label><div id="preset-profile-slots" class="profile-slot-grid"></div><button class="command primary" type="submit">Set speichern</button></form></section><section class="panel"><h3>Preset-Set auf Radio übertragen</h3><form id="preset-profile-apply-form" class="settings-form"><label>Preset-Set<select id="preset-profile-select" name="profile_id" required></select></label><label>Radio<select id="profile-apply-device" name="device_id" required></select></label><button class="command primary" type="submit">Jetzt übertragen</button></form><div id="preset-profile-list" class="event-list"></div></section></div>
              <div class="panel"><h3>Last Result</h3><pre id="preset-result">No preset written in this session.</pre></div>
              <div class="panel"><h3>Supported preset media types</h3><div id="media-types-list" class="media-grid"></div></div>
            </section>
            <section class="view" id="view-multiroom">
              <div class="page-head"><div><span class="section-kicker">Gemeinsam hören</span><h2>Multiroom</h2><p>Ein Radio gibt den Takt vor, die ausgewählten Räume spielen mit.</p></div><button class="command" id="reload-multiroom">Aktualisieren</button></div>
              <div id="multiroom-methods" class="method-card-grid"></div>
              <div class="multiroom-workflow" aria-label="Multiroom Ablauf"><span>1 · Hauptradio wählen</span><b>→</b><span>2 · Räume hinzufügen</span><b>→</b><span>3 · Gruppe starten</span></div>
              <div class="multiroom-grid clean-grid">
                <section class="panel focus-panel">
                  <h3>Radios gemeinsam abspielen</h3>
                  <p class="muted-copy">Das Hauptradio liefert Musik und Synchronisation. Alle weiteren Radios folgen ihm.</p>
                  <form id="multiroom-form" class="multiroom-form">
                    <label>Hauptradio<select name="master_device_id" id="multiroom-master" required></select><small>Hier startest du später Sender, Presets und Lautstärke.</small></label>
                    <div><span class="field-label">Weitere Räume</span><div class="member-list room-picker" id="multiroom-members"></div><small>Wähle mindestens ein zusätzliches Radio.</small></div>
                    <label>Sender beim Start (optional)<select id="multiroom-station" name="station_id"><option value="">Nur Radios verbinden</option></select></label>
                    <label>Synchronisationsart<select name="latency_mode"><option value="SYNC_TO_ZONE">Zone – beste Synchronität zwischen mehreren Radios</option><option value="SYNC_TO_ROOM">Raum – geringere lokale Verzögerung</option></select></label>
                    <label class="toggle-line"><input name="preserve_volumes" type="checkbox"> Bestehende Lautstärken beibehalten</label>
                    <small>BASSWIESN sendet dabei kein SetVolume. Falls die Bose-Firmware selbst einen Wert ändert, zeigt der Readback die Abweichung an und korrigiert sie nicht heimlich.</small>
                    <label>Gruppenlautstärke<input name="volume" type="number" min="0" max="100" value="5"><small>Wird nach dem Start auf jedem Radio nochmals geprüft.</small></label>
                    <div class="button-row"><button class="command primary" type="submit">Vorschau für Multiroom</button><button class="command primary" id="multiroom-confirm" type="button" hidden>Vorschau bestätigen und starten</button><button class="command" id="multiroom-clear" type="button">Ausgewählte Gruppe auflösen</button></div>
                  </form>
                  <div id="multiroom-preview" class="sync-preview" hidden></div>
                  <div class="single-room-remove lab-only"><span class="field-label">Experimentell: einzelnes Radio herauslösen</span><p class="muted-copy">Nur im Lab. Kann die aktive Gruppe verändern; nicht für produktive Gruppen empfohlen.</p><div id="multiroom-remove-device" class="radio-button-picker"></div><button class="command" id="multiroom-remove" type="button">Experimentell aus Gruppe entfernen</button></div>
                </section>
                <section class="panel">
                  <div class="panel-title-row zone-status-toolbar"><h3>Aktueller Zustand</h3><form id="zone-status-form" class="toolbar-form"><select id="zone-status-device" name="device_id" required></select><button class="command" type="submit">Status lesen</button></form></div>
                  <div id="zone-status-output" class="friendly-result">Noch kein Radio geprüft.</div>
                  <div id="multiroom-output" class="friendly-result">Wähle ein Hauptradio und die gewünschten Räume.</div>
                </section>
                <section class="panel">
                  <h3>Synchronisation</h3><p class="muted-copy"><b>Zone</b> ist für gemeinsames SoundTouch-Audio. <b>Raum</b> optimiert ein einzelnes Radio und erstellt keine Gruppe.</p>
                  <form id="multiroom-latency-form" class="settings-form"><label>Modus<select name="mode"><option value="SYNC_TO_ZONE">Mit der Multiroom-Zone synchronisieren</option><option value="SYNC_TO_ROOM">Auf den einzelnen Raum ausrichten</option></select></label><button class="command" type="submit">Auf ausgewählte Radios anwenden</button></form>
                  <div id="multiroom-latency-output" class="friendly-result">Empfehlung für Multiroom: Zone.</div>
                </section>
              </div>
              <details class="panel advanced-section"><summary>BASSWIESN-Multiroom-Szenen</summary><p class="muted-copy">Serverseitige Szenen, keine nativen Bose-Presets. Der BASSWIESN-Server muss laufen; Aktivierung prüft Teilnehmer, Schutzstatus, Lautstärke und Readback.</p><div class="split">
                <section><h3>BASSWIESN-Multiroom-Preset speichern</h3><form id="multiroom-scenario-form" class="settings-form"><label>Name<input name="name" placeholder="Überall Radio" required></label><label>Beschreibung<input name="description" placeholder="Küche und Wohnzimmer"></label><label>Hauptradio<select id="scenario-master" name="master_device_id" required></select></label><div><span class="field-label">Weitere Räume</span><div class="member-list" id="scenario-members"></div></div><label>Sender<select id="scenario-station" name="station_id"></select></label><label class="toggle-line"><input name="preserve_volumes" type="checkbox"> Bestehende Lautstärken beibehalten</label><label>Lautstärke<input name="volume" type="number" min="0" max="100" value="5"></label><label>Nur gespeicherte Zuordnung – Radio<select id="scenario-trigger-device" name="trigger_device_id"></select></label><label>Nur gespeicherte Zuordnung – Taste<select name="trigger_button"><option value="">Keine</option><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="4">4</option><option value="5">5</option><option value="6">6</option></select><small>Keine Hardwaretaste-Automatik: Dieses BASSWIESN-Preset wird ausschließlich über die WebUI gestartet.</small></label><button class="command primary" type="submit">BASSWIESN-Preset speichern</button></form></section>
                <section><h3>Gespeicherte Szenen</h3><div id="multiroom-scenarios" class="event-list"></div><div id="multiroom-scenario-output" class="friendly-result">Eine Szene merkt sich Radios, Sender und Lautstärke und kann direkt gestartet werden.</div></section>
              </div></details>
              <details class="panel advanced-section"><summary>Technische Unterschiede: Capabilities, Zone, Group und Room</summary><div id="multiroom-technical-explanation" class="help-prose"><p><b>/capabilities</b> erkennt nur Funktionen – es gruppiert nichts.</p><p><b>/setZone</b> ist der bestätigte lokale Hauptweg und wird von BASSWIESN verwendet.</p><p><b>Group</b> ist die modernere interne Bose-Zustandsmaschine, aber account- und versionsabhängiger.</p><p><b>SYNC_TO_ROOM</b> ist nur ein Latenzmodus; <b>SYNC_TO_ZONE</b> ist für gemeinsames Audio gedacht.</p></div></details>
            </section>

            <section class="view" id="view-schedules">
              <div class="page-head"><h2 data-i18n="alarms">Wecker Timer</h2><button class="command" id="reload-schedules" data-i18n="refresh">Aktualisieren</button></div>
              <div class="schedule-grid">
                <section class="panel">
                  <h3 data-i18n="new_alarm_timer">Neuer Wecker Timer</h3>
                  <form id="schedule-form" class="settings-form">
                    <label><span data-i18n="name">Name</span><input name="name" placeholder="Morgen Küche"></label>
                    <label><span data-i18n="start_time">Start</span><input name="start_time" type="time" required></label>
                    <label><span data-i18n="stop_time">Stop Timer / Ende</span><input name="end_time" type="time"></label>
                    <label><span data-i18n="days">Tage</span><select name="days" id="schedule-days-select"><option value="daily" data-i18n="daily">Täglich</option><option value="weekdays" data-i18n="weekdays">Werktage</option><option value="weekend" data-i18n="weekend">Wochenende</option><option value="custom" data-i18n="custom_weekdays">Einzelne Wochentage</option><option value="once" data-i18n="once">Einmalig</option></select></label>
                    <fieldset class="weekday-picker is-disabled" id="schedule-weekday-picker"><legend data-i18n="custom_weekdays">Einzelne Wochentage</legend><label><input type="checkbox" name="weekday" value="mon" disabled><span data-i18n="monday">Montag</span></label><label><input type="checkbox" name="weekday" value="tue" disabled><span data-i18n="tuesday">Dienstag</span></label><label><input type="checkbox" name="weekday" value="wed" disabled><span data-i18n="wednesday">Mittwoch</span></label><label><input type="checkbox" name="weekday" value="thu" disabled><span data-i18n="thursday">Donnerstag</span></label><label><input type="checkbox" name="weekday" value="fri" disabled><span data-i18n="friday">Freitag</span></label><label><input type="checkbox" name="weekday" value="sat" disabled><span data-i18n="saturday">Samstag</span></label><label><input type="checkbox" name="weekday" value="sun" disabled><span data-i18n="sunday">Sonntag</span></label></fieldset>
                    <label><span data-i18n="radio">Radio</span><select id="schedule-device-select" name="device_id" required></select></label>
                    <label><span data-i18n="station">Sender</span><select id="schedule-station-select" name="station_id"></select></label>
                    <label><span data-i18n="or_preset_slot">Oder Preset Slot</span><select name="preset_button"><option value="" data-i18n="use_station">Sender verwenden</option><option value="1">Preset 1</option><option value="2">Preset 2</option><option value="3">Preset 3</option><option value="4">Preset 4</option><option value="5">Preset 5</option><option value="6">Preset 6</option></select></label>
                    <label><span data-i18n="volume">Volume</span><input name="volume" type="number" min="0" max="100" value="25"></label>
                    <label><span data-i18n="stop_action">Stop-Aktion</span><select name="stop_action"><option value="stop_standby" selected data-i18n="stop_and_standby">Stop + Standby</option><option value="standby">Standby</option><option value="stop" data-i18n="stop_only">Nur Stop</option></select></label>
                    <label><span data-i18n="multiroom_master">Multiroom Master</span><select id="schedule-master-select" name="multiroom_master_id"><option value="" data-i18n="no_multiroom">Kein Multiroom</option></select></label>
                    <div><span class="field-label" data-i18n="multiroom_members">Multiroom Mitglieder</span><div class="member-list" id="schedule-members"></div></div>
                    <label class="toggle-line"><input name="dry_run" type="checkbox"><span data-i18n="preview_only">Nur Vorschau / nicht ausführen</span></label>
                    <button class="command primary" type="submit" data-i18n="save_alarm_timer">Wecker Timer speichern</button>
                  </form>
                </section>
                <section class="panel">
                  <div class="panel-title-row"><h3 data-i18n="saved_alarm_timers">Gespeicherte Wecker Timer</h3><button class="command" id="clear-schedules" type="button" data-i18n="clear_test_plans">Testpläne aufräumen</button></div>
                  <div id="schedule-list" class="event-list"></div>
                </section>
              </div>
            </section>
            <section class="view" id="view-system-settings">
              <div class="page-head"><h2>basswiesn Settings</h2><button class="command" id="reload-system-settings">Reload</button></div>
              <div class="settings-grid">
                <section class="panel"><h3>WebGUI defaults</h3><form id="system-settings-form" class="settings-form"><label>Sprache<select id="web-language-select" name="web_language"></select></label><label class="toggle-line"><input id="lab-mode" name="lab_mode" type="checkbox">Enable Lab Mode</label><label class="toggle-line"><input id="guided-hints" name="guided_hints" type="checkbox" checked>Guided hints</label><label>Safe Startup Volume<input id="safe-startup-volume" name="safe_startup_volume" type="number" min="0" max="100" value="30"></label><label class="toggle-line"><input id="show-startup-warning" name="show_startup_warning" type="checkbox" checked>Show startup warning</label><label class="toggle-line"><input id="ip-write-guard" name="ip_write_guard" type="checkbox">IP Write Guard</label><label>Geschützte Radio-IPs<input id="protected-device-ips" name="protected_device_ips" placeholder="Radio-IP, weitere Radio-IP"><small>Diese Radios bleiben read-only. Schreibende Aktionen werden serverseitig blockiert.</small></label><label>Default timezone<select id="system-timezone-select" name="default_timezone"></select></label><label>Default device language<select id="default-device-language-select" name="device_language_default"></select></label><label class="lab-only">Display metadata mode<select id="display-metadata-mode" name="display_metadata_mode"><option value="off">Off</option><option value="station_clock">Station + clock</option><option value="station_clock_wifi">Station + clock + WiFi</option></select></label><label class="lab-only">First-run warnings<select id="first-run-warning-required" name="first_run_warning_required"><option value="false">Confirmed</option><option value="true">Show again on next open</option></select></label><label class="lab-only">Firmware policy<select name="support_latest_firmware_only"><option value="true">Support latest firmware only</option><option value="false">Allow legacy research mode</option></select></label><label class="lab-only">Supported firmware family<input name="latest_supported_firmware_family" value="27.0.x" readonly aria-readonly="true"></label><div class="firmware-warning lab-only"><strong>⚠ Firmware update und unterstützte Modelle</strong><span>Direkt verifiziert sind nur SoundTouch 10, 20, 30 und Portable.</span></div><button class="command primary" type="submit">Einstellungen speichern</button></form></section>
                <section class="panel"><h3>Notes</h3><pre id="system-settings-output">Default UI language is English so every user can reach language setup. Device languages are the Stockholm-supported speaker languages.</pre></section>
              </div>
            </section>

            <section class="view" id="view-backup">
              <div class="page-head"><h2>Backup</h2><button class="command" id="reload-backup">Reload</button></div>
              <div class="split">
                <section class="panel"><h3>Vollständige Sicherung</h3><form id="backup-plan-form" class="settings-form"><label>Radio<select id="backup-device-select" name="device_id" required></select></label><button class="command primary" type="submit">Sicherungsplan erstellen</button></form><pre id="backup-plan-output">Sicherungen werden standardmäßig im lokalen BASSWIESN-Datenverzeichnis abgelegt.</pre></section>
                <section class="panel"><h3>Wiederherstellung</h3><p class="muted-copy">Der Setup-Assistent sichert die bisherigen Serverziele vor dem Schreiben. Seine Statusansicht bietet anschließend den passenden Rollback an.</p><div class="button-row"><button class="command" data-view-jump="setup" type="button">Setup und Rollback öffnen</button></div></section>
              </div>
              <div class="split">
                <section class="panel"><h3>Reference setup</h3><form id="reference-create-form" class="settings-form"><label>Source radio<select id="reference-source-device" name="device_id" required></select></label><label>Name<input name="name" placeholder="Hausstandard ST20"></label><label>Notes<input name="notes" placeholder="Sprache, Presets, Zeitzone, Standardwerte"></label><button class="command" type="submit">Save from radio</button></form></section>
                <section class="panel"><h3>Apply reference setup</h3><form id="reference-apply-form" class="settings-form"><label>Reference<select id="reference-setup-select" name="setup_id" required></select></label><label>Target radio<select id="reference-target-device" name="device_id" required></select></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><label class="toggle-line"><input name="memory_checked" type="checkbox">Memory checked before write</label><button class="command" type="submit">Migrationsplan anzeigen</button></form><div id="reference-setups" class="event-list"></div></section>
              </div>
              <div class="panel"><h3>Backup / reference result</h3><pre id="backup-reference-output">Ready.</pre></div>
            </section>
            <section class="view" id="view-config">
              <div class="page-head"><h2>Config</h2></div>
              <div class="panel"><h3>Rewrite Target</h3><div class="config-grid"><div><span>margeServerUrl</span><code>http://&lt;BASSWIESN-LAN-IP&gt;:{settings.cloud_port}</code></div><div><span>statsServerUrl</span><code>http://&lt;BASSWIESN-LAN-IP&gt;:{settings.cloud_port}</code></div><div><span>swUpdateUrl</span><code>http://&lt;BASSWIESN-LAN-IP&gt;:{settings.cloud_port}/updates/soundtouch</code></div><div><span>bmxRegistryUrl</span><code>http://&lt;BASSWIESN-LAN-IP&gt;:{settings.cloud_port}/bmx/registry/v1/services</code></div></div></div>
              <div class="panel"><h3>Cloud Registry</h3><pre id="registry-preview">Loading...</pre></div><div class="panel"><h3>Stockholm HTTP/XWeb Settings Catalog</h3><div id="settings-catalog" class="event-list"></div></div>
            </section>

            <section class="view" id="view-telnet">
              <div class="page-head"><h2>Telnet</h2><span class="lab-badge">manuell</span></div>
              <div class="split">
                <section class="panel danger-panel"><h3>Gerät per Telnet neu starten</h3><form id="telnet-reboot-form" class="settings-form"><label>Radio<select id="telnet-device-select" name="device_id" required></select></label><label>Bestätigung<input name="confirmation" placeholder="BASSWIESN TELNET REBOOT" autocomplete="off"></label><div class="button-row"><button class="command" id="telnet-capabilities-load" type="button">Fähigkeit prüfen</button><button class="command danger" type="submit">Gerät per Telnet neu starten</button></div></form><pre id="telnet-output">Telnet ist unverschlüsselt und standardmäßig deaktiviert. BASSWIESN sendet keine freien Kommandos.</pre></section>
                <section class="panel"><h3>Known commands</h3><div id="telnet-command-list" class="event-list"></div></section>
              </div>
              <div class="split"><section class="panel"><h3>Standby-Uhr neu aktivieren</h3><form id="standby-clock-form" class="settings-form"><label>Radio<select id="standby-clock-device-select" name="device_id" required></select></label><label>Zeitzone<input name="timezone" value="Europe/Berlin"></label><label>Bestätigung<input name="confirmation" placeholder="BASSWIESN STANDBY CLOCK" autocomplete="off"></label><div class="button-row"><button class="command" id="standby-clock-status" type="button">Status prüfen</button><button class="command primary" type="submit">Standby-Uhr neu aktivieren</button></div></form><pre id="standby-clock-output">Nur kompatible Display-Modelle; Erfolg benötigt Read-back oder manuelle Sichtprüfung.</pre></section><section class="panel"><h3>Jobstatus</h3><pre id="telnet-job-output">Noch kein Telnet- oder Clock-Job gestartet.</pre></section></div>
              <div class="panel"><h3>Stereo pairing research</h3><pre id="stereo-research-output">Loading...</pre></div>
            </section>
            <section class="view" id="view-debug"><div class="page-head"><h2>Protokoll</h2><button class="command" id="reload-debug">Aktualisieren</button></div><div class="panel maintenance-panel"><h3>Aufräumen</h3><p class="muted-copy">Testgeräte und Laufzeitprotokolle können getrennt entfernt werden. Sicherungen echter Radios bleiben erhalten.</p><div class="button-row"><button class="command" id="clear-runtime-logs" type="button">Protokolle leeren</button><button class="command danger" id="clear-test-devices" type="button">Testgeräte entfernen</button></div><p class="form-message" id="maintenance-message" role="status"></p></div><div class="panel"><div id="debug-requests" class="event-list"></div></div></section>

            <section class="view" id="view-telemetry">
              <div class="page-head"><h2>Telemetry</h2><button class="command" id="reload-telemetry">Reload</button></div>
              <div class="split">
                <section class="panel"><h3>Summary</h3><div id="telemetry-summary" class="event-list"></div><button class="command primary" id="download-support-bundle" type="button">Export Support Bundle</button></section>
                <section class="panel"><h3>Probe radio</h3><form id="telemetry-probe-form" class="settings-form"><label>Radio<select id="telemetry-device-select" name="device_id" required></select></label><label>Endpoint<select name="endpoint"><option value="/netStats">/netStats</option><option value="/networkInfo">/networkInfo</option><option value="/info">/info</option><option value="/now_playing">/now_playing</option><option value="/volume">/volume</option><option value="/getZone">/getZone</option></select></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><button class="command" type="submit">Probe</button></form><pre id="telemetry-probe-output">Dry-Run first. Real probes are local GET requests to port 8090.</pre></section>
                <section class="panel"><h3>Radio log capture</h3><form id="radio-log-capture-form" class="settings-form"><label>Radio<select id="radio-log-device-select" name="device_id" required></select></label><label>Reason<input name="reason" value="manual-before-setup" placeholder="setup-before, setup-after, manual"></label><label class="toggle-line"><input name="include_cli" type="checkbox" checked>Include CLI 17000 readouts</label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><div class="button-row"><button class="command" id="radio-log-sources" type="button">Show sources</button><button class="command primary" type="submit">Capture all radio logs</button></div></form><pre id="radio-log-output">Captures HTTP/XML state plus CLI17000 readouts into telemetry_events/config_backups. Full logread/syslog still needs SSH read-only access.</pre></section><section class="panel"><h3>SSH read-only logs <span class="requires-ssh">SSH</span></h3><form id="ssh-log-capture-form" class="settings-form"><label>Radio<select id="ssh-log-device-select" name="device_id" required></select></label><label>SSH user<input name="username" value="root"></label><label>Reason<input name="reason" value="manual-ssh"></label><label>Confirmation<input name="confirmation" placeholder="YES"></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Preview only</label><button class="command danger" type="submit">Capture SSH logs</button></form><pre id="ssh-log-output">Uses fixed read-only commands only: logread, dmesg, ps, netstat, wlan and *.log tails. Requires SSH access.</pre></section>
              </div>
              <div class="panel"><h3>Events</h3><label class="toggle-line"><input id="telemetry-debug-toggle" type="checkbox">Show raw XML/debug payloads</label><div id="telemetry-events" class="event-list"></div></div>
            </section>
            <section class="view" id="view-lab">
              <div class="page-head"><h2>Lab</h2><span class="lab-badge">manual only</span></div>
              <div class="split">
                <section class="panel lab-panel">
                  <h3>Runtime research</h3>
                  <div class="lab-list">
                    <button class="lab-item" data-lab="envswitch"><strong>envswitch / boseurls</strong><span>Runtime URL sync after XML migration</span></button>
                    <button class="lab-item" data-lab="telnet"><strong>CLI 17000</strong><span>Recovery and diagnostics commands</span></button>
                    <button class="lab-item" data-lab="persistence"><strong>Persistence scan</strong><span>/mnt/nv/BoseApp-Persistence inspection plan</span></button>
                    <button class="lab-item" data-lab="airplay"><strong>AirPlay notes</strong><span>Capability gated, not normal setup flow</span></button>
                  </div>
                </section>
                <section class="panel lab-output">
                  <h3 id="lab-title">Select lab item</h3>
                  <pre id="lab-detail">Click an item to read the exact purpose, guard rails and whether basswiesn can execute it. Manual only means: not part of the guided setup, no automatic write, and dangerous actions remain preview/plan until backup and memory checks exist.</pre><div class="guided-box"><h4>BatteryMonitor Patch / Portable Battery <span class="status-pill status-warning">LAB</span></h4><p>Manuelle Reparatur fuer SoundTouch Portable, wenn eine kompatible Ersatzbatterie vom Bose BatteryMonitor wegen strenger Profilpruefung nicht akzeptiert wird. BASSWIESN fragt Batteriewerte nicht im Hintergrund ab; dieser Bereich nutzt nur explizite SSH-Pruefung und Patch-Befehle.</p><p><strong>Was der Patch macht:</strong> Er ersetzt exakt fuenf Bytes im vorhandenen <code>/opt/Bose/BatteryMonitor</code>. Die bekannte Originalsequenz gehoert zur SANYO-Profilpruefung. Die Patchsequenz deaktiviert diese enge Herstellerpruefung, damit kompatible Ersatzpacks akzeptiert werden koennen.</p><form id="battery-patch-form" class="settings-form"><label>Portable Radio<select id="battery-patch-device-select" name="device_id" required></select></label><label>Bestätigung<input name="confirmation" placeholder="BASSWIESN BATTERY PATCH" autocomplete="off"></label><label class="toggle-line"><input name="memory_checked" type="checkbox">Backup-/Speichercheck wurde gelesen</label><div class="button-row"><button class="command" data-battery-action="status" type="submit">Status lesen</button><button class="command" data-battery-action="dry-run" type="submit">Dry Run</button><button class="command danger" data-battery-action="apply" type="submit">Patch anwenden</button><button class="command danger" data-battery-action="rollback" type="submit">Rollback</button></div></form><pre id="battery-patch-output">Warnung: Nur auf eigenen SoundTouch-Portable-Geraeten nutzen. Falsche Firmware/Checksumme wird blockiert. Apply verlangt BASSWIESN BATTERY PATCH, Rollback verlangt BASSWIESN BATTERY ROLLBACK.</pre><button class="command" id="battery-patch-plan" type="button">Plan anzeigen</button></div>
                </section>
              </div>
              <div class="split">
                <section class="panel danger-panel"><h3>Power / Standby</h3><form id="power-action-form" class="settings-form"><label>Radio<select id="recovery-device-select" name="device_id" required></select></label><label>Bestätigung für echte Aktion<input name="confirmation" placeholder="YES"></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Nur Vorschau</label><div class="button-row"><button class="command" data-power-action="standby" type="submit">Standby</button><button class="command danger" data-power-action="low_power_standby" type="submit">Tiefschlaf</button></div></form><pre id="power-action-output">Standby ist als GET-Aktion bestätigt. Tiefschlaf kann das Radio vom Netz trennen und benötigt eine exakte Bestätigung.</pre></section>
                <section class="panel danger-panel"><h3>SSH-Persistenz <span class="requires-ssh">LAB</span></h3><p class="muted-copy">Nur für ein bereits eindeutig erkanntes, bestätigtes Geräteprofil. Der normale Setup-Assistent benötigt diese Funktion nicht.</p><form id="recovery-action-form" class="settings-form"><label>Radio<select id="recovery-reset-device-select" name="device_id" required></select></label><label>Exakte Bestätigung<input name="confirmation" placeholder="YES" autocomplete="off"></label><label class="toggle-line"><input name="dry_run" type="checkbox" checked>Nur Vorschau</label><button class="command danger" data-recovery-action="persistent_ssh" type="submit">SSH-Persistenz prüfen / anwenden</button></form><pre id="recovery-output">Unbekannte Modelle oder Firmwarestände bleiben gesperrt. Es werden keine frei eingegebenen Shell-Befehle ausgeführt.</pre></section>
              </div>
            </section>
            <section class="view" id="view-about">
              <div class="page-head"><div><span class="section-kicker">Entwicklung · Version wird geladen</span><h2>Über BASSWIESN</h2></div></div>
              <section class="panel about-release-copy" aria-live="polite"></section>
            </section>
          </main>
        </div>
        <div class="modal-backdrop" id="first-run-warning" hidden>
          <section class="modal-card danger-panel">
            <h2 id="first-run-warning-title"></h2>
            <div id="first-run-warning-copy"></div>
            <label class="toggle-line"><input id="first-run-warning-read" type="checkbox"><span id="first-run-warning-read-label"></span></label>
            <label class="toggle-line"><input id="first-run-warning-never" type="checkbox"><span id="first-run-warning-never-label"></span></label>
            <button class="command danger" id="first-run-warning-ack" type="button" disabled></button>
          </section>
        </div>
        <div class="modal-backdrop" id="operation-overlay" hidden><section class="modal-card"><button class="modal-close" id="operation-overlay-close" type="button" aria-label="Schliessen">×</button><h2 id="operation-title">Aktion</h2><p id="operation-radio"></p><ol class="operation-steps"><li data-operation-step="execute">ausgefuehrt</li><li data-operation-step="reboot">warte auf reboot</li><li data-operation-step="verify">pruefe</li><li data-operation-step="ok">ok</li></ol><strong id="operation-countdown"></strong></section></div>
        <aside class="help-drawer" id="page-help" aria-hidden="true" aria-label="Seitenhilfe">
          <header><div><span>Einfach erklärt</span><h2 id="page-help-title">Hilfe</h2></div><button id="page-help-close" type="button" aria-label="Hilfe schließen">×</button></header>
          <div class="help-drawer-body"><p id="page-help-intro"></p><div id="page-help-flow" class="help-flow"></div><h3>Was kommt wo hinein?</h3><div id="page-help-fields" class="help-field-list"></div><div id="page-help-tip" class="help-tip"></div></div>
        </aside><div class="help-scrim" id="page-help-scrim" hidden></div>
        <script src="/static/js/api.js?v={settings.version}"></script>
        <script src="/static/js/ui-errors.js?v={settings.version}"></script>
        <script src="/static/js/translations.js?v={settings.version}"></script>
        <script src="/static/app.js?v={settings.version}"></script>
        </body></html>
        """

    @app.get("/remote/{device_id}", response_class=HTMLResponse)
    async def remote(device_id: str) -> str:
        settings = get_settings()
        return f"""
        <!doctype html>
        <html lang="de"><head><title>basswiesn remote</title>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
        <link rel="stylesheet" href="/static/remote.css?v={settings.version}"></head>
        <body>
          <main class="remote-shell" data-device-id="{device_id}">
            <header class="remote-header"><div><span id="remote-version">basswiesn remote · Version nicht verfügbar</span><h1 id="remote-title">Radio</h1></div><a href="/">Desktop</a></header>
            <section class="now-panel"><div id="remote-now">Loading...</div></section>
            <section class="volume-panel"><button data-volume-step="-5">-</button><input id="remote-volume" type="range" min="0" max="100" value="5"><button data-volume-step="5">+</button><strong id="remote-volume-label">5%</strong></section>
            <section class="transport-grid">
              <button data-key="PREV_TRACK">Prev</button><button data-key="PLAY_PAUSE">Play/Pause</button><button data-key="NEXT_TRACK">Next</button>
              <button data-key="STOP">Stop</button><button data-key="MUTE">Mute</button><button data-key="POWER">Power</button>
            </section>
            <section class="preset-grid" id="remote-presets"></section>
            <section class="station-panel"><select id="remote-station"></select><button id="remote-play-station">Bei 5% starten</button></section>
            <pre id="remote-output"></pre>
          </main>
          <script src="/static/remote.js?v={settings.version}"></script>
        </body></html>
        """

    # Some radios keep an old/wrong cloud port during recovery. Register only
    # known Bose cloud paths on the WebGUI port, without the cloud catchall.
    _mount_web_cloud_compat(app)
    return app


def create_cloud_app() -> FastAPI:
    app = FastAPI(title="basswiesn Cloud Emulator", lifespan=lifespan)
    install_exception_handlers(app)

    @app.middleware("http")
    async def log_cloud_errors(request: Request, call_next):
        raw_host = request.headers.get("host", "")[:255]
        host = raw_host.rsplit(":", 1)[0].strip("[]").lower()
        settings = get_settings()
        known_hosts = {item.lower() for item in HOSTS_DOMAINS}
        known_hosts.update({"localhost", "127.0.0.1", settings.lan_host.lower()})
        if host and host not in known_hosts and not is_safe_radio_host(host):
            fields = {
                "path": request.url.path,
                "host": raw_host,
                "origin": request.headers.get("origin", "")[:500],
                "referer": request.headers.get("referer", "")[:500],
                "user_agent": request.headers.get("user-agent", "")[:500],
            }
            write_masterlog("unknown_host_detected", **fields)
            write_masterlog("unknown_hosts_detected", count=1, hosts=[host])
        response = await call_next(request)
        if response.status_code >= 400:
            write_masterlog(
                "cloud_request_error",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
            )
        return response
    _allow_local_browser_clients(app)
    # Registry asset URLs point at the cloud port because that is the host the
    # radios can reach. Serve the small provider icon set there as well.
    app.mount("/static", StaticFiles(directory="basswiesn/app/static"), name="cloud-static")
    media_dir = get_settings().data_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.include_router(cloud.router)
    return app


def create_debug_app() -> FastAPI:
    app = FastAPI(title="basswiesn Diagnostics", lifespan=lifespan)
    install_exception_handlers(app)
    _allow_local_browser_clients(app)
    app.include_router(debug.router)
    return app


web_app = create_web_app()
https_app = create_web_app(title="basswiesn HTTPS WebGUI", background_tasks=False)
cloud_app = create_cloud_app()
debug_app = create_debug_app()
app = web_app
