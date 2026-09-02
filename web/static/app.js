/* GPT Register FP — web panel client */

// The legacy client only had a single 1,000-row account request. Keep stale
// tabs/bookmarks from running it after the paginated Vue console is deployed.
if (location.pathname === "/static/index.html") {
  location.replace("/" + location.hash);
}

function authHeaders(extra = {}) {
  return { ...(extra || {}) };
}

function withTokenQuery(url) {
  // Session is HttpOnly cookie; no token in query needed.
  return url;
}

async function ensureAuthGate() {
  try {
    const res = await fetch("/api/auth/status", { credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (data && data.authenticated) {
      const label = document.getElementById("loginUserLabel");
      if (label && data.username) {
        label.style.display = "";
        label.textContent = data.username;
      }
      return true;
    }
  } catch {}
  location.replace("/login");
  return false;
}

async function logout() {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch {}
  location.replace("/login");
}

const $ = (id) => document.getElementById(id);

const els = {
  pageTitle: $("pageTitle"),
  pageSubtitle: $("pageSubtitle"),
  runBadge: $("runBadge"),
  stAvail: $("stAvail"),
  stSplitAvail: $("stSplitAvail"),
  stMailTotal: $("stMailTotal"),
  stAccounts: $("stAccounts"),
  stAccountsUsable: $("stAccountsUsable"),
  stAccountsDeactivated: $("stAccountsDeactivated"),
  stSuccess: $("stSuccess"),
  stFailed: $("stFailed"),
  stProxy: $("stProxy"),

  fTotal: $("fTotal"),
  fConc: $("fConc"),
  fEngine: $("fEngine"),
  fEmail: $("fEmail"),
  fMailType: $("fMailType"),
  fKeep: $("fKeep"),
  btnStart: $("btnStart"),
  btnStop: $("btnStop"),
  jobMsg: $("jobMsg"),
  fpBrowser: $("fpBrowser"),
  fpCfxOs: $("fpCfxOs"),
  fpCfxLocale: $("fpCfxLocale"),
  fpCfxHumanize: $("fpCfxHumanize"),
  fpCfxHeadless: $("fpCfxHeadless"),
  fpCfxGeoip: $("fpCfxGeoip"),
  regTimeout: $("regTimeout"),
  regRetries: $("regRetries"),
  btnSaveFp: $("btnSaveFp"),
  mailMode: $("mailMode"),
  mailboxesOutlook: $("mailboxesOutlook"),
  mailMetaOutlook: $("mailMetaOutlook"),
  btnSaveMailOutlook: $("btnSaveMailOutlook"),
  icloudEnable: $("icloudEnable"),
  icloudApiBase: $("icloudApiBase"),
  mailboxesIcloud: $("mailboxesIcloud"),
  mailMetaIcloud: $("mailMetaIcloud"),
  btnSaveMailIcloud: $("btnSaveMailIcloud"),
  mailMeta: $("mailMeta"),
  btnResetFailed: $("btnResetFailed"),
  pxEnabled: $("pxEnabled"),
  pxMode: $("pxMode"),
  pxUrl: $("pxUrl"),
  pxResourceUrl: $("pxResourceUrl"),
  pxClearanceMode: $("pxClearanceMode"),
  pxFlareUrl: $("pxFlareUrl"),
  pxFlareProxy: $("pxFlareProxy"),
  pxFile: $("pxFile"),
  pxRotate: $("pxRotate"),
  pxText: $("pxText"),
  btnSaveProxy: $("btnSaveProxy"),
  c2aEnabled: $("c2aEnabled"),
  c2aAuto: $("c2aAuto"),
  c2aUrl: $("c2aUrl"),
  c2aKey: $("c2aKey"),
  btnSaveC2a: $("btnSaveC2a"),
  btnTestC2a: $("btnTestC2a"),
  btnUploadC2a: $("btnUploadC2a"),
  c2aMsg: $("c2aMsg"),
  logBox: $("logBox"),
  logStreamBadge: $("logStreamBadge"),
  btnClearLog: $("btnClearLog"),
  accBody: $("accBody"),
  btnReloadAcc: $("btnReloadAcc"),
  btnReloadAccBottom: $("btnReloadAccBottom"),
  btnRefresh: $("btnRefresh"),
  navAccounts: $("navAccounts"),
  runBadgeMirror: $("runBadgeMirror"),
  proxyRuntimeText: $("proxyRuntimeText"),
  taskBoard: $("taskBoard"),
  btnReloadTasks: $("btnReloadTasks"),
  taskSelectPage: $("taskSelectPage"),
  btnDeleteTasks: $("btnDeleteTasks"),
  btnClearTasks: $("btnClearTasks"),
  taskPrev: $("taskPrev"),
  taskNext: $("taskNext"),
  taskPager: $("taskPager"),
  mailboxList: $("mailboxList"),
  mailSearch: $("mailSearch"),
  mailListType: $("mailListType"),
  btnReloadMailboxes: $("btnReloadMailboxes"),
  mailSelectPage: $("mailSelectPage"),
  btnDeleteMailboxes: $("btnDeleteMailboxes"),
  mailPrev: $("mailPrev"),
  mailNext: $("mailNext"),
  mailPager: $("mailPager"),
  accSelectPage: $("accSelectPage"),
  btnDownloadAccounts: $("btnDownloadAccounts"),
  btnDownloadAllAccounts: $("btnDownloadAllAccounts"),
  btnHealthAccounts: $("btnHealthAccounts"),
  btnHealthAllAccounts: $("btnHealthAllAccounts"),
  btnDeleteAccounts: $("btnDeleteAccounts"),
  btnDeleteAllAccounts: $("btnDeleteAllAccounts"),
  mailModal: $("mailModal"),
  mailModalTitle: $("mailModalTitle"),
  mailModalMeta: $("mailModalMeta"),
  mailModalFrame: $("mailModalFrame"),
  btnCloseMailModal: $("btnCloseMailModal"),
};

let currentConfig = null;
let es = null;
let esReconnectTimer = null;
let esFailures = 0;
let logSnapshot = [];
let logsSyncing = false;
let lastTaskRefreshAt = 0;
let lastAccountRefreshAt = 0;
/** When true, periodic status refresh must not overwrite fingerprint form fields. */
let fpDirty = false;
let proxyDirty = false;
let c2aDirty = false;
let mailboxCache = [];
let mailboxPage = 1;
const MAIL_PAGE_SIZE = 10;
let taskCache = [];
let taskPage = 1;
const TASK_PAGE_SIZE = 5;
const mailMessageCache = new Map();

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: authHeaders({ "Content-Type": "application/json", ...(opts.headers || {}) }),
    ...opts,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.status === 401) {
    location.replace("/login");
    throw new Error("未登录或会话已过期");
  }
  if (!res.ok) {
    const detail =
      (data && (data.detail || data.message || data.error)) || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function setMsg(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

function applyJobBadge(job) {
  if (!job) return;
  const badge = els.runBadge;
  let text = "空闲";
  let cls = "badge idle";
  if (job.running) {
    text = job.stop_requested ? "停止中…" : "运行中";
    cls = "badge run";
  } else {
    text = "空闲";
    cls = "badge idle";
  }
  badge.className = cls;
  badge.innerHTML = `<i></i>${esc(text)}`;
  if (els.runBadgeMirror) {
    els.runBadgeMirror.textContent = text;
    els.runBadgeMirror.className = job.running ? "tag ok" : "tag";
  }
  if (els.stSuccess) {
    const ok = job.success_count || 0;
    const tot = job.total || 0;
    els.stSuccess.textContent = tot > 0 ? `${ok}/${tot}` : String(ok);
  }
  if (els.stFailed) {
    const fail = job.fail_count || 0;
    const tot = job.total || 0;
    els.stFailed.textContent = tot > 0 ? `${fail}/${tot}` : String(fail);
  }

  els.btnStart.disabled = !!job.running;
  els.btnStop.disabled = !job.running || !!job.stop_requested;
  els.btnStart.setAttribute("aria-busy", job.running ? "true" : "false");

}

function fillFingerprintForm(fp, reg) {
  if (fp.browser) els.fpBrowser.value = fp.browser;
  const cfx = fp.camoufox || {};
  const osValue = Array.isArray(cfx.os) ? cfx.os.join(",") : (cfx.os || "windows");
  els.fpCfxOs.value = osValue;
  if (!els.fpCfxOs.value) els.fpCfxOs.value = "windows";
  els.fpCfxLocale.value = cfx.locale || "";
  els.fpCfxHumanize.value = cfx.humanize ?? 0.15;
  els.fpCfxHeadless.checked = cfx.headless !== false;
  els.fpCfxGeoip.checked = cfx.geoip !== false;
  syncFingerprintOptions();
  if (reg.timeout) els.regTimeout.value = reg.timeout;
  if (reg.max_transient_retries != null) els.regRetries.value = reg.max_transient_retries;
}

function providerOf(cfg, type) {
  const providers =
    (cfg && cfg.mail && Array.isArray(cfg.mail.providers) && cfg.mail.providers) || [];
  return (
    providers.find((p) => p && (p.type === type || (type === "icloud_api" && ["icloud", "hide_my_email"].includes(p.type)))) ||
    null
  );
}

function fillConfig(cfg) {
  currentConfig = cfg;
  const fp = cfg.fingerprint || {};
  const reg = cfg.registration || {};
  const proxy = cfg.proxy || {};

  if (!fpDirty) {
    fillFingerprintForm(fp, reg);
  }
  if (!els.fTotal.dataset.touched) els.fTotal.value = reg.total || 1;
  if (!els.fConc.dataset.touched) els.fConc.value = reg.threads || 1;
  if (els.fEngine && !els.fEngine.dataset.touched) els.fEngine.value = reg.engine || "browser";

  const outlook = providerOf(cfg, "outlook_token") || {};
  const icloud = providerOf(cfg, "icloud_api") || {};

  if (document.activeElement !== els.mailboxesOutlook) {
    els.mailboxesOutlook.value = outlook.mailboxes || "";
  }
  els.mailMode.value = (outlook.mode || "imap").toLowerCase();

  if (document.activeElement !== els.mailboxesIcloud) {
    els.mailboxesIcloud.value = icloud.mailboxes || "";
  }
  els.icloudEnable.checked = icloud.enable !== false;
  if (document.activeElement !== els.icloudApiBase) {
    els.icloudApiBase.value = icloud.api_base || "https://icloud.xbovo.online";
  }

  if (!proxyDirty) {
    els.pxEnabled.checked = !!proxy.enabled;
    if (els.pxMode) els.pxMode.value = proxy.mode || "single_proxy";
    els.pxUrl.value = proxy.url || "";
    if (els.pxResourceUrl) els.pxResourceUrl.value = proxy.resource_url || "";
    const clearance = proxy.clearance || {};
    if (els.pxClearanceMode) els.pxClearanceMode.value = clearance.mode || "none";
    if (els.pxFlareUrl) els.pxFlareUrl.value = clearance.flaresolverr_url || "";
    if (els.pxFlareProxy) els.pxFlareProxy.value = clearance.proxy_url || "";
    els.pxFile.value = proxy.pool_file || "data/proxy_pool.txt";
    els.pxRotate.checked = proxy.rotate_per_task !== false;
  }

  if (!c2aDirty) fillC2aForm(cfg.chatgpt2api || {});
}

function fillC2aForm(c2a) {
  if (!els.c2aUrl) return;
  const c = c2a || {};
  if (els.c2aEnabled) els.c2aEnabled.checked = !!c.enabled;
  if (els.c2aAuto) els.c2aAuto.checked = c.auto_upload !== false;
  if (document.activeElement !== els.c2aUrl) {
    els.c2aUrl.value = c.url || "https://chatgpt2api.xbovo.online/";
  }
  // only overwrite key when field empty / not focused (avoid wiping while typing)
  if (els.c2aKey && document.activeElement !== els.c2aKey) {
    if (c.key) {
      els.c2aKey.value = c.key;
    } else if (c.has_key && !els.c2aKey.dataset.touched) {
      els.c2aKey.placeholder = c.key_masked ? `已保存 ${c.key_masked}（留空不修改）` : "已保存（留空不修改）";
      els.c2aKey.value = "";
    } else if (!els.c2aKey.dataset.touched) {
      els.c2aKey.value = "";
    }
  }
}

function fmtProvMeta(p) {
  if (!p) return "—";
  return `可用 ${p.available || 0} / 已用 ${p.used || 0} / 失败 ${p.failed || 0} / 占用 ${p.in_use || 0} / 无效 ${p.token_invalid || 0}`;
}

function applyStatus(data) {
  if (!data) return;
  const mail = data.mail || {};
  const proxy = data.proxy || {};
  const job = data.job || {};

  els.stAvail.textContent = mail.available ?? "—";
  if (els.stSplitAvail) els.stSplitAvail.textContent = mail.split_available ?? "—";
  els.stMailTotal.textContent = mail.total ?? "—";
  // Use the server's browser-safe concurrency recommendation.
  if (!els.fConc.dataset.touched && data.suggested_concurrency != null) {
    const s = Number(data.suggested_concurrency) || 0;
    if (s > 0) els.fConc.value = s;
  }
  if (!els.fTotal.dataset.touched && data.suggested_total != null) {
    const s = Number(data.suggested_total) || 0;
    if (s > 0) els.fTotal.value = s;
  }
  const accounts = data.accounts || {};
  const accountTotal = accounts.total ?? data.accounts_count ?? "—";
  els.stAccounts.textContent = accountTotal;
  if (els.stAccountsUsable) els.stAccountsUsable.textContent = accounts.usable ?? "—";
  if (els.stAccountsDeactivated) els.stAccountsDeactivated.textContent = accounts.deactivated ?? "—";
  if (els.navAccounts) els.navAccounts.textContent = accountTotal;
  els.stProxy.textContent = proxy.enabled
    ? `${proxy.pool_count || 0} 条`
    : "关闭";
  if (els.proxyRuntimeText) {
    els.proxyRuntimeText.textContent = proxy.enabled
      ? `${proxy.pool_count || 0} 条 · ${proxy.rotate_per_task === false ? "固定" : "轮换"}`
      : "本地直连";
  }

  const providers = Array.isArray(mail.providers) ? mail.providers : [];
  const outlook = providers.find((p) => p.type === "outlook_token");
  const icloud = providers.find((p) => p.type === "icloud_api");
  if (els.mailMetaOutlook) {
    els.mailMetaOutlook.textContent = outlook
      ? `Outlook · ${fmtProvMeta(outlook)}`
      : "Outlook · 未配置";
  }
  if (els.mailMetaIcloud) {
    els.mailMetaIcloud.textContent = icloud
      ? `iCloud · ${fmtProvMeta(icloud)}`
      : "iCloud · 未配置";
  }
  if (els.mailMeta) {
    els.mailMeta.textContent = `合计 · 可用 ${mail.available || 0} / 总数 ${mail.total || 0} · ${mail.label || ""}`;
  }
  applyJobBadge(job);

  if (!fpDirty && data.fingerprint) {
    fillFingerprintForm(data.fingerprint, data.registration || {});
  } else if (!fpDirty && data.registration) {
    if (data.registration.timeout) els.regTimeout.value = data.registration.timeout;
    if (data.registration.max_transient_retries != null) {
      els.regRetries.value = data.registration.max_transient_retries;
    }
  }
  if (data.registration) {
    if (!els.fTotal.dataset.touched) els.fTotal.value = data.registration.total || els.fTotal.value;
    if (!els.fConc.dataset.touched) els.fConc.value = data.registration.threads || els.fConc.value;
  }
  if (data.proxy && !proxyDirty) {
    els.pxEnabled.checked = !!data.proxy.enabled;
    if (data.proxy.url != null) els.pxUrl.value = data.proxy.url;
    if (data.proxy.pool_file) els.pxFile.value = data.proxy.pool_file;
    els.pxRotate.checked = data.proxy.rotate_per_task !== false;
    if (!els.pxText.dataset.touched) {
      els.pxText.value = data.proxy.pool_text || (data.proxy.pool_preview || []).join("\n");
    }
  }
  if (data.chatgpt2api && !c2aDirty) {
    fillC2aForm(data.chatgpt2api);
  }
}

function appendLogs(lines) {
  if (!lines || !lines.length) return;
  const box = els.logBox;
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    const div = document.createElement("div");
    const low = line.toLowerCase();
    if (low.includes("error") || low.includes("fail") || low.includes("traceback")) {
      div.className = "line-err";
    } else if (low.includes("[ok]") || low.includes("success") || low.includes("complete")) {
      div.className = "line-ok";
    }
    div.textContent = line;
    frag.appendChild(div);
  }
  box.appendChild(frag);
  while (box.childNodes.length > 2500) {
    box.removeChild(box.firstChild);
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function logDelta(previous, current) {
  if (!previous.length) return current;
  const maxOverlap = Math.min(previous.length, current.length);
  for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
    if (previous.slice(-overlap).every((line, i) => line === current[i])) {
      return current.slice(overlap);
    }
  }
  return current;
}

function setLogStreamState(text, kind = "queue") {
  if (!els.logStreamBadge) return;
  els.logStreamBadge.textContent = text;
  els.logStreamBadge.className = `pill ${kind}`;
}

async function syncLogs() {
  if (logsSyncing) return;
  logsSyncing = true;
  try {
    const data = await api("/api/logs?limit=1000");
    const lines = Array.isArray(data.lines) ? data.lines : [];
    if (!els.logBox.dataset.seeded) {
      els.logBox.innerHTML = "";
      appendLogs(lines);
      els.logBox.dataset.seeded = "1";
    } else {
      appendLogs(logDelta(logSnapshot, lines));
    }
    logSnapshot = lines;
    if (!es || es.readyState !== EventSource.OPEN) setLogStreamState("轮询", "queue");
  } catch {
    if (!es || es.readyState !== EventSource.OPEN) setLogStreamState("断开", "failed");
  } finally {
    logsSyncing = false;
  }
}

async function loadAccounts() {
  try {
    const data = await api("/api/accounts?limit=1000");
    const items = data.items || [];
    if (!items.length) {
      els.accBody.innerHTML = `<tr><td colspan="10" class="muted">暂无数据</td></tr>`;
      return;
    }
    els.accBody.innerHTML = items
      .map((a) => {
        const tok =
          a.has_access_token || a.has_session_token
            ? `<span class="token-yes">有</span>`
            : `<span class="token-no">无</span>`;
        const health = a.is_deactivated
          ? statusPill("deactivated")
          : a.health_status
            ? statusPill(a.health_status)
          : `<span class="muted">未测</span>`;
        const quota = a.image_quota_unknown ? "未知" : (a.quota ?? "—");
        const browserNames = { camoufox: "Camoufox", local: "Local Chrome", obscura: "Obscura" };
        const method = String(a.registration_method || "").toLowerCase();
        const registration = method === "protocol" || method === "protocol_login"
          ? "协议"
          : method === "browser"
            ? `浏览器 · ${browserNames[String(a.browser_type || "").toLowerCase()] || a.browser_type || "未记录"}`
            : "历史记录";
        return `<tr>
          <td><input class="acc-check" type="checkbox" value="${esc(a.delete_key || "")}"></td>
          <td>${esc(a.email || "")}</td>
          <td>${a.has_password ? `<code class="secret-mask">${esc(a.password_masked || "已保存")}</code>` : `<span class="muted">无</span>`}</td>
          <td>${esc(registration)}</td>
          <td>${esc(a.plan_type || "")}</td>
          <td title="${esc(a.health_error || "")}">${health}</td>
          <td>${esc(quota)}</td>
          <td>${tok}</td>
          <td class="muted">${esc(shortProxy(a.proxy || ""))}</td>
          <td class="muted">${esc(fmtTime(a.registered_at))}</td>
        </tr>`;
      })
      .join("");
  } catch (e) {
    els.accBody.innerHTML = `<tr><td colspan="10" class="muted">加载失败: ${esc(e.message)}</td></tr>`;
  }
}

function statusPill(status, options = {}) {
  const s = String(status || "available").toLowerCase();
  const cls =
    s === "used" || s === "success" || s === "alive" ? "success" :
    s === "failed" || s === "token_invalid" || s === "invalid" || s === "deactivated" || s === "limited" || s === "error" ? "failed" :
    s === "in_use" || s === "running" ? "running" :
    s === "cancelled" ? "queue" : "queue";
  const labelMap = {
    available: "可用",
    used: "已用",
    failed: "失败",
    token_invalid: "Token无效",
    in_use: "占用中",
    running: "运行中",
    success: "成功",
    cancelled: "已取消",
    skipped: "已跳过",
    queued: "排队",
    alive: "正常",
    limited: "限流",
    invalid: "失效",
    deactivated: "已封号",
    error: "错误",
  };
  const extraClass = options.className ? ` ${options.className}` : "";
  const attrs = options.attrs ? ` ${options.attrs}` : "";
  const suffix = options.suffix || "";
  return `<span class="pill ${cls}${extraClass}"${attrs}>${esc(labelMap[s] || status || "可用")}${esc(suffix)}</span>`;
}

function taskClockEpoch(clock) {
  const match = String(clock || "").match(/^(\d{1,2}):(\d{2}):(\d{2})$/);
  if (!match) return 0;
  const date = new Date();
  date.setHours(Number(match[1]), Number(match[2]), Number(match[3]), 0);
  if (date.getTime() > Date.now() + 60000) date.setDate(date.getDate() - 1);
  return date.getTime() / 1000;
}

function taskElapsedSeconds(task, now = Date.now() / 1000) {
  const started = Number(task.started_at_epoch) || taskClockEpoch(task.started_at);
  if (!started) return 0;
  const terminal = ["success", "failed", "cancelled", "skipped"].includes(String(task.status || "").toLowerCase());
  const ended = terminal
    ? Number(task.finished_at_epoch) || taskClockEpoch(task.updated_at) || now
    : now;
  return Math.max(0, Math.floor(ended - started));
}

function taskStatusPill(task) {
  const started = Number(task.started_at_epoch) || taskClockEpoch(task.started_at);
  const ended = Number(task.finished_at_epoch) || taskClockEpoch(task.updated_at);
  const status = String(task.status || "queued").toLowerCase();
  return statusPill(status, {
    className: "task-elapsed",
    attrs: `data-status="${esc(status)}" data-started="${started}" data-ended="${ended}"`,
    suffix: `（${taskElapsedSeconds(task)}s）`,
  });
}

function updateTaskElapsed() {
  const now = Date.now() / 1000;
  const labels = {queued: "排队", running: "运行中", success: "成功", failed: "失败", cancelled: "已取消", skipped: "已跳过"};
  document.querySelectorAll(".task-elapsed").forEach((el) => {
    const status = el.dataset.status || "queued";
    const started = Number(el.dataset.started) || 0;
    const terminal = ["success", "failed", "cancelled", "skipped"].includes(status);
    const ended = terminal ? Number(el.dataset.ended) || now : now;
    const seconds = started ? Math.max(0, Math.floor(ended - started)) : 0;
    el.textContent = `${labels[status] || status}（${seconds}s）`;
  });
}

async function loadMailboxes() {
  if (!els.mailboxList) return;
  const type = (els.mailListType && els.mailListType.value) || "auto";
  const q = encodeURIComponent((els.mailSearch && els.mailSearch.value) || "");
  try {
    const data = await api(`/api/mailboxes/list?provider_type=${encodeURIComponent(type)}&q=${q}&limit=1000`);
    mailboxCache = data.items || [];
    if (mailboxPage > Math.max(1, Math.ceil(mailboxCache.length / MAIL_PAGE_SIZE))) mailboxPage = 1;
    renderMailboxes();
  } catch (e) {
    els.mailboxList.innerHTML = `<div class="empty-card bad">邮箱列表加载失败：${esc(e.message)}</div>`;
  }
}

function renderMailboxes() {
  const items = mailboxCache || [];
  if (!items.length) {
    els.mailboxList.innerHTML = `<div class="empty-card">暂无邮箱。保存 Outlook / iCloud 邮箱池后会显示在这里。</div>`;
    if (els.mailPager) els.mailPager.textContent = "第 0/0 页 · 共 0 个";
    return;
  }
  const pages = Math.max(1, Math.ceil(items.length / MAIL_PAGE_SIZE));
  mailboxPage = Math.max(1, Math.min(mailboxPage, pages));
  const pageItems = items.slice((mailboxPage - 1) * MAIL_PAGE_SIZE, mailboxPage * MAIL_PAGE_SIZE);
  els.mailboxList.innerHTML = pageItems.map((m) => {
    const reg = m.registered ? `<span class="pill success">已注册</span>` : `<span class="pill queue">未注册</span>`;
    const provider = m.provider_type === "icloud_api" ? "iCloud" : "Outlook";
    return `<article class="mailbox-card" data-email="${esc(m.email)}" data-provider="${esc(m.provider_type)}">
        <div class="mailbox-select"><input class="mail-check" type="checkbox" data-email="${esc(m.email)}" data-provider="${esc(m.provider_type)}"></div>
        <div class="mailbox-main">
          <div class="mailbox-email">${esc(m.email)}</div>
          <div class="mailbox-sub">${esc(provider)} · ${esc(m.mode || "")} · 登录: ${esc(m.login_address || m.email)}</div>
          ${m.reason ? `<div class="mailbox-reason">${esc(m.reason)}</div>` : ""}
        </div>
        <div class="mailbox-tags">
          ${statusPill(m.state)}
          ${reg}
          ${m.enabled ? `<span class="pill queue">启用</span>` : `<span class="pill failed">禁用</span>`}
        </div>
        <div class="mailbox-actions">
          <button class="btn secondary sm mailbox-read" type="button">查看邮件</button>
        </div>
        <div class="mailbox-inline-viewer"></div>
      </article>`;
  }).join("");
  if (els.mailPager) els.mailPager.textContent = `第 ${mailboxPage}/${pages} 页 · 共 ${items.length} 个 · 每页 ${MAIL_PAGE_SIZE} 个`;
  if (els.mailPrev) els.mailPrev.disabled = mailboxPage <= 1;
  if (els.mailNext) els.mailNext.disabled = mailboxPage >= pages;
  if (els.mailSelectPage) els.mailSelectPage.checked = false;
}

function renderMailboxMessages(data, providerType) {
  const items = data.items || [];
  if (data.ok === false) {
    return `<div class="empty-card bad">${esc(data.error || "读取邮箱失败")}</div>`;
  }
  if (!items.length) {
    return `<div class="empty-card">没有读到邮件。</div>`;
  }
  return items.map((m, idx) => `<article class="message-card compact-message">
    <div class="message-title">
      <b>${esc(m.subject || "(无主题)")}</b>
      ${m.code ? `<span class="pill success">验证码 ${esc(m.code)}</span>` : ""}
    </div>
    <div class="message-meta">${esc(m.sender || "未知发件人")} · ${esc(fmtTime(m.received_at))}</div>
    <div class="message-actions"><button class="btn secondary sm mail-open-full" type="button" data-idx="${idx}">查看原文</button></div>
  </article>`).join("");
}

async function openMailboxMessages(card, email, providerType) {
  const viewer = card && card.querySelector(".mailbox-inline-viewer");
  const btn = card && card.querySelector(".mailbox-read");
  if (!viewer) return;
  if (viewer.classList.contains("active") && viewer.dataset.loaded === "1") {
    viewer.classList.remove("active");
    if (btn) btn.textContent = "查看邮件";
    return;
  }
  viewer.classList.add("active");
  viewer.dataset.loaded = "0";
  if (btn) btn.textContent = "收起邮件";
  viewer.innerHTML = `<div class="inline-viewer-head">
    <div><b>邮件预览 · ${esc(email)}</b><p class="hint">正在读取最近邮件…</p></div>
  </div><div class="message-list"><div class="empty-card">正在读取最近邮件…</div></div>`;
  try {
    const data = await api("/api/mailbox/messages", {
      method: "POST",
      body: JSON.stringify({ email, provider_type: providerType, limit: 10 }),
    });
    const items = data.items || [];
    mailMessageCache.set(`${providerType}:${email}`, items);
    viewer.dataset.loaded = "1";
    viewer.innerHTML = `<div class="inline-viewer-head">
      <div><b>邮件预览 · ${esc(email)}</b><p class="hint">${esc(data.provider_type || providerType)} · ${esc(data.mode || "")} · ${items.length} 封</p></div>
      <button class="btn secondary sm mailbox-refresh-mail" type="button">重新读取</button>
    </div><div class="message-list">${renderMailboxMessages(data, providerType)}</div>`;
  } catch (e) {
    viewer.dataset.loaded = "1";
    viewer.innerHTML = `<div class="inline-viewer-head">
      <div><b>邮件预览 · ${esc(email)}</b><p class="hint">读取失败</p></div>
      <button class="btn secondary sm mailbox-refresh-mail" type="button">重试</button>
    </div><div class="message-list"><div class="empty-card bad">读取邮箱失败：${esc(e.message)}</div></div>`;
  }
}

function openMailModal(email, providerType, idx) {
  const items = mailMessageCache.get(`${providerType}:${email}`) || [];
  const m = items[Number(idx)] || {};
  if (!els.mailModal || !els.mailModalFrame) return;
  els.mailModalTitle.textContent = m.subject || "邮件原文";
  els.mailModalMeta.textContent = `${m.sender || "未知发件人"} · ${fmtTime(m.received_at)}${m.code ? " · 验证码 " + m.code : ""}`;
  const html = m.html && String(m.html).trim()
    ? String(m.html)
    : `<pre style="white-space:pre-wrap;font-family:ui-monospace,Menlo,Consolas,monospace;line-height:1.6;padding:20px">${esc(m.text || m.preview || "")}</pre>`;
  els.mailModalFrame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><base target="_blank"></head><body>${html}</body></html>`;
  els.mailModal.classList.add("active");
}

async function loadTasks() {
  if (!els.taskBoard) return;
  try {
    const data = await api("/api/tasks");
    taskCache = data.items || [];
    renderTasks();
  } catch (e) {
    els.taskBoard.innerHTML = `<div class="empty-card bad">任务名片加载失败：${esc(e.message)}</div>`;
  }
}

function renderTasks() {
  const items = taskCache || [];
  if (!items.length) {
    els.taskBoard.innerHTML = `<div class="empty-card">暂无任务，点击开始运行后会生成任务名片。</div>`;
    if (els.taskPager) els.taskPager.textContent = "第 0/0 页 · 共 0 个";
    return;
  }
  const pages = Math.max(1, Math.ceil(items.length / TASK_PAGE_SIZE));
  taskPage = Math.max(1, Math.min(taskPage, pages));
  const pageItems = items.slice((taskPage - 1) * TASK_PAGE_SIZE, taskPage * TASK_PAGE_SIZE);
  els.taskBoard.innerHTML = pageItems.map((t) => {
      const steps = Array.isArray(t.steps) ? t.steps.slice(-8) : [];
      return `<article class="task-card ${esc(t.status || "queued")}">
        <div class="task-head">
          <input class="task-check" type="checkbox" value="${esc(t.index)}">
          <div>
            <div class="task-name">#${esc(t.index)} · ${esc(t.email || "等待邮箱")}</div>
            <div class="task-sub">${esc(t.provider || "provider?")} · 代理 ${esc(t.proxy || "none")} ${t.pid ? "· PID " + esc(t.pid) : ""}</div>
          </div>
          ${taskStatusPill(t)}
        </div>
        <div class="task-steps">
          ${steps.length ? steps.map((s) => `<div class="task-step"><span>${esc(s.time || "")}</span>${esc(s.text || "")}</div>`).join("") : `<div class="task-step muted">等待步骤更新…</div>`}
        </div>
      </article>`;
    }).join("");
  if (els.taskPager) els.taskPager.textContent = `第 ${taskPage}/${pages} 页 · 共 ${items.length} 个 · 每页 ${TASK_PAGE_SIZE} 个`;
  if (els.taskPrev) els.taskPrev.disabled = taskPage <= 1;
  if (els.taskNext) els.taskNext.disabled = taskPage >= pages;
  if (els.taskSelectPage) els.taskSelectPage.checked = false;
}

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shortProxy(p) {
  if (!p) return "—";
  try {
    const u = p.includes("://") ? new URL(p) : null;
    if (u) return u.host;
  } catch {}
  return p.length > 28 ? p.slice(0, 28) + "…" : p;
}

function fmtTime(t) {
  if (!t) return "—";
  try {
    return new Date(t).toLocaleString();
  } catch {
    return t;
  }
}

function checkedValues(selector) {
  return Array.from(document.querySelectorAll(selector))
    .filter((x) => x.checked)
    .map((x) => x.value)
    .filter(Boolean);
}

async function postDownload(path, body, filename) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: authHeaders({"Content-Type": "application/json"}),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function deleteSelectedAccounts(deleteAll = false) {
  const keys = checkedValues(".acc-check");
  if (!deleteAll && !keys.length) return setMsg(els.jobMsg, "先选择账号", "err");
  if (!confirm(deleteAll ? "确认删除全部账号记录？" : `确认删除选中的 ${keys.length} 条账号？`)) return;
  const data = await api("/api/accounts/delete", {method: "POST", body: JSON.stringify({keys, delete_all: deleteAll})});
  setMsg(els.jobMsg, `已删除账号 ${data.removed || 0} 条`, "ok");
  await loadAccounts();
  api("/api/status").then(applyStatus).catch(() => {});
}

async function downloadAccounts(downloadAll = false) {
  const keys = checkedValues(".acc-check");
  if (!downloadAll && !keys.length) return setMsg(els.jobMsg, "先选择账号", "err");
  if (downloadAll) {
    window.location.href = withTokenQuery("/api/accounts/download-all");
    return;
  }
  await postDownload("/api/accounts/download", {keys, delete_all: downloadAll}, downloadAll ? "registered_accounts.all.json" : "registered_accounts.selected.json");
}

async function deleteSelectedMailboxes(deleteAll = false) {
  const items = Array.from(document.querySelectorAll(".mail-check"))
    .filter((x) => x.checked)
    .map((x) => ({email: x.dataset.email || "", provider_type: x.dataset.provider || ""}));
  if (!deleteAll && !items.length) return setMsg(els.jobMsg, "先选择邮箱", "err");
  if (!confirm(deleteAll ? "确认删除当前筛选类型下的全部邮箱？" : `确认删除选中的 ${items.length} 个邮箱？`)) return;
  const data = await api("/api/mailboxes/delete", {
    method: "POST",
    body: JSON.stringify({items, delete_all: deleteAll, provider_type: (els.mailListType && els.mailListType.value) || "auto"}),
  });
  setMsg(els.jobMsg, `已删除邮箱 ${data.removed || 0} 个`, "ok");
  await refreshAll();
}

async function deleteSelectedTasks(deleteAll = false) {
  const indexes = checkedValues(".task-check").map((x) => Number(x)).filter((x) => Number.isFinite(x));
  if (!deleteAll && !indexes.length) return setMsg(els.jobMsg, "先选择任务名片", "err");
  const data = await api("/api/tasks/delete", {method: "POST", body: JSON.stringify({indexes, delete_all: deleteAll})});
  setMsg(els.jobMsg, `已删除任务名片 ${data.removed || 0} 个`, "ok");
  await loadTasks();
}

async function refreshAll() {
  const [status, conf] = await Promise.all([
    api("/api/status"),
    api("/api/config"),
  ]);
  applyStatus(status);
  if (conf && conf.config) {
    const oWas = els.mailboxesOutlook.value;
    const iWas = els.mailboxesIcloud.value;
    fillConfig(conf.config);
    if (document.activeElement === els.mailboxesOutlook) {
      els.mailboxesOutlook.value = oWas;
    }
    if (document.activeElement === els.mailboxesIcloud) {
      els.mailboxesIcloud.value = iWas;
    }
  }
  await loadAccounts();
  await loadMailboxes();
  await loadTasks();
  await syncLogs();
}

function activeViewId() {
  const active = document.querySelector(".view.active");
  return active ? active.id : "";
}

function throttledLoadTasks(ms = 2500) {
  const now = Date.now();
  if (now - lastTaskRefreshAt < ms) return;
  lastTaskRefreshAt = now;
  loadTasks().catch(() => {});
}

function throttledLoadAccounts(ms = 10000) {
  const now = Date.now();
  if (now - lastAccountRefreshAt < ms) return;
  lastAccountRefreshAt = now;
  loadAccounts().catch(() => {});
}

function connectSSE() {
  if (esReconnectTimer) {
    clearTimeout(esReconnectTimer);
    esReconnectTimer = null;
  }
  if (es) {
    try { es.close(); } catch {}
  }
  setLogStreamState("连接中", "running");
  es = new EventSource(withTokenQuery("/api/logs/stream"));
  es.onopen = () => {
    esFailures = 0;
    setLogStreamState("实时", "success");
    syncLogs().catch(() => {});
  };
  es.addEventListener("log", (ev) => {
    try {
      const data = JSON.parse(ev.data);
      const lines = Array.isArray(data.lines) ? data.lines : [];
      const fresh = logDelta(logSnapshot, lines);
      appendLogs(fresh);
      logSnapshot.push(...fresh);
      if (logSnapshot.length > 1000) logSnapshot = logSnapshot.slice(-1000);
    } catch {}
  });
  es.addEventListener("status", (ev) => {
    try {
      const job = JSON.parse(ev.data);
      applyJobBadge(job);
      // refresh task cards more eagerly while job runs
      if (job.running || job.success_count || job.fail_count) {
        throttledLoadTasks(job.running ? 2500 : 6000);
      }
    } catch {}
  });

  es.onerror = () => {
    try { es.close(); } catch {}
    es = null;
    esFailures += 1;
    const delay = Math.min(30000, 1000 * (2 ** Math.min(esFailures - 1, 5)));
    setLogStreamState(`重连 ${Math.ceil(delay / 1000)}s`, "failed");
    syncLogs().catch(() => {});
    esReconnectTimer = setTimeout(connectSSE, delay);
  };
}

async function startJob() {
  setMsg(els.jobMsg, "启动中…");
  els.btnStart.disabled = true;
  try {
    const body = {
      total: Number(els.fTotal.value || 1),
      concurrency: Number(els.fConc.value || 1),
      keep_on_fail: !!els.fKeep.checked,
      email: (els.fEmail.value || "").trim() || null,
      mail_type: (els.fMailType && els.fMailType.value) || "auto",
      engine: (els.fEngine && els.fEngine.value) || "browser",
    };
    const data = await api("/api/job/start", {
      method: "POST",
      body: JSON.stringify(body),
    });
    applyJobBadge(data.job);
    setMsg(els.jobMsg, "任务已启动", "ok");
    els.logBox.dataset.seeded = "1";
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
    els.btnStart.disabled = false;
  }
}

async function stopJob() {
  els.btnStop.disabled = true;
  try {
    const data = await api("/api/job/stop", { method: "POST", body: "{}" });
    applyJobBadge(data.job);
    setMsg(els.jobMsg, "正在立即停止…（取消任务并关闭浏览器）", "ok");
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
    els.btnStop.disabled = false;
  }
}

async function healthAccounts(all = false) {
  const keys = all ? [] : checkedValues(".acc-check");
  if (!all && !keys.length) {
    setMsg(els.jobMsg, "请先选择要测活的账号", "err");
    return;
  }
  if (!confirm(all ? "确认测活全部账号？" : `确认测活选中的 ${keys.length} 个账号？`)) return;
  setMsg(els.jobMsg, "账号测活中…");
  try {
    const data = await api("/api/accounts/health", {
      method: "POST",
      body: JSON.stringify({keys, delete_all: all}),
    });
    setMsg(
      els.jobMsg,
      `测活完成 · 请求 ${data.requested || 0} 条 · 实际检测 ${data.checked || 0} 条 · 跳过已封号 ${data.skipped_known_deactivated || 0} 条 · 可用 ${data.usable || 0} · 待续期 ${data.reauth_required || 0} · 其它失效 ${data.invalid_other || 0} · 错误 ${data.error || 0}`,
      data.error ? "err" : "ok"
    );
    await loadAccounts();
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
  }
}

async function saveMailOutlook() {
  try {
    const data = await api("/api/mailboxes", {
      method: "PUT",
      body: JSON.stringify({
        provider_type: "outlook_token",
        mailboxes: els.mailboxesOutlook.value,
        mode: els.mailMode.value,
      }),
    });
    const p = (data.mail?.providers || []).find((x) => x.type === "outlook_token");
    setMsg(
      els.jobMsg,
      `Outlook 池已保存 · ${p ? p.total : data.mail?.total ?? "?"} 条`,
      "ok"
    );
    await refreshAll();
    await loadMailboxes();
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
  }
}

async function saveMailIcloud() {
  try {
    const data = await api("/api/mailboxes", {
      method: "PUT",
      body: JSON.stringify({
        provider_type: "icloud_api",
        mailboxes: els.mailboxesIcloud.value,
        api_base: els.icloudApiBase.value || "https://icloud.xbovo.online",
        enable: !!els.icloudEnable.checked,
      }),
    });
    const p = (data.mail?.providers || []).find((x) => x.type === "icloud_api");
    setMsg(
      els.jobMsg,
      `iCloud 池已保存 · ${p ? p.total : "?"} 条 · ${els.icloudEnable.checked ? "启用" : "禁用"}`,
      "ok"
    );
    await refreshAll();
    await loadMailboxes();
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
  }
}

async function saveProxy() {
  try {
    const data = await api("/api/proxy", {
      method: "PUT",
      body: JSON.stringify({
        enabled: !!els.pxEnabled.checked,
        mode: (els.pxMode && els.pxMode.value) || "single_proxy",
        url: els.pxUrl.value || "",
        resource_url: (els.pxResourceUrl && els.pxResourceUrl.value) || "",
        pool_file: els.pxFile.value || "data/proxy_pool.txt",
        pool_text: els.pxText.value,
        rotate_per_task: !!els.pxRotate.checked,
        clearance: {
          enabled: !!(els.pxClearanceMode && els.pxClearanceMode.value !== "none"),
          mode: (els.pxClearanceMode && els.pxClearanceMode.value) || "none",
          flaresolverr_url: (els.pxFlareUrl && els.pxFlareUrl.value) || "",
          proxy_url: (els.pxFlareProxy && els.pxFlareProxy.value) || "",
          timeout_sec: 60,
          refresh_interval: 3600,
        },
      }),
    });
    proxyDirty = false;
    els.pxText.dataset.touched = "";
    setMsg(els.jobMsg, `代理已保存 · 池 ${data.proxy?.pool_count ?? 0} 条`, "ok");
    await refreshAll();
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
  }
}

async function saveC2a() {
  try {
    const data = await api("/api/chatgpt2api", {
      method: "PUT",
      body: JSON.stringify({
        enabled: !!(els.c2aEnabled && els.c2aEnabled.checked),
        url: (els.c2aUrl && els.c2aUrl.value) || "https://chatgpt2api.xbovo.online/",
        key: (els.c2aKey && els.c2aKey.value) || "",
        auto_upload: els.c2aAuto ? !!els.c2aAuto.checked : true,
      }),
    });
    c2aDirty = false;
    if (els.c2aKey) els.c2aKey.dataset.touched = "";
    if (data.chatgpt2api) fillC2aForm(data.chatgpt2api);
    setMsg(
      els.c2aMsg || els.jobMsg,
      `chatgpt2api 已保存 · ${data.chatgpt2api?.enabled ? "启用" : "关闭"} · ${data.chatgpt2api?.url || ""}`,
      "ok"
    );
    await refreshAll();
  } catch (e) {
    setMsg(els.c2aMsg || els.jobMsg, e.message, "err");
  }
}

async function testC2a() {
  setMsg(els.c2aMsg || els.jobMsg, "测试连接中…");
  try {
    const data = await api("/api/chatgpt2api/test", {
      method: "POST",
      body: JSON.stringify({
        url: (els.c2aUrl && els.c2aUrl.value) || "",
        key: (els.c2aKey && els.c2aKey.value) || "",
      }),
    });
    setMsg(els.c2aMsg || els.jobMsg, data.message || (data.ok ? "连接成功" : "连接失败"), data.ok ? "ok" : "err");
  } catch (e) {
    setMsg(els.c2aMsg || els.jobMsg, e.message, "err");
  }
}

async function uploadAllC2a() {
  if (!confirm("确认把本地已有账号批量上传到 chatgpt2api？")) return;
  setMsg(els.c2aMsg || els.jobMsg, "上传中…");
  try {
    const data = await api("/api/chatgpt2api/upload-all", {
      method: "POST",
      body: JSON.stringify({
        url: (els.c2aUrl && els.c2aUrl.value) || "",
        key: (els.c2aKey && els.c2aKey.value) || "",
      }),
    });
    const msg = data.message || "";
    setMsg(
      els.c2aMsg || els.jobMsg,
      data.ok
        ? `上传完成 · 本地 ${data.count || 0} 个 · ${msg}`
        : `上传失败 · ${msg}`,
      data.ok ? "ok" : "err"
    );
  } catch (e) {
    setMsg(els.c2aMsg || els.jobMsg, e.message, "err");
  }
}

async function saveFingerprint() {
  try {
    const conf = (await api("/api/config")).config || {};
    conf.fingerprint = conf.fingerprint || {};
    conf.registration = conf.registration || {};
    conf.fingerprint.browser = els.fpBrowser.value;
    conf.fingerprint.camoufox = {
      headless: !!els.fpCfxHeadless.checked,
      os: els.fpCfxOs.value || "windows",
      locale: (els.fpCfxLocale.value || "").trim(),
      geoip: !!els.fpCfxGeoip.checked,
      humanize: Math.max(0, Math.min(1.5, Number(els.fpCfxHumanize.value || 0))),
    };
    conf.registration.timeout = Number(els.regTimeout.value || 480);
    conf.registration.max_transient_retries = Number(els.regRetries.value || 2);
    conf.registration.engine = (els.fEngine && els.fEngine.value) || "browser";
    conf.registration.total = Number(els.fTotal.value || 1);
    conf.registration.threads = Number(els.fConc.value || 1);
    const saved = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config: conf }),
    });
    fpDirty = false;
    if (saved && saved.config) {
      fillConfig(saved.config);
    }
    setMsg(
      els.jobMsg,
      `指纹 / 注册参数已保存 · browser=${(saved.config?.fingerprint?.browser) || els.fpBrowser.value}`,
      "ok"
    );
    await refreshAll();
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
  }
}

async function resetFailed() {
  try {
    const data = await api("/api/mail/reset-failed", { method: "POST", body: "{}" });
    setMsg(els.jobMsg, `已释放 ${data.reset || 0} 个邮箱状态`, "ok");
    await refreshAll();
  } catch (e) {
    setMsg(els.jobMsg, e.message, "err");
  }
}

function markFpDirty() {
  fpDirty = true;
}

function syncFingerprintOptions() {
  const show = els.fpBrowser && els.fpBrowser.value === "camoufox";
  document.querySelectorAll(".camoufox-option").forEach((el) => {
    el.hidden = !show;
  });
}

function onMailTypeChange() {
  // when user picks icloud/outlook, re-suggest based on that pool's available
  // status endpoint is aggregate; we still clamp on start server-side
  els.fConc.dataset.touched = "";
  els.fTotal.dataset.touched = "";
  api("/api/status").then(applyStatus).catch(() => {});
}

function showView(targetId, pushHash = true) {
  const id = (targetId || "task").replace(/^#/, "") || "task";
  const target = document.getElementById(id) || document.getElementById("task");
  if (!target) return;

  const viewMeta = {
    task: ["注册任务", "创建、调度并观察账号注册任务"],
    fingerprint: ["指纹浏览器", "管理浏览器指纹与注册运行参数"],
    mail: ["邮箱池", "维护 Outlook 与 iCloud 邮箱资源"],
    proxy: ["代理设置", "配置注册出站、资源代理与清障服务"],
    chatgpt2api: ["chatgpt2api", "管理账号自动上传与连接状态"],
    logs: ["实时日志", "查看任务执行过程与异常信息"],
    accounts: ["账号记录", "管理、测活并导出已注册账号"],
  };
  const meta = viewMeta[target.id] || viewMeta.task;
  if (els.pageTitle) els.pageTitle.textContent = meta[0];
  if (els.pageSubtitle) els.pageSubtitle.textContent = meta[1];
  document.title = `${meta[0]} · GPT Register FP`;

  document.querySelectorAll(".view").forEach((el) => {
    el.classList.toggle("active", el === target);
  });
  document.querySelectorAll(".nav a[href^='#']").forEach((a) => {
    a.classList.toggle("active", a.getAttribute("href") === `#${target.id}`);
  });
  if (pushHash && window.location.hash !== `#${target.id}`) {
    history.replaceState(null, "", `#${target.id}`);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function bind() {
  const btnLogout = document.getElementById("btnLogout");
  if (btnLogout) btnLogout.addEventListener("click", () => logout());

  els.btnStart.addEventListener("click", startJob);
  if (els.fMailType) els.fMailType.addEventListener("change", onMailTypeChange);

  els.btnStop.addEventListener("click", stopJob);
  els.btnSaveMailOutlook.addEventListener("click", saveMailOutlook);
  els.btnSaveMailIcloud.addEventListener("click", saveMailIcloud);
  els.btnSaveProxy.addEventListener("click", saveProxy);
  if (els.btnSaveC2a) els.btnSaveC2a.addEventListener("click", saveC2a);
  if (els.btnTestC2a) els.btnTestC2a.addEventListener("click", testC2a);
  if (els.btnUploadC2a) els.btnUploadC2a.addEventListener("click", uploadAllC2a);
  if (els.c2aKey) {
    els.c2aKey.addEventListener("input", () => {
      els.c2aKey.dataset.touched = "1";
      c2aDirty = true;
    });
  }
  els.btnSaveFp.addEventListener("click", saveFingerprint);
  els.btnResetFailed.addEventListener("click", resetFailed);
  els.btnReloadAcc.addEventListener("click", loadAccounts);
  if (els.btnReloadAccBottom) els.btnReloadAccBottom.addEventListener("click", loadAccounts);
  if (els.btnReloadTasks) els.btnReloadTasks.addEventListener("click", loadTasks);
  if (els.btnDeleteTasks) els.btnDeleteTasks.addEventListener("click", () => deleteSelectedTasks(false));
  if (els.btnClearTasks) els.btnClearTasks.addEventListener("click", () => deleteSelectedTasks(true));
  if (els.taskSelectPage) els.taskSelectPage.addEventListener("change", () => {
    document.querySelectorAll(".task-check").forEach((x) => { x.checked = els.taskSelectPage.checked; });
  });
  if (els.taskPrev) els.taskPrev.addEventListener("click", () => { taskPage = Math.max(1, taskPage - 1); renderTasks(); });
  if (els.taskNext) els.taskNext.addEventListener("click", () => { taskPage += 1; renderTasks(); });
  if (els.btnReloadMailboxes) els.btnReloadMailboxes.addEventListener("click", loadMailboxes);
  if (els.btnDeleteMailboxes) els.btnDeleteMailboxes.addEventListener("click", () => deleteSelectedMailboxes(false));
  if (els.mailSelectPage) els.mailSelectPage.addEventListener("change", () => {
    document.querySelectorAll(".mail-check").forEach((x) => { x.checked = els.mailSelectPage.checked; });
  });
  if (els.mailPrev) els.mailPrev.addEventListener("click", () => { mailboxPage = Math.max(1, mailboxPage - 1); renderMailboxes(); });
  if (els.mailNext) els.mailNext.addEventListener("click", () => { mailboxPage += 1; renderMailboxes(); });
  if (els.mailListType) els.mailListType.addEventListener("change", () => { mailboxPage = 1; loadMailboxes(); });
  if (els.mailSearch) els.mailSearch.addEventListener("input", () => {
    clearTimeout(els.mailSearch._timer);
    els.mailSearch._timer = setTimeout(() => { mailboxPage = 1; loadMailboxes(); }, 250);
  });
  if (els.accSelectPage) els.accSelectPage.addEventListener("change", () => {
    document.querySelectorAll(".acc-check").forEach((x) => { x.checked = els.accSelectPage.checked; });
  });
  if (els.btnDownloadAccounts) els.btnDownloadAccounts.addEventListener("click", () => downloadAccounts(false).catch((e) => setMsg(els.jobMsg, e.message, "err")));
  if (els.btnDownloadAllAccounts) els.btnDownloadAllAccounts.addEventListener("click", () => downloadAccounts(true).catch((e) => setMsg(els.jobMsg, e.message, "err")));
  if (els.btnHealthAccounts) els.btnHealthAccounts.addEventListener("click", () => healthAccounts(false));
  if (els.btnHealthAllAccounts) els.btnHealthAllAccounts.addEventListener("click", () => healthAccounts(true));
  if (els.btnDeleteAccounts) els.btnDeleteAccounts.addEventListener("click", () => deleteSelectedAccounts(false).catch((e) => setMsg(els.jobMsg, e.message, "err")));
  if (els.btnDeleteAllAccounts) els.btnDeleteAllAccounts.addEventListener("click", () => deleteSelectedAccounts(true).catch((e) => setMsg(els.jobMsg, e.message, "err")));
  if (els.mailboxList) {
    els.mailboxList.addEventListener("click", (ev) => {
      const full = ev.target.closest(".mail-open-full");
      if (full) {
        const card = full.closest(".mailbox-card");
        if (card) openMailModal(card.dataset.email || "", card.dataset.provider || "auto", full.dataset.idx || "0");
        return;
      }
      const btn = ev.target.closest(".mailbox-read, .mailbox-refresh-mail");
      if (!btn) return;
      const card = btn.closest(".mailbox-card");
      if (!card) return;
      const viewer = card.querySelector(".mailbox-inline-viewer");
      if (btn.classList.contains("mailbox-refresh-mail") && viewer) {
        viewer.dataset.loaded = "0";
      }
      openMailboxMessages(card, card.dataset.email || "", card.dataset.provider || "auto");
    });
  }
  if (els.btnCloseMailModal) {
    els.btnCloseMailModal.addEventListener("click", () => {
      els.mailModal.classList.remove("active");
      els.mailModalFrame.srcdoc = "";
    });
  }
  if (els.mailModal) {
    els.mailModal.addEventListener("click", (ev) => {
      if (ev.target === els.mailModal) {
        els.mailModal.classList.remove("active");
        els.mailModalFrame.srcdoc = "";
      }
    });
  }
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && els.mailModal && els.mailModal.classList.contains("active")) {
      els.mailModal.classList.remove("active");
      els.mailModalFrame.srcdoc = "";
      if (els.btnCloseMailModal) els.btnCloseMailModal.focus();
    }
  });
  els.btnRefresh.addEventListener("click", () => {
    fpDirty = false;
    proxyDirty = false;
    c2aDirty = false;
    refreshAll().catch(console.error);
  });
  els.btnClearLog.addEventListener("click", () => {
    els.logBox.innerHTML = "";
    els.logBox.dataset.seeded = "1";
    logSnapshot = logSnapshot.slice();
  });
  els.fTotal.addEventListener("input", () => { els.fTotal.dataset.touched = "1"; });
  els.fConc.addEventListener("input", () => { els.fConc.dataset.touched = "1"; });
  if (els.fEngine) els.fEngine.addEventListener("change", () => { els.fEngine.dataset.touched = "1"; });
  [els.pxEnabled, els.pxMode, els.pxUrl, els.pxResourceUrl, els.pxClearanceMode, els.pxFlareUrl, els.pxFlareProxy, els.pxFile, els.pxRotate, els.pxText].forEach((el) => {
    if (!el) return;
    const mark = () => {
      proxyDirty = true;
      if (el === els.pxText) els.pxText.dataset.touched = "1";
    };
    el.addEventListener("input", mark);
    el.addEventListener("change", mark);
  });
  [els.c2aEnabled, els.c2aAuto, els.c2aUrl].forEach((el) => {
    if (!el) return;
    el.addEventListener("input", () => { c2aDirty = true; });
    el.addEventListener("change", () => { c2aDirty = true; });
  });
  els.fpBrowser.addEventListener("change", syncFingerprintOptions);
  [
    "fpBrowser", "fpCfxOs",
    "fpCfxLocale", "fpCfxHumanize", "fpCfxHeadless", "fpCfxGeoip",
    "regTimeout", "regRetries",
  ].forEach((id) => {
    const el = els[id];
    if (!el) return;
    el.addEventListener("change", markFpDirty);
    el.addEventListener("input", markFpDirty);
  });
  syncFingerprintOptions();
  document.querySelectorAll("a[href^='#']").forEach((a) => {
    a.addEventListener("click", (ev) => {
      const target = a.getAttribute("href") || "#task";
      if (document.querySelector(`.view${target}`)) {
        ev.preventDefault();
        showView(target);
        if (target === "#logs") syncLogs().catch(() => {});
      }
    });
  });
  window.addEventListener("hashchange", () => showView(window.location.hash || "#task", false));
  showView(window.location.hash || "#task", false);
}

bind();
(async () => {
  const ok = await ensureAuthGate();
  if (!ok) return;
  connectSSE();
  refreshAll().catch((e) => setMsg(els.jobMsg, e.message, "err"));
  setInterval(() => {
    const view = activeViewId();
    if (view === "accounts") throttledLoadAccounts(10000);
    if (view === "task") throttledLoadTasks(6000);
    if (view === "logs" || !es || es.readyState !== EventSource.OPEN) {
      syncLogs().catch(() => {});
    }
    api("/api/status").then(applyStatus).catch(() => {});
  }, 6000);
  setInterval(updateTaskElapsed, 1000);
})();
