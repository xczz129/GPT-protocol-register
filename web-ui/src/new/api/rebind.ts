/* --- 邮箱换绑 API：预检（预留目标邮箱）、启动、进度、停止、释放预留 --- */
import { api, jsonBody } from "./client";

export interface RebindEndpoint {
  email?: string;
  account_id?: string;
  plan_type?: string;
  health_status?: string;
  has_password?: boolean;
  password_confirmed?: boolean;
  has_totp?: boolean;
  provider?: string;
  domain?: string;
  reserved_at?: string;
}

export interface RebindPreflightItem {
  record_key?: string;
  input?: string;
  status?: string;
  message?: string;
  source?: RebindEndpoint;
  target?: RebindEndpoint;
}

export interface RebindPreflightResult {
  eligible?: boolean;
  requested?: number;
  matched?: number;
  eligible_count?: number;
  source?: RebindEndpoint | null;
  target?: RebindEndpoint | null;
  items?: RebindPreflightItem[];
  reservation_id?: string;
  invalid_format?: string[];
  invalid_targets?: string[];
  not_found?: string[];
  missing_credentials?: { email?: string; missing?: string[] }[];
  conflicts?: string[];
  message?: string;
}

export interface RebindProgressItem {
  key?: string;
  email?: string;
  old_email?: string;
  new_email?: string;
  status?: string;
  message?: string;
  access_token_masked?: string;
}

export interface RebindProgress {
  job_id?: string;
  jobId?: string;
  status?: string;
  running: boolean;
  stopping?: boolean;
  total: number;
  done: number;
  current?: string;
  success: number;
  failed: number;
  cancelled?: number;
  message?: string;
  lastMessage?: string;
  skipped?: string[];
  items?: RebindProgressItem[];
}

export function rebindPreflight(payload: {
  text: string;
  new_email: string;
  provider_type: string;
  reservation_id: string;
}): Promise<RebindPreflightResult> {
  return api<RebindPreflightResult>("/api/accounts/rebind/preflight", {
    method: "POST",
    body: jsonBody(payload),
  });
}

export function rebindStart(payload: {
  reservation_id: string;
  provider_type: string;
  concurrency: number;
}): Promise<RebindProgress> {
  return api<RebindProgress>("/api/accounts/rebind/start", {
    method: "POST",
    body: jsonBody(payload),
  });
}

export function fetchRebindProgress(): Promise<{ job: RebindProgress | null }> {
  return api<{ job: RebindProgress | null }>("/api/accounts/rebind/progress");
}

export function rebindStop(jobId: string) {
  return api(`/api/accounts/rebind/${encodeURIComponent(jobId)}/stop`, {
    method: "POST",
    body: "{}",
  });
}

/** 释放预检预留的目标邮箱（输入变化/重新预检时调用，失败静默） */
export function rebindReleaseReservation(reservationId: string) {
  return api(`/api/accounts/rebind/preflight/${encodeURIComponent(reservationId)}/cancel`, {
    method: "POST",
    body: "{}",
  }).catch(() => {});
}
