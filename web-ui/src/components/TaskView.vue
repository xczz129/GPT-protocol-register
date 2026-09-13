<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Check, Copy, Play, Square, Pause, RotateCcw, ClipboardList, RefreshCw, Trash2 } from "@lucide/vue";
import AppPager from "./AppPager.vue";
import { normalizeRegistrationNumbers, smsbowerPolicy } from "../shared/mailRegistration";

const props = defineProps({
  status: { type: Object, default: () => ({}) },
  tasks: { type: Array, default: () => [] },
  groups: { type: Array, default: () => [] },
  form: { type: Object, required: true },
  busy: Boolean,
});

const emit = defineEmits([
  "start", "stop", "pause", "resume", "retry-failed", "create-group", 
  "reset-mail", "refresh-accounts", "refresh-tasks", "delete-tasks", "navigate", "copy-value"
]);

const newGroupName = ref("");
const page = ref(1);
const selected = ref([]);
const clock = ref(Date.now());
let clockTimer = null;
const pageSize = 6;
const copiedFeedback = ref({});
const expandedTasks = ref(new Set());

function toggleExpand(key) {
  const next = new Set(expandedTasks.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  expandedTasks.value = next;
}

function triggerCopied(key) {
  copiedFeedback.value[key] = true;
  setTimeout(() => { copiedFeedback.value[key] = false; }, 1500);
}

function copyTextDirect(text, key) {
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      triggerCopied(key);
      emit("copy-value", { text, successMessage: `已复制: ${text}` });
    });
  }
}

const pages = computed(() => Math.max(1, Math.ceil(props.tasks.length / pageSize)));
const pageItems = computed(() => props.tasks.slice((page.value - 1) * pageSize, page.value * pageSize));
const jobDone = computed(() => Number(props.status.job?.success_count || 0) + Number(props.status.job?.fail_count || 0) + Number(props.status.job?.skipped_count || 0));
const jobPercent = computed(() => props.status.job?.total ? Math.round(jobDone.value * 100 / Number(props.status.job.total)) : 0);

watch(() => props.tasks.length, () => {
  page.value = Math.min(page.value, pages.value);
  selected.value = [];
});

function provider(type) {
  return (props.status.mail?.providers || []).find((item) => item.type === type) || {};
}

const registrationPolicy = computed(() => smsbowerPolicy(props.form.mailType, provider("smsbower_mail").register_child));
function normalizeNumbers() {
  Object.assign(props.form, normalizeRegistrationNumbers(props.form, provider("smsbower_mail").register_child));
}
watch(() => [props.form.mailType, provider("smsbower_mail").register_child], normalizeNumbers, { immediate: true });

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
    <!-- 任务创建卡片 -->
    <section class="card">
      <div class="card-h">
        <div>
          <h2>注册任务配置</h2>
          <p class="subtle">配置并发、数量、邮箱来源与注册引擎，开启批量自动化流程。</p>
        </div>
      </div>
      <div class="form">
        <label class="full">注册引擎
          <select v-model="form.engine">
            <option value="browser">全程指纹浏览器（自动补取 Refresh）</option>
            <option value="hybrid">会话内混合（自动补取 Refresh）</option>
            <option value="protocol">协议注册（原生 Refresh Token）</option>
          </select>
        </label>
        <label>{{ registrationPolicy.paired ? "任务数量（主号 + 子号）" : "任务数量" }}<input v-model.number="form.total" type="number" :min="registrationPolicy.totalMin" :step="registrationPolicy.totalStep" max="500" @blur="normalizeNumbers" /></label>
        <label>任务并发<input v-model.number="form.concurrency" type="number" :min="registrationPolicy.concurrencyMin" :step="registrationPolicy.concurrencyStep" max="64" @blur="normalizeNumbers" /></label>
        <label class="full">邮箱池类型
          <select v-model="form.mailType">
            <option value="auto">自动（全部可用邮箱池）</option>
            <option value="outlook">仅 Outlook</option>
            <option value="icloud">仅 iCloud</option>
            <option value="smsbower_mail">SMSBower 临时邮箱</option>
            <option value="domain">仅域名邮箱</option>
            <option value="maddy">仅 Maddy</option>
            <option value="fastmail">仅 Fastmail</option>
          </select>
        </label>
        <p v-if="registrationPolicy.selected" class="full hint" role="note">
          <template v-if="registrationPolicy.paired">{{ Math.ceil((Number(form.total) || 2) / 2) }} 组主子号 · 主号收码后保留邮箱，待子号收码流程结束再统一结算。启用子号时，数量、并发最低为 2，奇数补齐为偶数。</template>
          <template v-else>已关闭注册子号，仅注册主号；并发恢复普通规则，可使用 1、3、5 等整数。</template>
          检测到邮箱已注册时跳过；子号开关在邮箱池设置。
        </p>
        <label class="full">目标存储分组
          <select v-model="form.groupId" :disabled="!groups.length || form.autoGroup">
            <option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}（{{ group.account_count || 0 }}）</option>
          </select>
        </label>
        <div class="full inline-create-group">
          <input v-model.trim="newGroupName" type="text" maxlength="48" placeholder="新分组名称" @keyup.enter="createGroup" />
          <button class="btn secondary" :disabled="busy || !newGroupName.trim()" @click="createGroup">新建分组</button>
        </div>
        <label class="full">指定主邮箱 / 别名（可选）
          <input v-model.trim="form.email" type="text" placeholder="已注册主邮箱自动并发两个新别名" />
        </label>
        <label class="check"><input v-model="form.keepOnFail" type="checkbox" />失败保留指纹浏览器环境</label>
        <label class="check full"><input v-model="form.autoGroup" type="checkbox" />自动按数量切分分组</label>
        <label v-if="form.autoGroup">每组数量<input v-model.number="form.autoGroupSize" type="number" min="1" max="500" /></label>
      </div>
      <div class="actions" style="margin-top: 18px; display: flex; flex-wrap: wrap; gap: 8px;">
        <button class="btn" :disabled="status.job?.running || busy || !form.groupId" @click="emit('start')">
          <Play :size="14" aria-hidden="true" />
          开始运行
        </button>
        <button class="btn danger" :disabled="!status.job?.running || status.job?.stop_requested || busy" @click="emit('stop')">
          <Square :size="14" aria-hidden="true" />
          {{ status.job?.stop_requested ? "停止中…" : "停止任务" }}
        </button>
        <button v-if="status.job?.running && !status.job?.paused" class="btn secondary" :disabled="busy" @click="emit('pause')">
          <Pause :size="14" aria-hidden="true" />
          暂停派发
        </button>
        <button v-if="status.job?.running && status.job?.paused" class="btn secondary" :disabled="busy" @click="emit('resume')">
          <Play :size="14" aria-hidden="true" />
          继续派发
        </button>
        <button class="btn secondary" :disabled="status.job?.running || !status.job?.fail_count || busy" @click="emit('retry-failed')">
          <RotateCcw :size="14" aria-hidden="true" />
          重试失败（{{ status.job?.fail_count || 0 }}）
        </button>
        <button class="btn secondary" @click="emit('navigate', 'logs')">
          <ClipboardList :size="14" aria-hidden="true" />
          实时日志
        </button>
      </div>
    </section>

    <!-- 运行态监控看板 -->
    <aside class="card run-card">
      <div class="card-h">
        <div>
          <h2>系统运行状态</h2>
          <p class="subtle">实时观察邮箱余量与派发进度。</p>
        </div>
      </div>
      <div style="display: flex; flex-direction: column; gap: 8px;">
        <div class="stat">
          <span class="key">任务状态</span>
          <span :class="['capsule-badge', status.job?.running ? 'plus' : 'free']">
            {{ status.job?.paused ? "已暂停派发" : status.job?.running ? "运行中" : "空闲" }}
          </span>
        </div>
        <div class="stat"><span class="key">当前分组</span><span class="value">{{ form.autoGroup ? `自动分组（每组 ${Math.max(1, Number(form.autoGroupSize || 35))} 个）` : groups.find((item) => item.id === form.groupId)?.name || "-" }}</span></div>
        <div class="stat"><span class="key">邮箱合计</span><span class="value">可用 {{ status.mail?.available ?? 0 }} / 总数 {{ status.mail?.total ?? 0 }}</span></div>
        <div class="stat"><span class="key">Outlook</span><span class="value">可用 {{ provider('outlook_token').available ?? 0 }} / 总数 {{ provider('outlook_token').total ?? 0 }}</span></div>
        <div class="stat"><span class="key">iCloud</span><span class="value">可用 {{ provider('icloud_api').available ?? 0 }} / 总数 {{ provider('icloud_api').total ?? 0 }}</span></div>
        <div class="stat"><span class="key">SMSBower</span><span class="value">可用 {{ provider('smsbower_mail').available ?? 0 }} / 总数 {{ provider('smsbower_mail').total ?? 0 }}</span></div>
        <div class="stat"><span class="key">Maddy</span><span class="value">可用 {{ provider('maddy_webui').available ?? 0 }} / 总数 {{ provider('maddy_webui').total ?? 0 }}</span></div>
        <div class="stat"><span class="key">Fastmail</span><span class="value">可用 {{ provider('fastmail_api').available ?? 0 }} / 总数 {{ provider('fastmail_api').total ?? 0 }}</span></div>
        <div class="stat"><span class="key">代理池</span><span class="value">{{ status.proxy?.enabled ? `${status.proxy?.pool_count || 1} 条` : "关闭" }}</span></div>
      </div>

      <div class="toolbar" style="margin-top: 14px; display: flex; gap: 8px;">
        <button class="btn secondary" style="flex: 1;" @click="emit('reset-mail')">释放异常邮箱</button>
        <button class="btn secondary" style="flex: 1;" @click="emit('refresh-accounts')">刷新账号</button>
      </div>

      <div v-if="status.job?.total" class="health-progress" style="margin-top: 16px;">
        <div class="health-progress-head">
          <strong>本轮任务进度</strong>
          <span>{{ jobDone }} / {{ status.job.total }} · {{ jobPercent }}%</span>
        </div>
        <div class="health-progress-track">
          <i :style="{ width: `${jobPercent}%` }"></i>
        </div>
        <div class="health-progress-meta">
          <span>成功 {{ status.job.success_count || 0 }}</span>
          <span>失败 {{ status.job.fail_count || 0 }}</span>
          <span>跳过 {{ status.job.skipped_count || 0 }}</span>
        </div>
      </div>
    </aside>

    <!-- 任务名片看板 -->
    <section class="card span-all">
      <div class="card-h">
        <div>
          <h2>实时任务流名片</h2>
          <p class="subtle">跨批次保留邮箱、代理、各阶段步骤与生成结果，支持一键点击复制邮箱。</p>
        </div>
        <button class="btn secondary" @click="emit('refresh-tasks')">
          <RefreshCw :size="14" aria-hidden="true" />
          刷新任务
        </button>
      </div>

      <div class="task-board">
        <div v-if="!pageItems.length" class="empty-card" style="padding: 40px; text-align: center; color: var(--ink-400);">暂无运行任务</div>
        <article
          v-for="task in pageItems"
          :key="task.card_id || task.index"
          :class="['task-card', task.status || 'queued', { expanded: expandedTasks.has(task.card_id || task.index) }]"
        >
          <div class="task-head">
            <input v-model="selected" type="checkbox" :value="task.card_id || task.index" @click.stop />
            <div class="task-main">
              <div class="task-name" style="display: flex; align-items: center; gap: 6px;">
                <span>#{{ task.index }} ·</span>
                <span
                  v-if="task.email"
                  class="interactive-email"
                  role="button"
                  tabindex="0"
                  :aria-label="`复制邮箱 ${task.email}`"
                  :title="`点击复制邮箱: ${task.email}`"
                  @click="copyTextDirect(task.email, `task_email_${task.card_id || task.index}`)"
                  @keydown.enter.prevent="copyTextDirect(task.email, `task_email_${task.card_id || task.index}`)"
                  @keydown.space.prevent="copyTextDirect(task.email, `task_email_${task.card_id || task.index}`)"
                >
                  <b>{{ task.email }}</b>
                  <component :is="copiedFeedback[`task_email_${task.card_id || task.index}`] ? Check : Copy" :size="11" />
                </span>
                <span v-else style="color: var(--ink-400);">等待分配邮箱</span>
              </div>
              <div class="task-sub">
                {{ batchTime(task) }} · {{ task.provider || "-" }} · {{ task.proxy || "本地" }} · {{ elapsed(task) }}
              </div>
            </div>
            <span :class="['status-pill', task.status === 'success' ? 'alive' : task.status === 'failed' ? 'invalid' : 'limited']">
              <i></i>
              {{ statusText(task) }}
            </span>
          </div>

          <button
            class="task-expand-btn"
            type="button"
            :title="expandedTasks.has(task.card_id || task.index) ? '收起步骤' : `展开 ${task.steps?.length || 0} 个步骤`"
            @click="toggleExpand(task.card_id || task.index)"
          >
            <span>{{ expandedTasks.has(task.card_id || task.index) ? "收起" : `${task.steps?.length || 0} 步骤` }}</span>
            <svg :class="{ open: expandedTasks.has(task.card_id || task.index) }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
          </button>

          <div v-if="expandedTasks.has(task.card_id || task.index)" class="task-steps">
            <div v-if="(task.steps || []).length" class="task-steps-count">共 {{ task.steps.length }} 条日志 · 可上下滚动</div>
            <div v-for="(step, index) in (task.steps || [])" :key="index" class="task-step">
              <span>{{ step.time }}</span>
              {{ step.text }}
            </div>
            <div v-if="!task.steps?.length" class="task-step muted">等待步骤就绪...</div>
          </div>
        </article>
      </div>

      <div class="bulkbar" style="margin-top: 14px; display: flex; align-items: center; gap: 8px;">
        <button class="btn danger" :disabled="!selected.length" @click="emit('delete-tasks', selected)">
          <Trash2 :size="14" aria-hidden="true" />
          删除选中（{{ selected.length }}）
        </button>
        <button class="btn danger" :disabled="!tasks.length" @click="emit('delete-tasks', null)">清空任务</button>
        <div style="flex: 1;"></div>
        <AppPager v-model:page="page" :pages="pages" :total="tasks.length" />
      </div>
    </section>
  </div>
</template>
