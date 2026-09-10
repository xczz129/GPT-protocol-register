<script setup>
import { computed, ref, watch } from "vue";
import { History, ScrollText, Search, Users, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  loading: Boolean,
  entries: { type: Array, default: () => [] },
});
const emit = defineEmits(["close"]);

const query = ref("");
const expandedLogs = ref({});

watch(() => props.open, (value) => {
  if (value) { query.value = ""; expandedLogs.value = {}; }
});

/* 逐账号条目按批次分组（后端已按时间倒序返回） */
const batches = computed(() => {
  const needle = query.value.trim().toLowerCase();
  const groups = [];
  const byId = new Map();
  for (const entry of props.entries || []) {
    if (needle && !String(entry.email || "").toLowerCase().includes(needle)) continue;
    const id = String(entry.batch_id || "") || "unknown";
    let group = byId.get(id);
    if (!group) {
      group = {
        id,
        at: String(entry.at || ""),
        account_type: String(entry.account_type || "personal"),
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

function batchTitle(group) {
  const type = group.account_type === "team"
    ? `Team${group.workspace ? `「${group.workspace}」` : ""}`
    : "个人";
  return `${type} · 共 ${group.items.length} · 成功 ${group.success} · 失败 ${group.failed}`;
}

function toggleLogs(groupId, item, index) {
  const key = `${groupId}-${item.email}-${index}`;
  expandedLogs.value = { ...expandedLogs.value, [key]: !expandedLogs.value[key] };
}

function isExpanded(groupId, item, index) {
  return Boolean(expandedLogs.value[`${groupId}-${item.email}-${index}`]);
}

function logClass(level) {
  if (level === "error") return "codex-log-error";
  if (level === "warning") return "codex-log-warning";
  if (level === "ok") return "codex-log-ok";
  return "";
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('close')">
        <section class="app-dialog import-history-dialog" role="dialog" aria-modal="true" aria-label="导入历史日志" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div>
              <span class="app-dialog-kicker"><History :size="12" aria-hidden="true" />导入账号</span>
              <h2>历史日志</h2>
              <small>按批次查看每个账号的完整协议登录日志（重启/换批后不丢失）</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>

          <div class="import-history-search">
            <Search :size="13" aria-hidden="true" />
            <input v-model="query" type="text" placeholder="按邮箱搜索…" spellcheck="false" autocomplete="off" />
          </div>

          <div class="import-history-body">
            <div v-if="loading" class="import-history-empty">正在加载历史日志…</div>
            <div v-else-if="!batches.length" class="import-history-empty">
              {{ (entries || []).length ? "没有匹配该邮箱的记录" : "暂无导入历史,完成一次导入后这里会按批次展示每个账号的日志" }}
            </div>

            <div v-for="group in batches" :key="group.id" class="import-history-batch">
              <div class="import-history-batch-head">
                <span class="import-history-batch-time">{{ group.at.replace("T", " ") }}</span>
                <span class="import-history-batch-title">
                  <Users v-if="group.account_type === 'team'" :size="12" aria-hidden="true" />
                  {{ batchTitle(group) }}
                </span>
              </div>
              <div class="codex-oauth-results">
                <div v-for="(item, index) in group.items" :key="`${group.id}-${item.email}-${index}`" :class="['codex-result-row', item.status === 'success' ? 'ok' : 'bad', { expanded: isExpanded(group.id, item, index) }]">
                  <div class="codex-result-main">
                    <b>{{ item.email }}</b>
                    <small v-if="item.status === 'success'">导入成功</small>
                    <small v-else>{{ item.error || "导入失败" }}</small>
                  </div>
                  <div class="row-btns">
                    <button v-if="(item.logs || []).length" class="btn secondary sm" type="button" :title="isExpanded(group.id, item, index) ? '收起该账号日志' : '展开该账号日志'" @click="toggleLogs(group.id, item, index)"><ScrollText :size="12" aria-hidden="true" />{{ isExpanded(group.id, item, index) ? "收起日志" : "日志" }}</button>
                  </div>
                </div>
                <template v-for="(item, index) in group.items">
                  <div v-if="isExpanded(group.id, item, index)" :key="`logs-${group.id}-${item.email}-${index}`" class="codex-result-log-block import-history-log-block">
                    <div class="codex-result-log-title">{{ item.email }} · {{ (item.logs || []).length }} 条</div>
                    <p v-for="(line, lineIndex) in item.logs || []" :key="lineIndex" :class="logClass(line.level)"><span class="codex-log-time">{{ line.t }}</span>{{ line.m }}</p>
                  </div>
                </template>
              </div>
            </div>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style>
.import-history-dialog {
  max-width: 760px;
  width: min(760px, 94vw);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
}

.import-history-search {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px 0 4px;
  padding: 7px 10px;
  border: 1px solid var(--line, #e2e8f0);
  border-radius: 10px;
  color: var(--muted, #8a94a6);
}

.import-history-search input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  font-size: 13px;
  color: inherit;
}

.import-history-body {
  overflow-y: auto;
  margin-top: 10px;
  padding-right: 4px;
}

.import-history-empty {
  padding: 40px 0;
  text-align: center;
  color: var(--muted, #8a94a6);
  font-size: 13px;
}

.import-history-batch {
  margin-bottom: 14px;
}

.import-history-batch-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 2px;
  font-size: 12px;
  color: var(--muted, #8a94a6);
  position: sticky;
  top: 0;
  background: var(--card, #fff);
  z-index: 1;
}

.import-history-batch-time {
  font-variant-numeric: tabular-nums;
}

.import-history-batch-title {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 600;
  color: var(--ink, #1f2937);
}

.import-history-log-block {
  margin: 4px 0 8px;
}
</style>
