/* --- 账号 store：渐进分页加载（revision 一致性校验）、筛选、选择、测活 ---
   账号行是不可变快照，使用 shallowRef 避免数千行深度代理的开销；
   任何更新都整体替换数组（参考旧版 applyHealthProgress 的模式）。 */
import { computed, shallowRef, ref } from "vue";
import { defineStore } from "pinia";
import {
  deleteAccounts, deleteInvalidAccounts, fetchAccountsPage, fetchHealthProgress,
  resumeHealthCheck, startHealthCheck, type Account, type HealthProgress,
} from "../api/accounts";

const FIRST_PAGE = 200;
const PAGE_SIZE = 1000;

export const useAccountsStore = defineStore("accounts", () => {
  /* --- 列表数据 --- */
  const rows = shallowRef<Account[]>([]);
  const total = ref(0);
  const loading = ref(false);
  const loadError = ref("");
  const loadProgress = ref({ loaded: 0, total: 0 });

  async function load() {
    loading.value = true;
    loadError.value = "";
    try {
      const first = await fetchAccountsPage(FIRST_PAGE, 0);
      const revision = first.revision;
      const collected: Account[] = [...first.items];
      total.value = first.total;
      loadProgress.value = { loaded: collected.length, total: first.total };
      // 首批先上屏，其余页后台续载；revision 变化说明库在写，整体重来
      rows.value = collected;
      let offset = first.returned;
      while (offset < first.total) {
        const page = await fetchAccountsPage(PAGE_SIZE, offset);
        if (page.revision !== revision) {
          return await load();
        }
        collected.push(...page.items);
        offset += page.returned;
        if (!page.returned) break;
        loadProgress.value = { loaded: collected.length, total: first.total };
        rows.value = [...collected];
      }
    } catch (error) {
      loadError.value = error instanceof Error ? error.message : "加载失败";
    } finally {
      loading.value = false;
    }
  }

  /* --- 筛选与分页（视图状态） --- */
  const query = ref("");
  const statusFilter = ref("all");
  const planFilter = ref("all");
  const trialFilter = ref("all");
  const smsFilter = ref("all");
  const groupFilter = ref("all");
  const page = ref(1);
  const pageSize = ref(20);

  const filtered = computed(() => {
    const needle = query.value.trim().toLowerCase();
    return rows.value.filter((account) => {
      if (needle && !`${account.email} ${account.account_id} ${account.proxy}`.toLowerCase().includes(needle)) {
        return false;
      }
      if (statusFilter.value !== "all") {
        const health = String(account.health_status || "unknown").toLowerCase();
        const known = ["alive", "limited", "reauth_required", "invalid", "error"];
        if (statusFilter.value === "deactivated" && !account.is_deactivated) return false;
        if (statusFilter.value === "usable" && (account.is_deactivated || health !== "alive")) return false;
        if (statusFilter.value === "limited" && (account.is_deactivated || health !== "limited")) return false;
        if (statusFilter.value === "invalid" && (account.is_deactivated || health !== "invalid")) return false;
        if (statusFilter.value === "error" && (account.is_deactivated || !["error", "reauth_required"].includes(health))) return false;
        if (statusFilter.value === "unchecked" && (account.is_deactivated || known.includes(health))) return false;
      }
      if (planFilter.value !== "all" && (account.plan_type || "free") !== planFilter.value) return false;
      if (trialFilter.value !== "all" && (account.trial_eligibility_status || "") !== trialFilter.value) return false;
      if (smsFilter.value === "sms" && !account.has_refresh_token) return false;
      if (smsFilter.value === "no-sms" && account.has_refresh_token) return false;
      if (groupFilter.value !== "all" && account.group_id !== groupFilter.value) return false;
      return true;
    });
  });

  const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)));
  const pageRows = computed(() => {
    const current = Math.min(page.value, pageCount.value);
    return filtered.value.slice((current - 1) * pageSize.value, current * pageSize.value);
  });

  function resetPage() {
    page.value = 1;
  }

  /* --- 全量统计（不选中也可用的批量入口，口径与旧版一致） --- */
  /** 未封号账号数（测活全部的范围） */
  const activeCount = computed(() => rows.value.filter((a) => !a.is_deactivated).length);

  /** 待处理 keys：测活失败 ∪ AT过期/缺失，未封号 —— 刷新AT/重新登录"全部"按钮共用口径 */
  const atRiskKeys = computed(() =>
    rows.value
      .filter((a) => {
        if (!a.delete_key || a.is_deactivated) return false;
        const health = String(a.health_status || "").toLowerCase();
        return ["invalid", "error", "reauth_required"].includes(health) || a.at_expired || !a.has_access_token;
      })
      .map((a) => a.delete_key),
  );

  /** 待处理中可走协议重登的 keys（需同时保存密码和 TOTP 2FA） */
  const protocolReauthKeys = computed(() => {
    const atRisk = new Set(atRiskKeys.value);
    return rows.value
      .filter((a) => a.delete_key && atRisk.has(a.delete_key) && a.has_password && a.has_totp)
      .map((a) => a.delete_key);
  });

  /** 测活失效（invalid）且未封号的 keys —— 旧版"删除失效"按钮口径 */
  const invalidKeys = computed(() =>
    rows.value
      .filter((a) => a.delete_key && !a.is_deactivated && String(a.health_status || "").toLowerCase() === "invalid")
      .map((a) => a.delete_key),
  );

  /* --- 选择 --- */
  const selectedKeys = shallowRef<string[]>([]);
  const selectedSet = computed(() => new Set(selectedKeys.value));

  function toggle(key: string) {
    const set = new Set(selectedKeys.value);
    if (set.has(key)) set.delete(key);
    else set.add(key);
    selectedKeys.value = [...set];
  }

  function toggleAll(keys: string[]) {
    const set = new Set(selectedKeys.value);
    const allSelected = keys.length > 0 && keys.every((key) => set.has(key));
    if (allSelected) keys.forEach((key) => set.delete(key));
    else keys.forEach((key) => set.add(key));
    selectedKeys.value = [...set];
  }

  function clearSelection() {
    selectedKeys.value = [];
  }

  /* --- 测活进度 --- */
  const health = shallowRef<HealthProgress>({
    running: false, channel: "v4", done: 0, total: 0, actualTotal: 0, requested: 0, checked: 0,
    usable: 0, alive: 0, limited: 0, invalid: 0, invalidOther: 0, reauthRequired: 0,
    error: 0, skippedKnownDeactivated: 0, pendingCount: 0, current: "", message: "",
    interrupted: false, workers: 0, requestedWorkers: 0, updates: {},
  });
  const healthError = ref("");
  const healthSubmitting = ref(false);
  let healthTimer: ReturnType<typeof setTimeout> | null = null;
  let healthGeneration = 0;
  let healthInFlight: { generation: number; controller: AbortController; promise: Promise<HealthProgress | null> } | null = null;
  let healthMonitoring = false;
  let healthFailures = 0;
  let healthAwaitingStart = false;
  let healthPreviousStartedAt = "";
  let healthLoadQueued = false;

  function clearHealthTimer() {
    if (healthTimer !== null) { clearTimeout(healthTimer); healthTimer = null; }
  }

  function stopHealthPolling() {
    clearHealthTimer();
    healthMonitoring = false;
    healthGeneration += 1;
    healthInFlight?.controller.abort();
  }

  function scheduleHealthPoll() {
    clearHealthTimer();
    // The health POST remains open until the whole check finishes. Progress
    // must keep polling while that request is pending.
    if (!healthMonitoring) return;
    const generation = healthGeneration;
    const delay = Math.min(10000, healthFailures ? 1000 * (2 ** Math.min(healthFailures - 1, 3)) : 700);
    healthTimer = setTimeout(() => {
      healthTimer = null;
      if (generation === healthGeneration && healthMonitoring) void requestHealthProgress(generation);
    }, delay);
  }

  function applyHealthProgress(next: HealthProgress, generation: number) {
    if (generation !== healthGeneration) return;
    // A GET started before POST /health may return the previous completed
    // snapshot. Ignore it until the server exposes the new run.
    if (healthAwaitingStart && !next.running && next.startedAt === healthPreviousStartedAt) return;
    if (healthAwaitingStart && (next.running || next.startedAt !== healthPreviousStartedAt)) healthAwaitingStart = false;
    const wasRunning = health.value.running;
    health.value = next;
    healthError.value = "";
    healthFailures = 0;
    if (next.running) {
      healthMonitoring = true;
    } else {
      healthMonitoring = false;
      clearHealthTimer();
      if (wasRunning && !healthLoadQueued) {
        healthLoadQueued = true;
        void load().finally(() => { healthLoadQueued = false; });
      }
    }
  }

  function requestHealthProgress(generation: number): Promise<HealthProgress | null> {
    if (generation !== healthGeneration) return Promise.resolve(null);
    if (healthInFlight?.generation === generation) return healthInFlight.promise;
    clearHealthTimer();
    const request = { generation, controller: new AbortController(), promise: null as unknown as Promise<HealthProgress | null> };
    healthInFlight = request;
    request.promise = (async () => {
      const timeout = setTimeout(() => request.controller.abort(), 15000);
      try {
        const next = await fetchHealthProgress(false, request.controller.signal);
        applyHealthProgress(next, generation);
        return next;
      } catch (error) {
        if (generation !== healthGeneration) return null;
        healthFailures += 1;
        healthError.value = error instanceof Error ? error.message : "测活进度暂时不可用，正在重连";
        healthMonitoring = true;
        return null;
      } finally {
        clearTimeout(timeout);
        if (healthInFlight === request) healthInFlight = null;
        if (generation === healthGeneration) scheduleHealthPoll();
      }
    })();
    return request.promise;
  }

  /** Refresh once and keep polling while the server task is running. */
  function refreshHealth(): Promise<HealthProgress | null> {
    healthMonitoring = true;
    return requestHealthProgress(healthGeneration);
  }

  async function launchHealth(start: () => Promise<unknown>, requested: number, workers: number) {
    if (healthSubmitting.value || health.value.running) throw new Error("已有测活任务正在运行，请等待完成");
    healthSubmitting.value = true;
    healthGeneration += 1;
    const generation = healthGeneration;
    clearHealthTimer();
    healthInFlight?.controller.abort();
    healthMonitoring = true;
    healthFailures = 0;
    healthPreviousStartedAt = health.value.startedAt || "";
    healthAwaitingStart = true;
    // Show feedback before the long-running POST returns. The first progress
    // snapshot replaces this optimistic state as soon as the server accepts it.
    health.value = {
      ...health.value, running: true, requested, total: requested, actualTotal: requested,
      done: 0, checked: 0, workers, requestedWorkers: workers, pendingCount: 0,
      current: "", message: "正在启动测活…", interrupted: false, updates: {},
    };
    void requestHealthProgress(generation);
    try {
      await start();
      healthAwaitingStart = false;
      await requestHealthProgress(generation);
    } catch (error) {
      // A rejected POST may mean the server refused to start, or only that
      // its response was lost while the worker keeps running. Reconcile with
      // a fresh GET and stop ignoring the previous idle snapshot. Invalidate
      // any GET sent before the rejection so it cannot undo that result.
      healthAwaitingStart = false;
      if (generation === healthGeneration) {
        healthGeneration += 1;
        clearHealthTimer();
        healthInFlight?.controller.abort();
        await requestHealthProgress(healthGeneration);
      }
      throw error;
    } finally {
      healthSubmitting.value = false;
      if (healthMonitoring) scheduleHealthPoll();
    }
  }

  async function startHealth(keys: string[], deleteAll: boolean, workers = 20, channel: "v4" | "nerver" = "v4") {
    const requested = deleteAll ? Math.max(activeCount.value, 1) : Math.max(keys.length, 1);
    health.value = { ...health.value, channel };
    await launchHealth(() => startHealthCheck(keys, deleteAll, workers, channel), requested, workers);
  }

  async function resumeHealth(workers = 20, channel: "v4" | "nerver" = health.value.channel) {
    const requested = Math.max(health.value.pendingCount, 1);
    health.value = { ...health.value, channel };
    await launchHealth(() => resumeHealthCheck(workers, channel), requested, workers);
  }

  /* --- 删除后同步 --- */
  async function remove(keys: string[], deleteAll = false) {
    const result = await deleteAccounts(keys, deleteAll);
    clearSelection();
    await load();
    return result;
  }

  async function removeInvalid(keys: string[], deleteAll = false) {
    const result = await deleteInvalidAccounts(keys, deleteAll);
    clearSelection();
    await load();
    return result;
  }

  return {
    rows, total, loading, loadError, loadProgress, load,
    query, statusFilter, planFilter, trialFilter, smsFilter, groupFilter,
    page, pageSize, filtered, pageCount, pageRows, resetPage,
    activeCount, atRiskKeys, protocolReauthKeys, invalidKeys,
    selectedKeys, selectedSet, toggle, toggleAll, clearSelection,
    health, healthError, healthSubmitting, refreshHealth, startHealth, resumeHealth,
    remove, removeInvalid,
  };
});
