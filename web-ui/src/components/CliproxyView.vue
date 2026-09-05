<script setup>
import { computed, ref } from "vue";
import { apiUrl } from "../api.js";

const props = defineProps({
  accounts: { type: Object, default: () => ({ configured: false, auths_dir: "", items: [] }) },
  loading: Boolean,
  refreshing: Boolean,
  importing: Boolean,
  resettingFile: { type: String, default: "" },
});
const emit = defineEmits(["refresh", "refresh-quota", "import-cpa", "import-sub2", "export-cpa", "export-sub2", "consume-reset"]);

const selected = ref([]);
const cpaInput = ref(null);
const sub2Input = ref(null);
const exportDialog = ref({ open: false, files: [] });
const resetDialog = ref({ open: false, item: null });
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

function windowData(item, kind) {
  const quota = item?.quota || {};
  const key = kind === "weekly" ? "weekly" : "five_hour";
  if (Object.prototype.hasOwnProperty.call(quota, key)) return quota[key] || null;
  const windows = [quota.primary, quota.secondary].filter(Boolean);
  return windows.find((window_) => {
    const seconds = Number(window_.window_seconds || 0);
    return key === "weekly" ? seconds >= 6 * 86400 : seconds >= 4 * 3600 && seconds <= 6 * 3600;
  }) || null;
}
function usedPercent(window_) {
  const raw = window_?.used_percent;
  if (raw === null || raw === undefined || raw === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
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
  const used = usedPercent(window_);
  return used === null ? null : Math.round(100 - used);
}
function formatReset(window_) {
  const seconds = Number(window_?.reset_after_seconds);
  if (Number.isFinite(seconds) && seconds > 0) {
    if (seconds >= 86400) return `${Math.ceil(seconds / 86400)} 天后重置`;
    if (seconds >= 3600) return `${Math.ceil(seconds / 3600)} 小时后重置`;
    return `${Math.ceil(seconds / 60)} 分钟后重置`;
  }
  return window_?.reset_at ? formatResetAt(window_.reset_at) : "重置时间未知";
}
function windowLabel(window_, fallback) {
  return String(window_?.window_label || fallback);
}
function openSub2Picker() {
  sub2Input.value?.click();
}
function openCpaPicker() {
  cpaInput.value?.click();
}
async function handleImportFile(event, kind) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    emit(`import-${kind}`, { filename: file.name, content: "", error: "账号 JSON 不能超过 20MB" });
    return;
  }
  try {
    emit(`import-${kind}`, { filename: file.name, content: await file.text() });
  } catch {
    emit(`import-${kind}`, { filename: file.name, content: "", error: "读取文件失败" });
  }
}
function openExportDialog(kind = "cpa") {
  exportDialog.value = {
    open: true,
    kind,
    files: selected.value.length ? selectedFiles.value.map((item) => item.file) : [],
  };
}
function closeExportDialog() {
  exportDialog.value = { open: false, kind: "cpa", files: [] };
}
function chooseExport(scope) {
  const event = exportDialog.value.kind === "sub2" ? "export-sub2" : "export-cpa";
  emit(event, scope === "selected" ? exportDialog.value.files : []);
  closeExportDialog();
}
function openResetDialog(item) {
  resetDialog.value = { open: true, item };
}
function closeResetDialog() {
  resetDialog.value = { open: false, item: null };
}
function confirmReset() {
  const file = String(resetDialog.value.item?.file || "");
  if (!file) return;
  emit("consume-reset", { file });
  closeResetDialog();
}
function planClass(value) {
  const plan = String(value || "").trim().toLowerCase();
  if (plan.includes("team") || plan.includes("business")) return "team";
  if (plan.includes("pro")) return "pro";
  if (plan.includes("plus")) return "plus";
  return "free";
}
function quotaLevel(window_) {
  const used = usedPercent(window_);
  if (used === null) return "unknown";
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
          <button class="btn secondary" :disabled="loading || refreshing || importing || !items.length" @click="emit('refresh-quota', [])">{{ refreshing ? "刷新中…" : "刷新额度" }}</button>
          <button class="btn secondary" :disabled="loading || importing" @click="emit('refresh')">重新加载</button>
          <button class="btn secondary" :disabled="loading || refreshing || importing" @click="openCpaPicker">{{ importing ? "导入中…" : "导入 CPA JSON" }}</button>
          <button class="btn secondary" :disabled="loading || refreshing || importing" @click="openSub2Picker">导入 SUB2 JSON</button>
          <button class="btn primary" :disabled="loading || refreshing || importing || !items.length" @click="openExportDialog">导出 CPA JSON</button>
          <button class="btn secondary" :disabled="loading || refreshing || importing || !items.length" @click="openExportDialog('sub2')">导出 SUB2 JSON</button>
          <input ref="cpaInput" class="cliproxy-file-input" type="file" accept="application/json,.json" @change="(event) => handleImportFile(event, 'cpa')" />
          <input ref="sub2Input" class="cliproxy-file-input" type="file" accept="application/json,.json" @change="(event) => handleImportFile(event, 'sub2')" />
        </div>
      </div>
      <div class="cliproxy-stats">
        <div class="cliproxy-stat"><strong>{{ items.length }}</strong><span>账号总数</span></div>
        <div class="cliproxy-stat"><strong>{{ statusCounts.ok }}</strong><span>正常</span></div>
        <div class="cliproxy-stat" :class="{ 'is-alert': statusCounts.invalid > 0 }"><strong>{{ statusCounts.invalid }}</strong><span>封号/失效</span></div>
        <div class="cliproxy-stat"><strong>{{ statusCounts.limited }}</strong><span>限流/额度尽</span></div>
        <div class="cliproxy-stat"><strong>{{ resetCreditsTotal }}</strong><span>重置卡</span></div>
      </div>
      <p class="hint">额度刷新：{{ formatTime(lastQuotaFetched) }}<template v-if="!accounts.configured"> · 中转账号目录尚未配置</template></p>
      <div v-if="loading && !items.length" class="empty-card">加载中…</div>
      <div v-else-if="!items.length" class="empty-card">暂无中转账号;接码成功后会自动同步到这里。</div>
    </section>

    <section v-if="items.length" class="card span-all">
      <div class="card-h">
        <div><h2>账号明细</h2><p class="subtle">共 {{ items.length }} 个 · 已选 {{ selected.length }} 个</p></div>
        <p class="hint">单个账号可在操作列下载；批量导出请使用上方 CPA / SUB2 导出。</p>
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
              <td class="col-check"><input v-model="selected" type="checkbox" :value="item.file" :aria-label="`选择中转账号 ${item.email || item.file}`" /></td>
              <td>
                <div class="cliproxy-account">
                  <b class="cliproxy-email" :title="item.email || item.file">{{ item.email || "(未知邮箱)" }}</b>
                  <small class="cliproxy-sub" :title="item.file">{{ item.file }}</small>
                </div>
              </td>
              <td><span :class="['pill', 'cliproxy-plan', planClass(item.plan_type)]">{{ item.plan_type || "unknown" }}</span></td>
              <td>
                <span v-if="statusMeta(item)" :class="['pill', statusMeta(item).cls]" :title="item.quota_error || ''">{{ statusMeta(item).label }}</span>
                <span v-else class="subtle">未检测</span>
                <div v-if="item.quota_error && itemStatus(item) !== 'ok'" class="cliproxy-sub danger-text" :title="item.quota_error">{{ item.quota_error.slice(0, 44) }}</div>
              </td>
              <td>
                <template v-if="windowData(item, 'weekly')">
                  <div class="cliproxy-window">
                    <span class="cliproxy-window-label">{{ windowLabel(windowData(item, 'weekly'), "周额度") }}</span>
                    <div class="cliproxy-quota"><div class="cliproxy-track"><i :class="quotaLevel(windowData(item, 'weekly'))" :style="{ width: `${usedPercent(windowData(item, 'weekly')) ?? 0}%` }"></i></div><strong v-if="remainingPercent(windowData(item, 'weekly')) !== null">剩 {{ remainingPercent(windowData(item, 'weekly')) }}%</strong><strong v-else>额度未知</strong></div>
                    <small>已用 {{ usedPercent(windowData(item, 'weekly')) === null ? "—" : `${Math.round(usedPercent(windowData(item, 'weekly')))}%` }} · {{ formatReset(windowData(item, 'weekly')) }}</small>
                  </div>
                </template>
                <span v-else class="subtle">未提供周额度</span>
              </td>
              <td>
                <template v-if="windowData(item, 'five_hour')">
                  <div class="cliproxy-window">
                    <span class="cliproxy-window-label">{{ windowLabel(windowData(item, 'five_hour'), "5 小时窗口") }}</span>
                    <div class="cliproxy-quota"><div class="cliproxy-track"><i :class="quotaLevel(windowData(item, 'five_hour'))" :style="{ width: `${usedPercent(windowData(item, 'five_hour')) ?? 0}%` }"></i></div><strong v-if="remainingPercent(windowData(item, 'five_hour')) !== null">剩 {{ remainingPercent(windowData(item, 'five_hour')) }}%</strong><strong v-else>额度未知</strong></div>
                    <small>已用 {{ usedPercent(windowData(item, 'five_hour')) === null ? "—" : `${Math.round(usedPercent(windowData(item, 'five_hour')))}%` }} · {{ formatReset(windowData(item, 'five_hour')) }}</small>
                  </div>
                </template>
                <span v-else class="subtle">未提供 5 小时窗口</span>
              </td>
              <td><small class="cliproxy-sub">{{ windowData(item, 'weekly')?.reset_at ? formatResetAt(windowData(item, 'weekly').reset_at) : "-" }}</small></td>
              <td>
                <div class="cliproxy-reset-cell">
                  <button v-if="Number(item.quota?.reset_credits?.available || 0) > 0" class="btn secondary sm cliproxy-reset-btn" type="button" :disabled="loading || refreshing || importing || !!resettingFile" @click="openResetDialog(item)">{{ resettingFile === item.file ? "重置中…" : `重置 (${item.quota.reset_credits.available})` }}</button>
                  <span v-else class="cliproxy-reset-empty">{{ item.quota?.reset_credits?.available ?? "-" }}</span>
                </div>
              </td>
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
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="exportDialog.open" class="app-dialog-mask" role="presentation" @click.self="closeExportDialog">
          <section class="app-dialog cliproxy-download-dialog" role="dialog" aria-modal="true" :aria-label="`选择 ${exportDialog.kind === 'sub2' ? 'SUB2' : 'CPA'} 导出范围`">
            <div class="app-dialog-head">
            <div><span class="app-dialog-kicker">{{ exportDialog.kind === "sub2" ? "SUB2 JSON" : "CPA JSON" }}</span><h2>选择导出范围</h2></div>
            <button class="app-dialog-close" type="button" aria-label="关闭" @click="closeExportDialog">×</button>
          </div>
          <p>选择导出选中的账号，或导出全部中转账号。</p>
          <div class="cliproxy-download-options">
            <button class="cliproxy-download-option" type="button" :disabled="!exportDialog.files.length" @click="chooseExport('selected')">
              <strong>导出选中 ({{ exportDialog.files.length }})</strong><small>仅导出当前勾选的账号</small>
            </button>
            <button class="cliproxy-download-option" type="button" @click="chooseExport('all')">
              <strong>导出全部 ({{ items.length }})</strong><small>合并成一个 {{ exportDialog.kind === "sub2" ? "SUB2" : "CPA" }} JSON 文件</small>
            </button>
          </div>
          <div class="app-dialog-actions"><button class="btn secondary" type="button" @click="closeExportDialog">取消</button></div>
        </section>
      </div>
    </Transition>
  </Teleport>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="resetDialog.open" class="app-dialog-mask" role="presentation" @click.self="closeResetDialog">
        <section class="app-dialog cliproxy-download-dialog" role="dialog" aria-modal="true" aria-label="确认消耗重置卡">
          <div class="app-dialog-head">
            <div><span class="app-dialog-kicker">重置卡</span><h2>重置当前账号额度</h2></div>
            <button class="app-dialog-close" type="button" aria-label="关闭" @click="closeResetDialog">×</button>
          </div>
          <p>将为 <b>{{ resetDialog.item?.email || resetDialog.item?.file }}</b> 消耗 1 张重置卡，重置当前可重置的 Codex 限额窗口。此操作不可撤销。</p>
          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="closeResetDialog">取消</button>
            <button class="btn primary" type="button" @click="confirmReset">确认重置</button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
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
.cliproxy-track i.unknown { background: var(--line-2); }
.cliproxy-quota-text { font-size: 10.5px; color: var(--ink-500); font-family: var(--mono); white-space: nowrap; }
.cliproxy-file-input { display: none; }
.cliproxy-window { display: grid; gap: 3px; min-width: 150px; }
.cliproxy-window-label { font-size: 10px; color: var(--ink-500); font-weight: 700; letter-spacing: .04em; }
.cliproxy-window strong { font-family: var(--mono); font-size: 12px; color: var(--ink-800); }
.cliproxy-window small { color: var(--ink-500); font-size: 10px; white-space: nowrap; }
.cliproxy-download-dialog { width: min(520px, 100%); background: var(--bg-2); border-color: var(--line-2); box-shadow: 0 28px 90px rgba(0, 0, 0, .42); }
.cliproxy-download-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.cliproxy-download-option { display: grid; gap: 6px; padding: 14px; min-height: 92px; text-align: left; border: 1px solid var(--line-2); border-radius: 12px; background: var(--bg-inset); color: var(--ink-800); cursor: pointer; transition: border-color .15s, background .15s, transform .15s; }
.cliproxy-download-option:hover { border-color: var(--primary-500); background: var(--bg-3); transform: translateY(-1px); }
.cliproxy-download-option:disabled { opacity: .45; cursor: not-allowed; transform: none; }
.cliproxy-download-option strong { font-size: 14px; }
.cliproxy-download-option small { color: var(--ink-500); line-height: 1.5; }
@media (max-width: 640px) { .cliproxy-download-options { grid-template-columns: 1fr; } }
.cliproxy-plan { font-weight: 700; }
.cliproxy-plan.free { color: #93a4bd; background: rgba(147, 164, 189, .12); border-color: rgba(147, 164, 189, .28); }
.cliproxy-plan.plus { color: #fbbf24; background: rgba(251, 191, 36, .12); border-color: rgba(251, 191, 36, .35); }
.cliproxy-plan.pro { color: #a5b4fc; background: rgba(129, 140, 248, .14); border-color: rgba(129, 140, 248, .38); }
.cliproxy-plan.team { color: #22d3ee; background: rgba(34, 211, 238, .12); border-color: rgba(34, 211, 238, .35); }
.cliproxy-reset-cell { display: flex; align-items: center; gap: 8px; }
.cliproxy-reset-cell strong { font-family: var(--mono); color: var(--ink-800); font-size: 13px; }
.cliproxy-reset-btn { min-height: 30px; padding: 4px 10px; font-size: 11px; }
.cliproxy-reset-empty { color: var(--ink-500); font-family: var(--mono); font-size: 12px; }
</style>
