<script setup lang="ts">
/* --- 任务调度页：任务配置 + 运行控制 + 任务卡列表 --- */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  Check, ChevronDown, ChevronUp, ClipboardList, Copy, Pause, Play,
  RotateCcw, Square, Trash2,
} from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppEmpty from "../../components/ui/AppEmpty.vue";
import AppPager from "../../components/ui/AppPager.vue";
import AppTag from "../../components/ui/AppTag.vue";
import TaskForm from "./TaskForm.vue";
import { useTasksStore } from "../../stores/tasks";
import { useGroupsStore } from "../../stores/groups";
import { useStatusStore } from "../../stores/status";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import { useCopy } from "../../composables/useCopy";
import type { TaskCard } from "../../api/tasks";
import type { MailProviderStatus } from "../../api/types";

const tasks = useTasksStore();
const groups = useGroupsStore();
const status = useStatusStore();
const ui = useUiStore();
const confirm = useConfirm();
const { copiedKey, copyText } = useCopy();

const page = ref(1);
const pageSize = 6;
const clock = ref(Date.now());
const expanded = ref(new Set<string>());
const selected = ref(new Set<string>());
const busyAction = ref(false);

let clockTimer: ReturnType<typeof setInterval> | null = null;

const pageCount = computed(() => Math.max(1, Math.ceil(tasks.items.length / pageSize)));
const pageItems = computed(() => tasks.items.slice((page.value - 1) * pageSize, page.value * pageSize));

const jobDone = computed(
  () => (tasks.job.success_count || 0) + (tasks.job.fail_count || 0) + (tasks.job.skipped_count || 0),
);
const jobPercent = computed(() =>
  tasks.job.total ? Math.round((jobDone.value * 100) / tasks.job.total) : 0,
);
const smsbowerMail = computed<MailProviderStatus>(() =>
  (status.panel.mail?.providers || []).find((item) => item.type === "smsbower_mail") || { type: "smsbower_mail" },
);

function toggleExpand(id: string) {
  const next = new Set(expanded.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expanded.value = next;
}

function toggleSelect(id: string) {
  const next = new Set(selected.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selected.value = next;
}

function elapsed(task: TaskCard): string {
  const status = String(task.status || "queued");
  if (!["success", "failed", "cancelled", "skipped"].includes(status)) {
    const started = Number(task.started_at_epoch || 0);
    if (started > 0) return `${Math.max(0, Math.round(clock.value / 1000 - started))}s`;
  }
  const value = Number(task.elapsed_sec);
  return Number.isFinite(value) ? `${Math.max(0, Math.round(value))}s` : "-";
}

function statusInfo(task: TaskCard): { text: string; kind: "success" | "warning" | "error" | "info" | "default" } {
  const labels: Record<string, { text: string; kind: "success" | "warning" | "error" | "info" | "default" }> = {
    queued: { text: "排队", kind: "info" },
    running: { text: "运行中", kind: "warning" },
    success: { text: "成功", kind: "success" },
    failed: { text: "失败", kind: "error" },
    cancelled: { text: "已取消", kind: "default" },
    skipped: { text: "已跳过", kind: "default" },
  };
  const info = labels[task.status] || { text: task.status, kind: "default" as const };
  if (["success", "failed", "cancelled", "skipped"].includes(task.status)) {
    return { ...info, text: `${info.text} (${elapsed(task)})` };
  }
  return info;
}

async function run(action: () => Promise<unknown>, successMessage: string) {
  if (busyAction.value) return;
  busyAction.value = true;
  try {
    await action();
    if (successMessage) ui.notify(successMessage, "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "操作失败", "error");
  } finally {
    busyAction.value = false;
  }
}

function stop() {
  run(async () => {
    if (!(await confirm("停止当前注册任务？进行中的账号会标记为取消。", { title: "停止任务", confirmText: "停止", danger: true }))) return;
    await tasks.stop();
  }, "");
}

function pause() {
  run(() => tasks.pause(), "任务已暂停");
}

function resume() {
  run(() => tasks.resume(), "任务已继续");
}

function retryFailed() {
  run(async () => {
    if (!(await confirm("重试所有失败任务？", { title: "重试失败", confirmText: "重试" }))) return;
    await tasks.retryFailed();
  }, "");
}

function deleteSelected() {
  run(async () => {
    if (!(await confirm(`删除选中的 ${selected.value.size} 条任务记录？`, { title: "删除记录", confirmText: "删除", danger: true }))) return;
    await tasks.remove([...selected.value]);
    selected.value = new Set();
  }, "记录已删除");
}

function copyEmail(email: string) {
  copyText(email, `task-${email}`).then((ok) => {
    if (ok) ui.notify("邮箱已复制", "success", 1500);
  });
}

onMounted(async () => {
  await Promise.all([groups.load(), tasks.load()]);
  tasks.startPolling();
  clockTimer = setInterval(() => {
    clock.value = Date.now();
  }, 1000);
});

onBeforeUnmount(() => {
  tasks.stopPolling();
  if (clockTimer) clearInterval(clockTimer);
});
</script>

<template>
  <div class="tasks-page">
    <TaskForm />

    <section class="pool-summary glass-panel" aria-label="邮箱池状态">
      <span class="pool-summary-title">邮箱池</span>
      <span class="pool-summary-item">总可用 <b>{{ status.panel.mail?.available ?? "—" }}</b></span>
      <span class="pool-summary-item featured">SMSBower <b>{{ smsbowerMail.available ?? 0 }}</b> / {{ smsbowerMail.total ?? 0 }}</span>
    </section>

    <!-- 运行控制条 -->
    <section v-if="tasks.job.running || tasks.job.total" class="job-panel glass-panel">
      <div class="job-head">
        <AppTag :kind="tasks.job.running ? (tasks.job.paused ? 'warning' : 'success') : 'default'" dot>
          {{ tasks.job.running ? (tasks.job.paused ? "已暂停" : "运行中") : "已结束" }}
        </AppTag>
        <span class="job-count">{{ jobDone }} / {{ tasks.job.total || 0 }}</span>
        <span class="job-stats">
          <span class="ok">成功 {{ tasks.job.success_count || 0 }}</span>
          <span class="err">失败 {{ tasks.job.fail_count || 0 }}</span>
          <span v-if="tasks.job.skipped_count">跳过 {{ tasks.job.skipped_count }}</span>
        </span>

        <div class="job-actions">
          <AppButton v-if="tasks.job.running && !tasks.job.paused" variant="ghost" size="sm" :disabled="busyAction" @click="pause">
            <Pause :size="14" /><span>暂停</span>
          </AppButton>
          <AppButton v-if="tasks.job.running && tasks.job.paused" variant="primary" size="sm" :disabled="busyAction" @click="resume">
            <Play :size="14" /><span>继续</span>
          </AppButton>
          <AppButton v-if="tasks.job.running" variant="danger" size="sm" :disabled="busyAction" @click="stop">
            <Square :size="14" /><span>停止</span>
          </AppButton>
          <AppButton v-if="!tasks.job.running && tasks.job.fail_count" variant="ghost" size="sm" :disabled="busyAction" @click="retryFailed">
            <RotateCcw :size="14" /><span>重试失败</span>
          </AppButton>
        </div>
      </div>

      <div class="progress-track">
        <div class="progress-fill" :style="{ width: jobPercent + '%' }"></div>
      </div>

      <p v-if="tasks.job.last_error" class="job-error">最近错误：{{ tasks.job.last_error }}</p>
    </section>

    <!-- 任务卡列表 -->
    <section class="cards-panel glass-panel">
      <div class="cards-head">
        <h2 class="panel-title">任务记录</h2>
        <div class="cards-head-actions">
          <AppButton v-if="selected.size" variant="danger" size="sm" :disabled="busyAction" @click="deleteSelected">
            <Trash2 :size="14" /><span>删除选中（{{ selected.size }}）</span>
          </AppButton>
          <AppButton variant="ghost" size="sm" @click="tasks.load()">
            <RotateCcw :size="14" /><span>刷新</span>
          </AppButton>
        </div>
      </div>

      <AppEmpty v-if="!tasks.items.length" title="暂无任务记录" hint="配置上方表单并启动第一个注册任务">
        <template #icon><ClipboardList :size="48" :stroke-width="1" /></template>
      </AppEmpty>

      <template v-else>
        <div class="task-cards">
          <article v-for="task in pageItems" :key="task.card_id || task.index" :class="['task-card', `is-${task.status}`]">
            <div class="card-main">
              <input
                type="checkbox"
                class="card-check"
                :checked="selected.has(String(task.card_id || task.index))"
                :aria-label="`选择任务 ${task.email}`"
                @change="toggleSelect(String(task.card_id || task.index))"
              />
              <div class="card-info">
                <div class="card-email-row">
                  <span class="card-email" :title="task.email">{{ task.email || "（分配中）" }}</span>
                  <button class="mini-action" type="button" title="复制邮箱" @click="copyEmail(task.email)">
                    <Check v-if="copiedKey === `task-${task.email}`" :size="12" class="copied" />
                    <Copy v-else :size="12" />
                  </button>
                </div>
                <div class="card-meta">
                  <span v-if="task.provider">{{ task.provider }}</span>
                  <span v-if="task.proxy" class="meta-proxy" :title="task.proxy">代理 {{ task.proxy }}</span>
                  <span>耗时 {{ elapsed(task) }}</span>
                </div>
              </div>
              <div class="card-right">
                <AppTag :kind="statusInfo(task).kind">{{ statusInfo(task).text }}</AppTag>
                <button
                  v-if="(task.steps || []).length"
                  class="mini-action expand"
                  type="button"
                  :title="expanded.has(String(task.card_id || task.index)) ? '收起步骤' : '展开步骤'"
                  @click="toggleExpand(String(task.card_id || task.index))"
                >
                  <ChevronUp v-if="expanded.has(String(task.card_id || task.index))" :size="14" />
                  <ChevronDown v-else :size="14" />
                </button>
              </div>
            </div>

            <div v-if="expanded.has(String(task.card_id || task.index)) && (task.steps || []).length" class="card-steps">
              <p v-for="(step, index) in task.steps" :key="index" class="step-line">
                <span class="step-time">{{ step.time }}</span>{{ step.text }}
              </p>
            </div>
          </article>
        </div>

        <AppPager :page="page" :pages="pageCount" :total="tasks.items.length" @update:page="page = $event" />
      </template>
    </section>
  </div>
</template>

<style scoped>
.tasks-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.pool-summary {
  min-height: 42px;
  padding: 9px 14px;
  display: flex;
  align-items: center;
  gap: 14px;
  color: var(--text-muted);
  font-size: 0.74rem;
}

.pool-summary-title {
  color: var(--text-primary);
  font-weight: 600;
}

.pool-summary-item {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  white-space: nowrap;
}

.pool-summary-item b {
  color: var(--text-primary);
  font-size: 0.82rem;
  font-variant-numeric: tabular-nums;
}

.pool-summary-item.featured {
  margin-left: auto;
  color: var(--accent);
}

@media (max-width: 480px) {
  .pool-summary {
    flex-wrap: wrap;
    gap: 5px 12px;
  }

  .pool-summary-item.featured {
    width: 100%;
    margin-left: 0;
  }
}

/* --- 运行控制 --- */
.job-panel {
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.job-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.job-count {
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.job-stats {
  display: flex;
  gap: 12px;
  font-size: 0.76rem;
  flex: 1;
}

.job-stats .ok { color: var(--success); }
.job-stats .err { color: var(--error); }

.job-actions {
  display: flex;
  gap: 6px;
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
}

.job-error {
  font-size: 0.75rem;
  color: var(--error);
  overflow-wrap: anywhere;
}

/* --- 任务卡 --- */
.cards-panel {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.cards-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.cards-head-actions {
  display: flex;
  gap: 6px;
}

.task-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.task-card {
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  padding: 12px 14px;
  transition: var(--transition-fast);
}

.task-card:hover {
  background: var(--glass-bg-hover);
}

.task-card.is-running {
  border-color: var(--accent-border);
}

.task-card.is-failed {
  border-color: rgba(239, 68, 68, 0.25);
}

.card-main {
  display: flex;
  align-items: center;
  gap: 10px;
}

.card-check {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  flex-shrink: 0;
}

.card-info {
  flex: 1;
  min-width: 0;
}

.card-email-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.card-email {
  font-size: 0.87rem;
  font-weight: 500;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.card-meta {
  display: flex;
  gap: 10px;
  font-size: 0.7rem;
  color: var(--text-muted);
  margin-top: 2px;
  flex-wrap: wrap;
}

.meta-proxy {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.mini-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition-fast);
}

.mini-action:hover {
  background: var(--accent-bg);
  color: var(--accent);
}

.mini-action .copied {
  color: var(--success);
}

.card-steps {
  margin-top: 10px;
  border-top: 1px solid var(--line-subtle);
  padding-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.step-line {
  font-size: 0.75rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.step-time {
  color: var(--text-muted);
  font-family: var(--mono);
  font-size: 0.68rem;
  margin-right: 8px;
}
</style>
