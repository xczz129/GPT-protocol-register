import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function loadModule(path, imports, globals = {}) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: path,
  });
  const exports = {};
  runInNewContext(outputText, {
    exports,
    require(name) { assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`); return imports[name]; },
    AbortController,
    ...globals,
  }, { filename: path });
  return exports;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

test("health API normalizes snake_case progress and clamps counters", async () => {
  const api = loadModule("../src/new/api/accounts.ts", {
    "./client": {
      api: async () => ({
        running: true, requested: 8, actual_total: 5, total: 5, done: 99,
        checked: 4, pending_count: 2, skipped_known_deactivated: 3,
        started_at: "2026-09-12T01:00:00Z", updates: { key: { health_status: "alive" } },
      }),
    },
  });
  const value = await api.fetchHealthProgress(true);
  assert.equal(value.running, true);
  assert.equal(value.total, 5);
  assert.equal(value.done, 5);
  assert.equal(value.startedAt, "2026-09-12T01:00:00Z");
  assert.equal(value.pendingCount, 2);
  assert.equal(value.skippedKnownDeactivated, 3);
  assert.deepEqual(value.updates.key, { health_status: "alive" });
});

test("health panel does not count invalidOther twice in the invalid total", () => {
  const source = readFileSync(new URL("../src/new/views/accounts/HealthProgress.vue", import.meta.url), "utf8");
  assert.match(source, /失效\s*\{\{\s*health\.invalid\s*\|\|\s*0\s*\}\}/);
  assert.doesNotMatch(source, /health\.invalid[^\n]*\+[^\n]*health\.invalidOther/);
});

test("health start exposes an immediate running state while the long POST is pending", async () => {
  const start = deferred();
  const calls = [];
  const snapshot = { running: true, requested: 2, actualTotal: 2, total: 2, done: 1, checked: 1, pendingCount: 1, current: "a@example.test" };
  const { useAccountsStore } = loadModule("../src/new/stores/accounts.ts", {
    vue: {
      ref: (value) => ({ value }), shallowRef: (value) => ({ value }),
      computed: (get) => ({ get value() { return get(); } }),
    },
    pinia: { defineStore: (_name, setup) => setup },
    "../api/accounts": {
      fetchAccountsPage: async () => ({ items: [], total: 0, revision: 1, returned: 0 }),
      fetchHealthProgress: async () => { calls.push("progress"); return snapshot; },
      startHealthCheck: async () => { calls.push("start"); return start.promise; },
      resumeHealthCheck: async () => ({}),
      deleteAccounts: async () => ({}), deleteInvalidAccounts: async () => ({}),
    },
  }, {
    setTimeout: () => 1,
    clearTimeout: () => {},
  });
  const store = useAccountsStore();
  const pending = store.startHealth(["key-a", "key-b"], false, 2);
  assert.equal(store.health.value.running, true);
  assert.equal(store.health.value.requested, 2);
  assert.equal(store.health.value.message, "正在启动测活…");
  start.resolve({ ok: true });
  await pending;
  assert.ok(calls.includes("progress"));
  assert.equal(store.health.value.done, 1);
  assert.equal(store.health.value.current, "a@example.test");
});

test("health progress continues polling every 700ms while the long POST is pending", async () => {
  const start = deferred();
  const timers = new Map();
  let timerId = 0;
  let progressCalls = 0;
  const snapshots = [0, 1, 2, 3].map((done) => ({
    running: done < 3, requested: 3, actualTotal: 3, total: 3,
    done, checked: done, pendingCount: 3 - done,
    startedAt: "run-current", current: `account-${done}@example.test`,
  }));
  const { useAccountsStore } = loadModule("../src/new/stores/accounts.ts", {
    vue: {
      ref: (value) => ({ value }), shallowRef: (value) => ({ value }),
      computed: (get) => ({ get value() { return get(); } }),
    },
    pinia: { defineStore: (_name, setup) => setup },
    "../api/accounts": {
      fetchAccountsPage: async () => ({ items: [], total: 0, revision: 1, returned: 0 }),
      fetchHealthProgress: async () => snapshots[progressCalls++],
      startHealthCheck: async () => start.promise,
      resumeHealthCheck: async () => ({}),
      deleteAccounts: async () => ({}), deleteInvalidAccounts: async () => ({}),
    },
  }, {
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
  const poll = async () => {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === 700);
    assert.ok(entry, "the next progress poll must be scheduled before POST completes");
    timers.delete(entry[0]);
    entry[1].callback();
    await flush();
  };

  const store = useAccountsStore();
  const pending = store.startHealth(["a", "b", "c"], false, 2);
  await flush();
  assert.equal(store.healthSubmitting.value, true);
  assert.equal(progressCalls, 1);
  assert.equal(store.health.value.done, 0);

  await poll();
  assert.equal(store.healthSubmitting.value, true);
  assert.equal(store.health.value.done, 1);
  await poll();
  assert.equal(store.healthSubmitting.value, true);
  assert.equal(store.health.value.done, 2);
  assert.equal(progressCalls, 3);

  start.resolve({ ok: true });
  await pending;
  assert.equal(store.healthSubmitting.value, false);
  assert.equal(store.health.value.done, 3);
  assert.equal([...timers.values()].some((timer) => timer.delay === 700), false);
});

function rejectionHarness(startHealthCheck, fetchHealthProgress) {
  const timers = new Map();
  let timerId = 0;
  const { useAccountsStore } = loadModule("../src/new/stores/accounts.ts", {
    vue: {
      ref: (value) => ({ value }), shallowRef: (value) => ({ value }),
      computed: (get) => ({ get value() { return get(); } }),
    },
    pinia: { defineStore: (_name, setup) => setup },
    "../api/accounts": {
      fetchAccountsPage: async () => ({ items: [], total: 0, revision: 1, returned: 0 }),
      fetchHealthProgress, startHealthCheck,
      resumeHealthCheck: async () => ({}),
      deleteAccounts: async () => ({}), deleteInvalidAccounts: async () => ({}),
    },
  }, {
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  return { store: useAccountsStore(), timers };
}

test("health start rejected by maintenance clears optimistic running state and permits retry", async () => {
  const rejection = Object.assign(new Error("自动维护正在运行"), { status: 409 });
  const previous = {
    running: false, requested: 2, actualTotal: 2, total: 2,
    done: 2, checked: 2, pendingCount: 0, startedAt: "run-previous",
  };
  let starts = 0;
  let polls = 0;
  const { store, timers } = rejectionHarness(
    async () => { starts += 1; if (starts === 1) throw rejection; return { ok: true }; },
    async () => { polls += 1; return previous; },
  );
  store.health.value = previous;

  await assert.rejects(store.startHealth(["a", "b"], false, 2), (error) => error === rejection);
  assert.ok(polls >= 2, "rejection must recheck the server after the initial progress request");
  assert.equal(store.health.value.running, false);
  assert.equal(store.healthSubmitting.value, false);
  assert.equal([...timers.values()].some((timer) => timer.delay === 700), false);

  await store.startHealth(["a", "b"], false, 2);
  assert.equal(starts, 2, "a rejected launch must not keep the UI locked until reload");
});

test("lost health POST response retains server-running progress and ignores an older pending GET", async () => {
  const start = deferred();
  const oldProgress = deferred();
  const rejection = new Error("launch response lost");
  const previous = {
    running: false, requested: 2, actualTotal: 2, total: 2,
    done: 2, checked: 2, pendingCount: 0, startedAt: "run-previous",
  };
  const running = {
    ...previous, running: true, done: 1, checked: 1,
    pendingCount: 1, startedAt: "run-current",
  };
  let polls = 0;
  const { store, timers } = rejectionHarness(
    async () => start.promise,
    async () => (++polls === 1 ? oldProgress.promise : running),
  );
  store.health.value = previous;
  const pending = store.startHealth(["a", "b"], false, 2);
  const rejected = assert.rejects(pending, (error) => error === rejection);
  start.reject(rejection);
  await rejected;

  assert.equal(polls, 2, "reconciliation must not reuse a GET that began before the POST");
  assert.equal(store.health.value.running, true);
  assert.equal(store.health.value.startedAt, "run-current");
  assert.equal(store.healthSubmitting.value, false);
  assert.ok([...timers.values()].some((timer) => timer.delay === 700));
  await assert.rejects(store.startHealth(["a"], false, 1), /已有测活任务/);

  oldProgress.resolve(previous);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(store.health.value.running, true);
  assert.equal(store.health.value.done, 1);
  assert.equal(store.health.value.startedAt, "run-current");
});
