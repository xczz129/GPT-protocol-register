<script setup lang="ts">
import { computed } from "vue";
import { ChevronRight, History, RefreshCw, Smartphone, Upload, WifiOff } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useBatchOpsStore } from "../../stores/batchops";

const emit = defineEmits<{ importHistory: [] }>();
const batch = useBatchOpsStore();
const isImport = computed(() => batch.codex.mode === "import");
const hasTask = computed(() => batch.codexSummary.total > 0 || batch.codex.running);
const offline = computed(() => batch.codexConnection !== "connected");
const state = computed(() => offline.value ? "同步中断，正在重连" : batch.codex.stopping ? "正在停止" : batch.codex.running ? "运行中" : hasTask.value ? batch.codex.status === "cancelled" ? "已停止" : "已结束" : "暂无运行任务");
const progress = computed(() => batch.codexSummary.total ? Math.min(1, batch.codexSummary.done / batch.codexSummary.total) : 0);
const lastSync = computed(() => batch.codexLastUpdated ? `最近同步 ${new Date(batch.codexLastUpdated).toLocaleTimeString()}` : "等待首次同步");
function openHistory() { if (isImport.value) emit("importHistory"); else batch.openCodexHistory(); }
</script>

<template>
  <section :class="['sms-taskbar', 'glass-panel', { active: batch.codex.running, offline }]" aria-label="接码与导入任务">
    <button type="button" class="task-entry" @click="batch.openCodexDialog()">
      <span class="task-icon"><WifiOff v-if="offline" :size="17" /><Upload v-else-if="isImport" :size="17" /><Smartphone v-else :size="17" /></span>
      <span class="task-title"><b>{{ isImport ? '导入任务' : '接码与中转' }}<em>{{ state }}</em></b><small v-if="hasTask">{{ batch.codexSummary.done }}/{{ batch.codexSummary.total }} 已处理 · {{ isImport ? '成功' : '授权同步' }} {{ batch.codexSummary.newSuccess }}<template v-if="!isImport"> · 补同步 {{ batch.codexSummary.synced }}</template> · {{ isImport ? '沿用' : '中转已有' }} {{ batch.codexSummary.reused }} · 失败 {{ batch.codexSummary.failed }}<template v-if="batch.codexSummary.cancelled"> · 取消 {{ batch.codexSummary.cancelled }}</template><template v-if="batch.codexSummary.pending"> · 未执行 {{ batch.codexSummary.pending }}</template></small><small v-else>任务开始后可在这里随时查看进度与结果</small></span>
      <ChevronRight :size="16" class="entry-arrow" />
    </button>
    <span v-if="offline" class="last-sync" role="status">{{ lastSync }}</span>
    <div class="task-actions"><AppButton v-if="offline" size="sm" @click="batch.refreshCodexStatus()"><RefreshCw :size="14" />重连</AppButton><AppButton size="sm" @click="openHistory"><History :size="14" />{{ isImport ? '导入历史' : '接码历史' }}</AppButton><AppButton v-if="isImport" size="sm" @click="batch.openCodexHistory()"><Smartphone :size="14" />接码历史</AppButton></div>
    <div v-if="hasTask" class="task-progress" aria-hidden="true"><span :style="{ transform: `scaleX(${progress})` }"></span></div>
  </section>
</template>

<style scoped>
.sms-taskbar { position: sticky; top: 10px; z-index: 22; padding: 10px 14px; display: flex; align-items: center; flex-wrap: wrap; gap: 10px 14px; overflow: hidden; border-radius: var(--radius-sm); }.sms-taskbar.active { border-color: var(--accent-border); }.task-entry { display: flex; flex: 1; align-items: center; gap: 10px; min-width: 0; min-height: 42px; text-align: left; background: none; border: 0; color: var(--text-secondary); cursor: pointer; border-radius: 6px; padding: 0; }.task-icon { height: 32px; width: 32px; flex-shrink: 0; display: grid; place-items: center; background: var(--accent-bg); color: var(--accent); border-radius: 8px; }.offline .task-icon { background: var(--warning-bg); color: var(--warning); }.task-title { min-width: 0; display: grid; gap: 4px; }.task-title b { font-size: .8rem; font-weight: 600; color: var(--text-primary); display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }.task-title em { font-style: normal; font-weight: 400; font-size: .69rem; color: var(--text-secondary); }.active .task-title em { color: var(--accent); }.task-title small,.last-sync { font-size: .72rem; color: var(--text-secondary); line-height: 1.5; }.entry-arrow { margin-left: 6px; flex-shrink: 0; }.task-actions { display: flex; gap: 6px; flex-wrap: wrap; }.task-progress { position: absolute; height: 2px; background: var(--progress-track); bottom: 0; left: 0; width: 100%; }.task-progress span { display: block; width: 100%; height: 100%; background: var(--accent); transform-origin: left; transition: transform .3s ease; }.task-entry:focus-visible,:deep(button:focus-visible) { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 600px) { .sms-taskbar { top: 6px; padding: 8px 10px; gap: 6px; }.task-entry { flex-basis: 100%; }.task-actions { margin-left: 42px; }.task-title small { font-size: .7rem; }.last-sync { margin-left: 42px; }:deep(.btn) { min-height: 36px; } }
@media (prefers-reduced-motion: reduce) { *, :deep(*) { animation: none !important; transition: none !important; } }
</style>
