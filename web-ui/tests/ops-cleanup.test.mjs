import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import { parse, compileScript } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";
import ts from "typescript";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
function execute(source, imports = {}, globals = {}) {
  const exports = {};
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
  runInNewContext(outputText, { exports, Error, require(name) { assert.ok(name in imports, `Unexpected import ${name}`); return imports[name]; }, ...globals });
  return exports;
}
function script(path, names, imports = {}, globals = {}) {
  const source = read(path).match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  return execute(`${source}\nexport { ${names.join(", ")} };`, { vue, ...imports }, globals);
}

test("all five saved operation defaults accept large integers without clamping", async () => {
  const requests = [], notices = [];
  const h = script("../src/new/views/system/OpsSettings.vue", ["workersForm", "saveSettings"], {
    vue: { ...vue, onMounted() {} },
    "../../api/settings": { updateUiSettings: async (body) => requests.push(plain(body)) },
    "../../api/auth": {},
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => notices.push(args) }) },
  }, { defineProps: () => ({ cards: ["workers"] }), withDefaults: (props) => props });
  const names = ["health_workers", "reauth_workers", "trial_workers", "refresh_at_workers", "codex_sms_workers"];
  for (const name of names) h.workersForm[name] = 275;
  await h.saveSettings();
  assert.equal(requests.length, 1);
  for (const name of names) assert.equal(requests[0][name], 275);
  for (const name of names) {
    h.workersForm[name] = 1.5;
    await h.saveSettings();
    h.workersForm[name] = 275;
  }
  assert.equal(requests.length, 1);
  assert.equal(notices.filter(([message]) => /正整数/.test(message)).length, 5);
});

test("raw token trial batching uses the requested concurrency above twenty", async () => {
  const requests = [];
  const h = execute(read("../src/new/stores/trial.ts"), {
    vue, pinia: { defineStore: (_name, setup) => setup },
    "../api/trial": { checkTrialTokens: async (...args) => { requests.push(args); return { results: args[0].map((token) => ({ token_tail: token.slice(-10), ok: true })) }; } },
  }).useTrialStore();
  h.form.workers = 275;
  h.form.batchTokens = Array.from({ length: 65 }, (_, i) => `synthetic-access-token-for-testing-${i}`).join("\n");
  await h.startBatch();
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0].length, 65);
  assert.equal(requests[0][3], 275);
  assert.equal(h.batch.done, 65);
  h.form.workers = 0;
  await assert.rejects(h.startBatch(), /正整数/);
});

function pickerHarness(items = []) {
  const props = vue.reactive({ open: true, items, selected: [], providers: {}, loading: false, error: "" });
  const events = [];
  const h = script("../src/new/components/sms/SmsCountryPicker.vue", ["lowestPrice", "filtered", "minPrice", "maxPrice", "toggleTier", "providerList", "formatPrice", "formatStock"], {}, {
    defineProps: () => props, withDefaults: (value) => value,
    defineEmits: () => (...args) => events.push(plain(args)),
  });
  return { ...h, props, events };
}

test("country minimum is the numeric minimum among valid in-stock tiers", () => {
  const h = pickerHarness();
  assert.equal(h.lowestPrice({ min_price: "0.01", price: "0.02", tiers: [
    { id: "expensive", price: "10", count: 10 }, { id: "lowest", price: "0.24", count: 2 },
    { id: "numeric", price: "2", count: 5 }, { id: "empty", price: "0.01", count: 0 },
    { id: "missing", price: undefined, count: 3 }, { id: "zero", price: 0, count: 2 },
    { id: "bad", price: NaN, count: 3 }, { id: "infinite", price: Infinity, count: 3 },
    { id: "negative", price: -1, count: 3 },
  ] }), 0.24);
  assert.equal(h.lowestPrice({ tiers: [{ price: 0.24, count: 0 }], min_price: "0.10" }), null);
  assert.equal(h.lowestPrice({ tiers: [{ price: 0, count: 2 }], min_price: "0.10" }), null);
  assert.equal(h.lowestPrice({ min_price: "0.004", price: "999" }), 0.004);
  assert.equal(h.lowestPrice({ tiers: [], min_price: "0.24" }), 0.24);
  for (const price of [undefined, null, "", "bad", "0", -1, Infinity, true]) assert.equal(h.lowestPrice({ min_price: price }), null);
  assert.equal(h.formatPrice(0.0004), "0.0004");
});

test("tier stock preserves exact counts including zero and marks invalid counts unknown", () => {
  const h = pickerHarness();
  assert.equal(h.formatStock(2385000), "2,385,000 件");
  assert.equal(h.formatStock("1200"), "1,200 件");
  assert.equal(h.formatStock(0), "0 件");
  for (const value of [undefined, null, "", " ", "bad", NaN, Infinity, -1, 1.5, true, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(h.formatStock(value), "库存未知");
  }
});

test("price filtering follows the displayed minimum and tier order selection still works", () => {
  const h = pickerHarness([
    { id: "6", name: "巴西", calling: "+55", price: "9", tiers: [{ id: "b", price: 0.24, count: 1 }] },
    { id: "4", name: "其他", price: "0.01", min_price: "0.50" },
  ]);
  h.maxPrice.value = "0.25";
  assert.deepEqual(h.filtered.value.map((item) => item.id), ["6"]);
  h.props.providers["6"] = "first|second";
  h.toggleTier("6", "third");
  assert.deepEqual(h.events[0], ["set-provider", { id: "6", providerId: "first|second|third" }]);
  h.toggleTier("6", "first");
  assert.deepEqual(h.events[1], ["set-provider", { id: "6", providerId: "second" }]);
});

test("country cards render the exact lowest price with the country and calling code, without stock counts", async () => {
  const { descriptor } = parse(read("../src/new/components/sms/SmsCountryPicker.vue"));
  const code = compileScript(descriptor, { id: "country-price-test", inlineTemplate: true }).content;
  const Icon = vue.defineComponent({ render: () => vue.h("i") });
  const component = execute(code, { vue, "@lucide/vue": new Proxy({}, { get: () => Icon }) }).default;
  const context = {};
  const app = vue.createSSRApp(component, { open: true, items: [{ id: "6", name: "巴西", calling: "+55", price: "9", count: 2385000, tiers: [{ id: "1", price: 0.24, count: 300 }, { id: "2", price: 0.12, count: 0 }] }] });
  await renderToString(app, context);
  const html = context.teleports.body;
  assert.match(html, /巴西/);
  assert.match(html, /\+55/);
  assert.match(html, /最低 \$0\.24/);
  assert.match(html, /价位/);
  assert.doesNotMatch(html, /≈|2385|件|火狐狸/);
});

test("expanded price tiers show their own inventory while the country row has no aggregate inventory", async () => {
  const { descriptor } = parse(read("../src/new/components/sms/SmsCountryPicker.vue"));
  const code = compileScript(descriptor, { id: "country-stock-test", inlineTemplate: true }).content;
  const Icon = vue.defineComponent({ render: () => vue.h("i") });
  const component = execute(code, { vue, "@lucide/vue": new Proxy({}, { get: () => Icon }) }).default;
  const setup = component.setup;
  component.setup = (props, context) => {
    const render = setup(props, context);
    const findExpand = (node) => {
      if (!node || typeof node !== "object") return;
      if (Array.isArray(node)) return node.map(findExpand).find(Boolean);
      if (String(node.props?.class || "").includes("picker-tier-btn")) return node;
      return findExpand(typeof node.children?.default === "function" ? node.children.default() : node.children);
    };
    let expanded = false;
    return (...args) => {
      const tree = render(...args);
      if (expanded) return tree;
      const expand = findExpand(tree);
      assert.ok(expand, "the country exposes its price-tier expansion control");
      expand.props.onClick({ stopPropagation() {} });
      expanded = true;
      return render(...args);
    };
  };
  const context = {};
  await renderToString(vue.createSSRApp(component, { open: true, items: [{
    id: "6", name: "巴西", calling: "+55", count: 2385000,
    tiers: [
      { id: "1", price: 0.24, count: 12345 },
      { id: "2", price: 0.12, count: 0 },
      { id: "3", price: 0.3, count: "2500" },
      { id: "4", price: 0.4, count: NaN },
    ],
  }] }), context);
  const html = context.teleports.body;
  const countryRow = html.match(/<div[^>]*class="picker-row[^>]*>[\s\S]*?<\/div>/)?.[0];
  assert.ok(countryRow);
  assert.match(countryRow, /巴西[\s\S]*\+55[\s\S]*最低 \$0\.24/);
  assert.doesNotMatch(countryRow, /件|库存|2385|2,385,000/);
  assert.match(html, /#1 · 12,345 件/);
  assert.match(html, /#2 · 0 件/);
  assert.match(html, /#3 · 2,500 件/);
  assert.match(html, /#4 · 库存未知/);
  assert.doesNotMatch(html, /2385000|2,385,000|2385k|≈/);
});

test("country API always requests the supported SMS provider", async () => {
  const calls = [];
  const api = execute(read("../src/new/api/ops.ts"), { "./client": { api: async (...args) => { calls.push(args); return { items: [] }; } } });
  await api.fetchSmsCountries();
  assert.equal(calls[0][0], "/api/sms-countries?provider=smsbower");
  assert.equal(api.clearSmsDockingHistory, undefined);
});
