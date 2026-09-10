/* --- 统一轮询：生命周期自管理，页面隐藏时暂停 --- */
import { onBeforeUnmount, ref, type Ref } from "vue";

export interface PollerOptions {
  /** 页面隐藏时是否继续轮询（默认 false 暂停） */
  runWhenHidden?: boolean;
  /** 立即执行一次（默认 true） */
  immediate?: boolean;
}

export function usePoller(task: () => Promise<unknown> | unknown, intervalMs: number, options: PollerOptions = {}) {
  const running: Ref<boolean> = ref(false);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;

  async function tick() {
    if (stopped) return;
    if (!options.runWhenHidden && document.hidden) {
      schedule();
      return;
    }
    running.value = true;
    try {
      await task();
    } catch {
      /* 轮询任务失败不打断后续节奏 */
    } finally {
      running.value = false;
      schedule();
    }
  }

  function schedule() {
    if (stopped) return;
    timer = setTimeout(tick, intervalMs);
  }

  function stop() {
    stopped = true;
    if (timer) clearTimeout(timer);
    timer = null;
  }

  if (options.immediate !== false) void tick();
  else schedule();
  onBeforeUnmount(stop);

  return { running, stop };
}
