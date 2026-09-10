/* --- UI 全局状态：侧栏、通知、确认框 --- */
import { defineStore } from "pinia";

export type ToastKind = "success" | "error" | "warning" | "info";

export interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

export interface ConfirmOptions {
  title?: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}

interface ConfirmState extends ConfirmOptions {
  open: boolean;
  message: string;
  resolve: ((value: boolean) => void) | null;
}

let toastSeq = 0;

export const useUiStore = defineStore("ui", {
  state: () => ({
    sidebarOpen: false,
    toasts: [] as Toast[],
    confirm: { open: false, message: "", resolve: null } as ConfirmState,
  }),
  actions: {
    openSidebar() {
      this.sidebarOpen = true;
    },
    closeSidebar() {
      this.sidebarOpen = false;
    },

    notify(text: string, kind: ToastKind = "info", duration = 3200) {
      const id = ++toastSeq;
      this.toasts.push({ id, kind, text });
      window.setTimeout(() => {
        this.toasts = this.toasts.filter((t) => t.id !== id);
      }, duration);
    },

    /** Promise 化确认框：await ui.askConfirm("...") */
    askConfirm(message: string, options: ConfirmOptions = {}): Promise<boolean> {
      if (this.confirm.resolve) {
        const stale = this.confirm.resolve;
        this.confirm.resolve = null;
        stale(false);
      }
      this.confirm = { open: true, message: "", resolve: null, ...options };
      return new Promise((resolve) => {
        this.confirm.resolve = resolve;
      });
    },
    resolveConfirm(value: boolean) {
      const resolve = this.confirm.resolve;
      this.confirm.open = false;
      this.confirm.resolve = null;
      resolve?.(value);
    },
  },
});
