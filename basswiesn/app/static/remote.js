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
    reboots: "Radio restarts",
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
    displayHeading: "Radio display", displayFields: "Fields and order",
    metadataNotRequested: "Song collection is off for this layout.",
    metadataNotObserved: "No stored song observation yet. Data is collected during BASSWIESN internet-radio playback.",
    metadataStale: "Stored station data has expired. Old song titles are not reused.",
    metadataUnavailable: "The last stream check returned no usable metadata. Playback can still work.",
    metadataSong: "Last station observation: artist and title available. This is not a live display check.",
    metadataInformation: "Last station observation: station information only, no separate artist/title.",
    metadataNoSong: "Last station observation: no song information. Missing fields are skipped.",
    displayHelp: "Choose the fields and their order on the radio. Missing station data is skipped; it is never invented.",
    displayStation: "Station name", displayArtist: "Artist", displayTitle: "Song title", displayClock: "Time", displayOther: "Other station information",
    displayUp: "Move up", displayDown: "Move down", displaySave: "Save display",
    displayPreview: "Example preview (not live data)", displayEmpty: "No additional title text. The radio may still show its native station header.",
    exampleStation: "Example Station", exampleArtist: "Example Artist", exampleTitle: "Example Song", exampleOther: "Station information",
    clockSaved: "Display preference saved. It takes effect with the next metadata update; no playback restart is needed.",
    clockUnavailable: "The display preference could not be loaded. Playback controls remain available.",
    metadataHelp: "For BASSWIESN internet radio only. Selected fields share the title line; the radio controls its native station header, font and scrolling. Long fields are shortened. No source, preset or volume changes. Stream checks at most once a minute per station.",
    reconnect: "Reconnect live radio after stream end",
    reconnectHelp: "Opt in, then start a station. Reconnect only after confirmed unexpected stream end, never from standby or in a group. No volume commands.",
    reconnectSaved: "Reconnect preference saved. Applies to the next manually started internet-radio session.",
    reconnectUnavailable: "Reconnect preference unavailable. Automatic reconnect has not been enabled here.",
    groupVolumes: "Group volume readback", unchanged: "unchanged", changedByFirmware: "changed by firmware",
  },
  de: {
    reboots: "Radios neu starten",
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
    displayHeading: "Radioanzeige", displayFields: "Angaben und Reihenfolge",
    metadataNotRequested: "Für diese Anzeige ist das Sammeln von Songdaten aus.",
    metadataNotObserved: "Noch keine gespeicherte Songbeobachtung. Daten werden während der BASSWIESN-Internetradio-Wiedergabe gesammelt.",
    metadataStale: "Die gespeicherten Senderdaten sind veraltet. Alte Songtitel werden nicht wiederverwendet.",
    metadataUnavailable: "Die letzte Streamprüfung lieferte keine nutzbaren Metadaten. Die Wiedergabe kann trotzdem funktionieren.",
    metadataSong: "Letzte Senderbeobachtung: Interpret und Titel vorhanden. Das ist keine Live-Prüfung des Displays.",
    metadataInformation: "Letzte Senderbeobachtung: nur Senderinformationen, kein getrennter Interpret und Titel.",
    metadataNoSong: "Letzte Senderbeobachtung: keine Songdaten. Fehlende Angaben werden ausgelassen.",
    displayHelp: "Wähle die Angaben und ihre Reihenfolge auf dem Radio. Fehlende Senderdaten werden ausgelassen und nicht erfunden.",
    displayStation: "Sendername", displayArtist: "Interpret", displayTitle: "Songtitel", displayClock: "Uhrzeit", displayOther: "Sonstige Senderinformationen",
    displayUp: "Nach oben", displayDown: "Nach unten", displaySave: "Anzeige speichern",
    displayPreview: "Beispielvorschau (keine Live-Daten)", displayEmpty: "Kein zusätzlicher Titeltext. Die eingebaute Senderzeile kann weiterhin sichtbar sein.",
    exampleStation: "Beispielsender", exampleArtist: "Beispielinterpret", exampleTitle: "Beispieltitel", exampleOther: "Senderhinweis",
    clockSaved: "Anzeigeeinstellung gespeichert. Sie gilt ab der nächsten Metadatenaktualisierung; kein Wiedergabeneustart nötig.",
    clockUnavailable: "Die Anzeigeeinstellung konnte nicht geladen werden. Die Fernbedienung bleibt verfügbar.",
    metadataHelp: "Nur für BASSWIESN-Internetradio. Ausgewählte Angaben stehen gemeinsam in der Titelzeile; die eingebaute Senderzeile, Schrift und Laufschrift bestimmt das Radio. Lange Angaben werden gekürzt. Keine Sender-, Preset- oder Lautstärkeänderung. Streamprüfung höchstens einmal pro Minute und Sender.",
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
let reconnectPreference = null;
let displayPreference = null;
let displayDraft = null;
const displayFieldOrder = ["station", "artist", "title", "clock", "other"];
const displayLabels = { station: "displayStation", artist: "displayArtist", title: "displayTitle", clock: "displayClock", other: "displayOther" };
function displayPayload() {
  return { mode: "CUSTOM", fields: displayDraft.field_order.filter((field) => displayDraft.fields.includes(field)), field_order: [...displayDraft.field_order] };
}
const t = (key) => window.BasswiesnI18n.scoped(messages)[key];
const path = (suffix) => `/api/devices/${encodeURIComponent(deviceId)}/${suffix}`;
function details(value) { byId("remote-output").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function message(key) { byId("remote-message").textContent = key ? t(key) : ""; }
function localize() {
  byId("remote-reboots").href = `/reboots?device=${encodeURIComponent(deviceId)}`;
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
  byId("remote-reconnect").disabled = busy || reconnectPreference === null;
  byId("remote-display-fields").disabled = busy || displayDraft === null;
  byId("remote-display-save").disabled = busy || displayDraft === null || displayPreference === null || (
    JSON.stringify(displayPayload()) === JSON.stringify({ mode: "CUSTOM", fields: displayPreference.fields, field_order: displayPreference.field_order }));
  byId("remote-display-rows").querySelectorAll("[data-move]").forEach((node) => {
    const index = displayDraft?.field_order.indexOf(node.dataset.field);
    node.disabled = busy || displayDraft === null || (node.dataset.move === "up" ? index === 0 : index === 4);
  });
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
byId("remote-refresh").addEventListener("click", () => run(async () => {
  details(await readState()); message("");
  // Do not replace unsaved layout choices while refreshing cached evidence.
  try {
    const value = await request(path("metadata/display"));
    showDisplayObservation(value.observation);
  } catch { byId("remote-display-observation").textContent = t("clockUnavailable"); }
}));
function showReconnectPreference(value) {
  if (typeof value.enabled !== "boolean") throw new Error("invalid_reconnect_preference");
  reconnectPreference = value;
  byId("remote-reconnect").checked = value.enabled;
}
function showDisplayPreference(value) {
  if (!["STATION", "TRACK_ARTIST", "CUSTOM"].includes(value.mode)
      || !Array.isArray(value.fields) || !Array.isArray(value.field_order)
      || new Set(value.fields).size !== value.fields.length || value.fields.some((field) => !displayFieldOrder.includes(field))
      || value.field_order.length !== 5 || new Set(value.field_order).size !== 5
      || value.field_order.some((field) => !displayFieldOrder.includes(field))) throw new Error("invalid_display_preference");
  displayPreference = value;
  displayDraft = { fields: [...value.fields], field_order: [...value.field_order] };
  showDisplayObservation(value.observation);
  renderDisplayFields();
}

function showDisplayObservation(observation) {
  const observationKeys = { NOT_REQUESTED: "metadataNotRequested", NOT_OBSERVED: "metadataNotObserved",
    STALE: "metadataStale", UNAVAILABLE: "metadataUnavailable", SONG_AVAILABLE: "metadataSong",
    INFORMATION_ONLY: "metadataInformation", NO_SONG: "metadataNoSong" };
  byId("remote-display-observation").textContent = t(observationKeys[observation?.status] || "metadataNotObserved");
}

function showDisplayPreview() {
  const values = { station: t("exampleStation"), artist: t("exampleArtist"), title: t("exampleTitle"), clock: "20:15", other: t("exampleOther") };
  let preview = "";
  for (const field of displayDraft.field_order) {
    if (!displayDraft.fields.includes(field)) continue;
    preview += (preview ? (field === "clock" ? " " : " — ") : "") + values[field];
  }
  byId("remote-display-preview").textContent = preview || t("displayEmpty");
}
function renderDisplayFields() {
  const rows = byId("remote-display-rows");
  rows.replaceChildren();
  for (const field of displayDraft.field_order) {
    const row = document.createElement("div");
    row.className = "remote-display-row";
    row.dataset.field = field;
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = `remote-display-${field}`;
    input.checked = displayDraft.fields.includes(field);
    input.addEventListener("change", () => {
      displayDraft.fields = displayDraft.field_order.filter((item) => item === field ? input.checked : displayDraft.fields.includes(item));
      showDisplayPreview(); updateControls(); message("");
    });
    label.append(input, document.createTextNode(t(displayLabels[field])));
    row.append(label);
    for (const direction of ["up", "down"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.move = direction;
      button.dataset.field = field;
      button.textContent = direction === "up" ? "↑" : "↓";
      button.setAttribute("aria-label", `${t(displayLabels[field])}: ${t(direction === "up" ? "displayUp" : "displayDown")}`);
      button.addEventListener("click", () => {
        const index = displayDraft.field_order.indexOf(field);
        const next = index + (direction === "up" ? -1 : 1);
        if (next < 0 || next >= 5) return;
        [displayDraft.field_order[index], displayDraft.field_order[next]] = [displayDraft.field_order[next], displayDraft.field_order[index]];
        renderDisplayFields(); updateControls(); message("");
        const moved = rows.querySelector(`[data-field="${field}"][data-move="${direction}"]`);
        (moved.disabled ? byId(`remote-display-${field}`) : moved).focus();
      });
      row.append(button);
    }
    rows.append(row);
  }
  showDisplayPreview();
}
byId("remote-display-save").addEventListener("click", () => run(async () => {
  const payload = displayPayload();
  try {
    await request(path("metadata/display"), payload, "PUT");
    const saved = await request(path("metadata/display"));
    showDisplayPreference(saved);
    if (saved.mode !== "CUSTOM" || JSON.stringify(displayPayload()) !== JSON.stringify(payload)) throw new Error("display_preference_readback_mismatch");
    message("clockSaved");
  } catch (error) {
    try { showDisplayPreference(await request(path("metadata/display"))); }
    catch { displayPreference = null; displayDraft = null; }
    throw error;
  }
}));
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

function installDisplayProfiles() {
  const word=key=>window.BasswiesnI18n.word302(key);
  const panel=document.createElement("details");panel.id="remote-display-profiles";panel.dataset.authoredCopy="true";
  const heading=document.createElement("summary");heading.textContent=word("profiles")+" · LAB";
  const note=document.createElement("p");note.textContent=word("profile_note");
  const list=document.createElement("select");list.id="remote-profile-select";list.setAttribute("aria-label",word("saved"));
  const load=document.createElement("button");load.type="button";load.id="remote-profile-load";load.textContent=word("load");
  const copy=document.createElement("button");copy.type="button";copy.id="remote-profile-copy";copy.textContent=word("draft");
  let profiles=[];
  load.addEventListener("click",()=>run(async()=>{
    const value=await request("/api/lab/learning/display-profiles");
    profiles=Array.isArray(value.items)?value.items:[];list.replaceChildren();
    for(const [index,p] of profiles.entries()) list.add(new Option(p.name,String(index)));
    if(!profiles.length) list.add(new Option(word("no_profiles"),""));
    message("");
  }));
  copy.addEventListener("click",()=>{
    if(busy||displayDraft===null||list.value==="") return;
    const p=profiles[Number(list.value)];
    if(!p||!Array.isArray(p.fields)||!Array.isArray(p.field_order)||p.field_order.length!==5
      ||new Set(p.field_order).size!==5||new Set(p.fields).size!==p.fields.length
      ||p.fields.some(k=>!displayFieldOrder.includes(k))||p.field_order.some(k=>!displayFieldOrder.includes(k))) return;
    displayDraft={fields:[...p.fields],field_order:[...p.field_order]};
    renderDisplayFields();updateControls();
    byId("remote-message").textContent=word("copied");
    byId("remote-display-save").focus();
  });
  panel.append(heading,note,load,list,copy);byId("remote-display-panel").append(panel);
}

run(async () => {
  const settings = await request("/api/system/settings");
  language = window.BasswiesnI18n.normalizeLanguage(settings.web_language);
  window.BasswiesnI18n.setLanguage(language);
  localize();
  if(settings.ui_mode === "lab") installDisplayProfiles();
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
  try { showReconnectPreference(await request(path("live-reconnect"))); }
  catch (error) { message("reconnectUnavailable"); details(error.detail || String(error)); }
  try { showDisplayPreference(await request(path("metadata/display"))); }
  catch (error) { message("clockUnavailable"); details(error.detail || String(error)); }
});
