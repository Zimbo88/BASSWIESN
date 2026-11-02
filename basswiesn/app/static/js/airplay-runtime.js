/* Explicit LAB session control. No discovery, automatic start or POST retry. */
(() => {
  "use strict";
  const anchor = document.querySelector(".airplay-readiness-panel");
  if (!anchor) return;
  const t = (de, en) => window.BasswiesnI18n.copy(de, en);
  const api = window.BasswiesnApi;
  const box = document.createElement("section");
  box.className = "panel lab-only";
  box.id = "airplay-runtime";
  box.dataset.authoredCopy = "true";
  anchor.after(box);
  const title = document.createElement("h3");
  const note = document.createElement("p");
  const output = document.createElement("p");
  output.setAttribute("role", "status");
  output.setAttribute("aria-live", "polite");
  const label = document.createElement("label");
  label.htmlFor = "airplay-runtime-profile";
  const select = document.createElement("select");
  select.id = label.htmlFor;
  const confirmLabel = document.createElement("label");
  const confirm = document.createElement("input");
  confirm.type = "checkbox";
  const confirmText = document.createElement("span");
  confirmLabel.append(confirm, confirmText);
  const buttons = document.createElement("div");
  buttons.className = "button-row";
  const refresh = document.createElement("button");
  const start = document.createElement("button");
  refresh.type = start.type = "button";
  refresh.className = start.className = "command";
  buttons.append(refresh, start);
  const sessions = document.createElement("div");
  box.append(title, note, output, label, select, confirmLabel, buttons, sessions);
  const terminal = new Set(["FINISHED", "FAILED", "RECOVERY_REQUIRED"]);
  let value = null, busy = false, pending = "", timer = null, message = null;
  const storageKey = "basswiesn.ap2.pending";
  try { pending = sessionStorage.getItem(storageKey) || ""; } catch (_) { /* optional nonsecret ID */ }
  if (!/^[a-f0-9]{32}$/.test(pending)) pending = "";
  const active = () => document.body.classList.contains("lab-mode");
  const states = {
    STARTING: ["Startauftrag gespeichert", "Start request recorded"],
    WAITING: ["Wartet auf AirPlay-Auswahl", "Waiting for AirPlay selection"],
    PREPARING: ["Sichert und prüft Radios", "Backing up and checking radios"],
    ACTIVE: ["Gerätezustand bestätigt; Ton bitte prüfen", "Device state verified; please check audio"],
    RESTORING: ["Vorheriger Zustand wird wiederhergestellt", "Restoring previous state"],
    STOPPING: ["Stopp angefordert; Wiederherstellung läuft", "Stop requested; restoration in progress"],
    FINISHED: ["Sitzung beendet und Wiederherstellung bestätigt", "Session ended and restoration verified"],
    FAILED: ["Sitzung fehlgeschlagen; keine ungeklärte Änderung gemeldet", "Session failed; no unresolved change reported"],
    RECOVERY_REQUIRED: ["Wiederherstellung ungeklärt – kein erneuter Start", "Restoration unresolved — do not restart"]
  };
  function render() {
    title.textContent = t("AP2-Bridge · LAB-Sitzungen", "AP2 bridge · LAB sessions");
    note.textContent = t("Startet nur ein lokal vorbereitetes, zeitlich begrenztes Profil. Noch kein automatischer Dauerbetrieb. Anmeldung als Administrator erforderlich.",
      "Starts only a locally prepared, time-limited profile. Not an automatic permanent service yet. Administrator sign-in is required.");
    label.textContent = t("Vorbereitetes Profil", "Prepared profile");
    confirmText.textContent = t("Ich bestätige diese Geräte und die angezeigte Lautstärkegrenze.", "I confirm these devices and the displayed volume limit.");
    refresh.textContent = t("Status lesen", "Read status");
    start.textContent = t("Sitzung starten", "Start session");
    const selected = select.value;
    select.replaceChildren();
    for (const profile of value?.profiles || []) {
      const option = document.createElement("option");
      option.value = profile.id;
      option.disabled = profile.consumed;
      option.textContent = `${profile.id}: ${profile.targets.join(", ")} · ${t("Lautstärke höchstens", "Volume at most")} ${profile.volume_limit} · ${profile.maximum_seconds}s`;
      select.append(option);
    }
    if ([...select.options].some(option => option.value === selected && !option.disabled)) select.value = selected;
    const profile = value?.profiles?.find(item => item.id === select.value);
    start.disabled = busy || !!pending || !active() || !value?.available || !profile || profile.consumed || !confirm.checked;
    select.disabled = busy || !!pending;
    refresh.disabled = busy;
    output.textContent = message ? t(...message) : t("Noch kein Status gelesen. Es wurde nichts gestartet.", "Status not read yet. Nothing has started.");
    sessions.replaceChildren();
    for (const item of value?.sessions || []) {
      const row = document.createElement("div");
      row.className = "event-row";
      const text = document.createElement("p");
      text.textContent = `${item.targets.join(", ")}: ${states[item.state] ? t(...states[item.state]) : item.state}`;
      row.append(text);
      if (!terminal.has(item.state)) {
        const stop = document.createElement("button");
        stop.type = "button";
        stop.className = "command";
        stop.textContent = t("Beenden und wiederherstellen", "Stop and restore");
        stop.disabled = busy || !active() || ["STOPPING", "RESTORING"].includes(item.state);
        stop.addEventListener("click", () => command("stop", {request_id: item.request_id}));
        row.append(stop);
      }
      sessions.append(row);
    }
  }
  async function read() {
    clearTimeout(timer);
    if (!active() || document.hidden) return;
    try {
      const response = await api.getJson("/api/airplay-bridge/runtime/status", {cache: "no-store", signal: AbortSignal.timeout(4000)});
      value = response.ok ? response.result : null;
      message = value ? ["Lokaler Dienst erreichbar. Ein Startauftrag ist noch kein Wiedergabenachweis.", "Local service reachable. A start request is not proof of playback."]
        : ["Kein vorbereiteter AP2-Dienst erreichbar. Es wurde nichts gestartet.", "No prepared AP2 service reachable. Nothing has started."];
      const current = value?.sessions?.find(item => item.request_id === pending);
      if (current && terminal.has(current.state)) {
        pending = "";
        try { sessionStorage.removeItem(storageKey); } catch (_) { /* optional */ }
      }
      if (pending && !current) message = ["Auftrag noch nicht bestätigt. Nur Status lesen, nicht erneut starten.", "Request not confirmed. Read status only; do not start again."];
    } catch (_) {
      value = null;
      message = ["Status nicht erreichbar. Kein automatischer Neustart.", "Status unavailable. No automatic restart."];
    }
    render();
    if (pending) timer = setTimeout(read, 2000);
  }
  async function command(action, body) {
    if (busy || !active()) return;
    busy = true;
    if (action === "start") {
      pending = body.request_id;
      try { sessionStorage.setItem(storageKey, pending); } catch (_) { /* optional */ }
    }
    render();
    try {
      const result = await api.postJson(`/api/airplay-bridge/runtime/${action}`, body);
      if (!result.ok && result.code !== "AIRPLAY_SUBMISSION_UNCERTAIN") {
        message = [`Abgelehnt: ${result.code}`, `Rejected: ${result.code}`];
        if (action === "start") {
          pending = "";
          try { sessionStorage.removeItem(storageKey); } catch (_) { /* optional */ }
        }
      } else {
        message = ["Auftrag gesendet. Tatsächlichen Zustand zurücklesen.", "Request sent. Reading actual state next."];
        if (result.ok) value = result.result;
      }
    } catch (error) {
      message = ["Auftrag nicht bestätigt. Nicht erneut senden; Status prüfen.", "Request not confirmed. Do not resend; check status."];
      if (error.status && [400, 401, 403, 409].includes(error.status) && action === "start") {
        pending = "";
        try { sessionStorage.removeItem(storageKey); } catch (_) { /* optional */ }
        const code = error.payload?.code || error.code;
        message = [`Start abgelehnt: ${code}`, `Start rejected: ${code}`];
      }
    } finally {
      busy = false;
      confirm.checked = false;
      render();
      if (pending) timer = setTimeout(read, 2000);
    }
  }
  refresh.addEventListener("click", read);
  confirm.addEventListener("change", render);
  select.addEventListener("change", () => { confirm.checked = false; render(); });
  start.addEventListener("click", () => {
    if (start.disabled) return;
    const requestId = crypto.randomUUID().replaceAll("-", "");
    command("start", {profile: select.value, request_id: requestId});
  });
  new MutationObserver(render).observe(document.body, {attributes: true, attributeFilter: ["class"]});
  new MutationObserver(render).observe(document.documentElement, {attributes: true, attributeFilter: ["lang"]});
  window.addEventListener("pagehide", () => clearTimeout(timer));
  render();
})();
