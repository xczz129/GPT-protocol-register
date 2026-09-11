<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { ArrowRight, CircleAlert, RefreshCw, Smartphone } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppButton from "../../components/ui/AppButton.vue";
import { codexPreflight, type CodexPreflight, type SmsOptions } from "../../api/batchops";
import { useBatchOpsStore } from "../../stores/batchops";
import { useAccountsStore } from "../../stores/accounts";

const batch = useBatchOpsStore();
const accounts = useAccountsStore();
const checking = ref(false);
const error = ref("");
const preview = ref<CodexPreflight | null>(null);
const initialized = ref(false);
const form = reactive({ force: false, concurrency: 3, provider: "smsbower", countryOrder: "", maxPrice: "", attempts: 3, timeout: 180 });
let requestId = 0;
let debounce: ReturnType<typeof setTimeout> | undefined;

const invalid = computed(() => {
  if (!Number.isInteger(form.concurrency) || form.concurrency < 1 || form.concurrency > 20) return "并发必须是 1–20 之间的整数";
  if (!Number.isInteger(form.attempts) || form.attempts < 1 || form.attempts > 20) return "每个账号的取号上限必须是 1–20 之间的整数";
  if (!Number.isInteger(form.timeout) || form.timeout < 30 || form.timeout > 1800) return "单个号码的等码时间必须是 30–1800 秒之间的整数";
  if (form.provider === "smsbower" && form.maxPrice.trim() && (!/^\d+(\.\d+)?$/.test(form.maxPrice.trim()) || Number(form.maxPrice) <= 0)) return "每号最高价必须是大于 0 的金额，留空表示不设置本次统一限价";
  return "";
});
const unavailable = computed(() => (preview.value?.missing_credentials.length || 0) + (preview.value?.not_found.length || 0) + (preview.value?.deactivated.length || 0));
const validCount = computed(() => (preview.value?.eligible || 0) + (preview.value?.skipped || 0));
const configurationError = computed(() => preview.value && (!preview.value.configured || !preview.value.settings.enabled));
const launchDisabled = computed(() => !initialized.value || checking.value || !!error.value || !!invalid.value || !preview.value || !validCount.value || !!configurationError.value || batch.codex.running);
const launchLabel = computed(() => checking.value ? "正在预检…" : preview.value?.eligible ? `开始处理 ${preview.value.eligible} 个账号` : `沿用 ${preview.value?.skipped || 0} 个已有 RT`);

function options(): SmsOptions {
  return {
    provider: form.provider,
    ...(form.countryOrder.trim() ? { country_order: form.countryOrder.trim() } : {}),
    max_price: form.provider === "smsbower" ? form.maxPrice.trim() : "",
    max_phone_attempts: form.attempts,
    timeout_seconds: form.timeout,
  };
}

async function check(loadDefaults = false, providerDefaults = false) {
  const id = ++requestId;
  if (debounce) clearTimeout(debounce);
  if (!batch.codexStartOpen) return;
  checking.value = true;
  error.value = "";
  try {
    const overrides = loadDefaults ? undefined : providerDefaults ? { provider: form.provider } : options();
    const result = await codexPreflight({ emails: batch.codexStartEmails, concurrency: form.concurrency, force: form.force, sms_options: overrides });
    if (id !== requestId || !batch.codexStartOpen) return;
    preview.value = result;
    if (loadDefaults || providerDefaults) {
      initialized.value = false;
      form.provider = result.settings.provider;
      form.countryOrder = result.settings.country_order || "";
      form.maxPrice = result.settings.max_price || "";
      if (loadDefaults) {
        form.attempts = result.settings.max_phone_attempts;
        form.timeout = result.settings.timeout_seconds;
      }
      initialized.value = true;
    }
  } catch (cause) {
    if (id === requestId) error.value = cause instanceof Error ? cause.message : "预检失败，请重试";
  } finally {
    if (id === requestId) checking.value = false;
  }
}

watch(() => batch.codexStartOpen, (open) => {
  ++requestId;
  if (debounce) clearTimeout(debounce);
  initialized.value = false;
  if (!open) return;
  preview.value = null;
  error.value = "";
  form.force = batch.codexStartForce;
  form.concurrency = batch.codexStartEmails.length === 1 ? 1 : Math.min(20, batch.lastCodexOptions.concurrency || 3);
  void check(true);
});

watch(() => [form.force, form.concurrency, form.countryOrder, form.maxPrice, form.attempts, form.timeout], () => {
  if (!initialized.value || !batch.codexStartOpen) return;
  ++requestId;
  if (debounce) clearTimeout(debounce);
  checking.value = !invalid.value;
  if (!invalid.value) debounce = setTimeout(() => void check(), 300);
});

function changeProvider() {
  if (initialized.value) void check(false, true);
}

async function launch() {
  if (launchDisabled.value || batch.codexStartBusy) return;
  error.value = "";
  try {
    await batch.runCodex([...batch.codexStartEmails], form.concurrency, form.force, options());
    accounts.clearSelection();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "接码任务启动失败";
  }
}

function close() {
  if (!batch.codexStartBusy) batch.codexStartOpen = false;
}

onBeforeUnmount(() => {
  ++requestId;
  if (debounce) clearTimeout(debounce);
});
</script>

<template>
  <AppModal :open="batch.codexStartOpen" title="设置接码任务" subtitle="先核对账号与取号范围，开始后可随时查看进度" width="720px" @close="close">
    <template #icon><Smartphone :size="18" /></template>
    <div class="sms-start">
      <div class="preflight-grid" aria-live="polite" :aria-busy="checking">
        <div><span>已选择</span><b>{{ batch.codexStartEmails.length }}</b><small>个账号</small></div>
        <div class="eligible"><span>需要获取</span><b>{{ preview?.eligible ?? '—' }}</b><small>缺少 RT 或重新获取</small></div>
        <div><span>沿用已有</span><b>{{ preview?.skipped ?? '—' }}</b><small>不重复取号</small></div>
        <div :class="{ unavailable: unavailable > 0 }"><span>无法处理</span><b>{{ preview ? unavailable : '—' }}</b><small>凭据缺失或账号不可用</small></div>
      </div>

      <fieldset class="mode-options" :disabled="batch.codexStartBusy">
        <legend>本次处理方式</legend>
        <label :class="{ selected: !form.force }"><input v-model="form.force" type="radio" :value="false" name="sms-mode" /><span><b>补取缺少的 RT</b><small>已有 RT 的账号直接沿用</small></span></label>
        <label :class="{ selected: form.force }"><input v-model="form.force" type="radio" :value="true" name="sms-mode" /><span><b>重新获取已有 RT</b><small>选中的可用账号全部重新授权，可能再次取号</small></span></label>
      </fieldset>

      <fieldset class="settings" :disabled="batch.codexStartBusy || !initialized">
        <legend>本次参数 <small>仅用于本任务</small></legend>
        <div class="field"><label for="sms-run-provider">接码渠道</label><select id="sms-run-provider" v-model="form.provider" class="input" @change="changeProvider"><option value="smsbower">SMSBower</option><option value="firefox">火狐狸</option></select></div>
        <div class="field"><label for="sms-run-concurrency">同时处理账号数</label><input id="sms-run-concurrency" v-model.number="form.concurrency" class="input" type="number" min="1" max="20" step="1" /><small>建议从 1–3 开始</small></div>
        <div class="field country"><label for="sms-run-country">国家顺序</label><input id="sms-run-country" v-model="form.countryOrder" class="input" type="text" autocomplete="off" :disabled="form.provider === 'firefox'" :placeholder="form.provider === 'firefox' ? '例如 usa, chl, eng' : '例如 6, 4, 151'" /><small>{{ form.provider === 'firefox' ? '国家由已保存的对接码确定；更换国家请到接码设置更新对接码' : '按国家编号依次尝试，逗号分隔；可沿用已保存的国家与供应商顺序' }}</small></div>
        <div class="field"><label for="sms-run-price">每个号码最高价</label><input id="sms-run-price" v-model="form.maxPrice" class="input" type="text" inputmode="decimal" :disabled="form.provider !== 'smsbower'" :placeholder="form.provider === 'smsbower' ? '留空不设统一限价' : '此渠道不支持价格上限'" /><small>{{ form.provider === 'smsbower' ? '按渠道计价币种；留空不设统一限价，国家单独限价仍生效。非任务总预算' : '火狐狸当前接口不支持限价' }}</small></div>
        <div class="field"><label for="sms-run-attempts">每个账号最多取号</label><div class="unit-field"><input id="sms-run-attempts" v-model.number="form.attempts" class="input" type="number" min="1" max="20" step="1" /><span>次</span></div></div>
        <div class="field"><label for="sms-run-timeout">单个号码等码时间</label><div class="unit-field"><input id="sms-run-timeout" v-model.number="form.timeout" class="input" type="number" min="30" max="1800" step="30" /><span>秒</span></div></div>
      </fieldset>

      <details v-if="unavailable" class="preflight-details">
        <summary>查看 {{ unavailable }} 个无法处理的账号</summary>
        <ul><li v-for="item in preview?.missing_credentials || []" :key="`missing-${item.email}`"><b>{{ item.email }}</b><span>缺少 {{ item.missing.map((field) => ({ password: '密码', totp_secret: '2FA', email: '邮箱' }[field] || field)).join('、') }}</span></li><li v-for="email in preview?.not_found || []" :key="`not-found-${email}`"><b>{{ email }}</b><span>账号已不存在</span></li><li v-for="email in preview?.deactivated || []" :key="`deactivated-${email}`"><b>{{ email }}</b><span>账号已停用</span></li></ul>
      </details>

      <p v-if="configurationError" class="feedback" role="alert"><CircleAlert :size="16" />当前渠道尚未启用或缺少凭证，请到运维中心的接码设置中配置。</p>
      <p v-if="error || invalid" class="feedback error" role="alert"><CircleAlert :size="16" />{{ invalid || error }}<button v-if="error && !invalid" type="button" @click="check(!initialized)"><RefreshCw :size="13" />重新预检</button></p>
      <p v-if="batch.codex.running" class="feedback" role="status">已有接码或导入任务正在运行，请等待结束后再开始。</p>
      <p class="scope-note">{{ form.force ? '已明确选择重新获取。开始后可能产生新的取号费用。' : '已有 RT 的账号不会重复取号。' }}关闭进度窗口后，任务仍继续运行。</p>
    </div>
    <template #footer><AppButton :disabled="batch.codexStartBusy" @click="close">取消</AppButton><AppButton variant="primary" :disabled="launchDisabled" :loading="batch.codexStartBusy" @click="launch">{{ launchLabel }}<ArrowRight :size="15" /></AppButton></template>
  </AppModal>
</template>

<style scoped>
.sms-start { display: grid; gap: 18px; color: var(--text-secondary); }
.preflight-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; border: 1px solid var(--field-border); border-radius: var(--radius-sm); overflow: hidden; background: var(--line-subtle); }
.preflight-grid > div { padding: 12px; display: grid; gap: 3px; background: var(--field-bg); }
.preflight-grid span { font-size: .75rem; }.preflight-grid b { font: 600 1.65rem var(--mono); color: var(--text-primary); }.preflight-grid small { font-size: .69rem; }.preflight-grid .eligible b { color: var(--accent); }.preflight-grid .unavailable b { color: var(--error); }
fieldset { padding: 0; margin: 0; border: 0; min-width: 0; }legend { font-size: .83rem; color: var(--text-primary); font-weight: 600; margin-bottom: 9px; }legend small { font-weight: 400; color: var(--text-secondary); margin-left: 8px; }
.mode-options { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }.mode-options label { display: flex; gap: 9px; padding: 12px; border: 1px solid var(--field-border); background: var(--glass-bg); border-radius: var(--radius-sm); cursor: pointer; align-items: flex-start; }.mode-options label.selected { border-color: var(--accent); background: var(--accent-bg); }.mode-options input { margin-top: 4px; accent-color: var(--accent); }.mode-options span { display: grid; gap: 4px; }.mode-options b { color: var(--text-primary); font-size: .82rem; font-weight: 600; }.mode-options small { font-size: .73rem; line-height: 1.5; }
.settings { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; }.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }.field label { font-size: .78rem; color: var(--text-primary); }.field small { font-size: .71rem; line-height: 1.5; }.input { min-height: 42px; width: 100%; font-size: .82rem; }.unit-field { position: relative; }.unit-field input { padding-right: 38px; }.unit-field span { position: absolute; right: 28px; top: 12px; font-size: .75rem; pointer-events: none; }
.preflight-details { border: 1px solid var(--field-border); border-radius: var(--radius-sm); padding: 10px 12px; font-size: .77rem; }.preflight-details summary { cursor: pointer; color: var(--text-primary); }.preflight-details ul { padding: 10px 0 0; margin: 0; max-height: 160px; overflow: auto; list-style: none; }.preflight-details li { padding: 5px 0; display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; }.preflight-details b { font-weight: 500; overflow-wrap: anywhere; }
.feedback { padding: 10px 12px; background: var(--warning-bg); color: var(--text-primary); border-radius: var(--radius-sm); display: flex; align-items: center; gap: 8px; font-size: .78rem; }.feedback svg { flex-shrink: 0; }.feedback.error { background: var(--error-bg); }.feedback button { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; background: transparent; border: 0; color: var(--accent); cursor: pointer; min-height: 32px; white-space: nowrap; }.scope-note { font-size: .73rem; line-height: 1.6; }
:deep(button:focus-visible), input:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }fieldset:disabled { opacity: .65; }
@media (max-width: 520px) { .preflight-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.mode-options, .settings { grid-template-columns: 1fr; }.input { font-size: 16px; min-height: 44px; }.feedback { flex-wrap: wrap; } }
@media (prefers-reduced-motion: reduce) { :deep(*) { animation: none !important; transition: none !important; } }
</style>
