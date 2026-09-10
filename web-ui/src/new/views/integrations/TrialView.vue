<script setup lang="ts">
/* --- Plus 免费试用：Token 资格检测（单个/批量，v4/nerver 双渠道）+ 资格代理池 --- */
import { computed, onMounted, ref, watch } from "vue";
import { Eraser, Play, RefreshCw, Square } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useTrialStore } from "../../stores/trial";
import { useUiStore } from "../../stores/ui";

const trial = useTrialStore();
const ui = useUiStore();

const showToken = ref(false);
const mode = ref<"single" | "batch">("single");

const form = computed(() => trial.form);
const single = computed(() => trial.single);
const batch = computed(() => trial.batch);

const isNerver = computed(() => form.value.channel === "nerver");

const resultTitle = computed(() => {
  const result = single.value.result;
  if (!result) return "";
  if (result.is_deactivated) return "账号已封号";
  if (result.status === "at_expired" || result.jwt_expired) return "AT 已过期";
  if (result.has_active_subscription) return "Plus 已生效";
  if (result.eligible) return "可领 Plus 免费试用";
  return "无免费试用";
});

const resultTone = computed(() => {
  const result = single.value.result;
  if (!result) return "neutral";
  if (result.is_deactivated || result.status === "at_expired" || result.jwt_expired) return "danger";
  if (result.eligible || result.has_active_subscription) return "eligible";
  return "neutral";
});

const channelText = computed(() => {
  const result = single.value.result;
  if (!result) return "-";
  if (result.provider === "nerver_check" || result.proxy_mode === "nerver") return "nerver";
  return `v4 · ${result.proxy_country || result.proxy_mode || "direct"}`;
});

const parsedCount = computed(() => trial.parseBatchTokens().length);
const batchProgress = computed(() => (batch.value.total ? Math.round((batch.value.done / batch.value.total) * 100) : 0));
const batchSummary = computed(() => {
  const rows = batch.value.results;
  return {
    done: rows.length,
    eligible: rows.filter((row) => row.ok && row.eligible).length,
    plus: rows.filter((row) => row.ok && row.plusActive).length,
    banned: rows.filter((row) => row.ok && row.banned).length,
    failed: rows.filter((row) => !row.ok).length,
  };
});

function statusText(value?: string) {
  return ({ plus_eligible: "符合试用资格", plus_active: "Plus 生效中", free: "无免费试用", at_expired: "AT 已过期", banned: "已封号" } as Record<string, string>)[String(value || "")] || value || "-";
}

async function checkSingle() {
  try {
    await trial.checkSingle();
    const result = trial.single.result;
    ui.notify(
      result?.eligible ? "检测完成：可领 Plus 免费试用" : result?.label || "检测完成：无免费试用",
      result?.is_deactivated || result?.status === "at_expired" ? "error" : "success",
    );
  } catch { /* store 已记录 error，notify 统一在这里 */ ui.notify(trial.single.error || "Plus 资格检测失败", "error"); }
}

async function startBatch() {
  const count = await trial.startBatch();
  if (!count) {
    ui.notify("请先粘贴至少一个有效的 Access Token", "error");
    return;
  }
  if (count > 200) {
    ui.notify(`一次最多批量检测 200 个，当前 ${count} 个`, "error");
    return;
  }
  const eligibleCount = trial.batch.results.filter((item) => item.ok && item.eligible).length;
  const failedCount = trial.batch.results.filter((item) => !item.ok).length;
  ui.notify(
    trial.batch.stopped
      ? `批量检测已停止：完成 ${trial.batch.done}/${trial.batch.total}`
      : `批量检测完成：${eligibleCount} 个符合试用资格${failedCount ? `，${failedCount} 个失败` : ""}`,
    failedCount && !trial.batch.stopped ? "warning" : "success",
  );
}

/* --- 资格代理池 --- */
const active = ref("");
const code = ref("");
const label = ref("");
const pools = computed(() => trial.pool.countries);
const current = computed(() => pools.value.find((item) => item.country === active.value) || pools.value[0] || null);

watch(pools, (rows) => {
  if (!rows.some((item) => item.country === active.value)) active.value = rows[0]?.country || "";
}, { immediate: true, deep: true });

function poolCount(text: string) {
  return String(text || "").split(/\r?\n/).filter((line) => line.trim() && !line.trim().startsWith("#")).length;
}

function addCountry() {
  const country = code.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 2);
  if (country.length !== 2 || pools.value.some((item) => item.country === country)) return;
  trial.pool.countries.push({ country, label: label.value.trim() || country, text: "" });
  active.value = country;
  code.value = "";
  label.value = "";
}

function removeCountry() {
  if (!current.value) return;
  trial.pool.countries = pools.value.filter((item) => item.country !== current.value?.country);
  if (trial.pool.selected_country === current.value.country) trial.pool.selected_country = "";
}

async function savePool() {
  try {
    await trial.savePool();
    ui.notify("资格代理已保存", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "保存失败", "error");
  }
}

onMounted(() => {
  void trial.loadPool();
});
</script>

<template>
  <div class="trial-page">
    <!-- 资格检测 -->
    <section class="glass-panel panel">
      <div class="panel-head">
        <div>
          <h2>Plus 免费试用</h2>
          <p class="panel-sub">检查账号是否符合 Plus 1 个月免费试用资格；支持 v4（ChatGPT 直查）与 nerver 双渠道，支持批量。</p>
        </div>
        <AppButton variant="accent" size="sm" :disabled="single.loading || batch.running || (!form.accessToken && !form.batchTokens && !single.result && !batch.results.length)" @click="trial.clearAll()">
          <Eraser :size="14" /><span>清空</span>
        </AppButton>
      </div>

      <div class="mode-bar">
        <div class="mode-group">
          <span class="mode-label">检测模式</span>
          <div class="segmented">
            <button type="button" :class="{ active: mode === 'single' }" :disabled="single.loading || batch.running" @click="mode = 'single'">单个检测</button>
            <button type="button" :class="{ active: mode === 'batch' }" :disabled="single.loading || batch.running" @click="mode = 'batch'">批量检测</button>
          </div>
        </div>
        <div class="mode-group">
          <span class="mode-label">检测渠道</span>
          <div class="segmented">
            <button type="button" :class="{ active: !isNerver }" :disabled="single.loading || batch.running" @click="form.channel = 'v4'">v4 接口</button>
            <button type="button" :class="{ active: isNerver }" :disabled="single.loading || batch.running" @click="form.channel = 'nerver'">nerver 接口</button>
          </div>
        </div>
        <div v-if="!isNerver" class="mode-group">
          <span class="mode-label">检测网络</span>
          <div class="segmented">
            <button type="button" :class="{ active: !form.useConfiguredProxy }" :disabled="single.loading || batch.running" @click="form.useConfiguredProxy = false">本机直连</button>
            <button type="button" :class="{ active: form.useConfiguredProxy }" :disabled="single.loading || batch.running" @click="form.useConfiguredProxy = true">资格代理</button>
          </div>
        </div>
      </div>

      <!-- 单个检测 -->
      <template v-if="mode === 'single'">
        <div class="token-tool">
          <label class="field token-field">
            <span>Access Token</span>
            <div class="token-input">
              <input v-model="form.accessToken" class="input" :type="showToken ? 'text' : 'password'" autocomplete="off" spellcheck="false" placeholder="Bearer eyJ... 或 eyJ..." :disabled="single.loading" @keyup.enter="checkSingle" />
              <AppButton variant="accent" size="sm" :disabled="single.loading" @click="showToken = !showToken">{{ showToken ? "隐藏" : "显示" }}</AppButton>
            </div>
          </label>
          <AppButton variant="primary" :disabled="single.loading || !form.accessToken.trim()" :loading="single.loading" @click="checkSingle">
            {{ single.loading ? "检测中…" : "开始检测" }}
          </AppButton>
        </div>

        <div v-if="single.error" class="check-error">{{ single.error }}</div>

        <div v-if="single.result" :class="['check-result', resultTone]">
          <div class="result-head">
            <div><span>检测结果</span><strong>{{ resultTitle }}</strong></div>
            <span>{{ single.result.checked_at ? new Date(single.result.checked_at).toLocaleString("zh-CN", { hour12: false }) : "-" }}</span>
          </div>
          <div class="result-metrics">
            <div><span>账号状态</span><strong>{{ statusText(single.result.status) }}</strong></div>
            <div><span>当前套餐</span><strong>{{ String(single.result.plan_type || "free").toUpperCase() }}</strong></div>
            <div><span>免费试用</span><strong>{{ single.result.eligible ? "有资格" : "无资格" }}</strong></div>
            <div><span>检测渠道</span><strong>{{ channelText }}</strong></div>
          </div>
          <div class="result-detail">
            <span>判定依据</span><code>{{ single.result.reason || "-" }}</code>
            <span>请求耗时</span><code>{{ single.result.elapsed_seconds ?? "-" }}s</code>
          </div>
        </div>
      </template>

      <!-- 批量检测 -->
      <template v-else>
        <div class="batch-head">
          <label class="field batch-field">
            <span>Access Token 列表</span>
            <textarea v-model="form.batchTokens" class="input" rows="6" spellcheck="false" placeholder="每行一个 Access Token（支持 Bearer 前缀、逗号或空格分隔），自动去重，单次最多 200 个" :disabled="batch.running"></textarea>
          </label>
          <div class="batch-side">
            <div class="batch-count"><span>已识别</span><b>{{ parsedCount }}</b><span>个 Token</span></div>
            <label class="workers-field"><span>并发</span><input v-model.number="form.workers" class="input" type="number" min="1" max="20" :disabled="batch.running" /></label>
            <div class="batch-actions">
              <AppButton variant="primary" :disabled="batch.running || !parsedCount" @click="startBatch">
                <Play :size="14" /><span>{{ batch.running ? "检测中…" : "开始批量检测" }}</span>
              </AppButton>
              <AppButton v-if="batch.running" variant="danger" @click="trial.stopBatch()"><Square :size="12" /><span>停止</span></AppButton>
            </div>
          </div>
        </div>

        <div v-if="batch.running || batch.done" class="batch-progress">
          <div class="track"><i :style="{ width: `${batchProgress}%` }"></i></div>
          <span class="mono">{{ batch.done }} / {{ batch.total }}{{ batch.running ? " · 检测中…" : batch.stopped ? " · 已停止" : " · 完成" }}</span>
        </div>

        <div v-if="batch.results.length" class="batch-summary">
          <span class="pill">共 {{ batchSummary.done }}</span>
          <span class="pill success">符合试用 {{ batchSummary.eligible }}</span>
          <span class="pill run">Plus 生效 {{ batchSummary.plus }}</span>
          <span class="pill failed">已封号 {{ batchSummary.banned }}</span>
          <span v-if="batchSummary.failed" class="pill failed">失败 {{ batchSummary.failed }}</span>
        </div>

        <div v-if="batch.results.length" class="table-wrap">
          <table>
            <thead><tr><th>#</th><th>Token</th><th>状态</th><th>套餐</th><th>免费试用</th><th>耗时</th><th>说明</th></tr></thead>
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
                <td class="mono">{{ row.elapsed != null ? `${row.elapsed}s` : "-" }}</td>
                <td class="note" :title="row.error || row.reason || ''">{{ row.error || row.reason || "-" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </section>

    <!-- 资格代理池 -->
    <section class="glass-panel panel">
      <div class="panel-head">
        <div>
          <h2>资格代理</h2>
          <p class="panel-sub">仅用于 0 元 Plus 资格检测；注册任务继续使用"代理设置"。未选国家或该国家没有代理时，资格检测走本机直连。</p>
        </div>
        <div class="head-actions">
          <AppButton variant="ghost" size="sm" :disabled="trial.poolLoading || trial.poolSaving" @click="trial.loadPool()"><RefreshCw :size="14" /><span>刷新</span></AppButton>
          <AppButton variant="primary" size="sm" :loading="trial.poolSaving" @click="savePool">保存资格代理</AppButton>
        </div>
      </div>

      <div class="exit-picker">
        <span class="mode-label">资格检测出口</span>
        <div class="exit-options">
          <button type="button" :class="['exit-option', { active: !trial.pool.selected_country }]" @click="trial.pool.selected_country = ''">本机直连</button>
          <button v-for="item in pools" :key="item.country" type="button" :class="['exit-option', { active: trial.pool.selected_country === item.country }]" @click="trial.pool.selected_country = item.country">
            <b>{{ item.country }}</b>{{ item.label || item.country }}
          </button>
        </div>
      </div>

      <div class="country-tabs">
        <button v-for="item in pools" :key="item.country" type="button" :class="['country-tab', { active: active === item.country }]" @click="active = item.country">
          <b>{{ item.country }}</b><span>{{ item.label || item.country }}</span><em>{{ poolCount(item.text) }}</em>
        </button>
      </div>

      <div class="country-add">
        <label class="add-field"><span>国家代码</span><input v-model="code" class="input" maxlength="2" placeholder="JP" @keyup.enter="addCountry" /></label>
        <label class="add-field"><span>显示名称</span><input v-model="label" class="input" maxlength="40" placeholder="日本" @keyup.enter="addCountry" /></label>
        <AppButton variant="ghost" size="sm" @click="addCountry">添加国家</AppButton>
      </div>

      <template v-if="current">
        <div class="editor-head">
          <div><strong>{{ current.country }} · {{ current.label || current.country }}</strong><span class="muted">可调度 {{ poolCount(current.text) }} 条</span></div>
          <label class="add-field"><span>显示名称</span><input v-model="current.label" class="input" maxlength="40" /></label>
          <AppButton variant="danger" size="sm" @click="removeCountry">删除国家</AppButton>
        </div>
        <textarea v-model="current.text" class="input pool-text" rows="12" spellcheck="false" placeholder="每行一条：HOST:PORT@USERNAME:PASSWORD"></textarea>
      </template>
      <div v-else class="empty-hint">添加国家后粘贴该国家的代理。</div>
    </section>
  </div>
</template>

<style scoped>
.trial-page { display: flex; flex-direction: column; gap: 14px; }
.panel { padding: 18px 20px; display: flex; flex-direction: column; gap: 14px; }

.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.panel-head h2 { font-size: 1.05rem; color: var(--text-primary); }
.panel-sub { font-size: 0.8rem; color: var(--text-muted); margin-top: 3px; }
.head-actions { display: flex; gap: 8px; }

.mode-bar { display: flex; gap: 20px; flex-wrap: wrap; }
.mode-group { display: flex; align-items: center; gap: 10px; }
.mode-label { font-size: 0.75rem; color: var(--text-muted); white-space: nowrap; }

.segmented {
  display: inline-flex;
  padding: 3px;
  border-radius: 10px;
  background: var(--inset-bg);
  border: 1px solid var(--line-subtle);
  gap: 2px;
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

.field { display: flex; flex-direction: column; gap: 6px; font-size: 0.8rem; color: var(--text-secondary); }

.token-tool { display: flex; align-items: flex-end; gap: 12px; }
.token-field { flex: 1; }
.token-input { display: flex; gap: 8px; }
.token-input input { flex: 1; font-family: var(--font-mono, monospace); }

.check-error { color: var(--error); font-size: 0.82rem; }

.check-result {
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.check-result.eligible { border-color: var(--success); background: var(--success-bg); }
.check-result.danger { border-color: var(--error); background: var(--error-bg); }

.result-head { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-muted); }
.result-head strong { display: block; font-size: 1rem; color: var(--text-primary); margin-top: 2px; }

.result-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
.result-metrics span { display: block; font-size: 0.72rem; color: var(--text-muted); }
.result-metrics strong { font-size: 0.88rem; color: var(--text-primary); }

.result-detail { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; font-size: 0.78rem; color: var(--text-muted); }
.result-detail code { color: var(--text-secondary); font-size: 0.76rem; overflow-wrap: anywhere; }

.batch-head { display: flex; gap: 16px; align-items: flex-start; }
.batch-field { flex: 1; }
.batch-field textarea { resize: vertical; font-family: var(--font-mono, monospace); font-size: 0.8rem; }
.batch-side { display: flex; flex-direction: column; gap: 10px; }
.batch-count { font-size: 0.8rem; color: var(--text-secondary); display: flex; gap: 6px; align-items: baseline; }
.batch-count b { font-size: 1.1rem; color: var(--accent); }
.workers-field { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; color: var(--text-secondary); }
.workers-field input { width: 72px; text-align: center; }
.batch-actions { display: flex; gap: 8px; }

.batch-progress { display: flex; align-items: center; gap: 12px; }
.track { flex: 1; height: 6px; border-radius: 4px; background: var(--line-subtle); overflow: hidden; }
.track i { display: block; height: 100%; border-radius: 4px; background: var(--accent); transition: width 0.3s; }
.mono { font-family: var(--font-mono, monospace); font-size: 0.76rem; color: var(--text-secondary); }

.batch-summary { display: flex; gap: 8px; flex-wrap: wrap; }
.pill {
  padding: 3px 11px;
  border-radius: 20px;
  font-size: 0.75rem;
  background: var(--inset-bg);
  color: var(--text-secondary);
  border: 1px solid var(--line-subtle);
}
.pill.success { background: var(--success-bg); color: var(--success); border-color: transparent; }
.pill.run { background: var(--info-bg); color: var(--info); border-color: transparent; }
.pill.failed { background: var(--error-bg); color: var(--error); border-color: transparent; }

.table-wrap { overflow: auto; max-height: 44vh; border: 1px solid var(--line-subtle); border-radius: var(--radius-sm); }
table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--line-subtle); }
th { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; background: var(--inset-bg); position: sticky; top: 0; }
.row-err td { background: var(--error-bg); }
.note { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }

/* --- 资格代理池 --- */
.exit-picker { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.exit-options { display: flex; gap: 6px; flex-wrap: wrap; }
.exit-option {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 10px;
  border: 1px solid var(--line-subtle);
  background: var(--inset-bg);
  color: var(--text-secondary);
  font-size: 0.8rem;
  cursor: pointer;
  transition: var(--transition-fast);
}
.exit-option.active { border-color: var(--accent); background: var(--accent-bg); color: var(--accent); }
.exit-option b { font-size: 0.82rem; }

.country-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.country-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 10px;
  border: 1px solid var(--line-subtle);
  background: none;
  color: var(--text-secondary);
  font-size: 0.78rem;
  cursor: pointer;
}
.country-tab.active { border-color: var(--accent); color: var(--accent); background: var(--accent-bg); }
.country-tab em { font-style: normal; font-size: 0.7rem; color: var(--text-muted); }

.country-add { display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; }
.add-field { display: flex; flex-direction: column; gap: 4px; font-size: 0.75rem; color: var(--text-secondary); }
.add-field input { width: 110px; }

.editor-head { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.editor-head > div { display: flex; flex-direction: column; gap: 2px; flex: 1; }
.editor-head strong { color: var(--text-primary); font-size: 0.88rem; }
.muted { color: var(--text-muted); font-size: 0.75rem; }

.pool-text { resize: vertical; font-family: var(--font-mono, monospace); font-size: 0.8rem; }

.empty-hint { padding: 28px 0; text-align: center; color: var(--text-muted); font-size: 0.82rem; }

@media (max-width: 768px) {
  .batch-head { flex-direction: column; }
  .batch-side { flex-direction: row; align-items: center; flex-wrap: wrap; }
}
</style>
