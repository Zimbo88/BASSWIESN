const shell = document.querySelector(".app-shell");
const cloudPort = shell.dataset.cloudPort;
const debugPort = shell.dataset.debugPort;
const { getJson, postJson, putJson, deleteJson } = window.BasswiesnApi;
const { showToast, showApiError, setFormBusy } = window.BasswiesnUi;

window.addEventListener("unhandledrejection", (event) => {
  showApiError(event.reason, "Unerwarteter UI-Fehler");
});

function browserServiceBase(configuredUrl, port) {
  const browserHost = window.location.hostname || "127.0.0.1";
  try {
    const configured = new URL(configuredUrl || "", window.location.href);
    const unusableHost = configured.hostname === "127.0.0.1"
      || configured.hostname === "localhost";
    const browserIsLanAddress = browserHost !== "127.0.0.1"
      && browserHost !== "localhost"
      && browserHost !== "0.0.0.0";
    if (browserIsLanAddress && configured.hostname !== browserHost) {
      return `${window.location.protocol}//${browserHost}:${port}`;
    }
    if (!unusableHost) return configured.origin;
  } catch { /* fall back to the host used to open the WebGUI */ }
  return `${window.location.protocol}//${browserHost}:${port}`;
}

function serviceUrls() {
  const server = state.setupWizardServer || {};
  return {
    cloud: browserServiceBase(server.cloud_base_url || shell.dataset.cloudBaseUrl, cloudPort),
    debug: browserServiceBase(server.debug_base_url || shell.dataset.debugBaseUrl, debugPort),
  };
}

async function reportServiceStatus(service, online, reason = "") {
  try {
    await postJson("/api/setup/wizard/service-status", { service, online, reason: String(reason).slice(0, 300) });
  } catch { /* status logging must not change the dashboard result */ }
}

const state = {
  devices: [],
  deviceStatuses: [],
  deviceCapabilities: [],
  deviceHealth: [],
  stations: [],
  presets: [],
  presetStatus: null,
  providerStatus: null,
  requests: [],
  playHistory: [],
  playStats: null,
  statsDetail: { type: "overview", key: "" },
  schedules: [],
  telemetry: [],
  telemetrySummary: null,
  telemetryAnalysis: null,
  emulationGaps: null,
  storageSummary: null,
  cleanupPreview: null,
  onlineStations: [],
  presetProfiles: [],
  guidedSetupPlan: null,
  mediaTypes: [],
  systemSettings: null,
  offlineStatus: null,
  featureStatus: null,
  featureFilter: "all",
  offlinePreflight: null,
  multiroomScenarios: [],
  multiroomMethods: [],
  multiroomRecentStations: [],
  multiroomPendingPayload: null,
  multiroomPendingScenarioId: null,
  lastKnownMultiroomState: { scenarios: [], methods: [], recentStations: [], rendered: false },
  multiroomRemoveDeviceId: "",
  settingsCatalog: [],
  keyCommands: [],
  displayModes: [],
  telnetCommands: [],
  mediaCapabilities: null,
  mediaPlaylists: [],
  batteryStates: [],
  serviceCatalog: [],
  referenceSetups: [],
  stereoResearch: null,
  registry: null,
  liveComparison: null,
  setupWizardServer: null,
  setupFlowStep: 0,
  setupFlowDone: {},
  setupLastPreflight: null,
  setupLastRoute: null,
  setupDevices: [],
  setupJob: null,
  setupJobPoller: null,
  setupRebuildDevices: [],
  setupRebuildTargets: [],
  setupRebuildDiscovery: null,
  setupRebuildPreview: null,
  setupRebuildJob: null,
  setupRebuildPoller: null,
  guidedPreset: { active: false, deviceId: "", step: "", dismissed: false },
  systemHealth: null,
  localTestOverview: null,
  refreshSeq: 0,
  stationFilter: "",
  presetFilter: "",
  applicationVersion: "",
  researchHealth: {
    deviceId: "", playback: null, provider: null, metadata: null,
    artwork: null, restrictions: null, reporting: null, airplay: null, timeline: null, clock: null,
  },
};

function isLanHost(host) {
  const value = String(host || "").trim().toLowerCase();
  if (!value || value === "content.api.bose.io" || value === "localhost" || value === "127.0.0.1") return false;
  if (/^169\.254\./.test(value)) return false;
  return /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.)/.test(value);
}

function updateServerIdentity() {
  const element = document.getElementById("server-identity");
  if (!element) return;
  const version = state.applicationVersion;
  const displayVersion = version ? (String(version).startsWith("v") ? version : `v${version}`) : "Version nicht verfügbar";
  const configured = state.systemSettings?.lan_host || state.setupWizardServer?.recommended_host || "";
  const host = isLanHost(configured) ? configured : "";
  element.textContent = `${displayVersion} · ${host ? `Host ${host}` : "Host nicht gesetzt"}`;
  element.title = host ? "Aktive BASSWIESN LAN Host-IP" : "Keine sichere LAN Host-IP erkannt oder gesetzt.";
}

function ensureIntegratedPanels() {
  if (!document.getElementById("preset-checker-grid")) {
    const panel = document.createElement("section");
    panel.className = "panel preset-checker easy-hidden";
    panel.innerHTML = `<div class="panel-title-row"><div><h3 data-i18n="preset_checker">Preset Checker</h3><p class="muted-copy">Radio-Slots und BASSWIESN-Daten read-only vergleichen.</p></div><button class="command" id="preset-checker-refresh" type="button" data-i18n="refresh">Aktualisieren</button></div><p class="form-message" id="preset-checker-message"></p><div class="preset-checker-grid" id="preset-checker-grid"></div>`;
    const lastResult = document.getElementById("preset-result")?.closest(".panel");
    if (lastResult) lastResult.after(panel);
    else document.querySelector("#view-presets .preset-builder")?.after(panel);
  }
  if (!document.getElementById("provider-status-grid")) {
    const panel = document.createElement("section");
    panel.className = "panel provider-debug";
    panel.innerHTML = `<div class="panel-title-row"><div><h3 data-i18n="provider_status">Provider Status</h3><p class="muted-copy">Sources, Availability und Runtime-State gemeinsam prüfen.</p></div><button class="command" id="provider-status-load" type="button" data-i18n="load_status">Status laden</button></div><p class="form-message" id="provider-status-message">Status wird nur auf Klick vom ausgewählten Radio gelesen.</p><div class="provider-status-grid" id="provider-status-grid"></div><details><summary data-i18n="runtime_state">Runtime State</summary><pre id="runtime-state-output">Noch nicht geladen.</pre></details><details><summary data-i18n="capabilities">Capabilities</summary><pre id="capability-state-output">Noch nicht geladen.</pre></details>`;
    document.querySelector("#view-telemetry > .split")?.after(panel);
  }
  if (!document.getElementById("telemetry-analysis-panel")) {
    const panel = document.createElement("section");
    panel.className = "panel";
    panel.id = "telemetry-analysis-panel";
    panel.innerHTML = `<div class="panel-title-row"><div><h3 data-i18n="telemetry_analysis">Telemetry Analyse</h3><p class="muted-copy">Cloud, Radio-Aktivität, Fehlergruppen und Empfehlungen verständlich zusammengefasst.</p></div><label><span data-i18n="time_range">Zeitraum</span><select id="telemetry-range"><option value="1h">1h</option><option value="24h" selected>24h</option><option value="7d">7 Tage</option><option value="all">Alles</option></select></label></div><div class="button-row"><button class="command" id="download-telemetry-json" type="button" data-i18n="download_json">JSON herunterladen</button><button class="command" id="download-telemetry-csv" type="button" data-i18n="download_csv">CSV herunterladen</button><button class="command" id="download-telemetry-report" type="button" data-i18n="download_html_report">HTML Report herunterladen</button></div><div id="telemetry-analysis" class="event-list"></div></section>`;
    document.querySelector("#view-telemetry > .split")?.after(panel);
  }
  if (!document.getElementById("emulation-gaps-panel")) {
    const panel = document.createElement("section");
    panel.className = "panel";
    panel.id = "emulation-gaps-panel";
    panel.innerHTML = `<h3 data-i18n="emulation_gaps">Emulation Gap Report</h3><div id="emulation-gaps" class="event-list"></div>`;
    document.querySelector("#telemetry-analysis-panel")?.after(panel);
  }
  if (!document.getElementById("storage-cleanup-panel")) {
    const panel = document.createElement("section");
    panel.className = "panel";
    panel.id = "storage-cleanup-panel";
    panel.innerHTML = `<h3 data-i18n="storage_cleanup">Speicher & Cleanup</h3><div class="button-row"><button class="command" id="storage-check" type="button" data-i18n="check_storage">Speicher prüfen</button><button class="command" id="cleanup-dry-run" type="button" data-i18n="cleanup_preview">Cleanup Vorschau</button><button class="command danger" id="cleanup-run" type="button" data-i18n="run_cleanup">Cleanup ausführen</button></div><div id="storage-cleanup" class="event-list"></div>`;
    document.querySelector("#emulation-gaps-panel")?.after(panel);
  }
  if (!document.getElementById("local-test-center")) {
    const panel = document.createElement("section");
    panel.className = "panel local-test-center";
    panel.id = "local-test-center";
    panel.innerHTML = `<div class="panel-title-row"><div><h3>BASSWIESN Release-Candidate Center</h3><p class="muted-copy">Health, Discovery, Events, Webhooks, Medien, Diagnose und LAB-Status ohne blockierende Geräteabfragen.</p></div><button class="command" id="local-test-refresh" type="button">Status laden</button></div><div class="button-row"><button class="command primary" id="local-health-run" type="button">Healthcheck starten</button><button class="command" id="local-ssdp-test" type="button">SSDP-Test</button><button class="command" id="local-diagnostic-preview" type="button">Diagnosevorschau</button><button class="command" id="local-backup-create" type="button">Backup erstellen</button></div><div id="local-test-summary" class="local-test-grid"></div><details><summary>Technische Details</summary><pre id="local-test-output">Noch nicht geladen.</pre></details>`;
    const systemHealth = document.getElementById("system-health")?.closest(".panel");
    if (systemHealth) systemHealth.after(panel);
    else document.querySelector("#view-dashboard .metric-grid")?.after(panel);
  }
  if (!document.getElementById("events-webhooks-panel")) {
    const panel = document.createElement("section");
    panel.className = "panel";
    panel.id = "events-webhooks-panel";
    panel.innerHTML = `<div class="panel-title-row"><div><h3>Events & Webhooks</h3><p class="muted-copy">Interne Ereignisse, Zustellstatus und deaktivierte Webhook-Konfiguration.</p></div><button class="command" id="events-refresh" type="button">Events laden</button></div><div id="events-webhooks-summary" class="event-list"></div><details><summary>Letzte Events</summary><pre id="events-output">Noch nicht geladen.</pre></details>`;
    document.querySelector("#view-system-settings")?.append(panel);
  }
  if (!document.getElementById("media-library-local-panel")) {
    const panel = document.createElement("section");
    panel.className = "panel feature-limited";
    panel.id = "media-library-local-panel";
    panel.innerHTML = `<div class="panel-title-row"><div><h3>Lokale Medienbibliothek <span class="status-pill status-warning">experimentell</span></h3><p class="muted-copy">Nur konfigurierte Wurzelordner; Symlink-Ausbruch und Path Traversal werden blockiert.</p></div><button class="command" id="media-library-status-refresh" type="button">Status laden</button></div><div id="media-library-local-status" class="event-list"></div>`;
    document.querySelector("#view-media")?.append(panel);
  }
  const settingsForm = document.getElementById("system-settings-form");
  if (settingsForm && !document.getElementById("update-manifest-url")) {
    settingsForm.querySelector('button[type="submit"]')?.insertAdjacentHTML("beforebegin", `<label>BASSWIESN Host IP<input id="system-lan-host" name="lan_host" placeholder="LAN-IP des BASSWIESN Hosts"></label>`);
    settingsForm.querySelector('button[type="submit"]')?.insertAdjacentHTML("beforebegin", `<fieldset class="settings-section"><legend>Offline Mode</legend><label>Modus<select id="offline-mode" name="offline_mode"><option value="auto">automatisch</option><option value="strict">strikt lokal</option><option value="off">aus</option></select></label><label>Erlaubte Stream-Hosts<input id="offline-allowed-stream-hosts" name="offline_allowed_stream_hosts" placeholder="stream.example.org, radio.example.net"></label><p class="form-message" id="offline-status">Offline-Status wird geladen.</p><small>strict blockiert optionale externe BASSWIESN-Dienste. Das Radio und lokale Streams bleiben davon getrennt.</small></fieldset>`);
    settingsForm.querySelector('button[type="submit"]')?.insertAdjacentHTML("beforebegin", `<fieldset class="settings-section"><legend data-i18n="updates">Updates</legend><label class="toggle-line"><input id="update-check-enabled" name="update_check_enabled" type="checkbox"><span data-i18n="enable_update_check">Updateprüfung aktivieren</span></label><label>Manifest URL<input id="update-manifest-url" name="update_manifest_url" type="url" placeholder="https://…/manifest.json"></label><label>Repository URL<input id="update-repo-url" name="update_repo_url" type="url" placeholder="https://…"></label><label>Kanal<select id="update-channel" name="update_channel"><option value="manual">manual</option><option value="stable">stable</option><option value="beta">beta</option></select></label><button class="command" id="update-check" type="button" data-i18n="check_updates">Nach Update suchen</button><p class="form-message" id="update-status"></p></fieldset>`);
  }
}

function text(value, fallback = "-") {
  return value === undefined || value === null || value === "" ? fallback : value;
}

function formatDuration(seconds) {
  const total = Number(seconds || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function formatClockSeconds(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function renderLocalTestOverview(data = state.localTestOverview) {
  const summary = document.getElementById("local-test-summary");
  const output = document.getElementById("local-test-output");
  if (!summary || !output || !data) return;
  const cards = [
    ["Health", data.health?.items?.[0]?.status || data.healthRun?.status || "nicht geprüft"],
    ["Events", String(data.events?.items?.length ?? 0)],
    ["Webhooks", data.webhooks?.enabled_globally ? "aktiviert" : "deaktiviert"],
    ["Medien", data.media?.enabled ? "aktiv" : "deaktiviert"],
    ["DLNA", data.dlna?.enabled ? "aktiv" : "deaktiviert"],
    ["Announcements", data.announcements?.enabled ? "aktiv" : "deaktiviert"],
    ["LAB", data.lab?.enabled ? "aktiv" : "deaktiviert"],
  ];
  summary.innerHTML = cards.map(([label, value]) => `<article class="metric mini-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`).join("");
  output.textContent = JSON.stringify(data, null, 2);
}

async function refreshLocalTestOverview(extra = {}) {
  const endpoints = {
    health: "/api/health/center/latest",
    events: "/api/events?limit=10",
    webhooks: "/api/webhooks",
    media: "/api/media/library/status",
    dlna: "/api/dlna/status",
    announcements: "/api/announcements/status",
    lab: "/api/lab/status",
  };
  const entries = await Promise.all(Object.entries(endpoints).map(async ([key, url]) => {
    try { return [key, await getJson(url)]; }
    catch (error) { return [key, { error: String(error) }]; }
  }));
  state.localTestOverview = { ...Object.fromEntries(entries), ...extra };
  renderLocalTestOverview();
}

async function runLocalHealthcheck() {
  const result = await postJson("/api/health/center", {});
  await refreshLocalTestOverview({ healthRun: result });
  showToast("Healthcheck abgeschlossen.");
}

async function runLocalSsdpTest() {
  const result = await postJson("/api/discovery/ssdp", {});
  await refreshLocalTestOverview({ ssdp: result });
  showToast(`SSDP-Test abgeschlossen: ${result.devices?.length || 0} Gerät(e).`);
}

async function previewLocalDiagnostic() {
  const result = await getJson("/api/diagnostics/system/preview");
  await refreshLocalTestOverview({ diagnosticPreview: result });
  showToast("Diagnosevorschau geladen.");
}

async function createLocalBackup() {
  if (!window.confirm("Lokales Systembackup erstellen? Der Vorgang verändert keine Radios.")) return;
  const result = await postJson("/api/backups/create", {});
  await refreshLocalTestOverview({ backup: result });
  showToast("Backup wurde erstellt.");
}

async function refreshEventsPanel() {
  const [events, webhooks] = await Promise.all([getJson("/api/events?limit=25"), getJson("/api/webhooks")]);
  const summary = document.getElementById("events-webhooks-summary");
  const output = document.getElementById("events-output");
  if (summary) {
    summary.innerHTML = `<article><strong>Events</strong><span>${events.items?.length || 0} geladen</span></article><article><strong>Webhooks</strong><span>${webhooks.enabled_globally ? "global aktiv" : "global deaktiviert"} · ${webhooks.items?.length || 0} Endpoint(s)</span></article>`;
  }
  if (output) output.textContent = JSON.stringify({ events, webhooks }, null, 2);
}

async function refreshMediaLibraryPanel() {
  const media = await getJson("/api/media/library/status");
  const target = document.getElementById("media-library-local-status");
  if (!target) return;
  target.innerHTML = `<article><strong>Status</strong><span>${media.enabled ? "aktiv" : "deaktiviert"}</span></article><article><strong>Formate</strong><span>${(media.supported_formats || []).join(", ")}</span></article><article><strong>Wurzeln</strong><span>${media.roots?.length || 0}</span></article>`;
}

function setStatus(id, ok, label, mode = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = label || (ok ? "online" : "offline");
  el.className = mode || (ok ? "ok" : "bad");
}

function startOperationOverlay(title, device, seconds = 60) {
  const overlay = document.getElementById("operation-overlay");
  document.getElementById("operation-title").textContent = title;
  document.getElementById("operation-radio").textContent = `Radio: ${device?.name || "-"} / ${device?.ip_address || "-"}`;
  const nodes = [...overlay.querySelectorAll("[data-operation-step]")];
  nodes.forEach((node) => node.className = "");
  nodes[0].classList.add("is-active");
  overlay.hidden = false;
  syncBodyScrollLock();
  let timer;
  return {
    waitForReboot() {
      nodes[0].className = "is-done"; nodes[1].className = "is-active";
      let remaining = seconds;
      document.getElementById("operation-countdown").textContent = `${remaining}s`;
      timer = window.setInterval(() => {
        remaining -= 1;
        document.getElementById("operation-countdown").textContent = remaining > 0 ? `${remaining}s` : "";
        if (remaining <= 0) { window.clearInterval(timer); nodes[1].className = "is-done"; nodes[2].className = "is-active"; window.setTimeout(() => { nodes[2].className = "is-done"; nodes[3].className = "is-done"; document.getElementById("operation-countdown").textContent = "OK"; }, 1000); }
      }, 1000);
    },
    complete() { window.clearInterval(timer); nodes.slice(0, 3).forEach((node) => node.className = "is-done"); nodes[3].className = "is-done"; document.getElementById("operation-countdown").textContent = "OK"; },
    fail(error) { window.clearInterval(timer); document.getElementById("operation-countdown").textContent = String(error); },
  };
}

function syncBodyScrollLock() {
  const modalOpen = Array.from(document.querySelectorAll(".modal-backdrop")).some((modal) => !modal.hidden);
  const drawerOpen = Boolean(document.querySelector(".advanced-nav[open]"));
  document.body.classList.toggle("modal-open", modalOpen);
  document.body.classList.toggle("nav-menu-open", drawerOpen);
}

document.getElementById("operation-overlay-close")?.addEventListener("click", () => {
  document.getElementById("operation-overlay").hidden = true;
  syncBodyScrollLock();
});

function filteredStations(filter) {
  const needle = filter.trim().toLowerCase();
  if (!needle) return state.stations;
  return state.stations.filter((station) => `${station.name} ${station.stream_url}`.toLowerCase().includes(needle));
}

function selectedDeviceId() {
  const select = document.getElementById("preset-device-select");
  return select ? select.value : "";
}

function stationName(stationId) {
  const station = state.stations.find((item) => String(item.id) === String(stationId));
  return station ? station.name : "Unset";
}

function deviceName(deviceId) {
  const device = state.devices.find((item) => String(item.device_id) === String(deviceId));
  return device ? text(device.name, device.device_id) : text(deviceId);
}

function stationHost(streamUrl) {
  try {
    return new URL(streamUrl).hostname.replace(/^www\./, "");
  } catch {
    return "Internet stream";
  }
}

async function refreshStations(selectedStationId = "") {
  state.stations = await getJson("/api/stations");
  renderStations();
  const select = document.getElementById("preset-station-select");
  if (select && selectedStationId) {
    select.value = String(selectedStationId);
    if (select.value !== String(selectedStationId)) {
      state.presetFilter = "";
      renderStations();
      select.value = String(selectedStationId);
    }
  }
}

async function loadPresetsForSelectedDevice(probe = false) {
  const deviceId = selectedDeviceId();
  if (!deviceId) {
    state.presets = [];
    state.presetStatus = null;
    renderPresetSlots();
    return;
  }
  try {
    state.presets = await getJson(`/api/presets/${encodeURIComponent(deviceId)}`);
  } catch {
    state.presets = [];
  }
  try {
    state.presetStatus = await getJson(`/api/presets/${encodeURIComponent(deviceId)}/status${probe ? "?probe=true" : ""}`);
  } catch {
    state.presetStatus = null;
  }
  renderPresetSlots();
  renderPresetChecker();
}

function renderPresetChecker() {
  const grid = document.getElementById("preset-checker-grid");
  if (!grid) return;
  const slots = state.presetStatus?.slots || [];
  const labels = { valid: "VALID", warning: "WARNING", broken: "BROKEN", unknown: "UNKNOWN" };
  grid.innerHTML = slots.length ? slots.map((slot) => {
    const radioLocation = slot.radio?.location || "–";
    const basswiesnLocation = slot.basswiesn?.location || "–";
    const sync = state.presetStatus?.sync_state || {};
    const source = slot.basswiesn?.source || "–";
    const stream = slot.basswiesn?.stream_url || "–";
    const checks = (slot.checks || []).map((check) => `<li class="preset-check preset-${escapeHtml(String(check.status || "unknown").toLowerCase())}"><strong>${escapeHtml(check.id || "check")}: ${escapeHtml(check.status || "UNKNOWN")}</strong><span>${escapeHtml(check.message || "")}</span></li>`).join("");
    return `<article class="preset-compare-card preset-${escapeHtml(slot.state)}"><header><strong>Preset ${slot.button}</strong>${statusPill(labels[slot.state] || slot.verdict || "UNKNOWN")}</header><p>${escapeHtml(slot.message || "")}</p><div class="preset-compare-columns"><section><b>Radio</b><span>${escapeHtml(slot.radio?.title || "–")}</span><small>${escapeHtml(slot.radio?.source || "–")} · Readback ${escapeHtml(text(state.presetStatus?.radio_error ? "fehlt" : "vorhanden"))}</small></section><section><b>BASSWIESN</b><span>${escapeHtml(slot.basswiesn?.title || "–")}</span><small>${escapeHtml(slot.basswiesn?.provider || source)} · Logo ${escapeHtml(slot.basswiesn?.logo_mode || "radio_symbol")}</small></section></div><p class="preset-dependency-line"><strong>Quelle:</strong> ${escapeHtml(source)} · <strong>Stream:</strong> ${escapeHtml(stream)}</p><small>Letzter Sync: ${escapeHtml(text(sync.updated_at || sync.last_success_at, "unbekannt"))}</small><details><summary>${escapeHtml(i18nT("details"))}</summary><ul class="preset-check-list">${checks}</ul><p>${escapeHtml((slot.changed_fields || []).join(", ") || labels[slot.state] || "–")}</p><div class="preset-raw-grid"><section><b>Radio location/XML</b><pre>${escapeHtml(radioLocation)}</pre></section><section><b>BASSWIESN location/XML</b><pre>${escapeHtml(basswiesnLocation)}</pre></section></div></details></article>`;
  }).join("") : `<div class="empty">${escapeHtml(state.presetStatus?.radio_error || i18nT("unknown"))}</div>`;
}

function featureFilterMatches(feature) {
  const filter = state.featureFilter || "all";
  if (filter === "active") return feature.enabled && feature.available;
  if (filter === "action_required") return Boolean(feature.blockers?.length) || !feature.configured;
  if (filter === "disabled") return feature.status === "Deaktiviert";
  if (filter === "experimental") return feature.experimental || feature.lab_only;
  if (filter === "hardware_open") return feature.hardware_status === "offen";
  return true;
}

function featureLink(target) {
  if (!target?.view) return "";
  return `<button class="command feature-link" type="button" data-feature-view="${escapeHtml(target.view)}" data-feature-anchor="${escapeHtml(target.anchor || "")}">Zuständige Ansicht öffnen</button>`;
}

function renderFeatureStatus() {
  const summary = document.getElementById("feature-status-summary");
  const groups = document.getElementById("feature-status-groups");
  if (!summary || !groups) return;
  const payload = state.featureStatus || { features: [], counts: {} };
  const features = Array.isArray(payload.features) ? payload.features.filter(featureFilterMatches) : [];
  const counts = payload.counts || {};
  summary.innerHTML = [
    ["Alle", counts.all ?? 0], ["Aktiv", counts.active ?? 0], ["Aktion erforderlich", counts.action_required ?? 0],
    ["Deaktiviert", counts.disabled ?? 0], ["Experimentell/LAB", counts.experimental ?? 0], ["Hardwaretest offen", counts.hardware_open ?? 0],
  ].map(([label, value]) => `<span class="feature-summary-item"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></span>`).join("");
  const grouped = new Map();
  features.forEach((feature) => { if (!grouped.has(feature.category)) grouped.set(feature.category, []); grouped.get(feature.category).push(feature); });
  groups.innerHTML = grouped.size ? Array.from(grouped.entries()).map(([category, items]) => `<section class="feature-status-group"><h3>${escapeHtml(category)}</h3><div class="feature-status-grid">${items.map((feature) => {
    const blockers = feature.blockers?.length ? `<div class="feature-blockers"><strong>Aktion erforderlich</strong><ul>${feature.blockers.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : `<p class="feature-clear">Keine aktuell gemeldete Blockade.</p>`;
    const requirements = feature.requirements?.length ? `<ul>${feature.requirements.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<span>Keine zusätzlichen Angaben.</span>`;
    const doc = feature.documentation?.href ? `<a class="feature-doc-link" href="${escapeHtml(feature.documentation.href)}" target="_blank" rel="noopener">${escapeHtml(feature.documentation.title || "Dokumentation")}</a>` : "";
    const flags = feature.feature_flags?.length ? feature.feature_flags.map((item) => `<code>${escapeHtml(item)}</code>`).join(", ") : "keine";
    return `<article class="feature-card"><header><div><h4>${escapeHtml(feature.title)}</h4><p>${escapeHtml(feature.description)}</p></div>${statusPill(feature.status)}</header><div class="feature-badges">${statusPill(feature.enabled ? "aktiviert" : "deaktiviert", feature.enabled ? "ready" : "bad")}${statusPill(feature.available ? "nutzbar" : "nicht nutzbar", feature.available ? "ready" : "bad")}${feature.experimental ? statusPill("experimentell", "pending") : ""}${feature.lab_only ? statusPill("LAB", "pending") : ""}${feature.hardware_status === "offen" ? statusPill("Hardwaretest offen", "pending") : ""}</div>${blockers}<details><summary>Voraussetzungen und technische Details</summary><dl class="feature-detail-list"><div><dt>Aktivierung</dt><dd>${escapeHtml(feature.activation_method || "Laufzeitstatus")}</dd></div><div><dt>Feature Flags</dt><dd>${flags}</dd></div><div><dt>Konfiguration</dt><dd>${escapeHtml(feature.configured ? "vollständig erkannt" : "unvollständig oder nicht gesetzt")}</dd></div><div><dt>Neustart</dt><dd>${escapeHtml(feature.restart_required ? "erforderlich" : "nicht erforderlich")}</dd></div><div><dt>Hardware</dt><dd>${escapeHtml(feature.hardware_status || "nicht erforderlich")}</dd></div><div><dt>Sicherheit</dt><dd>${escapeHtml(feature.security_status || "keine zusätzlichen Angaben")}</dd></div><div><dt>Voraussetzungen</dt><dd>${requirements}</dd></div></dl></details><div class="feature-actions">${featureLink(feature.settings_target)}${feature.navigation_target && feature.navigation_target.view !== feature.settings_target?.view ? featureLink(feature.navigation_target) : ""}${doc}</div></article>`;
  }).join("")}</div></section>`).join("") : `<div class="empty">Für diesen Filter sind keine Funktionen vorhanden.</div>`;
  document.querySelectorAll("[data-feature-filter]").forEach((button) => button.classList.toggle("is-selected", button.dataset.featureFilter === state.featureFilter));
}

async function loadFeatureStatus() {
  state.featureStatus = await getJson("/api/features/status");
  renderFeatureStatus();
}

function renderOfflinePreflight() {
  const output = document.getElementById("offline-preflight-output");
  if (!output) return;
  const data = state.offlinePreflight;
  if (!data) return;
  const dependencies = (data.dependencies || []).map((item) => `<div class="event-row"><span>${escapeHtml(item.name)}</span><strong>${escapeHtml(item.value)}</strong><small>${escapeHtml(item.basis || "")}</small></div>`).join("");
  const probe = data.probe || {};
  output.innerHTML = `<div class="event-row"><span>Erkennung</span><strong>${escapeHtml(data.source_kind || "unbekannt")} · ${escapeHtml(data.basis || "unbekannt")}</strong><small>${escapeHtml(data.expectation || "")}</small></div>${dependencies}<div class="event-row"><span>Serverprüfung</span><strong>${escapeHtml(probe.status || "nicht ausgeführt")}</strong><small>${escapeHtml(probe.reason || "Keine Prüfung angefordert.")}</small></div><div class="event-row"><span>Radio-autark</span><strong>${escapeHtml(data.radio_autark?.status || "unbestätigt")}</strong><small>Softwareklassifizierung ersetzt keinen Hardwaretest.</small></div>`;
}

function renderProviderStatus() {
  const grid = document.getElementById("provider-status-grid");
  if (!grid) return;
  const priority = new Set(["LOCAL_INTERNET_RADIO", "TUNEIN", "RADIO_BROWSER", "PANDORA", "SPOTIFY", "DEEZER", "AMAZON"]);
  const rows = (state.providerStatus?.providers || []).filter((item) => priority.has(item.name));
  const flag = (label, value) => statusPill(`${label}: ${value ? "ja" : "nein"}`, value ? "ready" : "");
  grid.innerHTML = rows.length ? rows.map((item) => `<article class="provider-card"><header><strong>${escapeHtml(item.name)}</strong><code>ID ${escapeHtml(item.provider_id)}</code></header><div>${flag("registriert", item.registered)}${flag("verfügbar", item.available)}${flag("ready", item.ready)}${flag("OAuth Dummy", item.oauth_dummy_injected)}${flag("in /sources", item.visible_in_sources)}</div><small>Auth: ${escapeHtml(item.auth_model)}</small></article>`).join("") : `<div class="empty">Status noch nicht geladen.</div>`;
  document.getElementById("runtime-state-output").textContent = JSON.stringify(state.providerStatus?.runtime_state || {}, null, 2);
  const deviceId = document.getElementById("telemetry-device-select")?.value;
  document.getElementById("capability-state-output").textContent = JSON.stringify(state.deviceCapabilities.find((item) => item.device_id === deviceId) || {}, null, 2);
}

function statusTone(value) {
  const raw = String(value || "").toLowerCase();
  if (/implemented$|confirmed|online|ready|basswiesn/.test(raw) && !/dry|guarded|partly|candidate/.test(raw)) return "status-ok";
  if (/guarded|candidate|limited|partly|read-only|runtime only|preview|planned|dry-run|dry run|research|plan only|manual only|write-plan|bridge only|lab/.test(raw)) return "status-warning";
  if (/disabled/.test(raw)) return "status-warning";
  if (/blocked|not executed|danger|error|failed/.test(raw)) return "status-risk";
  return "";
}

function rowStatusClass(value) {
  return statusTone(value);
}

function statusPill(value, extra = "") {
  const key = String(value || "unknown").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
  return `<span class="status-pill config-${escapeHtml(key)} ${statusTone(value)} ${extra}">${escapeHtml(text(value))}</span>`;
}


function formatCloudRouteResult(result) {
  const diff = result.diff_text || "No current route diff available.";
  const sources = result.current_radio_settings?.sources || {};
  const sourceLines = Object.entries(sources).map(([name, value]) => {
    if (value?.ok) return `${name}: ok`;
    return `${name}: ${value?.error || "not available"}`;
  }).join("\n");
  return `${diff}\n\n--- read sources ---\n${sourceLines || "no probe sources"}\n\n--- debug ---\n${JSON.stringify(result, null, 2)}`;
}

function markRiskPanels() {
  const limitedNeedles = ["plan only", "manual only", "research", "candidate", "not fully", "lab", "bridge only"];
  document.querySelectorAll(".panel").forEach((panel) => {
    if (panel.classList.contains("danger-panel")) {
      panel.classList.add("feature-risk");
      return;
    }
    const sample = panel.textContent.toLowerCase();
    if (limitedNeedles.some((needle) => sample.includes(needle))) panel.classList.add("feature-limited");
  });
}

function renderRadioButtons(select) {
  if (!select) return;
  let picker = select.parentElement?.querySelector(":scope > .radio-button-picker");
  if (!picker) {
    picker = document.createElement("div");
    picker.className = "radio-button-picker";
    picker.setAttribute("role", "group");
    select.insertAdjacentElement("afterend", picker);
  }
  select.classList.add("radio-select-fallback");
  picker.innerHTML = Array.from(select.options).map((option) => {
    const device = state.devices.find((item) => item.device_id === option.value);
    const name = device ? text(device.name, device.device_id) : option.textContent;
    const detail = device ? text(device.ip_address) : "";
    return `<button class="radio-choice ${select.value === option.value ? "is-selected" : ""}" type="button" data-radio-value="${escapeHtml(option.value)}"><strong>${escapeHtml(name)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</button>`;
  }).join("");
  picker.querySelectorAll("[data-radio-value]").forEach((button) => button.addEventListener("click", () => {
    select.value = button.dataset.radioValue;
    picker.querySelectorAll(".radio-choice").forEach((item) => item.classList.toggle("is-selected", item === button));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }));
}

function renderAllRadioButtons() {
  document.querySelectorAll('select[name="device_id"], select[name="master_device_id"], select[name="source_device_id"], select[name="target_device_id"], #scenario-trigger-device').forEach(renderRadioButtons);
}

function renderDevices() {
  const isProtected = (d) => Boolean(d?.protected);
  const networkDevices = state.devices.filter((d) => !isProtected(d));
  const protectedBadge = (d) => isProtected(d) ? statusPill("GESCHÜTZT · kein Netzwerkzugriff", "bad") : "";
  const healthFor = (d) => state.deviceHealth.find((item) => item.device_id === d.device_id) || d.policy || {};
  const policyLine = (d) => {
    const h = healthFor(d);
    if (!h.device_id) return "";
    const safe = statusPill("gemeinsame Gerätepolicy");
    const circuit = statusPill(`Circuit ${text(h.circuit_state, "unbekannt")}`, h.circuit_state === "open" ? "pending" : h.circuit_state === "closed" ? "ready" : "");
    const next = h.next_planned_poll ? ` · nächste Prüfung ${escapeHtml(h.next_planned_poll)}` : "";
    const reason = h.last_skip_reason ? ` · ${escapeHtml(h.last_skip_reason)}` : "";
    return `<small class="policy-line easy-hidden">${safe}${circuit} · ${escapeHtml(text(h.suspected_state, "Zustand unbekannt"))} · Backoff ${escapeHtml(text(h.current_backoff_seconds, 0))}s${next}${reason}</small>`;
  };
  const badges = (d) => { const s = state.deviceStatuses.find((item) => item.device_id === d.device_id); if (!s) return `<div class="device-status-badges easy-hidden">${statusPill("Status unbekannt / noch nicht geprüft")}</div>`; const flag = (label, value, yes, no) => statusPill(`${label} ${value === null ? "unbekannt / noch nicht geprüft" : value ? yes : no}`, value === true ? "ready" : ""); const sshLabel = s.ssh === "available" ? "SSH verfügbar" : s.ssh === "unavailable" ? "SSH nicht verfügbar" : "SSH noch nicht geprüft"; return `<div class="device-status-badges easy-hidden">${statusPill(sshLabel, s.ssh === "available" ? "ready" : "")}${flag("Persistent SSH", s.persistent_ssh, "ja", "nein")}${flag("remote_services", s.remote_services, "aktiv", "fehlt")}${flag("Host Redirect", s.host_redirect, "ja", "nein")}</div>`; };
  const offlineLine = (d) => d.reachable === false ? `<small>offline · zuletzt gesehen ${escapeHtml(text(d.last_seen_at, "unbekannt"))}${d.offline_reason ? ` · ${escapeHtml(d.offline_reason)}` : ""}</small>` : "";
  const repairButton = (d) => !isProtected(d) && ["mixed", "bose", "other"].includes(d.configured_for) ? `<button class="command" data-view-jump="setup" type="button">Setup reparieren</button>` : "";
  const checkButton = (d) => isProtected(d) ? `<span class="protected-label">vollständig geschützt</span>` : `<button class="command" data-check-device="${escapeHtml(d.device_id)}" type="button">erneut prüfen</button>`;
  const rows = state.devices.map((d) => `<tr class="${isProtected(d) ? "protected-device" : ""}"><td>${escapeHtml(text(d.name))}${protectedBadge(d)}${badges(d)}${offlineLine(d)}${policyLine(d)}</td><td>${escapeHtml(text(d.ip_address))}</td><td>${escapeHtml(text(d.model))}</td><td>${escapeHtml(text(d.firmware))}</td><td>${statusPill(d.configured_for)}</td><td>${d.ready ? statusPill("bereit", "ready") : statusPill(d.reachable === false ? "offline" : "prüfen", "pending")}</td><td><strong>${escapeHtml(text(d.serial_number, d.radio_device_id))}</strong><small class="identifier-detail">ID ${escapeHtml(text(d.radio_device_id))}</small></td><td>${checkButton(d)}${repairButton(d)}${isProtected(d) ? `<span class="protected-label">Keine Schreibaktion</span>` : `<button class="command danger" data-remove-device="${escapeHtml(d.device_id)}" type="button">Entfernen</button>`}</td></tr>`).join("");
  document.getElementById("devices-table").innerHTML = rows || `<tr><td colspan="8" class="empty">No devices configured.</td></tr>`;
  document.getElementById("dashboard-devices").innerHTML = state.devices.length
      ? state.devices.map((d) => `<div class="list-row ${isProtected(d) ? "protected-device" : ""}"><strong>${escapeHtml(text(d.name, d.device_id))}</strong><span>${escapeHtml(text(d.ip_address))} · ${escapeHtml(text(d.model))} · ${escapeHtml(text(d.firmware))}</span><small>${escapeHtml(text(d.configured_for))} · ${d.ready ? "ready" : "pending"} ${isProtected(d) ? "· GESCHÜTZT · KEIN NETZWERKZUGRIFF" : ""}</small>${policyLine(d)}</div>`).join("")
    : `<div class="empty">No devices configured.</div>`;
  const readiness = document.getElementById("device-readiness");
  if (readiness) {
    readiness.innerHTML = state.devices.length
      ? state.devices.map((d) => `<div class="event-row"><span>${statusPill(d.configured_for)} ${d.ready ? statusPill("ready") : statusPill("pending")}</span><strong>${escapeHtml(text(d.name, d.device_id))}</strong><small>${escapeHtml(text(d.marge_url, "kein margeURL Cache"))}</small>${policyLine(d)}</div>`).join("")
      : `<div class="empty">Noch kein Radio angelegt.</div>`;
  }

  const presetSelect = document.getElementById("preset-device-select");
  const masterSelect = document.getElementById("multiroom-master");
  const settingsSelect = document.getElementById("settings-device-select");
  const keyDeviceSelect = document.getElementById("key-device-select");
  const displayDeviceSelect = document.getElementById("display-device-select");
  const displayRecoveryDeviceSelect = document.getElementById("display-recovery-device-select");
  const scheduleDeviceSelect = document.getElementById("schedule-device-select");
  const scheduleMasterSelect = document.getElementById("schedule-master-select");
  const renameSelect = document.getElementById("rename-device-select");
  const telemetrySelect = document.getElementById("telemetry-device-select");
  const radioLogDeviceSelect = document.getElementById("radio-log-device-select");
  const sshLogDeviceSelect = document.getElementById("ssh-log-device-select");
  const setupLiveTestDevice = document.getElementById("setup-live-test-device");
  const cloneSourceSelect = document.getElementById("clone-source-device");
  const cloneTargetSelect = document.getElementById("clone-target-device");
  const deviceInfoSelect = document.getElementById("device-info-select");
  const guidedSetupSelect = document.getElementById("guided-setup-device");
  const profileApplyDevice = document.getElementById("profile-apply-device");
  const recoveryDeviceSelect = document.getElementById("recovery-device-select");
  const recoveryResetDeviceSelect = document.getElementById("recovery-reset-device-select");
  const mediaDeviceSelect = document.getElementById("media-device-select");
  const stationPlayDevice = document.getElementById("station-play-device");
  const nativeStationDevice = document.getElementById("native-station-device");
  const nativeStationAddDevice = document.getElementById("native-station-add-device");
  const bassCapabilitiesDevice = document.getElementById("bass-capabilities-device");
  const sourceNameDevice = document.getElementById("source-name-device");
  const wirelessProfileDevice = document.getElementById("wireless-profile-device");
  const zoneStatusDevice = document.getElementById("zone-status-device");
  const cloudRouteDevice = document.getElementById("cloud-route-device");
  const setupWizardDevice = document.getElementById("setup-wizard-device");
  const backupDeviceSelect = document.getElementById("backup-device-select");
  const referenceSourceDevice = document.getElementById("reference-source-device");
  const referenceTargetDevice = document.getElementById("reference-target-device");
  const telnetDeviceSelect = document.getElementById("telnet-device-select");
  const batteryPatchDeviceSelect = document.getElementById("battery-patch-device-select");
  const standbyClockDeviceSelect = document.getElementById("standby-clock-device-select");
  const maintenanceRebootDevice = document.getElementById("maintenance-reboot-device");
  const scenarioMaster = document.getElementById("scenario-master");
  const scenarioTriggerDevice = document.getElementById("scenario-trigger-device");
  [presetSelect, masterSelect, settingsSelect, keyDeviceSelect, displayDeviceSelect, displayRecoveryDeviceSelect, scheduleDeviceSelect, renameSelect, telemetrySelect, radioLogDeviceSelect, sshLogDeviceSelect, setupLiveTestDevice, cloneSourceSelect, cloneTargetSelect, deviceInfoSelect, guidedSetupSelect, profileApplyDevice, recoveryDeviceSelect, recoveryResetDeviceSelect, mediaDeviceSelect, stationPlayDevice, nativeStationDevice, nativeStationAddDevice, bassCapabilitiesDevice, sourceNameDevice, wirelessProfileDevice, zoneStatusDevice, cloudRouteDevice, setupWizardDevice, backupDeviceSelect, referenceSourceDevice, referenceTargetDevice, telnetDeviceSelect, batteryPatchDeviceSelect, standbyClockDeviceSelect, maintenanceRebootDevice, scenarioMaster, scenarioTriggerDevice].forEach((select) => {
    if (!select) return;
    const previous = select.value;
    select.innerHTML = networkDevices.length
      ? networkDevices.map((d) => `<option value="${escapeHtml(d.device_id)}">${escapeHtml(text(d.name, d.device_id))} · ${escapeHtml(text(d.ip_address))}</option>`).join("")
      : `<option value="">No usable devices</option>`;
    if (previous && networkDevices.some((d) => d.device_id === previous)) select.value = previous;
  });
  if (scheduleMasterSelect) {
    const previous = scheduleMasterSelect.value;
    scheduleMasterSelect.innerHTML = `<option value="">Kein Multiroom</option>` + (networkDevices.length
      ? networkDevices.map((d) => `<option value="${escapeHtml(d.device_id)}">${escapeHtml(text(d.name, d.device_id))} · ${escapeHtml(text(d.ip_address))}</option>`).join("")
      : "");
    scheduleMasterSelect.value = previous || "";
  }
  const cards = document.getElementById("devices-cards");
  if (cards) {
    cards.innerHTML = state.devices.length
    ? state.devices.map((d) => `<article class="device-card ${isProtected(d) ? "protected-device" : ""}"><header><strong>${escapeHtml(text(d.name, d.device_id))}</strong>${protectedBadge(d)}${statusPill(d.reachable === false ? "offline" : d.configured_for)}</header><div>${escapeHtml(text(d.model))}</div><small>${escapeHtml(text(d.ip_address))} · Firmware ${escapeHtml(text(d.firmware))}</small><small>Seriennummer ${escapeHtml(text(d.serial_number, "nicht ausgelesen"))}</small>${offlineLine(d)}${badges(d)}${policyLine(d)}<code>${escapeHtml(text(d.radio_device_id, d.device_id))}</code><div class="button-row">${isProtected(d) ? "" : `<a class="command remote-link" href="/remote/${encodeURIComponent(d.device_id)}">Fernbedienung öffnen</a>`}${checkButton(d)}${repairButton(d)}${isProtected(d) ? "" : `<button class="command danger" data-remove-device="${escapeHtml(d.device_id)}" type="button">Entfernen</button>`}</div></article>`).join("")
      : `<div class="empty">No devices configured.</div>`;
  }
  renderMultiroomMembers();
  renderScheduleMembers();
  renderScenarioMembers();
  syncResearchHealthDeviceSelect();
  renderAllRadioButtons();
  syncSafeStartControl();
}

function syncResearchHealthDeviceSelect() {
  const select = document.getElementById("health-device-select");
  if (!select) return;
  const previous = select.value || state.researchHealth.deviceId;
  const available = state.devices.filter((device) => !device.protected);
  select.innerHTML = available.length
    ? available.map((device) => `<option value="${escapeHtml(device.device_id)}">${escapeHtml(text(device.name, device.device_id))} · ${escapeHtml(text(device.ip_address))}</option>`).join("")
    : `<option value="">Noch kein Radio vorhanden</option>`;
  if (previous && available.some((device) => device.device_id === previous)) select.value = previous;
  state.researchHealth.deviceId = select.value || "";
}

function researchTone(value) {
  const key = String(value || "UNKNOWN").toUpperCase();
  if (["HEALTHY", "PLAYING", "PAUSED", "SUCCESS", "RECOVERED", "CURRENT", "READY", "PRESENT"].includes(key)) return "ready";
  if (["FAILED", "SOURCE_INVALID", "SERVICE_UNAVAILABLE", "NOT_READY", "STALE", "ERROR"].includes(key)) return "bad";
  return "pending";
}

function setResearchBadge(id, value) {
  const node = document.getElementById(id);
  if (!node) return;
  const label = text(value, "UNKNOWN");
  node.className = `status-pill status-${researchTone(label)}`;
  node.textContent = label;
}

function researchValue(label, value, detail = "") {
  return `<div class="event-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(text(value, "Unbekannt"))}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</div>`;
}

function formatObservedAt(value) {
  if (!value) return "noch nicht beobachtet";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}

function renderResearchHealth() {
  const health = state.researchHealth;
  const playback = health.playback || { state: "UNKNOWN" };
  const provider = health.provider || { state: "UNKNOWN", providers: [] };
  const metadata = health.metadata || { state: "UNKNOWN", stale: true };
  const restrictions = health.restrictions || { state: "ABSENT", restrictions: [] };
  const reporting = health.reporting || { state: "UNKNOWN", providers: [] };
  const airplay = health.airplay || { state: "UNKNOWN", label: "Unbekannt", blocking_stage: "UNKNOWN" };
  const timeline = health.timeline || { items: [] };

  const overview = document.getElementById("research-health-overview");
  if (overview) {
    const cards = [
      ["Playback", playback.state], ["Provider", provider.state], ["Metadaten", metadata.state],
      ["Reporting", reporting.state], ["AirPlay 2", airplay.label || airplay.state],
    ];
    overview.innerHTML = health.deviceId
      ? cards.map(([label, value]) => `<article class="metric research-health-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(text(value, "UNKNOWN"))}</strong><small>${escapeHtml(formatObservedAt(label === "AirPlay 2" ? airplay.observed_at : ({ Playback: playback, Provider: provider, Metadaten: metadata, Reporting: reporting }[label] || {}).observed_at))}</small></article>`).join("")
      : `<div class="empty">Radio wählen, um die gespeicherten Zustände zu laden.</div>`;
  }

  setResearchBadge("playback-health-badge", playback.state);
  const playbackBox = document.getElementById("playback-health-details");
  if (playbackBox) playbackBox.innerHTML = [
    researchValue("Grund", playback.reason || "Noch kein autoritativer Radio-Readback"),
    researchValue("Quelle gültig", playback.source_valid === null || playback.source_valid === undefined ? "Unbekannt" : playback.source_valid ? "Ja" : "Nein"),
    researchValue("Streamsignal", playback.stream_alive === null || playback.stream_alive === undefined ? "nur sekundär / unbekannt" : playback.stream_alive ? "erreichbar (sekundär)" : "nicht erreichbar"),
    researchValue("Recovery-Stufe", playback.recovery_stage ?? 0, "0 bedeutet reiner Readback"),
  ].join("");

  setResearchBadge("provider-health-badge", provider.state);
  const providerBox = document.getElementById("provider-health-details");
  if (providerBox) providerBox.innerHTML = (provider.providers || []).length
    ? provider.providers.map((item) => researchValue(item.provider_id, item.state, item.user_visible_reason || item.cause || "Keine Störung gemeldet")).join("")
    : `<div class="empty">Noch keine Provider-Beobachtung gespeichert.</div>`;

  setResearchBadge("metadata-health-badge", metadata.state);
  const metadataBox = document.getElementById("metadata-health-details");
  if (metadataBox) metadataBox.innerHTML = [
    researchValue("Sender", metadata.station_name),
    researchValue("Titel", metadata.track),
    researchValue("Interpret · Album", [metadata.artist, metadata.album].filter(Boolean).join(" · ") || null),
    researchValue("Herkunft", metadata.provenance, `Konfidenz ${metadata.confidence ?? 0}% · ${formatObservedAt(metadata.observed_at)}`),
  ].join("");
  const metadataForm = document.getElementById("live-metadata-form");
  if (metadataForm && !metadataForm.contains(document.activeElement)) {
    metadataForm.elements.track.value = metadata.track || "";
    metadataForm.elements.artist.value = metadata.artist || "";
    metadataForm.elements.album.value = metadata.album || "";
    metadataForm.elements.imageUrl.value = metadata.image_url || "";
  }
  const artwork = document.getElementById("metadata-artwork");
  if (artwork) {
    const candidate = String(health.artwork?.public_url || "");
    // Provider and station URLs are resolved server-side. The browser only
    // receives a same-origin cache endpoint or a shipped static icon.
    artwork.src = candidate.startsWith("/api/artwork-cache/") || candidate.startsWith("/static/") ? candidate : "/static/bmx-icons/orion/monochrome.svg";
    artwork.alt = metadata.station_name ? `Artwork für ${metadata.station_name}` : "Quellen-Symbol als Artwork-Fallback";
  }

  setResearchBadge("restrictions-health-badge", restrictions.state);
  const restrictionBox = document.getElementById("restrictions-health-details");
  if (restrictionBox) restrictionBox.innerHTML = (restrictions.restrictions || []).length
    ? restrictions.restrictions.map((item) => researchValue(item.source_key, item.timer_enabled ? `${item.inactivity_timeout} Sekunden` : "deaktiviert", `empfangen ${formatObservedAt(item.received_at)}`)).join("")
    : `<div class="empty">Keine Restriction empfangen; Inaktivitätstimer ist nicht aktiv.</div>`;

  setResearchBadge("reporting-health-badge", reporting.state);
  const reportingBox = document.getElementById("reporting-health-details");
  if (reportingBox) reportingBox.innerHTML = (reporting.providers || []).length
    ? reporting.providers.map((item) => researchValue(item.provider_id, item.state, `Queue ${item.queue_depth ?? 0}/20 · Retry ${item.retry_count ?? 0}/5 · fällig ${formatObservedAt(item.next_due_at)}`)).join("")
    : `<div class="empty">Noch kein Reporting-Vertrag beobachtet.</div>`;

  setResearchBadge("airplay-health-badge", airplay.label || airplay.state);
  const airplaySummary = document.getElementById("airplay-health-summary");
  if (airplaySummary) airplaySummary.innerHTML = [
    researchValue("Ergebnis", airplay.label || "Unbekannt", airplay.blocking_stage && airplay.blocking_stage !== "NONE" ? `Blockierende Stufe: ${airplay.blocking_stage}` : "Kein blockierendes Gate beobachtet"),
    researchValue("Profil", [airplay.firmware_version, airplay.product_id, airplay.variant].filter(Boolean).join(" · ") || null, `Konfidenz ${airplay.confidence ?? 0}%`),
    researchValue("Evidence", airplay.provenance || "UNKNOWN", airplay.expires_at ? `Runtime-Evidence gültig bis ${formatObservedAt(airplay.expires_at)}` : "Keine flüchtige Runtime-Evidence gespeichert"),
  ].join("");
  const airplayProbe = document.getElementById("airplay-readonly-probe");
  if (airplayProbe) airplayProbe.disabled = !state.researchHealth.deviceId;
  const gateBox = document.getElementById("airplay-health-gates");
  if (gateBox) {
    const gates = [
      ["Product", airplay.product_allowed], ["Auth Hardware", airplay.auth_hardware_detected],
      ["STS", airplay.sts_registered], ["Source", airplay.source_visible], ["mDNS", airplay.mdns_visible],
      ["Pairing", airplay.pairing_ready], ["PTP", airplay.ptp_ready], ["Audio", airplay.audio_ready],
    ];
    gateBox.innerHTML = gates.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${value === true ? "bereit" : value === false ? "blockiert" : "unbekannt"}</strong></div>`).join("");
  }

  const clock = health.clock || { enabled: false, mode: "MISSING_TITLE", interval_seconds: 60 };
  const clockEnabled = document.getElementById("clock-metadata-enabled");
  const clockMode = document.getElementById("clock-metadata-mode");
  const clockInterval = document.getElementById("clock-metadata-interval");
  if (clockEnabled) clockEnabled.checked = clock.enabled === true;
  if (clockMode) clockMode.value = clock.mode || "MISSING_TITLE";
  if (clockInterval) clockInterval.value = Math.max(60, Number(clock.interval_seconds || 60));

  const timelineBox = document.getElementById("diagnostics-timeline");
  const items = timeline.items || [];
  if (timelineBox) timelineBox.innerHTML = items.length
    ? items.map((item) => `<article class="timeline-event severity-${escapeHtml(String(item.severity || "info").toLowerCase())}"><time datetime="${escapeHtml(item.occurred_at || "")}">${escapeHtml(formatObservedAt(item.occurred_at))}</time><strong>${escapeHtml(item.domain)} · ${escapeHtml(item.code)}</strong><span>${escapeHtml(item.message)}</span><small>Konfidenz ${escapeHtml(item.confidence ?? 0)}%${item.correlation_id ? ` · Korrelation ${escapeHtml(item.correlation_id)}` : ""}</small></article>`).join("")
    : `<div class="empty">Noch keine korrelierten Diagnoseereignisse gespeichert.</div>`;
  const timelineCount = document.getElementById("timeline-count");
  if (timelineCount) timelineCount.textContent = `${items.length} ${items.length === 1 ? "Ereignis" : "Ereignisse"}`;
}

async function loadResearchHealth() {
  const select = document.getElementById("health-device-select");
  const deviceId = select?.value || state.researchHealth.deviceId || "";
  state.researchHealth.deviceId = deviceId;
  if (!deviceId) {
    renderResearchHealth();
    return;
  }
  const path = `/api/devices/${encodeURIComponent(deviceId)}`;
  const safeLoad = async (suffix, fallback) => {
    try { return await getJson(`${path}/${suffix}`); }
    catch (error) { return { ...fallback, state: "ERROR", error: String(error) }; }
  };
  const [playback, provider, metadata, artwork, restrictions, reporting, airplay, timeline, clock] = await Promise.all([
    safeLoad("playback-health", {}), safeLoad("provider-health", { providers: [] }),
    safeLoad("metadata", { stale: true }), safeLoad("artwork", { public_url: "/static/bmx-icons/orion/monochrome.svg" }),
    safeLoad("restrictions", { restrictions: [] }),
    safeLoad("reporting", { providers: [] }), safeLoad("airplay-readiness", { label: "Unbekannt" }),
    safeLoad("diagnostics/timeline?limit=100", { items: [] }), safeLoad("metadata/clock", { enabled: false, mode: "MISSING_TITLE", interval_seconds: 60 }),
  ]);
  Object.assign(state.researchHealth, { playback, provider, metadata, artwork, restrictions, reporting, airplay, timeline, clock });
  renderResearchHealth();
}

async function probeAirplayReadiness() {
  const deviceId = document.getElementById("health-device-select")?.value || "";
  if (!deviceId) {
    showToast("Bitte zuerst ein Radio wählen.", "error");
    return;
  }
  const button = document.getElementById("airplay-readonly-probe");
  if (button) button.disabled = true;
  try {
    state.researchHealth.airplay = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/airplay-readiness/probe`, {});
    await loadResearchHealth();
    showToast("Read-only-AirPlay-Prüfung abgeschlossen.");
  } catch (error) {
    showApiError(error, "Read-only-AirPlay-Prüfung fehlgeschlagen");
    await loadResearchHealth();
  } finally {
    if (button) button.disabled = false;
  }
}

function renderProfileSlotInputs() {
  const box = document.getElementById("preset-profile-slots");
  if (!box) return;
  const options = `<option value="">Leer</option>` + state.stations.map((station) => `<option value="${station.id}">${escapeHtml(station.name)}</option>`).join("");
  box.innerHTML = [1, 2, 3, 4, 5, 6].map((slot) => `<label>Slot ${slot}<select id="profile-slot-${slot}" data-profile-slot="${slot}">${options}</select></label>`).join("");
}

function renderStations() {
  const tableStations = filteredStations(state.stationFilter);
  document.getElementById("stations-table").innerHTML = tableStations.length
    ? tableStations.map((s) => `<tr><td>${escapeHtml(text(s.name))}</td><td><code>${escapeHtml(text(s.stream_url))}</code></td><td><img class="station-logo-small" src="/api/stations/${encodeURIComponent(s.id)}/artwork/image" alt="Logo für ${escapeHtml(text(s.name))}" loading="lazy"></td><td><button class="command" data-play-station="${s.id}" type="button">Play</button></td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">No matching stations.</td></tr>`;

  const presetStations = filteredStations(state.presetFilter);
  const select = document.getElementById("preset-station-select");
  const selectedStation = select.value;
  select.innerHTML = presetStations.length
    ? presetStations.map((s) => `<option value="${s.id}">${escapeHtml(s.name)} · ${escapeHtml(s.stream_url)}</option>`).join("")
    : `<option value="">No matching stations</option>`;
  if (selectedStation && presetStations.some((station) => String(station.id) === selectedStation)) select.value = selectedStation;
  const scheduleSelect = document.getElementById("schedule-station-select");
  if (scheduleSelect) {
    scheduleSelect.innerHTML = state.stations.length
      ? `<option value="">Preset Slot verwenden</option>` + state.stations.map((s) => `<option value="${s.id}">${escapeHtml(s.name)} · ${escapeHtml(s.stream_url)}</option>`).join("")
      : `<option value="">No stations</option>`;
  }
  const stationPlaySelect = document.getElementById("station-play-select");
  if (stationPlaySelect) {
    const previous = stationPlaySelect.value;
    stationPlaySelect.innerHTML = state.stations.length
      ? state.stations.map((s) => `<option value="${s.id}">${escapeHtml(s.name)} · ${escapeHtml(s.stream_url)}</option>`).join("")
      : `<option value="">No stations</option>`;
    if (previous) stationPlaySelect.value = previous;
  }
  const displayDirectStation = document.getElementById("display-direct-station");
  if (displayDirectStation) {
    const previous = displayDirectStation.value;
    displayDirectStation.innerHTML = `<option value="">Current/probe only</option>` + state.stations.map((s) => `<option value="${s.id}">${escapeHtml(s.name)} · ${escapeHtml(s.stream_url)}</option>`).join("");
    displayDirectStation.value = previous || "";
  }
  const scenarioStation = document.getElementById("scenario-station");
  if (scenarioStation) {
    scenarioStation.innerHTML = `<option value="">No station</option>` + state.stations.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("");
  }
  const multiroomStation = document.getElementById("multiroom-station");
  if (multiroomStation) {
    const previous = multiroomStation.value;
    const recent = state.multiroomRecentStations.length ? state.multiroomRecentStations : state.stations.slice(0, 30);
    multiroomStation.innerHTML = `<option value="">Nur Radios verbinden</option>` + recent.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("");
    multiroomStation.value = previous || "";
  }
  const setupLiveTestStation = document.getElementById("setup-live-test-station");
  if (setupLiveTestStation) {
    const previous = setupLiveTestStation.value;
    setupLiveTestStation.innerHTML = `<option value="">No station playback</option>` + state.stations.map((s) => `<option value="${s.id}">${escapeHtml(s.name)} · ${escapeHtml(s.stream_url)}</option>`).join("");
    setupLiveTestStation.value = previous || "";
  }
  renderProfileSlotInputs();
}

function renderMediaTypes() {
  const box = document.getElementById("media-types-list");
  if (!box) return;
  box.innerHTML = state.mediaTypes.length
    ? state.mediaTypes.map((item) => `<article class="media-card ${escapeHtml(item.status)}"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.status)}</span><small>${escapeHtml(item.note)}</small><code>${escapeHtml((item.extensions || []).join(", ") || (item.mime_types || []).join(", ") || "no extension rule")}</code></article>`).join("")
    : `<div class="empty">Media type catalog not loaded.</div>`;
}

function renderPresetSlots() {
  const grid = document.getElementById("preset-slot-grid");
  if (!grid) return;
  const byButton = new Map(state.presets.map((preset) => [Number(preset.button), preset]));
  const statusByButton = new Map((state.presetStatus?.slots || []).map((slot) => [Number(slot.button), slot]));
  const selectedSlot = Number(document.querySelector('input[name="button"]:checked')?.value || 1);
  grid.innerHTML = [1, 2, 3, 4, 5, 6].map((slot) => {
    const preset = byButton.get(slot);
    const slotStatus = statusByButton.get(slot);
    const label = preset ? stationName(preset.station_id) : "Not set";
    const detail = preset ? preset.source : "Ready";
    const statusClass = slotStatus ? `preset-${slotStatus.state}` : "preset-unknown";
    const statusText = slotStatus ? `${slotStatus.state}: ${slotStatus.message}` : "not compared";
    const actions = preset ? `<div class="slot-card-actions"><button class="command slot-play" data-play-preset-slot="${slot}" type="button" title="Preset ${slot} starten">▶</button><button class="preset-delete" data-delete-preset="${slot}" type="button">Entfernen</button></div>` : "";
    return `<div class="slot-card ${statusClass} ${slot === selectedSlot ? "is-selected" : ""}"><button data-slot="${slot}" type="button" aria-pressed="${slot === selectedSlot}"><span>${slot}</span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small><small>${escapeHtml(statusText)}</small></button>${actions}</div>`;
  }).join("");
  grid.querySelectorAll("[data-slot]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.querySelector(`input[name="button"][value="${button.dataset.slot}"]`);
      if (input) input.checked = true;
      if (state.guidedPreset.active && button.dataset.slot === "1") {
        state.guidedPreset.step = "save";
        renderGuidedPresetSetup();
      }
      const preset = byButton.get(Number(button.dataset.slot));
      if (preset?.station_id) {
        state.presetFilter = "";
        renderStations();
        document.getElementById("preset-station-select").value = String(preset.station_id);
      }
      renderPresetSlots();
    });
  });
}

function renderMultiroomMembers() {
  const view = document.getElementById("view-multiroom");
  if (view) view.hidden = false;
  const box = document.getElementById("multiroom-members");
  if (!box) return;
  const selectedMembers = new Set(Array.from(box.querySelectorAll("input:checked")).map((node) => node.value));
  const masterId = document.getElementById("multiroom-master")?.value;
  box.innerHTML = state.devices.length
    ? state.devices.map((device) => `<label class="member-row ${device.protected ? "is-protected" : ""}"><input type="checkbox" value="${escapeHtml(device.device_id)}" ${selectedMembers.has(device.device_id) && device.device_id !== masterId && !device.protected ? "checked" : ""} ${device.device_id === masterId || device.protected ? "disabled" : ""}><span>${escapeHtml(text(device.name, device.device_id))}${device.protected ? " · GESCHÜTZT" : ""}</span><small>${escapeHtml(text(device.ip_address))}</small></label>`).join("")
    : `<div class="empty">Add at least two devices first.</div>`;
  const remove = document.getElementById("multiroom-remove-device");
  if (remove) {
    remove.innerHTML = state.devices.map((device) => `<button class="radio-choice ${state.multiroomRemoveDeviceId === device.device_id ? "is-selected" : ""}" type="button" data-multiroom-remove-device="${escapeHtml(device.device_id)}" ${device.protected ? "disabled" : ""}><strong>${escapeHtml(text(device.name, device.device_id))}</strong><small>${escapeHtml(device.ip_address)}${device.protected ? " · geschützt" : ""}</small></button>`).join("");
    remove.querySelectorAll("[data-multiroom-remove-device]").forEach((button) => button.addEventListener("click", () => {
      state.multiroomRemoveDeviceId = button.dataset.multiroomRemoveDevice;
      remove.querySelectorAll("[data-multiroom-remove-device]").forEach((item) => item.classList.toggle("is-selected", item.dataset.multiroomRemoveDevice === state.multiroomRemoveDeviceId));
    }));
  }
  renderMultiroomStartVolumes();
}

function renderMultiroomStartVolumes() {
  const enabled = document.getElementById("multiroom-start-volumes-enabled")?.checked;
  const box = document.getElementById("multiroom-start-volumes");
  if (!box) return;
  box.hidden = !enabled;
  if (!enabled) return;
  const ids = [
    document.getElementById("multiroom-master")?.value,
    ...Array.from(document.querySelectorAll("#multiroom-members input:checked")).map((node) => node.value),
  ].filter(Boolean);
  const oldValues = new Map(Array.from(box.querySelectorAll("input[data-start-volume]")).map((input) => [input.dataset.startVolume, input.value]));
  box.innerHTML = ids.map((deviceId) => {
    const device = state.devices.find((item) => item.device_id === deviceId);
    return `<label>${escapeHtml(text(device?.name, deviceId))}<input data-start-volume="${escapeHtml(deviceId)}" type="number" min="0" max="100" value="${escapeHtml(oldValues.get(deviceId) || "1")}"></label>`;
  }).join("");
}

function renderMultiroomMethods() {
  const box = document.getElementById("multiroom-methods");
  if (!box) return;
  const methods = Array.isArray(state.multiroomMethods) ? state.multiroomMethods : state.lastKnownMultiroomState.methods;
  box.innerHTML = methods.length
    ? methods.map((method) => `<article class="method-card ${method.recommended ? "is-recommended" : ""}"><span>${method.recommended ? "Empfohlen" : "Alternative / Information"}</span><h3>${escapeHtml(method.label)}</h3><strong>${escapeHtml(method.purpose)}</strong><p>${escapeHtml(method.detail)}</p><code>${escapeHtml(method.endpoint)}</code></article>`).join("")
    : `<div class="empty">Multiroom ist bereit. Methoden werden geladen.</div>`;
}

function renderFriendlyZone(result) {
  const zone = result.zone || {};
  const sources = result.sources || [];
  const allowed = sources.filter((item) => item.multiroom_allowed && item.status === "READY").map((item) => item.source);
  return `<div class="result-status ${zone.active ? "ok" : "neutral"}"><strong>${zone.active ? "Multiroom ist aktiv" : "Dieses Radio ist gerade allein"}</strong><span>${zone.active ? `Master: ${escapeHtml(zone.master_device_id)} · ${zone.members.length} weitere Radios` : "Keine Zone verbunden"}</span></div><dl class="friendly-details"><div><dt>Synchronisation</dt><dd>${escapeHtml(text(result.latency_mode, "unbekannt"))}</dd></div><div><dt>Geeignete aktive Quellen</dt><dd>${escapeHtml(allowed.join(", ") || "aktuell keine")}</dd></div></dl>`;
}


function renderScenarioMembers() {
  const box = document.getElementById("scenario-members");
  if (!box) return;
  const masterId = document.getElementById("scenario-master")?.value;
  box.innerHTML = state.devices.length
    ? state.devices.map((device) => `<label class="member-row ${device.protected ? "is-protected" : ""}"><input type="checkbox" value="${escapeHtml(device.device_id)}" ${device.device_id === masterId || device.protected ? "disabled" : ""}><span>${escapeHtml(text(device.name, device.device_id))}${device.protected ? " · GESCHÜTZT" : ""}</span><small>${escapeHtml(text(device.ip_address))}</small></label>`).join("")
    : `<div class="empty">Add devices first.</div>`;
}

function renderScheduleMembers() {
  const box = document.getElementById("schedule-members");
  if (!box) return;
  const masterId = document.getElementById("schedule-master-select")?.value;
  box.innerHTML = state.devices.length
    ? state.devices.map((device) => `<label class="member-row ${device.protected ? "is-protected" : ""}"><input type="checkbox" value="${escapeHtml(device.device_id)}" ${!masterId || device.device_id === masterId || device.protected ? "disabled" : ""}><span>${escapeHtml(text(device.name, device.device_id))}${device.protected ? " · GESCHÜTZT" : ""}</span><small>${escapeHtml(text(device.ip_address))}</small></label>`).join("")
    : `<div class="empty">Add devices first.</div>`;
}

function renderPlayback() {
  const historyBox = document.getElementById("dashboard-play-history");
  if (historyBox) {
    historyBox.innerHTML = state.playHistory.length
      ? state.playHistory.slice(0, 12).map((row) => {
          const zone = row.zone_member_ids?.length ? ` · Multiroom: ${row.zone_member_ids.join(", ")}` : "";
          const action = row.trigger === "stop" ? "Stop/Pause" : row.trigger?.startsWith("preset") ? "Preset Play" : row.trigger === "stream" ? "Stream Play" : "Play";
          const end = row.ended_at ? formatDuration(row.duration_seconds) : `läuft seit ${formatDuration(row.duration_seconds)}`;
          const source = row.station_display_name || row.station_name || row.stream_host || row.stream_url || row.source || "unbekannte Quelle";
          return `<div class="event-row"><span>${escapeHtml(row.started_at)} · ${escapeHtml(action)}</span><strong>${escapeHtml(text(row.device_name, row.device_id))} · ${escapeHtml(text(row.ip_address, ""))}</strong><small>${escapeHtml(source)} · ${escapeHtml(end)}${escapeHtml(zone)}</small></div>`;
        }).join("")
      : `<div class="empty">Noch keine Wiedergabe erfasst. Starte einen Stream oder ein Preset.</div>`;
  }
  const statsBox = document.getElementById("dashboard-play-stats");
  if (statsBox) {
    const today = state.playStats?.today || {};
    const devices = state.playStats?.by_device || [];
    const stations = state.playStats?.by_station || [];
    const active = state.playStats?.active || [];
    const todayHtml = `<div class="event-row"><span>Heute</span><strong>${escapeHtml(text(today.starts, 0))} Starts · ${escapeHtml(text(today.stops, 0))} Stops/Pauses</strong><small>${escapeHtml(text(today.errors, 0))} Playback-Fehler</small></div>`;
    const activeHtml = active.length ? active.map((item) => `<div class="event-row status-ready"><span>Jetzt aktiv</span><strong>${escapeHtml(text(item.device_name, item.device_id))} · ${escapeHtml(text(item.station_display_name, item.station))}</strong><small>${escapeHtml(formatDuration(item.seconds))}</small></div>`).join("") : "";
    const lifetime = state.playStats?.lifetime || {};
    const aggregate = state.playStats?.aggregate || {};
    const detailButton = (type, key, label, title, meta, extraClass = "") => `<button class="event-row stats-row ${extraClass}" type="button" data-stats-detail="${escapeHtml(type)}" data-stats-key="${escapeHtml(key)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(meta)}</small></button>`;
    const lifetimeHtml = detailButton("overview", "", "Lifetime", `${formatDuration((lifetime.total_seconds || 0))} · ${text(lifetime.total_plays, 0)} Plays`, `Heute ${text(aggregate.today_hours, 0)}h · Woche ${text(aggregate.week_hours, 0)}h · Monat ${text(aggregate.month_hours, 0)}h · Jahr ${text(aggregate.year_hours, 0)}h · Jahrzehnt ${text(aggregate.decade_hours, 0)}h · Gesamt ${text(aggregate.lifetime_hours, 0)}h`);
    const deviceHtml = devices.slice(0, 6).map((item) => detailButton("device", item.device_id, "Radio", text(item.device_name, item.device_id), `${item.plays} Plays · ${formatDuration(item.seconds || 0)} · Ø ${formatDuration(item.avg_session_seconds || 0)} · letzte Quelle: ${text(item.last_source, "unbekannt")}`)).join("");
    const stationHtml = stations.slice(0, 5).map((item) => detailButton("station", item.station_display_name || item.station, "Sender", item.station_display_name || item.station, `${item.plays} Plays · ${formatDuration(item.seconds)} · zuletzt ${text(item.last_played_at, "nie")}`)).join("");
    const presetHtml = (state.playStats?.top_presets || []).slice(0, 4).map((item) => detailButton("preset", String(item.preset_button), "Preset", `Preset ${text(item.preset_button)}`, `${text(item.plays, 0)} Plays · ${formatDuration(item.seconds || 0)}`)).join("");
    const triggerHtml = (state.playStats?.top_triggers || []).slice(0, 5).map((item) => detailButton("trigger", item.trigger_type, "Trigger", text(item.trigger_type), `${text(item.plays, 0)} Plays · ${formatDuration(item.seconds || 0)}`)).join("");
    const timerHtml = state.playStats?.timer ? detailButton("trigger", "timer", "Wecker Timer", `${text(state.playStats.timer.plays, 0)} Starts · ${text(state.playStats.timer.success_rate_percent, 0)}% Erfolg`, formatDuration(state.playStats.timer.seconds || 0)) : "";
    const server = state.playStats?.server || {};
    const serverHtml = detailButton("server", "", "Server", `${formatDuration(server.current_uptime_seconds || 0)} aktuell · ${text(server.restart_count, 0)} Starts`, `Gesamt ${text(server.total_runtime_hours, 0)}h · erster Start ${text(server.first_boot, "unbekannt")}`);
    statsBox.innerHTML = state.playHistory.length || state.playStats ? serverHtml + lifetimeHtml + todayHtml + activeHtml + deviceHtml + stationHtml + presetHtml + triggerHtml + timerHtml : `<div class="empty">Noch keine Wiedergabe erfasst. Starte einen Stream oder ein Preset.</div>`;
  }
  renderPlaybackStatsDetail();
}

function renderPlaybackStatsDetail() {
  const box = document.getElementById("dashboard-play-stats-detail");
  if (!box) return;
  const stats = state.playStats;
  if (!stats) {
    box.innerHTML = "";
    return;
  }
  const selected = state.statsDetail || { type: "overview", key: "" };
  const yearly = (stats.yearly || []).map((item) => `<div class="event-row"><span>${escapeHtml(item.year)}</span><strong>${escapeHtml(formatDuration(item.seconds || 0))}</strong><small>${escapeHtml(text(item.plays, 0))} Plays</small></div>`).join("");
  const linked = (stats.device_history?.linked || []).map((item) => `<div class="event-row"><span>verknüpft</span><strong>${escapeHtml(text(item.current_name, item.device_id))}</strong><small>${escapeHtml(text(item.device_id))} · ${escapeHtml((item.known_names || []).join(", ") || "kein alter Name")} · ${escapeHtml((item.known_ips || []).join(", ") || text(item.current_ip, "keine IP"))}</small></div>`).join("");
  const removed = (stats.device_history?.removed || []).map((item) => `<div class="event-row status-caution"><span>historisch</span><strong>${escapeHtml(text((item.known_names || [])[0], item.device_id))}</strong><small>${escapeHtml(text(item.device_id))} · zuletzt ${escapeHtml(text(item.last_seen, "unbekannt"))} · nicht mehr in Geräteverwaltung</small></div>`).join("");
  let title = "Langzeitübersicht";
  let specific = "";
  if (selected.type === "device") {
    const item = (stats.by_device || []).find((row) => row.device_id === selected.key);
    title = `Radio: ${text(item?.device_name, selected.key)}`;
    specific = item ? `<div class="event-row"><span>Details</span><strong>${escapeHtml(text(item.plays, 0))} Plays · ${escapeHtml(formatDuration(item.seconds || 0))}</strong><small>Erster Einsatz ${escapeHtml(text(item.first_usage, "unbekannt"))} · letzter Einsatz ${escapeHtml(text(item.last_played_at, "unbekannt"))} · aktueller Name ${escapeHtml(text(item.current_device_name, "-"))} · damals ${escapeHtml(text(item.device_name_snapshot, "-"))}</small></div>` : "";
  } else if (selected.type === "station") {
    const item = (stats.by_station || []).find((row) => row.station === selected.key);
    title = `Sender: ${text(selected.key)}`;
    specific = item ? `<div class="event-row"><span>Details</span><strong>${escapeHtml(text(item.plays, 0))} Plays · ${escapeHtml(formatDuration(item.seconds || 0))}</strong><small>zuletzt ${escapeHtml(text(item.last_played_at, "unbekannt"))}</small></div>` : "";
  } else if (selected.type === "preset") {
    const item = (stats.top_presets || []).find((row) => String(row.preset_button) === selected.key);
    title = `Preset ${escapeHtml(selected.key)}`;
    specific = item ? `<div class="event-row"><span>Details</span><strong>${escapeHtml(text(item.plays, 0))} Plays · ${escapeHtml(formatDuration(item.seconds || 0))}</strong><small>zuletzt ${escapeHtml(text(item.last_played_at, "unbekannt"))}</small></div>` : "";
  } else if (selected.type === "trigger") {
    const item = (stats.top_triggers || []).find((row) => row.trigger_type === selected.key);
    title = `Trigger: ${text(selected.key)}`;
    specific = item ? `<div class="event-row"><span>Details</span><strong>${escapeHtml(text(item.plays, 0))} Plays · ${escapeHtml(formatDuration(item.seconds || 0))}</strong></div>` : "";
  } else if (selected.type === "server") {
    const server = stats.server || {};
    title = "Server Statistik";
    specific = `<div class="event-row"><span>Runtime</span><strong>${escapeHtml(formatDuration(server.total_runtime_seconds || 0))} total</strong><small>aktueller Lauf ${escapeHtml(formatDuration(server.current_uptime_seconds || 0))} · Neustarts ${escapeHtml(text(server.restart_count, 0))}</small></div>`;
  }
  box.innerHTML = `<div class="stats-detail-head"><strong>${escapeHtml(title)}</strong><small>Auswertung über bis zu 20 Jahre gespeicherte Nutzung.</small></div>${specific}<div class="stats-detail-grid"><section><h4>Jahre</h4>${yearly || '<div class="empty">Noch keine Jahresdaten.</div>'}</section><section><h4>Verknüpfte Radios</h4>${linked || '<div class="empty">Keine verknüpften Radios.</div>'}</section><section><h4>Historische Radios</h4>${removed || '<div class="empty">Keine entfernten Radios.</div>'}</section></div>`;
}

function renderSystemHealth() {
  const box = document.getElementById("system-health");
  if (!box) return;
  const health = state.systemHealth;
  if (!health) {
    box.innerHTML = `<div class="empty">Healthcheck noch nicht geladen.</div>`;
    return;
  }
  const healthLabel = (value) => value === "green" ? "OK" : value === "red" ? "Problem" : "Hinweis";
  const healthClass = (value) => value === "green" ? "status-ready" : value === "red" ? "status-warning" : "status-caution";
  const checks = (health.checks || []).map((check) => `<div class="event-row ${healthClass(check.status)}"><span><i class="health-dot ${escapeHtml(check.status)}"></i>${escapeHtml(healthLabel(check.status))}</span><strong>${escapeHtml(check.name)}</strong><small>${escapeHtml(check.message || "")}</small></div>`).join("");
  box.innerHTML = `<div class="event-row ${healthClass(health.status)}"><span><i class="health-dot ${escapeHtml(health.status)}"></i>Release Health</span><strong>${escapeHtml(healthLabel(health.status))}</strong><small>${escapeHtml(health.summary || "")}</small></div>${checks}`;
}

function renderSchedules() {
  const box = document.getElementById("schedule-list");
  if (!box) return;
  box.innerHTML = state.schedules.length
    ? state.schedules.map((item) => {
        const station = item.preset_button ? `Preset ${item.preset_button}` : stationName(item.station_id);
        const dayLabel = scheduleDaysLabel(item.days);
        const deviceNames = (item.device_ids || []).map(deviceName).join(", ");
        const multiroom = item.multiroom_master_id ? `Multiroom: ${deviceName(item.multiroom_master_id)}${item.multiroom_member_ids?.length ? ` + ${item.multiroom_member_ids.map(deviceName).join(", ")}` : ""}` : "Multiroom: nein";
        const devices = deviceNames || (item.multiroom_master_id ? deviceName(item.multiroom_master_id) : "-");
        const mode = item.dry_run ? "Dry-Run" : "aktiv";
        const enabled = item.enabled ? "aktiviert" : "deaktiviert";
        const stopAction = ({ stop: "Stop", standby: "Standby", stop_standby: "Stop + Standby" })[item.stop_action || "stop"] || "Stop";
        return `<div class="event-row"><span>${escapeHtml(item.start_time)}-${escapeHtml(text(item.end_time, "kein Stop"))} · ${escapeHtml(dayLabel)} · ${enabled}</span><strong>${escapeHtml(text(item.name))} · ${escapeHtml(station)}</strong><small>Radio: ${escapeHtml(devices)} · Lautstärke ${escapeHtml(text(item.volume, "unverändert"))} · ${escapeHtml(multiroom)} · Stop-Aktion ${escapeHtml(stopAction)} · ${escapeHtml(mode)}</small><div class="button-row"><button class="command" data-schedule-trigger="${item.id}" type="button">Jetzt auslösen</button><button class="command" data-schedule-toggle="${item.id}" data-enabled="${item.enabled ? "false" : "true"}" type="button">${item.enabled ? "Deaktivieren" : "Aktivieren"}</button><button class="command danger" data-schedule-delete="${item.id}" type="button">Löschen</button></div></div>`;
      }).join("")
    : `<div class="empty">Noch keine Wecker Timer.</div>`;
}

function scheduleDaysLabel(value) {
  const key = text(value, "daily");
  const labels = { daily: "Täglich", weekdays: "Werktage", weekend: "Wochenende", once: "Einmalig", mon: "Mo", tue: "Di", wed: "Mi", thu: "Do", fri: "Fr", sat: "Sa", sun: "So" };
  if (labels[key]) return labels[key];
  return key.split(",").map((item) => labels[item.trim()] || item.trim()).filter(Boolean).join(", ");
}

function updateScheduleWeekdayControls() {
  const select = document.getElementById("schedule-days-select");
  const picker = document.getElementById("schedule-weekday-picker");
  if (!select || !picker) return;
  const custom = select.value === "custom";
  picker.classList.toggle("is-disabled", !custom);
  picker.querySelectorAll('input[name="weekday"]').forEach((input) => {
    input.disabled = !custom;
    if (!custom) input.checked = false;
  });
}


function renderTelemetry() {
  const summaryBox = document.getElementById("telemetry-summary");
  if (summaryBox) {
    const summary = state.telemetrySummary || { total: 0, by_type: {}, by_device: {} };
    const logPurpose = (key) => key.startsWith("radio_log_http") ? "HTTP/XML-Zustand des Radios – Quellen, Presets, Netzwerk und Einstellungen" : key.startsWith("radio_log_cli17000") ? "Interne CLI-Diagnose – Routing und Systemkonfiguration" : key.startsWith("radio_log_ssh") ? "Erweiterte read-only Systemlogs – nur wenn SSH verfügbar ist" : key === "probe" ? "Gezielte, lesende Abfrage eines einzelnen Endpunkts" : "Geräte- oder BASSWIESN-Ereignis";
    const typeRows = Object.entries(summary.by_type || {}).map(([key, value]) => `<div class="event-row"><span>${value} Einträge</span><strong>${escapeHtml(key)}</strong><small>${escapeHtml(logPurpose(key))}</small></div>`).join("");
    const deviceRows = Object.entries(summary.by_device || {}).map(([key, value]) => `<div class="event-row"><span>Device</span><strong>${escapeHtml(key)}</strong><small>${value} events</small></div>`).join("");
    summaryBox.innerHTML = `<div class="event-row"><span>Zusammenfassung</span><strong>${summary.total || 0} Diagnoseeinträge</strong><small>Maximal die letzten 1000 Einträge. HTTP zeigt Radiozustände, CLI interne Werte und SSH Betriebssystemlogs.</small></div>` + typeRows + deviceRows;
  }
  const eventsBox = document.getElementById("telemetry-events");
  if (eventsBox) {
    eventsBox.innerHTML = state.telemetry.length
      ? state.telemetry.map((row) => { const raw = document.getElementById("telemetry-debug-toggle")?.checked ? `<pre>${escapeHtml(row.payload || "")}</pre>` : ""; return `<div class="event-row"><span>${escapeHtml(row.ts)} · ${escapeHtml(text(row.event_type))}</span><strong>${escapeHtml(text(row.device_id))} ${escapeHtml(text(row.endpoint))}</strong><small>${escapeHtml(text(row.parsed_summary))}</small>${raw}</div>`; }).join("")
      : `<div class="empty">No telemetry captured yet.</div>`;
  }
  const analysisBox = document.getElementById("telemetry-analysis");
  if (analysisBox) {
    const analysis = state.telemetryAnalysis;
    if (!analysis) {
      analysisBox.innerHTML = `<div class="empty">Noch keine Analyse geladen.</div>`;
    } else {
      const cloud = analysis.cloud_requests || {};
      const heartbeat = analysis.heartbeat_analysis || {};
      const errors = Object.entries(analysis.error_groups || {}).map(([key, value]) => `<div class="event-row ${value ? "status-warning" : ""}"><span>${escapeHtml(value)}</span><strong>${escapeHtml(key)}</strong></div>`).join("");
      const recs = (analysis.recommendations || []).map((item) => `<div class="event-row status-warning"><span>Empfehlung</span><strong>${escapeHtml(item)}</strong></div>`).join("");
      const volumes = (analysis.volume_safety || []).map((item) => `<div class="event-row ${item.violations ? "status-warning" : "status-ready"}"><span>${escapeHtml(item.device_id)}</span><strong>max ${escapeHtml(text(item.max_volume, "unknown"))}</strong><small>${escapeHtml(item.recommendation)}</small></div>`).join("");
      const protection = (analysis.playback_protection || []).map((item) => `<div class="event-row ${["invalid_source_diagnosis_required", "restriction_expired_observed", "readback_warning"].includes(item.playback_observation_status) ? "status-warning" : item.playback_observation_status === "playing_observed" ? "status-ready" : ""}"><span>${escapeHtml(item.device_id)} · ${item.currently_playing ? "spielt" : "nicht spielend"}</span><strong>${escapeHtml(text(item.playback_observation_status, "unknown"))}</strong><small>letzte Wiedergabe ${escapeHtml(text(item.last_seen_playback, "unbekannt"))} · längste Wiedergabe ${escapeHtml(formatDuration(item.longest_playback_seconds || 0))} · letzter Readback ${escapeHtml(text(item.last_keepalive, "unbekannt"))}<br>${escapeHtml(item.recommendation || "")}</small></div>`).join("");
      analysisBox.innerHTML = `<div class="event-row"><span>Cloud Requests</span><strong>${escapeHtml(text(cloud.total, 0))} total · ${escapeHtml(text(cloud.unknown_requests, 0))} unknown · ${escapeHtml(text(cloud.error_requests, 0))} Fehler</strong><small>Top Pfade: ${escapeHtml((cloud.top_paths || []).slice(0, 5).map((item) => `${item[0]} (${item[1]})`).join(", ") || "-")}</small></div><div class="event-row ${heartbeat.six_hour_gap_candidate ? "status-warning" : ""}"><span>6h Analyse</span><strong>Längste Lücke: ${escapeHtml(formatDuration(heartbeat.longest_gap_seconds || 0))}</strong><small>Letzte erfolgreiche Antwort: ${escapeHtml(text(heartbeat.last_successful_response, "unbekannt"))} · power_on ${escapeHtml(text(heartbeat.power_on_events, 0))} · account full ${escapeHtml(text(heartbeat.account_sync_events, 0))} · provider_settings ${escapeHtml(text(heartbeat.provider_settings_requests, 0))}</small></div><div class="event-row"><span>6h Protection</span><strong>${(analysis.playback_protection || []).length} Radios</strong></div>${protection}${errors}${recs}<div class="event-row"><span>Volume Safety</span><strong>${(analysis.volume_safety || []).length} Radios</strong></div>${volumes}`;
    }
  }
  const gapsBox = document.getElementById("emulation-gaps");
  if (gapsBox) {
    const gaps = state.emulationGaps;
    gapsBox.innerHTML = gaps
      ? `<div class="event-row ${gaps.status === "ok" ? "status-ready" : "status-warning"}"><span>Status</span><strong>${escapeHtml(gaps.status)}</strong><small>${escapeHtml((gaps.unknown_routes || []).length)} unknown · ${escapeHtml((gaps.frequent_404 || []).length)} 404</small></div>` + (gaps.recommendations || []).map((item) => `<div class="event-row"><span>${escapeHtml(item.path || "")}</span><strong>${escapeHtml(item.recommendation || item)}</strong></div>`).join("")
      : `<div class="empty">Noch keine Gap-Analyse geladen.</div>`;
  }
  const storageBox = document.getElementById("storage-cleanup");
  if (storageBox) {
    const storage = state.storageSummary;
    const cleanup = state.cleanupPreview;
    storageBox.innerHTML = storage
      ? `<div class="event-row"><span>Datenbank</span><strong>${escapeHtml(storage.db_size_mb)} MB</strong><small>${escapeHtml(storage.request_log_count)} Requests · ${escapeHtml(storage.telemetry_count)} Telemetry · ${escapeHtml(storage.config_backup_count)} Backups</small></div>` + (cleanup ? `<div class="event-row"><span>Cleanup Vorschau</span><strong>${escapeHtml(cleanup.request_logs)} Requests · ${escapeHtml(cleanup.telemetry_events)} Telemetry · ${escapeHtml(cleanup.config_backups)} Backups</strong><small>Dry-run löscht nichts.</small></div>` : "")
      : `<div class="empty">Speicherstatus noch nicht geladen.</div>`;
  }
}

function renderPresetProfiles() {
  const select = document.getElementById("preset-profile-select");
  if (select) {
    const previous = select.value;
    select.innerHTML = state.presetProfiles.length
      ? state.presetProfiles.map((profile) => `<option value="${profile.id}">${escapeHtml(profile.name)}</option>`).join("")
      : `<option value="">Noch keine Profile</option>`;
    if (previous) select.value = previous;
  }
  const list = document.getElementById("preset-profile-list");
  if (list) {
    list.innerHTML = state.presetProfiles.length
      ? state.presetProfiles.map((profile) => {
          const filled = (profile.slots || []).filter((slot) => slot.station_id).length;
          return `<div class="event-row"><span>${filled}/6 Slots · ${escapeHtml(text(profile.updated_at))}</span><strong>${escapeHtml(profile.name)}</strong><small>${escapeHtml(text(profile.description, "keine Beschreibung"))}</small></div>`;
        }).join("")
      : `<div class="empty">Noch kein 6er Preset-Profil gespeichert.</div>`;
  }
}

function renderSystemSettings() {
  const settings = state.systemSettings;
  if (!settings) return;
  const lanHost = document.getElementById("system-lan-host");
  if (lanHost) lanHost.value = settings.lan_host || "";
  const tzSelects = [document.getElementById("system-timezone-select"), document.getElementById("device-timezone-select")];
  tzSelects.forEach((select) => {
    if (!select) return;
    const previous = select.value || settings.default_timezone;
    select.innerHTML = (settings.timezones || []).map((tz) => `<option value="${escapeHtml(tz)}">${escapeHtml(tz)}</option>`).join("");
    select.value = previous;
  });
  const defaultLang = document.getElementById("default-device-language-select");
  if (defaultLang) {
    defaultLang.innerHTML = (settings.device_languages || []).map((lang) => `<option value="${escapeHtml(lang.code)}">${escapeHtml(lang.label)}</option>`).join("");
    defaultLang.value = settings.device_language_default || "en";
  }
  const webLang = document.getElementById("web-language-select");
  if (webLang) {
    webLang.innerHTML = (settings.web_languages || []).map((lang) => `<option value="${escapeHtml(lang.code)}">${escapeHtml(lang.label)}</option>`).join("");
    webLang.value = settings.web_language || "en";
  }
  const deviceLangSelect = document.getElementById("device-language-select");
  if (deviceLangSelect) {
    const previous = deviceLangSelect.value || settings.device_language_default || "en";
    deviceLangSelect.innerHTML = (settings.device_languages || []).map((lang) => `<option value="${escapeHtml(lang.code)}">${escapeHtml(lang.label)}</option>`).join("");
    deviceLangSelect.value = previous;
  }
  const displayMode = document.getElementById("display-metadata-mode");
  if (displayMode) displayMode.value = settings.display_metadata_mode || "station_clock";
  const firstRunWarning = document.getElementById("first-run-warning-required");
  if (firstRunWarning) firstRunWarning.value = settings.first_run_warning_required || "true";
  const mode = ["easy", "standard", "lab"].includes(settings.ui_mode) ? settings.ui_mode : (settings.lab_mode === "true" ? "lab" : "standard");
  const modeSetting = document.getElementById("ui-mode-setting");
  const modeSwitch = document.getElementById("ui-mode-switch");
  if (modeSetting) modeSetting.value = mode;
  if (modeSwitch) modeSwitch.value = mode;
  document.getElementById("lab-mode").checked = mode === "lab";
  document.getElementById("guided-hints").checked = settings.guided_hints !== "false";
  document.getElementById("show-startup-warning").checked = settings.show_startup_warning !== "false";
  document.getElementById("ip-write-guard").checked = settings.ip_write_guard === "true";
  let allowedIps = document.getElementById("ip-write-allowed-ips");
  if (!allowedIps) {
    const label = document.createElement("label");
    label.innerHTML = 'Erlaubte Radio-IPs<input id="ip-write-allowed-ips" name="ip_write_allowed_ips" placeholder="Radio-IP, weitere Radio-IP"><small>Nur aktiv, wenn IP Write Guard eingeschaltet ist.</small>';
    document.getElementById("ip-write-guard")?.closest("label")?.after(label);
    allowedIps = label.querySelector("input");
  }
  if (allowedIps) allowedIps.value = settings.ip_write_allowed_ips || "";
  const protectedIps = document.getElementById("protected-device-ips");
  if (protectedIps) protectedIps.value = settings.protected_device_ips || settings.effective_protected_device_ips || "";
  const protectedIds = document.getElementById("protected-device-ids");
  if (protectedIds) protectedIds.value = settings.protected_device_ids || settings.effective_protected_device_ids || "";
  const updateEnabled = document.getElementById("update-check-enabled");
  if (updateEnabled) updateEnabled.checked = settings.update_check_enabled === "true";
  const manifestUrl = document.getElementById("update-manifest-url");
  if (manifestUrl) manifestUrl.value = settings.update_manifest_url || "";
  const repoUrl = document.getElementById("update-repo-url");
  if (repoUrl) repoUrl.value = settings.update_repo_url || "";
  const updateChannel = document.getElementById("update-channel");
  if (updateChannel) updateChannel.value = settings.update_channel || "manual";
  const offlineMode = document.getElementById("offline-mode");
  if (offlineMode) offlineMode.value = settings.offline_mode || "auto";
  const offlineHosts = document.getElementById("offline-allowed-stream-hosts");
  if (offlineHosts) offlineHosts.value = settings.offline_allowed_stream_hosts || "";
  const offlineStatus = document.getElementById("offline-status");
  if (offlineStatus) {
    const status = state.offlineStatus || {};
    offlineStatus.textContent = `${text(status.status, "Offline-Status unbekannt")} · ${text((status.dependencies || []).length, 0)} externe Abhängigkeiten`;
  }
  document.getElementById("safe-startup-volume").value = settings.safe_startup_volume ?? 30;
  const safeVolume = document.getElementById("key-safe-volume");
  if (safeVolume) safeVolume.value = settings.safe_startup_volume ?? 30;
  const recoveryVolume = document.getElementById("recovery-safe-volume");
  if (recoveryVolume) recoveryVolume.value = settings.safe_startup_volume ?? 30;
  updateServerIdentity();
  applyUiPreferences();
}

function i18nT(key) { return window.BasswiesnI18n?.t(key) || key; }
function i18nPhraseT(value) { return window.BasswiesnI18n?.phrase(value) || value; }
function i18nLangT(language, key) { return window.BasswiesnI18n?.catalogs?.[language]?.[key] || window.BasswiesnI18n?.catalogs?.en?.[key] || key; }

const ABOUT_COPY = {
  de: {
    kicker: "Entwicklung",
    title: "Über BASSWIESN",
    heading: "SoundTouch soll lokal weiterleben",
    paragraphs: [
      "BASSWIESN entstand aus weit mehr als 400 Stunden Reverse Engineering, Disassembly, Firmwareanalyse und Tests an meinen eigenen Bose SoundTouch Radios.",
      "Das Ziel war nie, die alte Bose-App einfach zu kopieren. Das Ziel war eine Software, die lokal funktioniert, nachvollziehbar arbeitet, endnutzerfreundlich ist und sicher prüft, bevor sie Erfolg meldet.",
      "BASSWIESN soll bekannte Abläufe verständlicher machen und SoundTouch-Geräte auch ohne Hersteller-Cloud sinnvoll weiter nutzbar halten.",
      "Ich liebe diese Radios. Ich besitze selbst viele davon und habe sie über Jahre hinweg Freunden, Familie und Bekannten empfohlen.",
      "Dann kam der Mai 2026. Die Bose-Cloud wurde abgeschaltet. Plötzlich fragten mich viele, warum diese teuren Radios nicht mehr funktionieren.",
      "Danach begann das Reverse Engineering: probieren, testen, analysieren. Aus diesem Prozess entstand nach und nach BASSWIESN.",
      "Mittlerweile läuft BASSWIESN auf rund 30 Raspberry Pi 5 im Freundes- und Bekanntenkreis. Die Geräte laufen einfach weiter, und größere kritische Fehler sind mir derzeit nicht bekannt.",
      "Für meinen Einsatz funktioniert BASSWIESN zuverlässig. Ich veröffentliche dieses Release, weil Entwickler vielleicht Teile daraus weiterentwickeln oder in eigene Projekte übernehmen möchten. Eine Erwähnung reicht mir völlig.",
      "Danke. Grüße aus Bayern, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Version", "", "Firmware", "27.0.x", "Verifizierte Geräte", "Arbeitsgrundsätze"],
    principles: ["sichere Abläufe", "niedrige Testlautstärke", "Rücklesen vor Erfolgsmeldungen", "kein Blindvertrauen in Befehle"],
    backups: "Backup und Restore sind verfügbar; die Oberfläche zeigt den jeweils nachgewiesenen Umfang.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  en: {
    kicker: "Development",
    title: "About BASSWIESN",
    heading: "SoundTouch should keep living locally",
    paragraphs: [
      "BASSWIESN grew out of more than 400 hours of reverse engineering, disassembly, firmware analysis and tests on my own Bose SoundTouch radios.",
      "The goal was never to simply copy the old Bose app. The goal was software that works locally, is understandable, is friendly for end users and verifies safely before reporting success.",
      "BASSWIESN should make known workflows easier to understand and keep SoundTouch devices useful without the manufacturer cloud.",
      "I love these radios. I own many of them and recommended them to friends, family and acquaintances for years.",
      "Then May 2026 arrived. The Bose cloud was shut down, and people suddenly asked why these expensive radios no longer worked.",
      "After that came reverse engineering: trying, testing and analyzing. BASSWIESN gradually grew out of that process.",
      "Today BASSWIESN runs on about 30 Raspberry Pi 5 systems among friends and acquaintances. The devices simply keep running, and I am not aware of major critical errors at the moment.",
      "For my use, BASSWIESN works reliably. I am publishing this release because other developers may want to evolve parts of it or reuse them in their own projects. A mention is enough for me.",
      "Thank you. Greetings from Bavaria, Mathias Zimmermann."
    ],
    project: "Project",
    facts: ["Version", "", "Firmware", "27.0.x", "Verified devices", "Working principles"],
    principles: ["safe workflows", "low test volume", "read back before reporting success", "no blind trust in commands"],
    backups: "Backup and restore are available; the interface shows the exact scope that can be verified.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  fr: {
    kicker: "Développement",
    title: "À propos de BASSWIESN",
    heading: "SoundTouch doit continuer à vivre en local",
    paragraphs: [
      "BASSWIESN est né de plus de 400 heures de rétro-ingénierie, de désassemblage, d'analyse de firmware et de tests sur mes propres radios Bose SoundTouch.",
      "Le but n'a jamais été de copier simplement l'ancienne application Bose. Le but était un logiciel local, compréhensible, accessible aux utilisateurs et capable de vérifier prudemment avant d'annoncer un succès.",
      "BASSWIESN doit rendre les procédures connues plus claires et garder les appareils SoundTouch utiles sans le cloud du fabricant.",
      "J'aime ces radios. J'en possède beaucoup et je les ai recommandées pendant des années à des amis, à ma famille et à des connaissances.",
      "Puis mai 2026 est arrivé. Le cloud Bose a été arrêté, et beaucoup de personnes m'ont demandé pourquoi ces radios coûteuses ne fonctionnaient plus.",
      "Ensuite a commencé la rétro-ingénierie: essayer, tester, analyser. BASSWIESN est né progressivement de ce processus.",
      "Aujourd'hui, BASSWIESN fonctionne sur environ 30 Raspberry Pi 5 chez des amis et des connaissances. Les appareils continuent simplement à fonctionner, et je ne connais actuellement aucun problème critique majeur.",
      "Pour mon usage, BASSWIESN fonctionne de manière fiable. Je publie cette version parce que d'autres développeurs pourront peut-être faire évoluer certaines parties ou les réutiliser dans leurs propres projets. Une mention me suffit.",
      "Merci. Salutations de Bavière, Mathias Zimmermann."
    ],
    project: "Projet",
    facts: ["Version", "", "Firmware", "27.0.x", "Appareils vérifiés", "Principes de travail"],
    principles: ["procédures sûres", "volume de test bas", "relire avant d'annoncer le succès", "pas de confiance aveugle dans les commandes"],
    backups: "La sauvegarde et la restauration sont disponibles; l’interface indique la portée vérifiable.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  it: {
    kicker: "Sviluppo",
    title: "Informazioni su BASSWIESN",
    heading: "SoundTouch deve continuare a vivere in locale",
    paragraphs: [
      "BASSWIESN nasce da oltre 400 ore di reverse engineering, disassemblaggio, analisi firmware e test sui miei Bose SoundTouch.",
      "L'obiettivo non era copiare la vecchia app Bose, ma creare un software locale, comprensibile, adatto agli utenti finali e capace di verificare prima di dichiarare un successo.",
      "BASSWIESN rende più chiari i flussi conosciuti e mantiene utili i dispositivi SoundTouch anche senza il cloud del produttore.",
      "Amo queste radio. Ne possiedo molte e le ho consigliate per anni ad amici, famiglia e conoscenti.",
      "Poi è arrivato maggio 2026. Il cloud Bose è stato spento e molte persone hanno iniziato a chiedermi perché radio costose non funzionassero più.",
      "Da lì sono iniziati tentativi, test e analisi. BASSWIESN è cresciuto poco alla volta da quel processo.",
      "Oggi BASSWIESN gira su circa 30 Raspberry Pi 5 tra amici e conoscenti. I dispositivi continuano a funzionare e al momento non conosco errori critici importanti.",
      "Per il mio uso BASSWIESN funziona in modo affidabile. Pubblico questa release perché altri sviluppatori possano migliorarne parti o riutilizzarle nei propri progetti. Una citazione mi basta.",
      "Grazie. Saluti dalla Baviera, Mathias Zimmermann."
    ],
    project: "Progetto",
    facts: ["Versione", "", "Firmware", "27.0.x", "Dispositivi verificati", "Principi di lavoro"],
    principles: ["flussi sicuri", "volume di test basso", "rilettura prima del successo", "nessuna fiducia cieca nei comandi"],
    backups: "Backup e ripristino sono disponibili; l’interfaccia mostra l’ambito verificabile.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  es: {
    kicker: "Desarrollo",
    title: "Acerca de BASSWIESN",
    heading: "SoundTouch debe seguir funcionando en local",
    paragraphs: [
      "BASSWIESN nació de más de 400 horas de ingeniería inversa, desensamblado, análisis de firmware y pruebas con mis propias radios Bose SoundTouch.",
      "El objetivo nunca fue copiar la antigua app de Bose, sino crear un software local, comprensible, amable para usuarios finales y que verifique antes de informar éxito.",
      "BASSWIESN debe aclarar los procesos conocidos y mantener útiles los dispositivos SoundTouch sin depender del cloud del fabricante.",
      "Me encantan estas radios. Tengo muchas y durante años las recomendé a amigos, familiares y conocidos.",
      "Después llegó mayo de 2026. El cloud de Bose se apagó y muchas personas me preguntaron por qué estas radios caras habían dejado de funcionar.",
      "Luego empezó la ingeniería inversa: probar, testear y analizar. BASSWIESN creció poco a poco a partir de ese proceso.",
      "Hoy BASSWIESN funciona en unos 30 Raspberry Pi 5 entre amigos y conocidos. Los dispositivos siguen funcionando y actualmente no conozco errores críticos importantes.",
      "Para mi uso, BASSWIESN funciona de forma fiable. Publico esta versión porque otros desarrolladores quizá quieran evolucionar partes o reutilizarlas en sus propios proyectos. Una mención es suficiente.",
      "Gracias. Saludos desde Baviera, Mathias Zimmermann."
    ],
    project: "Proyecto",
    facts: ["Versión", "", "Firmware", "27.0.x", "Dispositivos verificados", "Principios de trabajo"],
    principles: ["flujos seguros", "volumen de prueba bajo", "leer de vuelta antes de informar éxito", "no confiar ciegamente en comandos"],
    backups: "La copia de seguridad y la restauración están disponibles; la interfaz muestra el alcance verificable.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  nl: {
    kicker: "Ontwikkeling",
    title: "Over BASSWIESN",
    heading: "SoundTouch moet lokaal blijven leven",
    paragraphs: [
      "BASSWIESN is ontstaan uit meer dan 400 uur reverse engineering, disassembly, firmware-analyse en tests met mijn eigen Bose SoundTouch-radio's.",
      "Het doel was nooit om de oude Bose-app simpelweg te kopiëren, maar software te maken die lokaal werkt, begrijpelijk is, prettig is voor eindgebruikers en veilig controleert voordat succes wordt gemeld.",
      "BASSWIESN moet bekende workflows duidelijker maken en SoundTouch-apparaten bruikbaar houden zonder de cloud van de fabrikant.",
      "Ik hou van deze radio's. Ik bezit er zelf veel en heb ze jarenlang aanbevolen aan vrienden, familie en kennissen.",
      "Toen kwam mei 2026. De Bose-cloud werd uitgeschakeld en plots vroegen veel mensen waarom deze dure radio's niet meer werkten.",
      "Daarna begon het reverse engineering: proberen, testen en analyseren. BASSWIESN groeide stap voor stap uit dat proces.",
      "Vandaag draait BASSWIESN op ongeveer 30 Raspberry Pi 5-systemen bij vrienden en kennissen. De apparaten blijven gewoon werken en ik ken momenteel geen grote kritieke fouten.",
      "Voor mijn gebruik werkt BASSWIESN betrouwbaar. Ik publiceer deze release omdat andere ontwikkelaars misschien onderdelen willen doorontwikkelen of hergebruiken. Een vermelding is genoeg.",
      "Dank je. Groeten uit Beieren, Mathias Zimmermann."
    ],
    project: "Project",
    facts: ["Versie", "", "Firmware", "27.0.x", "Geverifieerde apparaten", "Werkprincipes"],
    principles: ["veilige workflows", "laag testvolume", "teruglezen vóór succesmelding", "geen blind vertrouwen in commando's"],
    backups: "Back-up en herstel zijn beschikbaar; de interface toont de aantoonbare reikwijdte.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  ja: {
    kicker: "開発",
    title: "BASSWIESN について",
    heading: "SoundTouch をローカルで使い続けるために",
    paragraphs: [
      "BASSWIESN は、私自身の Bose SoundTouch ラジオで行った 400 時間を超えるリバースエンジニアリング、逆アセンブル、ファームウェア解析、テストから生まれました。",
      "目的は古い Bose アプリを単にコピーすることではありません。ローカルで動き、動作が追いやすく、利用者に扱いやすく、成功を表示する前に安全に確認するソフトウェアを作ることでした。",
      "BASSWIESN は既知の手順を分かりやすくし、メーカーのクラウドなしでも SoundTouch 機器を実用的に使い続けられるようにするためのものです。",
      "私はこのラジオが好きです。自分でも多く所有し、何年も友人や家族、知人に勧めてきました。",
      "そして 2026 年 5 月、Bose クラウドが停止しました。高価なラジオがなぜ動かないのか、多くの人に聞かれるようになりました。",
      "その後は、試し、テストし、解析する日々でした。その過程から少しずつ BASSWIESN が生まれました。",
      "現在、BASSWIESN は友人や知人の環境で約 30 台の Raspberry Pi 5 上で動いています。機器はそのまま動き続けており、現時点で大きな重大問題は把握していません。",
      "私の用途では BASSWIESN は安定して動いています。他の開発者が一部を発展させたり、自分のプロジェクトに取り込んだりできるよう、このリリースを公開します。言及してもらえれば十分です。",
      "ありがとうございます。バイエルンより、Mathias Zimmermann。"
    ],
    project: "プロジェクト",
    facts: ["バージョン", "", "ファームウェア", "27.0.x", "検証済み機器", "作業原則"],
    principles: ["安全な手順", "低いテスト音量", "成功表示の前に読み戻す", "コマンドを盲信しない"],
    backups: "バックアップと復元を利用でき、検証可能な範囲が画面に表示されます。",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  }
};

const ABOUT_COPY_EXTENDED = {
  pt: {
    kicker: "Desenvolvimento",
    title: "Sobre o BASSWIESN",
    heading: "SoundTouch deve continuar a viver localmente",
    paragraphs: [
      "O BASSWIESN nasceu de mais de 400 horas de engenharia inversa, desmontagem, análise de firmware e testes nos meus próprios rádios Bose SoundTouch.",
      "O objetivo nunca foi simplesmente copiar a antiga aplicação Bose. O objetivo foi criar um software que funciona localmente, trabalha de forma compreensível, é amigável para o utilizador final e verifica com segurança antes de indicar sucesso.",
      "O BASSWIESN deve tornar os processos conhecidos mais claros e manter os dispositivos SoundTouch úteis mesmo sem a cloud do fabricante.",
      "Eu adoro estes rádios. Tenho muitos deles e recomendei-os durante anos a amigos, família e conhecidos.",
      "Depois chegou maio de 2026. A cloud da Bose foi desligada. De repente, muitas pessoas perguntaram-me porque é que estes rádios caros já não funcionavam.",
      "Depois começou a engenharia inversa: experimentar, testar, analisar. Deste processo nasceu pouco a pouco o BASSWIESN.",
      "Hoje o BASSWIESN corre em cerca de 30 Raspberry Pi 5 no meu círculo de amigos e conhecidos. Os dispositivos simplesmente continuam a funcionar, e neste momento não conheço erros críticos maiores.",
      "Para o meu uso, o BASSWIESN funciona de forma fiável. Publico esta release porque outros programadores talvez queiram evoluir partes dela ou reutilizá-las nos seus próprios projetos. Uma menção é suficiente para mim.",
      "Obrigado. Saudações da Baviera, Mathias Zimmermann."
    ],
    project: "Projeto",
    facts: ["Versão", "", "Firmware", "27.0.x", "Dispositivos verificados", "Princípios de trabalho"],
    principles: ["processos seguros", "volume de teste baixo", "read-back antes de indicar sucesso", "não confiar cegamente em comandos"],
    backups: "Backup e restauração estão disponíveis; a interface mostra o escopo verificável.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  da: {
    kicker: "Udvikling",
    title: "Om BASSWIESN",
    heading: "SoundTouch skal fortsætte lokalt",
    paragraphs: [
      "BASSWIESN voksede ud af mere end 400 timers reverse engineering, disassembly, firmwareanalyse og tests på mine egne Bose SoundTouch-radioer.",
      "Målet var aldrig bare at kopiere den gamle Bose-app. Målet var software, der virker lokalt, arbejder forståeligt, er brugervenlig for slutbrugere og kontrollerer sikkert, før den melder succes.",
      "BASSWIESN skal gøre kendte arbejdsgange tydeligere og holde SoundTouch-enheder nyttige også uden producentens cloud.",
      "Jeg elsker disse radioer. Jeg ejer selv mange af dem og har anbefalet dem til venner, familie og bekendte i årevis.",
      "Så kom maj 2026. Bose-cloud blev lukket. Pludselig spurgte mange mig, hvorfor disse dyre radioer ikke længere virkede.",
      "Derefter begyndte reverse engineering: prøve, teste, analysere. Ud af denne proces voksede BASSWIESN gradvist.",
      "I dag kører BASSWIESN på omkring 30 Raspberry Pi 5 hos venner og bekendte. Enhederne fortsætter bare med at virke, og større kritiske fejl kender jeg i øjeblikket ikke til.",
      "Til mit brug fungerer BASSWIESN pålideligt. Jeg udgiver denne release, fordi udviklere måske vil videreudvikle dele af den eller bruge dem i egne projekter. En omtale er helt nok for mig.",
      "Tak. Hilsen fra Bayern, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Version", "", "Firmware", "27.0.x", "Verificerede enheder", "Arbejdsprincipper"],
    principles: ["sikre arbejdsgange", "lav testlydstyrke", "read-back før succesmelding", "ingen blind tillid til kommandoer"],
    backups: "Backup og gendannelse er tilgængelige; brugerfladen viser det verificerbare omfang.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  sv: {
    kicker: "Utveckling",
    title: "Om BASSWIESN",
    heading: "SoundTouch ska fortsätta leva lokalt",
    paragraphs: [
      "BASSWIESN växte fram ur mer än 400 timmar reverse engineering, disassembly, firmwareanalys och tester på mina egna Bose SoundTouch-radioapparater.",
      "Målet var aldrig att bara kopiera den gamla Bose-appen. Målet var programvara som fungerar lokalt, arbetar begripligt, är vänlig för slutanvändare och kontrollerar säkert innan framgång rapporteras.",
      "BASSWIESN ska göra kända arbetsflöden tydligare och hålla SoundTouch-enheter användbara även utan tillverkarens moln.",
      "Jag älskar dessa radioapparater. Jag äger själv många av dem och har rekommenderat dem till vänner, familj och bekanta i flera år.",
      "Sedan kom maj 2026. Bose-molnet stängdes av. Plötsligt frågade många mig varför dessa dyra radioapparater inte längre fungerade.",
      "Efter det började reverse engineering: prova, testa, analysera. Ur den processen växte BASSWIESN gradvis fram.",
      "I dag kör BASSWIESN på omkring 30 Raspberry Pi 5 hos vänner och bekanta. Enheterna fortsätter helt enkelt att fungera, och större kritiska fel känner jag för närvarande inte till.",
      "För min användning fungerar BASSWIESN tillförlitligt. Jag publicerar denna release eftersom utvecklare kanske vill vidareutveckla delar av den eller använda dem i egna projekt. Ett omnämnande räcker helt för mig.",
      "Tack. Hälsningar från Bayern, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Version", "", "Firmware", "27.0.x", "Verifierade enheter", "Arbetsprinciper"],
    principles: ["säkra arbetsflöden", "låg testvolym", "återläsning före framgångsmeddelande", "ingen blind tillit till kommandon"],
    backups: "Säkerhetskopiering och återställning finns; gränssnittet visar verifierbar omfattning.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  no: {
    kicker: "Utvikling",
    title: "Om BASSWIESN",
    heading: "SoundTouch skal fortsette å leve lokalt",
    paragraphs: [
      "BASSWIESN vokste ut av mer enn 400 timer med reverse engineering, disassembly, fastvareanalyse og tester på mine egne Bose SoundTouch-radioer.",
      "Målet var aldri å bare kopiere den gamle Bose-appen. Målet var programvare som fungerer lokalt, arbeider forståelig, er vennlig for sluttbrukere og kontrollerer trygt før den melder suksess.",
      "BASSWIESN skal gjøre kjente arbeidsflyter mer forståelige og holde SoundTouch-enheter nyttige også uten produsentens sky.",
      "Jeg elsker disse radioene. Jeg eier selv mange av dem og har anbefalt dem til venner, familie og bekjente i årevis.",
      "Så kom mai 2026. Bose-skyen ble slått av. Plutselig spurte mange meg hvorfor disse dyre radioene ikke lenger fungerte.",
      "Deretter begynte reverse engineering: prøve, teste, analysere. Ut av denne prosessen vokste BASSWIESN gradvis frem.",
      "I dag kjører BASSWIESN på rundt 30 Raspberry Pi 5 hos venner og bekjente. Enhetene fortsetter bare å fungere, og større kritiske feil kjenner jeg for tiden ikke til.",
      "For min bruk fungerer BASSWIESN pålitelig. Jeg publiserer denne releasen fordi utviklere kanskje vil videreutvikle deler av den eller bruke dem i egne prosjekter. En omtale holder helt for meg.",
      "Takk. Hilsen fra Bayern, Mathias Zimmermann."
    ],
    project: "Prosjekt",
    facts: ["Versjon", "", "Fastvare", "27.0.x", "Verifiserte enheter", "Arbeidsprinsipper"],
    principles: ["sikre arbeidsflyter", "lavt testvolum", "tilbake-lesing før suksessmelding", "ingen blind tillit til kommandoer"],
    backups: "Sikkerhetskopi og gjenoppretting er tilgjengelig; grensesnittet viser verifiserbart omfang.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  fi: {
    kicker: "Kehitys",
    title: "Tietoja BASSWIESNistä",
    heading: "SoundTouchin pitää jatkaa elämää paikallisesti",
    paragraphs: [
      "BASSWIESN syntyi yli 400 tunnin reverse engineeringistä, disassemblauksesta, laiteohjelmistoanalyysistä ja testeistä omilla Bose SoundTouch -radioillani.",
      "Tavoite ei koskaan ollut vain kopioida vanhaa Bose-sovellusta. Tavoite oli ohjelmisto, joka toimii paikallisesti, toimii ymmärrettävästi, on loppukäyttäjälle ystävällinen ja tarkistaa turvallisesti ennen onnistumisesta ilmoittamista.",
      "BASSWIESN tekee tunnetuista työnkuluista selkeämpiä ja pitää SoundTouch-laitteet mielekkäästi käyttökelpoisina myös ilman valmistajan pilveä.",
      "Rakastan näitä radioita. Omistan itse monta niistä ja suosittelin niitä vuosien ajan ystäville, perheelle ja tuttaville.",
      "Sitten tuli toukokuu 2026. Bose-pilvi suljettiin. Yhtäkkiä monet kysyivät minulta, miksi nämä kalliit radiot eivät enää toimi.",
      "Sen jälkeen alkoi reverse engineering: kokeilua, testausta, analysointia. Tästä prosessista BASSWIESN syntyi vähitellen.",
      "Nykyään BASSWIESN toimii noin 30 Raspberry Pi 5:llä ystävien ja tuttavien luona. Laitteet vain jatkavat toimintaansa, enkä tällä hetkellä tunne suurempia kriittisiä virheitä.",
      "Omassa käytössäni BASSWIESN toimii luotettavasti. Julkaisen tämän releasen, koska kehittäjät voivat ehkä jatkokehittää osia siitä tai hyödyntää niitä omissa projekteissaan. Maininta riittää minulle täysin.",
      "Kiitos. Terveisiä Baijerista, Mathias Zimmermann."
    ],
    project: "Projekti",
    facts: ["Versio", "", "Laiteohjelmisto", "27.0.x", "Varmennetut laitteet", "Työperiaatteet"],
    principles: ["turvalliset työnkulut", "matala testivolyymi", "takaisinluku ennen onnistumisilmoitusta", "ei sokeaa luottamusta komentoihin"],
    backups: "Varmuuskopiointi ja palautus ovat käytettävissä; käyttöliittymä näyttää varmennetun laajuuden.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  pl: {
    kicker: "Rozwój",
    title: "O BASSWIESN",
    heading: "SoundTouch powinien nadal działać lokalnie",
    paragraphs: [
      "BASSWIESN powstał z ponad 400 godzin reverse engineeringu, disassembly, analizy firmware i testów na moich własnych radiach Bose SoundTouch.",
      "Celem nigdy nie było po prostu skopiowanie starej aplikacji Bose. Celem było oprogramowanie, które działa lokalnie, pracuje w sposób zrozumiały, jest przyjazne dla użytkownika końcowego i bezpiecznie sprawdza stan przed zgłoszeniem sukcesu.",
      "BASSWIESN ma czynić znane procesy bardziej zrozumiałymi i utrzymać urządzenia SoundTouch sensownie użyteczne także bez chmury producenta.",
      "Uwielbiam te radia. Sam mam ich wiele i przez lata polecałem je przyjaciołom, rodzinie i znajomym.",
      "Potem przyszedł maj 2026. Chmura Bose została wyłączona. Nagle wiele osób pytało mnie, dlaczego te drogie radia już nie działają.",
      "Potem zaczęła się inżynieria odwrotna: próby, testy, analiza. Z tego procesu stopniowo powstał BASSWIESN.",
      "Obecnie BASSWIESN działa na około 30 Raspberry Pi 5 u przyjaciół i znajomych. Urządzenia po prostu działają dalej, a większych krytycznych błędów obecnie nie znam.",
      "W moim zastosowaniu BASSWIESN działa niezawodnie. Publikuję to wydanie, ponieważ programiści mogą chcieć rozwijać jego części lub wykorzystać je we własnych projektach. Wzmianka w zupełności mi wystarczy.",
      "Dziękuję. Pozdrowienia z Bawarii, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Wersja", "", "Firmware", "27.0.x", "Zweryfikowane urządzenia", "Zasady pracy"],
    principles: ["bezpieczne procesy", "niska głośność testowa", "odczyt zwrotny przed zgłoszeniem sukcesu", "bez ślepego zaufania do poleceń"],
    backups: "Kopia zapasowa i przywracanie są dostępne; interfejs pokazuje możliwy do potwierdzenia zakres.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  cs: {
    kicker: "Vývoj",
    title: "O BASSWIESN",
    heading: "SoundTouch má dál žít lokálně",
    paragraphs: [
      "BASSWIESN vznikl z více než 400 hodin reverse engineeringu, disassembly, analýzy firmware a testů na mých vlastních rádiích Bose SoundTouch.",
      "Cílem nikdy nebylo jen zkopírovat starou aplikaci Bose. Cílem byl software, který funguje lokálně, pracuje srozumitelně, je přívětivý pro koncové uživatele a bezpečně ověřuje stav před oznámením úspěchu.",
      "BASSWIESN má známé postupy zpřehlednit a udržet zařízení SoundTouch smysluplně použitelná i bez cloudu výrobce.",
      "Miluji tato rádia. Sám jich mnoho vlastním a roky jsem je doporučoval přátelům, rodině a známým.",
      "Pak přišel květen 2026. Cloud Bose byl vypnut. Najednou se mě mnoho lidí ptalo, proč tato drahá rádia už nefungují.",
      "Poté začal reverse engineering: zkoušení, testování, analýza. Z tohoto procesu postupně vznikl BASSWIESN.",
      "Dnes BASSWIESN běží asi na 30 Raspberry Pi 5 u přátel a známých. Zařízení prostě fungují dál a větší kritické chyby mi momentálně nejsou známy.",
      "Pro mé použití funguje BASSWIESN spolehlivě. Tuto release zveřejňuji, protože vývojáři možná budou chtít části dále rozvíjet nebo je použít ve vlastních projektech. Zmínka mi úplně stačí.",
      "Děkuji. Pozdravy z Bavorska, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Verze", "", "Firmware", "27.0.x", "Ověřená zařízení", "Pracovní zásady"],
    principles: ["bezpečné postupy", "nízká testovací hlasitost", "zpětné čtení před oznámením úspěchu", "žádná slepá důvěra v příkazy"],
    backups: "Záloha a obnova jsou dostupné; rozhraní zobrazuje ověřitelný rozsah.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  }
};

Object.assign(ABOUT_COPY, ABOUT_COPY_EXTENDED);
Object.assign(ABOUT_COPY, {
  sk: {
    kicker: "Vývoj",
    title: "O BASSWIESN",
    heading: "SoundTouch má ďalej žiť lokálne",
    paragraphs: [
      "BASSWIESN vznikol z viac než 400 hodín reverse engineeringu, disassembly, analýzy firmvéru a testov na mojich vlastných rádiách Bose SoundTouch.",
      "Cieľom nikdy nebolo jednoducho skopírovať starú aplikáciu Bose. Cieľom bol softvér, ktorý funguje lokálne, pracuje zrozumiteľne, je prívetivý pre koncového používateľa a bezpečne overuje stav pred oznámením úspechu.",
      "BASSWIESN má známe postupy spraviť zrozumiteľnejšími a udržať zariadenia SoundTouch zmysluplne použiteľné aj bez cloudu výrobcu.",
      "Milujem tieto rádiá. Sám ich vlastním veľa a roky som ich odporúčal priateľom, rodine a známym.",
      "Potom prišiel máj 2026. Bose cloud bol vypnutý. Zrazu sa ma veľa ľudí pýtalo, prečo tieto drahé rádiá už nefungujú.",
      "Potom začal reverse engineering: skúšanie, testovanie, analyzovanie. Z tohto procesu postupne vznikol BASSWIESN.",
      "Dnes BASSWIESN beží približne na 30 Raspberry Pi 5 u priateľov a známych. Zariadenia jednoducho bežia ďalej a väčšie kritické chyby mi momentálne nie sú známe.",
      "Pre moje použitie funguje BASSWIESN spoľahlivo. Túto release zverejňujem, pretože vývojári možno budú chcieť časti ďalej rozvíjať alebo ich použiť vo vlastných projektoch. Zmienka mi úplne stačí.",
      "Ďakujem. Pozdravy z Bavorska, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Verzia", "", "Firmvér", "27.0.x", "Overené zariadenia", "Pracovné zásady"],
    principles: ["bezpečné postupy", "nízka testovacia hlasitosť", "read-back pred oznámením úspechu", "žiadna slepá dôvera v príkazy"],
    backups: "Záloha a obnova sú dostupné; rozhranie zobrazuje overiteľný rozsah.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  hu: {
    kicker: "Fejlesztés",
    title: "A BASSWIESN névjegye",
    heading: "A SoundTouch helyben éljen tovább",
    paragraphs: [
      "A BASSWIESN több mint 400 óra reverse engineeringből, disassemblyből, firmware-elemzésből és a saját Bose SoundTouch rádióimon végzett tesztekből született.",
      "A cél soha nem az volt, hogy egyszerűen lemásoljam a régi Bose alkalmazást. A cél olyan szoftver volt, amely helyben működik, átláthatóan dolgozik, felhasználóbarát, és biztonságosan ellenőriz, mielőtt sikert jelez.",
      "A BASSWIESN érthetőbbé teszi az ismert folyamatokat, és a SoundTouch eszközöket gyártói felhő nélkül is értelmesen használható állapotban tartja.",
      "Szeretem ezeket a rádiókat. Nekem is sok van belőlük, és éveken át ajánlottam őket barátoknak, családnak és ismerősöknek.",
      "Aztán eljött 2026 májusa. A Bose felhőt leállították. Hirtelen sokan kérdezték tőlem, miért nem működnek többé ezek a drága rádiók.",
      "Ezután kezdődött a reverse engineering: próbálgatás, tesztelés, elemzés. Ebből a folyamatból nőtt ki lépésről lépésre a BASSWIESN.",
      "Ma a BASSWIESN körülbelül 30 Raspberry Pi 5 rendszeren fut barátoknál és ismerősöknél. Az eszközök egyszerűen tovább működnek, és nagyobb kritikus hibáról jelenleg nem tudok.",
      "Az én használatomban a BASSWIESN megbízhatóan működik. Azért teszem közzé ezt a release-t, mert fejlesztők talán továbbfejlesztenék egyes részeit, vagy saját projektjeikben használnák fel. Egy említés nekem teljesen elég.",
      "Köszönöm. Üdvözlet Bajorországból, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Verzió", "", "Firmware", "27.0.x", "Ellenőrzött eszközök", "Munkaprincipiumok"],
    principles: ["biztonságos folyamatok", "alacsony teszthangerő", "visszaolvasás sikerjelzés előtt", "nincs vak bizalom a parancsokban"],
    backups: "A biztonsági mentés és visszaállítás elérhető; a felület a bizonyítható hatókört mutatja.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  ro: {
    kicker: "Dezvoltare",
    title: "Despre BASSWIESN",
    heading: "SoundTouch trebuie să continue local",
    paragraphs: [
      "BASSWIESN a apărut din peste 400 de ore de inginerie inversă, dezasamblare, analiză firmware și teste pe propriile mele radiouri Bose SoundTouch.",
      "Scopul nu a fost niciodată să copiez pur și simplu vechea aplicație Bose. Scopul a fost un software care funcționează local, lucrează transparent, este prietenos pentru utilizatori și verifică sigur înainte să raporteze succes.",
      "BASSWIESN trebuie să facă fluxurile cunoscute mai ușor de înțeles și să păstreze dispozitivele SoundTouch utile și fără cloud-ul producătorului.",
      "Îmi plac aceste radiouri. Dețin multe dintre ele și le-am recomandat ani la rând prietenilor, familiei și cunoscuților.",
      "Apoi a venit mai 2026. Cloud-ul Bose a fost oprit. Dintr-odată, mulți oameni m-au întrebat de ce aceste radiouri scumpe nu mai funcționează.",
      "Apoi a început ingineria inversă: încercare, testare, analiză. Din acest proces a apărut treptat BASSWIESN.",
      "Între timp, BASSWIESN rulează pe aproximativ 30 de Raspberry Pi 5 la prieteni și cunoscuți. Dispozitivele pur și simplu continuă să funcționeze, iar erori critice mari nu cunosc în acest moment.",
      "Pentru folosirea mea, BASSWIESN funcționează fiabil. Public această versiune deoarece dezvoltatorii ar putea continua anumite părți sau le-ar putea folosi în propriile proiecte. O mențiune este suficientă pentru mine.",
      "Mulțumesc. Salutări din Bavaria, Mathias Zimmermann."
    ],
    project: "Proiect",
    facts: ["Versiune", "", "Firmware", "27.0.x", "Dispozitive verificate", "Principii de lucru"],
    principles: ["fluxuri sigure", "volum de test scăzut", "read-back înainte de raportarea succesului", "fără încredere oarbă în comenzi"],
    backups: "Backupul și restaurarea sunt disponibile; interfața arată domeniul verificabil.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  bg: {
    kicker: "Разработка",
    title: "За BASSWIESN",
    heading: "SoundTouch трябва да продължи да живее локално",
    paragraphs: [
      "BASSWIESN възникна от повече от 400 часа reverse engineering, disassembly, анализ на firmware и тестове върху моите собствени Bose SoundTouch радиа.",
      "Целта никога не беше просто да се копира старата Bose апликация. Целта беше софтуер, който работи локално, действа разбираемо, е удобен за крайния потребител и проверява безопасно, преди да съобщи успех.",
      "BASSWIESN трябва да направи познатите процеси по-разбираеми и да запази SoundTouch устройствата смислено използваеми дори без cloud на производителя.",
      "Обичам тези радиа. Самият аз притежавам много от тях и години наред ги препоръчвах на приятели, семейство и познати.",
      "После дойде май 2026. Bose cloud беше изключен. Изведнъж много хора ме питаха защо тези скъпи радиа вече не работят.",
      "След това започна reverse engineering: пробване, тестване, анализиране. От този процес постепенно възникна BASSWIESN.",
      "Междувременно BASSWIESN работи на около 30 Raspberry Pi 5 при приятели и познати. Устройствата просто продължават да работят, а по-големи критични грешки в момента не са ми известни.",
      "За моята употреба BASSWIESN работи надеждно. Публикувам тази release, защото разработчици може би ще искат да развият части от нея или да ги използват в собствени проекти. Едно споменаване ми е напълно достатъчно.",
      "Благодаря. Поздрави от Бавария, Mathias Zimmermann."
    ],
    project: "Проект",
    facts: ["Версия", "", "Firmware", "27.0.x", "Проверени устройства", "Работни принципи"],
    principles: ["сигурни процеси", "ниска тестова сила на звука", "read-back преди съобщаване на успех", "без сляпо доверие в команди"],
    backups: "Архивирането и възстановяването са налични; интерфейсът показва проверимия обхват.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  }
});

Object.assign(ABOUT_COPY, {
  hr: {
    kicker: "Razvoj",
    title: "O BASSWIESN-u",
    heading: "SoundTouch treba nastaviti živjeti lokalno",
    paragraphs: [
      "BASSWIESN je nastao iz više od 400 sati reverznog inženjeringa, disassemblyja, analize firmwarea i testiranja na mojim vlastitim Bose SoundTouch radijima.",
      "Cilj nikada nije bio jednostavno kopirati staru Bose aplikaciju. Cilj je bio stvoriti softver koji radi lokalno, ponaša se razumljivo, ostaje pristupačan krajnjem korisniku i sigurno provjerava stanje prije nego što prijavi uspjeh.",
      "BASSWIESN treba poznate procese učiniti jasnijima i održati SoundTouch uređaje smisleno uporabljivima i bez proizvođačeva clouda.",
      "Volim ove radije. Imam ih mnogo i godinama sam ih preporučivao prijateljima, obitelji i poznanicima.",
      "Zatim je došao svibanj 2026. Bose cloud je ugašen. Odjednom su me mnogi pitali zašto ti skupi radiji više ne rade.",
      "Nakon toga krenuo je reverzni inženjering: isprobavanje, testiranje, analiziranje. Iz tog procesa BASSWIESN je postupno rastao.",
      "Danas BASSWIESN radi na približno 30 Raspberry Pi 5 sustava kod prijatelja i poznanika. Uređaji jednostavno nastavljaju raditi, a za veće kritične greške trenutačno ne znam.",
      "Za moju uporabu BASSWIESN radi pouzdano. Objavljujem ovu verziju jer bi drugi programeri možda htjeli dalje razvijati neke dijelove ili ih upotrijebiti u vlastitim projektima. Spominjanje mi je sasvim dovoljno.",
      "Hvala. Pozdrav iz Bavarske, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Verzija", "", "Firmware", "27.0.x", "Provjereni uređaji", "Radna načela"],
    principles: ["sigurni postupci", "niska testna glasnoća", "read-back prije prijave uspjeha", "bez slijepog povjerenja u naredbe"],
    backups: "Sigurnosna kopija i vraćanje su dostupni; sučelje prikazuje provjerljivi opseg.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  sl: {
    kicker: "Razvoj",
    title: "O BASSWIESN",
    heading: "SoundTouch naj se lokalno uporablja naprej",
    paragraphs: [
      "BASSWIESN je nastal iz več kot 400 ur reverznega inženiringa, disassemblyja, analize firmwarea in testov na mojih lastnih radijskih napravah Bose SoundTouch.",
      "Cilj nikoli ni bil preprosto kopirati stare aplikacije Bose. Cilj je bil ustvariti programsko opremo, ki deluje lokalno, je razumljiva, prijazna do končnega uporabnika in varno preveri stanje, preden prijavi uspeh.",
      "BASSWIESN mora znane postopke narediti jasnejše in ohraniti naprave SoundTouch smiselno uporabne tudi brez oblaka proizvajalca.",
      "Te radie imam rad. Sam jih imam veliko in sem jih leta priporočal prijateljem, družini in znancem.",
      "Nato je prišel maj 2026. Bose oblak je bil izklopljen. Nenadoma me je veliko ljudi spraševalo, zakaj ti dragi radii ne delujejo več.",
      "Potem se je začel reverzni inženiring: preizkušanje, testiranje, analiziranje. Iz tega procesa je BASSWIESN postopoma zrasel.",
      "Danes BASSWIESN deluje na približno 30 Raspberry Pi 5 pri prijateljih in znancih. Naprave preprosto delujejo naprej, večjih kritičnih napak pa trenutno ne poznam.",
      "Za mojo uporabo BASSWIESN deluje zanesljivo. To izdajo objavljam, ker bodo drugi razvijalci morda želeli razvijati posamezne dele naprej ali jih uporabiti v svojih projektih. Omemba mi popolnoma zadostuje.",
      "Hvala. Pozdravi iz Bavarske, Mathias Zimmermann."
    ],
    project: "Projekt",
    facts: ["Različica", "", "Firmware", "27.0.x", "Preverjene naprave", "Delovna načela"],
    principles: ["varni postopki", "nizka testna glasnost", "read-back pred prijavo uspeha", "brez slepega zaupanja v ukaze"],
    backups: "Varnostno kopiranje in obnovitev sta na voljo; vmesnik prikaže preverljiv obseg.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  el: {
    kicker: "Ανάπτυξη",
    title: "Σχετικά με το BASSWIESN",
    heading: "Το SoundTouch πρέπει να συνεχίσει να λειτουργεί τοπικά",
    paragraphs: [
      "Το BASSWIESN γεννήθηκε από περισσότερες από 400 ώρες reverse engineering, disassembly, ανάλυσης firmware και δοκιμών στα δικά μου ραδιόφωνα Bose SoundTouch.",
      "Ο στόχος δεν ήταν ποτέ να αντιγραφεί απλώς η παλιά εφαρμογή Bose. Ο στόχος ήταν λογισμικό που λειτουργεί τοπικά, παραμένει κατανοητό, είναι φιλικό για τον τελικό χρήστη και ελέγχει με ασφάλεια πριν αναφέρει επιτυχία.",
      "Το BASSWIESN πρέπει να κάνει τις γνωστές διαδικασίες πιο σαφείς και να κρατήσει τις συσκευές SoundTouch ουσιαστικά χρήσιμες χωρίς το cloud του κατασκευαστή.",
      "Αγαπώ αυτά τα ραδιόφωνα. Έχω πολλά από αυτά και για χρόνια τα σύστηνα σε φίλους, οικογένεια και γνωστούς.",
      "Ύστερα ήρθε ο Μάιος του 2026. Το Bose cloud απενεργοποιήθηκε. Ξαφνικά πολλοί με ρωτούσαν γιατί αυτά τα ακριβά ραδιόφωνα δεν λειτουργούσαν πια.",
      "Μετά άρχισε το reverse engineering: δοκιμές, έλεγχοι, ανάλυση. Από αυτή τη διαδικασία μεγάλωσε σταδιακά το BASSWIESN.",
      "Σήμερα το BASSWIESN λειτουργεί σε περίπου 30 Raspberry Pi 5 σε φίλους και γνωστούς. Οι συσκευές συνεχίζουν απλώς να λειτουργούν, και αυτή τη στιγμή δεν γνωρίζω μεγάλα κρίσιμα σφάλματα.",
      "Για τη δική μου χρήση το BASSWIESN λειτουργεί αξιόπιστα. Δημοσιεύω αυτή την έκδοση επειδή άλλοι προγραμματιστές ίσως θελήσουν να εξελίξουν μέρη της ή να τα χρησιμοποιήσουν στα δικά τους έργα. Μια αναφορά μού αρκεί απολύτως.",
      "Ευχαριστώ. Χαιρετισμούς από τη Βαυαρία, Mathias Zimmermann."
    ],
    project: "Έργο",
    facts: ["Έκδοση", "", "Firmware", "27.0.x", "Επαληθευμένες συσκευές", "Αρχές εργασίας"],
    principles: ["ασφαλείς διαδικασίες", "χαμηλή ένταση δοκιμής", "read-back πριν από αναφορά επιτυχίας", "όχι τυφλή εμπιστοσύνη σε εντολές"],
    backups: "Η δημιουργία και η επαναφορά backup είναι διαθέσιμες· η διεπαφή δείχνει το επαληθεύσιμο εύρος.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  tr: {
    kicker: "Geliştirme",
    title: "BASSWIESN hakkında",
    heading: "SoundTouch yerel olarak yaşamaya devam etmeli",
    paragraphs: [
      "BASSWIESN, kendi Bose SoundTouch radyolarımda yapılan 400 saatten fazla tersine mühendislik, disassembly, firmware analizi ve testten doğdu.",
      "Amaç hiçbir zaman eski Bose uygulamasını basitçe kopyalamak değildi. Amaç yerel çalışan, anlaşılır davranan, son kullanıcı için dostça olan ve başarı bildirmeden önce güvenli şekilde doğrulayan bir yazılım oluşturmaktı.",
      "BASSWIESN bilinen süreçleri daha anlaşılır yapmalı ve SoundTouch cihazlarını üretici bulutu olmadan da anlamlı şekilde kullanılabilir tutmalıdır.",
      "Bu radyoları seviyorum. Kendim de birçoğuna sahibim ve yıllarca arkadaşlarıma, aileme ve tanıdıklarıma tavsiye ettim.",
      "Sonra Mayıs 2026 geldi. Bose bulutu kapatıldı. Birden çok kişi bana bu pahalı radyoların neden artık çalışmadığını sordu.",
      "Ardından tersine mühendislik başladı: denemek, test etmek, analiz etmek. BASSWIESN bu süreçten adım adım büyüdü.",
      "Bugün BASSWIESN arkadaşlar ve tanıdıklar arasında yaklaşık 30 Raspberry Pi 5 üzerinde çalışıyor. Cihazlar basitçe çalışmaya devam ediyor ve şu anda büyük kritik hatalar bilmiyorum.",
      "Kendi kullanımımda BASSWIESN güvenilir çalışıyor. Bu sürümü yayımlıyorum çünkü başka geliştiriciler bazı parçaları ilerletmek veya kendi projelerinde kullanmak isteyebilir. Benim için bir atıf tamamen yeterli.",
      "Teşekkürler. Bavyera'dan selamlar, Mathias Zimmermann."
    ],
    project: "Proje",
    facts: ["Sürüm", "", "Firmware", "27.0.x", "Doğrulanmış cihazlar", "Çalışma ilkeleri"],
    principles: ["güvenli süreçler", "düşük test ses düzeyi", "başarı bildirmeden önce read-back", "komutlara kör güven yok"],
    backups: "Yedekleme ve geri yükleme kullanılabilir; arayüz doğrulanabilir kapsamı gösterir.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  ru: {
    kicker: "Разработка",
    title: "О BASSWIESN",
    heading: "SoundTouch должен продолжать работать локально",
    paragraphs: [
      "BASSWIESN появился из более чем 400 часов reverse engineering, disassembly, анализа прошивки и тестов на моих собственных радио Bose SoundTouch.",
      "Цель никогда не состояла в том, чтобы просто скопировать старое приложение Bose. Целью было программное обеспечение, которое работает локально, ведет себя понятно, удобно для конечного пользователя и безопасно проверяет состояние перед сообщением об успехе.",
      "BASSWIESN должен сделать известные процессы понятнее и сохранить устройства SoundTouch осмысленно полезными даже без облака производителя.",
      "Я люблю эти радио. У меня самого их много, и я годами рекомендовал их друзьям, семье и знакомым.",
      "Потом наступил май 2026 года. Облако Bose было отключено. Внезапно многие начали спрашивать меня, почему эти дорогие радио больше не работают.",
      "Затем начался reverse engineering: пробовать, тестировать, анализировать. Из этого процесса BASSWIESN постепенно вырос.",
      "Сегодня BASSWIESN работает примерно на 30 Raspberry Pi 5 у друзей и знакомых. Устройства просто продолжают работать, и о крупных критических ошибках на данный момент мне неизвестно.",
      "Для моего применения BASSWIESN работает надежно. Я публикую этот релиз, потому что другие разработчики, возможно, захотят развивать его части дальше или использовать их в собственных проектах. Упоминания для меня вполне достаточно.",
      "Спасибо. Привет из Баварии, Mathias Zimmermann."
    ],
    project: "Проект",
    facts: ["Версия", "", "Прошивка", "27.0.x", "Проверенные устройства", "Принципы работы"],
    principles: ["безопасные процессы", "низкая тестовая громкость", "read-back перед сообщением об успехе", "нет слепого доверия командам"],
    backups: "Резервное копирование и восстановление доступны; интерфейс показывает проверяемый объём.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  uk: {
    kicker: "Розробка",
    title: "Про BASSWIESN",
    heading: "SoundTouch має продовжувати працювати локально",
    paragraphs: [
      "BASSWIESN виник із понад 400 годин reverse engineering, disassembly, аналізу firmware та тестів на моїх власних радіо Bose SoundTouch.",
      "Мета ніколи не полягала в тому, щоб просто скопіювати старий застосунок Bose. Метою було програмне забезпечення, яке працює локально, поводиться зрозуміло, є дружнім для кінцевого користувача і безпечно перевіряє стан перед повідомленням про успіх.",
      "BASSWIESN має зробити відомі процеси зрозумілішими і зберегти пристрої SoundTouch змістовно корисними навіть без хмари виробника.",
      "Я люблю ці радіо. У мене самого їх багато, і я роками радив їх друзям, родині та знайомим.",
      "Потім настав травень 2026 року. Хмару Bose вимкнули. Раптом багато людей почали питати мене, чому ці дорогі радіо більше не працюють.",
      "Після цього почався reverse engineering: пробувати, тестувати, аналізувати. Із цього процесу BASSWIESN поступово виріс.",
      "Сьогодні BASSWIESN працює приблизно на 30 Raspberry Pi 5 у друзів і знайомих. Пристрої просто продовжують працювати, і про великі критичні помилки наразі мені невідомо.",
      "Для мого використання BASSWIESN працює надійно. Я публікую цей реліз, тому що інші розробники, можливо, захочуть розвивати його частини далі або використовувати їх у власних проєктах. Згадки для мене цілком достатньо.",
      "Дякую. Вітання з Баварії, Mathias Zimmermann."
    ],
    project: "Проєкт",
    facts: ["Версія", "", "Firmware", "27.0.x", "Перевірені пристрої", "Принципи роботи"],
    principles: ["безпечні процеси", "низька тестова гучність", "read-back перед повідомленням про успіх", "без сліпої довіри до команд"],
    backups: "Резервне копіювання та відновлення доступні; інтерфейс показує перевірений обсяг.",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  },
  zh: {
    kicker: "开发",
    title: "关于 BASSWIESN",
    heading: "让 SoundTouch 在本地继续运行",
    paragraphs: [
      "BASSWIESN 源自超过 400 小时的逆向工程、反汇编、固件分析，以及我用自己的 Bose SoundTouch 收音机进行的测试。",
      "目标从来不是简单复制旧的 Bose 应用。目标是创建一套可在本地运行、行为可理解、对最终用户友好，并且在报告成功之前会安全回读验证的软件。",
      "BASSWIESN 应当让已知流程更清晰，并让 SoundTouch 设备即使没有厂商云也能有意义地继续使用。",
      "我喜欢这些收音机。我自己拥有很多台，也多年向朋友、家人和熟人推荐它们。",
      "然后到了 2026 年 5 月。Bose 云被关闭。突然很多人问我，为什么这些昂贵的收音机不再工作。",
      "随后开始了逆向工程：尝试、测试、分析。BASSWIESN 就是在这个过程中一步步成长起来的。",
      "如今，BASSWIESN 在朋友和熟人那里大约 30 台 Raspberry Pi 5 上运行。设备继续正常工作，目前我不知道有重大关键错误。",
      "对我自己的使用来说，BASSWIESN 运行可靠。我发布这个版本，是因为其他开发者也许想继续改进其中某些部分，或在自己的项目中使用它们。对我来说，一个提及就完全足够了。",
      "谢谢。来自巴伐利亚的问候，Mathias Zimmermann。"
    ],
    project: "项目",
    facts: ["版本", "", "Firmware", "27.0.x", "已验证设备", "工作原则"],
    principles: ["安全流程", "低测试音量", "报告成功前 read-back", "不盲目信任命令"],
    backups: "备份和恢复可用；界面会显示能够验证的确切范围。",
    disclaimer: "Trademark Disclaimer: SoundTouch and Bose are registered trademarks of their respective owners. BASSWIESN is an independent unofficial project and is not affiliated with Bose Corporation."
  }
});

const FIRST_RUN_COPY = {
  de: {
    title: "Nutzung auf eigenes Risiko",
    paragraphs: [
      "BASSWIESN kann SoundTouch-Geräte umkonfigurieren und unter ungünstigen Umständen beschädigen.",
      "Radio-Setup, Presets, Telnet-Neustart und Recovery können Neustarts oder dauerhafte Änderungen auslösen. Echte Schreiboperationen erfolgen nur nach expliziter Benutzerfreigabe.",
      "Diese Software ist privat entwickelt und nicht fertig. Nutzung auf eigenes Risiko.",
      "Grüße aus Bayern"
    ],
    read: "Ich habe den Hinweis gelesen",
    never: "Nicht erneut anzeigen",
    ack: "Bestätigen"
  },
  en: {
    title: "Use at your own risk",
    paragraphs: [
      "BASSWIESN can reconfigure SoundTouch devices and may damage them under unfavorable circumstances.",
      "Radio setup, presets, Telnet reboot and recovery can trigger restarts or permanent changes. Real write operations happen only after explicit user approval.",
      "This software is privately developed and not finished. Use it at your own risk.",
      "Greetings from Bavaria"
    ],
    read: "I have read the warning",
    never: "Do not show again",
    ack: "Confirm"
  }
};

for (const lang of window.BasswiesnI18n?.languages || []) {
  if (!ABOUT_COPY[lang]) ABOUT_COPY[lang] = ABOUT_COPY.en;
  if (!FIRST_RUN_COPY[lang]) {
    FIRST_RUN_COPY[lang] = {
      title: i18nLangT(lang, "first_run_title"),
      paragraphs: ["first_run_p1", "first_run_p2", "first_run_p3", "first_run_p4"].map((key) => i18nLangT(lang, key)),
      read: i18nLangT(lang, "first_run_read"),
      never: i18nLangT(lang, "first_run_never"),
      ack: i18nLangT(lang, "first_run_ack"),
    };
  }
}

function renderAboutContent() {
  const box = document.querySelector(".about-release-copy");
  if (!box) return;
  const lang = state.systemSettings?.web_language || document.documentElement.lang || "en";
  const copy = ABOUT_COPY[lang] || ABOUT_COPY.en;
  const devices = ["Bose SoundTouch 10", "Bose SoundTouch 20", "Bose SoundTouch 30", "Bose SoundTouch Portable"];
  const version = state.applicationVersion;
  const displayVersion = version ? (String(version).startsWith("v") ? version : `v${version}`) : "Version nicht verfügbar";
  document.querySelector("#view-about .section-kicker").textContent = `${copy.kicker} · ${displayVersion}`;
  document.querySelector("#view-about h2").textContent = copy.title;
  box.innerHTML = `
    <h3>${escapeHtml(copy.heading)}</h3>
    ${copy.paragraphs.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
    <p>${escapeHtml(copy.project)}:<br><a class="about-github-link" href="https://github.com/Zimbo88/basswiesn" target="_blank" rel="noopener">https://github.com/Zimbo88/basswiesn</a></p>
    <div class="about-fact-grid">
      <article><span>${escapeHtml(copy.facts[0])}</span><strong>${escapeHtml(displayVersion)}</strong></article>
      <article><span>${escapeHtml(copy.facts[2])}</span><strong>${escapeHtml(copy.facts[3])}</strong></article>
      <article><span>${escapeHtml(copy.facts[4])}</span><ul>${devices.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></article>
      <article><span>${escapeHtml(copy.facts[5])}</span><ul>${copy.principles.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></article>
    </div>
    <p>${escapeHtml(copy.backups)}</p>
    <p class="about-disclaimer">${escapeHtml(copy.disclaimer)}</p>`;
}

function renderFirstRunWarning() {
  const lang = state.systemSettings?.web_language || document.documentElement.lang || "en";
  const copy = FIRST_RUN_COPY[lang] || FIRST_RUN_COPY.en;
  const title = document.getElementById("first-run-warning-title");
  const body = document.getElementById("first-run-warning-copy");
  const read = document.getElementById("first-run-warning-read-label");
  const never = document.getElementById("first-run-warning-never-label");
  const ack = document.getElementById("first-run-warning-ack");
  if (title) title.textContent = copy.title;
  if (body) body.innerHTML = copy.paragraphs.map((item) => `<p>${escapeHtml(item)}</p>`).join("");
  if (read) read.textContent = copy.read;
  if (never) never.textContent = copy.never;
  if (ack) ack.textContent = copy.ack;
}

function renderSetupPreparationCopy() {
  document.querySelectorAll("[data-setup-prep-title]").forEach((node) => {
    node.textContent = i18nT("setup_prep_title");
  });
  const steps = ["setup_prep_step1", "setup_prep_step2", "setup_prep_step3", "setup_prep_step4", "setup_prep_step5", "setup_prep_step6"];
  document.querySelectorAll("[data-setup-prep-steps]").forEach((list) => {
    list.innerHTML = steps.map((key) => `<li>${escapeHtml(i18nT(key))}</li>`).join("");
  });
  document.querySelectorAll("[data-setup-warning-title]").forEach((node) => {
    node.textContent = i18nT("setup_ssh_warning_title");
  });
  document.querySelectorAll("[data-setup-warning-body]").forEach((node) => {
    node.textContent = i18nT("setup_ssh_warning_body");
  });
}

function translateCoreUi() {
  document.querySelectorAll("[data-i18n]").forEach((element) => { element.textContent = i18nT(element.dataset.i18n); });
  const textMap = {
    "#view-setup .page-head h2": "setup", "#view-devices .page-head h2": "radios", "#view-health .page-head h2": "status", "#view-presets .page-head h2": "presets",
    "#view-system-settings .page-head h2": "settings", "#view-telemetry .page-head h2": "diagnostics", "#view-lab .page-head h2": "lab",
    "#download-support-bundle": "support_bundle", "#setup-finish": "complete", "[data-setup-flow-action='verify']": "verify", "[data-setup-flow-action='apply']": "apply",
  };
  for (const [selector, key] of Object.entries(textMap)) document.querySelectorAll(selector).forEach((element) => { element.textContent = i18nT(key); });
  const navMap = {
    dashboard: "start",
    setup: "setup",
    devices: "radios",
    health: "status",
    controls: "remote_control",
    stations: "stations",
    presets: "presets",
    multiroom: "multiroom",
    schedules: "alarms",
    "device-settings": "device_settings",
    about: "about_basswiesn",
    "system-settings": "settings",
  };
  document.querySelectorAll(".topnav > .nav-button").forEach((button) => {
    const key = navMap[button.dataset.view];
    if (key) button.textContent = i18nT(key);
  });
  const more = document.querySelector(".advanced-nav > summary");
  if (more) more.textContent = i18nT("more");
  const labelMap = { "web-language-select": "language", "guided-hints": "guided_hints", "ip-write-guard": "write_guard" };
  for (const [id, key] of Object.entries(labelMap)) {
    const label = document.getElementById(id)?.closest("label");
    const node = label ? [...label.childNodes].find((item) => item.nodeType === Node.TEXT_NODE && item.textContent.trim()) : null;
    if (node) node.nodeValue = `${i18nT(key)} `;
  }
  const aliases = { setup:"setup", radios:"radios", favoriten:"presets", favorites:"presets", einstellungen:"settings", settings:"settings", diagnose:"diagnostics", diagnostics:"diagnostics", labor:"lab", lab:"lab", scan:"scan", scannen:"scan", verify:"verify", prüfen:"verify", apply:"apply", anwenden:"apply", complete:"complete", abschließen:"complete", presets:"presets", sources:"sources", quellen:"sources", volume:"volume", lautstärke:"volume", playing:"playing", wiedergabe:"playing", capabilities:"capabilities", "runtime state":"runtime_state", "support bundle":"support_bundle", logs:"logs", protokolle:"logs", status:"status", cloud:"cloud", ssh:"ssh", "remote services":"remote_services", sprache:"language", language:"language", "guided hints":"guided_hints", "ip write guard":"write_guard", theme:"theme", design:"theme" };
  document.querySelectorAll("h2,h3,h4,button,summary,label,legend,span,b").forEach((element) => {
    [...element.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE).forEach((node) => {
      const raw = node.nodeValue.trim();
      const key = aliases[raw.toLowerCase()];
      if (key) node.nodeValue = node.nodeValue.replace(raw, i18nT(key));
    });
  });
  translateExactUiPhrases();
  renderSetupPreparationCopy();
  renderAboutContent();
  renderFirstRunWarning();
}

function translateExactUiPhrases(root = document.body) {
  if (!window.BasswiesnI18n?.phrase || !root) return;
  const skipSelector = "script,style,code,pre,textarea";
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent || parent.closest(skipSelector)) return NodeFilter.FILTER_REJECT;
      const raw = node.nodeValue.trim();
      if (!raw || /^[\d\s%:./<>()+-]+$/.test(raw)) return NodeFilter.FILTER_REJECT;
      return i18nPhraseT(raw) !== raw ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);
  nodes.forEach((item) => {
    const raw = item.nodeValue.trim();
    const translated = i18nPhraseT(raw);
    if (translated !== raw) item.nodeValue = item.nodeValue.replace(raw, translated);
  });
  document.querySelectorAll("[placeholder],[aria-label],input[value]").forEach((element) => {
    for (const attribute of ["placeholder", "aria-label", "value"]) {
      if (!element.hasAttribute(attribute)) continue;
      if (element.tagName === "INPUT" && attribute === "value" && !["button", "submit", "reset"].includes((element.type || "").toLowerCase())) continue;
      const value = element.getAttribute(attribute);
      const translated = i18nPhraseT(value);
      if (translated !== value) element.setAttribute(attribute, translated);
    }
  });
}

function applyUiPreferences() {
  if (!state.systemSettings) return;
  const mode = ["easy", "standard", "lab"].includes(state.systemSettings.ui_mode)
    ? state.systemSettings.ui_mode
    : (state.systemSettings.lab_mode === "true" ? "lab" : "standard");
  const hints = state.systemSettings.guided_hints !== "false";
  document.body.classList.toggle("easy-mode", mode === "easy");
  document.body.classList.toggle("standard-mode", mode === "standard");
  document.body.classList.toggle("lab-mode", mode === "lab");
  document.body.classList.remove("normal-mode");
  document.body.classList.toggle("guided-hints", hints);
  const modeSetting = document.getElementById("ui-mode-setting");
  const modeSwitch = document.getElementById("ui-mode-switch");
  if (modeSetting) modeSetting.value = mode;
  if (modeSwitch) modeSwitch.value = mode;
  if (mode === "easy") {
    document.querySelector(".advanced-nav")?.removeAttribute("open");
    const active = document.querySelector(".nav-button.is-active")?.dataset.view;
    const easyViews = new Set(["setup", "devices", "controls", "presets", "multiroom", "schedules", "device-settings"]);
    if (!easyViews.has(active)) document.querySelector('.nav-button[data-view="setup"]')?.click();
  }
  document.documentElement.lang = state.systemSettings.web_language || "de";
  window.BasswiesnI18n?.setLanguage(document.documentElement.lang);
  for (const [view, label] of Object.entries({ setup: i18nT("setup"), devices: i18nT("radios"), health: i18nT("status"), presets: i18nT("presets"), "system-settings": i18nT("settings"), telemetry: i18nT("diagnostics"), lab: i18nT("lab") })) {
    const button = document.querySelector(`.nav-button[data-view="${view}"]`);
    if (button) button.textContent = label;
  }
  const save = document.querySelector("#system-settings-form button[type=submit]");
  if (save) save.textContent = i18nT("save_settings");
  translateCoreUi();
}

function safeStartStorageKey(deviceId) {
  return `basswiesn_safe_start_volume_enabled_${deviceId || "none"}`;
}

function syncSafeStartControl() {
  const deviceId = document.getElementById("key-device-select")?.value || "";
  const checkbox = document.getElementById("key-safe-volume-enabled");
  const field = document.getElementById("key-safe-volume-field");
  if (!checkbox) return;
  const stored = localStorage.getItem(safeStartStorageKey(deviceId));
  checkbox.checked = stored === null ? state.systemSettings?.ui_mode !== "easy" : stored === "true";
  if (field) field.hidden = !checkbox.checked;
}

function selectedSafeStartVolume() {
  if (!document.getElementById("key-safe-volume-enabled")?.checked) return null;
  const value = Number(document.getElementById("key-safe-volume")?.value ?? 5);
  if (!Number.isInteger(value) || value < 0 || value > 100) throw new Error("Die sichere Startlautstärke muss zwischen 0 und 100 liegen.");
  return value;
}

function applyCapabilityUi() {
  const activeDeviceId = document.querySelector('.view.is-active select[name="device_id"]')?.value || "";
  const relevant = state.deviceCapabilities.filter((item) => !activeDeviceId || item.device_id === activeDeviceId);
  const supported = (feature) => relevant.some((item) => item.features?.[feature]);
  document.querySelectorAll("[data-capability]").forEach((element) => {
    if (element.closest(".topnav")) {
      element.hidden = false;
      return;
    }
    const alternatives = element.dataset.capability.split(/\s+/).filter(Boolean);
    element.hidden = !alternatives.some(supported);
  });
  const fieldCapabilities = { bass: "dsp", clockDisplay: "clockDisplay", rebroadcastlatencymode: "rebroadcastlatencymode" };
  for (const [name, feature] of Object.entries(fieldCapabilities)) {
    document.querySelectorAll(`[name="${name}"]`).forEach((control) => { const label = control.closest("label"); if (label) label.hidden = !supported(feature); });
  }
  document.querySelectorAll(".portable-only-setting").forEach((element) => { element.hidden = true; });
}

document.addEventListener("change", (event) => { if (event.target.matches('select[name="device_id"]')) applyCapabilityUi(); });

function renderMultiroomScenarios() {
  const view = document.getElementById("view-multiroom");
  if (view) view.hidden = false;
  const box = document.getElementById("multiroom-scenarios");
  if (!box) return;
  const scenarios = Array.isArray(state.multiroomScenarios) ? state.multiroomScenarios : state.lastKnownMultiroomState.scenarios;
  box.innerHTML = scenarios.length
    ? scenarios.map((scenario) => `<div class="event-row"><span>${statusPill("BASSWIESN_MULTIROOM_PRESET")} · Hauptradio ${escapeHtml(text(scenario.master_device_id))} · ${(scenario.member_device_ids || []).length} Räume · ${scenario.preserve_volumes ? "kein SetVolume durch BASSWIESN" : "mit Lautstärkelogik"}</span><strong>${escapeHtml(scenario.name)}</strong><small>Nicht im Radio gespeichert · BASSWIESN-Server erforderlich · manuelle WebUI-Aktivierung · Sender ${escapeHtml(stationName(scenario.station_id))}. Eine gespeicherte Tasten-Zuordnung löst derzeit nichts automatisch aus.</small><div class="button-row"><button class="command primary" data-scenario-activate="${scenario.id}" type="button" ${state.multiroomPendingScenarioId === scenario.id ? "" : "disabled"}>${state.multiroomPendingScenarioId === scenario.id ? "Vorschau bestätigen und starten" : "Zuerst Vorschau öffnen"}</button><button class="command" data-scenario-preview="${scenario.id}" type="button">Vorschau und Details</button><button class="command" data-scenario-delete="${scenario.id}" type="button">Löschen</button></div></div>`).join("")
    : `<div class="empty">No multiroom scenarios saved.</div>`;
  state.lastKnownMultiroomState.rendered = true;
}

function renderGuidedSetup(plan) {
  const box = document.getElementById("guided-setup-steps");
  if (!box) return;
  if (!plan) {
    box.innerHTML = `<div class="empty">Setup-Plan laden, nachdem ein Radio ausgewählt wurde.</div>`;
    return;
  }
  box.innerHTML = (plan.steps || []).map((step) => `<div class="event-row ${rowStatusClass(step.status)}"><span>${statusPill(step.status)}</span><strong>${escapeHtml(step.title)}</strong><small>${escapeHtml(step.action)}</small></div>`).join("");
}

function renderSetupWizardChecks(result) {
  const box = document.getElementById("setup-wizard-checks");
  if (!box) return;
  const checks = result?.checks || result?.preflight?.checks || [];
  box.innerHTML = checks.length
    ? checks.map((check) => `<div class="event-row ${check.ok ? "status-ok" : "status-risk"}"><span>${check.ok ? "OK" : "Check"}</span><strong>${escapeHtml(check.name)}</strong><small>${escapeHtml(check.message || "")}</small></div>`).join("")
    : `<div class="empty">No wizard checks yet.</div>`;
}

function setupCountdownText(item) {
  const estimate = Number(item.estimated_seconds || 390);
  const estimatedSeconds = estimate > 0 && estimate <= 3600 ? estimate : 390;
  if (!item.started_at || item.status !== "running") return item.status === "queued" ? formatClockSeconds(estimatedSeconds) : "";
  const started = new Date(item.started_at).getTime();
  if (!Number.isFinite(started)) return formatClockSeconds(estimatedSeconds);
  const elapsed = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const remaining = Math.max(0, estimatedSeconds - elapsed);
  return formatClockSeconds(remaining);
}

function renderSetupBatch() {
  const body = document.getElementById("setup-batch-devices");
  if (body) {
    body.innerHTML = state.setupDevices.length
      ? state.setupDevices.map((device) => `<tr><td><input type="checkbox" data-setup-device="${escapeHtml(device.device_id)}"></td><td>${escapeHtml(text(device.name, device.device_id))}</td><td><code>${escapeHtml(device.device_id)}</code></td><td>${escapeHtml(text(device.ip))}</td><td>${escapeHtml(text(device.model))}</td><td>${statusPill(device.ssh_status || (device.ssh_ready ? "ssh_ready" : "unavailable"))}</td><td>${statusPill(device.port_17000_status || (device.port_17000_available ? "available" : "unavailable"))}</td><td>${statusPill(device.ready_status)}</td><td>${escapeHtml(text(device.configured_for))}</td></tr>`).join("")
      : `<tr><td colspan="9">Keine Radios gefunden.</td></tr>`;
  }
  const box = document.getElementById("setup-batch-status");
  if (!box) return;
  const job = state.setupJob;
  if (!job) {
    box.innerHTML = `<div class="empty">Noch kein Batch-Setup gestartet.</div>`;
    return;
  }
  const setupSteps = ["volume_safety", "source_bootstrap", "cloud_route", "host_redirect", "reboot", "verify", "volume_safety_verify", "preset_checker", "activation_playback"];
  const labels = { volume_safety: "volume safety", source_bootstrap: "source bootstrap", cloud_route: "cloud route", host_redirect: "host redirect", reboot: "reboot", verify: "verify", volume_safety_verify: "volume verify", preset_checker: "preset seed/check", activation_playback: "activation playback" };
  const rows = (job.devices || []).map((item) => {
    const currentIndex = setupSteps.indexOf(item.step);
    const steps = setupSteps.map((step, index) => {
      const cls = item.status === "ready" || (item.status === "running" && index < currentIndex) ? "is-success" : item.step === step ? "is-current" : item.status === "failed" && item.step === step ? "is-failed" : "";
      return `<span class="setup-mini-step ${cls}">${escapeHtml(labels[step] || step)}</span>`;
    }).join("");
    const ready = item.status === "ready" ? `<strong>✓ READY FOR BASSWIESN</strong>` : `<strong>${escapeHtml(item.step_label || item.status)}</strong>`;
    return `<div class="event-row ${rowStatusClass(item.status)}"><span>${statusPill(item.status)} ${escapeHtml(setupCountdownText(item))}</span><strong>${escapeHtml(text(item.name, item.device_id))} · ${escapeHtml(text(item.ip))}</strong><small>Device ID ${escapeHtml(item.device_id)}</small>${ready}<small>${steps}${item.error ? `<br>${escapeHtml(item.error)}` : ""}</small></div>`;
  }).join("");
  const summary = job.summary || {};
  const done = !job.running && job.finished_at;
  const finish = done ? `<div class="event-row status-ready"><span>Setup abgeschlossen</span><strong>Erfolgreiche Radios: ${escapeHtml(text(summary.successful, 0))} · Fehlgeschlagene Radios: ${escapeHtml(text(summary.failed, 0))}</strong><small>Erfolgreiche Radios wurden durch eine erste 30-Sekunden-Wiedergabe aktiviert. Bei Fehlern zuerst die Aktivierungs-Wiedergabe erneut starten.</small><button class="command primary" data-view-jump="presets" type="button">Zu Presets</button></div>` : "";
  box.innerHTML = rows + finish;
  if (done && Number(summary.successful || 0) > 0) maybeStartGuidedPresetSetup();
}

function guidedPresetKey(deviceId) {
  return `basswiesn_guided_preset_${deviceId}`;
}

function maybeStartGuidedPresetSetup() {
  const device = state.devices.find((item) => !localStorage.getItem(guidedPresetKey(item.device_id)));
  if (!device) return;
  state.guidedPreset = { active: true, deviceId: device.device_id, step: "presets", dismissed: false };
  renderGuidedPresetSetup();
}

function completeGuidedPresetSetup(message = "Fertig - dein Radio spielt.") {
  if (state.guidedPreset.deviceId) localStorage.setItem(guidedPresetKey(state.guidedPreset.deviceId), "completed");
  state.guidedPreset = { active: false, deviceId: "", step: "", dismissed: false };
  document.querySelectorAll(".guided-pulse").forEach((node) => node.classList.remove("guided-pulse"));
  document.getElementById("guided-preset-banner")?.remove();
  showToast(message);
}

function renderGuidedPresetSetup() {
  document.querySelectorAll(".guided-pulse").forEach((node) => node.classList.remove("guided-pulse"));
  const guide = state.guidedPreset;
  if (!guide.active || guide.dismissed) return;
  let banner = document.getElementById("guided-preset-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "guided-preset-banner";
    banner.className = "event-row status-ready";
    banner.innerHTML = `<span>Preset Guide</span><strong>Setup fertig. Richte jetzt Presets ein.</strong><button class="command" data-guided-preset-dismiss type="button">Überspringen</button>`;
    document.querySelector("#view-presets .page-head")?.after(banner);
  }
  const map = {
    presets: '.nav-button[data-view="presets"]',
    search: '#online-search-form input[name="q"]',
    searchButton: '#online-search-form button[type="submit"]',
    add: '#online-station-results [data-online-station]',
    slot: '#preset-slot-grid [data-slot="1"]',
    save: '#preset-form button[type="submit"]',
    play: '#preset-slot-grid [data-play-preset-slot="1"]',
  };
  const target = document.querySelector(map[guide.step] || map.presets);
  target?.classList.add("guided-pulse");
}


const SETUP_FLOW_STEPS = [
  { key: "server", label: "Server", help: "LAN-IP erkennen" },
  { key: "radio", label: "Radio", help: "Geraet lokal auswaehlen" },
  { key: "preflight", label: "Preflight", help: "Ports und Route pruefen" },
  { key: "backup", label: "Backup", help: "optional" },
  { key: "route", label: "Route", help: "Dry-Run Diff" },
  { key: "apply", label: "Apply", help: "Confirmation" },
  { key: "verify", label: "Verify", help: "Nach Reboot pruefen" },
  { key: "done", label: "Fertig", help: "Plan sichern" },
];

function currentSetupDeviceId() {
  const selected = document.getElementById("setup-wizard-device")?.value || document.getElementById("cloud-route-device")?.value;
  const planned = state.setupLastPreflight?.device_id || state.guidedSetupPlan?.device_id || state.guidedSetupPlan?.device?.device_id;
  const radioIp = state.guidedSetupPlan?.radio_ip || state.guidedSetupPlan?.device?.ip_address;
  return selected || planned || state.devices.find((item) => item.ip_address === radioIp)?.device_id || state.devices[0]?.device_id || "";
}

async function setupCountdown(seconds, button, output) {
  button.disabled = true;
  for (let remaining = seconds; remaining > 0; remaining -= 1) {
    output.textContent = `Radio wird umgebogen und neu gestartet.\nBitte warten: ${String(remaining).padStart(2, "0")} Sekunden`;
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  renderAboutContent();
}

async function finishSetup(button) {
  const deviceId = currentSetupDeviceId();
  if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
  const status = document.getElementById("setup-finish-status");
  const host = currentSetupHost();
  try {
    const applyPromise = postJson(`/api/setup/cloud-route/${encodeURIComponent(deviceId)}/apply`, { dry_run: false, reboot: true, host, port: Number(cloudPort) })
      .then((value) => ({ value }), (error) => ({ error }));
    await setupCountdown(60, button, status);
    const apply = await applyPromise;
    if (apply.error) throw apply.error;
    const verify = await postJson(`/api/setup/cloud-route/${encodeURIComponent(deviceId)}/verify`, { host, port: Number(cloudPort) });
    if (verify.status !== "ready") throw new Error(JSON.stringify(verify));
    state.setupFlowStep = 0;
    state.setupFlowDone = {};
    status.innerHTML = `<strong>${escapeHtml(i18nT("success"))}: BASSWIESN</strong><br><button class="command primary" id="setup-to-presets" type="button">${escapeHtml(i18nT("presets"))}</button>`;
    document.getElementById("setup-to-presets")?.addEventListener("click", () => document.querySelector('.nav-button[data-view="presets"]')?.click());
    renderSetupFlow();
  } catch (error) {
    status.innerHTML = `<strong>${escapeHtml(i18nT("failed"))}</strong><br>${escapeHtml(String(error))}<br><small>Prüfe die angezeigte Ursache und den Rollback-Status, bevor du erneut startest.</small>`;
  } finally {
    button.disabled = false;
  }
}

function currentSetupHost() {
  const value = document.getElementById("setup-batch-host")?.value || document.getElementById("setup-wizard-host")?.value || document.querySelector("#setup-wizard-form input[name=host]")?.value || document.getElementById("cloud-route-host")?.value || state.systemSettings?.lan_host || state.setupWizardServer?.recommended_host || window.location.hostname;
  return String(value || "").trim();
}

function syncSetupControls() {
  const host = currentSetupHost();
  [document.getElementById("setup-batch-host"), document.getElementById("setup-wizard-host"), document.querySelector("#setup-wizard-form input[name=host]"), document.getElementById("cloud-route-host"), document.querySelector("#setup-live-test-form input[name=host]")].forEach((input) => {
    if (input && !input.value && host) input.value = host;
  });
  const target = document.getElementById("setup-batch-cloud-target");
  if (target) target.textContent = host ? `http://${host}:${cloudPort}` : "wird erkannt";
  const deviceId = currentSetupDeviceId();
  [document.getElementById("setup-wizard-device"), document.getElementById("cloud-route-device"), document.getElementById("setup-live-test-device"), document.getElementById("guided-setup-device")].forEach((select) => {
    if (select && deviceId && select.value !== deviceId) select.value = deviceId;
  });
  const hiddenDevice = document.querySelector("#setup-wizard-form input[name=device_id]");
  if (hiddenDevice) hiddenDevice.value = deviceId;
  const confirmation = document.getElementById("cloud-route-confirmation");
  if (confirmation) confirmation.placeholder = "yes";
}

async function persistSetupHost(host) {
  const value = String(host || "").trim();
  if (!value || value === state.systemSettings?.lan_host) return;
  state.systemSettings = await postJson("/api/system/settings", { lan_host: value });
  renderSystemSettings();
  syncSetupControls();
  updateServerIdentity();
}

const SETUP_REBUILD_STEP_BY_STATE = {
  UNKNOWN: "identity", DISCOVERED: "identity", IDENTIFIED: "backup", BACKUP_PENDING: "backup", BACKUP_COMPLETE: "route",
  SSH_STATUS_PENDING: "ssh", SSH_ALREADY_ACTIVE: "ssh", SSH_ACTIVATION_PENDING: "ssh", SSH_TEMPORARY_ACTIVE: "ssh",
  SSH_PERSISTENCE_PENDING: "ssh", SSH_REBOOT_PENDING: "ssh", SSH_VERIFIED: "route", ROUTING_BACKUP_COMPLETE: "route",
  BASSWIESN_ROUTE_PENDING: "route", BASSWIESN_ROUTE_ACTIVE: "route", RADIO_REBOOT_PENDING: "verify", RADIO_REBOOTED: "verify", RADIO_RECONNECT_PENDING: "verify", RADIO_REACHABLE: "verify",
  PRESETS_READABLE: "verify", PLAYBACK_READY: "verify", VERIFIED: "done", FAILED: "identity", ROLLBACK_PENDING: "route", ROLLED_BACK: "done",
};

function setupRebuildHost() {
  return String(document.getElementById("setup-rebuild-host")?.value || "").trim();
}

function setupRebuildSelectedIds() {
  return Array.from(document.querySelectorAll("[data-setup-rebuild-device]:checked")).map((node) => node.dataset.setupRebuildDevice).filter(Boolean);
}

function setupRebuildCurrentJobDevice(deviceId) {
  return (state.setupRebuildJob?.devices || []).find((item) => item.device_id === deviceId) || null;
}

function renderSetupRebuildDiscovery() {
  const container = document.getElementById("setup-rebuild-discovery-status");
  if (!container) return;
  const result = state.setupRebuildDiscovery;
  if (!result) {
    container.innerHTML = `<div class="empty">Noch keine ausdrückliche LAN-Suche in dieser Sitzung ausgeführt.</div>`;
    return;
  }
  if (result.running) {
    container.innerHTML = `<div class="setup-rebuild-discovery-result"><strong>Verbundene Radios werden gesucht …</strong><span>Nur SSDP-Multicast und anschließend /info für genau die neu gefundenen, ungeschützten Radios.</span></div>`;
    return;
  }
  const failures = (result.failures || []).map((item) => `<div class="setup-rebuild-error"><strong>${escapeHtml(item.device_id || "Nicht auswählbar")}</strong>: ${escapeHtml(item.reason || "Identitätsprüfung fehlgeschlagen")}</div>`).join("");
  const verified = Number(result.verified || 0);
  const found = Number(result.found || 0);
  container.innerHTML = `<div class="setup-rebuild-discovery-result"><strong>${escapeHtml(String(verified))} von ${escapeHtml(String(found))} gefundenen Radios sicher bestätigt</strong><span>WLAN-Einstellungen wurden nicht verändert.${result.descriptor_failures ? ` ${escapeHtml(String(result.descriptor_failures))} SSDP-Antwort(en) konnten nicht als SoundTouch-Gerät bestätigt werden.` : ""}</span>${found === 0 ? `<small>Prüfe, ob die Radios vom Benutzer bereits mit demselben Heimnetz wie BASSWIESN verbunden wurden.</small>` : ""}</div>${failures ? `<details open><summary>Geräte mit Fehlern</summary>${failures}</details>` : ""}`;
}

function renderSetupRebuild() {
  const list = document.getElementById("setup-rebuild-devices");
  if (!list) return;
  const jobDevices = new Map((state.setupRebuildJob?.devices || []).map((item) => [item.device_id, item]));
  if (!state.setupRebuildDevices.length) {
    list.innerHTML = `<div class="empty"><strong>Noch kein geeignetes Radio vorhanden.</strong><br>Verbinde jedes Radio zuerst selbst mit dem Heimnetz und klicke anschließend oben auf „Jetzt verbundene Radios suchen“.</div>`;
  } else {
    list.innerHTML = state.setupRebuildDevices.map((device) => {
      const job = jobDevices.get(device.device_id) || {};
      const stateName = job.state || device.setup_state || "UNKNOWN";
      const route = job.routing_status || "unknown";
      const audioLocked = Boolean(device.audio_safety_locked || job.evidence?.audio_test_locked);
      const audioLabel = audioLocked ? "Wiedergabe gesperrt" : "Lautstärkegrenze 1";
      const audioReason = audioLocked ? (device.audio_safety_reason || job.evidence?.audio_lock_reason || "Frühere Lautstärkeabweichung") : "";
      const blocked = !device.eligible;
      const audioAction = blocked ? "" : `<button class="command" data-setup-audio-safety="${escapeHtml(device.device_id)}" type="button">${audioLocked ? "Audio-Sperre sicher prüfen" : "Audiotest sicher vorbereiten"}</button>${audioReason ? `<small>${escapeHtml(audioReason)}</small>` : ""}`;
      const selectedIds = state.setupRebuildJob?.selected_device_ids || state.setupRebuildPreview?.device_ids || [];
      const checked = Boolean(selectedIds.includes(device.device_id));
      const profile = blocked ? device.blocking_reason : `Profil bestätigt: ${device.profile_key}`;
      const simulationLabel = device.simulated ? `<small>Testmodus · garantiert ohne Netzwerkzugriff</small>` : "";
      const phase = job.phase || stateName;
      const lastSeen = device.last_seen_at ? new Date(device.last_seen_at).toLocaleString() : "nicht in dieser Installation bestätigt";
      const productEvidence = device.product_id_provenance === "RADIO_INFO" ? "vom Radio" : device.product_id_provenance === "PROFILE_DERIVED" ? "aus eindeutigem Profil" : "unbekannte Herkunft";
      return `<article class="setup-rebuild-device ${blocked ? "is-blocked" : ""}"><input type="checkbox" name="setup-rebuild-device" data-setup-rebuild-device="${escapeHtml(device.device_id)}" ${checked && !blocked ? "checked" : ""} ${blocked ? "disabled" : ""} aria-label="${escapeHtml(device.name)} auswählen"><div><strong>${escapeHtml(device.name)}</strong><small>${escapeHtml(device.device_id)} · ${escapeHtml(device.ip_address)}</small><small>Zuletzt bestätigt: ${escapeHtml(lastSeen)}</small>${simulationLabel}</div><div><small>${escapeHtml(device.model || "Modell unbekannt")} · ${escapeHtml(device.firmware || "Firmware unbekannt")}</small><small>${escapeHtml(device.product_id || "Product-ID unbekannt")} (${escapeHtml(productEvidence)}) · ${escapeHtml(device.variant || "Variant unbekannt")} · ${escapeHtml(device.platform || "Plattform unbekannt")}</small><small>${escapeHtml(profile)}</small></div><div><small>Pfad: HTTP 8090 + CLI 17000</small><small>Routing: ${escapeHtml(route)} · ${escapeHtml(audioLabel)}</small>${audioAction}</div><div class="setup-rebuild-device-status">${statusPill(blocked ? "Profil prüfen" : stateName === "VERIFIED" ? "bereit" : stateName === "FAILED" ? "Fehler" : phase, blocked || stateName === "FAILED" ? "bad" : stateName === "VERIFIED" ? "ready" : "pending")}</div></article>`;
    }).join("");
  }
  const job = state.setupRebuildJob;
  const running = !!job && ["pending", "running"].includes(job.status);
  const finished = !!job && ["completed", "partial_failure", "failed", "cancelled", "rolled_back", "rollback_limited", "rollback_failed", "rollback_not_required"].includes(job.status);
  const start = document.getElementById("setup-rebuild-start");
  const cancel = document.getElementById("setup-rebuild-cancel");
  const rollback = document.getElementById("setup-rebuild-rollback");
  if (start) start.disabled = running;
  if (cancel) cancel.disabled = !running;
  if (rollback) rollback.disabled = !finished || !(job.devices || []).some((item) => item.backup_path);
  document.querySelectorAll("[data-setup-rebuild-step]").forEach((node) => {
    const key = node.dataset.setupRebuildStep;
    const states = (job?.devices || []).map((item) => item.state);
    const current = states.some((value) => SETUP_REBUILD_STEP_BY_STATE[value] === key);
    const done = key === "done" ? states.length > 0 && states.every((value) => ["VERIFIED", "ROLLED_BACK"].includes(value)) : states.length > 0 && states.every((value) => {
      const index = ["identity", "backup", "route", "verify", "done"].indexOf(SETUP_REBUILD_STEP_BY_STATE[value]);
      return index > ["identity", "backup", "route", "verify", "done"].indexOf(key);
    });
    node.classList.toggle("is-current", current);
    node.classList.toggle("is-done", done);
  });
  const status = document.getElementById("setup-rebuild-status");
  if (status) {
    if (!job) status.innerHTML = `<div class="empty">Noch kein Setup-Rebuild gestartet.</div>`;
    else {
      const errors = (job.devices || []).filter((item) => item.last_error).map((item) => `<div class="setup-rebuild-error"><strong>${escapeHtml(item.device_id)}</strong>: ${escapeHtml(item.last_error)}</div>`).join("");
      const limited = job.status === "rollback_limited" ? `<div class="setup-rebuild-error"><strong>Begrenzter Routing-Rollback</strong>: Der unmittelbare CLI-Readback stimmt, aber Account-/Environmentzustand und Persistenz nach einem späteren Reboot sind nicht vollständig restauriert. Das ist kein Full Rollback.</div>` : "";
      const summary = job.summary || {};
      status.innerHTML = `<div class="setup-rebuild-status-grid"><div><small>Job</small><strong>${escapeHtml(job.status)}</strong></div><div><small>Fortschritt</small><strong>${escapeHtml(String(job.progress ?? 0))}%</strong></div><div><small>Aktuelles Radio</small><strong>${escapeHtml(job.current_device_id || "—")}</strong></div><div><small>Phase</small><strong>${escapeHtml(job.phase || job.current_state || "QUEUED")}</strong></div></div><div class="setup-rebuild-summary"><strong>${escapeHtml(String(summary.verified ?? 0))}/${escapeHtml(String(summary.total ?? (job.devices || []).length))} Radios verifiziert</strong>${summary.failed ? ` · ${escapeHtml(String(summary.failed))} fehlgeschlagen` : ""}</div>${limited}${errors ? `<details open><summary>Fehlerdetails pro Radio</summary>${errors}</details>` : ""}`;
    }
  }
  const output = document.getElementById("setup-rebuild-output");
  if (output && (state.setupRebuildJob || state.setupRebuildPreview || state.setupRebuildDevices.length)) {
    output.textContent = JSON.stringify(state.setupRebuildJob || state.setupRebuildPreview || { devices: state.setupRebuildDevices }, null, 2);
  }
  renderSetupRebuildDiscovery();
}

function renderSetupRebuildTargets() {
  const select = document.getElementById("setup-rebuild-host");
  const help = document.getElementById("setup-rebuild-host-help");
  if (!select) return;
  const previous = select.value;
  if (!state.setupRebuildTargets.length) {
    select.innerHTML = `<option value="">Keine geeignete LAN-Adresse erkannt</option>`;
    select.disabled = true;
    if (help) help.textContent = "Verbinde BASSWIESN mit demselben LAN wie das Radio und aktualisiere die Auswahl.";
    return;
  }
  select.disabled = false;
  select.innerHTML = state.setupRebuildTargets.map((item) => `<option value="${escapeHtml(item.host)}">${escapeHtml(item.host)} · ${escapeHtml(item.interface)}${item.configured ? " · konfiguriert" : ""}</option>`).join("");
  if (state.setupRebuildTargets.some((item) => item.host === previous)) select.value = previous;
  if (help) help.textContent = state.setupRebuildTargets.length > 1
    ? "Mehrere LAN-Adressen sind erreichbar. Wähle das Netz, in dem sich das Radio befindet."
    : "Diese LAN-Adresse wird dem Radio als BASSWIESN-Serverziel angezeigt.";
}

async function refreshSetupRebuildDevices() {
  const [devices, targets] = await Promise.all([
    getJson("/api/setup/rebuild/devices"),
    getJson("/api/setup/rebuild/server-targets"),
  ]);
  state.setupRebuildDevices = devices;
  state.setupRebuildTargets = targets.candidates || [];
  renderSetupRebuildTargets();
  renderSetupRebuild();
  return state.setupRebuildDevices;
}

async function pollSetupRebuildJob(jobId) {
  if (!jobId) return;
  if (state.setupRebuildPoller) window.clearInterval(state.setupRebuildPoller);
  state.setupRebuildPoller = window.setInterval(async () => {
    try {
      state.setupRebuildJob = await getJson(`/api/setup/rebuild/jobs/${encodeURIComponent(jobId)}`);
      renderSetupRebuild();
      if (!["pending", "running"].includes(state.setupRebuildJob.status)) {
        window.clearInterval(state.setupRebuildPoller);
        state.setupRebuildPoller = null;
      }
    } catch (error) {
      window.clearInterval(state.setupRebuildPoller);
      state.setupRebuildPoller = null;
      const output = document.getElementById("setup-rebuild-output");
      if (output) output.textContent = String(error);
    }
  }, 1200);
}

async function loadSetupRebuildState() {
  try { state.setupRebuildDevices = await getJson("/api/setup/rebuild/devices"); } catch { state.setupRebuildDevices = []; }
  try {
    const targets = await getJson("/api/setup/rebuild/server-targets");
    state.setupRebuildTargets = targets.candidates || [];
  } catch { state.setupRebuildTargets = []; }
  try {
    const latest = await getJson("/api/setup/rebuild/jobs/latest");
    state.setupRebuildJob = latest?.job_id ? latest : null;
    if (state.setupRebuildJob && ["pending", "running"].includes(state.setupRebuildJob.status)) pollSetupRebuildJob(state.setupRebuildJob.job_id);
  } catch { state.setupRebuildJob = null; }
  renderSetupRebuildTargets();
  renderSetupRebuild();
}

async function refreshDeviceState({ live = true } = {}) {
  state.devices = await getJson(`/api/devices${live ? "?live=true" : ""}`);
  // The legacy batch setup is retired. Device refresh must never trigger
  // implicit SSH/CLI port probes; explicit Setup 2.0 actions own all hardware
  // preflights.
  state.setupDevices = [];
  try { state.deviceStatuses = await getJson("/api/devices/status-badges"); } catch { state.deviceStatuses = []; }
  try { state.deviceCapabilities = await getJson("/api/devices/ui-capabilities"); } catch { state.deviceCapabilities = []; }
  try { state.deviceHealth = await getJson("/api/devices/health"); } catch { state.deviceHealth = []; }
  try { state.liveComparison = await getJson("/api/devices/live-comparison"); } catch { state.liveComparison = null; }
  renderDevices();
  renderSetupFlow();
  renderSetupBatch();
  applyCapabilityUi();
  renderLiveComparison();
}

function markSetupStepDone(key, done = true) {
  state.setupFlowDone[key] = done;
  renderSetupFlow();
}

function showSetupConsole(id) {
  const output = document.getElementById(id);
  const details = output?.closest("details");
  if (details) details.open = true;
}

function renderSetupFlow() {
  syncSetupControls();
  const step = Math.max(0, Math.min(state.setupFlowStep, SETUP_FLOW_STEPS.length - 1));
  state.setupFlowStep = step;
  const progress = document.getElementById("setup-progress");
  if (progress) {
    progress.innerHTML = SETUP_FLOW_STEPS.map((item, index) => {
      const status = state.setupFlowDone[item.key] ? "is-done" : index === step ? "is-current" : index < step ? "is-open" : "";
      return `<div class="setup-progress-item ${status}"><span>${index + 1}</span><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.help)}</small></div>`;
    }).join("");
  }
  document.querySelectorAll(".setup-flow-step").forEach((panel) => panel.classList.toggle("is-current", Number(panel.dataset.setupStep) === step));
  const currentKey = SETUP_FLOW_STEPS[step]?.key;
  const next = document.querySelector("[data-setup-flow-next]");
  const prev = document.querySelector("[data-setup-flow-prev]");
  if (prev) prev.disabled = step === 0;
  if (next) {
    next.disabled = !state.setupFlowDone[currentKey] || step >= SETUP_FLOW_STEPS.length - 1;
    next.textContent = step >= SETUP_FLOW_STEPS.length - 2 ? "Abschliessen" : "Weiter";
  }
  const hint = document.getElementById("setup-flow-hint");
  if (hint) hint.textContent = state.setupFlowDone[currentKey] ? "Schritt erledigt. Weiter ist freigeschaltet." : "Fuehre den aktuellen Schritt aus oder ueberspringe ihn bewusst, wenn erlaubt.";
}

async function runSetupFlowAction(action) {
  syncSetupControls();
  const deviceId = currentSetupDeviceId();
  const host = currentSetupHost();
  const output = document.getElementById("setup-wizard-output");
  try {
    if (action !== "detect") await persistSetupHost(host);
    if (action === "detect") {
      const result = await detectSetupWizardServer();
      showSetupConsole("setup-wizard-output");
      output.textContent = JSON.stringify(result, null, 2);
      markSetupStepDone("server");
      return result;
    }
    if (action === "radio") {
      if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
      markSetupStepDone("radio");
      return { device_id: deviceId };
    }
    if (action === "preflight") {
      if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
      const form = document.getElementById("setup-wizard-form");
      const data = form ? new FormData(form) : new FormData();
      const result = await postJson(`/api/setup/wizard/preflight/${encodeURIComponent(deviceId)}`, { host, port: Number(cloudPort), reboot: data.get("reboot") === "on", capture_backup: false, force: data.get("force") === "on" });
      state.setupLastPreflight = result;
      renderSetupWizardChecks(result);
      showSetupConsole("setup-wizard-output");
      output.textContent = JSON.stringify(result, null, 2);
      markSetupStepDone("preflight");
      return result;
    }
    if (action === "backup") {
      if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
      const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/backup/plan`, {});
      showSetupConsole("setup-output");
      document.getElementById("setup-output").textContent = JSON.stringify(result, null, 2);
      markSetupStepDone("backup");
      return result;
    }
    if (action === "skip-backup") {
      showSetupConsole("setup-output");
      document.getElementById("setup-output").textContent = "Backup wurde bewusst uebersprungen. Fuer echte Writes bleibt ein echtes Backup empfohlen.";
      markSetupStepDone("backup");
      return { skipped: true };
    }
    if (action === "route-preview") {
      if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
      const result = await postJson(`/api/setup/cloud-route/${encodeURIComponent(deviceId)}`, { host, port: Number(cloudPort), reboot: document.querySelector("#cloud-route-form input[name=reboot]")?.checked ?? true });
      state.setupLastRoute = result;
      showSetupConsole("cloud-route-output");
      document.getElementById("cloud-route-output").textContent = formatCloudRouteResult(result);
      markSetupStepDone("route");
      return result;
    }
    if (action === "apply") {
      if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
      const dryRun = document.getElementById("setup-apply-dry-run")?.checked ?? true;
      const confirmation = document.getElementById("cloud-route-confirmation")?.value || "";
      const result = await postJson(`/api/setup/cloud-route/${encodeURIComponent(deviceId)}/apply`, { host, port: Number(cloudPort), reboot: !dryRun, dry_run: dryRun, confirmation });
      showSetupConsole("cloud-route-output");
      document.getElementById("cloud-route-output").textContent = formatCloudRouteResult(result);
      markSetupStepDone("apply");
      if (!dryRun) await loadAll();
      return result;
    }
    if (action === "rollback") {
      if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
      const confirmation = document.getElementById("cloud-route-confirmation")?.value || "";
      const result = await postJson(`/api/setup/cloud-route/${encodeURIComponent(deviceId)}/rollback`, { dry_run: false, reboot: true, confirmation });
      showSetupConsole("cloud-route-output");
      document.getElementById("cloud-route-output").textContent = formatCloudRouteResult(result);
      return result;
    }
    if (action === "verify") {
      if (!deviceId) throw new Error("Kein Radio ausgewaehlt.");
      const result = await postJson(`/api/setup/cloud-route/${encodeURIComponent(deviceId)}/verify`, { host, port: Number(cloudPort) });
      showSetupConsole("setup-output");
      document.getElementById("setup-output").textContent = formatCloudRouteResult(result);
      markSetupStepDone("verify");
      return result;
    }
  } catch (error) {
    if (output) output.textContent = String(error);
    throw error;
  } finally {
    markRiskPanels();
    renderSetupFlow();
  }
}

async function detectSetupWizardServer() {
  const result = await getJson("/api/setup/wizard/server-info");
  state.setupWizardServer = result;
  const hostInput = document.getElementById("setup-wizard-host");
  const cloudHostInput = document.getElementById("cloud-route-host");
  if (hostInput && result.recommended_host) hostInput.value = result.recommended_host;
  const wizardFormHost = document.querySelector('#setup-wizard-form input[name="host"]');
  if (wizardFormHost && result.recommended_host) wizardFormHost.value = result.recommended_host;
  const candidates = document.getElementById("setup-wizard-host-candidates");
  if (candidates) {
    candidates.innerHTML = (result.ip_candidates || []).map((item) => `<option value="${escapeHtml(item.ip)}">${escapeHtml(item.source)} · scan ${escapeHtml(item.suggested_cidr || "")}</option>`).join("");
  }
  const scanRange = document.querySelector('#network-scan-form input[name="cidr"]');
  if (scanRange && result.suggested_scan_cidr) scanRange.value = result.suggested_scan_cidr;
  if (cloudHostInput && !cloudHostInput.value && result.recommended_host) cloudHostInput.value = result.recommended_host;
  const cloudLink = document.querySelector('[data-service-link="cloud"]');
  const debugLink = document.querySelector('[data-service-link="debug"]');
  if (cloudLink && result.cloud_base_url) cloudLink.href = `${result.cloud_base_url.replace(/\/$/, "")}/about`;
  if (debugLink && result.debug_base_url) debugLink.href = `${result.debug_base_url.replace(/\/$/, "")}/`;
  const serverOutput = document.getElementById("setup-wizard-output");
  if (serverOutput) serverOutput.textContent = `${result.host_warning ? `${result.host_warning}\n\n` : ""}${JSON.stringify(result, null, 2)}`;
  if (result.host_warning) showToast(result.host_warning, "error");
  updateServerIdentity();
}

function renderOnlineStations() {
  const box = document.getElementById("online-station-results");
  if (!box) return;
  box.innerHTML = state.onlineStations.length
    ? state.onlineStations.map((station, index) => {
      const tags = String(station.tags || "").split(",").map((tag) => tag.trim()).filter(Boolean).slice(0, 3);
      const highAac = station.stream_format === "aac" && Number(station.stream_bitrate || station.bitrate || 0) >= 256;
      const format = station.is_hls ? "HLS Warnung" : highAac ? "AAC hohe Bitrate / eingeschränkt" : station.stream_format ? `${String(station.stream_format).toUpperCase()} geeignet` : "Unbekannt";
      const badges = [format, station.country, ...tags].filter(Boolean).map((tag) => `<span class="station-badge">${escapeHtml(tag)}</span>`).join("");
      const warning = station.compatibility_warning ? `<small class="form-message">${escapeHtml(station.compatibility_warning)}</small>` : "";
      return `<article class="station-result-card"><img src="/static/bmx-icons/orion/monochrome.svg" alt="Lokales Quellen-Symbol" loading="lazy"><div class="station-result-copy"><div class="station-badges">${badges}</div><strong>${escapeHtml(text(station.name))}</strong><small title="${escapeHtml(text(station.stream_url))}">${escapeHtml(stationHost(station.stream_url))}</small>${warning}</div><button class="command primary" data-online-station="${index}" type="button">Add &amp; select</button></article>`;
    }).join("")
    : `<div class="empty">No online search results loaded.</div>`;
}

function renderRequests() {
  const compact = state.requests.slice(0, 12).map((r) => `<div class="event-row"><span>${escapeHtml(text(r.service))}</span><strong>${escapeHtml(text(r.method))} ${escapeHtml(text(r.path))}</strong><small>${escapeHtml(text(r.status_code))} · ${escapeHtml(text(r.host))}</small></div>`).join("");
  document.getElementById("dashboard-requests").innerHTML = compact || `<div class="empty">No requests logged.</div>`;
  document.getElementById("debug-requests").innerHTML = state.requests.length
    ? state.requests.map((r) => `<div class="event-row"><span>${escapeHtml(text(r.ts))}</span><strong>${escapeHtml(text(r.service))} · ${escapeHtml(text(r.method))} ${escapeHtml(text(r.path))}</strong><small>${escapeHtml(text(r.status_code))} · ${escapeHtml(text(r.host))}</small></div>`).join("")
    : `<div class="empty">No requests logged.</div>`;
  document.getElementById("request-count").textContent = state.requests.length.toString();
}

function renderSettingsCatalog() {
  const box = document.getElementById("settings-catalog");
  if (!box) return;
  box.innerHTML = state.settingsCatalog.length
    ? state.settingsCatalog.map((item) => `<div class="event-row ${rowStatusClass(item.status)}"><span>${escapeHtml(item.area)} · ${statusPill(item.status)}</span><strong>${escapeHtml(item.endpoint)}</strong><small>${escapeHtml(item.note)}</small></div>`).join("")
    : `<div class="empty">No settings catalog loaded.</div>`;
}


function renderTelnet() {
  const list = document.getElementById("telnet-command-list");
  if (list) {
    list.innerHTML = state.telnetCommands.length
      ? state.telnetCommands.map((cmd) => {
          const note = cmd.key === "sys_reboot" ? `${cmd.note} · Ausführung nur über das Telnet-Reboot-Formular.` : cmd.note;
          return `<div class="event-row ${rowStatusClass(cmd.mode)}"><span>${statusPill(cmd.mode)}</span><strong>${escapeHtml(cmd.label)}</strong><small><code>${escapeHtml(cmd.command)}</code><br>${escapeHtml(note)}</small></div>`;
        }).join("")
      : `<div class="empty">No Telnet commands loaded.</div>`;
  }
}

function renderMediaLibrary() {
  const caps = document.getElementById("media-capabilities-output");
  if (caps) caps.textContent = JSON.stringify(state.mediaCapabilities || {}, null, 2);
  const playlists = document.getElementById("media-playlists");
  if (playlists) {
    playlists.innerHTML = state.mediaPlaylists.length
      ? state.mediaPlaylists.map((item) => `<div class="event-row"><span>${escapeHtml(item.source_type)} · ${escapeHtml(text(item.updated_at))}</span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(text(item.uri))}<br>${escapeHtml(text(item.notes))}</small></div>`).join("")
      : `<div class="empty">No NAS/media collections saved.</div>`;
  }
  const services = document.getElementById("service-catalog");
  if (services) {
    services.innerHTML = state.serviceCatalog.length
      ? state.serviceCatalog.map((item) => `<div class="event-row ${rowStatusClass(item.status)}"><span>${statusPill(item.status)}</span><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.path)} · ${escapeHtml(item.note)}</small></div>`).join("")
      : `<div class="empty">Service catalog not loaded.</div>`;
  }
}

function renderReferenceSetups() {
  const select = document.getElementById("reference-setup-select");
  if (select) {
    const previous = select.value;
    select.innerHTML = state.referenceSetups.length
      ? state.referenceSetups.map((setup) => `<option value="${setup.id}">${escapeHtml(setup.name)}</option>`).join("")
      : `<option value="">No reference setups</option>`;
    if (previous) select.value = previous;
  }
  const list = document.getElementById("reference-setups");
  if (list) {
    list.innerHTML = state.referenceSetups.length
      ? state.referenceSetups.map((setup) => `<div class="event-row"><span>${escapeHtml(text(setup.model_family))} · ${escapeHtml(text(setup.updated_at))}</span><strong>${escapeHtml(setup.name)}</strong><small>Source ${escapeHtml(text(setup.source_device_id))} · ${(setup.presets || []).length} preset slots · ${escapeHtml(text(setup.notes))}</small></div>`).join("")
      : `<div class="empty">No reference setup saved yet.</div>`;
  }
}

function renderStereoResearch() {
  const box = document.getElementById("stereo-research-output");
  if (box) box.textContent = JSON.stringify(state.stereoResearch || {}, null, 2);
}

function renderKeyCommands() {
  const select = document.getElementById("key-command-select");
  if (select) {
    select.innerHTML = state.keyCommands.map((cmd) => `<option value="${escapeHtml(cmd.key)}">${escapeHtml(cmd.label)}</option>`).join("");
  }
  const grid = document.getElementById("key-command-grid");
  if (grid) {
    const first = ["POWER", "SOURCE", "TV", "AUX_INPUT", "BLUETOOTH", "PRESET_1", "PRESET_2", "PRESET_3", "PRESET_4", "PRESET_5", "PRESET_6", "PREVIOUS", "PLAY_PAUSE", "NEXT", "PREV_TRACK", "PLAY", "PAUSE", "NEXT_TRACK", "VOLUME_UP", "MUTE", "VOLUME_DOWN", "STOP", "SHUFFLE", "REPEAT"];
    const byKey = new Map(state.keyCommands.map((cmd) => [cmd.key, cmd]));
    const ordered = [...first.map((key) => byKey.get(key)).filter(Boolean), ...state.keyCommands.filter((cmd) => !first.includes(cmd.key))];
    const iconFor = (key, label) => ({ POWER: "⏻", SOURCE: "SRC", TV: "TV", AUX_INPUT: "AUX", BLUETOOTH: "BT", PLAY_PAUSE: "▶/Ⅱ", PREVIOUS: "⏮", PREV_TRACK: "⏮", SKIP_BACK: "⏪", NEXT: "⏭", NEXT_TRACK: "⏭", SKIP_FORWARD: "⏩", PLAY: "▶", PAUSE: "Ⅱ", STOP: "■", VOLUME_UP: "+", VOLUME_DOWN: "−", MUTE: "M", SHUFFLE: "S", REPEAT: "R" }[key] || (key.startsWith("PRESET_") ? key.replace("PRESET_", "") : label));
    const classFor = (key) => key === "POWER" ? "remote-power" : key === "PLAY_PAUSE" ? "remote-play-pause" : key.startsWith("PRESET_") ? "remote-preset" : key.includes("VOLUME") || key === "MUTE" ? "remote-volume" : "";
    grid.innerHTML = ordered.map((cmd) => `<button class="command ${classFor(cmd.key)}" data-key-command="${escapeHtml(cmd.key)}" title="${escapeHtml(cmd.label)}" type="button"><span class="remote-icon">${escapeHtml(iconFor(cmd.key, cmd.label))}</span><small>${escapeHtml(cmd.label)}</small></button>`).join("");
  }
}

function renderDisplayModes() {
  const select = document.getElementById("display-mode-select");
  if (select) {
    select.innerHTML = state.displayModes.map((mode) => `<option value="${escapeHtml(mode.key)}">${escapeHtml(mode.label)}</option>`).join("");
    if (state.systemSettings?.display_metadata_mode) select.value = state.systemSettings.display_metadata_mode;
  }
  const list = document.getElementById("display-mode-list");
  if (list) {
    list.innerHTML = state.displayModes.length
      ? state.displayModes.map((mode) => `<div class="event-row ${mode.writes_radio ? "status-warning" : "status-risk"}"><span>${statusPill(mode.writes_radio ? "writes radio" : "runtime only")}</span><strong>${escapeHtml(mode.label)}</strong><small>${escapeHtml((mode.fields || []).join(" + ") || "disabled")}</small></div>`).join("")
      : `<div class="empty">No display modes loaded.</div>`;
  }
}


function renderLiveComparison() {
  const box = document.getElementById("device-live-comparison");
  const output = document.getElementById("device-live-comparison-output");
  if (!box || !output) return;
  const data = state.liveComparison || { devices: [], comparison: {} };
  output.textContent = JSON.stringify(data, null, 2);
  const devices = data.devices || [];
  if (!devices.length) {
    box.innerHTML = `<div class="empty">Noch keine Live-Captures gespeichert.</div>`;
    return;
  }
  const commonWriteCount = (data.comparison?.common_write_or_control || []).length;
  const mismatchCount = (data.comparison?.device_id_mismatches || []).length;
  const header = `<div class="event-row ${mismatchCount ? "status-warning" : ""}"><span>${statusPill("read-only")}</span><strong>${devices.length} Radios verglichen</strong><small>${commonWriteCount} gemeinsame Write-/Control-Endpunkte · ${mismatchCount} ID-Abweichung(en) zwischen lokalem Datensatz und Radio-/info.</small></div>`;
  const rows = devices.map((item) => `<div class="event-row ${item.device_id_matches_radio ? "" : "status-warning"}"><span>${escapeHtml(text(item.ip_address))}</span><strong>${escapeHtml(text(item.name))}</strong><small>Radio-ID ${escapeHtml(text(item.radio_device_id))} · lokal ${escapeHtml(text(item.device_id))}<br>${escapeHtml(text(item.model))} · ${escapeHtml(text(item.marge_url))}<br>${item.endpoint_counts.read_or_probe} Read/Probe · ${item.endpoint_counts.write_or_control} Write/Control · HTTP Captures ${item.captures.http_capture_count} · CLI17000 ${item.captures.cli17000_readonly ? "ok" : "fehlt"}</small></div>`).join("");
  box.innerHTML = header + rows;
}

function renderDeviceInfoCleartext(result) {
  const box = document.getElementById("device-info-cleartext");
  if (!box || !result) return;
  const device = result.device || result;
  const nonSsh = result.non_ssh || {};
  box.innerHTML = `<div class="event-row"><span>Name</span><strong>${escapeHtml(text(device.name))}</strong><small>Model ${escapeHtml(text(device.model))} · Firmware ${escapeHtml(text(device.firmware))}</small></div>`
    + `<div class="event-row"><span>Network</span><strong>${escapeHtml(text(device.ip_address))}</strong><small>Config ${escapeHtml(text(device.configured_for))} · Ready means: local record has enough reachable/probed data, not final setup complete.</small></div>`
    + `<div class="event-row"><span>Cloud target</span><strong>${escapeHtml(text(nonSsh.target_cloud_host || device.marge_url))}</strong><small>Port ${escapeHtml(text(nonSsh.target_cloud_port || "unknown"))}</small></div>`;
}

function renderRegistry() {
  document.getElementById("registry-preview").textContent = JSON.stringify(state.registry, null, 2);
}

let serviceStatusRefreshRunning = false;
async function refreshServiceStatus() {
  if (serviceStatusRefreshRunning) return;
  serviceStatusRefreshRunning = true;
  try {
    const health = await getJson("/api/system/service-health");
    setStatus("cloud-state", Boolean(health.cloud?.online), health.cloud?.online ? "online" : "offline");
    const debugSummary = document.getElementById("debug-summary");
    const debugOnline = Boolean(health.debug?.online);
    if (debugSummary) debugSummary.textContent = debugOnline ? "Online" : "Eingeschränkt";
    setStatus("debug-state", debugOnline, debugOnline ? "online" : "Diagnose-Port prüfen", debugOnline ? "ok" : "warn");
    reportServiceStatus("cloud", Boolean(health.cloud?.online), health.cloud?.error || "");
    reportServiceStatus("debug", Boolean(health.debug?.online), health.debug?.error || "");
    const urls = serviceUrls();
    const noStore = { cache: "no-store" };
    try {
      if (health.cloud?.online) await getJson(`${urls.cloud}/about`, noStore);
      state.registry = health.cloud?.online ? await getJson(`${urls.cloud}/bmx/registry/v1/services`, noStore) : { error: "Cloud registry unavailable" };
    } catch (error) {
      state.registry = { error: "Cloud registry unavailable" };
    }
    renderRegistry();
    try {
      if (health.debug?.online) await getJson(`${urls.debug}/`, noStore);
      state.requests = health.debug?.online ? await getJson(`${urls.debug}/requests`, noStore) : [];
    } catch (error) {
      const debugState = document.getElementById("debug-state");
      if (health.debug?.online && debugState) {
        debugState.textContent = "Browser-Link eingeschränkt";
        debugState.className = "warn";
        debugState.title = "Browser-Link nicht erreichbar; interner Diagnostics-Service ist online.";
      }
      state.requests = [];
    }
    renderRequests();
  } finally {
    serviceStatusRefreshRunning = false;
  }
}

async function loadAll() {
  const seq = ++state.refreshSeq;
  const stillCurrent = () => seq === state.refreshSeq;
  try {
    const health = await getJson("/api/health");
    state.applicationVersion = typeof health.version === "string" ? health.version : "";
    setStatus("web-state", true, "online");
  } catch {
    state.applicationVersion = "";
    setStatus("web-state", false);
  }
  updateServerIdentity();
  renderAboutContent();
  try { await detectSetupWizardServer(); } catch { /* browser-host fallback remains available */ }
  // Keep initial page loading side-effect free. In particular, do not call
  // the retired /api/setup/devices compatibility endpoint here.
  state.setupDevices = [];
  // The legacy batch setup is retired and hidden. Do not issue its former
  // startup poll, which produced a harmless 404 on every fresh installation.
  state.setupJob = null;
  renderSetupBatch();
  await loadSetupRebuildState();
  try { state.devices = await getJson("/api/devices"); } catch { state.devices = []; }
  try { state.deviceStatuses = await getJson("/api/devices/status-badges"); } catch { state.deviceStatuses = []; }
  try { state.deviceCapabilities = await getJson("/api/devices/ui-capabilities"); } catch { state.deviceCapabilities = []; }
  try { state.deviceHealth = await getJson("/api/devices/health"); } catch { state.deviceHealth = []; }
  try { state.liveComparison = await getJson("/api/devices/live-comparison"); } catch { state.liveComparison = null; }
  renderDevices();
  applyCapabilityUi();
  renderLiveComparison();
  renderSetupFlow();
  try { state.stations = await getJson("/api/stations"); } catch { state.stations = []; }
  renderStations();
  try { state.presetProfiles = await getJson("/api/preset-profiles"); } catch { state.presetProfiles = []; }
  renderPresetProfiles();
  try { state.mediaTypes = await getJson("/api/media-types"); } catch { state.mediaTypes = []; }
  renderMediaTypes();
  try { state.keyCommands = await getJson("/api/keys"); } catch { state.keyCommands = []; }
  renderKeyCommands();
  try { state.displayModes = await getJson("/api/display/metadata-modes"); } catch { state.displayModes = []; }
  renderDisplayModes();
  try { state.telnetCommands = await getJson("/api/telnet/commands"); } catch { state.telnetCommands = []; }
  renderTelnet();
  try { state.mediaCapabilities = await getJson("/api/media-library/capabilities"); } catch { state.mediaCapabilities = null; }
  try { state.mediaPlaylists = await getJson("/api/media-playlists"); } catch { state.mediaPlaylists = []; }
  state.batteryStates = [];
  renderDevices();
  try { state.serviceCatalog = await getJson("/api/services/catalog"); } catch { state.serviceCatalog = []; }
  renderMediaLibrary();
  try { state.referenceSetups = await getJson("/api/reference-setups"); } catch { state.referenceSetups = []; }
  renderReferenceSetups();
  try { state.stereoResearch = await getJson("/api/stereo-pairing/research"); } catch { state.stereoResearch = null; }
  renderStereoResearch();
  try { state.systemSettings = await getJson("/api/system/settings"); } catch { state.systemSettings = null; }
  try { state.offlineStatus = await getJson("/api/offline/status"); } catch { state.offlineStatus = null; }
  try { state.featureStatus = await getJson("/api/features/status"); } catch { state.featureStatus = null; }
  renderSystemSettings();
  renderFeatureStatus();
  try {
    const scenarios = await getJson("/api/multiroom/scenarios");
    if (stillCurrent() && Array.isArray(scenarios)) {
      state.multiroomScenarios = scenarios;
      state.lastKnownMultiroomState.scenarios = scenarios;
    }
  } catch {
    state.multiroomScenarios = state.lastKnownMultiroomState.scenarios || [];
  }
  renderMultiroomScenarios();
  try {
    const methods = await getJson("/api/multiroom/methods");
    if (stillCurrent() && Array.isArray(methods)) {
      state.multiroomMethods = methods;
      state.lastKnownMultiroomState.methods = methods;
    }
  } catch {
    state.multiroomMethods = state.lastKnownMultiroomState.methods || [];
  }
  renderMultiroomMethods();
  await loadPresetsForSelectedDevice();
  try { state.playHistory = await getJson("/api/play-history"); } catch { state.playHistory = []; }
  try {
    const recentStations = await getJson("/api/multiroom/recent-stations");
    if (stillCurrent() && Array.isArray(recentStations)) {
      state.multiroomRecentStations = recentStations;
      state.lastKnownMultiroomState.recentStations = recentStations;
    }
  } catch {
    state.multiroomRecentStations = state.lastKnownMultiroomState.recentStations || [];
  }
  renderStations();
  try { state.playStats = await getJson("/api/stats/playback"); } catch { state.playStats = null; }
  renderPlayback();
  try { state.schedules = await getJson("/api/schedules"); } catch { state.schedules = []; }
  renderSchedules();
  updateScheduleWeekdayControls();
  try { state.settingsCatalog = await getJson("/api/settings/catalog"); } catch { state.settingsCatalog = []; }
  renderSettingsCatalog();
  try { state.telemetry = await getJson("/api/telemetry"); } catch { state.telemetry = []; }
  try { state.telemetrySummary = await getJson("/api/telemetry/summary"); } catch { state.telemetrySummary = null; }
  const telemetryRange = document.getElementById("telemetry-range")?.value || "24h";
  try { state.telemetryAnalysis = await getJson(`/api/diagnostics/telemetry/summary?range=${encodeURIComponent(telemetryRange)}`); } catch { state.telemetryAnalysis = null; }
  try { state.emulationGaps = await getJson("/api/diagnostics/emulation-gaps"); } catch { state.emulationGaps = null; }
  try { state.storageSummary = await getJson("/api/maintenance/storage"); } catch { state.storageSummary = null; }
  try { state.systemHealth = await getJson("/api/system/healthcheck"); } catch { state.systemHealth = null; }
  renderTelemetry();
  renderSystemHealth();
  await refreshServiceStatus();
}

async function saveDeviceForm(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const output = formElement.closest(".setup-flow-step")?.querySelector("#setup-device-output") || formElement.closest(".setup-step")?.querySelector("#setup-device-output") || document.getElementById("device-form-output") || document.getElementById("setup-output");
  try {
    const result = await postJson("/api/devices", Object.fromEntries(new FormData(formElement).entries()));
    if (output) output.textContent = JSON.stringify(result, null, 2);
    formElement?.reset?.();
    await refreshDeviceState({ live: true });
    if (formElement.id === "setup-device-form") markSetupStepDone("radio");
    markRiskPanels();
    showToast("Radio gespeichert.");
  } catch (error) {
    if (output) output.textContent = String(error);
    showApiError(error, "Radio konnte nicht hinzugefügt werden");
  }
}

async function performRadioScan(button = null) {
  const scanForm = document.getElementById("network-scan-form");
  const form = scanForm ? new FormData(scanForm) : new FormData();
  const box = document.getElementById("scan-results") || document.getElementById("device-form-output");
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Suche läuft…";
  }
  if (box) box.innerHTML = `<div class="event-row"><strong>Radios werden gesucht…</strong><small>${escapeHtml(form.get("cidr") || "LAN")}</small></div>`;
  try {
    const result = await postJson("/api/devices/scan", { cidr: form.get("cidr") || "", host: currentSetupHost(), timeout: Number(form.get("timeout") || 0.7), save: true });
    if (box) box.innerHTML = result.found.length
      ? result.found.map((device) => `<button class="event-row scan-result" data-scan-ip="${escapeHtml(device.ip_address)}" data-scan-name="${escapeHtml(device.name)}" data-scan-model="${escapeHtml(device.model)}" type="button"><span>${escapeHtml(device.ip_address)}</span><strong>${escapeHtml(device.name)}</strong><small>${escapeHtml(device.model)} · ${escapeHtml(text(device.firmware))}</small></button>`).join("")
      : `<div class="empty">Keine SoundTouch-Radios gefunden.</div>`;
    await refreshDeviceState({ live: true });
    await refreshSetupRebuildDevices();
    showToast(`${result.found.length} Radio(s) gefunden.`);
  } catch (error) {
    if (box) box.innerHTML = `<div class="empty">${escapeHtml(String(error))}</div>`;
    showApiError(error, "Gerätescan fehlgeschlagen");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

document.querySelectorAll(".nav-button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-button").forEach((item) => item.classList.remove("is-active"));
    document.querySelectorAll(".view").forEach((view) => view.classList.remove("is-active"));
    button.classList.add("is-active");
    document.getElementById(`view-${button.dataset.view}`).classList.add("is-active");
    const strip = button.parentElement?.classList.contains("topnav") ? button.parentElement : null;
    if (strip && strip.scrollWidth > strip.clientWidth) {
      const left = Math.max(0, button.offsetLeft - ((strip.clientWidth - button.offsetWidth) / 2));
      strip.scrollTo({ left, behavior: "smooth" });
    }
    document.querySelector(".advanced-nav")?.removeAttribute("open");
    syncBodyScrollLock();
    window.scrollTo({ top: 0, behavior: "instant" });
    if (button.dataset.view === "health") loadResearchHealth().catch((error) => showApiError(error, "Status konnte nicht geladen werden"));
    if (button.dataset.view === "device-settings") loadDeviceSettings().catch((error) => showToast(String(error), "error"));
    if (button.dataset.view === "features") loadFeatureStatus().catch((error) => showApiError(error, "Funktionsstatus konnte nicht geladen werden"));
    if (button.dataset.view === "presets" && state.guidedPreset.active) {
      state.guidedPreset.step = "search";
      renderGuidedPresetSetup();
    }
  });
});

document.querySelectorAll("[data-feature-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.featureFilter = button.dataset.featureFilter || "all";
    renderFeatureStatus();
  });
});

document.addEventListener("click", (event) => {
  const link = event.target.closest?.("[data-feature-view]");
  if (!link) return;
  event.preventDefault();
  document.querySelector(`.nav-button[data-view="${CSS.escape(link.dataset.featureView)}"]`)?.click();
  if (link.dataset.featureAnchor) window.setTimeout(() => document.getElementById(link.dataset.featureAnchor)?.scrollIntoView({ behavior: "smooth", block: "center" }), 0);
});

document.querySelectorAll(".advanced-nav").forEach((details) => {
  details.addEventListener("toggle", () => {
    syncBodyScrollLock();
  });
});

document.getElementById("device-form").addEventListener("submit", saveDeviceForm);
document.getElementById("setup-device-form").addEventListener("submit", saveDeviceForm);
document.getElementById("preset-device-select").addEventListener("change", loadPresetsForSelectedDevice);
document.getElementById("key-device-select")?.addEventListener("change", syncSafeStartControl);
document.getElementById("key-safe-volume-enabled")?.addEventListener("change", (event) => {
  const deviceId = document.getElementById("key-device-select")?.value || "";
  localStorage.setItem(safeStartStorageKey(deviceId), event.currentTarget.checked ? "true" : "false");
  const field = document.getElementById("key-safe-volume-field");
  if (field) field.hidden = !event.currentTarget.checked;
});
document.querySelectorAll('input[name="button"]').forEach((input) => input.addEventListener("change", renderPresetSlots));
document.getElementById("multiroom-master").addEventListener("change", renderMultiroomMembers);
document.getElementById("multiroom-members")?.addEventListener("change", renderMultiroomStartVolumes);
document.getElementById("multiroom-start-volumes-enabled")?.addEventListener("change", (event) => {
  if (event.currentTarget.checked) {
    const preserve = document.querySelector('#multiroom-form input[name="preserve_volumes"]');
    if (preserve) preserve.checked = false;
  }
  renderMultiroomStartVolumes();
});
document.querySelector('#multiroom-form input[name="preserve_volumes"]')?.addEventListener("change", (event) => {
  if (event.currentTarget.checked) {
    const custom = document.getElementById("multiroom-start-volumes-enabled");
    if (custom) custom.checked = false;
  }
  renderMultiroomStartVolumes();
});

document.getElementById("maintenance-reboot-now")?.addEventListener("click", async () => {
  const deviceId = document.getElementById("maintenance-reboot-device")?.value;
  if (!deviceId || !window.confirm("LAB: Radiozustand sichern und dieses Radio jetzt kontrolliert neu starten?")) return;
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/maintenance-reboot/run`, { confirmation: "REBOOT RADIO" });
  document.getElementById("maintenance-reboot-output").textContent = JSON.stringify(result, null, 2);
});
document.getElementById("schedule-master-select").addEventListener("change", renderScheduleMembers);
document.getElementById("schedule-days-select")?.addEventListener("change", updateScheduleWeekdayControls);
document.getElementById("scenario-master")?.addEventListener("change", renderScenarioMembers);
document.querySelector('input[name="volume"]').addEventListener("input", (event) => document.getElementById("volume-value").textContent = event.target.value);
document.querySelector('input[name="bass"]').addEventListener("input", (event) => document.getElementById("bass-value").textContent = event.target.value);



document.getElementById("rename-device-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  try {
    const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/rename`, {
      name: form.get("name"),
      dry_run: form.get("dry_run") === "on",
      memory_checked: form.get("memory_checked") === "on",
    });
    document.getElementById("rename-device-output").textContent = JSON.stringify(result, null, 2);
    if (!result.dry_run) await refreshDeviceState({ live: true });
    markRiskPanels();
  } catch (error) {
    document.getElementById("rename-device-output").textContent = String(error);
  }
});

document.getElementById("network-scan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  setFormBusy(formElement, true, "Scan läuft …");
  try { await performRadioScan(event.submitter); }
  finally { setFormBusy(formElement, false); }
});

document.getElementById("scan-radios-now")?.addEventListener("click", (event) => performRadioScan(event.currentTarget));

document.getElementById("scan-results")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-scan-ip]");
  if (!button) return;
  const setupForm = document.getElementById("setup-device-form");
  if (setupForm) {
    setupForm.elements.ip_address.value = button.dataset.scanIp || "";
    setupForm.elements.name.value = button.dataset.scanName || "";
    if (setupForm.elements.model) setupForm.elements.model.value = button.dataset.scanModel || "SoundTouch";
  }
  const persisted = state.devices.find((device) => device.ip_address === button.dataset.scanIp);
  const wizardSelect = document.getElementById("setup-wizard-device");
  if (persisted && wizardSelect) {
    wizardSelect.value = persisted.device_id;
    syncSetupControls();
    markSetupStepDone("radio");
    const output = document.getElementById("setup-device-output");
    if (output) output.textContent = `${persisted.name || persisted.device_id} wurde beim Scan gespeichert und für das Setup ausgewählt.`;
  }
});

document.getElementById("system-settings-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  for (const name of ["lab_mode", "guided_hints", "show_startup_warning", "ip_write_guard", "update_check_enabled"]) {
    if (event.currentTarget.elements[name]) payload[name] = event.currentTarget.elements[name].checked ? "true" : "false";
  }
  const result = await postJson("/api/system/settings", payload);
  state.systemSettings = result;
  try { state.offlineStatus = await getJson("/api/offline/status"); } catch { state.offlineStatus = null; }
  renderSystemSettings();
  applyUiPreferences();
  syncSetupControls();
  document.documentElement.lang = result.web_language || "en";
  document.getElementById("system-settings-output").textContent = JSON.stringify({ ...result, ui_note: i18nT("language_saved_note") }, null, 2);
});

document.getElementById("ui-mode-switch")?.addEventListener("change", async (event) => {
  const uiMode = event.currentTarget.value;
  try {
    state.systemSettings = await postJson("/api/system/settings", { ui_mode: uiMode });
    renderSystemSettings();
    applyUiPreferences();
    syncSafeStartControl();
    showToast(`${uiMode === "easy" ? "Easy" : uiMode === "standard" ? "Standard" : "LAB"} Mode aktiviert.`);
  } catch (error) {
    renderSystemSettings();
    showApiError(error, "Modus konnte nicht gespeichert werden");
  }
});

document.getElementById("online-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = new FormData(event.currentTarget).get("q");
  const message = document.getElementById("online-station-message");
  message.textContent = "Searching…";
  try {
    state.onlineStations = await getJson(`/api/stations/search-online?q=${encodeURIComponent(q)}`);
  } catch (error) {
    state.onlineStations = [];
    document.getElementById("online-station-results").innerHTML = `<div class="empty">${escapeHtml(String(error))}</div>`;
    message.textContent = "Search failed.";
    return;
  }
  renderOnlineStations();
  message.textContent = `${state.onlineStations.length} station${state.onlineStations.length === 1 ? "" : "s"} found.`;
  if (state.guidedPreset.active) {
    state.guidedPreset.step = "add";
    renderGuidedPresetSetup();
  }
});

document.getElementById("online-station-results").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-online-station]");
  if (!button) return;
  const station = state.onlineStations[Number(button.dataset.onlineStation)];
  if (!station) return;
  const message = document.getElementById("online-station-message");
  button.disabled = true;
  button.textContent = "Adding…";
  try {
    if (station.compatibility_warning) showToast(station.compatibility_warning, "error");
    const created = await postJson("/api/stations", { name: station.name, stream_url: station.stream_url, image_url: station.image_url });
    state.presetFilter = "";
    await refreshStations(created.id);
    message.textContent = `${station.name} was added and selected for the current slot.`;
    button.textContent = "Added ✓";
    if (state.guidedPreset.active) {
      state.guidedPreset.step = "slot";
      renderGuidedPresetSetup();
    }
    markRiskPanels();
  } catch (error) {
    message.textContent = `Could not add station: ${error?.message || String(error)}`;
    showApiError(error, "Sender konnte nicht hinzugefügt werden");
    button.disabled = false;
    button.textContent = "Try again";
  }
});

document.getElementById("preset-profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const slots = [1, 2, 3, 4, 5, 6].map((slot) => ({
    button: slot,
    station_id: document.getElementById(`profile-slot-${slot}`)?.value || null,
    label: stationName(document.getElementById(`profile-slot-${slot}`)?.value || null),
  }));
  const result = await postJson("/api/preset-profiles", {
    name: form.get("name"),
    description: form.get("description"),
    slots,
  });
  document.getElementById("preset-result").textContent = JSON.stringify(result, null, 2);
  formElement?.reset?.();
  state.presetProfiles = await getJson("/api/preset-profiles");
  renderPresetProfiles();
});

document.getElementById("preset-profile-apply-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const profileId = form.get("profile_id");
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/preset-profiles/${encodeURIComponent(profileId)}/apply/${encodeURIComponent(deviceId)}`, {
    dry_run: false,
    memory_checked: true,
  });
  document.getElementById("preset-result").textContent = JSON.stringify(result, null, 2);
  await loadPresetsForSelectedDevice();
});

document.getElementById("preset-clone-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await postJson("/api/presets/clone", Object.fromEntries(form.entries()));
  document.getElementById("preset-clone-output").textContent = JSON.stringify(result, null, 2);
  markRiskPanels();
});

document.getElementById("telemetry-probe-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/telemetry/probe`, {
    endpoint: form.get("endpoint"),
    dry_run: form.get("dry_run") === "on",
  });
  document.getElementById("telemetry-probe-output").textContent = JSON.stringify(result, null, 2);
  markRiskPanels();
});

document.getElementById("radio-log-sources")?.addEventListener("click", async () => {
  const deviceId = document.getElementById("radio-log-device-select")?.value || document.getElementById("telemetry-device-select")?.value;
  if (!deviceId) return;
  const result = await getJson(`/api/devices/${encodeURIComponent(deviceId)}/radio-log/sources`);
  document.getElementById("radio-log-output").textContent = JSON.stringify(result, null, 2);
  markRiskPanels();
});

document.getElementById("radio-log-capture-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/radio-log/capture`, {
    reason: form.get("reason"),
    include_cli: form.get("include_cli") === "on",
    dry_run: form.get("dry_run") === "on",
  });
  document.getElementById("radio-log-output").textContent = JSON.stringify(result, null, 2);
  if (!result.dry_run) {
    state.telemetry = await getJson("/api/telemetry");
    state.telemetrySummary = await getJson("/api/telemetry/summary");
    renderTelemetry();
  }
  markRiskPanels();
});

document.getElementById("ssh-log-capture-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const body = {
    username: form.get("username") || "root",
    reason: form.get("reason") || "manual-ssh",
    confirmation: form.get("confirmation") || "",
    dry_run: form.get("dry_run") === "on",
  };
  try {
    const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/ssh-log/capture`, body);
    document.getElementById("ssh-log-output").textContent = JSON.stringify(result, null, 2);
    if (!result.dry_run) {
      state.telemetry = await getJson("/api/telemetry");
      state.telemetrySummary = await getJson("/api/telemetry/summary");
      renderTelemetry();
    }
    markRiskPanels();
  } catch (error) {
    document.getElementById("ssh-log-output").textContent = String(error);
  }
});

document.getElementById("probe-device-info").addEventListener("click", async () => {
  const form = new FormData(document.getElementById("device-info-form"));
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/probe-info`, { dry_run: form.get("dry_run") === "on" });
  renderDeviceInfoCleartext(result);
  document.getElementById("device-info-output").textContent = JSON.stringify(result, null, 2);
  if (!result.dry_run) await loadAll();
  markRiskPanels();
});

document.getElementById("load-host-config").addEventListener("click", async () => {
  const deviceId = document.getElementById("device-info-select").value;
  const result = await getJson(`/api/devices/${encodeURIComponent(deviceId)}/host-config`);
  renderDeviceInfoCleartext(result);
  document.getElementById("device-info-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("load-guided-setup").addEventListener("click", async () => {
  const deviceId = document.getElementById("guided-setup-device").value;
  state.guidedSetupPlan = await getJson(`/api/setup/plans/${encodeURIComponent(deviceId)}`);
  document.getElementById("setup-output").textContent = JSON.stringify(state.guidedSetupPlan.device, null, 2);
  renderGuidedSetup(state.guidedSetupPlan);
});

document.getElementById("save-guided-setup").addEventListener("click", async () => {
  const deviceId = document.getElementById("guided-setup-device").value || currentSetupDeviceId();
  const steps = state.guidedSetupPlan?.steps || [];
  const result = await postJson("/api/setup/plans/" + encodeURIComponent(deviceId), { name: document.getElementById("guided-setup-name")?.value || "", steps, status: "draft" });
  document.getElementById("setup-output").textContent = JSON.stringify(result, null, 2);
  markSetupStepDone("done");
});

document.getElementById("cloud-route-host")?.addEventListener("focus", (event) => {
  if (!event.target.value) event.target.value = window.location.hostname;
});

[document.getElementById("setup-batch-host"), document.getElementById("setup-wizard-host"), document.querySelector("#setup-wizard-form input[name=host]"), document.getElementById("cloud-route-host"), document.querySelector("#setup-live-test-form input[name=host]")].forEach((input) => {
  input?.addEventListener("change", () => persistSetupHost(input.value).catch((error) => showApiError(error, "Host konnte nicht gespeichert werden")));
  input?.addEventListener("blur", () => persistSetupHost(input.value).catch((error) => showApiError(error, "Host konnte nicht gespeichert werden")));
});

document.getElementById("cloud-route-device")?.addEventListener("change", (event) => {
  const confirmation = document.getElementById("cloud-route-confirmation");
  if (confirmation) confirmation.placeholder = "yes";
  markSetupStepDone("radio", Boolean(event.target.value));
});


document.getElementById("setup-wizard-detect")?.addEventListener("click", async () => {
  try { await runSetupFlowAction("detect"); }
  catch (error) { document.getElementById("setup-wizard-output").textContent = String(error); }
});

document.getElementById("setup-wizard-device")?.addEventListener("change", (event) => {
  const confirmation = document.getElementById("cloud-route-confirmation");
  if (confirmation) confirmation.placeholder = "yes";
  markSetupStepDone("radio", Boolean(event.target.value));
});

document.getElementById("setup-wizard-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = event.submitter?.dataset?.setupFlowAction || event.submitter?.dataset?.wizardAction || "preflight";
  try { await runSetupFlowAction(action); }
  catch (error) { document.getElementById("setup-wizard-output").textContent = String(error); }
});

document.getElementById("cloud-route-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = event.submitter?.dataset?.setupFlowAction || "route-preview";
  try { await runSetupFlowAction(action); }
  catch (error) { document.getElementById("cloud-route-output").textContent = String(error); }
});

document.getElementById("setup-backup-plan")?.addEventListener("click", async () => {
  try { await runSetupFlowAction("backup"); }
  catch (error) { document.getElementById("setup-output").textContent = String(error); }
});

document.getElementById("setup-finish")?.addEventListener("click", (event) => finishSetup(event.currentTarget));

document.getElementById("setup-live-test-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const host = form.get("host") || currentSetupHost();
  await persistSetupHost(host);
  const body = {
    host,
    port: Number(cloudPort),
    station_id: form.get("station_id") ? Number(form.get("station_id")) : null,
    dry_run: form.get("dry_run") === "on",
  };
  try {
    const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/setup/live-test`, body);
    document.getElementById("setup-output").textContent = JSON.stringify(result, null, 2);
    if (!result.dry_run) {
      state.telemetry = await getJson("/api/telemetry");
      state.telemetrySummary = await getJson("/api/telemetry/summary");
      renderTelemetry();
    }
    markRiskPanels();
  } catch (error) {
    document.getElementById("setup-output").textContent = String(error);
  }
});

document.getElementById("power-action-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = event.submitter?.dataset.powerAction;
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/power/${encodeURIComponent(action)}`, {
    dry_run: form.get("dry_run") === "on",
    confirmation: form.get("confirmation") || "",
  });
  document.getElementById("power-action-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("recovery-action-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = event.submitter?.dataset.recoveryAction;
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const device = state.devices.find((item) => item.device_id === deviceId);
  const confirmation = form.get("confirmation");
  const operation = startOperationOverlay(event.submitter?.textContent.trim() || "Recovery", device);
  try {
    const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/recovery/${encodeURIComponent(action)}`, {
      confirmation,
      dry_run: form.get("dry_run") === "on",
      safe_startup_volume: Number(form.get("safe_startup_volume") || 30),
    });
    document.getElementById("recovery-output").textContent = JSON.stringify(result, null, 2);
    operation.waitForReboot();
  } catch (error) {
    document.getElementById("recovery-output").textContent = String(error);
    operation.fail(error);
  }
});

document.getElementById("battery-patch-plan")?.addEventListener("click", async () => {
  const deviceId = document.getElementById("battery-patch-device-select")?.value || state.devices[0]?.device_id;
  if (!deviceId) return;
  const result = await getJson(`/api/devices/${encodeURIComponent(deviceId)}/battery/patch-plan`);
  document.getElementById("lab-detail").textContent = JSON.stringify(result, null, 2);
  document.getElementById("battery-patch-output").textContent = JSON.stringify(result.plan || result, null, 2);
});

let loadedDeviceSettings = null;
const changedDeviceSettings = new Set();
const deviceClockFields = new Set(["timezoneInfo", "timeFormat", "userOffsetMinute", "brightnessLevel"]);

function setDeviceSettingsDirty(dirty) {
  const button = document.getElementById("device-settings-apply");
  const message = document.getElementById("device-settings-dirty");
  button?.classList.toggle("has-pending-changes", dirty);
  if (message) message.textContent = dirty ? "Änderungen noch nicht gespeichert – bitte Anwenden klicken." : "Alle angezeigten Werte entsprechen dem Radio.";
}

async function loadDeviceSettings() {
  const form = document.getElementById("device-settings-form");
  const deviceId = form?.elements.device_id?.value;
  if (!deviceId) return;
  const result = await getJson(`/api/devices/${encodeURIComponent(deviceId)}/settings?probe=true`);
  loadedDeviceSettings = result.current || {};
  const values = loadedDeviceSettings;
  for (const key of ["name", "volume", "bass", "language", "powersaving", "rebroadcastlatencymode", "station_art_mode"]) {
    const element = form.elements[key];
    if (values[key] === undefined || !element) continue;
    const nextValue = String(values[key]);
    element.querySelector?.('option[data-radio-unmapped="true"]')?.remove();
    if (element.tagName === "SELECT" && !Array.from(element.options).some((option) => option.value === nextValue)) {
      // Firmware may return a numeric/unknown value (for example language ID
      // 0). Show that fact instead of silently displaying the first option.
      const unmapped = new Option(`Unverändert (Radio: ${nextValue})`, "", true, true);
      unmapped.disabled = true;
      unmapped.dataset.radioUnmapped = "true";
      element.prepend(unmapped);
      element.dataset.unmappedRadioValue = nextValue;
      continue;
    }
    delete element.dataset.unmappedRadioValue;
    element.value = nextValue;
  }
  form.elements.clockDisplay.value = String(Boolean(values.clockDisplay));
  const clock = values.clockConfig || {};
  for (const key of ["timezoneInfo", "timeFormat", "userOffsetMinute", "brightnessLevel"]) {
    const element = form.elements[key];
    if (clock[key] === undefined || !element) continue;
    const nextValue = String(clock[key]);
    element.querySelector?.('option[data-radio-unmapped="true"]')?.remove();
    if (element.tagName === "SELECT" && !Array.from(element.options).some((option) => option.value === nextValue)) {
      const unmapped = new Option(`Bitte wählen (Radio: ${nextValue})`, "", true, true);
      unmapped.disabled = true;
      unmapped.dataset.radioUnmapped = "true";
      element.prepend(unmapped);
      element.dataset.unmappedRadioValue = nextValue;
      continue;
    }
    delete element.dataset.unmappedRadioValue;
    element.value = nextValue;
  }
  document.getElementById("volume-value").textContent = form.elements.volume.value;
  document.getElementById("bass-value").textContent = form.elements.bass.value;
  document.getElementById("device-settings-output").textContent = JSON.stringify(result, null, 2);
  const liveInfo = document.getElementById("device-settings-live-info");
  const device = state.devices.find((item) => item.device_id === deviceId);
  const runtime = result.runtime_state || result.state || {};
  if (liveInfo) {
    liveInfo.innerHTML = [
      ["Radio", values.name || device?.name || deviceId],
      ["Aktuelle Source", result.now_playing?.source || runtime.current_source || values.source || "unbekannt"],
      ["Sender", result.now_playing?.station || result.now_playing?.item_name || runtime.station_name || "unbekannt"],
      ["Preset Slot", result.now_playing?.preset || runtime.current_preset || "keiner"],
      ["Playback", result.now_playing?.playback_state || result.now_playing?.play_status || runtime.playback_state || values.playback_state || "unbekannt"],
      ["Lautstärke", values.volume ?? "?"],
      ["Gerät-IP", device?.ip_address || result.radio_ip || ""],
      ["Firmware", device?.firmware || ""],
    ].map(([label, value]) => `<div class="event-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(text(value, "unbekannt"))}</strong></div>`).join("");
  }
  changedDeviceSettings.clear();
  setDeviceSettingsDirty(false);
}

document.getElementById("settings-device-select")?.addEventListener("change", loadDeviceSettings);
function markDeviceSettingChanged(event) {
  const name = event.target?.name;
  if (!name || name === "device_id") return;
  changedDeviceSettings.add(deviceClockFields.has(name) ? "clockConfig" : name);
  setDeviceSettingsDirty(true);
}
document.getElementById("device-settings-form")?.addEventListener("input", markDeviceSettingChanged);
document.getElementById("device-settings-form")?.addEventListener("change", markDeviceSettingChanged);
document.getElementById("device-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const deviceId = form.get("device_id");
  const values = {
    name: form.get("name"), volume: Number(form.get("volume")), bass: Number(form.get("bass")),
    clockDisplay: form.get("clockDisplay") === "true", language: form.get("language"),
    clockConfig: { timezoneInfo: form.get("timezoneInfo"), timeFormat: form.get("timeFormat"), userOffsetMinute: Number(form.get("userOffsetMinute")), brightnessLevel: Number(form.get("brightnessLevel")), userUtcTime: 0 },
    powersaving: form.get("powersaving") === "true", rebroadcastlatencymode: form.get("rebroadcastlatencymode"), station_art_mode: form.get("station_art_mode"),
  };
  const changedValues = Object.fromEntries(
    Object.entries(values).filter(([key]) => changedDeviceSettings.has(key)),
  );
  if (!Object.keys(changedValues).length) {
    showToast("Keine Änderung nötig.");
    setDeviceSettingsDirty(false);
    return;
  }
  if (changedDeviceSettings.has("clockConfig") && !form.get("timezoneInfo")) {
    const message = "Bitte eine Zeitzone auswählen; das Radio meldet derzeit keinen unterstützten Zeitzonenwert.";
    document.getElementById("device-settings-output").textContent = message;
    showToast(message, "error");
    return;
  }
  setFormBusy(formElement, true, "Einstellungen werden geprüft …");
  try {
    const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/settings-apply`, { values: changedValues });
    document.getElementById("device-settings-output").textContent = JSON.stringify(result, null, 2);
    const syncHint = result.preset_sync_required ? " Presets danach synchronisieren, damit das Radio die Anzeige übernimmt." : "";
    showToast(result.applied ? `${result.applied} Änderung(en) gespeichert.${syncHint}` : "Keine Änderung nötig.");
    // Refresh shared UI from the local database only. The explicit settings
    // action targets one radio; it must not fan out into live probes of every
    // saved device while the form remains blocked.
    if (result.applied && changedDeviceSettings.has("name")) await refreshDeviceState({ live: false });
    await loadDeviceSettings();
  } catch (error) {
    document.getElementById("device-settings-output").textContent = `Einstellungen nicht geschrieben: ${error.message}`;
    showApiError(error, "Geräteeinstellungen konnten nicht sicher geschrieben werden");
  } finally {
    setFormBusy(formElement, false);
  }
});

async function previewDevicePresetSync() {
  const deviceId = document.getElementById("settings-device-select")?.value;
  const memoryChecked = document.getElementById("device-settings-memory-check")?.checked;
  const status = document.getElementById("device-settings-preset-sync-status");
  const previewBox = document.getElementById("device-settings-preset-sync-preview");
  const confirmButton = document.getElementById("device-settings-preset-sync-confirm");
  const previewButton = document.getElementById("sync-device-presets");
  if (!deviceId) return showToast("Bitte zuerst ein Radio auswählen.", "error");
  if (!memoryChecked) return showToast("Bitte zuerst den Backup-/Memory-Hinweis bestätigen.", "error");
  previewButton.disabled = true;
  if (status) status.textContent = "Read-only-Vorschau wird erstellt …";
  try {
    const result = await postJson(`/api/presets/${encodeURIComponent(deviceId)}/sync`, { dry_run: true, memory_checked: true });
    const target = result.target || {};
    const changes = result.expected_changes || [];
    const logoWarnings = (result.logo_status || []).filter((item) => item.mode === "station_logo" && (!item.valid || item.verification === "syntax_only"));
    if (previewBox) {
      previewBox.hidden = false;
      previewBox.innerHTML = `<strong>Zielradio</strong><span>${escapeHtml(text(target.name, deviceId))} · ${escapeHtml(text(target.ip_address))} · ID ${escapeHtml(text(target.device_id, deviceId))}</span><strong>Betroffene Slots</strong><span>${escapeHtml(changes.map((item) => `Slot ${item.button}`).join(", ") || "keine lokalen Presets")}</span><strong>Schutzstatus</strong><span>${result.protection?.protected_ip ? "Geschütztes Radio: Write wird blockiert." : result.protection?.write_allowed === false ? escapeHtml(text(result.protection?.write_blocker, "Write-Guard blockiert")) : "Write-Gates werden serverseitig erneut geprüft."}</span><strong>Memory-Check</strong><span>Backup und Readback sind vor dem Write erforderlich.</span>${logoWarnings.length ? `<strong>Logo-Prüfung</strong><span>${logoWarnings.length} Slot(s) haben keinen vollständigen Bildprobe-Nachweis; ungültige Quellen fallen auf das Bose-Radiosymbol zurück.</span>` : ""}`;
    }
    if (confirmButton) confirmButton.hidden = Boolean(result.protection?.write_allowed === false || !changes.length);
    if (status) status.textContent = "Vorschau erstellt. Erst die Bestätigung startet den geschützten Write.";
    document.getElementById("device-settings-output").textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    if (status) status.textContent = `Vorschau fehlgeschlagen: ${error.message}`;
    showApiError(error, "Preset-Sync-Vorschau fehlgeschlagen");
  } finally {
    previewButton.disabled = false;
  }
}

document.getElementById("sync-device-presets")?.addEventListener("click", () => previewDevicePresetSync());
document.getElementById("device-settings-preset-sync-confirm")?.addEventListener("click", async (event) => {
  const deviceId = document.getElementById("settings-device-select")?.value;
  const status = document.getElementById("device-settings-preset-sync-status");
  if (!deviceId || !window.confirm("Die angezeigten lokalen Presets jetzt schreiben und jeden Slot per Readback bestätigen?")) return;
  const confirmButton = event.currentTarget;
  confirmButton.disabled = true;
  if (status) status.textContent = "Presets werden geschrieben und vom Radio gelesen …";
  try {
    const result = await postJson(`/api/presets/${encodeURIComponent(deviceId)}/sync`, { dry_run: false, memory_checked: true });
    const count = Array.isArray(result.radio_slots) ? result.radio_slots.length : 0;
    if (result.verified !== true) throw new Error("Der Readback wurde nicht bestätigt.");
    if (status) status.textContent = `Synchronisierung bestätigt: ${count} Radio-Slot(s) gelesen.`;
    document.getElementById("device-settings-output").textContent = JSON.stringify(result, null, 2);
    document.getElementById("device-settings-preset-sync-preview").hidden = true;
    confirmButton.hidden = true;
    showToast("Presets synchronisiert und Readback bestätigt.");
    await loadPresetsForSelectedDevice();
  } catch (error) {
    const slotResults = error.payload?.detail?.slot_results || error.payload?.slot_results;
    if (status) status.textContent = `Synchronisierung fehlgeschlagen: ${error.message}`;
    if (slotResults) document.getElementById("device-settings-output").textContent = JSON.stringify({ error: error.message, slot_results: slotResults }, null, 2);
    showApiError(error, "Preset-Synchronisierung fehlgeschlagen");
  } finally {
    confirmButton.disabled = false;
  }
});

document.getElementById("offline-preflight-run")?.addEventListener("click", async (event) => {
  const stationId = document.getElementById("preset-station-select")?.value;
  const output = document.getElementById("offline-preflight-output");
  if (!stationId) return showToast("Bitte zuerst ein Preset und einen Sender auswählen.", "error");
  const runButton = event.currentTarget;
  runButton.disabled = true;
  if (output) output.innerHTML = `<div class="empty">Offline-Abhängigkeiten werden analysiert …</div>`;
  try {
    state.offlinePreflight = await postJson("/api/offline/preflight", { station_id: Number(stationId), probe: Boolean(document.getElementById("offline-preflight-probe")?.checked) });
    renderOfflinePreflight();
  } catch (error) {
    if (output) output.innerHTML = `<div class="event-row status-warning"><strong>Preflight fehlgeschlagen</strong><small>${escapeHtml(error.message)}</small></div>`;
    showApiError(error, "Offline-Preflight fehlgeschlagen");
  } finally {
    runButton.disabled = false;
  }
});


document.getElementById("bass-capabilities-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await postJson(`/api/devices/${encodeURIComponent(form.get("device_id"))}/bass-capabilities`, { dry_run: form.get("dry_run") === "on" });
  document.getElementById("bass-capabilities-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("source-name-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await postJson(`/api/devices/${encodeURIComponent(form.get("device_id"))}/sources/name-plan`, { source: form.get("source"), sourceAccount: form.get("sourceAccount"), name: form.get("name") });
  document.getElementById("source-name-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("wireless-profile-read")?.addEventListener("click", async () => {
  const deviceId = document.getElementById("wireless-profile-device")?.value;
  if (!deviceId) return;
  const result = await getJson(`/api/devices/${encodeURIComponent(deviceId)}/wireless-profiles`);
  document.getElementById("wireless-profile-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("wireless-profile-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const device = state.devices.find((item) => item.device_id === form.get("device_id"));
  if (!device || form.get("understood") !== "on") return;
  if (!window.confirm(`${device.name} kann beim Hinzufügen sofort das aktuelle WLAN verlassen. Fortfahren?`)) return;
  const result = await postJson(`/api/devices/${encodeURIComponent(device.device_id)}/wireless-profiles`, { ssid: form.get("ssid"), password: form.get("password"), security_type: form.get("security_type"), confirmation: "YES" });
  document.getElementById("wireless-profile-output").textContent = JSON.stringify(result, null, 2);
  event.currentTarget.elements.password.value = "";
  showToast("WLAN-Profil wurde an das Radio übergeben.");
});

document.getElementById("station-search")?.addEventListener("input", (event) => {
  state.stationFilter = event.target.value;
  renderStations();
});

document.querySelector('#online-search-form input[name="q"]')?.addEventListener("input", (event) => {
  if (state.guidedPreset.active && String(event.target.value || "").trim()) {
    state.guidedPreset.step = "searchButton";
    renderGuidedPresetSetup();
  }
});


document.getElementById("native-station-search-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await postJson(`/api/devices/${encodeURIComponent(form.get("device_id"))}/station/search-native`, { source: form.get("source"), sourceAccount: form.get("sourceAccount"), query: form.get("query"), dry_run: form.get("dry_run") === "on" });
  document.getElementById("native-station-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("native-station-add-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await postJson(`/api/devices/${encodeURIComponent(form.get("device_id"))}/station/add-native`, { source: form.get("source"), sourceAccount: form.get("sourceAccount"), token: form.get("token"), name: form.get("name"), dry_run: form.get("dry_run") === "on" });
  document.getElementById("native-station-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("station-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const data = Object.fromEntries(new FormData(formElement).entries());
  if (!data.name?.trim()) return showToast("Station Name ist Pflicht.", "error");
  try {
    const url = new URL(data.stream_url);
    if (!["http:", "https:"].includes(url.protocol)) throw new Error("invalid protocol");
  } catch {
    return showToast("Stream URL muss eine gültige HTTP/HTTPS-Adresse sein.", "error");
  }
  const created = await postJson("/api/stations", data);
  formElement?.reset?.();
  await refreshStations(created.id);
  const deviceId = document.getElementById("station-play-device")?.value;
  // Creating a catalog entry must never start audio as a side effect.  Keep
  // the useful preview, but require a separate explicit human confirmation
  // for the real radio action.
  if (deviceId) await playStation(created.id, true);
  markRiskPanels();
});

document.getElementById("station-upload-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const response = await fetch(`/api/stations/upload?name=${encodeURIComponent(form.get("name"))}`, { method: "POST", body: form });
  if (!response.ok) throw new Error(await response.text());
  formElement?.reset?.();
  markRiskPanels();
});

async function playStation(stationId, dryRun = true) {
  const deviceId = document.getElementById("station-play-device")?.value;
  if (!deviceId || !stationId) return;
  const device = state.devices.find((item) => item.device_id === deviceId);
  const station = state.stations.find((item) => String(item.id) === String(stationId));
  let safeVolume = null;
  try { safeVolume = selectedSafeStartVolume(); } catch (error) { return showToast(error.message, "error"); }
  if (safeVolume !== null && safeVolume > 5) return showToast("Für einen Senderstart sind höchstens 5 erlaubt.", "error");
  const safetyCopy = safeVolume === null ? "Die aktuelle Radio-Lautstärke bleibt unverändert." : `Lautstärke ${safeVolume} wird vor dem Start per Readback bestätigt.`;
  if (!dryRun && !window.confirm(`${text(station?.name, "Diesen Sender")} auf ${text(device?.name, deviceId)} starten? ${safetyCopy}`)) return;
  const body = { dry_run: dryRun, trigger: "webui", trigger_type: "station" };
  if (!dryRun && safeVolume !== null) body.safe_volume = safeVolume;
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/stations/${encodeURIComponent(stationId)}/play`, body);
  document.getElementById("station-play-output").textContent = JSON.stringify(result, null, 2);
  showToast(dryRun ? "Wiedergabevorschau erstellt – es wurde kein Audio gestartet." : `Wiedergabe gestartet; Lautstärke ${result.confirmed_volume} per Radio bestätigt.`);
}

document.getElementById("station-play-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await playStation(form.get("station_id"), form.get("dry_run") === "on");
});

document.getElementById("stations-table")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-play-station]");
  if (!button) return;
  const dryRun = new FormData(document.getElementById("station-play-form")).get("dry_run") === "on";
  await playStation(button.dataset.playStation, dryRun);
});

document.getElementById("quick-station-add").addEventListener("click", async () => {
  const name = document.getElementById("quick-station-name").value.trim();
  const stream = document.getElementById("quick-station-url").value.trim();
  const image = document.getElementById("quick-station-image")?.value.trim() || "";
  const message = document.getElementById("quick-station-message");
  if (!name || !stream) {
    message.textContent = "Station name and stream URL are required.";
    return;
  }
  try {
    const created = await postJson("/api/stations", { name, stream_url: stream, image_url: image });
    document.getElementById("quick-station-name").value = "";
    document.getElementById("quick-station-url").value = "";
    const imageInput = document.getElementById("quick-station-image");
    if (imageInput) imageInput.value = "";
    state.presetFilter = "";
    await refreshStations(created.id);
    message.textContent = `${name} was created and selected.`;
    markRiskPanels();
  } catch (error) {
    message.textContent = `Could not create station: ${String(error)}`;
  }
});

document.getElementById("download-radio-presets")?.addEventListener("click", async () => {
  const deviceId = selectedDeviceId();
  if (!deviceId) return;
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/presets/download`, { dry_run: false });
  document.getElementById("preset-result").textContent = JSON.stringify(result, null, 2);
  if (!result.dry_run) await loadPresetsForSelectedDevice();
});

document.getElementById("preset-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const data = Object.fromEntries(new FormData(formElement).entries());
  const output = document.getElementById("preset-result");
  const status = document.getElementById("preset-form-status");
  setFormBusy(formElement, true, "Preset wird gespeichert …");
  output.textContent = "Preset wird an das Radio übertragen und geprüft …";
  if (status) status.textContent = `Preset ${data.button} wird übertragen und anschließend vom Radio gelesen …`;
  try {
    const result = await postJson(`/api/presets/${encodeURIComponent(data.device_id)}/${data.button}`, { station_id: Number(data.station_id), dry_run: false, memory_checked: true });
    output.textContent = result.dry_run
      ? `Vorschau für Slot ${result.button}. Noch nichts geschrieben.`
      : `Slot ${result.button} wurde auf dem Radio gespeichert und erfolgreich geprüft.`;
    if (status) status.textContent = result.dry_run
      ? `Vorschau für Preset ${result.button}; das Radio wurde nicht verändert.`
      : `Preset ${result.button} wurde gespeichert und durch Radio-Readback bestätigt.`;
    showToast(result.dry_run ? "Preset-Vorschau erstellt" : `Preset ${result.button} ist auf dem Radio gespeichert`);
    await loadPresetsForSelectedDevice();
    if (state.guidedPreset.active && String(data.button) === "1") {
      state.guidedPreset.step = "play";
      renderGuidedPresetSetup();
    }
  } catch (error) {
    output.textContent = `Speichern fehlgeschlagen: ${error.message}`;
    if (status) status.textContent = `Preset wurde nicht als erfolgreich gespeichert: ${error.message}`;
    showApiError(error, "Preset konnte nicht bestätigt werden");
  } finally {
    setFormBusy(formElement, false);
  }
});

document.getElementById("preset-slot-grid")?.addEventListener("click", async (event) => {
  const play = event.target.closest("[data-play-preset-slot]");
  if (play) {
    const slot = play.dataset.playPresetSlot;
    const deviceId = selectedDeviceId();
    if (!deviceId) return showToast("Bitte zuerst ein Radio auswählen.", "error");
    play.disabled = true;
    try {
      const safeVolume = selectedSafeStartVolume();
      const payload = { key: `PRESET_${slot}` };
      if (safeVolume !== null) payload.safe_volume = safeVolume;
      const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/key`, payload);
      document.getElementById("preset-result").textContent = JSON.stringify(result, null, 2);
      await postJson("/api/play-history/start", { device_id: deviceId, station_name: `Preset ${slot}`, trigger: `preset_${slot}`, trigger_type: "preset", preset_button: Number(slot), source: "PRESET" });
      if (state.guidedPreset.active && String(slot) === "1") completeGuidedPresetSetup();
    } finally {
      play.disabled = false;
    }
    state.playHistory = await getJson("/api/play-history");
    state.playStats = await getJson("/api/stats/playback");
    renderPlayback();
    return;
  }
  const button = event.target.closest("[data-delete-preset]");
  if (!button || !window.confirm(`Preset ${button.dataset.deletePreset} entfernen?`)) return;
  const slot = button.dataset.deletePreset;
  button.disabled = true;
  try {
    const result = await deleteJson(`/api/presets/${encodeURIComponent(selectedDeviceId())}/${slot}`);
    await loadPresetsForSelectedDevice();
    document.getElementById("preset-result").textContent = result.verified
      ? `Preset ${slot} wurde auf dem Radio entfernt und per Readback bestätigt.`
      : `Preset ${slot}: Löschstatus konnte nicht bestätigt werden.`;
    showToast(`Preset ${slot} wurde entfernt`);
  } catch (error) {
    document.getElementById("preset-result").textContent = `Löschen fehlgeschlagen: ${error.message}`;
    showApiError(error, `Preset ${slot} konnte nicht sicher entfernt werden`);
  } finally {
    button.disabled = false;
  }
});


document.getElementById("schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const master = form.get("multiroom_master_id");
  const memberIds = Array.from(document.querySelectorAll("#schedule-members input:checked")).map((node) => node.value);
  const deviceId = form.get("device_id");
  const stationId = form.get("station_id");
  const presetButton = form.get("preset_button");
  const weekdays = Array.from(formElement.querySelectorAll('input[name="weekday"]:checked')).map((node) => node.value);
  const days = form.get("days") === "custom" ? weekdays.join(",") : form.get("days");
  if (!stationId && !presetButton) return showToast("Bitte Sender oder Preset Slot wählen.", "error");
  if (!days) return showToast("Bitte mindestens einen Wochentag wählen.", "error");
  await postJson("/api/schedules", {
    name: form.get("name"),
    start_time: form.get("start_time"),
    end_time: form.get("end_time"),
    days,
    device_ids: master ? [master] : [deviceId],
    station_id: presetButton ? "" : stationId,
    preset_button: presetButton,
    volume: form.get("volume"),
    stop_action: form.get("stop_action"),
    multiroom_master_id: master,
    multiroom_member_ids: memberIds,
    dry_run: form.get("dry_run") === "on",
  });
  formElement?.reset?.();
  updateScheduleWeekdayControls();
  state.schedules = await getJson("/api/schedules");
  renderSchedules();
  markRiskPanels();
});

document.getElementById("clear-schedules")?.addEventListener("click", async () => {
  if (!window.confirm("Alle gespeicherten Test-Wecker Timer und geplanten Aktionen entfernen?")) return;
  const result = await deleteJson("/api/schedules");
  state.schedules = [];
  renderSchedules();
  showToast(`${result.deleted} geplante Aktion(en) entfernt.`);
});

document.getElementById("battery-patch-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitter = event.submitter;
  const action = submitter?.dataset.batteryAction || "status";
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const output = document.getElementById("battery-patch-output");
  if (!deviceId || !output) return;
  output.textContent = "BatteryMonitor LAB-Aktion wird vorbereitet …";
  try {
    let result;
    if (action === "status") {
      result = await getJson(`/api/battery/status/${encodeURIComponent(deviceId)}`);
    } else if (action === "dry-run") {
      result = await postJson(`/api/battery/patch/${encodeURIComponent(deviceId)}/dry-run`, {});
    } else if (action === "apply") {
      if (!window.confirm("BatteryMonitor wird auf dem Radio gepatcht. Backup, Checksumme und Read-back sind Pflicht. Fortfahren?")) return;
      result = await postJson(`/api/battery/patch/${encodeURIComponent(deviceId)}/apply`, {
        confirmation: form.get("confirmation") || "",
        memory_checked: form.get("memory_checked") === "on",
      });
    } else if (action === "rollback") {
      if (!window.confirm("BatteryMonitor wird aus dem Backup zurueckgerollt. Fortfahren?")) return;
      result = await postJson(`/api/battery/patch/${encodeURIComponent(deviceId)}/rollback`, {
        confirmation: form.get("confirmation") || "",
        memory_checked: form.get("memory_checked") === "on",
      });
    }
    output.textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    output.textContent = `${error.message}\n\nApply-Bestaetigung: BASSWIESN BATTERY PATCH\nRollback-Bestaetigung: BASSWIESN BATTERY ROLLBACK`;
    showApiError(error, "BatteryMonitor LAB-Aktion fehlgeschlagen");
  }
});

document.getElementById("schedule-list")?.addEventListener("click", async (event) => {
  const trigger = event.target.closest("[data-schedule-trigger]");
  const toggle = event.target.closest("[data-schedule-toggle]");
  const remove = event.target.closest("[data-schedule-delete]");
  if (trigger) {
    const result = await postJson(`/api/schedules/${encodeURIComponent(trigger.dataset.scheduleTrigger)}/trigger`, { dry_run: false });
    showToast(result.dry_run ? "Wecker Timer als Vorschau geprüft" : "Wecker Timer ausgelöst");
    state.playHistory = await getJson("/api/play-history");
    state.playStats = await getJson("/api/stats/playback");
    state.schedules = await getJson("/api/schedules");
    renderSchedules();
    renderHistory();
  }
  if (toggle) {
    await postJson(`/api/schedules/${encodeURIComponent(toggle.dataset.scheduleToggle)}/enable`, { enabled: toggle.dataset.enabled === "true" });
    state.schedules = await getJson("/api/schedules");
    renderSchedules();
  }
  if (remove) {
    if (!window.confirm("Diesen Wecker Timer löschen?")) return;
    await deleteJson(`/api/schedules/${encodeURIComponent(remove.dataset.scheduleDelete)}`);
    state.schedules = await getJson("/api/schedules");
    renderSchedules();
  }
});

document.getElementById("dashboard-play-stats")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-stats-detail]");
  if (!button) return;
  state.statsDetail = { type: button.dataset.statsDetail || "overview", key: button.dataset.statsKey || "" };
  renderPlayback();
});


document.getElementById("zone-status-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await getJson(`/api/multiroom/status/${encodeURIComponent(form.get("device_id"))}`);
  document.getElementById("zone-status-output").innerHTML = renderFriendlyZone(result);
});

document.getElementById("multiroom-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const memberIds = Array.from(document.querySelectorAll("#multiroom-members input:checked")).map((node) => node.value);
  if (!memberIds.length) {
    document.getElementById("multiroom-output").textContent = "Bitte mindestens einen weiteren Raum auswählen.";
    return;
  }
  const setStartVolumes = form.get("set_start_volumes") === "on";
  const startVolumes = Object.fromEntries(Array.from(document.querySelectorAll("#multiroom-start-volumes input[data-start-volume]")).map((input) => [input.dataset.startVolume, Number(input.value)]));
  const payload = { master_device_id: form.get("master_device_id"), member_device_ids: memberIds, station_id: form.get("station_id") || null, volume: Number(form.get("volume") || 5), preserve_volumes: form.get("preserve_volumes") === "on", set_start_volumes: setStartVolumes, start_volumes: setStartVolumes ? startVolumes : {}, latency_mode: form.get("latency_mode"), dry_run: true, memory_checked: true, read_volumes: true };
  const output = document.getElementById("multiroom-output");
  const preview = document.getElementById("multiroom-preview");
  const confirm = document.getElementById("multiroom-confirm");
  output.textContent = "Read-only-Vorschau wird erstellt …";
  setFormBusy(formElement, true, "Zone wird aufgebaut …");
  try {
    const result = await postJson("/api/multiroom/preview", payload);
    state.multiroomPendingPayload = { ...payload, dry_run: false, read_volumes: false };
    const volumeRows = (result.current_volumes || []).map((item) => `<span>${escapeHtml(text(item.name, item.device_id))} · ${escapeHtml(item.ip_address)} · Lautstärke ${escapeHtml(text(item.volume, "unbekannt"))}</span>`).join("");
    const volumePlan = result.preserve_volumes ? "BASSWIESN sendet kein SetVolume; Bose-Firmwareänderungen werden nur beobachtet und gemeldet" : result.set_start_volumes ? `Individuelle Startwerte: ${Object.entries(result.start_volumes || {}).map(([id, value]) => `${escapeHtml(id)} ${escapeHtml(value)}`).join(", ")}` : `Gemeinsame Lautstärke ${escapeHtml(payload.volume)}`;
    if (preview) { preview.hidden = false; preview.innerHTML = `<strong>Zonenmaster</strong><span>${escapeHtml(text(result.master))}</span><strong>Teilnehmer</strong><span>${escapeHtml((result.members || []).join(", "))}</span><strong>Aktuelle Lautstärken</strong>${volumeRows || "<span>nicht gelesen</span>"}<strong>Geplante Aktion</strong><span>${volumePlan}; Readback nach Ausführung erforderlich.</span><strong>Schutzstatus</strong><span>${result.blocked ? `Blockiert: ${escapeHtml((result.protected_devices || []).join(", "))}` : "Kein geschütztes Radio in der Vorschau."}</span>`; }
    if (confirm) confirm.hidden = Boolean(result.blocked);
    output.textContent = "Vorschau erstellt. Erst die Bestätigung startet die geschützte Aktion.";
  } catch (error) {
    output.textContent = `Multiroom fehlgeschlagen: ${error.message}`;
    showApiError(error, "Multiroom konnte nicht gestartet werden");
  } finally {
    setFormBusy(formElement, false);
  }
});

document.getElementById("multiroom-confirm")?.addEventListener("click", async (event) => {
  if (!state.multiroomPendingPayload || !window.confirm("Multiroom mit den angezeigten Teilnehmern starten und Readback prüfen?")) return;
  const confirmButton = event.currentTarget;
  confirmButton.disabled = true;
  const output = document.getElementById("multiroom-output");
  try {
    const result = await postJson("/api/multiroom/set", state.multiroomPendingPayload);
    const verified = Array.isArray(result.verification) && result.verification.length > 0 && result.verification.every((item) => item.ok === true);
    if (!verified) throw new Error("Multiroom-Readback wurde nicht für alle Radios bestätigt.");
    const firmwareVolumeChanges = Array.isArray(result.volume_warnings) ? result.volume_warnings : [];
    const firmwareWarning = firmwareVolumeChanges.length
      ? `<span>${result.preserve_volumes ? "Bose-Firmware änderte trotz ausbleibendem SetVolume" : "Bose-Firmware normalisierte Werte nach der bestätigten Startlautstärke"}: ${firmwareVolumeChanges.map((item) => `${escapeHtml(item.device_id)} ${escapeHtml(item.requested_start_volume ?? item.before)} → ${escapeHtml(item.after)}`).join(", ")}. BASSWIESN hat nicht heimlich zurückkorrigiert.</span>`
      : "";
    const volumeSummary = result.preserve_volumes ? "ohne SetVolume durch BASSWIESN" : result.set_start_volumes ? "mit einzeln bestätigten Startlautstärken" : `bei Lautstärke ${escapeHtml(result.volume)}`;
    output.innerHTML = `<div class="result-status ${firmwareVolumeChanges.length ? "pending" : "ok"}"><strong>${firmwareVolumeChanges.length ? "Multiroom bestätigt – Lautstärkeabweichung beobachtet" : "Multiroom bestätigt"}</strong><span>${escapeHtml(result.master)} ist mit ${(result.members || []).map(escapeHtml).join(", ")} verbunden ${volumeSummary}.</span>${firmwareWarning}</div>`;
    document.getElementById("multiroom-preview").hidden = true;
    confirmButton.hidden = true;
    showToast("Multiroom-Gruppe gestartet und per Readback bestätigt");
  } catch (error) {
    output.textContent = `Multiroom fehlgeschlagen: ${error.message}`;
    showApiError(error, "Multiroom konnte nicht bestätigt werden");
  } finally {
    confirmButton.disabled = false;
  }
});

document.getElementById("multiroom-clear").addEventListener("click", async () => {
  const masterDeviceId = document.getElementById("multiroom-master")?.value || "";
  if (!masterDeviceId) return showToast("Bitte zuerst ein Hauptradio auswählen.", "error");
  if (!window.confirm("Die Gruppe des ausgewählten Hauptradios auflösen und per Readback prüfen?")) return;
  const result = await postJson("/api/multiroom/clear", { master_device_id: masterDeviceId, dry_run: false, memory_checked: true });
  document.getElementById("multiroom-output").innerHTML = `<div class="result-status ${result.cleared ? "ok" : "bad"}"><strong>${result.cleared ? "Ausgewählte Gruppe aufgelöst" : "Gruppe konnte nicht bestätigt aufgelöst werden"}</strong><span>Nur das ausgewählte Hauptradio wurde kontaktiert.</span></div>`;
});

document.getElementById("multiroom-remove")?.addEventListener("click", async () => {
  if (!state.multiroomRemoveDeviceId) return showToast("Bitte zuerst ein Radio auswählen.", "error");
  if (!window.confirm("Dieses Radio aus der aktiven Gruppe entfernen? Es bleibt in BASSWIESN gespeichert.")) return;
  const output = document.getElementById("multiroom-output");
  output.textContent = "Neue Topologie wird geschrieben und auf Master sowie Mitglied geprüft …";
  try {
    const result = await postJson("/api/multiroom/remove-device", { device_id: state.multiroomRemoveDeviceId, confirmation: "REMOVE MEMBER" });
    document.getElementById("multiroom-output").innerHTML = `<div class="result-status ok"><strong>${result.already_standalone ? "Radio war bereits allein" : `${escapeHtml(result.name)} wurde herausgelöst`}</strong><span>${result.remaining?.length ? `Verbleibend: ${result.remaining.map(escapeHtml).join(", ")}` : "Keine weitere Gruppe aktiv."}</span><span>Master- und Member-Readback bestätigt.</span></div>`;
  } catch (error) {
    output.innerHTML = `<div class="result-status bad"><strong>Mitglied nicht bestätigt entfernt</strong><span>${escapeHtml(error.message)}</span><span>Das Radio bleibt in BASSWIESN gespeichert. Gruppenstatus erneut lesen oder Gruppe kontrolliert auflösen.</span></div>`;
    showApiError(error, "Mitglied konnte nicht sicher entfernt werden");
  }
});

document.getElementById("multiroom-latency-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const master = document.getElementById("multiroom-master").value;
  const members = Array.from(document.querySelectorAll("#multiroom-members input:checked")).map((node) => node.value);
  const result = await postJson("/api/multiroom/latency", { mode: form.get("mode"), device_ids: [master, ...members] });
  const ok = result.results.every((item) => item.ok);
  document.getElementById("multiroom-latency-output").innerHTML = `<div class="result-status ${ok ? "ok" : "bad"}"><strong>${ok ? "Synchronisation eingestellt" : "Nicht alle Radios haben bestätigt"}</strong><span>${escapeHtml(result.explanation)}</span></div>`;
});

document.getElementById("multiroom-scenario-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const memberIds = Array.from(document.querySelectorAll("#scenario-members input:checked")).map((node) => node.value);
  const result = await postJson("/api/multiroom/scenarios", {
    name: form.get("name"),
    description: form.get("description"),
    master_device_id: form.get("master_device_id"),
    member_device_ids: memberIds,
    station_id: form.get("station_id"),
    volume: form.get("volume"),
    preserve_volumes: form.get("preserve_volumes") === "on",
    trigger_device_id: form.get("trigger_device_id"),
    trigger_button: form.get("trigger_button"),
  });
  document.getElementById("multiroom-scenario-output").textContent = JSON.stringify(result, null, 2);
  state.multiroomScenarios = await getJson("/api/multiroom/scenarios");
  renderMultiroomScenarios();
});

document.getElementById("multiroom-scenarios")?.addEventListener("click", async (event) => {
  const activate = event.target.closest("[data-scenario-activate]");
  if (activate) {
    const scenarioId = Number(activate.dataset.scenarioActivate);
    if (state.multiroomPendingScenarioId !== scenarioId) return showToast("Bitte zuerst die Vorschau dieses BASSWIESN-Presets öffnen.", "error");
    if (!window.confirm("Dieses BASSWIESN-Multiroom-Preset jetzt auf den angezeigten Radios starten und per Readback prüfen?")) return;
    const activateButton = activate;
    activateButton.disabled = true;
    document.getElementById("multiroom-scenario-output").textContent = "BASSWIESN-Preset wird gestartet und auf allen Radios geprüft …";
    try {
      const result = await postJson(`/api/multiroom/scenarios/${scenarioId}/activate`, {});
      const warnings = result.result?.volume_warnings || [];
      const warning = warnings.length ? `<span>Bose-Firmware änderte Lautstärke: ${warnings.map((item) => `${escapeHtml(item.device_id)} ${escapeHtml(item.before)} → ${escapeHtml(item.after)}`).join(", ")}. Keine automatische Rückkorrektur.</span>` : "";
      document.getElementById("multiroom-scenario-output").innerHTML = `<div class="result-status ${warnings.length ? "pending" : "ok"}"><strong>${escapeHtml(result.scenario)} per Readback bestätigt</strong><span>BASSWIESN_MULTIROOM_PRESET · nicht im Radio gespeichert · manuelle WebUI-Aktivierung.</span>${warning}</div>`;
      state.multiroomPendingScenarioId = null;
      renderMultiroomScenarios();
    } catch (error) {
      showApiError(error, "BASSWIESN-Multiroom-Preset konnte nicht bestätigt werden");
    } finally {
      activateButton.disabled = false;
    }
    return;
  }
  const remove = event.target.closest("[data-scenario-delete]");
  if (remove) {
    if (!window.confirm("Dieses BASSWIESN-Multiroom-Preset löschen? Am Radio wird nichts verändert.")) return;
    const scenarioId = Number(remove.dataset.scenarioDelete);
    const result = await deleteJson(`/api/multiroom/scenarios/${scenarioId}`);
    if (state.multiroomPendingScenarioId === scenarioId) state.multiroomPendingScenarioId = null;
    state.multiroomScenarios = await getJson("/api/multiroom/scenarios");
    renderMultiroomScenarios();
    document.getElementById("multiroom-scenario-output").textContent = `${result.name} wurde nur aus BASSWIESN gelöscht. Das Radio wurde nicht kontaktiert.`;
    return;
  }
  const previewButton = event.target.closest("[data-scenario-preview]");
  if (!previewButton) return;
  const scenarioId = Number(previewButton.dataset.scenarioPreview);
  const result = await postJson(`/api/multiroom/scenarios/${scenarioId}/preview`, {});
  state.multiroomPendingScenarioId = result.blocked ? null : scenarioId;
  const volumes = (result.current_volumes || []).map((item) => `${item.name}: ${item.volume ?? "nicht gelesen"}`).join(", ");
  document.getElementById("multiroom-scenario-output").innerHTML = `<div class="result-status ${result.blocked ? "bad" : "pending"}"><strong>${escapeHtml(result.scenario)} · Vorschau</strong><span>${escapeHtml(result.preset_type)} · nicht im Radio gespeichert · ${escapeHtml(result.activation_contract)}</span><span>Lautstärken vor Start: ${escapeHtml(volumes || "unbekannt")}</span><span>${result.preserve_volumes ? "BASSWIESN sendet kein SetVolume; Firmwareabweichungen werden per Readback gemeldet." : `Geplante Lautstärke ${escapeHtml(result.volume)}`}</span><span>${result.blocked ? `Blockiert: ${escapeHtml((result.protected_devices || []).join(", "))}` : "Erst der nun freigegebene Bestätigungsbutton startet die Radios."}</span></div>`;
  renderMultiroomScenarios();
});


document.getElementById("media-server-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/media/list-servers`, { dry_run: form.get("dry_run") === "on" });
  document.getElementById("media-server-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("media-local-browser")?.addEventListener("change", (event) => {
  const first = event.target.files?.[0];
  const uriInput = document.querySelector('#media-playlist-form input[name="uri"]');
  if (first && uriInput) uriInput.value = first.webkitRelativePath || first.name;
});

document.getElementById("media-playlist-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const result = await postJson("/api/media-playlists", Object.fromEntries(new FormData(formElement).entries()));
  document.getElementById("media-server-output").textContent = JSON.stringify(result, null, 2);
  formElement?.reset?.();
  state.mediaPlaylists = await getJson("/api/media-playlists");
  renderMediaLibrary();
});

document.getElementById("clear-media-playlists")?.addEventListener("click", async () => {
  if (!window.confirm("Alle gespeicherten Medien-Sammlungen entfernen?")) return;
  const result = await deleteJson("/api/media-playlists");
  state.mediaPlaylists = [];
  renderMediaLibrary();
  showToast(`${result.deleted} Sammlung(en) entfernt.`);
});

document.getElementById("backup-plan-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/backup/plan`, {});
  document.getElementById("backup-plan-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("reference-create-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const result = await postJson(`/api/reference-setups/from-device/${encodeURIComponent(deviceId)}`, { name: form.get("name"), notes: form.get("notes") });
  document.getElementById("backup-reference-output").textContent = JSON.stringify(result, null, 2);
  state.referenceSetups = await getJson("/api/reference-setups");
  renderReferenceSetups();
});

document.getElementById("reference-apply-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await postJson(`/api/reference-setups/${encodeURIComponent(form.get("setup_id"))}/apply/${encodeURIComponent(form.get("device_id"))}`, { dry_run: form.get("dry_run") === "on", memory_checked: form.get("memory_checked") === "on" });
  document.getElementById("backup-reference-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("telnet-capabilities-load")?.addEventListener("click", async () => {
  const deviceId = document.getElementById("telnet-device-select")?.value;
  if (!deviceId) return;
  const result = await getJson(`/api/devices/${encodeURIComponent(deviceId)}/telnet/capabilities`);
  document.getElementById("telnet-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("telnet-reboot-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  if (!window.confirm("Telnet ist unverschlüsselt und startet das Radio neu. Fortfahren?")) return;
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/telnet/reboot`, { confirmation: form.get("confirmation") || "" });
  document.getElementById("telnet-output").textContent = JSON.stringify(result, null, 2);
  document.getElementById("telnet-job-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("standby-clock-status")?.addEventListener("click", async () => {
  const deviceId = document.getElementById("standby-clock-device-select")?.value;
  if (!deviceId) return;
  const result = await getJson(`/api/devices/${encodeURIComponent(deviceId)}/standby-clock/status`);
  document.getElementById("standby-clock-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("standby-clock-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  if (!window.confirm("Standby-Uhr-Recovery schreibt /clockDisplay und prüft danach den Status. Fortfahren?")) return;
  const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/standby-clock/restore`, {
    confirmation: form.get("confirmation") || "",
    timezone: form.get("timezone") || "Europe/Berlin",
  });
  document.getElementById("standby-clock-output").textContent = JSON.stringify(result, null, 2);
  document.getElementById("telnet-job-output").textContent = JSON.stringify(result, null, 2);
});

document.querySelectorAll("[data-view-jump]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.viewJump;
    document.querySelector(`.nav-button[data-view="${target}"]`)?.click();
  });
});

document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-guided-preset-dismiss]")) return;
  if (state.guidedPreset.deviceId) localStorage.setItem(guidedPresetKey(state.guidedPreset.deviceId), "dismissed");
  state.guidedPreset = { active: false, deviceId: "", step: "", dismissed: true };
  document.querySelectorAll(".guided-pulse").forEach((node) => node.classList.remove("guided-pulse"));
  document.getElementById("guided-preset-banner")?.remove();
});


async function sendKeyCommand(key, button = null) {
  const deviceId = document.getElementById("key-device-select")?.value;
  const device = state.devices.find((item) => item.device_id === deviceId);
  const status = document.getElementById("key-command-status");
  if (!deviceId || !key) return;
  let confirmation = "";
  if (key === "POWER") {
    if (!window.confirm(`Power/Standby für ${text(device?.name, deviceId)} senden?`)) return;
    confirmation = "YES";
  }
  if (button) button.disabled = true;
  status.textContent = `${key} wird an ${text(device?.name, deviceId)} gesendet…`;
  status.className = "friendly-status is-working";
  try {
    const safeVolume = selectedSafeStartVolume();
    const payload = { key, confirmation, trigger: "webui" };
    if (safeVolume !== null) payload.safe_volume = safeVolume;
    const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/key`, payload);
    document.getElementById("key-command-output").textContent = JSON.stringify(result, null, 2);
    status.textContent = `${text(device?.name, "Radio")} hat den Befehl ${key} angenommen.`;
    status.className = "friendly-status is-ok";
    if (key.startsWith("PRESET_")) {
      await postJson("/api/play-history/start", { device_id: deviceId, station_name: key.replace("_", " "), trigger: key.toLowerCase(), source: "PRESET" });
    } else if (["STOP", "PAUSE", "PLAY_PAUSE"].includes(key)) {
      await postJson("/api/play-history/event", { device_id: deviceId, trigger: "stop", station_name: key });
    }
    state.playHistory = await getJson("/api/play-history");
    state.playStats = await getJson("/api/stats/playback");
    renderPlayback();
  } catch (error) {
    document.getElementById("key-command-output").textContent = String(error);
    const stillPlaying = ["STOP", "PAUSE"].includes(key)
      && /spielt laut Readback aber weiter|did not confirm/.test(error?.message || "");
    status.textContent = stillPlaying
      ? `${text(device?.name, "Radio")} spielt laut Readback weiter. Kein falscher ${key}-Erfolg; Power/Standby ist eine getrennte Aktion.`
      : `${text(device?.name, "Radio")} konnte nicht gesteuert werden.`;
    status.className = "friendly-status is-error";
    showApiError(error, `${key} wurde nicht bestätigt`);
  } finally {
    if (button) button.disabled = false;
    if (button && ["VOLUME_UP", "VOLUME_DOWN"].includes(key)) button.blur();
  }
}

document.getElementById("key-command-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await sendKeyCommand(form.get("key"), event.submitter);
});

const volumeHold = { timer: null, activeKey: "", busy: false, suppressClick: false };

function isTouchVolumeEvent(event) {
  return event.pointerType === "touch" || event.pointerType === "pen" || window.matchMedia("(hover: none)").matches;
}

function stopVolumeHold() {
  if (volumeHold.timer) window.clearInterval(volumeHold.timer);
  volumeHold.timer = null;
  volumeHold.activeKey = "";
  volumeHold.busy = false;
  document.querySelectorAll(".remote-volume.is-holding").forEach((button) => button.classList.remove("is-holding"));
}

async function sendHeldVolume(key, button) {
  if (volumeHold.busy) return;
  volumeHold.busy = true;
  try {
    await sendKeyCommand(key, button);
  } finally {
    volumeHold.busy = false;
  }
}

document.getElementById("key-command-grid")?.addEventListener("pointerdown", async (event) => {
  const button = event.target.closest("[data-key-command]");
  if (!button || !["VOLUME_UP", "VOLUME_DOWN"].includes(button.dataset.keyCommand)) return;
  event.preventDefault();
  stopVolumeHold();
  const key = button.dataset.keyCommand;
  volumeHold.activeKey = key;
  button.classList.add("is-holding");
  await sendHeldVolume(key, button);
  if (isTouchVolumeEvent(event)) {
    volumeHold.suppressClick = true;
    stopVolumeHold();
    return;
  }
  volumeHold.timer = window.setInterval(() => sendHeldVolume(key, button), 320);
});

["pointerup", "pointerleave", "pointercancel", "lostpointercapture", "touchend", "touchcancel"].forEach((name) => {
  document.getElementById("key-command-grid")?.addEventListener(name, () => {
    if (volumeHold.activeKey) volumeHold.suppressClick = true;
    stopVolumeHold();
  });
});

["visibilitychange", "pagehide"].forEach((name) => {
  window.addEventListener(name, stopVolumeHold);
});

document.getElementById("key-command-grid")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-key-command]");
  if (!button) return;
  if (volumeHold.suppressClick && ["VOLUME_UP", "VOLUME_DOWN"].includes(button.dataset.keyCommand)) {
    volumeHold.suppressClick = false;
    return;
  }
  await sendKeyCommand(button.dataset.keyCommand, button);
});

document.getElementById("display-settings-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const deviceId = form.get("device_id");
  const action = event.submitter?.dataset.displayAction || "save";
  const payload = {
    mode: form.get("mode"),
    include_date: form.get("include_date") === "on",
    probe: form.get("probe") === "on",
    station_id: form.get("station_id") || null,
    dry_run: form.get("dry_run") === "on",
  };
  let result;
  if (action === "preview") {
    result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/display/metadata-preview`, payload);
  } else if (action === "direct_select") {
    result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/display/direct-select`, payload);
  } else {
    result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/display/settings`, { mode: payload.mode });
  }
  document.getElementById("display-output").textContent = JSON.stringify(result, null, 2);
});



document.querySelector("[data-setup-flow-action=radio]")?.addEventListener("click", async () => {
  try { await runSetupFlowAction("radio"); }
  catch (error) { document.getElementById("setup-wizard-output").textContent = String(error); }
});

document.querySelector("[data-setup-flow-action=skip-backup]")?.addEventListener("click", async () => {
  try { await runSetupFlowAction("skip-backup"); }
  catch (error) { document.getElementById("setup-output").textContent = String(error); }
});

document.querySelector("[data-setup-flow-action=apply]")?.addEventListener("click", async () => {
  try { await runSetupFlowAction("apply"); }
  catch (error) { document.getElementById("cloud-route-output").textContent = String(error); }
});

document.querySelector("[data-setup-flow-action=rollback]")?.addEventListener("click", async () => {
  try { await runSetupFlowAction("rollback"); }
  catch (error) { document.getElementById("cloud-route-output").textContent = String(error); }
});

document.querySelector("[data-setup-flow-action=verify]")?.addEventListener("click", async () => {
  try { await runSetupFlowAction("verify"); }
  catch (error) { document.getElementById("setup-output").textContent = String(error); }
});

document.querySelector("[data-setup-flow-prev]")?.addEventListener("click", () => {
  state.setupFlowStep = Math.max(0, state.setupFlowStep - 1);
  renderSetupFlow();
});

document.querySelector("[data-setup-flow-next]")?.addEventListener("click", () => {
  const currentKey = SETUP_FLOW_STEPS[state.setupFlowStep]?.key;
  if (!state.setupFlowDone[currentKey]) return;
  state.setupFlowStep = Math.min(SETUP_FLOW_STEPS.length - 1, state.setupFlowStep + 1);
  if (SETUP_FLOW_STEPS[state.setupFlowStep]?.key === "done") markSetupStepDone("done");
  renderSetupFlow();
});

document.getElementById("display-recovery-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await postJson(`/api/devices/${encodeURIComponent(form.get("device_id"))}/display-recovery/plan`, { mode: form.get("mode"), minutes: form.get("minutes"), cleanup_required: form.get("cleanup_required") === "on" });
  document.getElementById("display-output").textContent = JSON.stringify(result, null, 2);
});

document.getElementById("telemetry-debug-toggle")?.addEventListener("change", renderTelemetry);
document.getElementById("download-support-bundle")?.addEventListener("click", () => {
  window.location.href = "/api/support-bundle";
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  const id = button?.id;
  if (!button) return;
  const deviceId = selectedDeviceId();
  try {
    if (id === "preset-checker-refresh") {
      document.getElementById("preset-checker-message").textContent = "Radio, Provider und Streams werden read-only geprüft …";
      await loadPresetsForSelectedDevice(true);
      document.getElementById("preset-checker-message").textContent = "Prüfung abgeschlossen. UNKNOWN bedeutet: Es fehlt belastbare Evidence – nicht automatisch ein Fehler.";
    } else if (id === "update-check") {
      const output = document.getElementById("update-status");
      output.textContent = i18nT("loading");
      state.systemSettings = await postJson("/api/system/settings", {
        update_check_enabled: document.getElementById("update-check-enabled")?.checked ? "true" : "false",
        update_manifest_url: document.getElementById("update-manifest-url")?.value || "",
        update_repo_url: document.getElementById("update-repo-url")?.value || "",
        update_channel: document.getElementById("update-channel")?.value || "manual",
      });
      const result = await postJson("/api/update/check", {});
      output.textContent = `${result.message}${result.remote_version ? ` (${result.remote_version})` : ""}`;
    } else if (id === "provider-status-load") {
      const selected = document.getElementById("telemetry-device-select")?.value;
      if (!selected) return;
      document.getElementById("provider-status-message").textContent = "Provider- und Runtime-Status wird gelesen …";
      state.providerStatus = await getJson(`/api/devices/${encodeURIComponent(selected)}/provider-status`);
      renderProviderStatus();
      document.getElementById("provider-status-message").textContent = "Status aktualisiert.";
    }
  } catch (error) {
    const output = id?.startsWith("preset-checker") ? document.getElementById("preset-checker-message") : id === "update-check" ? document.getElementById("update-status") : document.getElementById("provider-status-message");
    if (output) output.textContent = `${error.message} Nächster Schritt: Radio/Netzwerk prüfen oder Support Bundle laden.`;
    showApiError(error, "Status/Aktion fehlgeschlagen");
  }
});

document.querySelectorAll(".step-pill").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".step-pill").forEach((item) => item.classList.remove("is-current"));
    document.querySelectorAll(".setup-step").forEach((item) => item.classList.remove("is-current"));
    button.classList.add("is-current");
    document.getElementById(`setup-step-${button.dataset.step}`).classList.add("is-current");
  });
});

document.querySelectorAll("[data-open]").forEach((button) => {
  button.addEventListener("click", async () => {
    const url = button.dataset.open;
    try { document.getElementById("setup-output").textContent = JSON.stringify(await getJson(url), null, 2); }
    catch (error) { document.getElementById("setup-output").textContent = String(error); }
  });
});


function startSetupRiskGate() {
  const checkbox = document.getElementById("setup-risk-ack");
  const countdown = document.getElementById("setup-risk-countdown");
  const overlay = document.getElementById("setup-risk-box");
  const layout = document.getElementById("setup-layout-main");
  if (!checkbox || !countdown || !overlay) return;
  if (localStorage.getItem("basswiesn_setup_risk_ack") === "yes") {
    overlay.classList.add("is-hidden");
    layout?.classList.remove("setup-locked");
    return;
  }
  let remaining = 10;
  checkbox.disabled = true;
  countdown.textContent = String(remaining);
  const timer = setInterval(() => {
    remaining -= 1;
    countdown.textContent = String(Math.max(remaining, 0));
    if (remaining <= 0) {
      checkbox.disabled = false;
      clearInterval(timer);
    }
  }, 1000);
  checkbox.addEventListener("change", () => {
    if (!checkbox.checked) return;
    localStorage.setItem("basswiesn_setup_risk_ack", "yes");
    overlay.classList.add("is-hidden");
    layout?.classList.remove("setup-locked");
  });
}

function updateClock() {
  const now = new Date();
  const date = document.getElementById("clock-date");
  const time = document.getElementById("clock-time");
  if (date) date.textContent = now.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
  if (time) time.textContent = now.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  updateServerIdentity();
}
setInterval(updateClock, 1000);
updateClock();
startSetupRiskGate();

const labDetails = {
  envswitch: {
    title: "envswitch / boseurls",
    body: `Status: instruction/plan only. Not executed automatically.

Purpose: sync the runtime Marge and update URL layer after SDK XML changes.

Guard rails:
- backup first
- memory check first
- validate host and port before command construction
- use old POSIX-safe commands only
- capture current getpdo CurrentSystemConfiguration first
- reboot or verify /info after change`,
  },
  telnet: {
    title: "CLI 17000",
    body: `Status: instruction/plan only. Not executed automatically.

Purpose: diagnostics, recovery path and expert-only SSH preparation.

Known useful checks:
- getpdo CurrentSystemConfiguration
- sys reboot
- sys configuration bmxRegistryUrl ...
- envswitch boseurls set ...

Portable FW 27.x finding:
- the normal setup uses the confirmed HTTP/CLI path and does not require SSH
- BASSWIESN detects port 17000 read-only and shows the strategy
- BASSWIESN does not run shell-injection or auto-enable SSH from the normal setup flow

Normal setup should not depend on CLI 17000 when HTTP/XML APIs are enough.`,
  },
  persistence: {
    title: "Persistence scan",
    body: `Status: instruction/plan only. Read-only until backup exists.

Focus paths:
- /mnt/nv/BoseApp-Persistence/1/SystemConfigurationDB.xml
- /mnt/nv/BoseApp-Persistence/1/Marge.xml
- /mnt/nv/BoseApp-Persistence/1/Sources.xml
- /mnt/nv/BoseApp-Persistence/1/Presets.xml

Use old embedded-Linux-safe commands: sh, cat, ls, du, df, grep, sed, awk. Avoid modern shell features.`,
  },
  airplay: {
    title: "AirPlay notes",
    body: `Status: research notes only.

AirPlay and AirPlay2 findings stay in Lab because capability flags and multiroom behavior differ by firmware/model.

No automatic AirPlay patch belongs in the normal setup flow.`,
  },
};

document.querySelectorAll(".lab-item").forEach((button) => {
  button.addEventListener("click", () => {
    const item = labDetails[button.dataset.lab];
    document.querySelectorAll(".lab-item").forEach((node) => node.classList.remove("is-active"));
    button.classList.add("is-active");
    document.getElementById("lab-title").textContent = item.title;
    document.getElementById("lab-detail").textContent = item.body;
  });
});

async function refreshAll(event) {
  const button = event?.currentTarget;
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Wird aktualisiert…";
  }
  try {
    if (["refresh-all", "reload-devices", "setup-refresh"].includes(button?.id)) {
      state.devices = await getJson("/api/devices?live=true");
      renderDevices();
    }
    await loadAll();
    showToast("Daten wurden aktualisiert.");
  } catch (error) {
    showToast(`Aktualisierung fehlgeschlagen: ${String(error)}`, "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

["refresh-all", "reload-devices", "reload-stations", "reload-preset-data", "reload-debug", "setup-refresh", "reload-multiroom", "reload-device-settings", "reload-schedules", "reload-telemetry", "reload-system-settings", "reload-media", "reload-backup", "reload-controls", "reload-display", "reload-health", "reload-features"].forEach((id) => {
  const button = document.getElementById(id);
  if (button) button.addEventListener("click", refreshAll);
});

document.getElementById("clear-runtime-logs")?.addEventListener("click", async () => {
  if (!window.confirm("Laufzeitprotokolle wirklich leeren? Radio-Sicherungen bleiben erhalten.")) return;
  const result = await postJson("/api/maintenance/clear-logs", { confirmation: "YES" });
  document.getElementById("maintenance-message").textContent = `${result.request_logs + result.telemetry_logs} Protokolle entfernt. Sicherungen wurden behalten.`;
  await refreshAll();
});

document.getElementById("clear-test-devices")?.addEventListener("click", async () => {
  if (!window.confirm("Alle erkannten Testgeräte und deren Testdaten entfernen?")) return;
  const result = await postJson("/api/maintenance/clear-test-devices", { confirmation: "YES" });
  document.getElementById("maintenance-message").textContent = `${result.removed_count} Testgeräte entfernt.`;
  await refreshAll();
});

async function pollSetupJob(jobId) {
  if (!jobId) return;
  if (state.setupJobPoller) window.clearInterval(state.setupJobPoller);
  state.setupJobPoller = window.setInterval(async () => {
    try {
      state.setupJob = await getJson(`/api/setup/jobs/${encodeURIComponent(jobId)}`);
      renderSetupBatch();
      if (!state.setupJob.running) {
        window.clearInterval(state.setupJobPoller);
        state.setupJobPoller = null;
        await refreshDeviceState({ live: true });
      }
    } catch {
      window.clearInterval(state.setupJobPoller);
      state.setupJobPoller = null;
    }
  }, 1500);
}

document.getElementById("setup-rebuild-discover")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  state.setupRebuildPreview = null;
  state.setupRebuildDiscovery = { running: true };
  renderSetupRebuildDiscovery();
  try {
    state.setupRebuildDiscovery = await postJson("/api/setup/rebuild/discover", { timeout_seconds: 3 });
    await refreshSetupRebuildDevices();
    renderSetupRebuildDiscovery();
    const count = Number(state.setupRebuildDiscovery.verified || 0);
    showToast(count ? `${count} verbundene Radio(s) sicher bestätigt.` : "Keine bereits mit dem Heimnetz verbundenen Radios gefunden.", count ? "ok" : "warning");
  } catch (error) {
    state.setupRebuildDiscovery = {
      found: 0,
      verified: 0,
      failures: [{ device_id: "", reason: String(error) }],
      network_configuration_changed: false,
    };
    renderSetupRebuildDiscovery();
    showApiError(error, "Die ausdrückliche LAN-Suche ist fehlgeschlagen");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("setup-rebuild-refresh")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await refreshSetupRebuildDevices();
    showToast("Radio- und Serverauswahl wurden aktualisiert.");
  } catch (error) {
    showApiError(error, "Radios konnten nicht gelesen werden");
  } finally { button.disabled = false; }
});

document.getElementById("setup-rebuild-devices")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-setup-audio-safety]");
  if (!button) return;
  const deviceId = button.dataset.setupAudioSafety;
  const device = state.setupRebuildDevices.find((item) => item.device_id === deviceId);
  if (!device) return;
  if (!window.confirm(`${device.name} wird identifiziert, ohne Audiostart auf Lautstärke 1 gesetzt und anschließend mit STOP/STANDBY gesichert. Fortfahren?`)) return;
  button.disabled = true;
  try {
    const result = await postJson(`/api/setup/rebuild/devices/${encodeURIComponent(deviceId)}/audio-safety/verify`, {
      confirm_stop_and_volume_one: true,
    });
    await refreshSetupRebuildDevices();
    const output = document.getElementById("setup-rebuild-output");
    if (output) output.textContent = JSON.stringify(result, null, 2);
    showToast("Audio-Sicherheitsprüfung bestanden: Identität, STOP/STANDBY und Lautstärke 1 bestätigt.");
  } catch (error) {
    showApiError(error, "Audio-Sicherheitsprüfung fehlgeschlagen");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("setup-rebuild-preview")?.addEventListener("click", async () => {
  try {
    const deviceIds = setupRebuildSelectedIds();
    if (!deviceIds.length) throw new Error("Bitte mindestens ein Radio auswählen.");
    const serverHost = setupRebuildHost();
    if (!serverHost) throw new Error("Keine geeignete BASSWIESN-LAN-Adresse verfügbar.");
    const result = await postJson("/api/setup/rebuild/preview", {
      device_ids: deviceIds,
      server_host: serverHost,
      playback_test: Boolean(document.getElementById("setup-rebuild-playback")?.checked),
    });
    state.setupRebuildPreview = result;
    const output = document.getElementById("setup-rebuild-output");
    if (output) output.textContent = JSON.stringify(result, null, 2);
    const details = document.getElementById("setup-rebuild-details");
    if (details) details.open = true;
    showToast(result.ready_for_start ? `Die gemeinsame Vorschau für ${deviceIds.length} Radio(s) ist bereit.` : "Mindestens ein Radio benötigt noch bestätigte Geräteinformationen.", result.ready_for_start ? "ok" : "warning");
  } catch (error) { showApiError(error, "Setup-Preview fehlgeschlagen"); }
});

document.getElementById("setup-rebuild-start")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  try {
    const deviceIds = setupRebuildSelectedIds();
    if (!deviceIds.length) throw new Error("Bitte mindestens ein Radio auswählen.");
    const serverHost = setupRebuildHost();
    if (!serverHost) throw new Error("Keine geeignete BASSWIESN-LAN-Adresse verfügbar.");
    const playbackTest = Boolean(document.getElementById("setup-rebuild-playback")?.checked);
    const selected = state.setupRebuildDevices.filter((item) => deviceIds.includes(item.device_id));
    const ineligible = selected.find((item) => !item.eligible);
    if (ineligible) throw new Error(`${ineligible.name}: ${ineligible.blocking_reason || "Das Geräteprofil ist noch nicht bestätigt."}`);
    if (playbackTest && selected.some((item) => item.audio_safety_locked)) throw new Error("Die Wiedergabeprüfung ist für mindestens ein Radio gesperrt. Bitte zuerst die sichtbare Audio-Sicherheitsprüfung ausführen oder den Audiotest abwählen.");
    const audioText = playbackTest ? " Anschließend folgt eine Wiedergabeprüfung ausschließlich bei Lautstärke 1." : "";
    const names = selected.map((item) => item.name).join(", ");
    if (!window.confirm(`BASSWIESN sichert ${selected.length} Radio(s) (${names}), richtet jedes unabhängig auf ${serverHost} ein und prüft jeden Readback.${audioText} Ein Fehler stoppt die übrigen Radios nicht. Fortfahren?`)) return;
    button.disabled = true;
    state.setupRebuildJob = await postJson("/api/setup/rebuild/start", {
      device_ids: deviceIds,
      server_host: serverHost,
      playback_test: playbackTest,
      dry_run: false,
    });
    state.setupRebuildPreview = null;
    renderSetupRebuild();
    pollSetupRebuildJob(state.setupRebuildJob.job_id);
  } catch (error) {
    showApiError(error, "Setup-Rebuild konnte nicht gestartet werden");
    button.disabled = false;
  }
});

document.getElementById("setup-rebuild-cancel")?.addEventListener("click", async () => {
  if (!state.setupRebuildJob?.job_id) return;
  try {
    state.setupRebuildJob = await postJson(`/api/setup/rebuild/jobs/${encodeURIComponent(state.setupRebuildJob.job_id)}/cancel`, {});
    renderSetupRebuild();
  } catch (error) { showApiError(error, "Setup-Rebuild konnte nicht abgebrochen werden"); }
});

document.getElementById("setup-rebuild-rollback")?.addEventListener("click", async () => {
  if (!state.setupRebuildJob?.job_id) return;
  if (!window.confirm("Gesicherte Routingwerte wiederherstellen und die betroffenen Radios kontrolliert neu starten?")) return;
  try {
    state.setupRebuildJob = await postJson(`/api/setup/rebuild/jobs/${encodeURIComponent(state.setupRebuildJob.job_id)}/rollback`, {});
    renderSetupRebuild();
  } catch (error) { showApiError(error, "Rollback fehlgeschlagen"); }
});

document.getElementById("setup-batch-start")?.addEventListener("click", async () => {
  const deviceIds = Array.from(document.querySelectorAll("[data-setup-device]:checked")).map((item) => item.dataset.setupDevice);
  if (!deviceIds.length) {
    showToast("Bitte mindestens ein Radio auswählen.", "error");
    return;
  }
  const host = currentSetupHost();
  await persistSetupHost(host);
  state.setupJob = await postJson("/api/setup/jobs/start", { device_ids: deviceIds, host, port: Number(cloudPort), dry_run: false });
  await refreshDeviceState({ live: false });
  pollSetupJob(state.setupJob.job_id);
});

document.getElementById("setup-batch-cancel")?.addEventListener("click", async () => {
  if (!state.setupJob?.job_id) return;
  state.setupJob = await postJson(`/api/setup/jobs/${encodeURIComponent(state.setupJob.job_id)}/cancel`, {});
  renderSetupBatch();
});

document.addEventListener("click", (event) => {
  const check = event.target?.closest?.("[data-check-device]");
  if (check) {
    event.preventDefault();
    refreshDeviceState({ live: true }).then(() => showToast("Radio-Status wurde geprüft.")).catch((error) => showToast(`Prüfung fehlgeschlagen: ${String(error)}`, "error"));
    return;
  }
  const remove = event.target?.closest?.("[data-remove-device]");
  if (remove) {
    event.preventDefault();
    const deviceId = remove.dataset.removeDevice;
    const device = state.devices.find((item) => item.device_id === deviceId);
    const confirmation = window.prompt(`${text(device?.name, deviceId)} nur lokal aus BASSWIESN entfernen? Am Radio wird nichts geändert.\n\nZum Bestätigen YES eingeben.`);
    if (!confirmation || confirmation.toLowerCase() !== "yes") return;
    fetch(`/api/devices/${encodeURIComponent(deviceId)}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation: "YES" }),
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(await response.text());
        return response.json();
      })
      .then(async () => {
        await refreshDeviceState({ live: false });
        showToast("Radio wurde lokal entfernt.");
      })
      .catch((error) => showApiError(error, "Radio konnte nicht entfernt werden"));
    return;
  }
  const view = event.target?.dataset?.viewJump;
  if (view) document.querySelector(`.nav-button[data-view="${view}"]`)?.click();
});

document.getElementById("telemetry-range")?.addEventListener("change", refreshAll);
document.getElementById("download-telemetry-json")?.addEventListener("click", () => {
  const range = document.getElementById("telemetry-range")?.value || "24h";
  window.location.href = `/api/diagnostics/telemetry/export?format=json&range=${encodeURIComponent(range)}`;
});
document.getElementById("download-telemetry-csv")?.addEventListener("click", () => {
  const range = document.getElementById("telemetry-range")?.value || "24h";
  window.location.href = `/api/diagnostics/telemetry/export?format=csv&range=${encodeURIComponent(range)}`;
});
document.getElementById("download-telemetry-report")?.addEventListener("click", () => {
  const range = document.getElementById("telemetry-range")?.value || "24h";
  window.location.href = `/api/diagnostics/telemetry/report?range=${encodeURIComponent(range)}`;
});
document.getElementById("storage-check")?.addEventListener("click", async () => {
  state.storageSummary = await getJson("/api/maintenance/storage");
  renderTelemetry();
});
document.getElementById("cleanup-dry-run")?.addEventListener("click", async () => {
  state.cleanupPreview = await postJson("/api/maintenance/cleanup/dry-run", {});
  renderTelemetry();
});
document.getElementById("cleanup-run")?.addEventListener("click", async () => {
  if (!window.confirm("Cleanup nach Retention-Regeln ausführen? Aktuelle Presets, Settings und Support-Bundles bleiben erhalten.")) return;
  state.cleanupPreview = await postJson("/api/maintenance/cleanup/run", {});
  state.storageSummary = await getJson("/api/maintenance/storage");
  renderTelemetry();
});

document.addEventListener("change", (event) => {
  if (event.target?.id === "telemetry-range") refreshAll();
});

document.addEventListener("click", async (event) => {
  const id = event.target?.id;
  if (!["download-telemetry-json", "download-telemetry-csv", "download-telemetry-report", "storage-check", "cleanup-dry-run", "cleanup-run"].includes(id)) return;
  const range = document.getElementById("telemetry-range")?.value || "24h";
  if (id === "download-telemetry-json") window.location.href = `/api/diagnostics/telemetry/export?format=json&range=${encodeURIComponent(range)}`;
  if (id === "download-telemetry-csv") window.location.href = `/api/diagnostics/telemetry/export?format=csv&range=${encodeURIComponent(range)}`;
  if (id === "download-telemetry-report") window.location.href = `/api/diagnostics/telemetry/report?range=${encodeURIComponent(range)}`;
  if (id === "storage-check") state.storageSummary = await getJson("/api/maintenance/storage");
  if (id === "cleanup-dry-run") state.cleanupPreview = await postJson("/api/maintenance/cleanup/dry-run", {});
  if (id === "cleanup-run") {
    if (!window.confirm("Cleanup nach Retention-Regeln ausführen? Aktuelle Presets, Settings und Support-Bundles bleiben erhalten.")) return;
    state.cleanupPreview = await postJson("/api/maintenance/cleanup/run", {});
    state.storageSummary = await getJson("/api/maintenance/storage");
  }
  if (id.startsWith("storage") || id.startsWith("cleanup")) renderTelemetry();
});

function maybeShowFirstRunWarning() {
  const modal = document.getElementById("first-run-warning");
  const read = document.getElementById("first-run-warning-read");
  const ack = document.getElementById("first-run-warning-ack");
  if (!modal || !read || !ack || !state.systemSettings) return;
  const never = document.getElementById("first-run-warning-never");
  if (state.systemSettings.first_run_warning_required !== "true" || state.systemSettings.show_startup_warning === "false") return;
  modal.hidden = false;
  syncBodyScrollLock();
  read.addEventListener("change", () => { ack.disabled = !read.checked; });
  ack.addEventListener("click", async () => {
    if (never?.checked) {
      const result = await postJson("/api/system/warnings/ack", {});
      state.systemSettings.first_run_warning_required = result.first_run_warning_required;
    }
    modal.hidden = true;
    syncBodyScrollLock();
    renderSystemSettings();
  }, { once: true });
}

const pageHelp = {
  dashboard: ["Start", "Hier siehst du auf einen Blick, ob BASSWIESN und deine Radios erreichbar sind.", ["Status prüfen", "Radio auswählen", "Zur passenden Seite wechseln"], "Rot bedeutet: Dieser Punkt braucht Aufmerksamkeit. Die Hilfe erklärt, ob du selbst etwas tun musst."],
  setup: ["Einrichtung", "Führt ein Radio nach Reset oder Neuinstallation sicher bis zur lokalen BASSWIESN-Cloud.", ["Server erkennen", "Radio sichern", "Verbinden und prüfen"], "Verwende immer die LAN-Adresse des BASSWIESN-Rechners, niemals 127.0.0.1."],
  devices: ["Radios", "Zeigt jedes echte Gerät genau einmal – mit Seriennummer, Firmware und Cloud-Ziel.", ["Radio wählen", "Live aktualisieren", "Name oder Einstellungen ändern"], "Die Seriennummer identifiziert das physische Radio; die Geräte-ID ist seine Netzwerkkennung."],
  health: ["Status & Diagnose", "Zeigt Playback, Provider, Metadaten, Reporting, Restrictions und AirPlay als getrennte Verträge.", ["Radio wählen", "Zustände vergleichen", "Zeitlinie von oben nach unten lesen"], "Das Öffnen dieser Seite liest nur die lokale Datenbank und sendet keine Anfrage an das Radio."],
  controls: ["Fernbedienung", "Bedient Lautstärke, Wiedergabe, Presets und Standby wie eine normale Fernbedienung.", ["Radio wählen", "Lautstärke festlegen", "Taste drücken"], "Beim Testen zuerst eine niedrige Lautstärke wählen."],
  stations: ["Sender", "Hier legst du Internetradios an, suchst Online-Sender und startest sie auf einem Radio.", ["Sender finden", "Radio wählen", "Wiedergabe starten"], "Am zuverlässigsten sind direkte HTTP/HTTPS-MP3-Streams."],
  presets: ["Presets", "Ordnet Sender den sechs echten Preset-Tasten eines Radios zu.", ["Radio und Taste wählen", "Sender auswählen", "Speichern und prüfen"], "BASSWIESN meldet Erfolg erst, nachdem das Radio den Slot zurückgelesen und bestätigt hat."],
  multiroom: ["Multiroom", "Verbindet mehrere SoundTouch-Radios zu einer synchronen lokalen Zone.", ["Hauptradio wählen", "Räume markieren", "Multiroom starten"], "Für normale Nutzung ist setZone richtig. Capabilities liest nur Fähigkeiten; SYNC_TO_ROOM bildet keine Gruppe."],
  schedules: ["Wecker Timer", "Speichert zeitgesteuerte Sender-, Preset-, Lautstärke- und Multiroom-Aktionen.", ["Zeit wählen", "Radio und Sender oder Preset wählen", "Wecker Timer speichern"], "Prüfe Start, Ende und Wochentage besonders sorgfältig."],
  "device-settings": ["Radio-Einstellungen", "Ändert unterstützte Einstellungen wie Bass, Sprache, Uhr und Energiesparen.", ["Radio wählen", "Wert einstellen", "Am Radio bestätigen lassen"], "Angebotene Werte werden aus Firmwarewissen und Gerätefähigkeiten begrenzt."],
  display: ["Display", "Steuert normale Wiedergabemetadaten und zeigt getrennt, welche Uhr- und WLAN-Daten verfügbar sind.", ["Radio wählen", "Anzeigeart wählen", "Speichern oder Sender starten"], "Künstlicher Text wird nicht als falscher Sendername an das Display geschickt."],
  media: ["Musikbibliothek", "Bereitet DLNA-, NAS- und lokale Medienquellen für SoundTouch vor.", ["Radio und Server wählen", "Ordner oder Titel-ID ermitteln", "Quelle auswählen und PlaybackRequest senden"], "Das Radio benötigt die Server-UUID, die Source (z. B. STORED_MUSIC/UPNP), eine Container-ID für Ordner/Album und eine Item-ID für den Titel. Der bestätigte Ablauf ist /listMediaServers → /selectLocalSource → /navigate → /playbackRequest."],
  "system-settings": ["BASSWIESN", "Legt Sprache, Zeitzone und allgemeine Standardwerte dieser Oberfläche fest.", ["Standard wählen", "Speichern", "Oberfläche aktualisieren"], "Diese Einstellungen sind von den Einstellungen eines einzelnen Radios getrennt."],
  backup: ["Sicherung", "Sichert Radiozustände und bereitet einen kontrollierten Wiederherstellungsweg vor.", ["Radio wählen", "Sicherung erstellen", "Vor Restore vergleichen"], "Vollständige Restores nur auf dasselbe Gerät und dieselbe Firmware anwenden."],
  config: ["Technik", "Zeigt die von BASSWIESN verwendeten lokalen Cloud-, Registry- und Gerätepfade.", ["Bereich wählen", "Status lesen", "Nur bestätigte Änderungen ausführen"], "Dieser Bereich ist für Diagnose; normale Bedienung findet auf den Hauptseiten statt."],
  telnet: ["CLI 17000", "Erstellt kontrollierte Befehle für die interne SoundTouch-Engineering-Schnittstelle.", ["Radio wählen", "Bekannten Befehl wählen", "Ergebnis prüfen"], "Unbekannte Schreibbefehle gehören nicht in den normalen Endnutzerbetrieb."],
  debug: ["Protokoll", "Zeigt nachvollziehbar, welche Anfragen Radios und BASSWIESN austauschen.", ["Zeitpunkt merken", "Aktion ausführen", "Passenden Eintrag öffnen"], "Sicherungen bleiben beim Leeren der Laufzeitprotokolle erhalten."],
  telemetry: ["Diagnose", "Liest Gerätewerte und sammelt strukturierte Fehlerdaten.", ["Radio wählen", "Messwert wählen", "Ergebnis vergleichen"], "Lesende Diagnosen verändern das Radio nicht."],
  lab: ["Labor", "Enthält Diagnose- und Forschungsfunktionen, die nicht in die tägliche Bedienung gehören.", ["Erklärung lesen", "Sicherung prüfen", "Exakte Bestätigung eingeben"], "Manuelle Schreibaktionen können das Radio vorübergehend vom Netzwerk trennen."],
  about: ["Über BASSWIESN", "Erklärt Motivation, Projektstatus und die technischen Grundsätze hinter BASSWIESN.", ["Motivation lesen", "Unterstützte Radios prüfen", "Research nachvollziehen"], "Die angezeigte Version kommt aus der laufenden BASSWIESN-Backend-Konfiguration."],
};

function describeHelpField(label, control) {
  const name = (control?.name || control?.id || "").toLowerCase();
  if (/device|radio|master/.test(name)) return "Wähle das Radio, auf das diese Aktion angewendet wird.";
  if (/station|source/.test(name)) return "Wähle den Sender oder die Audioquelle.";
  if (/volume/.test(name)) return "Gewünschte Lautstärke von 0 bis 100.";
  if (/name/.test(name)) return "Ein kurzer, verständlicher Anzeigename.";
  if (/host|ip/.test(name)) return "LAN-Adresse, unter der das Ziel im Heimnetz erreichbar ist.";
  if (/confirmation/.test(name)) return "Sicherheitsbestätigung für eine ausdrücklich gefährliche Aktion.";
  if (control?.tagName === "SELECT") return "Wähle eine der unterstützten Möglichkeiten aus.";
  if (control?.type === "checkbox") return "Schaltet diese Zusatzoption ein oder aus.";
  return `Trage hier „${label}“ ein. Pflichtfelder sind entsprechend markiert.`;
}

function openPageHelp(view) {
  const key = view.id.replace("view-", "");
  const info = pageHelp[key] || [view.querySelector("h2")?.textContent || "Hilfe", "Diese Seite erklärt ihren Ablauf Schritt für Schritt.", ["Auswählen", "Eingeben", "Ausführen"], "Bei Unsicherheit zuerst den Status aktualisieren."];
  document.getElementById("page-help-title").textContent = info[0];
  document.getElementById("page-help-intro").textContent = info[1];
  document.getElementById("page-help-flow").innerHTML = info[2].map((step, index) => `${index ? "<b>→</b>" : ""}<span>${escapeHtml(step)}</span>`).join("");
  document.getElementById("page-help-tip").innerHTML = `<strong>Gut zu wissen</strong><br>${escapeHtml(info[3])}`;
  const labels = Array.from(view.querySelectorAll("label")).filter((label) => label.offsetParent !== null).slice(0, 24);
  document.getElementById("page-help-fields").innerHTML = labels.length ? labels.map((label, index) => {
    const control = label.querySelector("input, select, textarea");
    const title = Array.from(label.childNodes).find((node) => node.nodeType === Node.TEXT_NODE)?.textContent.trim() || control?.name || "Feld";
    label.dataset.helpTarget = `${key}-${index}`;
    return `<button class="help-field-card" data-help-target="${key}-${index}" type="button"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(describeHelpField(title, control))}</small></button>`;
  }).join("") : `<div class="empty">Auf dieser Seite sind keine Eingabefelder nötig.</div>`;
  const drawer = document.getElementById("page-help");
  drawer.classList.add("is-open"); drawer.setAttribute("aria-hidden", "false"); document.getElementById("page-help-scrim").hidden = false;
}

function closePageHelp() {
  document.getElementById("page-help").classList.remove("is-open");
  document.getElementById("page-help").setAttribute("aria-hidden", "true");
  document.getElementById("page-help-scrim").hidden = true;
}

function initPageHelp() {
  document.querySelectorAll(".view").forEach((view) => {
    const head = view.querySelector(":scope > .page-head");
    if (!head || head.querySelector(".page-help-button")) return;
    const button = document.createElement("button"); button.type = "button"; button.className = "page-help-button"; button.textContent = "?"; button.setAttribute("aria-label", "Diese Seite erklären");
    button.addEventListener("click", () => openPageHelp(view)); head.append(button);
  });
  document.getElementById("page-help-close")?.addEventListener("click", closePageHelp);
  document.getElementById("page-help-scrim")?.addEventListener("click", closePageHelp);
  document.getElementById("page-help-fields")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-help-target]"); if (!button) return;
    const target = document.querySelector(`[data-help-target="${button.dataset.helpTarget}"]`); if (!target) return;
    closePageHelp(); target.scrollIntoView({ behavior: "smooth", block: "center" }); target.classList.add("help-highlight"); setTimeout(() => target.classList.remove("help-highlight"), 2400);
  });
  if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    document.getElementById("page-help-fields")?.addEventListener("mouseover", (event) => {
      const button = event.target.closest("[data-help-target]");
      if (!button) return;
      document.querySelector(`[data-help-target="${button.dataset.helpTarget}"]:not(.help-field-card)`)?.classList.add("help-hover-link");
    });
    document.getElementById("page-help-fields")?.addEventListener("mouseout", (event) => {
      const button = event.target.closest("[data-help-target]");
      if (!button) return;
      document.querySelector(`[data-help-target="${button.dataset.helpTarget}"]:not(.help-field-card)`)?.classList.remove("help-hover-link");
    });
  }
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closePageHelp(); });
}

function simplifyActionForms() {
  // End-user pages execute the selected action directly.  Backend guards,
  // explicit danger confirmations and verification remain active internally.
  document.querySelectorAll('input[name="dry_run"]').forEach((input) => {
    input.checked = false;
    const label = input.closest("label");
    if (label) label.hidden = true;
  });
  document.querySelectorAll('input[name="memory_checked"]').forEach((input) => {
    input.checked = true;
    const label = input.closest("label");
    if (label) label.hidden = true;
  });
}

ensureIntegratedPanels();
initPageHelp();
simplifyActionForms();
markRiskPanels();
document.getElementById("local-test-refresh")?.addEventListener("click", () => refreshLocalTestOverview().catch((error) => showApiError(error, "Local-Test-Status konnte nicht geladen werden")));
document.getElementById("local-health-run")?.addEventListener("click", () => runLocalHealthcheck().catch((error) => showApiError(error, "Healthcheck fehlgeschlagen")));
document.getElementById("local-ssdp-test")?.addEventListener("click", () => runLocalSsdpTest().catch((error) => showApiError(error, "SSDP-Test fehlgeschlagen")));
document.getElementById("local-diagnostic-preview")?.addEventListener("click", () => previewLocalDiagnostic().catch((error) => showApiError(error, "Diagnosevorschau fehlgeschlagen")));
document.getElementById("local-backup-create")?.addEventListener("click", () => createLocalBackup().catch((error) => showApiError(error, "Backup konnte nicht erstellt werden")));
document.getElementById("events-refresh")?.addEventListener("click", () => refreshEventsPanel().catch((error) => showApiError(error, "Events konnten nicht geladen werden")));
document.getElementById("media-library-status-refresh")?.addEventListener("click", () => refreshMediaLibraryPanel().catch((error) => showApiError(error, "Medienstatus konnte nicht geladen werden")));
document.getElementById("research-health-refresh")?.addEventListener("click", () => loadResearchHealth().catch((error) => showApiError(error, "Status konnte nicht geladen werden")));
document.getElementById("health-device-select")?.addEventListener("change", () => loadResearchHealth().catch((error) => showApiError(error, "Status konnte nicht geladen werden")));
document.getElementById("airplay-readonly-probe")?.addEventListener("click", () => probeAirplayReadiness());
document.getElementById("metadata-artwork")?.addEventListener("error", (event) => {
  if (!event.target.src.endsWith("/static/bmx-icons/orion/monochrome.svg")) event.target.src = "/static/bmx-icons/orion/monochrome.svg";
});
document.getElementById("live-metadata-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const deviceId = document.getElementById("health-device-select")?.value || "";
  const status = document.getElementById("live-metadata-status");
  if (!deviceId) {
    showToast("Bitte zuerst ein Radio wählen.", "error");
    return;
  }
  const values = new FormData(form);
  setFormBusy(form, true, "Metadaten werden zusammengeführt …");
  try {
    const result = await putJson(`/api/devices/${encodeURIComponent(deviceId)}/metadata/live`, {
      track: values.get("track") || null,
      artist: values.get("artist") || null,
      album: values.get("album") || null,
      imageUrl: values.get("imageUrl") || null,
    });
    if (status) status.textContent = "Angenommen · Zusammenführung ca. 2 s · kein Radio-Write und kein Playback-Neustart.";
    await new Promise((resolve) => window.setTimeout(resolve, 2300));
    await loadResearchHealth();
    if (status) status.textContent = `Aktualisiert · ${formatObservedAt(state.researchHealth.metadata?.observed_at)} · Playback-Aktion ${result.playback_action}.`;
    showToast("Live-Metadaten ohne Sourcewechsel aktualisiert.");
  } catch (error) {
    if (status) status.textContent = error?.message || String(error);
    showApiError(error, "Live-Metadaten konnten nicht aktualisiert werden");
  } finally {
    setFormBusy(form, false);
  }
});
document.getElementById("clock-metadata-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const deviceId = document.getElementById("health-device-select")?.value || "";
  if (!deviceId) {
    showToast("Bitte zuerst ein Radio wählen.", "error");
    return;
  }
  setFormBusy(form, true);
  try {
    state.researchHealth.clock = await putJson(`/api/devices/${encodeURIComponent(deviceId)}/metadata/clock`, {
      enabled: Boolean(document.getElementById("clock-metadata-enabled")?.checked),
      mode: document.getElementById("clock-metadata-mode")?.value || "MISSING_TITLE",
      interval_seconds: Number(document.getElementById("clock-metadata-interval")?.value || 60),
    });
    const status = document.getElementById("clock-metadata-status");
    if (status) status.textContent = `Gespeichert · ${state.researchHealth.clock.enabled ? "aktiv" : "aus"} · ${state.researchHealth.clock.interval_seconds}s · Hardwarevalidierung offen.`;
    renderResearchHealth();
  } catch (error) {
    showApiError(error, "LAB-Uhreinstellung konnte nicht gespeichert werden");
  } finally {
    setFormBusy(form, false);
  }
});
loadAll().then(() => {
  markRiskPanels();
  maybeShowFirstRunWarning();
  refreshLocalTestOverview().catch(() => {});
  refreshMediaLibraryPanel().catch(() => {});
});
window.setInterval(() => {
  if (!document.hidden) refreshServiceStatus();
}, 30_000);
