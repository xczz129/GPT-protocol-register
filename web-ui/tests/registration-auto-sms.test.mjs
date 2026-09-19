import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import ts from "typescript";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));

function execute(source, imports, globals = {}) {
  const exports = {};
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  runInNewContext(outputText, {
    exports, Error,
    require(name) { assert.ok(name in imports, `Unexpected import ${name}`); return imports[name]; },
    ...globals,
  });
  return exports;
}

function formHarness(config = {}, configError = null) {
  const source = read("../src/new/views/tasks/TaskForm.vue").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const sent = [], notices = [];
  const tasks = vue.reactive({ job: { running: false }, start: async (body) => { sent.push(plain(body)); } });
  const scope = vue.effectScope();
  const form = scope.run(() => execute(`${source}\nexport { form, submit, busy, loadSettings, settingsLoading, settingsError };`, {
    vue: { ...vue, onMounted() {} },
    "../../stores/tasks": { useTasksStore: () => tasks },
    "../../stores/groups": { useGroupsStore: () => ({ items: [] }) },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => notices.push(args) }) },
    "../../stores/status": { useStatusStore: () => ({ panel: {}, load: async () => {} }) },
    "../../api/config": { fetchConfig: async () => { if (configError) throw configError; return { config }; } },
    "../../../shared/mailRegistration": execute(read("../src/shared/mailRegistration.ts"), {}),
  }));
  return { ...form, tasks, sent, notices, dispose: () => scope.stop() };
}

test("auto SMS defaults off and submitting explicitly carries false", async () => {
  const h = formHarness();
  try {
    assert.equal(h.form.autoSmsAfterRegister, false);
    await h.submit();
    assert.equal(h.sent.length, 0, "wait for persisted settings before launching");
    await h.loadSettings();
    await h.submit();
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].auto_sms_after_register, false);
  } finally { h.dispose(); }
});

test("saved choice loads and can be explicitly disabled on the next run", async () => {
  const h = formHarness({ registration: { auto_sms_after_register: true } });
  try {
    await h.loadSettings();
    assert.equal(h.form.autoSmsAfterRegister, true);
    await h.submit();
    assert.equal(h.sent[0].auto_sms_after_register, true);
    h.form.autoSmsAfterRegister = false;
    await h.submit();
    assert.equal(h.sent[1].auto_sms_after_register, false);
  } finally { h.dispose(); }
});

test("an invalid legacy value never opts an account into a paid SMS flow", async () => {
  const h = formHarness({ registration: { auto_sms_after_register: "false" } });
  try {
    await h.loadSettings();
    assert.equal(h.form.autoSmsAfterRegister, false);
  } finally { h.dispose(); }
});

test("load failure blocks starting and exposes the concrete error", async () => {
  const h = formHarness({}, new Error("settings unavailable"));
  try {
    await h.loadSettings();
    await h.submit();
    assert.equal(h.settingsError.value, "settings unavailable");
    assert.equal(h.sent.length, 0);
    assert.equal(h.settingsLoading.value, false);
  } finally { h.dispose(); }
});

test("running and in-flight submissions cannot launch a second task", async () => {
  const h = formHarness();
  let finish;
  try {
    await h.loadSettings();
    h.tasks.job.running = true;
    await h.submit();
    assert.equal(h.sent.length, 0);
    h.tasks.job.running = false;
    h.tasks.start = (body) => { h.sent.push(plain(body)); return new Promise((resolve) => { finish = resolve; }); };
    const first = h.submit();
    await h.submit();
    assert.equal(h.sent.length, 1);
    finish();
    await first;
    assert.equal(h.busy.value, false);
  } finally { h.dispose(); }
});

function storeHarness(saveError = null) {
  const calls = [], timers = new Map(), snapshots = [], smsRefreshes = [];
  let nextTimer = 0;
  const client = {
    jsonBody: JSON.stringify,
    async api(path, options) {
      calls.push({ path, body: options?.body ? JSON.parse(options.body) : null });
      if (path === "/api/config" && saveError) throw saveError;
      if (path.startsWith("/api/tasks")) return { items: [], job: snapshots.shift() || { running: true } };
      return { ok: true };
    },
  };
  const tasksApi = execute(read("../src/new/api/tasks.ts"), { "./client": client });
  const configApi = execute(read("../src/new/api/config.ts"), { "./client": client });
  const status = { panel: {} };
  const store = execute(read("../src/new/stores/tasks.ts"), {
    vue, pinia: { defineStore: (_name, setup) => setup },
    "../api/tasks": tasksApi, "../api/config": configApi,
    "./status": { useStatusStore: () => status },
    "./batchops": { useBatchOpsStore: () => ({ refreshCodexStatus: async () => { smsRefreshes.push(true); } }) },
  }, {
    document: { hidden: false },
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout: (id) => timers.delete(id),
  }).useTasksStore();
  async function tick() {
    assert.equal(timers.size, 1);
    const [id, timer] = [...timers][0];
    timers.delete(id);
    await timer.fn();
    return timer.delay;
  }
  return { store, calls, snapshots, smsRefreshes, timers, tick };
}

for (const enabled of [false, true]) {
  test(`store persists only the registration choice and starts with auto SMS ${enabled}`, async () => {
    const h = storeHarness();
    const payload = { total: 2, concurrency: 1, keep_on_fail: false, auto_group: false, auto_group_size: 35, auto_sms_after_register: enabled };
    await h.store.start(payload);
    assert.deepEqual(plain(h.calls[0]), { path: "/api/config", body: { config: { registration: { auto_sms_after_register: enabled } } } });
    assert.deepEqual(plain(h.calls[1]), { path: "/api/job/start", body: payload });
    assert.equal(h.store.job.value.running, true);
  });
}

test("a failed settings save prevents a run with an unsaved automatic SMS preference", async () => {
  const h = storeHarness(new Error("config is locked"));
  await assert.rejects(h.store.start({ auto_sms_after_register: true }), /config is locked/);
  assert.equal(h.calls.length, 1);
});

test("automatic SMS discovery survives leaving registration and stops at the terminal outcome", async () => {
  const h = storeHarness();
  h.snapshots.push({ running: true, params: { auto_sms_after_register: true }, auto_sms: { status: "pending", total: 0 } });
  await h.store.load();
  h.store.startPolling();
  h.store.stopPolling();
  assert.equal(h.timers.size, 1, "leaving the view must retain discovery for an opted-in job");
  h.snapshots.push({ running: false, auto_sms: { status: "pending", total: 2 } });
  assert.equal(await h.tick(), 2000, "registration completion must not slow the pending SMS queue");
  h.snapshots.push({ running: false, auto_sms: { status: "running", total: 2, batch_id: "sms-one", source_batch_id: "reg-one" } });
  await h.tick();
  assert.equal(h.smsRefreshes.length, 1);
  h.snapshots.push({ running: false, auto_sms: { status: "running", total: 2, batch_id: "sms-one", source_batch_id: "reg-one" } });
  await h.tick();
  assert.equal(h.smsRefreshes.length, 1, "existing SMS store owns progress polling after discovery");
  h.snapshots.push({ running: false, auto_sms: { status: "completed", total: 2, completed: 2, batch_id: "sms-one", source_batch_id: "reg-one" } });
  await h.tick();
  assert.equal(h.smsRefreshes.length, 2, "refresh the shared final result once");
  assert.equal(h.timers.size, 0);
});

test("disabled follow-ups do not keep a background registration poll alive", async () => {
  const h = storeHarness();
  h.snapshots.push({ running: true, auto_sms: { status: "disabled", total: 0 } });
  await h.store.load();
  h.store.startPolling();
  h.store.stopPolling();
  assert.equal(h.timers.size, 0);
  assert.equal(h.smsRefreshes.length, 0);
});

test("concurrent shell and page restoration share one task request", async () => {
  const h = storeHarness();
  const first = h.store.load();
  const second = h.store.load();
  assert.equal(first, second);
  await Promise.all([first, second]);
  assert.equal(h.calls.length, 1);
});
