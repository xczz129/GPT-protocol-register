<script setup lang="ts">
/* --- 注册任务配置表单 --- */
import { computed, onMounted, reactive, ref, watch } from "vue";
import { Play } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useTasksStore } from "../../stores/tasks";
import { useGroupsStore } from "../../stores/groups";
import { useUiStore } from "../../stores/ui";
import { useStatusStore } from "../../stores/status";
import { fetchConfig } from "../../api/config";
import { normalizeRegistrationNumbers, smsbowerPolicy } from "../../../shared/mailRegistration";

const tasks = useTasksStore();
const groups = useGroupsStore();
const ui = useUiStore();
const status = useStatusStore();

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
  autoSmsAfterRegister: false,
});

const busy = ref(false);
const settingsLoading = ref(true);
const settingsError = ref("");
const smsbower = computed(() => (status.panel.mail?.providers || []).find((provider) => provider.type === "smsbower_mail"));
const registrationPolicy = computed(() => smsbowerPolicy(form.mailType, smsbower.value?.register_child));

function normalizeNumbers() {
  Object.assign(form, normalizeRegistrationNumbers(form, smsbower.value?.register_child));
}

watch(() => [form.mailType, smsbower.value?.register_child], normalizeNumbers);

async function loadSettings() {
  settingsLoading.value = true;
  settingsError.value = "";
  try {
    const { config } = await fetchConfig();
    form.autoSmsAfterRegister = config.registration?.auto_sms_after_register === true;
  } catch (error) {
    settingsError.value = error instanceof Error ? error.message : "注册设置加载失败";
  } finally {
    settingsLoading.value = false;
  }
}

onMounted(loadSettings);

async function submit() {
  if (busy.value || tasks.job.running || settingsLoading.value || settingsError.value) return;
  busy.value = true;
  try {
    if (form.mailType === "smsbower_mail") await status.load(true);
    normalizeNumbers();
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
      auto_sms_after_register: form.autoSmsAfterRegister,
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
        <span>{{ registrationPolicy.paired ? "注册数量（主号 + 子号）" : "注册数量" }}</span>
        <input v-model.number="form.total" class="input" type="number" :min="registrationPolicy.totalMin" :step="registrationPolicy.totalStep" max="500" @blur="normalizeNumbers" />
      </label>

      <label class="field">
        <span>并发数</span>
        <input v-model.number="form.concurrency" class="input" type="number" :min="registrationPolicy.concurrencyMin" :step="registrationPolicy.concurrencyStep" max="64" @blur="normalizeNumbers" />
      </label>

      <label class="field">
        <span>邮箱来源</span>
        <select v-model="form.mailType" class="input">
          <option value="auto">自动（全部可用邮箱池）</option>
          <option value="outlook">仅 Outlook</option>
          <option value="icloud">仅 iCloud</option>
          <option value="smsbower_mail">SMSBower 临时邮箱</option>
          <option value="online_disposablemail">OnlineDisposableMail 临时邮箱</option>
          <option value="domain">仅域名邮箱</option>
          <option value="maddy">仅 Maddy</option>
          <option value="fastmail">仅 Fastmail</option>
        </select>
      </label>

      <p v-if="registrationPolicy.selected" class="pair-hint">
        <template v-if="registrationPolicy.paired">{{ Math.ceil((Number(form.total) || 2) / 2) }} 组主子号 · 主号收码后保留邮箱，待子号收码流程结束再统一结算。启用子号时，数量、并发最低为 2，奇数补齐为偶数。</template>
        <template v-else>已关闭注册子号，仅注册主号；并发恢复普通规则，可使用 1、3、5 等整数。</template>
        <span>检测到邮箱已注册时跳过；子号开关在邮箱池设置。</span>
      </p>

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

      <div class="auto-sms-option">
        <label class="check">
          <input v-model="form.autoSmsAfterRegister" type="checkbox" role="switch" aria-describedby="auto-sms-hint" :disabled="busy || tasks.job.running || settingsLoading || !!settingsError" />
          <span>注册完成后自动接码</span>
        </label>
        <p id="auto-sms-hint">每个账号注册并测活后，在当前任务中继续接码，接码结束后才完成任务。沿用已有短信接码配置和费用上限，阶段和日志直接显示在任务卡片中。启动注册时保存此选项。</p>
      </div>
    </div>

    <div class="form-actions">
      <p v-if="settingsError" class="settings-error" role="alert">注册设置加载失败：{{ settingsError }} <button type="button" @click="loadSettings">重试</button></p>
      <AppButton variant="primary" :loading="busy || settingsLoading" :disabled="tasks.job.running || settingsLoading || !!settingsError" @click="submit">
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

.pair-hint {
  grid-column: 1 / -1;
  margin: 0;
  padding: 10px 12px;
  border-left: 2px solid var(--accent);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  background: var(--accent-bg);
  color: var(--text-secondary);
  font-size: 0.76rem;
  line-height: 1.6;
}

.pair-hint span { display: block; color: var(--text-muted); }

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

.auto-sms-option {
  grid-column: 1 / -1;
  padding-top: 8px;
  border-top: 1px solid var(--line-subtle);
}

.auto-sms-option p {
  margin: 6px 0 0 22px;
  font-size: 0.74rem;
  line-height: 1.6;
  color: var(--text-muted);
}

.settings-error {
  margin: 0 0 10px;
  color: var(--error);
  font-size: 0.75rem;
}

.settings-error button {
  border: 0;
  background: none;
  padding: 3px 6px;
  color: var(--accent);
  cursor: pointer;
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
