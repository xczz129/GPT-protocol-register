import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import ts from "typescript";

// Exercise the real API, store and component scripts using catalog responses only.
// The request stub rejects every route except the read-only catalog/list and config save.
function execute(source, imports, globals = {}) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const exports = {};
  runInNewContext(outputText, {
    exports, Error, Date, setInterval, clearInterval,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
    ...globals,
  });
  return exports;
}

function load(path, imports) {
  return execute(readFileSync(new URL(path, import.meta.url), "utf8"), imports);
}

const currentCatalog = () => ({
  ok: true,
  services: [{ service_id: 242, name: "OpenAi" }],
  types: [{ email_type_id: 26, name: "Gmail_1", price: "0.05", available_num: 10 }],
});

function harness(provider = {}) {
  const calls = [];
  let response = async () => currentCatalog();
  const api = async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : {};
    calls.push({ path, body });
    if (path === "/api/online-disposablemail/services") return response(body);
    if (path === "/api/mailboxes") return { ok: true };
    if (path.startsWith("/api/mailboxes/list?")) return { items: [] };
    throw new Error(`Unexpected request: ${path}`);
  };
  const mailApi = load("../src/new/api/mail.ts", { "./client": { api, jsonBody: JSON.stringify } });
  const { useMailStore } = load("../src/new/stores/mail.ts", {
    vue,
    pinia: { defineStore: (_name, setup) => setup },
    "../api/config": { fetchConfig: async () => ({ config: { mail: { providers: [
      { type: "online_disposablemail", ...provider },
    ] } } }) },
    "../api/mail": mailApi,
  });
  const store = vue.reactive(useMailStore());
  const path = "../src/new/views/register/MailView.vue";
  const source = readFileSync(new URL(path, import.meta.url), "utf8").match(/<script setup(?: lang="ts")?>([\s\S]*?)<\/script>/)[1];
  const scope = vue.effectScope();
  const component = scope.run(() => execute(`${source}\nexport { lookupOdmServices, selectOdmService, odmTypes, odmTypesLoaded, odmLookup, odmSelectedTypeMissing };`, {
    vue: { ...vue, onMounted() {}, onBeforeUnmount() {} },
    "../../api/mail": mailApi,
    "../../stores/mail": { useMailStore: () => store },
    "../../stores/status": { useStatusStore: () => ({}) },
    "../../stores/ui": { useUiStore: () => ({ notify() {} }) },
    "../../composables/useConfirm": { useConfirm: () => async () => false },
  }));
  return {
    ...component, store, calls, mailApi,
    respond: (next) => { response = next; },
    dispose: () => scope.stop(),
  };
}

test("ODM config defaults to Gmail_1 and preserves an explicitly configured type and saved key", async () => {
  const fresh = harness();
  assert.equal(fresh.store.forms.odmEmailTypeId, "26");
  await fresh.store.fillForms();
  assert.equal(fresh.store.forms.odmEmailTypeId, "26");
  fresh.dispose();

  const configured = harness({ email_type_id: "3", has_api_key: true, api_key_masked: "saved…abcd" });
  await configured.store.fillForms();
  assert.equal(configured.store.forms.odmEmailTypeId, "3");
  assert.equal(configured.store.forms.odmApiKey, "");
  assert.equal(configured.store.forms.odmApiKeyConfigured, true);
  await configured.store.saveOdm();
  const saved = configured.calls.find((call) => call.path === "/api/mailboxes").body;
  assert.equal(saved.email_type_id, "3");
  assert.equal(Object.hasOwn(saved, "api_key"), false);
  configured.dispose();
});

test("ODM API requests the selected service and omits an unspecified service or key", async () => {
  const h = harness();
  await h.mailApi.fetchOdmServices("https://mail.example.test", undefined, " 777 ");
  assert.equal(h.calls[0].body.service_id, "777");
  assert.equal(Object.hasOwn(h.calls[0].body, "api_key"), false);
  await h.mailApi.fetchOdmServices("https://mail.example.test");
  assert.equal(Object.hasOwn(h.calls[1].body, "service_id"), false);
  h.dispose();
});

// The retired App.vue no longer owns mail forms. Cover both current credential
// paths through the real store/component, including saved-key hydration.
async function catalogHarness(kind) {
  const h = harness(kind === "saved key" ? { has_api_key: true, api_key_masked: "saved…abcd" } : {});
  await h.store.fillForms();
  if (kind === "entered key") h.store.forms.odmApiKey = "synthetic-catalog-key";
  return h;
}

for (const kind of ["entered key", "saved key"]) {
  test(`${kind} catalog requests the current service without changing the selected SKU`, async () => {
    const h = await catalogHarness(kind);
    h.store.forms.odmServiceId = "777";
    h.store.forms.odmEmailTypeId = "3";
    await h.lookupOdmServices();
    assert.equal(h.calls[0].body.service_id, "777");
    assert.equal(h.calls[0].body.api_key, kind === "entered key" ? "synthetic-catalog-key" : undefined);
    assert.equal(h.store.forms.odmEmailTypeId, "3");
    assert.equal(h.odmTypesLoaded.value, true);
    assert.equal(h.odmSelectedTypeMissing.value, true);
    h.store.forms.odmEmailTypeId = "26";
    assert.equal(h.odmSelectedTypeMissing.value, false);
    h.dispose();
  });

  test(`${kind} switching service clears stale types and refreshes for the chosen service`, async () => {
    const h = await catalogHarness(kind);
    await h.lookupOdmServices();
    assert.equal(h.odmTypes.value.length, 1);
    h.store.forms.odmServiceId = "777";
    assert.equal(h.odmTypes.value.length, 0);
    assert.equal(h.odmTypesLoaded.value, false);
    assert.equal(h.store.forms.odmEmailTypeId, "26");
    await h.selectOdmService({ service_id: 888, name: "Other service" });
    assert.equal(h.calls.at(-1).body.service_id, "888");
    assert.equal(h.store.forms.odmEmailTypeId, "26");
    h.dispose();
  });

  test(`${kind} an older response cannot replace the selected service's catalog`, async () => {
    const h = await catalogHarness(kind);
    let completeOld;
    h.respond(() => new Promise((resolve) => { completeOld = resolve; }));
    const oldRequest = h.lookupOdmServices();
    h.store.forms.odmServiceId = "777";
    h.respond(async () => ({ ok: true, services: [], types: [{ email_type_id: 55, name: "Explicit choice only" }] }));
    await h.lookupOdmServices();
    completeOld(currentCatalog());
    await oldRequest;
    assert.equal(h.odmTypes.value[0].email_type_id, 55);
    assert.equal(h.store.forms.odmEmailTypeId, "26");
    assert.equal(h.odmSelectedTypeMissing.value, true);
    assert.equal(vue.unref(h.odmLookup).loading, false);
    h.dispose();
  });

  test(`${kind} an empty sales catalog stays empty and backend errors remain visible`, async () => {
    const h = await catalogHarness(kind);
    h.respond(async () => ({ ok: true, types: [], services: [] }));
    await h.lookupOdmServices();
    assert.equal(h.odmTypesLoaded.value, true);
    assert.equal(h.odmTypes.value.length, 0);
    assert.equal(h.odmSelectedTypeMissing.value, true);
    h.respond(async () => ({ ok: false, error: "该类型当前不可售" }));
    await h.lookupOdmServices();
    assert.equal(vue.unref(h.odmLookup).error, "该类型当前不可售");
    assert.equal(vue.unref(h.odmLookup).loading, false);
    assert.equal(h.odmTypesLoaded.value, false);
    h.dispose();
  });
}
