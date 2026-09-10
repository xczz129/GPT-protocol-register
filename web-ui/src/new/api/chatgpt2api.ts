/* --- chatgpt2api 集成 API：配置读写、连接测试、分组上传 --- */
import { api, jsonBody } from "./client";

export interface Chatgpt2ApiConfig {
  enabled: boolean;
  url: string;
  key: string;
  key_masked: string;
  has_key: boolean;
  auto_upload: boolean;
}

export async function fetchChatgpt2Api(): Promise<Chatgpt2ApiConfig> {
  const data = await api<{ chatgpt2api: Chatgpt2ApiConfig }>("/api/chatgpt2api");
  return data.chatgpt2api;
}

export async function saveChatgpt2Api(payload: { enabled: boolean; url: string; key: string; auto_upload: boolean }): Promise<Chatgpt2ApiConfig> {
  const data = await api<{ chatgpt2api: Chatgpt2ApiConfig }>("/api/chatgpt2api", {
    method: "PUT",
    body: jsonBody(payload),
  });
  return data.chatgpt2api;
}

export function testChatgpt2Api(url: string, key: string): Promise<{ ok: boolean; message: string; url?: string }> {
  return api("/api/chatgpt2api/test", {
    method: "POST",
    body: jsonBody({ url, key }),
  });
}

export function uploadAllChatgpt2Api(url: string, key: string, groupId: string): Promise<{ count?: number; message?: string }> {
  return api("/api/chatgpt2api/upload-all", {
    method: "POST",
    body: jsonBody({ url, key, group_id: groupId || null }),
  });
}
