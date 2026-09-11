import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import ts from "typescript";

// Execute the real component scripts with synthetic task data. No SMS request,
// browser credential, account mutation or external network is involved.
function component(name, names, overrides = {}) {
  const source = readFileSync(new URL(`../src/new/views/accounts/${name}.vue`, import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const exports = {};
  const watches = [];
  const cleanup = [];
  const task = vue.reactive({
    codex: { running: false, mode: "sms", results: [], items: [], logs: [] },
    codexSummary: { newSuccess: 0, reused: 0, failed: 0, cancelled: 0, pending: 0, active: 0, total: 0, done: 0 },
    codexDialogOpen: true, codexStartOpen: true, codexStartEmails: ["a@example.test"], codexStartForce: false,
    codexHistoryOpen: true, codexHistoryEmails: [], codexStartBusy: false, codexStopBusy: false,
    codexConnection: "connected", codexLastUpdated: 0, codexError: "",
    lastCodexOptions: { force: true, concurrency: 3 },
    canRetryImport: () => false,
    ...overrides.task,
  });
  const calls = [];
  const imports = {
    vue: { ...vue, watch: (source, handler) => watches.push({ source, handler }), onBeforeUnmount: (handler) => cleanup.push(handler) },
    "../../stores/batchops": { useBatchOpsStore: () => task },
    "../../stores/accounts": { useAccountsStore: () => ({ clearSelection: () => calls.push(["clearSelection"]) }) },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => calls.push(["notify", ...args]) }) },
    "../../composables/useCopy": { useCopy: () => ({ copiedKey: vue.ref(""), copyText: async (...args) => { calls.push(["copy", ...args]); return true; } }) },
    "../../api/batchops": overrides.api || {},
  };
  const { outputText } = ts.transpileModule(`${source}\nexport { ${names.join(", ")} };`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
  runInNewContext(outputText, { exports, require: (id) => { assert.ok(id in imports, `Unexpected import ${id}`); return imports[id]; }, defineEmits: () => (...args) => calls.push(["emit", ...args]), Date, Blob, URL, setTimeout, clearTimeout, setInterval, clearInterval });
  return { ...exports, task, calls, watches, dispose: () => cleanup.forEach((fn) => fn()) };
}

test("closing live progress preserves the running task and polling", () => {
  let disposed = 0;
  const h = component("CodexLiveDialog", ["close"], { task: { codex: { running: true }, stopCodexPolling: () => disposed++ } });
  h.close();
  assert.equal(h.task.codexDialogOpen, false);
  assert.equal(h.task.codex.running, true);
  assert.equal(disposed, 0);
  h.dispose();
});

test("legacy cancellation is excluded from failed-email copies and task result filters", () => {
  const h = component("CodexLiveDialog", ["items", "failedEmails", "filter", "filteredItems"]);
  h.task.codex.results = [
    { email: "failed@example.test", status: "failed", error: "provider unavailable" },
    { email: "cancel@example.test", status: "failed", error: "任务已停止" },
    { email: "reused@example.test", status: "success", skipped: true, refresh_token: "synthetic-rt" },
  ];
  assert.deepEqual([...h.failedEmails.value], ["failed@example.test"]);
  h.filter.value = "cancelled";
  assert.equal(h.filteredItems.value.length, 1);
  assert.equal(h.filteredItems.value[0].email, "cancel@example.test");
  h.filter.value = "skipped";
  assert.equal(h.filteredItems.value[0].email, "reused@example.test");
  h.dispose();
});

test("live stage merges a completed result while retaining number attempts", () => {
  const h = component("CodexLiveDialog", ["items"]);
  h.task.codex.items = [{ email: "a@example.test", status: "active", stage: "token", attempt: 2, max_phone_attempts: 3 }];
  h.task.codex.results = [{ email: "a@example.test", status: "success", refresh_token: "synthetic-rt" }];
  assert.equal(h.items.value.length, 1);
  assert.equal(h.items.value[0].status, "success");
  assert.equal(h.items.value[0].attempt, 2);
  h.dispose();
});

test("countdown never becomes negative or labels a finished account as waiting", () => {
  const h = component("CodexLiveDialog", ["countdown", "now"]);
  h.now.value = 100000;
  assert.equal(h.countdown({ status: "active", wait_deadline: 165 }), "等码剩余 1:05");
  assert.equal(h.countdown({ status: "active", wait_deadline: 99 }), "等码时间已到，正在更新状态");
  assert.equal(h.countdown({ status: "success", wait_deadline: 165 }), "");
  h.dispose();
});

test("scrolling up pauses new-log following without moving the reader", () => {
  const h = component("CodexLiveDialog", ["logsEl", "followLogs", "logScroll"]);
  h.logsEl.value = { scrollHeight: 500, scrollTop: 100, clientHeight: 100 };
  h.logScroll();
  assert.equal(h.followLogs.value, false);
  assert.equal(h.logsEl.value.scrollTop, 100);
  h.dispose();
});

test("SMS retry uses the SMS handler; import retries require cached credentials", async () => {
  const calls = [];
  const h = component("CodexLiveDialog", ["retry", "retryAvailable"], { task: {
    retryCodexFailed: async (email) => calls.push(["sms", email]),
    retryImportFailed: async (email) => calls.push(["import", email]),
  } });
  h.task.codexSummary.failed = 1;
  assert.equal(h.retryAvailable.value, true);
  await h.retry("a@example.test");
  assert.deepEqual(calls, [["sms", "a@example.test"]]);
  h.task.codex.mode = "import";
  assert.equal(h.retryAvailable.value, false);
  h.dispose();
});

function preflightResult(overrides = {}) {
  return { ok: true, total: 1, eligible: 1, skipped: 0, missing_credentials: [], not_found: [], deactivated: [], limit: 200, configured: true, emails: ["a@example.test"], settings: { provider: "smsbower", provider_label: "SMSBower", configured: true, enabled: true, country_order: "6, 4", max_price: "0.20", max_phone_attempts: 2, timeout_seconds: 90, price_cap_supported: true }, ...overrides };
}

test("start dialog defaults to missing-RT mode despite a previous forced batch, and opening only preflights", async () => {
  const requests = [];
  const h = component("SmsStartDialog", ["form", "options", "check", "launch"], { api: { codexPreflight: async (body) => { requests.push(body); return preflightResult(); } } });
  // The open watcher reads codexStartForce, not the prior accepted batch's force.
  h.watches[0].handler(true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.form.force, false);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].force, false);
  assert.equal(h.form.timeout, 90);
  assert.equal(h.form.maxPrice, "0.20");
  assert.equal(h.calls.length, 0);
  h.dispose();
});

test("start validates limits and sends only per-run public options after deliberate launch", async () => {
  const launches = [];
  const h = component("SmsStartDialog", ["check", "form", "invalid", "launch"], {
    api: { codexPreflight: async () => preflightResult() },
    task: { runCodex: async (...args) => launches.push(args) },
  });
  await h.check(true);
  h.form.attempts = 0;
  await h.launch();
  assert.match(h.invalid.value, /取号上限/);
  assert.equal(launches.length, 0);
  h.form.attempts = 2;
  h.form.maxPrice = "-1";
  assert.match(h.invalid.value, /最高价/);
  h.form.maxPrice = "0.25";
  await h.launch();
  assert.equal(launches.length, 1);
  assert.equal(launches[0][2], false);
  assert.equal(launches[0][3].max_phone_attempts, 2);
  assert.equal(launches[0][3].max_price, "0.25");
  assert.equal(Object.hasOwn(launches[0][3], "api_key"), false);
  h.dispose();
});

test("a late preflight response cannot overwrite a newer provider selection", async () => {
  let resolveFirst;
  let calls = 0;
  const h = component("SmsStartDialog", ["check", "preview", "form"], { api: { codexPreflight: () => ++calls === 1 ? new Promise((resolve) => { resolveFirst = resolve; }) : Promise.resolve(preflightResult({ eligible: 0, skipped: 1 })) } });
  const first = h.check(true);
  await h.check();
  resolveFirst(preflightResult());
  await first;
  assert.equal(h.preview.value.eligible, 0);
  assert.equal(h.preview.value.skipped, 1);
  h.dispose();
});

test("history retry excludes a historical failure which later succeeded or was cancelled", () => {
  const h = component("SmsHistoryDialog", ["entries", "latestFailures", "query"]);
  h.entries.value = [
    { email: "resolved@example.test", outcome: "success", at: "2026-09-11T10:00:00" },
    { email: "failed@example.test", outcome: "failed", at: "2026-09-11T09:00:00" },
    { email: "cancelled@example.test", outcome: "cancelled", at: "2026-09-11T08:00:00" },
    { email: "resolved@example.test", outcome: "failed", at: "2026-09-10T09:00:00" },
    { email: "cancelled@example.test", outcome: "failed", at: "2026-09-10T08:00:00" },
  ];
  assert.deepEqual([...h.latestFailures.value.map((entry) => entry.email)], ["failed@example.test"]);
  h.query.value = "resolved";
  assert.equal(h.latestFailures.value.length, 0);
  h.dispose();
});
