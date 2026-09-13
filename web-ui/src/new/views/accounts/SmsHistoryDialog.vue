<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Clipboard, History, RefreshCw, ScrollText, Search } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppButton from "../../components/ui/AppButton.vue";
import { fetchCodexHistory, type CodexHistoryEntry } from "../../api/batchops";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useCopy } from "../../composables/useCopy";

const batch = useBatchOpsStore();
const ui = useUiStore();
const { copyText } = useCopy();
type HistoryRow = CodexHistoryEntry & { email: string; key: string; outcome: string };
const entries = ref<HistoryRow[]>([]);
const loading = ref(false);
const error = ref("");
const query = ref("");
const filter = ref("all");
const expanded = ref<Record<string, boolean>>({});
const limit = ref(60);
let requestId = 0;

function outcome(entry: CodexHistoryEntry) {
  if (entry.outcome === "synced" || entry.status === "synced" || (entry.action === "sync_only" && entry.relay_status === "synced")) return "synced";
  if (entry.skipped || entry.outcome === "skipped" || entry.status === "skipped") return "skipped";
  if (entry.status === "cancelled" || (entry.status === "failed" && /^(?:任务已(?:停止|取消)|用户(?:停止|取消)|cancel(?:led|ed)\b)/i.test(entry.error || ""))) return "cancelled";
  return entry.status === "error" ? "failed" : entry.status;
}

async function load() {
  const id = ++requestId;
  loading.value = true; error.value = "";
  try {
    const result = await fetchCodexHistory(batch.codexHistoryEmails);
    if (id !== requestId || !batch.codexHistoryOpen) return;
    entries.value = Object.entries(result.history || {}).flatMap(([email, rows]) => rows.map((entry, index) => ({ ...entry, email, key: `${email}-${entry.at}-${index}`, outcome: outcome(entry) }))).sort((a, b) => b.at.localeCompare(a.at));
  } catch (cause) {
    if (id === requestId) error.value = cause instanceof Error ? cause.message : "接码历史加载失败";
  } finally { if (id === requestId) loading.value = false; }
}
watch(() => batch.codexHistoryOpen, (open) => {
  ++requestId;
  if (!open) return;
  query.value = ""; filter.value = "all"; expanded.value = {}; limit.value = 60; entries.value = [];
  void load();
});
watch([query, filter], () => { limit.value = 60; });
const filtered = computed(() => entries.value.filter((entry) => (!query.value.trim() || entry.email.toLowerCase().includes(query.value.trim().toLowerCase())) && (filter.value === "all" || entry.outcome === filter.value)));
// Historical failures which were later resolved should not be retried again.
const latestFailures = computed(() => {
  const seen = new Set<string>();
  const failures: HistoryRow[] = [];
  for (const entry of entries.value) {
    const key = entry.email.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    if (entry.outcome === "failed" && (!query.value.trim() || key.includes(query.value.trim().toLowerCase()))) failures.push(entry);
  }
  return failures;
});
const labels: Record<string, string> = { success: "授权成功", synced: "已补同步", skipped: "已存在 / 旧记录沿用", failed: "失败", cancelled: "取消" };
function optionLabel(entry: HistoryRow) {
  const options = entry.options?.sms_options;
  if (!options) return "";
  return [entry.options?.strategy ? ({ auto: "自动补齐中转", sync_only: "仅补同步", reauthorize: "重新授权" }[entry.options.strategy]) : "", options.provider === "firefox" ? "火狐狸" : options.provider === "smsbower" ? "SMSBower" : "", options.country_order ? `国家 ${options.country_order}` : "", options.max_phone_attempts === 0 ? "取号次数不限" : options.max_phone_attempts ? `每账号最多 ${options.max_phone_attempts} 次` : ""].filter(Boolean).join(" · ");
}
function retryEntries(emails: string[]) {
  batch.codexHistoryOpen = false;
  batch.openCodexStart(emails);
}
async function copyFailures() {
  const ok = await copyText(latestFailures.value.map((entry) => entry.email).join("\n"), "sms-history-failed");
  ui.notify(ok ? `已复制 ${latestFailures.value.length} 个失败邮箱` : "复制失败", ok ? "success" : "error");
}
async function copyRt(entry: HistoryRow) {
  if (!entry.rt) return;
  const ok = await copyText(entry.rt, `sms-history-${entry.key}`);
  ui.notify(ok ? "已复制历史 RT" : "复制失败", ok ? "success" : "error");
}
function logClass(level: string) { return level === "error" ? "log-error" : level === "warning" ? "log-warning" : level === "ok" ? "log-ok" : ""; }
function phoneAttemptText(attempt: NonNullable<CodexHistoryEntry["phone_attempts"]>[number]) {
  const state = String(attempt.result || attempt.status || "");
  const labels: Record<string, string> = { success: "成功", failed: "失败", cancelled: "取消", timeout: "等码超时", session_retry: "保留号码，重建登录会话", activation_expired: "号码租约已结束" };
  return `${attempt.phone || (attempt.acquired ? "已分配号码" : "未取到号码")} · ${labels[state] || state || "已记录"}`;
}
</script>

<template>
  <AppModal :open="batch.codexHistoryOpen" title="接码历史" subtitle="按时间查看账号处理记录；仅重试最新结果仍失败的账号" width="900px" @close="batch.codexHistoryOpen = false">
    <template #icon><History :size="18" /></template>
    <div class="history-toolbar"><label class="search-field"><Search :size="14" /><input v-model="query" type="search" placeholder="搜索邮箱" aria-label="按邮箱筛选接码历史" /></label><select v-model="filter" class="filter-select" aria-label="筛选接码历史结果"><option value="all">全部记录</option><option value="success">授权成功</option><option value="synced">补同步中转</option><option value="skipped">已存在 / 旧记录沿用</option><option value="failed">失败</option><option value="cancelled">取消</option></select><AppButton size="sm" :loading="loading" @click="load"><RefreshCw :size="14" />刷新</AppButton></div>
    <div v-if="latestFailures.length" class="failed-summary"><span>最新结果仍失败 <b>{{ latestFailures.length }}</b> 个账号</span><div class="actions"><AppButton size="sm" @click="copyFailures"><Clipboard :size="13" />复制失败邮箱</AppButton><AppButton size="sm" :disabled="batch.codex.running || batch.codexStartBusy" @click="retryEntries(latestFailures.map((entry) => entry.email))"><RefreshCw :size="13" />设置重试任务</AppButton></div></div>
    <p v-if="error" class="error-box" role="alert">{{ error }}</p>
    <p v-if="loading && !entries.length" class="empty-state">正在加载接码历史…</p>
    <p v-else-if="!filtered.length && !error" class="empty-state">{{ entries.length ? '没有匹配条件的接码记录' : '暂无接码记录，完成任务后可在这里查看历史' }}</p>
    <div class="history-list">
      <article v-for="entry in filtered.slice(0, limit)" :key="entry.key" :class="['history-row', `outcome-${entry.outcome}`]">
        <header><b>{{ entry.email }}</b><span class="outcome">{{ labels[entry.outcome] || entry.outcome }}</span><time>{{ entry.at.replace('T', ' ') }}</time></header>
        <p class="message">{{ entry.outcome === 'synced' ? '已补齐中转 JSON，未重新授权' : entry.outcome === 'skipped' ? (entry.relay_status === 'ready' ? '中转已有匹配凭据，未在线验证' : '旧记录仅确认持有 RT，未核对中转') : entry.error || entry.note || (entry.outcome === 'cancelled' ? '任务取消' : 'RT 获取成功') }}</p>
        <small v-if="entry.relay_file" class="options">中转文件：{{ entry.relay_file }}</small><small v-if="entry.authorization_status === 'success' && entry.relay_status === 'failed'" class="options">授权已成功，需补同步中转</small><small v-if="optionLabel(entry)" class="options">{{ optionLabel(entry) }}</small>
        <footer><small>{{ entry.phone_attempts?.length ? `取号 ${entry.phone_attempts.length} 次` : entry.phone ? `号码 ${entry.phone}` : entry.batch_id ? `批次 ${entry.batch_id.slice(0, 12)}` : '历史记录' }}</small><div class="actions"><AppButton v-if="entry.logs?.length || entry.phone_attempts?.length" size="sm" :aria-expanded="!!expanded[entry.key]" @click="expanded = { ...expanded, [entry.key]: !expanded[entry.key] }"><ScrollText :size="13" />{{ expanded[entry.key] ? '收起详情' : '记录详情' }}</AppButton><AppButton v-if="entry.rt && ['success', 'synced', 'skipped'].includes(entry.outcome)" size="sm" @click="copyRt(entry)"><Clipboard :size="13" />复制历史 RT</AppButton><AppButton v-if="entry.outcome === 'failed' && latestFailures.some((item) => item.key === entry.key)" size="sm" :disabled="batch.codex.running || batch.codexStartBusy" @click="retryEntries([entry.email])"><RefreshCw :size="13" />重试</AppButton></div></footer>
        <div v-if="expanded[entry.key]" class="details"><div v-if="entry.phone_attempts?.length" class="phone-attempts"><p v-for="(attempt, index) in entry.phone_attempts" :key="index">第 {{ index + 1 }} 次 · {{ phoneAttemptText(attempt) }}</p></div><p v-for="(line, index) in entry.logs || []" :key="index" :class="logClass(line.level)"><span class="log-time">{{ line.t }}</span>{{ line.m }}</p></div>
      </article>
      <AppButton v-if="filtered.length > limit" size="sm" @click="limit += 60">再显示 {{ Math.min(60, filtered.length - limit) }} 条记录</AppButton>
    </div>
    <template #footer><span class="record-count">共 {{ filtered.length }} 条匹配记录</span><AppButton @click="batch.codexHistoryOpen = false">关闭</AppButton></template>
  </AppModal>
</template>

<style scoped>
.history-toolbar,.actions,.failed-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }.history-toolbar { margin-bottom: 12px; }.search-field { display: flex; align-items: center; gap: 8px; padding: 0 10px; min-height: 40px; flex: 1; min-width: 150px; color: var(--text-secondary); border: 1px solid var(--field-border); background: var(--field-bg); border-radius: 8px; }.search-field input { width: 100%; background: none; border: 0; outline: none; min-height: 38px; color: var(--text-primary); font-size: .8rem; }.filter-select { min-height: 40px; border: 1px solid var(--field-border); border-radius: 8px; padding: 6px 10px; color: var(--text-primary); background: var(--field-bg); font-size: .78rem; }.failed-summary { padding: 10px 12px; background: var(--warning-bg); border-radius: var(--radius-sm); color: var(--text-primary); font-size: .78rem; margin-bottom: 12px; justify-content: space-between; }.history-list { display: grid; gap: 8px; }.history-row { padding: 12px; background: var(--glass-bg); border: 1px solid var(--field-border); border-left-width: 3px; border-radius: 8px; }.outcome-success { border-left-color: var(--success); }.outcome-failed { border-left-color: var(--error); }.outcome-skipped { border-left-color: var(--info); }.history-row header,.history-row footer { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }.history-row header b { font-weight: 500; font-size: .8rem; color: var(--text-primary); overflow-wrap: anywhere; }.outcome { font-size: .69rem; border: 1px solid var(--field-border); background: var(--field-bg); color: var(--text-secondary); padding: 2px 6px; border-radius: 4px; }time { margin-left: auto; font-size: .69rem; font-family: var(--mono); color: var(--text-secondary); }.message { font-size: .75rem; color: var(--text-primary); line-height: 1.6; overflow-wrap: anywhere; margin-top: 6px; }.options { font-size: .71rem; color: var(--text-secondary); display: block; margin-top: 4px; overflow-wrap: anywhere; }.history-row footer { justify-content: space-between; margin-top: 8px; }.history-row footer small { font-size: .7rem; color: var(--text-secondary); }.details { border: 1px solid var(--field-border); border-radius: 8px; background: var(--field-bg); padding: 10px; margin-top: 10px; max-height: 230px; overflow: auto; font: .72rem/1.65 var(--mono); color: var(--text-secondary); }.details p { margin: 0; overflow-wrap: anywhere; }.phone-attempts { padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid var(--field-border); }.log-time { margin-right: 8px; }.log-error { color: var(--error); }.log-warning { color: var(--warning); }.log-ok { color: var(--success); }.empty-state { padding: 35px 12px; text-align: center; color: var(--text-secondary); font-size: .8rem; }.error-box { padding: 12px; border-radius: 8px; background: var(--error-bg); color: var(--text-primary); font-size: .79rem; }.record-count { margin-right: auto; align-self: center; color: var(--text-secondary); font-size: .75rem; }
:deep(button:focus-visible),select:focus-visible,.search-field:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (max-width: 600px) { time { width: 100%; margin-left: 0; }.search-field input { font-size: 16px; }.history-row { padding: 10px; }:deep(.btn) { min-height: 40px; } }
@media (prefers-reduced-motion: reduce) { *, :deep(*) { animation: none !important; transition: none !important; } }
</style>
