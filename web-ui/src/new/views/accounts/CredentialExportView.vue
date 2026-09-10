<script setup lang="ts">
/* --- 凭据导出：按邮箱精确筛选，导出 账号----密码----2FA（仅密码+2FA 齐全的账号） --- */
import { computed, onMounted, ref } from "vue";
import { ClipboardPaste, Download, Eraser } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { useAccountsStore } from "../../stores/accounts";
import { useUiStore } from "../../stores/ui";
import { exportSecurityByEmail } from "../../api/accounts";

const accounts = useAccountsStore();
const ui = useUiStore();

const input = ref("");
const pasteError = ref("");
const busy = ref(false);

function parseEmails(value: string): string[] {
  const seen = new Set<string>();
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().replace(/^['"`]|['"`]$/g, "").toLowerCase())
    .filter((item) => item && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(item) && !seen.has(item) && seen.add(item));
}

const requestedEmails = computed(() => parseEmails(input.value));
const accountIndex = computed(() => new Map(accounts.rows.map((item) => [String(item.email || "").trim().toLowerCase(), item])));
const matched = computed(() => requestedEmails.value.map((email) => accountIndex.value.get(email)).filter((item): item is NonNullable<typeof item> => Boolean(item)));
const complete = computed(() => matched.value.filter((item) => item.has_password && item.has_totp));
const incomplete = computed(() => matched.value.filter((item) => !item.has_password || !item.has_totp));
const notFound = computed(() => requestedEmails.value.filter((email) => !accountIndex.value.has(email)));
const canExport = computed(() => complete.value.length > 0 && !busy.value);

async function pasteEmails() {
  pasteError.value = "";
  try {
    input.value = await navigator.clipboard.readText();
  } catch (error) {
    pasteError.value = error instanceof Error ? error.message : "读取剪贴板失败";
  }
}

async function doExport() {
  if (!canExport.value) return;
  busy.value = true;
  try {
    const result = await exportSecurityByEmail(requestedEmails.value);
    ui.notify(
      `已导出 ${result.exported} 个账号${result.missing ? `，跳过 ${result.missing} 个未匹配或缺少凭据的账号` : ""}`,
      result.missing ? "warning" : "success",
    );
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "导出失败", "error");
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  if (!accounts.rows.length) void accounts.load();
});
</script>

<template>
  <section class="export-panel glass-panel">
    <div class="panel-head">
      <div>
        <h2>一键导出凭据</h2>
        <p class="panel-sub">按邮箱精确筛选，导出格式：账号----密码----2FA</p>
      </div>
      <span class="loaded-count">{{ accounts.rows.length }} 个账号已载入</span>
    </div>

    <div class="editor">
      <label class="field">
        <span>邮箱列表</span>
        <textarea v-model="input" class="input" rows="9" spellcheck="false" placeholder="每行一个邮箱，也支持逗号或空格分隔"></textarea>
      </label>
      <div class="editor-actions">
        <AppButton variant="accent" size="sm" @click="pasteEmails"><ClipboardPaste :size="14" /><span>粘贴邮箱</span></AppButton>
        <AppButton variant="ghost" size="sm" :disabled="!input" @click="input = ''; pasteError = ''"><Eraser :size="14" /><span>清空</span></AppButton>
      </div>
      <small v-if="pasteError" class="error-text">{{ pasteError }}</small>
    </div>

    <div class="summary" aria-live="polite">
      <span>已识别 <b>{{ requestedEmails.length }}</b></span>
      <span>匹配 <b>{{ matched.length }}</b></span>
      <span class="ok-text">可导出 <b>{{ complete.length }}</b></span>
      <span v-if="incomplete.length" class="warn-text">缺少密码或 2FA <b>{{ incomplete.length }}</b></span>
      <span v-if="notFound.length" class="error-text">未找到 <b>{{ notFound.length }}</b></span>
    </div>

    <div v-if="matched.length" class="table-wrap">
      <table>
        <thead><tr><th>邮箱</th><th>账号状态</th><th>凭据</th></tr></thead>
        <tbody>
          <tr v-for="item in matched" :key="item.delete_key || item.email">
            <td><strong class="email">{{ item.email }}</strong></td>
            <td>
              <AppTag :kind="item.is_deactivated ? 'error' : 'default'">{{ item.is_deactivated ? "已封号" : (item.status || "已匹配") }}</AppTag>
            </td>
            <td>
              <span :class="['cred-state', item.has_password && item.has_totp ? 'yes' : 'no']">
                {{ item.has_password && item.has_totp ? "账号 / 密码 / 2FA" : [item.has_password ? "" : "缺密码", item.has_totp ? "" : "缺 2FA"].filter(Boolean).join("、") }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="empty-hint">输入邮箱后显示匹配结果</div>

    <div class="footer">
      <span class="footer-note">只导出同时保存了密码和 2FA 的账号，未匹配项不会写入文件。</span>
      <AppButton variant="primary" :disabled="!canExport" :loading="busy" @click="doExport">
        <Download :size="14" /><span>导出 {{ complete.length }} 个账号</span>
      </AppButton>
    </div>
  </section>
</template>

<style scoped>
.export-panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.panel-head h2 {
  font-size: 1.05rem;
  color: var(--text-primary);
}

.panel-sub {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 3px;
}

.loaded-count {
  font-size: 0.78rem;
  color: var(--text-muted);
  white-space: nowrap;
}

.editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 640px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.field textarea {
  resize: vertical;
  font-family: var(--font-mono, monospace);
  font-size: 0.8rem;
}

.editor-actions {
  display: flex;
  gap: 8px;
}

.summary {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 0.82rem;
  color: var(--text-secondary);
}

.summary b { color: var(--text-primary); }
.ok-text { color: var(--success); }
.warn-text { color: var(--warning); }
.error-text { color: var(--error); }

.table-wrap {
  overflow: auto;
  max-height: 46vh;
  border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm);
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
}

th, td {
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line-subtle);
}

th {
  font-size: 0.72rem;
  color: var(--text-muted);
  font-weight: 600;
  background: var(--inset-bg);
  position: sticky;
  top: 0;
}

.email { color: var(--text-primary); }

.cred-state.yes { color: var(--success); font-size: 0.78rem; }
.cred-state.no { color: var(--warning); font-size: 0.78rem; }

.empty-hint {
  padding: 32px 0;
  text-align: center;
  color: var(--text-muted);
  font-size: 0.82rem;
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.footer-note {
  font-size: 0.75rem;
  color: var(--text-muted);
}
</style>
