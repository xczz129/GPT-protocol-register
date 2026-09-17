import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

// Only the API client and timer callbacks are mocked; no network or real jobs run.
function loadModule(path, imports, globals = {}) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: path,
  });
  const exports = {};
  runInNewContext(outputText, {
    exports,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
    ...globals,
  }, { filename: path });
  return exports;
}

function harness() {
  const responses = [];
  const calls = [];
  const api = loadModule("../src/new/api/batchops.ts", {
    "./client": {
      api(path, options) {
        calls.push({ path, options });
        assert.ok(responses.length, `Unexpected request: ${path}`);
        return Promise.resolve(responses.shift());
      },
      jsonBody: JSON.stringify,
    },
  });
  const timers = new Map();
  let timerId = 0;
  const { useBatchOpsStore } = loadModule("../src/new/stores/batchops.ts", {
    "../api/batchops": api,
    vue: {
      reactive: (value) => value, ref: (value) => ({ value }),
      computed: (get) => ({ get value() { return get(); } }),
    },
    pinia: { defineStore: (_name, setup) => setup },
  }, {
    setInterval(callback) { timers.set(++timerId, callback); return timerId; },
    clearInterval(id) { timers.delete(id); },
  });
  return { api, responses, calls, timers, store: useBatchOpsStore() };
}

const families = [
  {
    name: "refresh AT", path: "/api/accounts/refresh-at", state: "refreshAt",
    fetch: "fetchRefreshAtProgress", start: "startRefreshAt", args: [["key"], false, 2],
    counters: { refreshed: 2, unchanged: 1, failed: 1, stopped: 1 },
  },
  {
    name: "trial", path: "/api/accounts/trial-eligibility", state: "trial",
    fetch: "fetchTrialProgress", start: "startTrial", args: [["key"], false, false, 2],
    counters: { eligible: 2, ineligible: 1, failed: 1, stopped: 1 },
  },
];

for (const family of families) {
  test(`${family.name}: unwrap actual active response and normalize statuses`, async () => {
    const h = harness();
    for (const status of ["queued", "running", "stopping", "success", "partial", "failed", "stopped", "cancelled", "interrupted"]) {
      h.responses.push({ ok: true, job: {
        job_id: "batch-a", status, running: true, total: 8, done: 5,
        current: "example@example.com", ...family.counters,
      } });
      const result = await h.api[family.fetch]();
      assert.equal(result.running, ["queued", "running", "stopping"].includes(status));
      assert.equal(result.total, 8);
      assert.equal(result.done, 5);
      assert.equal(result.current, "example@example.com");
      assert.equal(result.job_id, "batch-a");
      assert.equal(result.jobId, "batch-a");
      for (const [key, value] of Object.entries(family.counters)) assert.equal(result[key], value);
      assert.equal(h.calls.at(-1).path, `${family.path}/progress`);
      assert.equal("job" in result, false);
    }
  });

  test(`${family.name}: null active job yields idle defaults`, async () => {
    const h = harness();
    h.responses.push({ ok: true, job: null });
    const result = await h.api[family.fetch]();
    assert.equal(result.running, false);
    assert.equal(result.total, 0);
    assert.equal(result.done, 0);
    assert.equal(result.current, "");
    assert.equal(result.job_id, "");
    assert.equal(result.jobId, "");
    for (const key of Object.keys(family.counters)) assert.equal(result[key], 0);
  });

  test(`${family.name}: flat completed snapshot uses encoded job ID`, async () => {
    const h = harness();
    h.responses.push({ ok: true, job_id: "batch a/b", status: "partial", total: 5, done: 5, ...family.counters });
    const result = await h.api[family.fetch]("batch a/b");
    assert.equal(h.calls[0].path, `${family.path}/batch%20a%2Fb`);
    assert.equal(result.running, false);
    assert.equal(result.done, 5);
  });

  test(`${family.name}: store polls the started batch through terminal state`, async () => {
    const h = harness();
    let completed = 0;
    h.store.setOnFinished(() => completed++);
    h.responses.push({ ok: true, job_id: "batch-a", status: "queued", total: 5 });
    await h.store[family.start](...family.args);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].path, family.path);
    assert.equal(h.calls[0].options.method, "POST");
    assert.equal(h.store[family.state].running, true);
    assert.equal(h.store[family.state].total, 5);
    const tick = [...h.timers.values()][0];
    h.responses.push({ ok: true, job_id: "batch-a", status: "stopping", total: 5, done: 3 });
    await tick();
    assert.equal(h.store[family.state].running, true);
    assert.equal(h.timers.size, 1);
    h.responses.push({ ok: true, job_id: "batch-a", status: "partial", total: 5, done: 5, ...family.counters });
    await tick();
    assert.equal(h.calls.at(-1).path, `${family.path}/batch-a`);
    assert.equal(h.store[family.state].running, false);
    assert.equal(h.store[family.state].done, 5);
    for (const [key, value] of Object.entries(family.counters)) assert.equal(h.store[family.state][key], value);
    assert.equal(h.timers.size, 0);
    assert.equal(completed, 1);
  });

  test(`${family.name}: overlapping polls and stale previous-batch responses are ignored`, async () => {
    const h = harness();
    h.responses.push({ ok: true, job_id: "old", status: "queued", total: 1 });
    await h.store[family.start](...family.args);
    const oldTick = [...h.timers.values()][0];
    let finishOld;
    h.responses.push(new Promise((resolve) => { finishOld = resolve; }));
    const pending = oldTick();
    await oldTick();
    assert.equal(h.calls.length, 2, "only one poll may be in flight");
    h.responses.push({ ok: true, job_id: "new", status: "queued", total: 9 });
    await h.store[family.start](...family.args);
    finishOld({ ok: true, job_id: "old", status: "success", done: 1 });
    await pending;
    assert.equal(h.store[family.state].job_id, "new");
    assert.equal(h.store[family.state].running, true);
    assert.equal(h.store[family.state].total, 9);
    assert.equal(h.timers.size, 1);
  });

  test(`${family.name}: API failures propagate instead of becoming idle success`, async () => {
    const h = harness();
    const error = new Error("snapshot unavailable");
    h.responses.push(Promise.reject(error));
    await assert.rejects(h.api[family.fetch]("missing"), (value) => value === error);
  });
}

test("trial: restored interrupted job maps pending keys and stage, then resets them for a new batch", async () => {
  const h = harness();
  h.responses.push({ ok: true, job: {
    job_id: "restored-1234", status: "interrupted", total: 5, done: 3,
    pending_keys: ["a", "b"], stage: "任务已停止：剩余 2 个可续跑",
  } });
  const restored = await h.api.fetchTrialProgress();
  assert.equal(restored.running, false);
  assert.equal(restored.interrupted, true);
  assert.equal(restored.pendingCount, 2);
  assert.equal(restored.message, "任务已停止：剩余 2 个可续跑");
  h.responses.push({ ok: true, job_id: "new", status: "queued", total: 2 });
  Object.assign(restored, await h.api.trialStart(["a", "b"], false, false, 2));
  assert.equal(restored.interrupted, false);
  assert.equal(restored.pendingCount, 0);
  assert.equal(restored.message, "");
});

test("trial start forwards the selected v4 or nerver channel", async () => {
  const h = harness();
  h.responses.push({ ok: true, job: { job_id: "trial-nerver", status: "queued", total: 1, done: 0 } });
  await h.store.startTrial(["key"], false, false, 4, "nerver");
  assert.equal(h.calls.at(-1).path, "/api/accounts/trial-eligibility");
  assert.deepEqual(JSON.parse(h.calls.at(-1).options.body), {
    keys: ["key"], all_accounts: false, force: false, concurrency: 4, channel: "nerver",
  });

  h.responses.push({ ok: true, job: { job_id: "trial-v4", status: "queued", total: 1, done: 0 } });
  await h.api.trialStart(["key"], false, false, 4, "unsupported");
  assert.equal(JSON.parse(h.calls.at(-1).options.body).channel, "v4");
});

test("reauth: one skipped account does not stop the remaining 94 or count as failed", async () => {
  const h = harness();
  let completed = 0;
  h.store.setOnFinished(() => completed++);
  const skippedItem = { email: "banned@example.test", status: "skipped", message: "已封号，跳过重新登录" };
  h.responses.push({ ok: true }, { job: {
    job_id: "reauth-95", status: "running", running: true, total: 95, done: 1,
    success: 0, failed: 0, skipped: 1, cancelled: 0, items: [skippedItem],
    last: skippedItem.email, last_status: "skipped", last_message: skippedItem.message,
  } });
  await h.store.startReauthBatch("reauth", Array.from({ length: 95 }, (_, i) => `key-${i}`), { concurrency: 3 });
  assert.equal(h.store.reauth.running, true);
  assert.equal(h.store.reauth.total, 95);
  assert.equal(h.store.reauth.done, 1);
  assert.equal(h.store.reauth.skipped, 1);
  assert.equal(h.store.reauth.failed, 0);
  assert.equal(h.store.reauth.lastStatus, "skipped");
  assert.equal(h.store.reauth.lastMessage, skippedItem.message);
  assert.equal(h.timers.size, 1);

  h.responses.push({ job: {
    job_id: "reauth-95", status: "completed", running: false, total: 95, done: 95,
    success: 94, failed: 0, skipped: 1, cancelled: 0, items: [skippedItem],
  } });
  await [...h.timers.values()][0]();
  assert.equal(h.store.reauth.running, false);
  assert.equal(h.store.reauth.done, 95);
  assert.equal(h.store.reauth.success, 94);
  assert.equal(h.store.reauth.skipped, 1);
  assert.equal(h.store.reauth.failed, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(completed, 1);
});

test("reauth: completed all-skipped job retains full progress and zero failures", async () => {
  const h = harness();
  h.responses.push({ job: {
    job_id: "all-skipped", status: "completed", running: false, total: 95, done: 95,
    success: 0, failed: 0, skipped: 95, cancelled: 0,
    items: [{ email: "banned@example.test", status: "skipped", message: "已封号，跳过重新登录" }],
  } });
  await h.store.refreshReauthProgress();
  assert.equal(h.store.reauth.running, false);
  assert.equal(h.store.reauth.total, 95);
  assert.equal(h.store.reauth.done, 95);
  assert.equal(h.store.reauth.skipped, 95);
  assert.equal(h.store.reauth.failed, 0);
  assert.equal(h.store.reauth.items[0].status, "skipped");
  assert.equal(h.timers.size, 0);
});

test("reauth: a new older-format job resets skipped counters, rows and last diagnostics", async () => {
  const h = harness();
  h.responses.push({ job: {
    job_id: "old-skipped", status: "completed", running: false, total: 1, done: 1,
    success: 0, failed: 0, skipped: 1, cancelled: 0,
    items: [{ email: "banned@example.test", status: "skipped" }],
    last: "banned@example.test", last_status: "skipped", last_message: "已封号，跳过重新登录",
  } });
  await h.store.refreshReauthProgress();
  h.responses.push({ job: {
    job_id: "new-legacy", status: "running", running: true, total: 2, done: 0,
    success: 0, failed: 0,
  } });
  await h.store.refreshReauthProgress();
  assert.equal(h.store.reauth.job_id, "new-legacy");
  assert.equal(h.store.reauth.running, true);
  assert.equal(h.store.reauth.skipped, 0);
  assert.equal(h.store.reauth.failed, 0);
  assert.equal(h.store.reauth.items.length, 0);
  assert.equal(h.store.reauth.last, "");
  assert.equal(h.store.reauth.lastStatus, "");
  assert.equal(h.store.reauth.lastMessage, "");
});
