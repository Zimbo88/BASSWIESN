import assert from "node:assert/strict";
import test from "node:test";
import { createHealthReader, emptyHealth } from "../../basswiesn/app/static/js/health-reader.mjs";

test("old target cannot overwrite current health, even if GET ignores abort", async () => {
  let selected = "A";
  const held = [], commits = [], signals = [];
  const reader = createHealthReader({ selectedDeviceId: () => selected,
    commit: value => commits.push(value), getJson: (url, {signal}) => {
      signals.push(signal);
      if (url.includes("/A/")) return new Promise(resolve => held.push(resolve));
      return Promise.resolve({device_id: "B", state: "B"});
    }});
  const first = reader.load("A");
  assert.equal(commits.at(-1).metadata, null);
  selected = "B";
  await reader.load("B");
  assert.equal(signals[0].aborted, true);
  const final = commits.at(-1);
  held.forEach(resolve => resolve({device_id: "A", state: "A"}));
  await first;
  assert.equal(commits.at(-1), final);
  assert.equal(final.metadata.state, "B");
  assert.equal(final.loading, false);
});

test("new request of same target supersedes old generation", async () => {
  const held = [], commits = [];
  let old = true;
  const reader = createHealthReader({selectedDeviceId: () => "A", commit: x => commits.push(x),
    getJson: () => old ? new Promise(resolve => held.push(resolve)) : Promise.resolve({state:"NEW"})});
  const first = reader.load("A"); old = false;
  await reader.load("A");
  held.forEach(resolve => resolve({state:"OLD"})); await first;
  assert.equal(commits.at(-1).playback.state, "NEW");
});

test("empty selection clears projection and sends nothing", async () => {
  const commits = [];
  await createHealthReader({selectedDeviceId: () => "", commit: x => commits.push(x),
    getJson: () => assert.fail("no request permitted")}).load("");
  assert.deepEqual(commits, [emptyHealth("")]);
});

test("invalid or mismatched bodies remain errors, never another device's health", async () => {
  for (const body of [null, [], "ok", {device_id:"B", state:"HEALTHY"}]) {
    const commits = [];
    await createHealthReader({selectedDeviceId: () => "A", commit: x => commits.push(x),
      getJson: async () => body}).load("A");
    assert.equal(commits.at(-1).playback.state, "ERROR");
    assert.equal(commits.at(-1).loading, false);
  }
});

test("a failed health plane does not discard another plane or expose exceptions", async () => {
  const commits = [];
  await createHealthReader({selectedDeviceId: () => "A", commit: x => commits.push(x),
    getJson: async url => {
      if (url.endsWith("/metadata")) throw new Error("SYNTHETIC_SECRET");
      return {state:"KNOWN", custom_field:"preserved"};
    }}).load("A");
  assert.equal(commits.at(-1).metadata.state, "ERROR");
  assert.equal(commits.at(-1).provider.custom_field, "preserved");
  assert.equal(JSON.stringify(commits).includes("SYNTHETIC_SECRET"), false);
});
