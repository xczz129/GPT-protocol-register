<script setup lang="ts">
/* --- 批量任务进度弹窗：重新登录/安全修复/改密/改2FA/刷新AT/试用检测 通用 --- */
import { computed } from "vue";
import { Activity } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useBatchOpsStore } from "../../stores/batchops";

const props = defineProps<{
  open: boolean;
  kind: "reauth" | "refreshAt" | "trial";
}>();

const emit = defineEmits<{ close: [] }>();

const batch = useBatchOpsStore();

const titleMap = {
  reauth: "账号批次任务",
  refreshAt: "刷新 AT",
  trial: "Plus 试用检测",
};

const operationLabels: Record<string, string> = {
  reauth: "重新登录",
  security_repair: "安全字段补全",
  password_change: "修改密码",
  totp_change: "修改 2FA",
};

const title = computed(() => {
  if (props.kind === "reauth") {
    return operationLabels[String(batch.reauth.operation || "reauth")] || "账号批次任务";
  }
  return titleMap[props.kind];
});

const stats = computed(() => {
  if (props.kind === "reauth") {
    const p = batch.reauth;
    return {
      running: p.running,
      done: p.done || 0,
      total: p.total || 0,
      current: p.current || "",
      chips: [
        { label: "成功", value: p.success || 0, cls: "ok" },
        { label: "失败", value: p.failed || 0, cls: "bad" },
        { label: "取消", value: p.cancelled || 0, cls: "" },
      ],
      message: p.lastMessage || "",
      items: (p.items || []).map((item) => ({
        email: item.email || item.key || "",
        status: item.status || "",
        message: item.message || "",
      })),
    };
  }
  if (props.kind === "refreshAt") {
    const p = batch.refreshAt;
    return {
      running: p.running,
      done: p.done || 0,
      total: p.total || 0,
      current: p.current || "",
      chips: [
        { label: "已刷新", value: p.refreshed || 0, cls: "ok" },
        { label: "未变化", value: p.unchanged || 0, cls: "" },
        { label: "失败", value: p.failed || 0, cls: "bad" },
      ],
      message: "",
      items: [],
    };
  }
  const p = batch.trial;
  return {
    running: p.running,
    done: p.done || 0,
    total: p.total || 0,
    current: p.current || "",
    chips: [
      { label: "可试用", value: p.eligible || 0, cls: "ok" },
      { label: "不符合", value: p.ineligible || 0, cls: "" },
      { label: "失败", value: p.failed || 0, cls: "bad" },
    ],
    message: p.message || "",
    items: [],
  };
});

const percent = computed(() => {
  const { done, total } = stats.value;
  return total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
});

async function stop() {
  if (props.kind === "reauth") await batch.stopReauthBatch();
  /* refresh-at / trial 的停止由后端 job 控制，这里预留 */
}
</script>

<template>
  <AppModal :open="open" :title="title" :subtitle="stats.running ? '任务运行中，关闭弹窗后台继续执行' : '任务已结束'" width="620px" @close="emit('close')">
    <template #icon><Activity :size="18" /></template>

    <div class="progress-line">
      <div class="progress-track">
        <div class="progress-fill" :class="{ running: stats.running }" :style="{ width: `${percent}%` }"></div>
      </div>
      <span class="progress-text">{{ stats.done }} / {{ stats.total }} · {{ percent }}%</span>
    </div>

    <div class="chip-row">
      <span v-for="chip in stats.chips" :key="chip.label" :class="['stat-chip', chip.cls]">
        {{ chip.label }} {{ chip.value }}
      </span>
      <span v-if="stats.running" class="stat-chip running-chip">运行中…</span>
    </div>

    <p v-if="stats.current" class="current-line">{{ stats.current }}</p>
    <p v-if="stats.message" class="message-line">{{ stats.message }}</p>

    <div v-if="stats.items.length" class="item-list">
      <div v-for="(item, index) in stats.items" :key="index" :class="['item-row', item.status]">
        <b>{{ item.email }}</b>
        <span :class="['item-status', item.status]">{{ item.status }}</span>
        <small>{{ item.message }}</small>
      </div>
    </div>

    <template #footer>
      <AppButton v-if="stats.running && kind === 'reauth'" variant="danger" @click="stop">停止任务</AppButton>
      <AppButton variant="ghost" @click="emit('close')">关闭</AppButton>
    </template>
  </AppModal>
</template>

<style scoped>
.progress-line {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.progress-track {
  flex: 1;
  height: 8px;
  border-radius: 99px;
  background: var(--inset-bg);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  border-radius: 99px;
  background: var(--accent);
  transition: width 0.4s ease;
}

.progress-fill.running {
  background: linear-gradient(90deg, var(--accent), var(--accent-lighter));
  animation: shimmer 1.4s ease infinite;
}

@keyframes shimmer {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.65; }
}

.progress-text {
  font-size: 0.78rem;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.chip-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.stat-chip {
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 0.75rem;
  background: var(--inset-bg);
  color: var(--text-secondary);
}

.stat-chip.ok { background: var(--success-bg); color: var(--success); }
.stat-chip.bad { background: var(--error-bg); color: var(--error); }
.running-chip { background: var(--accent-bg); color: var(--accent); }

.current-line {
  font-size: 0.78rem;
  color: var(--text-muted);
  margin-bottom: 8px;
}

.message-line {
  font-size: 0.78rem;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.item-list {
  max-height: 300px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.item-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--glass-bg);
  font-size: 0.78rem;
}

.item-row b {
  color: var(--text-primary);
  font-weight: 500;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-status {
  font-size: 0.72rem;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--inset-bg);
  color: var(--text-muted);
}

.item-status.success { background: var(--success-bg); color: var(--success); }
.item-status.failed { background: var(--error-bg); color: var(--error); }
.item-status.running { background: var(--accent-bg); color: var(--accent); }

.item-row small {
  color: var(--text-muted);
  font-size: 0.72rem;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
