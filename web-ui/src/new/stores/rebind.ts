/* --- 邮箱换绑 store：预检（含预留释放）+ 批次进度轮询 ---
   预检会在服务端原子预留目标邮箱，输入变化时必须释放旧预留（与旧版一致）。 */
import { reactive, ref } from "vue";
import { defineStore } from "pinia";
import {
  fetchRebindProgress, rebindPreflight, rebindReleaseReservation, rebindStart, rebindStop,
  type RebindPreflightResult, type RebindProgress,
} from "../api/rebind";

export const useRebindStore = defineStore("rebind", () => {
  const preflight = reactive<RebindPreflightResult>({});
  const progress = reactive<RebindProgress>({ running: false, total: 0, done: 0, success: 0, failed: 0, items: [] });
  const checking = ref(false);
  const requestBusy = ref(false);
  let jobId = "";
  let timer: ReturnType<typeof setInterval> | null = null;
  let inputGeneration = 0;

  function applyProgress(value: RebindProgress | null | undefined): boolean {
    if (!value) return false;
    const id = String(value.job_id || value.jobId || "");
    if (id) jobId = id;
    Object.assign(progress, value, {
      running: ["queued", "running", "stopping"].includes(String(value.status || (value.running ? "running" : ""))),
    });
    return true;
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function startPolling(onFinished?: () => void) {
    stopPolling();
    timer = setInterval(async () => {
      try {
        const { job } = await fetchRebindProgress();
        applyProgress(job);
        if (!progress.running) {
          stopPolling();
          onFinished?.();
        }
      } catch {
        stopPolling();
      }
    }, 1500);
  }

  function resetPreflight() {
    preflight.eligible = false;
    preflight.reservation_id = "";
    preflight.source = null;
    preflight.target = null;
    preflight.items = [];
    preflight.invalid_format = [];
    preflight.invalid_targets = [];
    preflight.not_found = [];
    preflight.missing_credentials = [];
    preflight.conflicts = [];
    preflight.message = "";
  }

  /** 输入变化：作废旧预检并释放服务端预留 */
  function resetForInput() {
    inputGeneration += 1;
    const reservationId = String(preflight.reservation_id || "").trim();
    if (reservationId) void rebindReleaseReservation(reservationId);
    resetPreflight();
  }

  async function check(payload: { text: string; new_email: string; provider_type: string }) {
    if (progress.running || checking.value) return;
    const generation = ++inputGeneration;
    const previousReservation = String(preflight.reservation_id || "").trim();
    if (previousReservation) void rebindReleaseReservation(previousReservation);
    checking.value = true;
    resetPreflight();
    try {
      const value = await rebindPreflight({ ...payload, reservation_id: previousReservation });
      if (generation !== inputGeneration) {
        const staleId = String(value?.reservation_id || "").trim();
        if (staleId) void rebindReleaseReservation(staleId);
        return;
      }
      Object.assign(preflight, value);
    } finally {
      checking.value = false;
    }
  }

  async function start(providerType: string, concurrency: number) {
    const reservationId = String(preflight.reservation_id || "").trim();
    if (progress.running || requestBusy.value || !reservationId) return;
    requestBusy.value = true;
    try {
      const value = await rebindStart({
        reservation_id: reservationId,
        provider_type: providerType || "maddy_webui",
        concurrency: Math.min(20, Math.max(1, Math.floor(concurrency) || 1)),
      });
      if (!applyProgress(value)) throw new Error("换绑任务未返回有效进度");
      /* 预留已被 /start 消费，清空避免重复提交 */
      resetPreflight();
      startPolling();
    } finally {
      requestBusy.value = false;
    }
  }

  async function stop() {
    if (!jobId || !progress.running || progress.stopping) return;
    progress.stopping = true;
    try {
      await rebindStop(jobId);
      startPolling();
    } catch (error) {
      progress.stopping = false;
      throw error;
    }
  }

  /** 重连后台正在运行的换绑任务（进入页面时调用） */
  async function attachIfRunning(): Promise<boolean> {
    try {
      const { job } = await fetchRebindProgress();
      applyProgress(job);
      if (progress.running) {
        startPolling();
        return true;
      }
    } catch { /* 忽略 */ }
    return false;
  }

  return { preflight, progress, checking, requestBusy, check, start, stop, resetForInput, attachIfRunning, stopPolling };
});
