import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import ts from "typescript";

function evaluate(source, imports, globals = {}) {
  const exports = {};
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
  runInNewContext(outputText, { exports, require: (id) => { assert.ok(id in imports, `Unexpected import: ${id}`); return imports[id]; }, AbortController, setTimeout, clearTimeout, ...globals });
  return exports;
}
function plain(value) { return JSON.parse(JSON.stringify(value)); }
function file(name, overrides = {}) { return { file: name, email: "relay-only@example.test", account_id: "workspace-team", plan_type: "team", size_bytes: 1800, valid: true, revision: `revision-${name}`, ...overrides }; }
function preview(items, overrides = {}) { return { configured: true, total_files: items.length, matched_files: items.filter((item) => item.valid).length, matched_emails: 1, not_found: [], items, ...overrides }; }
function harness(overrides = {}) {
  const calls = [];
  const cleanup = [];
  const account = { delete_key: "a", email: "local@example.test", has_password: true, has_totp: true };
  const accounts = vue.reactive({ rows: [account], total: 1, loading: false, loadError: "", loadProgress: { loaded: 1, total: 1 }, load: async () => calls.push(["accounts.load"]), ...overrides.accounts });
  let source = readFileSync(new URL("../src/new/views/accounts/CredentialExportView.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  source += "\nexport { mode,input,removedColumns,parseEmails,updateInput,parsed,requestedEmails,accountsReady,matched,notFound,canExportTxt,doExport,relayAll,relayPreview,relayError,relayLoading,selectedFiles,validFiles,canPreview,canExportRelay,loadRelayPreview,doRelayExport,toggleAllFiles,format };";
  const values = evaluate(source, {
    vue: { ...vue, onMounted: () => {}, onBeforeUnmount: (fn) => cleanup.push(fn), watch: (...args) => { const stop = vue.watch(...args); cleanup.push(stop); return stop; } },
    "../../stores/accounts": { useAccountsStore: () => accounts },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => calls.push(["notify", ...args]) }) },
    "../../api/accounts": { exportSecurityByEmail: async (emails) => { calls.push(["txt", [...emails]]); return { exported: emails.length, missing: 0 }; } },
    "../../api/cliproxy": {
      fetchCliproxyExportPreview: overrides.fetchPreview || (async (body) => { calls.push(["preview", plain(body)]); return preview([]); }),
      exportCliproxyFiles: overrides.exportFiles || (async (files, format, revisions) => { calls.push(["download", [...files], format, plain(revisions)]); return { exported: files.length, filename: "selected.zip" }; }),
    },
  });
  return { ...values, accounts, calls, dispose: () => cleanup.forEach((fn) => fn()) };
}

test("email parsing strips credential columns, counts duplicate and invalid input without retaining secrets", () => {
  const h = harness();
  const value = h.parseEmails("A@example.test----synthetic-password----synthetic-totp\na@example.test, b@example.test;bad-input");
  assert.deepEqual(plain(value.emails), ["a@example.test", "b@example.test"]);
  assert.equal(value.duplicates, 1);
  assert.deepEqual(plain(value.invalidLines), [2]);
  h.updateInput("A@example.test----synthetic-password----synthetic-totp");
  assert.equal(h.input.value, "A@example.test");
  assert.equal(h.removedColumns.value, true);
  assert.equal(h.calls.length, 0);
  h.dispose();
});

test("TXT preview and download stay blocked while paginated loading is partial", async () => {
  const h = harness({ accounts: { loading: true, total: 200, loadProgress: { loaded: 1, total: 200 } } });
  h.mode.value = "txt";
  h.updateInput("local@example.test");
  assert.equal(h.matched.value.length, 0);
  assert.equal(h.notFound.value.length, 0);
  assert.equal(h.canExportTxt.value, false);
  await h.doExport();
  assert.equal(h.calls.some((call) => call[0] === "txt"), false);
  // A short page ending without an exception still cannot be treated as complete.
  h.accounts.loading = false;
  assert.equal(h.accountsReady.value, false);
  h.dispose();
});

test("TXT export requires a complete error-free snapshot and valid input", async () => {
  const h = harness();
  h.mode.value = "txt"; h.updateInput("local@example.test, bad-input");
  assert.equal(h.canExportTxt.value, false);
  h.updateInput("local@example.test"); h.accounts.loadError = "synthetic page failure";
  assert.equal(h.canExportTxt.value, false);
  h.accounts.loadError = "";
  assert.equal(h.canExportTxt.value, true);
  await h.doExport();
  assert.deepEqual(h.calls.find((call) => call[0] === "txt"), ["txt", ["local@example.test"]]);
  h.dispose();
});

test("relay preview uses actual files independently of the local account database and preserves duplicate-email workspaces", async () => {
  const files = [file("team.json"), file("personal.json", { account_id: "workspace-personal", plan_type: "free" })];
  const requests = [];
  const h = harness({ accounts: { rows: [], total: 500, loading: true }, fetchPreview: async (body) => { requests.push(plain(body)); return preview(files); } });
  h.updateInput("relay-only@example.test");
  await h.loadRelayPreview();
  assert.deepEqual(requests, [{ emails: ["relay-only@example.test"], all: false }]);
  assert.deepEqual(plain(h.selectedFiles.value), ["team.json", "personal.json"]);
  assert.equal(h.canExportRelay.value, true);
  h.selectedFiles.value = ["team.json"];
  await h.doRelayExport();
  assert.deepEqual(h.calls.find((call) => call[0] === "download"), ["download", ["team.json"], "original", { "team.json": "revision-team.json" }]);
  h.dispose();
});

test("empty or unmatched email selection never falls back to all relay files", async () => {
  const h = harness();
  await h.loadRelayPreview();
  assert.equal(h.calls.length, 0);
  h.updateInput("missing@example.test");
  await h.loadRelayPreview();
  assert.deepEqual(h.calls[0], ["preview", { emails: ["missing@example.test"], all: false }]);
  assert.equal(h.canExportRelay.value, false);
  await h.doRelayExport();
  assert.equal(h.calls.some((call) => call[0] === "download"), false);
  h.dispose();
});

test("all-file scope requires an explicit choice and still only downloads selected files", async () => {
  const calls = [];
  const h = harness({ fetchPreview: async (body) => { calls.push(plain(body)); return preview([file("one.json"), file("two.json")]); } });
  assert.equal(h.relayAll.value, false);
  h.relayAll.value = true;
  await h.loadRelayPreview();
  assert.deepEqual(calls, [{ emails: [], all: true }]);
  h.toggleAllFiles();
  assert.equal(h.selectedFiles.value.length, 0);
  assert.equal(h.canExportRelay.value, false);
  h.selectedFiles.value = ["two.json"];
  await h.doRelayExport();
  assert.deepEqual(h.calls.find((call) => call[0] === "download")[1], ["two.json"]);
  h.dispose();
});

test("invalid JSON and incomplete metadata cannot be selected for download", async () => {
  const h = harness({ fetchPreview: async () => preview([file("ok.json"), file("bad.json", { valid: false, revision: "", error: "Invalid JSON" }), file("missing-revision.json", { revision: "" })]) });
  h.updateInput("relay-only@example.test"); await h.loadRelayPreview();
  assert.deepEqual(plain(h.selectedFiles.value), ["ok.json"]);
  h.selectedFiles.value.push("bad.json");
  assert.equal(h.canExportRelay.value, false);
  await h.doRelayExport();
  assert.equal(h.calls.some((call) => call[0] === "download"), false);
  h.dispose();
});

test("changing email scope invalidates a preview immediately and ignores a late earlier response", async () => {
  let resolveOld;
  let attempts = 0;
  const h = harness({ fetchPreview: () => ++attempts === 1 ? new Promise((resolve) => { resolveOld = resolve; }) : Promise.resolve(preview([file("new.json")])) });
  h.updateInput("old@example.test");
  const old = h.loadRelayPreview();
  h.updateInput("new@example.test");
  assert.equal(h.canExportRelay.value, false);
  await h.loadRelayPreview();
  resolveOld(preview([file("old.json")])); await old;
  assert.deepEqual(plain(h.selectedFiles.value), ["new.json"]);
  h.updateInput("different@example.test");
  assert.equal(h.canExportRelay.value, false);
  assert.equal(h.selectedFiles.value.length, 0);
  h.dispose();
});

test("a file revision conflict clears the selection and requires a fresh preview", async () => {
  const h = harness({ fetchPreview: async () => preview([file("changed.json")]), exportFiles: async () => { throw new Error("文件已变化"); } });
  h.updateInput("relay-only@example.test"); await h.loadRelayPreview(); await h.doRelayExport();
  assert.match(h.relayError.value, /重新加载/);
  assert.equal(h.selectedFiles.value.length, 0);
  assert.equal(h.canExportRelay.value, false);
  h.dispose();
});

function apiHarness(response) {
  const requests = [];
  const downloads = [];
  const source = readFileSync(new URL("../src/new/api/cliproxy.ts", import.meta.url), "utf8");
  class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
  const values = evaluate(source, { "./client": { api: async (...args) => { requests.push(args); return {}; }, apiUrl: (path) => path, jsonBody: JSON.stringify, ApiError } }, {
    fetch: async (path, init) => { requests.push([path, init]); return response; },
    document: { body: { append() {} }, createElement: () => ({ href: "", download: "", click() { downloads.push(this.download); }, remove() {} }) },
    URL: { createObjectURL: () => "blob:synthetic-download", revokeObjectURL() {} },
    setTimeout: () => 1, clearTimeout() {}, location: { replace() {} },
  });
  return { ...values, requests, downloads };
}

test("relay download API refuses an empty file list and missing revision without a request", async () => {
  const h = apiHarness(null);
  await assert.rejects(h.exportCliproxyFiles([], "original", {}), /勾选/);
  await assert.rejects(h.exportCliproxyFiles(["one.json"], "original", {}), /信息不完整/);
  assert.equal(h.requests.length, 0);
});

test("relay download sends exact revisions and preserves attachment filename", async () => {
  const h = apiHarness({ ok: true, status: 200, headers: new Headers({ "Content-Disposition": "attachment; filename*=UTF-8''team%20workspace.json", "X-Exported-Count": "1" }), blob: async () => ({}) });
  const result = await h.exportCliproxyFiles(["one.json"], "original", { "one.json": "revision-a", "unselected.json": "revision-b" });
  assert.equal(h.requests[0][0], "/api/cliproxy/export-files");
  assert.deepEqual(JSON.parse(h.requests[0][1].body), { files: ["one.json"], format: "original", revisions: { "one.json": "revision-a" } });
  assert.deepEqual(h.downloads, ["team workspace.json"]);
  assert.equal(result.filename, "team workspace.json");
  assert.equal(result.exported, 1);
});

test("relay download conflict never creates a blob download", async () => {
  const h = apiHarness({ ok: false, status: 409, statusText: "Conflict", json: async () => ({ detail: "文件在预览后发生变化" }) });
  await assert.rejects(h.exportCliproxyFiles(["one.json"], "original", { "one.json": "old" }), /发生变化/);
  assert.equal(h.downloads.length, 0);
});
