<script setup>
import { nextTick, watch } from "vue";
import { ClipboardList, RefreshCw, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  account: { type: Object, default: null },
  logs: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  loading: Boolean,
  error: { type: String, default: "" },
});
const emit = defineEmits(["close", "refresh"]);

function statusClass(item) {
  const status = String(item?.status || "").toLowerCase();
  if (["failed", "error", "cancelled", "skipped"].includes(status)) return "account-log-error";
  if (["retry", "transport_retry", "warning"].includes(status)) return "account-log-retry";
  if (["success", "completed", "complete"].includes(status)) return "account-log-success";
  return "";
}

function statusLabel(item) {
  const status = String(item?.status || "").toLowerCase();
  return ({ running: "进行中", info: "信息", success: "成功", completed: "完成", complete: "完成", failed: "失败", error: "错误", cancelled: "已停止", skipped: "跳过", retry: "重试", transport_retry: "重试", warning: "警告" }[status] || status || "记录");
}

function operationLabel(item) {
  const operation = String(item?.operation || "").toLowerCase();
  return ({
    registration: "注册",
    login: "协议登录",
    reauth: "重新登录",
    security_repair: "安全字段补全",
    password_change: "修改密码",
    totp_change: "修改 2FA",
    email_rebind: "邮箱换绑",
    maintenance: "自动维护测活",
    health_check: "账号测活",
    trial_eligibility: "资格检测",
    cdk_generate: "生成 CDK",
    cdk_status: "更新 CDK 状态",
  }[operation] || operation || "账号操作");
}

function formatTime(value) {
  const raw = String(value || "").trim();
  if (!raw) return "-";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function message(item) {
  const error = String(item?.error || item?.reason || "").trim();
  const primary = String(item?.message || item?.step || "").trim();
  if (error && primary && primary !== error) return `${primary}：${error}`;
  return error || primary || "操作记录";
}

watch(() => props.open, async (open) => {
  if (!open) return;
  await nextTick();
  document.querySelector(".account-log-dialog .account-log-close")?.focus();
});
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('close')">
        <section class="app-dialog account-log-dialog" role="dialog" aria-modal="true" aria-label="账号操作日志" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div>
              <span class="app-dialog-kicker"><ClipboardList :size="12" aria-hidden="true" />账号日志</span>
              <h2>{{ account?.email || "账号操作日志" }}</h2>
              <small v-if="account?.account_id" class="account-log-id">{{ account.account_id }}</small>
            </div>
            <button class="app-dialog-close account-log-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <div class="account-log-toolbar">
            <span>{{ loading ? "正在读取日志…" : `${total || logs.length} 条记录` }}</span>
            <button class="btn secondary" type="button" :disabled="loading" title="刷新账号日志" @click="emit('refresh')"><RefreshCw :size="14" aria-hidden="true" />刷新</button>
          </div>
          <p v-if="error" class="account-log-error-text">{{ error }}</p>
          <div v-else class="account-log-list" aria-live="polite">
            <div v-if="loading && !logs.length" class="account-log-empty">正在读取账号日志</div>
            <div v-else-if="!logs.length" class="account-log-empty">暂无该账号的操作日志</div>
            <article v-for="(item, index) in logs" :key="item.id || `${item.at || 'log'}-${index}`" class="account-log-item">
              <div class="account-log-item-head">
                <span :class="['account-log-status', statusClass(item)]">{{ statusLabel(item) }}</span>
                <time>{{ formatTime(item.at || item.timestamp || item.created_at) }}</time>
                <span v-if="item.operation || item.operation_label" class="account-log-operation">{{ item.operation_label || operationLabel(item) }}</span>
              </div>
              <p>{{ message(item) }}</p>
              <small v-if="item.source" class="account-log-source">{{ item.source }}</small>
            </article>
          </div>
          <div class="app-dialog-actions"><button class="btn secondary" type="button" @click="emit('close')">关闭</button></div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
