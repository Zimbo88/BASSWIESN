/* Session-only credential management. No storage, silent retries or HTML interpolation. */
(function () {
  "use strict";
  const de = navigator.language.startsWith("de");
  const text = de ? {
    back: "Zurück zu BASSWIESN", title: "Automatisierungszugriff",
    intro: "Technische Vorschau. Zugriff nur auf geprüfte Endpunkte; alle Geräteschutzregeln bleiben aktiv. Kein Token erlaubt Updates oder Token-Verwaltung.",
    newSecret: "Zugang jetzt sicher speichern", credential: "Zugangsschlüssel",
    secretHelp: "Nur einmal sichtbar. Im privaten Schlüsselspeicher deiner Automatisierung ablegen, niemals in Logs, Screenshots oder URLs. Beim Verlassen dieser Seite wird die Anzeige gelöscht.",
    clear: "Gespeichert — Anzeige löschen", createTitle: "Befristeten Token erstellen",
    name: "Name (Buchstaben A–Z, Ziffern, Leerzeichen, . _ -)", days: "Gültigkeit in Tagen (1–90)",
    scopes: "Explizite Rechte — kein Recht schließt andere ein",
    scopeRead: "Lesen: gespeicherte Geräte- und lokale Statusdaten",
    scopePresets: "Presets: gespeicherte Plätze lesen, keine Änderungen",
    scopeMultiroom: "Multiroom: gespeicherte Szenarien lesen, keine Gruppenänderungen",
    scopeDiagnostics: "Diagnose: zusammengefasste Beobachtungen lesen",
    scopePlayback: "Wiedergabe: nur überprüfte Lautstärkeänderungen",
    scopeAdmin: "Administration: nur überprüfte Namens- und Einstellungsänderungen",
    create: "Token erstellen", existing: "Vorhandene Berechtigungen", refresh: "Berechtigungen neu einlesen",
    lastUseHelp: "Letzte Nutzung bedeutet autorisierte Anfrage, nicht erfolgreiche Geräteänderung. Rotation behält Rechte und Ablaufzeit; Widerruf ist endgültig.",
    loading: "Berechtigungen werden gelesen …", loaded: "Berechtigungen eingelesen. Keine Radios kontaktiert.",
    unavailable: "Verwaltung nicht verfügbar. Anmeldung und lokales Auth-Schema prüfen. Keine Aktion wiederholen, ohne den Zustand neu einzulesen.",
    uncertain: "Ergebnis nicht bestätigt. Nicht erneut senden: erst Berechtigungen neu einlesen und gegebenenfalls widerrufen. Ein verlorener Schlüssel ist nicht wieder abrufbar.",
    failed: "Aktion abgelehnt. Berechtigungen vor der nächsten Aktion neu einlesen.",
    issued: "Token erstellt. Schlüssel jetzt privat speichern.", rotated: "Token rotiert. Der vorherige Schlüssel ist ungültig.",
    confirming: "Antwort erhalten; Berechtigungszustand wird überprüft …",
    revoked: "Widerruf bestätigt.", empty: "Noch keine Berechtigungen.",
    active: "Aktiv", expired: "Abgelaufen", revokedLabel: "Widerrufen", expiry: "Ablauf", lastUse: "Letzte Nutzung", never: "Noch nicht beobachtet",
    rotate: "Rotieren", revoke: "Widerrufen", permissions: "Rechte", selectScope: "Mindestens ein Recht wählen.",
    confirmRotate: "Schlüssel für {name} ersetzen? Der alte Schlüssel wird sofort ungültig; Ablauf und Rechte bleiben gleich.",
    confirmRevoke: "Berechtigung {name} endgültig widerrufen?", state: "Status",
  } : {
    back: "Back to BASSWIESN", title: "Automation access",
    intro: "Engineering preview. Access applies only to reviewed routes; device safety checks still apply. No token grants updater or token-management access.",
    newSecret: "Save this credential now", credential: "Credential",
    secretHelp: "Shown once. Store it in your automation client's private credential store. Do not put it in logs, screenshots or URLs. It will be cleared when you leave this page.",
    clear: "I saved it — clear", createTitle: "Create a bounded token", name: "Name (letters A–Z, digits, spaces, . _ -)", days: "Lifetime in days (1–90)",
    scopes: "Explicit permissions — no scope implies another", scopeRead: "Read: cached device and local health information",
    scopePresets: "Presets: read stored slots only, no writes", scopeMultiroom: "Multiroom: read stored scenarios only, no group changes",
    scopeDiagnostics: "Diagnostics: read aggregate observations", scopePlayback: "Playback: verified volume changes only",
    scopeAdmin: "Admin: verified rename and settings changes only", create: "Create token", existing: "Existing grants", refresh: "Refresh grants",
    lastUseHelp: "Last use means an authorized request, not a successful device change. Rotation keeps the same permissions and expiry; revocation cannot be undone.",
    loading: "Reading grants …", loaded: "Grants read. No radios contacted.",
    unavailable: "Management unavailable. Check sign-in and the local auth schema. Read the current state before attempting another action.",
    uncertain: "Outcome not confirmed. Do not send again: refresh grants and revoke if necessary. A lost credential cannot be retrieved.",
    failed: "Action rejected. Refresh grants before the next action.", issued: "Token created. Save the credential privately now.",
    confirming: "Response received; verifying the grant state …",
    rotated: "Token rotated. The previous credential is invalid.", revoked: "Revocation confirmed.", empty: "No grants yet.",
    active: "Active", expired: "Expired", revokedLabel: "Revoked", expiry: "Expires", lastUse: "Last use", never: "Not observed yet",
    rotate: "Rotate", revoke: "Revoke", permissions: "Permissions", selectScope: "Select at least one permission.",
    confirmRotate: "Replace the credential for {name}? The old credential becomes invalid immediately; expiry and permissions remain unchanged.",
    confirmRevoke: "Permanently revoke {name}?", state: "State",
  };
  document.documentElement.lang = de ? "de" : "en";
  document.title = `BASSWIESN · ${text.title}`;
  for (const node of document.querySelectorAll("[data-text]")) node.textContent = text[node.dataset.text];
  const status = document.getElementById("token-status");
  const list = document.getElementById("token-list");
  const form = document.getElementById("token-create");
  const refresh = document.getElementById("token-refresh");
  const secret = document.getElementById("token-secret");
  let busy = false;
  let ready = false;
  let generation = 0;
  let gone = false;

  function clearSecret() {
    secret.value = "";
    document.getElementById("new-secret").hidden = true;
  }
  function controls() {
    form.querySelectorAll("button,input").forEach(node => { node.disabled = busy || !ready; });
    list.querySelectorAll("button").forEach(node => { node.disabled = busy || !ready || node.dataset.inactive === "true"; });
    refresh.disabled = busy;
  }
  function element(tag, content) {
    const node = document.createElement(tag);
    node.textContent = content;
    return node;
  }
  function render(records) {
    list.replaceChildren();
    if (!records.length) list.append(element("p", text.empty));
    for (const row of records) {
      const card = document.createElement("article");
      card.className = "token-card";
      card.dataset.tokenId = row.id;
      card.append(element("h3", row.name));
      const fields = document.createElement("dl");
      const expired = row.expires_at * 1000 <= Date.now();
      for (const [key, value] of [
        [text.state, row.revoked ? text.revokedLabel : expired ? text.expired : text.active],
        [text.permissions, row.scopes.join(", ")],
        [text.expiry, new Date(row.expires_at * 1000).toLocaleString()],
        [text.lastUse, row.last_used ? new Date(row.last_used * 1000).toLocaleString() : text.never],
      ]) fields.append(element("dt", key), element("dd", value));
      card.append(fields);
      for (const action of ["rotate", "revoke"]) {
        const button = element("button", text[action]);
        button.type = "button";
        button.dataset.action = action;
        button.dataset.inactive = String(row.revoked || (action === "rotate" && expired));
        button.addEventListener("click", () => {
          if (busy || !ready) return;
          const prompt = action === "rotate" ? text.confirmRotate : text.confirmRevoke;
          if (!window.confirm(prompt.replace("{name}", row.name))) return;
          const url = `/auth/tokens/${encodeURIComponent(row.id)}${action === "rotate" ? "/rotate" : ""}`;
          mutate(url, {method: action === "rotate" ? "POST" : "DELETE"}, action, row);
        });
        card.append(button);
      }
      list.append(card);
    }
  }
  async function readGrants() {
    const response = await window.BasswiesnAuth.fetch("/auth/tokens", {cache: "no-store"});
    if (!response.ok) throw new Error("GRANTS_UNAVAILABLE");
    const body = await response.json();
    if (!Array.isArray(body.tokens)) throw new Error("GRANTS_INVALID");
    return body.tokens;
  }
  async function load() {
    if (busy || gone) return;
    busy = true;
    ready = false;
    clearSecret();
    controls();
    status.textContent = text.loading;
    const ticket = ++generation;
    try {
      const records = await readGrants();
      if (gone || ticket !== generation) return;
      render(records);
      ready = true;
      status.textContent = text.loaded;
    } catch {
      if (!gone) status.textContent = text.unavailable;
    } finally {
      busy = false;
      controls();
    }
  }
  async function mutate(url, options, action, previous = null) {
    if (busy || !ready || gone || document.hidden) return;
    const requested = action === "create" ? JSON.parse(options.body) : null;
    busy = true;
    ready = false;
    clearSecret();
    controls();
    const ticket = ++generation;
    try {
      const response = await window.BasswiesnAuth.fetch(url, {...options, cache: "no-store"});
      if (gone || ticket !== generation) return;
      if (!response.ok) {
        status.textContent = response.status < 500 ? text.failed : text.uncertain;
        return;
      }
      const body = await response.json();
      if (gone || ticket !== generation) return;
      if (action !== "revoke") {
        const envelope = typeof body.token === "string" && /^bwapi_([a-f0-9]{32})_[A-Za-z0-9_-]{43}$/.exec(body.token);
        if (!envelope || envelope[1] !== body.id
            || (previous && (body.id !== previous.id || body.name !== previous.name))) {
          throw new Error("TOKEN_RESPONSE_INVALID");
        }
        secret.value = body.token;
        document.getElementById("new-secret").hidden = false;
        secret.focus();
        secret.select();
      } else if (typeof body.revoked !== "boolean") throw new Error("TOKEN_RESPONSE_INVALID");
      status.textContent = text.confirming;
      const records = await readGrants();
      if (gone || ticket !== generation) return;
      const actual = records.find(row => row.id === (action === "revoke" ? previous.id : body.id));
      if (action === "revoke") {
        if (actual && !actual.revoked) throw new Error("TOKEN_READBACK_MISMATCH");
      } else {
        const sameScopes = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
        if (!actual || actual.revoked || actual.name !== body.name || actual.expires_at !== body.expires_at
            || !sameScopes(actual.scopes, body.scopes)
            || (previous && (actual.expires_at !== previous.expires_at || !sameScopes(actual.scopes, previous.scopes)))
            || (requested && (actual.name !== requested.name || !sameScopes(actual.scopes, requested.scopes)
                || actual.expires_at - actual.created_at !== requested.lifetime_seconds))) {
          throw new Error("TOKEN_READBACK_MISMATCH");
        }
      }
      render(records);
      ready = true;
      status.textContent = text[action === "create" ? "issued" : action === "rotate" ? "rotated" : "revoked"];
    } catch {
      if (!gone) status.textContent = text.uncertain;
    } finally {
      busy = false;
      controls();
    }
  }
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (busy || !ready || !form.reportValidity()) return;
    const scopes = [...form.querySelectorAll('input[name="scope"]:checked')].map(node => node.value);
    if (!scopes.length) { status.textContent = text.selectScope; return; }
    mutate("/auth/tokens", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      name: document.getElementById("token-name").value,
      scopes, lifetime_seconds: Number(document.getElementById("token-days").value) * 86400,
    })}, "create");
  });
  document.getElementById("clear-secret").addEventListener("click", clearSecret);
  refresh.addEventListener("click", load);
  window.addEventListener("pagehide", () => { gone = true; generation++; ready = false; clearSecret(); });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) return;
    // A sent write may still complete. Invalidate only its display ticket:
    // never redisplay a late credential or retry the mutation on returning.
    generation++;
    clearSecret();
    if (busy) { ready = false; status.textContent = text.uncertain; }
  });
  window.addEventListener("pageshow", event => { if (event.persisted) { gone = false; busy = false; load(); } });
  controls();
  load();
}());
