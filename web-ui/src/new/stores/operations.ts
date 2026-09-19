/* --- 运维中心 store：聚合数据、自动维护、备份恢复、回收站、接码设置 --- */
import { defineStore } from "pinia";
import {
  createBackup,
  fetchOperations,
  fetchSmsConfig,
  fetchSmsStats,
  fetchTrash,
  restoreBackup,
  restoreTrash,
  runMaintenance,
  testMaintenanceWebhook,
  testSmsConfig,
  updateBackupSchedule,
  updateCliproxyQuota,
  updateMaintenance,
  updateSmsConfig,
  type BackupSchedule,
  type CliproxyQuotaSettings,
  type MaintenanceUpdate,
  type OperationsPayload,
  type SmsConfig,
  type SmsStats,
  type TrashItem,
} from "../api/ops";

export const useOperationsStore = defineStore("operations", {
  state: () => ({
    data: null as OperationsPayload | null,
    loaded: false,
    busy: {} as Record<string, boolean>,
    trash: [] as TrashItem[],
    trashLoaded: false,
    smsConfig: null as SmsConfig | null,
    smsStats: null as SmsStats | null,
    smsBalance: null as { provider: string; providerLabel: string; amount: string; points?: string; level?: string; checkedAt: string; error: string } | null,
  }),
  actions: {
    async _run<T>(key: string, task: () => Promise<T>): Promise<T> {
      this.busy[key] = true;
      try {
        return await task();
      } finally {
        this.busy[key] = false;
      }
    },
    async load(force = false) {
      if (this.loaded && !force) return;
      const data = await this._run("load", fetchOperations);
      this.data = data;
      this.loaded = true;
    },
    async refresh() {
      await this.load(true);
    },
    saveMaintenance(body: MaintenanceUpdate) {
      return this._run("maintenance", async () => {
        await updateMaintenance(body);
        await this.load(true);
      });
    },
    runMaintenanceNow() {
      return this._run("maintenance-run", () => runMaintenance());
    },
    testWebhook(url: string) {
      return this._run("webhook", () => testMaintenanceWebhook(url));
    },
    saveCliproxyQuota(body: CliproxyQuotaSettings) {
      return this._run("cliproxy-quota", async () => {
        await updateCliproxyQuota(body);
        await this.load(true);
      });
    },
    saveBackupSchedule(body: BackupSchedule) {
      return this._run("backup-schedule", async () => {
        await updateBackupSchedule(body);
        await this.load(true);
      });
    },
    createBackupNow() {
      return this._run("backup", async () => {
        await createBackup();
        await this.load(true);
      });
    },
    restoreBackupByName(name: string) {
      return this._run("backup-restore", () => restoreBackup(name));
    },
    async loadTrash(force = false) {
      if (this.trashLoaded && !force) return;
      const data = await this._run("trash", fetchTrash);
      this.trash = data.items || [];
      this.trashLoaded = true;
    },
    restoreTrashIds(ids: string[]) {
      return this._run("trash-restore", async () => {
        const result = await restoreTrash(ids);
        await Promise.all([this.loadTrash(true), this.load(true)]);
        return result;
      });
    },
    async loadSmsConfig() {
      const next = await this._run("sms", fetchSmsConfig);
      if (this.smsConfig && ["provider", "api_key_masked"].some(
        key => this.smsConfig?.[key as keyof SmsConfig] !== next[key as keyof SmsConfig],
      )) this.smsBalance = null;
      this.smsConfig = next;
    },
    saveSmsConfig(body: Record<string, unknown>) {
      return this._run("sms-save", async () => {
        await updateSmsConfig(body);
        this.smsBalance = null;
        await this.loadSmsConfig();
      });
    },
    testSms() {
      const configured = this.smsConfig;
      return this._run("sms-test", async () => {
        try {
          const result = await testSmsConfig();
          this.smsBalance = {
            provider: result.provider || configured?.provider || "",
            providerLabel: result.provider_label || configured?.provider_label || "接码平台",
            amount: result.ok ? String(result.balance?.balance ?? "") : "",
            points: result.balance?.points,
            level: result.balance?.level,
            checkedAt: result.checked_at || new Date().toISOString(),
            error: result.ok ? "" : result.message,
          };
          return result;
        } catch (error) {
          this.smsBalance = {
            provider: configured?.provider || "", providerLabel: configured?.provider_label || "接码平台",
            amount: "", checkedAt: new Date().toISOString(),
            error: error instanceof Error ? error.message : "余额查询失败，请重试",
          };
          throw error;
        }
      });
    },
    async loadSmsStats(force = false) {
      if (this.smsStats && !force) return;
      this.smsStats = await this._run("sms-stats", fetchSmsStats);
    },
  },
});
