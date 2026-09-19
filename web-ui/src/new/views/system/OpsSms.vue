<script setup lang="ts">
/* --- 运维中心 · 接码设置：SMSBower --- */
import { computed, reactive, ref, watch } from "vue";
import { Globe } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import SmsCountryPicker from "../../components/sms/SmsCountryPicker.vue";
import { fetchSmsCountries, type SmsCountryItem } from "../../api/ops";
import { useOperationsStore } from "../../stores/operations";
import { useUiStore } from "../../stores/ui";

const ops = useOperationsStore();
const ui = useUiStore();

const form = reactive({
  enabled: false,
  apiKey: "",
  countryOrder: "6, 4, 151",
  timeoutSeconds: 600,
  maxPhoneAttempts: 0,
  maxPrice: "" as string | number,
});

let inited = false;
watch(
  () => ops.smsConfig,
  (cfg) => {
    if (!cfg || inited) return;
    inited = true;
    form.enabled = !!cfg.enabled;
    form.countryOrder = String(cfg.country_order || "6, 4, 151");
    form.timeoutSeconds = Number(cfg.timeout_seconds ?? 600);
    form.maxPhoneAttempts = Number(cfg.max_phone_attempts ?? 0);
    form.maxPrice = String(cfg.max_price || "");
  },
  { immediate: true },
);

const cfg = computed(() => ops.smsConfig);
const savedProviderLabel = computed(() => cfg.value?.provider === "smsbower" ? "SMSBower" : "已停用渠道");
const hasUnsavedChanges = computed(() => {
  const c = cfg.value;
  if (!c) return false;
  return !!(form.apiKey.trim())
    || form.enabled !== !!c.enabled || c.provider !== "smsbower"
    || form.countryOrder.trim() !== String(c.country_order || "6, 4, 151").trim()
    || String(form.maxPrice ?? "").trim() !== String(c.max_price || "").trim()
    || form.timeoutSeconds !== Number(c.timeout_seconds ?? 600)
    || form.maxPhoneAttempts !== Number(c.max_phone_attempts ?? 0);
});
const balanceTime = computed(() => {
  const value = ops.smsBalance?.checkedAt;
  return value ? new Date(value).toLocaleString() : "";
});

/* --- 国家选择器：country_order 支持 id / id:price / id=pid1|pid2 三种 token --- */
interface CountryToken {
  id: string;
  price?: string;
  providerId?: string;
}

function parseCountryToken(raw: string): CountryToken | null {
  const token = String(raw || "").trim();
  if (!token) return null;
  if (token.includes("=")) {
    const [country, pid] = token.split("=");
    return { id: (country || "").trim(), providerId: (pid || "").trim() };
  }
  const parts = token.split(":");
  if (parts.length > 1) return { id: (parts[0] || "").trim(), price: (parts[1] || "").trim() };
  return { id: token };
}

const picker = reactive({
  open: false,
  loading: false,
  error: "",
  items: [] as SmsCountryItem[],
  selected: [] as string[],
  providers: {} as Record<string, string>,
  prices: {} as Record<string, string>,
});

/* 国家名称缓存（chips 友好显示），拉取过国家列表后可用 */
const countryNames = ref<Record<string, string>>({});

function cacheCountryNames(items: SmsCountryItem[]) {
  const names: Record<string, string> = {};
  for (const item of items) {
    names[String(item.id)] = item.calling ? `${item.name} ${item.calling}` : String(item.name || item.id);
  }
  countryNames.value = names;
}

async function openCountryPicker() {
  picker.open = true;
  picker.loading = true;
  picker.error = "";
  picker.selected = [];
  picker.providers = {};
  picker.prices = {};
  for (const raw of form.countryOrder.split(/[,，]/).map((s) => s.trim()).filter(Boolean)) {
    const parsed = parseCountryToken(raw);
    if (!parsed?.id) continue;
    picker.selected.push(parsed.id);
    if (parsed.price) picker.prices[parsed.id] = parsed.price;
    if (parsed.providerId) picker.providers[parsed.id] = parsed.providerId;
  }
  try {
    const value = await fetchSmsCountries();
    picker.items = value.items || [];
    cacheCountryNames(picker.items);
  } catch (err) {
    picker.error = err instanceof Error ? err.message : "获取国家列表失败";
  } finally {
    picker.loading = false;
  }
}

function rebuildCountryOrder() {
  form.countryOrder = picker.selected
    .map((id) => {
      if (picker.providers[id]) return `${id}=${picker.providers[id]}`;
      if (picker.prices[id]) return `${id}:${picker.prices[id]}`;
      return id;
    })
    .join(", ");
}

function toggleCountry(id: string) {
  const value = String(id);
  const index = picker.selected.indexOf(value);
  if (index >= 0) {
    picker.selected.splice(index, 1);
    delete picker.prices[value];
    delete picker.providers[value];
  } else {
    picker.selected.push(value);
  }
  rebuildCountryOrder();
}

function setCountryProvider(payload: { id: string; providerId: string }) {
  if (payload.providerId) picker.providers[payload.id] = payload.providerId;
  else delete picker.providers[payload.id];
  rebuildCountryOrder();
}

const countryChips = computed(() =>
  form.countryOrder
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .map((raw) => {
      const parsed = parseCountryToken(raw);
      if (!parsed) return raw;
      const label = countryNames.value[parsed.id] || parsed.id;
      if (parsed.providerId) {
        const pids = parsed.providerId
          .split(/[|,]/)
          .map((p) => `#${p.trim()}`)
          .filter((p) => p !== "#");
        return pids.length ? `${label} ${pids.join("→")}` : label;
      }
      if (parsed.price) return `${label} ≤$${parsed.price}`;
      return label;
    }),
);

const statusHint = computed(() => {
  const c = cfg.value;
  if (!c) return "";
  if (c.provider !== "smsbower") return "原接码渠道已停用，请保存 SMSBower 配置后使用";
  if (!c.configured) return "未配置接码凭证";
  return c.enabled ? `已启用 ${c.provider_label || ""}` : `已配置 ${c.provider_label || ""} 但未启用`;
});

function buildBody(): Record<string, unknown> {
  const c = cfg.value;
  const price = String(form.maxPrice ?? "").trim();
  if (price && (!Number.isFinite(Number(price)) || Number(price) <= 0)) {
    throw new Error("每号最高价必须是大于 0 的数字，或留空不限价");
  }
  if (!Number.isInteger(form.maxPhoneAttempts) || form.maxPhoneAttempts < 0) {
    throw new Error("取号次数必须是非负整数，0 表示不限次数");
  }
  const body: Record<string, unknown> = {
    enabled: form.enabled,
    provider: "smsbower",
    service: c?.service || "dr",
    country_order: form.countryOrder.trim(),
    poll_interval_ms: c?.poll_interval_ms ?? 2000,
    timeout_seconds: form.timeoutSeconds,
    max_price: price,
    max_phone_attempts: form.maxPhoneAttempts,
  };
  // 密钥留空表示沿用已保存值，仅在填写时提交
  if (form.apiKey.trim()) body.api_key = form.apiKey.trim();
  return body;
}

async function save() {
  try {
    await ops.saveSmsConfig(buildBody());
    form.apiKey = "";
    ui.notify("接码设置已保存", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "接码设置保存失败", "error");
  }
}

async function testBalance() {
  if (cfg.value?.provider !== "smsbower") { ui.notify("请先保存 SMSBower 接码配置", "error"); return; }
  try {
    const result = await ops.testSms();
    ui.notify(result.message || (result.ok ? "查询成功" : "查询失败"), result.ok ? "success" : "error");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "余额查询请求失败", "error");
  }
}

</script>

<template>
  <section class="card glass-panel">
    <div class="card-head">
      <h2 class="section-title">接码设置</h2>
      <p class="card-sub">Codex OAuth 补发 RT 用的手机验证接码渠道</p>
    </div>

    <div class="form-grid">
      <label class="check"><input v-model="form.enabled" type="checkbox" />启用自动接码</label>
        <label class="field span-2">
          <span>SMSBower API Key</span>
          <input
            v-model="form.apiKey"
            class="input"
            type="password"
            :placeholder="cfg?.has_api_key ? `已保存 ${cfg.api_key_masked}` : 'smsbower API key'"
            autocomplete="off"
          />
        </label>
        <label class="field span-2">
          <span>国家顺序（按顺序取号，可手填或用选择器查看各国价位）</span>
          <div class="country-row">
            <input v-model="form.countryOrder" class="input" type="text" placeholder="6, 4, 151 或 6=1170|1168, 4:0.5" />
            <AppButton :loading="picker.loading && picker.open" @click="openCountryPicker">
              <span class="btn-inner"><Globe :size="13" />选择国家</span>
            </AppButton>
          </div>
        </label>
        <div v-if="countryChips.length" class="span-2 chips">
          <span v-for="(label, index) in countryChips" :key="label + index" class="chip">{{ index + 1 }}. {{ label }}</span>
        </div>
        <label class="field span-2">
          <span>每号最高价</span>
          <input v-model="form.maxPrice" class="input" type="number" min="0" step="0.001" placeholder="留空不限价" />
          <small>取号时使用的价格上限。国家选择器中的价格筛选只影响列表展示。</small>
        </label>
      <label class="field">
        <span>等码超时（秒）</span>
        <input v-model.number="form.timeoutSeconds" class="input" type="number" min="30" max="1800" />
      </label>
      <label class="field">
        <span>取号次数上限（0 表示不限）</span>
        <input v-model.number="form.maxPhoneAttempts" class="input" type="number" min="0" step="1" />
        <small>0 为不限次数；正整数包含首次取号。同号码重建会话不增加取号次数，可随时停止任务。</small>
      </label>
    </div>

    <div class="actions">
      <AppButton variant="primary" :loading="ops.busy['sms-save']" @click="save">保存接码设置</AppButton>
      <AppButton :loading="ops.busy['sms-test']" :disabled="cfg?.provider !== 'smsbower' || !cfg?.configured || ops.busy['sms-save']" @click="testBalance">查询已保存的 {{ savedProviderLabel }} 余额</AppButton>
    </div>
    <p class="hint">{{ statusHint }}</p>
    <p v-if="hasUnsavedChanges" class="draft-hint" role="status">有未保存的更改。余额查询仍使用已保存的 {{ savedProviderLabel }} 配置。</p>
    <div class="balance-panel" aria-live="polite">
      <div>
        <span class="balance-label">{{ ops.smsBalance?.providerLabel || savedProviderLabel }} · 账户余额</span>
        <strong v-if="ops.smsBalance && !ops.smsBalance.error && ops.smsBalance.amount">{{ ops.smsBalance.amount }} <small>平台余额单位</small></strong>
        <span v-else-if="ops.smsBalance?.error" class="balance-error">{{ ops.smsBalance.error }}</span>
        <span v-else class="hint">尚未查询</span>
      </div>
      <div v-if="ops.smsBalance" class="balance-meta">
        <span v-if="ops.smsBalance.points">积分 {{ ops.smsBalance.points }}</span>
        <span>查询时间 {{ balanceTime }}</span>
      </div>
    </div>
  </section>

  <SmsCountryPicker
    :open="picker.open"
    :items="picker.items"
    :selected="picker.selected"
    :providers="picker.providers"
    :loading="picker.loading"
    :error="picker.error"
    @close="picker.open = false"
    @toggle="toggleCountry"
    @set-provider="setCountryProvider"
  />
</template>

<style scoped>
.card {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.card-head .section-title {
  margin-bottom: 0;
}

.card-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 0.76rem;
  color: var(--text-secondary);
}

.field .input {
  width: 100%;
}

.span-2 {
  grid-column: span 2;
}

.country-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.country-row .input {
  flex: 1;
}

.btn-inner {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}

.check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  cursor: pointer;
}

.check input {
  accent-color: var(--accent);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chip {
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 0.72rem;
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  color: var(--text-secondary);
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.hint {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.field small { color: var(--text-muted); line-height: 1.5; }
.draft-hint { margin: 0; color: var(--warning); font-size: 0.8rem; }
.balance-panel { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; padding: 14px 16px; border: 1px solid var(--glass-border); border-radius: var(--radius-sm); background: var(--glass-bg); }
.balance-panel > div { display: flex; flex-direction: column; gap: 6px; }
.balance-label { color: var(--text-secondary); font-size: 0.78rem; }
.balance-panel strong { font-size: 1.5rem; font-variant-numeric: tabular-nums; }
.balance-panel strong small, .balance-meta { font-size: 0.75rem; font-weight: 400; color: var(--text-muted); }
.balance-error { color: var(--error); font-size: 0.8rem; }

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .span-2 {
    grid-column: span 1;
  }
}
</style>
