/* --- API 客户端：统一 base 拼接、错误处理与 401 拦截 --- */

const configuredApiBase = String(import.meta.env.VITE_API_BASE_URL || "").trim().replace(/\/+$/, "");

/** 接受 API origin（https://api.example.com）或 API 路径（/api），保持 /api/... 调用不变 */
export function apiUrl(path: string): string {
  const value = String(path || "");
  if (!value) return configuredApiBase || "/";
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(value)) return value;
  if (!configuredApiBase) return value;
  if ((configuredApiBase === "/api" || configuredApiBase.endsWith("/api")) && value === "/api") {
    return configuredApiBase;
  }
  if ((configuredApiBase === "/api" || configuredApiBase.endsWith("/api")) && value.startsWith("/api/")) {
    return `${configuredApiBase}${value.slice(4)}`;
  }
  return `${configuredApiBase}${value.startsWith("/") ? value : `/${value}`}`;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export interface ApiOptions extends RequestInit {
  /** 401 时不跳转登录页（例如登录前的 auth/status 探测） */
  skipAuthRedirect?: boolean;
}

export async function api<T = unknown>(path: string, options: ApiOptions = {}): Promise<T> {
  const { skipAuthRedirect, headers, ...rest } = options;
  const response = await fetch(apiUrl(path), {
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(headers || {}) },
    ...rest,
  });
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401) {
    if (!skipAuthRedirect) location.replace("/login");
    throw new ApiError("登录会话已过期", 401);
  }
  if (!response.ok) {
    const body = data as Record<string, unknown> | null;
    const detail = body?.detail || body?.message || body?.error || response.statusText;
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), response.status);
  }
  return data as T;
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

/** POST 触发文件下载，返回导出统计头 */
export async function downloadPost(
  path: string,
  payload: unknown,
  filename: string,
): Promise<{ exported: number; missing: number }> {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new ApiError(await response.text(), response.status);
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
  return {
    exported: Number(response.headers.get("X-Exported-Count") || 0),
    missing: Number(response.headers.get("X-Missing-Count") || 0),
  };
}
