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

function reauthStore() {
  const responses = [];
  const { outputText } = ts.transpileModule(sourceOf("../src/new/stores/batchops.ts"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const imports = {
    vue,
    pinia: { defineStore: (_name, setup) => setup },
    "../api/batchops": {
      async fetchReauthProgress() {
        assert.ok(responses.length, "Unexpected progress request");
        return responses.shift();
      },
    },
  };
  const exports = {};
  runInNewContext(outputText, {
    exports,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
    setInterval() { assert.fail("Snapshot refresh must not start a timer"); },
    clearInterval() {},
  });
  return { responses, store: exports.useBatchOpsStore() };
}

test("current progress store normalizes skipped independently and resets it for an older-format job", async () => {
  const { responses, store } = reauthStore();
  responses.push({ job: { job_id: "mixed", status: "running", total: 95, done: 1, failed: 0, skipped: "1",
    last: "banned@example.test", last_status: "skipped", last_message: "已封号，跳过重新登录",
    items: [{ email: "banned@example.test", status: "skipped" }] } });
  await store.refreshReauthProgress();
  const reauthProgress = store.reauth;
  assert.equal(reauthProgress.running, true);
  assert.equal(reauthProgress.skipped, 1);
  assert.equal(reauthProgress.failed, 0);
  assert.equal(reauthProgress.lastStatus, "skipped");
  assert.equal(reauthProgress.lastMessage, "已封号，跳过重新登录");

  responses.push({ job: { job_id: "older", status: "running", total: 2, done: 0, failed: 0 } });
  await store.refreshReauthProgress();
  assert.equal(reauthProgress.job_id, "older");
  assert.equal(reauthProgress.running, true);
  assert.equal(reauthProgress.skipped, 0);
  assert.equal(reauthProgress.failed, 0);
  assert.equal(reauthProgress.last, "");
  assert.equal(reauthProgress.lastStatus, "");
  assert.equal(reauthProgress.lastMessage, "");
  assert.equal(reauthProgress.items.length, 0);
  assert.equal(responses.length, 0);
});

test("current batch dialog distinguishes skipped, cancelled, failed and success results", async () => {
  const labels = { skipped: "已跳过", cancelled: "已取消", failed: "失败", success: "成功" };
  const component = dialogComponent({
    operation: "reauth", running: false, status: "completed", total: 4, done: 4,
    success: 1, failed: 1, skipped: 1, cancelled: 1,
    items: Object.keys(labels).map((status) => ({ email: `${status}@example.test`, status, message: `${status} result` })),
  });
  const html = await renderToString(vue.createSSRApp(component, { open: true, kind: "reauth" }));
  for (const [status, label] of Object.entries(labels)) {
    assert.match(html, new RegExp(`class="item-row ${status}"><b>${status}@example\\.test</b><span class="item-status ${status}">${label}</span>`));
  }
  assert.match(html, /成功 1/);
  assert.match(html, /失败 1/);
  assert.match(html, /已跳过 1/);
  assert.match(html, /取消 1/);
  assert.match(html, /4 \/ 4 · 100%/);
});
