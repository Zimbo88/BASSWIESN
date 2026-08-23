const shell = document.querySelector(".remote-shell");
const deviceId = shell.dataset.deviceId;
const output = document.getElementById("remote-output");
const volume = document.getElementById("remote-volume");
const volumeLabel = document.getElementById("remote-volume-label");
let stations = [];

function writeResult(value) {
  output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function postJson(url, data) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function setVolumeLabel() {
  volumeLabel.textContent = `${volume.value}%`;
}

async function sendKey(key) {
  writeResult(await postJson(`/api/devices/${encodeURIComponent(deviceId)}/key`, { key, safe_volume: Number(volume.value) }));
}

async function setVolume(value) {
  volume.value = Math.max(0, Math.min(100, Number(value || 0)));
  setVolumeLabel();
  writeResult(await postJson(`/api/devices/${encodeURIComponent(deviceId)}/settings/volume`, { value: Number(volume.value), dry_run: false }));
}

async function refresh() {
  const [devices, stationRows, settings, health] = await Promise.all([getJson("/api/devices"), getJson("/api/stations"), getJson("/api/system/settings"), getJson("/api/health")]);
  const version = typeof health.version === "string" && health.version ? (health.version.startsWith("v") ? health.version : `v${health.version}`) : "Version nicht verfügbar";
  document.getElementById("remote-version").textContent = `basswiesn remote · ${version}`;
  volume.value = settings.safe_startup_volume ?? 30;
  setVolumeLabel();
  stations = stationRows;
  const device = devices.find((item) => item.device_id === deviceId) || {};
  document.getElementById("remote-title").textContent = device.name || deviceId;
  const stationSelect = document.getElementById("remote-station");
  stationSelect.innerHTML = stations.length
    ? stations.map((station) => `<option value="${station.id}">${station.name}</option>`).join("")
    : `<option value="">No stations</option>`;
  const presets = await getJson(`/api/presets/${encodeURIComponent(deviceId)}`).catch(() => []);
  document.getElementById("remote-presets").innerHTML = [1, 2, 3, 4, 5, 6].map((slot) => {
    const preset = presets.find((item) => Number(item.button) === slot);
    const label = preset?.station_name || `Preset ${slot}`;
    return `<button data-preset="${slot}">${label}</button>`;
  }).join("");
  const now = await postJson(`/api/devices/${encodeURIComponent(deviceId)}/telemetry/probe`, { endpoint: "/now_playing", dry_run: false }).catch((error) => ({ error: String(error) }));
  document.getElementById("remote-now").textContent = now.summary || now.error || "No now playing data";
}

document.querySelectorAll("[data-key]").forEach((button) => {
  button.addEventListener("click", () => sendKey(button.dataset.key).catch((error) => writeResult(String(error))));
});

document.querySelectorAll("[data-volume-step]").forEach((button) => {
  button.addEventListener("click", () => setVolume(Number(volume.value) + Number(button.dataset.volumeStep)).catch((error) => writeResult(String(error))));
});

volume.addEventListener("input", setVolumeLabel);
volume.addEventListener("change", () => setVolume(volume.value).catch((error) => writeResult(String(error))));

document.getElementById("remote-presets").addEventListener("click", (event) => {
  const button = event.target.closest("[data-preset]");
  if (!button) return;
  sendKey(`PRESET_${button.dataset.preset}`).catch((error) => writeResult(String(error)));
});

document.getElementById("remote-play-station").addEventListener("click", async () => {
  const stationId = document.getElementById("remote-station").value;
  if (!stationId) return;
  await setVolume(volume.value || 5);
  writeResult(await postJson(`/api/devices/${encodeURIComponent(deviceId)}/stations/${encodeURIComponent(stationId)}/play`, { dry_run: false }));
});

setVolumeLabel();
refresh().catch((error) => writeResult(String(error)));
