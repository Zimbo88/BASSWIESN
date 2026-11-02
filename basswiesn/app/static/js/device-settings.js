// Explicit settings actions only. Construction never probes a radio.
// Browser snapshots prevent accidental target changes; server identity,
// protection, capability and readback checks remain the security boundary.
export function createDeviceSettingsController({
  getJson, postJson, copy, showToast, showApiError, devices, refreshDevices, refreshPresets,
}) {
  const byId = (id) => document.getElementById(id);
  const form = byId("device-settings-form");
  const select = byId("settings-device-select");
  const output = byId("device-settings-output");
  const reload = byId("reload-device-settings");
  const apply = byId("device-settings-apply");
  const previewBox = byId("device-settings-preset-sync-preview");
  const previewButton = byId("sync-device-presets");
  const confirmButton = byId("device-settings-preset-sync-confirm");
  const memoryCheck = byId("device-settings-memory-check");
  const syncStatus = byId("device-settings-preset-sync-status");
  const clockFields = new Set(["timezoneInfo", "timeFormat", "userOffsetMinute", "brightnessLevel"]);
  const numberFields = new Set(["volume", "bass", "userOffsetMinute", "brightnessLevel"]);
  const booleanFields = new Set(["clockDisplay", "powersaving"]);
  const fields = ["name", "volume", "bass", "clockDisplay", "language", ...clockFields,
    "powersaving", "rebroadcastlatencymode", "station_art_mode"];
  const changed = new Set();
  let generation = 0;
  let readController = null;
  let loaded = null;
  let writing = false;
  let previewSequence = 0;
  let previewBusy = false;
  let preview = null;

  function context() { return { deviceId: select.value, generation }; }
  function isCurrent(ctx) { return ctx.deviceId === select.value && ctx.generation === generation; }
  function valueOf(values, key) { return clockFields.has(key) ? values.clockConfig?.[key] : values[key]; }
  function known(key, value) {
    if (booleanFields.has(key)) return typeof value === "boolean";
    if (numberFields.has(key)) {
      return clockFields.has(key) ? typeof value === "string" && /^-?\d+$/.test(value) : Number.isInteger(value);
    }
    return typeof value === "string" && value.length > 0 && !value.startsWith("ERROR:");
  }
  function dirtyMessage() {
    byId("device-settings-dirty").textContent = writing
      ? copy("Änderung läuft. Das Radio wird anschließend zurückgelesen – bitte nicht erneut senden.",
        "Change in progress. The radio state will be read back — do not resend.")
      : !loaded
      ? copy("Zustand nicht bestätigt. Bitte das ausgewählte Radio neu einlesen.", "State not confirmed. Reload the selected radio.")
      : changed.size
        ? copy("Änderungen noch nicht gespeichert – bitte Anwenden klicken.", "Unsaved changes — select Apply to confirm.")
        : copy("Angezeigt wird der zuletzt gelesene Zustand; vor Änderungen wird erneut geprüft.",
          "Showing the last read state; it is checked again before changes.");
  }
  function controls() {
    const ready = loaded && isCurrent(loaded);
    for (const key of fields) {
      form.elements[key].disabled = Boolean(writing || !ready || !known(key, valueOf(loaded.values, key)));
    }
    apply.disabled = Boolean(writing || !ready);
    apply.classList.toggle("has-pending-changes", changed.size > 0);
    form.setAttribute("aria-busy", String(writing || Boolean(readController)));
    reload.disabled = writing;
    previewButton.disabled = Boolean(writing || previewBusy || !ready);
    confirmButton.hidden = !preview || !isCurrent(preview) || !memoryCheck.checked;
    confirmButton.disabled = writing;
    dirtyMessage();
  }
  function invalidatePreview() {
    previewSequence += 1;
    previewBusy = false;
    preview = null;
    previewBox.hidden = true;
    previewBox.replaceChildren();
    confirmButton.hidden = true;
    syncStatus.textContent = copy("Noch keine bestätigte Vorschau für dieses Ziel.", "No confirmed preview for this target.");
  }
  function renderField(key, value) {
    const element = form.elements[key];
    element.querySelector?.('option[data-radio-unmapped="true"]')?.remove();
    delete element.dataset.unmappedRadioValue;
    const available = known(key, value);
    const next = available ? String(value) : "";
    if (element.tagName === "SELECT" && !Array.from(element.options).some((option) => option.value === next)) {
      const label = key === "language" && next === "id:0"
        ? copy("Sprache noch nicht eingerichtet", "Language not initialized")
        : available ? copy(`Unverändert (Radio: ${next})`, `Unchanged (radio: ${next})`)
        : copy("Unbekannt / nicht gelesen", "Unknown / not read");
      const option = new Option(label, "", true, true);
      option.disabled = true;
      option.dataset.radioUnmapped = "true";
      element.prepend(option);
      if (available) element.dataset.unmappedRadioValue = next;
    } else {
      element.value = next;
    }
    element.setAttribute("aria-invalid", String(!available));
    element.title = available ? "" : copy("Kein belastbarer Readback; dieses Feld ist gesperrt.",
      "No reliable readback; this field is disabled.");
  }
  function renderInfo(result, ctx) {
    const values = result.current;
    const device = devices().find((item) => item.device_id === ctx.deviceId);
    const runtime = result.runtime_state || result.state || {};
    const entries = [
      ["Radio", values.name || device?.name || ctx.deviceId],
      [copy("Quelle", "Source"), result.now_playing?.source || runtime.current_source],
      [copy("Sender", "Station"), result.now_playing?.station || result.now_playing?.item_name || runtime.station_name],
      ["Preset", result.now_playing?.preset || runtime.current_preset],
      ["Playback", result.now_playing?.playback_state || result.now_playing?.play_status || runtime.playback_state],
      [copy("Lautstärke", "Volume"), values.volume],
      ["IP", device?.ip_address || result.radio_ip], ["Firmware", device?.firmware || result.firmware],
    ];
    byId("device-settings-live-info").replaceChildren(...entries.map(([label, value]) => {
      const row = document.createElement("div");
      row.className = "event-row";
      const caption = document.createElement("span");
      const detail = document.createElement("strong");
      caption.textContent = label;
      detail.textContent = value ?? copy("unbekannt", "unknown");
      row.append(caption, detail);
      return row;
    }));
  }
  async function load() {
    generation += 1;
    readController?.abort();
    readController = null;
    loaded = null;
    changed.clear();
    invalidatePreview();
    memoryCheck.checked = false;
    const ctx = context();
    for (const key of fields) renderField(key, undefined);
    byId("volume-value").textContent = "?";
    byId("bass-value").textContent = "?";
    byId("device-settings-live-info").replaceChildren();
    output.textContent = copy("Zustand wird gelesen …", "Reading state …");
    const controller = new AbortController();
    readController = ctx.deviceId ? controller : null;
    controls();
    if (!ctx.deviceId) return;
    try {
      const result = await getJson(`/api/devices/${encodeURIComponent(ctx.deviceId)}/settings?probe=true`, { signal: controller.signal });
      // Abort alone is insufficient: a fulfilled promise may already be queued.
      if (!isCurrent(ctx)) return;
      if (result?.device_id !== ctx.deviceId) throw new Error("IDENTITY_MISMATCH");
      if (!result.current || typeof result.current !== "object" || Array.isArray(result.current)) throw new Error("SETTINGS_RESPONSE_INVALID");
      loaded = { ...ctx, values: structuredClone(result.current) };
      for (const key of fields) renderField(key, valueOf(loaded.values, key));
      byId("volume-value").textContent = known("volume", loaded.values.volume) ? String(loaded.values.volume) : "?";
      byId("bass-value").textContent = known("bass", loaded.values.bass) ? String(loaded.values.bass) : "?";
      output.textContent = JSON.stringify(result, null, 2);
      renderInfo(result, ctx);
    } catch (error) {
      if (!isCurrent(ctx) || error.name === "AbortError") return;
      output.textContent = `${copy("Einlesen fehlgeschlagen; keine Änderung freigegeben.", "Read failed; no change authorized.")}\n${error.message}`;
      showApiError(error, copy("Radiozustand nicht bestätigt", "Radio state not confirmed"));
    } finally {
      if (isCurrent(ctx)) { readController = null; controls(); }
    }
  }

  function onEdit(event) {
    const key = event.target?.name;
    if (!fields.includes(key) || writing || !loaded || !isCurrent(loaded) || event.target.disabled) return;
    changed.add(key);
    invalidatePreview();
    controls();
  }
  function requestFromSnapshot() {
    const values = {}, expected_previous = {};
    for (const key of changed) {
      const element = form.elements[key];
      const before = valueOf(loaded.values, key);
      if (!known(key, before) || !element.value || element.disabled) throw new Error("SETTINGS_READBACK_REQUIRED");
      const value = booleanFields.has(key) ? element.value === "true" : numberFields.has(key) ? Number(element.value) : element.value;
      if (clockFields.has(key)) {
        (values.clockConfig ??= {})[key] = value;
        (expected_previous.clockConfig ??= {})[key] = before;
      } else {
        values[key] = value;
        expected_previous[key] = before;
      }
    }
    return { values, expected_previous };
  }
  async function submit(event) {
    event.preventDefault();
    if (writing || !loaded || !isCurrent(loaded)) return;
    if (!changed.size) return showToast(copy("Keine Änderung nötig.", "No change requested."));
    const ctx = context();
    let payload;
    try { payload = requestFromSnapshot(); }
    catch (error) { showApiError(error, copy("Bitte zuerst neu einlesen.", "Reload the state first.")); return; }
    writing = true;
    invalidatePreview();
    controls();
    output.textContent = copy("Änderung wird geprüft und angewendet. Anschließend wird der echte Radiozustand zurückgelesen.",
      "Validating and applying the change, then reading back the actual radio state.");
    try {
      // Never abort or automatically replay a POST: losing its response says
      // nothing about whether the device changed. Only stale rendering stops.
      const result = await postJson(`/api/devices/${encodeURIComponent(ctx.deviceId)}/settings-apply`, payload);
      if (!isCurrent(ctx)) return;
      if (result?.device_id !== ctx.deviceId || result.status !== "VERIFIED") throw new Error("SETTINGS_RESULT_UNCONFIRMED");
      output.textContent = JSON.stringify(result, null, 2);
      showToast(copy("Änderungen geprüft. Zustand wird neu gelesen.", "Changes verified. Reading state again."));
      if (result.applied && Object.hasOwn(payload.values, "name")) await refreshDevices();
      if (!isCurrent(ctx)) return;
      await load();
      if (result.preset_sync_required && select.value === ctx.deviceId) {
        syncStatus.textContent = copy("Anzeige lokal gespeichert. Preset-Sync benötigt eine eigene Vorschau und Bestätigung.",
          "Display preference saved locally. Preset sync requires its own preview and confirmation.");
      }
    } catch (error) {
      if (!isCurrent(ctx)) return;
      loaded = null;
      const detail = error.payload?.detail;
      const message = copy(
        "Einstellungen nicht vollständig bestätigt. Bereits ausgeführte Änderungen sind möglich. Kein automatischer Wiederholungsversuch; Radiozustand vor einer weiteren Aktion neu einlesen.",
        "Settings not fully confirmed. Changes may already have occurred. No automatic retry; read the radio state before another action.",
      );
      output.textContent = `${message}\n${detail ? JSON.stringify(detail, null, 2) : error.message}`;
      showApiError(error, message);
    } finally { writing = false; controls(); }
  }

  async function previewSync() {
    if (writing || previewBusy || !loaded || !isCurrent(loaded)) return;
    invalidatePreview();
    const ctx = context();
    const sequence = previewSequence;
    const current = () => isCurrent(ctx) && sequence === previewSequence;
    previewBusy = true;
    controls();
    syncStatus.textContent = copy("Read-only-Vorschau wird erstellt …", "Preparing read-only preview …");
    try {
      const result = await postJson(`/api/presets/${encodeURIComponent(ctx.deviceId)}/sync`, { dry_run: true, probe: true, memory_checked: true });
      if (!current()) return;
      if (result.target?.device_id !== ctx.deviceId) throw new Error("IDENTITY_MISMATCH");
      previewBox.hidden = false;
      // Keep diagnostic values inert, including untrusted station/provider text.
      const summary = document.createElement("p");
      summary.textContent = `${result.target.name || ctx.deviceId} · ${copy("Slots", "Slots")}: ${(result.expected_changes || []).map((item) => item.button).join(", ") || "—"}`;
      const details = document.createElement("details");
      const title = document.createElement("summary");
      title.textContent = copy("Technische Vorschau", "Technical preview");
      const raw = document.createElement("pre");
      raw.textContent = JSON.stringify(result, null, 2);
      details.append(title, raw);
      previewBox.replaceChildren(summary, details);
      if (result.protection?.write_allowed === true && result.preview_readback_performed === true
        && result.expected_changes?.length && !result.preview_readback_error) preview = ctx;
      syncStatus.textContent = preview
        ? copy("Vorschau bereit. Backup-Hinweis und Bestätigung sind vor dem Write erforderlich.", "Preview ready. Acknowledge the backup notice and confirm before writing.")
        : copy("Kein Write freigegeben: keine Änderung oder Prüfung unvollständig/blockiert.", "No write authorized: no change or checks incomplete/blocked.");
      output.textContent = JSON.stringify(result, null, 2);
    } catch (error) {
      if (!current()) return;
      syncStatus.textContent = error.message;
      showApiError(error, copy("Preset-Vorschau fehlgeschlagen", "Preset preview failed"));
    } finally { if (current()) { previewBusy = false; controls(); } }
  }
  async function confirmSync() {
    if (writing || !preview || !isCurrent(preview) || !memoryCheck.checked) return;
    const ctx = { ...preview };
    if (!window.confirm(copy(`Presets für ${ctx.deviceId} schreiben und per Readback bestätigen?`,
      `Write presets for ${ctx.deviceId} and verify readback?`))) return;
    writing = true;
    invalidatePreview();
    controls();
    try {
      const result = await postJson(`/api/presets/${encodeURIComponent(ctx.deviceId)}/sync`, { dry_run: false, memory_checked: true });
      if (!isCurrent(ctx)) return;
      if (result.device_id !== ctx.deviceId || result.verified !== true) throw new Error("PRESET_READBACK_UNCONFIRMED");
      syncStatus.textContent = copy("Presets synchronisiert und Readback bestätigt.", "Presets synchronized and readback confirmed.");
      output.textContent = JSON.stringify(result, null, 2);
      await refreshPresets();
    } catch (error) {
      if (!isCurrent(ctx)) return;
      syncStatus.textContent = copy("Synchronisierung nicht bestätigt. Keine automatische Wiederholung; neu einlesen.",
        "Synchronization not confirmed. No automatic retry; read state again.");
      output.textContent = JSON.stringify(error.payload?.detail || { error: error.message }, null, 2);
      showApiError(error, syncStatus.textContent);
    } finally { writing = false; controls(); }
  }

  // app.js registers selection/reload before this dynamic import resolves, so
  // an early click waits for initialization instead of being silently lost.
  form.addEventListener("input", onEdit);
  form.addEventListener("change", onEdit);
  form.addEventListener("submit", submit);
  previewButton.addEventListener("click", previewSync);
  memoryCheck.addEventListener("change", controls);
  confirmButton.addEventListener("click", confirmSync);
  controls();
  return { load };
}
