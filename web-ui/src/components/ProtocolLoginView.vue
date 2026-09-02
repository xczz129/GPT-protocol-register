<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { ClipboardPaste, Eraser, LogIn, Play, RefreshCw, Square, Trash2 } from "@lucide/vue";

const props = defineProps({
  accounts: { type: Array, default: () => [] },
  progress: { type: Object, default: () => ({}) },
  healthProgress: { type: Object, default: () => ({}) },
  preflight: { type: Object, default: () => ({}) },
  busy: Boolean,
});
const emit = defineEmits(["start", "stop", "health", "input-change"]);

const INPUT_STATE_KEY = "gpt-register-fp.protocol-login-input.v1";

const input = ref("");
const concurrency = ref(3);
const pasteError = ref("");
const selected = ref([]);
const safeProgress = computed(() => (props.progress && typeof props.progress === "object" ? props.progress : {}));
const safeHealthProgress = computed(() => (props.healthProgress && typeof props.healthProgress === "object" ? props.healthProgress : {}));

function parseEmails(value) {
  const seen = new Set();
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]|['"`]$/g, "").toLowerCase())
    .filter((item) => item && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(item) && !seen.has(item) && seen.add(item));
}

const requestedEmails = computed(() => parseEmails(input.value));
const accountIndex = computed(() => new Map(props.accounts.map((item) => [String(item.email || "").trim().toLowerCase(), item])));
const matched = computed(() => requestedEmails.value.map((email) => accountIndex.value.get(email)).filter(Boolean));
const ready = computed(() => matched.value.filter((item) => item.has_password && item.has_totp));
const readyEmails = computed(() => new Set(ready.value.map((item) => String(item.email || "").trim().toLowerCase())));
const selectedReadyEmails = computed(() => selected.value.filter((email) => readyEmails.value.has(email)));
const incomplete = computed(() => matched.value.filter((item) => !item.has_password || !item.has_totp));
const notFound = computed(() => requestedEmails.value.filter((email) => !accountIndex.value.has(email)));
const progressByEmail = computed(() => new Map((Array.isArray(safeProgress.value.items) ? safeProgress.value.items : []).map((item) => [String(item.email || "").toLowerCase(), item])));
const displayEmails = computed(() => [...requestedEmails.value]);
const rows = computed(() => displayEmails.value.map((email) => {
  const account = accountIndex.value.get(email);
  const job = progressByEmail.value.get(email);
  const missing = [];
  if (account && !account.has_password) missing.push("密码");
  if (account && !account.has_totp) missing.push("2FA");
  return { email, account, job, missing };
}));
const allReadySelected = computed({
  get: () => readyEmails.value.size > 0 && [...readyEmails.value].every((email) => selected.value.includes(email)),
  set: (checked) => { selected.value = checked ? [...readyEmails.value] : []; },
});
const canStart = computed(() => selectedReadyEmails.value.length > 0 && !props.busy && !safeProgress.value.running && !safeHealthProgress.value.running);
const donePercent = computed(() => safeProgress.value.total ? Math.round((Number(safeProgress.value.done || 0) / Number(safeProgress.value.total)) * 100) : 0);
const healthPercent = computed(() => safeHealthProgress.value.total ? Math.round((Number(safeHealthProgress.value.done || 0) / Number(safeHealthProgress.value.total)) * 100) : 0);
const healthUpdateEmails = computed(() => new Set((Array.isArray(safeHealthProgress.value.updates) ? safeHealthProgress.value.updates : []).map((item) => String(item?.email || "").trim().toLowerCase()).filter(Boolean)));
const showHealthProgress = computed(() => Number(safeHealthProgress.value.total || 0) > 0 && (safeHealthProgress.value.running || requestedEmails.value.some((email) => healthUpdateEmails.value.has(email))));

function healthLabel(account) {
  if (!account) return "未匹配";
  if (account.is_deactivated) return "已封号";
  const status = String(account.health_status || "unknown").toLowerCase();
  return { alive: "可用", limited: "限流", reauth_required: "待续期", invalid: "失效", error: "错误", unknown: "未检测" }[status] || status;
}

function rowPlan(row) {
  if (row.job?.status === "success" && row.job?.plan_type) return String(row.job.plan_type).trim().toLowerCase();
  const health = String(row.account?.health_status || "").trim().toLowerCase();
  if (["alive", "limited"].includes(health)) return String(row.account?.plan_type || "").trim().toLowerCase();
  return "";
}

function planLabel(row) {
  const plan = rowPlan(row);
  if (!plan) return "未知";
  return plan === "plus" ? "Plus" : plan === "free" ? "Free" : plan.toUpperCase();
}

async function pasteEmails() {
  pasteError.value = "";
  try {
    input.value = await navigator.clipboard.readText();
  } catch (error) {
    pasteError.value = error?.message || "读取剪贴板失败";
  }
}

function clearInput() {
  input.value = "";
  selected.value = [];
  pasteError.value = "";
}

function removeEmails(emails) {
  const removing = new Set((Array.isArray(emails) ? emails : [emails]).map((email) => String(email || "").trim().toLowerCase()).filter(Boolean));
  if (!removing.size || safeProgress.value.running) return;
  input.value = requestedEmails.value.filter((email) => !removing.has(email)).join("\n");
  selected.value = selected.value.filter((email) => !removing.has(email));
}

function start() {
  if (canStart.value) emit("start", { emails: selectedReadyEmails.value, concurrency: Math.min(20, Math.max(1, Number(concurrency.value) || 3)) });
}

function saveInputState() {
  try {
    localStorage.setItem(INPUT_STATE_KEY, JSON.stringify({ input: input.value, concurrency: Number(concurrency.value) || 3 }));
  } catch {
    // Local storage can be disabled; the page remains usable in memory.
  }
}

onMounted(() => {
  try {
    const saved = JSON.parse(localStorage.getItem(INPUT_STATE_KEY) || "null");
    if (saved && typeof saved === "object") {
      if (typeof saved.input === "string") input.value = saved.input;
      if (Number.isFinite(Number(saved.concurrency))) concurrency.value = Math.min(20, Math.max(1, Number(saved.concurrency)));
    }
  } catch {
    // Ignore malformed or unavailable local state.
  }
});
watch([input, concurrency], saveInputState);
watch(input, () => {
  const current = new Set(requestedEmails.value);
  selected.value = selected.value.filter((email) => current.has(email));
  emit("input-change", requestedEmails.value);
});
</script>

<template>
  <section class="card protocol-login-panel">
    <div class="card-h protocol-login-head">
      <div>
        <h2>协议登录</h2>
        <p class="subtle">按邮箱批量重新登录并刷新账号状态</p>
      </div>
      <span class="protocol-login-mode"><LogIn :size="14" aria-hidden="true" />协议模式</span>
    </div>

    <div class="protocol-login-toolbar">
      <label class="protocol-login-field protocol-login-emails">
        <span>邮箱列表</span>
        <textarea v-model="input" rows="9" spellcheck="false" placeholder="每行一个邮箱，也支持逗号或空格分隔"></textarea>
      </label>
      <div class="protocol-login-controls">
        <label class="protocol-login-field">
          <span>并发数</span>
          <input v-model.number="concurrency" type="number" min="1" max="20" inputmode="numeric" />
        </label>
        <div class="protocol-login-actions">
          <button class="btn secondary" type="button" :disabled="props.busy" @click="pasteEmails"><ClipboardPaste :size="15" aria-hidden="true" />粘贴</button>
          <button class="btn ghost" type="button" :disabled="!input || props.busy" @click="clearInput"><Eraser :size="15" aria-hidden="true" />清空</button>
        </div>
        <small v-if="pasteError" class="protocol-login-error">{{ pasteError }}</small>
      </div>
    </div>

    <div class="protocol-login-summary" aria-live="polite">
      <span>识别 <b>{{ requestedEmails.length }}</b></span>
      <span>匹配 <b>{{ matched.length }}</b></span>
      <span class="ready">可登录 <b>{{ ready.length }}</b></span>
      <span class="selected">已选 <b>{{ selectedReadyEmails.length }}</b></span>
      <span class="warn">缺凭据 <b>{{ incomplete.length }}</b></span>
      <span class="danger-text">未找到 <b>{{ notFound.length }}</b></span>
    </div>

    <div v-if="props.progress.total" class="protocol-login-progress">
      <div class="protocol-login-progress-head"><strong>协议登录进度</strong><span>{{ props.progress.done }} / {{ props.progress.total }} · {{ donePercent }}%</span></div>
      <div class="protocol-login-track"><i :style="{ width: `${donePercent}%` }"></i></div>
      <div class="protocol-login-progress-meta"><span>{{ props.progress.current || (props.progress.running ? '队列处理中' : '已完成') }}</span><span class="ready">成功 {{ props.progress.success || 0 }}</span><span class="danger-text">失败 {{ props.progress.failed || 0 }}</span><button v-if="props.progress.running" class="btn danger protocol-login-stop" type="button" :disabled="props.progress.stopping" @click="emit('stop')"><Square :size="13" aria-hidden="true" />停止</button></div>
    </div>

    <div v-if="showHealthProgress" :class="['protocol-login-progress', 'protocol-health-progress', { active: props.healthProgress.running }]" aria-live="polite">
      <div class="protocol-login-progress-head"><strong>{{ props.healthProgress.running ? "协议状态检测中" : "最近一次协议状态检测完成" }}</strong><span>{{ props.healthProgress.done }} / {{ props.healthProgress.total }} · {{ healthPercent }}%</span></div>
      <div class="protocol-login-track"><i :style="{ width: `${healthPercent}%` }"></i></div>
      <div class="protocol-login-progress-meta"><span>{{ props.healthProgress.current ? `当前：${props.healthProgress.current}` : (props.healthProgress.running ? '正在分配协议检测任务' : '检测完成') }}</span><span>并发 {{ props.healthProgress.workers || props.healthProgress.requestedWorkers || concurrency }}</span><span class="ready">正常 {{ props.healthProgress.alive || 0 }}</span><span>限流 {{ props.healthProgress.limited || 0 }}</span><span>待续期 {{ props.healthProgress.reauthRequired || 0 }}</span><span class="danger-text">失效 {{ props.healthProgress.invalid || 0 }}</span><span class="danger-text">错误 {{ props.healthProgress.error || 0 }}</span></div>
    </div>

    <div v-if="rows.length" class="protocol-login-table-wrap">
      <table class="protocol-login-table">
        <thead><tr><th class="protocol-login-check"><input v-model="allReadySelected" type="checkbox" aria-label="全选可登录账号" :disabled="!ready.length || props.progress.running" /></th><th>邮箱</th><th>本地凭据</th><th>账号状态</th><th>登录结果</th><th>登录后套餐</th><th class="protocol-login-action-head">操作</th></tr></thead>
        <tbody>
          <tr v-for="row in rows" :key="row.email">
            <td class="protocol-login-check"><input v-model="selected" type="checkbox" :value="row.email" :aria-label="`选择 ${row.email}`" :disabled="!readyEmails.has(row.email) || props.progress.running" /></td>
            <td><strong>{{ row.email }}</strong></td>
            <td><span :class="['credential-ready', row.account?.has_password && row.account?.has_totp ? 'yes' : 'no']">{{ row.account ? (row.missing.length ? `缺 ${row.missing.join('、')}` : '密码 / 2FA 齐全') : '未找到' }}</span></td>
            <td><span class="tag">{{ healthLabel(row.account) }}</span></td>
            <td><span :class="['protocol-login-status', `is-${row.job?.status || (row.account ? 'ready' : 'not-found')}`]">{{ row.job?.message || (row.account ? (row.missing.length ? '跳过：缺少凭据' : '待提交') : '未找到本地账号') }}</span></td>
            <td><span :class="['protocol-login-plan', `is-${rowPlan(row)}`]">{{ planLabel(row) }}</span></td>
            <td class="protocol-login-row-action"><button type="button" title="仅从当前列表移除" :aria-label="`从列表移除 ${row.email}`" :disabled="props.progress.running" @click="removeEmails(row.email)"><Trash2 :size="14" aria-hidden="true" /></button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="protocol-login-empty">输入邮箱后显示匹配结果</div>

    <div class="protocol-login-footer">
      <span>只提交同时保存了密码和 TOTP 2FA 的账号；登录完成后会刷新账号状态。</span>
      <div class="protocol-login-footer-actions">
        <button class="btn ghost" type="button" :disabled="!selected.length || props.busy || props.progress.running" title="只从当前输入列表移除，不删除账号记录" @click="removeEmails(selected)"><Trash2 :size="15" aria-hidden="true" />从列表移除 {{ selected.length }}</button>
        <button class="btn secondary" type="button" :disabled="props.busy || !matched.length || props.healthProgress.running || props.progress.running" @click="emit('health', { emails: requestedEmails, concurrency: Math.min(20, Math.max(1, Number(concurrency) || 3)) })"><RefreshCw :size="15" aria-hidden="true" />{{ props.healthProgress.running ? "检测中" : "查看状态" }}</button>
        <button class="btn primary" type="button" :disabled="!canStart" @click="start"><Play :size="15" aria-hidden="true" />协议登录选中 {{ selectedReadyEmails.length }} 个</button>
      </div>
    </div>
  </section>
</template>
