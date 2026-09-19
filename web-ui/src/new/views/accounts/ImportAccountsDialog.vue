<script setup lang="ts">
/* --- 导入账号弹窗：粘贴 邮箱----密码----2FA，协议登录写入账号库 --- */
import { computed, ref, watch } from "vue";
import { ScrollText, Upload, Users, Zap } from "@lucide/vue";
import AppModal from "../../components/ui/AppModal.vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useBatchOpsStore } from "../../stores/batchops";
import { useUiStore } from "../../stores/ui";
import { IMPORT_MAX_CONCURRENCY, parseImportCredentials, validImportConcurrency } from "../../api/batchops";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: []; history: [] }>();

const batch = useBatchOpsStore();
const ui = useUiStore();

const text = ref("");
const accountType = ref("personal");
const workspace = ref("");
const concurrency = ref(3);
const submitting = ref(false);

watch(() => props.open, (value) => {
  if (value) {
    accountType.value = "personal";
    workspace.value = "";
    concurrency.value = 3;
  }
});

const parsed = computed(() => parseImportCredentials(text.value));
const concurrencyInvalid = computed(() => !validImportConcurrency(Number(concurrency.value)));
const workspaceMissing = computed(() => accountType.value === "team" && !workspace.value.trim());
const canSubmit = computed(() => parsed.value.rows.length > 0 && !parsed.value.errors.length
  && !workspaceMissing.value && !concurrencyInvalid.value && !submitting.value);

async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  try {
    await batch.startImport({
      text: text.value,
      accountType: accountType.value,
      workspace: workspace.value.trim(),
      concurrency: Number(concurrency.value),
    });
    emit("close");
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "导入任务启动失败", "error");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <AppModal :open="open" title="导入账号" subtitle="协议登录(密码 + TOTP)写入账号库，套餐由登录后自动识别" width="600px" @close="emit('close')">
    <template #icon><Upload :size="18" /></template>

    <div class="history-entry">
      <button class="history-btn" type="button" @click="emit('history')">
        <ScrollText :size="13" />历史日志
      </button>
    </div>

    <textarea
      v-model="text"
      class="import-textarea"
      placeholder="每行一条：邮箱----密码----2FA密钥(Base32)&#10;# 开头的行会被忽略"
      spellcheck="false"
    ></textarea>
    <p class="hint">
      已识别 <b>{{ parsed.rows.length }}</b> 个账号<template v-if="parsed.errors.length"> · {{ parsed.errors.length }} 行有误，请全部修正后导入</template>
    </p>
    <ul v-if="parsed.errors.length" class="validation-errors" role="alert">
      <li v-for="error in parsed.errors.slice(0, 10)" :key="error">{{ error }}</li>
      <li v-if="parsed.errors.length > 10">还有 {{ parsed.errors.length - 10 }} 行需要修正</li>
    </ul>

    <div class="form-grid">
      <div class="field">
        <span class="field-label">账号类型</span>
        <div class="segmented">
          <button type="button" :class="{ active: accountType === 'personal' }" @click="accountType = 'personal'">个人</button>
          <button type="button" :class="{ active: accountType === 'team' }" @click="accountType = 'team'">
            <Users :size="12" />Team
          </button>
        </div>
      </div>
      <div class="field">
        <span class="field-label"><Zap :size="11" /> 并发（1–{{ IMPORT_MAX_CONCURRENCY }}）</span>
        <input v-model.number="concurrency" class="field-input concurrency-input" type="number" min="1" :max="IMPORT_MAX_CONCURRENCY" step="1" />
        <small v-if="concurrencyInvalid" class="warn-text">请输入 1–{{ IMPORT_MAX_CONCURRENCY }} 的整数</small>
      </div>
      <div v-if="accountType === 'team'" class="field field-grow">
        <span class="field-label">工作区名称</span>
        <input v-model="workspace" class="field-input" type="text" placeholder="如 miaomiao" spellcheck="false" autocomplete="off" />
        <small v-if="workspaceMissing" class="warn-text">Team 账号需要填写工作区名称</small>
      </div>
    </div>

    <template #footer>
      <AppButton variant="ghost" @click="emit('close')">取消</AppButton>
      <AppButton variant="primary" :disabled="!canSubmit" :loading="submitting" @click="submit">
        开始导入
      </AppButton>
    </template>
  </AppModal>
</template>

<style scoped>
.history-entry {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}

.history-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border-radius: 8px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg);
  color: var(--text-secondary);
  font-size: 0.75rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.history-btn:hover {
  color: var(--accent);
  border-color: var(--accent-border);
}

.import-textarea {
  width: 100%;
  min-height: 150px;
  padding: 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--field-bg);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 0.78rem;
  line-height: 1.6;
  resize: vertical;
  outline: none;
  transition: var(--transition-fast);
}

.import-textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-bg);
}

.hint {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin: 8px 0 12px;
}

.hint b { color: var(--accent); }

.validation-errors {
  color: var(--error);
  font-size: 0.75rem;
  line-height: 1.6;
  margin: 0 0 12px;
  padding-left: 20px;
}

.form-grid {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  align-items: flex-start;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-grow { flex: 1; min-width: 180px; }

.field-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.field-input {
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--field-bg);
  color: var(--text-primary);
  font-size: 0.83rem;
  outline: none;
  transition: var(--transition-fast);
}

.field-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-bg);
}

.concurrency-input {
  width: 88px;
  text-align: center;
}

.segmented {
  display: inline-flex;
  padding: 3px;
  border-radius: 10px;
  background: var(--inset-bg);
  border: 1px solid var(--line-subtle);
  gap: 2px;
}

.segmented button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-radius: 8px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 0.8rem;
  cursor: pointer;
  transition: var(--transition-fast);
}

.segmented button.active {
  background: var(--accent);
  color: #fff;
}

.warn-text {
  color: var(--warning);
  font-size: 0.72rem;
}
</style>
