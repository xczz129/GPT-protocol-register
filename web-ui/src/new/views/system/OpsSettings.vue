<script setup lang="ts">
/* --- 运维中心 · 并发默认值（ui_settings）+ 面板安全 --- */
import { computed, onMounted, reactive, ref } from "vue";
import AppButton from "../../components/ui/AppButton.vue";
import { fetchUiSettings, updateUiSettings, type UiSettings } from "../../api/settings";
import { changePassword, revokeSessions } from "../../api/auth";
import { useUiStore } from "../../stores/ui";

const ui = useUiStore();

/* 双列布局下按区块拆分渲染：workers 并发默认值 / security 面板安全 */
const props = withDefaults(defineProps<{ cards?: Array<"workers" | "security"> }>(), {
  cards: () => ["workers", "security"],
});
const visible = computed(() => new Set(props.cards));

/* --- 并发默认值 --- */
const workersForm = reactive<UiSettings>({
  health_workers: 10,
  reauth_workers: 3,
  trial_workers: 20,
  refresh_at_workers: 10,
  codex_sms_workers: 2,
  reauth_mode: "protocol",
  trial_channel: "v4",
});
const workersLoading = ref(false);
const workersSaving = ref(false);

async function loadSettings() {
  workersLoading.value = true;
  try {
    const data = await fetchUiSettings();
    Object.assign(workersForm, data);
  } catch {
    ui.notify("并发设置加载失败", "error");
  } finally {
    workersLoading.value = false;
  }
}

async function saveSettings() {
  if (workersSaving.value) return;
  if ([workersForm.health_workers, workersForm.reauth_workers, workersForm.trial_workers, workersForm.refresh_at_workers, workersForm.codex_sms_workers].some((value) => !Number.isSafeInteger(value) || value < 1)) {
    ui.notify("并发数必须为正整数", "error");
    return;
  }
  workersSaving.value = true;
  try {
    await updateUiSettings({ ...workersForm });
    ui.notify("并发默认值已保存", "success");
  } catch {
    ui.notify("并发默认值保存失败", "error");
  } finally {
    workersSaving.value = false;
  }
}

/* --- 面板安全 --- */
const security = reactive({ currentPassword: "", newPassword: "", confirmPassword: "" });
const securityBusy = ref(false);
const revokeBusy = ref(false);

const canChangePassword = computed(
  () =>
    security.currentPassword.length > 0 &&
    security.newPassword.length >= 8 &&
    security.newPassword === security.confirmPassword,
);

async function submitPassword() {
  if (!canChangePassword.value) return;
  securityBusy.value = true;
  try {
    await changePassword(security.currentPassword, security.newPassword);
    security.currentPassword = "";
    security.newPassword = "";
    security.confirmPassword = "";
    ui.notify("密码已修改，其他会话已失效", "success", 5000);
  } catch (err) {
    ui.notify(err instanceof Error ? err.message : "密码修改失败", "error");
  } finally {
    securityBusy.value = false;
  }
}

async function revokeOthers() {
  if (!(await ui.askConfirm("退出除当前会话外的所有登录会话？", { title: "退出其他会话", confirmText: "退出", danger: true })))
    return;
  revokeBusy.value = true;
  try {
    await revokeSessions();
    ui.notify("其他会话已全部退出", "success");
  } catch {
    ui.notify("操作失败", "error");
  } finally {
    revokeBusy.value = false;
  }
}

onMounted(() => {
  if (visible.value.has("workers")) loadSettings();
});
</script>

<template>
  <section v-if="visible.has('workers')" class="card glass-panel">
    <div class="card-head">
      <h2 class="section-title">并发默认值</h2>
      <p class="card-sub">批量任务的默认并发数；单次任务可在发起时覆盖</p>
    </div>
    <div v-if="workersLoading" class="card-loading">加载中…</div>
    <template v-else>
      <div class="form-grid">
        <label class="field">
          <span>测活并发</span>
          <input v-model.number="workersForm.health_workers" class="input" type="number" min="1" step="1" />
        </label>
        <label class="field">
          <span>重登并发</span>
          <input v-model.number="workersForm.reauth_workers" class="input" type="number" min="1" step="1" />
        </label>
        <label class="field">
          <span>试用检测并发</span>
          <input v-model.number="workersForm.trial_workers" class="input" type="number" min="1" step="1" />
        </label>
        <label class="field">
          <span>刷新 AT 并发</span>
          <input v-model.number="workersForm.refresh_at_workers" class="input" type="number" min="1" step="1" />
        </label>
        <label class="field">
          <span>接码默认并发</span>
          <input v-model.number="workersForm.codex_sms_workers" class="input" type="number" min="1" step="1" />
        </label>
        <label class="field">
          <span>重登方式</span>
          <select v-model="workersForm.reauth_mode" class="input">
            <option value="protocol">协议登录</option>
            <option value="browser">浏览器登录</option>
          </select>
        </label>
        <label class="field">
          <span>试用检测渠道</span>
          <select v-model="workersForm.trial_channel" class="input">
            <option value="v4">v4</option>
            <option value="nerver">nerver</option>
          </select>
        </label>
      </div>
      <div class="actions">
        <AppButton variant="primary" :loading="workersSaving" @click="saveSettings">保存并发默认值</AppButton>
      </div>
    </template>
  </section>

  <section v-if="visible.has('security')" class="card glass-panel">
    <div class="card-head">
      <h2 class="section-title">面板安全</h2>
      <p class="card-sub">登录失败自动限速；修改密码会使其他会话失效</p>
    </div>
    <div class="form-grid">
      <label class="field span-2">
        <span>当前密码</span>
        <input v-model="security.currentPassword" class="input" type="password" autocomplete="current-password" />
      </label>
      <label class="field">
        <span>新密码（至少 8 位）</span>
        <input v-model="security.newPassword" class="input" type="password" minlength="8" autocomplete="new-password" />
      </label>
      <label class="field">
        <span>确认新密码</span>
        <input v-model="security.confirmPassword" class="input" type="password" minlength="8" autocomplete="new-password" />
      </label>
    </div>
    <div class="actions">
      <AppButton variant="primary" :disabled="!canChangePassword" :loading="securityBusy" @click="submitPassword">
        修改密码
      </AppButton>
      <AppButton variant="danger" :loading="revokeBusy" @click="revokeOthers">退出其他会话</AppButton>
    </div>
  </section>
</template>

<style scoped>
.card {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.card-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.card-head .section-title {
  margin-bottom: 0;
}

.card-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.card-loading {
  font-size: 0.8rem;
  color: var(--text-muted);
  padding: 12px 0;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .span-2 {
    grid-column: span 1 !important;
  }
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 0.76rem;
  color: var(--text-secondary);
}

.field .input {
  width: 100%;
}

.span-2 {
  grid-column: span 2;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
