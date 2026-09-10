<script setup lang="ts">
/* --- 运维中心：运行统计 + 自动维护 + 接码设置 + 备份恢复 + 回收站 + 并发与安全 --- */
import { onMounted } from "vue";
import OpsMetrics from "./OpsMetrics.vue";
import OpsMaintenance from "./OpsMaintenance.vue";
import OpsSms from "./OpsSms.vue";
import OpsBackup from "./OpsBackup.vue";
import OpsSettings from "./OpsSettings.vue";
import { useOperationsStore } from "../../stores/operations";
import { useUiStore } from "../../stores/ui";

const ops = useOperationsStore();
const ui = useUiStore();

onMounted(() => {
  // 各数据源独立加载，单个失败不阻塞其余卡片
  ops.load().catch(() => ui.notify("运维统计数据加载失败", "error"));
  ops.loadTrash().catch(() => ui.notify("回收站加载失败", "error"));
  ops.loadSmsConfig().catch(() => ui.notify("接码设置加载失败", "error"));
  ops.loadSmsStats().catch(() => {
    /* 接码统计依赖历史日志，无记录时静默 */
  });
});
</script>

<template>
  <div class="ops-view">
    <OpsMetrics />
    <div class="ops-grid">
      <div class="ops-col">
        <OpsMaintenance :cards="['maintenance']" />
        <OpsBackup :cards="['backup']" />
        <OpsSettings :cards="['security']" />
      </div>
      <div class="ops-col">
        <OpsMaintenance :cards="['quota']" />
        <OpsSms />
        <OpsBackup :cards="['trash']" />
        <OpsSettings :cards="['workers']" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.ops-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* 双列瀑布流：各列内部紧密堆叠，避免网格行高不一造成卡片间空洞 */
.ops-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  align-items: start;
}

.ops-col {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}

@media (max-width: 1100px) {
  .ops-grid {
    grid-template-columns: 1fr;
  }
}
</style>
