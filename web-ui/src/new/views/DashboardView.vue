<script setup lang="ts">
/* --- 总览：指标卡 + 运行状态 + 最近任务 + 快捷入口 --- */
import { computed, onBeforeUnmount, onMounted } from "vue";
import { RouterLink } from "vue-router";
import {
  ArrowLeftRight, CheckCircle2, Contact, FileDown, Mail, MailPlus, Network,
  ShieldCheck, SquareTerminal, Users, XCircle,
} from "@lucide/vue";
import AppTag from "../components/ui/AppTag.vue";
import { useStatusStore } from "../stores/status";
import { useTasksStore } from "../stores/tasks";

const status = useStatusStore();
const tasks = useTasksStore();

const quickLinks = [
  { to: "/register/tasks", label: "创建注册任务", desc: "调度账号注册", icon: SquareTerminal },
  { to: "/accounts", label: "账号列表", desc: "测活、分组与导出", icon: Contact },
  { to: "/accounts/export", label: "凭据导出", desc: "导出账号与 2FA", icon: FileDown },
  { to: "/accounts/rebind", label: "邮箱换绑", desc: "换绑到空闲邮箱", icon: ArrowLeftRight },
  { to: "/integrations/trial", label: "Plus 试用检查", desc: "检查试用资格", icon: ShieldCheck },
];

const jobDone = computed(
  () => (tasks.job.success_count || 0) + (tasks.job.fail_count || 0) + (tasks.job.skipped_count || 0),
);
const jobPercent = computed(() =>
  tasks.job.total ? Math.round((jobDone.value * 100) / tasks.job.total) : 0,
);
const recentTasks = computed(() => tasks.items.slice(0, 5));

function taskStatusKind(taskStatus: string): "success" | "warning" | "error" | "info" | "default" {
  return (
    { queued: "info", running: "warning", success: "success", failed: "error" } as Record<
      string,
      "success" | "warning" | "error" | "info" | "default"
    >
  )[taskStatus] || "default";
}

function taskStatusText(taskStatus: string): string {
  return (
    { queued: "排队", running: "运行中", success: "成功", failed: "失败", cancelled: "已取消", skipped: "已跳过" }[
      taskStatus
    ] || taskStatus
  );
}

onMounted(async () => {
  await tasks.load();
  tasks.startPolling();
});

onBeforeUnmount(() => {
  tasks.stopPolling();
});
</script>

<template>
  <div class="dashboard">
    <section class="metrics glass-panel" aria-label="运行概览">
      <article class="metric">
        <div class="metric-name"><span>邮箱可用</span><Mail :size="17" /></div>
        <div class="metric-value">{{ status.panel.mail?.available ?? "-" }}</div>
      </article>
      <article class="metric">
        <div class="metric-name"><span>别名邮箱</span><MailPlus :size="17" /></div>
        <div class="metric-value">{{ status.panel.mail?.split_available ?? "-" }}</div>
      </article>
      <article class="metric">
        <div class="metric-name"><span>邮箱总数</span><Mail :size="17" /></div>
        <div class="metric-value">{{ status.panel.mail?.total ?? "-" }}</div>
      </article>
      <article class="metric">
        <div class="metric-name"><span>账号可用</span><Users :size="17" /></div>
        <div class="metric-value is-accent">{{ status.panel.accounts?.usable ?? "-" }}</div>
      </article>
      <article class="metric">
        <div class="metric-name"><span>已封号留档</span><XCircle :size="17" /></div>
        <div class="metric-value">{{ status.panel.accounts?.deactivated ?? "-" }}</div>
      </article>
      <article class="metric">
        <div class="metric-name"><span>本轮成功</span><CheckCircle2 :size="17" /></div>
        <div class="metric-value is-success">{{ status.panel.job?.success_count ?? 0 }}</div>
      </article>
      <article class="metric">
        <div class="metric-name"><span>本轮失败</span><XCircle :size="17" /></div>
        <div class="metric-value is-error">{{ status.panel.job?.fail_count ?? 0 }}</div>
      </article>
      <article class="metric">
        <div class="metric-name"><span>代理状态</span><Network :size="17" /></div>
        <div class="metric-value is-small">
          {{ status.panel.proxy?.enabled ? (status.panel.proxy?.pool_count || 1) + " 条" : "关闭" }}
        </div>
      </article>
    </section>

    <!-- 运行状态 + 最近任务 -->
    <div class="dash-row">
      <section class="job glass-panel">
        <div class="job-head">
          <h2 class="section-title">注册任务</h2>
          <AppTag :kind="tasks.job.running ? (tasks.job.paused ? 'warning' : 'success') : 'default'" dot>
            {{ tasks.job.running ? (tasks.job.paused ? "已暂停" : "运行中") : "空闲" }}
          </AppTag>
        </div>
        <div class="job-numbers">
          <span class="job-count">{{ jobDone }} / {{ tasks.job.total || 0 }}</span>
          <span class="job-detail">
            <span class="ok">成功 {{ tasks.job.success_count || 0 }}</span>
            <span class="err">失败 {{ tasks.job.fail_count || 0 }}</span>
          </span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" :style="{ width: jobPercent + '%' }"></div>
        </div>
        <RouterLink to="/register/tasks" class="job-link">进入任务调度 →</RouterLink>
      </section>

      <section class="recent glass-panel">
        <div class="job-head">
          <h2 class="section-title">最近任务</h2>
          <RouterLink to="/register/tasks" class="more-link">全部 →</RouterLink>
        </div>
        <p v-if="!recentTasks.length" class="recent-empty">暂无任务记录</p>
        <div v-else class="recent-list">
          <div v-for="task in recentTasks" :key="task.card_id || task.index" class="recent-row">
            <span class="recent-email" :title="task.email">{{ task.email || "（分配中）" }}</span>
            <AppTag :kind="taskStatusKind(task.status)">{{ taskStatusText(task.status) }}</AppTag>
          </div>
        </div>
      </section>
    </div>

    <section class="quick glass-panel">
      <h2 class="section-title">快捷入口</h2>
      <div class="quick-grid">
        <RouterLink v-for="link in quickLinks" :key="link.to" :to="link.to" class="quick-card">
          <span class="quick-icon" aria-hidden="true">
            <component :is="link.icon" :size="18" />
          </span>
          <span class="quick-text">
            <b>{{ link.label }}</b>
            <small>{{ link.desc }}</small>
          </span>
        </RouterLink>
      </div>
    </section>
  </div>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* --- 指标卡 --- */
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 14px;
  padding: 20px;
}

.metric {
  padding: 18px 20px;
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  transition: var(--transition-fast);
}

.metric:hover {
  background: var(--glass-bg-hover);
  border-color: var(--glass-border-hover);
  transform: translateY(-1px);
  box-shadow: var(--shadow-sm);
}

.metric-name {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.85rem;
  color: var(--text-secondary);
  margin-bottom: 10px;
}

.metric-value {
  font-size: 2rem;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
}

.metric-value.is-accent { color: var(--accent); }
.metric-value.is-success { color: var(--success); }
.metric-value.is-error { color: var(--error); }
.metric-value.is-small { font-size: 1.35rem; }

/* --- 运行状态 + 最近任务 --- */
.dash-row {
  display: grid;
  grid-template-columns: 1fr 1.4fr;
  gap: 14px;
}

.job,
.recent {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.job-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.job-head .section-title {
  margin-bottom: 0;
}

.job-numbers {
  display: flex;
  align-items: baseline;
  gap: 14px;
}

.job-count {
  font-size: 1.6rem;
  font-weight: 700;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.job-detail {
  display: flex;
  gap: 12px;
  font-size: 0.78rem;
}

.job-detail .ok { color: var(--success); }
.job-detail .err { color: var(--error); }

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

.job-link,
.more-link {
  font-size: 0.78rem;
  color: var(--accent);
}

.job-link:hover,
.more-link:hover {
  text-decoration: underline;
}

.recent-empty {
  color: var(--text-muted);
  font-size: 0.82rem;
  padding: 12px 0;
}

.recent-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.recent-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
}

.recent-email {
  font-size: 0.82rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 900px) {
  .dash-row {
    grid-template-columns: 1fr;
  }
}

/* --- 快捷入口 --- */
.quick {
  padding: 18px;
}

.section-title {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 12px;
}

.quick-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 12px;
}

.quick-card {
  display: flex;
  align-items: center;
  gap: 13px;
  padding: 16px 18px;
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  transition: var(--transition-fast);
}

.quick-card:hover {
  background: var(--glass-bg-hover);
  border-color: var(--accent-border);
  transform: translateY(-1px);
  box-shadow: var(--shadow-sm);
}

.quick-icon {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--accent-bg);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.quick-text {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}

.quick-text b {
  font-size: 0.92rem;
  color: var(--text-primary);
}

.quick-text small {
  font-size: 0.76rem;
  color: var(--text-muted);
}
</style>
