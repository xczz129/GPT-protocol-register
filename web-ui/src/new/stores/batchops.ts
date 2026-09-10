/* --- 批量操作状态：codex/导入批次、reauth 家族批次、刷新AT、试用检测 ---
   每类批次一个轮询器，running 时 2s，页面隐藏自动暂停由 usePoller 思路延伸；
   这里手写 setInterval 保持与 accounts store 一致的模式。 */
import { reactive, ref } from "vue";
import { defineStore } from "pinia";
import {
  codexImport, codexRun, codexStop, fetchCodexStatus, fetchReauthProgress,
  fetchRefreshAtProgress, fetchTrialProgress, passwordChangeStart, reauthByEmailStart, reauthStart,
  reauthStop, refreshAtStart, securityRepairStart, totpChangeStart, trialStart,
  type CodexStatus, type ProtocolLoginStartResult, type ReauthProgress, type RefreshAtProgress, type TrialProgress,
} from "../api/batchops";

export const useBatchOpsStore = defineStore("batchops", () => {
  /* --- codex/导入批次 --- */
  const codex = ref<CodexStatus>({
    running: false, mode: "", total: 0, done: 0, success: 0, failed: 0, logs: [], results: [],
  });
  const codexDialogOpen = ref(false);
  let codexTimer: ReturnType<typeof setInterval> | null = null;

  /* 导入失败重试用的原始凭据行缓存：email -> 原始行 */
  const lastImportLines = ref(new Map<string, string>());
  const lastImportOptions = reactive({ accountType: "personal", workspace: "", concurrency: 1 });

  function stopCodexPolling() {
    if (codexTimer) {
      clearInterval(codexTimer);
      codexTimer = null;
    }
  }

  function startCodexPolling(onFinished?: () => void) {
    stopCodexPolling();
    codexTimer = setInterval(async () => {
      try {
        codex.value = await fetchCodexStatus();
        if (!codex.value.running) {
          stopCodexPolling();
          onFinished?.();
        }
      } catch {
        stopCodexPolling();
      }
    }, 2000);
  }

  /** 打开 codex 实时进度弹窗（启动后或重连后台批次时调用） */
  async function openCodexDialog(onFinished?: () => void) {
    try {
      codex.value = await fetchCodexStatus();
    } catch { /* keep stale */ }
    codexDialogOpen.value = true;
    startCodexPolling(onFinished);
  }

  /** 仅当后台有 codex/导入批次运行时重连实时进度；返回是否已接管 */
  async function openCodexDialogIfRunning(): Promise<boolean> {
    try {
      const status = await fetchCodexStatus();
      if (status?.running) {
        codex.value = status;
        codexDialogOpen.value = true;
        startCodexPolling(onFinishedRefresh);
        return true;
      }
    } catch { /* ignore */ }
    return false;
  }

  async function runCodex(emails: string[], concurrency: number, force = false) {
    await codexRun(emails, concurrency, force);
    await openCodexDialog();
  }

  async function stopCodex() {
    await codexStop();
  }

  /** 导入账号（协议登录 密码+TOTP） */
  async function startImport(payload: { text: string; accountType: string; workspace: string; concurrency: number }) {
    const value = String(payload.text || "").trim();
    if (!value) return;
    const rows = new Map<string, string>();
    for (const line of value.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const [email] = trimmed.split("----");
      if (email && email.trim()) rows.set(email.trim().toLowerCase(), trimmed);
    }
    lastImportLines.value = rows;
    lastImportOptions.accountType = payload.accountType;
    lastImportOptions.workspace = payload.workspace;
    lastImportOptions.concurrency = payload.concurrency;
    await codexImport(value, payload.accountType, payload.workspace, payload.concurrency);
    await openCodexDialog();
  }

  /** 重试导入批次中的失败账号（用缓存的原始凭据行） */
  async function retryImportFailed(onlyEmail?: string) {
    const failed = (codex.value.results || []).filter((item) => item.status !== "success");
    const emails = onlyEmail ? [onlyEmail] : failed.map((item) => item.email);
    const lines = emails
      .map((email) => lastImportLines.value.get(String(email || "").trim().toLowerCase()))
      .filter((line): line is string => Boolean(line));
    if (!lines.length) throw new Error("失败账号的原始导入行已丢失，请重新粘贴导入");
    await startImport({
      text: lines.join("\n"),
      accountType: lastImportOptions.accountType,
      workspace: lastImportOptions.workspace,
      concurrency: lastImportOptions.concurrency,
    });
  }

  /* --- reauth 家族批次（重新登录/安全修复/改密/改2FA 共用一个后端任务槽） --- */
  const reauth = reactive<ReauthProgress>({
    running: false, total: 0, done: 0, success: 0, failed: 0, items: [],
  });
  const reauthDialogOpen = ref(false);
  let reauthTimer: ReturnType<typeof setInterval> | null = null;

  function applyReauth(job: ReauthProgress | null | undefined) {
    if (!job) {
      reauth.running = false;
      return;
    }
    Object.assign(reauth, job, { running: ["queued", "running", "stopping"].includes(String(job.status || (job.running ? "running" : ""))) });
  }

  function stopReauthPolling() {
    if (reauthTimer) {
      clearInterval(reauthTimer);
      reauthTimer = null;
    }
  }

  function startReauthPolling(onFinished?: () => void) {
    stopReauthPolling();
    reauthTimer = setInterval(async () => {
      try {
        const { job } = await fetchReauthProgress();
        applyReauth(job);
        if (!reauth.running) {
          stopReauthPolling();
          onFinished?.();
        }
      } catch {
        stopReauthPolling();
      }
    }, 1500);
  }

  async function startReauthBatch(
    operation: "reauth" | "security_repair" | "password_change" | "totp_change",
    keys: string[],
    options: { concurrency: number; passwordMode?: string; newPassword?: string; confirmPassword?: string },
  ) {
    if (!keys.length) return;
    if (operation === "reauth") await reauthStart(keys, "protocol", options.concurrency);
    else if (operation === "security_repair") await securityRepairStart(keys, options.concurrency);
    else if (operation === "password_change") {
      await passwordChangeStart(keys, options.concurrency, options.passwordMode || "random", options.newPassword || "", options.confirmPassword || "");
    } else await totpChangeStart(keys, options.concurrency);
    const { job } = await fetchReauthProgress();
    applyReauth(job);
    reauthDialogOpen.value = true;
    startReauthPolling(onFinishedRefresh);
  }

  /** 协议登录（按邮箱）：预检+启动合一，返回预检摘要；有 job_id 时接管进度轮询 */
  async function startProtocolLogin(emails: string[], concurrency: number): Promise<ProtocolLoginStartResult> {
    const value = await reauthByEmailStart(emails, concurrency);
    applyReauth(value);
    if (value.job_id) startReauthPolling(onFinishedRefresh);
    return value;
  }

  /** 重连后台正在运行的 reauth 批次（如果有） */
  async function attachReauthIfRunning(): Promise<boolean> {
    try {
      const { job } = await fetchReauthProgress();
      applyReauth(job);
      if (reauth.running) {
        reauthDialogOpen.value = true;
        startReauthPolling(onFinishedRefresh);
        return true;
      }
    } catch { /* ignore */ }
    return false;
  }

  async function stopReauthBatch() {
    const jobId = reauth.job_id || reauth.jobId;
    if (jobId) await reauthStop(String(jobId));
  }

  /* --- 刷新 AT --- */
  const refreshAt = reactive<RefreshAtProgress>({
    running: false, total: 0, done: 0, refreshed: 0, unchanged: 0, failed: 0, stopped: 0,
  });
  let refreshAtTimer: ReturnType<typeof setInterval> | null = null;

  function stopRefreshAtPolling() {
    if (refreshAtTimer) {
      clearInterval(refreshAtTimer);
      refreshAtTimer = null;
    }
  }

  function startRefreshAtPolling() {
    stopRefreshAtPolling();
    const jobId = refreshAt.job_id || refreshAt.jobId;
    let polling = false;
    const timer = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const progress = await fetchRefreshAtProgress(jobId);
        if (refreshAtTimer !== timer) return;
        Object.assign(refreshAt, progress);
        if (!refreshAt.running) {
          stopRefreshAtPolling();
          onFinishedRefresh();
        }
      } catch {
        if (refreshAtTimer === timer) stopRefreshAtPolling();
      } finally {
        polling = false;
      }
    }, 1500);
    refreshAtTimer = timer;
  }

  async function startRefreshAt(keys: string[], allAccounts: boolean, concurrency: number) {
    Object.assign(refreshAt, await refreshAtStart(keys, allAccounts, concurrency));
    startRefreshAtPolling();
  }

  /* --- 试用检测 --- */
  const trial = reactive<TrialProgress>({
    running: false, total: 0, done: 0, eligible: 0, ineligible: 0, failed: 0, stopped: 0,
  });
  let trialTimer: ReturnType<typeof setInterval> | null = null;

  function stopTrialPolling() {
    if (trialTimer) {
      clearInterval(trialTimer);
      trialTimer = null;
    }
  }

  function startTrialPolling() {
    stopTrialPolling();
    const jobId = trial.job_id || trial.jobId;
    let polling = false;
    const timer = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const progress = await fetchTrialProgress(jobId);
        if (trialTimer !== timer) return;
        Object.assign(trial, progress);
        if (!trial.running) {
          stopTrialPolling();
          onFinishedRefresh();
        }
      } catch {
        if (trialTimer === timer) stopTrialPolling();
      } finally {
        polling = false;
      }
    }, 1500);
    trialTimer = timer;
  }

  async function startTrial(keys: string[], allAccounts: boolean, force: boolean, concurrency: number) {
    Object.assign(trial, await trialStart(keys, allAccounts, force, concurrency));
    startTrialPolling();
  }

  /* --- 批次完成后的账号列表刷新回调（由 accounts 视图注入） --- */
  let onFinishedRefresh: () => void = () => {};
  function setOnFinished(callback: () => void) {
    onFinishedRefresh = callback;
  }

  return {
    codex, codexDialogOpen, lastImportLines, lastImportOptions,
    openCodexDialog, openCodexDialogIfRunning, runCodex, stopCodex, startImport, retryImportFailed, stopCodexPolling,
    reauth, reauthDialogOpen, startReauthBatch, startProtocolLogin, attachReauthIfRunning, stopReauthBatch, stopReauthPolling,
    refreshAt, startRefreshAt, stopRefreshAtPolling,
    trial, startTrial, stopTrialPolling,
    setOnFinished,
  };
});
