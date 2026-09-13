import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as vue from "vue";
import { compileScript, parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";
import ts from "typescript";

function execute(source, imports = {}, globals = {}) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const exports = {};
  runInNewContext(outputText, {
    exports,
    Error,
    require(name) {
      assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`);
      return imports[name];
    },
    ...globals,
  });
  return exports;
}

const shared = execute(readFileSync(new URL("../src/shared/mailRegistration.ts", import.meta.url), "utf8"));
const plain = (value) => JSON.parse(JSON.stringify(value));

test("SMSBower account slots are even, bounded and include both the main and child", () => {
  for (const [total, concurrency, expected] of [
    [1, 1, { total: 2, concurrency: 2 }],
    [3, 3, { total: 4, concurrency: 4 }],
    [10, 4, { total: 10, concurrency: 4 }],
    [501, 65, { total: 500, concurrency: 64 }],
    ["", NaN, { total: 2, concurrency: 2 }],
    [-3, Infinity, { total: 2, concurrency: 2 }],
  ]) {
    assert.deepEqual(plain(shared.normalizeRegistrationNumbers({ mailType: "smsbower_mail", total, concurrency })), expected);
  }
});

test("turning off children permits ordinary odd and single-worker concurrency", () => {
  assert.deepEqual(plain(shared.normalizeRegistrationNumbers({ mailType: "smsbower_mail", total: 1, concurrency: 3 }, false)), { total: 1, concurrency: 3 });
  assert.deepEqual(plain(shared.normalizeRegistrationNumbers({ mailType: "smsbower_mail", total: 1, concurrency: 1 }, false)), { total: 1, concurrency: 1 });
  assert.deepEqual(plain(shared.normalizeRegistrationNumbers({ mailType: "icloud", total: 3, concurrency: 1 })), { total: 3, concurrency: 1 });
  assert.equal(shared.smsbowerPolicy("smsbower_mail", false).paired, false);
  assert.equal(shared.smsbowerPolicy("smsbower_mail").paired, true);
  assert.equal(shared.smsbowerPolicy("auto").selected, false);
});

function taskFormHarness(loadError = false) {
  const source = readFileSync(new URL("../src/new/views/tasks/TaskForm.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
  const sent = [];
  const notifications = [];
  const status = vue.reactive({ panel: { mail: { providers: [{ type: "smsbower_mail", register_child: true }] } } });
  status.load = async () => {
    if (loadError) throw new Error("状态刷新失败");
    status.panel.mail.providers[0].register_child = false;
  };
  const scope = vue.effectScope();
  const result = scope.run(() => execute(`${source}\nexport { form, submit, busy, registrationPolicy };`, {
    vue,
    "../../stores/tasks": { useTasksStore: () => ({ job: { running: false }, start: async (body) => sent.push(plain(body)) }) },
    "../../stores/groups": { useGroupsStore: () => ({ items: [] }) },
    "../../stores/ui": { useUiStore: () => ({ notify: (...args) => notifications.push(args) }) },
    "../../stores/status": { useStatusStore: () => status },
    "../../../shared/mailRegistration": shared,
  }));
  return { ...result, sent, notifications, dispose: () => scope.stop() };
}

test("the real task form normalizes selection and uses the latest saved child setting at submit", async () => {
  const h = taskFormHarness();
  try {
    h.form.mailType = "smsbower_mail";
    await vue.nextTick();
    assert.equal(h.form.total, 2);
    assert.equal(h.form.concurrency, 2);
    assert.equal(h.registrationPolicy.value.paired, true);
    h.form.total = 3;
    h.form.concurrency = 1;
    await h.submit();
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].total, 3);
    assert.equal(h.sent[0].concurrency, 1);
    assert.equal(h.registrationPolicy.value.paired, false);
    assert.equal(h.busy.value, false);
  } finally { h.dispose(); }
});

test("task creation stops when the saved mailbox setting cannot be refreshed", async () => {
  const h = taskFormHarness(true);
  try {
    h.form.mailType = "smsbower_mail";
    await h.submit();
    assert.equal(h.sent.length, 0);
    assert.equal(h.notifications[0][0], "状态刷新失败");
    assert.equal(h.busy.value, false);
  } finally { h.dispose(); }
});

const outcomeKeys = ["success", "already_registered", "otp_timeout", "other_failed", "cancelled"];
function summary(overrides = {}) {
  const totals = { attempted: 10, success: 4, already_registered: 2, otp_timeout: 1, other_failed: 1, cancelled: 1, in_progress: 1, ...overrides };
  return {
    totals,
    rates: Object.fromEntries(outcomeKeys.map((key) => [key, { numerator: totals[key], denominator: totals.attempted, value: totals.attempted ? totals[key] / totals.attempted : null }])),
    otp_wait: { received_count: 4, timeout_count: 1, average_seconds: 28.6, min_seconds: 0, max_seconds: 77.2 },
  };
}

function statsFixture() {
  return {
    ok: true, provider: "smsbower_mail", ...summary(), updated_at: "2026-09-14T01:00:00Z",
    by_domain: [{ domain: "gmail.com", ...summary() }],
    by_role: [
      { role: "primary", ...summary({ attempted: 8, success: 3, cancelled: 0 }) },
      { role: "child", ...summary({ attempted: 2, success: 1, already_registered: 0, otp_timeout: 0, other_failed: 0, in_progress: 0 }) },
    ],
    groups: [
      { provider: "smsbower_mail", domain: "gmail.com", role: "primary", ...summary({ attempted: 8, success: 3, cancelled: 0 }) },
      { provider: "smsbower_mail", domain: "icloud.com", role: "child", ...summary({ attempted: 2, success: 1, already_registered: 0, otp_timeout: 0, other_failed: 0, in_progress: 0 }) },
    ],
  };
}

test("rate and wait labels distinguish no samples from a measured zero", () => {
  assert.equal(shared.formatRegistrationRate(null), "—");
  assert.equal(shared.formatRegistrationRate(0), "0.0%");
  assert.equal(shared.formatRegistrationRate(0.257), "25.7%");
  assert.equal(shared.formatOtpSeconds(null), "—");
  assert.equal(shared.formatOtpSeconds(0), "0.0 秒");
  assert.equal(shared.formatOtpSeconds(180.15), "180.2 秒");
});

const statsUrl = new URL("../src/components/MailRegistrationStats.vue", import.meta.url);
const statsSource = readFileSync(statsUrl, "utf8");
const { descriptor } = parse(statsSource, { filename: fileURLToPath(statsUrl) });
const compiled = compileScript(descriptor, { id: "mail-registration-test", inlineTemplate: true });
const StatsComponent = execute(compiled.content, { vue, "../shared/mailRegistration": shared }).default;

test("rendered operations statistics include outcome denominators and received-code durations", async () => {
  const html = await renderToString(vue.createSSRApp(StatsComponent, { stats: statsFixture() }));
  assert.match(html, /10 <span>次尝试/);
  assert.match(html, /40\.0%/);
  assert.match(html, /已被注册 · 跳过/);
  assert.match(html, /未收到验证码/);
  assert.match(html, /28\.6 秒/);
  assert.match(html, /0\.0 秒/);
  assert.match(html, /77\.2 秒/);
  assert.match(html, /4 次收到 · 1 次等码超时/);
  assert.match(html, /超时不计入平均/);
  assert.match(html, /Gmail/);
});

test("role switching selects both the matching aggregate and its domain rows", () => {
  const props = { stats: statsFixture() };
  const source = descriptor.scriptSetup.content;
  const h = execute(`${source}\nexport { role, summary, domainRows };`, {
    vue, "../shared/mailRegistration": shared,
  }, { defineProps: () => props });
  assert.equal(h.summary.value.totals.attempted, 10);
  h.role.value = "child";
  assert.equal(h.summary.value.totals.attempted, 2);
  assert.equal(h.domainRows.value.length, 1);
  assert.equal(h.domainRows.value[0].domain, "icloud.com");
  h.role.value = "primary";
  assert.equal(h.summary.value.totals.attempted, 8);
  assert.equal(h.domainRows.value[0].domain, "gmail.com");
});

test("unavailable or missing statistics do not render misleading zero-result panels", async () => {
  const failed = await renderToString(vue.createSSRApp(StatsComponent, { stats: { ok: false, error: "metrics_unavailable" } }));
  assert.match(failed, /暂时无法读取/);
  assert.doesNotMatch(failed, /mail-outcomes|0 次尝试|metrics_unavailable/);
  const missing = await renderToString(vue.createSSRApp(StatsComponent, { loading: true }));
  assert.match(missing, /正在加载邮箱注册统计/);
  assert.doesNotMatch(missing, /mail-outcomes/);
});
