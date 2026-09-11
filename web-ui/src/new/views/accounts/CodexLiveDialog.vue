<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Check, Clipboard, Download, Pause, Play, RefreshCw, ScrollText, Search, Smartphone, Upload, WifiOff } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useCopy } from "../../composables/useCopy";
import type { CodexItem, CodexLogLine, CodexResultItem } from "../../api/batchops";

const emit = defineEmits<{ history: [] }>();
const batch = useBatchOpsStore();
const ui = useUiStore();
const { copiedKey, copyText } = useCopy();
const logsEl = ref<HTMLElement | null>(null);
const expandedLogs = ref<Record<string, boolean>>({});
const query = ref("");
const filter = ref("all");
const logFilter = ref("all");
const followLogs = ref(true);
const showLogs = ref(true);
const visibleLimit = ref(40);
const now = ref(Date.now());
let clock: ReturnType<typeof setInterval> | undefined;

const status = computed(() => batch.codex);
const summary = computed(() => batch.codexSummary);
const isImport = computed(() => status.value.mode === "import");
const stopping = computed(() => status.value.stopping || status.value.status === "stopping");
const hasFailed = computed(() => summary.value.failed > 0);
const retryAvailable = computed(() => !status.value.running && hasFailed.value && (!isImport.value || batch.canRetryImport()));
const disconnected = computed(() => ["reconnecting", "offline", "error"].includes(batch.codexConnection));
const progress = computed(() => summary.value.total ? Math.min(100, Math.round(summary.value.done / summary.value.total * 100)) : 0);
type DisplayItem = CodexItem & Partial<CodexResultItem>;

// Active account results are only finalized at the end of an attempt. The
// batch log is already live; expose its exact email-prefixed lines meanwhile.
const liveLogsByEmail = computed(() => {
  const byEmail = new Map<string, CodexLogLine[]>();
  for (const line of status.value.logs || []) {
    const separator = String(line.m || "").indexOf(" | ");
    if (separator < 1) continue;
    const email = line.m.slice(0, separator).trim().toLowerCase();
    const lines = byEmail.get(email) || [];
    lines.push({ ...line, m: line.m.slice(separator + 3) });
    byEmail.set(email, lines);
  }
  return byEmail;
});

function resultState(item: { status: string; skipped?: boolean; error?: string; outcome?: string; action?: string; relay_status?: string }) {
  if (item.outcome === "synced" || item.status === "synced" || (item.action === "sync_only" && item.relay_status === "synced")) return "synced";
  if (item.skipped || item.status === "skipped") return "skipped";
  if (item.status === "failed" && /^(?:任务已(?:停止|取消)|用户(?:停止|取消)|cancel(?:led|ed)\b)/i.test(item.error || "")) return "cancelled";
  return item.status === "error" ? "failed" : item.status;
}
const items = computed<DisplayItem[]>(() => {
  const results = new Map((status.value.results || []).map((item) => [item.email.toLowerCase(), item]));
  const out: DisplayItem[] = (status.value.items || []).map((item) => {
    const result = results.get(item.email.toLowerCase());
    results.delete(item.email.toLowerCase());
    const row = { ...item, ...result };
    if (!row.logs?.length) row.logs = liveLogsByEmail.value.get(item.email.toLowerCase()) || [];
    return { ...row, status: resultState(row) };
  });
  for (const item of results.values()) out.push({ ...item, status: resultState(item) });
  return out;
});
const filteredItems = computed(() => items.value.filter((item) => (!query.value.trim() || item.email.toLowerCase().includes(query.value.trim().toLowerCase())) && (filter.value === "all" || item.status === filter.value)));
const failedEmails = computed(() => [...new Set(items.value.filter((item) => item.status === "failed").map((item) => item.email))]);
const tokenResults = computed(() => (status.value.results || []).filter((item) => ["success", "synced", "skipped"].includes(resultState(item)) && item.refresh_token));
const filteredLogs = computed(() => (status.value.logs || []).filter((line) => logFilter.value === "all" || (logFilter.value === "issues" ? ["warning", "error"].includes(line.level) : line.level === logFilter.value)));
const lastUpdated = computed(() => batch.codexLastUpdated ? new Date(batch.codexLastUpdated).toLocaleTimeString() : "尚未同步");
const stageLabels: Record<string, string> = { pending: "等待执行", login: "登录", password: "验证密码", mfa: "二次认证", acquiring: "获取号码", phone: "提交号码", waiting_sms: "等待短信", verifying_sms: "验证短信", workspace: "选择工作区", token: "保存 RT", mfa_retry: "等待认证窗口", relay_sync: "同步中转", syncing: "同步中转", relay_check: "检查中转" };
const statusLabels: Record<string, string> = { pending: "未执行", active: "处理中", success: "授权并同步", synced: "已补同步", skipped: "中转已有", failed: "失败", cancelled: "已取消" };
function label(item: DisplayItem) {
  if (item.status === "active") return stageLabels[item.stage || ""] || item.message || "处理中";
  if (isImport.value && item.status === "success") return "导入成功";
  if (item.status === "skipped" && item.relay_status !== "ready") return "沿用已有 RT（旧记录）";
  if (item.status === "failed" && item.authorization_status === "success") return "授权成功 · 同步失败";
  return statusLabels[item.status] || item.status;
}
function countdown(item: DisplayItem) {
  if (!item.wait_deadline || item.status !== "active") return "";
  const remaining = Math.max(0, Math.ceil(item.wait_deadline - now.value / 1000));
  return remaining ? `等码剩余 ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}` : "等码时间已到，正在更新状态";
}
watch(() => batch.codexDialogOpen, (open) => {
  if (clock) clearInterval(clock);
  clock = undefined;
  if (open) {
    showLogs.value = true;
    followLogs.value = true;
    logFilter.value = "all";
    now.value = Date.now();
    clock = setInterval(() => { now.value = Date.now(); }, 1000);
    // Also recover direct open paths which do not call openCodexDialog(). The
    // store coalesces this with any existing status request.
    void batch.refreshCodexStatus();
  }
}, { immediate: true });
watch(() => status.value.batch_id, () => { query.value = ""; filter.value = "all"; visibleLimit.value = 40; expandedLogs.value = {}; });
watch([query, filter], () => { visibleLimit.value = 40; });
watch(() => [status.value.logs, status.value.logs?.[status.value.logs.length - 1]?.m, followLogs.value, showLogs.value, batch.codexDialogOpen, logFilter.value], async () => {
  if (!followLogs.value || !showLogs.value || !batch.codexDialogOpen) return;
  await nextTick();
  if (!followLogs.value || !showLogs.value || !batch.codexDialogOpen) return;
  if (logsEl.value) logsEl.value.scrollTop = logsEl.value.scrollHeight;
}, { flush: "post" });
onBeforeUnmount(() => { if (clock) clearInterval(clock); });
function logClass(level: string) { return level === "error" ? "log-error" : level === "warning" ? "log-warning" : level === "ok" ? "log-ok" : ""; }
function logScroll() { const el = logsEl.value; if (el && el.scrollHeight - el.scrollTop - el.clientHeight > 36) followLogs.value = false; }
function toggleLogs(item: DisplayItem) { expandedLogs.value = { ...expandedLogs.value, [item.email]: !expandedLogs.value[item.email] }; }
async function copyToken(item: DisplayItem) {
  if (!item.refresh_token) return;
  const ok = await copyText(item.refresh_token, `codex-${item.email}`);
  ui.notify(ok ? `已复制 ${item.token_kind === "at" ? "AT" : "RT"}` : "复制失败", ok ? "success" : "error", 1500);
}
async function copyFailed() {
  const ok = await copyText(failedEmails.value.join("\n"), "codex-failed");
  ui.notify(ok ? `已复制 ${failedEmails.value.length} 个失败邮箱` : "复制失败", ok ? "success" : "error");
}
async function copySuccess() {
  const ok = await copyText(tokenResults.value.map((item) => `${item.email}----${item.refresh_token}`).join("\n"), "codex-tokens");
  ui.notify(ok ? `已复制 ${tokenResults.value.length} 条账号 Token` : "复制失败", ok ? "success" : "error");
}
function exportSuccess() {
  const text = tokenResults.value.map((item) => `${item.email}----${item.refresh_token}`).join("\n");
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url; link.download = `account-tokens-${new Date().toISOString().slice(0, 10)}.txt`;
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function retry(email?: string) {
  try { if (isImport.value) await batch.retryImportFailed(email); else await batch.retryCodexFailed(email); }
  catch (cause) { ui.notify(cause instanceof Error ? cause.message : "重试失败", "error"); }
}
function history() { if (isImport.value) emit("history"); else { batch.codexDialogOpen = false; batch.openCodexHistory(); } }
function close() { batch.codexDialogOpen = false; }
</script>

<template>
  <AppModal :open="batch.codexDialogOpen" :title="isImport ? '协议登录任务' : '授权与中转任务'" subtitle="关闭窗口或切换页面后，任务与进度同步仍会继续" width="900px" @close="close">
    <template #icon><Upload v-if="isImport" :size="18" /><Smartphone v-else :size="18" /></template>
    <div class="task-head">
      <div class="task-state" role="status"><span :class="['state-dot', { running: status.running, stale: disconnected }]"></span><b>{{ disconnected ? '同步中断 · 自动重连中' : stopping ? '正在停止…' : status.running ? '任务运行中' : status.status === 'cancelled' ? '任务已停止' : summary.total ? '任务已结束' : '暂无任务' }}</b><span>已处理 {{ summary.done }} / {{ summary.total }}</span></div>
      <div class="actions"><AppButton size="sm" @click="batch.refreshCodexStatus()"><RefreshCw :size="14" />刷新进度</AppButton><AppButton size="sm" @click="history"><ScrollText :size="14" />{{ isImport ? '导入历史' : '接码历史' }}</AppButton><AppButton v-if="status.running" variant="danger" size="sm" :disabled="stopping" :loading="batch.codexStopBusy" @click="batch.stopCodex()">{{ stopping ? '正在停止' : '停止任务' }}</AppButton></div>
    </div>
    <div class="progress-track" role="progressbar" aria-label="账号处理进度" :aria-valuenow="summary.done" :aria-valuemin="0" :aria-valuemax="Math.max(1, summary.total)"><div :style="{ transform: `scaleX(${progress / 100})` }"></div></div>
    <div :class="['result-summary', { 'with-relay': !isImport }]"><span><b>{{ summary.newSuccess }}</b>{{ isImport ? '导入成功' : '授权并同步' }}</span><span v-if="!isImport"><b>{{ summary.synced }}</b>补同步中转</span><span><b>{{ summary.reused }}</b>{{ isImport ? '沿用已有' : '中转已有' }}</span><span :class="{ failure: summary.failed }"><b>{{ summary.failed }}</b>失败</span><span><b>{{ summary.cancelled }}</b>取消</span><span><b>{{ summary.pending }}</b>未执行</span><span><b>{{ summary.active }}</b>处理中</span></div>
    <p v-if="status.running && !isImport" class="task-note">总进度按账号处理结束计数；同一账号正在换号重试时不会增加完成数。请查看下方实时日志、当前阶段及取号次数。</p>
    <div v-if="disconnected || batch.codexError" class="connection-warning" role="alert"><WifiOff :size="16" /><span>{{ batch.codexError || '暂时无法同步进度，已自动重连。当前显示上次同步结果。' }}<small>最近同步 {{ lastUpdated }}</small></span><AppButton size="sm" @click="batch.refreshCodexStatus()">立即重连</AppButton></div>
    <p v-if="stopping" class="task-note" role="status">正在收尾，不再开始新的账号。请等待服务端确认，结束后会分别显示已取消和未执行的数量。</p>
    <p v-if="isImport && hasFailed && !status.running && !batch.canRetryImport()" class="task-note">当前浏览器没有全部失败账号的原始导入凭据，可单条重试仍有凭据的账号，或复制失败邮箱后重新粘贴导入。</p>
    <section class="live-log-panel" aria-label="任务实时日志区域">
    <div class="log-controls"><button class="text-button" type="button" :aria-expanded="showLogs" @click="showLogs = !showLogs"><ScrollText :size="14" />{{ showLogs ? (status.running ? '实时日志' : '任务日志') : '展开实时日志' }} ({{ status.logs?.length || 0 }})</button><span class="log-sync-time">最近同步 {{ lastUpdated }}</span><template v-if="showLogs"><select v-model="logFilter" class="filter-select" aria-label="筛选日志级别"><option value="all">全部日志</option><option value="issues">警告与错误</option><option value="error">仅错误</option></select><button class="text-button" type="button" :aria-pressed="followLogs" @click="followLogs = !followLogs"><Pause v-if="followLogs" :size="13" /><Play v-else :size="13" />{{ followLogs ? '暂停跟随' : '跟随新日志' }}</button></template></div>
    <div v-if="showLogs" ref="logsEl" class="log-stream" tabindex="0" aria-label="实时任务日志" @scroll="logScroll"><p v-if="!filteredLogs.length" class="empty-state">{{ (status.logs || []).length ? '没有匹配级别的日志，可切换为“全部日志”' : disconnected ? '连接中断，正在恢复实时日志…' : status.running ? '正在等待任务日志…' : '本任务暂无日志' }}</p><p v-for="(line, index) in filteredLogs" :key="index" :class="logClass(line.level)"><span class="log-time">{{ line.t }}</span>{{ line.m }}</p></div>
    </section>
    <div class="results-toolbar">
      <label class="search-field"><Search :size="15" /><input v-model="query" aria-label="按邮箱筛选任务结果" placeholder="搜索邮箱" type="search" /></label>
      <select v-model="filter" class="filter-select" aria-label="筛选任务结果"><option value="all">全部结果 ({{ items.length }})</option><option value="active">处理中</option><option value="success">{{ isImport ? '导入成功' : '授权并同步' }}</option><option v-if="!isImport" value="synced">补同步中转</option><option value="skipped">{{ isImport ? '沿用已有' : '中转已有 / 旧记录沿用' }}</option><option value="failed">失败</option><option value="cancelled">取消</option><option value="pending">未执行</option></select>
      <div class="actions"><AppButton v-if="failedEmails.length" size="sm" @click="copyFailed"><Clipboard :size="13" />复制失败邮箱</AppButton><AppButton v-if="retryAvailable" size="sm" :loading="batch.codexStartBusy" @click="retry()"><RefreshCw :size="13" />只重试失败项</AppButton></div>
    </div>
    <div class="result-list">
      <p v-if="!filteredItems.length" class="empty-state">{{ items.length ? '没有匹配条件的账号' : status.running ? '正在准备账号，请稍候…' : '本批次暂无账号结果' }}</p>
      <article v-for="item in filteredItems.slice(0, visibleLimit)" :key="item.email" :class="['result-row', `result-${item.status}`]">
        <div class="result-main"><div class="result-title"><b>{{ item.email }}</b><span class="result-status">{{ label(item) }}</span></div><small v-if="item.error" class="error-text">{{ item.error }}</small><small v-else-if="item.status === 'skipped'">{{ item.relay_status === 'ready' ? '中转已有匹配凭据，未在线验证' : '旧记录仅确认持有 RT，未核对中转' }}</small><small v-else-if="item.status === 'synced'">已补齐中转 JSON，未重新授权或取号</small><small v-else-if="item.status === 'pending'">{{ status.running ? '等待空闲处理位' : '尚未执行，可重新选择后启动' }}</small><small v-else-if="item.status === 'cancelled'">本次处理已取消</small><small v-else-if="item.status === 'success'">{{ item.token_kind === 'at' ? 'AT' : 'RT' }} 已保存</small><small v-else>{{ item.message || '正在处理' }}</small><small v-if="item.relay_file" class="relay-file">中转文件：{{ item.relay_file }}</small><small v-if="item.authorization_status === 'success' && item.relay_status === 'failed'" class="relay-warning">新凭据已保存在账号库，重试只补同步，不再重复授权。</small><div v-if="!isImport && (item.attempt || item.wait_deadline)" class="stage-meta"><span v-if="item.attempt">取号 {{ item.attempt }} / {{ item.max_phone_attempts || '—' }} 次</span><span v-if="countdown(item)" class="countdown">{{ countdown(item) }}</span></div></div>
        <div class="actions"><AppButton v-if="item.logs?.length" size="sm" :aria-expanded="!!expandedLogs[item.email]" @click="toggleLogs(item)"><ScrollText :size="13" />{{ expandedLogs[item.email] ? '收起' : item.status === 'active' ? '实时日志' : '日志' }}</AppButton><AppButton v-if="item.status === 'failed' && !status.running && (!isImport || batch.canRetryImport(item.email))" size="sm" :disabled="batch.codexStartBusy" :aria-label="`重试 ${item.email}`" @click="retry(item.email)"><RefreshCw :size="13" />{{ item.retry_action === 'sync_only' ? '补同步中转' : '重试' }}</AppButton><AppButton v-if="item.refresh_token && ['success', 'synced', 'skipped'].includes(item.status)" size="sm" :aria-label="`复制 ${item.email} 的 Token`" @click="copyToken(item)"><Check v-if="copiedKey === `codex-${item.email}`" :size="13" /><Clipboard v-else :size="13" />{{ item.token_kind === 'at' ? '复制 AT' : '复制 RT' }}</AppButton></div>
        <div v-if="expandedLogs[item.email]" class="account-logs"><p v-for="(line, index) in item.logs || []" :key="index" :class="logClass(line.level)"><span class="log-time">{{ line.t }}</span>{{ line.m }}</p></div>
      </article>
      <AppButton v-if="filteredItems.length > visibleLimit" size="sm" @click="visibleLimit += 40">再显示 {{ Math.min(40, filteredItems.length - visibleLimit) }} 个账号</AppButton>
    </div>
    <template #footer><div class="footer-actions"><div class="actions"><AppButton v-if="tokenResults.length" size="sm" @click="copySuccess"><Clipboard :size="14" />复制成功 Token</AppButton><AppButton v-if="tokenResults.length" size="sm" @click="exportSuccess"><Download :size="14" />导出成功 Token</AppButton></div><AppButton @click="close">{{ status.running ? '收起，后台继续' : '关闭' }}</AppButton></div></template>
  </AppModal>
</template>

<style scoped>
.result-summary.with-relay { grid-template-columns: repeat(7, minmax(0, 1fr)); }
.relay-file { overflow-wrap: anywhere; }.relay-warning { color: var(--warning); }
@media(max-width:640px) { .result-summary.with-relay { grid-template-columns: repeat(3, minmax(0,1fr)); } }
.task-head,.task-state,.actions,.results-toolbar,.log-controls,.footer-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }.task-head,.footer-actions { justify-content: space-between; width: 100%; }.task-state { color: var(--text-secondary); font-size: .78rem; }.task-state b { color: var(--text-primary); font-size: .85rem; }.state-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); }.state-dot.running { background: var(--accent); }.state-dot.stale { background: var(--warning); }
.progress-track { height: 4px; background: var(--progress-track); border-radius: 10px; overflow: hidden; margin: 13px 0; }.progress-track div { height: 100%; width: 100%; background: var(--accent); transform-origin: left; transition: transform .3s ease; }
.result-summary { display: grid; grid-template-columns: repeat(6, minmax(0,1fr)); gap: 1px; border: 1px solid var(--field-border); border-radius: var(--radius-sm); overflow: hidden; margin-bottom: 15px; background: var(--line-subtle); }.result-summary span { display: grid; gap: 4px; padding: 10px 12px; font-size: .72rem; background: var(--field-bg); color: var(--text-secondary); }.result-summary b { font: 600 1.2rem var(--mono); color: var(--text-primary); }.result-summary .failure b { color: var(--error); }
.connection-warning { display: flex; align-items: center; gap: 10px; padding: 10px 12px; margin-bottom: 12px; background: var(--warning-bg); border-radius: var(--radius-sm); color: var(--text-primary); font-size: .78rem; }.connection-warning > span { flex: 1; }.connection-warning small { display: block; margin-top: 4px; color: var(--text-secondary); }.task-note { padding: 8px 0 12px; font-size: .76rem; line-height: 1.6; color: var(--text-secondary); }
.results-toolbar { margin-bottom: 10px; }.results-toolbar > .actions { margin-left: auto; }.search-field { display: flex; align-items: center; gap: 7px; background: var(--field-bg); border: 1px solid var(--field-border); padding: 0 10px; min-height: 38px; border-radius: 8px; color: var(--text-secondary); flex: 1; min-width: 160px; max-width: 250px; }.search-field input { width: 100%; background: none; border: 0; color: var(--text-primary); font-size: .79rem; outline: none; min-height: 36px; }.filter-select { color: var(--text-primary); background: var(--field-bg); border: 1px solid var(--field-border); min-height: 38px; border-radius: 8px; padding: 5px 10px; font-size: .76rem; cursor: pointer; }
.result-list { display: flex; flex-direction: column; gap: 6px; max-height: 40vh; overflow: auto; }.result-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 10px 12px; border: 1px solid var(--field-border); border-left: 3px solid var(--field-border); border-radius: 8px; background: var(--glass-bg); }.result-active { border-left-color: var(--accent); }.result-success { border-left-color: var(--success); }.result-failed { border-left-color: var(--error); }.result-skipped { border-left-color: var(--info); }.result-main { flex: 1; min-width: 180px; overflow-wrap: anywhere; }.result-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }.result-title b { color: var(--text-primary); font-weight: 500; font-size: .8rem; }.result-status { font-size: .69rem; color: var(--text-secondary); padding: 2px 6px; background: var(--field-bg); border: 1px solid var(--field-border); border-radius: 4px; }.result-main small { display: block; color: var(--text-secondary); font-size: .74rem; line-height: 1.6; margin-top: 3px; }.result-main .error-text { color: var(--text-primary); }.stage-meta { display: flex; gap: 12px; flex-wrap: wrap; font-size: .71rem; color: var(--text-secondary); margin-top: 5px; }.countdown { font-family: var(--mono); color: var(--accent); }
.account-logs,.log-stream { border-radius: 8px; padding: 10px; background: var(--field-bg); border: 1px solid var(--field-border); font-family: var(--mono); font-size: .72rem; line-height: 1.65; overflow: auto; color: var(--text-secondary); }.account-logs { flex-basis: 100%; max-height: 180px; }.log-stream { min-height: 120px; height: clamp(140px, 26vh, 240px); max-height: 240px; }
.live-log-panel { margin-bottom: 12px; }.log-sync-time { font-size: .7rem; color: var(--text-secondary); font-variant-numeric: tabular-nums; }.account-logs p,.log-stream p { margin: 0; overflow-wrap: anywhere; }.log-time { margin-right: 8px; font-variant-numeric: tabular-nums; }.log-controls { justify-content: flex-end; padding: 10px 0 6px; }.log-controls > :first-child { margin-right: auto; }.text-button { display: inline-flex; align-items: center; gap: 5px; background: none; color: var(--text-secondary); border: 0; font-size: .75rem; cursor: pointer; min-height: 36px; }.text-button:hover { color: var(--accent); }.log-error { color: var(--error); }.log-warning { color: var(--warning); }.log-ok { color: var(--success); }.empty-state { padding: 25px 12px; font-size: .78rem; color: var(--text-secondary); text-align: center; }
:deep(button:focus-visible),.search-field:focus-within,select:focus-visible,.log-stream:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (max-width: 600px) { .result-summary { grid-template-columns: repeat(3,minmax(0,1fr)); }.results-toolbar > .actions { margin-left: 0; }.search-field { max-width: none; }.result-row { align-items: flex-start; }.result-main { flex-basis: 100%; }.result-list { max-height: 44vh; }.connection-warning { flex-wrap: wrap; }input { font-size: 16px !important; }.footer-actions > .actions { flex-wrap: wrap; }:deep(.btn) { min-height: 40px; } }
@media (prefers-reduced-motion: reduce) { *, :deep(*) { animation: none !important; transition: none !important; } }
</style>
