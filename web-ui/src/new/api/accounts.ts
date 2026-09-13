/* --- 账号域 API：列表、测活、凭据、删除、导出、日志 --- */
import { api, downloadPost, jsonBody } from "./client";

/** 账号公开视图（与后端 account_public_view 对齐，不含原始密码/令牌） */
export interface Account {
  delete_key: string;
  email: string;
  has_password: boolean;
  password_confirmed: boolean;
  password_masked: string;
  mfa_enabled: boolean;
  has_totp: boolean;
  totp_secret_masked: string;
  security_repair_available: boolean;
  reauth_available: boolean;
  plan_type: string;
  account_id: string;
  proxy: string;
  registration_method: string;
  registration_state: string;
  registration_state_label: string;
  browser_type: string;
  group_id: string;
  group_name?: string;
  registered_at: string;
  updated_at: string;
  has_access_token: boolean;
  at_expired: boolean;
  at_expires_epoch: number;
  has_refresh_token: boolean;
  has_session_token: boolean;
  source_type: string;
  status: string;
  health_status: string;
  health_error: string;
  health_checked_at: string;
  is_deactivated: boolean;
  trial_eligibility_status: string;
  trial_eligibility_label: string;
  trial_eligibility_checked_at: string;
  quota: unknown;
  c2a_upload_status: string;
  [key: string]: unknown;
}

export interface AccountsPage {
  count: number;
  total: number;
  revision: number;
  offset: number;
  limit: number;
  returned: number;
  items: Account[];
}

export interface HealthProgress {
  running: boolean;
  channel: "v4" | "nerver";
  done: number;
  total: number;
  actualTotal: number;
  requested: number;
  checked: number;
  usable: number;
  alive: number;
  limited: number;
  invalid: number;
  invalidOther: number;
  reauthRequired: number;
  error: number;
  skippedKnownDeactivated: number;
  pendingCount: number;
  current: string;
  message: string;
  interrupted: boolean;
  startedAt?: string;
  finishedAt?: string;
  workers: number;
  requestedWorkers: number;
  updates: Record<string, { health_status?: string; status?: string }>;
}

export function fetchAccountsPage(limit: number, offset: number): Promise<AccountsPage> {
  return api<AccountsPage>(`/api/accounts?limit=${limit}&offset=${offset}`);
}

/* --- 测活 --- */
export function startHealthCheck(keys: string[], deleteAll: boolean, workers: number, channel: "v4" | "nerver" = "v4") {
  return api<Record<string, unknown>>("/api/accounts/health", {
    method: "POST",
    body: jsonBody({ keys, delete_all: deleteAll, workers, channel }),
  });
}

export function resumeHealthCheck(workers: number, channel: "v4" | "nerver" = "v4") {
  return api<Record<string, unknown>>("/api/accounts/health/resume", { method: "POST", body: jsonBody({ workers, channel }) });
}

const emptyHealthProgress = (): HealthProgress => ({
  running: false, channel: "v4", done: 0, total: 0, actualTotal: 0, requested: 0, checked: 0,
  usable: 0, alive: 0, limited: 0, invalid: 0, invalidOther: 0,
  reauthRequired: 0, error: 0, skippedKnownDeactivated: 0, pendingCount: 0,
  current: "", message: "", interrupted: false, workers: 0, requestedWorkers: 0,
  updates: {},
});

/** Normalize the snake_case response used by the Python route in one place.
 * Keeping this at the API boundary prevents a transient response from making
 * the progress component disappear or showing NaN/negative percentages. */
export function normalizeHealthProgress(raw: Record<string, unknown> | null | undefined): HealthProgress {
  const value = raw || {};
  const number = (snake: string, camel: string, fallback = 0) => {
    const n = Number(value[snake] ?? value[camel] ?? fallback);
    return Number.isFinite(n) ? Math.max(0, n) : fallback;
  };
  const actualTotal = number("actual_total", "actualTotal", number("total", "total"));
  const requested = number("requested", "requested");
  const total = Math.max(actualTotal, number("total", "total"));
  const done = Math.min(total, number("done", "done", number("checked", "checked")));
  const updates = value.updates && typeof value.updates === "object" ? value.updates as HealthProgress["updates"] : {};
  return {
    ...emptyHealthProgress(),
    ...value,
    running: Boolean(value.running),
    channel: value.channel === "nerver" ? "nerver" : "v4",
    done, total, actualTotal, requested,
    checked: Math.min(total, number("checked", "checked", done)),
    usable: number("usable", "usable"), alive: number("alive", "alive"),
    limited: number("limited", "limited"), invalid: number("invalid", "invalid"),
    invalidOther: number("invalid_other", "invalidOther"),
    reauthRequired: number("reauth_required", "reauthRequired"),
    error: number("error", "error"),
    skippedKnownDeactivated: number("skipped_known_deactivated", "skippedKnownDeactivated"),
    pendingCount: number("pending_count", "pendingCount"),
    workers: number("workers", "workers"), requestedWorkers: number("requested_workers", "requestedWorkers"),
    current: String(value.current || ""), message: String(value.message || ""),
    startedAt: String(value.started_at || value.startedAt || ""), finishedAt: String(value.finished_at || value.finishedAt || ""),
    interrupted: Boolean(value.interrupted), updates,
  };
}

export function fetchHealthProgress(includeUpdates = false, signal?: AbortSignal): Promise<HealthProgress> {
  return api<Record<string, unknown>>(`/api/accounts/health/progress?include_updates=${includeUpdates}`, signal ? { signal } : {})
    .then(normalizeHealthProgress);
}

/* --- 删除 --- */
export function deleteAccounts(keys: string[], deleteAll = false) {
  return api<{ removed: number; count: number }>("/api/accounts/delete", {
    method: "POST",
    body: jsonBody({ keys, delete_all: deleteAll }),
  });
}

export function deleteInvalidAccounts(keys: string[], deleteAll = false) {
  return api<{ removed: number; count: number }>("/api/accounts/delete-invalid", {
    method: "POST",
    body: jsonBody({ keys, delete_all: deleteAll }),
  });
}

/* --- 凭据读取（单条，显式动作才返回明文） --- */
export type CredentialField = "password" | "totp_secret" | "totp_code" | "refresh_token" | "session_token";

export function fetchCredential(key: string, field: CredentialField) {
  return api<{ ok: boolean; value?: string; password?: string; totp_secret?: string; totp_code?: string; refresh_token?: string; session_token?: string }>(
    "/api/accounts/credentials",
    { method: "POST", body: jsonBody({ key, field }) },
  );
}

export function fetchAccessToken(key: string) {
  return api<{ access_token: string }>("/api/accounts/access-token", {
    method: "POST",
    body: jsonBody({ key }),
  });
}

export function fetchAccessTokens(keys: string[]) {
  return api<{ tokens: string[]; exported: number; missing: number }>("/api/accounts/access-tokens", {
    method: "POST",
    body: jsonBody({ keys }),
  });
}

/* --- 设备信息（注册环境 / 指纹快照） --- */
export interface DeviceInfo {
  fingerprint?: Record<string, unknown>;
  proxy?: string;
  registered_at?: string;
  registration_method?: string;
  device_id?: string;
  has_snapshot?: boolean;
}

export function fetchDeviceInfo(key: string) {
  return api<DeviceInfo>("/api/accounts/device-info", {
    method: "POST",
    body: jsonBody({ key }),
  });
}

/** 按邮箱列表一键导出凭据（账号----密码----2FA，仅导出密码+2FA 齐全的账号） */
export function exportSecurityByEmail(emails: string[]) {
  return downloadPost("/api/accounts/export-security-by-email", { emails }, "accounts.security.by-email.txt");
}

/* --- 导出 / 下载 --- */
export function downloadAccountsJson(keys: string[], deleteAll = false) {
  return downloadPost("/api/accounts/download", { keys, delete_all: deleteAll }, "registered_accounts.selected.json");
}

export function exportSecurityCredentials(keys: string[]) {
  return downloadPost("/api/accounts/export-security", { keys }, "accounts.security.selected.txt");
}

export function exportAccessTokens(keys: string[]) {
  return downloadPost("/api/accounts/export-access-tokens", { keys }, "accounts.access-tokens.selected.txt");
}

/* --- 账号操作日志 --- */
export interface AccountLogEntry {
  at?: string;
  action?: string;
  level?: string;
  message?: string;
  detail?: string;
  [key: string]: unknown;
}

export function fetchAccountLogs(deleteKey: string, limit = 200) {
  return api<{ items?: AccountLogEntry[]; logs?: AccountLogEntry[]; total?: number }>(
    `/api/accounts/logs/${encodeURIComponent(deleteKey)}?limit=${limit}&order=desc`,
  );
}
