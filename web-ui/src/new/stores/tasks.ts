/* --- 任务 store：任务卡快照 + 运行状态轮询 ---
   任务卡为不可变快照，shallowRef 整体替换；运行中 2s 轮询，空闲 30s。 */
import { ref, shallowRef } from "vue";
import { defineStore } from "pinia";
import {
  deleteTasks, fetchTasks, pauseJob, resumeJob, retryFailedJobs,
  startJob, stopJob, type JobSnapshot, type StartJobPayload, type TaskCard,
} from "../api/tasks";
import { useStatusStore } from "./status";
import { saveConfig } from "../api/config";
import { useBatchOpsStore } from "./batchops";

export const useTasksStore = defineStore("tasks", () => {
  const items = shallowRef<TaskCard[]>([]);
  const job = shallowRef<JobSnapshot>({} as JobSnapshot);
  const loading = ref(false);
  const loaded = ref(false);

  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let viewPolling = false;
  let loadRequest: Promise<void> | null = null;
  let autoSmsSync = "";

  function followupActive() {
    return ["pending", "running"].includes(job.value.auto_sms?.status || "")
      || (job.value.running && job.value.params?.auto_sms_after_register === true);
  }

  function load(): Promise<void> {
    if (loadRequest) return loadRequest;
    loadRequest = (async () => {
      try {
        const data = await fetchTasks();
        items.value = data.items || [];
        job.value = data.job || ({} as JobSnapshot);
        loaded.value = true;
        /* 同步顶栏运行徽标，并接入已有全局短信任务进度。 */
        const status = useStatusStore();
        status.panel = { ...status.panel, job: { ...status.panel.job, running: data.job?.running } };
        const sms = job.value.auto_sms;
        if (sms?.batch_id) {
          const signature = `${sms.source_batch_id || ""}:${sms.batch_id}:${sms.status}`;
          if (signature !== autoSmsSync) {
            autoSmsSync = signature;
            void useBatchOpsStore().refreshCodexStatus();
          }
        }
      } catch {
        /* 轮询失败静默，下次重试 */
      } finally {
        loading.value = false;
        loadRequest = null;
        schedulePoll();
      }
    })();
    return loadRequest;
  }

  function schedulePoll() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    if (!viewPolling && !followupActive()) return;
    const delay = job.value.running || followupActive() ? 2000 : 30000;
    pollTimer = setTimeout(async () => {
      if (!document.hidden) await load();
      schedulePoll();
    }, delay);
  }

  function startPolling() {
    viewPolling = true;
    schedulePoll();
  }

  function stopPolling() {
    viewPolling = false;
    // A server-started SMS task may begin after leaving the registration page.
    // Keep discovering that follow-up until it reaches a terminal state.
    schedulePoll();
  }

  async function start(payload: StartJobPayload) {
    const autoSms = payload.auto_sms_after_register === true;
    await saveConfig({ registration: { auto_sms_after_register: autoSms } });
    await startJob({ ...payload, auto_sms_after_register: autoSms });
    await load();
    startPolling();
  }

  async function stop() {
    await stopJob();
    await load();
  }

  async function pause() {
    await pauseJob();
    await load();
  }

  async function resume() {
    await resumeJob();
    await load();
  }

  async function retryFailed() {
    await retryFailedJobs();
    await load();
    startPolling();
  }

  async function remove(indexes: string[], deleteAll = false) {
    await deleteTasks(indexes, deleteAll);
    await load();
  }

  return {
    items, job, loading, loaded, load,
    startPolling, stopPolling,
    start, stop, pause, resume, retryFailed, remove,
  };
});
