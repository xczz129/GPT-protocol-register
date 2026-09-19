/* --- 批量操作 API：接码(codex)/导入/重新登录/安全修复/改密/改2FA/刷新AT/试用检测 --- */
import { api, jsonBody } from "./client";

export const IMPORT_MAX_CONCURRENCY = 10;

/** Shared by the paste preview, retry cache and API boundary. Never drop bad rows. */
export function parseImportCredentials(text: string) {
  const rows: { email: string; line: string; lineNumber: number }[] = [];
  const errors: string[] = [];
  const seen = new Set<string>();
  let total = 0;
  String(text || "").split(/\r\n|\n|\r/).forEach((raw, index) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    total++;
    const parts = line.split("----").map((part) => part.trim());
    const label = `第 ${index + 1} 行`;
    if (parts.length !== 3 || parts.some((part) => !part)) {
      errors.push(`${label}格式错误：需要邮箱、密码、2FA 密钥三列`);
      return;
    }
    const [email, password, rawSecret] = parts;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      errors.push(`${label}邮箱格式错误`);
      return;
    }
    const secret = rawSecret.replace(/\s/g, "").toUpperCase();
    const unpadded = secret.replace(/=+$/, "");
    // Match Python b32decode after the backend adds missing padding.
    const padding = secret.length - unpadded.length + ((8 - secret.length % 8) % 8);
    if (!/^[A-Z2-7]+={0,6}$/.test(secret) || ![0, 1, 3, 4, 6].includes(padding)) {
      errors.push(`${label}2FA 密钥不是有效 Base32`);
      return;
    }
    const key = email.toLowerCase();
    if (seen.has(key)) {
      errors.push(`${label}邮箱重复`);
      return;
    }
    seen.add(key);
    rows.push({ email: key, line: [email, password, secret].join("----"), lineNumber: index + 1 });
  });
  return { rows, errors, total };
}

export function importCredentialError(errors: string[]) {
  return errors.slice(0, 10).join("；") + (errors.length > 10 ? "；还有其他错误行" : "");
}

export function validImportConcurrency(value: number) {
  return Number.isInteger(value) && value >= 1 && value <= IMPORT_MAX_CONCURRENCY;
}

export function cancelledImportResult(item: { status?: string; error?: string }) {
  return item.status === "cancelled" || (item.status === "failed"
    && /^(?:任务已(?:停止|取消)|用户(?:停止|取消)|cancel(?:led|ed)\b|stop requested\b)/i.test(item.error || ""));
}

/* --- 类型 --- */
export interface CodexLogLine {
  t: string;
  level: string;
  m: string;
}

export type RelayStrategy = "auto" | "sync_only" | "reauthorize";
export type RelayAction = "sync_only" | "reauthorize";

export interface RelayOutcome {
  action?: string;
  relay_status?: string;
  relay_file?: string;
  authorization_status?: string;
  retry_action?: RelayAction;
}

export interface CodexResultItem extends RelayOutcome {
  email: string;
  status: string;
  refresh_token?: string;
  token_kind?: string;
  expires_in?: number;
  error?: string;
  skipped?: boolean;
  logs?: CodexLogLine[];
  outcome?: string;
}

/** Per-run overrides contain public settings only; API keys stay on the server. */
export interface SmsOptions {
  provider?: string;
  country_order?: string;
  max_price?: string;
  max_phone_attempts?: number;
  timeout_seconds?: number;
}

export interface CodexRunOptions {
  concurrency: number;
  force: boolean;
  sms_options?: SmsOptions;
  strategy?: RelayStrategy;
  account_actions?: Record<string, RelayAction>;
}

export interface CodexItem extends RelayOutcome {
  email: string;
  status: string;
  stage?: string;
  message?: string;
  attempt?: number;
  max_phone_attempts?: number;
  wait_deadline?: number | null;
  updated_at?: string | number;
}

export interface CodexCounts {
  total: number;
  done: number;
  success: number;
  skipped: number;
  failed: number;
  cancelled: number;
  pending: number;
  active: number;
  synced?: number;
}

export interface CodexStatus {
  running: boolean;
  mode: string;
  total: number;
  done: number;
  success: number;
  failed: number;
  logs: CodexLogLine[];
  results: CodexResultItem[];
  batch_id?: string;
  status?: string;
  stopping?: boolean;
  cancelled?: number;
  force?: boolean;
  concurrency?: number;
  options?: CodexRunOptions;
  items?: CodexItem[];
  counts?: Partial<CodexCounts>;
  started_at?: string;
  finished_at?: string;
}

export interface CodexPreflight {
  ok: boolean;
  total: number;
  eligible: number;
  skipped: number;
  missing_credentials: { email: string; missing: string[] }[];
  not_found: string[];
  deactivated: string[];
  limit: number;
  configured: boolean;
  emails: string[];
  sync_needed?: number;
  relay_ready?: number;
  reauthorize?: number;
  relay_configured?: boolean;
  sms_required_if_challenged?: boolean;
  relay_issues?: { email: string; reason: string; relay_status: string }[];
  action_items?: { email: string; action: RelayAction | "ready" | "blocked"; reason: string; relay_status: string }[];
  settings: SmsOptions & {
    provider: string;
    provider_label: string;
    enabled: boolean;
    configured: boolean;
    country_order: string;
    max_price: string;
    max_phone_attempts: number;
    timeout_seconds: number;
    price_cap_supported: boolean;
  };
}

export interface CodexHistoryEntry extends RelayOutcome {
  at: string;
  status: string;
  phone?: string;
  rt?: string;
  error?: string;
  note?: string;
  skipped?: boolean;
  outcome?: string;
  logs?: CodexLogLine[];
  batch_id?: string;
  options?: CodexRunOptions;
  phone_attempts?: { phone?: string; status?: string; [key: string]: unknown }[];
}

export interface CodexStartResult {
  ok?: boolean;
  batch_id?: string;
  total?: number;
  concurrency?: number;
  options?: CodexRunOptions;
}

export interface ReauthJobItem {
  key?: string;
  email?: string;
  status?: string;
  message?: string;
}

export interface ReauthProgress {
  job_id?: string;
  jobId?: string;
  status?: string;
  running: boolean;
  stopping?: boolean;
  mode?: string;
  operation?: string;
  concurrency?: number;
  total: number;
  done: number;
  current?: string;
  success: number;
  failed: number;
  cancelled?: number;
  skipped?: number;
  last?: string;
  lastStatus?: string;
  lastMessage?: string;
  items?: ReauthJobItem[];
  /** Backend uses snake_case; these aliases keep API snapshots lossless. */
  last_status?: string;
  last_message?: string;
}

export interface RefreshAtProgress {
  running: boolean;
  total: number;
  done: number;
  current?: string;
  refreshed: number;
  unchanged: number;
  failed: number;
  stopped: number;
  jobId?: string;
  job_id?: string;
  status?: string;
  stage?: string;
  items?: Record<string, unknown>[];
  pendingCount?: number;
  interrupted?: boolean;
  message?: string;
}

export interface TrialProgress {
  running: boolean;
  total: number;
  done: number;
  current?: string;
  eligible: number;
  ineligible: number;
  failed: number;
  stopped: number;
  jobId?: string;
  job_id?: string;
  interrupted?: boolean;
  pendingCount?: number;
  message?: string;
  status?: string;
  stage?: string;
}

type BatchJob = Partial<RefreshAtProgress & TrialProgress> & {
  status?: string;
  stage?: string;
  pending_keys?: string[];
};
type BatchProgressResponse = BatchJob | { ok?: boolean; job: BatchJob | null } | null;

function unwrapBatchJob(response: BatchProgressResponse): BatchJob {
  if (!response) return {};
  if ("job" in response) return response.job || {};
  return response;
}

function batchProgress(job: BatchJob) {
  const jobId = String(job.job_id || job.jobId || "");
  const status = String(job.status || "").toLowerCase();
  return {
    // Older workers only returned ``running``. Prefer an explicit terminal
    // status when present, while retaining that compatibility for snapshots.
    running: status
      ? ["queued", "running", "stopping"].includes(status)
      : Boolean(job.running),
    status: status || undefined,
    stage: String(job.stage || ""),
    total: Number(job.total || 0),
    done: Number(job.done || 0),
    current: job.current || "",
    failed: Number(job.failed || 0),
    stopped: Number(job.stopped || 0),
    job_id: jobId,
    jobId,
    pendingCount: Array.isArray(job.pending_keys) ? job.pending_keys.length : 0,
    interrupted: status === "interrupted",
    message: String(job.stage || ""),
  };
}

function refreshAtProgress(response: BatchProgressResponse): RefreshAtProgress {
  const job = unwrapBatchJob(response);
  return {
    ...batchProgress(job),
    refreshed: Number(job.refreshed || 0),
    unchanged: Number(job.unchanged || 0),
    stopped: Number(job.stopped || 0),
    items: Array.isArray(job.items) ? job.items : [],
  };
}

function trialProgress(response: BatchProgressResponse): TrialProgress {
  const job = unwrapBatchJob(response);
  return {
    ...batchProgress(job),
    eligible: Number(job.eligible || 0),
    ineligible: Number(job.ineligible || 0),
    interrupted: job.status === "interrupted",
    pendingCount: (job.pending_keys || []).length,
    message: job.stage || "",
    status: String(job.status || "") || undefined,
    stage: String(job.stage || ""),
  };
}

export interface ImportHistoryEntry {
  at: string;
  batch_id: string;
  account_type: string;
  workspace: string;
  email: string;
  status: string;
  error: string;
  logs: CodexLogLine[];
}

/* --- 接码 / 导入（共用 codex 批次状态） --- */
export function codexRun(emails: string[], concurrency: number, force = false, smsOptions?: SmsOptions, strategy?: RelayStrategy, accountActions?: Record<string, RelayAction>) {
  if (!Number.isSafeInteger(concurrency) || concurrency < 1) throw new Error("并发数必须为正整数");
  if (smsOptions?.provider && smsOptions.provider !== "smsbower") throw new Error("旧接码渠道仅供历史查看，请重新配置 SMSBower");
  return api<CodexStartResult>("/api/codex-oauth/run", {
    method: "POST",
    body: jsonBody({ emails, concurrency, force, ...(smsOptions ? { sms_options: smsOptions } : {}), ...(strategy ? { strategy } : {}), ...(accountActions ? { account_actions: accountActions } : {}) }),
  });
}

export function preflightCodex(payload: { emails: string[]; concurrency?: number; force?: boolean; sms_options?: SmsOptions; strategy?: RelayStrategy; account_actions?: Record<string, RelayAction> }) {
  if (payload.concurrency !== undefined && (!Number.isSafeInteger(payload.concurrency) || payload.concurrency < 1)) throw new Error("并发数必须为正整数");
  if (payload.sms_options?.provider && payload.sms_options.provider !== "smsbower") throw new Error("旧接码渠道仅供历史查看，请重新配置 SMSBower");
  return api<CodexPreflight>("/api/codex-oauth/preflight", { method: "POST", body: jsonBody(payload) });
}

export const codexPreflight = preflightCodex;

export function codexImport(text: string, accountType: string, workspace: string, concurrency: number) {
  const parsed = parseImportCredentials(text);
  if (parsed.errors.length) throw new Error(importCredentialError(parsed.errors));
  if (!parsed.rows.length) throw new Error("请填写需要导入的账号");
  if (!validImportConcurrency(concurrency)) throw new Error(`导入并发必须为 1–${IMPORT_MAX_CONCURRENCY} 的整数`);
  return api<CodexStartResult>("/api/codex-oauth/import", {
    method: "POST",
    body: jsonBody({ text: parsed.rows.map((row) => row.line).join("\n"), account_type: accountType, workspace, concurrency }),
  });
}

export function fetchCodexStatus(signal?: AbortSignal): Promise<CodexStatus> {
  return api<CodexStatus>("/api/codex-oauth/status", signal ? { signal } : {});
}

export function codexStop() {
  return api("/api/codex-oauth/stop", { method: "POST" });
}

export function fetchImportHistory(limit = 300): Promise<{ entries: ImportHistoryEntry[] }> {
  return api<{ entries: ImportHistoryEntry[] }>(`/api/codex-oauth/import-history?limit=${limit}`);
}

export function fetchCodexHistory(emails?: string[]): Promise<{ ok: boolean; history: Record<string, CodexHistoryEntry[]> }> {
  const query = emails?.length ? `?emails=${encodeURIComponent(emails.join(","))}` : "";
  return api(`/api/codex-oauth/history${query}`);
}

/* --- 重新登录 / 安全修复 / 改密 / 改2FA（共用 reauth 批次状态） --- */
export function reauthStart(keys: string[], mode: string, concurrency: number) {
  return api("/api/accounts/reauth/start", {
    method: "POST",
    body: jsonBody({ keys, mode, concurrency }),
  });
}

export function securityRepairStart(keys: string[], concurrency: number) {
  return api("/api/accounts/security-repair/start", {
    method: "POST",
    body: jsonBody({ keys, mode: "protocol", concurrency }),
  });
}

export function passwordChangeStart(keys: string[], concurrency: number, passwordMode: string, newPassword: string, confirmPassword: string) {
  const payload: Record<string, unknown> = { keys, concurrency, password_mode: passwordMode };
  if (passwordMode === "custom") {
    payload.new_password = newPassword;
    payload.confirm_password = confirmPassword;
  }
  return api("/api/accounts/password-change/start", { method: "POST", body: jsonBody(payload) });
}

export function totpChangeStart(keys: string[], concurrency: number) {
  return api("/api/accounts/totp-change/start", {
    method: "POST",
    body: jsonBody({ keys, concurrency }),
  });
}

export function fetchReauthProgress(): Promise<{ job: ReauthProgress | null }> {
  return api<{ job: ReauthProgress | null }>("/api/accounts/reauth/progress");
}

export function reauthStop(jobId: string) {
  return api(`/api/accounts/reauth/${encodeURIComponent(jobId)}/stop`, { method: "POST" });
}

/* --- 协议登录（按邮箱，预检 + 启动合一） --- */
export interface ProtocolLoginStartResult extends ReauthProgress {
  requested?: number;
  matched?: number;
  submitted?: number;
  invalid_format?: string[];
  not_found?: string[];
  missing_credentials?: { email?: string; missing?: string[] }[];
  message?: string;
}

export function reauthByEmailStart(emails: string[], concurrency: number): Promise<ProtocolLoginStartResult> {
  return api<ProtocolLoginStartResult>("/api/accounts/reauth-by-email/start", {
    method: "POST",
    body: jsonBody({ emails, concurrency }),
  });
}

/* --- 刷新 AT --- */
export async function refreshAtStart(keys: string[], allAccounts: boolean, concurrency: number): Promise<RefreshAtProgress> {
  return refreshAtProgress(await api<BatchProgressResponse>("/api/accounts/refresh-at", {
    method: "POST",
    body: jsonBody({ keys, all_accounts: allAccounts, concurrency }),
  }));
}

export async function fetchRefreshAtProgress(jobId?: string): Promise<RefreshAtProgress> {
  // 完成后 active() 会变成 null；已知任务按 ID 查询，保留最终计数。
  const suffix = jobId ? encodeURIComponent(jobId) : "progress";
  return refreshAtProgress(await api<BatchProgressResponse>(`/api/accounts/refresh-at/${suffix}`));
}

export async function refreshAtStop(jobId: string): Promise<RefreshAtProgress> {
  return refreshAtProgress(await api<BatchProgressResponse>(
    `/api/accounts/refresh-at/${encodeURIComponent(jobId)}/stop`,
    { method: "POST" },
  ));
}

/* --- 试用检测 --- */
export type TrialChannel = "v4" | "nerver";

export async function trialStart(
  keys: string[],
  allAccounts: boolean,
  force: boolean,
  concurrency: number,
  channel: TrialChannel = "v4",
): Promise<TrialProgress> {
  return trialProgress(await api<BatchProgressResponse>("/api/accounts/trial-eligibility", {
    method: "POST",
    body: jsonBody({ keys, all_accounts: allAccounts, force, concurrency, channel: channel === "nerver" ? "nerver" : "v4" }),
  }));
}

export async function fetchTrialProgress(jobId?: string): Promise<TrialProgress> {
  const suffix = jobId ? encodeURIComponent(jobId) : "progress";
  return trialProgress(await api<BatchProgressResponse>(`/api/accounts/trial-eligibility/${suffix}`));
}

export async function trialStop(jobId: string): Promise<TrialProgress> {
  return trialProgress(await api<BatchProgressResponse>(
    `/api/accounts/trial-eligibility/${encodeURIComponent(jobId)}/stop`,
    { method: "POST" },
  ));
}
