<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { api, jsonBody } from "../api.js";
import AppPager from "./AppPager.vue";

const props = defineProps({ forms: { type: Object, required: true }, mailboxes: { type: Array, default: () => [] }, progress: { type: Object, default: () => ({ items: [] }) }, busy: Boolean });
const emit = defineEmits(["save-outlook", "save-icloud", "save-maddy", "save-fastmail", "load-maddy-domains", "load-fastmail-accounts", "refresh", "delete", "state"]);
const query = ref("");
const provider = ref("auto");
const stateFilter = ref("all");
const page = ref(1);
const pageSize = 10;
const selected = ref([]);
const expanded = ref("");
const messageState = ref({ loading: false, refreshing: false, error: "", items: [], syncedAt: "" });
let messageTimer = null;
const filtered = computed(() => props.mailboxes.filter((item) => {
  if (provider.value !== "auto" && item.provider_type !== provider.value) return false;
  if (stateFilter.value !== "all" && String(item.state || "available") !== stateFilter.value) return false;
  const needle = query.value.trim().toLowerCase();
  return !needle || [item.email, item.state, item.provider_type, item.reason].some((value) => String(value || "").toLowerCase().includes(needle));
}));
const importProgress = computed(() => (Array.isArray(props.progress?.items) ? props.progress.items : []).find((item) => String(item.key || "").startsWith("mail_import_")));
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)));
const pageItems = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize));
watch([query, provider], () => { page.value = 1; selected.value = []; });

function mailboxKey(item) { return `${item.provider_type}:${item.email}`; }
function providerName(item) {
  if (item.provider_type === "icloud_api") return "iCloud";
  if (item.provider_type === "maddy_webui") return "Maddy";
  if (item.provider_type === "fastmail_api") return "Fastmail";
  return "Outlook";
}
function stateClass(item) { return item.state === "available" ? "success" : ["failed", "invalid", "token_invalid"].includes(item.state) ? "failed" : "queue"; }
function registeredClass(item) {
  if (item.registered) return "success";
  const reason = String(item.reason || "");
  if (reason.includes("cdk_bound")) return "run";
  if (reason.includes("account_deactivated") || reason.includes("known_deactivated")) return "failed";
  if (reason.includes("remote_registered") || reason.includes("invalid_auth_step") || reason.includes("password_reset_required")) return "failed";
  return "queue";
}
function registeredLabel(item) {
  if (item.registered) return "已注册";
  const reason = String(item.reason || "");
  if (reason.includes("cdk_bound")) return "已售出";
  if (reason.includes("account_deactivated") || reason.includes("known_deactivated")) return "已停用";
  if (reason.includes("remote_registered") || reason.includes("invalid_auth_step") || reason.includes("password_reset_required")) return "已被占用";
  if (reason.includes("registered_account")) return "已注册过";
  return "未注册";
}
function reasonText(item) {
  const reason = String(item.reason || "");
  if (reason.includes("cdk_bound")) return "已绑定 CDK · 账号售出后已移出账号库";
  if (reason.includes("remote_registered")) return "远端已有 ChatGPT 账号 · 不可再用";
  if (reason.includes("invalid_auth_step")) return "远端已有 ChatGPT 账号（注册被重定向到登录）";
  if (reason.includes("password_reset_required")) return "远端已有账号 · 要求重置密码";
  if (reason.includes("account_deactivated")) return "ChatGPT 账号已被停用";
  if (reason.includes("known_deactivated")) return "已知账号被停用";
  if (reason.includes("registered_account")) return "曾通过本系统注册 · 账号记录已不在库中";
  return reason;
}
function stopMessagePolling() {
  clearInterval(messageTimer);
  messageTimer = null;
}
function startMessagePolling(item) {
  stopMessagePolling();
  messageTimer = setInterval(() => {
    if (!document.hidden && expanded.value === mailboxKey(item)) loadMessages(item, true);
  }, 5000);
}
function syncTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}
async function loadMessages(item, silent = false) {
  if (messageState.value.loading || messageState.value.refreshing) return;
  messageState.value = {
    ...messageState.value,
    loading: !silent && !messageState.value.items.length,
    refreshing: silent || !!messageState.value.items.length,
    error: "",
  };
  const key = mailboxKey(item);
  try {
    const data = await api("/api/mailbox/messages", { method: "POST", body: jsonBody({ email: item.email, provider_type: item.provider_type, limit: 10 }) });
    if (expanded.value !== key) return;
    messageState.value = {
      loading: false,
      refreshing: false,
      error: data.ok === false ? data.error : "",
      items: data.items || [],
      syncedAt: new Date().toISOString(),
    };
  } catch (error) {
    if (expanded.value !== key) return;
    messageState.value = { ...messageState.value, loading: false, refreshing: false, error: error.message };
  }
}
async function readMessages(item) {
  const key = mailboxKey(item);
  if (expanded.value === key) {
    expanded.value = "";
    stopMessagePolling();
    return;
  }
  expanded.value = key;
  messageState.value = { loading: false, refreshing: false, error: "", items: [], syncedAt: "" };
  await loadMessages(item);
  if (expanded.value === key) startMessagePolling(item);
}
function deleteSelected() {
  const set = new Set(selected.value);
  emit("delete", props.mailboxes.filter((item) => set.has(mailboxKey(item))));
}
function selectedEmails() {
  const set = new Set(selected.value);
  return props.mailboxes.filter((item) => set.has(mailboxKey(item))).map((item) => item.email);
}
function releaseSelected() {
  const emails = selectedEmails();
  if (emails.length) emit("state", emails, "release", []);
  selected.value = [];
}
onBeforeUnmount(stopMessagePolling);
</script>

<template>
  <section class="card">
    <div class="card-h"><div><h2>批量导入邮箱</h2><p class="subtle">每次提交都会追加到邮箱池；同邮箱的新凭证会更新旧凭证。</p></div></div>
    <div class="split">
      <div class="panel-soft">
        <div class="task-title-row"><div><h3>批量导入 Outlook</h3><p class="hint">email----password----client_id----refresh_token</p></div><label class="compact">取码模式<select v-model="forms.outlookMode"><option value="imap">IMAP</option><option value="graph">Graph</option><option value="auto">Auto</option></select></label></div>
        <textarea v-model="forms.outlookText" rows="14" spellcheck="false" placeholder="user@outlook.com----password----client_id----refresh_token"></textarea>
        <div class="actions"><button class="primary" :disabled="busy || !forms.outlookText.trim()" @click="emit('save-outlook')">导入 Outlook</button></div>
      </div>
      <div class="panel-soft">
        <div class="task-title-row"><div><h3>批量导入 iCloud</h3><p class="hint">email----api_key</p></div><label class="checkline"><input v-model="forms.icloudEnable" type="checkbox" />启用</label></div>
        <label class="label-line">API Base<input v-model="forms.icloudApiBase" type="text" /></label>
        <textarea v-model="forms.icloudText" rows="12" spellcheck="false" placeholder="user@icloud.com----alias_key"></textarea>
        <div class="actions"><button class="primary" :disabled="busy || !forms.icloudText.trim()" @click="emit('save-icloud')">导入 iCloud</button></div>
      </div>
      <div class="panel-soft">
        <div class="task-title-row"><div><h3>Maddy WebUI</h3><p class="hint">X-API-Key + 域名列表。</p></div><label class="checkline"><input v-model="forms.maddyEnable" type="checkbox" />启用</label></div>
        <label class="label-line">
          <span>API Base</span>
          <input v-model="forms.maddyApiBase" type="text" placeholder="https://120.53.232.195/api" />
          <small class="field-help">使用 IP 地址连接，并通过 Host: admin.xb1219.com 路由。</small>
        </label>
        <label class="label-line">
          <span class="field-label-row"><span>API Key</span><span v-if="forms.maddyApiKeyConfigured" class="secret-configured">已配置</span></span>
          <input v-model="forms.maddyApiToken" type="password" autocomplete="new-password" :placeholder="forms.maddyApiKeyConfigured ? '留空继续使用已保存 key' : '输入 X-API-Key'" />
          <small class="field-help">只有输入新 Key 才会替换；留空会沿用服务器中已保存的 Key。</small>
        </label>
        <label class="label-line">默认域名<input v-model="forms.maddyDefaultDomain" type="text" placeholder="xb1219.com" /></label>
        <label class="label-line">域名列表<textarea v-model="forms.maddyDomainsText" rows="4" spellcheck="false" placeholder="xb1219.com&#10;unjome.com"></textarea></label>
        <div class="form two">
          <label>用户名长度<input v-model.number="forms.maddyLocalLength" type="number" min="8" max="30" /></label>
          <label>动态容量<input v-model.number="forms.maddyCapacity" type="number" min="1" max="500" /></label>
        </div>
        <label class="label-line">邮箱密码<input v-model="forms.maddyMailboxPassword" type="password" autocomplete="new-password" placeholder="留空自动生成" /></label>
        <div class="actions"><button class="btn secondary" :disabled="busy || !forms.maddyApiBase.trim()" @click="emit('load-maddy-domains')">刷新域名</button><button class="primary" :disabled="busy || !forms.maddyApiBase.trim()" @click="emit('save-maddy')">保存 Maddy</button></div>
      </div>
      <div class="panel-soft">
        <div class="task-title-row"><div><h3>Fastmail 别名池</h3><p class="hint">mailcode-api 服务端管理别名池并自动补池；注册时自动 acquire。</p></div><label class="checkline"><input v-model="forms.fastmailEnable" type="checkbox" />启用</label></div>
        <label class="label-line">API Base<input v-model="forms.fastmailApiBase" type="text" placeholder="http://服务器IP:8686" /></label>
        <label class="label-line">管理密钥<input v-model="forms.fastmailAdminKey" type="password" autocomplete="new-password" placeholder="留空保留已保存 key" /></label>
        <label class="label-line">主账号列表<textarea v-model="forms.fastmailAccountsText" rows="3" spellcheck="false" placeholder="xcovo1219@fastmail.com"></textarea></label>
        <div class="form two">
          <label>容量回退<input v-model.number="forms.fastmailCapacity" type="number" min="1" max="1000" /></label>
        </div>
        <div v-if="forms.fastmailPoolRows?.length" class="fastmail-pool">
          <div v-for="row in forms.fastmailPoolRows" :key="row.email" class="fastmail-pool-row">
            <span class="fastmail-account">{{ row.email }}</span>
            <span class="pill" :class="row.available_count > 0 ? 'success' : 'queue'">可用 {{ row.available_count }} / {{ row.alias_count }}（池 {{ row.pool_size }}）</span>
            <span v-if="!row.enabled" class="pill failed">已停用</span>
          </div>
        </div>
        <div class="actions"><button class="btn secondary" :disabled="busy || !forms.fastmailApiBase.trim()" @click="emit('load-fastmail-accounts')">刷新池状态</button><button class="primary" :disabled="busy || !forms.fastmailApiBase.trim()" @click="emit('save-fastmail')">保存 Fastmail</button></div>
      </div>
    </div>
  </section>

  <div v-if="importProgress" class="inline-progress card"><div><strong>{{ importProgress.label }} · {{ importProgress.stage }}</strong><span>{{ Math.round(importProgress.percent || 0) }}%</span></div><div class="inline-progress-track"><i :style="{ width: `${importProgress.percent || 0}%` }"></i></div><small>{{ importProgress.message || `已运行 ${Math.round(importProgress.elapsed_sec || 0)}s` }}</small></div>

  <section class="card">
    <div class="card-h">
      <div><h2>已导入邮箱</h2><p class="subtle">搜索、分页和读取邮箱最近邮件。</p></div>
      <button class="btn secondary" :disabled="busy" @click="emit('refresh')">刷新</button>
    </div>
    <div class="account-filters">
      <label class="search-field"><span>⌕</span><input v-model="query" type="search" placeholder="搜索邮箱、状态或失败原因" /></label>
      <select v-model="provider"><option value="auto">全部邮箱</option><option value="outlook_token">Outlook</option><option value="icloud_api">iCloud</option><option value="maddy_webui">Maddy</option><option value="fastmail_api">Fastmail</option></select>
      <select v-model="stateFilter"><option value="all">全部状态</option><option value="available">可用</option><option value="in_use">占用</option><option value="used">已使用</option><option value="failed">失败</option><option value="cooldown">冷却</option><option value="token_invalid">凭证失效</option></select>
      <span class="filter-result">{{ filtered.length }} 个邮箱</span>
    </div>
    <div class="mailbox-list">
      <div v-if="!pageItems.length" class="empty-card">没有匹配的邮箱</div>
      <article v-for="item in pageItems" :key="mailboxKey(item)" class="mailbox-card">
        <div class="mailbox-select"><input v-model="selected" type="checkbox" :value="mailboxKey(item)" /></div>
        <div class="mailbox-main"><div class="mailbox-email">{{ item.email }}</div><div class="mailbox-sub">{{ providerName(item) }} · {{ item.mode || "-" }} · 登录 {{ item.login_address || item.email }}</div><div v-if="item.reason" class="mailbox-reason">{{ reasonText(item) }}</div></div>
        <div class="mailbox-tags"><span :class="['pill', stateClass(item)]">{{ item.state || "unknown" }}</span><span :class="['pill', registeredClass(item)]">{{ registeredLabel(item) }}</span></div>
        <div class="mailbox-actions"><button class="btn secondary sm" @click="readMessages(item)">{{ expanded === mailboxKey(item) ? "收起邮件" : "查看邮件" }}</button></div>
        <div v-if="expanded === mailboxKey(item)" class="mailbox-inline-viewer active">
          <div class="mailbox-viewer-toolbar">
            <span>{{ messageState.refreshing ? "正在同步" : messageState.syncedAt ? `同步于 ${syncTime(messageState.syncedAt)}` : "读取中" }}</span>
            <button class="btn secondary sm" :disabled="messageState.loading || messageState.refreshing" @click="loadMessages(item, true)">立即刷新</button>
          </div>
          <div v-if="messageState.loading" class="empty-card">正在读取邮件</div>
          <div v-else-if="messageState.error" class="empty-card bad">{{ messageState.error }}</div>
          <div v-else-if="!messageState.items.length" class="empty-card">没有读到邮件</div>
          <div v-else class="message-list"><article v-for="(message, index) in messageState.items" :key="index" class="message-card"><div class="message-title"><b>{{ message.subject || "无主题" }}</b><span v-if="message.code" class="pill success">验证码 {{ message.code }}</span></div><div class="message-meta">{{ message.sender || "未知发件人" }} · {{ message.received_at || "" }}</div><details><summary>查看正文</summary><pre>{{ message.text || message.preview || "HTML 邮件" }}</pre></details></article></div>
        </div>
      </article>
    </div>
    <div class="bulkbar"><button class="btn secondary" :disabled="!selected.length || busy" @click="releaseSelected">释放选中状态</button><button class="btn secondary" :disabled="busy" @click="emit('state', [], 'release', ['failed', 'in_use', 'cooldown'])">释放异常与冷却</button><button class="btn danger" :disabled="!selected.length || busy" @click="deleteSelected">删除选中</button><AppPager v-model:page="page" :pages="pages" :total="filtered.length" /></div>
  </section>
</template>

<style scoped>
.panel-soft {
  display: flex;
  flex-direction: column;
}

.panel-soft > textarea {
  flex: 1;
  min-height: 260px;
}

.panel-soft .actions {
  margin-top: 10px;
}

.fastmail-pool {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 4px 0;
}

.fastmail-pool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--muted, #7a7f8a);
}

.fastmail-account {
  font-family: var(--mono, monospace);
  color: var(--text, #222);
}

.field-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.field-help {
  color: var(--muted, #7a7f8a);
  font-size: 9px;
  font-weight: 500;
  line-height: 1.45;
}

.secret-configured {
  color: #24834c;
  font-size: 9px;
  font-weight: 700;
}
</style>
