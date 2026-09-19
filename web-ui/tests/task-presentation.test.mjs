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
const RouteLink = vue.defineComponent({ props: ["to"], setup: (props, { slots }) => () => vue.h("a", { href: props.to }, slots.default?.()) });
const Pager = vue.defineComponent({
  props: ["page", "pages", "total"],
  setup: (props) => () => vue.h("nav", { "data-page": props.page, "data-pages": props.pages, "data-total": props.total }),
});
const paths = {
  modern: "../src/new/views/tasks/TasksView.vue",
  dashboard: "../src/new/views/DashboardView.vue",
};

function harness(kind, initialItems = []) {
  const url = new URL(paths[kind], import.meta.url);
  const { descriptor } = parse(readFileSync(url, "utf8"), { filename: fileURLToPath(url) });
  const retries = [], confirmations = [];
  const taskStore = vue.reactive({ items: initialItems, job: {}, load: async () => {}, startPolling() {}, stopPolling() {}, retryFailed: async () => retries.push(true) });
  const statusStore = vue.reactive({ panel: { mail: { providers: [] } } });
  const icons = new Proxy({}, { get: () => Passthrough });
  const imports = {
    vue,
    "@lucide/vue": icons,
    "vue-router": { RouterLink: RouteLink },
    "../../components/ui/AppButton.vue": { default: Passthrough },
    "../../components/ui/AppEmpty.vue": { default: Empty },
    "../../components/ui/AppPager.vue": { default: Pager },
    "../../components/ui/AppTag.vue": { default: Passthrough },
    "../components/ui/AppTag.vue": { default: Passthrough },
    "./TaskForm.vue": { default: Empty },
    "../../stores/tasks": { useTasksStore: () => taskStore },
    "../../stores/batchops": { useBatchOpsStore: () => ({ openCodexDialog() {} }) },
    "../stores/tasks": { useTasksStore: () => taskStore },
    "../../stores/groups": { useGroupsStore: () => ({ items: [], load: async () => {} }) },
    "../../stores/status": { useStatusStore: () => statusStore },
    "../stores/status": { useStatusStore: () => statusStore },
    "../../stores/ui": { useUiStore: () => ({ notify() {} }) },
    "../../composables/useConfirm": { useConfirm: () => async (message) => { confirmations.push(message); return true; } },
    "../../composables/useCopy": { useCopy: () => ({ copiedKey: vue.ref(""), copyText: async () => true }) },
    "../../shared/taskPresentation": shared,
    "../../../shared/taskPresentation": shared,
  };
  return {
    taskStore, retries, confirmations,
    async render({ expandCard = false } = {}) {
      const compiled = compileScript(descriptor, { id: `task-display-${kind}`, inlineTemplate: true });
      const component = execute(compiled.content, imports).default;
      if (expandCard) {
        const setup = component.setup;
        component.setup = (props, context) => {
          const render = setup(props, context);
          let opened = false;
          const findExpand = (node) => {
            if (!node || typeof node !== "object") return;
            if (Array.isArray(node)) return node.map(findExpand).find(Boolean);
            if (node.props?.title === "展开步骤与日志") return node;
            return findExpand(node.children);
          };
          return (...args) => {
            const tree = render(...args);
            if (opened) return tree;
            const expand = findExpand(tree);
            assert.ok(expand, "task card exposes its existing steps and logs");
            expand.props.onClick();
            opened = true;
            return render(...args);
          };
        };
      }
      return renderToString(vue.createSSRApp(component));
    },
    script(names = "page, pageCount, pageItems, visibleTasks, selected") {
      const scope = vue.effectScope();
      const result = scope.run(() => execute(`${descriptor.scriptSetup.content}\nexport { ${names} };`, {
        ...imports, vue: { ...vue, onMounted() {}, onBeforeUnmount() {} },
      }));
      return { ...result, dispose: () => scope.stop() };
    },
  };
}

for (const kind of ["modern", "dashboard"]) {
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

for (const kind of ["modern"]) {
  test(`${kind} filtering updates page counts and removes hidden selections after polling`, async () => {
    const items = Array.from({ length: 7 }, (_, index) => card({ card_id: `batch:${index}`, index, status: "queued" }));
    const h = harness(kind, items);
    const s = h.script();
    try {
      s.page.value = 2;
      s.selected.value = new Set(["batch:6", "batch:0"]);
      const updated = [...items.slice(0, 6), card({ status: "cancelled", card_id: "batch:6" })];
      h.taskStore.items = updated;
      await vue.nextTick();
      assert.equal(s.page.value, 1);
      assert.equal(s.visibleTasks.value.length, 6);
      assert.equal(s.pageItems.value.length, 6);
      assert.deepEqual([...s.selected.value], ["batch:0"]);
    } finally { s.dispose(); }
  });
}

// Legacy TaskView was removed; the current task store owns both refreshed rows
// and selection. Keep its archived-status and polling edge cases under test.
test("current task view retains assigned cancellations and real skipped identities", async () => {
  const html = await harness("modern", [
    childPlaceholder(), card({ card_id: "empty-cancelled", status: "cancelled" }),
    card({ card_id: "assigned-cancelled", status: "cancelled", email: "cancelled@example.test", elapsed_sec: 13 }),
    card({ card_id: "existing", status: "skipped", skip_reason: "already_registered", email: "existing@example.test", elapsed_sec: 2 }),
  ]).render();
  assert.match(html, /cancelled@example\.test/);
  assert.match(html, /已取消 \(13s\)/);
  assert.match(html, /existing@example\.test/);
  assert.match(html, /已跳过 \(2s\)/);
  assert.match(html, /data-total="2"/);
  assert.doesNotMatch(html, /未分配邮箱（已取消）|主号未完成，子号未启动/);
});

test("current task view renders historical failure steps when the top-level error is absent", async () => {
  const h = harness("modern", [card({ status: "failed", steps: [
    { status: "failed", text: "earlier mailbox error" },
    { status: "failed", text: "latest mailbox inventory failure" },
  ] })]);
  const html = await h.render();
  assert.match(html, /邮箱分配失败/);
  assert.match(html, /latest mailbox inventory failure/);
  assert.doesNotMatch(html, /等待分配邮箱|正在分配邮箱|title="复制邮箱/);
});

test("polling removes hidden selections for index-only historical rows and resets an empty page", async () => {
  const items = Array.from({ length: 7 }, (_, index) => card({ card_id: "", index: index + 1 }));
  const h = harness("modern", items);
  const s = h.script();
  try {
    s.page.value = 2;
    s.selected.value = new Set(["1", "7"]);
    h.taskStore.items = items.map((item) => ({ ...item, status: "cancelled" }));
    await vue.nextTick();
    assert.equal(s.pageCount.value, 1);
    assert.equal(s.page.value, 1);
    assert.equal(s.pageItems.value.length, 0);
    assert.equal(s.selected.value.size, 0);
    assert.match(await h.render(), /暂无任务记录/);
  } finally { s.dispose(); }
});

test("dashboard selects five visible recent tasks before applying the display limit", async () => {
  const cards = Array.from({ length: 5 }, (_, index) => card({
    card_id: `batch:${index}`, index, email: `visible-${index}@example.test`, status: "success",
  }));
  const html = await harness("dashboard", [childPlaceholder(), card({ status: "cancelled" }), ...cards]).render();
  for (let index = 0; index < 5; index++) assert.match(html, new RegExp(`visible-${index}@example\\.test`));
});

test("registration follow-up status distinguishes waiting, completion, cancellation and interrupted work", async () => {
  const h = harness("modern");
  const states = { pending: "等待接码", running: "正在接码", completed: "接码已完成", failed: "接码失败", cancelled: "接码已取消", interrupted: "接码已中断" };
  for (const [status, label] of Object.entries(states)) {
    h.taskStore.job = { running: false, total: 3, success_count: 3,
      auto_sms: { status, total: 3, completed: 2, failed: 1, message: "本次注册任务的接码结果", ...(status === "pending" ? {} : { batch_id: "sms-one" }) },
    };
    const html = await h.render();
    assert.ok(html.includes(`注册后自动接码 · ${label}`));
    assert.match(html, /已处理 2\/3/);
    assert.match(html, /本次注册任务的接码结果/);
    if (status === "pending") assert.doesNotMatch(html, /查看接码进度/);
    else assert.match(html, /查看接码进度/);
  }
  h.taskStore.job = { running: false, total: 3, auto_sms: { status: "disabled", total: 0 } };
  assert.doesNotMatch(await h.render(), /注册后自动接码/);
});

test("per-account SMS phase labels preserve terminal outcomes and leave legacy registrations unchanged", () => {
  assert.equal(shared.taskSmsLabel(card({ status: "running" })), "");
  const running = card({ status: "running", phase: "sms", registration_status: "success", sms_status: "running" });
  for (const [stage, label] of Object.entries({ acquiring: "获取号码", waiting_sms: "等待短信", verifying_sms: "验证短信", token: "保存 RT", relay_sync: "同步中转" })) {
    assert.equal(shared.taskSmsLabel({ ...running, stage }), label);
  }
  assert.equal(shared.taskSmsLabel({ ...running, sms_status: "pending" }), "等待接码");
  assert.equal(shared.taskSmsLabel({ ...running, stage: "future_stage" }), "正在接码");
  for (const [status, label] of Object.entries({ success: "接码完成", failed: "接码失败", cancelled: "接码已取消" })) {
    assert.equal(shared.taskSmsLabel({ ...running, status, sms_status: status, stage: "waiting_sms" }), label);
    assert.equal(shared.taskSmsNeedsAttention({ ...running, status, sms_status: status }), status !== "success");
  }
  assert.equal(shared.taskSmsNeedsAttention(card({ status: "failed" })), false);
});

test("SMS attempt and countdown metadata remain live only while the account task is running", () => {
  const running = card({ status: "running", phase: "sms", sms_status: "running", stage: "waiting_sms", attempt: 2, max_phone_attempts: 0, wait_deadline: 1090 });
  assert.equal(shared.taskSmsProgress(running, 1000), "取号 2 / 不限 次 · 等码剩余 1:30");
  assert.equal(shared.taskSmsProgress({ ...running, max_phone_attempts: 5 }, 1000), "取号 2 / 5 次 · 等码剩余 1:30");
  assert.match(shared.taskSmsProgress(running, 1091), /等码时间已到，正在更新状态/);
  assert.equal(shared.taskSmsProgress({ ...running, status: "failed" }, 1000), "");
  assert.equal(shared.taskSmsProgress({ ...running, stage: "token", attempt: 0 }, 1000), "");
  assert.equal(shared.taskSmsProgress({ ...running, attempt: NaN, wait_deadline: Infinity }, 1000), "");
});

test("registration card shows authorization cooldown without implying an SMS-code wait", async () => {
  const waiting = card({
    email: "cooldown@example.test", status: "running", phase: "sms", registration_status: "success", sms_status: "running",
    stage: "rate_limit_wait", message: "授权请求限流，同批授权和取号暂缓", wait_deadline: 1090,
  });
  assert.equal(shared.taskSmsLabel(waiting), "授权限流等待");
  assert.equal(shared.taskSmsProgress(waiting, 1000), "授权冷却剩余 1:30");
  assert.equal(shared.taskSmsProgress({ ...waiting, wait_deadline: 1121 }, 1000), "授权冷却剩余 2:01");
  assert.equal(shared.taskSmsProgress(waiting, 1091), "授权冷却时间已到，正在更新状态");
  for (const status of ["success", "failed", "cancelled"]) assert.equal(shared.taskSmsProgress({ ...waiting, status }, 1000), "");
  for (const wait_deadline of [undefined, null, 0, -1, NaN, Infinity]) assert.equal(shared.taskSmsProgress({ ...waiting, wait_deadline }, 1000), "");
  const h = harness("modern", [{ ...waiting, wait_deadline: Math.floor(Date.now() / 1000) + 90 }]);
  h.taskStore.job = { running: true, total: 1, success_count: 0, fail_count: 0, retryable_fail_count: 0 };
  const html = await h.render();
  assert.match(html, /授权限流等待/);
  assert.match(html, /授权冷却剩余/);
  assert.match(html, /授权请求限流，同批授权和取号暂缓/);
  assert.match(html, /注册成功/);
  assert.doesNotMatch(html, /等码剩余|等待短信|接码完成|已结束/);
});

test("registration card identifies a shared batch cooldown and preserves the paused account-stage message", async () => {
  const waiting = card({
    email: "batch-wait@example.test", status: "running", phase: "sms", registration_status: "success", sms_status: "running",
    stage: "batch_rate_limit_wait", message: "已提交号码，因同批其他账号限流暂缓查询短信", wait_deadline: 1090,
  });
  assert.equal(shared.taskSmsLabel(waiting), "批次限流等待");
  assert.equal(shared.taskSmsProgress(waiting, 1000), "批次冷却剩余 1:30");
  assert.equal(shared.taskSmsProgress({ ...waiting, wait_deadline: 1121 }, 1000), "批次冷却剩余 2:01");
  assert.equal(shared.taskSmsProgress(waiting, 1091), "批次冷却时间已到，正在更新状态");
  for (const status of ["success", "failed", "cancelled"]) assert.equal(shared.taskSmsProgress({ ...waiting, status }, 1000), "");
  for (const wait_deadline of [undefined, null, 0, -1, NaN, Infinity]) assert.equal(shared.taskSmsProgress({ ...waiting, wait_deadline }, 1000), "");
  const h = harness("modern", [{ ...waiting, wait_deadline: Math.floor(Date.now() / 1000) + 90 }]);
  h.taskStore.job = { running: true, total: 1, success_count: 0, fail_count: 0, retryable_fail_count: 0 };
  const html = await h.render();
  assert.match(html, /批次限流等待/);
  assert.match(html, /批次冷却剩余/);
  assert.match(html, /已提交号码，因同批其他账号限流暂缓查询短信/);
  assert.match(html, /注册成功/);
  assert.doesNotMatch(html, /授权限流等待|授权冷却剩余|等码剩余|接码完成|已结束/);
});

test("interrupted SMS has an explicit label and keeps the registered account available for manual review", () => {
  const interrupted = card({ phase: "sms", registration_status: "success", sms_status: "interrupted", stage: "waiting_sms" });
  for (const status of ["failed", "cancelled"]) {
    assert.equal(shared.taskSmsLabel({ ...interrupted, status }), "接码已中断");
    assert.equal(shared.taskSmsNeedsAttention({ ...interrupted, status }), true);
  }
  assert.equal(shared.taskSmsNeedsAttention({ ...interrupted, registration_status: "incomplete" }), false);
});

test("the same running registration card displays SMS stage and the combined registration/SMS logs", async () => {
  const h = harness("modern", [card({
    email: "ready@example.test", status: "running", phase: "sms", registration_status: "success", sms_status: "running",
    stage: "waiting_sms", message: "正在等待本账号的短信", attempt: 2, max_phone_attempts: 0,
    wait_deadline: Math.floor(Date.now() / 1000) + 90,
    steps: [{ time: "12:00:00", text: "账号注册与测活完成", status: "running" }, { time: "12:00:01", text: "本账号已取号，等待短信", status: "running" }],
  })]);
  h.taskStore.job = { running: true, total: 1, success_count: 0, fail_count: 0, retryable_fail_count: 0 };
  const html = await h.render({ expandCard: true });
  assert.equal((html.match(/class="task-card is-running"/g) || []).length, 1);
  assert.match(html, /注册成功/);
  assert.match(html, /等待短信/);
  assert.match(html, /正在等待本账号的短信/);
  assert.match(html, /取号 2 \/ 不限 次 · 等码剩余/);
  assert.match(html, /账号注册与测活完成/);
  assert.match(html, /本账号已取号，等待短信/);
  assert.match(html, /0 \/ 1/);
  assert.doesNotMatch(html, /接码完成|已结束|重试注册失败/);
});

test("SMS failure, cancellation and interruption retain registration success and link to the account SMS action", async () => {
  for (const [status, smsStatus, label] of [
    ["failed", "failed", "接码失败"], ["cancelled", "cancelled", "接码已取消"],
    ["failed", "interrupted", "接码已中断"], ["cancelled", "interrupted", "接码已中断"],
  ]) {
    const h = harness("modern", [card({
      email: "registered@example.test", status, phase: "sms", registration_status: "success", sms_status: smsStatus,
      elapsed_sec: 120, error: "短信阶段已结束", stage: "waiting_sms", attempt: 2, wait_deadline: 9999999999,
    })]);
    h.taskStore.job = { running: false, total: 1, fail_count: 1, retryable_fail_count: 0 };
    const html = await h.render();
    assert.match(html, /注册成功/);
    assert.ok(html.includes(`${label} (120s)`));
    assert.match(html, /短信阶段已结束/);
    assert.match(html, /href="\/accounts"[^>]*>到账号页为此账号接码/);
    assert.doesNotMatch(html, /重试注册失败|等码剩余|取号 2/);
    const s = h.script("retryFailed");
    try {
      await s.retryFailed();
      await vue.nextTick();
      assert.equal(h.retries.length, 0);
      assert.equal(h.confirmations.length, 0);
    } finally { s.dispose(); }
  }
});

test("registration retry uses the server's eligible count with a fallback for old snapshots", async () => {
  const h = harness("modern");
  for (const [extra, expected] of [[{ retryable_fail_count: 2 }, 2], [{}, 5]]) {
    h.taskStore.job = { running: false, total: 5, fail_count: 5, ...extra };
    assert.ok((await h.render()).includes(`重试注册失败（${expected}）`));
    const s = h.script("retryFailed, retryableFailCount");
    try {
      assert.equal(s.retryableFailCount.value, expected);
      const before = h.retries.length;
      await s.retryFailed();
      await vue.nextTick();
      assert.equal(h.retries.length, before + 1);
      assert.match(h.confirmations.at(-1), new RegExp(`重试 ${expected} 个注册失败`));
    } finally { s.dispose(); }
  }
  h.taskStore.job.running = true;
  const s = h.script("retryFailed");
  try {
    const before = h.retries.length;
    await s.retryFailed();
    await vue.nextTick();
    assert.equal(h.retries.length, before);
    assert.doesNotMatch(await h.render(), /重试注册失败/);
  } finally { s.dispose(); }
});

test("a fully completed SMS account is terminal with registration success retained", async () => {
  const h = harness("modern", [card({
    email: "complete@example.test", status: "success", phase: "sms", registration_status: "success", sms_status: "success", elapsed_sec: 140,
  })]);
  h.taskStore.job = { running: false, total: 1, success_count: 1, fail_count: 0, retryable_fail_count: 0 };
  const html = await h.render();
  assert.match(html, /注册成功/);
  assert.match(html, /接码完成 \(140s\)/);
  assert.match(html, /1 \/ 1/);
  assert.doesNotMatch(html, /到账号页为此账号接码|重试注册失败/);
});
