/** Target-bound, cancellable reads. This module never performs a radio write. */
export function createPresetReader({ getJson, selectedDeviceId, commit }) {
  let generation = 0;
  let controller;

  function cancel() {
    generation += 1;
    controller?.abort();
  }

  async function load(deviceId, probe = false) {
    cancel();
    const requestGeneration = generation;
    controller = new AbortController();
    const { signal } = controller;
    const current = () => !signal.aborted && requestGeneration === generation
      && selectedDeviceId() === deviceId;
    if (!current()) return;
    // Do not leave the previous radio's slots actionable while reading this one.
    commit([], null);
    if (!deviceId) return;

    const base = `/api/presets/${encodeURIComponent(deviceId)}`;
    let presets = [];
    let status = null;
    let failure;
    try {
      const value = await getJson(base, { signal });
      if (Array.isArray(value)) presets = value;
    } catch { /* A cached-slot failure must not manufacture a successful status. */ }
    if (!current()) return;
    try {
      status = await getJson(`${base}/status${probe === true ? "?probe=true" : ""}`, { signal });
      if (status?.device_id && status.device_id !== deviceId) {
        status = null;
        throw new Error("Preset response identity mismatch");
      }
    } catch (error) {
      failure = error;
    }
    if (!current()) return;
    commit(presets, status);
    if (failure && probe === true) throw failure;
  }

  return { load, cancel };
}
