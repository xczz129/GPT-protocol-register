import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import ts from "typescript";

const STORAGE_KEY = "gpt-register-fp.protocol-login-input.v1";
const EMAILS = Array.from({ length: 22 }, (_, index) => `protocol-${String(index).padStart(2, "0")}@example.test`);
// All fixtures are synthetic. Eight passwords include @, reproducing the
// former 22 -> 14 parse and the contaminated email lookup without real data.
const CREDENTIAL_LINES = EMAILS.map((email, index) => `${email}----SyntheticSecret${index < 8 ? "@" : "-"}${index}----SYNTH-TOTP-${index}`).join("\n");
const PUBLIC_ROWS = EMAILS.map((email, index) => ({ delete_key: `test-key-${index}`, email, has_password: true, has_totp: true }));
function plain(value) { return JSON.parse(JSON.stringify(value)); }
function assertEmailOnly(value) { assert.doesNotMatch(value, /SyntheticSecret|SYNTH-TOTP|----/); }

function harness(options = {}) {
  const calls = [];
  const mounted = [];
  const stops = [];
  const storageWrites = [];
  const storage = new Map(options.saved ? [[STORAGE_KEY, JSON.stringify(options.saved)]] : []);
  const accounts = vue.reactive({
    rows: PUBLIC_ROWS.map((row) => ({ ...row })), total: 22, loading: false, loadError: "",
    loadProgress: { loaded: 22, total: 22 }, health: { running: false, total: 0 },
    load: options.load || (async () => { calls.push(["load"]); }),
    refreshHealth: async () => { calls.push(["refreshHealth"]); },
    startHealth: async (...args) => { calls.push(["health", ...plain(args)]); },
    ...options.accounts,
  });
  const batch = vue.reactive({
    reauth: { running: false, total: 0, items: [] },
    startProtocolLogin: async (emails, concurrency) => {
      calls.push(["start", [...emails], concurrency]);
      return { job_id: "synthetic-job", submitted: emails.length };
    },
    attachReauthIfRunning: () => { calls.push(["attach"]); },
  });
  let source = readFileSync(new URL("../src/new/views/accounts/ProtocolLoginView.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  source += "\nexport { input,concurrency,onInput,parseEmails,requestedEmails,matched,ready,notFound,rows,selected,allReadySelected,canStart,pasteEmails,clearInput,saveInputState,start,healthCheck };";
  const exports = {};
  const imports = {
    vue: { ...vue, onMounted: (fn) => mounted.push(fn), watch: (...args) => { const stop = vue.watch(...args); stops.push(stop); return stop; } },
    "../../stores/accounts": { useAccountsStore: () => accounts },
    "../../stores/batchops": { useBatchOpsStore: () => batch },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => calls.push(["notify", ...args]) }) },
    "../../composables/useConfirm": { useConfirm: () => async () => true },
  };
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
  runInNewContext(outputText, {
    exports,
    require: (id) => { assert.ok(id in imports, `Unexpected import: ${id}`); return imports[id]; },
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => { if (options.storageFails) throw new Error("Synthetic unavailable storage"); storageWrites.push({ key, value }); storage.set(key, value); },
      removeItem: (key) => storage.delete(key),
    },
    navigator: { clipboard: { readText: async () => options.clipboard ?? CREDENTIAL_LINES } },
  });
  return { ...exports, accounts, batch, calls, storage, storageWrites, mount: () => Promise.all(mounted.map((fn) => fn())), dispose: () => stops.forEach((stop) => stop()) };
}

test("22 credential lines including eight @ passwords produce the exact 22 clean email addresses", () => {
  const h = harness();
  const parsed = h.parseEmails(CREDENTIAL_LINES);
  assert.deepEqual(plain(parsed), EMAILS);
  assert.equal(parsed.length, 22);
  assertEmailOnly(parsed.join("\n"));
  h.dispose();
});

test("clipboard paste immediately discards credential columns and only stores email input", async () => {
  const h = harness();
  await h.pasteEmails();
  assert.equal(h.input.value, EMAILS.join("\n"));
  assert.deepEqual(plain(h.requestedEmails.value), EMAILS);
  await vue.nextTick();
  h.saveInputState();
  assert.ok(h.storageWrites.length > 0);
  for (const write of h.storageWrites) {
    assertEmailOnly(write.value);
    assert.equal(JSON.parse(write.value).input, EMAILS.join("\n"));
  }
  assert.equal(h.calls.some((call) => call[0] === "start"), false);
  h.dispose();
});

test("typing through the textarea model sanitizes credential suffixes while preserving partial email edits", async () => {
  const h = harness();
  h.input.value = "partial-user@";
  assert.equal(h.input.value, "partial-user@");
  assert.equal(h.requestedEmails.value.length, 0);
  h.input.value = "partial-user@example.test----SyntheticSecret@typed----SYNTH-TOTP-typed";
  assert.equal(h.input.value, "partial-user@example.test");
  await vue.nextTick();
  assertEmailOnly(h.storage.get(STORAGE_KEY));
  assert.deepEqual(plain(h.requestedEmails.value), ["partial-user@example.test"]);
  h.dispose();
});

test("plain comma, space, newline and quoted email lists still normalize and deduplicate", () => {
  const h = harness();
  const parsed = h.parseEmails("'FIRST@example.test',second@example.test，FIRST@example.test\n`third@example.test` fourth@example.test; bad-input");
  assert.deepEqual(plain(parsed), ["first@example.test", "second@example.test", "third@example.test", "fourth@example.test"]);
  h.dispose();
});

test("pasting credentials for the same email clears the DOM suffix even when the model value does not change", () => {
  const h = harness();
  h.input.value = EMAILS[0];
  const target = { value: `${EMAILS[0]}----SyntheticSecret@replacement----SYNTH-TOTP-replacement` };
  h.onInput({ target });
  assert.equal(h.input.value, EMAILS[0]);
  assert.equal(target.value, EMAILS[0]);
  assertEmailOnly(target.value);
  h.dispose();
});

test("escaped at-signs and Windows or carriage-return credential rows retain every email", () => {
  const h = harness();
  const rows = [
    "first\\@example.test----SyntheticSecret@one----SYNTH-TOTP-one",
    "second@example.test----SyntheticSecret-two----SYNTH-TOTP-two",
    "third@example.test----SyntheticSecret-three----SYNTH-TOTP-three",
  ];
  assert.deepEqual(plain(h.parseEmails(`${rows[0]}\r\n${rows[1]}\r${rows[2]}`)), ["first@example.test", "second@example.test", "third@example.test"]);
  h.dispose();
});

test("legacy v1 credential cache is rewritten immediately before awaiting account loading", async () => {
  let resolveLoad;
  const h = harness({
    saved: { input: CREDENTIAL_LINES, concurrency: 4 },
    accounts: { rows: [], total: 0, loadProgress: { loaded: 0, total: 0 } },
    load: () => new Promise((resolve) => { resolveLoad = resolve; }),
  });
  const mount = h.mount();
  assert.equal(h.input.value, EMAILS.join("\n"));
  // This assertion precedes both the load promise and the next Vue tick.
  const persisted = h.storage.get(STORAGE_KEY);
  assertEmailOnly(persisted);
  assert.equal(JSON.parse(persisted).input, EMAILS.join("\n"));
  assert.equal(h.concurrency.value, 4);
  resolveLoad();
  await mount;
  for (const write of h.storageWrites) assertEmailOnly(write.value);
  h.dispose();
});

test("unavailable browser storage cannot prevent safe in-memory input or restore", async () => {
  const h = harness({ storageFails: true, saved: { input: CREDENTIAL_LINES, concurrency: 3 } });
  await h.mount();
  assert.equal(h.input.value, EMAILS.join("\n"));
  assert.doesNotThrow(() => h.saveInputState());
  assert.equal(h.requestedEmails.value.length, 22);
  assert.equal(h.storage.has(STORAGE_KEY), false, "Failed legacy cache rewriting must remove the old credential-bearing value when deletion is available");
  h.dispose();
});

test("full public account loading matches all 22 sanitized credential-line emails", () => {
  const h = harness();
  h.input.value = CREDENTIAL_LINES;
  assert.equal(h.matched.value.length, 22);
  assert.equal(h.ready.value.length, 22);
  assert.equal(h.notFound.value.length, 0);
  assert.deepEqual(plain(h.rows.value.map((row) => row.email)), EMAILS);
  h.dispose();
});

test("partial loading does not report false not-found results or permit login or health requests", async () => {
  const h = harness({ accounts: { rows: PUBLIC_ROWS.slice(0, 14), total: 22, loading: true, loadProgress: { loaded: 14, total: 22 } } });
  h.input.value = CREDENTIAL_LINES;
  h.selected.value = [...EMAILS];
  assert.equal(h.matched.value.length, 0);
  assert.equal(h.notFound.value.length, 0);
  assert.equal(h.canStart.value, false);
  await h.start(); await h.healthCheck();
  assert.equal(h.calls.some((call) => ["start", "health"].includes(call[0])), false);
  h.accounts.loading = false;
  assert.equal(h.canStart.value, false, "A short page without an exception is still incomplete");
  await h.healthCheck();
  assert.equal(h.calls.some((call) => call[0] === "health"), false);
  h.accounts.rows = PUBLIC_ROWS.map((row) => ({ ...row }));
  h.accounts.loadProgress = { loaded: 22, total: 22 };
  assert.equal(h.matched.value.length, 22);
  assert.equal(h.notFound.value.length, 0);
  assert.equal(h.canStart.value, true);
  h.dispose();
});

test("a later refresh or page error blocks previously selected login and health actions", async () => {
  const h = harness();
  h.input.value = CREDENTIAL_LINES;
  h.selected.value = [...EMAILS];
  assert.equal(h.canStart.value, true);
  h.accounts.loadError = "Synthetic page read error";
  assert.equal(h.canStart.value, false);
  await h.start(); await h.healthCheck();
  assert.equal(h.calls.some((call) => ["start", "health"].includes(call[0])), false);
  h.dispose();
});

test("login sends only clean emails plus concurrency and uses only credentials already present in the account store", async () => {
  const h = harness();
  h.input.value = CREDENTIAL_LINES + "\nnot-in-db@example.test----SyntheticSecret@new----SYNTH-TOTP-new";
  h.accounts.rows = h.accounts.rows.map((row, index) => index === 1 ? { ...row, has_password: false } : row);
  h.selected.value = [...EMAILS, "not-in-db@example.test"];
  h.concurrency.value = 4;
  await h.start();
  const request = h.calls.find((call) => call[0] === "start");
  assert.deepEqual(request, ["start", EMAILS.filter((_email, index) => index !== 1), 4]);
  assertEmailOnly(JSON.stringify(request));
  assert.equal(h.accounts.rows.length, 22, "Pasted unknown emails must not create account records");
  assert.equal(h.accounts.rows[1].has_password, false, "Pasted credentials must not repair missing stored credentials");
  h.dispose();
});
