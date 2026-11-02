/* Explicit LAB plans. No inspection, SSH, playback or installation on load. */
(() => {
  "use strict";
  const panel = document.getElementById("radio-patches");
  if (!panel) return;
  panel.dataset.authoredCopy = "true";
  const copy = {
    de: {
      title: "Radio-Darstellung und Patches", open: "Auswahl öffnen", radio: "Radio", save: "Plan speichern",
      note: "LAB · Display und Bootlogo sind im Setup optional. Normal lässt Firmware und bestehende Patches unverändert. Dieser separate Plan installiert nichts. Der neue Setup-Ablauf benötigt noch Hardwareabnahme.",
      help: "Hilfe und Kompatibilität", keep: "Unverändert lassen", enable: "Einbau vorsehen", disable: "Rückbau vorsehen",
      display: "Display: Sender, Titel/Interpret, Uhr", bootlogo: "Bootbild: BASSWIESN", battery: "Ersatzbatterie",
      battery_kind: "Eingebaute Batterie", unknown: "Nicht geprüft", original: "Originalbatterie", compatible_replacement: "Geprüfte kompatible Ersatzbatterie",
      display_help: "Normale Wiedergabe: feste Zeilen und zentrierte Uhr. Bei fehlenden Musikdaten: Sender einmal, Uhr größer. Nur nach geprüftem vollständigem Neustart; BoseApp niemals im laufenden Betrieb ersetzen.",
      bootlogo_help: "BASSWIESN über dem nativen Statusfeld. Kein Bootloader-Patch; der Schriftzug ist nicht für das allererste Bootbild garantiert. Separat vom Display rückbaubar.",
      battery_help: "Nur für passende SoundTouch-Portables mit kompatibler Ersatzbatterie. Nicht für Originalbatterien nötig. Display und Bootlogo dürfen BatteryMonitor nicht verändern; vorhandene Anpassungen werden erhalten.",
      blocked: "WLAN-Kanalwechsel und Langdruck-Neustart: noch nicht für die Installation freigegeben.",
      saved: "Plan gespeichert. Am Radio wurde nichts geändert.", busy: "Bitte warten …",
      failed: "Nicht gespeichert. Auswahl neu laden und Voraussetzungen prüfen.", empty: "Kein ungeschütztes Radio gespeichert.",
    },
    en: {
      title: "Radio presentation and patches", open: "Open selection", radio: "Radio", save: "Save plan",
      note: "LAB · Display and boot lettering are optional in setup. Normal preserves firmware and existing patches. This separate plan installs nothing. The new setup workflow still requires hardware acceptance.",
      help: "Help and compatibility", keep: "Keep unchanged", enable: "Plan installation", disable: "Plan removal",
      display: "Display: station, title/artist, clock", bootlogo: "Boot image: BASSWIESN", battery: "Replacement battery",
      battery_kind: "Installed battery", unknown: "Not checked", original: "Original battery", compatible_replacement: "Verified compatible replacement battery",
      display_help: "Playback uses fixed rows and a centred clock. Missing song data: one station label and a larger clock. Activation requires a verified full reboot; never replace a running BoseApp.",
      bootlogo_help: "BASSWIESN above the native progress field. No bootloader patch; lettering on the very first boot frame is not guaranteed. Can be removed independently of the display.",
      battery_help: "Only for exact SoundTouch Portable profiles with compatible replacement batteries. Not needed for original batteries. Display and logo must not alter BatteryMonitor; existing modifications are retained.",
      blocked: "Wi-Fi channel recovery and long-press reboot: not qualified for installation yet.",
      saved: "Plan saved. No radio was changed.", busy: "Please wait …",
      failed: "Not saved. Reload the selection and check the prerequisites.", empty: "No unprotected radio is stored.",
    },
  };
  const text = key => (copy[document.documentElement.lang.split("-")[0]] || copy.en)[key] || copy.en[key] || key;
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const state = {open: false, busy: false, devices: [], device: "", selection: null, message: ""};
  const active = () => document.body.classList.contains("lab-mode");
  async function request(path, options = {}) {
    const response = await (window.BasswiesnAuth?.fetch || window.fetch)("/api/lab/radio-patches" + path, {cache: "no-store", ...options});
    const result = await response.json();
    if (!response.ok) throw new Error("PATCH_REQUEST_FAILED");
    return result;
  }
  const select = (key, values, chosen) => `<label>${escape(text(key))}<select id="patch-${key}" ${state.busy ? "disabled" : ""}>${values.map(v => `<option value="${v}" ${v === chosen ? "selected" : ""}>${escape(text(v))}</option>`).join("")}</select></label>`;
  function render() {
    panel.setAttribute("aria-busy", String(state.busy));
    panel.innerHTML = `<h3>${escape(text("title"))}</h3><p>${escape(text("note"))}</p><button type="button" class="command" id="patch-open" ${state.busy ? "disabled" : ""}>${escape(text("open"))}</button>
      <p role="status" aria-live="polite">${escape(state.busy ? text("busy") : state.message ? text(state.message) : "")}</p>
      <details><summary>${escape(text("help"))}</summary>${["display_help", "bootlogo_help", "battery_help", "blocked"].map(k => `<p>${escape(text(k))}</p>`).join("")}</details>`;
    if (!state.open) return;
    if (!state.devices.length) { panel.insertAdjacentHTML("beforeend", `<p>${escape(text("empty"))}</p>`); return; }
    panel.insertAdjacentHTML("beforeend", `<label>${escape(text("radio"))}<select id="patch-device" ${state.busy ? "disabled" : ""}>${state.devices.map(d => `<option value="${escape(d.device_id)}" ${state.device === d.device_id ? "selected" : ""}>${escape(d.name)}</option>`).join("")}</select></label>`);
    if (state.selection) panel.insertAdjacentHTML("beforeend", `<div class="settings-form">${["display","bootlogo","battery"].map(k => select(k,["keep","enable","disable"],state.selection[k])).join("")}${select("battery_kind",["unknown","original","compatible_replacement"],state.selection.battery_kind)}</div><button type="button" class="command" id="patch-save" ${state.busy ? "disabled" : ""}>${escape(text("save"))}</button>`);
  }
  async function run(action) {
    if (state.busy || !active()) return;
    state.busy = true; state.message = ""; render();
    try { await action(); } catch { state.message = "failed"; }
    finally { state.busy = false; if (!active()) { state.open = false; state.selection = null; } render(); }
  }
  const load = async () => { state.selection = await request(`/${encodeURIComponent(state.device)}/selection`); };
  panel.addEventListener("change", e => {
    if (state.busy || !active()) return;
    if (e.target.id === "patch-device") {
      state.device = e.target.value; state.selection = null; run(load);
    } else if (state.selection && ["patch-display","patch-bootlogo","patch-battery","patch-battery_kind"].includes(e.target.id)) {
      state.selection[e.target.id.slice(6)] = e.target.value;
    }
  });
  panel.addEventListener("click", e => {
    const id = e.target.closest("button")?.id;
    if (id === "patch-open") run(async () => {
      state.devices = (await request("")).devices; state.open = true;
      state.device = state.devices.some(d => d.device_id === state.device) ? state.device : state.devices[0]?.device_id || "";
      if (state.device) await load();
    });
    if (id === "patch-save" && state.selection) run(async () => {
      const {display,bootlogo,battery,battery_kind,revision} = state.selection;
      await request(`/${encodeURIComponent(state.device)}/selection`, {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({display,bootlogo,battery,battery_kind,revision})});
      await load(); state.message = "saved";
    });
  });
  new MutationObserver(render).observe(document.documentElement,{attributes:true,attributeFilter:["lang"]});
  new MutationObserver(() => { if (!active()) { state.open=false; state.selection=null; render(); } }).observe(document.body,{attributes:true,attributeFilter:["class"]});
  render();
})();
