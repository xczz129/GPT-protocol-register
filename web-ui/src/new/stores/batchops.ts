/* --- 批量操作状态：codex/导入批次、reauth 家族批次、刷新AT、试用检测 --- */
import { computed, reactive, ref } from "vue";
import { defineStore } from "pinia";
import {
  codexImport, codexRun, codexStop, fetchCodexStatus, fetchReauthProgress,
  fetchRefreshAtProgress, fetchTrialProgress, passwordChangeStart, preflightCodex, reauthByEmailStart, reauthStart,
  reauthStop, refreshAtStart, refreshAtStop, securityRepairStart, totpChangeStart, trialStart, trialStop,
  cancelledImportResult, parseImportCredentials, importCredentialError, validImportConcurrency, IMPORT_MAX_CONCURRENCY,
  type CodexRunOptions, type CodexStatus, type ProtocolLoginStartResult, type ReauthProgress,
  type RefreshAtProgress, type RelayAction, type RelayStrategy, type SmsOptions, type TrialChannel, type TrialProgress,
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
  const codexStartStrategy = ref<RelayStrategy>("auto");
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
    return cancelledImportResult(item);
  }

  const codexSummary = computed(() => {
    const value = codex.value;
    const counts = value.counts || {};
    const results = value.results || [];
    const items = value.items || [];
    const reused = number(counts.skipped, results.filter((item) => item.skipped || item.status === "skipped").length);
    const synced = number(counts.synced, results.filter((item) => item.outcome === "synced" || item.status === "synced" || (item.action === "sync_only" && item.relay_status === "synced")).length);
    const legacyCancelled = results.filter((item) => item.status === "failed" && cancelledItem(item)).length;
    const resultCancelled = results.filter(cancelledItem).length;
    const newSuccess = number(counts.success, Math.max(0, number(value.success) - reused - synced));
    const failed = number(counts.failed, Math.max(0, number(value.failed) - legacyCancelled));
    const cancelled = number(counts.cancelled, number(value.cancelled, resultCancelled));
    const active = number(counts.active, items.filter((item) => item.status === "active").length);
    const done = Math.max(number(counts.done, number(value.done)), newSuccess + synced + reused + failed + cancelled);
    const total = Math.max(number(counts.total, number(value.total)), done + active);
    const pending = number(counts.pending, Math.max(0, total - done - active));
    return { newSuccess, synced, reused, failed, cancelled, pending, active, total, done };
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
    if (options?.strategy) lastCodexOptions.strategy = options.strategy;
    if (options?.account_actions) lastCodexOptions.account_actions = { ...options.account_actions };
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

  function openCodexStart(emails: string[], force = false, strategy?: RelayStrategy) {
    codexStartEmails.value = normalizedEmails(emails);
    codexStartForce.value = force;
    codexStartStrategy.value = strategy || (force ? "reauthorize" : "auto");
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

  async function runCodex(emails: string[], concurrency: number, force = false, smsOptions?: SmsOptions, strategy?: RelayStrategy, accountActions?: Record<string, RelayAction>) {
    const requested = normalizedEmails(emails);
    if (!requested.length) throw new Error("请先选择需要接码的账号");
    const options: CodexRunOptions = { concurrency, force, ...(smsOptions ? { sms_options: { ...smsOptions } } : {}), ...(strategy ? { strategy } : {}), ...(accountActions ? { account_actions: { ...accountActions } } : {}) };
    await launchCodexBatch("codex", requested.length, () => codexRun(requested, concurrency, force, smsOptions, strategy, accountActions), () => {
      delete lastCodexOptions.sms_options;
      delete lastCodexOptions.strategy;
      delete lastCodexOptions.account_actions;
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

  function failedCodexEmails(onlyEmail?: string | string[]) {
    const results = [...(codex.value.results || []), ...(codex.value.items || [])];
    const failed = normalizedEmails(results.filter((item) => {
      const status = String(item.status || "").toLowerCase();
      const outcome = String((item as { outcome?: string }).outcome || "").toLowerCase();
      const relayFailed = String((item as { relay_status?: string }).relay_status || "").toLowerCase() === "failed";
      return (status === "failed" || status === "error" || status === "failure" || outcome === "failed" || relayFailed)
        && !cancelledItem(item)
        && !("skipped" in item && item.skipped);
    }).map((item) => item.email));
    if (!onlyEmail) return failed;
    const requested = Array.isArray(onlyEmail) ? normalizedEmails(onlyEmail) : [String(onlyEmail).trim().toLowerCase()];
    return requested.filter((email) => failed.includes(email));
  }

  async function retryCodexFailed(onlyEmail?: string | string[]) {
    if (codex.value.mode === "import") throw new Error("此批次为账号导入，请使用导入重试");
    if (codex.value.running || codexStartBusy.value) throw new Error("请等待当前任务结束后重试");
    const oldProvider = codex.value.options?.sms_options?.provider || lastCodexOptions.sms_options?.provider;
    if (oldProvider && oldProvider !== "smsbower") throw new Error("旧接码渠道仅供历史查看，请重新配置 SMSBower");
    const emails = failedCodexEmails(onlyEmail);
    if (!emails.length) throw new Error("没有可重试的失败账号");
    const options: CodexRunOptions = {
      concurrency: lastCodexOptions.concurrency, force: lastCodexOptions.force,
      ...(lastCodexOptions.sms_options ? { sms_options: { ...lastCodexOptions.sms_options } } : {}),
      ...(lastCodexOptions.strategy ? { strategy: lastCodexOptions.strategy } : {}),
    };
    const actions: Record<string, RelayAction> = {};
    for (const email of emails) {
      const result = (codex.value.results || []).find(item => String(item.email || "").trim().toLowerCase() === email);
      const action = result?.retry_action || lastCodexOptions.account_actions?.[email];
      if (action) actions[email] = action;
    }
    if (Object.keys(actions).length) options.account_actions = actions;
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
    await runCodex(emails, options.concurrency, options.force, options.sms_options, options.strategy, options.account_actions);
  }

  /** 导入账号（协议登录 密码+TOTP） */
  async function startImport(payload: { text: string; accountType: string; workspace: string; concurrency: number }) {
    const value = String(payload.text || "");
    const parsed = parseImportCredentials(value);
    if (parsed.errors.length) throw new Error(importCredentialError(parsed.errors));
    if (!parsed.rows.length) throw new Error("请填写需要导入的账号");
    if (!validImportConcurrency(payload.concurrency)) throw new Error(`导入并发必须为 1–${IMPORT_MAX_CONCURRENCY} 的整数`);
    const rows = new Map(parsed.rows.map((row) => [row.email, row.line]));
    await launchCodexBatch("import", rows.size,
      () => codexImport(value, payload.accountType, payload.workspace, payload.concurrency), () => {
        lastImportLines.value = rows;
        lastImportOptions.accountType = payload.accountType;
        lastImportOptions.workspace = payload.workspace;
        lastImportOptions.concurrency = payload.concurrency;
      });
  }

  function canRetryImport(onlyEmail?: string | string[]): boolean {
    if (codex.value.mode !== "import" || codex.value.running || codexStartBusy.value) return false;
    const emails = failedCodexEmails(onlyEmail);
    return emails.length > 0 && emails.every((email) => lastImportLines.value.has(email));
  }

  /** 重试导入批次中的失败账号（用缓存的原始凭据行） */
  async function retryImportFailed(onlyEmail?: string | string[]) {
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
    running: false, total: 0, done: 0, success: 0, failed: 0, cancelled: 0, skipped: 0, items: [],
  });
  const reauthDialogOpen = ref(false);
  const reauthConnection = ref<"connected" | "reconnecting" | "offline">("connected");
  const reauthLastUpdated = ref(0);
  const reauthError = ref("");
  let reauthTimer: ReturnType<typeof setInterval> | null = null;
  let reauthPolling = false;
  let reauthFailures = 0;
  let reauthGeneration = 0;

  function applyReauth(job: ReauthProgress | null | undefined) {
    if (!job) {
      reauth.running = false;
      return;
    }
    const status = String(job.status || "").toLowerCase();
    Object.assign(reauth, job, {
      // Older jobs do not carry skipped; never retain another batch's count
      // or skipped rows when applying a complete progress snapshot.
      skipped: number(job.skipped),
      cancelled: number(job.cancelled),
      items: Array.isArray(job.items) ? job.items : [],
      current: job.current || "",
      last: job.last || "",
      // Keep the backend's snake_case diagnostics available to components
      // using the existing camelCase fields.
      lastStatus: job.lastStatus || job.last_status || "",
      lastMessage: job.lastMessage || job.last_message || "",
      running: status ? ["queued", "running", "stopping"].includes(status) : Boolean(job.running),
      stopping: Boolean(job.stopping || status === "stopping"),
    });
    reauthLastUpdated.value = Date.now();
    reauthConnection.value = "connected";
    reauthFailures = 0;
    reauthError.value = "";
  }

  function stopReauthPolling() {
    reauthGeneration += 1;
    if (reauthTimer) {
      clearInterval(reauthTimer);
      reauthTimer = null;
    }
    reauthPolling = false;
  }

  async function refreshReauthProgress(generation = reauthGeneration) {
    if (reauthPolling) return;
    if (generation !== reauthGeneration) return;
    reauthPolling = true;
    try {
      const { job } = await fetchReauthProgress();
      if (generation !== reauthGeneration) return;
      applyReauth(job);
      if (!reauth.running) stopReauthPolling();
    } catch (error) {
      reauthFailures += 1;
      reauthConnection.value = reauthFailures >= 3 ? "offline" : "reconnecting";
      reauthError.value = error instanceof Error ? error.message : "重新登录进度暂时不可用，正在重试";
    } finally {
      reauthPolling = false;
    }
  }

  function startReauthPolling(onFinished?: () => void) {
    stopReauthPolling();
    const generation = reauthGeneration;
    reauthConnection.value = "connected";
    reauthError.value = "";
    reauthTimer = setInterval(async () => {
      await refreshReauthProgress(generation);
      if (!reauth.running) {
        stopReauthPolling();
        onFinished?.();
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

  /** Reconnect to a running reauth batch after a transient network failure. */
  async function restoreReauth(): Promise<boolean> {
    await refreshReauthProgress();
    if (reauth.running) {
      reauthDialogOpen.value = true;
      startReauthPolling(onFinishedRefresh);
    }
    return reauth.running;
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
    if (!jobId || !reauth.running) return false;
    const result = await reauthStop(String(jobId));
    // Mark stopping immediately; the next poll will reconcile the terminal
    // state and prevents a second click from issuing duplicate stop requests.
    Object.assign(reauth, {
      status: "stopping",
      running: true,
      stopping: true,
      ...(result && typeof result === "object" ? result : {}),
    });
    startReauthPolling(onFinishedRefresh);
    return true;
  }

  /* --- 刷新 AT --- */
  const refreshAt = reactive<RefreshAtProgress>({
    running: false, total: 0, done: 0, refreshed: 0, unchanged: 0, failed: 0, stopped: 0,
  });
  const refreshAtDialogOpen = ref(false);
  const refreshAtConnection = ref<"connected" | "reconnecting" | "offline">("connected");
  const refreshAtLastUpdated = ref(0);
  const refreshAtError = ref("");
  let refreshAtFailures = 0;
  let refreshAtTimer: ReturnType<typeof setInterval> | null = null;
  let refreshAtPolling = false;
  let refreshAtGeneration = 0;

  function stopRefreshAtPolling() {
    refreshAtGeneration += 1;
    if (refreshAtTimer) {
      clearInterval(refreshAtTimer);
      refreshAtTimer = null;
    }
    refreshAtPolling = false;
  }

  async function refreshRefreshAtProgress(generation = refreshAtGeneration) {
    if (refreshAtPolling) return;
    if (generation !== refreshAtGeneration) return;
    refreshAtPolling = true;
    try {
      const progress = await fetchRefreshAtProgress(refreshAt.job_id || refreshAt.jobId);
      if (generation !== refreshAtGeneration || !refreshAtTimer) return;
      Object.assign(refreshAt, progress);
      refreshAtLastUpdated.value = Date.now();
      refreshAtConnection.value = "connected";
      refreshAtFailures = 0;
      refreshAtError.value = "";
      if (!refreshAt.running) {
        stopRefreshAtPolling();
        onFinishedRefresh();
      }
    } catch (error) {
      refreshAtFailures += 1;
      refreshAtConnection.value = refreshAtFailures >= 3 ? "offline" : "reconnecting";
      refreshAtError.value = error instanceof Error ? error.message : "刷新 AT 进度暂时不可用，正在重试";
    } finally {
      refreshAtPolling = false;
    }
  }

  function startRefreshAtPolling() {
    stopRefreshAtPolling();
    const generation = refreshAtGeneration;
    refreshAtConnection.value = "connected";
    refreshAtError.value = "";
    const timer = setInterval(async () => { await refreshRefreshAtProgress(generation); }, 1500);
    refreshAtTimer = timer;
    // The POST response already contains the queued state; the first poll is
    // intentionally left to the timer so callers can finish rendering before
    // another request is issued.
  }

  async function startRefreshAt(keys: string[], allAccounts: boolean, concurrency: number) {
    stopRefreshAtPolling();
    refreshAtDialogOpen.value = true;
    refreshAtConnection.value = "connected";
    refreshAtError.value = "";
    Object.assign(refreshAt, await refreshAtStart(keys, allAccounts, concurrency));
    startRefreshAtPolling();
  }

  async function stopRefreshAt(): Promise<boolean> {
    const jobId = String(refreshAt.job_id || refreshAt.jobId || "");
    if (!jobId || !refreshAt.running) return false;
    try {
      Object.assign(refreshAt, await refreshAtStop(jobId));
      startRefreshAtPolling();
      return true;
    } catch (error) {
      refreshAtError.value = error instanceof Error ? error.message : "停止刷新 AT 失败";
      throw error;
    }
  }

  async function restoreRefreshAt(): Promise<boolean> {
    try {
      const progress = await fetchRefreshAtProgress();
      Object.assign(refreshAt, progress);
      refreshAtLastUpdated.value = Date.now();
      if (refreshAt.running) {
        refreshAtDialogOpen.value = true;
        startRefreshAtPolling();
      }
      return refreshAt.running;
    } catch (error) {
      refreshAtConnection.value = "reconnecting";
      refreshAtError.value = error instanceof Error ? error.message : "刷新 AT 进度暂时不可用";
      return false;
    }
  }

  /* --- 试用检测 --- */
  const trial = reactive<TrialProgress>({
    running: false, total: 0, done: 0, eligible: 0, ineligible: 0, failed: 0, stopped: 0,
  });
  const trialDialogOpen = ref(false);
  const trialConnection = ref<"connected" | "reconnecting" | "offline">("connected");
  const trialLastUpdated = ref(0);
  const trialError = ref("");
  let trialFailures = 0;
  let trialTimer: ReturnType<typeof setInterval> | null = null;
  let trialPolling = false;
  let trialGeneration = 0;

  function stopTrialPolling() {
    trialGeneration += 1;
    if (trialTimer) {
      clearInterval(trialTimer);
      trialTimer = null;
    }
    trialPolling = false;
  }

  async function refreshTrialProgress(generation = trialGeneration) {
    if (trialPolling) return;
    if (generation !== trialGeneration) return;
    trialPolling = true;
    try {
      const progress = await fetchTrialProgress(trial.job_id || trial.jobId);
      if (generation !== trialGeneration || !trialTimer) return;
      Object.assign(trial, progress);
      trialLastUpdated.value = Date.now();
      trialConnection.value = "connected";
      trialFailures = 0;
      trialError.value = "";
      if (!trial.running) {
        stopTrialPolling();
        onFinishedRefresh();
      }
    } catch (error) {
      trialFailures += 1;
      trialConnection.value = trialFailures >= 3 ? "offline" : "reconnecting";
      trialError.value = error instanceof Error ? error.message : "试用检测进度暂时不可用，正在重试";
    } finally {
      trialPolling = false;
    }
  }

  function startTrialPolling() {
    stopTrialPolling();
    const generation = trialGeneration;
    trialConnection.value = "connected";
    trialError.value = "";
    const timer = setInterval(async () => { await refreshTrialProgress(generation); }, 1500);
    trialTimer = timer;
  }

  async function startTrial(
    keys: string[],
    allAccounts: boolean,
    force: boolean,
    concurrency: number,
    channel: TrialChannel = "v4",
  ) {
    stopTrialPolling();
    trialDialogOpen.value = true;
    trialConnection.value = "connected";
    trialError.value = "";
    Object.assign(trial, await trialStart(keys, allAccounts, force, concurrency, channel));
    startTrialPolling();
  }

  async function stopTrial(): Promise<boolean> {
    const jobId = String(trial.job_id || trial.jobId || "");
    if (!jobId || !trial.running) return false;
    try {
      Object.assign(trial, await trialStop(jobId));
      startTrialPolling();
      return true;
    } catch (error) {
      trialError.value = error instanceof Error ? error.message : "停止试用检测失败";
      throw error;
    }
  }

  async function restoreTrial(): Promise<boolean> {
    try {
      const progress = await fetchTrialProgress();
      Object.assign(trial, progress);
      trialLastUpdated.value = Date.now();
      if (trial.running) {
        trialDialogOpen.value = true;
        startTrialPolling();
      }
      return trial.running;
    } catch (error) {
      trialConnection.value = "reconnecting";
      trialError.value = error instanceof Error ? error.message : "试用检测进度暂时不可用";
      return false;
    }
  }

  /** Restore all background account jobs after mounting the account page. */
  async function restoreBackgroundBatches(): Promise<void> {
    await Promise.allSettled([restoreReauth(), restoreRefreshAt(), restoreTrial()]);
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
    codex, codexDialogOpen, codexStartOpen, codexStartEmails, codexStartForce, codexStartStrategy, codexHistoryOpen, codexHistoryEmails, lastCodexOptions,
    codexConnection, codexLastUpdated, codexError, codexStartBusy, codexStopBusy, codexSummary,
    lastImportLines, lastImportOptions, canRetryImport,
    openCodexStart, openCodexHistory, openCodexDialog, openCodexDialogIfRunning, restoreCodex, refreshCodexStatus,
    runCodex, retryCodexFailed, stopCodex, startImport, retryImportFailed, stopCodexPolling,
    reauth, reauthDialogOpen, reauthConnection, reauthLastUpdated, reauthError,
    startReauthBatch, startProtocolLogin, restoreReauth, attachReauthIfRunning, refreshReauthProgress, stopReauthBatch, stopReauthPolling,
    refreshAt, refreshAtDialogOpen, refreshAtConnection, refreshAtLastUpdated, refreshAtError,
    startRefreshAt, restoreRefreshAt, refreshRefreshAtProgress, stopRefreshAt, stopRefreshAtPolling,
    trial, trialDialogOpen, trialConnection, trialLastUpdated, trialError,
    startTrial, restoreTrial, refreshTrialProgress, stopTrial, stopTrialPolling, restoreBackgroundBatches,
    setOnFinished, setOnCodexFinished,
  };
});
