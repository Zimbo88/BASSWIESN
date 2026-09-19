/* Explicit selections; no network discovery, implicit writes or auto-resume. */
(() => {
  const el = (id) => document.getElementById(id);
  const messages = {
    en: {
      heading: "Radio restarts", home: "Home", radios: "Select radios", all: "Select all reachable radios",
      intro: "Restart selected radios without resetting their configuration. No automatic playback resume. Protected radios are never selectable.",
      offlineHelp: "Reachability shown here is the last known state. Identity and backups are checked before any reboot. An unresponsive radio may still need a manual power cycle.",
      manual: "Restart now", manualHelp: "All selected radios must pass identity and backup checks first. Restart commands are then dispatched together, up to four at a time. Stop active SoundTouch groups yourself first.",
      interrupt: "Allow interruption of active playback on selected radios", preview: "Review restart", confirmHeading: "Confirm the exact radios",
      warning: "Playback stops. Backups are mandatory. This is a restart, not a factory reset.", ack: "I want to restart these radios",
      start: "Restart selected radios", cancel: "Cancel", schedule: "Optional restart schedule",
      scheduleHelp: "Off by default. Only selected radios in standby, without an active group. If any selected radio is active or unavailable, the batch is skipped. Missed times are not caught up.",
      enable: "Enable scheduled restarts", time: "Time", timezone: "Timezone (IANA)", days: "Days", scheduleAck: "I authorize this schedule for the selected radios", save: "Save schedule",
      result: "Restart progress", none: "No restart started.", details: "Technical details", offline: "last known offline", weekdays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
      failed: "Action or verification failed. See technical details; no automatic retry.", saved: "Schedule saved and read back.", off: "Scheduled restarts are off.", on: "Enabled for", loading: "Working…", unknown: "Unconfirmed state",
      volumeChange: "Firmware volume after restart", sourceChange: "Source registry changed during restart; inspect/read back before playback.",
      PREPARING: "Verifying identities and saving backups", DISPATCHING: "Sending restart commands", BACKED_UP: "Backup verified",
      COMMAND_ATTEMPTED: "Restart command attempted", WAITING_FOR_RESTART: "Waiting for radio restart", VERIFIED: "Return and captured settings verified",
      STATE_DIFFERENCE: "Returned with a state difference — review required", REBOOT_NOT_OBSERVED: "Restart was not observed",
      COMMAND_UNCONFIRMED_NO_RETRY: "Command outcome unknown — not retried", VERIFICATION_FAILED: "Return/readback not verified",
      BLOCKED_NO_REBOOT: "Preflight blocked; no radios restarted", PARTIAL_OR_UNCONFIRMED: "Not all radios verified — review required", INTERRUPTED_NO_REPLAY: "Interrupted; commands will not be replayed",
    },
    de: {
      heading: "Radios neu starten", home: "Startseite", radios: "Radios auswählen", all: "Alle erreichbaren Radios auswählen",
      intro: "Ausgewählte Radios neu starten, ohne ihre Konfiguration zurückzusetzen. Keine automatische Wiedergabe danach. Geschützte Radios sind nicht auswählbar.",
      offlineHelp: "Die Erreichbarkeit ist der zuletzt bekannte Stand. Identität und Backups werden vor jedem Neustart geprüft. Ein nicht reagierendes Radio muss eventuell weiterhin manuell vom Strom getrennt werden.",
      manual: "Jetzt neu starten", manualHelp: "Zuerst müssen Identität und Backup aller ausgewählten Radios bestätigt sein. Danach werden die Neustartbefehle gemeinsam gesendet, höchstens vier gleichzeitig. Aktive SoundTouch-Gruppen bitte vorher selbst beenden.",
      interrupt: "Laufende Wiedergabe der ausgewählten Radios darf unterbrochen werden", preview: "Neustart prüfen", confirmHeading: "Diese Radios bestätigen",
      warning: "Die Wiedergabe endet. Backups sind Pflicht. Dies ist ein Neustart, kein Werksreset.", ack: "Ich möchte diese Radios neu starten",
      start: "Ausgewählte Radios neu starten", cancel: "Abbrechen", schedule: "Optionaler Neustart-Zeitplan",
      scheduleHelp: "Standardmäßig aus. Nur ausgewählte Radios im Standby und ohne aktive Gruppe. Ist ein ausgewähltes Radio aktiv oder nicht erreichbar, wird der gesamte Durchlauf ausgelassen. Verpasste Zeiten werden nicht nachgeholt.",
      enable: "Geplante Neustarts aktivieren", time: "Uhrzeit", timezone: "Zeitzone (IANA)", days: "Wochentage", scheduleAck: "Ich erlaube diesen Zeitplan für die ausgewählten Radios", save: "Zeitplan speichern",
      result: "Neustart-Fortschritt", none: "Kein Neustart gestartet.", details: "Technische Details", offline: "zuletzt offline", weekdays: ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
      failed: "Aktion oder Rückprüfung fehlgeschlagen. Siehe technische Details; keine automatische Wiederholung.", saved: "Zeitplan gespeichert und zurückgelesen.", off: "Geplante Neustarts sind aus.", on: "Aktiv für", loading: "Wird bearbeitet…", unknown: "Unbestätigter Zustand",
      volumeChange: "Firmware-Lautstärke nach Neustart", sourceChange: "Quellenliste nach Neustart verändert; vor Wiedergabe prüfen/zurücklesen.",
      PREPARING: "Identitäten prüfen und Backups sichern", DISPATCHING: "Neustartbefehle senden", BACKED_UP: "Backup bestätigt",
      COMMAND_ATTEMPTED: "Neustartbefehl versucht", WAITING_FOR_RESTART: "Warte auf Radio-Neustart", VERIFIED: "Rückkehr und gesicherte Einstellungen bestätigt",
      STATE_DIFFERENCE: "Radio zurück, aber Zustand abweichend — bitte prüfen", REBOOT_NOT_OBSERVED: "Neustart nicht beobachtet",
      COMMAND_UNCONFIRMED_NO_RETRY: "Befehlsausgang unklar — keine Wiederholung", VERIFICATION_FAILED: "Rückkehr oder Readback nicht bestätigt",
      BLOCKED_NO_REBOOT: "Vorprüfung blockiert; kein Radio neu gestartet", PARTIAL_OR_UNCONFIRMED: "Nicht alle Radios bestätigt — bitte prüfen", INTERRUPTED_NO_REPLAY: "Unterbrochen; Befehle werden nicht erneut gesendet",
    },
  };
  let lang = "en", config = null, busy = false, previewIds = null, timer = null;
  const t = (key) => messages[lang][key] || messages[lang].unknown;
  const selected = () => [...el("reboot-devices").querySelectorAll("input:checked")].map((input) => input.value);
  const terminal = new Set(["VERIFIED", "STATE_DIFFERENCE", "BLOCKED_NO_REBOOT", "PARTIAL_OR_UNCONFIRMED", "INTERRUPTED_NO_REPLAY"]);
  async function api(path, body, method = "POST") {
    const response = await fetch(path, body === undefined ? { cache: "no-store" } : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const result = await response.json();
    if (!response.ok) { const error = new Error(`HTTP ${response.status}`); error.detail = result; throw error; }
    return result;
  }
  function controls() {
    document.querySelectorAll("input,button").forEach((node) => { node.disabled = busy || !config; });
    el("reboot-preview").disabled = busy || !config || !selected().length;
    el("reboot-start").disabled = busy || !previewIds || !el("reboot-confirm-check").checked;
    el("reboot-schedule-save").disabled = busy || !config || (el("reboot-schedule-enabled").checked && (!selected().length || !el("reboot-schedule-confirm").checked));
  }
  function invalidatePreview() {
    previewIds = null;
    el("reboot-confirmation").hidden = true;
    el("reboot-confirm-check").checked = false;
    el("reboot-schedule-confirm").checked = false;
    controls();
  }
  async function run(action) {
    if (busy) return;
    busy = true; controls(); el("reboot-message").textContent = t("loading");
    try { await action(); }
    catch (error) { el("reboot-message").textContent = t("failed"); el("reboot-details").textContent = JSON.stringify(error.detail || String(error), null, 2); }
    finally { busy = false; controls(); }
  }
  function showJob(job) {
    el("reboot-job-status").textContent = t(job.status);
    el("reboot-details").textContent = JSON.stringify(job, null, 2);
    const list = el("reboot-results"); list.replaceChildren();
    for (const result of job.results || []) {
      const item = document.createElement("li");
      item.textContent = `${result.name || result.device_id}: ${t(result.status)}`;
      if (result.volume_before?.actual !== result.volume_after?.actual) item.textContent += ` — ${t("volumeChange")}: ${result.volume_before?.actual ?? "?"} → ${result.volume_after?.actual ?? "?"}`;
      if (result.source_registry_changed) item.textContent += ` — ${t("sourceChange")}`;
      list.append(item);
    }
  }
  async function pollJob(id) {
    clearTimeout(timer);
    try {
      const job = await api(`/api/radio-reboots/jobs/${encodeURIComponent(id)}`);
      showJob(job);
      if (!terminal.has(job.status)) timer = setTimeout(() => pollJob(id), 3000);
    } catch (error) {
      el("reboot-message").textContent = t("failed");
      // A browser read failure never replays the restart POST.
      el("reboot-details").textContent = JSON.stringify(error.detail || String(error), null, 2);
    }
  }
  function showSchedule(schedule) {
    el("reboot-schedule-enabled").checked = schedule.enabled;
    el("reboot-schedule-time").value = schedule.time;
    el("reboot-schedule-timezone").value = schedule.timezone;
    el("reboot-weekdays").querySelectorAll("input").forEach((input) => { input.checked = schedule.weekdays.includes(Number(input.value)); });
    const names = schedule.device_ids.map((id) => config.devices.find((device) => device.device_id === id)?.name || id);
    el("reboot-schedule-state").textContent = schedule.enabled ? `${t("on")} ${names.join(", ")} — ${schedule.time} (${schedule.timezone})` : t("off");
  }
  el("reboot-select-all").addEventListener("click", () => {
    el("reboot-devices").querySelectorAll("input").forEach((input) => { input.checked = config.devices.some((device) => device.device_id === input.value && device.reachable === true); });
    invalidatePreview();
  });
  el("reboot-devices").addEventListener("change", invalidatePreview);
  el("reboot-interrupt").addEventListener("change", invalidatePreview);
  el("reboot-confirm-check").addEventListener("change", controls);
  el("reboot-cancel").addEventListener("click", invalidatePreview);
  el("reboot-preview").addEventListener("click", () => run(async () => {
    const ids = selected();
    const result = await api("/api/radio-reboots/preview", { device_ids: ids });
    if (new Set(result.devices.map((device) => device.device_id)).size !== ids.length || result.devices.length !== ids.length || result.devices.some((device) => !ids.includes(device.device_id))) throw new Error("preview_selection_mismatch");
    previewIds = [...ids];
    el("reboot-preview-list").replaceChildren();
    for (const device of result.devices) { const item = document.createElement("li"); item.textContent = device.name; el("reboot-preview-list").append(item); }
    el("reboot-confirm-check").checked = false;
    el("reboot-confirmation").hidden = false;
    el("reboot-message").textContent = "";
  }));
  el("reboot-start").addEventListener("click", () => run(async () => {
    if (!previewIds || !el("reboot-confirm-check").checked || JSON.stringify(previewIds) !== JSON.stringify(selected())) throw new Error("new_preview_required");
    const ids = [...previewIds];
    invalidatePreview();
    const job = await api("/api/radio-reboots/start", { device_ids: ids, confirmation: config.manual_confirmation, allow_interrupt: el("reboot-interrupt").checked });
    el("reboot-message").textContent = "";
    showJob(job); await pollJob(job.id);
  }));
  for (const id of ["reboot-schedule-enabled", "reboot-schedule-time", "reboot-schedule-timezone", "reboot-weekdays"]) {
    el(id).addEventListener("change", () => { el("reboot-schedule-confirm").checked = false; controls(); });
  }
  el("reboot-schedule-confirm").addEventListener("change", controls);
  el("reboot-schedule-save").addEventListener("click", () => run(async () => {
    const enabled = el("reboot-schedule-enabled").checked;
    const body = { enabled, device_ids: selected(), time: el("reboot-schedule-time").value,
      weekdays: [...el("reboot-weekdays").querySelectorAll("input:checked")].map((input) => Number(input.value)),
      timezone: el("reboot-schedule-timezone").value, skip_active: true, confirmation: enabled ? config.schedule_confirmation : "" };
    if (enabled && !el("reboot-schedule-confirm").checked) throw new Error("schedule_confirmation_required");
    await api("/api/radio-reboots/schedule", body, "PUT");
    const readback = await api("/api/radio-reboots");
    for (const key of ["enabled", "device_ids", "time", "weekdays", "timezone", "skip_active"]) {
      if (JSON.stringify(body[key]) !== JSON.stringify(readback.schedule[key])) throw new Error("schedule_readback_mismatch");
    }
    config.schedule = readback.schedule;
    showSchedule(config.schedule); el("reboot-schedule-confirm").checked = false;
    el("reboot-message").textContent = t("saved");
  }));
  window.addEventListener("pagehide", () => clearTimeout(timer));
  run(async () => {
    const settings = await api("/api/system/settings");
    lang = settings.web_language === "de" ? "de" : "en";
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-text]").forEach((node) => { node.textContent = t(node.dataset.text); });
    document.title = `BASSWIESN — ${t("heading")}`;
    config = await api("/api/radio-reboots");
    const preselected = new URLSearchParams(location.search).get("device");
    for (const device of config.devices) {
      const label = document.createElement("label"), input = document.createElement("input");
      input.type = "checkbox"; input.value = device.device_id;
      input.checked = preselected ? device.device_id === preselected : config.schedule.device_ids.includes(device.device_id);
      label.append(input, document.createTextNode(device.name + (device.reachable ? "" : ` (${t("offline")})`)));
      el("reboot-devices").append(label);
    }
    el("reboot-devices").disabled = false;
    t("weekdays").forEach((name, index) => {
      const label = document.createElement("label"), input = document.createElement("input");
      input.type = "checkbox"; input.value = String(index); label.append(input, document.createTextNode(name)); el("reboot-weekdays").append(label);
    });
    showSchedule(config.schedule); el("reboot-message").textContent = "";
    if (config.latest_job_id) await pollJob(config.latest_job_id);
  });
})();
