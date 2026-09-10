/* --- 面板聚合状态（指标卡、运行徽标数据源） --- */
import { defineStore } from "pinia";
import { fetchPanelStatus } from "../api/status";
import type { PanelStatus } from "../api/types";

export const useStatusStore = defineStore("status", {
  state: () => ({
    panel: {} as PanelStatus,
    loadedAt: 0,
    loading: false,
  }),
  getters: {
    running: (s) => !!s.panel.job?.running,
  },
  actions: {
    async load(force = false) {
      if (this.loading) return;
      if (!force && Date.now() - this.loadedAt < 5000) return;
      this.loading = true;
      try {
        this.panel = await fetchPanelStatus();
        this.loadedAt = Date.now();
      } finally {
        this.loading = false;
      }
    },
  },
});
