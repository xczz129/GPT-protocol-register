import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

// Execute the actual store and API modules with synthetic HTTP replies and
// controlled clocks. No SMS provider, browser storage, credentials or network.
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
    AbortController, Error,
    ...globals,
  }, { filename: path });
  return exports;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const plain = (value) => JSON.parse(JSON.stringify(value));
const status = (fields = {}) => ({
  running: false, mode: "codex", total: 1, done: 1, success: 1, failed: 0,
  logs: [], results: [], batch_id: "batch-one", status: "completed", ...fields,
});

function harness() {
  const responses = [], calls = [], timers = new Map();
  let timerId = 0;
  const api = loadModule("../src/new/api/batchops.ts", {
    "./client": {
      api(path, options) {
        calls.push({ path, options });
        assert.ok(responses.length, `Unexpected request: ${path}`);
        const reply = responses.shift();
        if (typeof reply === "function") return reply(path, options);
        return Promise.resolve(reply);
      },
      jsonBody: JSON.stringify,
    },
  });
  const { useBatchOpsStore } = loadModule("../src/new/stores/batchops.ts", {
    "../api/batchops": api,
    vue: {
      ref: (value) => ({ value }), reactive: (value) => value,
      computed: (get) => ({ get value() { return get(); } }),
    },
    pinia: { defineStore: (_name, setup) => setup },
  }, {
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { throw new Error("SMS monitoring must not use overlapping intervals"); },
    clearInterval() {},
  });
  const store = useBatchOpsStore();
  async function tick() {
    assert.ok(timers.size, "expected a scheduled retry");
    const [id, timer] = [...timers].sort((a, b) => a[1].delay - b[1].delay)[0];
    timers.delete(id);
    timer.callback();
    await flush();
    return timer.delay;
  }
  return { api, responses, calls, timers, store, tick };
}

test("SMS API carries only explicit per-run settings and preserves the history dictionary", async () => {
  const h = harness();
  const options = { provider: "smsbower", country_order: "6,4", max_price: "0.2", max_phone_attempts: 1, timeout_seconds: 60 };
  h.responses.push({ ok: true }, { ok: true }, { ok: true, history: { "a@example.test": [{ status: "success", at: "now" }] } });
  await h.api.codexRun(["a@example.test"], 2, false, options);
  await h.api.preflightCodex({ emails: ["a@example.test"], concurrency: 2, force: false, sms_options: options });
  const history = await h.api.fetchCodexHistory(["a+one@example.test", "b@example.test"]);
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { emails: ["a@example.test"], concurrency: 2, force: false, sms_options: options });
  assert.equal(h.calls[1].path, "/api/codex-oauth/preflight");
  assert.equal(h.calls[2].path, "/api/codex-oauth/history?emails=a%2Bone%40example.test%2Cb%40example.test");
  assert.equal(history.history["a@example.test"][0].status, "success");
});

test("SMS API and retries reject retired provider options before any network call", async () => {
  const h = harness();
  const sms_options = { provider: "firefox", country_order: "usa" };
  assert.throws(() => h.api.codexRun(["old@example.test"], 100, false, sms_options), /历史查看/);
  assert.throws(() => h.api.preflightCodex({ emails: ["old@example.test"], sms_options }), /历史查看/);
  h.store.codex.value = status({ results: [{ email: "old@example.test", status: "failed" }], options: { concurrency: 3, force: false, sms_options } });
  await assert.rejects(h.store.retryCodexFailed(), /历史查看/);
  assert.equal(h.calls.length, 0);
});

test("closing the modal keeps monitoring and refreshes accounts exactly once at completion", async () => {
  const h = harness();
  let accountsReloads = 0, unrelatedReloads = 0;
  h.store.setOnCodexFinished(() => accountsReloads++);
  h.store.setOnFinished(() => unrelatedReloads++);
  h.responses.push({ ok: true, batch_id: "batch-one", total: 1 }, status({ running: true, status: "running", done: 0, success: 0 }));
  await h.store.runCodex(["a@example.test"], 1);
  h.store.codexDialogOpen.value = false;
  assert.equal(h.timers.size, 1);
  h.responses.push(status());
  await h.tick();
  assert.equal(h.store.codex.value.running, false);
  assert.equal(h.store.codexDialogOpen.value, false);
  assert.equal(accountsReloads, 1);
  assert.equal(unrelatedReloads, 0, "other views cannot replace the account reload callback");
  assert.equal(h.timers.size, 0);
  h.responses.push(status());
  await h.store.openCodexDialog();
  assert.equal(accountsReloads, 1, "reopening a completed batch must not reload twice");
});

test("temporary network failures back off, remain recoverable offline, and retain the final result", async () => {
  const h = harness();
  h.responses.push(status({ running: true, status: "running", done: 0, success: 0 }));
  await h.store.restoreCodex();
  for (const [index, delay] of [2000, 4000, 8000, 15000].entries()) {
    h.responses.push(() => Promise.reject(new Error("connection interrupted")));
    assert.equal(await h.tick(), delay);
    assert.equal(h.store.codex.value.running, true, "a network failure cannot turn a job into a completion");
    assert.equal(h.store.codexConnection.value, index >= 2 ? "offline" : "reconnecting");
    assert.equal(h.timers.size, 1);
  }
  h.responses.push(status({ total: 2, done: 2, success: 1, failed: 1 }));
  assert.equal(await h.tick(), 15000);
  assert.equal(h.store.codexConnection.value, "connected");
  assert.equal(h.store.codexError.value, "");
  assert.ok(h.store.codexLastUpdated.value > 0);
  assert.equal(h.store.codex.value.failed, 1);
  assert.equal(h.timers.size, 0);
});

test("manual refresh and polling share one in-flight status request", async () => {
  const h = harness(), pending = deferred();
  h.responses.push(pending.promise);
  const first = h.store.refreshCodexStatus();
  const second = h.store.refreshCodexStatus();
  assert.equal(first, second);
  assert.equal(h.calls.length, 1);
  pending.resolve(status({ running: true, status: "running", done: 0, success: 0 }));
  await Promise.all([first, second]);
  assert.equal(h.timers.size, 1);
});

test("a status request from an older generation cannot overwrite a newly started batch", async () => {
  const h = harness(), old = deferred();
  h.responses.push(old.promise);
  const restore = h.store.restoreCodex();
  h.responses.push({ ok: true, batch_id: "new-batch", total: 9 }, status({ batch_id: "new-batch", total: 9, done: 0, success: 0, running: true, status: "running" }));
  const launched = h.store.runCodex(["new@example.test"], 1);
  await flush();
  assert.equal(h.store.codex.value.batch_id, "new-batch");
  assert.equal(h.calls.length, 2, "new status waits for the outstanding old fetch to settle");
  old.resolve(status({ batch_id: "old-batch" }));
  await Promise.all([restore, launched]);
  assert.equal(h.store.codex.value.batch_id, "new-batch");
  assert.equal(h.store.codex.value.running, true);
  assert.equal(h.store.codex.value.total, 9);
  assert.equal(h.timers.size, 1);
});

test("a hung status request is aborted and can reconnect", async () => {
  const h = harness();
  h.responses.push((_path, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new Error("request timed out")), { once: true });
  }));
  const pending = h.store.restoreCodex();
  assert.equal(await h.tick(), 15000);
  await pending;
  assert.equal(h.store.codexConnection.value, "reconnecting");
  assert.equal(h.timers.size, 1);
  h.responses.push(status());
  await h.tick();
  assert.equal(h.store.codex.value.running, false);
  assert.equal(h.store.codexConnection.value, "connected");
});

test("reload restores completed results without opening a modal or duplicating completion callbacks", async () => {
  const h = harness();
  let finished = 0;
  h.store.setOnCodexFinished(() => finished++);
  h.responses.push(status({ total: 4, done: 4, success: 2, failed: 2 }), status({ total: 4, done: 4, success: 2, failed: 2 }));
  assert.equal(await h.store.restoreCodex(), false);
  assert.equal(h.store.codex.value.done, 4);
  assert.equal(h.store.codexDialogOpen.value, false);
  assert.equal(await h.store.openCodexDialogIfRunning(), false);
  assert.equal(finished, 1);
  assert.equal(h.timers.size, 0);
});

test("stopping shows an in-flight request and a stopping state until the final snapshot", async () => {
  const h = harness(), stopped = deferred();
  let finished = 0;
  h.store.setOnCodexFinished(() => finished++);
  h.store.codex.value = status({ running: true, status: "running", done: 0, success: 0 });
  h.responses.push(stopped.promise, status({ running: true, status: "stopping", done: 0, success: 0 }));
  const first = h.store.stopCodex();
  assert.equal(h.store.codexStopBusy.value, true);
  assert.equal(await h.store.stopCodex(), false);
  assert.equal(await h.store.refreshCodexStatus(), null, "a read started before stop acceptance cannot regress the stopping state");
  assert.equal(h.calls.length, 1);
  stopped.resolve({ ok: true });
  assert.equal(await first, true);
  assert.equal(h.store.codexStopBusy.value, false);
  assert.equal(h.store.codex.value.stopping, true);
  assert.equal(finished, 0);
  h.responses.push(status({ status: "cancelled", success: 0, cancelled: 1 }));
  await h.tick();
  assert.equal(h.store.codex.value.running, false);
  assert.equal(h.store.codex.value.stopping, false);
  assert.equal(h.store.codexSummary.value.cancelled, 1);
  assert.equal(finished, 1);
});

test("failed stop requests report the error and retain live monitoring", async () => {
  const h = harness();
  h.store.codex.value = status({ running: true, status: "running", done: 0, success: 0 });
  h.responses.push(() => Promise.reject(new Error("stop unavailable")), status({ running: true, status: "running", done: 0, success: 0 }));
  assert.equal(await h.store.stopCodex(), false);
  assert.equal(h.store.codexError.value, "stop unavailable");
  assert.equal(h.store.codex.value.running, true);
  assert.equal(h.store.codexStopBusy.value, false);
  assert.equal(h.timers.size, 1);
});

test("summary separates new RTs, reused RTs, cancellations and unexecuted accounts for both schemas", () => {
  const h = harness();
  h.store.codex.value = status({ total: 7, done: 5, success: 3, failed: 2, results: [
    { email: "reuse@example.test", status: "success", skipped: true },
    { email: "cancel@example.test", status: "failed", error: "任务已停止" },
  ] });
  assert.deepEqual(plain(h.store.codexSummary.value), { newSuccess: 2, synced: 0, reused: 1, failed: 1, cancelled: 1, pending: 2, active: 0, total: 7, done: 5 });
  h.store.codex.value = status({ total: 8, done: 6, success: 4, failed: 1, counts: {
    total: 8, done: 6, success: 2, skipped: 2, failed: 1, cancelled: 1, pending: 1, active: 1,
  } });
  assert.deepEqual(plain(h.store.codexSummary.value), { newSuccess: 2, synced: 0, reused: 2, failed: 1, cancelled: 1, pending: 1, active: 1, total: 8, done: 6 });
});

test("SMS retry selects failures only and preserves original settings when another start dialog was cancelled", async () => {
  const h = harness();
  h.store.codex.value = status({ total: 5, done: 5, results: [
    { email: "failed@example.test", status: "failed" },
    { email: "success@example.test", status: "success" },
    { email: "skip@example.test", status: "success", skipped: true },
    { email: "cancel@example.test", status: "cancelled" },
    { email: "pending@example.test", status: "pending" },
  ] });
  const options = { concurrency: 4, force: true, sms_options: { provider: "smsbower", country_order: "6", max_phone_attempts: 1, timeout_seconds: 60 } };
  Object.assign(h.store.lastCodexOptions, options);
  h.store.openCodexStart(["next@example.test"], false);
  h.store.codexStartOpen.value = false;
  assert.equal(h.store.codexStartForce.value, false);
  h.responses.push({ total: 1, missing_credentials: [], not_found: [], deactivated: [] }, { ok: true, batch_id: "retry" }, status({ batch_id: "retry" }));
  await h.store.retryCodexFailed();
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { emails: ["failed@example.test"], ...options });
  assert.deepEqual(JSON.parse(h.calls[1].options.body), { emails: ["failed@example.test"], ...options });
});

test("SMS retry batches failed and error snapshots together", async () => {
  const h = harness();
  h.store.codex.value = status({ total: 4, done: 4, failed: 3, results: [
    { email: "first@example.test", status: "failed" },
    { email: "second@example.test", status: "error", error: "号码不可用" },
    { email: "third@example.test", outcome: "failed", status: "complete" },
    { email: "ok@example.test", status: "success" },
  ] });
  Object.assign(h.store.lastCodexOptions, { concurrency: 3, force: false });
  h.responses.push(
    { total: 3, missing_credentials: [], not_found: [], deactivated: [] },
    { ok: true, batch_id: "retry-three" },
    status({ batch_id: "retry-three" }),
  );
  await h.store.retryCodexFailed();
  const payload = JSON.parse(h.calls[0].options.body);
  assert.deepEqual(payload.emails, ["first@example.test", "second@example.test", "third@example.test"]);
  assert.equal(h.calls.filter((call) => call.path.endsWith("/run")).length, 1);
});

test("SMS retry refuses a missing-account partial retry and refuses retrying successful rows", async () => {
  const h = harness();
  h.store.codex.value = status({ results: [
    { email: "failed@example.test", status: "failed" },
    { email: "gone@example.test", status: "failed" },
    { email: "success@example.test", status: "success" },
  ] });
  h.responses.push({ total: 2, missing_credentials: [], not_found: ["gone@example.test"], deactivated: [] });
  await assert.rejects(h.store.retryCodexFailed(), /本次未启动/);
  assert.equal(h.calls.length, 1);
  await assert.rejects(h.store.retryCodexFailed("success@example.test"), /没有可重试/);
  assert.equal(h.calls.length, 1);
});

test("SMS retry holds the start lock during preflight and cannot submit duplicate batches", async () => {
  const h = harness(), preview = deferred();
  h.store.codex.value = status({ results: [{ email: "failed@example.test", status: "failed" }] });
  h.responses.push(preview.promise, { ok: true, batch_id: "retry-one" }, status({ batch_id: "retry-one" }));
  const first = h.store.retryCodexFailed();
  assert.equal(h.store.codexStartBusy.value, true);
  await assert.rejects(h.store.retryCodexFailed(), /等待当前任务/);
  assert.equal(h.calls.length, 1);
  preview.resolve({ total: 1, missing_credentials: [], not_found: [], deactivated: [] });
  await first;
  assert.equal(h.store.codexStartBusy.value, false);
  assert.equal(h.calls.filter((call) => call.path.endsWith("/run")).length, 1);
});

test("import retry requires every selected credential row and excludes stopped or successful accounts", async () => {
  const h = harness();
  h.store.codex.value = status({ mode: "import", results: [
    { email: "a@example.test", status: "failed" },
    { email: "b@example.test", status: "failed" },
    { email: "stop@example.test", status: "failed", error: "任务已停止" },
    { email: "ok@example.test", status: "success" },
  ] });
  h.store.lastImportLines.value.set("a@example.test", "a@example.test----synthetic-password----JBSWY3DPEHPK3PXP");
  assert.equal(h.store.canRetryImport(), false);
  assert.equal(h.store.canRetryImport("a@example.test"), true);
  assert.equal(h.store.canRetryImport("stop@example.test"), false);
  await assert.rejects(h.store.retryImportFailed(), /本次未启动/);
  assert.equal(h.calls.length, 0);
  h.responses.push({ ok: true, batch_id: "import-retry" }, status({ mode: "import", batch_id: "import-retry" }));
  await h.store.retryImportFailed("a@example.test");
  assert.equal(h.calls[0].path, "/api/codex-oauth/import");
  assert.equal(JSON.parse(h.calls[0].options.body).text, "a@example.test----synthetic-password----JBSWY3DPEHPK3PXP");
});

test("a lost launch response reconciles the actual server job before exposing a retry", async () => {
  const h = harness();
  h.responses.push(() => Promise.reject(new Error("launch response lost")), status({ running: true, status: "running", success: 0, done: 0 }));
  await assert.rejects(h.store.runCodex(["a@example.test"], 1), /launch response lost/);
  assert.equal(h.store.codex.value.running, true);
  assert.equal(h.store.codexStartBusy.value, false);
  assert.equal(h.timers.size, 1);
  await assert.rejects(h.store.runCodex(["a@example.test"], 1), /已有接码或导入任务/);
  assert.equal(h.calls.filter((call) => call.path.endsWith("/run")).length, 1);
});

test("terminal cancelled response clears a stale stopping flag", async () => {
  const h = harness();
  h.responses.push(status({ running: false, status: "cancelled", stopping: true, done: 1, cancelled: 1 }));
  await h.store.restoreCodex();
  assert.equal(h.store.codex.value.running, false);
  assert.equal(h.store.codex.value.stopping, false);
});

test("relay syncs are counted independently from newly authorized RTs", () => {
  const h = harness();
  h.store.codex.value = status({ total: 3, done: 3, success: 3, counts: {
    total: 3, done: 3, success: 1, synced: 1, skipped: 1, failed: 0, cancelled: 0, pending: 0, active: 0,
  } });
  assert.deepEqual(plain(h.store.codexSummary.value), { newSuccess: 1, synced: 1, reused: 1, failed: 0, cancelled: 0, pending: 0, active: 0, total: 3, done: 3 });
});

test("partial authorization success retries only relay sync while other auth failures preserve reauthorization", async () => {
  const h = harness();
  h.store.codex.value = status({ total: 2, done: 2, results: [
    { email: "sync@example.test", status: "failed", authorization_status: "success", relay_status: "failed", retry_action: "sync_only" },
    { email: "auth@example.test", status: "failed", retry_action: "reauthorize" },
  ] });
  Object.assign(h.store.lastCodexOptions, { concurrency: 2, force: true, strategy: "reauthorize" });
  h.responses.push({ total: 2, missing_credentials: [], not_found: [], deactivated: [] }, { ok: true, batch_id: "repair" }, status({ batch_id: "repair" }));
  await h.store.retryCodexFailed();
  for (const call of h.calls.slice(0, 2)) {
    const payload = JSON.parse(call.options.body);
    assert.deepEqual(payload.account_actions, { "sync@example.test": "sync_only", "auth@example.test": "reauthorize" });
    assert.equal(payload.strategy, "reauthorize");
  }
});
