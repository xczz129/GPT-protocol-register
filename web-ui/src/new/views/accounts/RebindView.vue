<script setup lang="ts">
/* --- 邮箱换绑：预检（原子预留目标邮箱）→ 确认 → 批次换绑（进度/逐项结果/停止） --- */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import {
  AlertTriangle, ArrowLeftRight, ArrowRight, CheckCircle2, ClipboardPaste, Eraser,
  MailCheck, Play, RefreshCw, ShieldCheck, Square,
} from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { useRebindStore } from "../../stores/rebind";
import { useAccountsStore } from "../../stores/accounts";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";

const INPUT_STATE_KEY = "gpt-register-fp.rebind-input.v1";

const rebind = useRebindStore();
const accounts = useAccountsStore();
const ui = useUiStore();
const confirm = useConfirm();

const sourceEmail = ref("");
const targetMode = ref<"auto" | "custom">("auto");
const targetEmail = ref("");
const providerType = ref("maddy_webui");
const concurrency = ref(1);
const pasteError = ref("");
const restoringInput = ref(true);

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function splitEntries(value: string): string[] {
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]|['"`]$/g, "").toLowerCase())
    .filter(Boolean);
}

const sourceEntries = computed(() => {
  const values: string[] = [];
  String(sourceEmail.value || "").split(/[\r\n]+/).forEach((line) => {
    let value = line.trim();
    if (!value) return;
    if (value.includes("----")) value = value.split("----", 1)[0].trim();
    else if (value.includes("->")) value = value.split("->", 1)[0].trim();
    values.push(...splitEntries(value));
  });
  return [...new Set(values)];
});

const sourceInvalid = computed(() => sourceEntries.value.filter((value) => !emailPattern.test(value)));
const sourceValid = computed(() => sourceEntries.value.length > 0 && sourceInvalid.value.length === 0);
const validSourceCount = computed(() => sourceEntries.value.filter((value) => emailPattern.test(value)).length);
const targetEntries = computed(() => splitEntries(targetEmail.value));
const targetInvalid = computed(() => targetEntries.value.filter((value) => !emailPattern.test(value)));
const targetCountValid = computed(() => targetMode.value === "auto" || targetEntries.value.length === validSourceCount.value);
const targetValid = computed(() => targetMode.value === "auto" || (targetEntries.value.length > 0 && targetInvalid.value.length === 0 && targetCountValid.value));

const progress = computed(() => rebind.progress);
const preflight = computed(() => rebind.preflight);
const progressRunning = computed(() => Boolean(progress.value.running) || ["queued", "running", "stopping"].includes(String(progress.value.status || "")));
const reservationId = computed(() => String(preflight.value.reservation_id || "").trim());
const eligible = computed(() => preflight.value.eligible === true && Boolean(reservationId.value));
const canCheck = computed(() => sourceEntries.value.some((value) => emailPattern.test(value)) && targetValid.value && !rebind.requestBusy && !rebind.checking && !progressRunning.value);
const canStart = computed(() => eligible.value && !rebind.requestBusy && !progressRunning.value);
const percent = computed(() => {
  const total = Number(progress.value.total || 0);
  const done = Number(progress.value.done || 0);
  return total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
});

const hasPreflight = computed(() => {
  const value = preflight.value;
  return Boolean(
    value.message || value.source || value.target
    || value.not_found?.length || value.invalid_format?.length
    || value.missing_credentials?.length || value.conflicts?.length || value.items?.length,
  );
});

const source = computed(() => preflight.value.source || null);
const target = computed(() => preflight.value.target || null);
const preflightItems = computed(() => (Array.isArray(preflight.value.items) ? preflight.value.items : []));
const eligibleCount = computed(() => Number(preflight.value.eligible_count || (preflight.value.eligible ? 1 : 0)));
const invalidFormat = computed(() => preflight.value.invalid_format || []);
const invalidTargets = computed(() => preflight.value.invalid_targets || []);
const notFound = computed(() => preflight.value.not_found || []);
const missingCredentials = computed(() => preflight.value.missing_credentials || []);
const conflicts = computed(() => preflight.value.conflicts || []);
const progressItems = computed(() => (Array.isArray(progress.value.items) ? progress.value.items : []) as NonNullable<typeof progress.value.items>);

function providerLabel(value?: string) {
  return String(value || "maddy_webui") === "fastmail_api" ? "Fastmail 别名池" : "Maddy 域名邮箱";
}

function credentialLabel(value?: string) {
  return ({ password: "密码", totp: "TOTP 2FA", totp_secret: "TOTP 2FA" } as Record<string, string>)[String(value || "").toLowerCase()] || String(value || "安全字段");
}

function healthLabel(value?: string) {
  return ({ alive: "可用", ok: "可用", limited: "限流", reauth_required: "待续期", invalid: "失效", error: "错误", unknown: "待检测 / 复测" } as Record<string, string>)[String(value || "unknown").toLowerCase()] || String(value || "未检测");
}

function planLabel(value?: string) {
  const plan = String(value || "free").trim().toLowerCase();
  return plan === "plus" ? "Plus" : plan === "free" ? "Free" : plan.toUpperCase();
}

function itemStatusLabel(item: { status?: string }) {
  const status = String(item?.status || "queued").toLowerCase();
  return ({ queued: "排队", running: "进行中", success: "成功", completed: "成功", failed: "失败", cancelled: "已停止", stopped: "已停止" } as Record<string, string>)[status] || status;
}

function itemStatusKind(item: { status?: string }): "success" | "error" | "warning" | "info" {
  const status = String(item?.status || "queued").toLowerCase();
  if (["success", "completed"].includes(status)) return "success";
  if (status === "failed") return "error";
  if (["cancelled", "stopped"].includes(status)) return "warning";
  return "info";
}

function domainOf(value?: string) {
  const text = String(value || "");
  return text.includes("@") ? text.split("@").slice(1).join("@") : "";
}

async function check() {
  if (!canCheck.value) return;
  try {
    await rebind.check({
      text: String(sourceEmail.value || "").trim(),
      new_email: targetMode.value === "custom" ? String(targetEmail.value || "").trim() : "",
      provider_type: providerType.value,
    });
    if (!rebind.preflight.eligible) ui.notify(rebind.preflight.message || "换绑预检未通过", "error");
    else ui.notify(`已匹配 ${eligibleCount.value || 1} 个未占用邮箱，请确认后开始换绑`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "换绑预检失败", "error");
  }
}

async function start() {
  if (!canStart.value) return;
  if (!(await confirm(`将 ${eligibleCount.value} 个账号换绑到预检匹配的新邮箱。换绑完成后账号库会同步新邮箱和访问凭据。`, { title: "开始邮箱换绑", confirmText: "开始换绑" }))) return;
  try {
    await rebind.start(providerType.value, concurrency.value);
    ui.notify("换绑任务已启动", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "换绑任务启动失败", "error");
  }
}

async function stop() {
  if (!(await confirm("停止当前邮箱换绑任务？正在处理的账号会在当前步骤结束后停止。", { title: "停止邮箱换绑", confirmText: "停止", danger: true }))) return;
  try {
    await rebind.stop();
    ui.notify("已发送停止请求", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "停止换绑失败", "error");
  }
}

async function pasteEmail() {
  pasteError.value = "";
  try {
    sourceEmail.value = String(await navigator.clipboard.readText() || "").trim();
  } catch (error) {
    pasteError.value = error instanceof Error ? error.message : "读取剪贴板失败";
  }
}

function clearForm() {
  sourceEmail.value = "";
  targetEmail.value = "";
  targetMode.value = "auto";
  providerType.value = "maddy_webui";
  concurrency.value = 1;
  pasteError.value = "";
  rebind.resetForInput();
}

function saveInputState() {
  try {
    localStorage.setItem(INPUT_STATE_KEY, JSON.stringify({
      sourceEmail: sourceEmail.value,
      targetMode: targetMode.value,
      targetEmail: targetEmail.value,
      providerType: providerType.value,
      concurrency: Number(concurrency.value) || 1,
    }));
  } catch { /* 本地存储不可用时仅内存可用 */ }
}

watch([sourceEmail, targetMode, targetEmail, providerType], () => {
  saveInputState();
  if (restoringInput.value) return;
  rebind.resetForInput();
});
watch(concurrency, saveInputState);

onMounted(async () => {
  try {
    const saved = JSON.parse(localStorage.getItem(INPUT_STATE_KEY) || "null");
    if (saved && typeof saved === "object") {
      if (typeof saved.sourceEmail === "string") sourceEmail.value = saved.sourceEmail;
      if (["auto", "custom"].includes(saved.targetMode)) targetMode.value = saved.targetMode;
      if (typeof saved.targetEmail === "string") targetEmail.value = saved.targetEmail;
      if (["maddy_webui", "fastmail_api"].includes(saved.providerType)) providerType.value = saved.providerType;
      if (Number.isFinite(Number(saved.concurrency))) concurrency.value = Math.min(20, Math.max(1, Number(saved.concurrency)));
    }
  } catch { /* 忽略损坏的本地状态 */ }
  await nextTick();
  restoringInput.value = false;
  /* 后台有换绑任务时接管进度；任务结束后刷新账号库 */
  rebind.attachIfRunning();
  if (!accounts.rows.length) void accounts.load();
});
</script>

<template>
  <section class="rebind-panel glass-panel">
    <div class="panel-head">
      <div>
        <h2><MailCheck :size="16" /> 邮箱换绑</h2>
        <p class="panel-sub">验证当前账号后，从所选邮箱池预留一个未占用邮箱并同步新的登录凭据。</p>
      </div>
      <AppTag kind="info"><ArrowLeftRight :size="12" />协议流程</AppTag>
    </div>

    <div class="form-grid">
      <label class="field">
        <span>当前 GPT 账号邮箱</span>
        <div class="input-with-btn">
          <textarea
            v-model="sourceEmail" class="input" rows="5" autocomplete="off" spellcheck="false"
            placeholder="每行一个账号邮箱，可粘贴多行"
            :disabled="rebind.requestBusy || rebind.checking || progressRunning"
            @keydown.ctrl.enter.prevent="check" @keydown.meta.enter.prevent="check"
          ></textarea>
          <AppButton variant="accent" size="sm" title="从剪贴板粘贴邮箱" :disabled="rebind.requestBusy || rebind.checking || progressRunning" @click="pasteEmail">
            <ClipboardPaste :size="14" />
          </AppButton>
        </div>
        <small v-if="sourceEmail && !sourceValid" class="error-text">每行输入一个有效的邮箱地址，可用逗号或空格分隔</small>
        <small v-if="pasteError" class="error-text">{{ pasteError }}</small>
      </label>

      <div class="target-box">
        <span class="field-label">新邮箱</span>
        <div class="segmented">
          <button type="button" :class="{ active: targetMode === 'auto' }" :disabled="rebind.requestBusy || rebind.checking || progressRunning" @click="targetMode = 'auto'">自动从邮箱池选择</button>
          <button type="button" :class="{ active: targetMode === 'custom' }" :disabled="rebind.requestBusy || rebind.checking || progressRunning" @click="targetMode = 'custom'">指定邮箱</button>
        </div>
        <textarea v-if="targetMode === 'custom'" v-model="targetEmail" class="input" rows="5" autocomplete="off" spellcheck="false" placeholder="每行一个新邮箱，顺序与左侧账号对应" :disabled="rebind.requestBusy || rebind.checking || progressRunning"></textarea>
        <div v-else class="auto-target"><RefreshCw :size="14" /><span>预检时从所选邮箱池中原子预留</span></div>
        <small v-if="targetMode === 'custom' && targetEmail && !targetValid" class="error-text">批量指定时，新邮箱数量要与账号数量一致，且格式有效</small>
        <div class="inline-options">
          <label>邮箱来源
            <select v-model="providerType" class="input" :disabled="rebind.requestBusy || rebind.checking || progressRunning">
              <option value="maddy_webui">Maddy 域名邮箱</option>
              <option value="fastmail_api">Fastmail 别名池</option>
            </select>
          </label>
          <label>并发
            <input v-model.number="concurrency" class="input concurrency" type="number" min="1" max="20" inputmode="numeric" :disabled="rebind.requestBusy || rebind.checking || progressRunning" />
          </label>
        </div>
      </div>
    </div>

    <div class="actions">
      <AppButton variant="ghost" :disabled="(!sourceEmail && !targetEmail) || rebind.requestBusy || progressRunning" @click="clearForm">
        <Eraser :size="14" /><span>清空</span>
      </AppButton>
      <AppButton variant="primary" :disabled="!canCheck" :loading="rebind.checking" @click="check">
        <RefreshCw :size="14" /><span>{{ rebind.checking ? "检查中" : "检查并匹配邮箱" }}</span>
      </AppButton>
    </div>

    <!-- 预检结果 -->
    <div v-if="hasPreflight" class="preflight" aria-live="polite">
      <div class="result-banner" :class="eligible ? 'ok' : 'warn'">
        <CheckCircle2 v-if="eligible" :size="16" />
        <AlertTriangle v-else :size="16" />
        <span>{{ preflight.message || (eligible ? `已匹配 ${eligibleCount} 个可用目标邮箱` : "预检未通过") }}</span>
      </div>

      <div v-if="preflightItems.length > 1" class="batch-summary">
        <div class="batch-summary-head">
          <strong>逐项预检结果</strong>
          <span>{{ preflight.matched || 0 }} / {{ preflight.requested || preflightItems.length }} 个账号已识别</span>
        </div>
        <div class="batch-list">
          <div v-for="(item, index) in preflightItems" :key="item.record_key || item.input || index" class="batch-row">
            <span class="batch-index">{{ index + 1 }}</span>
            <strong>{{ item.source?.email || item.input || "-" }}</strong>
            <span v-if="item.target?.email" class="batch-target">{{ item.target.email }}</span>
            <span v-else class="batch-message">{{ item.message || item.status || "-" }}</span>
            <AppTag :kind="item.status === 'eligible' ? 'success' : 'error'">
              {{ item.status === "eligible" ? "可执行" : item.status === "missing_credentials" ? "缺安全字段" : item.status === "not_found" ? "未找到" : "跳过" }}
            </AppTag>
          </div>
        </div>
      </div>

      <div v-else-if="source || target" class="route">
        <article v-if="source" class="endpoint">
          <span class="endpoint-kicker">当前账号</span>
          <strong>{{ source.email || sourceEmail }}</strong>
          <small v-if="source.account_id">Account ID：{{ source.account_id }}</small>
          <div class="endpoint-tags">
            <AppTag>{{ planLabel(source.plan_type) }}</AppTag>
            <AppTag kind="info">{{ healthLabel(source.health_status) }}</AppTag>
            <AppTag :kind="source.has_password && source.password_confirmed !== false && source.has_totp ? 'success' : 'warning'">
              {{ source.has_password && source.password_confirmed !== false && source.has_totp ? "密码 / 2FA 齐全" : "缺少安全字段" }}
            </AppTag>
          </div>
        </article>
        <ArrowRight v-if="source && target" class="route-arrow" :size="20" />
        <article v-if="target" class="endpoint target">
          <span class="endpoint-kicker">换绑目标</span>
          <strong>{{ target.email || "-" }}</strong>
          <small>{{ providerLabel(target.provider || providerType) }}<template v-if="target.domain || domainOf(target.email)"> · {{ target.domain || domainOf(target.email) }}</template></small>
          <div class="endpoint-tags">
            <AppTag kind="success">已预留</AppTag>
            <small v-if="target.reserved_at">预检时间 {{ target.reserved_at }}</small>
          </div>
        </article>
      </div>

      <div v-if="invalidFormat.length || invalidTargets.length || notFound.length || missingCredentials.length || conflicts.length" class="issues">
        <div v-if="invalidFormat.length"><b>格式无效</b><span>{{ invalidFormat.join("、") }}</span></div>
        <div v-if="invalidTargets.length"><b>目标格式无效</b><span>{{ invalidTargets.join("、") }}</span></div>
        <div v-if="notFound.length"><b>未找到账号</b><span>{{ notFound.join("、") }}</span></div>
        <div v-if="missingCredentials.length">
          <b>缺少安全字段</b>
          <span v-for="item in missingCredentials" :key="item.email">{{ item.email }}（{{ (item.missing || []).map(credentialLabel).join("、") }}）</span>
        </div>
        <div v-if="conflicts.length"><b>目标冲突</b><span>{{ conflicts.join("；") }}</span></div>
      </div>

      <div class="preflight-footer">
        <small v-if="eligible">候选邮箱只在本次预检中保留一段时间，启动前会再次校验账号状态。{{ providerType === "fastmail_api" ? " Fastmail 别名领取后远端不会因取消预检而恢复。" : "" }}</small>
        <small v-else>请修正输入或先补齐当前账号的密码和 TOTP 2FA。</small>
        <AppButton v-if="eligible" variant="primary" :disabled="!canStart" :loading="rebind.requestBusy" @click="start">
          <Play :size="14" /><span>开始换绑</span>
        </AppButton>
      </div>
    </div>

    <!-- 换绑进度 -->
    <div v-if="progress.total" class="progress-block" :class="{ active: progressRunning }" aria-live="polite">
      <div class="progress-head">
        <strong>{{ progressRunning ? "邮箱换绑进行中" : progress.status === "completed" ? "邮箱换绑完成" : "最近一次换绑结果" }}</strong>
        <span>{{ progress.done || 0 }} / {{ progress.total || 0 }} · {{ percent }}%</span>
        <AppButton v-if="progressRunning" variant="danger" size="sm" :disabled="progress.stopping" @click="stop">
          <Square :size="12" /><span>{{ progress.stopping ? "停止中" : "停止" }}</span>
        </AppButton>
      </div>
      <div class="track"><i :style="{ width: `${percent}%` }"></i></div>
      <div class="progress-meta">
        <span>{{ progress.current || progress.lastMessage || "等待结果" }}</span>
        <span class="ok-text">成功 {{ progress.success || 0 }}</span>
        <span class="error-text">失败 {{ progress.failed || 0 }}</span>
        <span>已停止 {{ progress.cancelled || 0 }}</span>
      </div>
      <div v-if="progress.message" class="progress-message">{{ progress.message }}</div>
      <div v-if="progress.skipped?.length" class="progress-message error-text">启动前跳过：{{ progress.skipped.join("；") }}</div>
    </div>

    <div v-if="progressItems.length" class="table-wrap">
      <table>
        <thead><tr><th>原邮箱</th><th>新邮箱</th><th>状态</th><th>结果</th><th>AT</th></tr></thead>
        <tbody>
          <tr v-for="(item, index) in progressItems" :key="item.key || `${item.email}-${index}`">
            <td><strong class="email">{{ item.old_email || item.email || "-" }}</strong></td>
            <td><strong class="email">{{ item.new_email || "-" }}</strong></td>
            <td><AppTag :kind="itemStatusKind(item)">{{ itemStatusLabel(item) }}</AppTag></td>
            <td><span class="item-message">{{ item.message || "-" }}</span></td>
            <td><code v-if="item.access_token_masked" class="mono">{{ item.access_token_masked }}</code><span v-else class="muted">-</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="!hasPreflight && !progress.total" class="empty-hint">
      <ShieldCheck :size="18" /><span>输入一个或多个当前 GPT 账号邮箱后开始预检</span>
    </div>
  </section>
</template>

<style scoped>
.rebind-panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.panel-head h2 { display: flex; align-items: center; gap: 8px; font-size: 1.05rem; color: var(--text-primary); }
.panel-sub { font-size: 0.8rem; color: var(--text-muted); margin-top: 3px; }

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.field { display: flex; flex-direction: column; gap: 6px; font-size: 0.8rem; color: var(--text-secondary); }
.field textarea { resize: vertical; font-family: var(--font-mono, monospace); font-size: 0.8rem; }
.field-label { font-size: 0.8rem; color: var(--text-secondary); }

.input-with-btn { display: flex; gap: 8px; align-items: flex-start; }
.input-with-btn textarea { flex: 1; }

.target-box { display: flex; flex-direction: column; gap: 8px; }

.segmented {
  display: inline-flex;
  padding: 3px;
  border-radius: 10px;
  background: var(--inset-bg);
  border: 1px solid var(--line-subtle);
  gap: 2px;
  width: fit-content;
}

.segmented button {
  padding: 6px 14px;
  border-radius: 8px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 0.8rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.segmented button.active { background: var(--accent); color: #fff; }
.segmented button:disabled { opacity: 0.5; cursor: not-allowed; }

.auto-target {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  border: 1px dashed var(--line-subtle);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 0.8rem;
}

.inline-options { display: flex; gap: 16px; flex-wrap: wrap; }
.inline-options label { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; color: var(--text-secondary); }
.inline-options select { width: auto; }
.concurrency { width: 72px; text-align: center; }

.actions { display: flex; gap: 8px; }

.error-text { color: var(--error); }
.ok-text { color: var(--success); }
.muted { color: var(--text-muted); }
.mono { font-family: var(--font-mono, monospace); font-size: 0.75rem; }

.result-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  font-size: 0.84rem;
}

.result-banner.ok { background: var(--success-bg); color: var(--success); }
.result-banner.warn { background: var(--warning-bg); color: var(--warning); }

.batch-summary { border: 1px solid var(--line-subtle); border-radius: var(--radius-sm); overflow: hidden; }
.batch-summary-head {
  display: flex;
  justify-content: space-between;
  padding: 8px 12px;
  font-size: 0.78rem;
  color: var(--text-secondary);
  background: var(--inset-bg);
}

.batch-list { max-height: 220px; overflow: auto; }
.batch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 12px;
  border-top: 1px solid var(--line-subtle);
  font-size: 0.8rem;
}
.batch-index { color: var(--text-muted); font-size: 0.72rem; width: 20px; }
.batch-target { color: var(--accent); }
.batch-message { color: var(--text-muted); flex: 1; }
.batch-row strong { color: var(--text-primary); }

.route { display: flex; align-items: stretch; gap: 12px; }
.endpoint {
  flex: 1;
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.endpoint-kicker { font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; }
.endpoint strong { color: var(--text-primary); overflow-wrap: anywhere; }
.endpoint small { color: var(--text-muted); }
.endpoint-tags { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.route-arrow { align-self: center; color: var(--accent); flex-shrink: 0; }

.issues {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.78rem;
  color: var(--warning);
}
.issues b { margin-right: 8px; }
.issues span { margin-right: 12px; }

.preflight-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.preflight-footer small { color: var(--text-muted); }

.preflight { display: flex; flex-direction: column; gap: 12px; }

.progress-block {
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--inset-bg);
}

.progress-head { display: flex; align-items: center; gap: 12px; font-size: 0.82rem; color: var(--text-primary); }
.progress-head strong { flex: 1; }

.track { height: 6px; border-radius: 4px; background: var(--line-subtle); overflow: hidden; }
.track i { display: block; height: 100%; border-radius: 4px; background: var(--accent); transition: width 0.3s; }

.progress-meta { display: flex; gap: 14px; flex-wrap: wrap; font-size: 0.76rem; color: var(--text-secondary); }
.progress-message { font-size: 0.78rem; color: var(--text-secondary); }

.table-wrap { overflow: auto; max-height: 44vh; border: 1px solid var(--line-subtle); border-radius: var(--radius-sm); }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line-subtle); }
th { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; background: var(--inset-bg); position: sticky; top: 0; }
.email { color: var(--text-primary); }
.item-message { font-size: 0.78rem; color: var(--text-secondary); }

.empty-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 36px 0;
  color: var(--text-muted);
  font-size: 0.84rem;
}

@media (max-width: 768px) {
  .form-grid { grid-template-columns: 1fr; }
  .route { flex-direction: column; }
  .route-arrow { transform: rotate(90deg); }
}
</style>
