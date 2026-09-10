<script setup lang="ts">
/* --- 账号操作日志对话框 --- */
import { ref, watch } from "vue";
import { X } from "@lucide/vue";
import { fetchAccountLogs, type AccountLogEntry } from "../../api/accounts";
import AppEmpty from "../../components/ui/AppEmpty.vue";
import { ClipboardList } from "@lucide/vue";

const props = defineProps<{
  accountKey: string;
  open: boolean;
}>();

const emit = defineEmits<{
  close: [];
}>();

const logs = ref<AccountLogEntry[]>([]);
const loading = ref(false);
const error = ref("");

async function load() {
  if (!props.accountKey) return;
  loading.value = true;
  error.value = "";
  try {
    const data = await fetchAccountLogs(props.accountKey);
    logs.value = data.items || data.logs || [];
  } catch (err) {
    error.value = err instanceof Error ? err.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.open, props.accountKey],
  ([open]) => {
    if (open) load();
  },
);

function logText(entry: AccountLogEntry): string {
  return String(entry.message || entry.action || entry.detail || JSON.stringify(entry));
}

function logTime(entry: AccountLogEntry): string {
  return String(entry.at || entry.time || "");
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div v-if="open" class="log-mask" @click.self="emit('close')">
        <section class="log-dialog glass-panel" role="dialog" aria-modal="true" aria-label="账号操作日志">
          <header class="log-head">
            <div>
              <h3>操作日志</h3>
              <p class="log-key">{{ accountKey }}</p>
            </div>
            <button class="log-close" type="button" aria-label="关闭" @click="emit('close')">
              <X :size="16" />
            </button>
          </header>

          <div class="log-body">
            <div v-if="loading" class="log-state">加载中…</div>
            <div v-else-if="error" class="log-state error">{{ error }}</div>
            <AppEmpty v-else-if="!logs.length" title="暂无操作日志">
              <template #icon><ClipboardList :size="40" :stroke-width="1" /></template>
            </AppEmpty>
            <div v-else class="log-list">
              <p v-for="(entry, index) in logs" :key="index" class="log-line">
                <span class="log-time">{{ logTime(entry) }}</span>
                {{ logText(entry) }}
              </p>
            </div>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.log-mask {
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(30, 41, 59, 0.25);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.log-dialog {
  width: 640px;
  max-width: 100%;
  max-height: 78vh;
  display: flex;
  flex-direction: column;
  padding: 18px 20px;
}

.log-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line-subtle);
}

.log-head h3 {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.log-key {
  font-size: 0.72rem;
  color: var(--text-muted);
  margin-top: 2px;
  overflow-wrap: anywhere;
}

.log-close {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px;
  border-radius: 6px;
}

.log-close:hover {
  background: var(--glass-bg-hover);
  color: var(--text-primary);
}

.log-body {
  flex: 1;
  overflow-y: auto;
  padding-top: 10px;
  min-height: 200px;
}

.log-state {
  text-align: center;
  color: var(--text-muted);
  font-size: 0.82rem;
  padding: 40px 0;
}

.log-state.error {
  color: var(--error);
}

.log-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.log-line {
  font-size: 0.78rem;
  color: var(--text-secondary);
  line-height: 1.55;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--glass-bg);
  overflow-wrap: anywhere;
}

.log-time {
  color: var(--text-muted);
  font-family: var(--mono);
  font-size: 0.7rem;
  margin-right: 6px;
}

.dialog-enter-active,
.dialog-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-enter-from,
.dialog-leave-to {
  opacity: 0;
}
</style>
