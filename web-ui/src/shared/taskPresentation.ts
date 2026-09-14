interface TaskPresentation {
  email?: string;
  status?: string;
  skip_reason?: string;
  error?: string;
  steps?: { text?: string; status?: string }[];
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function status(task: TaskPresentation): string {
  return text(task.status).toLowerCase();
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
