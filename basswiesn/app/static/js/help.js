/* Context help and illustrated tutorials. Intentionally no transport/API access. */
(() => {
  "use strict";
  const i = window.BasswiesnI18n;
  const content = window.BasswiesnHelpContent;
  const topics = {
    setup: ["setup", "writes"], remote: ["remote_control", "writes"],
    presets: ["presets", "writes"], multiroom: ["multiroom", "writes"],
    display: ["help:display", "writes"], workbench: ["word:workbench", "preview"],
    airplay: ["word:airplay", "preview"], dlna: ["word:dlna", "preview"],
    updater: ["word:updater", "preview"], recovery: ["diagnostics", "readOnly"]
  };
  const context = {
    dashboard: "recovery", setup: "setup", devices: "setup", health: "recovery",
    controls: "remote", stations: "presets", presets: "presets", multiroom: "multiroom",
    schedules: "remote", "device-settings": "display", display: "display", media: "dlna",
    "system-settings": "updater", backup: "recovery", config: "setup", telnet: "recovery",
    debug: "recovery", telemetry: "recovery", lab: "workbench", about: "recovery"
  };
  // Specialized context is kept separate from a tutorial: a tutorial button is
  // not permission to run discovery, import, reset, install or audio tests.
  const extra = {
    "system-settings": ["Oberflächensprache, Darstellung und Standardwerte betreffen BASSWIESN, nicht die Sprache oder Firmware des Radios. Ein Updatecheck installiert nichts. Erweiterte Installation bleibt in LAB.", "Interface language, appearance and defaults belong to BASSWIESN, not the radio's language or firmware. An update check installs nothing. Advanced installation remains in LAB."],
    factory: ["Werksreset löscht die Radio-Konfiguration. Vorher Identität, Modell und Sicherung prüfen. Nur das ausdrücklich bestätigte Radio wird angesprochen. Nach dem Befehl ist Nichterreichbarkeit zu erwarten; sie beweist nicht allein einen vollständig abgeschlossenen Reset. Anschließend neu einrichten.", "Factory reset erases radio configuration. Verify identity, model and backup first. Only the explicitly confirmed radio is targeted. Becoming unreachable after the command is expected; it does not alone prove reset completion. Setup is required afterward."],
    battery: ["Der Batteriepatch ist eine invasive, modellspezifische Forschungsfunktion, keine allgemeine Reparatur. Nicht vorsorglich auf ein Gerät mit Originalbatterie anwenden. Originalzustand, Kompatibilität, Sicherung und Rückweg müssen vor einem Versuch feststehen. Bei unklarer Ursache nicht ausführen.", "The battery patch is invasive, model-specific research, not a general repair. Do not apply it preventively to a device with an original battery. Establish the original state, compatibility, backup and restoration path first. Do not run it when the cause is unknown."],
    restart: ["Neustart ist kein Werksreset. Identität und Sicherung werden vor den Befehlen geprüft. Laufende Wiedergabe nur nach eigener Freigabe unterbrechen. Ein Zeitplan ist standardmäßig aus und überspringt aktive oder nicht erreichbare Geräte. Danach Rückkehr und Werte prüfen; kein automatischer Wiedergabestart.", "Restart is not factory reset. Identity and backups are checked before commands. Interrupt active playback only with explicit approval. Scheduling is off by default and skips active or unreachable devices. Verify return and values afterward; no automatic playback resume."],
    ssh: ["SSH und remote_services sind technische Zugriffswege. Erreichbarkeit, Marker und dauerhafte Aktivierung sind verschiedene Nachweise. Niemals Passwörter, private Schlüssel oder unbereinigte Logs veröffentlichen. Aktivierung kann persistent sein; vorher sichern und Rückweg prüfen.", "SSH and remote_services are technical access paths. Reachability, markers and persistent enablement are different observations. Never publish passwords, private keys or unredacted logs. Enabling access may persist; back up and verify the restoration path first."],
    schedules: ["Wecker und Timer speichern künftige Aktionen. Zeitzone, Wochentage, Zielradio und Lautstärke vor dem Aktivieren prüfen. Ein verpasster Lauf ist nicht automatisch ein Fehler am Radio.", "Alarms and timers store future actions. Check timezone, weekdays, target radio and volume before enabling them. A missed run does not automatically mean the radio failed."],
    backup: ["Radio-Backups und Werkstatt-Momentaufnahmen sind verschieden. Vor einem Restore Geräte-ID, Firmware und Umfang prüfen. Ein Werksreset löscht die Gerätekonfiguration und erfordert eigene Bestätigung; er ist kein Neustart.", "Radio backups and workbench snapshots are different. Before restoring, check device identity, firmware and scope. Factory reset erases device configuration and requires separate confirmation; it is not a restart."],
    telnet: ["Interne CLI-Befehle können dauerhaft schreiben. Nur bekannte Befehle verwenden, vorher sichern und danach zurücklesen. Geschützte Geräte bleiben gesperrt. Ein sichtbares Werkzeug ist keine Empfehlung, es auszuführen.", "Internal CLI commands may make persistent changes. Use known commands only, back up first and read back afterward. Protected devices remain blocked. A visible tool is not a recommendation to run it."],
    config: ["Routing bestimmt, wohin Radioanfragen gehen. Eine falsche Adresse kann Wiedergabe verhindern. Erst Identität und Sicherung prüfen; technische Rohdaten enthalten möglicherweise private Angaben.", "Routing determines where radio requests go. A wrong address can prevent playback. Verify identity and backup first; technical raw data may contain private information."],
    about: ["Version und Veröffentlichungsdatum gehören zur laufenden Software. Die persönliche Projektbeschreibung bleibt im Original erhalten; unübersetzte Inhalte werden auf Englisch gezeigt.", "Version and publication date refer to the running software. The personal project statement is preserved; untranslated content is shown in English."],
    lab: ["LAB ist kein Qualitätsversprechen. Jede Aktion hat eigene Schutzregeln. AirPlay-Bridge, DLNA-Audio und Updateinstallation warten weiterhin auf die angegebene Geräte-/Produktionsabnahme. Keine automatische Freigabe durch einen Moduswechsel.", "LAB is not a quality guarantee. Each action has its own guards. The AirPlay bridge, DLNA audio and update installation still require their stated hardware/production acceptance. Switching mode does not grant automatic approval."],
    "device-settings": ["Radio-Sprache, Bass, Standby-Uhr und Zeitlimits sind Geräteeinstellungen. Die Sprache der Weboberfläche ist davon unabhängig. Die Uhr in Wiedergabemetadaten ist kein eigener nativer Uhrbereich des Radios.", "Radio language, bass, standby clock and timeouts are device settings. The web interface language is independent. A clock in playback metadata is not a separate native clock area on the radio."],
    health: ["Die Zeitlinie trennt Provider, Audio, Metadaten und Reporting. Klicke einen Eintrag für Details. UNKNOWN heißt: Noch kein belastbarer Nachweis. Fehlende Titel können vom Sender fehlen, obwohl Audio funktioniert.", "The timeline separates provider, audio, metadata and reporting. Open an entry for details. UNKNOWN means evidence is missing. A station may supply no titles even while audio works."],
    debug: ["Protokolle können Geräteadressen, Sendernamen und weitere private Daten enthalten. Vor Export oder Weitergabe prüfen und bereinigen. Ein Logeintrag ist keine vollständige Aufzeichnung jeder Radioaktion.", "Logs can include device addresses, station names and other private data. Review and redact them before sharing. A log entry is not a complete record of every radio action."]
  };
  let dialog, topic = "setup", pageContext = "", position = null, opener;
  const word = key => i.word(key);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function title(key) {
    const value = topics[key][0];
    if (value.startsWith("word:")) return word(value.slice(5));
    if (value === "help:display") return i.copy("Radio-Display", "Radio display");
    return i.t(value);
  }
  function button(text, action, className = "command") {
    const result = node("button", text, className);
    result.type = "button";
    result.addEventListener("click", action);
    return result;
  }
  function ensureDialog() {
    if (dialog) return;
    dialog = node("dialog", undefined, "guide-dialog");
    dialog.id = "page-help";
    dialog.dataset.authoredCopy = "true";
    dialog.setAttribute("translate", "no");
    dialog.setAttribute("aria-labelledby", "guide-title");
    document.body.append(dialog);
    dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener("close", () => { if (opener?.isConnected) opener.focus(); });
  }
  function diagram(steps) {
    const figure = node("figure", undefined, "guide-illustration");
    const flow = node("ol", undefined, "guide-flow");
    // HTML diagram: translated labels wrap naturally at mobile widths and high
    // zoom. It is an illustration, never a fabricated hardware screenshot.
    for (const [index, text] of steps.entries()) {
      const item = node("li", undefined, position === index ? "current" : "");
      const number = node("span", String(index + 1), "guide-number");
      number.setAttribute("aria-hidden", "true");
      item.append(number, node("span", position === null ? text : `${word("step")} ${index + 1}`));
      if (position === index) item.setAttribute("aria-current", "step");
      flow.append(item);
    }
    figure.append(flow, node("figcaption", word("illustration")));
    return figure;
  }
  function render() {
    if (!dialog) return;
    const steps = (content[i.language()] || content.en)[topic];
    const head = node("header"), heading = node("h2", title(topic));
    heading.id = "guide-title";
    head.append(heading, button(word("close"), () => dialog.close()));
    const body = node("div", undefined, "guide-body");
    body.append(node("p", word(topics[topic][1]), "guide-badge"), node("p", word("passive")));
    if (extra[pageContext] && position === null) {
      const [de, en] = extra[pageContext];
      const isGerman = i.language() === "de";
      const text = node("p", isGerman ? de : en);
      text.lang = isGerman ? "de" : "en";
      if (!["de", "en"].includes(i.language())) body.append(node("small", word("fallback")));
      body.append(text);
    }
    body.append(diagram(steps));
    if (position === null) {
      body.append(button(word("tutorial"), () => { position = 0; render(); }));
      // LAB help exposes the separate contracts, not a wall of terminology.
      if (pageContext === "lab") {
        const choices = node("div", undefined, "guide-choices");
        for (const key of ["workbench", "airplay", "dlna", "updater", "recovery"]) {
          choices.append(button(title(key), () => { topic = key; position = 0; render(); }));
        }
        body.append(choices);
      }
    } else {
      const step = node("section", undefined, "guide-step");
      step.tabIndex = -1;
      const label = node("h3", `${word("step")} ${position + 1} / ${steps.length}`);
      label.id = "guide-step-title";
      step.setAttribute("aria-labelledby", label.id);
      step.append(label, node("p", steps[position]));
      body.append(step);
      const controls = node("div", undefined, "guide-controls");
      const back = button(word("back"), () => { position = position > 0 ? position - 1 : null; render(); });
      const next = button(position === steps.length - 1 ? word("done") : word("next"), () => {
        if (position === steps.length - 1) dialog.close();
        else { position += 1; render(); }
      });
      controls.append(back, next); body.append(controls);
    }
    body.append(node("p", topic === "workbench" ? word("cached") : word("caution"), "guide-tip"));
    dialog.replaceChildren(head, body);
    if (dialog.open && position !== null) dialog.querySelector(".guide-step")?.focus();
  }
  function open(key, trigger, tutorial = false, view = "") {
    if (!topics[key]) return;
    ensureDialog();
    topic = key; position = tutorial ? 0 : null; pageContext = view; opener = trigger;
    render();
    if (!dialog.open) dialog.showModal();
    if (tutorial) dialog.querySelector(".guide-step")?.focus();
  }
  function labels() {
    document.querySelectorAll("[data-guide-page],[data-guide-note]").forEach(element => {
      element.setAttribute("aria-label", word("help"));
      element.title = word("help");
    });
    document.querySelectorAll("[data-guide-topic]").forEach(element => {
      element.textContent = `${word("tutorial")} · ${title(element.dataset.guideTopic)}`;
    });
    if (dialog?.open) render();
  }
  function install() {
    document.querySelectorAll(".view").forEach(view => {
      const head = view.querySelector(":scope > .page-head");
      const key = view.id.replace("view-", "");
      if (!head || !context[key] || head.querySelector("[data-guide-page]")) return;
      const help = button("?", () => open(context[key], help, false, key), "page-help-button");
      help.dataset.guidePage = key;
      head.append(help);
    });
    for (const [selector, key] of [
      ["#lab-workbench", "workbench"], ["#dlna-library-panel", "dlna"],
      ["#update-admin", "updater"], ["#airplay-readonly-probe", "airplay"],
      ["#view-presets .page-head", "presets"], ["#view-multiroom .page-head", "multiroom"],
      ["#view-display .page-head", "display"], [".remote-shell header", "remote"],
      ["#reboot-preview", "recovery"]
    ]) {
      const target = document.querySelector(selector);
      if (!target || document.querySelector(`[data-guide-topic="${key}"]`)) continue;
      const help = button("", () => open(key, help, true), "command guide-launch");
      help.dataset.guideTopic = key;
      // Keep tutorial links inside the owning view / LAB-only container.
      if (target.classList.contains("lab-only") || target.id === "update-admin" || target.id === "airplay-readonly-probe") help.classList.add("lab-only");
      target.after(help);
    }
    for (const [selector, note] of [["#lab-factory-reset-card", "factory"], ["#battery-patch-form", "battery"],
      ["#reboot-preview", "restart"], ["#telnet-reboot-form", "restart"], ["#ssh-log-capture-form", "ssh"]]) {
      const target = document.querySelector(selector);
      if (!target || document.querySelector(`[data-guide-note="${note}"]`)) continue;
      const help = button("?", () => open("recovery", help, false, note), "page-help-button");
      help.dataset.guideNote = note;
      help.setAttribute("aria-label", word("help"));
      target.after(help);
    }
    labels();
  }
  window.BasswiesnHelp = { install, open, topics: Object.keys(topics) };
  // Widget scripts run in document order; one deferred installation includes
  // their created panels without a perpetual subtree observer.
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, {once:true});
  else install();
  new MutationObserver(labels).observe(document.documentElement, {attributes:true, attributeFilter:["lang"]});
})();
