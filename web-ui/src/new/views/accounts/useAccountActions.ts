/* --- 账号行操作与展示辅助：复制凭据字段 / AT / Session / 2FA 实时验证码、
   代理位置标签、状态与套餐颜色映射（表格与卡片共用） --- */
import { computed, reactive, ref } from "vue";
import { fetchAccessToken, fetchCredential, type Account, type CredentialField } from "../../api/accounts";
import { useCopy } from "../../composables/useCopy";
import { useUiStore } from "../../stores/ui";

/* --- 2FA 实时验证码缓存：delete_key -> { code, expiresAt, error, loading } --- */
interface TotpEntry {
  code: string;
  expiresAt: number;
  error: string;
  loading: boolean;
}
const totpMap = reactive<Record<string, TotpEntry>>({});
const totpNow = ref(Math.floor(Date.now() / 1000));
let totpTimer: ReturnType<typeof setInterval> | null = null;

function ensureTotpTimer() {
  if (totpTimer) return;
  totpTimer = setInterval(() => {
    totpNow.value = Math.floor(Date.now() / 1000);
  }, 1000);
}

export function useAccountActions() {
  const ui = useUiStore();
  const { copiedKey, copyText } = useCopy();
  ensureTotpTimer();

  /** 复制完整凭据行：email----password----totp_secret（缺失字段自动跳过） */
  async function copyCredential(account: Account) {
    try {
      const parts = [account.email];
      if (account.has_password) {
        const { value } = await fetchCredential(account.delete_key, "password") as { value?: string };
        parts.push(value || "");
      }
      if (account.has_totp) {
        const { value } = await fetchCredential(account.delete_key, "totp_secret") as { value?: string };
        parts.push(value || "");
      }
      const ok = await copyText(parts.join("----"), `cred-${account.delete_key}`);
      ui.notify(ok ? "凭据已复制" : "复制失败", ok ? "success" : "error", 1800);
    } catch (error) {
      ui.notify(error instanceof Error ? error.message : "读取凭据失败", "error");
    }
  }

  /** 复制单个凭据字段：password / totp_secret / refresh_token / session_token */
  async function copyField(account: Account, field: CredentialField) {
    const labels: Record<string, string> = {
      password: "密码",
      totp_secret: "2FA 密钥",
      refresh_token: "Refresh Token",
      session_token: "Session Token",
    };
    try {
      const { value } = await fetchCredential(account.delete_key, field) as { value?: string };
      if (!value) {
        ui.notify(`该账号没有${labels[field] || field}`, "warning");
        return;
      }
      const ok = await copyText(value, `${field}-${account.delete_key}`);
      ui.notify(ok ? `${labels[field] || field}已复制` : "复制失败", ok ? "success" : "error", 1800);
    } catch (error) {
      ui.notify(error instanceof Error ? error.message : "读取失败", "error");
    }
  }

  /** 复制邮箱地址（点击邮箱即复制） */
  async function copyEmail(account: Account) {
    const ok = await copyText(account.email, `email-${account.delete_key}`);
    ui.notify(ok ? "邮箱已复制" : "复制失败", ok ? "success" : "error", 1500);
  }

  /** 复制 Account ID */
  async function copyAccountId(account: Account) {
    if (!account.account_id) {
      ui.notify("该账号没有 Account ID", "warning");
      return;
    }
    const ok = await copyText(account.account_id, `accid-${account.delete_key}`);
    ui.notify(ok ? "Account ID 已复制" : "复制失败", ok ? "success" : "error", 1500);
  }

  async function copyAccessToken(account: Account) {
    try {
      const { access_token } = await fetchAccessToken(account.delete_key);
      const ok = await copyText(access_token, `at-${account.delete_key}`);
      ui.notify(ok ? "Access Token 已复制" : "复制失败", ok ? "success" : "error", 1800);
    } catch (error) {
      ui.notify(error instanceof Error ? error.message : "该账号没有可复制的 AT", "error");
    }
  }

  async function copySessionToken(account: Account) {
    await copyField(account, "session_token");
  }

  /* --- 2FA 实时验证码：点击获取并复制，有效期内直接复制缓存 --- */
  function totpEntry(key: string): TotpEntry | undefined {
    const entry = totpMap[key];
    if (!entry) return undefined;
    if (entry.code && entry.expiresAt <= totpNow.value) {
      return { ...entry, code: "" };
    }
    return entry;
  }

  async function fetchTotp(account: Account): Promise<TotpEntry | undefined> {
    const key = account.delete_key;
    totpMap[key] = { code: "", expiresAt: 0, error: "", loading: true };
    try {
      const result = await fetchCredential(key, "totp_code") as { value?: string; expires_at?: number };
      if (!result.value) throw new Error("该账号没有已保存的 TOTP 密钥");
      totpMap[key] = {
        code: String(result.value),
        expiresAt: Number(result.expires_at) || totpNow.value + 30,
        error: "",
        loading: false,
      };
    } catch (error) {
      totpMap[key] = {
        code: "",
        expiresAt: 0,
        error: error instanceof Error ? error.message : "获取失败",
        loading: false,
      };
    }
    return totpMap[key];
  }

  async function handleTotpClick(account: Account) {
    const key = account.delete_key;
    const cached = totpEntry(key);
    if (cached?.code) {
      const ok = await copyText(cached.code, `totp_code-${key}`);
      ui.notify(ok ? "2FA 验证码已复制" : "复制失败", ok ? "success" : "error", 1500);
      return;
    }
    if (cached?.loading) return;
    const fresh = await fetchTotp(account);
    if (fresh?.code) {
      const ok = await copyText(fresh.code, `totp_code-${key}`);
      ui.notify(ok ? "2FA 验证码已复制" : "复制失败", ok ? "success" : "error", 1500);
    } else if (fresh?.error) {
      ui.notify(fresh.error, "error");
    }
  }

  function totpDisplay(account: Account): string {
    const entry = totpEntry(account.delete_key);
    if (entry?.loading) return "…";
    if (entry?.code) {
      const left = Math.max(0, entry.expiresAt - totpNow.value);
      return `${entry.code} · ${left}s`;
    }
    return "获取验证码";
  }

  return {
    copiedKey, copyCredential, copyEmail, copyAccountId, copyField, copyAccessToken, copySessionToken,
    totpEntry, totpDisplay, handleTotpClick,
  };
}

/* --- 代理位置标签：空代理显示"本地·美国"，否则解析国家码并本地化 --- */
let regionNames: Intl.DisplayNames | null = null;

function countryFromProxyUrl(value: string): string {
  let decoded = String(value || "");
  try { decoded = decodeURIComponent(decoded); } catch { /* keep raw */ }
  const patterns = [
    /(?:^|[_-])g[-_=]?([a-z]{2})(?=[_:@/?&-]|$)/i,
    /(?:region|country|countrycode|cc)[-_=:/]?([a-z]{2})(?=[_:@/?&-]|$)/i,
    /[?&](?:region|country|country_code|cc)=([a-z]{2})(?:[&#]|$)/i,
  ];
  for (const pattern of patterns) {
    const match = decoded.match(pattern);
    if (match) return match[1].toUpperCase();
  }
  return "";
}

export function proxyDisplay(value: string): { country: string; label: string; local: boolean } {
  if (!String(value || "").trim()) return { country: "US", label: "本地·美国", local: true };
  const country = countryFromProxyUrl(value);
  if (!country) return { country: "", label: "未识别", local: false };
  let label = "";
  try {
    regionNames ||= new Intl.DisplayNames(["zh-CN"], { type: "region" });
    label = regionNames.of(country) || "";
  } catch { /* ignore */ }
  return { country, label: label || country, local: false };
}

/* --- 套餐颜色映射 --- */
export function planChip(account: Account): { text: string; color: string; bg: string } {
  const plan = (account.plan_type || "free").toLowerCase();
  switch (plan) {
    case "plus":
      return { text: "Plus", color: "#2563eb", bg: "rgba(37, 99, 235, 0.12)" };
    case "pro":
      return { text: "Pro", color: "#d97706", bg: "rgba(217, 119, 6, 0.14)" };
    case "team":
      return { text: "Team", color: "#7c3aed", bg: "rgba(124, 58, 237, 0.13)" };
    case "business":
      return { text: "Business", color: "#4f46e5", bg: "rgba(79, 70, 229, 0.12)" };
    case "enterprise":
      return { text: "Enterprise", color: "#e11d48", bg: "rgba(225, 29, 72, 0.12)" };
    default:
      return plan === "free"
        ? { text: "Free", color: "#64748b", bg: "rgba(100, 116, 139, 0.12)" }
        : { text: plan.charAt(0).toUpperCase() + plan.slice(1), color: "#64748b", bg: "rgba(100, 116, 139, 0.12)" };
  }
}

/* --- 注册方式徽标：协议注册（蓝）与浏览器注册（紫）一眼可辨 --- */
export function methodChip(account: Account): { text: string; cls: string } {
  if (account.registration_method === "protocol") return { text: "协议注册", cls: "is-protocol" };
  if (account.registration_method === "browser") return { text: "浏览器注册", cls: "is-browser" };
  return { text: account.registration_method || "未知方式", cls: "is-unknown" };
}

/* --- 接码状态：持有 RT = 已接码（绿），否则未接码（橙） --- */
export function smsChip(account: Account): { text: string; kind: "success" | "warning" } {
  return account.has_refresh_token
    ? { text: "已接码", kind: "success" }
    : { text: "未接码", kind: "warning" };
}

/* --- 状态文案与样式映射（表格/卡片共用）
   后端值域：alive / limited / invalid / reauth_required / error / unknown（与旧版一致） --- */
export function healthChip(account: Account): { text: string; kind: "success" | "warning" | "error" | "info" | "default" } {
  if (account.is_deactivated) return { text: "已封号", kind: "default" };
  switch (account.health_status) {
    case "alive": return { text: "可用", kind: "success" };
    case "limited": return { text: "限流", kind: "warning" };
    case "invalid": return { text: "失效", kind: "error" };
    case "reauth_required": return { text: "待续期", kind: "warning" };
    case "error": return { text: "异常", kind: "error" };
    default: return { text: "未测活", kind: "info" };
  }
}

export function trialChip(account: Account): { text: string; kind: "success" | "warning" | "error" | "info" | "default" } {
  switch (account.trial_eligibility_status) {
    case "eligible": return { text: account.trial_eligibility_label || "可试用", kind: "success" };
    case "discount": return { text: "有折扣优惠", kind: "warning" };
    case "ineligible": return { text: "不符合", kind: "default" };
    case "error": case "invalid": return { text: "检测失败", kind: "error" };
    case "no_at": return { text: "无 AT", kind: "default" };
    default: return { text: "待检测", kind: "info" };
  }
}

export function formatTime(value: string | undefined): string {
  if (!value) return "-";
  const text = String(value);
  return text.length > 16 ? text.slice(0, 16) : text;
}

/** 紧凑时间短格式：9/10 08:16 */
export function formatShortTime(value: string | undefined): string {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value).slice(5, 16);
  const m = date.getMonth() + 1;
  const d = date.getDate();
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${m}/${d} ${hh}:${mm}`;
}

/** 最近失效时间：last_invalid_at 优先，测活失败时回退 health_checked_at */
export function lastInvalidAt(account: Account): string {
  const direct = String((account as Record<string, unknown>).last_invalid_at || "");
  if (direct) return direct;
  if (account.health_status === "invalid") return account.health_checked_at || "";
  return "";
}

export const totpNowRef = computed(() => totpNow.value);
