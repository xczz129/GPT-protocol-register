<script setup lang="ts">
/* --- 中转管理：cliproxy 账号额度、导入导出、重置卡、凭据失效重登 ---
   额度与账号状态由面板直连 OpenAI usage 端点查询，不经 cliproxy 接口（与旧版一致）。 */
import { computed, onMounted, ref } from "vue";
import { Download, RefreshCw, Upload } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppTag from "../../components/ui/AppTag.vue";
import CodexLiveDialog from "../accounts/CodexLiveDialog.vue";
import { useCliproxyStore } from "../../stores/cliproxy";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import { cliproxyDownloadUrl, type CliproxyItem, type CliproxyQuotaWindow } from "../../api/cliproxy";

const cliproxy = useCliproxyStore();
const batch = useBatchOpsStore();
const ui = useUiStore();
const confirm = useConfirm();

const selected = ref<string[]>([]);
const planFilter = ref("all");
const cpaInput = ref<HTMLInputElement | null>(null);
const sub2Input = ref<HTMLInputElement | null>(null);
const exportDialog = ref<{ open: boolean; kind: "cpa" | "sub2"; files: string[] }>({ open: false, kind: "cpa", files: [] });
const reloginLoading = ref(false);

const items = computed(() => cliproxy.accounts.items || []);

const STATUS_META: Record<string, { label: string; kind: "success" | "warning" | "error" }> = {
  ok: { label: "正常", kind: "success" },
  limit_reached: { label: "额度用尽", kind: "warning" },
  blocked: { label: "已被限制", kind: "warning" },
  rate_limited: { label: "限流 · 429", kind: "warning" },
  unauthorized: { label: "已失效 · 401", kind: "error" },
  forbidden: { label: "被拒 · 403", kind: "error" },
  refresh_failed: { label: "凭据失效", kind: "error" },
  error: { label: "查询失败", kind: "error" },
};

function itemStatus(item: CliproxyItem) {
  return String(item?.status || item?.quota?.status || "");
}

function statusMeta(item: CliproxyItem) {
  return STATUS_META[itemStatus(item)] || null;
}

function windowData(item: CliproxyItem, kind: "weekly" | "five_hour"): CliproxyQuotaWindow | null {
  const quota = item?.quota || {};
  if (Object.prototype.hasOwnProperty.call(quota, kind)) return (quota[kind] as CliproxyQuotaWindow | null) || null;
  const windows = [quota.primary, quota.secondary].filter((w): w is CliproxyQuotaWindow => Boolean(w));
  return windows.find((w) => {
    const seconds = Number(w.window_seconds || 0);
    return kind === "weekly" ? seconds >= 6 * 86400 : seconds >= 4 * 3600 && seconds <= 6 * 3600;
  }) || null;
}

function usedPercent(w: CliproxyQuotaWindow | null): number | null {
  const raw = w?.used_percent;
  if (raw === null || raw === undefined || raw === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
}

function remainingPercent(w: CliproxyQuotaWindow | null): number | null {
  const used = usedPercent(w);
  return used === null ? null : Math.round(100 - used);
}

function quotaLevel(w: CliproxyQuotaWindow | null): string {
  const used = usedPercent(w);
  if (used === null) return "unknown";
  if (used >= 95) return "critical";
  if (used >= 80) return "warn";
  return "ok";
}

function formatTime(value?: string) {
  if (!value) return "-";
  try {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return "-";
  }
}

function formatResetAt(value?: number) {
  const ts = Number(value);
  if (!ts) return "-";
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return "-";
  }
}

function formatReset(w: CliproxyQuotaWindow | null) {
  const seconds = Number(w?.reset_after_seconds);
  if (Number.isFinite(seconds) && seconds > 0) {
    if (seconds >= 86400) return `${Math.ceil(seconds / 86400)} 天后重置`;
    if (seconds >= 3600) return `${Math.ceil(seconds / 3600)} 小时后重置`;
    return `${Math.ceil(seconds / 60)} 分钟后重置`;
  }
  return w?.reset_at ? formatResetAt(w.reset_at) : "重置时间未知";
}

function planClass(value?: string) {
  const plan = String(value || "").trim().toLowerCase();
  if (plan.includes("team") || plan.includes("business")) return "team";
  if (plan.includes("pro")) return "pro";
  if (plan.includes("plus")) return "plus";
  return "free";
}

const planFilterOptions = computed(() => {
  const counts: Record<string, number> = { all: items.value.length, free: 0, plus: 0, pro: 0, team: 0 };
  for (const item of items.value) counts[planClass(item.plan_type)] += 1;
  return [
    { value: "all", label: `全部 (${counts.all})` },
    { value: "free", label: `Free (${counts.free})` },
    { value: "plus", label: `Plus (${counts.plus})` },
    { value: "pro", label: `Pro (${counts.pro})` },
    { value: "team", label: `Team (${counts.team})` },
  ].filter((option) => option.value === "all" || counts[option.value] > 0);
});

const filteredItems = computed(() => {
  if (planFilter.value === "all") return items.value;
  return items.value.filter((item) => planClass(item.plan_type) === planFilter.value);
});

const statusCounts = computed(() => {
  const counts = { ok: 0, invalid: 0, limited: 0, unchecked: 0 };
  for (const item of items.value) {
    const status = itemStatus(item);
    if (!status) counts.unchecked += 1;
    else if (status === "ok") counts.ok += 1;
    else if (["limit_reached", "rate_limited", "blocked"].includes(status)) counts.limited += 1;
    else if (["unauthorized", "forbidden", "refresh_failed"].includes(status)) counts.invalid += 1;
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

const allChecked = computed(() => filteredItems.value.length > 0 && filteredItems.value.every((item) => selected.value.includes(item.file)));

function toggleAll() {
  selected.value = allChecked.value ? [] : filteredItems.value.map((item) => item.file);
}

function isCredentialDead(item: CliproxyItem) {
  return ["unauthorized", "forbidden", "refresh_failed"].includes(itemStatus(item));
}

/* --- 操作 --- */
async function refreshAll() {
  try {
    await cliproxy.refreshQuota([]);
    ui.notify("额度已刷新", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "额度刷新失败", "error");
  }
}

async function reload() {
  try {
    await cliproxy.load();
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "加载失败", "error");
  }
}

async function handleImportFile(event: Event, kind: "cpa" | "sub2") {
  const inputEl = event.target as HTMLInputElement;
  const file = inputEl.files?.[0];
  inputEl.value = "";
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    ui.notify("账号 JSON 不能超过 20MB", "error");
    return;
  }
  try {
    const content = await file.text();
    if (!content.trim()) {
      ui.notify(`请选择有效的 ${kind.toUpperCase()} JSON 文件`, "error");
      return;
    }
    const result = await cliproxy.importJson(kind, file.name, content);
    ui.notify(
      `${kind.toUpperCase()} 导入完成：新增 ${result.imported || 0} 个，跳过 ${result.skipped || 0} 个`,
      result.imported ? "success" : "warning",
    );
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : `${kind.toUpperCase()} JSON 导入失败`, "error");
  }
}

function openExportDialog(kind: "cpa" | "sub2") {
  exportDialog.value = { open: true, kind, files: [...selected.value] };
}

async function chooseExport(scope: "selected" | "all") {
  const kind = exportDialog.value.kind;
  const files = scope === "selected" ? exportDialog.value.files : [];
  exportDialog.value = { open: false, kind, files: [] };
  try {
    await cliproxy.exportJson(kind, files);
    ui.notify(`${kind.toUpperCase()} JSON 已导出 ${files.length || items.value.length} 个账号`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : `${kind.toUpperCase()} JSON 导出失败`, "error");
  }
}

async function requestDelete(files: string[]) {
  const list = files.filter(Boolean);
  if (!list.length) return;
  const byFile = new Map(items.value.map((item) => [item.file, item]));
  const label = list.length === 1 ? (byFile.get(list[0])?.email || list[0]) : `选中的 ${list.length} 个账号`;
  if (!(await confirm(`删除中转账号 ${label}？凭据会移出中转目录，不影响账号库。`, { title: "删除中转账号", confirmText: "删除", danger: true }))) return;
  try {
    const result = await cliproxy.remove(list);
    selected.value = selected.value.filter((file) => !list.includes(file));
    ui.notify(`已删除 ${result.deleted?.length || 0} 个中转账号`, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "中转账号删除失败", "error");
  }
}

async function requestReset(item: CliproxyItem) {
  if (!(await confirm(`将为 ${item.email || item.file} 消耗 1 张重置卡，重置当前可重置的 Codex 限额窗口。此操作不可撤销。`, { title: "重置当前账号额度", confirmText: "确认重置" }))) return;
  try {
    await cliproxy.consumeReset(item.file);
    ui.notify("重置卡已消耗，额度窗口正在刷新", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "重置卡消耗失败", "error");
  }
}

/** 凭据失效的中转账号：用账号库凭据重新 OAuth 登录（走 codex 批次 + 实时弹窗） */
async function requestRelogin(item: CliproxyItem) {
  const email = String(item?.email || "").trim();
  if (!email) return;
  if (!(await confirm(`对 ${email} 重新 OAuth 登录？将用账号库凭据重新走完整登录流程，成功后新 RT 会写回账号库和中转。`, { title: "重新登录", confirmText: "开始登录" }))) return;
  reloginLoading.value = true;
  try {
    await batch.runCodex([email], 1, true);
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "重新登录任务启动失败", "error");
  } finally {
    reloginLoading.value = false;
  }
}

onMounted(async () => {
  batch.setOnFinished(() => cliproxy.load());
  await reload();
});
</script>

<template>
  <div class="cliproxy-page">
    <section class="glass-panel panel">
      <div class="panel-head">
        <div>
          <h2>中转账号</h2>
          <p class="panel-sub">接码成功的账号自动同步；额度与账号状态（封号/401/429）由面板直连 OpenAI 查询，不经 cliproxy 接口。</p>
        </div>
        <div class="head-actions">
          <AppButton variant="accent" size="sm" :disabled="cliproxy.loading || cliproxy.refreshing || cliproxy.importing || !items.length" @click="refreshAll">
            <RefreshCw :size="14" /><span>{{ cliproxy.refreshing ? "刷新中…" : "刷新额度" }}</span>
          </AppButton>
          <AppButton variant="ghost" size="sm" :disabled="cliproxy.loading || cliproxy.importing" @click="reload">重新加载</AppButton>
          <AppButton variant="ghost" size="sm" :disabled="cliproxy.loading || cliproxy.refreshing || cliproxy.importing" @click="cpaInput?.click()">
            <Upload :size="14" /><span>{{ cliproxy.importing ? "导入中…" : "导入 CPA JSON" }}</span>
          </AppButton>
          <AppButton variant="ghost" size="sm" :disabled="cliproxy.loading || cliproxy.refreshing || cliproxy.importing" @click="sub2Input?.click()">导入 SUB2 JSON</AppButton>
          <AppButton variant="primary" size="sm" :disabled="cliproxy.loading || cliproxy.refreshing || cliproxy.importing || !items.length" @click="openExportDialog('cpa')">
            <Download :size="14" /><span>导出 CPA JSON</span>
          </AppButton>
          <AppButton variant="accent" size="sm" :disabled="cliproxy.loading || cliproxy.refreshing || cliproxy.importing || !items.length" @click="openExportDialog('sub2')">导出 SUB2 JSON</AppButton>
          <AppButton variant="danger" size="sm" :disabled="cliproxy.loading || cliproxy.refreshing || cliproxy.importing || cliproxy.deleting || !selected.length" @click="requestDelete(selected)">
            {{ cliproxy.deleting ? "删除中…" : `删除选中 (${selected.length})` }}
          </AppButton>
          <input ref="cpaInput" class="file-input" type="file" accept="application/json,.json" @change="(event) => handleImportFile(event, 'cpa')" />
          <input ref="sub2Input" class="file-input" type="file" accept="application/json,.json" @change="(event) => handleImportFile(event, 'sub2')" />
        </div>
      </div>

      <div class="stats">
        <div class="stat"><strong>{{ items.length }}</strong><span>账号总数</span></div>
        <div class="stat"><strong>{{ statusCounts.ok }}</strong><span>正常</span></div>
        <div class="stat" :class="{ 'is-alert': statusCounts.invalid > 0 }"><strong>{{ statusCounts.invalid }}</strong><span>封号/失效</span></div>
        <div class="stat"><strong>{{ statusCounts.limited }}</strong><span>限流/额度尽</span></div>
        <div class="stat"><strong>{{ resetCreditsTotal }}</strong><span>重置卡</span></div>
      </div>
      <p class="hint">额度刷新：{{ formatTime(lastQuotaFetched) }}<template v-if="!cliproxy.accounts.configured"> · 中转账号目录尚未配置</template></p>
      <div v-if="cliproxy.loading && !items.length" class="empty-hint">加载中…</div>
      <div v-else-if="!items.length" class="empty-hint">暂无中转账号；接码成功后会自动同步到这里。</div>
    </section>

    <section v-if="items.length" class="glass-panel panel">
      <div class="panel-head">
        <div>
          <h2>账号明细</h2>
          <p class="panel-sub">共 {{ items.length }} 个 · 已选 {{ selected.length }} 个<template v-if="planFilter !== 'all'"> · 筛选 {{ filteredItems.length }} 个</template></p>
        </div>
        <select v-model="planFilter" class="input plan-filter" aria-label="按套餐筛选">
          <option v-for="option in planFilterOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="col-check"><input type="checkbox" :checked="allChecked" aria-label="全选" @change="toggleAll" /></th>
              <th>账号</th><th>套餐</th><th>状态</th><th>周额度</th><th>5 小时窗口</th><th>重置时间</th><th>重置卡</th><th>AT 过期</th><th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in filteredItems" :key="item.file">
              <td class="col-check"><input v-model="selected" type="checkbox" :value="item.file" :aria-label="`选择中转账号 ${item.email || item.file}`" /></td>
              <td>
                <div class="account-cell">
                  <b class="email" :title="item.email || item.file">{{ item.email || "(未知邮箱)" }}</b>
                  <small class="sub mono" :title="item.file">{{ item.file }}</small>
                </div>
              </td>
              <td><span :class="['plan-pill', planClass(item.plan_type)]">{{ item.plan_type || "unknown" }}</span></td>
              <td>
                <AppTag v-if="statusMeta(item)" :kind="statusMeta(item)!.kind">{{ statusMeta(item)!.label }}</AppTag>
                <span v-else class="sub">未检测</span>
                <div v-if="item.quota_error && itemStatus(item) !== 'ok'" class="sub error-text" :title="item.quota_error">{{ item.quota_error.slice(0, 44) }}</div>
              </td>
              <td>
                <template v-if="windowData(item, 'weekly')">
                  <div class="window">
                    <span class="window-label">{{ windowData(item, "weekly")?.window_label || "周额度" }}</span>
                    <div class="quota">
                      <div class="track"><i :class="quotaLevel(windowData(item, 'weekly'))" :style="{ width: `${usedPercent(windowData(item, 'weekly')) ?? 0}%` }"></i></div>
                      <strong v-if="remainingPercent(windowData(item, 'weekly')) !== null">剩 {{ remainingPercent(windowData(item, "weekly")) }}%</strong>
                      <strong v-else>额度未知</strong>
                    </div>
                    <small>已用 {{ usedPercent(windowData(item, "weekly")) === null ? "—" : `${Math.round(usedPercent(windowData(item, "weekly"))!)}%` }} · {{ formatReset(windowData(item, "weekly")) }}</small>
                  </div>
                </template>
                <span v-else class="sub">未提供周额度</span>
              </td>
              <td>
                <template v-if="windowData(item, 'five_hour')">
                  <div class="window">
                    <span class="window-label">{{ windowData(item, "five_hour")?.window_label || "5 小时窗口" }}</span>
                    <div class="quota">
                      <div class="track"><i :class="quotaLevel(windowData(item, 'five_hour'))" :style="{ width: `${usedPercent(windowData(item, 'five_hour')) ?? 0}%` }"></i></div>
                      <strong v-if="remainingPercent(windowData(item, 'five_hour')) !== null">剩 {{ remainingPercent(windowData(item, "five_hour")) }}%</strong>
                      <strong v-else>额度未知</strong>
                    </div>
                    <small>已用 {{ usedPercent(windowData(item, "five_hour")) === null ? "—" : `${Math.round(usedPercent(windowData(item, "five_hour"))!)}%` }} · {{ formatReset(windowData(item, "five_hour")) }}</small>
                  </div>
                </template>
                <span v-else class="sub">未提供 5 小时窗口</span>
              </td>
              <td><small class="sub">{{ windowData(item, "weekly")?.reset_at ? formatResetAt(windowData(item, "weekly")?.reset_at) : "-" }}</small></td>
              <td>
                <AppButton v-if="Number(item.quota?.reset_credits?.available || 0) > 0" variant="accent" size="sm" :disabled="cliproxy.loading || cliproxy.refreshing || cliproxy.importing || Boolean(cliproxy.resettingFile)" @click="requestReset(item)">
                  {{ cliproxy.resettingFile === item.file ? "重置中…" : `重置 (${item.quota?.reset_credits?.available})` }}
                </AppButton>
                <span v-else class="sub mono">{{ item.quota?.reset_credits?.available ?? "-" }}</span>
              </td>
              <td><small class="sub">{{ item.expired ? formatTime(item.expired) : "-" }}</small></td>
              <td class="col-actions">
                <div class="row-actions">
                  <AppButton v-if="isCredentialDead(item)" variant="primary" size="sm" :disabled="reloginLoading" :title="`用账号库凭据对 ${item.email || item.file} 重新 OAuth 登录并覆盖写回`" @click="requestRelogin(item)">重新登录</AppButton>
                  <AppButton variant="ghost" size="sm" :disabled="cliproxy.refreshing" @click="cliproxy.refreshQuota([item])">刷新</AppButton>
                  <a class="download-link" :href="cliproxyDownloadUrl([item.file])">下载</a>
                  <AppButton variant="danger" size="sm" :disabled="cliproxy.deleting" @click="requestDelete([item.file])">删除</AppButton>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 导出范围选择 -->
    <AppModal :open="exportDialog.open" :title="`选择 ${exportDialog.kind === 'sub2' ? 'SUB2' : 'CPA'} 导出范围`" subtitle="选择导出选中的账号，或导出全部中转账号" width="520px" @close="exportDialog.open = false">
      <div class="export-options">
        <button class="export-option" type="button" :disabled="!exportDialog.files.length" @click="chooseExport('selected')">
          <strong>导出选中 ({{ exportDialog.files.length }})</strong>
          <small>仅导出当前勾选的账号</small>
        </button>
        <button class="export-option" type="button" @click="chooseExport('all')">
          <strong>导出全部 ({{ items.length }})</strong>
          <small>合并成一个 {{ exportDialog.kind === "sub2" ? "SUB2" : "CPA" }} JSON 文件</small>
        </button>
      </div>
      <template #footer>
        <AppButton variant="ghost" @click="exportDialog.open = false">取消</AppButton>
      </template>
    </AppModal>

    <!-- 重登实时进度（共用接码批次弹窗） -->
    <CodexLiveDialog @history="() => {}" />
  </div>
</template>

<style scoped>
.cliproxy-page { display: flex; flex-direction: column; gap: 14px; }
.panel { padding: 18px 20px; display: flex; flex-direction: column; gap: 12px; }

.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.panel-head h2 { font-size: 1.05rem; color: var(--text-primary); }
.panel-sub { font-size: 0.8rem; color: var(--text-muted); margin-top: 3px; }
.head-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.file-input { display: none; }

.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 14px;
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
  background: var(--inset-bg);
}
.stat strong { font-size: 1.25rem; color: var(--text-primary); font-family: var(--font-mono, monospace); }
.stat span { font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; }
.stat.is-alert strong { color: var(--error); }

.hint { font-size: 0.75rem; color: var(--text-muted); }
.empty-hint { padding: 28px 0; text-align: center; color: var(--text-muted); font-size: 0.84rem; }

.plan-filter { width: auto; min-width: 110px; }

.table-wrap { overflow: auto; max-height: 60vh; border: 1px solid var(--line-subtle); border-radius: var(--radius-sm); }
table { width: 100%; min-width: 1080px; border-collapse: collapse; font-size: 0.8rem; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line-subtle); }
th { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; background: var(--inset-bg); position: sticky; top: 0; }

.col-check { width: 34px; }
.col-actions { text-align: right; white-space: nowrap; }

.account-cell { display: flex; flex-direction: column; gap: 3px; min-width: 180px; }
.email { font-size: 0.8rem; color: var(--text-primary); overflow-wrap: anywhere; }
.sub { font-size: 0.68rem; color: var(--text-muted); }
.mono { font-family: var(--font-mono, monospace); max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.error-text { color: var(--error); }

.plan-pill { padding: 3px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; }
.plan-pill.free { color: var(--text-muted); background: var(--inset-bg); }
.plan-pill.plus { color: var(--warning); background: var(--warning-bg); }
.plan-pill.pro { color: var(--info); background: var(--info-bg); }
.plan-pill.team { color: var(--accent); background: var(--accent-bg); }

.window { display: grid; gap: 3px; min-width: 150px; }
.window-label { font-size: 0.66rem; color: var(--text-muted); font-weight: 700; letter-spacing: 0.04em; }
.quota { display: flex; align-items: center; gap: 8px; min-width: 150px; }
.track { flex: 1; height: 5px; border-radius: 4px; background: var(--line-subtle); overflow: hidden; min-width: 70px; }
.track i { display: block; height: 100%; border-radius: 4px; background: var(--success); transition: width 0.3s; }
.track i.warn { background: var(--warning); }
.track i.critical { background: var(--error); }
.track i.unknown { background: var(--line-subtle); }
.quota strong { font-family: var(--font-mono, monospace); font-size: 0.76rem; color: var(--text-primary); white-space: nowrap; }
.window small { color: var(--text-muted); font-size: 0.66rem; white-space: nowrap; }

.row-actions { display: flex; gap: 6px; justify-content: flex-end; align-items: center; }
.download-link {
  font-size: 0.75rem;
  color: var(--accent);
  text-decoration: none;
  padding: 5px 8px;
  border-radius: 6px;
}
.download-link:hover { background: var(--accent-bg); }

.export-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.export-option {
  display: grid;
  gap: 6px;
  padding: 14px;
  min-height: 88px;
  text-align: left;
  border: 1px solid var(--line-subtle);
  border-radius: 12px;
  background: var(--inset-bg);
  color: var(--text-primary);
  cursor: pointer;
  transition: var(--transition-fast);
}
.export-option:hover { border-color: var(--accent); background: var(--accent-bg); }
.export-option:disabled { opacity: 0.45; cursor: not-allowed; }
.export-option strong { font-size: 0.88rem; }
.export-option small { color: var(--text-muted); line-height: 1.5; }

@media (max-width: 640px) {
  .export-options { grid-template-columns: 1fr; }
}
</style>
