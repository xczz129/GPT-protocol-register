<script setup lang="ts">
/* --- 导入历史日志弹窗：按批次分组，逐账号展开完整登录日志，支持邮箱搜索 --- */
import { computed, ref, watch } from "vue";
import { History, ScrollText, Search, Users } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import { fetchImportHistory, type ImportHistoryEntry } from "../../api/batchops";
import { useUiStore } from "../../stores/ui";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const ui = useUiStore();
const loading = ref(false);
const entries = ref<ImportHistoryEntry[]>([]);
const query = ref("");
const expandedLogs = ref<Record<string, boolean>>({});

watch(() => props.open, async (value) => {
  if (!value) return;
  query.value = "";
  expandedLogs.value = {};
  loading.value = true;
  try {
    const result = await fetchImportHistory(300);
    entries.value = result.entries || [];
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "历史日志加载失败", "error");
  } finally {
    loading.value = false;
  }
});

interface BatchGroup {
  id: string;
  at: string;
  accountType: string;
  workspace: string;
  items: ImportHistoryEntry[];
  success: number;
  failed: number;
}

const batches = computed<BatchGroup[]>(() => {
  const needle = query.value.trim().toLowerCase();
  const groups: BatchGroup[] = [];
  const byId = new Map<string, BatchGroup>();
  for (const entry of entries.value) {
    if (needle && !String(entry.email || "").toLowerCase().includes(needle)) continue;
    const id = String(entry.batch_id || "") || "unknown";
    let group = byId.get(id);
    if (!group) {
      group = {
        id,
        at: String(entry.at || "").replace("T", " "),
        accountType: String(entry.account_type || "personal"),
        workspace: String(entry.workspace || ""),
        items: [],
        success: 0,
        failed: 0,
      };
      byId.set(id, group);
      groups.push(group);
    }
    group.items.push(entry);
    if (entry.status === "success") group.success += 1;
    else group.failed += 1;
  }
  return groups;
});

function toggle(groupId: string, item: ImportHistoryEntry, index: number) {
  const key = `${groupId}-${item.email}-${index}`;
  expandedLogs.value = { ...expandedLogs.value, [key]: !expandedLogs.value[key] };
}

function isExpanded(groupId: string, item: ImportHistoryEntry, index: number) {
  return Boolean(expandedLogs.value[`${groupId}-${item.email}-${index}`]);
}

function logClass(level: string) {
  if (level === "error") return "log-error";
  if (level === "warning") return "log-warning";
  if (level === "ok") return "log-ok";
  return "";
}
</script>

<template>
  <AppModal :open="open" title="导入历史日志" subtitle="按批次查看每个账号的完整协议登录日志（重启/换批后不丢失）" width="740px" @close="emit('close')">
    <template #icon><History :size="18" /></template>

    <div class="search-bar">
      <Search :size="14" />
      <input v-model="query" type="text" placeholder="按邮箱搜索…" spellcheck="false" autocomplete="off" />
    </div>

    <div v-if="loading" class="empty-state">正在加载历史日志…</div>
    <div v-else-if="!batches.length" class="empty-state">
      {{ entries.length ? "没有匹配该邮箱的记录" : "暂无导入历史，完成一次导入后这里会按批次展示每个账号的日志" }}
    </div>

    <div v-for="group in batches" :key="group.id" class="batch">
      <div class="batch-head">
        <span class="batch-time">{{ group.at }}</span>
        <span class="batch-title">
          <Users v-if="group.accountType === 'team'" :size="12" />
          {{ group.accountType === 'team' ? `Team${group.workspace ? `「${group.workspace}」` : ""}` : "个人" }}
          · 共 {{ group.items.length }} · 成功 {{ group.success }} · 失败 {{ group.failed }}
        </span>
      </div>
      <div
        v-for="(item, index) in group.items"
        :key="`${group.id}-${item.email}-${index}`"
        :class="['account-row', item.status === 'success' ? 'ok' : 'bad']"
      >
        <div class="account-main">
          <b>{{ item.email }}</b>
          <small :class="{ 'error-text': item.status !== 'success' }">
            {{ item.status === 'success' ? '导入成功' : (item.error || '导入失败') }}
          </small>
        </div>
        <button v-if="(item.logs || []).length" class="log-toggle" type="button" @click="toggle(group.id, item, index)">
          <ScrollText :size="12" />{{ isExpanded(group.id, item, index) ? "收起日志" : "日志" }}
        </button>
        <div v-if="isExpanded(group.id, item, index)" class="account-logs">
          <div class="logs-title">{{ item.email }} · {{ (item.logs || []).length }} 条</div>
          <p v-for="(line, lineIndex) in item.logs || []" :key="lineIndex" :class="logClass(line.level)">
            <span class="log-time">{{ line.t }}</span>{{ line.m }}
          </p>
        </div>
      </div>
    </div>
  </AppModal>
</template>

<style scoped>
.search-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--field-bg);
  color: var(--text-muted);
  margin-bottom: 12px;
}

.search-bar input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  font-size: 0.83rem;
  color: var(--text-primary);
}

.empty-state {
  padding: 40px 0;
  text-align: center;
  color: var(--text-muted);
  font-size: 0.83rem;
}

.batch { margin-bottom: 14px; }

.batch-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 2px;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.batch-time { font-variant-numeric: tabular-nums; }

.batch-title {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 600;
  color: var(--text-primary);
}

.account-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 12px;
  margin-bottom: 6px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  font-size: 0.82rem;
}

.account-row.ok { border-left: 3px solid var(--success); }
.account-row.bad { border-left: 3px solid var(--error); }

.account-main {
  flex: 1;
  min-width: 200px;
  display: flex;
  flex-direction: column;
}

.account-main b { color: var(--text-primary); font-weight: 500; }
.account-main small { color: var(--text-muted); font-size: 0.72rem; }
.error-text { color: var(--error) !important; }

.log-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 9px;
  border-radius: 7px;
  border: 1px solid var(--glass-border);
  background: none;
  color: var(--text-muted);
  font-size: 0.72rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.log-toggle:hover { color: var(--accent); border-color: var(--accent-border); }

.account-logs {
  flex-basis: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--inset-bg);
  max-height: 200px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 0.74rem;
  line-height: 1.6;
}

.logs-title {
  font-size: 0.72rem;
  color: var(--text-muted);
  margin-bottom: 6px;
}

.account-logs p { color: var(--text-secondary); word-break: break-all; }
.log-time { color: var(--text-muted); margin-right: 8px; font-variant-numeric: tabular-nums; }
.log-error { color: var(--error) !important; }
.log-warning { color: var(--warning) !important; }
.log-ok { color: var(--success) !important; }
</style>
