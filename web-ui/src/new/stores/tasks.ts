/* --- 任务 store：任务卡快照 + 运行状态轮询 ---
   任务卡为不可变快照，shallowRef 整体替换；运行中 2s 轮询，空闲 30s。 */
import { ref, shallowRef } from "vue";
import { defineStore } from "pinia";
import {
  deleteTasks, fetchTasks, pauseJob, resumeJob, retryFailedJobs,
  startJob, stopJob, type JobSnapshot, type StartJobPayload, type TaskCard,
} from "../api/tasks";
import { useStatusStore } from "./status";

export const useTasksStore = defineStore("tasks", () => {
  const items = shallowRef<TaskCard[]>([]);
  const job = shallowRef<JobSnapshot>({} as JobSnapshot);
  const loading = ref(false);
  const loaded = ref(false);

  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  async function load() {
    try {
      const data = await fetchTasks();
      items.value = data.items || [];
      job.value = data.job || ({} as JobSnapshot);
      loaded.value = true;
      /* 同步顶栏运行徽标 */
      const status = useStatusStore();
      status.panel = { ...status.panel, job: { ...status.panel.job, running: data.job?.running } };
    } catch {
      /* 轮询失败静默，下次重试 */
    } finally {
      loading.value = false;
    }
  }

  function schedulePoll() {
    if (pollTimer) clearTimeout(pollTimer);
    const delay = job.value.running ? 2000 : 30000;
    pollTimer = setTimeout(async () => {
      if (!document.hidden) await load();
      schedulePoll();
    }, delay);
  }

  function startPolling() {
    stopPolling();
    schedulePoll();
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  async function start(payload: StartJobPayload) {
    await startJob(payload);
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
