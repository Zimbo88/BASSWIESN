/* Explicit privileged action. Store only the request ID, never the admin code. */
(() => {
  "use strict";
  const t = (de, en) => window.BasswiesnI18n.copy(de, en);
  const storageKey = "basswiesn.update.request";
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const terminal = new Set(["COMPLETE", "RESTORED", "FAILED", "MANUAL_ACTION_REQUIRED"]);
  let release = null, helper = false, pending = "", timer = null, inFlight = false;
  let statusView = { kind: "empty" };
  try { pending = sessionStorage.getItem(storageKey) || ""; } catch (_) { /* no secret storage */ }
  if (!uuid.test(pending)) pending = "";
  const boundary = document.querySelector('[data-i18n="update_install_boundary"]');
  if (!boundary) return;
  const box = document.createElement("div");
  box.id = "update-admin";
  box.dataset.authoredCopy = "true";
  box.className = "lab-only";
  box.innerHTML = `<p id="update-admin-status" role="status"></p><a id="update-admin-https" hidden></a>
    <label><span id="update-admin-code-label"></span><input id="update-admin-code" type="password" autocomplete="off" spellcheck="false" maxlength="43"></label>
    <label class="toggle-line"><input id="update-admin-confirm" type="checkbox"><span id="update-admin-confirm-label"></span></label>
    <div class="button-row"><button id="update-admin-install" type="button" class="command primary" disabled></button>
    <button id="update-admin-refresh" type="button" class="command"></button></div>`;
  boundary.after(box);
  const code = box.querySelector("#update-admin-code"), confirm = box.querySelector("#update-admin-confirm");
  const install = box.querySelector("#update-admin-install"), output = box.querySelector("#update-admin-status");
  function labels() {
    box.querySelector("#update-admin-code-label").textContent = t("Administratorcode (aus der Installation)", "Administrator code (from installation)");
    box.querySelector("#update-admin-confirm-label").textContent = t("Ich bestätige die kurze Unterbrechung von BASSWIESN und laufenden Streams.", "I approve a brief interruption of BASSWIESN and active streams.");
    install.textContent = t("Update installieren", "Install update");
    box.querySelector("#update-admin-refresh").textContent = t("Installationsstatus prüfen", "Check installation status");
    const secure = location.protocol === "https:";
    const secureLink = box.querySelector("#update-admin-https");
    const secureUrl = new URL(location.href); secureUrl.protocol = "https:"; secureUrl.port = "1329";
    secureUrl.search = ""; secureUrl.hash = "";
    secureLink.href = secureUrl.href; secureLink.hidden = secure;
    secureLink.textContent = t("Sichere Oberfläche öffnen", "Open secure interface");
    code.disabled = !secure || !helper || !!pending;
    confirm.disabled = code.disabled;
    install.disabled = code.disabled || inFlight || !confirm.checked || !/^[A-Za-z0-9_-]{43}$/.test(code.value)
      || release?.status !== "update_available" || release?.package_assets_present !== true;
    renderStatus();
  }
  function stateText(state) {
    const words = {
      IDLE: ["Bereit. Zuerst nach einem offiziellen Update suchen.", "Ready. Check for an official update first."],
      REQUESTED: ["Auftrag sicher gespeichert.", "Request durably recorded."],
      VALIDATED: ["Installation geprüft.", "Installation checked."],
      STAGING: ["Release wird geprüft und vorbereitet. Das kann einige Minuten dauern.", "Verifying and preparing the release. This can take several minutes."],
      STAGED: ["Release vorbereitet.", "Release prepared."],
      STOP_REQUESTED: ["BASSWIESN wird angehalten.", "Stopping BASSWIESN."],
      STOPPED: ["BASSWIESN angehalten.", "BASSWIESN stopped."],
      BACKUP_STARTED: ["Sicherung wird erstellt.", "Creating backup."],
      BACKED_UP: ["Sicherung verifiziert.", "Backup verified."],
      START_REQUESTED: ["Neue Version wird gestartet.", "Starting the new version."],
      STARTED: ["Start und Version werden geprüft.", "Checking startup and version."],
      VERIFIED: ["Neue Version verifiziert.", "New version verified."],
      COMPLETE: ["Update abgeschlossen. Seite neu laden.", "Update complete. Reload the page."],
      ROLLING_BACK: ["Vorherige Version wird wiederhergestellt.", "Restoring the previous version."],
      RESTORED: ["Update fehlgeschlagen; vorherige Version und Daten wiederhergestellt.", "Update failed; previous version and data restored."],
      FAILED: ["Update vor dem Wechsel abgebrochen. Fehlercode prüfen.", "Update stopped before switching. Check the error code."],
      MANUAL_ACTION_REQUIRED: ["Manuelle Wiederherstellung erforderlich. Kein weiteres Update starten; Sicherungen bleiben erhalten.", "Manual recovery required. Do not start another update; backups are retained."]
    };
    return words[state] ? t(...words[state]) : t("Status nicht bestätigt.", "Status not confirmed.");
  }
  const reconnectText = () => t("Verbindung unterbrochen oder Auftrag noch nicht bestätigt. Es wird nur der Status gelesen – kein zweiter Installationsauftrag gesendet.", "Connection interrupted or request not yet confirmed. Only status is being read — no second installation request is sent.");
  function renderStatus() {
    if (location.protocol !== "https:") output.textContent = t("Für die Installation BASSWIESN über HTTPS öffnen und das Serverzertifikat anhand der Installationsangaben prüfen. Hier wird kein Administratorcode übertragen.", "To install, open BASSWIESN over HTTPS and verify the server certificate against the installation information. No administrator code is transmitted here.");
    else if (statusView.kind === "reconnect") output.textContent = reconnectText();
    else if (statusView.kind === "unavailable") output.textContent = t("Hostdienst nicht erreichbar. Administrator-Einrichtung im Installer erforderlich.", "Host service unavailable. Administrator setup in the installer is required.");
    else if (statusView.kind === "rejected") output.textContent = t("Installation abgelehnt", "Installation rejected") + ` (${statusView.code})`;
    else if (statusView.kind === "job") output.textContent = `${stateText(statusView.state)}${statusView.code ? ` (${statusView.code})` : ""}`;
  }
  async function readStatus() {
    clearTimeout(timer);
    try {
      const response = await fetch("/api/update/admin/status", { cache: "no-store", signal: AbortSignal.timeout(6000) });
      const value = await response.json();
      helper = value.installation_available === true;
      const job = value.result;
      if (!value.ok) statusView = { kind: pending ? "reconnect" : "unavailable" };
      else if (pending && job?.request_id !== pending) statusView = { kind: "reconnect" };
      else {
        statusView = { kind: "job", state: job.state, code: job.code };
        if (job.request_id && !terminal.has(job.state)) {
          pending = job.request_id;
          try { sessionStorage.setItem(storageKey, pending); } catch (_) { /* polling also recovers the latest job */ }
        }
        if (pending === job.request_id && terminal.has(job.state)) {
          pending = "";
          try { sessionStorage.removeItem(storageKey); } catch (_) { /* optional storage */ }
          release = null; // new explicit release check required, never replay
        }
      }
    } catch (_) { helper = false; statusView = { kind: "reconnect" }; }
    labels();
    if (pending) timer = setTimeout(readStatus, 3000);
  }
  install.addEventListener("click", async () => {
    if (install.disabled) return;
    inFlight = true;
    pending = crypto.randomUUID();
    try { sessionStorage.setItem(storageKey, pending); } catch (_) { /* no capability is stored */ }
    const body = JSON.stringify({ protocol: 1, action: "submit", approve: true, request_id: pending,
      target_version: release.remote_version, expected_current_version: release.local_version, credential: code.value });
    code.value = ""; confirm.checked = false; labels();
    try {
      const response = await fetch("/api/update/admin/install", { method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-BASSWIESN-Update": "1" }, body,
        signal: AbortSignal.timeout(12000) });
      const value = await response.json();
      if (!value.ok && value.code !== "SUBMISSION_UNCERTAIN") {
        pending = "";
        try { sessionStorage.removeItem(storageKey); } catch (_) { /* optional */ }
        statusView = { kind: "rejected", code: value.code || value.detail?.code || "UNKNOWN" };
        return;
      }
      statusView = value.ok ? { kind: "job", state: value.result.state } : { kind: "reconnect" };
    } catch (_) { statusView = { kind: "reconnect" }; }
    finally { inFlight = false; labels(); if (pending) timer = setTimeout(readStatus, 1500); }
  });
  box.querySelector("#update-admin-refresh").addEventListener("click", readStatus);
  code.addEventListener("input", labels); confirm.addEventListener("change", labels);
  code.addEventListener("keydown", event => { if (event.key === "Enter") event.preventDefault(); });
  window.addEventListener("basswiesn:official-release", event => { release = event.detail; readStatus(); });
  new MutationObserver(labels).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
  labels();
  if (pending) readStatus(); // read-only reconnect; no automatic install
})();
