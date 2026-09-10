/* --- 中转管理 store：账号列表 + 额度刷新 + 导入导出 + 重置卡 ---
   额度与状态由面板直连 OpenAI 查询（不经 cliproxy 接口），刷新为局部合并更新。 */
import { ref } from "vue";
import { defineStore } from "pinia";
import {
  consumeCliproxyReset, deleteCliproxyAccounts, exportCliproxy, fetchCliproxyAccounts,
  importCliproxy, refreshCliproxyQuota, type CliproxyAccounts, type CliproxyItem,
} from "../api/cliproxy";

export const useCliproxyStore = defineStore("cliproxy", () => {
  const accounts = ref<CliproxyAccounts>({ configured: false, auths_dir: "", items: [] });
  const loading = ref(false);
  const refreshing = ref(false);
  const importing = ref(false);
  const deleting = ref(false);
  const resettingFile = ref("");

  async function load() {
    loading.value = true;
    try {
      accounts.value = await fetchCliproxyAccounts();
    } finally {
      loading.value = false;
    }
  }

  /** rows 为空 = 刷新全部；否则按文件名单个刷新（局部合并回列表） */
  async function refreshQuota(rows: CliproxyItem[] = []) {
    const files = rows.length ? rows.map((row) => row.file).filter(Boolean) : [];
    refreshing.value = true;
    try {
      const response = await refreshCliproxyQuota(files);
      const byFile = response.items || {};
      accounts.value = {
        ...accounts.value,
        items: (accounts.value.items || []).map((item) => {
          const updated = byFile[item.file];
          return updated
            ? { ...item, quota: updated.quota, quota_error: updated.error || "", status: updated.status || "" }
            : item;
        }),
      };
    } finally {
      refreshing.value = false;
    }
  }

  /** 导入 CPA/SUB2 JSON，成功后重载列表并后台刷新新账号额度 */
  async function importJson(kind: "cpa" | "sub2", filename: string, content: string) {
    importing.value = true;
    try {
      const result = await importCliproxy(kind, filename, content);
      await load();
      if (result.imported) void refreshQuota((result.files || []).map((file) => ({ file } as CliproxyItem)));
      return result;
    } finally {
      importing.value = false;
    }
  }

  async function exportJson(kind: "cpa" | "sub2", files: string[]) {
    await exportCliproxy(kind, files);
  }

  async function remove(files: string[]) {
    deleting.value = true;
    try {
      const result = await deleteCliproxyAccounts(files);
      await load();
      return result;
    } finally {
      deleting.value = false;
    }
  }

  async function consumeReset(file: string) {
    resettingFile.value = file;
    try {
      const result = await consumeCliproxyReset(file);
      if (result.quota) {
        accounts.value = {
          ...accounts.value,
          items: (accounts.value.items || []).map((item) => (
            item.file === file
              ? { ...item, quota: result.quota, quota_error: result.quota_error || "", status: result.quota?.status || item.status }
              : item
          )),
        };
      } else {
        await refreshQuota([{ file } as CliproxyItem]);
      }
    } finally {
      resettingFile.value = "";
    }
  }

  return { accounts, loading, refreshing, importing, deleting, resettingFile, load, refreshQuota, importJson, exportJson, remove, consumeReset };
});
