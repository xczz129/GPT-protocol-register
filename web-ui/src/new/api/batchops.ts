/* --- 批量操作 API：接码(codex)/导入/重新登录/安全修复/改密/改2FA/刷新AT/试用检测 --- */
import { api, jsonBody } from "./client";

/* --- 类型 --- */
export interface CodexLogLine {
  t: string;
  level: string;
  m: string;
}

export interface CodexResultItem {
  email: string;
  status: string;
  refresh_token?: string;
  token_kind?: string;
  expires_in?: number;
  error?: string;
  skipped?: boolean;
  logs?: CodexLogLine[];
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
}

export interface CodexItem {
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

export interface CodexHistoryEntry {
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
  last?: string;
  lastStatus?: string;
  lastMessage?: string;
  items?: ReauthJobItem[];
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
  const jobId = job.job_id || "";
  return {
    running: ["queued", "running", "stopping"].includes(job.status || ""),
    total: Number(job.total || 0),
    done: Number(job.done || 0),
    current: job.current || "",
    failed: Number(job.failed || 0),
    stopped: Number(job.stopped || 0),
    job_id: jobId,
    jobId,
  };
}

function refreshAtProgress(response: BatchProgressResponse): RefreshAtProgress {
  const job = unwrapBatchJob(response);
  return {
    ...batchProgress(job),
    refreshed: Number(job.refreshed || 0),
    unchanged: Number(job.unchanged || 0),
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
export function codexRun(emails: string[], concurrency: number, force = false, smsOptions?: SmsOptions) {
  return api<CodexStartResult>("/api/codex-oauth/run", {
    method: "POST",
    body: jsonBody({ emails, concurrency, force, ...(smsOptions ? { sms_options: smsOptions } : {}) }),
  });
}

export function preflightCodex(payload: { emails: string[]; concurrency?: number; force?: boolean; sms_options?: SmsOptions }) {
  return api<CodexPreflight>("/api/codex-oauth/preflight", { method: "POST", body: jsonBody(payload) });
}

export const codexPreflight = preflightCodex;

export function codexImport(text: string, accountType: string, workspace: string, concurrency: number) {
  return api<CodexStartResult>("/api/codex-oauth/import", {
    method: "POST",
    body: jsonBody({ text, account_type: accountType, workspace, concurrency }),
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

/* --- 试用检测 --- */
export async function trialStart(keys: string[], allAccounts: boolean, force: boolean, concurrency: number): Promise<TrialProgress> {
  return trialProgress(await api<BatchProgressResponse>("/api/accounts/trial-eligibility", {
    method: "POST",
    body: jsonBody({ keys, all_accounts: allAccounts, force, concurrency }),
  }));
}

export async function fetchTrialProgress(jobId?: string): Promise<TrialProgress> {
  const suffix = jobId ? encodeURIComponent(jobId) : "progress";
  return trialProgress(await api<BatchProgressResponse>(`/api/accounts/trial-eligibility/${suffix}`));
}
