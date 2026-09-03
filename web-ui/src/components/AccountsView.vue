<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { 
  Check, 
  CheckSquare, 
  Clipboard, 
  ClipboardList, 
  Copy, 
  Download, 
  Eye, 
  EyeOff, 
  KeyRound, 
  Pencil, 
  Plus, 
  RefreshCw, 
  ShieldCheck, 
  Trash2, 
  Users,
  Smartphone,
  Upload,
  X
} from "@lucide/vue";
import AppPager from "./AppPager.vue";
import { api, jsonBody } from "../api.js";

const props = defineProps({ 
  accounts: { type: Array, default: () => [] }, 
  accountsLoading: Boolean, 
  accountLoadProgress: { type: Object, default: () => ({ loaded: 0, total: 0 }) }, 
  accountLoadError: { type: String, default: "" }, 
  groups: { type: Array, default: () => [] }, 
  proxyCountries: { type: Array, default: () => [] }, 
  busy: Boolean, 
  copyingAt: Boolean,
  codexOauthRunning: Boolean, 
  healthProgress: { type: Object, default: () => ({}) }, 
  reauthProgress: { type: Object, default: () => ({}) }, 
  trialProgress: { type: Object, default: () => ({}) } 
});

const emit = defineEmits([
  "refresh", "health", "reauth", "security-repair", "password-change", "totp-change", 
  "account-logs", "stop-reauth", "delete", "delete-invalid", "download", "copy-selected-at", "codex-oauth", "import-accounts", 
  "export-selected-at", "export-security", "create-group", "rename-group", "delete-group", 
  "move-group", "export-group", "copy-at", "copy-credential", "copy-value", "trial-check"
]);

const query = ref("");
const searchNeedle = ref("");
const statusFilter = ref("all");
const planFilter = ref("all");
const offerFilter = ref("all");
const groupFilter = ref("all");
const groupQuery = ref("");
const moveTarget = ref("");
const newGroupName = ref("");
const pageSize = ref(20);
const page = ref(1);
const selected = ref([]);
const reauthMode = ref("protocol");
const reauthConcurrency = ref(3);
const copiedFeedback = ref({});

function triggerCopied(key) {
  copiedFeedback.value[key] = true;
  setTimeout(() => {
    copiedFeedback.value[key] = false;
  }, 1500);
}

async function copyTextDirect(text, key, fallbackItem, fallbackField) {
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
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("clipboard copy failed");
    }
    triggerCopied(key);
    emit("copy-value", { text, successMessage: `已复制到剪贴板: ${text}` });
  } catch {
    if (fallbackItem && fallbackField) {
      emit("copy-credential", fallbackItem, fallbackField);
    }
  }
}

const healthPercent = computed(() => props.healthProgress.total ? Math.round((props.healthProgress.done / props.healthProgress.total) * 100) : (props.healthProgress.requested && !props.healthProgress.running ? 100 : 0));

const accountIndex = computed(() => {
  const rows = props.accounts || [];
  const invalidKeys = [];
  const reauthKeys = [];
  const protocolReauthKeys = [];
  const securityRepairKeys = [];
  const securityOperationKeys = [];
  const accessTokenKeys = new Set();
  let usable = 0;
  let deactivated = 0;
  let checked = 0;
  for (const item of rows) {
    const key = item?.delete_key;
    const health = String(item.health_status || "unknown").toLowerCase();
    const isDeactivated = !!item.is_deactivated;
    if (!isDeactivated && ["alive", "limited"].includes(health)) usable += 1;
    if (isDeactivated) deactivated += 1;
    if (health !== "unknown") checked += 1;
    if (!isDeactivated && health === "invalid" && key) invalidKeys.push(key);
    if (item.reauth_available && key) {
      reauthKeys.push(key);
      if (item.has_password && item.has_totp) protocolReauthKeys.push(key);
    }
    if (item.security_repair_available && key) securityRepairKeys.push(key);
    const passwordConfirmed = item.password_confirmed !== false
      && !!item.has_password
      && !(item.password_confirmed === undefined
        && !!item.security_repair_available
        && !!item.has_totp);
    if (!isDeactivated && passwordConfirmed && item.has_totp && key) securityOperationKeys.push(key);
    if (item.has_access_token && key) accessTokenKeys.add(key);
  }
  return {
    stats: { total: rows.length, usable, deactivated, checked },
    invalidKeys,
    reauthKeys,
    protocolReauthKeys,
    securityRepairKeys,
    securityOperationKeys,
    accessTokenKeys,
  };
});

const accountStats = computed(() => accountIndex.value.stats);
const accountLoadPercent = computed(() => {
  const total = Number(props.accountLoadProgress?.total || 0);
  const loaded = Number(props.accountLoadProgress?.loaded || props.accounts.length || 0);
  return total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
});
const accountLoadBlocked = computed(() => props.accountsLoading || !!props.accountLoadError);
const invalidKeys = computed(() => accountIndex.value.invalidKeys);
const invalidCount = computed(() => invalidKeys.value.length);
const reauthKeys = computed(() => accountIndex.value.reauthKeys);
const protocolReauthKeys = computed(() => accountIndex.value.protocolReauthKeys);
const reauthTargetKeys = computed(() => reauthMode.value === "protocol" ? protocolReauthKeys.value : reauthKeys.value);
const selectedReauthKeys = computed(() => {
  const available = new Set(reauthTargetKeys.value);
  return selected.value.filter((key) => available.has(key));
});
const securityRepairKeys = computed(() => accountIndex.value.securityRepairKeys);
const selectedSecurityRepairKeys = computed(() => {
  const available = new Set(securityRepairKeys.value);
  return selected.value.filter((key) => available.has(key));
});
const securityOperationKeys = computed(() => accountIndex.value.securityOperationKeys);
const selectedSecurityOperationKeys = computed(() => {
  const available = new Set(securityOperationKeys.value);
  return selected.value.filter((key) => available.has(key));
});
const selectedAccessTokenKeys = computed(() => {
  const available = accountIndex.value.accessTokenKeys;
  return selected.value.filter((key) => available.has(key));
});

let searchDebounceTimer = null;
watch(query, (val) => {
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    searchNeedle.value = String(val || "").trim().toLowerCase();
  }, 120);
});

const proxyCountriesByUrl = computed(() => {
  const map = new Map();
  for (const item of props.proxyCountries || []) {
    if (item?.url && item?.country) {
      map.set(normalizeProxyUrl(item.url), item.country);
    }
  }
  return map;
});

const proxyCountryCache = new Map();
let regionDisplayNames = null;
const countryLabels = computed(() => {
  const map = new Map();
  for (const item of props.proxyCountries || []) {
    if (item?.country && item?.label) {
      map.set(item.country, item.label);
    }
  }
  return map;
});

const searchTextByItem = computed(() => {
  if (!searchNeedle.value) return null;
  const index = new Map();
  for (const item of props.accounts || []) {
    const proxy = proxyDisplay(item.proxy);
    index.set(item, [
      item.email,
      item.account_id,
      item.group_name,
      item.plan_type,
      item.health_status,
      item.status,
      item.registration_state_label,
      statusName(item),
      item.proxy,
      proxy.label,
      proxy.country,
      item.source_type,
      item.registration_method,
      item.browser_type,
      registrationText(item),
      item.trial_eligibility_label,
      item.trial_eligibility_status,
    ].map((value) => String(value || "").toLowerCase()));
  }
  return index;
});

const reauthPercent = computed(() => props.reauthProgress.total ? Math.round((props.reauthProgress.done / props.reauthProgress.total) * 100) : 0);
const trialPercent = computed(() => props.trialProgress.total ? Math.round((props.trialProgress.done / props.trialProgress.total) * 100) : 0);
const reauthModeLabel = computed(() => props.reauthProgress.mode === "protocol" ? "协议" : "浏览器");
const reauthOperationLabel = computed(() => ({
  reauth: "重新登录",
  security_repair: "安全字段补全",
  password_change: "修改密码",
  totp_change: "修改 2FA",
}[props.reauthProgress.operation] || "账号操作"));

const totpCodes = ref({});
const totpLoading = ref({});
const visibleGroups = computed(() => {
  const needle = groupQuery.value.trim().toLowerCase();
  return needle
    ? props.groups.filter((item) => String(item.name || "").toLowerCase().includes(needle))
    : props.groups;
});
const activeGroup = computed(() => props.groups.find((item) => item.id === groupFilter.value) || null);

const filteredGroupKeys = computed(() => {
  if (!activeGroup.value) return [];
  return filtered.value.filter((item) => (item.group_id || "default") === activeGroup.value.id).map((item) => item.delete_key).filter(Boolean);
});
const filteredGroupAllSelected = computed(() => {
  if (!filteredGroupKeys.value.length) return false;
  const current = new Set(selected.value);
  return filteredGroupKeys.value.every((key) => current.has(key));
});

const filtered = computed(() => {
  const needle = searchNeedle.value;
  const status = statusFilter.value;
  const plan = planFilter.value;
  const offer = offerFilter.value;
  const group = groupFilter.value;
  const textIndex = searchTextByItem.value;

  return (props.accounts || []).filter((item) => {
    if (group !== "all" && (item.group_id || "default") !== group) return false;
    if (status !== "all") {
      const isDeactivated = !!item.is_deactivated;
      const normalizedStatus = isDeactivated ? "deactivated" : (item.health_status || item.status || "unknown").toLowerCase();
      if (status === "alive" && normalizedStatus !== "alive") return false;
      if (status === "limited" && normalizedStatus !== "limited") return false;
      if (status === "reauth_required" && normalizedStatus !== "reauth_required") return false;
      if (status === "deactivated" && !isDeactivated) return false;
      if (status === "invalid" && (isDeactivated || normalizedStatus !== "invalid")) return false;
      if (status === "error" && normalizedStatus !== "error") return false;
      if (status === "unknown" && normalizedStatus !== "unknown") return false;
    }
    if (plan !== "all" && planType(item) !== plan) return false;
    if (offer !== "all") {
      const hasOffer = !!(item.trial_eligibility_status === "eligible" || item.has_discount || item.has_offer);
      const isTrial = item.trial_eligibility_status === "eligible";
      const isDiscount = !!(item.has_discount || (item.has_offer && !isTrial));
      if (offer === "has" && !hasOffer) return false;
      if (offer === "trial" && !isTrial) return false;
      if (offer === "discount" && !isDiscount) return false;
      if (offer === "none" && hasOffer) return false;
    }
    if (!needle) return true;
    const values = textIndex?.get(item);
    return values ? values.some((val) => val.includes(needle)) : true;
  });
});

const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)));
const pageItems = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return filtered.value.slice(start, start + pageSize.value);
});

const allPageSelected = computed({
  get() {
    if (!pageItems.value.length) return false;
    return pageItems.value.every((item) => selected.value.includes(item.delete_key));
  },
  set(val) {
    const current = new Set(selected.value);
    for (const item of pageItems.value) {
      if (!item.delete_key) continue;
      if (val) current.add(item.delete_key);
      else current.delete(item.delete_key);
    }
    selected.value = [...current];
  },
});

function clearAllSelected() {
  selected.value = [];
}

function planType(item) {
  const plan = String(item.plan_type || "free").toLowerCase();
  return plan.includes("plus") ? "plus" : (plan.includes("team") ? "team" : "free");
}
function planName(item) {
  const plan = String(item.plan_type || "Free").toLowerCase();
  if (plan.includes("plus")) return "Plus";
  if (plan.includes("team")) return "Team";
  return "Free";
}
function statusName(item) {
  if (item.is_deactivated) return "已封号留档";
  const map = {
    alive: "可用",
    limited: "限流",
    reauth_required: "待续期",
    invalid: "失效",
    error: "错误",
    unknown: "未检测",
  };
  return map[String(item.health_status || item.status || "unknown").toLowerCase()] || "未检测";
}
function statusClass(item) {
  if (item.is_deactivated) return "deactivated";
  const s = String(item.health_status || item.status || "unknown").toLowerCase();
  return ["alive", "limited", "reauth_required", "invalid", "error"].includes(s) ? s : "unknown";
}
function trialLabel(item) {
  if (item.trial_eligibility_label) return item.trial_eligibility_label;
  if (item.trial_eligibility_status === "eligible") return "符合0元资格";
  if (item.trial_eligibility_status === "ineligible") return "不符合";
  if (item.trial_eligibility_status === "failed") return "检测失败";
  return "未检测";
}
function trialClass(item) {
  if (item.trial_eligibility_status === "eligible") return "alive";
  if (item.trial_eligibility_status === "ineligible") return "limited";
  if (item.trial_eligibility_status === "failed") return "error";
  return "unknown";
}
function registrationText(item) {
  return item.registration_state_label || (item.source_type === "imported" ? "外部导入" : (item.browser_type || "指纹注册"));
}
function quotaText(val) {
  return typeof val === "number" ? `${val} 次` : "-";
}
function mfaText(item) {
  if (item.mfa_enabled) return "已启用";
  if (item.has_totp) return "已存秘钥";
  return "未设置";
}

function totpEntry(item) {
  return totpCodes.value[item.delete_key] || null;
}
function totpDisplay(item) {
  const entry = totpEntry(item);
  if (entry?.code) return entry.code;
  if (entry?.error) return "获取失败";
  return totpLoading.value[item.delete_key] ? "获取中…" : "获取验证码";
}
async function handleTotpClick(item) {
  const entry = totpEntry(item);
  if (entry?.code) {
    copyTextDirect(entry.code, `totp_code_${item.delete_key}`);
    return;
  }
  totpLoading.value[item.delete_key] = true;
  try {
    const res = await api("/api/accounts/credentials", { method: "POST", body: jsonBody({ key: item.delete_key, field: "totp_code" }) });
    if (res.code) {
      totpCodes.value[item.delete_key] = { code: res.code, expires_in: res.remaining ?? 30 };
      copyTextDirect(res.code, `totp_code_${item.delete_key}`);
    } else {
      totpCodes.value[item.delete_key] = { error: res.message || "解码失败" };
    }
  } catch (err) {
    totpCodes.value[item.delete_key] = { error: err.message || "解码异常" };
  } finally {
    totpLoading.value[item.delete_key] = false;
  }
}

function fmtTime(value) {
  if (!value) return "-";
  try { return new Date(value).toLocaleString("zh-CN", { hour12: false }); } catch { return value; }
}
function fmtShortTime(value) {
  if (!value) return "-";
  try {
    const d = new Date(value);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`;
  } catch { return value; }
}
function isStale(value) {
  if (!value) return false;
  try { return Date.now() - new Date(value).getTime() > 3 * 24 * 3600 * 1000; } catch { return false; }
}
function lastInvalidAt(item) {
  const failed = item.is_deactivated || ["invalid", "deactivated", "error", "token_invalid"].includes(String(item.health_status || item.status || "").toLowerCase());
  return failed ? (item.updated_at || item.health_checked_at || null) : null;
}
function normalizeProxyUrl(value) {
  let proxy = String(value || "").trim().replace(/^\ufeff/, "").replace(/[,;]+$/, "");
  if (!proxy || proxy.startsWith("#")) return "";
  if (!proxy.includes("://")) {
    const parts = proxy.split(":");
    if (parts.length === 4 && /^\d+$/.test(parts[1])) {
      const [host, port, username, password] = parts;
      proxy = `http://${username}:${password}@${host}:${port}`;
    } else if (parts.length === 4 && /^\d+$/.test(parts[3])) {
      const [username, password, host, port] = parts;
      proxy = `http://${username}:${password}@${host}:${port}`;
    } else {
      proxy = `http://${proxy}`;
    }
  }
  return proxy.replace(/^socks:\/\//i, "socks5h://").replace(/^socks4a?:\/\//i, "socks4://");
}
function countryFromProxyUrl(value) {
  const raw = String(value || "");
  if (proxyCountryCache.has(raw)) return proxyCountryCache.get(raw);
  const normalized = normalizeProxyUrl(value);
  const configured = proxyCountriesByUrl.value.get(normalized);
  if (configured) {
    proxyCountryCache.set(raw, configured);
    return configured;
  }
  let decoded = raw;
  try { decoded = decodeURIComponent(decoded); } catch {}
  const patterns = [
    /(?:^|[_-])g[-_=]?([a-z]{2})(?=[_:@/?&-]|$)/i,
    /(?:region|country|countrycode|cc)[-_=:/]?([a-z]{2})(?=[_:@/?&-]|$)/i,
    /[?&](?:region|country|country_code|cc)=([a-z]{2})(?:[&#]|$)/i,
  ];
  for (const pattern of patterns) {
    const match = decoded.match(pattern);
    if (match) {
      const country = match[1].toUpperCase();
      proxyCountryCache.set(raw, country);
      return country;
    }
  }
  proxyCountryCache.set(raw, "");
  return "";
}
function proxyDisplay(value) {
  if (!String(value || "").trim()) return { country: "US", label: "本地·美国", local: true };
  const country = countryFromProxyUrl(value);
  if (!country) return { country: "", label: "未识别" };
  let label = countryLabels.value.get(country) || "";
  if (!label && typeof Intl?.DisplayNames === "function") {
    try {
      regionDisplayNames ||= new Intl.DisplayNames(["zh-CN"], { type: "region" });
      label = regionDisplayNames.of(country) || "";
    } catch {}
  }
  return { country, label: label || country };
}
function createGroup() {
  const name = newGroupName.value.trim();
  if (!name) return;
  emit("create-group", name);
  newGroupName.value = "";
}
function renameGroup(group) {
  if (!group) return;
  emit("rename-group", group.id, group.name);
}
function deleteGroup(group) {
  if (!group) return;
  emit("delete-group", group.id, group.name);
}
function toggleActiveGroupSelection() {
  if (!filteredGroupKeys.value.length) return;
  const next = new Set(selected.value);
  const shouldSelect = !filteredGroupAllSelected.value;
  for (const key of filteredGroupKeys.value) {
    if (shouldSelect) next.add(key);
    else next.delete(key);
  }
  selected.value = [...next];
}
function moveSelected() {
  if (!selected.value.length || !moveTarget.value) return;
  emit("move-group", [...selected.value], moveTarget.value);
  selected.value = [];
}
</script>

<template>
  <section class="card accounts-panel">
    <div class="card-h">
      <div>
        <h2>账号记录</h2>
        <p class="subtle">搜索、筛选、测活并导出已注册账号，全字段支持直接点击复制。</p>
        <p v-if="accountsLoading" class="account-load-status" aria-live="polite">正在加载账号：{{ accountLoadProgress.loaded || accounts.length }} / {{ accountLoadProgress.total || "…" }}（{{ accountLoadPercent }}%），加载完成后才能执行批量测活。</p>
        <p v-else-if="accountLoadError" class="account-load-status account-load-error" role="alert">{{ accountLoadError }}；当前显示 {{ accounts.length }} / {{ accountLoadProgress.total || "未知" }} 条。</p>
      </div>
      <button class="btn secondary" :disabled="busy || accountsLoading" @click="emit('refresh')">
        <RefreshCw :size="14" aria-hidden="true" />
        刷新列表
      </button>
    </div>

    <div class="accounts-workspace">
      <!-- 账号分组管理侧边栏 -->
      <aside class="account-group-sidebar" aria-label="账号分组">
        <div class="account-group-sidebar-head">
          <div><strong>账号分组</strong><span>{{ groups.length }} 个分组</span></div>
        </div>
        <label class="account-group-search">
          <span aria-hidden="true">⌕</span>
          <input v-model="groupQuery" type="search" placeholder="搜索分组" />
        </label>
        <button
          :class="['account-group-select-all', { active: filteredGroupAllSelected }]"
          type="button"
          :disabled="!activeGroup || !filteredGroupKeys.length || busy"
          :title="activeGroup ? `${filteredGroupAllSelected ? '取消选择' : '选中'}“${activeGroup.name}”内符合当前套餐、状态和搜索条件的全部账号` : '请先选择一个账号分组'"
          @click="toggleActiveGroupSelection"
        >
          <CheckSquare :size="14" aria-hidden="true" />
          <span>{{ filteredGroupAllSelected ? "取消筛选全选" : "全选筛选结果" }}</span>
          <em>{{ filteredGroupKeys.length }}</em>
        </button>
        <div class="account-group-actions">
          <button type="button" title="导出该组邮箱凭证" aria-label="导出该组邮箱凭证" :disabled="!activeGroup || busy" @click="activeGroup && emit('export-group', activeGroup.id)"><Download :size="14" aria-hidden="true" /></button>
          <button type="button" title="重命名分组" aria-label="重命名分组" :disabled="!activeGroup || busy" @click="renameGroup(activeGroup)"><Pencil :size="14" aria-hidden="true" /></button>
          <button class="danger" type="button" title="删除分组" aria-label="删除分组" :disabled="!activeGroup || groups.length <= 1 || busy" @click="deleteGroup(activeGroup)"><Trash2 :size="14" aria-hidden="true" /></button>
        </div>
        <div class="account-group-create">
          <input v-model.trim="newGroupName" maxlength="48" placeholder="新分组名称" @keyup.enter="createGroup" />
          <button type="button" title="新建分组" aria-label="新建分组" :disabled="!newGroupName.trim() || busy" @click="createGroup"><Plus :size="16" aria-hidden="true" /></button>
        </div>
        <div class="account-group-list">
          <div :class="['account-group-row', { active: groupFilter === 'all' }]">
            <button class="account-group-main" type="button" @click="groupFilter = 'all'">
              <span class="account-group-icon"><Users :size="14" aria-hidden="true" /></span>
              <span class="account-group-copy"><b>全部账号</b><small>所有分组</small></span>
              <em>{{ accountsLoading ? (accountLoadProgress.total || accounts.length) : accounts.length }}</em>
            </button>
          </div>
          <div v-for="group in visibleGroups" :key="group.id" :class="['account-group-row', { active: groupFilter === group.id }]">
            <button class="account-group-main" type="button" @click="groupFilter = group.id">
              <span class="account-group-icon"><Users :size="14" aria-hidden="true" /></span>
              <span class="account-group-copy"><b>{{ group.name }}</b><small>{{ groupFilter === group.id ? '当前分组' : '账号分组' }}</small></span>
              <em>{{ group.account_count || 0 }}</em>
            </button>
          </div>
          <div v-if="!visibleGroups.length" class="account-group-empty">没有匹配的分组</div>
        </div>
      </aside>

      <!-- 账号主体列表与操作区 -->
      <div class="accounts-main">
        <!-- 进度看板 -->
        <div v-if="healthProgress.requested || healthProgress.total" class="health-progress" :class="{ active: healthProgress.running }" aria-live="polite">
          <div class="health-progress-head">
            <strong>{{ healthProgress.running ? "账号测活进行中" : "最近一次测活完成" }}</strong>
            <span>{{ healthProgress.done }} / {{ healthProgress.total }} 实际检测 · {{ healthPercent }}%</span>
          </div>
          <div class="health-progress-track"><i :style="{ width: `${healthPercent}%` }"></i></div>
          <div class="health-progress-meta">
            <span v-if="healthProgress.current">当前：{{ healthProgress.current }}</span>
            <span>请求记录 {{ healthProgress.requested || 0 }}</span>
            <span>实际检测 {{ healthProgress.checked || healthProgress.done || 0 }}</span>
            <span>跳过已封号 {{ healthProgress.skippedKnownDeactivated || 0 }}</span>
            <span>实际并发 {{ healthProgress.workers || healthProgress.requestedWorkers || 0 }} / 请求 {{ healthProgress.requestedWorkers || healthProgress.workers || 0 }}</span>
            <span v-if="healthProgress.throttled">注册优先</span>
            <span>正常 {{ healthProgress.alive || 0 }}</span>
            <span>限流 {{ healthProgress.limited || 0 }}</span>
            <span>待续期 {{ healthProgress.reauthRequired || 0 }}</span>
            <span>其它失效 {{ healthProgress.invalidOther || 0 }}</span>
            <span>错误 {{ healthProgress.error || 0 }}</span>
          </div>
        </div>

        <div v-if="reauthProgress.total" class="health-progress" :class="{ active: reauthProgress.running }" aria-live="polite">
          <div class="health-progress-head">
            <strong>{{ reauthProgress.stopping ? `正在停止${reauthOperationLabel}` : (reauthProgress.running ? `${reauthOperationLabel}进行中` : (reauthProgress.lastStatus === "cancelled" || reauthProgress.cancelled ? `${reauthOperationLabel}已停止` : `最近一次${reauthOperationLabel}完成`)) }} · {{ reauthModeLabel }} · 并发 {{ reauthProgress.concurrency || 3 }}</strong>
            <span>{{ reauthProgress.done }} / {{ reauthProgress.total }} · {{ reauthPercent }}%</span>
            <button v-if="reauthProgress.running" class="btn danger reauth-stop-btn" type="button" :disabled="reauthProgress.stopping" @click="emit('stop-reauth')">{{ reauthProgress.stopping ? "停止中…" : `停止${reauthOperationLabel}` }}</button>
          </div>
          <div class="health-progress-track"><i :style="{ width: `${reauthPercent}%` }"></i></div>
          <div class="health-progress-meta">
            <span v-if="reauthProgress.current">处理中：{{ reauthProgress.current }}</span>
            <span>成功 {{ reauthProgress.success || 0 }}</span>
            <span>失败 {{ reauthProgress.failed || 0 }}</span>
            <span>已停止 {{ reauthProgress.cancelled || 0 }}</span>
          </div>
          <div v-if="reauthProgress.last" class="health-progress-meta">
            <span>最近完成：{{ reauthProgress.last }}</span>
            <span>{{ reauthProgress.lastStatus === "success" ? "成功" : "失败" }}</span>
            <span v-if="reauthProgress.lastMessage">{{ reauthProgress.lastMessage }}</span>
          </div>
        </div>

        <div v-if="trialProgress.total" class="health-progress" :class="{ active: trialProgress.running }" aria-live="polite">
          <div class="health-progress-head">
            <strong>{{ trialProgress.running ? "0元资格检测中" : "最近一次0元资格检测完成" }}</strong>
            <span>{{ trialProgress.done }} / {{ trialProgress.total }} · {{ trialPercent }}%</span>
          </div>
          <div class="health-progress-track"><i :style="{ width: `${trialPercent}%` }"></i></div>
          <div class="health-progress-meta">
            <span v-if="trialProgress.current">当前：{{ trialProgress.current }}</span>
            <span>符合资格 {{ trialProgress.eligible || 0 }}</span>
            <span>不符合 {{ trialProgress.ineligible || 0 }}</span>
            <span>失败 {{ trialProgress.failed || 0 }}</span>
            <span>已停止 {{ trialProgress.stopped || 0 }}</span>
          </div>
        </div>

        <!-- 筛选栏 -->
        <div class="account-filters">
          <label class="search-field">
            <span aria-hidden="true">⌕</span>
            <input v-model="query" type="search" placeholder="搜索邮箱、注册方式、状态、ID或代理" />
          </label>
          <select v-model="statusFilter" aria-label="账号状态筛选">
            <option value="all">全部状态</option>
            <option value="alive">可用</option>
            <option value="limited">限流</option>
            <option value="reauth_required">待续期</option>
            <option value="deactivated">已封号</option>
            <option value="invalid">失效</option>
            <option value="error">错误</option>
            <option value="unknown">未检测</option>
          </select>
          <select v-model="planFilter" aria-label="账号套餐筛选">
            <option value="all">全部套餐</option>
            <option value="free">Free</option>
            <option value="plus">Plus</option>
          </select>
          <select v-model="offerFilter" aria-label="优惠筛选">
            <option value="all">全部优惠状态</option>
            <option value="has">有优惠（试用/折扣）</option>
            <option value="trial">可领试用</option>
            <option value="discount">有折扣优惠</option>
            <option value="none">没优惠</option>
          </select>
          <select v-model.number="pageSize" aria-label="每页数量">
            <option :value="10">10 / 页</option>
            <option :value="20">20 / 页</option>
            <option :value="50">50 / 页</option>
            <option :value="100">100 / 页</option>
          </select>
          <span class="filter-result">显示 {{ filtered.length }} / {{ accounts.length }} · 可用 {{ accountStats.usable }}</span>
        </div>

        <!-- 批量操作栏 -->
        <div class="bulkbar account-actions">
          <label class="checkline"><input v-model="allPageSelected" type="checkbox" />全选本页</label>
          <button class="btn secondary" type="button" :disabled="!selected.length || busy" title="取消所有分页中已选择的账号" @click="clearAllSelected"><X :size="14" aria-hidden="true" />取消（{{ selected.length }}）</button>
          <button class="btn primary" :disabled="!selected.length || busy" @click="emit('download', selected)"><Download :size="14" aria-hidden="true" />下载选中</button>
          <button class="btn secondary" :disabled="!selectedAccessTokenKeys.length || busy || copyingAt" title="复制选中账号的完整 Access Token" @click="emit('copy-selected-at', selected)"><Copy :size="14" aria-hidden="true" />{{ copyingAt ? "读取AT中" : "复制选中AT" }}</button><button class="btn secondary" type="button" :disabled="!selected.length || busy || codexOauthRunning" title="对选中账号补发 Codex RT(协议登录+接码)" @click="emit('codex-oauth', selected)"><Smartphone :size="14" aria-hidden="true" />接码</button><button class="btn secondary" type="button" :disabled="busy || codexOauthRunning" title="粘贴 邮箱----密码----2FA 导入新账号" @click="emit('import-accounts')"><Upload :size="14" aria-hidden="true" />导入账号</button>
          <button class="btn secondary" :disabled="!selectedAccessTokenKeys.length || busy" title="导出选中账号的完整 Access Token" @click="emit('export-selected-at', selected)">导出选中AT</button>
          <button class="btn secondary" :disabled="!selected.length || busy" title="导出选中账号、密码和完整 TOTP 密钥" @click="emit('export-security', selected)">导出账号+密码+2FA</button>
          <button class="btn secondary" :disabled="!accounts.length || busy" @click="emit('download', null)">下载全部</button>
          <button class="btn secondary" :disabled="!selected.length || busy || accountLoadBlocked || healthProgress.running" @click="emit('health', selected)">测活选中</button>
          <button class="btn secondary" :disabled="!accounts.length || busy || accountLoadBlocked || healthProgress.running" @click="emit('health', null)">测活全部（跳过已封号）</button>
          <button class="btn secondary" :disabled="!selected.length || busy || accountLoadBlocked" @click="emit('trial-check', { keys: selected, force: false })">检测资格选中</button>
          <button class="btn secondary" :disabled="!accounts.length || busy || accountLoadBlocked" @click="emit('trial-check', { all_accounts: true, force: false })">检测全部 Free 资格</button>
          
          <label class="reauth-mode-field">
            <span>登录方式</span>
            <select v-model="reauthMode" :disabled="busy">
              <option value="protocol">协议</option>
              <option value="browser">浏览器（手动选择）</option>
            </select>
          </label>
          <label class="reauth-mode-field reauth-concurrency-field">
            <span>并发</span>
            <input v-model.number="reauthConcurrency" type="number" min="1" :disabled="busy || reauthProgress.running" />
          </label>
          
          <button class="btn secondary" :disabled="!selectedReauthKeys.length || busy || reauthProgress.running" @click="emit('reauth', { keys: selectedReauthKeys, mode: reauthMode, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">重新登录选中（{{ selectedReauthKeys.length }}）</button>
          <button class="btn secondary" :disabled="!reauthTargetKeys.length || busy || reauthProgress.running" @click="emit('reauth', { keys: reauthTargetKeys, mode: reauthMode, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">重新登录可恢复（{{ reauthTargetKeys.length }}）</button>
          <button class="btn primary" :disabled="!selectedSecurityRepairKeys.length || busy || reauthProgress.running" @click="emit('security-repair', { keys: selectedSecurityRepairKeys, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">补全安全字段（{{ selectedSecurityRepairKeys.length }}）</button>
          <button class="btn secondary" :disabled="!selectedSecurityOperationKeys.length || busy || reauthProgress.running" @click="emit('password-change', { keys: selectedSecurityOperationKeys, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">修改密码（{{ selectedSecurityOperationKeys.length }}）</button>
          <button class="btn secondary" :disabled="!selectedSecurityOperationKeys.length || busy || reauthProgress.running" @click="emit('totp-change', { keys: selectedSecurityOperationKeys, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">修改 2FA（{{ selectedSecurityOperationKeys.length }}）</button>
          
          <button class="btn danger" :disabled="!invalidCount || busy" @click="emit('delete-invalid', invalidKeys)"><Trash2 :size="14" aria-hidden="true" />删除失效（{{ invalidCount }}）</button>
          <select v-model="moveTarget" aria-label="移动目标分组"><option v-for="group in groups" :key="group.id" :value="group.id">移至：{{ group.name }}</option></select>
          <button class="btn secondary" :disabled="!selected.length || !moveTarget || busy" @click="moveSelected">移动选中</button>
          <button class="btn danger" :disabled="!selected.length || busy" @click="emit('delete', selected)">删除选中</button>
          <button class="btn danger" :disabled="!accounts.length || busy" @click="emit('delete', null)">全部删除</button>
        </div>

        <!-- 账号表格：9 列分组布局 -->
        <div class="table-wrap acct-wrap">
          <table class="acct">
            <thead>
              <tr>
                <th class="col-check"></th>
                <th class="col-account">账号</th>
                <th class="col-group">分组 / 方式</th>
                <th class="col-status">状态</th>
                <th class="col-cred">凭据 · 点击复制</th>
                <th class="col-totp">验证码</th>
                <th class="col-meta">代理 / 时间</th>
                <th class="col-actions">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!pageItems.length">
                <td colspan="8" class="muted account-empty" style="text-align: center; padding: 32px;">没有匹配的账号记录</td>
              </tr>
              <tr v-for="item in pageItems" :key="item.delete_key">
                <!-- 选择框 -->
                <td data-label="选择">
                  <input v-model="selected" type="checkbox" :value="item.delete_key" />
                </td>

                <!-- 账号：邮箱 + ID -->
                <td class="col-account" data-label="账号">
                  <div class="acct-cell-main">
                    <button
                      class="acct-email interactive-email"
                      type="button"
                      :title="`点击复制邮箱: ${item.email}`"
                      @click="copyTextDirect(item.email, `email_${item.delete_key}`)"
                    >
                      <strong>{{ item.email || "-" }}</strong>
                      <component :is="copiedFeedback[`email_${item.delete_key}`] ? Check : Copy" :size="12" aria-hidden="true" />
                    </button>
                    <div class="acct-id-row">
                      <button
                        v-if="item.account_id"
                        class="copy-pill-btn"
                        type="button"
                        :title="`点击复制 Account ID: ${item.account_id}`"
                        @click="copyTextDirect(item.account_id, `acc_id_${item.delete_key}`)"
                      >
                        <span class="acct-id-text">{{ item.account_id }}</span>
                        <component :is="copiedFeedback[`acc_id_${item.delete_key}`] ? Check : Copy" :size="11" aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                </td>

                <!-- 分组 / 注册方式 -->
                <td class="col-group" data-label="分组 / 方式">
                  <div class="acct-cell-stack">
                    <span class="acct-group-name" :title="item.group_name || '默认分组'">{{ item.group_name || "默认分组" }}</span>
                    <span class="acct-reg-source">{{ registrationText(item) }}</span>
                  </div>
                </td>

                <!-- 状态：套餐 + 测活 + 0元资格 -->
                <td class="col-status" data-label="状态">
                  <div class="acct-cell-stack">
                    <span :class="['capsule-badge', 'acct-plan', planType(item)]" :title="`测活识别套餐：${planName(item)}`">
                      {{ planName(item) }}
                    </span>
                    <span :class="['status-pill', statusClass(item)]">
                      <i></i>
                      {{ statusName(item) }}
                    </span>
                    <span :class="['status-pill', 'status-pill-sub', trialClass(item)]" :title="item.trial_eligibility_error || item.trial_eligibility_reason || ''">
                      <i></i>
                      {{ trialLabel(item) }}
                    </span>
                    <span v-if="item.health_error" class="acct-health-error" :title="item.health_error">{{ item.health_error }}</span>
                  </div>
                </td>

                <!-- 凭据：密码 / 2FA / AT -->
                <td class="col-cred" data-label="凭据">
                  <div class="acct-cell-stack">
                    <button
                      v-if="item.has_password"
                      class="copy-pill-btn cred-chip"
                      type="button"
                      title="点击直接复制完整密码"
                      @click="emit('copy-credential', item, 'password')"
                    >
                      <em>密码</em>
                      <span>{{ item.password_masked || "••••••••" }}</span>
                      <Copy :size="11" aria-hidden="true" />
                    </button>
                    <span v-else class="cred-missing">密码缺失</span>
                    <button
                      v-if="item.has_totp"
                      class="copy-pill-btn cred-chip"
                      type="button"
                      title="点击直接复制完整 2FA 密钥"
                      @click="emit('copy-credential', item, 'totp_secret')"
                    >
                      <em>2FA</em>
                      <span>{{ item.totp_secret_masked || "已存秘钥" }}</span>
                      <Copy :size="11" aria-hidden="true" />
                    </button>
                    <span v-else class="cred-missing">2FA 未设置</span>
                    <button
                      class="copy-pill-btn cred-chip"
                      type="button"
                      :disabled="!item.has_access_token || copyingAt"
                      :title="item.has_access_token ? '点击复制 Access Token' : '该账号没有 AT'"
                      @click="emit('copy-at', item)"
                    >
                      <em>AT</em>
                      <span>{{ !item.has_access_token ? "缺失" : copyingAt ? "读取中…" : "复制 Token" }}</span>
                      <Copy :size="11" aria-hidden="true" />
                    </button>
                    <small v-if="item.c2a_upload_status" :class="['capsule-badge', 'acct-c2a', item.c2a_upload_status === 'success' ? 'plus' : 'free']">
                      c2a {{ item.c2a_upload_status === 'success' ? '已上传' : '失败' }}
                    </small>
                  </div>
                </td>

                <!-- 2FA 实时验证码 -->
                <td class="col-totp" data-label="验证码">
                  <button
                    v-if="item.has_totp"
                    :class="['copy-pill-btn', 'totp-btn', { copied: copiedFeedback[`totp_code_${item.delete_key}`] }]"
                    type="button"
                    :disabled="totpLoading[item.delete_key]"
                    :title="totpEntry(item)?.error || '点击获取并复制 6 位实时 2FA 验证码'"
                    @click="handleTotpClick(item)"
                  >
                    <strong>{{ totpDisplay(item) }}</strong>
                    <component :is="copiedFeedback[`totp_code_${item.delete_key}`] ? Check : Copy" :size="11" aria-hidden="true" />
                  </button>
                  <span v-else class="cred-missing">-</span>
                </td>

                <!-- 代理 / 时间 -->
                <td class="col-meta" data-label="代理 / 时间">
                  <div class="acct-meta">
                    <span class="acct-proxy" :title="item.proxy || '本地美国'">{{ proxyDisplay(item.proxy).label }}</span>
                    <span class="acct-time" :title="`注册时间：${fmtTime(item.registered_at || item.updated_at)}`">
                      <em>注册</em>{{ fmtShortTime(item.registered_at || item.updated_at) }}
                    </span>
                    <small class="acct-time" :class="{ 'is-stale': isStale(item.health_checked_at) }" :title="`最近刷新：${fmtTime(item.health_checked_at)}${isStale(item.health_checked_at) ? '（已超过 3 天，数据可能失效）' : ''}`">
                      <em>刷新</em>{{ fmtShortTime(item.health_checked_at) }}
                    </small>
                    <small
                      :class="['acct-time', { 'is-failed': lastInvalidAt(item) }]"
                      :title="lastInvalidAt(item) ? `最后失效：${fmtTime(lastInvalidAt(item))}` : '账号当前状态有效'"
                    >
                      <em>失效</em>{{ lastInvalidAt(item) ? fmtShortTime(lastInvalidAt(item)) : "-" }}
                    </small>
                  </div>
                </td>

                <!-- 操作按钮组 -->
                <td class="col-actions" data-label="操作">
                  <div class="account-row-actions">
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || !item.delete_key"
                      title="查看账号操作日志"
                      aria-label="查看账号操作日志"
                      @click="emit('account-logs', item)"
                    >
                      <ClipboardList :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || reauthProgress.running || !item.delete_key || item.password_confirmed === false || !item.has_password || !item.has_totp || item.is_deactivated || (item.password_confirmed === undefined && item.security_repair_available && item.has_totp)"
                      title="修改密码"
                      aria-label="修改密码"
                      @click="emit('password-change', { keys: [item.delete_key], concurrency: 1 })"
                    >
                      <KeyRound :size="13" aria-hidden="true" />
                    </button>
                    <button
                      class="btn secondary row-icon-btn"
                      type="button"
                      :disabled="busy || reauthProgress.running || !item.delete_key || item.password_confirmed === false || !item.has_password || !item.has_totp || item.is_deactivated || (item.password_confirmed === undefined && item.security_repair_available && item.has_totp)"
                      title="修改 TOTP 2FA"
                      aria-label="修改 TOTP 2FA"
                      @click="emit('totp-change', { keys: [item.delete_key], concurrency: 1 })"
                    >
                      <ShieldCheck :size="13" aria-hidden="true" />
                    </button>
                  </div>
                </td>
              </tr>
</tbody>
          </table>
        </div>

        <AppPager v-model:page="page" :pages="pages" :total="filtered.length" />
      </div>
    </div>
  </section>
</template>
