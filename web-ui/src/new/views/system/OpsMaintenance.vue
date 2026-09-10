<script setup lang="ts">
/* --- 运维中心 · 自动维护 + 中转账号额度 --- */
import { computed, reactive, watch } from "vue";
import { Play, Send } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import { useOperationsStore } from "../../stores/operations";
import { useUiStore } from "../../stores/ui";

const ops = useOperationsStore();
const ui = useUiStore();

/* 双列布局下按区块拆分渲染：maintenance 自动维护 / quota 中转额度 */
const props = withDefaults(defineProps<{ cards?: Array<"maintenance" | "quota"> }>(), {
  cards: () => ["maintenance", "quota"],
});
const visible = new Set(props.cards);

/* --- 自动维护表单（webhook 留空表示沿用已保存值） --- */
const form = reactive({
  enabled: false,
  autoDeleteInvalid: false,
  intervalHours: 12,
  workers: 10,
  webhookUrl: "",
  notifyOnComplete: false,
  notifyOnPoolLow: false,
  poolLowThreshold: 3,
});

/* --- 中转账号额度表单 --- */
const quotaForm = reactive({ enabled: true, intervalSeconds: 60 });

let inited = false;
watch(
  () => ops.data,
  (data) => {
    if (!data || inited) return;
    inited = true;
    const m = data.maintenance;
    form.enabled = !!m.enabled;
    form.autoDeleteInvalid = !!m.auto_delete_invalid;
    form.intervalHours = Number(m.interval_hours ?? 12);
    form.workers = Number(m.workers ?? 10);
    form.notifyOnComplete = !!m.notify_on_complete;
    form.notifyOnPoolLow = !!m.notify_on_pool_low;
    form.poolLowThreshold = Number(m.pool_low_threshold ?? 3);
    const q = data.cliproxy_quota;
    quotaForm.enabled = q.enabled !== false;
    quotaForm.intervalSeconds = Number(q.interval_seconds ?? 60);
  },
  { immediate: true },
);

const lastHealthAt = computed(() => formatTime(ops.data?.state?.last_health_at));
const webhookPlaceholder = computed(() =>
  ops.data?.maintenance?.has_webhook ? `已保存 ${ops.data.maintenance.webhook_masked}` : "https://...",
);

function formatTime(value: unknown): string {
  if (!value) return "-";
  try {
    const date = new Date(String(value));
    return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return "-";
  }
}

async function saveMaintenance() {
  try {
    await ops.saveMaintenance({
      enabled: form.enabled,
      interval_hours: form.intervalHours,
      workers: form.workers,
      auto_delete_invalid: form.autoDeleteInvalid,
      webhook_url: form.webhookUrl.trim(),
      notify_on_complete: form.notifyOnComplete,
      notify_on_pool_low: form.notifyOnPoolLow,
      pool_low_threshold: form.poolLowThreshold,
    });
    form.webhookUrl = "";
    ui.notify("维护设置已保存", "success");
  } catch {
    ui.notify("维护设置保存失败", "error");
  }
}

async function runNow() {
  try {
    await ops.runMaintenanceNow();
    ui.notify("全量测活已完成", "success");
    ops.refresh().catch(() => {});
  } catch {
    ui.notify("测活执行失败", "error");
  }
}

async function testWebhook() {
  try {
    const result = await ops.testWebhook(form.webhookUrl.trim());
    ui.notify(result.message || (result.ok ? "Webhook 测试成功" : "Webhook 测试失败"), result.ok ? "success" : "error");
  } catch {
    ui.notify("Webhook 测试请求失败", "error");
  }
}

async function saveQuota() {
  try {
    await ops.saveCliproxyQuota({ enabled: quotaForm.enabled, interval_seconds: quotaForm.intervalSeconds });
    ui.notify("额度刷新设置已保存", "success");
  } catch {
    ui.notify("额度刷新设置保存失败", "error");
  }
}
</script>

<template>
  <section v-if="visible.has('maintenance')" class="card glass-panel">
    <div class="card-head">
      <h2 class="section-title">自动维护</h2>
      <p class="card-sub">定时测活、清理失效账号和资源预警</p>
    </div>
    <div class="form-grid">
      <label class="check"><input v-model="form.enabled" type="checkbox" />启用定时测活</label>
      <label class="check"><input v-model="form.autoDeleteInvalid" type="checkbox" />自动移入回收站</label>
      <label class="field">
        <span>检测间隔（小时）</span>
        <input v-model.number="form.intervalHours" class="input" type="number" min="1" max="168" />
      </label>
      <label class="field">
        <span>测活并发</span>
        <input v-model.number="form.workers" class="input" type="number" min="1" max="20" />
      </label>
      <label class="field span-2">
        <span>Webhook</span>
        <input v-model="form.webhookUrl" class="input" type="password" :placeholder="webhookPlaceholder" autocomplete="off" />
      </label>
      <label class="check"><input v-model="form.notifyOnComplete" type="checkbox" />任务完成通知</label>
      <label class="check"><input v-model="form.notifyOnPoolLow" type="checkbox" />邮箱不足通知</label>
      <label class="field">
        <span>邮箱预警阈值</span>
        <input v-model.number="form.poolLowThreshold" class="input" type="number" min="0" />
      </label>
    </div>
    <div class="actions">
      <AppButton variant="primary" :loading="ops.busy['maintenance']" @click="saveMaintenance">保存维护设置</AppButton>
      <AppButton :loading="ops.busy['maintenance-run']" @click="runNow">
        <span class="btn-inner"><Play :size="13" />立即测活</span>
      </AppButton>
      <AppButton :loading="ops.busy['webhook']" @click="testWebhook">
        <span class="btn-inner"><Send :size="13" />测试 Webhook</span>
      </AppButton>
    </div>
    <p class="hint">最近测活：{{ lastHealthAt }}</p>
  </section>

  <section v-if="visible.has('quota')" class="card glass-panel">
    <div class="card-head">
      <h2 class="section-title">中转账号额度</h2>
      <p class="card-sub">后台定时刷新 cliproxy 中转账号额度缓存</p>
    </div>
    <div class="form-grid">
      <label class="check"><input v-model="quotaForm.enabled" type="checkbox" />启用自动刷新</label>
      <label class="field">
        <span>刷新间隔（秒）</span>
        <input v-model.number="quotaForm.intervalSeconds" class="input" type="number" min="30" max="3600" />
      </label>
    </div>
    <div class="actions">
      <AppButton variant="primary" :loading="ops.busy['cliproxy-quota']" @click="saveQuota">保存额度刷新设置</AppButton>
    </div>
    <p class="hint">保存后后台任务按新间隔生效；间隔下限 30 秒，上限 3600 秒。</p>
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

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
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

.check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  cursor: pointer;
}

.check input {
  accent-color: var(--accent);
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
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

.hint.danger {
  color: var(--error);
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .span-2 {
    grid-column: span 1;
  }
}
</style>
