<script setup lang="ts">
/* --- 实时日志：SSE 日志流（status,log 事件）+ 搜索/级别过滤 + 刷新/下载/清空 --- */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Download, Eraser, RefreshCw } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { fetchLogs, logsDownloadUrl } from "../../api/ops";
import { apiUrl } from "../../api/client";
import { useUiStore } from "../../stores/ui";

const ui = useUiStore();

const logs = ref<string[]>([]);
const connected = ref(false);
const loading = ref(false);
const query = ref("");
const level = ref<"all" | "error" | "success">("all");
const box = ref<HTMLElement | null>(null);

const MAX_LINES = 2000;
const ERROR_MARKERS = ["失败", "错误", "error", "timeout", "http 4", "http 5"];
const SUCCESS_MARKERS = ["成功", "完成", "http 200"];

let source: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let attempts = 0;
let closed = false;
let seeded = false;

const filtered = computed(() =>
  logs.value.filter((line) => {
    const text = String(line || "").toLowerCase();
    if (query.value.trim() && !text.includes(query.value.trim().toLowerCase())) return false;
    if (level.value === "error" && !ERROR_MARKERS.some((m) => text.includes(m))) return false;
    if (level.value === "success" && !SUCCESS_MARKERS.some((m) => text.includes(m))) return false;
    return true;
  }),
);

function lineClass(line: string): string {
  const text = String(line || "").toLowerCase();
  if (text.includes("失败") || text.includes("error")) return "line-err";
  if (text.includes("成功") || text.includes("完成")) return "line-ok";
  return "";
}

/* 用户停留在底部附近时保持自动跟随滚动 */
watch(
  () => logs.value.length,
  async () => {
    const el = box.value;
    if (!el) return;
    const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    await nextTick();
    if (stick && box.value) box.value.scrollTop = box.value.scrollHeight;
  },
);

function connect() {
  if (closed) return;
  source?.close();
  seeded = false;
  const es = new EventSource(apiUrl(`/api/logs/stream?events=status,log&t=${Date.now()}`), {
    withCredentials: true,
  });
  source = es;
  es.onopen = () => {
    attempts = 0;
    connected.value = true;
  };
  es.addEventListener("log", (event) => {
    const data = JSON.parse((event as MessageEvent).data || "{}") as { lines?: string[] };
    const lines = Array.isArray(data.lines) ? data.lines : [];
    if (!lines.length) return;
    // 服务端按连接维护游标：每次连接的首个事件是全量快照，之后均为增量
    logs.value = seeded ? [...logs.value, ...lines].slice(-MAX_LINES) : lines.slice(-300);
    seeded = true;
  });
  es.onerror = () => {
    connected.value = false;
    es.close();
    if (closed) return;
    const delay = Math.min(2000 * 2 ** attempts, 16000);
    attempts += 1;
    reconnectTimer = setTimeout(connect, delay);
  };
}

async function refresh() {
  loading.value = true;
  try {
    const data = await fetchLogs({ limit: 300, q: query.value, level: level.value });
    logs.value = data.lines || [];
    await nextTick();
    if (box.value) box.value.scrollTop = box.value.scrollHeight;
  } catch {
    ui.notify("日志加载失败", "error");
  } finally {
    loading.value = false;
  }
}

function download() {
  window.location.href = logsDownloadUrl();
}

async function clearDisplay() {
  if (!(await ui.askConfirm("清空当前显示的日志？（仅清空面板显示，不影响服务端日志）", { title: "清空日志" }))) return;
  logs.value = [];
}

onMounted(async () => {
  connect();
  await refresh();
});

onBeforeUnmount(() => {
  closed = true;
  connected.value = false;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  source?.close();
  source = null;
});
</script>

<template>
  <section class="logs-view glass-panel">
    <div class="logs-head">
      <div class="logs-title">
        <h2 class="section-title">实时日志</h2>
        <AppTag :kind="connected ? 'success' : 'warning'" dot>{{ connected ? "实时" : "重连中" }}</AppTag>
      </div>
      <div class="logs-actions">
        <AppButton :loading="loading" @click="refresh">
          <span class="btn-inner"><RefreshCw :size="13" />刷新</span>
        </AppButton>
        <AppButton @click="download">
          <span class="btn-inner"><Download :size="13" />下载</span>
        </AppButton>
        <AppButton variant="danger" @click="clearDisplay">
          <span class="btn-inner"><Eraser :size="13" />清空显示</span>
        </AppButton>
      </div>
    </div>

    <div class="logs-filters">
      <input v-model="query" class="input logs-search" type="search" placeholder="搜索任务编号、邮箱或错误" />
      <select v-model="level" class="input logs-level">
        <option value="all">全部日志</option>
        <option value="error">仅错误</option>
        <option value="success">仅成功</option>
      </select>
      <span class="logs-count">{{ filtered.length }} / {{ logs.length }}</span>
    </div>

    <div ref="box" class="logs-box">
      <div v-if="!filtered.length" class="logs-empty">暂无匹配日志</div>
      <div v-for="(line, index) in filtered" :key="index" class="log-line" :class="lineClass(line)">{{ line }}</div>
    </div>
  </section>
</template>

<style scoped>
.logs-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 18px 20px;
  gap: 12px;
  min-height: 0;
}

.logs-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.logs-title {
  display: flex;
  align-items: center;
  gap: 10px;
}

.logs-title .section-title {
  margin-bottom: 0;
}

.logs-actions {
  display: flex;
  gap: 8px;
}

.btn-inner {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.logs-filters {
  display: flex;
  align-items: center;
  gap: 8px;
}

.logs-search {
  flex: 1;
  min-width: 180px;
}

.logs-level {
  width: 120px;
}

.logs-count {
  font-size: 0.75rem;
  color: var(--text-muted);
  white-space: nowrap;
}

.logs-box {
  flex: 1;
  min-height: 320px;
  max-height: calc(100vh - 260px);
  overflow-y: auto;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.75rem;
  line-height: 1.65;
  color: var(--text-secondary);
  word-break: break-all;
  white-space: pre-wrap;
}

.logs-empty {
  color: var(--text-muted);
  text-align: center;
  padding: 40px 0;
}

.log-line.line-err {
  color: var(--error);
}

.log-line.line-ok {
  color: var(--success);
}
</style>
