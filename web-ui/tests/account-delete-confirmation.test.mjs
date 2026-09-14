import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import * as pinia from "pinia";
import { compileScript, parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";
import ts from "typescript";

// Execute the real store, handler and confirmation template with synthetic
// account rows. Every account mutation is a local stub; no API is contacted.
const tick = () => new Promise((resolve) => setImmediate(resolve));
const source = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

function evaluate(code, imports, globals = {}) {
  const exports = {};
  const { outputText } = ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  runInNewContext(outputText, {
    exports,
    Error,
    require(id) {
      assert.ok(id in imports, `Unexpected import: ${id}`);
      return imports[id];
    },
    window: { setTimeout: () => 0 },
    ...globals,
  });
  return exports;
}

function createUi() {
  pinia.setActivePinia(pinia.createPinia());
  return evaluate(source("../src/new/stores/ui.ts"), { pinia }).useUiStore();
}

function rows(count) {
  return Array.from({ length: count }, (_, index) => ({
    email: `synthetic-${index}@example.test`, delete_key: `synthetic-${index}`,
  }));
}

function harness(options = {}) {
  const ui = createUi();
  const notices = [];
  const deletes = [];
  let loads = 0;
  ui.notify = (...args) => notices.push(args);
  const accounts = vue.reactive({
    rows: rows(3), total: 3, loading: false, loadError: "",
    async load() {
      loads += 1;
      await options.load?.(accounts, loads);
    },
    async remove(...args) {
      deletes.push(args);
      return options.remove ? options.remove() : { removed: 3 };
    },
    ...options.accounts,
  });
  const script = source("../src/new/views/accounts/AccountToolbar.vue")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const component = evaluate(`${script}\nexport { removeAllAccounts, deletingAll };`, {
    vue,
    "../../stores/accounts": { useAccountsStore: () => accounts },
    "../../stores/groups": { useGroupsStore: () => ({}) },
    "../../stores/batchops": { useBatchOpsStore: () => ({}) },
    "../../stores/ui": { useUiStore: () => ui },
    "../../composables/useConfirm": { useConfirm: () => ui.askConfirm.bind(ui) },
    "../../api/accounts": {},
  }, { defineEmits: () => () => {} });
  return { ...component, ui, accounts, notices, deletes, get loads() { return loads; } };
}

async function openSecondPrompt(h) {
  const completion = h.removeAllAccounts();
  await tick();
  assert.equal(h.ui.confirm.title, "全部删除");
  h.ui.resolveConfirm(true);
  await tick();
  assert.equal(h.ui.confirm.requiredInput, "删除全部");
  return { completion };
}

test("ordinary confirmations retain their warning message and cancel/accept normally", async () => {
  const ui = createUi();
  const pending = ui.askConfirm("将删除 3 个账号并移入回收站。", { title: "删除确认", danger: true });
  assert.equal(ui.confirm.message, "将删除 3 个账号并移入回收站。");
  assert.equal(ui.canConfirm, true);
  ui.resolveConfirm(false);
  assert.equal(await pending, false);
  const accepted = ui.askConfirm("一般确认仍可直接确认");
  ui.resolveConfirm(true);
  assert.equal(await accepted, true);
});

test("typed confirmations reject empty, wrong and whitespace-altered input at the store boundary", async () => {
  const ui = createUi();
  const pending = ui.askConfirm("删除所有分组中的 3 个账号", { requiredInput: "删除全部" });
  for (const input of ["", "确认", " 删除全部", "删除全部 "]) {
    ui.confirm.input = input;
    ui.resolveConfirm(true);
    assert.equal(ui.confirm.open, true);
    assert.equal(ui.canConfirm, false);
  }
  ui.confirm.input = "删除全部";
  assert.equal(ui.canConfirm, true);
  ui.resolveConfirm(true);
  assert.equal(await pending, true);
  assert.equal(ui.confirm.input, "");
});

test("opening another confirmation cancels the previous promise and clears typed approval", async () => {
  const ui = createUi();
  const stale = ui.askConfirm("旧操作", { requiredInput: "删除全部" });
  ui.confirm.input = "删除全部";
  const next = ui.askConfirm("新操作", { requiredInput: "删除全部" });
  assert.equal(await stale, false);
  assert.equal(ui.confirm.input, "");
  assert.equal(ui.canConfirm, false);
  ui.resolveConfirm(false);
  assert.equal(await next, false);
});

test("delete-all displays the newly loaded complete count in both confirmations", async () => {
  const h = harness({ load(accounts) { accounts.rows = rows(7); accounts.total = 7; } });
  const completion = h.removeAllAccounts();
  await tick();
  assert.match(h.ui.confirm.message, /全部 7 个账号/);
  h.ui.resolveConfirm(true);
  await tick();
  assert.match(h.ui.confirm.message, /全部 7 个账号/);
  assert.match(h.ui.confirm.message, /所有分组/);
  assert.equal(h.ui.confirm.confirmText, "删除全部 7 个账号");
  h.ui.resolveConfirm(false);
  await completion;
  assert.equal(h.deletes.length, 0);
  assert.equal(h.deletingAll.value, false);
});

test("cancelling either delete-all confirmation sends no mutation and releases the busy lock", async () => {
  for (const secondStep of [false, true]) {
    const h = harness();
    const completion = h.removeAllAccounts();
    await tick();
    if (secondStep) {
      h.ui.resolveConfirm(true);
      await tick();
    }
    h.ui.resolveConfirm(false);
    await completion;
    assert.equal(h.deletes.length, 0);
    assert.equal(h.deletingAll.value, false);
  }
});

test("repeated toolbar or confirmation clicks cannot skip the typed second step", async () => {
  const h = harness();
  const completion = h.removeAllAccounts();
  await h.removeAllAccounts();
  await tick();
  assert.equal(h.loads, 1);
  h.ui.resolveConfirm(true);
  await tick();
  h.ui.resolveConfirm(true);
  await tick();
  h.ui.resolveConfirm(true);
  await h.removeAllAccounts();
  assert.equal(h.ui.confirm.open, true);
  assert.equal(h.ui.confirm.input, "");
  assert.equal(h.deletes.length, 0);
  h.ui.resolveConfirm(false);
  await completion;
});

test("exact typed approval sends one deletion and stays locked until it finishes", async () => {
  let finish;
  const mutation = new Promise((resolve) => { finish = resolve; });
  const h = harness({ remove: () => mutation });
  const { completion } = await openSecondPrompt(h);
  h.ui.confirm.input = "删除全部";
  h.ui.resolveConfirm(true);
  await tick();
  h.ui.resolveConfirm(true);
  await h.removeAllAccounts();
  assert.equal(h.deletingAll.value, true);
  assert.equal(h.loads, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(h.deletes)), [[[], true]]);
  finish({ removed: 3 });
  await completion;
  assert.equal(h.deletingAll.value, false);
});

test("partial or failed first loads prevent any delete-all confirmation", async () => {
  for (const changes of [{ total: 5 }, { loadError: "synthetic load failure" }]) {
    const h = harness({ accounts: changes });
    await h.removeAllAccounts();
    assert.equal(h.ui.confirm.open, false);
    assert.equal(h.deletes.length, 0);
    assert.equal(h.deletingAll.value, false);
    assert.equal(h.notices[0][1], "error");
  }
});

test("a changed count or failed reload after typed confirmation prevents deletion", async () => {
  for (const failure of ["changed", "failed", "partial"]) {
    const h = harness({ load(accounts, count) {
      if (count !== 2) return;
      if (failure === "changed") { accounts.total = 4; accounts.rows = rows(4); }
      if (failure === "failed") accounts.loadError = "synthetic reload failure";
      if (failure === "partial") accounts.rows = rows(2);
    } });
    const { completion } = await openSecondPrompt(h);
    h.ui.confirm.input = "删除全部";
    h.ui.resolveConfirm(true);
    await completion;
    assert.equal(h.deletes.length, 0);
    assert.equal(h.deletingAll.value, false);
    assert.match(h.notices[0][0], /未执行删除/);
  }
});

test("a rejected deletion is reported once and releases the busy lock", async () => {
  const h = harness({ remove: async () => { throw new Error("synthetic deletion failure"); } });
  const { completion } = await openSecondPrompt(h);
  h.ui.confirm.input = "删除全部";
  h.ui.resolveConfirm(true);
  await completion;
  assert.equal(h.deletes.length, 1);
  assert.equal(h.deletingAll.value, false);
  assert.match(h.notices[0][0], /synthetic deletion failure/);
});

test("the actual dialog renders warning text and an input-gated confirmation button", async () => {
  const ui = createUi();
  const { descriptor } = parse(source("../src/new/components/ui/AppDialog.vue"));
  const script = compileScript(descriptor, { id: "confirmation-test", inlineTemplate: true });
  const AppButton = vue.defineComponent({
    props: { disabled: Boolean },
    setup(props, { slots }) {
      return () => vue.h("button", { type: "button", disabled: props.disabled }, slots.default?.());
    },
  });
  const component = evaluate(script.content, {
    vue, "./AppButton.vue": { default: AppButton },
    "../../stores/ui": { useUiStore: () => ui },
  }).default;
  async function render() {
    const context = {};
    await renderToString(vue.createSSRApp(component), context);
    return context.teleports.body;
  }
  const pending = ui.askConfirm("全部 3 个账号，影响所有分组。", {
    requiredInput: "删除全部", confirmText: "执行删除", danger: true,
  });
  const blocked = await render();
  assert.match(blocked, /全部 3 个账号，影响所有分组/);
  assert.match(blocked, /<input\b/);
  assert.match(blocked, /请输入“删除全部”确认/);
  assert.match(blocked, /<button\b[^>]*disabled[^>]*>执行删除<\/button>/);
  ui.confirm.input = "删除全部";
  assert.doesNotMatch(await render(), /<button\b[^>]*disabled/);
  ui.resolveConfirm(false);
  await pending;
  const ordinary = ui.askConfirm("普通确认正文");
  assert.doesNotMatch(await render(), /<input\b/);
  ui.resolveConfirm(false);
  await ordinary;
});
