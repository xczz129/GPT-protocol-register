/* --- 配置域 API：/api/config 读写 + 浏览器运行环境检测 --- */
import { api, jsonBody } from "./client";

/** 服务器配置（结构松散，按页面所需字段渐进类型化） */
export interface AppConfig {
  mail?: { providers?: Record<string, unknown>[] };
  proxy?: Record<string, unknown>;
  fingerprint?: Record<string, unknown>;
  registration?: Record<string, unknown>;
  [key: string]: unknown;
}

export function fetchConfig(): Promise<{ config: AppConfig }> {
  return api<{ config: AppConfig }>("/api/config");
}

export function saveConfig(config: AppConfig) {
  return api("/api/config", { method: "PUT", body: jsonBody({ config }) });
}

/* --- 浏览器运行环境（指纹环境页） --- */
export interface BrowserCapability {
  id: string;
  name: string;
  kind: string;
  available: boolean;
  selected: boolean;
  detail: string;
  endpoint?: string;
}

export function fetchBrowserStatus(): Promise<{ items: BrowserCapability[] }> {
  return api<{ items: BrowserCapability[] }>("/api/browsers/status");
}
