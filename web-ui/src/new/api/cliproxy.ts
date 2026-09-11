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

/** Metadata only: token contents are downloaded only after an explicit export. */
export interface CliproxyExportItem {
  file: string;
  email: string;
  account_id: string;
  plan_type: string;
  size_bytes: number;
  valid: boolean;
  error?: string;
  revision: string;
}

export interface CliproxyExportPreview {
  configured: boolean;
  total_files: number;
  matched_files: number;
  matched_emails: number;
  not_found: string[];
  items: CliproxyExportItem[];
}

export type CliproxyExportFormat = "original" | "merged" | "sub2";

export function fetchCliproxyExportPreview(
  payload: { emails: string[]; all: boolean },
  signal?: AbortSignal,
): Promise<CliproxyExportPreview> {
  return api("/api/cliproxy/export-preview", {
    method: "POST", body: jsonBody(payload), signal,
  });
}

function attachmentFilename(header: string | null, fallback: string): string {
  const encoded = header?.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  const plain = header?.match(/filename\s*=\s*(?:"([^"]+)"|([^;]+))/i);
  let name = plain?.[1] || plain?.[2]?.trim() || fallback;
  if (encoded) {
    try { name = decodeURIComponent(encoded.trim().replace(/^"|"$/g, "")); } catch { /* Use the plain name if decoding fails. */ }
  }
  return name.replace(/[\\/]/g, "_").replace(/[\u0000-\u001f\u007f]/g, "").trim() || fallback;
}

/** Exact, reviewed file selection. Empty selection never means export all. */
export async function exportCliproxyFiles(
  files: string[], format: CliproxyExportFormat, revisions: Record<string, string>,
): Promise<{ exported: number; filename: string }> {
  const selected = [...new Set(files)];
  if (!selected.length) throw new ApiError("请先勾选要导出的中转文件", 400);
  if (selected.some((file) => !file || /[\\/\u0000]/.test(file) || !revisions[file])) {
    throw new ApiError("所选文件信息不完整，请重新加载中转文件", 400);
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60000);
  try {
    const response = await fetch(apiUrl("/api/cliproxy/export-files"), {
      method: "POST", credentials: "include", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: jsonBody({ files: selected, format, revisions: Object.fromEntries(selected.map((file) => [file, revisions[file]])) }),
      signal: controller.signal,
    });
    if (response.status === 401) {
      location.replace("/login");
      throw new ApiError("登录会话已过期", 401);
    }
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = typeof body?.detail === "string" ? body.detail : body?.detail ? JSON.stringify(body.detail) : detail;
      } catch { /* Use the status text if the response is not JSON. */ }
      throw new ApiError(detail || "中转文件导出失败", response.status);
    }
    const fallback = format === "original" ? selected.length === 1 ? selected[0] : "cliproxy-auths-selected.zip" : `cliproxy-${format === "merged" ? "cpa" : "sub2"}-selected.json`;
    const filename = attachmentFilename(response.headers.get("Content-Disposition"), fallback);
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = filename;
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return { exported: Number(response.headers.get("X-Exported-Count") || selected.length), filename };
  } finally { clearTimeout(timeout); }
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
