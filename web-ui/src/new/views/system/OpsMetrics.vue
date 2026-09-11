<script setup lang="ts">
/* --- 运维中心 · 运行统计：成功率环、邮箱/账号资源条、失败原因排行、接码成功率 --- */
import { computed, ref } from "vue";
import { RefreshCw } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useOperationsStore } from "../../stores/operations";
import { useUiStore } from "../../stores/ui";

const ops = useOperationsStore();
const ui = useUiStore();

const successRate = computed(() => Math.max(0, Math.min(100, Number(ops.data?.metrics?.success_rate || 0))));
const mailRate = computed(() =>
  ops.data?.mail?.total ? Math.round((Number(ops.data.mail.available || 0) * 100) / Number(ops.data.mail.total)) : 0,
);
const healthyAccounts = computed(
  () => Number(ops.data?.accounts?.alive || 0) + Number(ops.data?.accounts?.limited || 0),
);
const accountScope = computed(() =>
  Math.max(0, Number(ops.data?.accounts?.total || 0) - Number(ops.data?.accounts?.deactivated || 0)),
);
const accountRate = computed(() => (accountScope.value ? Math.round((healthyAccounts.value * 100) / accountScope.value) : 0));
const failureMax = computed(() =>
  Math.max(1, ...(ops.data?.metrics?.failure_reasons || []).map((item) => Number(item.count || 0))),
);

/* --- 接码成功率（按国家 / 按档位切换） --- */
const smsStatsView = ref<"country" | "tier">("country");
const smsStatsSummary = computed(() => ops.smsStats?.summary || { total: 0, success: 0, failed: 0, rate: 0 });
const smsStatsRows = computed(() => {
  const source = smsStatsView.value === "tier" ? ops.smsStats?.tiers : ops.smsStats?.countries;
  return (source || []).map((row) => ({
    name: row.name,
    total: Number(row.total || 0),
    ok: Number(row.ok || 0),
    fail: Number(row.fail || 0),
    rate: Number(row.rate || 0),
    cost: Number(row.cost || 0),
    price: Number(row.price || 0),
    cancelled: Number(row.cancelled || 0),
    unknown: Number(row.unknown || 0),
    unpriced: Number(row.unpriced || 0),
    currency: row.currency || "渠道报价单位",
  }));
});
const smsStatsMaxTotal = computed(() => Math.max(1, ...smsStatsRows.value.map((row) => row.total)));

function smsRateLevel(rate: number): string {
  if (rate >= 20) return "good";
  if (rate >= 8) return "mid";
  return "low";
}

async function refresh() {
  try {
    await Promise.all([ops.refresh(), ops.loadSmsStats(true)]);
  } catch {
    ui.notify("运维统计刷新失败", "error");
  }
}
</script>

<template>
  <section class="metrics-panel glass-panel">
    <div class="panel-head">
      <h2 class="section-title">运行统计</h2>
      <AppButton size="sm" :loading="ops.busy['load']" @click="refresh">
        <span class="btn-inner"><RefreshCw :size="13" />刷新</span>
      </AppButton>
    </div>

    <div v-if="!ops.data" class="panel-empty">{{ ops.busy["load"] ? "加载中…" : "暂无数据" }}</div>

    <div v-else class="dashboard-grid">
      <div class="dashboard-score">
        <div
          class="score-ring"
          :style="{ background: `conic-gradient(var(--accent) ${successRate * 3.6}deg, var(--progress-track) 0deg)` }"
        >
          <div class="score-ring-inner">
            <strong>{{ successRate }}%</strong>
            <span>成功率</span>
          </div>
        </div>
        <div class="score-caption">
          <b>{{ ops.data.metrics?.success || 0 }} 成功</b>
          <span>{{ ops.data.metrics?.failed || 0 }} 失败 · 平均 {{ ops.data.metrics?.average_seconds || 0 }}s</span>
        </div>
      </div>

      <div class="dashboard-resources">
        <div class="resource-progress">
          <div class="resource-head">
            <b>邮箱可用率</b>
            <span>{{ ops.data.mail?.available || 0 }} / {{ ops.data.mail?.total || 0 }}</span>
          </div>
          <div class="resource-track"><i :style="{ width: `${mailRate}%` }"></i></div>
          <small>已使用 {{ ops.data.mail?.used || 0 }} · 冷却 {{ ops.data.mail?.cooldown || 0 }} · 失败 {{ ops.data.mail?.failed || 0 }}</small>
        </div>
        <div class="resource-progress">
          <div class="resource-head">
            <b>账号健康率</b>
            <span>{{ healthyAccounts }} / {{ accountScope }}</span>
          </div>
          <div class="resource-track account-track"><i :style="{ width: `${accountRate}%` }"></i></div>
          <small>
            正常 {{ ops.data.accounts?.alive || 0 }} · 限流 {{ ops.data.accounts?.limited || 0 }} · 待续期 {{ ops.data.accounts?.reauth_required || 0 }}
            · 其它失效 {{ ops.data.accounts?.invalid_other || 0 }} · 错误 {{ ops.data.accounts?.error || 0 }}
            · 已封号留档 {{ ops.data.accounts?.deactivated || 0 }} · 未检测 {{ ops.data.accounts?.unknown || 0 }}
          </small>
        </div>
        <div class="dashboard-kpis">
          <div><strong>{{ ops.data.mail?.available ?? 0 }}</strong><span>可用邮箱</span></div>
          <div><strong>{{ ops.data.accounts?.invalid_other ?? 0 }}</strong><span>其它失效账号</span></div>
          <div><strong>{{ ops.data.trash_count ?? 0 }}</strong><span>回收站</span></div>
        </div>
      </div>

      <div class="rank-card">
        <div class="rank-head">
          <b>失败原因排行</b>
          <span class="rank-total">{{ ops.data.metrics?.failed || 0 }} 次失败</span>
        </div>
        <div v-if="!ops.data.metrics?.failure_reasons?.length" class="panel-empty">暂无累计失败明细</div>
        <ol v-else class="rank-list">
          <li v-for="(item, rank) in ops.data.metrics.failure_reasons" :key="item.reason" class="rank-item">
            <span class="rank-no">{{ rank + 1 }}</span>
            <span class="rank-reason" :title="item.reason">{{ item.reason }}</span>
            <b class="rank-count">{{ item.count }}</b>
            <div class="rank-track"><i :style="{ width: `${Math.max(4, (item.count * 100) / failureMax)}%` }"></i></div>
          </li>
        </ol>
      </div>

      <div class="rank-card">
        <div class="rank-head">
          <b>短信验证统计</b>
          <span class="rank-total">{{ smsStatsSummary.success }} 通过 / {{ smsStatsSummary.total }} 次取号尝试</span>
        </div>
        <p class="sms-estimate-note">按号码尝试记录统计。报价合计用于比较渠道，实际扣费及退款以平台账单为准。</p>
        <div class="sms-toolbar">
          <div class="seg">
            <button type="button" class="seg-btn" :class="{ active: smsStatsView === 'country' }" @click="smsStatsView = 'country'">按国家</button>
            <button type="button" class="seg-btn" :class="{ active: smsStatsView === 'tier' }" @click="smsStatsView = 'tier'">按档位</button>
          </div>
          <strong :class="['sms-overall', `rate-${smsRateLevel(smsStatsSummary.rate)}`]">{{ smsStatsSummary.rate }}%</strong>
          <AppButton size="sm" :loading="ops.busy['sms-stats']" @click="ops.loadSmsStats(true)">刷新</AppButton>
        </div>
        <div v-if="!smsStatsRows.length" class="panel-empty">{{ ops.busy["sms-stats"] ? "统计中…" : "暂无接码记录" }}</div>
        <div v-else class="sms-list">
          <div v-for="(row, index) in smsStatsRows" :key="row.name" class="sms-row">
            <span class="rank-no">{{ index + 1 }}</span>
            <div class="sms-main">
              <div class="sms-line">
                <b class="sms-name" :title="row.name">{{ row.name }}</b>
                <span class="sms-nums">{{ row.ok }}/{{ row.total }}</span>
                <strong :class="`rate-${smsRateLevel(row.rate)}`">{{ row.rate }}%</strong>
              </div>
              <div class="rank-track"><i :style="{ width: `${Math.max(2, (row.total * 100) / smsStatsMaxTotal)}%` }"></i></div>
              <small class="sms-meta">失败 {{ row.fail }} · 取消 {{ row.cancelled }} · 未知 {{ row.unknown }} · <template v-if="row.unpriced >= row.total">暂无报价</template><template v-else>已知报价合计 {{ row.cost.toFixed(3) }} {{ row.currency }}</template><template v-if="row.unpriced"> · {{ row.unpriced }} 次未提供报价</template></small>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.sms-estimate-note { margin: 4px 0 10px; font-size: 0.75rem; line-height: 1.5; color: var(--text-muted); }
.metrics-panel {
  padding: 18px 20px;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 14px;
}

.panel-head .section-title {
  margin-bottom: 0;
}

.btn-inner {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.panel-empty {
  color: var(--text-muted);
  font-size: 0.8rem;
  padding: 18px 0;
  text-align: center;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: 220px 1fr 1fr 1fr;
  gap: 16px;
  align-items: start;
}

@media (max-width: 1400px) {
  .dashboard-grid {
    grid-template-columns: 220px 1fr 1fr;
  }
}

@media (max-width: 900px) {
  .dashboard-grid {
    grid-template-columns: 1fr;
  }
}

/* --- 成功率环 --- */
.dashboard-score {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.score-ring {
  width: 132px;
  height: 132px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.score-ring-inner {
  width: 102px;
  height: 102px;
  border-radius: 50%;
  background: var(--panel-bg, var(--glass-bg));
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.score-ring-inner strong {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
}

.score-ring-inner span {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.score-caption {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  font-size: 0.78rem;
}

.score-caption b {
  color: var(--success);
}

.score-caption span {
  color: var(--text-muted);
}

/* --- 资源条 --- */
.dashboard-resources {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.resource-progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.resource-head {
  display: flex;
  justify-content: space-between;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.resource-head span {
  font-variant-numeric: tabular-nums;
}

.resource-track {
  height: 8px;
  border-radius: 8px;
  background: var(--progress-track);
  overflow: hidden;
}

.resource-track i {
  display: block;
  height: 100%;
  border-radius: 8px;
  background: linear-gradient(90deg, var(--accent-light), var(--accent-2));
  transition: width 0.4s ease;
}

.resource-progress small {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.dashboard-kpis {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.dashboard-kpis > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
}

.dashboard-kpis strong {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.dashboard-kpis span {
  font-size: 0.7rem;
  color: var(--text-muted);
}

/* --- 排行卡片（失败原因 / 接码成功率） --- */
.rank-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  min-height: 100%;
}

.rank-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 0.82rem;
  color: var(--text-secondary);
}

.rank-total {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.rank-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rank-item {
  display: grid;
  grid-template-columns: 20px 1fr auto;
  grid-template-areas: "no reason count" "no track track";
  column-gap: 8px;
  row-gap: 4px;
  align-items: center;
}

.rank-no {
  grid-area: no;
  font-size: 0.72rem;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.rank-reason {
  grid-area: reason;
  font-size: 0.78rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rank-count {
  grid-area: count;
  font-size: 0.78rem;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.rank-track {
  grid-area: track;
  height: 5px;
  border-radius: 5px;
  background: var(--progress-track);
  overflow: hidden;
}

.rank-track i {
  display: block;
  height: 100%;
  border-radius: 5px;
  background: linear-gradient(90deg, var(--accent-light), var(--accent-2));
}

/* --- 接码统计 --- */
.sms-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
}

.seg {
  display: inline-flex;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  overflow: hidden;
}

.seg-btn {
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.72rem;
  padding: 5px 12px;
  cursor: pointer;
}

.seg-btn.active {
  background: var(--accent-bg);
  color: var(--accent);
  font-weight: 600;
}

.sms-overall {
  margin-left: auto;
  font-size: 0.95rem;
}

.rate-good { color: var(--success); }
.rate-mid { color: var(--warning); }
.rate-low { color: var(--error); }

.sms-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 260px;
  overflow-y: auto;
}

.sms-row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.sms-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.sms-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.sms-name {
  flex: 1;
  font-size: 0.78rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sms-nums {
  font-size: 0.72rem;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.sms-meta {
  font-size: 0.7rem;
  color: var(--text-muted);
}
</style>
