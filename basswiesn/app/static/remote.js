/* Standalone remote: explicit actions, readable state, opt-in diagnostics. */
const shell = document.querySelector(".remote-shell");
const deviceId = shell.dataset.deviceId;
const byId = (id) => document.getElementById(id);
const volume = byId("remote-volume");
const safeStartEnabled = byId("remote-safe-start-enabled");
const safeStartField = byId("remote-safe-start-field");
const safeStartVolume = byId("remote-safe-start-volume");
const messages = {
  en: {
    home: "Home", loading: "Loading…", refresh: "Refresh", safeStart: "Safe start volume",
    volumeBefore: "Volume before playback", safeHelp: "Off keeps the radio's current volume. On verifies this value before playback.",
    previous: "Previous", next: "Next", playPause: "Play / pause", stop: "Stop", mute: "Mute", power: "Power",
    playStation: "Play station", multiroom: "Play radios together", members: "Choose additional radios",
    multiroomHelp: "Share this radio's current playback. BASSWIESN sends no volume commands; the radio firmware may adjust volumes when grouping.",
    startGroup: "Start group", cancel: "Cancel", details: "Technical details / XML", master: "Main radio",
    empty: "No additional online, unprotected radios are available.", noStations: "No stations saved",
    noState: "Playback state unavailable", failed: "Action or readback failed. See technical details.",
    sent: "Command sent; current state has been read back.", groupOK: "Group confirmed by every selected radio.",
    volumeWarning: "The firmware changed one or more volumes. Review technical details before continuing.",
    confirmGroup: "Share the current playback with these radios?", unverified: "Group not confirmed by every selected radio.",
    playing: "Playing", paused: "Paused", stopped: "Stopped", buffering: "Buffering", standby: "Standby",
    unknown: "Unknown", invalid: "Source unavailable", volume: "Volume", station: "Station", preset: "Preset",
    clock: "Time in playback title",
    clockHelp: "For BASSWIESN internet radio, add time to the title line. Other sources and the radio's native standby clock are unchanged.",
    clockSaved: "Display preference saved. It takes effect with the next metadata update; no playback restart is needed.",
    clockUnavailable: "The display preference could not be loaded. Playback controls remain available.",
    reconnect: "Reconnect live radio after stream end",
    reconnectHelp: "Opt in, then start a station. Reconnect only after confirmed unexpected stream end, never from standby or in a group. No volume commands.",
    reconnectSaved: "Reconnect preference saved. Applies to the next manually started internet-radio session.",
    reconnectUnavailable: "Reconnect preference unavailable. Automatic reconnect has not been enabled here.",
    groupVolumes: "Group volume readback", unchanged: "unchanged", changedByFirmware: "changed by firmware",
  },
  de: {
    home: "Startseite", loading: "Wird geladen…", refresh: "Aktualisieren", safeStart: "Sichere Startlautstärke",
    volumeBefore: "Lautstärke vor der Wiedergabe", safeHelp: "Aus lässt die aktuelle Radiolautstärke unverändert. Ein setzt und prüft diesen Wert vor der Wiedergabe.",
    previous: "Zurück", next: "Weiter", playPause: "Wiedergabe / Pause", stop: "Stopp", mute: "Stumm", power: "Ein / Standby",
    playStation: "Sender abspielen", multiroom: "Radios gemeinsam abspielen", members: "Weitere Radios auswählen",
    multiroomHelp: "Die aktuelle Wiedergabe dieses Radios teilen. BASSWIESN sendet keine Lautstärkebefehle; die Radio-Firmware kann beim Gruppieren Lautstärken anpassen.",
    startGroup: "Gruppe starten", cancel: "Abbrechen", details: "Technische Details / XML", master: "Hauptradio",
    empty: "Keine weiteren erreichbaren, ungeschützten Radios verfügbar.", noStations: "Keine Sender gespeichert",
    noState: "Wiedergabestatus nicht verfügbar", failed: "Aktion oder Rückprüfung fehlgeschlagen. Siehe technische Details.",
    sent: "Befehl gesendet; aktueller Zustand zurückgelesen.", groupOK: "Gruppe von allen ausgewählten Radios bestätigt.",
    volumeWarning: "Die Firmware hat mindestens eine Lautstärke verändert. Vor dem Fortfahren technische Details prüfen.",
    confirmGroup: "Die aktuelle Wiedergabe mit diesen Radios teilen?", unverified: "Gruppe nicht von allen ausgewählten Radios bestätigt.",
    playing: "Wiedergabe läuft", paused: "Pausiert", stopped: "Gestoppt", buffering: "Wird gepuffert", standby: "Standby",
    unknown: "Unbekannt", invalid: "Quelle nicht verfügbar", volume: "Lautstärke", station: "Sender", preset: "Preset",
    clock: "Uhrzeit in der Titelzeile",
    clockHelp: "Bei BASSWIESN-Internetradio die Uhrzeit in der Titelzeile ergänzen. Andere Quellen und die eingebaute Standby-Uhr bleiben unverändert.",
    clockSaved: "Anzeigeeinstellung gespeichert. Sie gilt ab der nächsten Metadatenaktualisierung; kein Wiedergabeneustart nötig.",
    clockUnavailable: "Die Anzeigeeinstellung konnte nicht geladen werden. Die Fernbedienung bleibt verfügbar.",
    reconnect: "Live-Radio nach Streamende neu verbinden",
    reconnectHelp: "Einschalten, dann einen Sender starten. Neuverbindung nur nach bestätigtem unerwartetem Streamende, nie aus Standby oder in einer Gruppe. Keine Lautstärkebefehle.",
    reconnectSaved: "Neuverbindungseinstellung gespeichert. Sie gilt für die nächste manuell gestartete Internetradio-Sitzung.",
    reconnectUnavailable: "Neuverbindungseinstellung nicht verfügbar. Automatische Neuverbindung wurde hier nicht eingeschaltet.",
    groupVolumes: "Zurückgelesene Gruppenlautstärken", unchanged: "unverändert", changedByFirmware: "von der Firmware geändert",
  },
};
let language = "en";
let devices = [];
let busy = false;
let volumeKnown = false;
let clockPreference = null;
let reconnectPreference = null;
const t = (key) => messages[language][key];
const path = (suffix) => `/api/devices/${encodeURIComponent(deviceId)}/${suffix}`;
function details(value) { byId("remote-output").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function message(key) { byId("remote-message").textContent = key ? t(key) : ""; }
function localize() {
  document.documentElement.lang = language;
  document.querySelectorAll("[data-remote-text]").forEach((node) => { node.textContent = t(node.dataset.remoteText); });
  volume.setAttribute("aria-label", t("volume"));
  byId("remote-station").setAttribute("aria-label", t("station"));
  byId("remote-volume-readback").setAttribute("aria-label", t("groupVolumes"));
}
async function request(url, body, method = "POST") {
  const response = await fetch(url, body === undefined ? { cache: "no-store" } : {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({ error: "invalid_server_response" }));
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.detail = result;
    throw error;
  }
  return result;
}
function setVolumeLabel() { byId("remote-volume-label").textContent = volumeKnown ? `${volume.value}%` : "—"; }
function updateControls() {
  shell.querySelectorAll("button").forEach((node) => { node.disabled = busy; });
  volume.disabled = busy || !volumeKnown;
  byId("remote-clock").disabled = busy || clockPreference === null;
  byId("remote-reconnect").disabled = busy || reconnectPreference === null;
  shell.querySelectorAll("[data-volume-step]").forEach((node) => { node.disabled = busy || !volumeKnown; });
  byId("remote-multiroom-start").disabled = busy || !byId("remote-members").querySelector("input:checked");
}
async function readState() {
  try {
    const state = await request(path("remote-state"));
    if (state.verified !== true || !Number.isInteger(state.volume) || state.volume < 0 || state.volume > 100) throw new Error("invalid_radio_readback");
    const now = state.now_playing || {};
    const station = now.stationName || now.itemName || "";
    byId("remote-now").textContent = station || state.source || t("noState");
    byId("remote-track").textContent = [now.artist, now.track !== station ? now.track : ""].filter(Boolean).join(" — ");
    const states = { PLAY_STATE: "playing", PAUSE_STATE: "paused", STOP_STATE: "stopped", BUFFERING_STATE: "buffering" };
    byId("remote-play-status").textContent = t(state.source === "STANDBY" ? "standby" : state.source === "INVALID_SOURCE" ? "invalid" : states[state.play_status] || "unknown");
    volume.value = state.volume;
    volumeKnown = true;
    setVolumeLabel();
    return state;
  } catch (error) {
    volumeKnown = false;
    setVolumeLabel();
    byId("remote-now").textContent = t("noState");
    byId("remote-track").textContent = "";
    byId("remote-play-status").textContent = "";
    throw error;
  }
}
async function run(action) {
  if (busy) return;
  busy = true;
  updateControls();
  message("loading");
  try { await action(); }
  catch (error) { message("failed"); details(error.detail || String(error)); }
  finally { busy = false; updateControls(); }
}
function safeVolumePayload() {
  if (!safeStartEnabled.checked) return {};
  const value = Number(safeStartVolume.value);
  if (!Number.isInteger(value) || value < 0 || value > 5) throw new Error("safe_volume_must_be_0_to_5");
  return { safe_volume: value };
}
async function command(url, body) {
  const result = await request(url, body);
  const state = await readState();
  details({ result, state });
  message("sent");
}
async function sendKey(key) { await command(path("key"), { key, ...safeVolumePayload() }); }
async function setVolume(value) {
  await command(path("settings/volume"), { value: Math.max(0, Math.min(100, value)), dry_run: false });
}
function renderMembers() {
  const fieldset = byId("remote-members");
  fieldset.replaceChildren();
  const legend = document.createElement("legend");
  legend.textContent = t("members");
  fieldset.append(legend);
  const members = devices.filter((item) => item.device_id !== deviceId && item.reachable === true && !item.protected);
  for (const device of members) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = device.device_id;
    input.addEventListener("change", updateControls);
    label.append(input, document.createTextNode(device.name || device.device_id));
    fieldset.append(label);
  }
  if (!members.length) {
    const empty = document.createElement("p");
    empty.textContent = t("empty");
    fieldset.append(empty);
  }
  const master = devices.find((item) => item.device_id === deviceId);
  byId("remote-master").textContent = `${t("master")}: ${master?.name || deviceId}`;
}
function showGroupVolumes(result) {
  const list = byId("remote-volume-readback");
  list.replaceChildren();
  for (const item of result.volume_observations || []) {
    const row = document.createElement("li");
    const name = devices.find((device) => device.device_id === item.device_id)?.name || item.device_id;
    const known = Number.isInteger(item.before) && Number.isInteger(item.after);
    const values = known ? `${item.before}% → ${item.after}% (${t(item.before === item.after ? "unchanged" : "changedByFirmware")})` : t("unknown");
    row.textContent = `${name}: ${values}`;
    list.append(row);
  }
  list.hidden = !list.children.length;
}
document.querySelectorAll("[data-key]").forEach((button) => button.addEventListener("click", () => run(() => sendKey(button.dataset.key))));
document.querySelectorAll("[data-volume-step]").forEach((button) => button.addEventListener("click", () => run(() => setVolume(Number(volume.value) + Number(button.dataset.volumeStep)))));
volume.addEventListener("input", setVolumeLabel);
volume.addEventListener("change", () => run(() => setVolume(Number(volume.value))));
byId("remote-presets").addEventListener("click", (event) => {
  const button = event.target.closest("[data-preset]");
  if (button) run(() => sendKey(`PRESET_${button.dataset.preset}`));
});
byId("remote-play-station").addEventListener("click", () => run(async () => {
  const stationId = byId("remote-station").value;
  if (!stationId) { message("noStations"); return; }
  await command(path(`stations/${encodeURIComponent(stationId)}/play`), { dry_run: false, ...safeVolumePayload() });
}));
safeStartEnabled.addEventListener("change", () => {
  safeStartField.hidden = !safeStartEnabled.checked;
  try { localStorage.setItem(`basswiesn_remote_safe_start_${deviceId}`, String(safeStartEnabled.checked)); } catch { /* Storage can be unavailable. */ }
});
byId("remote-refresh").addEventListener("click", () => run(async () => { details(await readState()); message(""); }));
function showClockPreference(value) {
  if (typeof value.enabled !== "boolean" || !["OFF", "MISSING_TITLE", "APPEND"].includes(value.mode)) throw new Error("invalid_clock_preference");
  clockPreference = value;
  byId("remote-clock").checked = value.enabled && value.mode !== "OFF";
  byId("remote-clock-help").textContent = `${t("clockHelp")} ${value.timezone || ""}`.trim();
}
function showReconnectPreference(value) {
  if (typeof value.enabled !== "boolean") throw new Error("invalid_reconnect_preference");
  reconnectPreference = value;
  byId("remote-reconnect").checked = value.enabled;
}
byId("remote-reconnect").addEventListener("change", () => run(async () => {
  const enabled = byId("remote-reconnect").checked;
  try {
    await request(path("live-reconnect"), { enabled }, "PUT");
    const saved = await request(path("live-reconnect"));
    showReconnectPreference(saved);
    if (saved.enabled !== enabled) throw new Error("reconnect_preference_readback_mismatch");
    message("reconnectSaved");
  } catch (error) {
    try { showReconnectPreference(await request(path("live-reconnect"))); }
    catch { reconnectPreference = null; byId("remote-reconnect").checked = false; }
    throw error;
  }
}));
byId("remote-clock").addEventListener("change", () => run(async () => {
  const enabled = byId("remote-clock").checked;
  try {
    await request(path("metadata/clock"), { enabled, mode: "APPEND", interval_seconds: 60 }, "PUT");
    const saved = await request(path("metadata/clock"));
    showClockPreference(saved);
    if (saved.enabled !== enabled || saved.mode !== "APPEND") throw new Error("clock_preference_readback_mismatch");
    message("clockSaved");
  } catch (error) {
    // The write may have reached the server even if its response was lost.
    // Show readback if available; otherwise disable the indeterminate control.
    try { showClockPreference(await request(path("metadata/clock"))); }
    catch { clockPreference = null; byId("remote-clock").checked = false; }
    throw error;
  }
}));
byId("remote-multiroom-open").addEventListener("click", () => run(async () => {
  devices = await request("/api/devices"); // DB list only; no discovery or other-radio probes
  showGroupVolumes({});
  renderMembers();
  byId("remote-multiroom").hidden = false;
  message("");
}));
byId("remote-multiroom-cancel").addEventListener("click", () => { byId("remote-multiroom").hidden = true; });
byId("remote-multiroom-start").addEventListener("click", () => run(async () => {
  const ids = [...byId("remote-members").querySelectorAll("input:checked")].map((input) => input.value);
  if (!ids.length) { message("empty"); return; }
  const names = ids.map((id) => devices.find((item) => item.device_id === id)?.name || id);
  if (!window.confirm(`${t("confirmGroup")}\n${byId("remote-master").textContent}\n${names.join("\n")}`)) { message(""); return; }
  const payload = { master_device_id: deviceId, member_device_ids: ids, preserve_volumes: true, set_start_volumes: false };
  const preview = await request("/api/multiroom/preview", payload);
  if (preview.blocked) throw new Error("protected_device");
  // This endpoint performs a real identity / before-state backup preflight.
  const result = await request("/api/multiroom/remote-start", payload);
  details(result);
  showGroupVolumes(result);
  const expected = new Set([deviceId, ...ids]);
  const verified = Array.isArray(result.verification) && result.verification.length === expected.size && result.verification.every((item) => item.ok === true && expected.delete(item.device_id)) && expected.size === 0;
  if (!verified) { message("unverified"); return; }
  message(result.volume_warnings?.length ? "volumeWarning" : "groupOK");
}));

run(async () => {
  const settings = await request("/api/system/settings");
  language = settings.web_language === "de" ? "de" : "en";
  localize();
  byId("remote-version").textContent = `BASSWIESN Remote · v${shell.dataset.version}`;
  try { safeStartEnabled.checked = localStorage.getItem(`basswiesn_remote_safe_start_${deviceId}`) === "true"; } catch { safeStartEnabled.checked = false; }
  safeStartField.hidden = !safeStartEnabled.checked;
  safeStartVolume.value = Math.max(0, Math.min(5, Number(settings.safe_startup_volume) || 1));
  const [deviceRows, stations, presets] = await Promise.all([request("/api/devices"), request("/api/stations"), request(`/api/presets/${encodeURIComponent(deviceId)}`)]);
  devices = deviceRows;
  byId("remote-title").textContent = devices.find((item) => item.device_id === deviceId)?.name || deviceId;
  const select = byId("remote-station");
  for (const station of stations) select.add(new Option(station.name, String(station.id)));
  if (!stations.length) select.add(new Option(t("noStations"), ""));
  for (const slot of [1, 2, 3, 4, 5, 6]) {
    const button = document.createElement("button");
    button.dataset.preset = slot;
    button.textContent = presets.find((item) => Number(item.button) === slot)?.station_name || `${t("preset")} ${slot}`;
    byId("remote-presets").append(button);
  }
  details(await readState());
  message("");
  try { showClockPreference(await request(path("metadata/clock"))); }
  catch (error) { message("clockUnavailable"); details(error.detail || String(error)); }
  try { showReconnectPreference(await request(path("live-reconnect"))); }
  catch (error) { message("reconnectUnavailable"); details(error.detail || String(error)); }
});
