/** Cached health projections, bound to one target and one request generation.
 * No discovery, probes or writes. Aborting a stale GET is only an optimization:
 * the generation/identity check also rejects transports that ignore abort.
 */
const endpoints = [
  ["playback", "playback-health", {}],
  ["provider", "provider-health", { providers: [] }],
  ["metadata", "metadata", { stale: true }],
  ["artwork", "artwork", { public_url: "/static/bmx-icons/orion/monochrome.svg" }],
  ["restrictions", "restrictions", { restrictions: [] }],
  ["reporting", "reporting", { providers: [] }],
  ["airplay", "airplay-readiness", { label: "UNKNOWN" }],
  ["timeline", "diagnostics/timeline?limit=100", { items: [] }],
  ["clock", "metadata/clock", { enabled: false, mode: "MISSING_TITLE", interval_seconds: 60 }],
];

export function emptyHealth(deviceId) {
  return { deviceId, loading: Boolean(deviceId),
    ...Object.fromEntries(endpoints.map(([key]) => [key, null])) };
}

export function createHealthReader({ getJson, selectedDeviceId, commit }) {
  let generation = 0;
  let controller;
  function cancel() {
    generation += 1;
    controller?.abort();
  }
  async function load(deviceId) {
    cancel();
    const epoch = generation;
    controller = new AbortController();
    const { signal } = controller;
    const current = () => !signal.aborted && epoch === generation && selectedDeviceId() === deviceId;
    if (!current()) return;
    commit(emptyHealth(deviceId));
    if (!deviceId) return;
    const base = `/api/devices/${encodeURIComponent(deviceId)}/`;
    const values = await Promise.all(endpoints.map(async ([key, suffix, fallback]) => {
      try {
        const value = await getJson(base + suffix, { signal });
        if (!value || typeof value !== "object" || Array.isArray(value)
            || ("device_id" in value && value.device_id !== deviceId)) {
          throw new Error("HEALTH_RESPONSE_INVALID");
        }
        return [key, value];
      } catch {
        return [key, { ...fallback, state: "ERROR", error: "HEALTH_READ_UNAVAILABLE" }];
      }
    }));
    if (current()) commit({ deviceId, loading: false, ...Object.fromEntries(values) });
  }
  return { load, cancel };
}
