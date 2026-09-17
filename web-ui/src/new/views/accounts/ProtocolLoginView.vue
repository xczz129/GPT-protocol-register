<script setup lang="ts">
/* --- 协议登录：按邮箱批量重新登录（预检本地凭据 + reauth 批次进度 + 协议测活） --- */
import { computed, onMounted, ref, watch } from "vue";
import { ClipboardPaste, Eraser, LogIn, Play, RefreshCw, Square, Trash2 } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { useAccountsStore } from "../../stores/accounts";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";

const INPUT_STATE_KEY = "gpt-register-fp.protocol-login-input.v1";

const accounts = useAccountsStore();
const batch = useBatchOpsStore();
const ui = useUiStore();
const confirm = useConfirm();

const emailInput = ref("");
const credentialColumnsRemoved = ref(false);
const input = computed({
  get: () => emailInput.value,
  set: (value: string) => {
    const raw = String(value || "");
    const cleaned = stripCredentialColumns(raw);
    credentialColumnsRemoved.value = raw !== cleaned;
    emailInput.value = cleaned;
  },
});
const concurrency = ref(3);
const pasteError = ref("");
const selected = ref<string[]>([]);
const busy = ref(false);

function stripCredentialColumns(value: string): string {
  return String(value || "").split(/\r\n?|\n/)
    .map(line => line.split("----", 1)[0].replace(/\\@/g, "@"))
    .join("\n");
}

function onInput(event: Event) {
  const target = event.target as HTMLTextAreaElement;
  input.value = target.value;
  // Update the DOM even when pasting another credential row for the same email.
  target.value = input.value;
}

function parseEmails(value: string): string[] {
  const seen = new Set<string>();
  return stripCredentialColumns(value)
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]+|['"`]+$/g, "").toLowerCase())
    .filter((item) => item && /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(item) && !seen.has(item) && seen.add(item));
}

const requestedEmails = computed(() => parseEmails(input.value));
const accountsReady = computed(() => !accounts.loading && !accounts.loadError
  && accounts.rows.length >= accounts.total && accounts.loadProgress.loaded >= accounts.loadProgress.total);
const accountIndex = computed(() => new Map(accounts.rows.map((item) => [String(item.email || "").trim().toLowerCase(), item])));
const matched = computed(() => accountsReady.value ? requestedEmails.value.map((email) => accountIndex.value.get(email)).filter((item): item is NonNullable<typeof item> => Boolean(item)) : []);
const ready = computed(() => matched.value.filter((item) => item.has_password && item.has_totp));
const readyEmails = computed(() => new Set(ready.value.map((item) => String(item.email || "").trim().toLowerCase())));
const selectedReadyEmails = computed(() => selected.value.filter((email) => readyEmails.value.has(email)));
const incomplete = computed(() => matched.value.filter((item) => !item.has_password || !item.has_totp));
const notFound = computed(() => accountsReady.value ? requestedEmails.value.filter((email) => !accountIndex.value.has(email)) : []);

const progress = computed(() => batch.reauth);
const health = computed(() => accounts.health);
const progressByEmail = computed(() => new Map((progress.value.items || []).map((item) => [String(item.email || item.key || "").toLowerCase(), item])));

const rows = computed(() => requestedEmails.value.map((email) => {
  const account = accountsReady.value ? accountIndex.value.get(email) : undefined;
  const job = progressByEmail.value.get(email);
  const missing: string[] = [];
  if (account && !account.has_password) missing.push("密码");
  if (account && !account.has_totp) missing.push("2FA");
  return { email, account, job, missing, matching: !accountsReady.value };
}));

const allReadySelected = computed({
  get: () => readyEmails.value.size > 0 && [...readyEmails.value].every((email) => selected.value.includes(email)),
  set: (checked: boolean) => { selected.value = checked ? [...readyEmails.value] : []; },
});

const canStart = computed(() => accountsReady.value && selectedReadyEmails.value.length > 0 && !busy.value && !progress.value.running && !health.value.running);
const donePercent = computed(() => progress.value.total ? Math.round((Number(progress.value.done || 0) / Number(progress.value.total)) * 100) : 0);
const healthPercent = computed(() => health.value.total ? Math.round((Number(health.value.done || 0) / Number(health.value.total)) * 100) : 0);

function healthLabel(account?: (typeof accounts.rows)[number]) {
  if (!account) return "未匹配";
  if (account.is_deactivated) return "已封号";
  const status = String(account.health_status || "unknown").toLowerCase();
  return ({ alive: "可用", ok: "可用", limited: "限流", reauth_required: "待续期", invalid: "失效", error: "错误", unknown: "未检测" } as Record<string, string>)[status] || status;
}

function rowPlan(row: (typeof rows.value)[number]): string {
  const job = row.job as { status?: string; plan_type?: string } | undefined;
  if (job?.status === "success" && job?.plan_type) return String(job.plan_type).trim().toLowerCase();
  const h = String(row.account?.health_status || "").trim().toLowerCase();
  if (["alive", "ok", "limited"].includes(h)) return String(row.account?.plan_type || "").trim().toLowerCase();
  return "";
}

function planLabel(row: (typeof rows.value)[number]): string {
  const plan = rowPlan(row);
  if (!plan) return "未知";
  return plan === "plus" ? "Plus" : plan === "free" ? "Free" : plan.toUpperCase();
}

async function pasteEmails() {
  pasteError.value = "";
  try {
    input.value = await navigator.clipboard.readText();
  } catch (error) {
    pasteError.value = error instanceof Error ? error.message : "读取剪贴板失败";
  }
}

function clearInput() {
  input.value = "";
  selected.value = [];
  pasteError.value = "";
}

function removeEmails(emails: string | string[]) {
  const removing = new Set((Array.isArray(emails) ? emails : [emails]).map((email) => email.trim().toLowerCase()).filter(Boolean));
  if (!removing.size || progress.value.running) return;
  input.value = requestedEmails.value.filter((email) => !removing.has(email)).join("\n");
  selected.value = selected.value.filter((email) => !removing.has(email));
}

async function start() {
  if (!canStart.value) return;
  const workers = Math.min(20, Math.max(1, Number(concurrency.value) || 3));
  busy.value = true;
  try {
    const value = await batch.startProtocolLogin(selectedReadyEmails.value, workers);
    if (!value.job_id) {
      ui.notify(value.message || "预检完成，没有可提交账号", "warning");
    } else {
      const skipped = Number(value.skipped || 0) + (value.not_found?.length || 0) + (value.missing_credentials?.length || 0) + (value.invalid_format?.length || 0);
      const state = value.status === "completed" ? "已结束" : "已启动";
      ui.notify(`协议登录${state}：提交 ${value.submitted || 0} 个${skipped ? `，已跳过 ${skipped} 个` : ""}`, "success");
    }
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "协议登录批次启动失败", "error");
  } finally {
    busy.value = false;
  }
}

async function stopJob() {
  if (!(await confirm("停止当前协议登录批次？正在处理的账号会在当前步骤结束后停止。", { title: "停止协议登录", confirmText: "停止", danger: true }))) return;
  try {
    await batch.stopReauthBatch();
    ui.notify("已发送停止请求", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "停止失败", "error");
  }
}

/** 查看状态：对输入列表中匹配到的账号做协议测活（不启动浏览器） */
async function healthCheck() {
  if (!accountsReady.value) return;
  const wanted = new Set(requestedEmails.value);
  const keys = accounts.rows.filter((item) => wanted.has(String(item.email || "").trim().toLowerCase())).map((item) => item.delete_key).filter(Boolean);
  if (!keys.length || health.value.running || progress.value.running) return;
  try {
    await accounts.startHealth(keys, false, Math.min(20, Math.max(1, Number(concurrency.value) || 3)));
    ui.notify(`已开始协议状态检测（${keys.length} 个账号）`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "检测启动失败", "error");
  }
}

function saveInputState() {
  try {
    localStorage.setItem(INPUT_STATE_KEY, JSON.stringify({ input: stripCredentialColumns(input.value), concurrency: Number(concurrency.value) || 3 }));
  } catch {
    // Do not leave a legacy credential-bearing cache when rewriting fails.
    try { localStorage.removeItem(INPUT_STATE_KEY); } catch { /* storage unavailable */ }
  }
}

watch([input, concurrency], saveInputState);
watch(input, () => {
  const current = new Set(requestedEmails.value);
  selected.value = selected.value.filter((email) => current.has(email));
});

onMounted(async () => {
  try {
    const saved = JSON.parse(localStorage.getItem(INPUT_STATE_KEY) || "null");
    if (saved && typeof saved === "object") {
      if (typeof saved.input === "string") input.value = saved.input;
      if (Number.isFinite(Number(saved.concurrency))) concurrency.value = Math.min(20, Math.max(1, Number(saved.concurrency)));
      saveInputState(); // Sanitize v1 caches immediately, including old full rows.
    }
  } catch { /* 忽略损坏的本地状态 */ }
  if (!accounts.loading && (!accountsReady.value || !accounts.rows.length)) await accounts.load();
  await accounts.refreshHealth();
  batch.attachReauthIfRunning();
});
</script>

<template>
  <section class="protocol-panel glass-panel">
    <div class="panel-head">
      <div>
        <h2>协议登录</h2>
        <p class="panel-sub">按邮箱批量重新登录并刷新账号状态</p>
      </div>
      <AppTag kind="info"><LogIn :size="12" />协议模式</AppTag>
    </div>

    <div class="toolbar">
      <label class="field emails-field">
        <span>邮箱列表</span>
        <textarea :value="input" @input="onInput" class="input" rows="9" spellcheck="false" placeholder="每行一个邮箱；也支持每行一条 邮箱----密码----2FA，会自动只提取邮箱"></textarea>
        <small class="input-help">本页使用账号库已保存的密码和 2FA。完整凭据行仅提取邮箱，不会更新账号凭据；新账号请到账号列表使用“导入账号”。</small>
        <small v-if="credentialColumnsRemoved" class="input-help" role="status">已提取邮箱并移除密码、2FA 列，浏览器缓存只保存邮箱。</small>
      </label>
      <div class="controls">
        <label class="field">
          <span>并发数</span>
          <input v-model.number="concurrency" class="input concurrency" type="number" min="1" max="20" inputmode="numeric" />
        </label>
        <div class="control-actions">
          <AppButton variant="accent" size="sm" @click="pasteEmails"><ClipboardPaste :size="14" /><span>粘贴</span></AppButton>
          <AppButton variant="ghost" size="sm" :disabled="!input" @click="clearInput"><Eraser :size="14" /><span>清空</span></AppButton>
        </div>
        <small v-if="pasteError" class="error-text">{{ pasteError }}</small>
      </div>
    </div>

    <div class="summary" aria-live="polite">
      <span>识别 <b>{{ requestedEmails.length }}</b></span>
      <span>匹配 <b>{{ accountsReady ? matched.length : '—' }}</b></span>
      <span class="ok-text">可登录 <b>{{ accountsReady ? ready.length : '—' }}</b></span>
      <span class="info-text">已选 <b>{{ selectedReadyEmails.length }}</b></span>
      <span class="warn-text">缺凭据 <b>{{ accountsReady ? incomplete.length : '—' }}</b></span>
      <span class="error-text">未找到 <b>{{ accountsReady ? notFound.length : '—' }}</b></span>
    </div>
    <div v-if="!accountsReady" class="matching-notice" role="status">
      <span>{{ accounts.loadError ? `账号库加载失败：${accounts.loadError}` : accounts.loading ? `正在加载账号库 ${accounts.loadProgress.loaded} / ${accounts.loadProgress.total}，完成后显示匹配结果。` : '账号库尚未加载完整，请重新加载后匹配。' }}</span>
      <AppButton size="sm" :disabled="accounts.loading" @click="accounts.load()">重新加载账号</AppButton>
    </div>

    <div v-if="progress.total" class="progress-block">
      <div class="progress-head">
        <strong>协议登录进度</strong>
        <span>{{ progress.done }} / {{ progress.total }} · {{ donePercent }}%</span>
      </div>
      <div class="track"><i :style="{ width: `${donePercent}%` }"></i></div>
      <div class="progress-meta">
        <span>{{ progress.current || (progress.running ? "队列处理中" : "已完成") }}</span>
        <span class="ok-text">成功 {{ progress.success || 0 }}</span>
        <span class="error-text">失败 {{ progress.failed || 0 }}</span>
        <span>已跳过 {{ progress.skipped || 0 }}</span>
        <span>已取消 {{ progress.cancelled || 0 }}</span>
        <AppButton v-if="progress.running" variant="danger" size="sm" :disabled="progress.stopping" @click="stopJob">
          <Square :size="12" /><span>停止</span>
        </AppButton>
      </div>
    </div>

    <div v-if="health.total && (health.running || health.done)" class="progress-block">
      <div class="progress-head">
        <strong>{{ health.running ? "协议状态检测中" : "最近一次协议状态检测完成" }}</strong>
        <span>{{ health.done }} / {{ health.total }} · {{ healthPercent }}%</span>
      </div>
      <div class="track"><i :style="{ width: `${healthPercent}%` }"></i></div>
      <div class="progress-meta">
        <span>{{ health.current ? `当前：${health.current}` : (health.running ? "正在分配协议检测任务" : "检测完成") }}</span>
        <span class="ok-text">正常 {{ health.alive || 0 }}</span>
        <span>限流 {{ health.limited || 0 }}</span>
        <span>待续期 {{ health.reauthRequired || 0 }}</span>
        <span class="error-text">失效 {{ health.invalid || 0 }}</span>
        <span class="error-text">错误 {{ health.error || 0 }}</span>
      </div>
    </div>

    <div v-if="rows.length" class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="col-check"><input v-model="allReadySelected" type="checkbox" aria-label="全选可登录账号" :disabled="!ready.length || progress.running" /></th>
            <th>邮箱</th><th>本地凭据</th><th>账号状态</th><th>登录结果</th><th>登录后套餐</th><th class="col-action">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.email">
            <td class="col-check"><input v-model="selected" type="checkbox" :value="row.email" :aria-label="`选择 ${row.email}`" :disabled="!readyEmails.has(row.email) || progress.running" /></td>
            <td><strong class="email">{{ row.email }}</strong></td>
            <td>
              <span :class="['cred-state', row.account?.has_password && row.account?.has_totp ? 'yes' : 'no']">
                {{ row.matching ? "正在匹配" : row.account ? (row.missing.length ? `缺 ${row.missing.join("、")}` : "密码 / 2FA 齐全") : "未找到" }}
              </span>
            </td>
            <td><AppTag :kind="row.account?.is_deactivated ? 'error' : 'default'">{{ row.matching ? '正在匹配' : healthLabel(row.account) }}</AppTag></td>
            <td>
              <span :class="['job-status', `is-${row.job?.status || (row.account ? 'ready' : 'not-found')}`]">
                <template v-if="row.job?.status === 'skipped'">已跳过{{ row.job.message ? `：${row.job.message}` : '' }}</template>
                <template v-else>{{ row.job?.message || (row.matching ? "等待账号库加载" : row.account ? (row.missing.length ? "跳过：缺少凭据" : "待提交") : "未找到本地账号，请先导入") }}</template>
              </span>
            </td>
            <td><span :class="['plan-text', `is-${rowPlan(row)}`]">{{ planLabel(row) }}</span></td>
            <td class="col-action">
              <button class="row-btn" type="button" title="仅从当前列表移除" :disabled="progress.running" @click="removeEmails(row.email)">
                <Trash2 :size="14" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="empty-hint">输入邮箱后显示匹配结果</div>

    <div class="footer">
      <span class="footer-note">只提交同时保存了密码和 TOTP 2FA 的账号；登录完成后会刷新账号状态。</span>
      <div class="footer-actions">
        <AppButton variant="ghost" :disabled="!selected.length || progress.running" title="只从当前输入列表移除，不删除账号记录" @click="removeEmails(selected)">
          <Trash2 :size="14" /><span>从列表移除 {{ selected.length }}</span>
        </AppButton>
        <AppButton variant="accent" :disabled="!matched.length || health.running || progress.running" @click="healthCheck">
          <RefreshCw :size="14" /><span>{{ health.running ? "检测中" : "查看状态" }}</span>
        </AppButton>
        <AppButton variant="primary" :disabled="!canStart" :loading="busy" @click="start">
          <Play :size="14" /><span>协议登录选中 {{ selectedReadyEmails.length }} 个</span>
        </AppButton>
      </div>
    </div>
  </section>
</template>

<style scoped>
.input-help { font-size: .76rem; line-height: 1.6; color: var(--text-secondary); }
.matching-notice { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 10px 12px; border-radius: var(--radius-sm); background: var(--accent-bg); color: var(--text-secondary); font-size: .8rem; }
.protocol-panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.panel-head h2 { font-size: 1.05rem; color: var(--text-primary); }
.panel-sub { font-size: 0.8rem; color: var(--text-muted); margin-top: 3px; }

.toolbar {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.emails-field { flex: 1; }
.emails-field textarea { resize: vertical; font-family: var(--font-mono, monospace); font-size: 0.8rem; }

.controls { display: flex; flex-direction: column; gap: 8px; }
.concurrency { width: 88px; text-align: center; }
.control-actions { display: flex; gap: 6px; }

.summary {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 0.82rem;
  color: var(--text-secondary);
}
.summary b { color: var(--text-primary); }
.ok-text { color: var(--success); }
.warn-text { color: var(--warning); }
.error-text { color: var(--error); }
.info-text { color: var(--info); }

.progress-block {
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--inset-bg);
}

.progress-head {
  display: flex;
  justify-content: space-between;
  font-size: 0.82rem;
  color: var(--text-primary);
}

.track {
  height: 6px;
  border-radius: 4px;
  background: var(--line-subtle);
  overflow: hidden;
}

.track i {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: var(--accent);
  transition: width 0.3s;
}

.progress-meta {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 0.76rem;
  color: var(--text-secondary);
}

.table-wrap {
  overflow: auto;
  max-height: 48vh;
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
}

table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line-subtle); }
th {
  font-size: 0.72rem;
  color: var(--text-muted);
  font-weight: 600;
  background: var(--inset-bg);
  position: sticky;
  top: 0;
}

.col-check { width: 34px; }
.col-action { text-align: right; width: 48px; }
.email { color: var(--text-primary); }

.cred-state.yes { color: var(--success); font-size: 0.78rem; }
.cred-state.no { color: var(--warning); font-size: 0.78rem; }

.job-status { font-size: 0.78rem; color: var(--text-secondary); }
.job-status.is-success { color: var(--success); }
.job-status.is-failed { color: var(--error); }
.job-status.is-running { color: var(--info); }

.plan-text { font-size: 0.78rem; color: var(--text-muted); }
.plan-text.is-plus { color: var(--warning); font-weight: 600; }

.row-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px;
  border-radius: 6px;
}
.row-btn:hover { color: var(--error); background: var(--error-bg); }
.row-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.empty-hint { padding: 32px 0; text-align: center; color: var(--text-muted); font-size: 0.82rem; }

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.footer-note { font-size: 0.75rem; color: var(--text-muted); }
.footer-actions { display: flex; gap: 8px; flex-wrap: wrap; }

@media (max-width: 768px) {
  .toolbar { flex-direction: column; }
  .controls { flex-direction: row; align-items: flex-end; }
}
</style>
