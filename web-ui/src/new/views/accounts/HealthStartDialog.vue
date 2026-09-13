<script setup lang="ts">
/* --- 测活启动弹窗：读取服务端默认并发，并允许单次覆盖 --- */
import { computed, ref, watch } from "vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppButton from "../../components/ui/AppButton.vue";
import { fetchUiSettings } from "../../api/settings";
import { useAccountsStore } from "../../stores/accounts";
import { useUiStore } from "../../stores/ui";

const props = defineProps<{
  open: boolean;
  keys: string[];
  all?: boolean;
}>();

const emit = defineEmits<{ close: []; started: [workers: number] }>();

const accounts = useAccountsStore();
const ui = useUiStore();
const concurrency = ref(10);
const channel = ref<"v4" | "nerver">("v4");
const loadingSettings = ref(false);
const submitting = ref(false);
let openGeneration = 0;

const scopeText = computed(() => props.all ? `全部未封号账号（当前约 ${accounts.activeCount} 个）` : `${props.keys.length} 个选中账号`);

function clampWorkers(value: unknown): number {
  const parsed = Math.floor(Number(value));
  return Math.max(1, Math.min(20, Number.isFinite(parsed) ? parsed : 10));
}

watch(() => props.open, (open) => {
  if (!open) {
    openGeneration += 1;
    return;
  }
  const generation = ++openGeneration;
  submitting.value = false;
  channel.value = "v4";
  loadingSettings.value = true;
  void fetchUiSettings()
    .then((settings) => {
      if (generation === openGeneration) concurrency.value = clampWorkers(settings.health_workers);
    })
    .catch(() => {
      // Keep the local fallback when settings cannot be read.
    })
    .finally(() => {
      if (generation === openGeneration) loadingSettings.value = false;
    });
}, { immediate: true });

async function submit() {
  if (submitting.value || loadingSettings.value || accounts.health.running || accounts.healthSubmitting || (!props.all && !props.keys.length)) return;
  submitting.value = true;
  const workers = clampWorkers(concurrency.value);
  try {
    const completion = accounts.startHealth(props.keys, Boolean(props.all), workers, channel.value);
    ui.notify(`测活请求已提交（${channel.value}，并发 ${workers}）`, "success");
    emit("close");
    await completion;
    emit("started", workers);
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "测活启动失败", "error");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <AppModal :open="open" title="启动账号测活" :subtitle="scopeText" width="460px" @close="emit('close')">
    <p class="start-message">ChatGPT /me 与会话接口负责检查账号有效性；所选渠道只影响资格与套餐探测。已封号账号会自动跳过。</p>

    <div class="field-row channel-row">
      <span class="field-label">检测渠道</span>
      <div class="segmented" role="radiogroup" aria-label="检测渠道">
        <button type="button" :class="{ active: channel === 'v4' }" :aria-checked="channel === 'v4'" role="radio" :disabled="submitting" @click="channel = 'v4'">v4 接口</button>
        <button type="button" :class="{ active: channel === 'nerver' }" :aria-checked="channel === 'nerver'" role="radio" :disabled="submitting" @click="channel = 'nerver'">nerver 接口</button>
      </div>
    </div>
    <p class="channel-note">{{ channel === "nerver" ? "nerver 仅辅助探测资格与套餐，不会单独把账号判为存活。" : "v4 通过 ChatGPT 账号接口探测资格与套餐。" }}</p>

    <div class="field-row">
      <span class="field-label">并发数</span>
      <input v-model.number="concurrency" class="field-input" type="number" min="1" max="20" step="1" :disabled="loadingSettings || submitting" />
      <span class="field-hint">1–20</span>
    </div>
    <p v-if="loadingSettings" class="settings-note">正在读取并发默认值…</p>

    <template #footer>
      <AppButton variant="ghost" :disabled="submitting" @click="emit('close')">取消</AppButton>
      <AppButton variant="primary" :disabled="loadingSettings || submitting" :loading="submitting" @click="submit">开始测活</AppButton>
    </template>
  </AppModal>
</template>

<style scoped>
.start-message {
  margin: 0 0 16px;
  color: var(--text-secondary);
  font-size: 0.82rem;
  line-height: 1.7;
}

.field-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.channel-row {
  align-items: flex-start;
}

.segmented {
  display: inline-flex;
  min-height: 34px;
  padding: 2px;
  border: 1px solid var(--field-border);
  border-radius: var(--radius-sm);
  background: var(--field-bg);
}

.segmented button {
  min-width: 92px;
  padding: 6px 10px;
  border: 0;
  border-radius: calc(var(--radius-sm) - 2px);
  background: transparent;
  color: var(--text-muted);
  font-size: 0.78rem;
  cursor: pointer;
}

.segmented button.active {
  background: var(--accent-bg);
  color: var(--accent);
}

.segmented button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.channel-note {
  margin: 0 0 14px 70px;
  color: var(--text-muted);
  font-size: 0.72rem;
  line-height: 1.55;
}

.field-label {
  min-width: 60px;
  color: var(--text-muted);
  font-size: 0.78rem;
}

.field-input {
  width: 88px;
  padding: 7px 12px;
  border: 1px solid var(--field-border);
  border-radius: var(--radius-sm);
  background: var(--field-bg);
  color: var(--text-primary);
  font-size: 0.83rem;
  text-align: center;
  outline: none;
}

.field-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-bg);
}

.field-hint,
.settings-note {
  color: var(--text-muted);
  font-size: 0.72rem;
}

.settings-note {
  margin: 0 0 4px 70px;
}
</style>
