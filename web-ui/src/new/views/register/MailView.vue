<script setup lang="ts">
/* --- 邮箱池：批量导入与按需购买（Outlook/iCloud/SMSBower/Maddy/Fastmail）+ 邮箱管理 ---
   交互对齐旧版 MailView：读信展开后每 5s 轮询，页面隐藏时暂停。 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { Inbox, MailPlus, RefreshCw } from "@lucide/vue";
import AppButton from "../../components/ui/AppButton.vue";
import AppEmpty from "../../components/ui/AppEmpty.vue";
import AppPager from "../../components/ui/AppPager.vue";
import AppTag from "../../components/ui/AppTag.vue";
import { useMailStore } from "../../stores/mail";
import { useStatusStore } from "../../stores/status";
import { useUiStore } from "../../stores/ui";
import { useConfirm } from "../../composables/useConfirm";
import { fetchMailboxMessages, type Mailbox, type MailboxMessage } from "../../api/mail";

const mail = useMailStore();
const status = useStatusStore();
const ui = useUiStore();
const confirm = useConfirm();

const busy = ref(false);

/* --- 列表筛选与分页 --- */
const query = ref("");
const provider = ref("auto");
const stateFilter = ref("all");
const page = ref(1);
const pageSize = 10;
const selected = ref<string[]>([]);

const filtered = computed(() =>
  mail.mailboxes.filter((item) => {
    if (provider.value !== "auto" && item.provider_type !== provider.value) return false;
    if (stateFilter.value !== "all" && String(item.state || "available") !== stateFilter.value) return false;
    const needle = query.value.trim().toLowerCase();
    return (
      !needle ||
      [item.email, item.state, item.provider_type, item.reason].some((value) =>
        String(value || "").toLowerCase().includes(needle),
      )
    );
  }),
);
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)));
const pageItems = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize));
watch([query, provider, stateFilter], () => {
  page.value = 1;
  selected.value = [];
});

/* --- 展示映射（对齐旧版 pill 语义） --- */
type TagKind = "default" | "success" | "warning" | "error" | "info";

function mailboxKey(item: Mailbox) {
  return `${item.provider_type}:${item.email}`;
}
function providerName(item: Mailbox) {
  if (item.provider_type === "icloud_api") return "iCloud";
  if (item.provider_type === "smsbower_mail") return "SMSBower";
  if (item.provider_type === "maddy_webui") return "Maddy";
  if (item.provider_type === "fastmail_api") return "Fastmail";
  return "Outlook";
}
function stateKind(item: Mailbox): TagKind {
  if (item.state === "available") return "success";
  if (["failed", "invalid", "token_invalid"].includes(String(item.state))) return "error";
  return "info";
}
const STATE_LABELS: Record<string, string> = {
  available: "可用", in_use: "占用", used: "已使用", failed: "失败", cooldown: "冷却", token_invalid: "凭证失效",
};
function stateLabel(item: Mailbox) {
  return STATE_LABELS[String(item.state || "")] || item.state || "未知";
}
function registeredKind(item: Mailbox): TagKind {
  if (item.registered) return "success";
  const reason = String(item.reason || "");
  if (reason.includes("cdk_bound")) return "warning";
  if (["account_deactivated", "known_deactivated", "remote_registered", "invalid_auth_step", "password_reset_required"].some((k) => reason.includes(k))) return "error";
  return "default";
}
function registeredLabel(item: Mailbox) {
  if (item.registered) return "已注册";
  const reason = String(item.reason || "");
  if (reason.includes("cdk_bound")) return "已售出";
  if (reason.includes("account_deactivated") || reason.includes("known_deactivated")) return "已停用";
  if (reason.includes("remote_registered") || reason.includes("invalid_auth_step") || reason.includes("password_reset_required")) return "已被占用";
  if (reason.includes("registered_account")) return "已注册过";
  return "未注册";
}
function reasonText(item: Mailbox) {
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

/* --- 行内读信：展开后 5s 轮询 --- */
const expanded = ref("");
const messageState = ref<{ loading: boolean; refreshing: boolean; error: string; items: MailboxMessage[]; syncedAt: string }>({
  loading: false, refreshing: false, error: "", items: [], syncedAt: "",
});
let messageTimer: ReturnType<typeof setInterval> | null = null;

function stopMessagePolling() {
  if (messageTimer) clearInterval(messageTimer);
  messageTimer = null;
}
function startMessagePolling(item: Mailbox) {
  stopMessagePolling();
  messageTimer = setInterval(() => {
    if (!document.hidden && expanded.value === mailboxKey(item)) void loadMessages(item, true);
  }, 5000);
}
async function loadMessages(item: Mailbox, silent = false) {
  if (messageState.value.loading || messageState.value.refreshing) return;
  messageState.value = {
    ...messageState.value,
    loading: !silent && !messageState.value.items.length,
    refreshing: silent || !!messageState.value.items.length,
    error: "",
  };
  const key = mailboxKey(item);
  try {
    const data = await fetchMailboxMessages(item.email, item.provider_type, 10);
    if (expanded.value !== key) return;
    messageState.value = {
      loading: false, refreshing: false,
      error: data.ok === false ? String(data.error || "读取失败") : "",
      items: data.items || [],
      syncedAt: new Date().toISOString(),
    };
  } catch (error) {
    if (expanded.value !== key) return;
    messageState.value = { ...messageState.value, loading: false, refreshing: false, error: error instanceof Error ? error.message : "读取失败" };
  }
}
async function readMessages(item: Mailbox) {
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
function syncTime(value: string) {
  return value ? new Date(value).toLocaleTimeString("zh-CN", { hour12: false }) : "";
}
onBeforeUnmount(stopMessagePolling);

/* --- 保存动作：统一 busy + 结果提示（消息对齐旧版） --- */
async function run(action: () => Promise<Record<string, unknown>>, message: (r: Record<string, unknown>) => string, refresh = false) {
  if (busy.value) return;
  busy.value = true;
  try {
    const result = await action();
    if (result.ok === false) {
      ui.notify(String(result.error || "操作失败"), "error");
      return;
    }
    ui.notify(message(result), "success");
    if (refresh) await Promise.all([mail.fillForms(), status.load(true)]);
  } catch (error) {
    ui.notify(error instanceof Error ? error.message : "操作失败", "error");
  } finally {
    busy.value = false;
  }
}

const importSummary = (label: string) => (r: Record<string, unknown>) =>
  `${label} 导入完成：新增 ${r.added || 0}，更新 ${r.updated || 0}，已存在 ${r.unchanged || 0}，合计 ${r.total || 0}`;

/* --- 列表操作 --- */
function selectedItems() {
  const set = new Set(selected.value);
  return mail.mailboxes.filter((item) => set.has(mailboxKey(item)));
}
async function deleteSelected() {
  const items = selectedItems();
  if (!items.length) return;
  if (!(await confirm(`删除选中的 ${items.length} 个邮箱？`, { title: "删除邮箱", confirmText: "删除", danger: true }))) return;
  await run(() => mail.remove(items), (r) => `已删除 ${r.removed || 0} 个邮箱`);
  selected.value = [];
}
async function releaseSelected() {
  const emails = selectedItems().map((item) => item.email);
  if (!emails.length) return;
  await run(() => mail.setState(emails, "release", []), (r) => `已更新 ${r.changed || 0} 个邮箱状态`);
  selected.value = [];
}
async function releaseAbnormal() {
  await run(() => mail.setState([], "release", ["failed", "in_use", "cooldown"]), (r) => `已更新 ${r.changed || 0} 个邮箱状态`);
}

onMounted(async () => {
  await Promise.all([mail.fillForms(), mail.load()]);
});
</script>

<template>
  <div class="mail-page">
    <!-- 批量导入与按需购买：五渠道 -->
    <section class="panel glass-panel">
      <h2 class="panel-title">邮箱来源配置</h2>
      <p class="panel-sub">导入已有邮箱，或配置注册时按需购买的临时邮箱</p>

      <div class="channel-grid">
        <!-- Outlook -->
        <div class="channel">
          <div class="channel-head">
            <div><h3>批量导入 Outlook</h3><p class="hint">email----password----client_id----refresh_token</p></div>
            <label class="field inline">
              <span>取码模式</span>
              <select v-model="mail.forms.outlookMode" class="input">
                <option value="imap">IMAP</option><option value="graph">Graph</option><option value="auto">Auto</option>
              </select>
            </label>
          </div>
          <textarea v-model="mail.forms.outlookText" class="input area" rows="12" spellcheck="false" placeholder="user@outlook.com----password----client_id----refresh_token"></textarea>
          <div class="channel-actions">
            <AppButton variant="primary" :disabled="busy || !mail.forms.outlookText.trim()" @click="run(mail.saveOutlook, importSummary('Outlook'))">
              <MailPlus :size="14" /><span>导入 Outlook</span>
            </AppButton>
          </div>
        </div>

        <!-- iCloud -->
        <div class="channel">
          <div class="channel-head">
            <div><h3>批量导入 iCloud</h3><p class="hint">email----api_key</p></div>
            <label class="check"><input v-model="mail.forms.icloudEnable" type="checkbox" /><span>启用</span></label>
          </div>
          <label class="field"><span>API Base</span><input v-model="mail.forms.icloudApiBase" class="input" type="text" /></label>
          <textarea v-model="mail.forms.icloudText" class="input area" rows="10" spellcheck="false" placeholder="user@icloud.com----alias_key"></textarea>
          <div class="channel-actions">
            <AppButton variant="primary" :disabled="busy || !mail.forms.icloudText.trim()" @click="run(mail.saveIcloud, importSummary('iCloud'))">
              <MailPlus :size="14" /><span>导入 iCloud</span>
            </AppButton>
          </div>
        </div>

        <!-- SMSBower 临时邮箱 -->
        <div class="channel">
          <div class="channel-head">
            <div><h3>SMSBower 临时邮箱</h3><p class="hint">注册时按需购买，可选择 Gmail 或 iCloud 域名</p></div>
            <label class="check"><input v-model="mail.forms.smsbowerEnable" type="checkbox" /><span>启用</span></label>
          </div>
          <label class="field">
            <span>API Base</span>
            <input v-model="mail.forms.smsbowerApiBase" class="input" type="url" placeholder="https://smsbower.page" />
          </label>
          <label class="field">
            <span class="label-row"><span>API Key</span><em v-if="mail.forms.smsbowerApiKeyConfigured" class="configured">已配置</em></span>
            <input v-model="mail.forms.smsbowerApiKey" class="input" type="password" autocomplete="new-password" :placeholder="mail.forms.smsbowerApiKeyConfigured ? '留空继续使用已保存 key' : '输入 SMSBower API Key'" />
            <small class="help">只有输入新 Key 才会替换；页面不会回显已保存的完整密钥</small>
          </label>
          <div class="field-pair">
            <label class="field"><span>服务代码</span><input v-model="mail.forms.smsbowerService" class="input" type="text" placeholder="dr" /></label>
            <label class="field"><span>单个邮箱最高价（可选）</span><input v-model="mail.forms.smsbowerMaxPrice" class="input" type="text" inputmode="decimal" placeholder="留空不限价" /></label>
          </div>
          <fieldset class="domain-field">
            <legend>邮箱域名</legend>
            <div class="domain-options">
              <label :class="['domain-option', { selected: mail.forms.smsbowerDomains.includes('gmail.com') }]">
                <input v-model="mail.forms.smsbowerDomains" type="checkbox" value="gmail.com" />
                <span><b>Google 邮箱</b><small>@gmail.com</small></span>
              </label>
              <label :class="['domain-option', { selected: mail.forms.smsbowerDomains.includes('icloud.com') }]">
                <input v-model="mail.forms.smsbowerDomains" type="checkbox" value="icloud.com" />
                <span><b>iCloud 邮箱</b><small>@icloud.com</small></span>
              </label>
            </div>
            <small v-if="!mail.forms.smsbowerDomains.length" class="domain-error">请至少选择一个域名</small>
            <small v-else class="help">注册时按勾选顺序尝试可用域名</small>
          </fieldset>
          <label class="check"><input v-model="mail.forms.smsbowerRegisterChild" type="checkbox" /><span>注册子号（邮箱保留到子号收码结束）</span></label>
          <p class="help">子号使用同一邮箱的 +split 别名收码。开启后，每组为 1 个主号 + 1 个子号，任务数量包含两者；主号已注册则跳过整组。设置保存后对新任务生效。</p>
          <div class="channel-actions">
            <AppButton
              variant="primary"
              :disabled="busy || !mail.forms.smsbowerDomains.length || !mail.forms.smsbowerService.trim() || (mail.forms.smsbowerEnable && !mail.forms.smsbowerApiKeyConfigured && !mail.forms.smsbowerApiKey.trim())"
              @click="run(mail.saveSmsbower, () => 'SMSBower 邮箱配置已保存', true)"
            >
              <span>保存 SMSBower</span>
            </AppButton>
          </div>
        </div>

        <!-- Maddy -->
        <div class="channel">
          <div class="channel-head">
            <div><h3>Maddy WebUI</h3><p class="hint">X-API-Key + 域名列表</p></div>
            <label class="check"><input v-model="mail.forms.maddyEnable" type="checkbox" /><span>启用</span></label>
          </div>
          <label class="field">
            <span>API Base</span>
            <input v-model="mail.forms.maddyApiBase" class="input" type="text" placeholder="https://120.53.232.195/api" />
            <small class="help">使用 IP 地址连接，并通过 Host 路由到管理域名</small>
          </label>
          <label class="field">
            <span class="label-row"><span>API Key</span><em v-if="mail.forms.maddyApiKeyConfigured" class="configured">已配置</em></span>
            <input v-model="mail.forms.maddyApiToken" class="input" type="password" autocomplete="new-password" :placeholder="mail.forms.maddyApiKeyConfigured ? '留空继续使用已保存 key' : '输入 X-API-Key'" />
            <small class="help">只有输入新 Key 才会替换；留空沿用服务器已保存的 Key</small>
          </label>
          <label class="field"><span>默认域名</span><input v-model="mail.forms.maddyDefaultDomain" class="input" type="text" placeholder="xb1219.com" /></label>
          <label class="field"><span>域名列表</span><textarea v-model="mail.forms.maddyDomainsText" class="input" rows="3" spellcheck="false" placeholder="xb1219.com&#10;unjome.com"></textarea></label>
          <div class="field-pair">
            <label class="field"><span>用户名长度</span><input v-model.number="mail.forms.maddyLocalLength" class="input" type="number" min="8" max="30" /></label>
            <label class="field"><span>动态容量</span><input v-model.number="mail.forms.maddyCapacity" class="input" type="number" min="1" max="500" /></label>
          </div>
          <label class="field"><span>邮箱密码</span><input v-model="mail.forms.maddyMailboxPassword" class="input" type="password" autocomplete="new-password" placeholder="留空自动生成" /></label>
          <div class="channel-actions">
            <AppButton :disabled="busy || !mail.forms.maddyApiBase.trim()" @click="run(mail.loadMaddyDomains, (r) => `已读取 ${(r.items as string[] || []).length} 个 Maddy 域名`)">
              <RefreshCw :size="14" /><span>刷新域名</span>
            </AppButton>
            <AppButton variant="primary" :disabled="busy || !mail.forms.maddyApiBase.trim()" @click="run(mail.saveMaddy, (r) => `Maddy 配置已保存：动态容量 ${r.total || mail.forms.maddyCapacity}`, true)">
              <span>保存 Maddy</span>
            </AppButton>
          </div>
        </div>

        <!-- Fastmail -->
        <div class="channel">
          <div class="channel-head">
            <div><h3>Fastmail 别名池</h3><p class="hint">mailcode-api 管理别名池并自动补池，注册时自动 acquire</p></div>
            <label class="check"><input v-model="mail.forms.fastmailEnable" type="checkbox" /><span>启用</span></label>
          </div>
          <label class="field"><span>API Base</span><input v-model="mail.forms.fastmailApiBase" class="input" type="text" placeholder="http://服务器IP:8686" /></label>
          <label class="field"><span>管理密钥</span><input v-model="mail.forms.fastmailAdminKey" class="input" type="password" autocomplete="new-password" placeholder="留空保留已保存 key" /></label>
          <label class="field"><span>主账号列表</span><textarea v-model="mail.forms.fastmailAccountsText" class="input" rows="3" spellcheck="false" placeholder="xcovo1219@fastmail.com"></textarea></label>
          <div class="field-pair">
            <label class="field"><span>容量回退</span><input v-model.number="mail.forms.fastmailCapacity" class="input" type="number" min="1" max="1000" /></label>
          </div>
          <div v-if="mail.forms.fastmailPoolRows.length" class="pool-rows">
            <div v-for="row in mail.forms.fastmailPoolRows" :key="row.email" class="pool-row">
              <span class="pool-account">{{ row.email }}</span>
              <AppTag :kind="row.available_count > 0 ? 'success' : 'default'">可用 {{ row.available_count }} / {{ row.alias_count }}（池 {{ row.pool_size }}）</AppTag>
              <AppTag v-if="!row.enabled" kind="error">已停用</AppTag>
            </div>
          </div>
          <div class="channel-actions">
            <AppButton :disabled="busy || !mail.forms.fastmailApiBase.trim()" @click="run(mail.loadFastmailPool, (r) => `Fastmail 池：${r.available ?? 0} 可用 / ${r.total ?? 0} 别名`)">
              <RefreshCw :size="14" /><span>刷新池状态</span>
            </AppButton>
            <AppButton variant="primary" :disabled="busy || !mail.forms.fastmailApiBase.trim()" @click="run(mail.saveFastmail, (r) => `Fastmail 配置已保存：容量回退 ${r.total || mail.forms.fastmailCapacity}`, true)">
              <span>保存 Fastmail</span>
            </AppButton>
          </div>
        </div>
      </div>
    </section>

    <!-- 已导入邮箱 -->
    <section class="panel glass-panel">
      <div class="list-head">
        <div>
          <h2 class="panel-title">已导入邮箱</h2>
          <p class="panel-sub">搜索、分页和读取邮箱最近邮件</p>
        </div>
        <AppButton :loading="mail.loading" @click="mail.load()"><RefreshCw :size="14" /><span>刷新</span></AppButton>
      </div>

      <div class="filters">
        <input v-model="query" class="input search" type="search" placeholder="搜索邮箱、状态或失败原因" />
        <select v-model="provider" class="input">
          <option value="auto">全部邮箱</option><option value="outlook_token">Outlook</option>
          <option value="icloud_api">iCloud</option><option value="smsbower_mail">SMSBower</option><option value="maddy_webui">Maddy</option><option value="fastmail_api">Fastmail</option>
        </select>
        <select v-model="stateFilter" class="input">
          <option value="all">全部状态</option><option value="available">可用</option><option value="in_use">占用</option>
          <option value="used">已使用</option><option value="failed">失败</option><option value="cooldown">冷却</option><option value="token_invalid">凭证失效</option>
        </select>
        <span class="count">{{ filtered.length }} 个邮箱</span>
      </div>

      <AppEmpty v-if="mail.loadError" title="邮箱加载失败" :hint="mail.loadError">
        <template #icon><Inbox :size="48" :stroke-width="1" /></template>
      </AppEmpty>
      <AppEmpty v-else-if="!pageItems.length" :title="mail.mailboxes.length ? '没有匹配的邮箱' : '邮箱池为空'" hint="先在上方批量导入邮箱">
        <template #icon><Inbox :size="48" :stroke-width="1" /></template>
      </AppEmpty>

      <div v-else class="mailbox-list">
        <article v-for="item in pageItems" :key="mailboxKey(item)" class="mailbox-card">
          <div class="mailbox-top">
            <input v-model="selected" type="checkbox" :value="mailboxKey(item)" :aria-label="`选择 ${item.email}`" />
            <div class="mailbox-main">
              <b class="mailbox-email">{{ item.email }}</b>
              <span class="mailbox-sub">{{ providerName(item) }} · {{ item.mode || "-" }} · 登录 {{ item.login_address || item.email }}</span>
              <span v-if="item.reason" class="mailbox-reason">{{ reasonText(item) }}</span>
            </div>
            <div class="mailbox-tags">
              <AppTag :kind="stateKind(item)">{{ stateLabel(item) }}</AppTag>
              <AppTag :kind="registeredKind(item)">{{ registeredLabel(item) }}</AppTag>
            </div>
            <AppButton size="sm" @click="readMessages(item)">{{ expanded === mailboxKey(item) ? "收起邮件" : "查看邮件" }}</AppButton>
          </div>

          <div v-if="expanded === mailboxKey(item)" class="message-viewer">
            <div class="viewer-toolbar">
              <span>{{ messageState.refreshing ? "正在同步…" : messageState.syncedAt ? `同步于 ${syncTime(messageState.syncedAt)}` : "读取中…" }}</span>
              <AppButton size="sm" :disabled="messageState.loading || messageState.refreshing" @click="loadMessages(item, true)">立即刷新</AppButton>
            </div>
            <div v-if="messageState.loading" class="viewer-empty">正在读取邮件</div>
            <div v-else-if="messageState.error" class="viewer-empty error">{{ messageState.error }}</div>
            <div v-else-if="!messageState.items.length" class="viewer-empty">没有读到邮件</div>
            <div v-else class="message-list">
              <article v-for="(message, index) in messageState.items" :key="index" class="message-card">
                <div class="message-title">
                  <b>{{ message.subject || "无主题" }}</b>
                  <AppTag v-if="message.code" kind="success">验证码 {{ message.code }}</AppTag>
                </div>
                <div class="message-meta">{{ message.sender || "未知发件人" }} · {{ message.received_at || "" }}</div>
                <details>
                  <summary>查看正文</summary>
                  <pre>{{ message.text || message.preview || "HTML 邮件" }}</pre>
                </details>
              </article>
            </div>
          </div>
        </article>
      </div>

      <div class="bulkbar">
        <AppButton size="sm" :disabled="!selected.length || busy" @click="releaseSelected">释放选中状态</AppButton>
        <AppButton size="sm" :disabled="busy" @click="releaseAbnormal">释放异常与冷却</AppButton>
        <AppButton size="sm" variant="danger" :disabled="!selected.length || busy" @click="deleteSelected">删除选中</AppButton>
        <AppPager :page="page" :pages="pages" :total="filtered.length" @update:page="page = $event" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.mail-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.panel-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.panel-sub {
  font-size: 0.76rem;
  color: var(--text-muted);
  margin-top: -10px;
}

/* --- 渠道网格 --- */
.channel-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 14px;
}

.channel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-radius: var(--radius-md);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
}

.channel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.channel-head h3 {
  font-size: 0.86rem;
  font-weight: 600;
  color: var(--text-primary);
}

.hint {
  font-size: 0.7rem;
  color: var(--text-muted);
  margin-top: 2px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field > span {
  font-size: 0.74rem;
  color: var(--text-secondary);
  font-weight: 500;
}

.field.inline {
  min-width: 110px;
}

.field-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.configured {
  font-style: normal;
  color: var(--success);
  font-size: 0.68rem;
  font-weight: 700;
}

.help {
  font-size: 0.66rem;
  color: var(--text-muted);
}

.area {
  flex: 1;
  min-height: 200px;
  resize: vertical;
}

.channel-actions {
  display: flex;
  gap: 8px;
  margin-top: auto;
}

.check {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 0.78rem;
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
}

.check input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
}

.pool-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 0.74rem;
  color: var(--text-muted);
}

.pool-account {
  font-family: var(--font-mono);
  color: var(--text-primary);
}

.domain-field {
  border: 0;
  padding: 0;
  min-width: 0;
}

.domain-field legend {
  margin-bottom: 6px;
  font-size: 0.74rem;
  color: var(--text-secondary);
  font-weight: 500;
}

.domain-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.domain-option {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  background: var(--inset-bg);
  color: var(--text-secondary);
  cursor: pointer;
  transition: var(--transition-fast);
}

.domain-option.selected {
  border-color: var(--accent-border);
  background: var(--accent-bg);
  color: var(--text-primary);
}

.domain-option input {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  accent-color: var(--accent);
}

.domain-option span {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.domain-option b {
  font-size: 0.75rem;
  font-weight: 600;
}

.domain-option small {
  font-size: 0.66rem;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.domain-error {
  display: block;
  margin-top: 5px;
  color: var(--error);
  font-size: 0.66rem;
}

/* --- 列表 --- */
.list-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}

.filters .search {
  flex: 1;
  min-width: 200px;
}

.filters select {
  width: auto;
}

.count {
  font-size: 0.74rem;
  color: var(--text-muted);
}

.mailbox-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mailbox-card {
  border-radius: var(--radius-sm);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  padding: 10px 12px;
}

.mailbox-top {
  display: flex;
  align-items: center;
  gap: 10px;
}

.mailbox-top > input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  flex-shrink: 0;
}

.mailbox-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.mailbox-email {
  color: var(--text-primary);
  font-weight: 500;
  overflow-wrap: anywhere;
}

.mailbox-sub {
  font-size: 0.7rem;
  color: var(--text-muted);
}

.mailbox-reason {
  font-size: 0.7rem;
  color: var(--warning);
}

.mailbox-tags {
  display: flex;
  gap: 5px;
  flex-shrink: 0;
}

/* --- 行内读信 --- */
.message-viewer {
  margin-top: 10px;
  border-top: 1px solid var(--line-subtle);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.viewer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.72rem;
  color: var(--text-muted);
}

.viewer-empty {
  text-align: center;
  color: var(--text-muted);
  font-size: 0.78rem;
  padding: 14px 0;
}

.viewer-empty.error {
  color: var(--error);
}

.message-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.message-card {
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--inset-bg);
  font-size: 0.76rem;
}

.message-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--text-primary);
}

.message-meta {
  color: var(--text-muted);
  font-size: 0.68rem;
  margin: 2px 0 4px;
}

.message-card summary {
  cursor: pointer;
  color: var(--accent);
  font-size: 0.7rem;
}

.message-card pre {
  margin: 6px 0 0;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-secondary);
  font-size: 0.72rem;
  max-height: 220px;
  overflow-y: auto;
}

/* --- 底部操作条 --- */
.bulkbar {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  border-top: 1px solid var(--line-subtle);
  padding-top: 12px;
}

.bulkbar :deep(.pager) {
  margin-left: auto;
}

@media (max-width: 1024px) {
  .channel-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 420px) {
  .domain-options {
    grid-template-columns: 1fr;
  }
}
</style>
