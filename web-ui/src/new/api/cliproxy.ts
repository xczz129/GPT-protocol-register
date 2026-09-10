/* --- 中转管理（cliproxy）API：账号列表、额度、导入导出、删除、重置卡 --- */
import { api, apiUrl, jsonBody, ApiError } from "./client";

export interface CliproxyQuotaWindow {
  window_label?: string;
  window_seconds?: number;
  used_percent?: number | string | null;
  reset_after_seconds?: number;
  reset_at?: number;
}

export interface CliproxyQuota {
  status?: string;
  five_hour?: CliproxyQuotaWindow | null;
  weekly?: CliproxyQuotaWindow | null;
  primary?: CliproxyQuotaWindow | null;
  secondary?: CliproxyQuotaWindow | null;
  reset_credits?: { available?: number };
  fetched_at?: string;
}

export interface CliproxyItem {
  file: string;
  email?: string;
  plan_type?: string;
  status?: string;
  quota?: CliproxyQuota;
  quota_error?: string;
  expired?: string;
}

export interface CliproxyAccounts {
  configured: boolean;
  auths_dir: string;
  items: CliproxyItem[];
}

export function fetchCliproxyAccounts(): Promise<CliproxyAccounts> {
  return api<CliproxyAccounts>("/api/cliproxy/accounts");
}

export function refreshCliproxyQuota(files: string[]): Promise<{ items: Record<string, { quota?: CliproxyQuota; error?: string; status?: string }> }> {
  return api("/api/cliproxy/refresh-quota", {
    method: "POST",
    body: jsonBody({ files }),
  });
}

export function importCliproxy(kind: "cpa" | "sub2", filename: string, content: string): Promise<{ imported?: number; skipped?: number; files?: string[] }> {
  return api(`/api/cliproxy/import-${kind}`, {
    method: "POST",
    body: jsonBody({ filename, content }),
  });
}

export function deleteCliproxyAccounts(files: string[]): Promise<{ deleted?: string[] }> {
  return api("/api/cliproxy/delete", {
    method: "POST",
    body: jsonBody({ files }),
  });
}

export function consumeCliproxyReset(file: string): Promise<{ quota?: CliproxyQuota; quota_error?: string }> {
  return api("/api/cliproxy/reset-credits/consume", {
    method: "POST",
    body: jsonBody({ file }),
  });
}

/** 单个账号凭据下载地址（浏览器直接打开即下载） */
export function cliproxyDownloadUrl(files: string[]): string {
  return apiUrl(`/api/cliproxy/download?files=${encodeURIComponent(files.join(","))}`);
}

/** GET 触发文件下载（导出 CPA/SUB2 JSON） */
export async function exportCliproxy(kind: "cpa" | "sub2", files: string[]): Promise<void> {
  const query = files.length ? `?files=${encodeURIComponent(files.join(","))}` : "";
  const response = await fetch(apiUrl(`/api/cliproxy/export-${kind}${query}`), {
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json())?.detail || detail;
    } catch { /* 保持状态文本 */ }
    throw new ApiError(detail, response.status);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = files.length ? `cliproxy-${kind}-selected.json` : `cliproxy-${kind}-all.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}
