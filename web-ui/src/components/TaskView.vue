<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import AppPager from "./AppPager.vue";

const props = defineProps({
  status: { type: Object, default: () => ({}) },
  tasks: { type: Array, default: () => [] },
  groups: { type: Array, default: () => [] },
  form: { type: Object, required: true },
  busy: Boolean,
});
const emit = defineEmits(["start", "stop", "pause", "resume", "retry-failed", "create-group", "reset-mail", "refresh-accounts", "refresh-tasks", "delete-tasks", "navigate"]);
const newGroupName = ref("");
const page = ref(1);
const selected = ref([]);
const clock = ref(Date.now());
let clockTimer = null;
const pageSize = 5;
const pages = computed(() => Math.max(1, Math.ceil(props.tasks.length / pageSize)));
const pageItems = computed(() => props.tasks.slice((page.value - 1) * pageSize, page.value * pageSize));
const jobDone = computed(() => Number(props.status.job?.success_count || 0) + Number(props.status.job?.fail_count || 0) + Number(props.status.job?.skipped_count || 0));
const jobPercent = computed(() => props.status.job?.total ? Math.round(jobDone.value * 100 / Number(props.status.job.total)) : 0);
watch(() => props.tasks.length, () => { page.value = Math.min(page.value, pages.value); selected.value = []; });

function provider(type) {
  return (props.status.mail?.providers || []).find((item) => item.type === type) || {};
}
function elapsed(task) {
  const status = String(task.status || "queued");
  if (!["success", "failed", "cancelled", "skipped"].includes(status)) {
    const started = Number(task.started_at_epoch || 0);
    if (started > 0) return `${Math.max(0, Math.round(clock.value / 1000 - started))}s`;
  }
  const value = Number(task.elapsed_sec);
  return Number.isFinite(value) ? `${Math.max(0, Math.round(value))}s` : "-";
}
function statusText(task) {
  const status = task.status || "queued";
  const labels = { queued: "排队", running: "运行中", success: "成功", failed: "失败", cancelled: "已取消", skipped: "已跳过" };
  return ["success", "failed", "cancelled", "skipped"].includes(status)
    ? `${labels[status] || status} (${elapsed(task)})`
    : labels[status] || status;
}
function batchTime(task) {
  const value = task.batch_started_at;
  if (!value) return "历史批次";
  try {
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "历史批次"
      : date.toLocaleString("zh-CN", { hour12: false });
  } catch { return "历史批次"; }
}
function createGroup() {
  const name = newGroupName.value.trim();
  if (!name) return;
  emit("create-group", name);
  newGroupName.value = "";
}
onMounted(() => { clockTimer = setInterval(() => { clock.value = Date.now(); }, 1000); });
onBeforeUnmount(() => clearInterval(clockTimer));
</script>

<template>
  <div class="grid">
    <section class="card">
      <div class="card-h"><div><h2>注册任务</h2><p class="subtle">配置本轮数量、并发、邮箱来源与注册引擎。</p></div></div>
      <div class="form">
        <label class="full">注册引擎
          <select v-model="form.engine"><option value="browser">全程指纹浏览器（自动补取 Refresh）</option><option value="hybrid">会话内混合（自动补取 Refresh）</option><option value="protocol">协议注册（原生 Refresh Token）</option></select>
        </label>
        <label>数量<input v-model.number="form.total" type="number" min="1" max="500" /></label>
        <label>并发<input v-model.number="form.concurrency" type="number" min="1" /></label>
        <label class="full">邮箱类型
          <select v-model="form.mailType"><option value="auto">自动（全部邮箱池）</option><option value="outlook">仅 Outlook</option><option value="icloud">仅 iCloud</option><option value="domain">仅域名邮箱</option><option value="maddy">仅 Maddy</option><option value="fastmail">仅 Fastmail</option></select>
        </label>
        <label class="full">目标分组
          <select v-model="form.groupId" :disabled="!groups.length || form.autoGroup">
            <option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}（{{ group.account_count || 0 }}）</option>
          </select>
        </label>
        <div class="full inline-create-group">
          <input v-model.trim="newGroupName" type="text" maxlength="48" placeholder="新分组名称" @keyup.enter="createGroup" />
          <button class="btn secondary" :disabled="busy || !newGroupName.trim()" @click="createGroup">新建分组</button>
        </div>
        <label class="full">指定主邮箱 / 别名（可选）<input v-model.trim="form.email" type="text" placeholder="已注册主邮箱自动并发两个新别名" /></label>
        <label class="check"><input v-model="form.keepOnFail" type="checkbox" />失败保留浏览器</label>
        <label class="check full"><input v-model="form.autoGroup" type="checkbox" />自动按数量分组</label>
        <label v-if="form.autoGroup">每组数量<input v-model.number="form.autoGroupSize" type="number" min="1" max="500" /></label>
        <p v-if="form.autoGroup" class="subtle full auto-group-hint">本轮将自动创建分组，任务按序号每 {{ Math.max(1, Number(form.autoGroupSize || 35)) }} 个切换一次。</p>
      </div>
      <div class="actions">
        <button class="primary" :disabled="status.job?.running || busy || !form.groupId" @click="emit('start')">开始运行</button>
        <button class="danger" :disabled="!status.job?.running || status.job?.stop_requested || busy" @click="emit('stop')">{{ status.job?.stop_requested ? "停止中…" : "停止任务" }}</button>
        <button v-if="status.job?.running && !status.job?.paused" class="btn secondary" :disabled="busy" @click="emit('pause')">暂停派发</button>
        <button v-if="status.job?.running && status.job?.paused" class="btn secondary" :disabled="busy" @click="emit('resume')">继续派发</button>
        <button class="btn secondary" :disabled="status.job?.running || !status.job?.fail_count || busy" @click="emit('retry-failed')">重试失败（{{ status.job?.fail_count || 0 }}）</button>
        <button class="btn secondary" @click="emit('navigate', 'logs')">查看日志</button>
      </div>
    </section>

    <aside class="card run-card">
      <h2>运行态</h2>
      <div class="stat"><span class="key">任务状态</span><span :class="['tag', status.job?.running ? 'ok' : '']">{{ status.job?.paused ? "已暂停派发" : status.job?.running ? "运行中" : "空闲" }}</span></div>
      <div class="stat"><span class="key">当前分组</span><span class="value">{{ form.autoGroup ? `自动分组（每组 ${Math.max(1, Number(form.autoGroupSize || 35))} 个）` : groups.find((item) => item.id === form.groupId)?.name || "-" }}</span></div>
      <div class="stat"><span class="key">邮箱合计</span><span class="value">可用 {{ status.mail?.available ?? 0 }} / 总数 {{ status.mail?.total ?? 0 }}</span></div>
      <div class="stat"><span class="key">Outlook</span><span class="value">可用 {{ provider('outlook_token').available ?? 0 }} / 总数 {{ provider('outlook_token').total ?? 0 }}</span></div>
      <div class="stat"><span class="key">iCloud</span><span class="value">可用 {{ provider('icloud_api').available ?? 0 }} / 总数 {{ provider('icloud_api').total ?? 0 }}</span></div>
      <div class="stat"><span class="key">域名邮箱</span><span class="value">可用 {{ provider('domain_api').available ?? 0 }} / 总数 {{ provider('domain_api').total ?? 0 }}</span></div>
      <div class="stat"><span class="key">Maddy</span><span class="value">可用 {{ provider('maddy_webui').available ?? 0 }} / 总数 {{ provider('maddy_webui').total ?? 0 }}</span></div>
      <div class="stat"><span class="key">Fastmail</span><span class="value">可用 {{ provider('fastmail_api').available ?? 0 }} / 总数 {{ provider('fastmail_api').total ?? 0 }}</span></div>
      <div class="stat"><span class="key">代理池</span><span class="value">{{ status.proxy?.enabled ? `${status.proxy?.pool_count || 1} 条` : "关闭" }}</span></div>
      <div class="toolbar">
        <button class="btn secondary" @click="emit('reset-mail')">释放异常邮箱</button>
        <button class="btn secondary" @click="emit('refresh-accounts')">刷新账号</button>
      </div>
      <div v-if="status.job?.total" class="inline-progress run-progress"><div><strong>本轮任务进度</strong><span>{{ jobDone }} / {{ status.job.total }} · {{ jobPercent }}%</span></div><div class="inline-progress-track"><i :style="{ width: `${jobPercent}%` }"></i></div><small>成功 {{ status.job.success_count || 0 }} · 失败 {{ status.job.fail_count || 0 }} · 跳过 {{ status.job.skipped_count || 0 }}</small></div>
    </aside>

    <section class="card span-all">
      <div class="task-title-row">
        <div><h2>任务名片</h2><p class="subtle">跨批次保留邮箱、代理、步骤和结果；仅手动删除才会清除。</p></div>
        <button class="btn secondary" @click="emit('refresh-tasks')">刷新任务</button>
      </div>
      <div class="task-board">
        <div v-if="!pageItems.length" class="empty-card">暂无任务</div>
        <article v-for="task in pageItems" :key="task.card_id || task.index" :class="['task-card', task.status || 'queued']">
          <div class="task-head">
            <input v-model="selected" type="checkbox" :value="task.card_id || task.index" />
            <div class="task-main"><div class="task-name">#{{ task.index }} · {{ task.email || "等待邮箱" }}</div><div class="task-sub">{{ batchTime(task) }} · {{ task.provider || "-" }} · {{ task.proxy || "无代理" }} · {{ elapsed(task) }}</div></div>
            <span :class="['pill', task.status === 'success' ? 'success' : task.status === 'failed' ? 'failed' : task.status === 'skipped' || task.status === 'cancelled' ? 'queue' : 'running']">{{ statusText(task) }}</span>
          </div>
          <div class="task-steps"><div v-for="(step, index) in (task.steps || []).slice(-8)" :key="index" class="task-step"><span>{{ step.time }}</span>{{ step.text }}</div><div v-if="!task.steps?.length" class="task-step muted">等待步骤更新</div></div>
        </article>
      </div>
      <div class="bulkbar">
        <button class="btn danger" :disabled="!selected.length" @click="emit('delete-tasks', selected)">删除选中</button>
        <button class="btn danger" :disabled="!tasks.length" @click="emit('delete-tasks', null)">清空任务</button>
        <AppPager v-model:page="page" :pages="pages" :total="tasks.length" />
      </div>
    </section>
  </div>
</template>
