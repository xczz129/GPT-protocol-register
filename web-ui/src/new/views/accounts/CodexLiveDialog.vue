<script setup lang="ts">
/* --- 接码/导入实时进度弹窗：日志流 + 逐账号结果（可展开日志、复制 RT/AT、停止、重试失败） --- */
import { computed, nextTick, ref, watch } from "vue";
import { Check, Clipboard, RefreshCw, ScrollText, Smartphone, Upload, X } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { useCopy } from "../../composables/useCopy";
import type { CodexResultItem } from "../../api/batchops";

const emit = defineEmits<{ history: [] }>();

const batch = useBatchOpsStore();
const ui = useUiStore();
const { copiedKey, copyText } = useCopy();

const logsEl = ref<HTMLElement | null>(null);
const expandedLogs = ref<Record<string, boolean>>({});

const status = computed(() => batch.codex);
const isImport = computed(() => status.value.mode === "import");
const failedCount = computed(() => Number(status.value.failed || 0));
const canRetry = computed(() => isImport.value && !status.value.running && failedCount.value > 0);

watch(() => status.value.logs?.length, async () => {
  await nextTick();
  if (logsEl.value) logsEl.value.scrollTop = logsEl.value.scrollHeight;
});

watch(() => batch.codexDialogOpen, (value) => {
  if (!value) expandedLogs.value = {};
});

function logClass(level: string) {
  if (level === "error") return "log-error";
  if (level === "warning") return "log-warning";
  if (level === "ok") return "log-ok";
  return "";
}

function toggleLogs(item: CodexResultItem, index: number) {
  const key = `${item.email}-${index}`;
  expandedLogs.value = { ...expandedLogs.value, [key]: !expandedLogs.value[key] };
}

function isExpanded(item: CodexResultItem, index: number) {
  return Boolean(expandedLogs.value[`${item.email}-${index}`]);
}

function shortToken(token: string) {
  const text = String(token || "");
  return text.length > 26 ? `${text.slice(0, 14)}…${text.slice(-8)}` : text;
}

async function copyToken(item: CodexResultItem) {
  const text = String(item.refresh_token || "");
  if (!text) return;
  const kind = item.token_kind === "at" ? "AT" : "RT";
  const ok = await copyText(text, `codex-${item.email}`);
  ui.notify(ok ? `已复制 ${item.email} 的 ${kind}` : "复制失败", ok ? "success" : "error", 1500);
}

async function retryOne(item: CodexResultItem) {
  try {
    await batch.retryImportFailed(String(item.email || ""));
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "重试失败", "error");
  }
}

async function retryAll() {
  try {
    await batch.retryImportFailed();
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "重试失败", "error");
  }
}

function close() {
  batch.codexDialogOpen = false;
  batch.stopCodexPolling();
}
</script>

<template>
  <AppModal :open="batch.codexDialogOpen" :title="isImport ? '协议登录进度' : '接码进度'" :subtitle="isImport ? '协议登录(密码 + TOTP) · 关闭后后台继续运行' : '自动接码 · 关闭后后台继续运行'" width="720px" @close="close">
    <template #icon>
      <Upload v-if="isImport" :size="18" />
      <Smartphone v-else :size="18" />
    </template>

    <div class="codex-toolbar">
      <span class="codex-stats">
        {{ status.done || 0 }} / {{ status.total || 0 }} · 成功 {{ status.success || 0 }} · 失败 {{ status.failed || 0 }}
        <template v-if="status.running"> · <b class="running-text">运行中…</b></template>
        <template v-else> · 已结束</template>
      </span>
      <div class="toolbar-btns">
        <button v-if="isImport" class="btn-ghost sm" type="button" @click="emit('history')">
          <ScrollText :size="13" />历史日志
        </button>
        <button v-if="canRetry" class="btn-ghost sm" type="button" @click="retryAll">
          <RefreshCw :size="13" />重试失败项 ({{ failedCount }})
        </button>
        <button v-if="status.running" class="btn-danger sm" type="button" @click="batch.stopCodex()">停止</button>
      </div>
    </div>

    <div class="codex-body">
      <div v-if="(status.results || []).length" class="result-list">
        <div
          v-for="(item, index) in status.results"
          :key="`${item.email}-${index}`"
          :class="['result-row', item.status === 'success' ? 'ok' : 'bad']"
        >
          <div class="result-main">
            <b>{{ item.email }}</b>
            <small v-if="item.status === 'success'">
              {{ item.token_kind === "at" ? "AT" : "RT" }} {{ shortToken(item.refresh_token || "") }}
              <em v-if="item.skipped" class="skip-tag">已接码·沿用已有</em>
            </small>
            <small v-else class="error-text">{{ item.error || "失败" }}</small>
          </div>
          <div class="result-btns">
            <button v-if="(item.logs || []).length" class="btn-ghost xs" type="button" @click="toggleLogs(item, index)">
              <ScrollText :size="12" />{{ isExpanded(item, index) ? "收起" : "日志" }}
            </button>
            <button v-if="isImport && item.status !== 'success' && !status.running" class="btn-ghost xs" type="button" @click="retryOne(item)">
              <RefreshCw :size="12" />重试
            </button>
            <button v-if="item.status === 'success'" class="btn-ghost xs" type="button" @click="copyToken(item)">
              <Check v-if="copiedKey === `codex-${item.email}`" :size="12" class="copied" />
              <Clipboard v-else :size="12" />
              {{ item.token_kind === "at" ? "复制 AT" : "复制 RT" }}
            </button>
          </div>
          <div v-if="isExpanded(item, index)" class="account-logs">
            <p v-for="(line, lineIndex) in item.logs || []" :key="lineIndex" :class="logClass(line.level)">
              <span class="log-time">{{ line.t }}</span>{{ line.m }}
            </p>
          </div>
        </div>
      </div>

      <div ref="logsEl" class="log-stream">
        <div v-if="!(status.logs || []).length" class="log-empty">暂无日志</div>
        <p v-for="(line, index) in status.logs || []" :key="index" :class="logClass(line.level)">
          <span class="log-time">{{ line.t }}</span>{{ line.m }}
        </p>
      </div>
    </div>

    <template #footer>
      <button class="btn-ghost" type="button" @click="close"><X :size="14" />关闭（后台继续运行）</button>
    </template>
  </AppModal>
</template>

<style scoped>
.codex-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.codex-stats {
  font-size: 0.82rem;
  color: var(--text-secondary);
}

.running-text {
  color: var(--accent);
}

.toolbar-btns {
  display: flex;
  gap: 6px;
}

.codex-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 62vh;
  overflow-y: auto;
}

.result-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.result-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  font-size: 0.82rem;
}

.result-row.ok { border-left: 3px solid var(--success); }
.result-row.bad { border-left: 3px solid var(--error); }

.result-main {
  flex: 1;
  min-width: 200px;
  display: flex;
  flex-direction: column;
}

.result-main b {
  color: var(--text-primary);
  font-weight: 500;
}

.result-main small {
  color: var(--text-muted);
  font-size: 0.72rem;
}

.error-text { color: var(--error) !important; }

.skip-tag {
  font-style: normal;
  color: var(--info);
  margin-left: 6px;
}

.result-btns {
  display: flex;
  gap: 4px;
}

.account-logs {
  flex-basis: 100%;
  margin-top: 6px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--inset-bg);
  max-height: 180px;
  overflow-y: auto;
}

.log-stream {
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--inset-bg);
  border: 1px solid var(--line-subtle);
  max-height: 220px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 0.75rem;
  line-height: 1.6;
}

.log-stream p, .account-logs p {
  color: var(--text-secondary);
  word-break: break-all;
}

.log-time {
  color: var(--text-muted);
  margin-right: 8px;
  font-variant-numeric: tabular-nums;
}

.log-empty {
  text-align: center;
  color: var(--text-muted);
  padding: 16px 0;
}

.log-error { color: var(--error) !important; }
.log-warning { color: var(--warning) !important; }
.log-ok { color: var(--success) !important; }

.btn-ghost, .btn-danger {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg);
  color: var(--text-secondary);
  cursor: pointer;
  transition: var(--transition-fast);
}

.btn-ghost:hover { background: var(--glass-bg-hover); color: var(--accent); border-color: var(--accent-border); }

.btn-danger {
  border-color: transparent;
  background: var(--error-bg);
  color: var(--error);
}

.btn-danger:hover { filter: brightness(1.05); }

.btn-ghost.sm, .btn-danger.sm { padding: 5px 10px; font-size: 0.75rem; }
.btn-ghost.xs { padding: 3px 8px; font-size: 0.72rem; }
.btn-ghost { padding: 8px 14px; font-size: 0.83rem; }

.copied { color: var(--success); }
</style>
