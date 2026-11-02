// Explicit LAB requests only. Initialization never contacts a radio.
// Server identity/profile/ownership remain the security boundary. Browser state
// prevents accidental repeats, but is not evidence of hardware completion.
export function createManualRadioController({ getJson, postJson, copy, showApiError, devices }) {
  const blocked = new Set();
  const bindings = [];

  function refresh() {
    for (const binding of bindings) binding.refresh();
  }

  function bind(formId, outputId, kind) {
    const form = document.getElementById(formId);
    const output = document.getElementById(outputId);
    if (!form || !output) return;
    const select = form.elements.device_id;
    let busy = false;
    const results = new Map();
    output.setAttribute("aria-live", "polite");
    if (kind === "wifi") {
      const label = document.createElement("label");
      label.className = "toggle-line";
      const preview = document.createElement("input");
      preview.type = "checkbox";
      preview.name = "dry_run";
      preview.checked = true;
      label.append(preview, document.createTextNode(copy("Nur Vorschau", "Preview only")));
      form.insertBefore(label, form.querySelector(".button-row"));
      form.elements.understood.required = false; // Required for execution, not preview.
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "command";
    reset.dataset.manualReview = "true";
    reset.hidden = true;
    form.append(reset);

    function render(deviceId, result) {
      results.set(deviceId, result);
      if (select.value !== deviceId) return; // A late response never labels another selected radio.
      const heading = result.dry_run ? copy("Vorschau — nichts geändert", "Preview — nothing changed")
        : copy("Zustand nicht vollständig bestätigt — nicht blind wiederholen", "State not fully confirmed — do not retry blindly");
      output.textContent = `${heading}\n${deviceId}\n${JSON.stringify(result, null, 2)}`;
    }

    const binding = {
      refresh() {
        reset.textContent = copy("Nach manueller Prüfung: neue Aktion", "After manual review: new action");
        for (const button of form.querySelectorAll('button[type="submit"]')) {
          button.disabled = busy || blocked.has(select.value);
        }
        reset.hidden = !blocked.has(select.value);
        reset.disabled = busy;
      },
    };
    bindings.push(binding);
    select.addEventListener("change", () => {
      if (results.has(select.value)) render(select.value, results.get(select.value));
      else output.textContent = "";
      refresh();
    });
    reset.addEventListener("click", () => {
      if (busy || !window.confirm(copy(
        "Hast du den tatsächlichen Radiozustand geprüft? Dies bestätigt keine Speicherung und wiederholt keinen Befehl.",
        "Have you checked the actual radio state? This does not certify persistence or repeat a command."))) return;
      blocked.delete(select.value);
      refresh();
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const deviceId = String(data.get("device_id") || "");
      if (!deviceId || busy || blocked.has(deviceId)) return;
      const device = devices().find((item) => item.device_id === deviceId);
      if (!device) return;
      const preview = data.get("dry_run") === "on";
      if (!preview && kind === "wifi" && data.get("understood") !== "on") return;
      if (!preview && !window.confirm(`${device.name || deviceId}\n${copy(
        "Das Radio kann die Wiedergabe starten oder das WLAN verlassen. Manuelle Wiederherstellung kann nötig sein. Einmal ausführen?",
        "The radio may start playback or leave Wi-Fi. Manual recovery may be necessary. Execute once?")}`)) return;
      const payload = { dry_run: preview };
      if (kind === "wifi") Object.assign(payload, {
        ssid: data.get("ssid"), password: data.get("password"), security_type: data.get("security_type"),
      });
      else Object.assign(payload, {
        source: data.get("source"), sourceAccount: data.get("sourceAccount"),
        token: data.get("token"), name: data.get("name"),
      });
      if (!preview) {
        // Insecure HTTP origins may lack randomUUID; getRandomValues remains
        // available. No random fallback or predictable operation key.
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        payload.operation_id = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
        payload.confirmation = "YES";
        payload.acknowledge_manual_recovery = true;
      }
      busy = true;
      const controls = Array.from(form.querySelectorAll("input, select, button"));
      const disabled = controls.map((control) => control.disabled);
      controls.forEach((control) => { control.disabled = true; });
      form.setAttribute("aria-busy", "true");
      const endpoint = kind === "wifi" ? "wireless-profiles" : "station/add-native";
      try {
        const result = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/${endpoint}`, payload);
        if (!preview) blocked.add(deviceId);
        render(deviceId, result);
      } catch (error) {
        if (!preview) blocked.add(deviceId); // A lost HTTP response is not a failed radio write.
        render(deviceId, error.payload?.detail || error.payload || { status: "OUTCOME_UNKNOWN", code: error.code || "REQUEST_FAILED" });
        showApiError(error);
      } finally {
        if (form.elements.password) form.elements.password.value = "";
        if (form.elements.token) form.elements.token.value = "";
        delete payload.password;
        delete payload.token;
        controls.forEach((control, index) => { control.disabled = disabled[index]; });
        busy = false;
        form.removeAttribute("aria-busy");
        refresh();
      }
    });
    form.dataset.manualControllerReady = "true";
    binding.refresh();
  }

  bind("wireless-profile-form", "wireless-profile-output", "wifi");
  bind("native-station-add-form", "native-station-output", "station");
  const read = document.getElementById("wireless-profile-read");
  read?.addEventListener("click", async () => {
    const select = document.getElementById("wireless-profile-device");
    const id = select?.value;
    if (!id || read.disabled) return;
    read.disabled = true;
    try {
      const result = await getJson(`/api/devices/${encodeURIComponent(id)}/wireless-profiles`);
      if (select.value === id) document.getElementById("wireless-profile-output").textContent = JSON.stringify(result, null, 2);
      // Observed SSID/count alone does not clear an uncertain write lock.
    } catch (error) { showApiError(error); }
    finally { read.disabled = false; }
  });
  return { refresh };
}
