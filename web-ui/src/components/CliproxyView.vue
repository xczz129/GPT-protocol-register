<script setup>
import { computed, ref } from "vue";
import { apiUrl } from "../api.js";

const props = defineProps({
  accounts: { type: Object, default: () => ({ configured: false, auths_dir: "", items: [] }) },
  loading: Boolean,
  refreshing: Boolean,
});
const emit = defineEmits(["refresh", "refresh-quota"]);

const selected = ref([]);
const items = computed(() => props.accounts?.items || []);
const selectedFiles = computed(() => items.value.filter((item) => selected.value.includes(item.file)));

// 账号健康状态:由面板直连 OpenAI usage 端点的结果分类
const STATUS_META = {
  ok: { label: "正常", cls: "ok" },
  limit_reached: { label: "额度用尽", cls: "warn" },
  blocked: { label: "已被限制", cls: "warn" },
  rate_limited: { label: "限流 · 429", cls: "warn" },
  unauthorized: { label: "已失效 · 401", cls: "failed" },
  forbidden: { label: "被拒 · 403", cls: "failed" },
  refresh_failed: { label: "凭据失效", cls: "failed" },
  error: { label: "查询失败", cls: "failed" },
};

function itemStatus(item) {
  return String(item?.status || item?.quota?.status || "");
}
function statusMeta(item) {
  return STATUS_META[itemStatus(item)] || null;
}

function toggleAll(event) {
  selected.value = event.target.checked ? items.value.map((item) => item.file) : [];
}
function downloadUrl(files) {
  return apiUrl(`/api/cliproxy/download?files=${encodeURIComponent(files.join(","))}`);
}
function formatTime(value) {
  if (!value) return "-";
  try {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return "-";
  }
}
function formatResetAt(value) {
  const ts = Number(value);
  if (!ts) return "-";
  try { return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }); } catch { return "-"; }
}
function remainingPercent(window_) {
  const used = Number(window_?.used_percent ?? 0);
  return Math.max(0, Math.min(100, 100 - used));
}
function quotaLevel(window_) {
  const used = Number(window_?.used_percent ?? 0);
  if (used >= 95) return "critical";
  if (used >= 80) return "warn";
  return "ok";
}
const statusCounts = computed(() => {
  const counts = { ok: 0, invalid: 0, limited: 0, other: 0, unchecked: 0 };
  for (const item of items.value) {
    const status = itemStatus(item);
    if (!status) counts.unchecked += 1;
    else if (status === "ok") counts.ok += 1;
    else if (status === "limit_reached" || status === "rate_limited" || status === "blocked") counts.limited += 1;
    else if (status === "unauthorized" || status === "forbidden" || status === "refresh_failed") counts.invalid += 1;
    else counts.other += 1;
  }
  return counts;
});
const resetCreditsTotal = computed(() => items.value.reduce((sum, item) => sum + (Number(item.quota?.reset_credits?.available ?? 0) || 0), 0));
const lastQuotaFetched = computed(() => {
  let latest = "";
  for (const item of items.value) {
    const stamp = item.quota?.fetched_at || "";
    if (stamp > latest) latest = stamp;
  }
  return latest;
});
</script>

<template>
  <div class="ops-grid">
    <section class="card span-all">
      <div class="card-h">
        <div>
          <h2>中转账号</h2>
          <p class="subtle">接码成功的账号自动同步;额度与账号状态(封号/401/429)由面板直连 OpenAI 查询,不经 cliproxy 接口。</p>
        </div>
        <div class="row-btns">
          <button class="btn secondary" :disabled="loading || refreshing || !items.length" @click="emit('refresh-quota', [])">{{ refreshing ? "刷新中…" : "刷新额度" }}</button>
          <button class="btn secondary" :disabled="loading" @click="emit('refresh')">重新加载</button>
        </div>
      </div>
      <div class="cliproxy-stats">
        <div class="cliproxy-stat"><strong>{{ items.length }}</strong><span>账号总数</span></div>
        <div class="cliproxy-stat"><strong>{{ statusCounts.ok }}</strong><span>正常</span></div>
        <div class="cliproxy-stat" :class="{ 'is-alert': statusCounts.invalid > 0 }"><strong>{{ statusCounts.invalid }}</strong><span>封号/失效</span></div>
        <div class="cliproxy-stat"><strong>{{ statusCounts.limited }}</strong><span>限流/额度尽</span></div>
        <div class="cliproxy-stat"><strong>{{ resetCreditsTotal }}</strong><span>重置卡</span></div>
      </div>
      <p class="hint">目录 {{ accounts.auths_dir || "-" }} · 额度刷新:{{ formatTime(lastQuotaFetched) }}<template v-if="!accounts.configured"> · 未检测到 cliproxy auths 目录</template></p>
      <div v-if="loading && !items.length" class="empty-card">加载中…</div>
      <div v-else-if="!items.length" class="empty-card">暂无中转账号;接码成功后会自动同步到这里。</div>
    </section>

    <section v-if="items.length" class="card span-all">
      <div class="card-h">
        <div><h2>账号明细</h2><p class="subtle">共 {{ items.length }} 个 · 已选 {{ selected.length }} 个</p></div>
        <div class="row-btns">
          <a v-if="selected.length" class="btn secondary sm" :href="downloadUrl(selectedFiles.map((item) => item.file))">下载选中 ({{ selected.length }})</a>
          <a class="btn secondary sm" :href="downloadUrl(items.map((item) => item.file))">下载全部</a>
        </div>
      </div>
      <div class="table-wrap cliproxy-wrap">
        <table class="cliproxy-table">
          <thead>
            <tr>
              <th class="col-check"><input type="checkbox" :checked="selected.length === items.length && items.length > 0" @change="toggleAll" /></th>
              <th>账号</th>
              <th>套餐</th>
              <th>状态</th>
              <th>周额度</th>
              <th>5 小时窗口</th>
              <th>重置时间</th>
              <th>重置卡</th>
              <th>AT 过期</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.file">
              <td class="col-check"><input v-model="selected" type="checkbox" :value="item.file" /></td>
              <td>
                <div class="cliproxy-account">
                  <b class="cliproxy-email" :title="item.email || item.file">{{ item.email || "(未知邮箱)" }}</b>
                  <small class="cliproxy-sub" :title="item.file">{{ item.file }}</small>
                </div>
              </td>
              <td><span class="pill">{{ item.plan_type || "unknown" }}</span></td>
              <td>
                <span v-if="statusMeta(item)" :class="['pill', statusMeta(item).cls]" :title="item.quota_error || ''">{{ statusMeta(item).label }}</span>
                <span v-else class="subtle">未检测</span>
                <div v-if="item.quota_error && itemStatus(item) !== 'ok'" class="cliproxy-sub danger-text" :title="item.quota_error">{{ item.quota_error.slice(0, 44) }}</div>
              </td>
              <td>
                <template v-if="item.quota">
                  <div class="cliproxy-quota">
                    <div class="cliproxy-track"><i :class="quotaLevel(item.quota.primary)" :style="{ width: `${Number(item.quota.primary?.used_percent ?? 0)}%` }"></i></div>
                    <span class="cliproxy-quota-text">{{ item.quota.primary ? `剩 ${remainingPercent(item.quota.primary)}%` : "-" }}</span>
                  </div>
                </template>
                <span v-else class="subtle">-</span>
              </td>
              <td>
                <template v-if="item.quota?.secondary">
                  <div class="cliproxy-quota">
                    <div class="cliproxy-track"><i :class="quotaLevel(item.quota.secondary)" :style="{ width: `${Number(item.quota.secondary.used_percent ?? 0)}%` }"></i></div>
                    <span class="cliproxy-quota-text">剩 {{ remainingPercent(item.quota.secondary) }}%</span>
                  </div>
                </template>
                <span v-else class="subtle">-</span>
              </td>
              <td><small class="cliproxy-sub">{{ item.quota?.primary?.reset_at ? formatResetAt(item.quota.primary.reset_at) : "-" }}</small></td>
              <td><small class="cliproxy-sub">{{ item.quota?.reset_credits?.available ?? "-" }}</small></td>
              <td><small class="cliproxy-sub">{{ item.expired ? formatTime(item.expired) : "-" }}</small></td>
              <td class="col-actions">
                <div class="row-btns">
                  <button class="btn secondary sm" :disabled="refreshing" @click="emit('refresh-quota', [item])">刷新</button>
                  <a class="btn secondary sm" :href="downloadUrl([item.file])">下载</a>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.cliproxy-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 10px 0 6px; }
.cliproxy-stat { display: flex; flex-direction: column; gap: 2px; padding: 10px 14px; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--bg-2); }
.cliproxy-stat strong { font-size: 20px; font-weight: 700; color: var(--ink-800); font-family: var(--mono); }
.cliproxy-stat span { font-size: 10.5px; color: var(--ink-400); text-transform: uppercase; letter-spacing: 0.06em; }
.cliproxy-stat.is-alert strong { color: var(--err); }
.cliproxy-wrap { max-height: 62vh; overflow: auto; }
.cliproxy-table { min-width: 1080px; }
.cliproxy-table .col-check { width: 34px; }
.cliproxy-table .col-actions { text-align: right; white-space: nowrap; }
.cliproxy-account { display: flex; flex-direction: column; gap: 3px; min-width: 200px; }
.cliproxy-email { font-size: 12px; color: var(--ink-800); font-weight: 600; word-break: break-all; }
.cliproxy-sub { font-size: 10px; color: var(--ink-400); font-family: var(--mono); max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cliproxy-quota { display: flex; align-items: center; gap: 8px; min-width: 150px; }
.cliproxy-track { flex: 1; height: 5px; border-radius: 4px; background: var(--bg-inset); overflow: hidden; min-width: 70px; }
.cliproxy-track i { display: block; height: 100%; border-radius: 4px; background: var(--ok); transition: width 0.3s; }
.cliproxy-track i.warn { background: var(--warn); }
.cliproxy-track i.critical { background: var(--err); }
.cliproxy-quota-text { font-size: 10.5px; color: var(--ink-500); font-family: var(--mono); white-space: nowrap; }
</style>
