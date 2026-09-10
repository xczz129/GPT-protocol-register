/* --- 认证状态 --- */
import { defineStore } from "pinia";
import { fetchAuthStatus, logout as apiLogout } from "../api/auth";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    authenticated: false,
    username: "",
    loaded: false,
  }),
  actions: {
    async fetchStatus() {
      try {
        const value = await fetchAuthStatus();
        this.authenticated = !!value?.authenticated;
        this.username = value?.username || "";
      } catch {
        this.authenticated = false;
      } finally {
        this.loaded = true;
      }
      if (!this.authenticated) location.replace("/login");
    },
    async logout() {
      try {
        await apiLogout();
      } finally {
        location.replace("/login");
      }
    },
  },
});
