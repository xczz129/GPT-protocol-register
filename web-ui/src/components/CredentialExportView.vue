<script setup>
import { computed, ref } from "vue";
import { ClipboardPaste, Download, Eraser } from "@lucide/vue";

const props = defineProps({ accounts: { type: Array, default: () => [] }, busy: Boolean });
const emit = defineEmits(["export"]);
const input = ref("");
const pasteError = ref("");

function parseEmails(value) {
  const seen = new Set();
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]|['"`]$/g, "").toLowerCase())
    .filter((item) => item && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(item) && !seen.has(item) && seen.add(item));
}

const requestedEmails = computed(() => parseEmails(input.value));
const accountIndex = computed(() => new Map(props.accounts.map((item) => [String(item.email || "").trim().toLowerCase(), item])));
const matched = computed(() => requestedEmails.value.map((email) => accountIndex.value.get(email)).filter(Boolean));
const complete = computed(() => matched.value.filter((item) => item.has_password && item.has_totp));
const incomplete = computed(() => matched.value.filter((item) => !item.has_password || !item.has_totp));
const notFound = computed(() => requestedEmails.value.filter((email) => !accountIndex.value.has(email)));
const canExport = computed(() => complete.value.length > 0 && !props.busy);

async function pasteEmails() {
  pasteError.value = "";
  try {
    input.value = await navigator.clipboard.readText();
  } catch (error) {
    pasteError.value = error?.message || "读取剪贴板失败";
  }
}

function clearInput() {
  input.value = "";
  pasteError.value = "";
}

function exportCredentials() {
  if (canExport.value) emit("export", requestedEmails.value);
}
</script>

<template>
  <section class="card credential-export-panel">
    <div class="card-h credential-export-head">
      <div>
        <h2>一键导出凭据</h2>
        <p class="subtle">按邮箱精确筛选，导出格式：账号----密码----2FA</p>
      </div>
      <span class="credential-export-count">{{ props.accounts.length }} 个账号已载入</span>
    </div>

    <div class="credential-export-editor">
      <label class="credential-export-field">
        <span>邮箱列表</span>
        <textarea v-model="input" rows="9" spellcheck="false" placeholder="每行一个邮箱，也支持逗号或空格分隔"></textarea>
      </label>
      <div class="credential-export-actions">
        <button class="btn secondary" type="button" :disabled="props.busy" @click="pasteEmails"><ClipboardPaste :size="15" aria-hidden="true" />粘贴邮箱</button>
        <button class="btn ghost" type="button" :disabled="!input || props.busy" @click="clearInput"><Eraser :size="15" aria-hidden="true" />清空</button>
      </div>
      <small v-if="pasteError" class="credential-export-error">{{ pasteError }}</small>
    </div>

    <div class="credential-export-summary" aria-live="polite">
      <span>已识别 <b>{{ requestedEmails.length }}</b></span>
      <span>匹配 <b>{{ matched.length }}</b></span>
      <span class="ready">可导出 <b>{{ complete.length }}</b></span>
      <span v-if="incomplete.length" class="warn">缺少密码或 2FA <b>{{ incomplete.length }}</b></span>
      <span v-if="notFound.length" class="danger-text">未找到 <b>{{ notFound.length }}</b></span>
    </div>

    <div v-if="matched.length" class="credential-export-table-wrap">
      <table class="credential-export-table">
        <thead><tr><th>邮箱</th><th>账号状态</th><th>凭据</th></tr></thead>
        <tbody>
          <tr v-for="item in matched" :key="item.delete_key || item.email">
            <td><strong>{{ item.email }}</strong></td>
            <td><span :class="['tag', item.is_deactivated ? 'tag-danger' : '']">{{ item.is_deactivated ? '已封号' : (item.status || '已匹配') }}</span></td>
            <td><span :class="['credential-ready', item.has_password && item.has_totp ? 'yes' : 'no']">{{ item.has_password && item.has_totp ? '账号 / 密码 / 2FA' : `${item.has_password ? '' : '缺密码'}${item.has_password || item.has_totp ? '' : '、'}${item.has_totp ? '' : '缺 2FA'}` }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="credential-export-empty">输入邮箱后显示匹配结果</div>

    <div class="credential-export-footer">
      <span>只导出同时保存了密码和 2FA 的账号，未匹配项不会写入文件。</span>
      <button class="btn primary" type="button" :disabled="!canExport" @click="exportCredentials"><Download :size="15" aria-hidden="true" />导出 {{ complete.length }} 个账号</button>
    </div>
  </section>
</template>
