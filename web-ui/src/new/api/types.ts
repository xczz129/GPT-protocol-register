/* --- 共享 API 类型 --- */

export interface AuthStatus {
  authenticated: boolean;
  username: string;
}

export interface MailStatus {
  total?: number;
  available?: number;
  split_available?: number;
  providers?: MailProviderStatus[];
}

export interface MailProviderStatus {
  type: string;
  register_child?: boolean;
  total?: number;
  available?: number;
}

export interface AccountsStatus {
  total?: number;
  usable?: number;
  deactivated?: number;
}

export interface JobStatus {
  running?: boolean;
  paused?: boolean;
  success_count?: number;
  fail_count?: number;
  total?: number;
  current?: number;
}

export interface ProxyStatus {
  enabled?: boolean;
  pool_count?: number;
}

/** GET /api/status 聚合响应（字段按后端实际返回渐进补充） */
export interface PanelStatus {
  mail?: MailStatus;
  accounts?: AccountsStatus;
  accounts_count?: number;
  job?: JobStatus;
  proxy?: ProxyStatus;
  [key: string]: unknown;
}
