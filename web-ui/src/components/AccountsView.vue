<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { CheckSquare, ClipboardList, Download, KeyRound, Pencil, Plus, ShieldCheck, Trash2, Users, X } from "@lucide/vue";
import AppPager from "./AppPager.vue";
import { api, jsonBody } from "../api.js";

const props = defineProps({ accounts: { type: Array, default: () => [] }, accountsLoading: Boolean, accountLoadProgress: { type: Object, default: () => ({ loaded: 0, total: 0 }) }, accountLoadError: { type: String, default: "" }, groups: { type: Array, default: () => [] }, proxyCountries: { type: Array, default: () => [] }, busy: Boolean, copyingAt: Boolean, healthProgress: { type: Object, default: () => ({}) }, reauthProgress: { type: Object, default: () => ({}) }, trialProgress: { type: Object, default: () => ({}) } });
const emit = defineEmits(["refresh", "health", "reauth", "security-repair", "password-change", "totp-change", "account-logs", "stop-reauth", "delete", "delete-invalid", "download", "copy-selected-at", "export-selected-at", "export-security", "create-group", "rename-group", "delete-group", "move-group", "export-group", "copy-at", "copy-credential", "copy-value", "trial-check", "generate-cdk"]);
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
const healthPercent = computed(() => props.healthProgress.total ? Math.round((props.healthProgress.done / props.healthProgress.total) * 100) : (props.healthProgress.requested && !props.healthProgress.running ? 100 : 0));
// Build toolbar indexes in one pass. Previously each computed property
// independently traversed the complete account collection.
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
    // Keep toolbar semantics identical to the original fields: these counts
    // use the persisted health_status value, while the table filter below
    // still accepts the legacy `status` fallback.
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
    // A browser flow can expose a candidate password while the server has not
    // confirmed it yet. Such rows are deliberately marked for security repair;
    // keep the known-credential password/2FA actions hidden until repair
    // succeeds, otherwise the user gets a predictable backend preflight error.
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
      item.cdk_redeemed ? "CDK 已兑换" : "",
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
const countryLabels = computed(() => {
  const labels = new Map();
  for (const item of props.proxyCountries) {
    const country = String(item?.country || "").trim().toUpperCase();
    if (country) labels.set(country, String(item?.label || country).trim() || country);
  }
  return labels;
});
const proxyCountriesByUrl = computed(() => {
  const countries = new Map();
  for (const item of props.proxyCountries) {
    const country = String(item?.country || "").trim().toUpperCase();
    if (!country) continue;
    for (const line of String(item?.text || "").split(/\r?\n/)) {
      const proxy = normalizeProxyUrl(line);
      if (proxy) countries.set(proxy, country);
    }
  }
  return countries;
});
const proxyCountryCache = new Map();
let regionDisplayNames = null;

const filtered = computed(() => {
  const needle = searchNeedle.value;
  if (!needle && statusFilter.value === "all" && planFilter.value === "all" && offerFilter.value === "all" && groupFilter.value === "all") {
    return props.accounts;
  }
  return props.accounts.filter((item) => {
    const health = String(item.health_status || item.status || "unknown").toLowerCase();
    const eligibility = String(item.trial_eligibility_status || "pending").toLowerCase();
    if (statusFilter.value === "deactivated" && !item.is_deactivated) return false;
    if (statusFilter.value === "invalid" && (item.is_deactivated || health !== "invalid")) return false;
    if (!["all", "deactivated", "invalid"].includes(statusFilter.value) && health !== statusFilter.value) return false;
    if (planFilter.value !== "all" && planType(item) !== planFilter.value) return false;
    if (offerFilter.value === "has" && !["eligible", "discount"].includes(eligibility)) return false;
    if (offerFilter.value === "trial" && eligibility !== "eligible") return false;
    if (offerFilter.value === "discount" && eligibility !== "discount") return false;
    if (offerFilter.value === "none" && eligibility !== "ineligible") return false;
    if (groupFilter.value !== "all" && item.group_id !== groupFilter.value) return false;
    if (!needle) return true;
    return searchTextByItem.value?.get(item)?.some((value) => value.includes(needle)) || false;
  });
});
const filteredGroupKeys = computed(() => {
  if (!activeGroup.value) return [];
  return filtered.value.map((item) => item.delete_key).filter(Boolean);
});
const filteredGroupAllSelected = computed(() => {
  if (!filteredGroupKeys.value.length) return false;
  const selectedKeys = new Set(selected.value);
  return filteredGroupKeys.value.every((key) => selectedKeys.has(key));
});
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)));
const pageItems = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
const pageKeys = computed(() => pageItems.value.map((item) => item.delete_key).filter(Boolean));
const selectedPageKeys = computed(() => {
  const currentPage = new Set(pageKeys.value);
  return selected.value.filter((key) => currentPage.has(key));
});
function setCurrentPageSelected(checked) {
  const next = new Set(selected.value);
  for (const key of pageKeys.value) {
    if (checked) next.add(key);
    else next.delete(key);
  }
  selected.value = [...next];
}
function clearAllSelected() {
  selected.value = [];
}
const allPageSelected = computed({
  get: () => pageKeys.value.length > 0 && selectedPageKeys.value.length === pageKeys.value.length,
  set: setCurrentPageSelected,
});
let searchDebounceTimer = null;
function resetAccountSelection() { page.value = 1; selected.value = []; }
watch([statusFilter, planFilter, offerFilter, groupFilter, pageSize], resetAccountSelection);
watch(query, (value) => {
  resetAccountSelection();
  clearTimeout(searchDebounceTimer);
  const next = String(value || "").trim().toLowerCase();
  if (!next) {
    searchNeedle.value = "";
    return;
  }
  searchDebounceTimer = setTimeout(() => {
    searchNeedle.value = next;
    searchDebounceTimer = null;
  }, 120);
});
watch([() => props.accounts.length, () => props.accountsLoading], () => {
  // Incremental account refreshes publish the first API page before the tail
  // pages arrive.  Clamping against that temporary 1,000-row snapshot would
  // move a user on page 70 back to page 50.  Only clamp once the refresh has
  // reached a terminal state and `pages` reflects the complete collection.
  if (props.accountsLoading) return;
  page.value = Math.min(page.value, pages.value);
  // Account pages now arrive incrementally. Preserve selections that are
  // still present instead of clearing them on every tail-page publication.
  const available = new Set((props.accounts || []).map((item) => item?.delete_key).filter(Boolean));
  selected.value = selected.value.filter((key) => available.has(key));
});
watch(() => props.groups, (items) => {
  if (groupFilter.value !== "all" && !items.some((item) => item.id === groupFilter.value)) groupFilter.value = "all";
  if (!items.some((item) => item.id === moveTarget.value)) moveTarget.value = items[0]?.id || "";
}, { immediate: true, deep: true });
watch(() => props.proxyCountries, () => proxyCountryCache.clear(), { deep: true });
onBeforeUnmount(() => clearTimeout(searchDebounceTimer));

function totpEntry(item) {
  return totpCodes.value[item?.delete_key] || null;
}
function totpDisplay(item) {
  const entry = totpEntry(item);
  if (entry?.code) return entry.code;
  if (totpLoading.value[item?.delete_key]) return "获取中";
  return entry?.error ? "重试获取" : "点击获取";
}
async function loadTotpCode(item) {
  const key = item?.delete_key;
  if (!key || !item.has_totp || totpLoading.value[key]) return;
  totpLoading.value = { ...totpLoading.value, [key]: true };
  try {
    const result = await api("/api/accounts/credentials", { method: "POST", body: jsonBody({ key, field: "totp_code" }) });
    const entry = { code: result.code || result.value || "", expiresAt: Number(result.expires_at || 0) * 1000, error: "" };
    totpCodes.value = {
      ...totpCodes.value,
      [key]: entry,
    };
    return entry;
  } catch (error) {
    totpCodes.value = {
      ...totpCodes.value,
      [key]: { code: "", expiresAt: 0, error: error?.message || "读取失败" },
    };
  } finally {
    const next = { ...totpLoading.value };
    delete next[key];
    totpLoading.value = next;
  }
}
async function handleTotpClick(item) {
  const key = item?.delete_key;
  if (!key || !item.has_totp || totpLoading.value[key]) return;
  const entry = totpEntry(item);
  if (entry?.code && (!entry.expiresAt || entry.expiresAt > Date.now())) {
    emit("copy-value", { value: entry.code, message: "2FA 验证码已复制" });
    return;
  }
  const refreshed = await loadTotpCode(item);
  if (entry?.code && refreshed?.code) {
    emit("copy-value", { value: refreshed.code, message: "2FA 验证码已更新并复制" });
  }
}

function statusName(item) {
  if (item.is_deactivated) return "已封号";
  if (String(item.registration_state || "").toLowerCase() === "security_pending") return "待补全安全字段";
  const status = item.health_status || item.status || "unknown";
  return { alive: "可用", limited: "限流", invalid: "失效", reauth_required: "待续期", error: "错误", unknown: "未检测" }[status] || status;
}
function planType(item) {
  return String(item?.plan_type || "free").toLowerCase() === "plus" ? "plus" : "free";
}
function planName(item) {
  return planType(item) === "plus" ? "Plus" : "Free";
}
function statusClass(item) {
  if (String(item.registration_state || "").toLowerCase() === "security_pending") return "warn";
  const status = item.health_status || item.status || "unknown";
  return status === "alive" ? "ok" : ["invalid", "error"].includes(status) ? "bad" : ["limited", "reauth_required"].includes(status) ? "warn" : "";
}
function trialLabel(item) {
  const status = String(item.trial_eligibility_status || "pending").toLowerCase();
  return item.trial_eligibility_label || ({ pending: "检测中", eligible: "可领 Plus", active: "Plus 生效中", discount: "有折扣优惠", ineligible: "无优惠", invalid: "AT 已失效", error: "检测失败" }[status] || "待检测");
}
function trialClass(item) {
  const status = String(item.trial_eligibility_status || "pending").toLowerCase();
  return ["eligible", "active"].includes(status) ? "ok" : status === "discount" ? "offer" : ["invalid", "error"].includes(status) ? "bad" : status === "ineligible" ? "warn" : "";
}
function quotaText(value) {
  if (value == null) return "-";
  if (typeof value === "string" || typeof value === "number") return String(value);
  const number = value.remaining ?? value.left ?? value.limit;
  return number == null ? "已获取" : String(number);
}
function browserName(value) {
  return { camoufox: "Camoufox", local: "Local Chrome", obscura: "Obscura" }[String(value || "").toLowerCase()] || value;
}
function registrationText(item) {
  const method = String(item.registration_method || "").toLowerCase();
  if (method === "hybrid") return `混合 · ${browserName(item.browser_type) || "未记录"}`;
  if (method === "protocol") return "协议";
  if (method === "browser") return `浏览器 · ${browserName(item.browser_type) || "未记录"}`;
  return "历史记录";
}
function mfaText(item) {
  if (!item.mfa_enabled) return "未启用";
  const type = String(item.mfa_factor_type || "").toLowerCase();
  return type === "totp" ? "TOTP" : type || "已启用";
}
function cdkEligible(item) {
  return !!item.has_password && !!item.has_totp && !!item.mfa_enabled && !item.cdk_bound && String(item.health_status || "").toLowerCase() !== "invalid";
}
function cdkTitle(item) {
  if (item.cdk_bound) return "该账号已绑定 CDK";
  if (item.is_deactivated) return "已封号账号不能生成 CDK";
  if (String(item.health_status || "").toLowerCase() === "invalid") return "失效账号不能生成 CDK";
  if (!item.has_password || !item.has_totp || !item.mfa_enabled) return "需要先设置密码并启用 TOTP 2FA";
  return "为该账号生成 CDK";
}
function fmtTime(value) {
  if (!value) return "-";
  try { return new Date(value).toLocaleString("zh-CN", { hour12: false }); } catch { return value; }
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
        <p class="subtle">搜索、筛选、分页、测活和导出已注册账号。</p>
        <p v-if="accountsLoading" class="account-load-status" aria-live="polite">正在加载账号：{{ accountLoadProgress.loaded || accounts.length }} / {{ accountLoadProgress.total || "…" }}（{{ accountLoadPercent }}%），加载完成后才能执行批量测活。</p>
        <p v-else-if="accountLoadError" class="account-load-status account-load-error" role="alert">{{ accountLoadError }}；当前显示 {{ accounts.length }} / {{ accountLoadProgress.total || "未知" }} 条。</p>
      </div>
      <button class="btn secondary" :disabled="busy || accountsLoading" @click="emit('refresh')">刷新列表</button>
    </div>

    <div class="accounts-workspace">
      <aside class="account-group-sidebar" aria-label="账号分组">
        <div class="account-group-sidebar-head"><div><strong>账号分组</strong><span>{{ groups.length }} 个分组</span></div></div>
        <label class="account-group-search"><span aria-hidden="true">⌕</span><input v-model="groupQuery" type="search" placeholder="搜索分组" /></label>
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
        <div class="account-group-create"><input v-model.trim="newGroupName" maxlength="48" placeholder="新分组名称" @keyup.enter="createGroup" /><button type="button" title="新建分组" aria-label="新建分组" :disabled="!newGroupName.trim() || busy" @click="createGroup"><Plus :size="16" aria-hidden="true" /></button></div>
        <div class="account-group-list">
          <div :class="['account-group-row', { active: groupFilter === 'all' }]">
            <button class="account-group-main" type="button" @click="groupFilter = 'all'"><span class="account-group-icon"><Users :size="14" aria-hidden="true" /></span><span class="account-group-copy"><b>全部账号</b><small>所有分组</small></span><em>{{ accountsLoading ? (accountLoadProgress.total || accounts.length) : accounts.length }}</em></button>
          </div>
          <div v-for="group in visibleGroups" :key="group.id" :class="['account-group-row', { active: groupFilter === group.id }]">
            <button class="account-group-main" type="button" @click="groupFilter = group.id"><span class="account-group-icon"><Users :size="14" aria-hidden="true" /></span><span class="account-group-copy"><b>{{ group.name }}</b><small>{{ groupFilter === group.id ? '当前分组' : '账号分组' }}</small></span><em>{{ group.account_count || 0 }}</em></button>
          </div>
          <div v-if="!visibleGroups.length" class="account-group-empty">没有匹配的分组</div>
        </div>
      </aside>

      <div class="accounts-main">

    <div v-if="healthProgress.requested || healthProgress.total" class="health-progress" :class="{ active: healthProgress.running }" aria-live="polite">
      <div class="health-progress-head"><strong>{{ healthProgress.running ? "账号测活进行中" : "最近一次测活完成" }}</strong><span>{{ healthProgress.done }} / {{ healthProgress.total }} 实际检测 · {{ healthPercent }}%</span></div>
      <div class="health-progress-track"><i :style="{ width: `${healthPercent}%` }"></i></div>
      <div class="health-progress-meta"><span v-if="healthProgress.current">当前：{{ healthProgress.current }}</span><span>请求记录 {{ healthProgress.requested || 0 }}</span><span>实际检测 {{ healthProgress.checked || healthProgress.done || 0 }}</span><span>跳过已封号 {{ healthProgress.skippedKnownDeactivated || 0 }}</span><span>实际并发 {{ healthProgress.workers || healthProgress.requestedWorkers || 0 }} / 请求 {{ healthProgress.requestedWorkers || healthProgress.workers || 0 }}</span><span v-if="healthProgress.throttled">注册优先</span><span>正常 {{ healthProgress.alive || 0 }}</span><span>限流 {{ healthProgress.limited || 0 }}</span><span>待续期 {{ healthProgress.reauthRequired || 0 }}</span><span>其它失效 {{ healthProgress.invalidOther || 0 }}</span><span>错误 {{ healthProgress.error || 0 }}</span><span>CDK 替换 {{ healthProgress.cdkReplaced || 0 }}</span><span v-if="healthProgress.cdkUnresolved">待补位 {{ healthProgress.cdkUnresolved }}</span></div>
    </div>

    <div v-if="reauthProgress.total" class="health-progress" :class="{ active: reauthProgress.running }" aria-live="polite">
      <div class="health-progress-head"><strong>{{ reauthProgress.stopping ? `正在停止${reauthOperationLabel}` : (reauthProgress.running ? `${reauthOperationLabel}进行中` : (reauthProgress.lastStatus === "cancelled" || reauthProgress.cancelled ? `${reauthOperationLabel}已停止` : `最近一次${reauthOperationLabel}完成`)) }} · {{ reauthModeLabel }} · 并发 {{ reauthProgress.concurrency || 3 }}</strong><span>{{ reauthProgress.done }} / {{ reauthProgress.total }} · {{ reauthPercent }}%</span><button v-if="reauthProgress.running" class="btn danger reauth-stop-btn" type="button" :disabled="reauthProgress.stopping" @click="emit('stop-reauth')">{{ reauthProgress.stopping ? "停止中…" : `停止${reauthOperationLabel}` }}</button></div>
      <div class="health-progress-track"><i :style="{ width: `${reauthPercent}%` }"></i></div>
      <div class="health-progress-meta"><span v-if="reauthProgress.current">处理中：{{ reauthProgress.current }}</span><span>成功 {{ reauthProgress.success || 0 }}</span><span>失败 {{ reauthProgress.failed || 0 }}</span><span>已停止 {{ reauthProgress.cancelled || 0 }}</span></div>
      <div v-if="reauthProgress.last" class="health-progress-meta"><span>最近完成：{{ reauthProgress.last }}</span><span>{{ reauthProgress.lastStatus === "success" ? "成功" : "失败" }}</span><span v-if="reauthProgress.lastMessage">{{ reauthProgress.lastMessage }}</span></div>
    </div>
    <div v-if="trialProgress.total" class="health-progress" :class="{ active: trialProgress.running }" aria-live="polite">
      <div class="health-progress-head"><strong>{{ trialProgress.running ? "0元资格检测中" : "最近一次0元资格检测完成" }}</strong><span>{{ trialProgress.done }} / {{ trialProgress.total }} · {{ trialPercent }}%</span></div>
      <div class="health-progress-track"><i :style="{ width: `${trialPercent}%` }"></i></div>
      <div class="health-progress-meta"><span v-if="trialProgress.current">当前：{{ trialProgress.current }}</span><span>符合资格 {{ trialProgress.eligible || 0 }}</span><span>不符合 {{ trialProgress.ineligible || 0 }}</span><span>失败 {{ trialProgress.failed || 0 }}</span><span>已停止 {{ trialProgress.stopped || 0 }}</span></div>
    </div>

    <div class="account-filters">
      <label class="search-field"><span aria-hidden="true">⌕</span><input v-model="query" type="search" placeholder="搜索邮箱、注册方式、浏览器、状态或代理" /></label>
      <select v-model="statusFilter" aria-label="账号状态筛选">
        <option value="all">全部状态</option><option value="alive">可用</option><option value="limited">限流</option><option value="reauth_required">待续期</option><option value="deactivated">已封号</option><option value="invalid">失效</option><option value="error">错误</option><option value="unknown">未检测</option>
      </select>
      <select v-model="planFilter" aria-label="账号套餐筛选"><option value="all">全部套餐</option><option value="free">Free</option><option value="plus">Plus</option></select>
      <select v-model="offerFilter" aria-label="优惠筛选"><option value="all">全部优惠状态</option><option value="has">有优惠（试用/折扣）</option><option value="trial">可领试用</option><option value="discount">有折扣优惠</option><option value="none">没优惠</option></select>
      <select v-model.number="pageSize" aria-label="每页数量"><option :value="10">10 / 页</option><option :value="20">20 / 页</option><option :value="50">50 / 页</option><option :value="100">100 / 页</option></select>
      <span class="filter-result">显示 {{ filtered.length }} / {{ accounts.length }} · 总记录 {{ accountLoadProgress.total || accountStats.total }}<template v-if="accountsLoading">（已加载 {{ accounts.length }}）</template> · 已测 {{ accountStats.checked }} · 可用 {{ accountStats.usable }} · 已封号留档 {{ accountStats.deactivated }}</span>
    </div>

    <div class="bulkbar account-actions">
      <label class="checkline"><input v-model="allPageSelected" type="checkbox" />全选本页</label>
      <button class="btn secondary" type="button" :disabled="!selected.length || busy" title="取消所有分页中已选择的账号" @click="clearAllSelected"><X :size="14" aria-hidden="true" />取消全部（{{ selected.length }}）</button>
      <button class="btn primary" :disabled="!selected.length || busy" @click="emit('download', selected)">下载选中</button>
      <button class="btn secondary" :disabled="!selectedAccessTokenKeys.length || busy || copyingAt" title="复制选中账号的完整 Access Token，一行一个" @click="emit('copy-selected-at', selected)">{{ copyingAt ? "正在读取AT" : "复制选中AT" }}</button>
      <button class="btn secondary" :disabled="!selectedAccessTokenKeys.length || busy" title="导出选中账号的完整 Access Token，一行一个" @click="emit('export-selected-at', selected)">导出选中AT</button>
      <button class="btn secondary" :disabled="!selected.length || busy" title="导出选中账号、密码和完整 TOTP 密钥" @click="emit('export-security', selected)">导出账号+密码+2FA</button>
      <button class="btn secondary" :disabled="!accounts.length || busy" @click="emit('download', null)">下载全部</button>
      <button class="btn secondary" :disabled="!selected.length || busy || accountLoadBlocked || healthProgress.running" @click="emit('health', selected)">测活选中</button>
      <button class="btn secondary" :disabled="!accounts.length || busy || accountLoadBlocked || healthProgress.running" @click="emit('health', null)">测活全部（跳过已封号）</button>
      <button class="btn secondary" :disabled="!selected.length || busy || accountLoadBlocked" @click="emit('trial-check', { keys: selected, force: false })">检测资格选中</button>
      <button class="btn secondary" :disabled="!accounts.length || busy || accountLoadBlocked" @click="emit('trial-check', { all_accounts: true, force: false })">检测全部 Free 资格</button>
      <label class="reauth-mode-field"><span>重新登录方式</span><select v-model="reauthMode" :disabled="busy"><option value="protocol">协议</option><option value="browser">浏览器（手动选择）</option></select></label>
      <label class="reauth-mode-field reauth-concurrency-field"><span>登录并发</span><input v-model.number="reauthConcurrency" type="number" min="1" :disabled="busy || reauthProgress.running" /></label>
      <button class="btn secondary" :disabled="!selectedReauthKeys.length || busy || reauthProgress.running" :title="reauthMode === 'protocol' ? '仅处理同时保存密码和 TOTP 的账号' : '处理可恢复账号'" @click="emit('reauth', { keys: selectedReauthKeys, mode: reauthMode, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">重新登录选中（{{ selectedReauthKeys.length }}）</button>
      <button class="btn secondary" :disabled="!reauthTargetKeys.length || busy || reauthProgress.running" :title="reauthMode === 'protocol' ? '仅处理同时保存密码和 TOTP 的账号' : '处理可恢复账号'" @click="emit('reauth', { keys: reauthTargetKeys, mode: reauthMode, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">重新登录可恢复（{{ reauthTargetKeys.length }}）</button>
      <button class="btn primary" :disabled="!selectedSecurityRepairKeys.length || busy || reauthProgress.running" @click="emit('security-repair', { keys: selectedSecurityRepairKeys, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">补全选中安全字段（{{ selectedSecurityRepairKeys.length }}）</button>
      <button class="btn primary" :disabled="!securityRepairKeys.length || busy || reauthProgress.running" @click="emit('security-repair', { keys: securityRepairKeys, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">补全缺失安全字段（{{ securityRepairKeys.length }}）</button>
      <button class="btn secondary" :disabled="!selectedSecurityOperationKeys.length || busy || reauthProgress.running" title="为选中账号设置新的密码" @click="emit('password-change', { keys: selectedSecurityOperationKeys, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">修改选中密码（{{ selectedSecurityOperationKeys.length }}）</button>
      <button class="btn secondary" :disabled="!selectedSecurityOperationKeys.length || busy || reauthProgress.running" title="停用旧因子并重新启用 TOTP 2FA" @click="emit('totp-change', { keys: selectedSecurityOperationKeys, concurrency: Math.max(1, Math.floor(Number(reauthConcurrency) || 3)) })">修改选中 2FA（{{ selectedSecurityOperationKeys.length }}）</button>
      <button class="btn danger" :disabled="!invalidCount || busy" @click="emit('delete-invalid', invalidKeys)">删除失效（{{ invalidCount }}）</button>
      <select v-model="moveTarget" aria-label="移动目标分组"><option v-for="group in groups" :key="group.id" :value="group.id">移至：{{ group.name }}</option></select>
      <button class="btn secondary" :disabled="!selected.length || !moveTarget || busy" @click="moveSelected">移动选中</button>
      <button class="btn danger" :disabled="!selected.length || busy" @click="emit('delete', selected)">删除选中</button>
      <button class="btn danger" :disabled="!accounts.length || busy" @click="emit('delete', null)">全部删除</button>
    </div>

    <div class="table-wrap">
      <table class="acct">
        <thead><tr><th></th><th>邮箱 / ID</th><th>分组</th><th>注册方式</th><th>套餐</th><th>测活状态</th><th>0元资格</th><th>额度</th><th>Token</th><th>密码</th><th>2FA</th><th>2FA 解码</th><th>代理</th><th>注册时间</th><th>最近刷新</th><th>最后失效</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-if="!pageItems.length"><td colspan="17" class="muted account-empty">没有匹配的账号</td></tr>
          <tr v-for="item in pageItems" :key="item.delete_key">
            <td data-label="选择"><input v-model="selected" type="checkbox" :value="item.delete_key" /></td>
            <td data-label="邮箱 / ID"><strong class="account-email">{{ item.email || "-" }}</strong><code class="account-id">{{ item.account_id || "无 Account ID" }}</code><span v-if="item.cdk_redeemed" class="account-redeemed-mark" :title="`兑换时间：${fmtTime(item.cdk_redeemed_at)}`">CDK 已兑换</span></td>
            <td data-label="分组"><span class="tag group-tag">{{ item.group_name || "默认分组" }}</span></td>
            <td data-label="注册方式"><span class="tag">{{ registrationText(item) }}</span></td>
            <td data-label="套餐"><span :class="['tag', 'plan-tag', planType(item)]" :title="`测活识别套餐：${planName(item)}`">{{ planName(item) }}</span></td>
            <td data-label="测活状态"><span :class="['tag', statusClass(item)]">{{ statusName(item) }}</span><small v-if="item.health_error" class="health-error" :title="item.health_error">{{ item.health_error }}</small></td>
            <td data-label="0元资格"><span :class="['tag', 'trial-eligibility-tag', trialClass(item)]" :title="item.trial_eligibility_error || item.trial_eligibility_reason || ''">{{ trialLabel(item) }}</span><small v-if="item.trial_eligibility_checked_at" class="upload-state">{{ fmtTime(item.trial_eligibility_checked_at) }}</small></td>
            <td data-label="额度">{{ quotaText(item.quota) }}</td>
            <td data-label="Token"><button class="token-at-btn" type="button" :disabled="!item.has_access_token || copyingAt" :title="item.has_access_token ? '点击复制 Access Token' : '该账号没有 AT'" @click="emit('copy-at', item)"><span :class="['token-dot', item.has_access_token ? 'yes' : 'no']"></span>{{ copyingAt ? "…" : "AT" }}</button><small v-if="item.has_refresh_token" class="upload-state success">Refresh</small><small v-if="item.c2a_upload_status" :class="['upload-state', item.c2a_upload_status]">c2a {{ item.c2a_upload_status === 'success' ? '已上传' : '失败' }}</small></td>
            <td data-label="密码"><button class="credential-copy-btn" type="button" :disabled="!item.has_password" :title="item.has_password ? '点击复制完整密码' : '该账号没有已保存的密码'" @click="emit('copy-credential', item, 'password')"><span :class="['tag', item.has_password ? 'ok' : 'warn']">{{ item.has_password ? (item.password_masked || "已设置") : "缺失" }}</span></button></td>
            <td data-label="2FA"><button class="credential-copy-btn" type="button" :disabled="!item.has_totp" :title="item.has_totp ? '点击复制完整 TOTP 密钥' : '该账号没有已保存的 TOTP 密钥'" @click="emit('copy-credential', item, 'totp_secret')"><span :class="['tag', item.mfa_enabled ? 'ok' : '']">{{ mfaText(item) }}</span><small v-if="item.totp_secret_masked" class="upload-state">{{ item.totp_secret_masked }}</small></button></td>
            <td data-label="2FA 解码"><button v-if="item.has_totp" class="totp-code-btn" type="button" :disabled="totpLoading[item.delete_key]" :title="totpEntry(item)?.error || (totpEntry(item)?.code ? '点击复制当前 2FA 验证码' : '点击获取当前 2FA 验证码')" @click="handleTotpClick(item)"><strong>{{ totpDisplay(item) }}</strong><small v-if="totpEntry(item)?.code">点击复制</small></button><span v-else class="tag">未设置</span></td>
            <td data-label="代理"><span :class="['tag', 'proxy-country-tag', { unknown: item.proxy && !proxyDisplay(item.proxy).country }]" :title="item.proxy || '未使用代理'">{{ proxyDisplay(item.proxy).label }}</span></td>
            <td data-label="注册时间">{{ fmtTime(item.registered_at || item.updated_at) }}</td>
            <td data-label="最近刷新">{{ fmtTime(item.health_checked_at || item.updated_at) }}</td>
            <td data-label="最后失效">{{ fmtTime(item.last_invalid_at) }}</td>
            <td data-label="操作"><div class="account-row-actions"><button class="btn secondary account-security-icon" type="button" :disabled="busy || !item.delete_key" title="查看账号操作日志" aria-label="查看账号操作日志" @click="emit('account-logs', item)"><ClipboardList :size="14" aria-hidden="true" /></button><button class="btn secondary account-cdk-btn" type="button" :disabled="busy || !cdkEligible(item)" :title="cdkTitle(item)" @click="emit('generate-cdk', item)">CDK</button><button class="btn secondary account-security-icon" type="button" :disabled="busy || reauthProgress.running || !item.delete_key || item.password_confirmed === false || !item.has_password || !item.has_totp || item.is_deactivated || (item.password_confirmed === undefined && item.security_repair_available && item.has_totp)" title="修改密码" aria-label="修改密码" @click="emit('password-change', { keys: [item.delete_key], concurrency: 1 })"><KeyRound :size="14" aria-hidden="true" /></button><button class="btn secondary account-security-icon" type="button" :disabled="busy || reauthProgress.running || !item.delete_key || item.password_confirmed === false || !item.has_password || !item.has_totp || item.is_deactivated || (item.password_confirmed === undefined && item.security_repair_available && item.has_totp)" title="修改 TOTP 2FA" aria-label="修改 TOTP 2FA" @click="emit('totp-change', { keys: [item.delete_key], concurrency: 1 })"><ShieldCheck :size="14" aria-hidden="true" /></button></div></td>
          </tr>
        </tbody>
      </table>
    </div>
    <AppPager v-model:page="page" :pages="pages" :total="filtered.length" />
      </div>
    </div>
  </section>
</template>
