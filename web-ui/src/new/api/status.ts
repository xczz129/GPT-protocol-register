/* --- 面板聚合状态接口 --- */
import { api } from "./client";
import type { PanelStatus } from "./types";

export function fetchPanelStatus(): Promise<PanelStatus> {
  return api<PanelStatus>("/api/status");
}
