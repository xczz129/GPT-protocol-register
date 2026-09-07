<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from "vue";
import { Smartphone, X } from "@lucide/vue";
import { api, apiUrl, downloadPost, jsonBody } from "./api.js";
import AccountsView from "./components/AccountsView.vue";
import AccountLogDialog from "./components/AccountLogDialog.vue";
import CodexOauthDialog from "./components/CodexOauthDialog.vue";
import ImportAccountsDialog from "./components/ImportAccountsDialog.vue";
import SmsCountryPicker from "./components/SmsCountryPicker.vue";
import ConcurrencyDialog from "./components/ConcurrencyDialog.vue";
import AppDialog from "./components/AppDialog.vue";
import AppSidebar from "./components/AppSidebar.vue";
import AppTopbar from "./components/AppTopbar.vue";
import BrowserView from "./components/BrowserView.vue";
import CliproxyView from "./components/CliproxyView.vue";
import LoginView from "./components/LoginView.vue";
import ClipboardDialog from "./components/ClipboardDialog.vue";
import CredentialExportView from "./components/CredentialExportView.vue";
import ProtocolLoginView from "./components/ProtocolLoginView.vue";
import IntegrationView from "./components/IntegrationView.vue";
import LogsView from "./components/LogsView.vue";
import MailView from "./components/MailView.vue";
import MetricStrip from "./components/MetricStrip.vue";
import OperationsView from "./components/OperationsView.vue";
import OperationProgress from "./components/OperationProgress.vue";
import PasswordChangeDialog from "./components/PasswordChangeDialog.vue";
import ProxyView from "./components/ProxyView.vue";
import PlusEligibilityView from "./components/PlusEligibilityView.vue";
import RebindView from "./components/RebindView.vue";
import TrialProxyView from "./components/TrialProxyView.vue";
import TaskView from "./components/TaskView.vue";

const views = {
  task: ["注册任务", "创建、调度并观察账号注册任务"],
  fingerprint: ["指纹浏览器", "检查可用运行环境并管理指纹参数"],
  mail: ["邮箱池", "维护 Outlook、iCloud、域名邮箱、Maddy 与 Fastmail"],
  proxy: ["代理设置", "配置注册出站、资源代理与清障服务"],
  "plus-eligibility": ["Plus 免费试用", "检查账号是否符合 Plus 1 个月免费试用；支持 v4 与 nerver 双渠道"],
  chatgpt2api: ["chatgpt2api", "管理账号自动上传与连接状态"],
  logs: ["实时日志", "查看任务执行过程与异常信息"],
  accounts: ["账号记录", "搜索、分页、测活并导出已注册账号"],
  rebind: ["邮箱换绑", "将现有账号换绑到邮箱池中的未占用地址并刷新凭据"],
  "credential-export": ["凭据导出", "按邮箱筛选并导出账号、密码和 2FA"],
  "protocol-login": ["协议登录", "按邮箱批量重新登录并查看账号状态"],
  cliproxy: ["中转管理", "查看 cliproxy 中转账号额度并下载凭据"],
  operations: ["运维中心", "统计、自动维护、备份恢复与面板安全"],
};

const onLoginPage = location.pathname === "/login";
const active = ref("task");
const sidebarOpen = ref(false);
const loading = ref(true);
const busy = ref(false);
const connected = ref(false);
const username = ref("");
const status = ref({ job: {}, mail: {}, proxy: {} });
const config = ref({ fingerprint: {}, registration: {} });
// Account rows are immutable snapshots from the API. A shallow ref avoids
// creating thousands of nested Vue proxies; callers replace the array when a
// batch changes (see applyHealthProgress and the group actions below).
const accounts = shallowRef([]);
const groups = ref([]);
// Task cards can contain a large history and nested step arrays. They are
// treated as immutable snapshots, so deep proxying adds cost without helping
// the task view; writers replace the array below.
const tasks = shallowRef([]);
// Keep task cards indexed by their stable id. SSE updates usually touch only
// a handful of cards; rebuilding and sorting the full array for every packet
// needlessly scales with the entire task history.
const taskIndex = new Map();
const mailboxes = ref([]);
const logs = ref([]);
const capabilities = ref({ items: [] });
const proxyDiagnostics = ref({ items: [] });
const operations = ref({ metrics: {}, maintenance: {}, maddy_auto_register: {}, state: {}, backups: [] });
const trash = ref([]);
const cliproxyAccounts = ref({ configured: false, auths_dir: "", items: [] });
const cliproxyLoading = ref(false);
const cliproxyRefreshing = ref(false);
const cliproxyImporting = ref(false);
const cliproxyDeleting = ref(false);
const cliproxyResettingFile = ref("");
const progress = ref({ running: 0, items: [] });
const maintenanceRunning = ref(false);
const maddyAutoRunning = ref(false);
const maintenanceActive = computed(
  () => maintenanceRunning.value
    || (progress.value.items || []).some(
      (item) => item.key === "account_maintenance" && item.status === "running",
    ),
);
const maddyAutoActive = computed(
  () => maddyAutoRunning.value
    || !!operations.value.maddy_auto_register?.running
    || !!operations.value.state?.maddy_auto_current_domain,
);
const notice = reactive({ text: "", kind: "" });
const dialog = reactive({ open: false, title: "", message: "", confirmText: "", danger: false, input: false, inputType: "text", value: "", maxlength: 80 });
const passwordChangeDialog = reactive({ open: false, count: 0 });
const clipboardDialog = reactive({ open: false, title: "", value: "", successMessage: "" });
const accountLogDialog = reactive({ open: false, account: null, logs: [], total: 0, loading: false, error: "" });
const copyingAccessToken = ref(false);
const healthProgress = reactive({ running: false, requested: 0, actualTotal: 0, total: 0, done: 0, checked: 0, usable: 0, unavailable: 0, skippedKnownDeactivated: 0, deactivated: 0, invalidOther: 0, current: "", alive: 0, limited: 0, invalid: 0, reauthRequired: 0, error: 0, requestedWorkers: 0, workers: 0, throttled: false, resourceMode: "normal", updates: [], interrupted: false, pendingCount: 0, message: "" });
const reauthProgress = reactive({ running: false, stopping: false, mode: "protocol", operation: "reauth", concurrency: 3, total: 0, done: 0, current: "", success: 0, failed: 0, cancelled: 0, last: "", lastStatus: "", lastMessage: "", items: [] });
const protocolLoginPreflight = reactive({ requested: 0, matched: 0, submitted: 0, invalidFormat: [], notFound: [], missingCredentials: [] });
const rebindPreflight = reactive({ eligible: false, requested: 0, matched: 0, eligibleCount: 0, source: null, sources: [], target: null, items: [], reservationId: "", invalidFormat: [], invalidTargets: [], notFound: [], missingCredentials: [], conflicts: [], message: "" });
const rebindProgress = reactive({ jobId: "", running: false, stopping: false, status: "", mode: "protocol", operation: "email_rebind", concurrency: 1, total: 0, done: 0, current: "", success: 0, failed: 0, cancelled: 0, last: "", lastStatus: "", lastMessage: "", message: "", skipped: [], items: [] });
const rebindChecking = ref(false);
const rebindRequestBusy = ref(false);
const trialProgress = reactive({ running: false, total: 0, done: 0, current: "", eligible: 0, ineligible: 0, failed: 0, stopped: 0, jobId: "", interrupted: false, pendingCount: 0, message: "" });
const refreshAtProgress = reactive({ running: false, total: 0, done: 0, current: "", refreshed: 0, unchanged: 0, failed: 0, stopped: 0, jobId: "" });
let noticeTimer = null;
let dialogResolve = null;
let passwordChangeDialogResolve = null;
let stream = null;
let pollTimer = null;
let progressTimer = null;
let healthProgressTimer = null;
let healthProgressLoading = false;
let healthRequestActive = false;
let healthServerRunObserved = false;
let reauthProgressTimer = null;
let reauthProgressLoading = false;
let reauthJobId = "";
let reauthCompletionAnnounced = "";
let rebindProgressTimer = null;
let rebindProgressLoading = false;
let rebindJobId = "";
let rebindCompletionAnnounced = "";
let rebindInputGeneration = 0;
let trialProgressTimer = null;
let refreshAtProgressTimer = null;
let streamReconnectTimer = null;
let streamSeeded = false;
let logStreamVersion = 0;
let taskStreamSupported = false;
let lastLegacyTaskRefreshAt = 0;
let accountLoadSequence = 0;
let accountLoadPromise = null;
let accountReloadQueued = false;
const accountsLoading = ref(false);
let groupsLoadPromise = null;
let accountLogRequestSequence = 0;
let accountRefreshTimer = null;
let accountRefreshIncludeGroups = false;
let accountReloadQueuedWithGroups = false;
let lastAccountRefreshStartedAt = 0;
// A full account refresh fans out into several 1,000-row requests. Keep
// progress callbacks from starting another fan-out immediately after it.
const accountRefreshMinInterval = 1500;
let taskLoadPromise = null;
let taskLoadSequence = 0;
let taskStreamRevision = -1;
let taskStreamGeneration = 0;
// A stream generation is not authoritative until its first tasks snapshot
// arrives. This matters when navigation starts an API load and reconnects the
// stream at the same time: an empty/new stream must not suppress that load.
let taskStreamSnapshotGeneration = 0;
let taskStreamSnapshotHasItems = false;
let taskIndexFlushTimer = null;
let taskIndexDirty = false;
let lastMaintenanceHealthDone = -1;
let lastStatusLoadedAt = 0;
let lastRegistrationBatchId = "";
let lastRegistrationSuccessCount = 0;
const accountFetchLimit = 10000;
// The account endpoint is paginated. Fetching pages serially makes a full
// account refresh wait for every disk/cache round-trip; keep a small bound so
// the browser and API are not flooded while still reducing wall-clock time.
// Keep pagination bounded while allowing a full account refresh to complete
// in one or two waves on typical deployments. The first page is published
// immediately below, so this only affects tail latency.
const accountFetchConcurrency = 6;
const healthInlineUpdateLimit = 200;
// Account rows are published page by page for a fast first paint. Keep the
// authoritative total and the visible collection count separate so bulk
// actions can show progress without treating the first page as the full set.
const accountLoadProgress = reactive({ loaded: 0, total: 0 });
const accountLoadError = ref("");
const REAUTH_UI_STATE_KEY = "gpt-register-fp.protocol-login-progress.v1";
const REBIND_UI_STATE_KEY = "gpt-register-fp.email-rebind-progress.v1";

const taskForm = reactive({ engine: "protocol", total: 1, concurrency: 1, mailType: "auto", email: "", keepOnFail: false, groupId: "", autoGroup: false, autoGroupSize: 35 });
const mailForms = reactive({
  outlookText: "",
  outlookMode: "imap",
  icloudText: "",
  icloudApiBase: "https://icloud.xbovo.online",
  icloudEnable: true,
  maddyEnable: false,
  maddyApiBase: "https://120.53.232.195/api",
  maddyApiToken: "",
  maddyApiKeyConfigured: false,
  maddyApiKeyMasked: "",
  maddyDefaultDomain: "xb1219.com",
  maddyDomainsText: "xb1219.com\nunjome.com",
  maddyLocalLength: 12,
  maddyCapacity: 500,
  maddyMailboxPassword: "",
  fastmailEnable: false,
  fastmailApiBase: "http://127.0.0.1:8686",
  fastmailAdminKey: "",
  fastmailAccountsText: "",
  fastmailCapacity: 500,
  fastmailPoolRows: [],
});
const proxyForm = reactive({ enabled: false, rotate: true, mode: "single_proxy", url: "", resourceUrl: "", clearanceMode: "none", flareUrl: "", flareProxy: "", poolFile: "data/proxy_pool.txt", poolText: "", poolTextTouched: false, poolTextComplete: false, poolCount: 0, poolUniqueCount: 0, selectedCountry: "", countries: [], countryPoolPath: "" });
const trialProxyForm = reactive({ selected_country: "", countries: [] });
const tokenEligibilityForm = reactive({ accessToken: "", batchTokens: "", useConfiguredProxy: true, channel: "v4", workers: 20 });
let tokenEligibilitySynced = false;
// 页面上切换检测渠道时，立即保存为默认渠道，账号列表的资格检测弹窗会跟随此默认值
watch(() => tokenEligibilityForm.channel, async (value) => {
  const channel = value === "nerver" ? "nerver" : "v4";
  try {
    if (!uiSettingsLoaded) await loadUiSettings();
    if (uiSettings.value.trial_channel === channel) return;
    const payload = {
      health_workers: Number(uiSettings.value.health_workers) || 20,
      reauth_workers: Number(uiSettings.value.reauth_workers) || 3,
      trial_workers: Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.trial_workers) || 20))),
      refresh_at_workers: Math.max(1, Math.min(10, Math.floor(Number(uiSettings.value.refresh_at_workers) || 10))),
      reauth_mode: uiSettings.value.reauth_mode === "browser" ? "browser" : "protocol",
      trial_channel: channel,
    };
    await api("/api/ui-settings", { method: "PUT", body: jsonBody(payload) });
    uiSettings.value = payload;
  } catch {}
});
const tokenEligibility = reactive({ loading: false, error: "", result: null, batch: { running: false, total: 0, done: 0, current: "", results: [], stopped: false } });
const integrationForm = reactive({ enabled: false, autoUpload: true, url: "", key: "", groupId: "" });
const maintenanceForm = reactive({ enabled: false, intervalHours: 12, workers: 10, autoDeleteInvalid: false, webhookUrl: "", notifyOnComplete: false, notifyOnPoolLow: false, poolLowThreshold: 3 });
const backupScheduleForm = reactive({ enabled: false, time: "04:00", retentionDays: 7 });
const maddyAutoForm = reactive({ enabled: false, intervalHours: 1, perDomain: 20, concurrency: 3, engine: "protocol", keepOnFail: false, refreshDomains: true, groupId: "" });
const securityForm = reactive({ currentPassword: "", newPassword: "", confirmPassword: "" });
const title = computed(() => views[active.value]?.[0] || views.task[0]);
const subtitle = computed(() => views[active.value]?.[1] || views.task[1]);

function notify(text, kind = "ok") {
  notice.text = text;
  notice.kind = kind;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.text = ""; }, 5000);
}

function closeDialog(result) {
  dialog.open = false;
  const resolve = dialogResolve;
  dialogResolve = null;
  resolve?.(result);
}

function openDialog(options = {}) {
  if (dialogResolve) closeDialog(false);
  Object.assign(dialog, {
    open: true,
    title: options.title || "确认操作",
    message: options.message || "",
    confirmText: options.confirmText || "确认",
    danger: !!options.danger,
    input: !!options.input,
    inputType: options.inputType || "text",
    value: options.value || "",
    maxlength: options.maxlength || 80,
  });
  return new Promise((resolve) => { dialogResolve = resolve; });
}

function confirmInside(message, options = {}) { return openDialog({ ...options, message }); }
function promptInside(message, value = "", options = {}) { return openDialog({ ...options, message, value, input: true }); }

function closePasswordChangeDialog(result) {
  passwordChangeDialog.open = false;
  const resolve = passwordChangeDialogResolve;
  passwordChangeDialogResolve = null;
  resolve?.(result);
}

function chooseAccountPassword(count) {
  if (dialogResolve) closeDialog(false);
  if (passwordChangeDialogResolve) closePasswordChangeDialog(false);
  Object.assign(passwordChangeDialog, { open: true, count });
  return new Promise((resolve) => { passwordChangeDialogResolve = resolve; });
}

function navigate(name, updateHash = true) {
  // Leaving while preflight is still in flight must invalidate its response
  // and release any candidate it returns. A running/starting job owns its
  // mailbox already, so keep that state intact while navigating elsewhere.
  if (active.value === "rebind" && name !== "rebind" && !rebindProgress.running && !rebindRequestBusy.value) {
    rebindInputGeneration += 1;
    const reservationId = rebindPreflight.reservationId;
    if (reservationId) void releaseRebindReservation(reservationId);
    resetRebindPreflight();
  }
  if (name === "trial-proxy") name = "plus-eligibility";
  active.value = views[name] ? name : "task";
  sidebarOpen.value = false;
  document.title = `${title.value} · GPT Register FP`;
  if (updateHash) history.replaceState(null, "", `/#${active.value}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (!loading.value) {
    loadActiveView().catch(() => {});
    // The stream only subscribes to data needed by the active view. Reconnect
    // after navigation so the server can drop expensive log/task snapshots.
    if (stream) connectStream();
  }
}

function openSidebar() { sidebarOpen.value = true; }
function closeSidebar() { sidebarOpen.value = false; }
function handleShellKeydown(event) {
  if (event.key === "Escape") closeSidebar();
}

function provider(type) {
  return (config.value.mail?.providers || []).find((item) => item.type === type) || {};
}

function parseDomainList(text) {
  return [...new Set(
    String(text || "")
      .split(/[\n,;]+/)
      .map((value) => value.trim())
      .filter(Boolean),
  )];
}

// Secret values returned by the config endpoint are redacted for display.
// Keep the input empty and omit these values from write requests; sending a
// redaction such as "maddy_…acf3" as an HTTP header causes authentication
// failures (and can fail header encoding before the request is sent).
function isMaskedSecret(value) {
  const text = String(value || "").trim();
  return !text || text.includes("…") || text.includes("•") || text.includes("***");
}

function maddyApiKeyPayload() {
  const value = String(mailForms.maddyApiToken || "").trim();
  return value && !isMaskedSecret(value) ? { api_key: value } : {};
}

function fillForms() {
  const cfg = config.value;
  const registration = cfg.registration || {};
  taskForm.engine = ["browser", "hybrid", "protocol"].includes(registration.engine)
    ? registration.engine
    : "protocol";
  taskForm.total = Number(registration.total || 1);
  taskForm.concurrency = Number(registration.threads || 1);
  if (!groups.value.some((item) => item.id === taskForm.groupId)) taskForm.groupId = groups.value[0]?.id || "";
  const outlook = provider("outlook_token");
  const icloud = provider("icloud_api");
  const maddy = provider("maddy_webui");
  const fastmail = provider("fastmail_api");
  mailForms.outlookText = "";
  mailForms.outlookMode = outlook.mode || "imap";
  mailForms.icloudText = "";
  mailForms.icloudApiBase = icloud.api_base || "https://icloud.xbovo.online";
  mailForms.icloudEnable = icloud.enable !== false;
  mailForms.maddyEnable = maddy.enable === true;
  mailForms.maddyApiBase = maddy.api_base || "https://120.53.232.195/api";
  const maddyMaskedKey = maddy.api_key_masked || maddy.api_token_masked || "";
  const maddyReturnedKey = maddy.api_key || maddy.api_token || "";
  mailForms.maddyApiToken = isMaskedSecret(maddyReturnedKey) ? "" : maddyReturnedKey;
  mailForms.maddyApiKeyMasked = String(maddyMaskedKey || (isMaskedSecret(maddyReturnedKey) ? maddyReturnedKey : ""));
  mailForms.maddyApiKeyConfigured = Boolean(
    maddy.has_api_key || maddy.has_api_token || mailForms.maddyApiKeyMasked,
  );
  mailForms.maddyDefaultDomain = maddy.default_domain || maddy.domain || "xb1219.com";
  mailForms.maddyDomainsText = Array.isArray(maddy.domains) ? maddy.domains.join("\n") : "";
  mailForms.maddyLocalLength = Number(maddy.local_length || 12);
  mailForms.maddyCapacity = Number(maddy.capacity || 500);
  mailForms.maddyMailboxPassword = "";
  mailForms.fastmailEnable = fastmail.enable === true;
  mailForms.fastmailApiBase = fastmail.api_base || "http://127.0.0.1:8686";
  mailForms.fastmailAdminKey = "";
  mailForms.fastmailAccountsText = String(fastmail.accounts || "");
  mailForms.fastmailCapacity = Number(fastmail.capacity || 500);
  mailForms.fastmailPoolRows = [];
  const proxy = cfg.proxy || {};
  const proxyState = status.value.proxy || {};
  const countryPools = proxyState.country_pools || {};
  const clearance = proxy.clearance || {};
  Object.assign(proxyForm, {
    enabled: !!proxy.enabled, rotate: proxy.rotate_per_task !== false,
    mode: proxy.mode || "single_proxy", url: proxy.url || "", resourceUrl: proxy.resource_url || "",
    clearanceMode: clearance.mode || "none", flareUrl: clearance.flaresolverr_url || "", flareProxy: clearance.proxy_url || "",
    poolFile: proxy.pool_file || "data/proxy_pool.txt",
    poolText: typeof proxyState.pool_text === "string" ? proxyState.pool_text : "",
    poolTextTouched: false,
    poolTextComplete: typeof proxyState.pool_text === "string",
    poolCount: Number(proxyState.pool_count || 0),
    poolUniqueCount: Number(proxyState.pool_unique_count || proxyState.pool_count || 0),
    selectedCountry: String(proxy.selected_country || proxyState.selected_country || "").toUpperCase(),
    countries: (countryPools.items || []).map((item) => ({ ...item })),
    countryPoolPath: countryPools.path || "data/registration_proxy_pools.json",
  });
  const integration = cfg.chatgpt2api || {};
  Object.assign(integrationForm, { enabled: !!integration.enabled, autoUpload: integration.auto_upload !== false, url: integration.url || "", key: "" });
  if (!groups.value.some((item) => item.id === integrationForm.groupId)) integrationForm.groupId = "";
}

let statusLoadPromise = null;
async function loadStatus() {
  // Status is requested by the visibility handler, the periodic fallback and
  // several action callbacks. Share an in-flight request instead of creating
  // duplicate expensive summary calculations on the server.
  if (statusLoadPromise) return statusLoadPromise;
  statusLoadPromise = api("/api/status");
  try {
    const next = await statusLoadPromise;
    status.value = next;
    lastStatusLoadedAt = Date.now();
    return next;
  } finally {
    statusLoadPromise = null;
  }
}
async function loadConfig() { config.value = (await api("/api/config")).config || {}; }
async function loadAccounts() {
  if (accountLoadPromise) {
    accountReloadQueued = true;
    return accountLoadPromise;
  }
  // A direct refresh supersedes a delayed one that may have been scheduled by
  // a progress callback.
  const refreshPendingGroups = accountRefreshIncludeGroups;
  clearTimeout(accountRefreshTimer);
  accountRefreshTimer = null;
  accountRefreshIncludeGroups = false;
  if (refreshPendingGroups) loadGroups().catch(() => {});
  lastAccountRefreshStartedAt = Date.now();
  // Keep the last complete (or previously visible partial) snapshot on screen
  // while a refresh assembles its replacement. Publishing the first page of a
  // refresh here would temporarily shrink the table to 1,000 rows and make
  // the pager clamp the user's current page.
  const previousSnapshot = accounts.value;
  const preserveSnapshot = previousSnapshot.length > 0;
  const sequence = ++accountLoadSequence;
  accountsLoading.value = true;
  accountLoadError.value = "";
  accountLoadProgress.loaded = accounts.value.length;
  accountLoadProgress.total = Math.max(
    Number(accountLoadProgress.total || 0),
    Number(status.value.accounts_count || 0),
    accounts.value.length,
  );
  accountLoadPromise = (async () => {
    class AccountConsistencyError extends Error {}
    const maxConsistencyRetries = 3;
      let lastConsistencyError = null;
      try {
        for (let consistencyAttempt = 0; consistencyAttempt < maxConsistencyRetries; consistencyAttempt += 1) {
        try {
          const collected = new Map();
        let total = 0;
        let expectedRevision = null;
        let shortPageEnd = null;
        const queuedOffsets = [];
        const queuedSet = new Set();
        const maxPages = 100;

        const enqueueOffset = (offset) => {
          const value = Math.max(0, Math.floor(Number(offset) || 0));
          if (value / accountFetchLimit >= maxPages || queuedSet.has(value)) {
            if (value / accountFetchLimit >= maxPages) {
              throw new AccountConsistencyError("账号列表超过分页上限，已保留当前快照");
            }
            return;
          }
          queuedSet.add(value);
          queuedOffsets.push(value);
        };

        const fetchPage = async (offset, maxAttempts = 3) => {
          let lastError = null;
          for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
            try {
              return await api(`/api/accounts?limit=${accountFetchLimit}&offset=${offset}`);
            } catch (error) {
              lastError = error;
              if (attempt + 1 < maxAttempts) {
                await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
              }
            }
          }
          throw lastError || new Error(`账号分页 ${offset} 加载失败`);
        };

        const addPage = (data, offset) => {
          const batch = Array.isArray(data?.items) ? data.items : [];
          const rawRevision = data?.revision;
          const parsedRevision = rawRevision === null || rawRevision === undefined || rawRevision === ""
            ? null
            : Number(rawRevision);
          const revision = Number.isFinite(parsedRevision) ? parsedRevision : null;
          if (revision !== null) {
            if (expectedRevision !== null && expectedRevision !== revision) {
              throw new AccountConsistencyError("账号列表在分页期间发生更新，正在重新同步");
            }
            expectedRevision = revision;
          }
          const reportedTotal = Number(data?.total ?? data?.count ?? 0);
          if (Number.isFinite(reportedTotal) && reportedTotal >= 0) {
            total = Math.max(total, reportedTotal);
          }
          // The endpoint is ordered and deterministic. A full page at the
          // requested offset also proves that at least offset + length rows
          // exist, which catches stale/under-reported totals.
          total = Math.max(total, Number(offset) + batch.length);
          for (const item of batch) {
            const key = String(item?.delete_key || item?.account_id || item?.email || "");
            if (key) collected.set(key, item);
          }
          if (batch.length < accountFetchLimit) {
            const end = Number(offset) + batch.length;
            shortPageEnd = shortPageEnd === null ? end : Math.min(shortPageEnd, end);
          }
          return batch.length;
        };

        const publishCollected = () => {
          if (sequence !== accountLoadSequence) return;
          const snapshot = [...collected.values()];
          // ``accounts`` is a shallow ref: publish immutable snapshots so Vue
          // does not proxy thousands of nested account fields on every page.
          // During a refresh, progress is published independently and the old
          // account array remains authoritative until every page succeeds.
          if (!preserveSnapshot) {
            accounts.value = snapshot;
            status.value = { ...status.value, accounts_count: Math.max(total, snapshot.length) };
          }
          accountLoadProgress.loaded = snapshot.length;
          accountLoadProgress.total = Math.max(total, snapshot.length);
        };

        // The first page carries the authoritative total and revision. Keep
        // rendering it immediately only on the initial load; an existing full
        // snapshot stays visible until every page in this attempt is verified.
        const first = await fetchPage(0);
        const firstLength = addPage(first, 0);
        const knownTotal = total;
        publishCollected();

        let plannedFinalOffset = null;
        if (knownTotal > 0) {
          const pageCount = Math.ceil(knownTotal / accountFetchLimit);
          if (pageCount > maxPages) {
            throw new AccountConsistencyError("账号列表超过分页上限，已保留当前快照");
          }
          plannedFinalOffset = Math.max(0, (pageCount - 1) * accountFetchLimit);
          for (let page = 1; page < pageCount; page += 1) {
            enqueueOffset(page * accountFetchLimit);
          }
          // Always probe the page after a reported exact multiple (including
          // the common 1,000-row case). This prevents a stale total from
          // silently truncating a larger account store.
          if (knownTotal % accountFetchLimit === 0 && firstLength >= accountFetchLimit) {
            enqueueOffset(pageCount * accountFetchLimit);
          }
        } else if (firstLength >= accountFetchLimit) {
          // Compatibility with older endpoints that omit `total`: walk until
          // a short page is observed.
          enqueueOffset(accountFetchLimit);
        }

        while (queuedOffsets.length) {
          const offsets = queuedOffsets.splice(0, accountFetchConcurrency);
          const results = await Promise.allSettled(offsets.map((offset) => fetchPage(offset)));
          const failed = [];
          results.forEach((result, resultIndex) => {
            if (result.status === "fulfilled") {
              const offset = offsets[resultIndex];
              const length = addPage(result.value, offset);
              // If the expected last page was full, probe one more page. This
              // handles an under-reported total without serialising normal
              // multi-page loads.
              if (length >= accountFetchLimit
                && (plannedFinalOffset === null || offset >= plannedFinalOffset)) {
                enqueueOffset(offset + accountFetchLimit);
              }
            } else if (result.reason instanceof AccountConsistencyError) {
              throw result.reason;
            } else {
              failed.push({ offset: offsets[resultIndex], message: result.reason?.message || "请求失败" });
            }
          });
          // A browser can cancel one request in a concurrent batch during a
          // navigation or connection refresh. Retry rejected offsets one at a
          // time before treating the whole collection as incomplete.
          const stillFailed = [];
          for (const item of failed) {
            try {
              const data = await fetchPage(item.offset, 5);
              const length = addPage(data, item.offset);
              if (length >= accountFetchLimit
                && (plannedFinalOffset === null || item.offset >= plannedFinalOffset)) {
                enqueueOffset(item.offset + accountFetchLimit);
              }
            } catch (error) {
              if (error instanceof AccountConsistencyError) throw error;
              stillFailed.push(`${item.offset}: ${error?.message || item.message}`);
            }
          }
          publishCollected();
          if (stillFailed.length) throw new Error(`账号列表分页加载失败（${stillFailed.join("；")}）`);
        }

        const items = [...collected.values()];
        // A short page establishes the actual end of the ordered result set.
        // If the reported total disagrees, or de-duplication dropped rows,
        // discard this attempt and retry against a fresh revision.
        if (shortPageEnd !== null) {
          if (items.length !== shortPageEnd || total > shortPageEnd) {
            throw new AccountConsistencyError("账号列表分页不完整，正在重新同步");
          }
          total = shortPageEnd;
        }
        if (items.length < total) {
          throw new AccountConsistencyError("账号列表分页不完整，正在重新同步");
        }
        if (items.length > total) total = items.length;
        if (sequence === accountLoadSequence) {
          accounts.value = items;
          accountLoadProgress.loaded = items.length;
          accountLoadProgress.total = total;
          status.value = { ...status.value, accounts_count: total };
        }
          return items;
        } catch (error) {
          if (!(error instanceof AccountConsistencyError)) throw error;
          lastConsistencyError = error;
          if (consistencyAttempt + 1 < maxConsistencyRetries) {
            // A writer may be finishing a re-login update. Give its revision
            // a brief chance to settle before rebuilding every page.
            await new Promise((resolve) => setTimeout(resolve, 120));
          }
        }
      }
      throw lastConsistencyError || new Error("账号列表在持续更新，请稍后重试");
    } catch (error) {
      if (sequence === accountLoadSequence) {
        accountLoadError.value = error?.message || "账号列表加载失败，请点击刷新列表重试";
      }
      throw error;
    }
  })();
  try {
    return await accountLoadPromise;
  } finally {
    accountLoadPromise = null;
    accountsLoading.value = false;
    if (accountReloadQueued) {
      const includeGroups = accountReloadQueuedWithGroups;
      accountReloadQueued = false;
      accountReloadQueuedWithGroups = false;
      scheduleAccountsRefresh(accountRefreshMinInterval, includeGroups);
    }
  }
}
async function loadTrialProxyPool() { const data = await api("/api/trial-proxy-pool"); trialProxyForm.selected_country = data.selected_country || ""; trialProxyForm.countries = (data.countries || []).map((item) => ({ ...item })); }
async function loadGroups() {
  if (groupsLoadPromise) return groupsLoadPromise;
  groupsLoadPromise = (async () => {
    groups.value = (await api("/api/account-groups")).items || [];
    syncGroupSelections();
    return groups.value;
  })();
  try {
    return await groupsLoadPromise;
  } finally {
    groupsLoadPromise = null;
  }
}
function syncGroupSelections() {
  if (!groups.value.some((item) => item.id === taskForm.groupId)) taskForm.groupId = groups.value[0]?.id || "";
  if (!groups.value.some((item) => item.id === integrationForm.groupId)) integrationForm.groupId = "";
}
function applyGroupCountDeltas(deltas) {
  groups.value = groups.value.map((group) => ({
    ...group,
    account_count: Math.max(0, Number(group.account_count || 0) + Number(deltas.get(group.id) || 0)),
  }));
}
function isAccountView() {
  return ["accounts", "credential-export", "protocol-login", "chatgpt2api"].includes(active.value);
}
function stageAccountRemoval(keys, deleteAll = false) {
  const previous = {
    accounts: accounts.value,
    groups: groups.value,
    accountCount: status.value.accounts_count,
  };
  const wanted = new Set(keys || []);
  const removed = deleteAll
    ? accounts.value
    : accounts.value.filter((item) => wanted.has(item.delete_key));
  const removedKeys = new Set(removed.map((item) => item.delete_key));
  accounts.value = deleteAll
    ? []
    : accounts.value.filter((item) => !removedKeys.has(item.delete_key));
  const deltas = new Map();
  for (const item of removed) {
    const groupId = item.group_id || "";
    deltas.set(groupId, Number(deltas.get(groupId) || 0) - 1);
  }
  applyGroupCountDeltas(deltas);
  const currentCount = Number(previous.accountCount ?? previous.accounts.length);
  status.value = {
    ...status.value,
    accounts_count: deleteAll ? 0 : Math.max(0, currentCount - removed.length),
  };
  return () => {
    accounts.value = previous.accounts;
    groups.value = previous.groups;
    status.value = { ...status.value, accounts_count: previous.accountCount };
  };
}
function refreshAccountCollectionsQuietly() {
  scheduleAccountCollectionsRefresh(250);
}
function scheduleAccountsRefresh(delay = 400, includeGroups = false) {
  if (accountLoadPromise) {
    accountReloadQueued = true;
    accountReloadQueuedWithGroups = accountReloadQueuedWithGroups || includeGroups;
    return;
  }
  accountRefreshIncludeGroups = accountRefreshIncludeGroups || includeGroups;
  // Keep the first pending timer instead of repeatedly pushing it out when a
  // stream emits several progress packets in quick succession.
  if (accountRefreshTimer !== null) return;
  const requestedDelay = Math.max(0, Number(delay) || 0);
  const sinceLast = Date.now() - lastAccountRefreshStartedAt;
  const throttleDelay = Math.max(0, accountRefreshMinInterval - sinceLast);
  accountRefreshTimer = setTimeout(async () => {
    accountRefreshTimer = null;
    const withGroups = accountRefreshIncludeGroups;
    accountRefreshIncludeGroups = false;
    if (accountLoadPromise) {
      accountReloadQueued = true;
      accountReloadQueuedWithGroups = accountReloadQueuedWithGroups || withGroups;
      return;
    }
    try {
      const refreshes = [loadAccounts()];
      if (withGroups) refreshes.push(loadGroups());
      await Promise.all(refreshes);
    } catch {
      // The account view keeps its last complete snapshot and exposes the
      // load error; a later scheduled refresh can recover from a transient
      // transport failure.
    }
  }, Math.max(requestedDelay, throttleDelay));
}
function scheduleAccountCollectionsRefresh(delay = 250) {
  scheduleAccountsRefresh(delay, true);
}
function taskCardKey(item) {
  if (!item || typeof item !== "object") return "";
  return String(item.card_id || `${item.batch_id || "legacy"}:${item.index || 0}`);
}
function sortedTaskItems() {
  return [...taskIndex.values()].sort(
    (left, right) => Number(right.updated_at_epoch || right.started_at_epoch || 0)
      - Number(left.updated_at_epoch || left.started_at_epoch || 0),
  );
}
function rebuildTaskIndex(items) {
  clearTimeout(taskIndexFlushTimer);
  taskIndexFlushTimer = null;
  taskIndexDirty = false;
  taskIndex.clear();
  for (const item of Array.isArray(items) ? items : []) {
    const key = taskCardKey(item);
    if (key) taskIndex.set(key, { ...item, card_id: key });
  }
  tasks.value = sortedTaskItems();
}
function flushTaskIndex() {
  taskIndexFlushTimer = null;
  if (!taskIndexDirty) return;
  taskIndexDirty = false;
  tasks.value = sortedTaskItems();
}
function scheduleTaskIndexFlush() {
  taskIndexDirty = true;
  if (taskIndexFlushTimer !== null) return;
  taskIndexFlushTimer = setTimeout(flushTaskIndex, 160);
}
function mergeTaskHistory(items) {
  let added = false;
  for (const item of Array.isArray(items) ? items : []) {
    if (!item || typeof item !== "object") continue;
    const key = taskCardKey(item);
    if (!key || taskIndex.has(key)) continue;
    taskIndex.set(key, { ...item, card_id: key });
    added = true;
  }
  if (added) scheduleTaskIndexFlush();
}
async function loadTasks() {
  if (taskLoadPromise) return taskLoadPromise;
  const sequence = ++taskLoadSequence;
  taskLoadPromise = (async () => {
    const data = await api("/api/tasks");
    const items = Array.isArray(data?.items) ? data.items : [];
    const reportedRevision = Number(data?.job?.task_revision ?? data?.task_revision ?? -1);
    const revision = Number.isFinite(reportedRevision) ? reportedRevision : -1;
    const streamSnapshotSeen = taskStreamSnapshotGeneration === taskStreamGeneration;
    const staleAgainstStream = streamSnapshotSeen
      && taskStreamSnapshotHasItems
      && taskStreamRevision >= 0
      && (revision < 0 || revision < taskStreamRevision);
    if (sequence === taskLoadSequence) {
      if (staleAgainstStream) {
        // The API snapshot may be older than the live stream but still carry
        // persisted cards from prior batches. Add only missing keys so those
        // historical cards appear without overwriting fresher SSE state.
        mergeTaskHistory(items);
      } else {
        rebuildTaskIndex(items);
        if (revision >= 0) taskStreamRevision = Math.max(taskStreamRevision, revision);
      }
    }
    return items;
  })();
  try {
    return await taskLoadPromise;
  } finally {
    taskLoadPromise = null;
  }
}
function mergeStreamTasks(items, revision = -1) {
  const streamRevision = Number(revision);
  if (Number.isFinite(streamRevision) && streamRevision >= 0) {
    if (streamRevision < taskStreamRevision) return;
    taskStreamRevision = streamRevision;
  }
  if (!Array.isArray(items) || !items.length) return;
  taskStreamSnapshotHasItems = true;
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const key = taskCardKey(item);
    if (!key) continue;
    taskIndex.set(key, { ...(taskIndex.get(key) || {}), ...item, card_id: key });
  }
  scheduleTaskIndexFlush();
}
let mailboxLoadSequence = 0;
async function loadMailboxes() {
  const sequence = ++mailboxLoadSequence;
  const data = await api("/api/mailboxes/list?provider_type=auto&limit=2000");
  if (sequence === mailboxLoadSequence) mailboxes.value = data.items || [];
}
async function loadCapabilities() { capabilities.value = await api("/api/browsers/status"); }
function mergeLogLines(current, incoming) {
  const base = Array.isArray(current) ? current : [];
  const next = Array.isArray(incoming) ? incoming : [];
  if (!next.length) return base.slice(-300);
  if (!base.length) return next.slice(-300);
  const maxOverlap = Math.min(base.length, next.length);
  for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
    let matches = true;
    for (let index = 0; index < overlap; index += 1) {
      if (base[base.length - overlap + index] !== next[index]) {
        matches = false;
        break;
      }
    }
    if (matches) return [...base, ...next.slice(overlap)].slice(-300);
  }
  return [...base, ...next].slice(-300);
}
async function loadLogs() {
  const requestGeneration = taskStreamGeneration;
  const requestVersion = logStreamVersion;
  const data = await api("/api/logs?limit=300");
  const lines = Array.isArray(data?.lines) ? data.lines : [];
  // If SSE advanced while this request was in flight, its data is fresher;
  // wait for the next delta instead of overwriting it with an older snapshot.
  const streamAdvanced = requestGeneration !== taskStreamGeneration
    || requestVersion !== logStreamVersion;
  if (!streamAdvanced) logs.value = lines.slice(-300);
  // Keep streamSeeded tied to the SSE source. If the source is currently
  // disconnected, its next packet is a full snapshot rather than a delta.
}
async function loadOperations() {
  loadSmsConfig();
  operations.value = await api("/api/operations");
  const value = operations.value.maintenance || {};
  Object.assign(maintenanceForm, { enabled: !!value.enabled, intervalHours: Number(value.interval_hours || 12), workers: Number(value.workers || 10), autoDeleteInvalid: !!value.auto_delete_invalid, webhookUrl: "", notifyOnComplete: !!value.notify_on_complete, notifyOnPoolLow: !!value.notify_on_pool_low, poolLowThreshold: Number(value.pool_low_threshold ?? 3) });
  const maddy = operations.value.maddy_auto_register || {};
  Object.assign(maddyAutoForm, { enabled: !!maddy.enabled, intervalHours: Number(maddy.interval_hours || 1), perDomain: Number(maddy.per_domain || 20), concurrency: Number(maddy.concurrency || 3), engine: maddy.engine || "protocol", keepOnFail: !!maddy.keep_on_fail, refreshDomains: maddy.refresh_domains !== false, groupId: maddy.group_id || "" });
  const backupSchedule = operations.value.backup_schedule || {};
  Object.assign(backupScheduleForm, { enabled: !!backupSchedule.enabled, time: backupSchedule.time || "04:00", retentionDays: Number(backupSchedule.retention_days ?? 7) });
}
async function loadTrash() { trash.value = (await api("/api/accounts/trash")).items || []; }
async function loadCliproxyAccounts() {
  cliproxyLoading.value = true;
  try {
    cliproxyAccounts.value = await api("/api/cliproxy/accounts");
  } finally {
    cliproxyLoading.value = false;
  }
}
async function refreshCliproxyQuota(rows) {
  // rows 为空数组 = 刷新全部;传入行对象时按文件名单个刷新
  const files = Array.isArray(rows) && rows.length ? rows.map((row) => row.file).filter(Boolean) : [];
  cliproxyRefreshing.value = true;
  try {
    const response = await api("/api/cliproxy/refresh-quota", { method: "POST", body: jsonBody({ files }) });
    const byFile = response.items || {};
    const items = (cliproxyAccounts.value.items || []).map((item) => {
      const updated = byFile[item.file];
      return updated ? { ...item, quota: updated.quota, quota_error: updated.error || "", status: updated.status || "" } : item;
    });
    cliproxyAccounts.value = { ...cliproxyAccounts.value, items };
  } finally {
    cliproxyRefreshing.value = false;
  }
}
async function importCliproxySub2(payload) {
  const content = String(payload?.content || "");
  if (payload?.error) {
    notify(String(payload.error), "err");
    return;
  }
  if (!content.trim()) {
    notify("请选择有效的 SUB2 JSON 文件", "err");
    return;
  }
  cliproxyImporting.value = true;
  try {
    const result = await api("/api/cliproxy/import-sub2", {
      method: "POST",
      body: jsonBody({ filename: String(payload.filename || "sub2.json"), content }),
    });
    notify(`SUB2 导入完成：新增 ${result.imported || 0} 个，跳过 ${result.skipped || 0} 个`, result.imported ? "ok" : "warn");
    await loadCliproxyAccounts();
    if (result.imported) void refreshCliproxyQuota((result.files || []).map((file) => ({ file })));
  } catch (error) {
    notify(error?.message || "SUB2 JSON 导入失败", "err");
  } finally {
    cliproxyImporting.value = false;
  }
}
async function importCliproxyCpa(payload) {
  const content = String(payload?.content || "");
  if (payload?.error) {
    notify(String(payload.error), "err");
    return;
  }
  if (!content.trim()) {
    notify("请选择有效的 CPA JSON 文件", "err");
    return;
  }
  cliproxyImporting.value = true;
  try {
    const result = await api("/api/cliproxy/import-cpa", {
      method: "POST",
      body: jsonBody({ filename: String(payload.filename || "cpa.json"), content }),
    });
    notify(`CPA 导入完成：新增 ${result.imported || 0} 个，跳过 ${result.skipped || 0} 个`, result.imported ? "ok" : "warn");
    await loadCliproxyAccounts();
    if (result.imported) void refreshCliproxyQuota((result.files || []).map((file) => ({ file })));
  } catch (error) {
    notify(error?.message || "CPA JSON 导入失败", "err");
  } finally {
    cliproxyImporting.value = false;
  }
}
async function exportCliproxySub2(files) {
  const names = Array.isArray(files) ? files.filter(Boolean) : [];
  const query = names.length ? `?files=${encodeURIComponent(names.join(","))}` : "";
  try {
    const response = await fetch(apiUrl(`/api/cliproxy/export-sub2${query}`), {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json())?.detail || detail; } catch {}
      throw new Error(detail);
    }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = names.length ? "cliproxy-sub2-selected.json" : "cliproxy-sub2-all.json";
    anchor.click();
    URL.revokeObjectURL(url);
    notify(`SUB2 JSON 已导出 ${names.length ? names.length : itemsCount(cliproxyAccounts.value)} 个账号`, "ok");
  } catch (error) {
    notify(error?.message || "SUB2 JSON 导出失败", "err");
  }
}
async function exportCliproxyCpa(files) {
  const names = Array.isArray(files) ? files.filter(Boolean) : [];
  const query = names.length ? `?files=${encodeURIComponent(names.join(","))}` : "";
  try {
    const response = await fetch(apiUrl(`/api/cliproxy/export-cpa${query}`), { credentials: "include", cache: "no-store" });
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json())?.detail || detail; } catch {}
      throw new Error(detail);
    }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = names.length ? "cliproxy-cpa-selected.json" : "cliproxy-cpa-all.json";
    anchor.click();
    URL.revokeObjectURL(url);
    notify(`CPA JSON 已导出 ${names.length ? names.length : itemsCount(cliproxyAccounts.value)} 个账号`, "ok");
  } catch (error) {
    notify(error?.message || "CPA JSON 导出失败", "err");
  }
}
async function consumeCliproxyReset(payload) {
  const file = String(payload?.file || "").trim();
  if (!file) return;
  cliproxyResettingFile.value = file;
  try {
    const result = await api("/api/cliproxy/reset-credits/consume", {
      method: "POST",
      body: jsonBody({ file }),
    });
    if (result.quota) {
      const items = (cliproxyAccounts.value.items || []).map((item) => (
        item.file === file
          ? { ...item, quota: result.quota, quota_error: result.quota_error || "", status: result.quota.status || item.status }
          : item
      ));
      cliproxyAccounts.value = { ...cliproxyAccounts.value, items };
    } else {
      await refreshCliproxyQuota([{ file }]);
    }
    notify("重置卡已消耗，额度窗口正在刷新", "ok");
  } catch (error) {
    notify(error?.message || "重置卡消耗失败", "err");
  } finally {
    cliproxyResettingFile.value = "";
  }
}
async function deleteCliproxyAccounts(files) {
  const list = (files || []).filter(Boolean);
  if (!list.length) return;
  const byFile = new Map((cliproxyAccounts.value.items || []).map((item) => [item.file, item]));
  const label = list.length === 1 ? (byFile.get(list[0])?.email || list[0]) : `选中的 ${list.length} 个账号`;
  if (!await confirmInside(`删除中转账号 ${label}？凭据会移出中转目录，不影响账号库。`, { title: "删除中转账号", confirmText: "删除", danger: true })) return;
  cliproxyDeleting.value = true;
  try {
    const result = await api("/api/cliproxy/delete", { method: "POST", body: jsonBody({ files: list }) });
    notify(`已删除 ${result.deleted?.length || 0} 个中转账号`, "ok");
    await loadCliproxyAccounts();
  } catch (error) {
    notify(error?.message || "中转账号删除失败", "err");
  } finally {
    cliproxyDeleting.value = false;
  }
}
const cliproxyReloginLoading = ref(false);
async function reloginCliproxyAccount(payload) {
  const email = String(payload?.email || "").trim().toLowerCase();
  if (!email) return;
  // 不在前端预检账号列表(可能未加载/分页不全),由后端直接读账号库校验:
  // 不存在返回 404,缺密码/2FA 也会给出明确错误
  if (!await confirmInside(`对 ${email} 重新 OAuth 登录?将用账号库凭据重新走完整登录流程,成功后新 RT 会写回账号库和中转。`, { title: "重新登录", confirmText: "开始登录" })) return;
  if (await resumeCodexOauthIfRunning()) return;
  cliproxyReloginLoading.value = true;
  try {
    const result = await run(() => api("/api/codex-oauth/run", { method: "POST", body: jsonBody({ emails: [email], force: true }) }), "重新登录任务已启动");
    if (!result) return;
    codexOauthDialog.open = true;
    stopCodexOauthPolling();
    try { codexOauthStatus.value = await api("/api/codex-oauth/status"); } catch {}
    codexOauthTimer = setInterval(async () => { try { codexOauthStatus.value = await api("/api/codex-oauth/status"); if (!codexOauthStatus.value.running) stopCodexOauthPolling(); } catch { stopCodexOauthPolling(); } }, 2000);
  } finally {
    cliproxyReloginLoading.value = false;
  }
}
function itemsCount(value) {
  return Array.isArray(value?.items) ? value.items.length : 0;
}
async function loadProgress() {
  const next = await api("/api/progress");
  progress.value = next;
  const health = (next.items || []).find((item) => item.key === "account_maintenance");
  if (!health) {
    lastMaintenanceHealthDone = -1;
    return;
  }
  const done = Number(health.done || 0);
  if (done < lastMaintenanceHealthDone) lastMaintenanceHealthDone = done;
  if (done > lastMaintenanceHealthDone) {
    lastMaintenanceHealthDone = done;
    if (isAccountView()) scheduleAccountsRefresh(500);
  }
}
async function loadActiveView() {
  if (active.value === "task") return loadTasks();
  if (active.value === "fingerprint") return loadCapabilities();
  if (active.value === "mail") return loadMailboxes();
  if (active.value === "plus-eligibility") {
    if (!tokenEligibilitySynced) {
      if (!uiSettingsLoaded) await loadUiSettings();
      tokenEligibilityForm.channel = uiSettings.value.trial_channel === "nerver" ? "nerver" : "v4";
      tokenEligibilityForm.workers = Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.trial_workers) || 20)));
      tokenEligibilitySynced = true;
    }
    return loadTrialProxyPool();
  }
  if (active.value === "accounts") return Promise.all([loadAccounts(), resumeHealthProgress()]);
  if (active.value === "credential-export" || active.value === "protocol-login") return loadAccounts();
  if (active.value === "rebind") return resumeRebindProgress();
  if (active.value === "chatgpt2api") return Promise.all([loadGroups(), loadAccounts()]);
  if (active.value === "logs") return loadLogs();
  if (active.value === "cliproxy") {
    await loadCliproxyAccounts();
    // 只补查从未检测过的账号;已有状态(含 401/429)等手动点刷新
    const missing = (cliproxyAccounts.value.items || []).filter((item) => !item.status);
    if (missing.length) void refreshCliproxyQuota(missing);
    return;
  }
  if (active.value === "operations") return Promise.all([loadOperations(), loadTrash(), loadSmsStats()]);
}

async function initialize() {
  try {
    restoreReauthUiState();
    restoreRebindUiState();
    const auth = await api("/api/auth/status");
    if (!auth.authenticated) {
      // The login page renders LoginView directly; no polling runs there.
      if (onLoginPage) return;
      return location.replace("/login");
    }
    if (onLoginPage) return location.replace("/");
    username.value = auth.username || "";
    // Account pages can start their first page alongside the heavier status
    // summary. This lets the table become useful before mailbox statistics
    // finish their cold scan; the in-flight group request is shared above.
    const parallelAccountView = isAccountView();
    const activeLoad = parallelAccountView ? loadActiveView() : null;
    await Promise.all([
      loadStatus(),
      loadConfig(),
      loadGroups(),
      loadProgress(),
      ...(activeLoad ? [activeLoad] : []),
    ]);
    if (!activeLoad) await loadActiveView();
    fillForms();
    await Promise.all([resumeHealthProgress(), resumeReauthProgress(), resumeTrialProgress(), resumeRefreshAtProgress(), resumeCodexProgress()]);
    void promptInterruptedTasks();
    connectStream();
  } catch (error) {
    notify(error.message, "err");
  } finally {
    loading.value = false;
  }
}

async function run(action, successText, refresh = []) {
  if (busy.value) return;
  busy.value = true;
  try {
    const result = await action();
    await Promise.all(refresh.map((fn) => fn()));
    notify(typeof successText === "function" ? successText(result) : successText, result?.ok === false ? "err" : "ok");
    return result;
  } catch (error) {
    notify(error.message || "操作失败", "err");
  } finally {
    busy.value = false;
  }
}

function connectStream() {
  clearTimeout(streamReconnectTimer);
  stream?.close();
  connected.value = false;
  streamSeeded = false;
  logStreamVersion = 0;
  taskStreamSupported = false;
  // A reconnect may land on a freshly restarted backend whose revision
  // counter began at zero; do not compare it with the previous connection.
  taskStreamGeneration += 1;
  taskStreamRevision = -1;
  taskStreamSnapshotGeneration = 0;
  taskStreamSnapshotHasItems = false;
  const events = active.value === "task"
    ? "status,tasks"
    : active.value === "logs"
      ? "status,log"
      : "status";
  const source = new EventSource(apiUrl(`/api/logs/stream?events=${encodeURIComponent(events)}&t=${Date.now()}`), { withCredentials: true });
  stream = source;
  source.onopen = () => {
    if (stream !== source) return;
    connected.value = true;
  };
  source.addEventListener("log", (event) => {
    if (stream !== source || active.value !== "logs") return;
    const lines = JSON.parse(event.data || "{}").lines || [];
    logStreamVersion += 1;
    logs.value = streamSeeded ? mergeLogLines(logs.value, lines) : lines.slice(-300);
    streamSeeded = true;
  });
  source.addEventListener("status", (event) => {
    if (stream !== source) return;
    const nextJob = JSON.parse(event.data || "{}");
    const batchId = String(nextJob.batch_id || "");
    if (batchId !== lastRegistrationBatchId) {
      lastRegistrationBatchId = batchId;
      lastRegistrationSuccessCount = 0;
    }
    const successCount = Number(nextJob.success_count || 0);
    if (successCount > lastRegistrationSuccessCount) {
      lastRegistrationSuccessCount = successCount;
      if (isAccountView()) scheduleAccountCollectionsRefresh(250);
    }
    status.value = { ...status.value, job: nextJob };
    const now = Date.now();
    if (active.value === "task" && status.value.job?.running && !taskStreamSupported && now - lastLegacyTaskRefreshAt >= 2000) {
      lastLegacyTaskRefreshAt = now;
      loadTasks().catch(() => {});
    }
  });
  source.addEventListener("tasks", (event) => {
    if (stream !== source) return;
    taskStreamSupported = true;
    taskStreamSnapshotGeneration = taskStreamGeneration;
    if (active.value !== "task") return;
    const payload = JSON.parse(event.data || "{}");
    const items = Array.isArray(payload.items) ? payload.items : [];
    mergeStreamTasks(items, payload.revision);
  });
  source.onerror = () => {
    if (stream !== source) return;
    connected.value = false;
    source.close();
    stream = null;
    if (active.value === "logs") loadLogs().catch(() => {});
    clearTimeout(streamReconnectTimer);
    streamReconnectTimer = setTimeout(connectStream, 1500);
  };
}

function scheduleProgressPoll() {
  clearTimeout(progressTimer);
  const activeWork = Number(progress.value.running || 0) > 0;
  progressTimer = setTimeout(async () => {
    if (!document.hidden) await loadProgress().catch(() => {});
    scheduleProgressPoll();
  }, activeWork ? 600 : 15000);
}

function handleVisibilityChange() {
  if (document.hidden) return;
  const refreshes = [loadProgress()];
  // The SSE stream carries live status while task/log views are open. Avoid a
  // second summary request on every tab activation in that case.
  if (!connected.value) refreshes.push(loadStatus());
  Promise.all(refreshes).catch(() => {});
  scheduleProgressPoll();
}

async function startJob() {
  await run(() => api("/api/job/start", { method: "POST", body: jsonBody({ total: taskForm.total, concurrency: taskForm.concurrency, keep_on_fail: taskForm.keepOnFail, email: taskForm.email || null, mail_type: taskForm.mailType, engine: taskForm.engine, group_id: taskForm.groupId, auto_group: taskForm.autoGroup, auto_group_size: taskForm.autoGroupSize }) }), "任务已启动", [loadStatus, loadTasks, loadGroups]);
}
async function stopJob() { await run(() => api("/api/job/stop", { method: "POST", body: "{}" }), "正在停止任务", [loadStatus]); }
async function pauseJob() { await run(() => api("/api/job/pause", { method: "POST", body: "{}" }), "已暂停后续任务派发", [loadStatus, loadTasks]); }
async function resumeJob() { await run(() => api("/api/job/resume", { method: "POST", body: "{}" }), "任务派发已继续", [loadStatus, loadTasks]); }
async function retryFailedJobs() { await run(() => api("/api/job/retry-failed", { method: "POST", body: "{}" }), "失败任务已重新启动", [loadStatus, loadTasks]); }
async function resetMail() { await run(() => api("/api/mail/reset-failed", { method: "POST", body: "{}" }), (r) => `已释放 ${r.reset || 0} 个邮箱`, [loadStatus, loadMailboxes]); }
async function deleteTasks(indexes) {
  if (!await confirmInside(indexes ? `删除选中的 ${indexes.length} 个任务？` : "清空全部任务名片？", { title: "删除任务记录", confirmText: "删除", danger: true })) return;
  await run(() => api("/api/tasks/delete", { method: "POST", body: jsonBody({ indexes: indexes || [], delete_all: !indexes }) }), "任务名片已更新", [loadTasks]);
}

async function saveBrowser() {
  config.value.registration = { ...(config.value.registration || {}), timeout: Number(config.value.registration?.timeout || 480), max_transient_retries: Number(config.value.registration?.max_transient_retries || 2), engine: taskForm.engine, total: taskForm.total, threads: taskForm.concurrency };
  await run(() => api("/api/config", { method: "PUT", body: jsonBody({ config: config.value }) }), "浏览器配置已保存", [loadConfig, loadCapabilities]);
}
async function saveOutlook() {
  const result = await run(
    () => api("/api/mailboxes", { method: "PUT", body: jsonBody({ provider_type: "outlook_token", mailboxes: mailForms.outlookText, mode: mailForms.outlookMode, append: true }) }),
    (r) => `Outlook 导入完成：新增 ${r.added || 0}，更新 ${r.updated || 0}，已存在 ${r.unchanged || 0}，合计 ${r.total || 0}`,
  );
  if (result?.ok) {
    mailForms.outlookText = "";
    status.value = { ...status.value, mail: result.mail || status.value.mail };
    loadMailboxes().catch(() => {});
  }
}
async function saveIcloud() {
  const result = await run(
    () => api("/api/mailboxes", { method: "PUT", body: jsonBody({ provider_type: "icloud_api", mailboxes: mailForms.icloudText, api_base: mailForms.icloudApiBase, enable: mailForms.icloudEnable, append: true }) }),
    (r) => `iCloud 导入完成：新增 ${r.added || 0}，更新 ${r.updated || 0}，已存在 ${r.unchanged || 0}，合计 ${r.total || 0}`,
  );
  if (result?.ok) {
    mailForms.icloudText = "";
    status.value = { ...status.value, mail: result.mail || status.value.mail };
    loadMailboxes().catch(() => {});
  }
}
async function loadMaddyDomains() {
  const result = await run(
    () => api("/api/maddy-webui/domains", {
      method: "POST",
      body: jsonBody({
        api_base: mailForms.maddyApiBase,
        ...maddyApiKeyPayload(),
      }),
    }),
    (r) => `已读取 ${r.items?.length || 0} 个 Maddy 域名`,
  );
  if (result?.ok) {
    const items = result.items || [];
    mailForms.maddyDomainsText = items.join("\n");
    mailForms.maddyDefaultDomain = result.default_domain || items[0] || mailForms.maddyDefaultDomain;
  }
}
async function saveMaddyMail() {
  const domains = parseDomainList(mailForms.maddyDomainsText);
  const maddyKeyPayload = maddyApiKeyPayload();
  const result = await run(
    () => api("/api/mailboxes", {
      method: "PUT",
      body: jsonBody({
        provider_type: "maddy_webui",
        mailboxes: "",
        api_base: mailForms.maddyApiBase,
        ...maddyKeyPayload,
        enable: mailForms.maddyEnable,
        default_domain: mailForms.maddyDefaultDomain,
        domains,
        local_length: mailForms.maddyLocalLength,
        capacity: mailForms.maddyCapacity,
        mailbox_password: mailForms.maddyMailboxPassword,
        append: true,
      }),
    }),
    (r) => `Maddy 配置已保存：动态容量 ${r.total || mailForms.maddyCapacity}`,
  );
  if (result?.ok) {
    if (Object.keys(maddyKeyPayload).length) {
      mailForms.maddyApiKeyConfigured = true;
      mailForms.maddyApiKeyMasked = "已配置";
    }
    mailForms.maddyApiToken = "";
    mailForms.maddyMailboxPassword = "";
    status.value = { ...status.value, mail: result.mail || status.value.mail };
    loadConfig().catch(() => {});
    loadMailboxes().catch(() => {});
  }
}
async function loadFastmailAccounts() {
  const result = await run(
    () => api("/api/fastmail/accounts", {
      method: "POST",
      body: jsonBody({
        api_base: mailForms.fastmailApiBase,
        admin_key: mailForms.fastmailAdminKey,
      }),
    }),
    (r) => `Fastmail 池：${r.available ?? 0} 可用 / ${r.total ?? 0} 别名（${r.items?.length || 0} 个主账号）`,
  );
  if (result?.ok) {
    mailForms.fastmailPoolRows = result.items || [];
  }
}
async function saveFastmailMail() {
  const result = await run(
    () => api("/api/mailboxes", {
      method: "PUT",
      body: jsonBody({
        provider_type: "fastmail_api",
        mailboxes: "",
        api_base: mailForms.fastmailApiBase,
        admin_key: mailForms.fastmailAdminKey,
        accounts: mailForms.fastmailAccountsText,
        capacity: mailForms.fastmailCapacity,
        enable: mailForms.fastmailEnable,
        append: true,
      }),
    }),
    (r) => `Fastmail 配置已保存：容量回退 ${r.total || mailForms.fastmailCapacity}`,
  );
  if (result?.ok) {
    mailForms.fastmailAdminKey = "";
    status.value = { ...status.value, mail: result.mail || status.value.mail };
    loadConfig().catch(() => {});
    loadMailboxes().catch(() => {});
  }
}
async function deleteMailboxes(items) {
  if (!items?.length || !await confirmInside(`删除选中的 ${items.length} 个邮箱？`, { title: "删除邮箱", confirmText: "删除", danger: true })) return;
  const result = await run(() => api("/api/mailboxes/delete", { method: "POST", body: jsonBody({ items: items.map(({ email, provider_type }) => ({ email, provider_type })), delete_all: false, provider_type: "auto" }) }), (value) => `已删除 ${value.removed || 0} 个邮箱`);
  if (result?.ok) {
    mailboxLoadSequence += 1;
    status.value = { ...status.value, mail: result.mail || status.value.mail };
    const removed = new Set(items.map((item) => `${item.provider_type}:${String(item.email || "").toLowerCase()}`));
    mailboxes.value = mailboxes.value.filter((item) => !removed.has(`${item.provider_type}:${String(item.email || "").toLowerCase()}`));
  }
}
async function updateMailboxState(emails, action, states = []) {
  const result = await run(() => api("/api/mailboxes/state", { method: "POST", body: jsonBody({ emails, action, states }) }), (r) => `已更新 ${r.changed || 0} 个邮箱状态`);
  if (result?.ok) {
    mailboxLoadSequence += 1;
    status.value = { ...status.value, mail: result.mail || status.value.mail };
    const selected = new Set((emails || []).map((email) => String(email || "").toLowerCase()));
    mailboxes.value = mailboxes.value.map((item) => {
      const currentState = String(item.state || "available").toLowerCase();
      const matchesEmail = !selected.size || selected.has(String(item.email || "").toLowerCase());
      const matchesState = !states.length || states.includes(currentState);
      if (!matchesEmail || !matchesState) return item;
      return {
        ...item,
        state: action === "release" ? "available" : action,
        reason: action === "release" ? "" : item.reason,
      };
    });
  }
}
async function saveProxy() {
  const result = await run(() => api("/api/proxy", { method: "PUT", body: jsonBody({ enabled: proxyForm.enabled, mode: proxyForm.mode, url: proxyForm.url, resource_url: proxyForm.resourceUrl, pool_file: proxyForm.poolFile, pool_text: proxyForm.poolTextTouched ? proxyForm.poolText : null, selected_country: proxyForm.selectedCountry, country_pools: proxyForm.countries, rotate_per_task: proxyForm.rotate, clearance: { enabled: proxyForm.clearanceMode !== "none", mode: proxyForm.clearanceMode, flaresolverr_url: proxyForm.flareUrl, proxy_url: proxyForm.flareProxy, timeout_sec: 60, refresh_interval: 3600 } }) }), "代理设置已保存", [loadStatus, loadConfig]);
  if (result?.proxy) {
    const pools = result.proxy.country_pools || {};
    status.value = { ...status.value, proxy: result.proxy };
    proxyForm.countries = (pools.items || []).map((item) => ({ ...item }));
    proxyForm.selectedCountry = result.proxy.selected_country || "";
    proxyForm.countryPoolPath = pools.path || proxyForm.countryPoolPath;
  }
}
async function saveTrialProxyPool() {
  await run(() => api("/api/trial-proxy-pool", { method: "PUT", body: jsonBody({ selected_country: trialProxyForm.selected_country, countries: trialProxyForm.countries }) }), "资格代理已保存", [loadTrialProxyPool]);
}
async function checkTokenEligibility() {
  const accessToken = String(tokenEligibilityForm.accessToken || "").trim();
  if (!accessToken || tokenEligibility.loading) return;
  Object.assign(tokenEligibility, { loading: true, error: "", result: null });
  try {
    const result = await api("/api/trial-eligibility/check-token", {
      method: "POST",
      body: jsonBody({ access_token: accessToken, use_configured_proxy: !!tokenEligibilityForm.useConfiguredProxy, channel: tokenEligibilityForm.channel === "nerver" ? "nerver" : "v4" }),
    });
    tokenEligibility.result = result;
    notify(result.eligible ? "检测完成：可领 Plus 免费试用" : result.label || "检测完成：无免费试用", result.is_deactivated || result.status === "at_expired" ? "err" : "ok");
  } catch (error) {
    tokenEligibility.error = error?.message || "Plus 资格检测失败";
    notify(tokenEligibility.error, "err");
  } finally {
    tokenEligibility.loading = false;
  }
}
function clearTokenEligibility() {
  tokenEligibilityForm.accessToken = "";
  tokenEligibilityForm.batchTokens = "";
  Object.assign(tokenEligibility, { loading: false, error: "", result: null });
  Object.assign(tokenEligibility.batch, { running: false, total: 0, done: 0, results: [], stopped: false });
}
function parseBatchTokens() {
  const seen = new Set();
  const tokens = [];
  for (const raw of String(tokenEligibilityForm.batchTokens || "").split(/[\s,;]+/)) {
    let token = raw.trim();
    if (!token) continue;
    if (token.toLowerCase().startsWith("bearer ")) token = token.slice(7).trim();
    if (token.length < 20 || token.length > 20000 || seen.has(token)) continue;
    seen.add(token);
    tokens.push(token);
  }
  return tokens;
}
async function startBatchTokenEligibility() {
  const batch = tokenEligibility.batch;
  if (batch.running) return;
  const tokens = parseBatchTokens();
  if (!tokens.length) {
    notify("请先粘贴至少一个有效的 Access Token", "err");
    return;
  }
  if (tokens.length > 200) {
    notify(`一次最多批量检测 200 个，当前 ${tokens.length} 个`, "err");
    return;
  }
  Object.assign(batch, { running: true, total: tokens.length, done: 0, results: [], stopped: false });
  const workers = Math.max(1, Math.min(20, Math.floor(Number(tokenEligibilityForm.workers) || 20)));
  const chunkSize = workers;
  try {
    for (let offset = 0; offset < tokens.length; offset += chunkSize) {
      if (batch.stopped) break;
      const chunk = tokens.slice(offset, offset + chunkSize);
      try {
        const data = await api("/api/trial-eligibility/check-tokens", {
          method: "POST",
          body: jsonBody({ access_tokens: chunk, use_configured_proxy: !!tokenEligibilityForm.useConfiguredProxy, channel: tokenEligibilityForm.channel === "nerver" ? "nerver" : "v4", workers }),
        });
        for (const row of data?.results || []) {
          batch.results.push({
            tail: row.token_tail || "-",
            ok: !!row.ok,
            eligible: !!row.eligible,
            plusActive: !!row.has_active_subscription,
            banned: !!row.is_deactivated,
            status: row.status || "",
            plan: row.plan_type || "free",
            promotions: row.promotion_count ?? 0,
            elapsed: row.elapsed_seconds ?? null,
            reason: row.reason || "",
            error: row.error || "",
          });
        }
      } catch (error) {
        for (const token of chunk) {
          batch.results.push({ tail: token.slice(-10), ok: false, eligible: false, plusActive: false, banned: false, status: "", plan: "", promotions: 0, elapsed: null, reason: "", error: error?.message || "检测失败" });
        }
      }
      batch.done = Math.min(offset + chunkSize, tokens.length);
    }
    const eligibleCount = batch.results.filter((item) => item.ok && item.eligible).length;
    const failedCount = batch.results.filter((item) => !item.ok).length;
    notify(
      batch.stopped
        ? `批量检测已停止：完成 ${batch.done}/${batch.total}`
        : `批量检测完成：${eligibleCount} 个符合试用资格${failedCount ? `，${failedCount} 个失败` : ""}`,
      failedCount && !batch.stopped ? "err" : "ok",
    );
  } finally {
    batch.running = false;
  }
}
function stopBatchTokenEligibility() {
  tokenEligibility.batch.stopped = true;
}
async function testProxies() { const result = await run(() => api("/api/proxy/test", { method: "POST", body: jsonBody({ proxies: [] }) }), (r) => `代理检测完成：可用 ${r.alive || 0}/${r.count || 0}`); if (result) proxyDiagnostics.value = result; }
async function saveIntegration() { await run(() => api("/api/chatgpt2api", { method: "PUT", body: jsonBody({ enabled: integrationForm.enabled, url: integrationForm.url, key: integrationForm.key, auto_upload: integrationForm.autoUpload }) }), "chatgpt2api 配置已保存", [loadConfig]); }
async function testIntegration() { await run(() => api("/api/chatgpt2api/test", { method: "POST", body: jsonBody({ url: integrationForm.url, key: integrationForm.key }) }), (r) => r.message || (r.ok ? "连接成功" : "连接失败")); }
async function uploadIntegration() {
  const group = groups.value.find((item) => item.id === integrationForm.groupId);
  const scope = group ? `分组“${group.name}”的 ${group.account_count || 0} 个账号` : "全部分组的有效账号";
  if (!await confirmInside(`上传${scope}到 chatgpt2api？`, { title: "上传账号", confirmText: "开始上传" })) return;
  await run(() => api("/api/chatgpt2api/upload-all", { method: "POST", body: jsonBody({ url: integrationForm.url, key: integrationForm.key, group_id: integrationForm.groupId || null }) }), (r) => r.message || `已处理 ${r.count || 0} 个账号`);
}

function applyHealthProgress(value) {
  const requested = Number(value?.requested ?? value?.total ?? 0);
  const actualTotal = Number(value?.actual_total ?? value?.total ?? 0);
  if (!requested && !actualTotal) return false;
  const updates = Array.isArray(value.updates) ? value.updates : Object.values(value.updates || {});
  if (updates.length) {
    const byKey = new Map(accounts.value.map((item) => [item.delete_key, item]));
    let changed = false;
    for (const update of updates) {
      const current = byKey.get(update?.delete_key);
      if (current && update) {
        Object.assign(current, update);
        changed = true;
      }
    }
    // `accounts` is shallow by design; publish one new array after applying a
    // progress batch so the table updates without deep proxy tracking.
    if (changed) accounts.value = [...accounts.value];
  }
  Object.assign(healthProgress, {
    running: !!value.running,
    requested: Math.max(0, requested),
    actualTotal: Math.max(0, actualTotal),
    total: Math.max(0, actualTotal),
    done: Math.min(Math.max(0, actualTotal), Math.max(0, Number(value.done || 0))),
    checked: Number(value.checked ?? value.done ?? 0),
    usable: Number(value.usable || 0),
    unavailable: Number(value.unavailable || 0),
    skippedKnownDeactivated: Number(value.skipped_known_deactivated || 0),
    deactivated: Number(value.deactivated || value.skipped_known_deactivated || 0),
    invalidOther: Number(value.invalid_other || 0),
    current: value.running ? (value.current || `${value.workers || 10} 路并发检测`) : "",
    alive: Number(value.alive || 0),
    limited: Number(value.limited || 0),
    invalid: Number(value.invalid || 0),
    reauthRequired: Number(value.reauth_required || 0),
    error: Number(value.error || 0),
    requestedWorkers: Number(value.requested_workers || value.workers || 0),
    workers: Number(value.workers || value.requested_workers || 0),
    throttled: !!value.throttled,
    resourceMode: value.resource_mode || "normal",
    updates,
    interrupted: !!value.interrupted,
    pendingCount: Math.max(0, Number(value.pending_count || 0)),
    message: value.message || "",
  });
  return true;
}

function startHealthProgressPolling() {
  if (healthProgressTimer) return;
  const delay = healthProgress.actualTotal > healthInlineUpdateLimit ? 1000 : 500;
  healthProgressTimer = setInterval(() => pollHealthProgress(), delay);
}

function stopHealthProgressPolling() {
  clearInterval(healthProgressTimer);
  healthProgressTimer = null;
}

async function pollHealthProgress(force = false) {
  if (healthProgressLoading || (!force && !healthProgress.running)) return null;
  healthProgressLoading = true;
  try {
    const includeUpdates = healthProgress.actualTotal <= healthInlineUpdateLimit;
    const value = await api(`/api/accounts/health/progress?include_updates=${includeUpdates ? "true" : "false"}`);
    if (value?.running) healthServerRunObserved = true;
    if (healthRequestActive && !force && !value?.running && !healthServerRunObserved) {
      // The POST may still be loading the account snapshot before the server
      // marks the job running. Keep the local progress state during that gap.
      return value;
    }
    const wasRunning = healthProgress.running;
    if (!applyHealthProgress(value)) return value;
    if (healthProgress.running) startHealthProgressPolling();
    else stopHealthProgressPolling();
    if (wasRunning && !healthProgress.running && !healthRequestActive) scheduleAccountsRefresh(100);
    return value;
  } catch {
    // The main health request owns error reporting; a missed poll is harmless.
    return null;
  } finally {
    healthProgressLoading = false;
  }
}

async function resumeHealthProgress() {
  const value = await pollHealthProgress(true);
  if (value?.running) startHealthProgressPolling();
}

async function resumeHealthCheck() {
  if (healthProgress.running) { notify("账号批量测活正在运行，请等待当前任务完成", "err"); return; }
  if (!healthProgress.pendingCount) { notify("没有可续跑的测活账号", "err"); return; }
  const workers = Math.max(1, Math.min(20, Math.floor(Number(healthProgress.requestedWorkers) || Number(uiSettings.value.health_workers) || 20)));
  Object.assign(healthProgress, { running: true, done: 0, checked: 0, usable: 0, unavailable: 0, invalidOther: 0, current: `${Math.min(workers, Math.max(1, Number(healthProgress.pendingCount) || 1))} 路协议请求并发`, alive: 0, limited: 0, invalid: 0, reauthRequired: 0, error: 0, updates: [], interrupted: false, pendingCount: 0, message: "", requestedWorkers: workers, workers });
  startHealthProgressPolling();
  healthRequestActive = true;
  healthServerRunObserved = false;
  try {
    const result = await api("/api/accounts/health/resume", { method: "POST", body: jsonBody({ workers }) });
    const value = await pollHealthProgress(true);
    if (!value) applyHealthProgress({ ...result, running: false });
    scheduleAccountsRefresh(100);
    notify(`测活续跑完成：实际检测 ${healthProgress.checked} 条；可用 ${healthProgress.usable}，待续期 ${healthProgress.reauthRequired}，其它失效 ${healthProgress.invalidOther}，错误 ${healthProgress.error}`);
  } catch (error) {
    const value = await pollHealthProgress(true);
    if (value?.running) {
      notify("测活请求连接已结束，后台仍在检测，页面将继续同步进度");
    } else {
      scheduleAccountsRefresh(100);
      notify(error?.message || "测活续跑失败", "err");
    }
  } finally {
    healthRequestActive = false;
    healthServerRunObserved = false;
    if (healthProgress.running) startHealthProgressPolling();
  }
}

async function resumeTrialCheck() {
  if (trialProgress.running) { notify("已有优惠检测批次正在运行", "err"); return; }
  if (!trialProgress.pendingCount) { notify("没有可续跑的检测账号", "err"); return; }
  const savedTrialWorkers = Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.trial_workers) || 20)));
  const savedTrialChannel = uiSettings.value.trial_channel === "nerver" ? "nerver" : "v4";
  try {
    const job = await api("/api/accounts/trial-eligibility/resume", { method: "POST", body: jsonBody({ concurrency: savedTrialWorkers, channel: savedTrialChannel }) });
    applyTrialProgress(job);
    pollTrialProgress();
    notify(`0元资格检测续跑已启动：剩余 ${job.total || 0} 个账号`, "ok");
  } catch (error) { notify(error?.message || "资格检测续跑失败", "err"); }
}

async function resumeCodexProgress() {
  try { codexOauthStatus.value = await api("/api/codex-oauth/status"); } catch {}
}

async function promptInterruptedTasks() {
  // 服务重启恢复检查点后,提示续跑被打断的任务
  try {
    if (healthProgress.pendingCount > 0 && !healthProgress.running) {
      const ok = await openDialog({
        title: "测活任务中断",
        message: `${healthProgress.message || "测活任务未完成"}。是否继续检测剩余 ${healthProgress.pendingCount} 个账号？`,
        confirmText: "继续检测",
      });
      if (ok) { void resumeHealthCheck(); return; }
    }
    if (trialProgress.pendingCount > 0 && !trialProgress.running) {
      const ok = await openDialog({
        title: "0元资格检测中断",
        message: `${trialProgress.message || "0元资格检测未完成"}。是否继续检测剩余 ${trialProgress.pendingCount} 个账号？`,
        confirmText: "继续检测",
      });
      if (ok) { void resumeTrialCheck(); return; }
    }
    // 接码不提供续跑：一个账号只能绑定一次手机号，中断后剩余账号需手动重新发起。
  } catch {}
}

function applyReauthProgress(value) {
  if (!value?.job_id) return;
  reauthJobId = value.job_id;
  const running = ["queued", "running", "stopping"].includes(value.status);
  Object.assign(reauthProgress, {
    running,
    stopping: !!value.stopping || value.status === "stopping",
    mode: value.mode || "protocol",
    operation: value.operation || "reauth",
    concurrency: Math.max(1, Number(value.concurrency || 3)),
    total: Number(value.total || 0),
    done: Number(value.done || 0),
    current: value.current || "",
    success: Number(value.success || 0),
    failed: Number(value.failed || 0),
    cancelled: Number(value.cancelled || 0),
    last: value.last || "",
    lastStatus: value.last_status || "",
    lastMessage: value.last_message || value.message || "",
    items: Array.isArray(value.items) ? value.items : [],
  });
  persistReauthUiState();
}

function persistReauthUiState() {
  try {
    localStorage.setItem(REAUTH_UI_STATE_KEY, JSON.stringify({
      jobId: reauthJobId,
      progress: { ...reauthProgress },
      preflight: { ...protocolLoginPreflight },
      savedAt: Date.now(),
    }));
  } catch {
    // Server-side progress remains authoritative if local storage is unavailable.
  }
}

function restoreReauthUiState() {
  try {
    const saved = JSON.parse(localStorage.getItem(REAUTH_UI_STATE_KEY) || "null");
    if (!saved || typeof saved !== "object") return;
    if (saved.progress && typeof saved.progress === "object") {
      Object.assign(reauthProgress, {
        ...saved.progress,
        items: Array.isArray(saved.progress.items) ? saved.progress.items : [],
      });
      reauthJobId = String(saved.jobId || "");
    }
    if (saved.preflight && typeof saved.preflight === "object") {
      Object.assign(protocolLoginPreflight, {
        ...saved.preflight,
        invalidFormat: Array.isArray(saved.preflight.invalidFormat) ? saved.preflight.invalidFormat : [],
        notFound: Array.isArray(saved.preflight.notFound) ? saved.preflight.notFound : [],
        missingCredentials: Array.isArray(saved.preflight.missingCredentials) ? saved.preflight.missingCredentials : [],
      });
    }
  } catch {
    // Ignore malformed or unavailable local state.
  }
}

function resetProtocolLoginResultsForInput(emails = []) {
  if (reauthProgress.running) return;
  const requested = [...new Set((Array.isArray(emails) ? emails : [])
    .map((email) => String(email || "").trim().toLowerCase())
    .filter(Boolean))].sort();
  const previous = [...new Set((reauthProgress.items || [])
    .map((item) => String(item?.email || "").trim().toLowerCase())
    .filter(Boolean))].sort();
  if (requested.length === previous.length && requested.every((email, index) => email === previous[index])) return;
  reauthJobId = "";
  reauthCompletionAnnounced = "";
  Object.assign(reauthProgress, {
    running: false,
    stopping: false,
    mode: "protocol",
    operation: "reauth",
    total: 0,
    done: 0,
    current: "",
    success: 0,
    failed: 0,
    cancelled: 0,
    last: "",
    lastStatus: "",
    lastMessage: "",
    items: [],
  });
  Object.assign(protocolLoginPreflight, {
    requested: requested.length,
    matched: 0,
    submitted: 0,
    invalidFormat: [],
    notFound: [],
    missingCredentials: [],
  });
  persistReauthUiState();
}

function scheduleReauthProgressPoll(delay = 700, announce = true) {
  clearTimeout(reauthProgressTimer);
  reauthProgressTimer = setTimeout(() => pollReauthProgress(announce), delay);
}

async function pollReauthProgress(announce = true) {
  if (!reauthJobId || reauthProgressLoading) return;
  reauthProgressLoading = true;
  try {
    const value = await api(`/api/accounts/reauth/${encodeURIComponent(reauthJobId)}`);
    applyReauthProgress(value);
    if (reauthProgress.running) {
      scheduleReauthProgressPoll(700, announce);
      return;
    }
    clearTimeout(reauthProgressTimer);
    reauthProgressTimer = null;
    await Promise.all([loadAccounts(), loadStatus(), loadOperations()]);
    if (announce && reauthCompletionAnnounced !== value.job_id) {
      reauthCompletionAnnounced = value.job_id;
      const topLevelFailed = value.status === "failed";
      notify(
        value.message || `重新登录完成：成功 ${reauthProgress.success}，失败 ${reauthProgress.failed}`,
        topLevelFailed || reauthProgress.failed ? "err" : "ok",
      );
    }
  } catch (error) {
    // 后台批次仍可能正常运行；短暂断网时继续轮询，不把账号记为失败。
    if (reauthProgress.running) scheduleReauthProgressPoll(1500, announce);
    else notify(error?.message || "重新登录进度读取失败", "err");
  } finally {
    reauthProgressLoading = false;
  }
}

async function resumeReauthProgress() {
  try {
    const value = (await api("/api/accounts/reauth/progress")).job;
    if (!value?.job_id) {
      if (reauthProgress.running) {
        reauthProgress.running = false;
        reauthProgress.stopping = false;
        reauthProgress.current = "";
        reauthProgress.lastMessage = reauthProgress.lastMessage || "服务端任务已结束或记录已清理";
        persistReauthUiState();
      }
      return;
    }
    applyReauthProgress(value);
    if (reauthProgress.running) {
      scheduleReauthProgressPoll(250, true);
    }
  } catch {
    // 页面初始化不因历史进度读取失败而中断。
  }
}

function resetRebindPreflight() {
  Object.assign(rebindPreflight, {
    eligible: false,
    requested: 0,
    matched: 0,
    eligibleCount: 0,
    source: null,
    sources: [],
    target: null,
    items: [],
    reservationId: "",
    invalidFormat: [],
    invalidTargets: [],
    notFound: [],
    missingCredentials: [],
    conflicts: [],
    message: "",
  });
}

function releaseRebindReservation(reservationId = rebindPreflight.reservationId) {
  const value = String(reservationId || "").trim();
  if (!value) return Promise.resolve();
  return api(`/api/accounts/rebind/preflight/${encodeURIComponent(value)}/cancel`, {
    method: "POST",
    keepalive: true,
    body: "{}",
  }).catch(() => {});
}

function resetRebindProgress() {
  clearTimeout(rebindProgressTimer);
  rebindProgressTimer = null;
  rebindJobId = "";
  rebindCompletionAnnounced = "";
  Object.assign(rebindProgress, {
    jobId: "",
    running: false,
    stopping: false,
    status: "",
    mode: "protocol",
    operation: "email_rebind",
    concurrency: 1,
    total: 0,
    done: 0,
    current: "",
    success: 0,
    failed: 0,
    cancelled: 0,
    last: "",
    lastStatus: "",
    lastMessage: "",
    message: "",
    skipped: [],
    items: [],
  });
}

function resetRebindForInput() {
  rebindInputGeneration += 1;
  const reservationId = rebindPreflight.reservationId;
  resetRebindPreflight();
  if (reservationId) void releaseRebindReservation(reservationId);
  if (rebindProgress.running || rebindChecking.value || rebindRequestBusy.value) {
    persistRebindUiState();
    return;
  }
  if (rebindProgress.total) resetRebindProgress();
  persistRebindUiState();
}

function applyRebindProgress(value) {
  const job = value?.job && typeof value.job === "object" ? value.job : value;
  if (!job?.job_id) return false;
  rebindJobId = String(job.job_id);
  const statusValue = String(job.status || "").toLowerCase();
  const running = ["queued", "running", "stopping"].includes(statusValue) || job.running === true;
  Object.assign(rebindProgress, {
    jobId: rebindJobId,
    running,
    stopping: !!job.stopping || statusValue === "stopping",
    status: statusValue,
    mode: job.mode || "protocol",
    operation: job.operation || "email_rebind",
    concurrency: Math.max(1, Number(job.concurrency || job.requested_concurrency || 1)),
    total: Number(job.total || 0),
    done: Number(job.done || 0),
    current: job.current || "",
    success: Number(job.success || 0),
    failed: Number(job.failed || 0),
    cancelled: Number(job.cancelled || 0),
    last: job.last || "",
    lastStatus: job.last_status || "",
    lastMessage: job.last_message || job.message || "",
    message: job.message || "",
    skipped: Array.isArray(job.skipped) ? job.skipped : [],
    items: Array.isArray(job.items) ? job.items : [],
  });
  persistRebindUiState();
  return true;
}

function persistRebindUiState() {
  try {
    localStorage.setItem(REBIND_UI_STATE_KEY, JSON.stringify({
      jobId: rebindJobId,
      progress: { ...rebindProgress },
      preflight: { ...rebindPreflight },
      savedAt: Date.now(),
    }));
  } catch {
    // Server-side progress remains authoritative if local storage is unavailable.
  }
}

function restoreRebindUiState() {
  try {
    const saved = JSON.parse(localStorage.getItem(REBIND_UI_STATE_KEY) || "null");
    if (!saved || typeof saved !== "object") return;
    if (saved.progress && typeof saved.progress === "object") {
      Object.assign(rebindProgress, {
        ...saved.progress,
        skipped: Array.isArray(saved.progress.skipped) ? saved.progress.skipped : [],
        items: Array.isArray(saved.progress.items) ? saved.progress.items : [],
      });
      rebindJobId = String(saved.jobId || saved.progress.jobId || "");
    }
    if (saved.preflight && typeof saved.preflight === "object") {
      Object.assign(rebindPreflight, {
        ...saved.preflight,
        invalidFormat: Array.isArray(saved.preflight.invalidFormat) ? saved.preflight.invalidFormat : [],
        invalidTargets: Array.isArray(saved.preflight.invalidTargets) ? saved.preflight.invalidTargets : [],
        notFound: Array.isArray(saved.preflight.notFound) ? saved.preflight.notFound : [],
        missingCredentials: Array.isArray(saved.preflight.missingCredentials) ? saved.preflight.missingCredentials : [],
        conflicts: Array.isArray(saved.preflight.conflicts) ? saved.preflight.conflicts : [],
        sources: Array.isArray(saved.preflight.sources) ? saved.preflight.sources : [],
        items: Array.isArray(saved.preflight.items) ? saved.preflight.items : [],
      });
    }
  } catch {
    // Ignore malformed or unavailable local state.
  }
}

function scheduleRebindProgressPoll(delay = 700, announce = true) {
  clearTimeout(rebindProgressTimer);
  rebindProgressTimer = setTimeout(() => pollRebindProgress(announce), delay);
}

async function pollRebindProgress(announce = true) {
  if (!rebindJobId || rebindProgressLoading) return;
  rebindProgressLoading = true;
  try {
    const value = await api(`/api/accounts/rebind/${encodeURIComponent(rebindJobId)}`);
    const job = value?.job || value;
    applyRebindProgress(job);
    if (rebindProgress.running) {
      scheduleRebindProgressPoll(700, announce);
      return;
    }
    clearTimeout(rebindProgressTimer);
    rebindProgressTimer = null;
    const refreshes = [loadStatus(), loadGroups()];
    if (isAccountView()) refreshes.push(loadAccounts());
    await Promise.all(refreshes);
    if (announce && rebindCompletionAnnounced !== rebindJobId) {
      rebindCompletionAnnounced = rebindJobId;
      notify(
        rebindProgress.message || `邮箱换绑完成：成功 ${rebindProgress.success}，失败 ${rebindProgress.failed}`,
        rebindProgress.failed || rebindProgress.status === "failed" ? "err" : "ok",
      );
    }
  } catch (error) {
    if (rebindProgress.running) scheduleRebindProgressPoll(1500, announce);
    else notify(error?.message || "换绑进度读取失败", "err");
  } finally {
    rebindProgressLoading = false;
  }
}

async function resumeRebindProgress() {
  try {
    const value = await api("/api/accounts/rebind/progress");
    const job = value?.job;
    if (!job?.job_id) {
      if (rebindProgress.running) {
        rebindProgress.running = false;
        rebindProgress.stopping = false;
        rebindProgress.current = "";
        rebindProgress.lastMessage = rebindProgress.lastMessage || "服务端换绑任务已结束或记录已清理";
        persistRebindUiState();
      }
      return;
    }
    applyRebindProgress(job);
    if (rebindProgress.running) scheduleRebindProgressPoll(250, false);
  } catch {
    // 页面初始化不因历史换绑进度读取失败而中断。
  }
}

async function preflightRebind(payload = {}) {
  if (rebindProgress.running || rebindChecking.value) return;
  const text = String(payload.text || "").trim();
  if (!text) return notify("请输入当前 GPT 账号邮箱", "err");
  const requestGeneration = ++rebindInputGeneration;
  const previousReservationId = rebindPreflight.reservationId;
  if (previousReservationId) void releaseRebindReservation(previousReservationId);
  rebindChecking.value = true;
  resetRebindPreflight();
  try {
    const value = await api("/api/accounts/rebind/preflight", {
      method: "POST",
      body: jsonBody({
        text,
        new_email: String(payload.new_email || "").trim(),
        provider_type: payload.provider_type || "maddy_webui",
        reservation_id: String(previousReservationId || ""),
      }),
    });
    if (requestGeneration !== rebindInputGeneration) {
      const staleReservationId = String(value?.reservation_id || value?.reservationId || "").trim();
      if (staleReservationId) void releaseRebindReservation(staleReservationId);
      return;
    }
    Object.assign(rebindPreflight, {
      eligible: value?.eligible === true,
      requested: Number(value?.requested || 0),
      matched: Number(value?.matched || 0),
      eligibleCount: Number(value?.eligible_count || value?.eligibleCount || (value?.eligible ? 1 : 0)),
      source: value?.source || null,
      sources: Array.isArray(value?.sources) ? value.sources : [],
      target: value?.target || null,
      items: Array.isArray(value?.items) ? value.items : [],
      reservationId: String(value?.reservation_id || value?.reservationId || ""),
      invalidFormat: Array.isArray(value?.invalid_format) ? value.invalid_format : (Array.isArray(value?.invalidFormat) ? value.invalidFormat : []),
      invalidTargets: Array.isArray(value?.invalid_targets) ? value.invalid_targets : (Array.isArray(value?.invalidTargets) ? value.invalidTargets : []),
      notFound: Array.isArray(value?.not_found) ? value.not_found : (Array.isArray(value?.notFound) ? value.notFound : []),
      missingCredentials: Array.isArray(value?.missing_credentials) ? value.missing_credentials : (Array.isArray(value?.missingCredentials) ? value.missingCredentials : []),
      conflicts: Array.isArray(value?.conflicts) ? value.conflicts : [],
      message: value?.message || "",
    });
    persistRebindUiState();
    if (!rebindPreflight.eligible) notify(rebindPreflight.message || "换绑预检未通过", "err");
    else notify(`已匹配 ${rebindPreflight.eligibleCount || 1} 个未占用邮箱，请确认后开始换绑`);
  } catch (error) {
    if (requestGeneration !== rebindInputGeneration) return;
    rebindPreflight.message = error?.message || "换绑预检失败";
    notify(rebindPreflight.message, "err");
  } finally {
    rebindChecking.value = false;
  }
}

async function startRebind(request = {}) {
  if (rebindProgress.running || rebindRequestBusy.value || !rebindPreflight.reservationId) return;
  const concurrency = Math.min(20, Math.max(1, Math.floor(Number(request.concurrency) || 1)));
  rebindRequestBusy.value = true;
  const batchCount = Number(rebindPreflight.eligibleCount || 1);
  if (!await confirmInside(`将 ${batchCount} 个账号换绑到预检匹配的新邮箱。换绑完成后账号库会同步新邮箱和访问凭据。`, { title: "开始邮箱换绑", confirmText: "开始换绑" })) {
    rebindRequestBusy.value = false;
    return;
  }
  rebindCompletionAnnounced = "";
  try {
    const value = await api("/api/accounts/rebind/start", {
      method: "POST",
      body: jsonBody({
        reservation_id: String(request.reservation_id || rebindPreflight.reservationId),
        provider_type: request.provider_type || "maddy_webui",
        concurrency,
      }),
    });
    if (!applyRebindProgress(value)) throw new Error(value?.message || "换绑任务未返回有效进度");
    // The reservation is consumed by /start. Remove it from the UI so a
    // completed task cannot be submitted a second time after a reload.
    resetRebindPreflight();
    persistRebindUiState();
    scheduleRebindProgressPoll(250, true);
    notify("换绑任务已启动");
  } catch (error) {
    try {
      const activeJob = (await api("/api/accounts/rebind/progress")).job;
      if (activeJob?.job_id && ["queued", "running", "stopping"].includes(activeJob.status)) {
        applyRebindProgress(activeJob);
        resetRebindPreflight();
        persistRebindUiState();
        scheduleRebindProgressPoll(250, true);
        notify("已连接到服务器正在执行的换绑任务");
        return;
      }
    } catch {
      // Keep the original startup error below.
    }
    notify(error?.message || "换绑任务启动失败", "err");
  } finally {
    rebindRequestBusy.value = false;
  }
}

async function stopRebind() {
  if (!rebindJobId || !rebindProgress.running || rebindProgress.stopping) return;
  if (!await confirmInside("停止当前邮箱换绑任务？正在处理的账号会在当前步骤结束后停止。", { title: "停止邮箱换绑", confirmText: "停止", danger: true })) return;
  rebindRequestBusy.value = true;
  rebindProgress.stopping = true;
  try {
    const value = await api(`/api/accounts/rebind/${encodeURIComponent(rebindJobId)}/stop`, { method: "POST", body: "{}" });
    applyRebindProgress(value?.job || value);
    scheduleRebindProgressPoll(250, true);
    notify("已发送停止请求");
  } catch (error) {
    rebindProgress.stopping = false;
    notify(error?.message || "停止换绑失败", "err");
  } finally {
    rebindRequestBusy.value = false;
  }
}

async function healthAccounts(keys, options = {}) {
  const all = keys == null;
  if (accountLoadError.value) {
    notify("账号列表加载不完整，请点击刷新列表重试后再测活", "err");
    return;
  }
  // The first account page is rendered immediately while the remaining
  // pages arrive in the background. Wait for the collection to settle so a
  // bulk action cannot accidentally use the first 1,000 rows as its scope.
  if (accountsLoading.value) {
    try {
      await loadAccounts();
    } catch (error) {
      notify(error?.message || "账号列表仍在加载，暂时无法测活", "err");
      return;
    }
  }
  // Progress is restored asynchronously on page entry. Refresh it once more
  // before submitting so an already-running server job is reported locally
  // instead of producing a misleading confirmation followed by HTTP 409.
  if (!healthProgress.running) {
    try {
      const current = await api("/api/accounts/health/progress?include_updates=false");
      applyHealthProgress(current);
      if (healthProgress.running) startHealthProgressPolling();
    } catch {
      // The submit endpoint still performs its own running-job check.
    }
  }
  if (healthProgress.running) {
    notify("账号批量测活正在运行，请等待当前任务完成", "err");
    return;
  }
  const targetKeys = all ? accounts.value.map((item) => item.delete_key).filter(Boolean) : [...(keys || [])];
  let knownTotal = Math.max(
    targetKeys.length,
    Number(status.value.accounts_count || 0),
    Number(status.value.accounts?.total || 0),
  );
  if (all) {
    try {
      const summary = await api("/api/accounts?limit=1&offset=0");
      const reportedTotal = Number(summary?.total || summary?.count || 0);
      if (reportedTotal > 0) knownTotal = Math.max(targetKeys.length, reportedTotal);
    } catch {
      // The full-account endpoint will perform its own authoritative read;
      // retain the best locally known count for the confirmation text.
    }
  }
  if ((!all && !targetKeys.length) || (all && !targetKeys.length && !knownTotal) || healthProgress.running) return;
  const title = options.title || "账号测活";
  const wantedKeys = all ? null : new Set(targetKeys);
  let knownSkipped = 0;
  for (const item of accounts.value) {
    if (item?.is_deactivated && (all || wantedKeys.has(item.delete_key))) knownSkipped += 1;
  }
  const requestCount = all ? Math.max(knownTotal, targetKeys.length) : targetKeys.length;
  const savedHealthWorkers = Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.health_workers) || 20)));
  const picked = await openConcurrencyDialog({
    title,
    message: options.description || `请求 ${requestCount} 条账号记录，已封号记录自动跳过。`,
    value: savedHealthWorkers,
    kind: "health",
  });
  if (!picked) return;
  const workers = Math.max(1, Math.min(20, Math.floor(Number(picked.workers) || savedHealthWorkers)));
  Object.assign(healthProgress, { running: true, requested: requestCount, actualTotal: Math.max(0, requestCount - knownSkipped), total: Math.max(0, requestCount - knownSkipped), done: 0, checked: 0, usable: 0, unavailable: knownSkipped, skippedKnownDeactivated: knownSkipped, deactivated: knownSkipped, invalidOther: 0, current: `${Math.min(workers, Math.max(1, requestCount - knownSkipped))} 路协议请求并发`, alive: 0, limited: 0, invalid: 0, reauthRequired: 0, error: 0, requestedWorkers: workers, workers, throttled: false, resourceMode: "normal", updates: [], interrupted: false, pendingCount: 0, message: "" });
  startHealthProgressPolling();
  healthRequestActive = true;
  healthServerRunObserved = false;
  try {
    const result = await api("/api/accounts/health", { method: "POST", body: jsonBody({ keys: all ? [] : targetKeys, delete_all: all, workers }) });
    const value = await pollHealthProgress(true);
    if (!value) {
      applyHealthProgress({ ...result, running: false, actual_total: result.actual_total, total: result.actual_total, done: result.checked });
    }
    // Progress polling schedules one debounced refresh. Avoid issuing a
    // second full multi-page account load for the same completed run.
    scheduleAccountsRefresh(100);
    notify(`测活完成：请求 ${healthProgress.requested} 条，实际检测 ${healthProgress.checked} 条，跳过已封号 ${healthProgress.skippedKnownDeactivated} 条；可用 ${healthProgress.usable}，待续期 ${healthProgress.reauthRequired}，其它失效 ${healthProgress.invalidOther}，错误 ${healthProgress.error}`);
  } catch (error) {
    const value = await pollHealthProgress(true);
    if (value?.running) {
      notify("测活请求连接已结束，后台仍在检测，页面将继续同步进度");
    } else if (value?.requested || value?.total) {
      scheduleAccountsRefresh(100);
      notify(value.message || `测活已结束：请求 ${value.requested || 0} 条，实际检测 ${value.checked || value.done || 0} 条，跳过已封号 ${value.skipped_known_deactivated || 0} 条`, value.message ? "err" : "ok");
    } else {
      notify(error?.message || "账号测活失败", "err");
    }
  } finally {
    healthRequestActive = false;
    healthServerRunObserved = false;
    if (healthProgress.running) startHealthProgressPolling();
    else stopHealthProgressPolling();
  }
}
async function healthProtocolLoginEmails(request = {}) {
  if (accountsLoading.value) {
    try { await loadAccounts(); } catch { return; }
  }
  const emails = Array.isArray(request) ? request : request.emails;
  const concurrency = Math.max(1, Math.min(20, Math.floor(Number(request.concurrency) || 3)));
  const wanted = new Set((Array.isArray(emails) ? emails : []).map((email) => String(email || "").trim().toLowerCase()));
  const keys = accounts.value.filter((item) => wanted.has(String(item.email || "").trim().toLowerCase())).map((item) => item.delete_key).filter(Boolean);
  if (!keys.length || healthProgress.running || reauthProgress.running) return;
  await healthAccounts(keys, {
    workers: concurrency,
    title: "协议状态检测",
    description: `将使用 ${concurrency} 路协议请求检测 ${keys.length} 个账号并刷新当前套餐，全程不启动浏览器。`,
  });
}
async function trialEligibilityAccounts(payload = {}) {
  if (accountLoadError.value) {
    notify("账号列表加载不完整，请点击刷新列表重试后再检测资格", "err");
    return;
  }
  if (accountsLoading.value) {
    try {
      await loadAccounts();
    } catch (error) {
      notify(error?.message || "账号列表仍在加载，暂时无法检测资格", "err");
      return;
    }
  }
  const freeKeys = new Set(accounts.value.filter((item) => !item.is_deactivated && String(item.plan_type || "free").toLowerCase() === "free").map((item) => item.delete_key));
  const targetKeys = Array.isArray(payload.keys) ? payload.keys.filter((key) => freeKeys.has(key)) : [];
  if (!payload.all_accounts && !targetKeys.length) return;
  const count = payload.all_accounts ? freeKeys.size : targetKeys.length;
  const savedTrialWorkers = Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.trial_workers) || 20)));
  const savedTrialChannel = uiSettings.value.trial_channel === "nerver" ? "nerver" : "v4";
  const picked = await openConcurrencyDialog({
    title: "Plus 免费试用检测",
    message: `将检测 ${count} 个 Free 账号的 Plus 免费试用资格；Plus 账号自动跳过，检测在后台执行。`,
    value: savedTrialWorkers,
    kind: "trial",
    max: 20,
    mode: savedTrialChannel,
    modeOptions: [{ value: "v4", label: "v4 接口" }, { value: "nerver", label: "nerver 接口" }],
    modeMessages: {
      v4: `将按资格代理配置走 ChatGPT v4 接口检测 ${count} 个 Free 账号；Plus 账号自动跳过。`,
      nerver: `将通过 nerver 接口检测 ${count} 个 Free 账号的免费试用资格（无需代理）；Plus 账号自动跳过。`,
    },
  });
  if (!picked) return;
  const trialChannel = picked.mode === "nerver" ? "nerver" : "v4";
  try {
    const job = await api("/api/accounts/trial-eligibility", { method: "POST", body: jsonBody({ keys: targetKeys, all_accounts: !!payload.all_accounts, force: !!payload.force, concurrency: Math.max(1, Math.min(20, Math.floor(Number(picked.workers) || savedTrialWorkers))), channel: trialChannel }) });
    applyTrialProgress(job);
    pollTrialProgress();
  } catch (error) { notify(error?.message || "资格检测启动失败", "err"); }
}
function applyTrialProgress(job) {
  Object.assign(trialProgress, { jobId: job.job_id || "", running: ["queued", "running", "stopping"].includes(job.status), total: Number(job.total || 0), done: Number(job.done || 0), current: job.current || "", eligible: Number(job.eligible || 0), ineligible: Number(job.ineligible || 0), failed: Number(job.failed || 0), stopped: Number(job.stopped || 0), interrupted: job.status === "interrupted", pendingCount: Math.max(0, Number((job.pending_keys || []).length || 0)), message: job.stage || "" });
}
async function resumeTrialProgress() {
  try {
    const result = await api("/api/accounts/trial-eligibility/progress");
    if (!result?.job?.job_id) return;
    applyTrialProgress(result.job);
    if (trialProgress.running) pollTrialProgress();
  } catch {}
}
async function pollTrialProgress() {
  if (!trialProgress.jobId) return;
  clearTimeout(trialProgressTimer);
  try {
    const job = await api(`/api/accounts/trial-eligibility/${encodeURIComponent(trialProgress.jobId)}`);
    applyTrialProgress(job);
    if (trialProgress.running) { trialProgressTimer = setTimeout(pollTrialProgress, 900); return; }
    await loadAccounts();
    notify(`0元资格检测完成：符合 ${trialProgress.eligible}，不符合 ${trialProgress.ineligible}，失败 ${trialProgress.failed}`, trialProgress.failed ? "err" : "ok");
  } catch (error) {
    // 任务已被清理(如服务重启)时停止轮询,避免无限重试
    if (String(error?.message || "").includes("不存在或已清理")) { trialProgressTimer = null; return; }
    trialProgressTimer = setTimeout(pollTrialProgress, 1800);
  }
}
async function refreshAtAccounts(payload = {}) {
  if (accountLoadError.value) {
    notify("账号列表加载不完整，请点击刷新列表重试后再刷新 AT", "err");
    return;
  }
  if (accountsLoading.value) {
    try { await loadAccounts(); } catch (error) { notify(error?.message || "账号列表仍在加载，暂时无法刷新 AT", "err"); return; }
  }
  const explicitKeys = Array.isArray(payload?.keys) ? payload.keys.filter(Boolean) : [];
  const all = !explicitKeys.length && !!payload?.all_accounts;
  if (!explicitKeys.length && !all) return;
  const savedWorkers = Math.max(1, Math.min(10, Math.floor(Number(uiSettings.value.refresh_at_workers) || 10)));
  let workers = 1;
  if (explicitKeys.length > 1 || all) {
    const targetLabel = all ? "全部临期/过期账号" : `${explicitKeys.length} 个账号`;
    const picked = await openConcurrencyDialog({
      title: "刷新 AT",
      message: `将通过 Session Token 为${targetLabel}重签 Access Token；已封号、无 Session 的账号自动跳过，刷新在后台执行。`,
      value: savedWorkers,
      kind: "refresh_at",
      max: 10,
    });
    if (!picked) return;
    workers = Math.max(1, Math.min(10, Math.floor(Number(picked.workers) || savedWorkers)));
  }
  try {
    const job = await api("/api/accounts/refresh-at", { method: "POST", body: jsonBody({ keys: explicitKeys, all_accounts: all, concurrency: workers }) });
    applyRefreshAtProgress(job);
    pollRefreshAtProgress();
  } catch (error) { notify(error?.message || "刷新 AT 启动失败", "err"); }
}
function applyRefreshAtProgress(job) {
  Object.assign(refreshAtProgress, { jobId: job.job_id || "", running: ["queued", "running", "stopping"].includes(job.status), total: Number(job.total || 0), done: Number(job.done || 0), current: job.current || "", refreshed: Number(job.refreshed || 0), unchanged: Number(job.unchanged || 0), failed: Number(job.failed || 0), stopped: Number(job.stopped || 0) });
}
async function resumeRefreshAtProgress() {
  try {
    const result = await api("/api/accounts/refresh-at/progress");
    if (!result?.job?.job_id) return;
    applyRefreshAtProgress(result.job);
    if (refreshAtProgress.running) pollRefreshAtProgress();
  } catch {}
}
async function pollRefreshAtProgress() {
  if (!refreshAtProgress.jobId) return;
  clearTimeout(refreshAtProgressTimer);
  try {
    const job = await api(`/api/accounts/refresh-at/${encodeURIComponent(refreshAtProgress.jobId)}`);
    applyRefreshAtProgress(job);
    if (refreshAtProgress.running) { refreshAtProgressTimer = setTimeout(pollRefreshAtProgress, 900); return; }
    await loadAccounts();
    notify(`刷新 AT 完成：已刷新 ${refreshAtProgress.refreshed}，AT 未变化 ${refreshAtProgress.unchanged}，失败 ${refreshAtProgress.failed}`, refreshAtProgress.failed ? "err" : "ok");
  } catch (error) { refreshAtProgressTimer = setTimeout(pollRefreshAtProgress, 1800); }
}
async function reauthAccounts(request) {
  const explicitKeys = [...(Array.isArray(request) ? request : request?.keys || [])].filter(Boolean);
  // 与"刷新AT"同一口径：测活失败 ∪ AT过期/缺失，未封号（刷新AT按钮显示同一数量）。
  const allKeys = accounts.value.filter((item) => {
    if (!item?.delete_key || item.is_deactivated) return false;
    const health = String(item.health_status || "").toLowerCase();
    return ["invalid", "error", "reauth_required"].includes(health) || item.at_expired || !item.has_access_token;
  }).map((item) => item.delete_key);
  const targetKeys = explicitKeys.length ? explicitKeys : allKeys;
  if (!targetKeys.length || reauthProgress.running) return;
  const savedReauthWorkers = Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.reauth_workers) || 3)));
  const requestedMode = request?.mode === "browser" || request?.mode === "protocol" ? request.mode : (uiSettings.value.reauth_mode === "browser" ? "browser" : "protocol");
  const credentialKeys = new Set(accounts.value.filter((item) => item?.has_password && item?.has_totp && item?.delete_key).map((item) => item.delete_key));
  const protocolCount = targetKeys.filter((key) => credentialKeys.has(key)).length;
  const picked = await openConcurrencyDialog({
    title: "重新登录账号",
    value: savedReauthWorkers,
    kind: "reauth",
    mode: requestedMode,
    modeOptions: [{ value: "protocol", label: "协议" }, { value: "browser", label: "浏览器" }],
    modeMessages: {
      protocol: protocolCount
        ? `将 ${protocolCount} 个账号使用已保存的密码和 TOTP 2FA 重新登录，不读取邮箱验证码。`
        : "所选账号没有同时保存密码和 TOTP 2FA 的记录，请改用浏览器方式。",
      browser: `将 ${targetKeys.length} 个账号使用浏览器重新登录并读取邮箱验证码。`,
    },
  });
  if (!picked) return;
  const mode = picked.mode === "browser" ? "browser" : "protocol";
  const concurrency = Math.max(1, Math.floor(Number(picked.workers) || 3));
  const finalKeys = mode === "protocol" ? targetKeys.filter((key) => credentialKeys.has(key)) : targetKeys;
  if (!finalKeys.length) { notify("所选账号没有同时保存密码和 TOTP 2FA 的记录，无法走协议登录", "err"); return; }
  Object.assign(reauthProgress, { running: true, stopping: false, mode, operation: "reauth", concurrency, total: finalKeys.length, done: 0, current: `${concurrency} 路并发`, success: 0, failed: 0, cancelled: 0, last: "", lastStatus: "", lastMessage: "" });
  try {
    const value = await api("/api/accounts/reauth/start", { method: "POST", body: jsonBody({ keys: finalKeys, mode, concurrency }) });
    reauthCompletionAnnounced = "";
    applyReauthProgress(value);
    scheduleReauthProgressPoll(250, true);
  } catch (error) {
    // 另一标签页已经启动批次时，连接到服务端任务，而不是把所选账号逐个判失败。
    try {
      const activeJob = (await api("/api/accounts/reauth/progress")).job;
      if (activeJob?.job_id && ["queued", "running", "stopping"].includes(activeJob.status)) {
        applyReauthProgress(activeJob);
        notify("已连接到服务器正在执行的重新登录批次");
        scheduleReauthProgressPoll(250, true);
        return;
      }
    } catch {
      // 使用原始启动错误提示。
    }
    reauthProgress.running = false;
    reauthProgress.current = "";
    notify(error?.message || "重新登录批次启动失败", "err");
  }
}
async function startProtocolLoginByEmail(request = {}) {
  const emails = Array.isArray(request.emails) ? request.emails.filter(Boolean) : [];
  const concurrency = Math.max(1, Math.min(20, Math.floor(Number(request.concurrency) || 3)));
  if (!emails.length || reauthProgress.running) return;
  if (!await confirmInside(`将 ${emails.length} 个邮箱按 ${concurrency} 路并发执行协议登录，仅提交本地同时保存密码和 TOTP 2FA 的账号。`, { title: "协议登录", confirmText: "开始登录" })) return;
  reauthJobId = "";
  reauthCompletionAnnounced = "";
  Object.assign(protocolLoginPreflight, { requested: emails.length, matched: 0, submitted: 0, invalidFormat: [], notFound: [], missingCredentials: [] });
  Object.assign(reauthProgress, { running: true, stopping: false, mode: "protocol", operation: "reauth", concurrency, total: 0, done: 0, current: `${concurrency} 路并发`, success: 0, failed: 0, cancelled: 0, last: "", lastStatus: "", lastMessage: "", items: [] });
  persistReauthUiState();
  try {
    const value = await api("/api/accounts/reauth-by-email/start", { method: "POST", body: jsonBody({ emails, concurrency }) });
    Object.assign(protocolLoginPreflight, {
      requested: Number(value.requested || emails.length),
      matched: Number(value.matched || 0),
      submitted: Number(value.submitted || value.total || 0),
      invalidFormat: Array.isArray(value.invalid_format) ? value.invalid_format : [],
      notFound: Array.isArray(value.not_found) ? value.not_found : [],
      missingCredentials: Array.isArray(value.missing_credentials) ? value.missing_credentials : [],
    });
    persistReauthUiState();
    if (!value.job_id) {
      Object.assign(reauthProgress, {
        running: false,
        stopping: false,
        total: 0,
        done: 0,
        current: "",
        success: 0,
        failed: 0,
        cancelled: 0,
        lastMessage: value.message || "预检完成，没有可提交账号",
        items: [],
      });
      persistReauthUiState();
      notify(value.message || "预检完成，没有可提交账号", "err");
      return;
    }
    applyReauthProgress(value);
    scheduleReauthProgressPoll(250, true);
  } catch (error) {
    reauthProgress.running = false;
    reauthProgress.current = "";
    persistReauthUiState();
    notify(error?.message || "协议登录批次启动失败", "err");
  }
}
async function repairSecurityAccounts(request) {
  const targetKeys = [...(Array.isArray(request) ? request : request?.keys || [])].filter(Boolean);
  if (!targetKeys.length || reauthProgress.running) return;
  const picked = await openConcurrencyDialog({
    title: "补全账号安全字段",
    message: `将 ${targetKeys.length} 个账号补全缺失的密码和 TOTP 2FA，已存在字段会跳过。`,
    value: Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.reauth_workers) || 3))),
    kind: "reauth",
  });
  if (!picked) return;
  const concurrency = Math.max(1, Math.floor(Number(picked.workers) || 3));
  Object.assign(reauthProgress, { running: true, stopping: false, mode: "protocol", operation: "security_repair", concurrency, total: targetKeys.length, done: 0, current: `${concurrency} 路并发`, success: 0, failed: 0, cancelled: 0, last: "", lastStatus: "", lastMessage: "" });
  try {
    const value = await api("/api/accounts/security-repair/start", { method: "POST", body: jsonBody({ keys: targetKeys, mode: "protocol", concurrency }) });
    reauthCompletionAnnounced = "";
    applyReauthProgress(value);
    scheduleReauthProgressPoll(250, true);
  } catch (error) {
    reauthProgress.running = false;
    reauthProgress.current = "";
    notify(error?.message || "安全字段补全批次启动失败", "err");
  }
}
async function startAccountSecurityBatch(request, operation) {
  const targetKeys = [...(Array.isArray(request) ? request : request?.keys || [])].filter(Boolean);
  if (!targetKeys.length || reauthProgress.running) return;
  let newPassword = "";
  let confirmPassword = "";
  let passwordMode = "custom";
  if (operation === "password_change") {
    const passwordSelection = await chooseAccountPassword(targetKeys.length);
    if (!passwordSelection) return;
    passwordMode = passwordSelection.passwordMode;
    newPassword = passwordSelection.newPassword || "";
    confirmPassword = passwordSelection.confirmPassword || "";
  }
  const picked = await openConcurrencyDialog({
    title: operation === "password_change" ? "修改账号密码" : "修改账号 2FA",
    message: operation === "password_change"
      ? `将 ${targetKeys.length} 个账号按所选策略更新密码。`
      : `将 ${targetKeys.length} 个账号停用旧 2FA 因子并重新启用 TOTP，完成后会保存新的密钥。`,
    value: Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.reauth_workers) || 3))),
    kind: "reauth",
  });
  if (!picked) return;
  const concurrency = Math.max(1, Math.floor(Number(picked.workers) || 3));
  const operationLabel = operation === "password_change" ? "修改密码" : "修改 2FA";
  Object.assign(reauthProgress, {
    running: true,
    stopping: false,
    mode: "protocol",
    operation,
    concurrency,
    total: targetKeys.length,
    done: 0,
    current: `${concurrency} 路并发`,
    success: 0,
    failed: 0,
    cancelled: 0,
    last: "",
    lastStatus: "",
    lastMessage: "",
  });
  try {
    const endpoint = operation === "password_change"
      ? "/api/accounts/password-change/start"
      : "/api/accounts/totp-change/start";
    const payload = { keys: targetKeys, concurrency };
    if (operation === "password_change") {
      payload.password_mode = passwordMode;
      if (passwordMode === "custom") Object.assign(payload, { new_password: newPassword, confirm_password: confirmPassword });
    }
    const value = await api(endpoint, { method: "POST", body: jsonBody(payload) });
    reauthCompletionAnnounced = "";
    applyReauthProgress(value);
    scheduleReauthProgressPoll(250, true);
  } catch (error) {
    // A second panel tab can race the start request. Attach to the existing
    // authoritative task instead of presenting the operation as failed.
    try {
      const activeJob = (await api("/api/accounts/reauth/progress")).job;
      if (activeJob?.job_id && ["queued", "running", "stopping"].includes(activeJob.status)) {
        applyReauthProgress(activeJob);
        notify(`已连接到服务器正在执行的${operationLabel}批次`);
        scheduleReauthProgressPoll(250, true);
        return;
      }
    } catch {
      // Fall through to the original startup error.
    }
    reauthProgress.running = false;
    reauthProgress.current = "";
    notify(error?.message || `${operationLabel}批次启动失败`, "err");
  }
}
async function changeAccountPasswords(request) {
  await startAccountSecurityBatch(request, "password_change");
}
async function changeAccountTotp(request) {
  await startAccountSecurityBatch(request, "totp_change");
}
async function stopReauth() {
  if (!reauthJobId || !reauthProgress.running || reauthProgress.stopping) return;
  const operationLabels = { reauth: "重新登录", security_repair: "安全字段补全", password_change: "修改密码", totp_change: "修改 2FA" };
  const operationLabel = operationLabels[reauthProgress.operation] || "账号操作";
  if (!await confirmInside(`停止当前${operationLabel}批次？正在处理的账号会在当前步骤结束后停止，剩余账号不再执行。`, { title: `停止${operationLabel}`, confirmText: "停止", danger: true })) return;
  reauthProgress.stopping = true;
  try {
    const value = await api(`/api/accounts/reauth/${encodeURIComponent(reauthJobId)}/stop`, { method: "POST", body: "{}" });
    applyReauthProgress(value);
    notify("已发送停止请求，正在清理当前登录流程");
    scheduleReauthProgressPoll(250, true);
  } catch (error) {
    reauthProgress.stopping = false;
    notify(error?.message || "停止重新登录失败", "err");
  }
}
async function deleteAccounts(keys) {
  if (!await confirmInside(keys ? `删除选中的 ${keys.length} 个账号？` : "删除全部账号？", { title: "删除账号", confirmText: "删除", danger: true })) return;
  const rollback = stageAccountRemoval(keys || [], !keys);
  notify("正在删除并写入回收站");
  const result = await run(() => api("/api/accounts/delete", { method: "POST", body: jsonBody({ keys: keys || [], delete_all: !keys }) }), (r) => `已删除 ${r.removed || 0} 个账号`);
  if (!result?.ok) return rollback();
  status.value = { ...status.value, accounts_count: Number(result.count ?? status.value.accounts_count) };
  refreshAccountCollectionsQuietly();
}
async function deleteInvalidAccounts(keys) {
  if (!keys?.length || !await confirmInside(`删除 ${keys.length} 个已确认失效的账号？可用、限流和检测错误账号不会删除。`, { title: "删除失效账号", confirmText: "删除失效", danger: true })) return;
  const rollback = stageAccountRemoval(keys, false);
  notify("正在删除失效账号并写入回收站");
  const result = await run(() => api("/api/accounts/delete-invalid", { method: "POST", body: jsonBody({ keys, delete_all: false }) }), (r) => `已删除 ${r.removed || 0} 个失效账号`);
  if (!result?.ok) return rollback();
  status.value = { ...status.value, accounts_count: Number(result.count ?? status.value.accounts_count) };
  refreshAccountCollectionsQuietly();
}
async function downloadAccounts(keys) {
  if (!keys) { location.href = apiUrl("/api/accounts/download-all"); return; }
  await run(() => downloadPost("/api/accounts/download", { keys, delete_all: false }, "registered_accounts.selected.json"), "下载已生成");
}
async function exportSecurityCredentials(keys) {
  if (!keys?.length) return;
  if (!await confirmInside(`导出选中的 ${keys.length} 个账号的邮箱、密码和完整 2FA 密钥？文件只包含具备密码与 2FA 的账号。`, { title: "导出账号密码 + 2FA", confirmText: "开始导出" })) return;
  const result = await run(
    () => downloadPost("/api/accounts/export-security", { keys, delete_all: false }, "accounts.security.selected.txt"),
    (value) => `已导出 ${value.exported || 0} 个账号${value.missing ? `，跳过 ${value.missing} 个缺少密码或 2FA 的账号` : ""}`,
  );
  if (result?.missing) notify(`导出完成：已导出 ${result.exported} 个，跳过 ${result.missing} 个缺少密码或 2FA 的账号`, "err");
}
async function exportSecurityByEmail(emails) {
  if (!emails?.length) return;
  const result = await run(
    () => downloadPost(
      "/api/accounts/export-security-by-email",
      { emails },
      "accounts.security.by-email.txt",
    ),
    (value) => `已导出 ${value.exported || 0} 个账号${value.missing ? `，跳过 ${value.missing} 个未匹配或缺少凭据的账号` : ""}`,
  );
  if (result?.missing) notify(`导出完成：已导出 ${result.exported} 个，跳过 ${result.missing} 个未匹配或缺少凭据的账号`, "err");
}

async function copySelectedAccessTokens(keys) {
  if (!keys?.length || copyingAccessToken.value) return;
  copyingAccessToken.value = true;
  try {
    const result = await api("/api/accounts/access-tokens", { method: "POST", body: jsonBody({ keys, delete_all: false }) });
    const tokens = Array.isArray(result.tokens) ? result.tokens.filter(Boolean) : [];
    if (!tokens.length) return notify("选中账号里没有可复制的 AT", "err");
    await copyOrReveal(tokens.join("\n"), {
      title: `复制 ${tokens.length} 个 AT`,
      successMessage: `已复制 ${tokens.length} 个 AT${result.missing ? `，跳过 ${result.missing} 个缺少 AT 的账号` : ""}`,
      kind: result.missing ? "err" : "ok",
    });
  } catch (error) {
    notify(error?.message || "选中 AT 复制失败", "err");
  } finally {
    copyingAccessToken.value = false;
  }
}

async function exportSelectedAccessTokens(keys) {
  if (!keys?.length) return;
  const result = await run(
    () => downloadPost("/api/accounts/export-access-tokens", { keys, delete_all: false }, "accounts.access-tokens.selected.txt"),
    (value) => `已导出 ${value.exported || 0} 个 AT${value.missing ? `，跳过 ${value.missing} 个缺少 AT 的账号` : ""}`,
  );
  if (result?.missing) notify(`AT 导出完成：已导出 ${result.exported} 个，跳过 ${result.missing} 个`, "err");
}

async function writeClipboard(value) {
  const text = String(value || "");
  if (!text) throw new Error("empty clipboard value");
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {}
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {}
  textarea.remove();
  // Legacy execCommand reports false in some browsers even when the text
  // actually landed in the clipboard (select+copy on a focused textarea).
  // Reporting failure here shows a wrong "copy failed" toast while the user
  // can already paste the value, so treat the fallback as best-effort OK.
  if (!copied) console.warn("execCommand returned false; assuming copy succeeded");
}

function closeClipboardDialog() {
  Object.assign(clipboardDialog, { open: false, title: "", value: "", successMessage: "" });
}

function clipboardDialogCopied() {
  const message = clipboardDialog.successMessage || "已复制";
  closeClipboardDialog();
  notify(message);
}

async function copyOrReveal(value, options = {}) {
  const text = String(value || "");
  if (!text) throw new Error("empty clipboard value");
  try {
    await writeClipboard(text);
    notify(options.successMessage || "已复制", options.kind || "ok");
    return true;
  } catch {
    Object.assign(clipboardDialog, {
      open: true,
      title: options.title || "复制内容",
      value: text,
      successMessage: options.successMessage || "已复制",
    });
    notify("浏览器限制了自动复制，已打开复制窗口", "err");
    return false;
  }
}

async function copyAccessToken(account) {
  if (!account?.delete_key || !account.has_access_token || copyingAccessToken.value) return;
  copyingAccessToken.value = true;
  try {
    const result = await api("/api/accounts/access-token", { method: "POST", body: jsonBody({ key: account.delete_key }) });
    await copyOrReveal(result.access_token, { title: `复制 ${account.email || "账号"} 的 AT`, successMessage: "AT 已复制" });
  } catch (error) {
    notify(error?.message || "AT 复制失败", "err");
  } finally {
    copyingAccessToken.value = false;
  }
}

async function copySessionToken(account) {
  if (!account?.delete_key || !account.has_session_token || copyingAccessToken.value) return;
  copyingAccessToken.value = true;
  try {
    const result = await api("/api/accounts/credentials", { method: "POST", body: jsonBody({ key: account.delete_key, field: "session_token" }) });
    await copyOrReveal(result.value, { title: `复制 ${account.email || "账号"} 的 Session`, successMessage: "Session Token 已复制" });
  } catch (error) {
    notify(error?.message || "Session 复制失败", "err");
  } finally {
    copyingAccessToken.value = false;
  }
}

async function copyAccountCredential(account, field) {
  if (!account?.delete_key || !field) return;
  try {
    const result = await api("/api/accounts/credentials", { method: "POST", body: jsonBody({ key: account.delete_key, field }) });
    const successMessage = field === "password" ? "密码已复制" : field === "totp_secret" ? "2FA 密钥已复制" : "2FA 验证码已复制";
    await copyOrReveal(result.value, { title: successMessage.replace("已复制", ""), successMessage });
  } catch (error) {
    notify(error?.message || "凭据复制失败", "err");
  }
}

async function loadAccountLogs(account = accountLogDialog.account) {
  const key = String(account?.delete_key || "").trim();
  if (!key) return;
  const sequence = ++accountLogRequestSequence;
  accountLogDialog.loading = true;
  accountLogDialog.error = "";
  try {
    const value = await api(`/api/accounts/logs/${encodeURIComponent(key)}?limit=1000`);
    if (sequence !== accountLogRequestSequence) return;
    accountLogDialog.logs = Array.isArray(value?.items) ? value.items : [];
    accountLogDialog.total = Number.isFinite(Number(value?.total)) ? Number(value.total) : accountLogDialog.logs.length;
  } catch (error) {
    if (sequence === accountLogRequestSequence) {
      accountLogDialog.logs = [];
      accountLogDialog.total = 0;
      accountLogDialog.error = error?.message || "账号日志读取失败";
    }
  } finally {
    if (sequence === accountLogRequestSequence) accountLogDialog.loading = false;
  }
}

function openAccountLogs(account) {
  if (!account?.delete_key) return;
  Object.assign(accountLogDialog, { open: true, account, logs: [], total: 0, loading: true, error: "" });
  loadAccountLogs(account);
}

function closeAccountLogs() {
  accountLogRequestSequence += 1;
  Object.assign(accountLogDialog, { open: false, account: null, logs: [], total: 0, loading: false, error: "" });
}

async function copyValue(payload) {
  try {
    await copyOrReveal(payload?.value ?? payload?.text, { title: "复制内容", successMessage: payload?.message || payload?.successMessage || "已复制" });
  } catch {
    notify("复制失败，请重试", "err");
  }
}

async function createAccountGroup(name, selectForTask = false) {
  const clean = String(name || "").trim();
  if (!clean) return notify("请输入分组名称", "err");
  const result = await run(() => api("/api/account-groups", { method: "POST", body: jsonBody({ name: clean }) }), "分组已创建");
  if (!result?.group?.id) return;
  groups.value = [...groups.value, { ...result.group, account_count: 0 }];
  syncGroupSelections();
  if (selectForTask) taskForm.groupId = result.group.id;
}
async function renameAccountGroup(groupId, currentName) {
  const name = await promptInside("输入新的分组名称。", currentName, { title: "重命名分组", confirmText: "保存", maxlength: 48 });
  const clean = typeof name === "string" ? name.trim() : "";
  if (!clean) return;
  const result = await run(() => api(`/api/account-groups/${encodeURIComponent(groupId)}`, { method: "PUT", body: jsonBody({ name: clean }) }), "分组已重命名");
  if (!result?.group) return;
  groups.value = groups.value.map((group) => group.id === groupId ? { ...group, ...result.group } : group);
  accounts.value = accounts.value.map((account) => account.group_id === groupId ? { ...account, group_name: result.group.name } : account);
}
async function deleteAccountGroup(groupId, groupName) {
  if (!await confirmInside(`删除分组“${groupName || "未命名分组"}”？该组账号会迁移到保留分组。`, { title: "删除分组", confirmText: "删除", danger: true })) return;
  const previousAccounts = accounts.value;
  const previousGroups = groups.value;
  const previousTaskGroup = taskForm.groupId;
  const previousIntegrationGroup = integrationForm.groupId;
  const removed = groups.value.find((group) => group.id === groupId);
  const fallback = groups.value.find((group) => group.id !== groupId);
  if (!removed || !fallback) return;
  groups.value = groups.value
    .filter((group) => group.id !== groupId)
    .map((group) => group.id === fallback.id
      ? { ...group, account_count: Number(group.account_count || 0) + Number(removed.account_count || 0) }
      : group);
  accounts.value = accounts.value.map((account) => account.group_id === groupId
    ? { ...account, group_id: fallback.id, group_name: fallback.name }
    : account);
  syncGroupSelections();
  notify("正在删除分组并迁移账号");
  const result = await run(() => api(`/api/account-groups/${encodeURIComponent(groupId)}`, { method: "DELETE" }), (value) => `分组已删除，迁移 ${value.moved || 0} 个账号`);
  if (result?.ok) return refreshAccountCollectionsQuietly();
  accounts.value = previousAccounts;
  groups.value = previousGroups;
  taskForm.groupId = previousTaskGroup;
  integrationForm.groupId = previousIntegrationGroup;
}
async function moveAccountsToGroup(keys, groupId) {
  const target = groups.value.find((group) => group.id === groupId);
  if (!target || !keys?.length) return;
  const previousAccounts = accounts.value;
  const previousGroups = groups.value;
  const wanted = new Set(keys);
  const deltas = new Map();
  const now = new Date().toISOString();
  accounts.value = accounts.value.map((account) => {
    if (!wanted.has(account.delete_key) || account.group_id === groupId) return account;
    deltas.set(account.group_id || "", Number(deltas.get(account.group_id || "") || 0) - 1);
    deltas.set(groupId, Number(deltas.get(groupId) || 0) + 1);
    return { ...account, group_id: groupId, group_name: target.name, updated_at: now };
  });
  applyGroupCountDeltas(deltas);
  notify("正在保存分组变更");
  const result = await run(() => api("/api/accounts/group", { method: "POST", body: jsonBody({ keys, delete_all: false, group_id: groupId }) }), (value) => `已移动 ${value.changed || 0} 个账号`);
  if (result?.ok) return refreshAccountCollectionsQuietly();
  accounts.value = previousAccounts;
  groups.value = previousGroups;
}
function exportGroupCredentials(groupId) {
  if (groupId) location.href = apiUrl(`/api/account-groups/${encodeURIComponent(groupId)}/export-credentials`);
}
function downloadLogs() { location.href = apiUrl("/api/logs/download"); }
async function saveMaintenance() { await run(() => api("/api/maintenance", { method: "PUT", body: jsonBody({ enabled: maintenanceForm.enabled, interval_hours: maintenanceForm.intervalHours, workers: maintenanceForm.workers, auto_delete_invalid: maintenanceForm.autoDeleteInvalid, webhook_url: maintenanceForm.webhookUrl, notify_on_complete: maintenanceForm.notifyOnComplete, notify_on_pool_low: maintenanceForm.notifyOnPoolLow, pool_low_threshold: maintenanceForm.poolLowThreshold }) }), "维护设置已保存", [loadOperations]); }
async function runMaintenance() {
  if (maintenanceActive.value) return;
  maintenanceRunning.value = true;
  try {
    const result = await api("/api/maintenance/run", { method: "POST", body: "{}" });
    await Promise.all([loadOperations(), loadAccounts(), loadStatus(), loadTrash()]);
    notify(`测活完成：请求 ${result.requested || 0} 条，实际检测 ${result.checked || 0} 条，跳过已封号 ${result.skipped_known_deactivated || 0} 条；可用 ${result.usable || 0}，待续期 ${result.reauth_required || 0}，其它失效 ${result.invalid_other || 0}，删除 ${result.deleted || 0}`);
  } catch (error) {
    notify(error.message || "账号维护失败", "err");
  } finally {
    maintenanceRunning.value = false;
  }
}
async function saveMaddyAutoRegister() {
  await run(
    () => api("/api/maddy-auto-register", {
      method: "PUT",
      body: jsonBody({
        enabled: maddyAutoForm.enabled,
        interval_hours: maddyAutoForm.intervalHours,
        per_domain: maddyAutoForm.perDomain,
        concurrency: maddyAutoForm.concurrency,
        engine: maddyAutoForm.engine,
        keep_on_fail: maddyAutoForm.keepOnFail,
        refresh_domains: maddyAutoForm.refreshDomains,
        group_id: maddyAutoForm.groupId,
      }),
    }),
    "Maddy 自动注册策略已保存",
    [loadOperations],
  );
}
async function runMaddyAutoRegister() {
  if (maddyAutoActive.value) return;
  maddyAutoRunning.value = true;
  try {
    const result = await api("/api/maddy-auto-register/run", { method: "POST", body: "{}" });
    await Promise.all([loadOperations(), loadStatus(), loadTasks()]);
    notify(result.message || (result.skipped ? "Maddy 自动注册已在运行" : "Maddy 自动注册已触发"));
  } catch (error) {
    notify(error.message || "Maddy 自动注册触发失败", "err");
  } finally {
    maddyAutoRunning.value = false;
  }
}
async function testWebhook() { await run(() => api("/api/maintenance/test-webhook", { method: "POST", body: jsonBody({ enabled: maintenanceForm.enabled, interval_hours: maintenanceForm.intervalHours, workers: maintenanceForm.workers, auto_delete_invalid: maintenanceForm.autoDeleteInvalid, webhook_url: maintenanceForm.webhookUrl, notify_on_complete: maintenanceForm.notifyOnComplete, notify_on_pool_low: maintenanceForm.notifyOnPoolLow, pool_low_threshold: maintenanceForm.poolLowThreshold }) }), (r) => r.message || "Webhook 测试完成"); }
async function createBackupNow() { await run(() => api("/api/backups", { method: "POST", body: "{}" }), "备份已创建", [loadOperations]); }
const smsForm = reactive({ enabled: false, provider: "smsbower", apiKey: "", service: "dr", countryOrder: "6, 4, 151", timeoutSeconds: 180, maxPhoneAttempts: 5, firefoxApiName: "", firefoxToken: "", firefoxCountryOrder: "usa, chl, eng" });
const smsConfig = ref({});
const codexOauthDialog = reactive({ open: false });
const codexOauthStatus = ref({ running: false, total: 0, done: 0, success: 0, failed: 0, logs: [], results: [] });
let codexOauthTimer = null;
const codexOauthRunning = computed(() => codexOauthDialog.open && !!codexOauthStatus.value.running);
function stopCodexOauthPolling() {
  if (codexOauthTimer) {
    clearInterval(codexOauthTimer);
    codexOauthTimer = null;
    // 接码/重新登录任务结束后刷新中转列表(新 RT 会覆盖写入 auth 文件)
    void loadCliproxyAccounts();
  }
}
async function resumeCodexOauthIfRunning() {
  try {
    const st = await api("/api/codex-oauth/status");
    if (st && st.running) {
      codexOauthStatus.value = st;
      codexOauthDialog.open = true;
      stopCodexOauthPolling();
      codexOauthTimer = setInterval(async () => { try { codexOauthStatus.value = await api("/api/codex-oauth/status"); if (!codexOauthStatus.value.running) stopCodexOauthPolling(); } catch { stopCodexOauthPolling(); } }, 2000);
      notify("检测到接码/重新登录任务进行中,已为你打开实时日志", "ok");
      return true;
    }
  } catch {}
  return false;
}
async function loadSmsConfig() { try { const value = await api("/api/sms-config"); smsConfig.value = value || {}; Object.assign(smsForm, { enabled: !!value.enabled, provider: value.provider || "smsbower", apiKey: "", service: value.service || "dr", countryOrder: value.country_order || "6, 4, 151", timeoutSeconds: Number(value.timeout_seconds || 180), maxPhoneAttempts: Number(value.max_phone_attempts || 5), firefoxApiName: value.firefox_api_name || "", firefoxToken: "", firefoxDockingCode: value.firefox_docking_code || "", firefoxCountryOrder: value.firefox_country_order || "usa, chl, eng" }); } catch {} }
const smsStats = ref(null);
const smsStatsLoading = ref(false);
async function loadSmsStats() {
  smsStatsLoading.value = true;
  try { smsStats.value = await api("/api/sms/stats"); } catch { smsStats.value = null; } finally { smsStatsLoading.value = false; }
}
async function saveSmsConfig() { await run(() => api("/api/sms-config", { method: "PUT", body: jsonBody({ enabled: smsForm.enabled, provider: smsForm.provider, api_key: smsForm.apiKey, service: smsForm.service, country_order: smsForm.countryOrder, poll_interval_ms: 5000, timeout_seconds: smsForm.timeoutSeconds, max_phone_attempts: smsForm.maxPhoneAttempts, firefox_api_name: smsForm.firefoxApiName, firefox_api_token: smsForm.firefoxToken, firefox_docking_code: smsForm.firefoxDockingCode, firefox_country_order: smsForm.firefoxCountryOrder }) }), "接码设置已保存", [loadSmsConfig]); }
async function testSmsConfig() { await run(() => api("/api/sms-config/test", { method: "POST", body: "{}" }), (r) => r.message || "测试完成"); }
async function clearDockingHistory() { if (!await confirmInside("清空火狐狸历史对接码?", { title: "清空历史", confirmText: "清空" })) return; await run(() => api("/api/sms-config/docking-history", { method: "DELETE" }), "历史对接码已清空", [loadSmsConfig]); }
const codexHistoryDialog = reactive({ open: false, loading: false, email: "", key: "", hasRt: false, rows: [] });
const codexHistoryEmailKey = computed(() => codexHistoryDialog.key || "");
const expandedHistoryLogs = ref(new Set());
function toggleHistoryLogs(index) {
  const next = new Set(expandedHistoryLogs.value);
  if (next.has(index)) next.delete(index); else next.add(index);
  expandedHistoryLogs.value = next;
}
function historyLogClass(level) {
  if (level === "error") return "codex-log-error";
  if (level === "warning") return "codex-log-warning";
  if (level === "ok") return "codex-log-ok";
  return "";
}
async function openCodexHistory(account) {
  Object.assign(codexHistoryDialog, { open: true, loading: true, email: account?.email || "", key: account?.delete_key || "", hasRt: !!account?.has_refresh_token, rows: [] });
  expandedHistoryLogs.value = new Set();
  try {
    const result = await api(`/api/codex-oauth/history?emails=${encodeURIComponent(account?.email || "")}`);
    codexHistoryDialog.rows = (result?.history || {})[String(account?.email || "").toLowerCase()] || [];
  } catch { codexHistoryDialog.rows = []; }
  codexHistoryDialog.loading = false;
}
function closeCodexHistory() { codexHistoryDialog.open = false; }
async function rerunCodexOauthFromHistory() {
  const email = codexHistoryDialog.email;
  codexHistoryDialog.open = false;
  if (!email) return;
  await startCodexOauth([email], true);
}
async function startCodexOauth(emails, skipConfirm = false) {
  if (!emails.length) return;
  if (!skipConfirm && !await confirmInside(`对选中 ${emails.length} 个账号接码?`, { title: "接码", confirmText: "开始" })) return;
  if (await resumeCodexOauthIfRunning()) return;
  const result = await run(() => api("/api/codex-oauth/run", { method: "POST", body: jsonBody({ emails }) }), "接码任务已启动");
  if (!result) return;
  codexOauthDialog.open = true;
  stopCodexOauthPolling();
  try { codexOauthStatus.value = await api("/api/codex-oauth/status"); } catch {}
  codexOauthTimer = setInterval(async () => { try { codexOauthStatus.value = await api("/api/codex-oauth/status"); if (!codexOauthStatus.value.running) stopCodexOauthPolling(); } catch { stopCodexOauthPolling(); } }, 2000);
}
async function runCodexOauth(selected) {
  const incoming = Array.isArray(selected) ? selected : [];
  const rows = [];
  const emails = [];
  const seen = new Set();
  for (const value of incoming) {
    const key = typeof value === "string" ? value.trim() : String(value?.delete_key || value?.email || "").trim();
    const row = typeof value === "object" && value
      ? value
      : accounts.value.find((item) => item?.delete_key === key || item?.email === key || item?.account_id === key);
    const email = String(row?.email || (key.includes("@") ? key : "")).trim().toLowerCase();
    if (!email || seen.has(email)) continue;
    seen.add(email);
    rows.push(row || { email });
    emails.push(email);
  }
  if (!emails.length) { notify("选中的账号没有可用邮箱，请刷新账号列表后重试", "err"); return; }
  if (await resumeCodexOauthIfRunning()) return;
  const withRt = rows.filter((item) => item.has_refresh_token);
  if (withRt.length === 1 && rows.length === 1) { await openCodexHistory(rows[0]); return; }
  if (withRt.length) {
    const label = rows.length === 1 ? `该账号已接码过,重新接码?` : `选中 ${rows.length} 个账号中有 ${withRt.length} 个已接码过,继续重新接码?`;
    if (!await confirmInside(label, { title: "接码", confirmText: "重新接码" })) return;
  }
  await startCodexOauth(emails);
}
async function copyRefreshToken(account) {
  if (!account?.delete_key || !account?.has_refresh_token) return;
  try {
    const result = await api("/api/accounts/credentials", { method: "POST", body: jsonBody({ key: account.delete_key, field: "refresh_token" }) });
    await copyOrReveal(result.value, { title: `复制 ${account.email || "账号"} 的 RT`, successMessage: "RT 已复制" });
  } catch (error) { notify(error?.message || "RT 复制失败", "err"); }
}
async function copyHistoryRt(row) {
  const value = String(row?.rt || "").trim();
  if (!value) return;
  await copyOrReveal(value, { title: `复制 ${codexHistoryDialog.email || "账号"} 的 RT`, successMessage: "RT 已复制" });
}
async function stopCodexOauth() { await run(() => api("/api/codex-oauth/stop", { method: "POST", body: "{}" }), "已请求停止"); }
function closeCodexOauth() {
  codexOauthDialog.open = false;
  // 只关弹窗不打断任务;保留静默轮询,任务结束后自动刷新中转列表
  stopCodexOauthPolling();
  startCodexBackgroundWatch();
}
let codexBackgroundWatchTimer = null;
function startCodexBackgroundWatch() {
  if (codexBackgroundWatchTimer) return;
  codexBackgroundWatchTimer = setInterval(async () => {
    try {
      const st = await api("/api/codex-oauth/status");
      if (!st.running) {
        clearInterval(codexBackgroundWatchTimer);
        codexBackgroundWatchTimer = null;
        // 任务结束(含成功写回 RT)后刷新中转列表
        void loadCliproxyAccounts();
      }
    } catch {
      clearInterval(codexBackgroundWatchTimer);
      codexBackgroundWatchTimer = null;
    }
  }, 3000);
}
async function codexCopyRt(payload) { if (payload?.fallback) { Object.assign(clipboardDialog, { open: true, title: `复制 ${payload.email || "账号"} 的 RT`, value: payload.text, successMessage: "RT 已复制" }); return; } notify(payload?.successMessage || "已复制", "ok"); }
onBeforeUnmount(stopCodexOauthPolling);
const smsCountryDialog = reactive({ open: false, loading: false, error: "", items: [], selected: [] });
const smsCountryPrices = reactive({});
const smsCountryProviders = reactive({});
function rebuildSmsCountryOrder() { const text = smsCountryDialog.selected.map((id) => { let token = id; if (smsCountryProviders[id]) { token = `${id}=${smsCountryProviders[id]}`; } else if (smsCountryPrices[id]) { token = `${id}:${smsCountryPrices[id]}`; } return token; }).join(", "); if (smsForm.provider === "firefox") { smsForm.firefoxCountryOrder = text; } else { smsForm.countryOrder = text; } }
function setSmsCountryPrice(payload) { const id = String(payload?.id || ""); if (!id) return; if (payload?.price) { smsCountryPrices[id] = String(payload.price); } else { delete smsCountryPrices[id]; } rebuildSmsCountryOrder(); }
function setSmsCountryProvider(payload) { const id = String(payload?.id || ""); if (!id) return; const value = String(payload?.providerId || "").trim(); if (value) { smsCountryProviders[id] = value; } else { delete smsCountryProviders[id]; } rebuildSmsCountryOrder(); }
function parseSmsCountryToken(raw) { const token = String(raw || "").trim(); if (!token) return null; if (token.includes("=")) { const [country, pid] = token.split("="); return { id: country.trim(), providerId: (pid || "").trim() }; } const parts = token.split(":"); return { id: (parts[0] || "").trim(), price: (parts[1] || "").trim() }; }
function smsCountryIds() { const text = smsForm.provider === "firefox" ? smsForm.firefoxCountryOrder : smsForm.countryOrder; return String(text || "").split(/[,,]/).map((item) => item.trim()).filter(Boolean); }
const smsCountries = computed(() => { if (smsForm.provider === "firefox") { const dock = String(smsForm.firefoxDockingCode || "").trim(); return dock ? [dock] : []; } const list = String(smsForm.countryOrder || "").split(/[,,]/).map((s) => s.trim()).filter(Boolean); return list.map((raw) => { const parsed = parseSmsCountryToken(raw); const id = parsed?.id || raw; const hit = (smsCountryDialog.items || []).find((item) => String(item.id) === String(id)); const label = hit ? `${hit.name}${hit.calling || ""}` : id; if (parsed?.providerId) { const pids = String(parsed.providerId).split(/[|,]/).map((p) => `#${p.trim()}`).filter((p) => p !== "#"); return `${label} ${pids.join("→")}`; } if (parsed?.price) { return `${label}≤$${parsed.price}`; } return label; }); });
async function openSmsCountryPicker() { smsCountryDialog.open = true; smsCountryDialog.loading = true; smsCountryDialog.error = ""; smsCountryDialog.selected = []; smsCountryDialog.items = []; for (const raw of smsCountryIds()) { const parsed = parseSmsCountryToken(raw); if (!parsed?.id) continue; smsCountryDialog.selected.push(parsed.id); if (parsed.price) { smsCountryPrices[parsed.id] = parsed.price; } else { delete smsCountryPrices[parsed.id]; } if (parsed.providerId) { smsCountryProviders[parsed.id] = parsed.providerId; } else { delete smsCountryProviders[parsed.id]; } } try { const value = await api(`/api/sms-countries?provider=${encodeURIComponent(smsForm.provider)}`); smsCountryDialog.items = value.items || []; } catch (error) { smsCountryDialog.error = error?.message || "获取国家列表失败"; } finally { smsCountryDialog.loading = false; } }
function toggleSmsCountry(id) { const value = String(id); const list = smsCountryDialog.selected; const index = list.indexOf(value); if (index >= 0) { list.splice(index, 1); delete smsCountryPrices[value]; delete smsCountryProviders[value]; } else { list.push(value); } rebuildSmsCountryOrder(); }
const uiSettings = ref({ health_workers: 20, reauth_workers: 3, trial_workers: 20, refresh_at_workers: 10, reauth_mode: "protocol", trial_channel: "v4" });
let uiSettingsLoaded = false;
async function loadUiSettings() { try { const value = await api("/api/ui-settings"); uiSettings.value = value || {}; uiSettingsLoaded = true; } catch {} }
const concurrencyDialog = reactive({ open: false, title: "", message: "", value: 10, kind: "health", max: 20, mode: "protocol", modeOptions: [], modeMessages: {} });
let concurrencyDialogResolve = null;
async function openConcurrencyDialog({ title, message, value, kind, max, mode, modeOptions, modeMessages }) {
  if (!uiSettingsLoaded) await loadUiSettings();
  if (concurrencyDialogResolve) { const stale = concurrencyDialogResolve; concurrencyDialogResolve = null; stale(null); }
  const ceiling = Math.max(1, Math.floor(Number(max) || 20));
  Object.assign(concurrencyDialog, { open: true, title: title || "并发设置", message: message || "", value: Math.max(1, Math.min(ceiling, Math.floor(Number(value) || 1))), kind: kind || "health", max: ceiling, mode: mode || "protocol", modeOptions: Array.isArray(modeOptions) ? modeOptions : [], modeMessages: modeMessages && typeof modeMessages === "object" ? modeMessages : {} });
  return new Promise((resolve) => { concurrencyDialogResolve = resolve; });
}
function closeConcurrencyDialog(result) {
  concurrencyDialog.open = false;
  const resolve = concurrencyDialogResolve;
  concurrencyDialogResolve = null;
  resolve?.(result);
}
async function saveConcurrencyDefault(pick) {
  const picked = pick && typeof pick === "object" ? pick : { workers: pick };
  const ceiling = Math.max(1, Math.floor(Number(concurrencyDialog.max) || 20));
  const workers = Math.max(1, Math.min(ceiling, Math.floor(Number(picked.workers) || 1)));
  const mode = picked.mode === "browser" ? "browser" : "protocol";
  const payload = {
    health_workers: Number(uiSettings.value.health_workers) || 20,
    reauth_workers: Number(uiSettings.value.reauth_workers) || 3,
    trial_workers: Math.max(1, Math.min(20, Math.floor(Number(uiSettings.value.trial_workers) || 20))),
    refresh_at_workers: Math.max(1, Math.min(10, Math.floor(Number(uiSettings.value.refresh_at_workers) || 10))),
    reauth_mode: uiSettings.value.reauth_mode === "browser" ? "browser" : "protocol",
    trial_channel: uiSettings.value.trial_channel === "nerver" ? "nerver" : "v4",
  };
  if (concurrencyDialog.kind === "health") { payload.health_workers = workers; }
  else if (concurrencyDialog.kind === "trial") { payload.trial_workers = Math.min(20, workers); payload.trial_channel = picked.mode === "nerver" ? "nerver" : "v4"; }
  else if (concurrencyDialog.kind === "refresh_at") { payload.refresh_at_workers = Math.min(10, workers); }
  else { payload.reauth_workers = workers; payload.reauth_mode = mode; }
  try { await api("/api/ui-settings", { method: "PUT", body: jsonBody(payload) }); uiSettings.value = payload; notify("默认值已保存", "ok"); } catch (error) { notify(error?.message || "保存失败", "err"); }
}
const importAccountsDialog = reactive({ open: false });
// 导入任务的 email -> 原始凭据行缓存：失败重试时无需重新粘贴全部内容
const lastImportLines = ref(new Map());
// 最近一次导入的账号类型/工作区/并发设置，重试失败项时沿用
const lastImportOptions = reactive({ accountType: "personal", workspace: "", concurrency: 1 });
async function submitImportAccounts(payload) {
  const value = String(payload?.text || "").trim();
  if (!value) return;
  const accountType = String(payload?.accountType || "personal").trim() || "personal";
  const workspace = String(payload?.workspace || "").trim();
  const concurrency = Math.min(Math.max(Number(payload?.concurrency) || 1, 1), 8);
  const rows = new Map();
  for (const line of value.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const [email] = trimmed.split("----");
    if (email && email.trim()) rows.set(email.trim().toLowerCase(), trimmed);
  }
  lastImportLines.value = rows;
  lastImportOptions.accountType = accountType;
  lastImportOptions.workspace = workspace;
  lastImportOptions.concurrency = concurrency;
  await run(() => api("/api/codex-oauth/import", { method: "POST", body: jsonBody({ text: value, account_type: accountType, workspace, concurrency }) }), "导入任务已启动");
  importAccountsDialog.open = false; codexOauthDialog.open = true; stopCodexOauthPolling(); codexOauthTimer = setInterval(async () => { try { codexOauthStatus.value = await api("/api/codex-oauth/status"); if (!codexOauthStatus.value.running) { stopCodexOauthPolling(); loadAccounts(); } } catch { stopCodexOauthPolling(); } }, 2000); }
async function retryCodexOauthFailed(item) {
  const email = String(item?.email || "").trim();
  if (!email) return;
  if (codexOauthStatus.value?.mode === "import") {
    const line = lastImportLines.value.get(email.toLowerCase());
    if (!line) { notify("该账号的原始导入行已丢失,请重新粘贴导入", "err"); return; }
    await submitImportAccounts({ text: line, accountType: lastImportOptions.accountType, workspace: lastImportOptions.workspace, concurrency: lastImportOptions.concurrency });
    return;
  }
  await startCodexOauth([email], true);
}
async function retryAllCodexOauthFailed() {
  const failed = (codexOauthStatus.value?.results || []).filter((item) => item?.status === "failed");
  if (!failed.length) return;
  if (codexOauthStatus.value?.mode === "import") {
    const lines = failed.map((item) => lastImportLines.value.get(String(item.email || "").trim().toLowerCase())).filter(Boolean);
    if (!lines.length) { notify("失败账号的原始导入行已丢失,请重新粘贴导入", "err"); return; }
    await submitImportAccounts({ text: lines.join("\n"), accountType: lastImportOptions.accountType, workspace: lastImportOptions.workspace, concurrency: lastImportOptions.concurrency });
    return;
  }
  await startCodexOauth(failed.map((item) => item.email).filter(Boolean), true);
}

async function saveBackupSchedule() { await run(() => api("/api/backup-schedule", { method: "PUT", body: jsonBody({ enabled: backupScheduleForm.enabled, time: backupScheduleForm.time, retention_days: backupScheduleForm.retentionDays }) }), "定时备份设置已保存", [loadOperations]); }
async function restoreBackupNow(name) { if (!await confirmInside(`恢复备份 ${name}？当前配置和数据会被覆盖。`, { title: "恢复备份", confirmText: "恢复", danger: true })) return; await run(() => api("/api/backups/restore", { method: "POST", body: jsonBody({ name }) }), "备份已恢复", [initialize]); }
async function restoreTrash(ids) { await run(() => api("/api/accounts/trash/restore", { method: "POST", body: jsonBody({ ids }) }), (r) => `已恢复 ${r.restored || 0} 个账号`, [loadTrash, loadAccounts, loadGroups, loadStatus, loadOperations]); }
async function changePassword() { const result = await run(() => api("/api/auth/change-password", { method: "POST", body: jsonBody({ current_password: securityForm.currentPassword, new_password: securityForm.newPassword }) }), "密码已修改"); if (result) Object.assign(securityForm, { currentPassword: "", newPassword: "", confirmPassword: "" }); }
async function revokeSessions() { if (!await confirmInside("退出其他所有面板会话？", { title: "会话安全", confirmText: "退出其他会话", danger: true })) return; await run(() => api("/api/auth/revoke-sessions", { method: "POST", body: "{}" }), "其他会话已失效"); }
async function logout() { await api("/api/auth/logout", { method: "POST", body: "{}" }); location.replace("/login"); }

onMounted(() => {
  const hash = location.hash.replace(/^#/, "");
  navigate(views[hash] ? hash : "task", false);
  window.addEventListener("hashchange", () => {
    const next = location.hash.replace(/^#/, "");
    navigate(views[next] ? next : "task", false);
  });
  initialize();
  if (onLoginPage) return;  // 认证前的登录页不启动任何轮询
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    // The SSE stream carries job status continuously. Keep the aggregate
    // resource summary warm at a lower cadence while connected, and retain
    // the 30s fallback when the stream is unavailable.
    if (!connected.value || Date.now() - lastStatusLoadedAt >= 60000) loadStatus().catch(() => {});
    if (active.value === "task" && !connected.value) loadTasks().catch(() => {});
    if (active.value === "logs" && !connected.value) loadLogs().catch(() => {});
  }, 30000);
  scheduleProgressPoll();
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("keydown", handleShellKeydown);
});
onBeforeUnmount(() => {
  rebindInputGeneration += 1;
  stream?.close();
  clearTimeout(streamReconnectTimer);
  clearInterval(pollTimer);
  clearTimeout(progressTimer);
  clearTimeout(accountRefreshTimer);
  accountRefreshTimer = null;
  accountRefreshIncludeGroups = false;
  accountReloadQueued = false;
  accountReloadQueuedWithGroups = false;
  clearTimeout(taskIndexFlushTimer);
  taskIndexFlushTimer = null;
  stopHealthProgressPolling();
  clearTimeout(reauthProgressTimer);
  clearTimeout(rebindProgressTimer);
  clearTimeout(refreshAtProgressTimer);
  if (!rebindProgress.running && !rebindRequestBusy.value && rebindPreflight.reservationId) {
    const reservationId = rebindPreflight.reservationId;
    resetRebindPreflight();
    void releaseRebindReservation(reservationId);
    persistRebindUiState();
  }
  clearTimeout(noticeTimer);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  window.removeEventListener("keydown", handleShellKeydown);
});
</script>

<template>
  <LoginView v-if="onLoginPage" />
  <div v-else class="shell vue-shell">
    <button v-if="sidebarOpen" class="sidebar-scrim" type="button" aria-label="关闭导航" @click="closeSidebar"></button>
    <AppSidebar :active="active" :account-count="status.accounts_count" :open="sidebarOpen" @navigate="navigate" @close="closeSidebar" />
    <AppTopbar
      :title="title"
      :subtitle="subtitle"
      :username="username"
      :running="!!status.job?.running"
      :sidebar-open="sidebarOpen"
      @open-sidebar="openSidebar"
      @refresh="initialize"
      @logout="logout"
    />
    <main class="content">
      <!-- Account pages can render their first page while the remaining
           pages continue loading in the background. Keep the full-screen
           gate for other views and for an actually empty account store. -->
      <div v-if="loading && !(isAccountView() && accounts.length)" class="page-loading"><span></span>正在载入控制台</div>
      <template v-else>
        <MetricStrip v-if="active !== 'operations'" :status="status" />
        <OperationProgress :progress="progress" />
        <TaskView v-if="active === 'task'" :status="status" :tasks="tasks" :groups="groups" :form="taskForm" :busy="busy" @start="startJob" @stop="stopJob" @pause="pauseJob" @resume="resumeJob" @retry-failed="retryFailedJobs" @create-group="(name) => createAccountGroup(name, true)" @reset-mail="resetMail" @refresh-accounts="loadAccounts" @refresh-tasks="loadTasks" @delete-tasks="deleteTasks" @navigate="navigate" />
        <BrowserView v-else-if="active === 'fingerprint'" :config="config" :capabilities="capabilities" :busy="busy" @save="saveBrowser" @refresh="loadCapabilities" />
        <MailView v-else-if="active === 'mail'" :forms="mailForms" :mailboxes="mailboxes" :progress="progress" :busy="busy" @save-outlook="saveOutlook" @save-icloud="saveIcloud" @save-maddy="saveMaddyMail" @save-fastmail="saveFastmailMail" @load-maddy-domains="loadMaddyDomains" @load-fastmail-accounts="loadFastmailAccounts" @refresh="loadMailboxes" @delete="deleteMailboxes" @state="updateMailboxState" />
        <ProxyView v-else-if="active === 'proxy'" :form="proxyForm" :diagnostics="proxyDiagnostics" :progress="progress" :busy="busy" @save="saveProxy" @test="testProxies" />
        <template v-else-if="active === 'plus-eligibility'">
          <PlusEligibilityView :form="tokenEligibilityForm" :state="tokenEligibility" @check="checkTokenEligibility" @clear="clearTokenEligibility" @batch-check="startBatchTokenEligibility" @batch-stop="stopBatchTokenEligibility" />
          <TrialProxyView :form="trialProxyForm" :busy="busy" @save="saveTrialProxyPool" @refresh="loadTrialProxyPool" />
        </template>
        <IntegrationView v-else-if="active === 'chatgpt2api'" :form="integrationForm" :groups="groups" :progress="progress" :busy="busy" @save="saveIntegration" @test="testIntegration" @upload="uploadIntegration" />
        <LogsView v-else-if="active === 'logs'" :logs="logs" :connected="connected" @refresh="loadLogs" @download="downloadLogs" @clear="logs = []" />
        <AccountsView v-else-if="active === 'accounts'" :accounts="accounts" :accounts-loading="accountsLoading" :account-load-progress="accountLoadProgress" :account-load-error="accountLoadError" :groups="groups" :proxy-countries="proxyForm.countries" :busy="busy" :copying-at="copyingAccessToken" :health-progress="healthProgress" :reauth-progress="reauthProgress" :trial-progress="trialProgress" :refresh-at-progress="refreshAtProgress" @refresh="loadAccounts" @health="healthAccounts" @resume-health="resumeHealthCheck" @resume-trial="resumeTrialCheck" @trial-check="trialEligibilityAccounts" @refresh-at="refreshAtAccounts" @reauth="reauthAccounts" @security-repair="repairSecurityAccounts" @password-change="changeAccountPasswords" @totp-change="changeAccountTotp" @account-logs="openAccountLogs" @stop-reauth="stopReauth" @delete="deleteAccounts" @delete-invalid="deleteInvalidAccounts" @download="downloadAccounts" @copy-selected-at="copySelectedAccessTokens" @export-selected-at="exportSelectedAccessTokens" @export-security="exportSecurityCredentials" @create-group="createAccountGroup" @rename-group="renameAccountGroup" @delete-group="deleteAccountGroup" @move-group="moveAccountsToGroup" @export-group="exportGroupCredentials" @copy-at="copyAccessToken" @copy-session="copySessionToken" @copy-credential="copyAccountCredential" @copy-value="copyValue" @codex-oauth="runCodexOauth" @import-accounts="importAccountsDialog.open = true" :codex-oauth-running="codexOauthRunning" />
        <RebindView v-else-if="active === 'rebind'" :progress="rebindProgress" :preflight="rebindPreflight" :busy="busy || rebindRequestBusy" :checking="rebindChecking" @preflight="preflightRebind" @start="startRebind" @stop="stopRebind" @reset="resetRebindForInput" @input-change="resetRebindForInput" />
        <CredentialExportView v-else-if="active === 'credential-export'" :accounts="accounts" :busy="busy" @export="exportSecurityByEmail" />
        <ProtocolLoginView v-else-if="active === 'protocol-login'" :accounts="accounts" :progress="reauthProgress" :health-progress="healthProgress" :preflight="protocolLoginPreflight" :busy="busy" @start="startProtocolLoginByEmail" @stop="stopReauth" @health="healthProtocolLoginEmails" @input-change="resetProtocolLoginResultsForInput" />
        <OperationsView v-else-if="active === 'operations'" :operations="operations" :trash="trash" :form="maintenanceForm" :maddy-auto-form="maddyAutoForm" :backup-schedule-form="backupScheduleForm" :sms-form="smsForm" :sms-config="smsConfig" :groups="groups" :security="securityForm" :busy="busy" :maintenance-running="maintenanceActive" :maddy-auto-running="maddyAutoActive" :sms-stats="smsStats" :sms-stats-loading="smsStatsLoading" @refresh="() => Promise.all([loadOperations(), loadTrash(), loadSmsStats()])" @save="saveMaintenance" @run="runMaintenance" @save-maddy-auto="saveMaddyAutoRegister" @run-maddy-auto="runMaddyAutoRegister" @test-webhook="testWebhook" @backup="createBackupNow" @save-backup-schedule="saveBackupSchedule" @save-sms-config="saveSmsConfig" @test-sms="testSmsConfig" @pick-countries="openSmsCountryPicker" @clear-docking-history="clearDockingHistory" :sms-countries="smsCountries" @restore-backup="restoreBackupNow" @restore-trash="restoreTrash" @change-password="changePassword" @revoke-sessions="revokeSessions" @refresh-sms-stats="loadSmsStats" />
          <CliproxyView v-else-if="active === 'cliproxy'" :accounts="cliproxyAccounts" :loading="cliproxyLoading" :refreshing="cliproxyRefreshing" :importing="cliproxyImporting" :deleting="cliproxyDeleting" :relogin-loading="cliproxyReloginLoading" :resetting-file="cliproxyResettingFile" @refresh="loadCliproxyAccounts" @refresh-quota="refreshCliproxyQuota" @import-cpa="importCliproxyCpa" @import-sub2="importCliproxySub2" @export-cpa="exportCliproxyCpa" @export-sub2="exportCliproxySub2" @consume-reset="consumeCliproxyReset" @delete-accounts="deleteCliproxyAccounts" @relogin-account="reloginCliproxyAccount" />
      </template>
    </main>
    <footer class="foot"><span>gpt-register-fp · Vue 3</span></footer>
    <Transition name="notice"><div v-if="notice.text" :class="['vue-notice', notice.kind]" role="status">{{ notice.text }}</div></Transition>
    <AppDialog :dialog="dialog" @confirm="closeDialog" @cancel="closeDialog(false)" />
    <PasswordChangeDialog :open="passwordChangeDialog.open" :count="passwordChangeDialog.count" @confirm="closePasswordChangeDialog" @cancel="closePasswordChangeDialog(false)" />
    <ClipboardDialog :open="clipboardDialog.open" :title="clipboardDialog.title" :value="clipboardDialog.value" @close="closeClipboardDialog" @copied="clipboardDialogCopied" />
    <ConcurrencyDialog :open="concurrencyDialog.open" :title="concurrencyDialog.title" :message="concurrencyDialog.message" :value="concurrencyDialog.value" :max="concurrencyDialog.max" :mode="concurrencyDialog.mode" :mode-options="concurrencyDialog.modeOptions" :mode-messages="concurrencyDialog.modeMessages" @confirm="closeConcurrencyDialog" @cancel="closeConcurrencyDialog(null)" @save="saveConcurrencyDefault" />
    <SmsCountryPicker :open="smsCountryDialog.open" :provider="smsForm.provider" :items="smsCountryDialog.items" :selected="smsCountryDialog.selected" :loading="smsCountryDialog.loading" :error="smsCountryDialog.error" :prices="smsCountryPrices" :providers="smsCountryProviders" @close="smsCountryDialog.open = false" @toggle="toggleSmsCountry" @set-price="setSmsCountryPrice" @set-provider="setSmsCountryProvider" />
    <ImportAccountsDialog :open="importAccountsDialog.open" :busy="busy" @close="importAccountsDialog.open = false" @submit="submitImportAccounts" />
    <CodexOauthDialog :open="codexOauthDialog.open" :status="codexOauthStatus" @close="closeCodexOauth" @stop="stopCodexOauth" @copy="codexCopyRt" @retry="retryCodexOauthFailed" @retry-all="retryAllCodexOauthFailed" />
    <Teleport to="body">
      <Transition name="dialog-fade">
        <div v-if="codexHistoryDialog.open" class="app-dialog-mask" @click.self="closeCodexHistory">
          <section class="app-dialog codex-history-dialog" role="dialog" aria-modal="true" aria-label="接码历史">
            <div class="app-dialog-head">
              <div>
                <span class="app-dialog-kicker"><Smartphone :size="12" aria-hidden="true" />接码历史</span>
                <h2>{{ codexHistoryDialog.email }}</h2>
                <small>历史接码记录 · 可复制 RT 或重新接码</small>
              </div>
              <button class="app-dialog-close" type="button" title="关闭" aria-label="关闭" @click="closeCodexHistory"><X :size="16" aria-hidden="true" /></button>
            </div>
          <p class="codex-hint"><Smartphone :size="14" aria-hidden="true" /> {{ codexHistoryDialog.hasRt ? "该账号已有 RT(可直接复制或重新接码)" : "该账号尚无 RT" }}</p>
          <div class="codex-history-list-wrap">
            <div v-if="codexHistoryDialog.loading" class="empty-card">加载历史中…</div>
            <div v-else-if="!codexHistoryDialog.rows.length" class="empty-card">暂无接码历史记录</div>
            <div v-else class="codex-history-list">
              <div v-for="(row, index) in codexHistoryDialog.rows" :key="index" class="codex-history-row" :class="row.status">
                <div class="codex-history-row-main">
                  <div class="codex-history-main">
                    <b>{{ row.status === "success" ? "接码成功" : "接码失败" }}<span v-if="row.phone" class="codex-phone"> · {{ row.phone }}</span></b>
                    <small>{{ row.at }}<template v-if="row.error"> · {{ row.error }}</template></small>
                    <small v-if="row.rt" class="codex-rt">RT: {{ row.rt.slice(0, 36) }}…</small>
                  </div>
                  <div class="codex-history-row-actions">
                    <button v-if="(row.logs || []).length" class="btn secondary sm" type="button" @click="toggleHistoryLogs(index)">{{ expandedHistoryLogs.has(index) ? "收起日志" : `流程日志(${row.logs.length})` }}</button>
                    <button v-if="row.rt" class="btn secondary sm" type="button" @click="copyHistoryRt(row)">复制RT</button>
                  </div>
                </div>
                <div v-if="expandedHistoryLogs.has(index) && (row.logs || []).length" class="codex-history-flow">
                  <p v-for="(line, li) in row.logs" :key="li" :class="historyLogClass(line.level)"><span class="codex-log-time">{{ line.t }}</span>{{ line.m }}</p>
                </div>
              </div>
            </div>
          </div>
          <div class="app-dialog-actions">
            <button class="btn secondary" type="button" @click="closeCodexHistory">关闭</button>
            <button class="btn secondary" type="button" :disabled="!codexHistoryDialog.hasRt" @click="copyRefreshToken({ delete_key: codexHistoryDialog.key, has_refresh_token: codexHistoryDialog.hasRt, email: codexHistoryDialog.email })">复制当前RT</button>
            <button class="btn primary" type="button" @click="rerunCodexOauthFromHistory">重新接码</button>
          </div>
        </section>
      </div>
      </Transition>
    </Teleport>
    <AccountLogDialog :open="accountLogDialog.open" :account="accountLogDialog.account" :logs="accountLogDialog.logs" :total="accountLogDialog.total" :loading="accountLogDialog.loading" :error="accountLogDialog.error" @close="closeAccountLogs" @refresh="loadAccountLogs()" />
  </div>
</template>
