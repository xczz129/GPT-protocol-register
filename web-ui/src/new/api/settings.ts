/* --- 面板 UI 设置（并发默认值等） --- */
import { api, jsonBody } from "./client";

export interface UiSettings {
  health_workers: number;
  reauth_workers: number;
  trial_workers: number;
  refresh_at_workers: number;
  codex_sms_workers: number;
  reauth_mode: string;
  trial_channel: string;
}

export function fetchUiSettings(): Promise<UiSettings> {
  return api<UiSettings>("/api/ui-settings");
}

export function updateUiSettings(body: UiSettings) {
  return api("/api/ui-settings", { method: "PUT", body: jsonBody(body) });
}
