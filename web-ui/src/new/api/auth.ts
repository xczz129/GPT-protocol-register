/* --- 认证接口 --- */
import { api, jsonBody } from "./client";
import type { AuthStatus } from "./types";

export function fetchAuthStatus(): Promise<AuthStatus> {
  return api<AuthStatus>("/api/auth/status", { skipAuthRedirect: true });
}

export function logout(): Promise<unknown> {
  return api("/api/auth/logout", { method: "POST", body: jsonBody({}) });
}

export function changePassword(currentPassword: string, newPassword: string): Promise<unknown> {
  return api("/api/auth/change-password", {
    method: "POST",
    body: jsonBody({ current_password: currentPassword, new_password: newPassword }),
  });
}

export function revokeSessions(): Promise<unknown> {
  return api("/api/auth/revoke-sessions", { method: "POST", body: jsonBody({}) });
}
