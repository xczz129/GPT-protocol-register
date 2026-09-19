interface TaskPresentation {
  email?: string;
  status?: string;
  skip_reason?: string;
  error?: string;
  phase?: string;
  registration_status?: string;
  sms_status?: string;
  stage?: string;
  message?: string;
  attempt?: number;
  max_phone_attempts?: number;
  wait_deadline?: number | null;
  steps?: { text?: string; status?: string }[];
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function status(task: TaskPresentation): string {
  return text(task.status).toLowerCase();
}

export function taskSmsLabel(task: TaskPresentation): string {
  if (task.phase !== "sms" && !task.sms_status) return "";
  const outcome = text(task.sms_status).toLowerCase();
  const terminal: Record<string, string> = { success: "接码完成", failed: "接码失败", cancelled: "接码已取消", interrupted: "接码已中断" };
  if (terminal[outcome]) return terminal[outcome];
  if (["success", "failed", "cancelled"].includes(status(task))) return terminal[status(task)] || "";
  const stages: Record<string, string> = {
    pending: "等待接码", queued: "等待接码", login: "接码登录", password: "验证密码", mfa: "二次认证",
    acquiring: "获取号码", phone: "提交号码", waiting_sms: "等待短信", verifying_sms: "验证短信",
    workspace: "选择工作区", token: "保存 RT", mfa_retry: "等待认证窗口", relay_sync: "同步中转",
    syncing: "同步中转", relay_check: "检查中转", rate_limit_wait: "授权限流等待", batch_rate_limit_wait: "批次限流等待",
  };
  return stages[text(task.stage)] || (outcome === "pending" ? "等待接码" : "正在接码");
}

export function taskSmsNeedsAttention(task: TaskPresentation): boolean {
  return task.registration_status === "success"
    && (task.phase === "sms" || !!task.sms_status)
    && (["failed", "cancelled", "interrupted"].includes(text(task.sms_status)) || ["failed", "cancelled"].includes(status(task)));
}

export function taskSmsProgress(task: TaskPresentation, nowEpoch: number): string {
  if ((task.phase !== "sms" && !task.sms_status) || status(task) !== "running") return "";
  const parts: string[] = [];
  if (Number.isSafeInteger(task.attempt) && Number(task.attempt) > 0) {
    const limit = task.max_phone_attempts === 0 ? "不限" : Number.isSafeInteger(task.max_phone_attempts) && Number(task.max_phone_attempts) > 0 ? task.max_phone_attempts : "—";
    parts.push(`取号 ${task.attempt} / ${limit} 次`);
  }
  if (typeof task.wait_deadline === "number" && Number.isFinite(task.wait_deadline) && task.wait_deadline > 0 && ["waiting_sms", "rate_limit_wait", "batch_rate_limit_wait"].includes(task.stage || "")) {
    const remaining = Math.max(0, Math.ceil(task.wait_deadline - nowEpoch));
    const waiting = task.stage === "batch_rate_limit_wait" ? "批次冷却" : task.stage === "rate_limit_wait" ? "授权冷却" : "等码";
    parts.push(remaining ? `${waiting}剩余 ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}` : `${waiting}时间已到，正在更新状态`);
  }
  return parts.join(" · ");
}

export function taskFailureDetail(task: TaskPresentation): string {
  if (!["failed", "cancelled", "skipped"].includes(status(task))) return "";
  if (text(task.error)) return text(task.error);
  // Some historical cards recorded errors only in their step log.
  const steps = task.steps || [];
  for (let index = steps.length - 1; index >= 0; index--) {
    if (text(steps[index].status) === "failed" && text(steps[index].text)) {
      return text(steps[index].text);
    }
  }
  return "";
}

export function isVisibleTask(task: TaskPresentation): boolean {
  // An unallocated slot that was cancelled adds no account to the task list.
  // Keep assigned accounts and actual failed allocations visible.
  if (!text(task.email) && status(task) === "cancelled") return false;
  if (text(task.email) || taskFailureDetail(task)) return true;
  return !(status(task) === "skipped"
    && text(task.skip_reason) === "parent_not_registered");
}

export function taskEmailLabel(task: TaskPresentation): string {
  if (text(task.email)) return text(task.email);
  const labels: Record<string, string> = {
    queued: "等待分配邮箱",
    running: "正在分配邮箱",
    failed: "邮箱分配失败",
    cancelled: "未分配邮箱（已取消）",
    skipped: "未分配邮箱（已跳过）",
    success: "未记录邮箱",
  };
  return labels[status(task)] || "未分配邮箱";
}
