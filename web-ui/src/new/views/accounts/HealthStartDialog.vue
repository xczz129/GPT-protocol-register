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
    const completion = accounts.startHealth(props.keys, Boolean(props.all), workers);
    ui.notify(`账号测活请求已提交（并发 ${workers}）`, "success");
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
    <p class="start-message">检查账号是否可用，区分存活、凭据失效、封号和检测异常。已封号账号会自动跳过。</p>

    <div class="field-row">
      <label class="field-label" for="health-workers">并发数</label>
      <input id="health-workers" v-model.number="concurrency" class="field-input" type="number" min="1" max="20" step="1" :disabled="loadingSettings || submitting" />
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
