<script setup>
import { computed, nextTick, ref, watch } from "vue";
import { Copy, RefreshCw, Smartphone, ScrollText, Upload, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  status: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["close", "stop", "copy", "retry", "retry-all", "history"]);

const logsEl = ref(null);
const copiedKey = ref("");
const expandedLogs = ref({});

const isImport = computed(() => props.status?.mode === "import");
const dialogTitle = computed(() => (isImport.value ? "协议登录进度" : "接码进度"));
const dialogHint = computed(() =>
  isImport.value ? "协议登录(密码 + TOTP) · 失败项可展开查看该账号的完整登录日志" : "自动接码 · 完成后可复制 RT",
);
const failedItems = computed(() => (props.status?.results || []).filter((item) => {
  const status = String(item?.status || "").toLowerCase();
  const outcome = String(item?.outcome || "").toLowerCase();
  const relayFailed = String(item?.relay_status || "").toLowerCase() === "failed";
  return (status === "failed" || status === "error" || status === "failure" || outcome === "failed" || relayFailed) && !item?.skipped;
}));
const failedCount = computed(() => Math.max(Number(props.status?.failed || 0), failedItems.value.length));
const skippedCount = computed(() => (props.status?.results || []).filter((item) => item.skipped).length);
const canRetry = computed(() => !props.status?.running && failedCount.value > 0);

watch(() => props.status?.logs?.length, async () => {
  await nextTick();
  if (logsEl.value) logsEl.value.scrollTop = logsEl.value.scrollHeight;
});

watch(() => props.open, (value) => {
  if (!value) expandedLogs.value = {};
});

function logClass(level) {
  if (level === "error") return "codex-log-error";
  if (level === "warning") return "codex-log-warning";
  if (level === "ok") return "codex-log-ok";
  return "";
}

function toggleLogs(item, index) {
  const key = `${item.email}-${index}`;
  expandedLogs.value = { ...expandedLogs.value, [key]: !expandedLogs.value[key] };
}

function isExpanded(item, index) {
  return Boolean(expandedLogs.value[`${item.email}-${index}`]);
}

const expandedCount = computed(() => Object.values(expandedLogs.value).filter(Boolean).length);

function shortRt(rt) {
  const text = String(rt || "");
  return text.length > 26 ? `${text.slice(0, 14)}…${text.slice(-8)}` : text;
}

function rtDays(expiresIn) {
  const seconds = Number(expiresIn || 0);
  return seconds ? Math.round(seconds / 86400) : 0;
}

async function copyRt(rt, email) {
  const text = String(rt || "");
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    copiedKey.value = email;
    setTimeout(() => { if (copiedKey.value === email) copiedKey.value = ""; }, 1500);
    emit("copy", { text, email, successMessage: `已复制 ${email} 的 RT` });
  } catch {
    emit("copy", { text, email, fallback: true });
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="app-dialog-mask" role="presentation" @click.self="emit('close')">
        <section class="app-dialog codex-oauth-dialog" role="dialog" aria-modal="true" :aria-label="isImport ? '导入账号' : '接码'" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div>
              <span class="app-dialog-kicker">
                <Upload v-if="isImport" :size="12" aria-hidden="true" />
                <Smartphone v-else :size="12" aria-hidden="true" />
                {{ isImport ? "导入账号" : "接码" }}
              </span>
              <h2>{{ dialogTitle }}</h2>
              <small>{{ dialogHint }}</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <div class="codex-oauth-toolbar">
            <span>{{ status.done || 0 }} / {{ status.total || 0 }} · 成功 {{ status.success || 0 }}<template v-if="skippedCount"> · 已接码 {{ skippedCount }}</template> · 失败 {{ status.failed || 0 }}<template v-if="status.running"> · 运行中…</template><template v-else> · 已结束</template></span>
            <div class="row-btns">
              <button v-if="isImport" class="btn secondary sm" type="button" title="查看每个账号的历史导入日志" @click="emit('history')"><ScrollText :size="12" aria-hidden="true" />历史日志</button>
              <button v-if="canRetry" class="btn secondary sm" type="button" @click="emit('retry-all')"><RefreshCw :size="12" aria-hidden="true" />重试失败项 ({{ failedCount }})</button>
              <button v-if="status.running" class="btn danger sm" type="button" @click="emit('stop')">停止</button>
              <button class="btn secondary sm" type="button" @click="emit('close')">关闭</button>
            </div>
          </div>
          <p v-if="!status.running && (status.logs || []).length && (status.logs || []).some((line) => line.level === 'warning')" class="codex-hint"><Smartphone :size="14" aria-hidden="true" /> {{ (status.logs || []).filter((line) => line.level === 'warning').slice(-1)[0]?.m || "" }}</p>
          <div class="codex-oauth-body">
            <div v-if="(status.results || []).length" class="codex-oauth-results">
              <div v-for="(item, index) in status.results" :key="`${item.email}-${index}`" :class="['codex-result-row', item.status === 'success' ? 'ok' : 'bad', { expanded: isExpanded(item, index) }]">
                <div class="codex-result-main">
                  <b>{{ item.email }}<em v-if="item.skipped" class="codex-skip-tag">已接码</em></b>
                  <small v-if="item.status === 'success'">RT {{ shortRt(item.refresh_token) }}<template v-if="item.skipped"> · 沿用已有</template><template v-else-if="rtDays(item.expires_in)"> · 约 {{ rtDays(item.expires_in) }} 天</template></small>
                  <small v-else>{{ item.error }}</small>
                </div>
                <div class="row-btns">
                  <button v-if="(item.logs || []).length" class="btn secondary sm" type="button" :title="isExpanded(item, index) ? '收起该账号日志' : '展开该账号日志'" @click="toggleLogs(item, index)"><ScrollText :size="12" aria-hidden="true" />{{ isExpanded(item, index) ? "收起日志" : "日志" }}</button>
                  <button v-if="item.status !== 'success' && !status.running" class="btn secondary sm" type="button" title="重新尝试该账号" @click="emit('retry', item)"><RefreshCw :size="12" aria-hidden="true" />重试</button>
                  <button v-if="item.status === 'success'" class="btn secondary sm" type="button" title="复制完整 refresh_token" @click="copyRt(item.refresh_token, item.email)"><Copy :size="12" aria-hidden="true" />{{ copiedKey === item.email ? "已复制" : (item.token_kind === "at" ? "复制 AT" : "复制 RT") }}</button>
                </div>
              </div>
              <div v-if="expandedCount" class="codex-result-logs">
                <div v-for="(item, index) in status.results" v-show="isExpanded(item, index)" :key="`logs-${item.email}-${index}`" class="codex-result-log-block">
                  <div class="codex-result-log-title">{{ item.email }} · {{ (item.logs || []).length }} 条</div>
                  <p v-for="(line, lineIndex) in item.logs || []" :key="lineIndex" :class="logClass(line.level)"><span class="codex-log-time">{{ line.t }}</span>{{ line.m }}</p>
                  <p v-if="!(item.logs || []).length" class="codex-log-empty">该账号无日志</p>
                </div>
              </div>
            </div>
            <div ref="logsEl" class="codex-oauth-logs">
              <div v-if="!(status.logs || []).length" class="codex-log-empty">暂无日志</div>
              <p v-for="(line, index) in status.logs || []" :key="index" :class="logClass(line.level)"><span class="codex-log-time">{{ line.t }}</span>{{ line.m }}</p>
            </div>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
