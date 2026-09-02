<script setup>
import { computed } from "vue";

const props = defineProps({ progress: { type: Object, default: () => ({ items: [] }) } });
const safeProgress = computed(() => (props.progress && typeof props.progress === "object" ? props.progress : { items: [] }));
const visible = computed(() => (Array.isArray(safeProgress.value.items) ? safeProgress.value.items : []).slice(0, 5));
function percent(item) { return Math.max(0, Math.min(100, Number(item.percent || 0))); }
function statusText(item) { return item.status === "running" ? item.stage || "处理中" : item.status === "success" ? "完成" : "失败"; }
</script>

<template>
  <section v-if="visible.length" class="operation-progress" aria-live="polite">
    <div class="operation-progress-head"><strong>操作进度</strong><span v-if="safeProgress.running">{{ safeProgress.running }} 项正在运行</span><span v-else>最近操作</span></div>
    <div class="operation-progress-list">
      <div v-for="item in visible" :key="item.key" :class="['operation-progress-row', item.status]">
        <div class="operation-progress-meta"><b>{{ item.label }}</b><span>{{ statusText(item) }}</span><strong>{{ Math.round(percent(item)) }}%</strong></div>
        <div class="operation-progress-track"><i :style="{ width: `${percent(item)}%` }"></i></div>
        <div class="operation-progress-detail"><span v-if="item.total">{{ item.done || 0 }} / {{ item.total }}</span><span>{{ item.message || `${Math.round(item.elapsed_sec || 0)}s` }}</span></div>
      </div>
    </div>
  </section>
</template>
