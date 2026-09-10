/* --- Plus 试用资格 API：Token 检测（单个/批量）+ 资格代理池 --- */
import { api, jsonBody } from "./client";

export interface TrialCheckResult {
  eligible?: boolean;
  has_active_subscription?: boolean;
  is_deactivated?: boolean;
  jwt_expired?: boolean;
  status?: string;
  plan_type?: string;
  reason?: string;
  label?: string;
  provider?: string;
  proxy_mode?: string;
  proxy_country?: string;
  elapsed_seconds?: number;
  checked_at?: string;
}

export interface TrialBatchRow {
  token_tail?: string;
  ok?: boolean;
  eligible?: boolean;
  has_active_subscription?: boolean;
  is_deactivated?: boolean;
  status?: string;
  plan_type?: string;
  promotion_count?: number;
  elapsed_seconds?: number | null;
  reason?: string;
  error?: string;
}

export interface TrialProxyCountry {
  country: string;
  label: string;
  text: string;
}

export interface TrialProxyPool {
  selected_country: string;
  countries: TrialProxyCountry[];
}

export function checkTrialToken(accessToken: string, useConfiguredProxy: boolean, channel: string): Promise<TrialCheckResult> {
  return api<TrialCheckResult>("/api/trial-eligibility/check-token", {
    method: "POST",
    body: jsonBody({
      access_token: accessToken,
      use_configured_proxy: useConfiguredProxy,
      channel: channel === "nerver" ? "nerver" : "v4",
    }),
  });
}

export function checkTrialTokens(tokens: string[], useConfiguredProxy: boolean, channel: string, workers: number): Promise<{ results: TrialBatchRow[] }> {
  return api<{ results: TrialBatchRow[] }>("/api/trial-eligibility/check-tokens", {
    method: "POST",
    body: jsonBody({
      access_tokens: tokens,
      use_configured_proxy: useConfiguredProxy,
      channel: channel === "nerver" ? "nerver" : "v4",
      workers,
    }),
  });
}

export async function fetchTrialProxyPool(): Promise<TrialProxyPool> {
  const data = await api<{ selected_country?: string; countries?: TrialProxyCountry[] }>("/api/trial-proxy-pool");
  return {
    selected_country: data.selected_country || "",
    countries: Array.isArray(data.countries) ? data.countries : [],
  };
}

export function saveTrialProxyPool(pool: TrialProxyPool) {
  return api("/api/trial-proxy-pool", {
    method: "PUT",
    body: jsonBody({ selected_country: pool.selected_country, countries: pool.countries }),
  });
}
