/* --- 运维域 API：实时日志、运行统计、自动维护、备份恢复、回收站、接码设置 --- */
import { api, apiUrl, jsonBody } from "./client";

/* --- 实时日志 --- */
export function fetchLogs(params: { limit?: number; q?: string; level?: string } = {}): Promise<{ lines: string[] }> {
  const search = new URLSearchParams();
  search.set("limit", String(params.limit ?? 300));
  if (params.q?.trim()) search.set("q", params.q.trim());
  if (params.level && params.level !== "all") search.set("level", params.level);
  return api<{ lines: string[] }>(`/api/logs?${search.toString()}`);
}

export function logsDownloadUrl(): string {
  return apiUrl("/api/logs/download");
}

/* --- 运维聚合数据（GET /api/operations） --- */
export interface FailureReason {
  reason: string;
  count: number;
}

export interface OperationsMetrics {
  success: number;
  failed: number;
  success_rate: number;
  average_seconds: number;
  failure_reasons: FailureReason[];
}

export interface OperationsMail {
  total: number;
  available: number;
  used: number;
  cooldown: number;
  failed: number;
}

export interface OperationsAccounts {
  total: number;
  alive: number;
  limited: number;
  reauth_required: number;
  invalid_other: number;
  error: number;
  deactivated: number;
  unknown: number;
}

export interface MaintenanceSettings {
  enabled: boolean;
  interval_hours: number;
  workers: number;
  auto_delete_invalid: boolean;
  has_webhook: boolean;
  webhook_masked: string;
  notify_on_complete: boolean;
  notify_on_pool_low: boolean;
  pool_low_threshold: number;
}

export interface CliproxyQuotaSettings {
  enabled: boolean;
  interval_seconds: number;
}

export interface BackupItem {
  name: string;
  created_at: string;
  size: number;
  auto?: boolean;
}

export interface BackupSchedule {
  enabled: boolean;
  time: string;
  retention_days: number;
}

export interface OperationsPayload {
  metrics: OperationsMetrics;
  mail: OperationsMail;
  accounts: OperationsAccounts;
  maintenance: MaintenanceSettings;
  cliproxy_quota: CliproxyQuotaSettings;
  state: Record<string, unknown>;
  backups: BackupItem[];
  backup_schedule: BackupSchedule;
  trash_count: number;
}

export function fetchOperations(): Promise<OperationsPayload> {
  return api<OperationsPayload>("/api/operations");
}

/* --- 自动维护 --- */
export interface MaintenanceUpdate {
  enabled: boolean;
  interval_hours: number;
  workers: number;
  auto_delete_invalid: boolean;
  webhook_url: string;
  notify_on_complete: boolean;
  notify_on_pool_low: boolean;
  pool_low_threshold: number;
}

export function updateMaintenance(body: MaintenanceUpdate) {
  return api("/api/maintenance", { method: "PUT", body: jsonBody(body) });
}

export function runMaintenance() {
  return api("/api/maintenance/run", { method: "POST", body: jsonBody({}) });
}

export function testMaintenanceWebhook(webhookUrl: string) {
  return api<{ ok: boolean; message: string }>("/api/maintenance/test-webhook", {
    method: "POST",
    body: jsonBody({ webhook_url: webhookUrl }),
  });
}

export function updateCliproxyQuota(body: CliproxyQuotaSettings) {
  return api("/api/cliproxy-quota", { method: "PUT", body: jsonBody(body) });
}

/* --- 备份与恢复 --- */
export function updateBackupSchedule(body: BackupSchedule) {
  return api("/api/backup-schedule", { method: "PUT", body: jsonBody(body) });
}

export function createBackup() {
  return api<{ ok: boolean; item: BackupItem }>("/api/backups", { method: "POST", body: jsonBody({}) });
}

export function restoreBackup(name: string) {
  return api<{ ok: boolean; message: string }>("/api/backups/restore", {
    method: "POST",
    body: jsonBody({ name }),
  });
}

export function backupDownloadUrl(name: string): string {
  return apiUrl(`/api/backups/${encodeURIComponent(name)}`);
}

/* --- 账号回收站 --- */
export interface TrashItem {
  trash_id: string;
  deleted_at: string;
  reason: string;
  account: Record<string, unknown>;
}

export function fetchTrash(): Promise<{ items: TrashItem[] }> {
  return api<{ items: TrashItem[] }>("/api/accounts/trash");
}

export function restoreTrash(ids: string[]) {
  return api<{ ok: boolean; restored: number }>("/api/accounts/trash/restore", {
    method: "POST",
    body: jsonBody({ ids }),
  });
}

/* --- 接码设置 --- */
export interface SmsConfig {
  enabled: boolean;
  provider: string;
  service: string;
  country_order: string;
  poll_interval_ms: number;
  timeout_seconds: number;
  max_price: string;
  max_phone_attempts: number;
  firefox_api_name: string;
  firefox_docking_code: string;
  firefox_did: string;
  firefox_item_id: string;
  firefox_country_order: string;
  has_api_key: boolean;
  api_key_masked: string;
  has_firefox_token: boolean;
  firefox_api_token_masked: string;
  provider_label: string;
  configured: boolean;
  firefox_docking_code_history: string[];
}

export function fetchSmsConfig(): Promise<SmsConfig> {
  return api<SmsConfig>("/api/sms-config");
}

export function updateSmsConfig(body: Record<string, unknown>) {
  return api("/api/sms-config", { method: "PUT", body: jsonBody(body) });
}

export function testSmsConfig() {
  return api<{ ok: boolean; message: string }>("/api/sms-config/test", { method: "POST", body: jsonBody({}) });
}

export function clearSmsDockingHistory() {
  return api("/api/sms-config/docking-history", { method: "DELETE" });
}

export interface SmsStatsRow {
  name: string;
  total: number;
  ok: number;
  fail: number;
  rate: number;
  cost: number;
  price: number;
}

export interface SmsStats {
  summary: { total: number; success: number; failed: number; rate: number };
  countries: SmsStatsRow[];
  tiers: SmsStatsRow[];
}

export function fetchSmsStats(): Promise<SmsStats> {
  return api<SmsStats>("/api/sms/stats");
}

/* --- 接码国家列表（含各国价位与号码库存） --- */
export interface SmsCountryTier {
  id: string;
  count: number;
  price: number;
}

export interface SmsCountryItem {
  id: string;
  name: string;
  calling?: string;
  count?: number;
  price?: string | number;
  tiers?: SmsCountryTier[];
}

export function fetchSmsCountries(provider: string): Promise<{ ok: boolean; items: SmsCountryItem[] }> {
  return api<{ ok: boolean; items: SmsCountryItem[] }>(
    `/api/sms-countries?provider=${encodeURIComponent(provider)}`,
  );
}
