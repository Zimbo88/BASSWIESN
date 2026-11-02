import assert from "node:assert/strict";
import test from "node:test";
import { createControlState } from "../../basswiesn/app/static/js/control-state.mjs";

const snapshot = (id = "A", volume = 23) => ({ device_id: id, volume, verified: true });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
function fixture(read = async id => snapshot(id)) {
  const state = { target: "A", protected: false, events: [], reads: [] };
  const controller = createControlState({
    selectedDeviceId: () => state.target,
    allowed: () => !state.protected,
    readState: (id, options) => { state.reads.push({ id, ...options }); return read(id, options); },
    commit: event => state.events.push(event),
  });
  return { state, controller };
}

test("constructing and invalidating the UI never discovers or contacts a radio", () => {
  const { controller, state } = fixture();
  controller.invalidate();
  assert.deepEqual(state.reads, []);
  assert.deepEqual(state.events, []);
});

test("explicit read is identity-bound and does not manufacture a sent command", async () => {
  const { controller, state } = fixture();
  assert.equal((await controller.run("A")).kind, "read");
  assert.equal(state.reads.length, 1);
  assert.equal(state.events[0].readback.volume, 23);
});

test("command is attempted once, then an actual observation is shown", async () => {
  let writes = 0;
  const { controller, state } = fixture();
  const outcome = await controller.run("A", async id => { writes++; return { device_id: id }; });
  assert.equal(outcome.kind, "observed");
  assert.equal(writes, 1);
  assert.equal(state.reads.length, 1);
  assert.equal(controller.blocked("A"), false);
});

test("lost POST reply triggers GET only and requires an explicit reload before more writes", async () => {
  let writes = 0;
  const { controller, state } = fixture();
  const write = async () => { writes++; throw new Error("reply lost"); };
  assert.equal((await controller.run("A", write)).kind, "uncertain");
  assert.equal(state.events[0].readback.volume, 23);
  assert.equal(controller.blocked("A"), true);
  assert.equal((await controller.run("A", write)).kind, "blocked");
  assert.equal(writes, 1);
  assert.equal(state.reads.length, 1);
  assert.equal((await controller.run("A")).kind, "read");
  assert.equal(controller.blocked("A"), false);
  assert.equal(writes, 1);
});

test("undefined rejection is still an uncertain write", async () => {
  const { controller } = fixture();
  const outcome = await controller.run("A", async () => Promise.reject(undefined));
  assert.equal(outcome.kind, "uncertain");
  assert.equal(controller.blocked("A"), true);
});

for (const receipt of [null, undefined, [], "OK", { device_id: "B" }, { verified: false },
  { verified: "true" }, { verified: null }, { dry_run: true }, { dry_run: "false" }, { ok: false }]) {
  test(`unsafe command receipt ${JSON.stringify(receipt)} cannot become success`, async () => {
    const { controller, state } = fixture();
    assert.equal((await controller.run("A", async () => receipt)).kind, "uncertain");
    assert.equal(state.reads.length, 1);
    assert.equal(controller.blocked("A"), true);
  });
}

for (const response of [null, {}, snapshot("B"), { ...snapshot(), verified: false },
  snapshot("A", -1), snapshot("A", 101), snapshot("A", "23"), snapshot("A", 2.5)]) {
  test(`invalid readback ${JSON.stringify(response)} leaves the write barrier in place`, async () => {
    const { controller } = fixture(async () => response);
    assert.equal((await controller.run("A", async () => ({}))).kind, "readback_failed");
    assert.equal(controller.blocked("A"), true);
    assert.equal((await controller.run("A")).kind, "read_failed");
    assert.equal(controller.blocked("A"), true);
  });
}

test("readback timeout does not replay the write or release uncertainty", async () => {
  let writes = 0;
  const { controller, state } = fixture(async () => { throw new Error("timeout"); });
  assert.equal((await controller.run("A", async () => { writes++; })).kind, "readback_failed");
  assert.equal((await controller.run("A")).kind, "read_failed");
  assert.equal((await controller.run("A", async () => { writes++; })).kind, "blocked");
  assert.equal(writes, 1);
  assert.equal(state.reads.length, 2);
});

test("in-flight command serializes subsequent clicks and is never cancelled", async () => {
  const pending = deferred();
  const { controller, state } = fixture();
  let writes = 0;
  const first = controller.run("A", id => { assert.equal(id, "A"); writes++; return pending.promise; });
  assert.equal((await controller.run("A", async () => { writes++; })).kind, "skipped");
  controller.invalidate();
  pending.resolve({});
  assert.equal((await first).kind, "stale");
  assert.equal(writes, 1);
  assert.equal(state.reads.length, 0);
  assert.equal(controller.blocked("A"), true);
  await controller.run("A");
  assert.equal(controller.blocked("A"), false);
});

test("A to B to A cannot resurrect a stale response even if GET ignores abort", async () => {
  const pending = deferred();
  const { controller, state } = fixture(() => pending.promise);
  const first = controller.run("A");
  state.target = "B";
  controller.invalidate();
  state.target = "A";
  controller.invalidate();
  pending.resolve(snapshot());
  assert.equal((await first).kind, "stale");
  assert.equal(state.reads[0].signal.aborted, true);
  assert.equal(state.events.length, 0);
});

test("changed selection without an event cannot receive another device's result", async () => {
  const pending = deferred();
  const { controller, state } = fixture(() => pending.promise);
  const first = controller.run("A");
  state.target = "B";
  pending.resolve(snapshot());
  assert.equal((await first).kind, "stale");
  assert.equal(state.events.length, 0);
});

test("protected or absent targets never invoke the write or read port", async () => {
  const { controller, state } = fixture();
  let writes = 0;
  const write = async () => { writes++; };
  assert.equal((await controller.run("", write)).kind, "skipped");
  assert.equal((await controller.run("B", write)).kind, "skipped");
  state.protected = true;
  assert.equal((await controller.run("A", write)).kind, "skipped");
  assert.equal(writes, 0);
  assert.equal(state.reads.length, 0);
});

test("protection changes while a command is pending prevent a new read", async () => {
  const pending = deferred();
  const { controller, state } = fixture();
  const first = controller.run("A", () => pending.promise);
  state.protected = true;
  pending.resolve({});
  assert.equal((await first).kind, "stale");
  assert.equal(state.reads.length, 0);
  assert.equal(controller.blocked("A"), true);
});

test("uncertainty is per device; reading another radio cannot clear it", async () => {
  const { controller, state } = fixture();
  await controller.run("A", async () => { throw new Error("lost reply"); });
  state.target = "B";
  controller.invalidate();
  assert.equal((await controller.run("B")).kind, "read");
  assert.equal(controller.blocked("B"), false);
  assert.equal(controller.blocked("A"), true);
});

test("page departure handles GET rejection without an unhandled error or stale commit", async () => {
  const pending = deferred();
  const { controller, state } = fixture(() => pending.promise);
  const first = controller.run("A");
  controller.invalidate();
  pending.reject(new Error("page departed"));
  assert.equal((await first).kind, "stale");
  assert.equal(state.events.length, 0);
});
