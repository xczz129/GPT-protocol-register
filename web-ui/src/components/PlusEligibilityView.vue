<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  form: { type: Object, required: true },
  state: { type: Object, required: true },
});
const emit = defineEmits(["check", "clear"]);
const showToken = ref(false);

const offers = computed(() => Object.entries(props.state.result?.eligible_promo_campaigns || {}).filter(([, value]) => value != null));
const resultTitle = computed(() => {
  const result = props.state.result;
  if (!result) return "";
  if (result.is_deactivated) return "账号已封号";
  if (result.has_active_subscription) return "Plus 已生效";
  if (result.eligible) return "可领 Plus 免费试用";
  if (result.has_promotion) return "检测到优惠活动";
  return "当前没有可用优惠";
});
const resultTone = computed(() => {
  const result = props.state.result;
  if (!result) return "neutral";
  if (result.is_deactivated) return "danger";
  if (result.eligible || result.has_active_subscription) return "eligible";
  return result.has_promotion ? "offer" : "neutral";
});

function statusText(value) {
  return ({ plus_eligible: "符合试用资格", plus_active: "Plus 生效中", free: "Free", banned: "已封号" })[String(value || "")] || value || "-";
}
function campaignId(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "-";
  return value.id || value.campaign_id || value.offer_id || value.code || "-";
}
function campaignDetail(value) {
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value || "-"); }
}
function check() {
  if (!String(props.form.accessToken || "").trim() || props.state.loading) return;
  emit("check");
}
</script>

<template>
  <section class="card plus-eligibility-page">
    <div class="card-h">
      <div><h2>Plus 试用资格检测</h2><p class="subtle">检查当前套餐、Plus 试用资格与账号可用优惠。</p></div>
      <button class="btn secondary" type="button" :disabled="state.loading || (!form.accessToken && !state.result)" @click="emit('clear')">清空</button>
    </div>

    <div class="plus-token-tool">
      <label class="plus-token-field">
        <span>Access Token</span>
        <div class="plus-token-input">
          <input v-model="form.accessToken" :type="showToken ? 'text' : 'password'" autocomplete="off" spellcheck="false" placeholder="Bearer eyJ... 或 eyJ..." :disabled="state.loading" @keyup.enter="check" />
          <button class="btn secondary" type="button" :disabled="state.loading" @click="showToken = !showToken">{{ showToken ? "隐藏" : "显示" }}</button>
        </div>
      </label>
      <div class="plus-mode" role="group" aria-label="检测网络模式">
        <button type="button" :class="{ active: !form.useConfiguredProxy }" :disabled="state.loading" @click="form.useConfiguredProxy = false">本机直连</button>
        <button type="button" :class="{ active: form.useConfiguredProxy }" :disabled="state.loading" @click="form.useConfiguredProxy = true">资格代理</button>
      </div>
      <button class="btn primary plus-check-button" type="button" :disabled="state.loading || !String(form.accessToken || '').trim()" @click="check">{{ state.loading ? "检测中…" : "开始检测" }}</button>
    </div>

    <div v-if="state.loading" class="plus-check-loading"><span></span><strong>正在读取账号资格与优惠活动</strong></div>
    <div v-if="state.error" class="plus-check-error">{{ state.error }}</div>

    <div v-if="state.result" :class="['plus-check-result', resultTone]">
      <div class="plus-result-head">
        <div><span>检测结果</span><strong>{{ resultTitle }}</strong></div>
        <span>{{ state.result.checked_at ? new Date(state.result.checked_at).toLocaleString('zh-CN', { hour12: false }) : '-' }}</span>
      </div>
      <div class="plus-result-metrics">
        <div><span>账号状态</span><strong>{{ statusText(state.result.status) }}</strong></div>
        <div><span>当前套餐</span><strong>{{ String(state.result.plan_type || 'free').toUpperCase() }}</strong></div>
        <div><span>Plus 试用</span><strong>{{ state.result.eligible ? "有资格" : "无资格" }}</strong></div>
        <div><span>优惠活动</span><strong>{{ state.result.has_promotion ? `有 · ${state.result.promotion_count || offers.length}` : "没有" }}</strong></div>
        <div><span>活动代码</span><strong>{{ state.result.promo_campaign_id || '-' }}</strong></div>
        <div><span>检测出口</span><strong>{{ state.result.proxy_country || state.result.proxy_mode || 'direct' }}</strong></div>
      </div>

      <div class="plus-result-detail">
        <span>判定依据</span><code>{{ state.result.reason || '-' }}</code>
        <span>请求耗时</span><code>{{ state.result.elapsed_seconds ?? '-' }}s</code>
      </div>

      <div class="plus-offers">
        <div class="plus-offers-head"><strong>优惠活动明细</strong><span>{{ offers.length }} 项</span></div>
        <div v-if="!offers.length" class="plus-offers-empty">当前账号未返回优惠活动。</div>
        <article v-for="([name, offer]) in offers" :key="name">
          <div><strong>{{ name }}</strong><code>{{ campaignId(offer) }}</code></div>
          <pre>{{ campaignDetail(offer) }}</pre>
        </article>
      </div>
    </div>
  </section>
</template>
