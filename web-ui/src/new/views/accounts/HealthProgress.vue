<script setup lang="ts">
/* --- 测活进度面板：运行中 / 中断可续跑 / 最近完成 --- */
import { computed } from "vue";
import { useAccountsStore } from "../../stores/accounts";
import { useUiStore } from "../../stores/ui";
import AppButton from "../../components/ui/AppButton.vue";

const accounts = useAccountsStore();
const ui = useUiStore();

const health = computed(() => accounts.health);
const visible = computed(() => !!(health.value.requested || health.value.total || health.value.running || accounts.healthSubmitting));
const percent = computed(() => {
  const h = health.value;
  if (!h.total) return h.requested && !h.running ? 100 : 0;
  return Math.min(100, Math.max(0, Math.round(((h.done || 0) / h.total) * 100)));
});

async function resume() {
  try {
    await accounts.resumeHealth(health.value.requestedWorkers || health.value.workers || 20);
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "续跑失败", "error");
  }
}
</script>

<template>
  <section v-if="visible" :class="['health-panel glass-panel', { active: health.running, warning: accounts.healthError }]" aria-live="polite">
    <div class="health-head">
      <strong>
        {{ health.running ? "账号测活进行中" : health.pendingCount ? "测活任务已中断" : "最近一次测活完成" }}
      </strong>
      <span class="health-count">{{ health.done || 0 }} / {{ health.total || 0 }} · {{ percent }}%</span>
      <AppButton v-if="!health.running && health.pendingCount" variant="primary" size="sm" @click="resume">
        继续检测剩余 {{ health.pendingCount }} 个
      </AppButton>
    </div>

    <div class="progress-track" role="progressbar" :aria-valuenow="percent" aria-valuemin="0" aria-valuemax="100" :aria-label="`测活进度 ${percent}%`">
      <div class="progress-fill" :style="{ width: percent + '%' }"></div>
    </div>

    <p v-if="accounts.healthError" class="health-error" role="status">
      {{ accounts.healthError }}，正在自动重连
    </p>
    <p v-else-if="health.message" class="health-message">{{ health.message }}</p>

    <div class="health-stats">
      <span v-if="health.current">当前：{{ health.current }}</span>
      <span>账号可用性检查</span>
      <span>请求 {{ health.requested || 0 }}</span>
      <span>实际检测 {{ health.checked ?? health.done ?? 0 }}</span>
      <span>跳过已封号 {{ health.skippedKnownDeactivated || 0 }}</span>
      <span class="ok">可用 {{ health.usable ?? health.alive ?? 0 }}</span>
      <span class="warn">待续期 {{ health.reauthRequired || 0 }}</span>
      <span class="warn">待复测 {{ health.unknown || 0 }}</span>
      <span class="err">失效 {{ health.invalid || 0 }}</span>
      <span class="err">错误 {{ health.error || 0 }}</span>
    </div>
  </section>
</template>

<style scoped>
.health-panel {
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  animation: fade-in-up 0.3s ease;
}

.health-head {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 0.85rem;
  color: var(--text-primary);
}

.health-count {
  color: var(--text-muted);
  font-size: 0.78rem;
  flex: 1;
}

.progress-track {
  width: 100%;
  height: 6px;
  background: var(--progress-track);
  border-radius: 6px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent-light), var(--accent-2));
  border-radius: 6px;
  transition: width 0.4s ease;
  position: relative;
  overflow: hidden;
}

.active .progress-fill::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.4), transparent);
  animation: shimmer 1.5s infinite;
}

.health-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.health-stats .ok { color: var(--success); }
.health-stats .warn { color: var(--warning); }
.health-stats .err { color: var(--error); }
.health-error { margin: 0; color: var(--warning); font-size: .75rem; }
.health-message { margin: 0; color: var(--text-muted); font-size: .75rem; }
</style>
