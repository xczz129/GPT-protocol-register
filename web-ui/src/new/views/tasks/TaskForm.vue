<script setup lang="ts">
/* --- 注册任务配置表单 --- */
import { reactive, ref } from "vue";
import { Play } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useTasksStore } from "../../stores/tasks";
import { useGroupsStore } from "../../stores/groups";
import { useUiStore } from "../../stores/ui";

const tasks = useTasksStore();
const groups = useGroupsStore();
const ui = useUiStore();

const form = reactive({
  engine: "protocol",
  total: 1,
  concurrency: 1,
  mailType: "auto",
  email: "",
  keepOnFail: false,
  groupId: "",
  autoGroup: false,
  autoGroupSize: 35,
});

const busy = ref(false);

async function submit() {
  if (busy.value) return;
  busy.value = true;
  try {
    await tasks.start({
      engine: form.engine,
      total: Math.max(1, Math.min(500, Math.floor(form.total) || 1)),
      concurrency: Math.max(1, Math.floor(form.concurrency) || 1),
      keep_on_fail: form.keepOnFail,
      email: form.email.trim() || null,
      mail_type: form.mailType,
      group_id: form.groupId || null,
      auto_group: form.autoGroup,
      auto_group_size: Math.max(1, Math.min(500, Math.floor(form.autoGroupSize) || 35)),
    });
    ui.notify("注册任务已启动", "success");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "启动失败", "error");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="task-form glass-panel">
    <h2 class="panel-title">注册任务配置</h2>
    <p class="panel-sub">配置引擎、数量、并发与邮箱来源，启动批量注册</p>

    <div class="form-grid">
      <label class="field full">
        <span>注册引擎</span>
        <select v-model="form.engine" class="input">
          <option value="protocol">协议注册（原生 Refresh Token）</option>
          <option value="browser">全程指纹浏览器（自动补取 Refresh）</option>
          <option value="hybrid">会话内混合（自动补取 Refresh）</option>
        </select>
      </label>

      <label class="field">
        <span>注册数量</span>
        <input v-model.number="form.total" class="input" type="number" min="1" max="500" />
      </label>

      <label class="field">
        <span>并发数</span>
        <input v-model.number="form.concurrency" class="input" type="number" min="1" max="64" />
      </label>

      <label class="field">
        <span>邮箱来源</span>
        <select v-model="form.mailType" class="input">
          <option value="auto">自动（全部可用邮箱池）</option>
          <option value="outlook">仅 Outlook</option>
          <option value="icloud">仅 iCloud</option>
          <option value="domain">仅域名邮箱</option>
          <option value="maddy">仅 Maddy</option>
          <option value="fastmail">仅 Fastmail</option>
        </select>
      </label>

      <label class="field">
        <span>目标分组</span>
        <select v-model="form.groupId" class="input">
          <option value="">默认分组</option>
          <option v-for="group in groups.items" :key="group.id" :value="group.id">{{ group.name }}</option>
        </select>
      </label>

      <label class="field full">
        <span>指定邮箱（可选，仅使用该邮箱注册）</span>
        <input v-model="form.email" class="input" type="email" placeholder="留空则从邮箱池自动分配" />
      </label>

      <label class="check">
        <input v-model="form.keepOnFail" type="checkbox" />
        <span>失败时保留邮箱占用（便于排查）</span>
      </label>

      <label class="check">
        <input v-model="form.autoGroup" type="checkbox" />
        <span>自动分组，每组</span>
        <input v-model.number="form.autoGroupSize" class="input inline-num" type="number" min="1" max="500" :disabled="!form.autoGroup" />
        <span>个账号</span>
      </label>
    </div>

    <div class="form-actions">
      <AppButton variant="primary" :loading="busy" :disabled="tasks.job.running" @click="submit">
        <Play :size="15" /><span>{{ tasks.job.running ? "任务运行中" : "启动注册任务" }}</span>
      </AppButton>
    </div>
  </section>
</template>

<style scoped>
.task-form {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.panel-sub {
  font-size: 0.76rem;
  color: var(--text-muted);
  margin-top: -10px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field.full {
  grid-column: 1 / -1;
}

.field > span {
  font-size: 0.76rem;
  color: var(--text-secondary);
  font-weight: 500;
}

.check {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  cursor: pointer;
}

.check input[type="checkbox"] {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
}

.inline-num {
  width: 70px;
  padding: 4px 8px;
  font-size: 0.78rem;
}

.form-actions {
  border-top: 1px solid var(--line-subtle);
  padding-top: 14px;
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
