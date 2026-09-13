/* --- 邮箱池 API：导入/按需购买配置/列表/删除/状态/读信 + Maddy 域名 + Fastmail 池 --- */
import { api, jsonBody } from "./client";

export interface Mailbox {
  email: string;
  provider_type: string;
  mode?: string;
  login_address?: string;
  state?: string;
  reason?: string;
  registered?: boolean;
  [key: string]: unknown;
}

export interface MailSaveResult {
  ok?: boolean;
  added?: number;
  updated?: number;
  unchanged?: number;
  removed?: number;
  changed?: number;
  total?: number;
  mail?: unknown;
  [key: string]: unknown;
}

export interface MailboxMessage {
  subject?: string;
  sender?: string;
  received_at?: string;
  code?: string;
  text?: string;
  preview?: string;
  [key: string]: unknown;
}

/* --- 列表 --- */
export async function fetchMailboxes(limit = 2000): Promise<Mailbox[]> {
  const data = await api<{ items?: Mailbox[] } | Mailbox[]>(`/api/mailboxes/list?provider_type=auto&limit=${limit}`);
  return Array.isArray(data) ? data : data.items || [];
}

/* --- 导入 / 保存配置（五种邮箱渠道共用 PUT /api/mailboxes） --- */
export function saveMailboxes(payload: Record<string, unknown>): Promise<MailSaveResult> {
  return api<MailSaveResult>("/api/mailboxes", { method: "PUT", body: jsonBody(payload) });
}

/* --- 删除 --- */
export function deleteMailboxes(items: { email: string; provider_type: string }[]): Promise<MailSaveResult> {
  return api<MailSaveResult>("/api/mailboxes/delete", {
    method: "POST",
    body: jsonBody({ items, delete_all: false, provider_type: "auto" }),
  });
}

/* --- 状态操作（释放占用/冷却等） --- */
export function updateMailboxState(emails: string[], action: string, states: string[] = []): Promise<MailSaveResult> {
  return api<MailSaveResult>("/api/mailboxes/state", {
    method: "POST",
    body: jsonBody({ emails, action, states }),
  });
}

/* --- 读取最近邮件 --- */
export function fetchMailboxMessages(email: string, providerType: string, limit = 10) {
  return api<{ ok?: boolean; error?: string; items?: MailboxMessage[] }>("/api/mailbox/messages", {
    method: "POST",
    body: jsonBody({ email, provider_type: providerType, limit }),
  });
}

/* --- Maddy WebUI --- */
export function fetchMaddyDomains(apiBase: string, apiKey?: string) {
  const payload: Record<string, unknown> = { api_base: apiBase };
  if (apiKey) payload.api_key = apiKey;
  return api<{ ok?: boolean; items?: string[]; default_domain?: string }>("/api/maddy-webui/domains", {
    method: "POST",
    body: jsonBody(payload),
  });
}

/* --- Fastmail 别名池 --- */
export interface FastmailPoolRow {
  email: string;
  available_count: number;
  alias_count: number;
  pool_size: number;
  enabled: boolean;
}

export function fetchFastmailAccounts(apiBase: string, adminKey?: string) {
  const payload: Record<string, unknown> = { api_base: apiBase };
  if (adminKey) payload.admin_key = adminKey;
  return api<{ ok?: boolean; items?: FastmailPoolRow[]; available?: number; total?: number }>("/api/fastmail/accounts", {
    method: "POST",
    body: jsonBody(payload),
  });
}

/* --- 工具：域名列表解析 / 脱敏密钥判断（对齐旧版行为） --- */
export function parseDomainList(text: string): string[] {
  return [...new Set(String(text || "").split(/[\n,;]+/).map((v) => v.trim()).filter(Boolean))];
}

export function isMaskedSecret(value: string): boolean {
  const text = String(value || "").trim();
  return !text || text.includes("…") || text.includes("•") || text.includes("***");
}
