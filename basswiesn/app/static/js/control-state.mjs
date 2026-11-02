/**
 * One explicit control action, followed by one identity-bound observation.
 * A lost command reply is not proof of failure: never replay the action.
 * This coordinator is a UI barrier, not a replacement for server write guards.
 * Its uncertainty memory lasts for this document; the backend owns durable
 * operation records and must enforce safety across tabs and restarts.
 *
 * @typedef {{device_id: string, verified: boolean, volume: number,
 *   source?: string, play_status?: string, now_playing?: object}} Readback
 * @typedef {{kind: string, deviceId: string, result?: unknown,
 *   readback?: Readback, error?: unknown, actionError?: unknown}} Outcome
 */

/** @param {{selectedDeviceId: () => string, allowed: (id: string) => boolean,
 * readState: (id: string, options: {signal: AbortSignal}) => Promise<Readback>,
 * commit: (outcome: Outcome) => void}} ports */
export function createControlState(ports) {
  let busy = false;
  let generation = 0;
  /** @type {AbortController | undefined} */
  let readController;
  const uncertain = new Set();

  function invalidate() {
    generation += 1;
    // Only GET has a cancellation signal. Navigation cannot undo a POST.
    readController?.abort();
  }

  /** @param {string} deviceId */
  function blocked(deviceId) {
    return uncertain.has(deviceId);
  }

  /** @param {string} deviceId
   * @param {((id: string) => Promise<unknown>) | undefined} action
   * @returns {Promise<Outcome>} */
  async function run(deviceId, action) {
    const epoch = generation;
    const current = () => generation === epoch
      && ports.selectedDeviceId() === deviceId && ports.allowed(deviceId);
    if (busy || !deviceId || !current()) return { kind: "skipped", deviceId };
    if (action && blocked(deviceId)) {
      const outcome = { kind: "blocked", deviceId };
      ports.commit(outcome);
      return outcome;
    }
    busy = true;
    let result;
    let actionError;
    let actionFailed = false;
    const attempted = Boolean(action);
    try {
      if (action) {
        // Mark before await: selection changes, aborts and thrown non-Error
        // values must not accidentally clear the need for reconciliation.
        uncertain.add(deviceId);
        try {
          result = await action(deviceId);
          if (!result || typeof result !== "object" || Array.isArray(result)
              || ("device_id" in result && result.device_id !== deviceId)
              || ("verified" in result && result.verified !== true)
              || ("dry_run" in result && result.dry_run !== false)
              || ("ok" in result && result.ok === false)) {
            throw new Error("invalid_command_receipt");
          }
        }
        catch (error) { actionFailed = true; actionError = error; }
      }
      if (!current()) return { kind: "stale", deviceId };
      readController = new AbortController();
      const readback = await ports.readState(deviceId, { signal: readController.signal });
      if (!current()) return { kind: "stale", deviceId };
      if (!readback || readback.verified !== true || readback.device_id !== deviceId
          || !Number.isInteger(readback.volume) || readback.volume < 0 || readback.volume > 100) {
        throw new Error("invalid_radio_readback");
      }
      // Reading a current state cannot prove which action caused it. The user
      // explicitly reloads after uncertainty before another command is enabled.
      const outcome = { kind: actionFailed ? "uncertain" : attempted ? "observed" : "read",
        deviceId, result, readback, actionError };
      ports.commit(outcome);
      if (!actionFailed) uncertain.delete(deviceId);
      return outcome;
    } catch (error) {
      if (!current()) return { kind: "stale", deviceId };
      const outcome = { kind: attempted ? "readback_failed" : "read_failed",
        deviceId, result, error, actionError };
      ports.commit(outcome);
      return outcome;
    } finally {
      busy = false;
      readController = undefined;
    }
  }

  return { run, invalidate, blocked };
}
