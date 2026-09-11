<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ArrowRight, CircleAlert, ClipboardPaste, Download, Eraser, FileJson, FileText, RefreshCw, Search } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { useAccountsStore } from "../../stores/accounts";
import { useUiStore } from "../../stores/ui";
import { exportSecurityByEmail } from "../../api/accounts";
import { exportCliproxyFiles, fetchCliproxyExportPreview, type CliproxyExportFormat, type CliproxyExportItem, type CliproxyExportPreview } from "../../api/cliproxy";

const accounts = useAccountsStore();
const ui = useUiStore();
const mode = ref<"txt" | "relay">("relay");
const input = ref("");
const pasteError = ref("");
const removedColumns = ref(false);
const busy = ref(false);
const relayAll = ref(false);
const relayLoading = ref(false);
const relayError = ref("");
const relayPreview = ref<CliproxyExportPreview | null>(null);
const previewKey = ref("");
const selectedFiles = ref<string[]>([]);
const format = ref<CliproxyExportFormat>("original");
const visibleLimit = ref(100);
let requestId = 0;
let previewController: AbortController | undefined;

/** Credential columns are discarded immediately and never stored or sent. */
function emailColumns(value: string): string {
  return value.split(/\r?\n/).map((line) => line.includes("----") ? line.slice(0, line.indexOf("----")) : line).join("\n");
}
function updateInput(value: string) {
  const clean = emailColumns(value);
  removedColumns.value = clean !== value;
  input.value = clean;
  pasteError.value = "";
}
function onInput(event: Event) {
  const target = event.target as HTMLTextAreaElement;
  updateInput(target.value);
  target.value = input.value;
}
function parseEmails(value: string) {
  const emails: string[] = [];
  const seen = new Set<string>();
  const invalidLines: number[] = [];
  let duplicates = 0;
  emailColumns(String(value || "")).split(/\r?\n/).forEach((line, index) => {
    for (const raw of line.split(/[\s,，;；]+/)) {
      const item = raw.trim().replace(/^[`'"]+|[`'"]+$/g, "").toLowerCase();
      if (!item) continue;
      if (!/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(item)) { invalidLines.push(index + 1); continue; }
      if (seen.has(item)) { duplicates++; continue; }
      seen.add(item); emails.push(item);
    }
  });
  return { emails, duplicates, invalidLines };
}
const parsed = computed(() => parseEmails(input.value));
const requestedEmails = computed(() => parsed.value.emails);
const invalidInput = computed(() => parsed.value.invalidLines.length > 0);
const accountsReady = computed(() => !accounts.loading && !accounts.loadError && accounts.rows.length >= accounts.total && accounts.loadProgress.loaded >= accounts.loadProgress.total);
const accountIndex = computed(() => new Map(accounts.rows.map((item) => [String(item.email || "").trim().toLowerCase(), item])));
const matched = computed(() => accountsReady.value ? requestedEmails.value.map((email) => accountIndex.value.get(email)).filter((item): item is NonNullable<typeof item> => Boolean(item)) : []);
const complete = computed(() => matched.value.filter((item) => item.has_password && item.has_totp));
const incomplete = computed(() => matched.value.filter((item) => !item.has_password || !item.has_totp));
const notFound = computed(() => accountsReady.value ? requestedEmails.value.filter((email) => !accountIndex.value.has(email)) : []);
const canExportTxt = computed(() => mode.value === "txt" && accountsReady.value && complete.value.length > 0 && !invalidInput.value && !busy.value);

const relayRequestKey = computed(() => JSON.stringify({ all: relayAll.value, emails: relayAll.value ? [] : requestedEmails.value, invalid: relayAll.value ? 0 : parsed.value.invalidLines.length }));
const previewCurrent = computed(() => !!relayPreview.value && previewKey.value === relayRequestKey.value && !relayError.value && !relayLoading.value);
function selectable(item: CliproxyExportItem) { return item.valid && !!item.revision && !!item.file; }
const validFiles = computed(() => previewCurrent.value ? (relayPreview.value?.items || []).filter(selectable) : []);
const validFileSet = computed(() => new Set(validFiles.value.map((item) => item.file)));
const selectedRows = computed(() => validFiles.value.filter((item) => selectedFiles.value.includes(item.file)));
const selectionComplete = computed(() => selectedFiles.value.length === selectedRows.value.length && selectedFiles.value.every((file) => validFileSet.value.has(file)));
const allSelected = computed(() => validFiles.value.length > 0 && validFiles.value.every((item) => selectedFiles.value.includes(item.file)));
const someSelected = computed(() => selectedFiles.value.length > 0 && !allSelected.value);
const invalidFiles = computed(() => (relayPreview.value?.items || []).filter((item) => !selectable(item)));
const canPreview = computed(() => mode.value === "relay" && !busy.value && !relayLoading.value && (relayAll.value || (!!requestedEmails.value.length && !invalidInput.value)));
const canExportRelay = computed(() => mode.value === "relay" && previewCurrent.value && relayPreview.value?.configured && selectedFiles.value.length > 0 && selectionComplete.value && !busy.value && (relayAll.value || !invalidInput.value));
const formatDescription = computed(() => format.value === "original" ? selectedFiles.value.length > 1 ? "下载 ZIP，逐个保留原始 JSON 内容和文件名" : "直接下载选中 JSON，保留原始内容和文件名" : format.value === "merged" ? "将选中的中转记录合并为一个 CPA JSON 数组" : "将选中的中转记录转换为一个 SUB2 JSON 文件");

watch(relayRequestKey, () => {
  ++requestId; previewController?.abort();
  relayLoading.value = false; relayPreview.value = null; selectedFiles.value = []; relayError.value = ""; previewKey.value = "";
}, { flush: "sync" });
watch(mode, (value) => {
  pasteError.value = "";
  if (value === "txt" && !accounts.loading && (!accountsReady.value || !accounts.rows.length)) void accounts.load();
});

async function pasteEmails() {
  pasteError.value = "";
  try { updateInput(await navigator.clipboard.readText()); }
  catch { pasteError.value = "无法读取剪贴板，请直接在输入框粘贴邮箱。"; }
}
function clearInput() { input.value = ""; pasteError.value = ""; removedColumns.value = false; }
function toggleAllFiles() { selectedFiles.value = allSelected.value ? [] : validFiles.value.map((item) => item.file); }
async function loadRelayPreview() {
  if (!canPreview.value) return;
  const id = ++requestId;
  const key = relayRequestKey.value;
  previewController?.abort();
  const controller = new AbortController();
  previewController = controller;
  relayLoading.value = true; relayError.value = ""; relayPreview.value = null; selectedFiles.value = [];
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const result = await fetchCliproxyExportPreview({ emails: relayAll.value ? [] : [...requestedEmails.value], all: relayAll.value }, controller.signal);
    if (id !== requestId || key !== relayRequestKey.value) return;
    relayPreview.value = result; previewKey.value = key;
    selectedFiles.value = result.items.filter(selectable).map((item) => item.file);
    visibleLimit.value = 100;
    if (!result.configured) relayError.value = "尚未配置中转认证文件目录，请先到中转管理完成配置。";
  } catch (cause) {
    if (id === requestId) relayError.value = cause instanceof Error ? cause.name === "AbortError" ? "中转文件加载超时，请重试。" : cause.message : "中转文件加载失败";
  } finally { clearTimeout(timeout); if (id === requestId) relayLoading.value = false; }
}
async function doExport() {
  if (!canExportTxt.value) return;
  busy.value = true;
  try {
    const result = await exportSecurityByEmail([...requestedEmails.value]);
    ui.notify(`已导出 ${result.exported} 个账号${result.missing ? `，${result.missing} 个未匹配或缺少凭据` : ""}`, result.missing ? "warning" : "success");
  } catch (cause) { ui.notify(cause instanceof Error ? cause.message : "凭据导出失败", "error"); }
  finally { busy.value = false; }
}
async function doRelayExport() {
  if (!canExportRelay.value) return;
  busy.value = true; relayError.value = "";
  const revisions = Object.fromEntries(selectedRows.value.map((item) => [item.file, item.revision]));
  try {
    const result = await exportCliproxyFiles([...selectedFiles.value], format.value, revisions);
    ui.notify(`已下载 ${result.filename} · ${result.exported} 个中转文件`, "success");
  } catch (cause) {
    relayError.value = `${cause instanceof Error ? cause.message : "中转文件导出失败"} 请重新加载文件列表后再导出。`;
    relayPreview.value = null; selectedFiles.value = [];
  } finally { busy.value = false; }
}
function fileSize(value: number) { return value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`; }
onMounted(() => { if (mode.value === "txt" && !accounts.loading && !accounts.rows.length) void accounts.load(); });
onBeforeUnmount(() => { ++requestId; previewController?.abort(); input.value = ""; });
</script>

<template>
  <section class="export-panel glass-panel">
    <header class="panel-head"><div><h2>凭据导出</h2><p>按用途选择格式，再核对本次导出范围。</p></div><span class="source-label">{{ mode === 'relay' ? '来源：中转认证目录' : '来源：账号库' }}</span></header>
    <div class="export-modes" role="group" aria-label="选择导出内容">
      <button type="button" :class="{ selected: mode === 'relay' }" :aria-pressed="mode === 'relay'" :disabled="busy" @click="mode = 'relay'"><FileJson :size="21" /><span><b>中转 JSON</b><small>真实中转文件 · 原文件或兼容格式</small></span><ArrowRight :size="16" /></button>
      <button type="button" :class="{ selected: mode === 'txt' }" :aria-pressed="mode === 'txt'" :disabled="busy" @click="mode = 'txt'"><FileText :size="21" /><span><b>账号凭据 TXT</b><small>邮箱 · 密码 · 2FA</small></span><ArrowRight :size="16" /></button>
    </div>
    <fieldset v-if="mode === 'relay'" class="scope-options" :disabled="busy || relayLoading"><legend>查找范围</legend><label><input v-model="relayAll" type="radio" :value="false" name="relay-export-scope" />按邮箱查找</label><label><input v-model="relayAll" type="radio" :value="true" name="relay-export-scope" />列出全部中转文件</label><small>列出后可逐个勾选，本次只导出已选文件。</small></fieldset>
    <div v-if="mode === 'txt' || !relayAll" class="editor">
      <label for="credential-export-emails">邮箱列表</label><textarea id="credential-export-emails" :value="input" class="input" rows="5" spellcheck="false" autocomplete="off" :disabled="busy" placeholder="每行一个邮箱，也支持逗号、空格分隔&#10;粘贴 邮箱----密码----2FA 时仅保留邮箱" @input="onInput"></textarea>
      <div class="editor-actions"><AppButton size="sm" :disabled="busy" @click="pasteEmails"><ClipboardPaste :size="14" />粘贴邮箱</AppButton><AppButton size="sm" :disabled="busy || !input" @click="clearInput"><Eraser :size="14" />清空</AppButton><span class="parse-summary">有效邮箱 <b>{{ requestedEmails.length }}</b><template v-if="parsed.duplicates"> · 重复 {{ parsed.duplicates }} 条已合并</template></span></div>
      <p v-if="removedColumns" class="field-note">已提取邮箱，密码和 2FA 字段已从输入中移除。</p><p v-if="invalidInput" class="input-error" role="alert">有 {{ parsed.invalidLines.length }} 个无效条目（第 {{ [...new Set(parsed.invalidLines)].slice(0, 10).join('、') }} 行），请修正或移除后继续。</p><p v-if="pasteError" class="input-error" role="alert">{{ pasteError }}</p>
    </div>

    <template v-if="mode === 'relay'">
      <div class="preview-action"><p>{{ relayAll ? '从全部中转 JSON 文件中选择。' : '按邮箱直接匹配中转文件，不要求账号同时存在于账号库。' }}同一邮箱的不同工作区会分别列出。</p><AppButton variant="accent" :disabled="!canPreview" :loading="relayLoading" @click="loadRelayPreview"><Search :size="14" />{{ relayAll ? '加载全部中转文件' : '查找中转文件' }}</AppButton></div>
      <p v-if="relayError" class="error-box" role="alert"><CircleAlert :size="17" />{{ relayError }}</p>
      <div v-if="relayLoading" class="empty-hint" role="status">正在读取中转文件目录…</div>
      <template v-else-if="relayPreview && previewCurrent">
        <div class="summary" aria-live="polite"><span><b>{{ relayPreview.matched_emails }}</b> 匹配邮箱</span><span><b>{{ validFiles.length }}</b> 可导出文件</span><span class="selected-count"><b>{{ selectedFiles.length }}</b> 已选择</span><span v-if="relayPreview.not_found.length"><b>{{ relayPreview.not_found.length }}</b> 个邮箱无匹配</span><span v-if="invalidFiles.length"><b>{{ invalidFiles.length }}</b> 个文件不可导出</span></div>
        <details v-if="relayPreview.not_found.length" class="missing-list"><summary>查看未匹配邮箱（不会扩大到全部文件）</summary><p v-for="email in relayPreview.not_found" :key="email">{{ email }}</p></details>
        <div v-if="relayPreview.items.length" class="relay-files"><div class="files-head"><label><input type="checkbox" :checked="allSelected" :indeterminate="someSelected" :disabled="busy || !validFiles.length" @change="toggleAllFiles" />选择全部可导出文件</label><span>{{ relayAll ? '全部中转文件' : '当前邮箱匹配结果' }}</span></div>
          <label v-for="item in relayPreview.items.slice(0, visibleLimit)" :key="item.file" :class="['file-row', { invalid: !selectable(item), checked: selectedFiles.includes(item.file) }]"><input v-model="selectedFiles" type="checkbox" :value="item.file" :disabled="busy || !selectable(item)" :aria-label="`选择文件 ${item.file}，${item.email || '未知邮箱'}，工作区 ${item.account_id || '未记录'}`" /><span class="file-main"><b>{{ item.file }}</b><span>{{ item.email || '无法读取邮箱' }}</span><small v-if="!selectable(item)" class="file-error">{{ item.error || '文件校验信息不完整，请重新加载' }}</small><small v-else>工作区 {{ item.account_id || '未记录' }}</small></span><span class="file-meta"><AppTag :kind="!selectable(item) ? 'error' : 'default'">{{ !selectable(item) ? '不可导出' : item.plan_type || '套餐未记录' }}</AppTag><small>{{ fileSize(item.size_bytes) }}</small></span></label>
          <div v-if="relayPreview.items.length > visibleLimit" class="show-more"><AppButton size="sm" @click="visibleLimit += 100">再显示 {{ Math.min(100, relayPreview.items.length - visibleLimit) }} 个文件</AppButton><span>全选会包含尚未展开的可导出文件。</span></div>
        </div><div v-else class="empty-hint">没有找到中转文件，请核对邮箱或选择“列出全部中转文件”。</div>
      </template><div v-else-if="!relayLoading && !relayError" class="empty-hint">{{ relayAll ? '点击加载，核对全部中转文件后再导出。' : '输入邮箱并查找，预览将列出实际 JSON 文件。' }}</div>
      <div class="relay-format"><label for="relay-export-format">下载格式</label><select id="relay-export-format" v-model="format" class="input" :disabled="busy"><option value="original">原始 JSON（多个文件打包 ZIP）</option><option value="merged">合并 CPA JSON</option><option value="sub2">SUB2 JSON</option></select><small>{{ formatDescription }}</small></div>
      <footer class="export-footer"><p>仅下载勾选的 <b>{{ selectedFiles.length }}</b> 个文件。文件在预览后发生变更时，需重新加载核对。</p><AppButton variant="primary" :disabled="!canExportRelay" :loading="busy" :aria-label="`下载 ${selectedFiles.length} 个已选中转文件`" @click="doRelayExport"><Download :size="15" />下载 {{ selectedFiles.length }} 个文件</AppButton></footer>
    </template>

    <template v-else>
      <div v-if="!accountsReady" class="loading-box" role="status"><CircleAlert v-if="accounts.loadError" :size="17" /><RefreshCw v-else :size="17" /><span>{{ accounts.loadError ? `账号加载失败：${accounts.loadError}` : accounts.loading ? `正在载入账号 ${accounts.loadProgress.loaded} / ${accounts.loadProgress.total}，完成后显示准确匹配结果。` : '账号尚未加载完整，请重新载入后导出。' }}</span><AppButton size="sm" :disabled="accounts.loading" @click="accounts.load()">重新载入</AppButton></div>
      <template v-else>
        <div class="summary" aria-live="polite"><span>匹配 <b>{{ matched.length }}</b></span><span class="selected-count">可导出 <b>{{ complete.length }}</b></span><span v-if="incomplete.length">缺少密码或 2FA <b>{{ incomplete.length }}</b></span><span v-if="notFound.length">未找到 <b>{{ notFound.length }}</b></span><span class="loaded-count">账号库共 {{ accounts.rows.length }} 条</span></div>
        <details v-if="notFound.length" class="missing-list"><summary>查看未匹配邮箱</summary><p v-for="email in notFound" :key="email">{{ email }}</p></details>
        <div v-if="matched.length" class="table-wrap"><table><thead><tr><th>邮箱</th><th>账号状态</th><th>可导出内容</th></tr></thead><tbody><tr v-for="item in matched" :key="item.delete_key || item.email"><td><strong>{{ item.email }}</strong></td><td><AppTag :kind="item.is_deactivated ? 'error' : 'default'">{{ item.is_deactivated ? '已封号' : item.status || '已匹配' }}</AppTag></td><td>{{ item.has_password && item.has_totp ? '邮箱 / 密码 / 2FA' : [item.has_password ? '' : '缺密码', item.has_totp ? '' : '缺 2FA'].filter(Boolean).join('、') }}</td></tr></tbody></table></div><div v-else class="empty-hint">{{ requestedEmails.length ? '账号库中没有匹配邮箱。中转 JSON 可切换到另一种导出方式查找。' : '输入邮箱后显示匹配结果。' }}</div>
      </template>
      <footer class="export-footer"><p>格式：邮箱----密码----2FA。只导出凭据齐全的 {{ complete.length }} 个账号。</p><AppButton variant="primary" :disabled="!canExportTxt" :loading="busy" @click="doExport"><Download :size="15" />导出 {{ complete.length }} 个账号</AppButton></footer>
    </template>
  </section>
</template>

<style scoped>
.export-panel { padding: 20px; display: flex; flex-direction: column; gap: 16px; }.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.panel-head h2 { font-size: 1.08rem; color: var(--text-primary); }.panel-head p { color: var(--text-secondary); font-size: .79rem; margin-top: 5px; }.source-label { color: var(--text-secondary); font-size: .73rem; padding: 6px 9px; border: 1px solid var(--field-border); background: var(--field-bg); border-radius: 6px; white-space: nowrap; }
.export-modes { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }.export-modes button { display: flex; align-items: center; gap: 12px; border: 1px solid var(--field-border); background: var(--glass-bg); border-radius: var(--radius-sm); color: var(--text-secondary); cursor: pointer; padding: 14px; text-align: left; }.export-modes button.selected { border-color: var(--accent); background: var(--accent-bg); color: var(--accent); }.export-modes button > span { flex: 1; display: grid; gap: 4px; }.export-modes b { font-size: .86rem; color: var(--text-primary); }.export-modes small { font-size: .72rem; color: var(--text-secondary); }
.scope-options { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 20px; border: 0; padding: 0; margin: 0; min-width: 0; color: var(--text-secondary); }.scope-options legend { font-size: .8rem; color: var(--text-primary); margin-bottom: 8px; }.scope-options label { display: flex; align-items: center; gap: 7px; font-size: .8rem; min-height: 32px; cursor: pointer; }.scope-options small { font-size: .72rem; }input[type='radio'],input[type='checkbox'] { accent-color: var(--accent); height: 16px; width: 16px; flex-shrink: 0; }
.editor { display: flex; flex-direction: column; gap: 8px; }.editor > label { font-size: .8rem; color: var(--text-primary); }.editor textarea { resize: vertical; font-family: var(--mono); font-size: .8rem; line-height: 1.65; min-height: 130px; max-height: 400px; }.editor-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }.parse-summary { font-size: .76rem; margin-left: auto; color: var(--text-secondary); }.parse-summary b { color: var(--text-primary); }.field-note,.input-error { font-size: .75rem; line-height: 1.5; color: var(--text-secondary); }.input-error { color: var(--text-primary); background: var(--error-bg); padding: 8px 10px; border-radius: 6px; }
.preview-action { display: flex; align-items: center; justify-content: space-between; gap: 12px; }.preview-action p { color: var(--text-secondary); font-size: .76rem; line-height: 1.6; }.summary { display: flex; gap: 10px 22px; flex-wrap: wrap; font-size: .78rem; color: var(--text-secondary); }.summary b { font-size: .91rem; font-variant-numeric: tabular-nums; color: var(--text-primary); }.summary .selected-count b { color: var(--accent); }.loaded-count { margin-left: auto; font-size: .72rem; }.missing-list { border: 1px solid var(--field-border); border-radius: var(--radius-sm); padding: 9px 12px; font-size: .75rem; color: var(--text-secondary); }.missing-list summary { cursor: pointer; color: var(--text-primary); }.missing-list p { margin-top: 6px; overflow-wrap: anywhere; }
.relay-files { border: 1px solid var(--field-border); border-radius: var(--radius-sm); max-height: 46vh; overflow: auto; }.files-head { position: sticky; top: 0; z-index: 2; padding: 10px 12px; border-bottom: 1px solid var(--field-border); background: var(--field-bg); display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--text-secondary); font-size: .73rem; }.files-head label { display: flex; align-items: center; gap: 10px; cursor: pointer; min-height: 30px; }.file-row { display: flex; align-items: center; gap: 12px; padding: 12px; border-bottom: 1px solid var(--field-border); cursor: pointer; }.file-row:last-child { border-bottom: none; }.file-row.checked { background: var(--accent-bg); }.file-row.invalid { cursor: default; }.file-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; overflow-wrap: anywhere; }.file-main b { font: 500 .76rem var(--mono); color: var(--text-primary); }.file-main > span { font-size: .76rem; color: var(--text-primary); }.file-main small { font-size: .7rem; color: var(--text-secondary); }.file-main .file-error { color: var(--text-primary); }.file-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; color: var(--text-secondary); font-size: .69rem; }.show-more { padding: 10px 12px; display: flex; align-items: center; gap: 10px; color: var(--text-secondary); font-size: .72rem; }
.relay-format { display: grid; grid-template-columns: auto minmax(180px,330px) 1fr; align-items: center; gap: 10px 16px; padding-top: 2px; }.relay-format label { color: var(--text-primary); font-size: .8rem; }.relay-format select { font-size: .78rem; min-height: 42px; }.relay-format small { font-size: .74rem; color: var(--text-secondary); line-height: 1.5; }.export-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; border-top: 1px solid var(--field-border); padding-top: 14px; }.export-footer p { font-size: .75rem; color: var(--text-secondary); line-height: 1.6; }.export-footer b { color: var(--text-primary); }
.empty-hint { padding: 30px 16px; text-align: center; font-size: .79rem; color: var(--text-secondary); border: 1px dashed var(--field-border); border-radius: var(--radius-sm); line-height: 1.6; }.error-box,.loading-box { padding: 12px; display: flex; align-items: center; gap: 10px; background: var(--error-bg); border-radius: var(--radius-sm); color: var(--text-primary); font-size: .78rem; line-height: 1.5; }.error-box svg,.loading-box svg { flex-shrink: 0; }.loading-box { background: var(--accent-bg); }.loading-box > span { flex: 1; }.table-wrap { overflow: auto; max-height: 45vh; border: 1px solid var(--field-border); border-radius: var(--radius-sm); }table { width: 100%; border-collapse: collapse; color: var(--text-secondary); font-size: .78rem; }th,td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--field-border); }th { color: var(--text-primary); font-size: .73rem; background: var(--field-bg); position: sticky; top: 0; }td strong { color: var(--text-primary); font-weight: 500; overflow-wrap: anywhere; }
:deep(button:focus-visible),input:focus-visible,textarea:focus-visible,select:focus-visible,summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }button:disabled { cursor: not-allowed; opacity: .6; }
@media (max-width: 700px) { .export-panel { padding: 14px; }.panel-head { flex-wrap: wrap; }.export-modes { grid-template-columns: 1fr; }.preview-action { align-items: stretch; flex-direction: column; }.relay-format { grid-template-columns: 1fr; gap: 6px; }.relay-format select,.editor textarea { font-size: 16px; }.file-row { align-items: flex-start; gap: 9px; }.file-meta { max-width: 90px; }.files-head > span { display: none; }.parse-summary { width: 100%; margin-left: 0; }.show-more,.loading-box { flex-wrap: wrap; }.export-footer { align-items: stretch; flex-direction: column; }:deep(.btn) { min-height: 40px; } }
@media (prefers-reduced-motion: reduce) { *, :deep(*) { transition: none !important; animation: none !important; } }
</style>
