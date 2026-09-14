/* --- 注册任务 API --- */
import { api, jsonBody } from "./client";

export interface TaskStep {
  text: string;
  time: string;
  status?: string;
}

export interface TaskCard {
  card_id: string;
  index: number;
  email: string;
  provider: string;
  proxy: string;
  status: "queued" | "running" | "success" | "failed" | "cancelled" | "skipped" | string;
  steps: TaskStep[];
  started_at_epoch: number;
  elapsed_sec: number;
  batch_started_at: string;
  skip_reason?: string;
  error?: string;
}

export interface JobSnapshot {
  running: boolean;
  paused: boolean;
  success_count: number;
  fail_count: number;
  skipped_count: number;
  total: number;
  last_error: string;
  started_at: string;
  params?: Record<string, unknown>;
}

export interface TasksResponse {
  items: TaskCard[];
  job: JobSnapshot;
}

export interface StartJobPayload {
  total: number;
  concurrency: number;
  keep_on_fail: boolean;
  email?: string | null;
  mail_type?: string | null;
  engine?: string | null;
  group_id?: string | null;
  auto_group: boolean;
  auto_group_size: number;
}

export function fetchTasks(limit = 500): Promise<TasksResponse> {
  return api<TasksResponse>(`/api/tasks?limit=${limit}`);
}

export function startJob(payload: StartJobPayload) {
  return api("/api/job/start", { method: "POST", body: jsonBody(payload) });
}

export function stopJob() {
  return api("/api/job/stop", { method: "POST", body: "{}" });
}

export function pauseJob() {
  return api("/api/job/pause", { method: "POST", body: "{}" });
}

export function resumeJob() {
  return api("/api/job/resume", { method: "POST", body: "{}" });
}

export function retryFailedJobs() {
  return api("/api/job/retry-failed", { method: "POST", body: "{}" });
}

export function deleteTasks(indexes: string[], deleteAll = false) {
  return api("/api/tasks/delete", { method: "POST", body: jsonBody({ indexes, delete_all: deleteAll }) });
}
