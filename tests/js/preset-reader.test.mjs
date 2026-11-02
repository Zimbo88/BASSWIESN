import assert from "node:assert/strict";
import test from "node:test";
import { createPresetReader } from "../../basswiesn/app/static/js/preset-reader.mjs";

function harness(getJson) {
  const state = { target: "A", commits: [] };
  const reader = createPresetReader({ getJson, selectedDeviceId: () => state.target,
    commit: (...args) => state.commits.push(args) });
  return { state, reader };
}

test("change events are not live-probe approval", async () => {
  const requests = [];
  const { reader, state } = harness(async (url) => { requests.push(url); return []; });
  await reader.load("A", { type: "change" });
  assert.deepEqual(requests, ["/api/presets/A", "/api/presets/A/status"]);
  assert.deepEqual(state.commits[0], [[], null]);
  await reader.load("A", true);
  assert.equal(requests.at(-1), "/api/presets/A/status?probe=true");
});

test("cancellation works even if transport ignores AbortSignal", async () => {
  let release;
  const requests = [];
  const { reader, state } = harness(async (url, { signal }) => {
    requests.push({ url, signal });
    if (url === "/api/presets/A") await new Promise(resolve => { release = resolve; });
    return url.includes("/status") ? { device_id: "B" } : [];
  });
  const old = reader.load("A");
  state.target = "B";
  await reader.load("B");
  release();
  await old;
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(requests.some(r => r.url === "/api/presets/A/status"), false);
  assert.deepEqual(state.commits.at(-1), [[], { device_id: "B" }]);
});

test("deselection clears the old view and makes no new request", async () => {
  let release;
  const { reader, state } = harness(() => new Promise(resolve => { release = resolve; }));
  const old = reader.load("A");
  state.target = "";
  await reader.load("");
  release([{ device_id: "A" }]);
  await old;
  assert.deepEqual(state.commits, [[[], null], [[], null]]);
});

test("page departure prevents late commit", async () => {
  let release;
  const { reader, state } = harness(() => new Promise(resolve => { release = resolve; }));
  const old = reader.load("A");
  reader.cancel();
  release([]);
  await old;
  assert.deepEqual(state.commits, [[[], null]]);
});

test("status identity mismatch cannot masquerade as successful probe", async () => {
  const { reader, state } = harness(async url => url.includes("/status") ? { device_id: "WRONG" } : []);
  await assert.rejects(reader.load("A", true), /identity mismatch/);
  assert.deepEqual(state.commits.at(-1), [[], null]);
});

test("only current explicit probe failures are surfaced; no replay", async () => {
  let calls = 0;
  const { reader, state } = harness(async () => { calls += 1; throw new Error("offline"); });
  await reader.load("A");
  assert.equal(calls, 2);
  await assert.rejects(reader.load("A", true), /offline/);
  assert.equal(calls, 4);
  assert.deepEqual(state.commits.at(-1), [[], null]);
});
