/* --- 代理设置 API：保存 /api/proxy + 代理检测 --- */
import { api, jsonBody } from "./client";

export interface CountryPool {
  country: string;
  label: string;
  text: string;
  count?: number;
}

export interface ProxySavePayload {
  enabled: boolean;
  mode: string;
  url: string;
  resource_url: string;
  pool_file: string;
  pool_text: string | null;
  selected_country: string;
  country_pools: CountryPool[];
  rotate_per_task: boolean;
  clearance: {
    enabled: boolean;
    mode: string;
    flaresolverr_url: string;
    proxy_url: string;
    timeout_sec: number;
    refresh_interval: number;
  };
}

export interface ProxySaveResult {
  ok?: boolean;
  proxy?: {
    selected_country?: string;
    country_pools?: { items?: CountryPool[]; path?: string };
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export function saveProxy(payload: ProxySavePayload): Promise<ProxySaveResult> {
  return api<ProxySaveResult>("/api/proxy", { method: "PUT", body: jsonBody(payload) });
}

/* --- 代理检测 --- */
export interface ProxyDiagnosticItem {
  proxy: string;
  ok: boolean;
  ip?: string;
  country?: string;
  latency_ms?: number;
  error?: string;
}

export interface ProxyDiagnostics {
  items: ProxyDiagnosticItem[];
  alive?: number;
  count?: number;
}

export function testProxies(): Promise<ProxyDiagnostics> {
  return api<ProxyDiagnostics>("/api/proxy/test", {
    method: "POST",
    body: jsonBody({ proxies: [] }),
  });
}
