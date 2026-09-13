import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function loadModule(path, imports) {
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
  }, { filename: path });
  return exports;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function harness(provider = {}) {
  const saved = [];
  let mailboxLoads = 0;
  const mailApi = {
    async fetchMailboxes() {
      mailboxLoads += 1;
      return [];
    },
    async saveMailboxes(payload) {
      saved.push(plain(payload));
      return { ok: true, total: 0 };
    },
    async deleteMailboxes() { return { ok: true }; },
    async updateMailboxState() { return { ok: true }; },
    async fetchFastmailAccounts() { return { ok: true, items: [] }; },
    async fetchMaddyDomains() { return { ok: true, items: [] }; },
    isMaskedSecret(value) {
      const text = String(value || "").trim();
      return !text || text.includes("…") || text.includes("•") || text.includes("***");
    },
    parseDomainList(value) { return String(value || "").split(/[,;\n]+/).filter(Boolean); },
  };
  const { useMailStore } = loadModule("../src/new/stores/mail.ts", {
    vue: { reactive: (value) => value, ref: (value) => ({ value }) },
    pinia: { defineStore: (_name, setup) => setup },
    "../api/config": {
      fetchConfig: async () => ({ config: { mail: { providers: [{ type: "smsbower_mail", ...provider }] } } }),
    },
    "../api/mail": mailApi,
  });
  return { store: useMailStore(), saved, get mailboxLoads() { return mailboxLoads; } };
}

test("SMSBower config loads without exposing a saved API key", async () => {
  const h = harness({
    enable: true,
    api_base: "https://mail-api.example.test",
    api_key: "saved…7f2a",
    api_key_masked: "saved…7f2a",
    has_api_key: true,
    service: "dr",
    domains: ["icloud.com", "gmail.com"],
    max_price: "0.40",
  });

  await h.store.fillForms();

  assert.equal(h.store.forms.smsbowerEnable, true);
  assert.equal(h.store.forms.smsbowerApiBase, "https://mail-api.example.test");
  assert.equal(h.store.forms.smsbowerApiKey, "");
  assert.equal(h.store.forms.smsbowerApiKeyConfigured, true);
  assert.deepEqual(plain(h.store.forms.smsbowerDomains), ["icloud.com", "gmail.com"]);
  assert.equal(h.store.forms.smsbowerMaxPrice, "0.40");
});

test("even an accidentally unmasked API key response is never copied into the form", async () => {
  const h = harness({ api_key: "raw-secret-that-must-not-render" });
  await h.store.fillForms();
  assert.equal(h.store.forms.smsbowerApiKey, "");
  assert.equal(h.store.forms.smsbowerApiKeyConfigured, true);
});

test("child registration defaults on and an explicitly disabled setting round-trips", async () => {
  const fresh = harness();
  await fresh.store.fillForms();
  assert.equal(fresh.store.forms.smsbowerRegisterChild, true);
  await fresh.store.saveSmsbower();
  assert.equal(fresh.saved[0].register_child, true);

  const disabled = harness({ register_child: false });
  await disabled.store.fillForms();
  assert.equal(disabled.store.forms.smsbowerRegisterChild, false);
  await disabled.store.saveSmsbower();
  assert.equal(disabled.saved[0].register_child, false);
  disabled.store.forms.smsbowerRegisterChild = true;
  await disabled.store.saveSmsbower();
  assert.equal(disabled.saved[1].register_child, true);
});

test("a legacy single-domain config remains selected", async () => {
  const h = harness({ domain: "iCloud.com" });
  await h.store.fillForms();
  assert.deepEqual(plain(h.store.forms.smsbowerDomains), ["icloud.com"]);
});

test("blank API key preserves the saved secret and blank price clears the cap", async () => {
  const h = harness({ has_api_key: true });
  await h.store.fillForms();
  h.store.forms.smsbowerEnable = true;
  h.store.forms.smsbowerApiBase = "";
  h.store.forms.smsbowerDomains = ["gmail.com"];
  h.store.forms.smsbowerMaxPrice = "";

  await h.store.saveSmsbower();

  assert.equal(h.saved.length, 1);
  assert.equal(h.saved[0].provider_type, "smsbower_mail");
  assert.equal(Object.hasOwn(h.saved[0], "api_key"), false);
  assert.equal(Object.hasOwn(h.saved[0], "api_base"), false);
  assert.equal(h.saved[0].max_price, "");
  assert.deepEqual(h.saved[0].domains, ["gmail.com"]);
  assert.equal(h.mailboxLoads, 1);
});

test("a newly entered API key is submitted once and then removed from memory", async () => {
  const h = harness();
  h.store.forms.smsbowerApiKey = "test-mail-key";
  h.store.forms.smsbowerApiBase = "https://mail-api.example.test/";
  h.store.forms.smsbowerService = " dr ";
  h.store.forms.smsbowerDomains = ["gmail.com", "icloud.com"];
  h.store.forms.smsbowerMaxPrice = "0.25";

  await h.store.saveSmsbower();

  assert.equal(h.saved[0].api_key, "test-mail-key");
  assert.equal(h.saved[0].api_base, "https://mail-api.example.test/");
  assert.equal(h.saved[0].service, "dr");
  assert.equal(h.saved[0].max_price, "0.25");
  assert.equal(h.store.forms.smsbowerApiKey, "");
  assert.equal(h.store.forms.smsbowerApiKeyConfigured, true);
});

test("invalid price or an empty domain selection stops before saving", async () => {
  const h = harness();
  h.store.forms.smsbowerDomains = [];
  await assert.rejects(h.store.saveSmsbower(), /至少选择一个/);
  h.store.forms.smsbowerDomains = ["icloud.com"];
  h.store.forms.smsbowerMaxPrice = "-1";
  await assert.rejects(h.store.saveSmsbower(), /最高价/);
  assert.equal(h.saved.length, 0);
  assert.equal(h.mailboxLoads, 0);
});

test("both UIs expose masked SMSBower settings and canonical task/provider choices", () => {
  const legacyMail = readFileSync(new URL("../src/components/MailView.vue", import.meta.url), "utf8");
  const modernMail = readFileSync(new URL("../src/new/views/register/MailView.vue", import.meta.url), "utf8");
  const legacyTask = readFileSync(new URL("../src/components/TaskView.vue", import.meta.url), "utf8");
  const modernTask = readFileSync(new URL("../src/new/views/tasks/TaskForm.vue", import.meta.url), "utf8");
  const modernTaskView = readFileSync(new URL("../src/new/views/tasks/TasksView.vue", import.meta.url), "utf8");
  const legacyApp = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  for (const source of [legacyMail, modernMail]) {
    assert.match(source, /smsbowerApiKey[^\n]*type="password"|type="password"[^\n]*smsbowerApiKey/);
    assert.match(source, /value="gmail\.com"/);
    assert.match(source, /value="icloud\.com"/);
    assert.match(source, /value="smsbower_mail"/);
    assert.match(source, /v-model="(?:mail\.)?forms\.smsbowerRegisterChild"/);
  }
  for (const source of [legacyTask, modernTask]) {
    assert.match(source, /<option value="smsbower_mail">SMSBower 临时邮箱<\/option>/);
  }
  assert.match(legacyApp, /@save-smsbower="saveSmsbowerMail"/);
  assert.match(legacyApp, /provider_type: "smsbower_mail"/);
  assert.match(legacyApp, /register_child: mailForms\.smsbowerRegisterChild/);
  assert.match(modernTaskView, /item\.type === "smsbower_mail"/);
  assert.match(modernTaskView, /SMSBower/);
});
