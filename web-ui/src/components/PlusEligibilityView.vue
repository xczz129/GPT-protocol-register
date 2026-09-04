<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  form: { type: Object, required: true },
  state: { type: Object, required: true },
});
const emit = defineEmits(["check", "clear", "batch-check", "batch-stop"]);
const showToken = ref(false);
const mode = ref("single");

const isNerver = computed(() => props.form.channel === "nerver");
const resultTitle = computed(() => {
  const result = props.state.result;
  if (!result) return "";
  if (result.is_deactivated) return "账号已封号";
  if (result.status === "at_expired" || result.jwt_expired) return "AT 已过期";
  if (result.has_active_subscription) return "Plus 已生效";
  if (result.eligible) return "可领 Plus 免费试用";
  return "无免费试用";
});
const resultTone = computed(() => {
  const result = props.state.result;
  if (!result) return "neutral";
  if (result.is_deactivated || result.status === "at_expired" || result.jwt_expired) return "danger";
  if (result.eligible || result.has_active_subscription) return "eligible";
  return "neutral";
});
const channelText = computed(() => {
  const result = props.state.result;
  if (!result) return "-";
  if (result.provider === "nerver_check" || result.proxy_mode === "nerver") return "nerver";
  return `v4 · ${result.proxy_country || result.proxy_mode || "direct"}`;
});

const batch = computed(() => props.state.batch || { running: false, total: 0, done: 0, results: [], stopped: false });
const parsedCount = computed(() => {
  const seen = new Set();
  for (const raw of String(props.form.batchTokens || "").split(/[\s,;]+/)) {
    let token = raw.trim();
    if (!token) continue;
    if (token.toLowerCase().startsWith("bearer ")) token = token.slice(7).trim();
    if (token.length >= 20 && !seen.has(token)) seen.add(token);
  }
  return seen.size;
});
const batchProgress = computed(() => (batch.value.total ? Math.round((batch.value.done / batch.value.total) * 100) : 0));
const batchSummary = computed(() => {
  const rows = batch.value.results || [];
  return {
    done: rows.length,
    eligible: rows.filter((row) => row.ok && row.eligible).length,
    plus: rows.filter((row) => row.ok && row.plusActive).length,
    banned: rows.filter((row) => row.ok && row.banned).length,
    failed: rows.filter((row) => !row.ok).length,
  };
});

function statusText(value) {
  return ({ plus_eligible: "符合试用资格", plus_active: "Plus 生效中", free: "无免费试用", at_expired: "AT 已过期", banned: "已封号" })[String(value || "")] || value || "-";
}
function check() {
  if (!String(props.form.accessToken || "").trim() || props.state.loading) return;
  emit("check");
}
function batchCheck() {
  if (!parsedCount.value || batch.value.running) return;
  emit("batch-check");
}
</script>

<template>
  <section class="card plus-eligibility-page">
    <div class="card-h">
      <div><h2>Plus 免费试用</h2><p class="subtle">检查账号是否符合 Plus 1 个月免费试用资格；支持 v4（ChatGPT 直查）与 nerver 双渠道，支持批量。</p></div>
      <button class="btn secondary" type="button" :disabled="state.loading || batch.running || (!form.accessToken && !form.batchTokens && !state.result && !batch.results.length)" @click="emit('clear')">清空</button>
    </div>

    <div class="plus-toolbar">
      <div class="plus-mode" role="group" aria-label="检测模式">
        <span class="exit-label">检测模式</span>
        <div class="exit-options">
          <button type="button" :class="['exit-option', { active: mode === 'single' }]" :disabled="state.loading || batch.running" @click="mode = 'single'">单个检测</button>
          <button type="button" :class="['exit-option', { active: mode === 'batch' }]" :disabled="state.loading || batch.running" @click="mode = 'batch'">批量检测</button>
        </div>
      </div>
      <div class="plus-mode" role="group" aria-label="检测渠道">
        <span class="exit-label">检测渠道</span>
        <div class="exit-options">
          <button type="button" :class="['exit-option', { active: !isNerver }]" :disabled="state.loading || batch.running" @click="form.channel = 'v4'">v4 接口</button>
          <button type="button" :class="['exit-option', { active: isNerver }]" :disabled="state.loading || batch.running" @click="form.channel = 'nerver'">nerver 接口</button>
        </div>
      </div>
      <div v-if="!isNerver" class="plus-mode" role="group" aria-label="检测网络模式">
        <span class="exit-label">检测网络</span>
        <div class="exit-options">
          <button type="button" :class="['exit-option', { active: !form.useConfiguredProxy }]" :disabled="state.loading || batch.running" @click="form.useConfiguredProxy = false">本机直连</button>
          <button type="button" :class="['exit-option', { active: form.useConfiguredProxy }]" :disabled="state.loading || batch.running" @click="form.useConfiguredProxy = true">资格代理</button>
        </div>
      </div>
    </div>

    <template v-if="mode === 'single'">
      <div class="plus-token-tool">
        <label class="plus-token-field">
          <span>Access Token</span>
          <div class="plus-token-input">
            <input v-model="form.accessToken" :type="showToken ? 'text' : 'password'" autocomplete="off" spellcheck="false" placeholder="Bearer eyJ... 或 eyJ..." :disabled="state.loading" @keyup.enter="check" />
            <button class="btn secondary" type="button" :disabled="state.loading" @click="showToken = !showToken">{{ showToken ? "隐藏" : "显示" }}</button>
          </div>
        </label>
        <button class="btn primary plus-check-button" type="button" :disabled="state.loading || !String(form.accessToken || '').trim()" @click="check">{{ state.loading ? "检测中…" : "开始检测" }}</button>
      </div>

      <div v-if="state.loading" class="plus-check-loading"><span></span><strong>正在读取账号免费试用资格</strong></div>
      <div v-if="state.error" class="plus-check-error">{{ state.error }}</div>

      <div v-if="state.result" :class="['plus-check-result', resultTone]">
        <div class="plus-result-head">
          <div><span>检测结果</span><strong>{{ resultTitle }}</strong></div>
          <span>{{ state.result.checked_at ? new Date(state.result.checked_at).toLocaleString('zh-CN', { hour12: false }) : '-' }}</span>
        </div>
        <div class="plus-result-metrics">
          <div><span>账号状态</span><strong>{{ statusText(state.result.status) }}</strong></div>
          <div><span>当前套餐</span><strong>{{ String(state.result.plan_type || 'free').toUpperCase() }}</strong></div>
          <div><span>免费试用</span><strong>{{ state.result.eligible ? "有资格" : "无资格" }}</strong></div>
          <div><span>检测渠道</span><strong>{{ channelText }}</strong></div>
        </div>

        <div class="plus-result-detail">
          <span>判定依据</span><code>{{ state.result.reason || '-' }}</code>
          <span>请求耗时</span><code>{{ state.result.elapsed_seconds ?? '-' }}s</code>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="plus-batch">
        <div class="plus-batch-head">
          <label class="plus-token-field plus-batch-field">
            <span>Access Token 列表</span>
            <textarea v-model="form.batchTokens" class="plus-batch-input" rows="6" spellcheck="false" placeholder="每行一个 Access Token（支持 Bearer 前缀、逗号或空格分隔），自动去重，单次最多 200 个" :disabled="batch.running"></textarea>
          </label>
          <div class="plus-batch-side">
            <div class="plus-batch-count"><span>已识别</span><b>{{ parsedCount }}</b><span>个 Token</span></div>
            <label class="plus-workers-field"><span>并发</span><input v-model.number="form.workers" type="number" min="1" max="20" :disabled="batch.running" /></label>
            <div class="plus-batch-actions">
              <button class="btn primary" type="button" :disabled="batch.running || !parsedCount" @click="batchCheck">{{ batch.running ? "检测中…" : "开始批量检测" }}</button>
              <button v-if="batch.running" class="btn danger" type="button" @click="emit('batch-stop')">停止</button>
            </div>
          </div>
        </div>

        <div v-if="batch.running || batch.done" class="plus-batch-progress">
          <div class="plus-batch-progress-bar"><span :style="{ width: batchProgress + '%' }"></span></div>
          <span class="mono">{{ batch.done }} / {{ batch.total }}{{ batch.running ? " · 检测中…" : batch.stopped ? " · 已停止" : " · 完成" }}</span>
        </div>

        <div v-if="batch.results.length" class="plus-batch-summary">
          <span class="pill">共 {{ batchSummary.done }}</span>
          <span class="pill success">符合试用 {{ batchSummary.eligible }}</span>
          <span class="pill run">Plus 生效 {{ batchSummary.plus }}</span>
          <span class="pill failed">已封号 {{ batchSummary.banned }}</span>
          <span v-if="batchSummary.failed" class="pill failed">失败 {{ batchSummary.failed }}</span>
        </div>

        <div v-if="batch.results.length" class="plus-batch-table">
          <table>
            <thead>
              <tr><th>#</th><th>Token</th><th>状态</th><th>套餐</th><th>免费试用</th><th>耗时</th><th>说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in batch.results" :key="index" :class="{ 'row-err': !row.ok }">
                <td class="mono">{{ index + 1 }}</td>
                <td class="mono">…{{ row.tail }}</td>
                <td>
                  <span v-if="!row.ok" class="pill failed">失败</span>
                  <span v-else-if="row.banned" class="pill failed">已封号</span>
                  <span v-else-if="row.status === 'at_expired'" class="pill failed">AT 已过期</span>
                  <span v-else-if="row.plusActive" class="pill run">Plus 生效</span>
                  <span v-else-if="row.eligible" class="pill success">可领试用</span>
                  <span v-else class="pill">无资格</span>
                </td>
                <td class="mono">{{ row.ok && row.plan ? String(row.plan).toUpperCase() : "-" }}</td>
                <td>{{ row.ok ? (row.eligible ? "有" : "无") : "-" }}</td>
                <td class="mono">{{ row.elapsed != null ? row.elapsed + "s" : "-" }}</td>
                <td class="plus-batch-note" :title="row.error || row.reason || ''">{{ row.error || row.reason || "-" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.plus-workers-field {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.plus-workers-field span {
  color: var(--text-dim, #8b8b8b);
}
.plus-workers-field input {
  width: 76px;
}
</style>
