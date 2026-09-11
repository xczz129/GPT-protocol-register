/* --- 批量操作状态：codex/导入批次、reauth 家族批次、刷新AT、试用检测 --- */
import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";
import {
  codexImport, codexRun, codexStop, fetchCodexStatus, fetchReauthProgress,
  fetchRefreshAtProgress, fetchTrialProgress, passwordChangeStart, preflightCodex, reauthByEmailStart, reauthStart,
  reauthStop, refreshAtStart, securityRepairStart, totpChangeStart, trialStart,
  type CodexRunOptions, type CodexStatus, type ProtocolLoginStartResult, type ReauthProgress,
  type RefreshAtProgress, type SmsOptions, type TrialProgress,
} from "../api/batchops";

export const useBatchOpsStore = defineStore("batchops", () => {
  /* --- codex/导入批次 --- */
  const codex = ref<CodexStatus>({
    running: false, mode: "", total: 0, done: 0, success: 0, failed: 0, logs: [], results: [],
  });
  const codexDialogOpen = ref(false);
  const codexStartOpen = ref(false);
  const codexStartEmails = ref<string[]>([]);
  const codexStartForce = ref(false);
  const codexHistoryOpen = ref(false);
  const codexHistoryEmails = ref<string[]>([]);
  const lastCodexOptions = reactive<CodexRunOptions>({ concurrency: 3, force: false });
  const codexConnection = ref<"connected" | "reconnecting" | "offline">("connected");
  const codexLastUpdated = ref(0);
  const codexError = ref("");
  const codexStartBusy = ref(false);
  const codexStopBusy = ref(false);
  let codexTimer: ReturnType<typeof setTimeout> | null = null;
  let codexGeneration = 0;
  let codexMonitoring = false;
  let codexFailures = 0;
  let codexInFlight: { generation: number; controller: AbortController; promise: Promise<CodexStatus | null> } | null = null;
  const codexFinished = new Set<string>();
  let codexFinishedCallback: (() => void | Promise<unknown>) | null = null;
  let codexDialogFinishedCallback: (() => void) | undefined;

  /* 导入失败重试用的原始凭据行缓存：email -> 原始行 */
  const lastImportLines = ref(new Map<string, string>());
  const lastImportOptions = reactive({ accountType: "personal", workspace: "", concurrency: 1 });

  function number(value: unknown, fallback = 0) {
    const parsed = Number(value);
    return value !== undefined && value !== null && Number.isFinite(parsed) ? Math.max(0, parsed) : fallback;
  }

  function cancelledItem(item: { status?: string; error?: string }) {
    return item.status === "cancelled" || (item.status === "failed" && /^(?:任务已(?:停止|取消)|用户(?:停止|取消)|cancel(?:led|ed)\b)/i.test(item.error || ""));
  }

  const codexSummary = computed(() => {
    const value = codex.value;
    const counts = value.counts || {};
    const results = value.results || [];
    const items = value.items || [];
    const reused = number(counts.skipped, results.filter((item) => item.skipped || item.status === "skipped").length);
    const legacyCancelled = results.filter(cancelledItem).length;
    const newSuccess = number(counts.success, Math.max(0, number(value.success) - reused));
    const failed = number(counts.failed, Math.max(0, number(value.failed) - legacyCancelled));
    const cancelled = number(counts.cancelled, number(value.cancelled, legacyCancelled));
    const active = number(counts.active, items.filter((item) => item.status === "active").length);
    const done = Math.max(number(counts.done, number(value.done)), newSuccess + reused + failed + cancelled);
    const total = Math.max(number(counts.total, number(value.total)), done + active);
    const pending = number(counts.pending, Math.max(0, total - done - active));
    return { newSuccess, reused, failed, cancelled, pending, active, total, done };
  });

  function normalizedEmails(emails: string[]) {
    return [...new Set(emails.map((email) => String(email || "").trim().toLowerCase()).filter(Boolean))];
  }

  function clearCodexTimer() {
    if (codexTimer !== null) {
      clearTimeout(codexTimer);
      codexTimer = null;
    }
  }

  /** Explicit disposal only. Closing a dialog must not stop task monitoring. */
  function stopCodexPolling() {
    clearCodexTimer();
    codexMonitoring = false;
    codexGeneration++;
    codexInFlight?.controller.abort();
  }

  function scheduleCodexPoll() {
    clearCodexTimer();
    if (!codexMonitoring || codexStartBusy.value || codexStopBusy.value) return;
    const generation = codexGeneration;
    const delay = Math.min(15000, 2000 * (2 ** Math.min(codexFailures, 3)));
    codexTimer = setTimeout(() => {
      codexTimer = null;
      if (generation === codexGeneration && codexMonitoring) void requestCodexStatus(generation);
    }, delay);
  }

  function saveCodexOptions(options?: CodexRunOptions, status?: CodexStatus) {
    if (!options && !status) return;
    lastCodexOptions.concurrency = options?.concurrency ?? status?.concurrency ?? lastCodexOptions.concurrency;
    lastCodexOptions.force = options?.force ?? status?.force ?? lastCodexOptions.force;
    if (options?.sms_options) lastCodexOptions.sms_options = { ...options.sms_options };
  }

  function finishCodexOnce(status: CodexStatus) {
    if (status.running || (!status.total && !status.batch_id && !status.results?.length)) return;
    const key = status.batch_id || `${status.mode}:${status.started_at || `local-${codexGeneration}`}`;
    if (codexFinished.has(key)) return;
    codexFinished.add(key);
    const callback = codexFinishedCallback || onFinishedRefresh;
    try {
      Promise.resolve(callback()).catch(() => {
        codexError.value = "任务已结束，但账号列表刷新失败，请刷新账号列表";
      });
      if (codexDialogFinishedCallback && codexDialogFinishedCallback !== callback) {
        codexDialogFinishedCallback();
      }
    } catch {
      codexError.value = "任务已结束，但账号列表刷新失败，请刷新账号列表";
    }
    codexDialogFinishedCallback = undefined;
  }

  function applyCodexStatus(status: CodexStatus) {
    const running = Boolean(status.running || ["queued", "running", "stopping"].includes(status.status || ""));
    codex.value = {
      mode: "", total: 0, done: 0, success: 0, failed: 0, logs: [], results: [],
      ...status, running, stopping: running && Boolean(status.stopping || status.status === "stopping"),
    };
    if (status.mode !== "import") saveCodexOptions(status.options, status);
    codexLastUpdated.value = Date.now();
    codexConnection.value = "connected";
    codexFailures = 0;
    codexError.value = "";
    codexMonitoring = running;
    finishCodexOnce(codex.value);
  }

  function requestCodexStatus(generation: number): Promise<CodexStatus | null> {
    if (generation !== codexGeneration || codexStartBusy.value || codexStopBusy.value) return Promise.resolve(null);
    if (codexInFlight) {
      if (codexInFlight.generation === generation) return codexInFlight.promise;
      // A new batch invalidates the previous response, but never overlaps its fetch.
      return codexInFlight.promise.then(() => requestCodexStatus(generation));
    }
    clearCodexTimer();
    const request = { generation, controller: new AbortController(), promise: null as unknown as Promise<CodexStatus | null> };
    codexInFlight = request;
    request.promise = (async () => {
      const timeout = setTimeout(() => request.controller.abort(), 15000);
      try {
        const status = await fetchCodexStatus(request.controller.signal);
        if (generation !== codexGeneration) return null;
        applyCodexStatus(status);
        return codex.value;
      } catch (error) {
        if (generation !== codexGeneration) return null;
        codexFailures++;
        codexConnection.value = codexFailures >= 3 ? "offline" : "reconnecting";
        codexError.value = error instanceof Error ? error.message : "任务状态暂时不可用，正在自动重连";
        codexMonitoring = true;
        return null;
      } finally {
        clearTimeout(timeout);
        if (codexInFlight === request) codexInFlight = null;
        if (generation === codexGeneration) scheduleCodexPoll();
      }
    })();
    return request.promise;
  }

  /** Both manual refresh and background polls use the same single-flight request. */
  function refreshCodexStatus(): Promise<CodexStatus | null> {
    codexMonitoring = true;
    return requestCodexStatus(codexGeneration);
  }

  /** Restore running or completed results after a reload, without opening a modal. */
  async function restoreCodex(): Promise<boolean> {
    const status = await refreshCodexStatus();
    return Boolean(status?.running);
  }

  async function openCodexDialog(onFinished?: () => void) {
    if (onFinished) codexDialogFinishedCallback = onFinished;
    codexDialogOpen.value = true;
    await refreshCodexStatus();
  }

  async function openCodexDialogIfRunning(): Promise<boolean> {
    const status = await refreshCodexStatus();
    if (status?.running) codexDialogOpen.value = true;
    return Boolean(status?.running);
  }

  function openCodexStart(emails: string[], force = false) {
    codexStartEmails.value = normalizedEmails(emails);
    codexStartForce.value = force;
    codexStartOpen.value = true;
  }

  function openCodexHistory(emails: string[] = []) {
    codexHistoryEmails.value = normalizedEmails(emails);
    codexHistoryOpen.value = true;
  }

  async function launchCodexBatch(
    mode: string, total: number,
    launch: () => Promise<{ batch_id?: string; total?: number; options?: CodexRunOptions }>,
    accepted: () => void,
  ) {
    if (codexStartBusy.value || codex.value.running) throw new Error("已有接码或导入任务，请等待任务结束");
    codexStartBusy.value = true;
    stopCodexPolling();
    codexFailures = 0;
    codexError.value = "";
    let launchError: unknown;
    try {
      const result = await launch();
      accepted();
      saveCodexOptions(result?.options);
      codex.value = {
        running: true, mode, status: "running", total: result?.total ?? total,
        done: 0, success: 0, failed: 0, logs: [], results: [], batch_id: result?.batch_id,
      };
      codexDialogOpen.value = true;
      codexStartOpen.value = false;
    } catch (error) {
      launchError = error;
    } finally {
      codexStartBusy.value = false;
    }
    // Even if the POST response was lost, reconcile the server before allowing retry.
    await refreshCodexStatus();
    if (launchError) {
      codexError.value = launchError instanceof Error ? launchError.message : "任务启动失败";
      throw launchError;
    }
  }

  async function runCodex(emails: string[], concurrency: number, force = false, smsOptions?: SmsOptions) {
    const requested = normalizedEmails(emails);
    if (!requested.length) throw new Error("请先选择需要接码的账号");
    const options: CodexRunOptions = { concurrency, force, ...(smsOptions ? { sms_options: { ...smsOptions } } : {}) };
    await launchCodexBatch("codex", requested.length, () => codexRun(requested, concurrency, force, smsOptions), () => {
      delete lastCodexOptions.sms_options;
      saveCodexOptions(options);
    });
  }

  async function stopCodex(): Promise<boolean> {
    if (codexStopBusy.value || !codex.value.running || codex.value.stopping) return false;
    codexStopBusy.value = true;
    stopCodexPolling();
    let stopError: unknown;
    try {
      await codexStop();
      codex.value = { ...codex.value, status: "stopping", stopping: true };
    } catch (error) {
      stopError = error;
    } finally {
      codexStopBusy.value = false;
    }
    await refreshCodexStatus();
    if (stopError) {
      codexError.value = stopError instanceof Error ? stopError.message : "停止请求失败，请重试";
      return false;
    }
    return true;
  }

  function failedCodexEmails(onlyEmail?: string) {
    const results = [...(codex.value.results || []), ...(codex.value.items || [])];
    const failed = normalizedEmails(results.filter((item) => item.status === "failed" && !cancelledItem(item)
      && !("skipped" in item && item.skipped)).map((item) => item.email));
    if (!onlyEmail) return failed;
    const email = String(onlyEmail || "").trim().toLowerCase();
    return failed.includes(email) ? [email] : [];
  }

  async function retryCodexFailed(onlyEmail?: string) {
    if (codex.value.mode === "import") throw new Error("此批次为账号导入，请使用导入重试");
    if (codex.value.running || codexStartBusy.value) throw new Error("请等待当前任务结束后重试");
    const emails = failedCodexEmails(onlyEmail);
    if (!emails.length) throw new Error("没有可重试的失败账号");
    const options: CodexRunOptions = {
      concurrency: lastCodexOptions.concurrency, force: lastCodexOptions.force,
      ...(lastCodexOptions.sms_options ? { sms_options: { ...lastCodexOptions.sms_options } } : {}),
    };
    codexStartBusy.value = true;
    let preview;
    try {
      preview = await preflightCodex({ emails, ...options });
    } finally {
      codexStartBusy.value = false;
    }
    if (preview.not_found?.length || preview.missing_credentials?.length || preview.deactivated?.length
      || preview.total !== emails.length) {
      throw new Error("部分失败账号已删除、停用或缺少登录凭据，请先处理后重试；本次未启动");
    }
    await runCodex(emails, options.concurrency, options.force, options.sms_options);
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
    await launchCodexBatch("import", rows.size,
      () => codexImport(value, payload.accountType, payload.workspace, payload.concurrency), () => {
        lastImportLines.value = rows;
        lastImportOptions.accountType = payload.accountType;
        lastImportOptions.workspace = payload.workspace;
        lastImportOptions.concurrency = payload.concurrency;
      });
  }

  function canRetryImport(onlyEmail?: string): boolean {
    if (codex.value.mode !== "import" || codex.value.running || codexStartBusy.value) return false;
    const emails = failedCodexEmails(onlyEmail);
    return emails.length > 0 && emails.every((email) => lastImportLines.value.has(email));
  }

  /** 重试导入批次中的失败账号（用缓存的原始凭据行） */
  async function retryImportFailed(onlyEmail?: string) {
    if (codex.value.mode !== "import") throw new Error("此批次为接码任务，请使用接码重试");
    if (codex.value.running || codexStartBusy.value) throw new Error("请等待当前任务结束后重试");
    const emails = failedCodexEmails(onlyEmail);
    if (!emails.length) throw new Error("没有可重试的失败账号");
    if (!canRetryImport(onlyEmail)) throw new Error("部分失败账号的原始导入行已丢失，请重新粘贴导入；本次未启动");
    const lines = emails.map((email) => lastImportLines.value.get(email)!);
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

  function setOnCodexFinished(callback: () => void | Promise<unknown>) {
    codexFinishedCallback = callback;
  }

  return {
    codex, codexDialogOpen, codexStartOpen, codexStartEmails, codexStartForce, codexHistoryOpen, codexHistoryEmails, lastCodexOptions,
    codexConnection, codexLastUpdated, codexError, codexStartBusy, codexStopBusy, codexSummary,
    lastImportLines, lastImportOptions, canRetryImport,
    openCodexStart, openCodexHistory, openCodexDialog, openCodexDialogIfRunning, restoreCodex, refreshCodexStatus,
    runCodex, retryCodexFailed, stopCodex, startImport, retryImportFailed, stopCodexPolling,
    reauth, reauthDialogOpen, startReauthBatch, startProtocolLogin, attachReauthIfRunning, stopReauthBatch, stopReauthPolling,
    refreshAt, startRefreshAt, stopRefreshAtPolling,
    trial, startTrial, stopTrialPolling,
    setOnFinished, setOnCodexFinished,
  };
});
