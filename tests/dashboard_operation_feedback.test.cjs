"use strict";
// Actual dashboard script in an inert DOM/fetch fixture. Not live browser proof.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../src/engineering_control/dashboard/static/dashboard.js"), "utf8");
const id = "adfbb0a9-089f-4264-87ea-2d3ea93c4202";

function fixture({state = "PENDING", admission = "SYNTHETIC_ONLY", lost = false} = {}) {
  const nodes = new Map();
  const calls = [];
  const counters = {reload: 0, close: 0, reauth: 0, execute: 0, get: 0};
  let getStatus = 200;
  let identity = id;
  let currentState = state;
  const getNode = (name) => {
    if (!nodes.has(name)) nodes.set(name, {value: "", textContent: "", required: false, disabled: false,
      classList: {toggle() {}}, addEventListener() {}, focus() {}, showModal() {},
      close() { counters.close++; }});
    return nodes.get(name);
  };
  const result = () => ({operation_id: identity, status: currentState, dispatch_observation: {status: admission}});
  const response = (value, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => value});
  const context = vm.createContext({
    AbortController, setTimeout, clearTimeout,
    document: {getElementById: getNode, body: {dataset: {csrfToken: "fixture-csrf-old"}},
      querySelectorAll: () => [], addEventListener() {}},
    window: {setTimeout: (callback) => { callback(); return 1; }},
    location: {reload() { counters.reload++; }, assign() { counters.reload++; }},
    fetch: async (url, options = {}) => {
      calls.push({url, method: options.method || "GET", body: options.body});
      if (url.endsWith("/candidates")) return response({candidate_id: "fixture-candidate", capability: {status: "AVAILABLE"}, operation_binding: {}});
      if (url.endsWith("/reauthenticate")) {
        counters.reauth++;
        return response({session_rotated: true, csrf_token: "fixture-new-csrf-0123456789", reauthentication_token: "fixture-proof", expires_at: new Date(Date.now() + 60000).toISOString()});
      }
      if (url.endsWith("/execute")) {
        counters.execute++;
        if (lost && counters.execute === 1) throw new Error("synthetic lost response after acceptance");
        return response(result(), 202);
      }
      assert.equal(url, `/api/dashboard/operations/${id}`);
      assert.equal(options.method || "GET", "GET");
      counters.get++;
      return response(result(), getStatus);
    },
  });
  vm.runInContext(source, context);
  return {context, nodes, counters, calls, getNode,
    prepare: () => context.prepareAction({action: "refresh_connections", scope: "all"}),
    confirm: () => context.confirmAction({preventDefault() {}}),
    state: (value) => { currentState = value; },
    failGet: () => { getStatus = 503; },
    wrongIdentity: () => { identity = "00000000-0000-0000-0000-000000000001"; },
  };
}

for (const admission of ["SYNTHETIC_ONLY", "DISPATCHER_STOPPED", "DISPATCHING", "UNKNOWN", "invalid"]) {
  test(`pending ${admission} is explicit and remains on the same receipt`, async () => {
    const f = fixture({admission});
    await f.prepare(); await f.confirm();
    assert.equal(f.counters.close, 0); assert.equal(f.counters.reload, 0);
    assert.match(f.getNode("action-progress").textContent, new RegExp(id));
    assert.match(f.getNode("action-progress").textContent, /PENDING/);
    assert.equal(f.getNode("action-execute").textContent, "Check operation status");
    assert.equal(f.getNode("action-cancel").textContent, "Close (does not cancel)");
    // Once a receipt is known, no reauthentication/POST or expiring proof is needed.
    vm.runInContext("if (pendingExecution !== null) throw new Error('proof retained')", f.context);
    await f.confirm();
    assert.equal(f.counters.execute, 1); assert.equal(f.counters.reauth, 1);
    assert.ok(f.counters.get > 0);
  });
}

test("claimed operation is not described as unstarted when maintenance changes", async () => {
  const f = fixture({state: "DISPATCHING"});
  await f.prepare(); await f.confirm();
  assert.match(f.getNode("action-progress").textContent, /claimed; completion is not yet confirmed/);
  assert.doesNotMatch(f.getNode("action-progress").textContent, /has not started/);
  assert.equal(f.counters.close, 0);
});

test("lost receipt retries the exact body, then switches to GET-only observation", async () => {
  const f = fixture({lost: true});
  await f.prepare(); await f.confirm();
  assert.equal(f.getNode("action-execute").textContent, "Retry same operation");
  await f.confirm(); await f.confirm();
  const writes = f.calls.filter((call) => call.url.endsWith("/execute"));
  assert.equal(writes.length, 2); assert.equal(writes[0].body, writes[1].body);
  assert.equal(f.counters.reauth, 1); assert.ok(f.counters.get > 0);
  assert.equal(f.counters.reload, 0);
});

for (const terminal of ["COMPLETED", "STARTED", "FAILED", "BLOCKED", "UNRECOGNIZED"]) {
  test(`same receipt transitions to ${terminal} without another submit`, async () => {
    const f = fixture(); await f.prepare(); await f.confirm();
    f.state(terminal); await f.confirm();
    assert.equal(f.counters.execute, 1); assert.equal(f.counters.reauth, 1);
    if (["COMPLETED", "STARTED"].includes(terminal)) assert.equal(f.counters.reload, 1);
    else {
      assert.equal(f.counters.close, 0);
      assert.match(f.getNode("action-blockers").textContent, new RegExp(id));
    }
  });
}

for (const failure of ["failGet", "wrongIdentity"]) {
  test(`${failure} does not invent completion or resubmit`, async () => {
    const f = fixture(); await f.prepare(); await f.confirm();
    f[failure](); await f.confirm();
    assert.equal(f.counters.execute, 1); assert.equal(f.counters.close, 0);
    assert.match(f.getNode("action-blockers").textContent, /unknown|not confirmed/);
    assert.equal(f.getNode("action-execute").textContent, "Check operation status");
  });
}
