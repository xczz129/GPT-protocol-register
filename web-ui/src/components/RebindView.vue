<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";
import {
  AlertTriangle,
  ArrowLeftRight,
  ArrowRight,
  CheckCircle2,
  ClipboardPaste,
  Eraser,
  MailCheck,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
} from "@lucide/vue";

const props = defineProps({
  progress: { type: Object, default: () => ({}) },
  preflight: { type: Object, default: () => ({}) },
  busy: Boolean,
  checking: Boolean,
});

const emit = defineEmits(["preflight", "start", "stop", "reset", "input-change"]);

const INPUT_STATE_KEY = "gpt-register-fp.rebind-input.v1";
const sourceEmail = ref("");
const targetMode = ref("auto");
const targetEmail = ref("");
const providerType = ref("domain_api");
const concurrency = ref(1);
const pasteError = ref("");
const restoringInput = ref(true);

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
function splitEntries(value) {
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]|['"`]$/g, "").toLowerCase())
    .filter(Boolean);
}
const sourceEntries = computed(() => {
  const values = [];
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
const progressRunning = computed(() => !!props.progress?.running || ["queued", "running", "stopping"].includes(String(props.progress?.status || "")));
const reservationId = computed(() => String(props.preflight?.reservationId || props.preflight?.reservation_id || "").trim());
const eligible = computed(() => props.preflight?.eligible === true && !!reservationId.value);
const canCheck = computed(() => sourceEntries.value.some((value) => emailPattern.test(value)) && targetValid.value && !props.busy && !props.checking && !progressRunning.value);
const canStart = computed(() => eligible.value && !props.busy && !progressRunning.value);
const percent = computed(() => {
  const total = Number(props.progress?.total || 0);
  const done = Number(props.progress?.done || 0);
  return total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
});
const hasPreflight = computed(() => {
  const value = props.preflight || {};
  return Boolean(
    value.message
      || value.source
      || value.target
      || value.notFound?.length
      || value.invalidFormat?.length
      || value.invalid_format?.length
      || value.missingCredentials?.length
      || value.missing_credentials?.length
      || value.conflicts?.length
      || value.items?.length,
  );
});
const source = computed(() => props.preflight?.source || props.preflight?.source_account || null);
const target = computed(() => props.preflight?.target || null);
const preflightItems = computed(() => Array.isArray(props.preflight?.items) ? props.preflight.items : []);
const eligibleCount = computed(() => Number(props.preflight?.eligibleCount || props.preflight?.eligible_count || (props.preflight?.eligible ? 1 : 0)));
const invalidFormat = computed(() => props.preflight?.invalidFormat || props.preflight?.invalid_format || []);
const notFound = computed(() => props.preflight?.notFound || props.preflight?.not_found || []);
const missingCredentials = computed(() => props.preflight?.missingCredentials || props.preflight?.missing_credentials || []);
const conflicts = computed(() => props.preflight?.conflicts || []);
const progressItems = computed(() => Array.isArray(props.progress?.items) ? props.progress.items : []);

function providerLabel(value) {
  const provider = String(value || "domain_api");
  if (provider === "maddy_webui") return "Maddy 域名邮箱";
  if (provider === "fastmail_api") return "Fastmail 别名池";
  return "域名邮箱 API";
}

function credentialLabel(value) {
  return ({ password: "密码", totp: "TOTP 2FA", totp_secret: "TOTP 2FA" }[String(value || "").toLowerCase()] || String(value || "安全字段"));
}

function healthLabel(value) {
  return ({ alive: "可用", limited: "限流", reauth_required: "待续期", invalid: "失效", error: "错误", unknown: "未检测" }[String(value || "unknown").toLowerCase()] || String(value || "未检测"));
}

function planLabel(value) {
  const plan = String(value || "free").trim().toLowerCase();
  return plan === "plus" ? "Plus" : plan === "free" ? "Free" : plan.toUpperCase();
}

function itemStatusLabel(item) {
  const status = String(item?.status || "queued").toLowerCase();
  return ({ queued: "排队", running: "进行中", success: "成功", completed: "成功", failed: "失败", cancelled: "已停止", stopped: "已停止" }[status] || status);
}

function itemStatusClass(item) {
  const status = String(item?.status || "queued").toLowerCase();
  if (["success", "completed"].includes(status)) return "success";
  if (["failed"].includes(status)) return "failed";
  if (["cancelled", "stopped"].includes(status)) return "cancelled";
  return "running";
}

function domainOf(value) {
  const text = String(value || "");
  return text.includes("@") ? text.split("@").slice(1).join("@") : "";
}

function check() {
  if (!canCheck.value) return;
  emit("preflight", {
    text: String(sourceEmail.value || "").trim(),
    new_email: targetMode.value === "custom" ? String(targetEmail.value || "").trim() : "",
    provider_type: providerType.value,
  });
}

function start() {
  if (!canStart.value) return;
  emit("start", {
    reservation_id: reservationId.value,
    provider_type: providerType.value,
    concurrency: Math.min(20, Math.max(1, Math.floor(Number(concurrency.value) || 1))),
  });
}

async function pasteEmail() {
  pasteError.value = "";
  try {
    sourceEmail.value = String(await navigator.clipboard.readText() || "").trim();
  } catch (error) {
    pasteError.value = error?.message || "读取剪贴板失败";
  }
}

function clearForm() {
  sourceEmail.value = "";
  targetEmail.value = "";
  targetMode.value = "auto";
  providerType.value = "domain_api";
  concurrency.value = 1;
  pasteError.value = "";
  emit("reset");
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
  } catch {
    // Local storage can be disabled; the page remains usable in memory.
  }
}

onMounted(async () => {
  try {
    const saved = JSON.parse(localStorage.getItem(INPUT_STATE_KEY) || "null");
    if (saved && typeof saved === "object") {
      if (typeof saved.sourceEmail === "string") sourceEmail.value = saved.sourceEmail;
      if (["auto", "custom"].includes(saved.targetMode)) targetMode.value = saved.targetMode;
      if (typeof saved.targetEmail === "string") targetEmail.value = saved.targetEmail;
      if (["domain_api", "maddy_webui", "fastmail_api"].includes(saved.providerType)) providerType.value = saved.providerType;
      if (Number.isFinite(Number(saved.concurrency))) concurrency.value = Math.min(20, Math.max(1, Number(saved.concurrency)));
    }
  } catch {
    // Ignore malformed or unavailable local state.
  }
  // Let the initial v-model restoration flush before input changes can clear
  // a server-side preflight restored by the parent shell.
  await nextTick();
  restoringInput.value = false;
});

watch([sourceEmail, targetMode, targetEmail, providerType], () => {
  saveInputState();
  if (restoringInput.value) return;
  emit("input-change");
});
watch(concurrency, saveInputState);
</script>

<template>
  <section class="card rebind-panel">
    <div class="card-h rebind-head">
      <div>
        <h2><MailCheck :size="16" aria-hidden="true" />邮箱换绑</h2>
        <p class="subtle">验证当前账号后，从所选邮箱池预留一个未占用邮箱并同步新的登录凭据。</p>
      </div>
      <span class="rebind-mode"><ArrowLeftRight :size="14" aria-hidden="true" />协议流程</span>
    </div>

    <div class="rebind-form-grid">
      <label class="rebind-field rebind-source-field">
        <span>当前 GPT 账号邮箱</span>
        <div class="rebind-input-with-actions">
          <textarea v-model="sourceEmail" rows="5" autocomplete="off" spellcheck="false" placeholder="每行一个账号邮箱，可粘贴多行" :disabled="props.busy || props.checking || progressRunning" @keydown.ctrl.enter.prevent="check" @keydown.meta.enter.prevent="check"></textarea>
          <button class="btn secondary rebind-icon-btn" type="button" title="从剪贴板粘贴邮箱" aria-label="从剪贴板粘贴邮箱" :disabled="props.busy || props.checking || progressRunning" @click="pasteEmail"><ClipboardPaste :size="15" aria-hidden="true" /></button>
        </div>
        <small v-if="sourceEmail && !sourceValid" class="rebind-error">每行输入一个有效的邮箱地址，可用逗号或空格分隔</small>
        <small v-if="pasteError" class="rebind-error">{{ pasteError }}</small>
      </label>

      <div class="rebind-target-box">
        <span class="rebind-field-label">新邮箱</span>
        <div class="rebind-mode-switch" role="tablist" aria-label="新邮箱来源">
          <button type="button" :class="{ active: targetMode === 'auto' }" :disabled="props.busy || props.checking || progressRunning" @click="targetMode = 'auto'">自动从邮箱池选择</button>
          <button type="button" :class="{ active: targetMode === 'custom' }" :disabled="props.busy || props.checking || progressRunning" @click="targetMode = 'custom'">指定邮箱</button>
        </div>
        <textarea v-if="targetMode === 'custom'" v-model="targetEmail" rows="5" autocomplete="off" spellcheck="false" placeholder="每行一个新邮箱，顺序与左侧账号对应" :disabled="props.busy || props.checking || progressRunning"></textarea>
        <div v-else class="rebind-auto-target"><RefreshCw :size="14" aria-hidden="true" /><span>预检时从所选邮箱池中原子预留</span></div>
        <small v-if="targetMode === 'custom' && targetEmail && !targetValid" class="rebind-error">批量指定时，新邮箱数量要与账号数量一致，且格式有效</small>
        <div class="rebind-inline-options">
          <label>邮箱来源<select v-model="providerType" :disabled="props.busy || props.checking || progressRunning"><option value="domain_api">域名邮箱 API</option><option value="maddy_webui">Maddy 域名邮箱</option><option value="fastmail_api">Fastmail 别名池</option></select></label>
          <label>并发<input v-model.number="concurrency" type="number" min="1" max="20" inputmode="numeric" :disabled="props.busy || props.checking || progressRunning" /></label>
        </div>
      </div>
    </div>

    <div class="rebind-actions">
      <button class="btn secondary" type="button" :disabled="!sourceEmail && !targetEmail || props.busy || progressRunning" @click="clearForm"><Eraser :size="15" aria-hidden="true" />清空</button>
      <button class="btn primary" type="button" :disabled="!canCheck" @click="check"><RefreshCw :size="15" aria-hidden="true" />{{ props.checking ? "检查中" : "检查并匹配邮箱" }}</button>
    </div>

    <div v-if="hasPreflight" class="rebind-preflight" aria-live="polite">
      <div class="rebind-result-banner" :class="eligible ? 'ok' : 'warn'">
        <CheckCircle2 v-if="eligible" :size="16" aria-hidden="true" />
        <AlertTriangle v-else :size="16" aria-hidden="true" />
        <span>{{ props.preflight.message || (eligible ? `已匹配 ${eligibleCount} 个可用目标邮箱` : "预检未通过") }}</span>
      </div>

      <div v-if="preflightItems.length > 1" class="rebind-batch-summary">
        <div class="rebind-batch-summary-head"><strong>逐项预检结果</strong><span>{{ props.preflight.matched || 0 }} / {{ props.preflight.requested || preflightItems.length }} 个账号已识别</span></div>
        <div class="rebind-batch-list">
          <div v-for="(item, index) in preflightItems" :key="item.record_key || item.input || index" class="rebind-batch-row">
            <span class="rebind-batch-index">{{ index + 1 }}</span>
            <strong>{{ item.source?.email || item.input || "-" }}</strong>
            <span v-if="item.target?.email" class="rebind-batch-target">{{ item.target.email }}</span>
            <span v-else class="rebind-batch-message">{{ item.message || item.status || "-" }}</span>
            <span :class="['rebind-item-status', item.status === 'eligible' ? 'success' : 'failed']">{{ item.status === 'eligible' ? "可执行" : (item.status === 'missing_credentials' ? "缺安全字段" : item.status === 'not_found' ? "未找到" : "跳过") }}</span>
          </div>
        </div>
      </div>

      <div v-else-if="source || target" class="rebind-route">
        <article v-if="source" class="rebind-endpoint">
          <span class="rebind-endpoint-kicker">当前账号</span>
          <strong>{{ source.email || sourceEmail }}</strong>
          <small v-if="source.account_id">Account ID：{{ source.account_id }}</small>
          <div class="rebind-endpoint-tags"><span class="tag">{{ planLabel(source.plan_type) }}</span><span class="tag">{{ healthLabel(source.health_status) }}</span><span :class="['tag', source.has_password && source.password_confirmed !== false && source.has_totp ? 'ok' : 'warn']">{{ source.has_password && source.password_confirmed !== false && source.has_totp ? "密码 / 2FA 齐全" : "缺少安全字段" }}</span></div>
        </article>
        <ArrowRight v-if="source && target" class="rebind-route-arrow" :size="20" aria-hidden="true" />
        <article v-if="target" class="rebind-endpoint target">
          <span class="rebind-endpoint-kicker">换绑目标</span>
          <strong>{{ target.email || "-" }}</strong>
          <small>{{ providerLabel(target.provider || providerType) }}<template v-if="target.domain || domainOf(target.email)"> · {{ target.domain || domainOf(target.email) }}</template></small>
          <div class="rebind-endpoint-tags"><span class="tag ok">已预留</span><small v-if="target.reserved_at">预检时间 {{ target.reserved_at }}</small></div>
        </article>
      </div>

      <div v-if="invalidFormat.length || notFound.length || missingCredentials.length || conflicts.length || props.preflight.invalidTargets?.length" class="rebind-issues">
        <div v-if="invalidFormat.length"><b>格式无效</b><span>{{ invalidFormat.join("、") }}</span></div>
        <div v-if="notFound.length"><b>未找到账号</b><span>{{ notFound.join("、") }}</span></div>
        <div v-if="missingCredentials.length"><b>缺少安全字段</b><span v-for="item in missingCredentials" :key="item.email">{{ item.email }}（{{ (item.missing || []).map(credentialLabel).join("、") }}）</span></div>
        <div v-if="conflicts.length"><b>目标冲突</b><span>{{ conflicts.join("；") }}</span></div>
        <div v-if="props.preflight.invalidTargets?.length"><b>目标格式无效</b><span>{{ props.preflight.invalidTargets.join("、") }}</span></div>
      </div>

      <div class="rebind-preflight-footer">
        <small v-if="eligible">候选邮箱只在本次预检中保留一段时间，启动前会再次校验账号状态。{{ providerType === 'fastmail_api' ? ' Fastmail 别名领取后远端不会因取消预检而恢复。' : '' }}</small>
        <small v-else>请修正输入或先补齐当前账号的密码和 TOTP 2FA。</small>
        <button v-if="eligible" class="btn primary" type="button" :disabled="!canStart" @click="start"><Play :size="15" aria-hidden="true" />开始换绑</button>
      </div>
    </div>

    <div v-if="props.progress?.total" class="rebind-progress" :class="{ active: progressRunning }" aria-live="polite">
      <div class="rebind-progress-head"><strong>{{ progressRunning ? "邮箱换绑进行中" : (props.progress?.status === 'completed' ? "邮箱换绑完成" : "最近一次换绑结果") }}</strong><span>{{ props.progress.done || 0 }} / {{ props.progress.total || 0 }} · {{ percent }}%</span><button v-if="progressRunning" class="btn danger rebind-stop" type="button" :disabled="props.progress.stopping" @click="emit('stop')"><Square :size="13" aria-hidden="true" />{{ props.progress.stopping ? "停止中" : "停止" }}</button></div>
      <div class="rebind-track"><i :style="{ width: `${percent}%` }"></i></div>
      <div class="rebind-progress-meta"><span class="rebind-current">{{ props.progress.current || props.progress.lastMessage || "等待结果" }}</span><span class="ok-text">成功 {{ props.progress.success || 0 }}</span><span class="danger-text">失败 {{ props.progress.failed || 0 }}</span><span>已停止 {{ props.progress.cancelled || 0 }}</span></div>
      <div v-if="props.progress.message" class="rebind-progress-message">{{ props.progress.message }}</div>
      <div v-if="props.progress.skipped?.length" class="rebind-progress-message danger-text">启动前跳过：{{ props.progress.skipped.join("；") }}</div>
    </div>

    <div v-if="progressItems.length" class="rebind-items-wrap">
      <table class="rebind-items">
        <thead><tr><th>原邮箱</th><th>新邮箱</th><th>状态</th><th>结果</th><th>AT</th></tr></thead>
        <tbody>
          <tr v-for="(item, index) in progressItems" :key="item.key || `${item.email}-${index}`">
            <td><strong>{{ item.old_email || item.email || "-" }}</strong></td>
            <td><strong>{{ item.new_email || "-" }}</strong></td>
            <td><span :class="['rebind-item-status', itemStatusClass(item)]">{{ itemStatusLabel(item) }}</span></td>
            <td><span class="rebind-item-message">{{ item.message || "-" }}</span></td>
            <td><code v-if="item.access_token_masked">{{ item.access_token_masked }}</code><span v-else class="muted">-</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="!hasPreflight && !props.progress?.total" class="rebind-empty"><ShieldCheck :size="18" aria-hidden="true" /><span>输入一个或多个当前 GPT 账号邮箱后开始预检</span></div>
  </section>
</template>
