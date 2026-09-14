import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { compileScript, parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";
import * as vue from "vue";
import ts from "typescript";

function execute(source, imports = {}, globals = {}) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const exports = {};
  runInNewContext(outputText, {
    exports, Date, Set,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
    ...globals,
  });
  return exports;
}

const shared = execute(readFileSync(new URL("../src/shared/taskPresentation.ts", import.meta.url), "utf8"));
const registration = execute(readFileSync(new URL("../src/shared/mailRegistration.ts", import.meta.url), "utf8"));
function card(fields = {}) {
  return {
    card_id: "batch:1", index: 1, email: "", provider: "", proxy: "", status: "queued",
    started_at_epoch: 0, elapsed_sec: 0, batch_started_at: "", steps: [], ...fields,
  };
}
const childPlaceholder = () => card({
  card_id: "batch:child-placeholder", index: 2, status: "skipped",
  skip_reason: "parent_not_registered", mailbox_role: "child",
  steps: [{ time: "13:00:00", status: "skipped", text: "主号未完成，子号未启动" }],
});

test("cancelled unallocated slots and explicitly unstarted children are hidden", () => {
  assert.equal(shared.isVisibleTask(childPlaceholder()), false);
  assert.equal(shared.isVisibleTask({ ...childPlaceholder(), status: "cancelled" }), false);
  assert.equal(shared.isVisibleTask(card({ status: "cancelled" })), false);
  assert.equal(shared.isVisibleTask(card({ status: "cancelled", email: "   ", error: "stop requested" })), false);
  assert.equal(shared.isVisibleTask(card({ status: "cancelled", email: "assigned@example.test" })), true);
  for (const fields of [
    { email: "child@example.test" }, { error: "provider request failed" },
    { status: "failed" }, { status: "queued" }, { status: "running" },
    { skip_reason: "already_registered" }, { skip_reason: "" },
    { steps: [{ status: "failed", text: "allocation timed out" }] },
  ]) assert.equal(shared.isVisibleTask({ ...childPlaceholder(), ...fields }), true, JSON.stringify(fields));
});

test("empty task titles reflect the current state and never imply allocation after termination", () => {
  const expected = {
    queued: "等待分配邮箱", running: "正在分配邮箱", failed: "邮箱分配失败",
    cancelled: "未分配邮箱（已取消）", skipped: "未分配邮箱（已跳过）", success: "未记录邮箱",
  };
  for (const [status, label] of Object.entries(expected)) {
    assert.equal(shared.taskEmailLabel(card({ status })), label);
    assert.equal(shared.taskEmailLabel(card({ status, email: " user@example.test " })), "user@example.test");
  }
  assert.equal(shared.taskEmailLabel(card({ status: "unknown" })), "未分配邮箱");
});

test("terminal task errors retain the API reason and historical failed-step details", () => {
  assert.equal(shared.taskFailureDetail(card({ status: "failed", error: "upstream_timeout" })), "upstream_timeout");
  assert.equal(shared.taskFailureDetail(card({ status: "failed", steps: [
    { status: "failed", text: "earlier error" }, { status: "failed", text: "latest error" },
  ] })), "latest error");
  assert.equal(shared.taskFailureDetail(card({ status: "running", error: "old attempt error" })), "");
  assert.equal(shared.taskFailureDetail(childPlaceholder()), "");
});

const Empty = vue.defineComponent({ props: ["title"], setup: (props) => () => vue.h("div", props.title) });
const Passthrough = vue.defineComponent({ setup: (_, { slots }) => () => vue.h("span", slots.default?.()) });
const Pager = vue.defineComponent({
  props: ["page", "pages", "total"],
  setup: (props) => () => vue.h("nav", { "data-page": props.page, "data-pages": props.pages, "data-total": props.total }),
});
const paths = {
  modern: "../src/new/views/tasks/TasksView.vue",
  legacy: "../src/components/TaskView.vue",
  dashboard: "../src/new/views/DashboardView.vue",
};

function harness(kind, initialItems = []) {
  const url = new URL(paths[kind], import.meta.url);
  const { descriptor } = parse(readFileSync(url, "utf8"), { filename: fileURLToPath(url) });
  const taskStore = vue.reactive({ items: initialItems, job: {}, load: async () => {}, startPolling() {}, stopPolling() {} });
  const statusStore = vue.reactive({ panel: { mail: { providers: [] } } });
  const props = vue.reactive({ tasks: initialItems, status: {}, groups: [], form: { total: 2, concurrency: 2, mailType: "smsbower_mail" } });
  const icons = new Proxy({}, { get: () => Passthrough });
  const imports = {
    vue,
    "@lucide/vue": icons,
    "vue-router": { RouterLink: Passthrough },
    "./AppPager.vue": { default: Pager },
    "../../components/ui/AppButton.vue": { default: Passthrough },
    "../../components/ui/AppEmpty.vue": { default: Empty },
    "../../components/ui/AppPager.vue": { default: Pager },
    "../../components/ui/AppTag.vue": { default: Passthrough },
    "../components/ui/AppTag.vue": { default: Passthrough },
    "./TaskForm.vue": { default: Empty },
    "../../stores/tasks": { useTasksStore: () => taskStore },
    "../stores/tasks": { useTasksStore: () => taskStore },
    "../../stores/groups": { useGroupsStore: () => ({ items: [], load: async () => {} }) },
    "../../stores/status": { useStatusStore: () => statusStore },
    "../stores/status": { useStatusStore: () => statusStore },
    "../../stores/ui": { useUiStore: () => ({ notify() {} }) },
    "../../composables/useConfirm": { useConfirm: () => async () => false },
    "../../composables/useCopy": { useCopy: () => ({ copiedKey: vue.ref(""), copyText: async () => true }) },
    "../shared/mailRegistration": registration,
    "../shared/taskPresentation": shared,
    "../../shared/taskPresentation": shared,
    "../../../shared/taskPresentation": shared,
  };
  return {
    taskStore, props,
    async render() {
      const compiled = compileScript(descriptor, { id: `task-display-${kind}`, inlineTemplate: true });
      const component = execute(compiled.content, imports).default;
      return renderToString(vue.createSSRApp(component, kind === "legacy" ? props : {}));
    },
    script() {
      const names = kind === "legacy" ? "page, pages, pageItems, visibleTasks, selected" : "page, pageCount, pageItems, visibleTasks, selected";
      const scope = vue.effectScope();
      const result = scope.run(() => execute(`${descriptor.scriptSetup.content}\nexport { ${names} };`, {
        ...imports, vue: { ...vue, onMounted() {}, onBeforeUnmount() {} },
      }, { defineProps: () => props, defineEmits: () => () => {} }));
      return { ...result, dispose: () => scope.stop() };
    },
  };
}

for (const kind of ["modern", "legacy", "dashboard"]) {
  test(`${kind} task view hides unstarted child cards while displaying a real failed account`, async () => {
    const h = harness(kind, [childPlaceholder(), card({ card_id: "batch:cancelled", status: "cancelled" }), card({
      card_id: "batch:parent", email: "parent@example.test", status: "failed", elapsed_sec: 247,
      error: "provider timeout after 247 seconds",
    })]);
    const html = await h.render();
    assert.match(html, /parent@example\.test/);
    assert.match(html, /provider timeout after 247 seconds/);
    assert.doesNotMatch(html, /已跳过|（分配中）|未分配邮箱（已取消）|主号未完成，子号未启动/);
    if (kind !== "dashboard") assert.match(html, /data-total="1"/);
  });

  test(`${kind} task view retains an allocation failure with no email`, async () => {
    const h = harness(kind, [card({ status: "failed", error: "mail inventory unavailable" })]);
    const html = await h.render();
    assert.match(html, /邮箱分配失败/);
    assert.match(html, /mail inventory unavailable/);
    assert.doesNotMatch(html, /title="复制邮箱|aria-label="复制邮箱|class="interactive-email"|等待分配邮箱|正在分配邮箱|（分配中）/);
  });
}

for (const kind of ["modern", "legacy"]) {
  test(`${kind} filtering updates page counts and removes hidden selections after polling`, async () => {
    const items = Array.from({ length: 7 }, (_, index) => card({ card_id: `batch:${index}`, index, status: "queued" }));
    const h = harness(kind, items);
    const s = h.script();
    try {
      s.page.value = 2;
      s.selected.value = kind === "modern" ? new Set(["batch:6", "batch:0"]) : ["batch:6", "batch:0"];
      const updated = [...items.slice(0, 6), card({ status: "cancelled", card_id: "batch:6" })];
      h.taskStore.items = updated;
      h.props.tasks = updated;
      await vue.nextTick();
      assert.equal(s.page.value, 1);
      assert.equal(s.visibleTasks.value.length, 6);
      assert.equal(s.pageItems.value.length, 6);
      assert.deepEqual([...s.selected.value], ["batch:0"]);
    } finally { s.dispose(); }
  });
}

test("dashboard selects five visible recent tasks before applying the display limit", async () => {
  const cards = Array.from({ length: 5 }, (_, index) => card({
    card_id: `batch:${index}`, index, email: `visible-${index}@example.test`, status: "success",
  }));
  const html = await harness("dashboard", [childPlaceholder(), card({ status: "cancelled" }), ...cards]).render();
  for (let index = 0; index < 5; index++) assert.match(html, new RegExp(`visible-${index}@example\\.test`));
});
