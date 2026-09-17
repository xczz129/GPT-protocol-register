import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import { compileScript, parse } from "@vue/compiler-sfc";
import { renderToString } from "vue/server-renderer";
import ts from "typescript";

const sourceOf = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

function dialogComponent(reauth) {
  const filename = "../src/new/views/accounts/BatchJobDialog.vue";
  const { descriptor } = parse(sourceOf(filename), { filename });
  const script = compileScript(descriptor, { id: "reauth-test", inlineTemplate: true });
  const { outputText } = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const slotComponent = (tag) => vue.defineComponent({
    inheritAttrs: false,
    setup(_props, { slots }) { return () => vue.h(tag, slots.default?.()); },
  });
  const batch = { reauth, reauthConnection: "connected", reauthError: "", reauthLastUpdated: 0 };
  const imports = {
    vue,
    "@lucide/vue": { Activity: slotComponent("i") },
    "../../components/ui/AppModal.vue": { default: slotComponent("section") },
    "../../components/ui/AppButton.vue": { default: slotComponent("button") },
    "../../stores/batchops": { useBatchOpsStore: () => batch },
  };
  const exports = {};
  runInNewContext(outputText, {
    exports,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
  });
  return exports.default;
}

for (const skipped of [1, 95]) {
  test(`batch progress renders ${skipped} skipped separately from failures at 100%`, async () => {
    const component = dialogComponent({
      operation: "reauth", running: false, status: "completed", total: 95, done: 95,
      success: 95 - skipped, failed: 0, skipped, cancelled: 0,
      items: [{ email: "banned@example.test", status: "skipped", message: "已封号，跳过重新登录" }],
    });
    const html = await renderToString(vue.createSSRApp(component, { open: true, kind: "reauth" }));
    assert.match(html, new RegExp(`已跳过 ${skipped}`));
    assert.match(html, /失败 0/);
    assert.match(html, /95 \/ 95 · 100%/);
    assert.match(html, /class="item-status skipped">已跳过<\/span>/);
    assert.match(html, /已封号，跳过重新登录/);
    assert.doesNotMatch(html, /运行中…|停止任务/);
  });
}

function legacyFunction(name) {
  const source = sourceOf("../src/App.vue").match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
  const parsed = ts.createSourceFile("App.js", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const declaration = parsed.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(declaration, `Missing legacy function: ${name}`);
  return declaration.getText(parsed);
}

test("legacy progress normalizes skipped independently and resets it for an older job", () => {
  const reauthProgress = {};
  const exports = {};
  runInNewContext(`${legacyFunction("applyReauthProgress")}\nexports.apply = applyReauthProgress;`, {
    exports, reauthProgress, reauthJobId: "", persistReauthUiState() {},
  });
  exports.apply({ job_id: "mixed", status: "running", total: 95, done: 1, failed: 0, skipped: 1,
    last_status: "skipped", items: [{ email: "banned@example.test", status: "skipped" }] });
  assert.equal(reauthProgress.running, true);
  assert.equal(reauthProgress.skipped, 1);
  assert.equal(reauthProgress.failed, 0);
  assert.equal(reauthProgress.lastStatus, "skipped");

  exports.apply({ job_id: "older", status: "running", total: 2, done: 0, failed: 0 });
  assert.equal(reauthProgress.skipped, 0);
  assert.equal(reauthProgress.failed, 0);
  assert.equal(reauthProgress.lastStatus, "");
  assert.equal(reauthProgress.items.length, 0);
});

test("legacy recent result distinguishes skipped, cancelled, failed and success", () => {
  const source = sourceOf("../src/components/AccountsView.vue").match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
  const parsed = ts.createSourceFile("AccountsView.js", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const declaration = parsed.statements.find((node) => ts.isVariableStatement(node)
    && node.declarationList.declarations.some((item) => item.name.getText(parsed) === "reauthLastStatusLabel"));
  assert.ok(declaration);
  const props = vue.reactive({ reauthProgress: { lastStatus: "skipped" } });
  const exports = {};
  runInNewContext(`${declaration.getText(parsed)}\nexports.label = reauthLastStatusLabel;`, {
    exports, props, computed: vue.computed,
  });
  for (const [status, label] of Object.entries({ skipped: "已跳过", cancelled: "已取消", failed: "失败", success: "成功" })) {
    props.reauthProgress.lastStatus = status;
    assert.equal(exports.label.value, label);
  }
});
