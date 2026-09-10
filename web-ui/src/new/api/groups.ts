/* --- 账号分组 API --- */
import { api, apiUrl, jsonBody } from "./client";

export interface AccountGroup {
  id: string;
  name: string;
  count?: number;
  is_default?: boolean;
}

export function fetchGroups(): Promise<{ items: AccountGroup[] }> {
  return api("/api/account-groups");
}

export function createGroup(name: string) {
  return api<{ group: AccountGroup }>("/api/account-groups", {
    method: "POST",
    body: jsonBody({ name }),
  });
}

export function renameGroup(id: string, name: string) {
  return api<{ group: AccountGroup }>(`/api/account-groups/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: jsonBody({ name }),
  });
}

export function deleteGroup(id: string) {
  return api<{ fallback_group_id: string; moved: number }>(
    `/api/account-groups/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
}

export function moveAccountsToGroup(groupId: string, keys: string[], deleteAll = false) {
  return api<{ changed: number; count: number }>("/api/accounts/group", {
    method: "POST",
    body: jsonBody({ group_id: groupId, keys, delete_all: deleteAll }),
  });
}

/** 分组凭据导出（GET 流式下载） */
export function exportGroupCredentials(groupId: string) {
  const anchor = document.createElement("a");
  anchor.href = apiUrl(`/api/account-groups/${encodeURIComponent(groupId)}/export-credentials`);
  anchor.download = "";
  anchor.click();
}
