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
  let resolve;
  const promise = new Promise((ok) => { resolve = ok; });
  return { promise, resolve };
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
