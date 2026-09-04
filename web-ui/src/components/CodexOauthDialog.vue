<script setup>
import { nextTick, ref, watch } from "vue";
import { Copy, Smartphone, X } from "@lucide/vue";

const props = defineProps({
  open: Boolean,
  status: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["close", "stop", "copy", "resume"]);

const logsEl = ref(null);
const copiedKey = ref("");

watch(() => props.status?.logs?.length, async () => {
  await nextTick();
  if (logsEl.value) logsEl.value.scrollTop = logsEl.value.scrollHeight;
});

function logClass(level) {
  if (level === "error") return "codex-log-error";
  if (level === "warning") return "codex-log-warning";
  if (level === "ok") return "codex-log-ok";
  return "";
}

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
        <section class="app-dialog codex-oauth-dialog" role="dialog" aria-modal="true" aria-label="接码" @keyup.esc="emit('close')">
          <div class="app-dialog-head">
            <div>
              <span class="app-dialog-kicker"><Smartphone :size="12" aria-hidden="true" />接码</span>
              <h2>接码进度</h2>
              <small>自动接码 · 完成后可复制 RT</small>
            </div>
            <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="16" aria-hidden="true" /></button>
          </div>
          <div class="codex-oauth-toolbar">
            <span>{{ status.done || 0 }} / {{ status.total || 0 }} · 成功 {{ status.success || 0 }} · 失败 {{ status.failed || 0 }}<template v-if="status.running"> · 运行中…</template><template v-else> · 已结束</template></span>
            <div class="row-btns">
              <button v-if="status.running" class="btn danger sm" type="button" @click="emit('stop')">停止</button>
              <button v-if="!status.running && Number(status.pending_count || 0) > 0" class="btn primary sm" type="button" @click="emit('resume')">继续接码剩余 {{ status.pending_count }} 个</button>
              <button class="btn secondary sm" type="button" @click="emit('close')">关闭</button>
            </div>
          </div>
          <p v-if="!status.running && (status.logs || []).length && (status.logs || []).some((line) => line.level === 'warning')" class="codex-hint"><Smartphone :size="14" aria-hidden="true" /> {{ (status.logs || []).filter((line) => line.level === 'warning').slice(-1)[0]?.m || "" }}</p>
          <div class="codex-oauth-body">
            <div v-if="(status.results || []).length" class="codex-oauth-results">
              <div v-for="(item, index) in status.results" :key="`${item.email}-${index}`" :class="['codex-result-row', item.status === 'success' ? 'ok' : 'bad']">
                <div class="codex-result-main">
                  <b>{{ item.email }}</b>
                  <small v-if="item.status === 'success'">RT {{ shortRt(item.refresh_token) }}<template v-if="rtDays(item.expires_in)"> · 约 {{ rtDays(item.expires_in) }} 天</template></small>
                  <small v-else>{{ item.error }}</small>
                </div>
                <button v-if="item.status === 'success'" class="btn secondary sm" type="button" title="复制完整 refresh_token" @click="copyRt(item.refresh_token, item.email)"><Copy :size="12" aria-hidden="true" />{{ copiedKey === item.email ? "已复制" : (item.token_kind === "at" ? "复制 AT" : "复制 RT") }}</button>
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
