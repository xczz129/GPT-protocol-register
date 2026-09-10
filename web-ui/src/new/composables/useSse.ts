/* --- SSE 长连接：唯一实例管理 + 指数退避重连 --- */
import { onBeforeUnmount, ref, type Ref } from "vue";
import { apiUrl } from "../api/client";

export interface SseOptions {
  /** 重连基础间隔 ms，指数退避至 8 倍封顶 */
  baseDelay?: number;
  /** 页面隐藏时保持连接（默认 true，依赖后端心跳保活） */
  keepAliveWhenHidden?: boolean;
}

export function useSse(path: string, onMessage: (event: MessageEvent) => void, options: SseOptions = {}) {
  const connected: Ref<boolean> = ref(false);
  const baseDelay = options.baseDelay ?? 2000;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempts = 0;
  let closed = false;

  function connect() {
    if (closed) return;
    source?.close();
    const es = new EventSource(apiUrl(path), { withCredentials: true });
    source = es;
    es.onopen = () => {
      attempts = 0;
      connected.value = true;
    };
    es.onmessage = onMessage;
    es.onerror = () => {
      connected.value = false;
      es.close();
      if (closed) return;
      const delay = Math.min(baseDelay * 2 ** attempts, baseDelay * 8);
      attempts += 1;
      reconnectTimer = setTimeout(connect, delay);
    };
  }

  function close() {
    closed = true;
    connected.value = false;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    source?.close();
    source = null;
  }

  connect();
  onBeforeUnmount(close);

  return { connected, close, reconnect: connect };
}
