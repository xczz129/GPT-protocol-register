<script setup lang="ts">
/* --- 运维中心 · 备份与恢复 + 账号回收站 --- */
import { computed, reactive, ref, watch } from "vue";
import { Archive, Download, RotateCcw } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { backupDownloadUrl } from "../../api/ops";
import { useOperationsStore } from "../../stores/operations";
import { useUiStore } from "../../stores/ui";

const ops = useOperationsStore();
const ui = useUiStore();

/* 双列布局下按区块拆分渲染：backup 备份恢复 / trash 回收站 */
const props = withDefaults(defineProps<{ cards?: Array<"backup" | "trash"> }>(), {
  cards: () => ["backup", "trash"],
});
const visible = computed(() => new Set(props.cards));

const scheduleForm = reactive({ enabled: false, time: "04:00", retentionDays: 7 });

let inited = false;
watch(
  () => ops.data,
  (data) => {
    if (!data || inited) return;
    inited = true;
    const s = data.backup_schedule;
    scheduleForm.enabled = !!s.enabled;
    scheduleForm.time = String(s.time || "04:00");
    scheduleForm.retentionDays = Number(s.retention_days ?? 7);
  },
  { immediate: true },
);

const backups = computed(() => ops.data?.backups || []);
const trash = computed(() => ops.trash);
const selectedTrash = ref<string[]>([]);

function formatTime(value: unknown): string {
  if (!value) return "-";
  try {
    const date = new Date(String(value));
    return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return "-";
  }
}

function formatSize(value: unknown): string {
  const size = Number(value || 0);
  return size > 1048576 ? `${(size / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`;
}

async function saveSchedule() {
  try {
    await ops.saveBackupSchedule({
      enabled: scheduleForm.enabled,
      time: scheduleForm.time,
      retention_days: scheduleForm.retentionDays,
    });
    ui.notify("定时备份设置已保存", "success");
  } catch {
    ui.notify("定时备份设置保存失败", "error");
  }
}

async function backupNow() {
  try {
    await ops.createBackupNow();
    ui.notify("备份已创建", "success");
  } catch {
    ui.notify("备份创建失败", "error");
  }
}

async function restore(name: string) {
  if (
    !(await ui.askConfirm(`恢复备份 ${name}？将覆盖当前数据库与配置文件，且任务运行时不可恢复。`, {
      title: "恢复备份",
      confirmText: "恢复",
      danger: true,
    }))
  )
    return;
  try {
    const result = await ops.restoreBackupByName(name);
    ui.notify(result.message || "备份已恢复，请刷新页面", "success", 6000);
  } catch (err) {
    ui.notify(err instanceof Error ? err.message : "备份恢复失败", "error");
  }
}

async function restoreSelected() {
  if (!selectedTrash.value.length) return;
  try {
    const result = await ops.restoreTrashIds(selectedTrash.value);
    ui.notify(`已恢复 ${result.restored || 0} 个账号`, "success");
    selectedTrash.value = [];
  } catch {
    ui.notify("回收站恢复失败", "error");
  }
}
</script>

<template>
  <section v-if="visible.has('backup')" class="card glass-panel">
    <div class="card-head">
      <div class="card-head-row">
        <div>
          <h2 class="section-title">备份与恢复</h2>
          <p class="card-sub">备份数据库、配置、分组和邮箱状态</p>
        </div>
        <AppButton variant="primary" :loading="ops.busy['backup']" @click="backupNow">
          <span class="btn-inner"><Archive :size="13" />立即备份</span>
        </AppButton>
      </div>
    </div>

    <div class="form-grid">
      <label class="check"><input v-model="scheduleForm.enabled" type="checkbox" />启用定时备份</label>
      <label class="field">
        <span>每天备份时间</span>
        <input v-model="scheduleForm.time" class="input" type="time" />
      </label>
      <label class="field">
        <span>保留天数</span>
        <input v-model.number="scheduleForm.retentionDays" class="input" type="number" min="1" max="90" />
      </label>
    </div>
    <div class="actions">
      <AppButton variant="primary" :loading="ops.busy['backup-schedule']" @click="saveSchedule">保存定时设置</AppButton>
    </div>
    <p class="hint">
      定时：{{ scheduleForm.enabled ? `每天 ${scheduleForm.time}` : "未启用" }} · 保留 {{ scheduleForm.retentionDays }} 天
      · 最近自动备份：{{ formatTime(ops.data?.state?.backup_last_at) }}
      <template v-if="ops.data?.state?.backup_last_error">
        · <span class="danger">{{ ops.data.state.backup_last_error }}</span>
      </template>
    </p>

    <div class="rows">
      <div v-if="!backups.length" class="rows-empty">暂无备份</div>
      <div v-for="item in backups" :key="item.name" class="row">
        <div class="row-info">
          <b>
            {{ item.name }}
            <AppTag v-if="item.auto" kind="info">自动</AppTag>
          </b>
          <small>{{ formatTime(item.created_at) }} · {{ formatSize(item.size) }}</small>
        </div>
        <div class="row-btns">
          <a class="link-btn" :href="backupDownloadUrl(item.name)">
            <Download :size="13" />下载
          </a>
          <AppButton size="sm" variant="danger" :loading="ops.busy['backup-restore']" @click="restore(item.name)">
            <span class="btn-inner"><RotateCcw :size="13" />恢复</span>
          </AppButton>
        </div>
      </div>
    </div>
  </section>

  <section v-if="visible.has('trash')" class="card glass-panel">
    <div class="card-head">
      <h2 class="section-title">账号回收站</h2>
      <p class="card-sub">网页删除的账号可在这里恢复，共 {{ trash.length }} 条</p>
    </div>
    <div class="rows trash-rows">
      <div v-if="!trash.length" class="rows-empty">回收站为空</div>
      <label v-for="item in trash.slice(0, 50)" :key="item.trash_id" class="row selectable">
        <input v-model="selectedTrash" type="checkbox" :value="item.trash_id" />
        <div class="row-info">
          <b>{{ item.account?.email || item.account?.account_id || "未知账号" }}</b>
          <small>
            <span class="trash-reason">{{ item.reason }}</span>
            <span class="trash-time">{{ formatTime(item.deleted_at) }}</span>
          </small>
        </div>
      </label>
    </div>
    <div class="actions">
      <AppButton :disabled="!selectedTrash.length" :loading="ops.busy['trash-restore']" @click="restoreSelected">
        <span class="btn-inner"><RotateCcw :size="13" />恢复选中（{{ selectedTrash.length }}）</span>
      </AppButton>
      <AppButton size="sm" :loading="ops.busy['trash']" @click="ops.loadTrash(true)">刷新</AppButton>
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

.card-head-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.card-head .section-title {
  margin-bottom: 3px;
}

.card-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 10px 14px;
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
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

.check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  cursor: pointer;
}

.check input,
.row.selectable input {
  accent-color: var(--accent);
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}

.btn-inner {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.hint {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.hint .danger {
  color: var(--error);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 300px;
  overflow-y: auto;
}

.rows-empty {
  font-size: 0.78rem;
  color: var(--text-muted);
  text-align: center;
  padding: 20px 0;
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
}

.row.selectable {
  justify-content: flex-start;
  cursor: pointer;
}

.row-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.row-info b {
  font-size: 0.8rem;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 6px;
  word-break: break-all;
}

.row-info small {
  font-size: 0.7rem;
  color: var(--text-muted);
  display: flex;
  gap: 8px;
}

.row-btns {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
  align-items: center;
}

.link-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.75rem;
  color: var(--accent);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--field-border);
  background: var(--glass-bg);
  white-space: nowrap;
}

.link-btn:hover {
  background: var(--glass-bg-hover);
}

.trash-reason {
  color: var(--warning);
}

.trash-time {
  color: var(--text-muted);
}
</style>
