import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import ts from "typescript";

const source = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const secret = "JBSWY3DPEHPK3PXP";
const line = (email = "a@example.test") => `${email}----synthetic-password----${secret}`;

function evaluate(code, imports, globals = {}) {
  const exports = {};
  const { outputText } = ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  runInNewContext(outputText, {
    exports, Error, AbortController,
    require(name) {
      assert.ok(name in imports, `Unexpected import: ${name}`);
      return imports[name];
    },
    ...globals,
  });
  return exports;
}

function harness() {
  const calls = [], responses = [];
  const api = evaluate(source("../src/new/api/batchops.ts"), {
    "./client": {
      api(path, options) {
        calls.push({ path, options });
        assert.ok(responses.length, `Unexpected request: ${path}`);
        return Promise.resolve(responses.shift());
      },
      jsonBody: JSON.stringify,
    },
  });
  const { useBatchOpsStore } = evaluate(source("../src/new/stores/batchops.ts"), {
    vue, pinia: { defineStore: (_name, setup) => setup }, "../api/batchops": api,
  }, { setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {} });
  return { calls, responses, api, store: useBatchOpsStore() };
}

function component(path, exports, imports, globals = {}) {
  const script = source(path).match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  return evaluate(`${script}\nexport { ${exports.join(", ")} };`, { vue, ...imports }, {
    defineProps: () => ({ open: false }), defineEmits: () => () => {}, ...globals,
  });
}

function snapshot(extra = {}) {
  return { running: false, mode: "import", total: 0, done: 0, success: 0, failed: 0, logs: [], results: [], ...extra };
}

test("new import cancellation counters never subtract actual failures", () => {
  const { store } = harness();
  store.codex.value = snapshot({ total: 6, done: 6, success: 1, failed: 2, cancelled: 3,
    results: Array.from({ length: 3 }, (_, i) => ({ email: `${i}@example.test`, status: "cancelled" })),
  });
  assert.deepEqual(plain(store.codexSummary.value), {
    newSuccess: 1, synced: 0, reused: 0, failed: 2, cancelled: 3, pending: 0, active: 0, total: 6, done: 6,
  });
});

test("only legacy failed rows with cancellation reasons are removed from failed counts", () => {
  const { store } = harness();
  store.codex.value = snapshot({ total: 4, done: 4, success: 1, failed: 3, results: [
    { email: "a@example.test", status: "failed", error: "任务已停止" },
    { email: "b@example.test", status: "failed", error: "stop requested" },
    { email: "c@example.test", status: "failed", error: "HTTP 401" },
  ] });
  assert.equal(store.codexSummary.value.failed, 1);
  assert.equal(store.codexSummary.value.cancelled, 2);
  assert.equal(store.codexSummary.value.done, 4);
});

test("credential parsing retains line numbers and detects bad columns, email, Base32 and duplicates", () => {
  const { api } = harness();
  const parsed = api.parseImportCredentials([
    "# comment", "", line("A@example.test"), line("a@example.test"),
    "bad----password----ABC", `bad-address----password----${secret}`,
    "b@example.test----password----123456", `${line("c@example.test")}----extra`,
  ].join("\n"));
  assert.equal(parsed.total, 6);
  assert.equal(parsed.rows.length, 1);
  assert.equal(parsed.rows[0].lineNumber, 3);
  assert.equal(parsed.rows[0].email, "a@example.test");
  assert.equal(parsed.errors.length, 5);
  assert.match(parsed.errors[0], /第 4 行邮箱重复/);
  assert.ok(parsed.errors.some((error) => /第 7 行.*Base32/.test(error)));
  assert.ok(parsed.errors.some((error) => /第 8 行格式错误/.test(error)));
  assert.ok(!parsed.errors.join(" ").includes("password"));
});

test("Base32 accepts backend-compatible whitespace, lower case and partial padding", () => {
  const { api } = harness();
  for (const value of ["jbsw y3dp ehpk 3pxp", "MY", "MY=", "MY======", "MZXW6==="]) {
    const parsed = api.parseImportCredentials(`a@example.test----password----${value}`);
    assert.equal(parsed.errors.length, 0, value);
    assert.match(parsed.rows[0].line, /^[^\n]+----password----[A-Z2-7=]+$/);
  }
  for (const value of ["A", "ABC", "AAAAAA", "========", "M=Y", "MY=======", "AB08", "otpauth://example"]) {
    assert.equal(api.parseImportCredentials(`a@example.test----password----${value}`).errors.length, 1, value);
  }
});

test("mixed invalid input blocks the store and API without requests or lost retry rows", async () => {
  const h = harness();
  const text = `\n${line()}\n${line("bad-address")}`;
  h.store.lastImportLines.value.set("old@example.test", line("old@example.test"));
  await assert.rejects(h.store.startImport({ text, accountType: "personal", workspace: "", concurrency: 3 }), /第 3 行/);
  assert.throws(() => h.api.codexImport(text, "personal", "", 3), /第 3 行/);
  assert.equal(h.calls.length, 0);
  assert.equal(h.store.lastImportLines.value.size, 1);
  assert.equal(h.store.codexStartBusy.value, false);
});

test("API rejects concurrency outside the displayed 1–10 integer range", () => {
  const h = harness();
  assert.equal(h.api.IMPORT_MAX_CONCURRENCY, 10);
  for (const value of [0, 11, 1.5, NaN, Infinity]) {
    assert.throws(() => h.api.codexImport(line(), "personal", "", value), /1–10/);
  }
  assert.equal(h.calls.length, 0);
});

test("a valid import posts every canonical row and caches those same rows for retry", async () => {
  const h = harness();
  h.responses.push({ batch_id: "synthetic", total: 2 }, snapshot({ batch_id: "synthetic", total: 2, done: 2, success: 2 }));
  await h.store.startImport({ text: `# note\r\n${line("A@example.test")}\r\n${line("b@example.test")}`,
    accountType: "personal", workspace: "", concurrency: 10 });
  const body = JSON.parse(h.calls[0].options.body);
  assert.equal(body.concurrency, 10);
  assert.equal(body.text.split("\n").length, 2);
  assert.equal(h.store.lastImportLines.value.size, 2);
  assert.equal(h.store.lastImportLines.value.get("a@example.test"), line("A@example.test"));
});

test("import dialog blocks any invalid row and out-of-range concurrency", async () => {
  const { api } = harness();
  const submissions = [];
  const dialog = component("../src/new/views/accounts/ImportAccountsDialog.vue",
    ["text", "concurrency", "parsed", "canSubmit", "submit"], {
      "../../api/batchops": api,
      "../../stores/batchops": { useBatchOpsStore: () => ({ startImport: async (value) => submissions.push(value) }) },
      "../../stores/ui": { useUiStore: () => ({ notify() {} }) },
    });
  dialog.text.value = `${line()}\n${line()}`;
  assert.equal(dialog.canSubmit.value, false);
  await dialog.submit();
  assert.equal(submissions.length, 0);
  dialog.text.value = line();
  dialog.concurrency.value = 11;
  assert.equal(dialog.canSubmit.value, false);
  dialog.concurrency.value = 10;
  assert.equal(dialog.canSubmit.value, true);
  await dialog.submit();
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].concurrency, 10);
});

test("import history counts explicit and legacy cancellations separately from errors", () => {
  const { api } = harness();
  const history = component("../src/new/views/accounts/ImportHistoryDialog.vue", ["entries", "batches"], {
    "../../api/batchops": api, "../../stores/ui": { useUiStore: () => ({ notify() {} }) },
  });
  history.entries.value = [
    { batch_id: "batch", status: "success" },
    { batch_id: "batch", status: "cancelled" },
    { batch_id: "batch", status: "failed", error: "任务已取消" },
    { batch_id: "batch", status: "failed", error: "workspace mismatch" },
  ];
  const group = history.batches.value[0];
  assert.equal(group.success, 1);
  assert.equal(group.cancelled, 2);
  assert.equal(group.failed, 1);
});

test("group creation stays single-flight and preserves a rejected name and API error", async () => {
  let reject, calls = 0;
  const notices = [];
  const groups = { create: () => { calls++; return new Promise((_resolve, fail) => { reject = fail; }); } };
  const toolbar = component("../src/new/views/accounts/AccountToolbar.vue", ["newGroupName", "creatingGroup", "addGroup"], {
    "../../stores/accounts": { useAccountsStore: () => ({}) },
    "../../stores/groups": { useGroupsStore: () => groups },
    "../../stores/batchops": { useBatchOpsStore: () => ({}) },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => notices.push(args) }) },
    "../../composables/useConfirm": { useConfirm: () => () => {} },
    "../../api/accounts": {},
  });
  toolbar.newGroupName.value = "synthetic group";
  const pending = toolbar.addGroup();
  assert.equal(toolbar.creatingGroup.value, true);
  await toolbar.addGroup();
  assert.equal(calls, 1);
  reject(new Error("分组名已存在"));
  await pending;
  assert.equal(toolbar.creatingGroup.value, false);
  assert.equal(toolbar.newGroupName.value, "synthetic group");
  assert.equal(notices[0][0], "分组名已存在");
  groups.create = async () => { calls++; };
  await toolbar.addGroup();
  assert.equal(calls, 2);
  assert.equal(toolbar.newGroupName.value, "");
});

test("health chips distinguish inconclusive probes from unchecked and healthy rows", () => {
  const { healthChip } = evaluate(source("../src/new/views/accounts/useAccountActions.ts"), {
    vue, "../../api/accounts": {}, "../../composables/useCopy": {}, "../../stores/ui": {},
  });
  assert.equal(healthChip({ health_status: "unknown", health_probe_error: "timeout" }).text, "待复测");
  assert.equal(healthChip({ health_status: "unknown", status: "待复测" }).kind, "warning");
  assert.equal(healthChip({ health_status: "unknown" }).text, "未测活");
  assert.equal(healthChip({ health_status: "alive" }).text, "可用");
});

test("health progress preserves unknown as its own count", () => {
  const { normalizeHealthProgress } = evaluate(source("../src/new/api/accounts.ts"), { "./client": {} });
  const result = normalizeHealthProgress({ total: 3, done: 3, usable: 1, alive: 1, unknown: 2, error: 0 });
  assert.equal(result.unknown, 2);
  assert.equal(result.usable, 1);
  assert.equal(result.error, 0);
});
