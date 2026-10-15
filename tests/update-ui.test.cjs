const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/dyspark/gui_static/updates.js"), "utf8");

async function controls(info, failure) {
  const elements = new Map();
  const calls = [];
  const document = { hidden: false, querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      textContent: "", disabled: false, listeners: {},
      addEventListener(name, listener) { this.listeners[name] = listener; },
    });
    return elements.get(selector);
  }};
  const window = { confirm: () => true, addEventListener() {} };
  vm.runInNewContext(source, {
    document, window, AbortController, Date, setTimeout: () => 1, clearTimeout() {},
  });
  window.createUpdateControls({ async api(url, options) {
    calls.push({ url, options });
    if (options?.method === "POST" && failure) throw new Error(failure);
    return info;
  }});
  const settle = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
  await settle();
  return { elements, window, calls, settle };
}

const available = {
  current: "a".repeat(40), latest: "c".repeat(40), phase: "idle",
  managed: true, can_update: true, summary: '<img src=x onerror="bad()">',
};

test("remote metadata remains text and unmanaged installations cannot apply", async () => {
  const ui = await controls({ ...available, managed: false, can_update: false });
  assert.equal(ui.elements.get("#update-summary").textContent, '<img src=x onerror="bad()">');
  assert.equal(ui.elements.get("#update-apply").disabled, true);
});

test("cancelling confirmation does not send an update request", async () => {
  const ui = await controls(available);
  ui.window.confirm = () => false;
  ui.elements.get("#update-apply").listeners.click();
  await ui.settle();
  assert.equal(ui.calls.filter(call => call.options?.method === "POST").length, 0);
});

test("busy-task refusal stays visible after polling refresh", async () => {
  const ui = await controls(available, "已有发送任务，请等待结束");
  ui.elements.get("#update-apply").listeners.click();
  await ui.settle();
  assert.match(ui.elements.get("#update-error").textContent, /已有发送任务/);
});

test("maintenance disables actions and shows truthful failed recovery", async () => {
  const pending = await controls({ ...available, phase: "preparing", can_update: false });
  assert.equal(pending.elements.get("#update-apply").disabled, true);
  assert.equal(pending.elements.get("#update-check").disabled, true);
  const failed = await controls({ ...available, phase: "recovery_failed", can_update: false });
  assert.match(failed.elements.get("#update-progress").textContent, /旧版恢复失败/);
  assert.equal(failed.elements.get("#update-apply").disabled, true);
});
